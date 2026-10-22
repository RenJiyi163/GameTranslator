"""Adapter registry."""

from __future__ import annotations

from .base import Adapter
from .electron_cdp import ElectronCDPAdapter
from .ocr_overlay import OCROverlayAdapter
from .renpy import RenPyAdapter
from .rpgmaker_mv_mz import RPGMakerAdapter
from .unity_bepinex import UnityBepInExAdapter
from .unreal_ue4ss import UnrealUE4SSAdapter

REGISTRY: dict[str, type[Adapter]] = {
    "electron_cdp": ElectronCDPAdapter,
    "renpy": RenPyAdapter,
    "rpgmaker_mv_mz": RPGMakerAdapter,
    "unity_bepinex": UnityBepInExAdapter,
    "unreal_ue4ss": UnrealUE4SSAdapter,
    "ocr_overlay": OCROverlayAdapter,
}

__all__ = ["Adapter", "REGISTRY"]
