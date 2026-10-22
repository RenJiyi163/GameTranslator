"""Electron adapter over the Chrome DevTools Protocol.

Works with any Electron/Chromium application that can be started with
`--remote-debugging-port=<n>`; nothing here knows which game it is talking to.
The selectors come from the profile, the in-page runtime is generic.

Shape of the conversation:
    page  --__gt_report(json)-->  binding event  -->  queue  -->  poll()
    python --Runtime.evaluate("__GT.apply(...)")--> page
"""

from __future__ import annotations

import json
import logging
import queue
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..config import Profile
from ..segment import Segment
from .base import Adapter

RUNTIME_JS_PATH = Path(__file__).resolve().parent.parent / "js" / "runtime.js"


class CDPError(RuntimeError):
    pass


class CDPClient:
    """Minimal synchronous CDP client: one reader thread, id-matched replies."""

    def __init__(self, ws_url: str, log: logging.Logger, open_timeout: float = 15.0):
        from websockets.sync.client import connect

        self.log = log
        self._next_id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        self._handlers: dict[str, list] = {}
        self._closed = threading.Event()
        # Chromium's devtools endpoint does not answer WebSocket pings, so the
        # library's keepalive would tear the session down every ~20s.
        self.ws = connect(ws_url, max_size=None, open_timeout=open_timeout, ping_interval=None)
        self._reader = threading.Thread(target=self._read_loop, name="gt-cdp-reader", daemon=True)
        self._reader.start()

    # -- plumbing ----------------------------------------------------------

    def on(self, method: str, handler) -> None:
        self._handlers.setdefault(method, []).append(handler)

    def _read_loop(self) -> None:
        while not self._closed.is_set():
            try:
                raw = self.ws.recv()
            except Exception as exc:  # connection closed, protocol error
                if not self._closed.is_set():
                    self.log.debug("cdp reader stopped: %s", exc)
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_id = msg.get("id")
            if msg_id is not None:
                slot = self._pending.pop(msg_id, None)
                if slot is not None:
                    event, box = slot
                    box.append(msg)
                    event.set()
                continue
            method = msg.get("method", "")
            for handler in self._handlers.get(method, []):
                try:
                    handler(msg.get("params") or {})
                except Exception:
                    self.log.exception("cdp handler for %s failed", method)
        self._closed.set()

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        if self._closed.is_set():
            raise CDPError("connection closed")
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            box: list = []
            event = threading.Event()
            self._pending[msg_id] = (event, box)
            try:
                self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            except Exception as exc:
                self._pending.pop(msg_id, None)
                raise CDPError(f"send failed: {exc}") from exc
        if not event.wait(timeout):
            self._pending.pop(msg_id, None)
            raise CDPError(f"timeout waiting for {method}")
        reply = box[0]
        if "error" in reply:
            raise CDPError(f"{method}: {reply['error']}")
        return reply.get("result") or {}

    def evaluate(self, expression: str, timeout: float = 20.0) -> dict:
        return self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": False},
            timeout=timeout,
        )

    def close(self) -> None:
        self._closed.set()
        try:
            self.ws.close()
        except Exception:
            pass


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


