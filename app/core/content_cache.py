"""Content-addressed extraction cache and versioned analysis cache."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from app.core.evidence import EvidenceBundle

CACHE_VERSION = 3
DEFAULT_DB = Path(os.environ.get("ORGANIZER_CACHE_DB", Path.home() / ".file_organizer" / "cache.db"))


class ContentIntelligenceCache:
    def __init__(self, db_path: Path | str | None = None):
        path = Path(db_path or DEFAULT_DB)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS evidence_cache ("
            "fingerprint TEXT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS intelligence_cache ("
            "cache_key TEXT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL)"
        )
        self._db.commit()

    def get_evidence(self, fingerprint: str) -> EvidenceBundle | None:
        if not fingerprint:
            return None
        row = self._db.execute(
            "SELECT payload FROM evidence_cache WHERE fingerprint = ? AND version = ?",
            (fingerprint, CACHE_VERSION),
        ).fetchone()
        if not row:
            return None
        try:
            return EvidenceBundle.from_json(row[0])
        except (TypeError, ValueError):
            return None

    def put_evidence(self, evidence: EvidenceBundle) -> None:
        if not evidence.fingerprint:
            return
        self._db.execute(
            "INSERT OR REPLACE INTO evidence_cache(fingerprint, version, payload, created) VALUES (?, ?, ?, ?)",
            (evidence.fingerprint, CACHE_VERSION, evidence.to_json(), time.time()),
        )
        self._db.commit()

    def get_analysis(self, key: str) -> dict | None:
        row = self._db.execute(
            "SELECT payload FROM intelligence_cache WHERE cache_key = ? AND version = ?",
            (key, CACHE_VERSION),
        ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except (TypeError, ValueError):
            return None

    def put_analysis(self, key: str, payload: dict) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO intelligence_cache(cache_key, version, payload, created) VALUES (?, ?, ?, ?)",
            (key, CACHE_VERSION, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()


def analysis_cache_key(
    evidence: EvidenceBundle,
    models: list[str],
    known_folders: list[str],
    allow_rename: bool = True,
) -> str:
    value = json.dumps(
        {
            "version": CACHE_VERSION,
            "fingerprint": evidence.fingerprint,
            "filename": evidence.filename.lower(),
            "models": models,
            "known_folders": sorted(folder.lower() for folder in known_folders),
            "allow_rename": allow_rename,
            "endpoints": {
                key: value
                for key, value in sorted(os.environ.items())
                if key.startswith("ORGANIZER_") and key.endswith("_BASE_URL")
            },
        },
        sort_keys=True,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
