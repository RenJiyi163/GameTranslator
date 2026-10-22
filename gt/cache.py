"""Persistent cache for translations produced at runtime.

The acceptance criterion "quit, restart, hit the cache" is what this exists
for: anything a backend produced once must never be paid for twice.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    scope       TEXT NOT NULL,
    key         TEXT NOT NULL,
    original    TEXT NOT NULL,
    translation TEXT NOT NULL,
    backend     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (scope, key)
);
"""


class TranslationCache:
    def __init__(self, db_path: Path, scope: str):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.scope = scope
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT translation FROM cache WHERE scope = ? AND key = ?",
                (self.scope, key),
            ).fetchone()
        return row[0] if row else None

    def put(self, key: str, original: str, translation: str, backend: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?,?)",
                (self.scope, key, original, translation, backend, time.time()),
            )
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM cache WHERE scope = ?", (self.scope,)
            ).fetchone()[0]

    def clear(self) -> int:
        # Count under the same lock instead of calling count(): the lock is
        # not reentrant, so count() inside the with-block deadlocks forever.
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) FROM cache WHERE scope = ?", (self.scope,)
            ).fetchone()[0]
            self._conn.execute("DELETE FROM cache WHERE scope = ?", (self.scope,))
            self._conn.commit()
        return n

    def close(self) -> None:
        with self._lock:
            self._conn.close()
