"""Adapter interface.

An adapter owns the connection to one running application and does three
things: report text it has not seen before, write translations back, and put
everything back to the original on exit.

Adapters must not translate, cache or validate. They report raw segments and
apply whatever the pipeline hands them.
"""

from __future__ import annotations

import abc
import logging

from ..config import Profile
from ..segment import Segment


class Adapter(abc.ABC):
    name: str = "base"

    def __init__(self, profile: Profile, log: logging.Logger | None = None):
        self.profile = profile
        self.log = log or logging.getLogger(f"gt.adapter.{self.name}")

    @abc.abstractmethod
    def start(self) -> None:
        """Launch or attach to the host application."""

    @abc.abstractmethod
    def poll(self, timeout: float = 0.5) -> list[Segment]:
        """Block up to `timeout` seconds and return newly reported segments."""

    @abc.abstractmethod
    def apply(self, translations: dict[str, str]) -> int:
        """Write translations into the host UI; return how many landed."""

    def export_text(self, raw: str) -> tuple[str, list[str]]:
        """A string as a document translator should see it.

        Engines that write control codes into the text override this to swap
        the runtime references for markable placeholders; everything else
        hands the text over unchanged and has nothing to restore.
        """
        return raw, []

    def set_mode(self, mode: str) -> None:
        """Switch between 'translated' and 'original'."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Restore originals and detach."""

    def alive(self) -> bool:
        return True
