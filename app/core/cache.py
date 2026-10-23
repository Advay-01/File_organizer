"""Local, path-independent cache for completed file classifications."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("ORGANIZER_CACHE_DB", Path.home() / ".file_organizer" / "cache.db"))
_CHUNK_SIZE = 64 * 1024


def fingerprint(path: Path) -> str | None:
    """Return a cheap content-based identifier without retaining file contents."""
    try:
        size = path.stat().st_size
        digest = hashlib.sha256(str(size).encode("ascii"))
        with path.open("rb") as file:
            digest.update(file.read(_CHUNK_SIZE))
            if size > _CHUNK_SIZE * 2:
                file.seek(-_CHUNK_SIZE, os.SEEK_END)
                digest.update(file.read(_CHUNK_SIZE))
        return digest.hexdigest()
    except OSError:
        return None


class OrganizeCache:
    """Persisted folder suggestions keyed by a file's fingerprint."""

    def __init__(self, db_path: Path | str | None = None):
        path = Path(db_path or DEFAULT_DB)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS entries ("
            "fingerprint TEXT PRIMARY KEY, folder TEXT NOT NULL, "
            "reason TEXT NOT NULL, created REAL NOT NULL)"
        )
        self._db.commit()

    def get(self, path: Path) -> tuple[str, str] | None:
        key = fingerprint(path)
        if key is None:
            return None
        row = self._db.execute(
            "SELECT folder, reason FROM entries WHERE fingerprint = ?", (key,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def put_many(self, entries: list[tuple[Path, str, str]]) -> None:
        rows = []
        for path, folder, reason in entries:
            key = fingerprint(path)
            if key:
                rows.append((key, folder, reason, time.time()))
        if rows:
            self._db.executemany(
                "INSERT OR REPLACE INTO entries(fingerprint, folder, reason, created) VALUES (?, ?, ?, ?)",
                rows,
            )
            self._db.commit()

    def clear(self) -> None:
        self._db.execute("DELETE FROM entries")
        self._db.commit()

    def close(self) -> None:
        self._db.close()
