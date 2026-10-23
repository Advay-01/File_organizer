"""Apply a plan to disk, with dry-run and an undo log."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import List

from app.core.planner import Move
from app.core.safety import UnsafePathError, move_file_no_overwrite, validate_move_paths

UNDO_LOG = ".organizer_undo.json"


def execute_plan(
    moves: List[Move],
    output_root: str,
    dry_run: bool = True,
    source_root: str | None = None,
) -> List[dict]:
    """Move files. Returns the list of moves actually made (empty on dry run)."""
    done: List[dict] = []
    for m in moves:
        src, dest = Path(m.src), Path(m.dest)
        category = "/".join(part for part in (m.folder, m.subfolder) if part)
        if not m.selected:
            print(f"  {src.name}  ->  kept in place (unselected: {category})")
            continue
        print(f"  {src.name}  ->  {category}/{dest.name}   ({m.reason})")
        if dry_run:
            continue
        try:
            validate_move_paths(
                src,
                dest,
                Path(output_root),
                Path(source_root) if source_root else None,
            )
        except UnsafePathError as error:
            print(f"    skipped: unsafe path ({error})")
            continue
        if not src.exists():
            print(f"    skipped: source missing")
            continue
        if dest.exists():
            print(f"    skipped: destination already exists")
            continue
        try:
            validate_move_paths(
                src,
                dest,
                Path(output_root),
                Path(source_root) if source_root else None,
            )
            move_file_no_overwrite(src, dest)
            done.append({"src": str(src), "dest": str(dest)})
        except OSError as e:
            print(f"    FAILED: {e}")
    if done:
        _write_log(output_root, done)
    return done


def _write_log(output_root: str, done: List[dict]) -> None:
    log = Path(output_root) / UNDO_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        history = json.loads(log.read_text(encoding="utf-8")) if log.exists() else []
    except (OSError, ValueError):
        history = []
    history.append({"time": datetime.now().isoformat(timespec="seconds"), "moves": done})
    temporary = log.with_name(log.name + ".tmp")
    temporary.write_text(json.dumps(history, indent=2), encoding="utf-8")
    os.replace(temporary, log)


def undo_last(output_root: str) -> int:
    """Reverse the most recent run using the hardened history implementation."""
    from app.core.history import undo_last_run

    result = undo_last_run(output_root)
    if result is None:
        print("Nothing to undo.")
        return 0
    return int(result["restored"])
