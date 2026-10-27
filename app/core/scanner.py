"""Walk a directory and collect file facts (no content reading here)."""
from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List

from app.models.schemas import FileInfo
from app.core.safety import (
    ScanPolicy,
    UnsafePathError,
    assert_safe_root,
    is_hidden_or_system,
    is_partial_download,
    is_reparse_point,
    is_system_path,
    resolved_inside,
    should_skip_directory,
    snapshot_file,
)

SKIP_DIRS = {".git", ".idea", ".venv", "venv", "__pycache__", "node_modules"}
SKIP_FILES = {".organizer_undo.json", ".organizer_undo.json.tmp"}


@dataclass
class ScanSummary:
    files_seen: int = 0
    skipped: int = 0
    reasons: dict = field(default_factory=dict)
    truncated: bool = False


def scan_directory_with_report(
    root: str,
    recursive: bool = False,
    policy: ScanPolicy | None = None,
) -> tuple[List[FileInfo], ScanSummary]:
    """Return safe file facts and aggregate skip information."""
    policy = policy or ScanPolicy.from_env(recent_default=0)
    base = assert_safe_root(Path(root))
    if not base.is_dir():
        raise FileNotFoundError(f"Not a directory: {root}")

    found: List[FileInfo] = []
    summary = ScanSummary()
    reasons = Counter()
    for dirpath, dirnames, filenames in os.walk(base):
        current = Path(dirpath)
        safe_dirs = []
        for directory in dirnames:
            candidate = current / directory
            if directory in SKIP_DIRS or directory.startswith(".") or should_skip_directory(candidate):
                reasons["protected_or_special_directory"] += 1
            else:
                safe_dirs.append(directory)
        dirnames[:] = safe_dirs
        for name in sorted(filenames):
            summary.files_seen += 1
            p = current / name
            skip_reason = None
            if name.casefold() in SKIP_FILES:
                skip_reason = "organizer_metadata"
            elif policy.skip_hidden_system and is_system_path(p):
                skip_reason = "system_file"
            elif name.startswith(".") and not policy.include_hidden_files:
                skip_reason = "hidden"
            elif (
                policy.skip_hidden_system
                and not policy.include_hidden_files
                and is_hidden_or_system(p)
            ):
                skip_reason = "hidden_or_system_attribute"
            elif policy.skip_partial and is_partial_download(p):
                skip_reason = "partial_download"
            elif is_reparse_point(p) or not p.is_file():
                skip_reason = "not_regular_file_or_reparse_point"
            elif not resolved_inside(p, base):
                skip_reason = "outside_approved_root"
            else:
                try:
                    st = p.stat()
                    if st.st_size > policy.max_file_bytes:
                        skip_reason = "file_size_limit"
                    elif policy.recent_seconds and time.time() - st.st_mtime < policy.recent_seconds:
                        skip_reason = "recently_modified"
                    else:
                        snapshot = snapshot_file(p)
                except (OSError, PermissionError):
                    skip_reason = "locked_or_unreadable"
            if skip_reason:
                summary.skipped += 1
                reasons[skip_reason] += 1
                continue
            st = p.stat()
            found.append(
                FileInfo(
                    path=str(p),
                    filename=name,
                    extension=p.suffix.lower(),
                    size_kb=round(st.st_size / 1024, 2),
                    date_created=datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
                    source_snapshot=snapshot,
                )
            )
            if len(found) >= policy.max_files:
                summary.truncated = True
                reasons["file_count_limit"] += 1
                break
        if summary.truncated:
            break
        if not recursive:
            break
    summary.reasons = dict(reasons)
    return found, summary


def scan_directory(
    root: str,
    recursive: bool = False,
    policy: ScanPolicy | None = None,
) -> List[FileInfo]:
    """Return FileInfo for each regular, safe file under ``root``."""
    files, _summary = scan_directory_with_report(root, recursive, policy)
    return files
