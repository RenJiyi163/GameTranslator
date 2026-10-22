"""RPG Maker MV / MZ adapter.

Both engines run the game in a Chromium window (NW.js for MV and for MZ
releases packaged that way, Electron otherwise) and draw every string onto a
canvas. Scraping the DOM finds nothing, so this adapter hooks the engine's
own text choke points from inside the page and lets the engine keep drawing:
font, wrapping and escape codes stay exactly as the game intended.

The launch/attach plumbing is the same as `electron_cdp` and is reused rather
than copied; only the in-page runtime and the apply protocol differ.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import secrets
import subprocess
import time
import urllib.request
from pathlib import Path

from ..config import Profile
from ..segment import Segment
from .base import Adapter
from .electron_cdp import CDPClient, CDPError, _js_string, _match_url, _port_open

RUNTIME_JS_PATH = Path(__file__).resolve().parent.parent / "js" / "rpgmaker.js"

# \V[3] \N[1] \P[2] \G  ->  positional placeholders. The bracket is required
# for V/N/P: a looser pattern eats the first letter of plugin codes like
# \PX[30] and leaves "X[30]" in the text handed to a translator.
_DYNAMIC = re.compile(r"\\[VNP]\[\d+\]|\\G(?![A-Za-z])")
# Styling and unknown codes. Plugins add their own (\SP[3000], \MY[10]), and a
# translator has no way to preserve them, so they are all stripped.
_STYLE = re.compile(r"\\[A-Za-z]+(?:\[[^\]]*\])?|\\[{}|!.<>^$]")


def split_codes(text: str) -> tuple[str, list[str], list[str]]:
    """Return (text with placeholders, dynamic refs, style codes in order)."""
    refs: list[str] = []

    def grab(match: re.Match) -> str:
        token = match.group(0)
        refs.append(token)
        return "〔%d〕" % (len(refs) - 1)

    plain = _DYNAMIC.sub(grab, text)
    styles = _STYLE.findall(plain)
    plain = _STYLE.sub("", plain)
    return plain, refs, styles


def restore_codes(text: str, refs: list[str]) -> str:
    for index, token in enumerate(refs):
        text = text.replace("〔%d〕" % index, token)
    return text


class RPGMakerAdapter(Adapter):
    name = "rpgmaker_mv_mz"

    # Engine control codes that only change how the line looks. They are
    # stripped for the memory's second lookup, because text that went through
    # an offline translator came back without them. Dynamic references
    # (\V[3], \N[1], \P[1], \G) are deliberately kept: they carry a value the
    # engine fills in, and a memory entry for the line must match them.
    DEFAULT_STRIP_PATTERN = (
        r"\\(?!(?:[VNP]\[|[VNP](?![A-Za-z])|G(?![A-Za-z])))[A-Za-z]+(?:\[[^\]]*\])?"
        r"|\\[{}|!.<>^$]|\\\$"
    )

    @property
    def strip_pattern(self) -> str:
        return self.profile.adapter_config.get("stripPattern") or self.DEFAULT_STRIP_PATTERN

    def export_text(self, raw: str) -> tuple[str, list[str]]:
        """Strip styling codes, turn runtime refs into 〔n〕 for the document."""
        plain, refs, _styles = split_codes(raw)
        return plain, refs

    def __init__(self, profile: Profile, log: logging.Logger | None = None):
        super().__init__(profile, log)
        cfg = profile.adapter_config
        self.exe = cfg.get("exe")
        self.args: list[str] = list(cfg.get("args", []))
        self.port = int(cfg.get("port", 9222))
        self.url_match = cfg.get("urlMatch", "*")
        self.startup_timeout = float(cfg.get("startupTimeout", 60))
        self.attach_only = bool(cfg.get("attachOnly", False))
        self.restart_message = bool(cfg.get("restartMessage", True))

        self.binding_name = "gt_report_" + secrets.token_hex(4)

        self.proc = None
        self.client: CDPClient | None = None
        self.inbox: queue.Queue[list[Segment]] = queue.Queue()
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

    def _launch(self) -> None:
        if not self.exe:
            raise CDPError("profile.adapterConfig.exe is required to launch the game")
        exe = Path(self.exe)
        if not exe.exists():
            raise CDPError(f"executable not found: {exe}")
        args = list(self.args)
        if not any(a.startswith("--remote-debugging-port") for a in args):
            args.insert(0, f"--remote-debugging-port={self.port}")
        self.log.info("launching %s %s", exe, " ".join(args))
        self.proc = subprocess.Popen([str(exe), *args], cwd=str(exe.parent))

    def _wait_for_target(self, timeout: float | None = None) -> str:
        deadline = time.time() + (self.startup_timeout if timeout is None else timeout)
        last_err = ""
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise CDPError(f"process exited early with code {self.proc.returncode}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2) as resp:
                    targets = json.loads(resp.read().decode("utf-8"))
            except Exception as exc:
                last_err = str(exc)
                time.sleep(0.4)
                continue
            for target in targets:
                if target.get("type") != "page":
                    continue
                if not _match_url(target.get("url", ""), self.url_match):
                    continue
                ws_url = target.get("webSocketDebuggerUrl")
                if ws_url:
                    return ws_url
            time.sleep(0.4)
        raise CDPError(f"no debuggable page after {timeout or self.startup_timeout}s ({last_err})")

    def _attach(self, ws_url: str) -> None:
        client = CDPClient(ws_url, self.log)
        client.on("Runtime.bindingCalled", self._on_binding)
        client.on("Runtime.executionContextsCleared", self._on_contexts_cleared)
        client.call("Runtime.enable")
        client.call("Runtime.addBinding", {"name": self.binding_name})
        self.client = client
        self._inject()
        self.log.info("attached to %s", ws_url)

    def _inject(self) -> None:
        if self.client is None:
            return
        config = {
            "reportName": self.binding_name,
            "restartMessage": self.restart_message,
            "skipPatterns": list(self.profile.adapter_config.get("skipPatterns", [])),
        }
        source = RUNTIME_JS_PATH.read_text(encoding="utf-8")
        source = source.replace("/*__GT_CONFIG__*/ null", json.dumps(config, ensure_ascii=False))
        result = self.client.evaluate(source, timeout=30)
        if result.get("exceptionDetails"):
            raise CDPError(f"runtime injection failed: {result['exceptionDetails']}")
        self._injected = True
        # A canvas screen only emits text when it is drawn. A scene that was
        # already on screen when we attached would otherwise stay silent until
        # the player opened something, so force one full redraw.
        try:
            self.client.evaluate("window.__GT && window.__GT.rescan()")
        except CDPError:
            self.log.debug("initial rescan failed")
        self.log.info("in-page runtime installed (rpgmaker)")

    # -- events ------------------------------------------------------------

    def _on_binding(self, params: dict) -> None:
        if params.get("name") != self.binding_name:
            return
        try:
            items = json.loads(params.get("payload") or "[]")
        except json.JSONDecodeError:
            self.log.warning("bad report payload: %r", (params.get("payload") or "")[:120])
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
        self._injected = False

    def _reconnect(self) -> bool:
        now = time.time()
        if now - self._last_retry < 3.0:
            return False
        self._last_retry = now
        try:
            self._attach(self._wait_for_target(timeout=3.0))
            self.log.info("reconnected")
            return True
        except CDPError as exc:
            self.log.debug("reconnect failed: %s", exc)
            return False

    # -- adapter contract --------------------------------------------------

    def alive(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        now = time.time()
        if now - self._last_port_check > 5.0:
            self._last_port_check = now
            self._port_alive = _port_open(self.port)
        return self._port_alive

    def poll(self, timeout: float = 0.5) -> list[Segment]:
        if self.client is None or self.client._closed.is_set():
            if not self._reconnect():
                return []
        elif self._injected is False:
            # The page reloaded (title screen to game, or a crash-recovery
            # reload): the hooks died with the old context.
            try:
                self.client.call("Runtime.addBinding", {"name": self.binding_name})
                self._inject()
            except CDPError:
                self.log.debug("re-injection failed")
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return []

    def apply(self, translations: dict[str, str]) -> int:
        if not translations or self.client is None:
            return 0
        items = [{"id": str(key), "text": text} for key, text in translations.items()]
        literal = _js_string(json.dumps(items, ensure_ascii=False))
        try:
            # The page escapes this through JSON: the payload is already a
            # JSON string, so it has to be embedded, not re-quoted by hand.
            expr = f"window.__GT ? window.__GT.apply({literal}) : 0"
            result = self.client.evaluate(expr)
        except CDPError:
            self.log.exception("apply failed")
            return 0
        return int((result.get("result") or {}).get("value") or 0)

    def set_mode(self, mode: str) -> None:
        if self.client is None:
            return
        try:
            # `_js_string` already produces a JS string literal; round-tripping
            # through json.dumps first would hand the page "\"original\"" and
            # the runtime would quietly fall back to "translated".
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
        if self.client is not None:
            try:
                self.client.evaluate("window.__GT && window.__GT.restore()", timeout=3)
            except Exception:
                self.log.debug("restore failed (page likely already gone)")
            self.client.close()
            self.client = None
        if self.proc is not None and self.proc.poll() is None:
            self.log.info("leaving the application running (pid %s)", self.proc.pid)
        self.proc = None
