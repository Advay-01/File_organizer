"""Walk a directory and collect file facts (no content reading here)."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import List

from app.models.schemas import FileInfo

SKIP_DIRS = {".git", ".idea", ".venv", "venv", "__pycache__", "node_modules"}


def scan_directory(root: str, recursive: bool = False) -> List[FileInfo]:
    """Return FileInfo for each regular, non-hidden file under ``root``."""
    base = Path(root)
    if not base.is_dir():
        raise FileNotFoundError(f"Not a directory: {root}")

    found: List[FileInfo] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():
                continue
            st = p.stat()
            found.append(
                FileInfo(
                    path=str(p),
                    filename=name,
                    extension=p.suffix.lower(),
                    size_kb=round(st.st_size / 1024, 2),
                    date_created=datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
                )
            )
        if not recursive:
            break
    return found
