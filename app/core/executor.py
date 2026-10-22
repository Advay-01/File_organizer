"""Apply a plan to disk, with dry-run and an undo log."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import List

from app.core.planner import Move

UNDO_LOG = ".organizer_undo.json"


def execute_plan(moves: List[Move], output_root: str, dry_run: bool = True) -> List[dict]:
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
        if not src.exists():
            print(f"    skipped: source missing")
            continue
        if dest.exists():
            print(f"    skipped: destination already exists")
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            done.append({"src": str(src), "dest": str(dest)})
        except OSError as e:
            print(f"    FAILED: {e}")
    if done:
        _write_log(output_root, done)
    return done


def _write_log(output_root: str, done: List[dict]) -> None:
    log = Path(output_root) / UNDO_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    history = json.loads(log.read_text()) if log.exists() else []
    history.append({"time": datetime.now().isoformat(timespec="seconds"), "moves": done})
    log.write_text(json.dumps(history, indent=2))


def undo_last(output_root: str) -> int:
    """Reverse the most recent run. Returns number of files restored."""
    log = Path(output_root) / UNDO_LOG
    if not log.exists():
        print("Nothing to undo.")
        return 0
    history = json.loads(log.read_text())
    if not history:
        print("Nothing to undo.")
        return 0
    last = history.pop()
    restored = 0
    for mv in reversed(last["moves"]):
        src, dest = Path(mv["src"]), Path(mv["dest"])
        if dest.exists() and not src.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(src))
            restored += 1
        else:
            print(f"  could not restore {dest.name}")
    log.write_text(json.dumps(history, indent=2))
    return restored
