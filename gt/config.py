"""Profile loading.

A profile is the only place that knows anything about a specific game:
executable path, launch arguments, DOM selectors, translation memory location,
and which backends may be used for it.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

def _root() -> Path:
    """Where profiles/, logs/ and cache/ live.

    Inside a PyInstaller bundle `__file__` points into the unpacked archive,
    which is read-only and wiped on exit; the profiles the user edits have to
    sit next to the executable instead.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _root()
PROFILE_DIR = ROOT / "profiles"
LOG_DIR = ROOT / "logs"
CACHE_DIR = ROOT / "cache"


class ConfigError(RuntimeError):
    pass


#: memory = dictionary only; tm_first = dictionary, then a model; backend_only
#: = skip the dictionary and translate everything again.
STRATEGIES = ("memory", "tm_first", "backend_only")
STRATEGY_LABELS = {
    "memory": "只查词库",
    "tm_first": "词库优先，后端兜底",
    "backend_only": "全走后端",
}


@dataclass(frozen=True)
class Selector:
    css: str
    kind: str = "text"


@dataclass
class Profile:
    id: str
    name: str
    adapter: str
    adapter_config: dict[str, Any]
    selectors: list[Selector]
    skip_selectors: list[str] = field(default_factory=list)
    skip_tags: list[str] = field(default_factory=list)
    hotkey: str = "Ctrl+Shift+T"
    tm: dict[str, Any] = field(default_factory=dict)
    glossary: str | None = None
    #: None means "inherit the machine-wide chain" (backends.json `active`);
    #: an explicit list, even an empty one, pins this game.
    backends: list[str] | None = None
    backend_config: dict[str, Any] = field(default_factory=dict)
    #: "memory" | "tm_first" | "backend_only"; None means infer from backends.
    strategy: str | None = None
    root: Path = PROFILE_DIR

    @property
    def dir(self) -> Path:
        return self.root

    def path(self, value: str | None) -> Path | None:
        """Resolve a profile-relative path, keeping absolute paths as-is."""
        if not value:
            return None
        p = Path(value)
        return p if p.is_absolute() else (self.root / p).resolve()

    @property
    def tm_source(self) -> Path | None:
        return self.path(self.tm.get("source"))

    @property
    def tm_db(self) -> Path:
        return self.path(self.tm.get("db") or "tm.sqlite")  # type: ignore[return-value]

    @property
    def cache_db(self) -> Path:
        return CACHE_DIR / f"{self.id}.sqlite"

    @property
    def glossary_path(self) -> Path | None:
        return self.path(self.glossary)

    def backend_names(self) -> list[str]:
        """Which backends this game uses, profile first, settings second."""
        if self.backends is not None:
            return list(self.backends)
        from .settings import active_backends

        return active_backends()

    def effective_strategy(self) -> str:
        """How this game resolves text.

        Profiles written before strategies existed only say which backends
        they use, and that is enough to tell the two old cases apart: no
        backends meant memory-only.
        """
        if self.strategy in STRATEGIES:
            return self.strategy
        return "memory" if self.backends == [] else "tm_first"

    @classmethod
    def load(cls, profile_id: str, base: Path | None = None) -> "Profile":
        root = (base or PROFILE_DIR) / profile_id
        cfg_path = root / "profile.json"
        if not cfg_path.exists():
            raise ConfigError(f"profile not found: {cfg_path}")
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        return cls.from_dict(raw, root)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], root: Path) -> "Profile":
        selectors = []
        for item in raw.get("selectors", []):
            if isinstance(item, str):
                selectors.append(Selector(css=item))
            else:
                selectors.append(Selector(css=item["css"], kind=item.get("kind", "text")))
        # Selectors only mean something to the DOM-scraping adapters; a canvas
        # engine (RPG Maker) and an OCR adapter have nothing to select.
        if not selectors and raw.get("adapter", "electron_cdp") == "electron_cdp":
            raise ConfigError(f"{root}/profile.json: at least one selector is required")
        return cls(
            id=raw["id"],
            name=raw.get("name", raw["id"]),
            adapter=raw.get("adapter", "electron_cdp"),
            adapter_config=raw.get("adapterConfig", {}),
            selectors=selectors,
            skip_selectors=raw.get("skipSelectors", []),
            skip_tags=raw.get("skipTags", []),
            hotkey=raw.get("hotkey", "Ctrl+Shift+T"),
            tm=raw.get("tm", {}),
            glossary=raw.get("glossary"),
            backends=list(raw["backends"]) if "backends" in raw else None,
            backend_config=raw.get("backendConfig", {}),
            strategy=raw.get("strategy"),
            root=root,
        )

    def page_config(self) -> dict[str, Any]:
        """The slice of the profile the in-page runtime is allowed to see."""
        config: dict[str, Any] = {
            "selectors": [{"css": s.css, "kind": s.kind} for s in self.selectors],
            "hotkey": self.hotkey,
        }
        # Omit empty lists rather than sending them: a key that is present but
        # empty reads as "override the defaults with nothing".
        if self.skip_selectors:
            config["skipSelectors"] = self.skip_selectors
        if self.skip_tags:
            config["skipTags"] = self.skip_tags
        return config


def list_profiles() -> list[str]:
    if not PROFILE_DIR.exists():
        return []
    return sorted(p.name for p in PROFILE_DIR.iterdir() if (p / "profile.json").exists())
