"""Self-update against a plain manifest file.

Nothing here assumes a particular host: `update.json` just names a URL that
serves

    {"version": "0.2.0", "url": "https://…/GameTranslator.exe",
     "sha256": "…", "size": 22000000, "notes": "…"}

so a folder on any static host works. The download is verified by hash before
it is allowed anywhere near the running executable.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__
from .config import ROOT

CONFIG_PATH = ROOT / "update.json"
DEFAULTS = {"manifestUrl": "", "autoCheck": True, "lastCheck": 0.0}
TIMEOUT = 30


def load_config() -> dict[str, Any]:
    config = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return config


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_version(text: str) -> tuple[int, ...]:
    parts = []
    for piece in str(text).strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    left, right = parse_version(candidate), parse_version(current)
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": f"GameTranslator/{__version__}"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def check(url: str | None = None) -> dict[str, Any]:
    config = load_config()
    target = url or config.get("manifestUrl") or ""
    if not target:
        return {"ok": False, "reason": "没有配置更新地址", "current": __version__}
    try:
        manifest = _fetch_json(target)
    except Exception as exc:  # network, DNS, proxy, bad JSON
        return {"ok": False, "reason": f"取不到更新信息：{exc}", "current": __version__}
    latest = str(manifest.get("version") or "")
    if not latest:
        return {"ok": False, "reason": "更新文件里没有 version 字段", "current": __version__}
    available = is_newer(latest)
    config["lastCheck"] = time.time()
    try:
        save_config(config)
    except OSError:
        pass
    return {
        "ok": True,
        "current": __version__,
        "latest": latest,
        "available": available,
        "url": manifest.get("url"),
        "sha256": manifest.get("sha256"),
        "size": manifest.get("size"),
        "notes": manifest.get("notes", ""),
    }


def download(manifest: dict[str, Any], dest: Path | None = None) -> Path:
    url = manifest.get("url")
    if not url:
        raise RuntimeError("更新文件里没有 url")
    suffix = Path(urllib.parse.urlparse(url).path).suffix or ".exe"
    dest = dest or Path(tempfile.gettempdir()) / f"GameTranslator-{manifest['latest']}{suffix}"
    digest = hashlib.sha256()
    received = 0
    request = urllib.request.Request(url, headers={"User-Agent": f"GameTranslator/{__version__}"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response, dest.open("wb") as out:
        for chunk in iter(lambda: response.read(1 << 20), b""):
            out.write(chunk)
            digest.update(chunk)
            received += len(chunk)
    expected = (manifest.get("sha256") or "").lower()
    if expected and digest.hexdigest() != expected:
        dest.unlink(missing_ok=True)
        raise RuntimeError("下载完成的文件校验不通过，已删除")
    size = manifest.get("size")
    if size and int(size) != received:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"下载大小不符：{received} 字节，应为 {size}")
    return dest


SWAP_TEMPLATE = """@echo off
setlocal
set "NEW={new}"
set "OLD={old}"
for /l %%i in (1,1,30) do (
  move /y "%NEW%" "%OLD%" >nul 2>&1 && goto done
  timeout /t 1 /nobreak >nul
)
exit /b 1
:done
start "" "%OLD%"
del "%~f0"
"""


def apply(downloaded: Path, exe: Path | None = None) -> Path:
    """Stage the new build and swap it in after this process exits.

    Windows will not let a running image be overwritten, so the move happens
    in a detached batch file that waits for the old one to unlock.
    """
    if not getattr(sys, "frozen", False) and exe is None:
        raise RuntimeError("源码运行时不能自更新，请直接替换 exe")
    exe = Path(exe or sys.executable)
    staged = exe.with_suffix(".new.exe")
    staged.write_bytes(Path(downloaded).read_bytes())
    script = Path(tempfile.gettempdir()) / f"gt-update-{int(time.time())}.cmd"
    script.write_text(
        SWAP_TEMPLATE.format(new=staged, old=exe).replace("\n", "\r\n"), encoding="ascii")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags, close_fds=True)
    return script


def auto_check() -> dict[str, Any] | None:
    """Called once at startup; quiet unless something is actually waiting."""
    config = load_config()
    if not config.get("autoCheck") or not config.get("manifestUrl"):
        return None
    last = float(config.get("lastCheck") or 0)
    if time.time() - last < 24 * 3600:
        return None
    result = check()
    return result if result.get("available") else None
