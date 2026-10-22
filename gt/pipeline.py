"""Resolution pipeline: cache -> memory -> backend chain.

Split is deliberate. The first two stages are synchronous and instant, which is
what makes the common case (text the community already translated) feel like
the game itself is in Chinese. Only misses reach a model, and those run on a
worker thread so the UI never waits on inference.

Validation happens here, not in the adapters: a translation that loses a
placeholder is worse than no translation, because the game may render
`Missing string [name]` or crash on a format call. Rejected output is dropped
and the original stays on screen.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections.abc import Callable

from .backends import Backend, Request
from .cache import TranslationCache
from .glossary import Glossary
from .placeholders import problems
from .segment import Segment
from .text import is_translatable, normalize
from .tm import TranslationMemory


class Pipeline:
    def __init__(
        self,
        cache: TranslationCache,
        tm: TranslationMemory,
        glossary: Glossary,
        backends: list[Backend],
        on_ready: Callable[[dict[str, str]], None],
        log: logging.Logger | None = None,
        strip_pattern: str | None = None,
        workers: int = 1,
        use_tm: bool = True,
    ):
        self.cache = cache
        self.tm = tm
        self.glossary = glossary
        self.backends = backends
        self.on_ready = on_ready
        self.log = log or logging.getLogger("gt.pipeline")
        # Memory written from an offline pass (a document the translator
        # worked on) is keyed on plain text, because styling codes did not
        # travel with it. Without a second lookup on the stripped form, every
        # line the engine draws with \C[17] in it misses that memory even
        # though the words are right there.
        self._strip = re.compile(strip_pattern) if strip_pattern else None
        # "Everything through the backend" is a strategy, not an accident:
        # a game whose memory was built from another version may be worse
        # served by the old lines than by a fresh translation.
        self.use_tm = use_tm

        self.stats = {
            "seen": 0,
            "cache_hits": 0,
            "tm_hits": 0,
            "queued": 0,
            "backend_hits": 0,
            "rejected": 0,
        }

        self._queue: queue.Queue[Request | None] = queue.Queue()
        self._inflight: set[str] = set()
        # Keys the worker has already finished with, hit or miss. Without this,
        # a second screen fragment arriving while the first result lands would
        # be queued again - the inflight set is cleared before the caller can
        # notice, which is a real race, not a theoretical one.
        self._settled: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # Remote endpoints (DeepSeek and friends) answer several requests at
        # once; a local single-model server does not. The count comes from the
        # backend config, never from a guess.
        self.workers = max(1, int(workers))
        self._threads: list[threading.Thread] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        for backend in self.backends:
            backend.glossary = self.glossary
            try:
                backend.start()
            except Exception:
                self.log.exception("backend start failed: %s", backend.name)
        self._threads = [
            threading.Thread(target=self._run, name=f"gt-backend-{i}", daemon=True)
            for i in range(self.workers)
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for _ in self._threads:
            self._queue.put(None)
        for thread in self._threads:
            thread.join(timeout=5)
        for backend in self.backends:
            try:
                backend.stop()
            except Exception:
                self.log.exception("backend stop failed: %s", backend.name)

    # -- resolution --------------------------------------------------------

    def register(self, segments: list[Segment]) -> dict[str, str]:
        """Return translations available right now, as {segment_id: text}."""
        ready: dict[str, str] = {}
        for seg in segments:
            self.stats["seen"] += 1
            key = normalize(seg.text)
            if not key or not is_translatable(key):
                continue
            hit, origin = self._lookup(key, seg.text)
            if hit is not None:
                self.stats["cache_hits" if origin == "cache" else "tm_hits"] += 1
                ready[seg.id] = hit
                continue
            self._enqueue(Request(key=key, text=seg.text, kind=seg.kind, where=seg.where))
        return ready

    def translate_text(self, text: str, timeout: float = 8.0) -> str | None:
        """Resolve one string and wait for it, for hosts that cannot redraw.

        The RPG Maker adapter gets the original on screen and repaints when a
        translation lands. Ren'Py's text filter has to return something before
        the line is drawn, so this is the one caller that blocks; the wait is
        bounded and a timeout degrades to the original.
        """
        key = normalize(text)
        if not key or not is_translatable(key):
            return None
        self.stats["seen"] += 1
        hit, origin = self._lookup(key, text)
        if hit is not None:
            self.stats["cache_hits" if origin == "cache" else "tm_hits"] += 1
            return hit
        self._enqueue(Request(key=key, text=text, kind="text", where="renpy"))
        if not self.backends:
            # Nothing will ever fill the cache on a dictionary-only run; do not
            # hold the game on the line for a timeout that cannot produce text.
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = self.cache.get(key)
            if value is not None:
                self.stats["backend_hits"] += 1
                return value
            time.sleep(0.05)
        return None

    def _lookup(self, key: str, text: str) -> tuple[str | None, str]:
        hit, origin = self._probe(key, text)
        if hit is not None:
            return hit, origin
        if self._strip is None:
            return None, ""
        plain = self._strip.sub("", text)
        if plain == text:
            return None, ""
        plain_key = normalize(plain)
        if not plain_key or plain_key == key:
            return None, ""
        hit, origin = self._probe(plain_key, plain)
        if hit is not None:
            self.log.debug("hit %s on stripped key %r", origin, plain_key[:60])
        return hit, origin

    def _probe(self, key: str, text: str) -> tuple[str | None, str]:
        for origin, store in (("cache", self.cache), ("tm", self.tm)):
            if origin == "tm" and not self.use_tm:
                continue
            value = store.get(key) if origin == "cache" else store.lookup(key)
            if value is None:
                continue
            issues = problems(normalize(text), normalize(value))
            if issues:
                # A stale memory entry that cannot be rendered safely is worse
                # than a miss; let it fall through to a fresh translation.
                self.log.debug("reject %s hit %r: %s", origin, key[:60], issues)
                self.stats["rejected"] += 1
                continue
            return value, origin
        return None, ""

    def _enqueue(self, req: Request) -> None:
        if not self.backends:
            return
        with self._lock:
            if len(self._settled) > 50000:
                cutoff = time.time() - 1800
                self._settled = {k: v for k, v in self._settled.items() if v > cutoff}
            if req.key in self._inflight or req.key in self._settled:
                return
            self._inflight.add(req.key)
        self.stats["queued"] += 1
        self._queue.put(req)

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        max_batch = max((b.batch_size for b in self.backends), default=1)
        while not self._stop.is_set():
            try:
                first = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if first is None:
                break
            batch: list[Request] = [first]
            while len(batch) < max_batch:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._stop.set()
                    break
                batch.append(nxt)

            results = self._translate(batch)

            ready: dict[str, str] = {}
            for req in batch:
                with self._lock:
                    self._inflight.discard(req.key)
                    self._settled[req.key] = time.time()
                value = results.get(req.key)
                if value is None:
                    continue
                issues = problems(normalize(req.text), normalize(value))
                if issues:
                    self.stats["rejected"] += 1
                    self.log.debug("reject backend output %r: %s", req.key[:60], issues)
                    continue
                self.cache.put(req.key, req.text, value, "backend")
                self.stats["backend_hits"] += 1
                ready[req.key] = value

            if ready:
                try:
                    self.on_ready(ready)
                except Exception:
                    self.log.exception("on_ready handler failed")

    def _translate(self, batch: list[Request]) -> dict[str, str]:
        pending = batch
        out: dict[str, str] = {}
        for backend in self.backends:
            if not pending:
                break
            try:
                got = backend.translate(pending) or {}
            except Exception:
                self.log.exception("backend %s raised", backend.name)
                continue
            if not got:
                continue
            out.update(got)
            pending = [r for r in pending if r.key not in got]
        return out

    def wait_idle(self, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._inflight:
                    return True
            time.sleep(0.05)
        return False
