"""Window state that outlives one run: overlay position, tray hints.

Profiles describe a game; this describes how the window was left. It is
deliberately dumb: a JSON file next to backends.json, written best-effort, and
a corrupt or missing file just means "first launch".
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import ROOT


def path() -> Path:
    return ROOT / "ui.json"


def load() -> dict:
    target = path()
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    try:
        path().write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    except OSError:
        pass
