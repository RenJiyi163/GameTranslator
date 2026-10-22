"""Runtime loop: wire one adapter to one pipeline and keep them in sync."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from .adapters import REGISTRY as ADAPTERS
from .backends import build_chain
from .cache import TranslationCache
from .config import Profile
from .games import mark_run
from .glossary import Glossary
from .pipeline import Pipeline
from .segment import Segment
from .text import is_translatable, normalize
from .tm import TranslationMemory


class TranslationRuntime:
    def __init__(self, profile: Profile, log: logging.Logger, use_backends: bool = True):
        self.profile = profile
        self.log = log
        self.cache = TranslationCache(profile.cache_db, profile.id)
        self.tm = TranslationMemory(profile.tm_db)
        self.glossary = Glossary.load(profile.glossary_path)
        self.strategy = profile.effective_strategy()
        # "memory" means no model even when a chain is configured; the GUI's
        # per-run "dictionary only" checkbox comes in as use_backends=False.
        self.backends = (build_chain(profile.backend_names(), profile.backend_config, log)
                         if use_backends and self.strategy != "memory" else [])
        # A backend that can serve several requests at once gets the pipeline
        # that many workers; a single-model local server stays at one.
        self.workers = max((getattr(b, "concurrency", 1) for b in self.backends), default=1)

        adapter_cls = ADAPTERS.get(profile.adapter)
        if adapter_cls is None:
            raise SystemExit(f"unknown adapter: {profile.adapter}")
        self.adapter = adapter_cls(profile, log.getChild("adapter"))

        self.pipeline = Pipeline(
            cache=self.cache,
            tm=self.tm,
            glossary=self.glossary,
            backends=self.backends,
            on_ready=self._on_translations,
            log=log.getChild("pipeline"),
            strip_pattern=getattr(self.adapter, "strip_pattern", None),
            workers=self.workers,
            use_tm=self.strategy != "backend_only",
        )

        # Adapters that pull (Ren'Py's in-game filter asks over HTTP) need the
        # pipeline handed over; the push adapters do not implement this.
        bind = getattr(self.adapter, "attach_pipeline", None)
        if callable(bind):
            bind(self.pipeline)

        # A pull adapter can also toggle itself (Ren'Py's in-game hotkey). Let
        # it report back, so the overlay and the next hotkey press agree.
        if hasattr(self.adapter, "on_mode_change"):
            self.adapter.on_mode_change = self._adopt_mode

        # key -> {segment id: first seen}. Needed because several on-screen
        # nodes can share one source string; a single result fills them all.
        self._by_key: dict[str, dict[str, float]] = {}
        self._unmatched: dict[str, dict] = {}
        self._applied = 0
        self._idle_since = time.time()
        # Mirrors what the adapter last painted; the hotkey flips it blind, so
        # the value has to live somewhere both sides agree on.
        self.mode = "translated"
        # Backend results arrive on worker threads, so the maps they touch and
        # the adapters they write through are only safe behind a lock.
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def run(self, duration: float | None = None, stats_every: float = 30.0,
            unmatched_out: Path | None = None, stop_event: threading.Event | None = None) -> int:
        started = time.time()
        mark_run(self.profile)
        self.adapter.start()
        self.pipeline.start()
        last_stats = time.time()
        self.log.info("runtime up: profile=%s adapter=%s strategy=%s backends=%s",
                      self.profile.id, self.profile.adapter, self.strategy,
                      ",".join(b.name for b in self.backends) or "none")
        try:
            while True:
                segments = self.adapter.poll(0.5)
                if segments:
                    self._handle(segments)

                now = time.time()
                if stats_every and now - last_stats >= stats_every:
                    last_stats = now
                    self.log.info("stats %s", json.dumps(self.snapshot(), ensure_ascii=False))

                if duration is not None and now - started >= duration:
                    self.log.info("duration reached, stopping")
                    break
                if stop_event is not None and stop_event.is_set():
                    self.log.info("stop requested, shutting down")
                    break
                if not self.adapter.alive():
                    self.log.warning("adapter is gone, stopping")
                    break
        except KeyboardInterrupt:
            self.log.info("interrupted")
        finally:
            self._shutdown(unmatched_out)
        return 0

    def _shutdown(self, unmatched_out: Path | None) -> None:
        try:
            self.adapter.stop()
        except Exception:
            self.log.exception("adapter stop failed")
        try:
            self.pipeline.stop()
        except Exception:
            self.log.exception("pipeline stop failed")
        # Always keep the list on disk: it is what the export button feeds on
        # after the session is over.
        try:
            self.dump_unmatched(unmatched_out or (self.profile.dir / "unmatched.jsonl"))
        except OSError:
            self.log.exception("could not write the unmatched list")
        self.tm.close()
        self.cache.close()

    # -- work --------------------------------------------------------------

    def _handle(self, segments: list[Segment]) -> None:
        ready = self.pipeline.register(segments)
        now = time.time()
        with self._lock:
            for seg in segments:
                if seg.id in ready:
                    continue
                key = normalize(seg.text)
                if not key:
                    continue
                self._by_key.setdefault(key, {})[seg.id] = now
                self._unmatched.setdefault(key, {"text": seg.text, "kind": seg.kind, "where": seg.where})
            stale = now - 600
            for key in [k for k, ids in self._by_key.items() if all(ts < stale for ts in ids.values())]:
                self._by_key.pop(key, None)
        if ready:
            with self._lock:
                self._applied += self.adapter.apply(ready)

    def unmatched_rows(self) -> list[dict]:
        """Strings seen this session that nothing could translate."""
        with self._lock:
            items = sorted(self._unmatched.items())
        rows: list[dict] = []
        for key, item in items:
            text, refs = self.adapter.export_text(item.get("text", ""))
            if not is_translatable(normalize(text)):
                continue
            rows.append({
                "id": key[:24],
                "kind": item.get("kind", ""),
                "where": item.get("where", ""),
                "raw": item.get("text", ""),
                "text": text,
                "refs": refs,
            })
        return rows

    def dump_unmatched(self, path: Path) -> dict:
        rows = self.unmatched_rows()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.log.info("wrote %d unmatched rows to %s", len(rows), path)
        return {"count": len(rows), "path": str(path)}

    def _on_translations(self, mapping: dict[str, str]) -> None:
        with self._lock:
            out: dict[str, str] = {}
            for key, text in mapping.items():
                ids = self._by_key.pop(key, None)
                if not ids:
                    continue
                self._unmatched.pop(key, None)
                for sid in ids:
                    out[sid] = text
            if out:
                self._applied += self.adapter.apply(out)
        if out:
            self.log.info("applied %d translations (%d source strings)", len(out), len(mapping))

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> dict:
        data = {
            "seen": self.pipeline.stats["seen"],
            "cache_hits": self.pipeline.stats["cache_hits"],
            "tm_hits": self.pipeline.stats["tm_hits"],
            "queued": self.pipeline.stats["queued"],
            "backend_hits": self.pipeline.stats["backend_hits"],
            "rejected": self.pipeline.stats["rejected"],
            "applied": self._applied,
            "pending_keys": len(self._by_key),
            "mode": self.mode,
        }
        if hasattr(self.adapter, "stats"):
            try:
                data["page"] = self.adapter.stats()  # type: ignore[attr-defined]
            except Exception:
                pass
        return data

    def set_mode(self, mode: str) -> str:
        self.mode = "original" if mode == "original" else "translated"
        self.adapter.set_mode(self.mode)
        return self.mode

    def _adopt_mode(self, mode: str) -> None:
        """Called by an adapter the game toggled on its own."""
        self.mode = "original" if mode == "original" else "translated"
        self.log.info("mode now %s (reported by the game)", self.mode)

    def toggle_mode(self) -> str:
        return self.set_mode("original" if self.mode == "translated" else "translated")
