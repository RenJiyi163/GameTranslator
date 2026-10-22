"""Unreal Engine adapter via UE4SS (not implemented).

UE4SS exposes a Lua API and a live console. The realistic route is a Lua mod
that hooks `FText` construction / `UTextBlock` setters and talks to this
process over a socket. Same caveat as the Unity adapter: it is half a C++/Lua
project, not a Python adapter.
"""

from __future__ import annotations

from ..segment import Segment
from .base import Adapter


class UnrealUE4SSAdapter(Adapter):
    name = "unreal_ue4ss"

    def start(self) -> None:
        raise NotImplementedError("unreal_ue4ss adapter: not implemented")

    def poll(self, timeout: float = 0.5) -> list[Segment]:
        return []

    def apply(self, translations: dict[str, str]) -> int:
        return 0

    def stop(self) -> None:
        pass
