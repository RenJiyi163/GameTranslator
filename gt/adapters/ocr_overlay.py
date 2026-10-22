"""Screen-capture + OCR adapter (not implemented).

The universal fallback: works with anything that draws pixels, including
consoles, emulators and encrypted engines. Also the slowest and the least
reliable, so it is deliberately last on the list.

Planned pieces: capture a window region, run OCR on a change-detected frame,
diff against what is already on screen, translate, then draw an overlay window
with the translation positioned over the original text.
"""

from __future__ import annotations

from ..segment import Segment
from .base import Adapter


class OCROverlayAdapter(Adapter):
    name = "ocr_overlay"

    def start(self) -> None:
        raise NotImplementedError("ocr_overlay adapter: not implemented")

    def poll(self, timeout: float = 0.5) -> list[Segment]:
        return []

    def apply(self, translations: dict[str, str]) -> int:
        return 0

    def stop(self) -> None:
        pass
