"""Ren'Py adapter: external injection, no game file is touched.

Ren'Py ships no debug port, so there is nothing to attach to the way the
Electron and RPG Maker adapters attach over CDP. What it does ship is
`RENPY_SEARCHPATH`, an environment variable that appends extra directories to
the loader's search path. A `.rpy` dropped in such a directory is compiled and
run by the engine exactly like one of the game's own scripts, while the game
directory stays untouched.

The injected script replaces `renpy.translation.translate_string`, the function
every displayed string passes through: dialogue, menu choices, and the engine's
own chrome (main menu, save screen, preferences). An unknown string is queued
and drawn in the original language, then repainted when the answer arrives, so
the renderer never waits on a backend. The game also polls the bridge for mode
changes, which is how the app's hotkey reaches the injected script.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..segment import Segment
from .base import Adapter

#: The script written into the profile's mod directory. Kept as a string, not
#: a data file, so a PyInstaller build cannot lose it.
INJECT_SCRIPT = r'''# Written by GameTranslator at run time. Loaded through RENPY_SEARCHPATH;
# nothing here lives inside the game directory.
#
# One hook does the translating: renpy.translation.translate_string, which
# every displayed string goes through (say lines, menu choices, main menu,
# save screen). It never blocks the renderer. An unknown string is queued,
# drawn as the original, and repainted when the worker thread gets an answer.
#
# A second hook, a periodic callback, carries the two-way chatter with the
# app: it polls for mode changes (the app's hotkey writes them into the
# bridge) and repaints when new answers land. The same poll carries commands
# back the other way: the app can ask the game to start, advance a line, or
# take a keystroke through Ren'Py's own event queue. That is how the test
# harness drives the game without touching the machine's mouse or keyboard.
init -100 python:
    import json as _gt_json
    import os as _gt_os
    import queue as _gt_queue
    import threading as _gt_threading
    import time as _gt_time
    import types as _gt_types
    import urllib.request as _gt_url

    if _gt_os.environ.get("GT_RENPY_PORT"):
        _gt_port = int(_gt_os.environ["GT_RENPY_PORT"])
        _gt_timeout = float(_gt_os.environ.get("GT_RENPY_TIMEOUT", "12"))
        _gt_workers = max(1, int(_gt_os.environ.get("GT_RENPY_WORKERS", "3")))

        _gt_first_wait = float(_gt_os.environ.get("GT_RENPY_FIRST_WAIT", "1.2"))
        _gt_wait_budget = float(_gt_os.environ.get("GT_RENPY_WAIT_BUDGET", "2.0"))

        # A plain namespace, not store variables: the cache is a view of what
        # the bridge answered, not game state to rewind on rollback.
        _gt_state = _gt_types.SimpleNamespace(
            cache={}, queue=_gt_queue.Queue(), mode="translated", seq=0,
            dirty=False, polled=0.0, events={}, produced=set(),
            budget=_gt_wait_budget, waited=0.0)

        _gt_trace_path = _gt_os.environ.get("GT_RENPY_TRACE")

        def _gt_trace(message):
            if not _gt_trace_path:
                return
            try:
                with open(_gt_trace_path, "a", encoding="utf-8") as handle:
                    handle.write("%.2f %s\n" % (_gt_time.time() % 1000, message))
            except Exception:
                pass

        def _gt_call(path, payload, timeout=2.0):
            body = _gt_json.dumps(payload).encode("utf-8")
            request = _gt_url.Request(
                "http://127.0.0.1:%d%s" % (_gt_port, path),
                data=body,
                headers={"Content-Type": "application/json"})
            with _gt_url.urlopen(request, timeout=timeout) as response:
                return _gt_json.loads(response.read().decode("utf-8"))

        def _gt_work():
            while True:
                text = _gt_state.queue.get()
                if text in _gt_state.cache:
                    continue
                try:
                    value = _gt_call("/translate", {"text": text},
                                     _gt_timeout).get("text") or text
                except Exception:
                    value = text
                _gt_state.cache[text] = value
                _gt_state.dirty = True
                event = _gt_state.events.pop(text, None)
                if event is not None:
                    event.set()

        for _gt_index in range(_gt_workers):
            _gt_threading.Thread(target=_gt_work, name="gt-renpy-%d" % _gt_index,
                                 daemon=True).start()

        def gt_lookup(s, language=None):
            """Stands in for both `_()` and renpy.translation.translate_string.

            The first caller of an unknown string may wait a moment, because a
            say line is substituted once and never relaid out: waiting is the
            only way to show it translated. The wait is capped per string and
            per burst, so a slow backend costs one short hitch, not a freeze.
            """
            if not isinstance(s, str) or not s.strip():
                return s
            if s in _gt_state.produced:
                return s
            hit = _gt_state.cache.get(s)
            if hit is None:
                event = _gt_state.events.get(s)
                if event is None:
                    event = _gt_state.events[s] = _gt_threading.Event()
                    _gt_state.queue.put(s)
                if not renpy.game.context().init_phase:
                    # A fresh burst (nothing waited on lately) gets a full
                    # budget; strings in the same burst share what is left.
                    # The refill has to sit outside the `budget > 0` test:
                    # inside it, a burst that drained the budget to zero could
                    # never refill it again, and every later string (dialogue,
                    # game menu) was drawn in the original language forever.
                    if _gt_time.time() - _gt_state.waited > 0.6:
                        _gt_state.budget = _gt_wait_budget
                    wait = min(_gt_first_wait, _gt_state.budget)
                    if wait > 0:
                        _gt_state.waited = _gt_time.time()
                        landed = event.wait(wait)
                        _gt_state.budget -= wait
                        _gt_trace("wait %r %.2fs landed=%r" % (s[:24], wait, landed))
                hit = _gt_state.cache.get(s)
                if hit is None:
                    return s
            if _gt_state.mode == "original":
                return s
            _gt_state.produced.add(hit)
            if _gt_state.produced.__len__() < 40:
                _gt_trace("hit %r -> %r" % (s[:40], hit[:40]))
            return hit

        import renpy.translation as _gt_translation
        _gt_translation.translate_string = gt_lookup
        # `_()` only marks a string as translatable; returning the translation
        # here changes the argument a screen passes to its text displayable,
        # which is what makes an already-drawn screen update.
        renpy.store._ = gt_lookup
        _gt_trace("patched; store._ is gt_lookup: %r" % (renpy.store._ is gt_lookup,))

        def _gt_repaint():
            _gt_state.dirty = False
            try:
                renpy.exports.restart_interaction()
                _gt_trace("repaint cache=%d mode=%s" % (len(_gt_state.cache), _gt_state.mode))
            except Exception as exc:
                _gt_trace("repaint failed: %r" % (exc,))

        def _gt_start_game():
            # Exactly what the main menu's Start button does.
            renpy.jump_out_of_context("start")

        def _gt_run_command(command):
            name = str(command.get("cmd") or "")
            arg = str(command.get("arg") or "")
            # `event` goes through the engine's own event queue: the keymap
            # sees a real keystroke, but nothing reaches the OS keyboard.
            # `start` must run on the main thread, in an event-handler
            # position, because it leaves the menu context by raising.
            handlers = {
                "event": renpy.queue_event,
                "start": lambda _arg: renpy.invoke_in_main_thread(_gt_start_game),
                "toggle": lambda _arg: _gt_toggle(),
            }
            handler = handlers.get(name)
            if handler is None:
                _gt_trace("unknown command %r" % (command,))
                return
            try:
                handler(arg)
                _gt_trace("command %s %r" % (name, arg))
            except Exception as exc:
                _gt_trace("command %r failed: %r" % (command, exc))

        def _gt_periodic():
            now = _gt_time.time()
            if now - _gt_state.polled > 0.25:
                _gt_state.polled = now
                try:
                    remote = _gt_call("/poll", {}, 1.0)
                except Exception:
                    remote = None
                if remote and remote.get("seq") != _gt_state.seq:
                    _gt_state.seq = remote.get("seq")
                    _gt_state.mode = remote.get("mode") or "translated"
                    _gt_state.dirty = True
                for command in (remote or {}).get("cmds") or []:
                    _gt_run_command(command)
            if _gt_state.dirty:
                _gt_repaint()

        config.periodic_callbacks.append(_gt_periodic)

        def _gt_toggle():
            mode = "original" if _gt_state.mode == "translated" else "translated"
            try:
                answer = _gt_call("/mode", {"mode": mode}, 1.0)
                _gt_state.seq = answer.get("seq", _gt_state.seq)
            except Exception:
                pass
            _gt_state.mode = mode
            _gt_state.dirty = True
            _gt_repaint()

        import renpy.display.behavior as _gt_behavior
        config.underlay.append(
            _gt_behavior.Keymap(**{"ctrl_shift_K_t": _gt_toggle}))

        _gt_snap = _gt_os.environ.get("GT_RENPY_SNAP")
        if _gt_snap:
            _gt_born = _gt_time.time()
            _gt_snap_delays = [float(x) for x in
                               (_gt_os.environ.get("GT_RENPY_SNAP_AT") or "8").split(",") if x.strip()]
            _gt_snap_taken = set()

            def _gt_take_snapshot():
                age = _gt_time.time() - _gt_born
                for index, delay in enumerate(_gt_snap_delays):
                    if index in _gt_snap_taken or age < delay:
                        continue
                    _gt_snap_taken.add(index)
                    target = _gt_snap if len(_gt_snap_delays) == 1 else (
                        "%s_%d" % (_gt_snap.rsplit(".", 1)[0], int(delay)) + "." +
                        (_gt_snap.rsplit(".", 1)[1] if "." in _gt_snap else "png"))
                    try:
                        renpy.screenshot(target)
                    except Exception:
                        pass

            config.periodic_callbacks.append(_gt_take_snapshot)

        _gt_font = _gt_os.environ.get("GT_RENPY_FONT")
        if _gt_font:
            try:
                config.font_replacement_map[("DejaVuSans.ttf", False, False)] = (
                    _gt_font, False, False)
                style.default.font = _gt_font
            except Exception:
                pass
'''

#: Fonts tried in order. `Deng.ttf` (等线) ships with Windows and covers CJK.
FONT_CANDIDATES = ("Deng.ttf", "simhei.ttf", "msyh.ttc", "simsun.ttc")


def _free_port(start: int = 9761) -> int:
    for port in range(start, start + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise OSError("no free port for the renpy bridge")


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (name fixed by http.server)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            payload = {}
        adapter = self.server.adapter  # type: ignore[attr-defined]
        if self.path == "/poll":
            self._reply(adapter.poll_state())
            return
        if self.path == "/mode":
            self._reply(adapter.report_mode(str(payload.get("mode") or "")))
            return
        text = str(payload.get("text") or "")
        adapter.log.debug("renpy asks: %r", text[:120])
        value, hit = adapter.translate(text)
        self._reply({"text": value, "hit": hit})

    def _reply(self, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # The game gave up waiting; nothing to do.
            self.server.adapter.log.debug(  # type: ignore[attr-defined]
                "renpy closed the connection before the answer landed")

    def log_message(self, *_args) -> None:
        pass


class RenPyAdapter(Adapter):
    name = "renpy"

    def __init__(self, profile, log=None):
        super().__init__(profile, log)
        cfg = profile.adapter_config
        self.exe = cfg.get("exe")
        # SDK projects have no launcher of their own: the engine is started
        # with the project folder as its argument.
        self.args = list(cfg.get("args") or [])
        self.workdir = cfg.get("workdir")
        self.port = int(cfg.get("port") or _free_port())
        self.sync_timeout = float(cfg.get("syncTimeout") or 12.0)
        self.mod_dir = profile.dir / "renpy_mod"
        self.proc: subprocess.Popen | None = None
        self.server: ThreadingHTTPServer | None = None
        self.pipeline = None
        self.counts = {"requests": 0, "hits": 0, "misses": 0}
        # Mode lives here rather than in the injected script so the app owns
        # it: hotkey or GUI writes it, the game polls it back. `seq` makes the
        # poll idempotent, so a missed poll is not a missed toggle.
        self.mode = "translated"
        self.seq = 0
        #: Commands waiting for the game's next poll. The test harness drives
        #: navigation through these instead of clicking: a background window
        #: cannot be clicked anyway, and real clicks land wherever the author
        #: happens to have the cursor.
        self.cmds: list[dict] = []
        self._cmd_lock = threading.Lock()
        #: Set by the runtime to mirror in-game toggles back into its own state.
        self.on_mode_change = None

    # -- wiring ------------------------------------------------------------

    def attach_pipeline(self, pipeline) -> None:
        """Called by the runtime once its pipeline exists."""
        self.pipeline = pipeline

    def translate(self, text: str) -> tuple[str, bool]:
        self.counts["requests"] += 1
        value = None
        if self.pipeline is not None:
            try:
                value = self.pipeline.translate_text(text, timeout=self.sync_timeout)
            except Exception:
                self.log.exception("renpy bridge lookup failed")
        if value:
            self.counts["hits"] += 1
            return value, True
        self.counts["misses"] += 1
        return text, False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._prepare_mod()
        self._start_server()
        self._launch()

    def _prepare_mod(self) -> None:
        self.mod_dir.mkdir(parents=True, exist_ok=True)
        (self.mod_dir / "zz_gt_inject.rpy").write_text(INJECT_SCRIPT, encoding="utf-8")
        target = self.mod_dir / "gt-font.ttf"
        if not target.exists():
            for name in FONT_CANDIDATES:
                source = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
                if source.exists():
                    shutil.copyfile(source, target)
                    break
            else:
                self.log.warning("no CJK font found; translations may render as boxes")

    def _start_server(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        server.adapter = self  # type: ignore[attr-defined]
        server.daemon_threads = True
        self.server = server
        threading.Thread(target=server.serve_forever, name="gt-renpy-bridge",
                         daemon=True).start()
        self.log.info("renpy bridge listening on 127.0.0.1:%d", self.port)

    def _launch(self) -> None:
        if not self.exe:
            raise RuntimeError("profile has no adapterConfig.exe")
        env = dict(os.environ)
        env["RENPY_SEARCHPATH"] = str(self.mod_dir)
        env["GT_RENPY_PORT"] = str(self.port)
        env["GT_RENPY_TIMEOUT"] = str(self.sync_timeout)
        env["GT_RENPY_WORKERS"] = str(int(self.profile.adapter_config.get("workers") or 3))
        if self.profile.adapter_config.get("firstWait") is not None:
            env["GT_RENPY_FIRST_WAIT"] = str(self.profile.adapter_config["firstWait"])
        if self.profile.adapter_config.get("waitBudget") is not None:
            env["GT_RENPY_WAIT_BUDGET"] = str(self.profile.adapter_config["waitBudget"])
        trace = self.profile.adapter_config.get("trace")
        if trace:
            env["GT_RENPY_TRACE"] = str(trace)
        font = self.mod_dir / "gt-font.ttf"
        if font.exists():
            env["GT_RENPY_FONT"] = "gt-font.ttf"
        snapshot = self.profile.adapter_config.get("snapshot")
        if snapshot:
            env["GT_RENPY_SNAP"] = str(snapshot)
            snap_at = self.profile.adapter_config.get("snapshotAt")
            if snap_at:
                env["GT_RENPY_SNAP_AT"] = str(snap_at)
        workdir = Path(self.workdir) if self.workdir else Path(self.exe).parent
        self.log.info("launching %s %s with RENPY_SEARCHPATH=%s",
                      self.exe, " ".join(self.args), self.mod_dir)
        self.proc = subprocess.Popen([self.exe, *self.args], cwd=str(workdir), env=env)

    def poll(self, timeout: float = 0.5) -> list[Segment]:
        # Translations are pulled by the game through the bridge; there is no
        # push channel to report from.
        time.sleep(min(timeout, 0.5))
        return []

    def apply(self, translations: dict[str, str]) -> int:
        return 0

    def set_mode(self, mode: str) -> None:
        # Just record it; the game's periodic poll picks the change up within
        # a quarter second and repaints itself.
        mode = "original" if mode == "original" else "translated"
        if mode == self.mode:
            return
        self.mode = mode
        self.seq += 1
        self.log.info("renpy mode -> %s (seq %d)", self.mode, self.seq)

    def send_command(self, cmd: str, arg: str = "") -> None:
        """Ask the game to do something on its next poll.

        `cmd` is one of `start`, `event` (arg: a keymap name such as
        `dismiss`), or `toggle`. The game runs it in its own event loop, so
        the machine's mouse and keyboard are never involved.
        """
        with self._cmd_lock:
            self.cmds.append({"cmd": cmd, "arg": arg})
        self.log.debug("renpy command queued: %s %r", cmd, arg)

    def poll_state(self) -> dict:
        with self._cmd_lock:
            cmds, self.cmds = self.cmds, []
        return {"mode": self.mode, "seq": self.seq, "cmds": cmds}

    def report_mode(self, mode: str) -> dict:
        """The game toggled itself (in-game hotkey); adopt its answer."""
        mode = "original" if mode == "original" else "translated"
        if mode != self.mode:
            self.mode = mode
            self.seq += 1
            self.log.info("renpy toggled in-game -> %s (seq %d)", self.mode, self.seq)
            hook = self.on_mode_change
            if callable(hook):
                try:
                    hook(mode)
                except Exception:
                    self.log.exception("mode change hook failed")
        return self.poll_state()

    def alive(self) -> bool:
        if self.proc is None:
            return False
        return self.proc.poll() is None

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.proc is not None and self.proc.poll() is None:
            self._kill_tree()
            self.proc = None

    def _kill_tree(self) -> None:
        """End the game by PID, and anything it started underneath it.

        The PID is used, never the image name: `taskkill /IM renpy.exe` would
        also hit a game the author is playing, and `/IM` on a common host like
        pythonw.exe would hit unrelated programs.
        """
        proc = self.proc
        if proc is None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True, check=False)
            else:
                proc.kill()
        except OSError:
            self.log.exception("could not stop renpy")
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.log.warning("renpy %s survived taskkill", proc.pid)

    def stats(self) -> dict:
        return dict(self.counts, port=self.port, exe=self.exe, alive=self.alive(),
                    mode=self.mode, seq=self.seq)
