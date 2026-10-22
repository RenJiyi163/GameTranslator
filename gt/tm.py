"""Translation memory backed by SQLite.

Why SQLite instead of loading the JSONL: a large memory is ~178 MB of JSONL.
Parsing it takes seconds and several hundred MB of RAM, and the runtime only
ever needs point lookups. The index is built once (`gt tm build`) and queried
constantly afterwards.

Schema is deliberately dumb: one normalised key -> one translation. Conflicts
are resolved at build time (first pair wins, counted in the build report).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .text import normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS tm (
    key         TEXT PRIMARY KEY,
    original    TEXT NOT NULL,
    translation TEXT NOT NULL,
    kind        TEXT,
    source      TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def build(jsonl_path: Path, db_path: Path) -> dict:
    """Convert a JSONL translation memory into the SQLite index."""
    if not jsonl_path.exists():
        raise FileNotFoundError(jsonl_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".building")
    if tmp.exists():
        tmp.unlink()

    rows = 0
    skipped = 0
    conflicts = 0
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA journal_mode=OFF")
        batch = []
        seen: dict[str, str] = {}
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                original = item.get("original") or ""
                translation = item.get("translation") or ""
                key = normalize(original)
                if not key or not translation.strip():
                    skipped += 1
                    continue
                prev = seen.get(key)
                if prev is not None:
                    if prev != translation:
                        conflicts += 1
                    continue
                seen[key] = translation
                batch.append((key, original, translation, item.get("category"), item.get("source")))
                if len(batch) >= 5000:
                    conn.executemany("INSERT OR REPLACE INTO tm VALUES (?,?,?,?,?)", batch)
                    rows += len(batch)
                    batch.clear()
        if batch:
            conn.executemany("INSERT OR REPLACE INTO tm VALUES (?,?,?,?,?)", batch)
            rows += len(batch)
        conn.executemany(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            [
                ("source", str(jsonl_path)),
                ("rows", str(rows)),
                ("conflicting_duplicates", str(conflicts)),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    if db_path.exists():
        db_path.unlink()
    tmp.replace(db_path)
    return {"rows": rows, "skipped": skipped, "conflicting_duplicates": conflicts, "db": str(db_path)}


def merge(jsonl_path: Path, db_path: Path) -> dict:
    """Add translations to an existing index without rebuilding it.

    Importing a document the translator worked on must not drop the entries
    the index already had, so this is an upsert keyed on the normalised
    source, not `build()`. Rows whose translation is empty or identical are
    counted but not written.
    """
    if not jsonl_path.exists():
        raise FileNotFoundError(jsonl_path)
    if not db_path.exists():
        result = build(jsonl_path, db_path)
        result["added"] = result.get("rows", 0)
        result["updated"] = 0
        return result

    added = updated = unchanged = skipped = 0
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                original = item.get("original") or ""
                translation = item.get("translation") or ""
                key = normalize(original)
                if not key or not translation.strip():
                    skipped += 1
                    continue
                row = conn.execute("SELECT translation FROM tm WHERE key = ?", (key,)).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO tm VALUES (?,?,?,?,?)",
                        (key, original, translation, item.get("category"), item.get("source")))
                    added += 1
                elif row[0] != translation:
                    conn.execute("UPDATE tm SET translation = ? WHERE key = ?", (translation, key))
                    updated += 1
                else:
                    unchanged += 1
        conn.commit()
    finally:
        conn.close()
    return {"added": added, "updated": updated, "unchanged": unchanged,
            "skipped": skipped, "db": str(db_path)}


class TranslationMemory:
    """Read-only lookups over the built index."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._cache: dict[str, str | None] = {}
        self._cache_limit = 20000

    def available(self) -> bool:
        return self.db_path.exists()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            uri = f"file:{self.db_path.as_posix()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.execute("PRAGMA query_only=ON")
        return self._conn

    def lookup(self, key: str) -> str | None:
        if not key:
            return None
        hit = self._cache.get(key, "")
        if hit:
            return hit
        if key in self._cache:
            return None
        with self._lock:
            try:
                row = self._connect().execute(
                    "SELECT translation FROM tm WHERE key = ?", (key,)
                ).fetchone()
            except sqlite3.Error:
                return None
        value = row[0] if row else None
        if len(self._cache) < self._cache_limit:
            self._cache[key] = value
        return value

    def stats(self) -> dict:
        if not self.available():
            return {"db": str(self.db_path), "exists": False}
        conn = self._connect()
        rows = conn.execute("SELECT COUNT(*) FROM tm").fetchone()[0]
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        return {"db": str(self.db_path), "exists": True, "rows": rows, **meta}

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
