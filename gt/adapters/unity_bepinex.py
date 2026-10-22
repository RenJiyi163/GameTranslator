"""Unity adapter via BepInEx (not implemented).

Requires a small C# plugin on the game side: it hooks `Text.text` setters and
`TMP_Text.text`, forwards strings over a local socket, and writes translations
back. The plugin is a separate build product; nothing here can substitute for
it. Compiled plugin bytes would have to live under `profiles/<id>/plugins/`.
"""

from __future__ import annotations

from ..segment import Segment
from .base import Adapter


class UnityBepInExAdapter(Adapter):
    name = "unity_bepinex"

    def start(self) -> None:
        raise NotImplementedError("unity_bepinex adapter: not implemented")

    def poll(self, timeout: float = 0.5) -> list[Segment]:
        return []

    def apply(self, translations: dict[str, str]) -> int:
        return 0

    def stop(self) -> None:
        pass
