"""Per-profile term glossary.

Two uses, both optional:
  1. Feed fixed renderings to model backends so names do not drift between
     paragraphs ("Kitsune" should not be 狐狸 in one place and 狐妖 in another).
  2. Audit the translation memory: a reported inconsistency is a signal, not an
     error, because the same source word can be legitimately translated
     differently depending on context.

File format: TSV, `source<TAB>target[<TAB>note]`, `#` starts a comment.
"""

from __future__ import annotations

from pathlib import Path


class Glossary:
    def __init__(self, terms: dict[str, str] | None = None):
        self.terms = terms or {}

    @classmethod
    def load(cls, path: Path | None) -> "Glossary":
        if path is None or not path.exists():
            return cls()
        terms: dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("\t")]
            if len(parts) < 2 or not parts[0]:
                continue
            terms[parts[0]] = parts[1]
        return cls(terms)

    def __len__(self) -> int:
        return len(self.terms)

    def hint(self, text: str, limit: int = 24) -> list[tuple[str, str]]:
        """Terms that actually occur in `text`, longest first."""
        hits = [(s, t) for s, t in self.terms.items() if s and s in text]
        hits.sort(key=lambda kv: len(kv[0]), reverse=True)
        return hits[:limit]

    def render_hint(self, text: str, limit: int = 24) -> str:
        hits = self.hint(text, limit)
        if not hits:
            return ""
        return "；".join(f"{s} => {t}" for s, t in hits)


class GlossaryFile:
    """A glossary.tsv that can be edited without dropping its comments.

    The editor rewrites single lines instead of regenerating the file, so
    hand-written comments and the optional note column survive a round trip.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lines: list[str] = []
        if path.exists():
            self._lines = path.read_text(encoding="utf-8").splitlines()

    def entries(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for line in self._lines:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].strip():
                out.append((parts[0].strip(), parts[1].strip()))
        return out

    def _find(self, source: str) -> int | None:
        for index, line in enumerate(self._lines):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if parts[0].strip() == source:
                return index
        return None

    def set(self, source: str, target: str) -> None:
        source, target = source.strip(), target.strip()
        if not source:
            raise ValueError("原文不能为空")
        index = self._find(source)
        if index is None:
            self._lines.append(f"{source}\t{target}")
            return
        parts = self._lines[index].split("\t")
        self._lines[index] = "\t".join([source, target, *parts[2:]])

    def delete(self, source: str) -> bool:
        index = self._find(source.strip())
        if index is None:
            return False
        del self._lines[index]
        return True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(self._lines)
        self.path.write_text(text.rstrip("\n") + ("\n" if text.strip() else ""), encoding="utf-8")
