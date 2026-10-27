"""Content-addressed extraction cache and versioned analysis cache."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

from app.core.evidence import EvidenceBundle
from app.core.privacy import detect_sensitive, redact_sensitive
from app.models.schemas import FileInfo

CACHE_VERSION = 13
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

    def get_evidence(self, fingerprint: str, extension: str | None = None) -> EvidenceBundle | None:
        if not fingerprint:
            return None
        if extension is not None:
            key = f"{fingerprint}:{extension.lower()}"
            row = self._db.execute(
                "SELECT payload FROM evidence_cache WHERE fingerprint = ? AND version = ?",
                (key, CACHE_VERSION),
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT payload FROM evidence_cache "
                "WHERE (fingerprint = ? OR fingerprint LIKE ?) AND version = ? "
                "ORDER BY created DESC LIMIT 1",
                (fingerprint, f"{fingerprint}:%", CACHE_VERSION),
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
        key = f"{evidence.fingerprint}:{evidence.extension.lower()}"
        self._db.execute(
            "INSERT OR REPLACE INTO evidence_cache(fingerprint, version, payload, created) VALUES (?, ?, ?, ?)",
            (key, CACHE_VERSION, evidence.to_json(), time.time()),
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
            "extension": evidence.extension,
            "detected_type": evidence.detected_type,
            "detected_type_confidence": evidence.detected_type_confidence,
            "parser_status": evidence.parser_status,
            "filename": evidence.filename.lower(),
            "parent_directory": evidence.parent_directory.lower(),
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


def refresh_evidence_identity(evidence: EvidenceBundle, file: FileInfo) -> EvidenceBundle:
    """Reuse content-derived evidence without retaining another file's identity."""
    path = Path(file.path)
    findings = detect_sensitive(
        evidence.content,
        [
            *[
                str(value)
                for value in evidence.metadata.values()
                if value not in (None, "")
            ],
            path.name,
            path.parent.name,
        ],
    )
    warnings = [
        warning
        for warning in evidence.warnings
        if not warning.startswith("Potential sensitive content detected (")
    ]
    if findings:
        kinds = ", ".join(
            sorted({finding.kind.replace("_", " ") for finding in findings})
        )
        warnings.append(
            f"Potential sensitive content detected ({kinds}). "
            "Cloud processing is disabled by default."
        )
    return replace(
        evidence,
        path=str(path),
        filename=path.name,
        extension=path.suffix.lower(),
        parent_directory=path.parent.name,
        warnings=warnings,
        sensitive_findings=[finding.to_dict() for finding in findings],
        redacted_content=redact_sensitive(evidence.content),
    )
