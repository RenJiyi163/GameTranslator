"""Youdao web UI automation backend (last resort).

Status: interface only.

This is the fallback the author asked for and also the one he asked not to be
a core dependency: it drives a browser page, so it is slow, brittle, and
breaks whenever the site changes. It exists so the tool still works when the
local model is unavailable and the TM has no hit.

Rules if this is ever implemented:
  - It must never block the runtime; it runs from the same worker queue as any
    other backend and returns whatever it managed to collect.
  - It must keep no state in the core: selectors for the Youdao page belong in
    this file, not in profiles.
"""

from __future__ import annotations

from .base import Backend, Request


class YoudaoUIBackend(Backend):
    name = "youdao_ui"
    batch_size = 1

    def available(self) -> bool:
        return False

    def translate(self, items: list[Request]) -> dict[str, str]:
        raise NotImplementedError("youdao_ui: not implemented")
