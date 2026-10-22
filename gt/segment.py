"""A segment is one unit of text an adapter found in the host application."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Segment:
    id: str
    text: str
    kind: str = "text"
    where: str = ""
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.id = str(self.id)