class ElectronCDPAdapter(Adapter):
    name = "electron_cdp"

    def __init__(self, profile: Profile, log: logging.Logger | None = None):
        super().__init__(profile, log)
        cfg = profile.adapter_config
        self.exe = cfg.get("exe")
        self.args: list[str] = list(cfg.get("args", []))
        self.port = int(cfg.get("port", 9222))
        self.url_match = cfg.get("urlMatch", "*")
        self.startup_timeout = float(cfg.get("startupTimeout", 60))
        self.attach_only = bool(cfg.get("attachOnly", False))
        self.inject_ready_selector = cfg.get("readySelector")

        # A CDP binding outlives the session that created it, and a second
        # session cannot re-point it. A per-session name guarantees this run
        # gets its own event stream even if the page was injected before.
        self.binding_name = "gt_report_" + secrets.token_hex(4)

        self.proc: subprocess.Popen | None = None
        self.client: CDPClient | None = None
        self.inbox: queue.Queue[list[Segment]] = queue.Queue()
        self._stop = threading.Event()
        self._injected = False
        self._last_retry = 0.0
        self._last_port_check = 0.0
        self._port_alive = True

    # -- setup -------------------------------------------------------------

    def start(self) -> None:
        if self.attach_only and not _port_open(self.port):
            raise CDPError(f"attachOnly set but nothing is listening on port {self.port}")
        if not _port_open(self.port):
            self._launch()
        self._attach(self._wait_for_target())

    def _attach(self, ws_url: str) -> None:
        client = CDPClient(ws_url, self.log)
        client.on("Runtime.bindingCalled", self._on_binding)
        client.on("Runtime.executionContextsCleared", self._on_contexts_cleared)
        client.on("Page.loadEventFired", self._on_load)
        client.call("Runtime.enable")
        client.call("Page.enable")
        client.call("Runtime.addBinding", {"name": self.binding_name})
        self.client = client
        self._inject()
        self.log.info("attached to %s", ws_url)

    def _reconnect(self) -> bool:
        """Re-attach after the devtools socket dropped but the app still runs."""
        now = time.time()
        if now - self._last_retry < 3.0:
            return False
        self._last_retry = now
        try:
            ws_url = self._wait_for_target(timeout=3.0)
            self._attach(ws_url)
            self.log.info("reconnected")
            return True
        except CDPError as exc:
            self.log.debug("reconnect failed: %s", exc)
            return False

    def _launch(self) -> None:
        if not self.exe:
            raise CDPError("profile.adapterConfig.exe is required to launch the app")
        exe = Path(self.exe)
        if not exe.exists():
            raise CDPError(f"executable not found: {exe}")
        args = list(self.args)
        flag = f"--remote-debugging-port={self.port}"
        if not any(a.startswith("--remote-debugging-port") for a in args):
            args.insert(0, flag)
        self.log.info("launching %s %s", exe, " ".join(args))
        self.proc = subprocess.Popen([str(exe), *args], cwd=str(exe.parent))

    def _wait_for_target(self, timeout: float | None = None) -> str:
        deadline = time.time() + (self.startup_timeout if timeout is None else timeout)
        last_err: str = ""
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise CDPError(f"process exited early with code {self.proc.returncode}")
            try:
                targets = self._list_targets()
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                last_err = str(exc)
                time.sleep(0.4)
                continue
            for target in targets:
                if target.get("type") != "page":
                    continue
                url = target.get("url", "")
                if not _match_url(url, self.url_match):
                    continue
                ws_url = target.get("webSocketDebuggerUrl")
                if ws_url:
                    return ws_url
            time.sleep(0.4)
        raise CDPError(f"no debuggable page after {timeout or self.startup_timeout}s ({last_err})")

    def _list_targets(self) -> list[dict]:
        url = f"http://127.0.0.1:{self.port}/json/list"
        with urllib.request.urlopen(url, timeout=2) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _inject(self) -> None:
        if self.client is None:
            return
        config = self.profile.page_config()
        config["reportName"] = self.binding_name
        source = RUNTIME_JS_PATH.read_text(encoding="utf-8")
        source = source.replace("/*__GT_CONFIG__*/ null", json.dumps(config, ensure_ascii=False))
        result = self.client.evaluate(source, timeout=30)
        if result.get("exceptionDetails"):
            raise CDPError(f"runtime injection failed: {result['exceptionDetails']}")
        self._injected = True
        self.log.info("in-page runtime installed (%d selectors)", len(self.profile.selectors))

    # -- events ------------------------------------------------------------

    def _on_binding(self, params: dict) -> None:
        if params.get("name") != self.binding_name:
            return
        payload = params.get("payload") or ""
        try:
            items = json.loads(payload)
        except json.JSONDecodeError:
            self.log.warning("bad report payload: %r", payload[:120])
            return
        segments = [
            Segment(id=item["id"], text=item.get("text", ""), kind=item.get("kind", "text"),
                    where=item.get("where", ""))
            for item in items
            if item.get("text")
        ]
        if segments:
            self.inbox.put(segments)

    def _on_contexts_cleared(self, _params: dict) -> None:
        # The page navigated or reloaded: the runtime died with the old context.
        self._injected = False

    def _on_load(self, _params: dict) -> None:
        if not self._injected and self.client is not None:
            try:
                self.client.call("Runtime.addBinding", {"name": self.binding_name})
                self._inject()
            except CDPError:
                self.log.exception("re-injection after navigation failed")

    # -- adapter contract --------------------------------------------------

    def alive(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        # Attached to something we did not start: the listen socket is the
        # only evidence we have, and probing it costs a syscall.
        now = time.time()
        if now - self._last_port_check > 5.0:
            self._last_port_check = now
            self._port_alive = _port_open(self.port)
        return self._port_alive

    def poll(self, timeout: float = 0.5) -> list[Segment]:
        if self.client is None or self.client._closed.is_set():
            if not self._reconnect():
                return []
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return []

    def apply(self, translations: dict[str, str]) -> int:
        if not translations or self.client is None:
            return 0
        items = [{"id": int(sid), "text": text} for sid, text in translations.items() if str(sid).isdigit()]
        if not items:
            return 0
        literal = _js_string(json.dumps(items, ensure_ascii=False))
        try:
            result = self.client.evaluate(f"window.__GT ? window.__GT.apply({literal}) : 0")
        except CDPError:
            self.log.exception("apply failed")
            return 0
        return int((result.get("result") or {}).get("value") or 0)

    def set_mode(self, mode: str) -> None:
        if self.client is None:
            return
        try:
            self.client.evaluate(f"window.__GT && window.__GT.setMode({_js_string(mode)})")
        except CDPError:
            self.log.exception("setMode failed")

    def stats(self) -> dict:
        if self.client is None:
            return {}
        try:
            result = self.client.evaluate("window.__GT ? window.__GT.stats() : {}")
        except CDPError:
            return {}
        return (result.get("result") or {}).get("value") or {}

    def stop(self) -> None:
        self._stop.set()
        if self.client is not None:
            try:
                self.client.evaluate("window.__GT && window.__GT.restore()", timeout=3)
            except Exception:
                self.log.debug("restore failed (page likely already gone)")
            self.client.close()
            self.client = None
        if self.proc is not None and self.proc.poll() is None:
            # We started this process; closing the window is the caller's call,
            # but a headless run has nobody to click it.
            self.log.info("leaving the application running (pid %s)", self.proc.pid)
        self.proc = None


def _js_string(value: str) -> str:
    """JSON-encode, then neutralise the two characters JS strings dislike."""
    return json.dumps(value).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _match_url(url: str, pattern: str) -> bool:
    if pattern in ("", "*"):
        return True
    if pattern.endswith("*"):
        return url.startswith(pattern[:-1])
    return url == pattern
