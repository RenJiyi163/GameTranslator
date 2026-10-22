"""Backend interface.

A backend is anything that can turn source strings into target-language
strings. It never touches the game and never decides what to show: the
pipeline validates the output and drops it if it breaks protection rules.

Implementations must be safe to call from a worker thread and must return a
dict keyed by the request key; missing keys mean "no translation", which the
game handles by keeping the original text.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class Request:
    key: str
    text: str
    kind: str = "text"
    where: str = ""


class Backend(ABC):
    name: str = "base"
    #: Number of items the backend prefers per call.
    batch_size: int = 1
    #: Set by the pipeline; backends that can use fixed renderings read it.
    glossary = None

    def __init__(self, config: dict | None = None, log: logging.Logger | None = None):
        self.config = config or {}
        self.log = log or logging.getLogger(f"gt.backend.{self.name}")

    def available(self) -> bool:
        """False means "configured but unusable"; the chain skips it."""
        return True

    def start(self) -> None:
        """Optional one-time setup (load a model, open a session)."""

    def stop(self) -> None:
        """Release resources."""

    @abstractmethod
    def translate(self, items: list[Request]) -> dict[str, str]:
        """Translate a batch. Never raise for a single bad item; log and skip."""
