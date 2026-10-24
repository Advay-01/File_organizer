"""Run history on top of the undo log the executor already writes.

The executor appends one entry per --apply run to `<output>/.organizer_undo.json`:
    [{"time": "...", "moves": [{"src": "...", "dest": "..."}]}, ...]
This module lists those runs, shows what each one did, and can undo ANY run (not just the
last) safely. History is never destroyed: undone runs stay listed with a status, and each
move keeps a `restored` flag so a partly-undone run can be retried.

Safety rules (all covered by tests):
  * never overwrites: if the original spot is taken the file stays where it is, with a reason
  * never touches files the user already deleted or moved again (reported, not failed)
  * a tampered log cannot make undo touch files outside the output folder
  * folders left empty by an undo are removed, the output folder itself never is
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from app.core.safety import (
    UnsafePathError,
    move_file_no_overwrite,
    validate_move_paths,
)

UNDO_LOG = ".organizer_undo.json"
SAMPLE_SIZE = 5
_LOG_WRITE_LOCK = threading.Lock()


class UndoLogError(OSError):
    """The undo journal cannot be read or updated safely."""


# --------------------------------------------------------------------------- log io


def _log_path(output_root: str) -> Path:
    return Path(output_root) / UNDO_LOG


def load_history_for_update(output_root: str) -> List[dict]:
    """Load and validate an existing journal before a destructive operation."""
    p = _log_path(output_root)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise UndoLogError("The undo record could not be read safely.") from error
    if not isinstance(data, list) or any(not isinstance(entry, dict) for entry in data):
        raise UndoLogError("The undo record is not valid.")
    for entry in data:
        moves = entry.get("moves", [])
        if not isinstance(moves, list) or any(
            not isinstance(move, dict)
            or not isinstance(move.get("src"), str)
            or not isinstance(move.get("dest"), str)
            for move in moves
        ):
            raise UndoLogError("The undo record contains an invalid move.")
    return data


def write_history(output_root: str, entries: List[dict]) -> None:
    """Atomically persist a complete journal with retries for transient Windows locks."""
    p = _log_path(output_root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise UndoLogError("The undo record directory could not be opened safely.") from error
    tmp = p.with_name(p.name + ".tmp")
    with _LOG_WRITE_LOCK:
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(entries, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise UndoLogError("The undo record could not be written safely.") from error

        last_error: OSError | None = None
        for attempt in range(5):
            try:
                os.replace(tmp, p)
                return
            except OSError as error:
                last_error = error
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
        try:
            tmp.unlink()
        except OSError:
            pass
        raise UndoLogError("The undo record is temporarily locked.") from last_error


def _load(output_root: str) -> List[dict]:
    """Read the log; a missing, unreadable or corrupt file simply means 'no history'."""
    try:
        data = load_history_for_update(output_root)
    except UndoLogError:
        return []
    return data


def _save(output_root: str, history: List[dict]) -> None:
    write_history(output_root, history)


# --------------------------------------------------------------------------- helpers


def _all_moves(entry: dict) -> List[dict]:
    return [
        m
        for m in entry.get("moves", [])
        if isinstance(m, dict) and isinstance(m.get("src"), str) and isinstance(m.get("dest"), str)
    ]


def _moves(entry: dict, output_root: str | None = None) -> List[dict]:
    """Return completed moves, including a move interrupted before its final journal update."""
    moves = []
    for move in _all_moves(entry):
        if move.get("completed", True):
            moves.append(move)
            continue
        if not output_root or not move.get("started"):
            continue
        src, dest = Path(move["src"]), Path(move["dest"])
        try:
            interrupted_move_completed = (
                _inside(move["dest"], output_root) and dest.exists() and not src.exists()
            )
        except OSError:
            interrupted_move_completed = False
        if interrupted_move_completed:
            move["completed"] = True
            move["started"] = False
            moves.append(move)
    return moves


def _folder_of(dest: str, output_root: str) -> str:
    try:
        return Path(dest).parent.relative_to(output_root).as_posix() or "."
    except ValueError:
        return Path(dest).parent.name or "."


def _status(entry: dict, output_root: str) -> str:
    moves = _moves(entry, output_root)
    done = sum(1 for m in moves if m.get("restored"))
    if not moves:
        return str(entry.get("state", "applied"))
    if done == 0:
        if entry.get("state") == "applying":
            return "partial"
        if entry.get("state") == "failed":
            return "failed"
        if entry.get("state") == "partial":
            return "partial"
        return "applied"
    return "undone" if done == len(moves) else "partially undone"


def _inside(path: str, root: str) -> bool:
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def _prune_empty_dirs(start: Path, stop: Path) -> None:
    """Remove folders left empty by an undo, walking up but never removing `stop`."""
    stop = stop.resolve()
    d = start.resolve()
    while d != stop and stop in d.parents:
        try:
            d.rmdir()  # only succeeds when empty
        except OSError:
            return
        d = d.parent


# --------------------------------------------------------------------------- public API


def list_runs(output_root: str) -> List[dict]:
    """Newest first. `id` is the run's position in the log and is what undo_run takes."""
    runs = []
    for i, entry in enumerate(_load(output_root)):
        moves = _moves(entry, output_root)
        runs.append({
            "id": i,
            "time": entry.get("time", ""),
            "files": len(moves),
            "renamed": sum(1 for m in moves if Path(m["src"]).name != Path(m["dest"]).name),
            "folders": dict(Counter(_folder_of(m["dest"], output_root) for m in moves).most_common()),
            "status": _status(entry, output_root),
            "undone_at": entry.get("undone_at"),
            "sample": [{"from": Path(m["src"]).name, "to": Path(m["dest"]).name,
                        "folder": _folder_of(m["dest"], output_root)} for m in moves[:SAMPLE_SIZE]],
        })
    return runs[::-1]


def get_run(output_root: str, run_id: int) -> dict:
    """Every move of one run. Raises KeyError for an unknown id."""
    history = _load(output_root)
    if not (0 <= run_id < len(history)):
        raise KeyError(run_id)
    entry = history[run_id]
    moves = [{
        "src": m["src"], "dest": m["dest"],
        "from_name": Path(m["src"]).name, "to_name": Path(m["dest"]).name,
        "folder": _folder_of(m["dest"], output_root),
        "renamed": Path(m["src"]).name != Path(m["dest"]).name,
        "restored": bool(m.get("restored")),
    } for m in _moves(entry, output_root)]
    return {"id": run_id, "time": entry.get("time", ""), "status": _status(entry, output_root),
            "undone_at": entry.get("undone_at"), "moves": moves}


def undo_run(output_root: str, run_id: int) -> Dict[str, object]:
    """Move one run's files back. Raises KeyError (unknown id) or ValueError (already undone).
    Returns {"restored": n, "skipped": [{"file", "reason"}], "status": ...}."""
    history = _load(output_root)
    if not (0 <= run_id < len(history)):
        raise KeyError(run_id)
    entry = history[run_id]
    if _status(entry, output_root) == "undone":
        raise ValueError("this run was already undone")

    restored, skipped = 0, []
    for m in reversed(_moves(entry, output_root)):
        if m.get("restored"):
            continue
        src, dest = Path(m["src"]), Path(m["dest"])
        if not _inside(m["dest"], output_root):
            skipped.append({"file": dest.name, "reason": "outside the output folder (log ignored)"})
        elif not dest.exists():
            skipped.append({"file": dest.name, "reason": "file is no longer there"})
        elif src.exists():
            skipped.append({"file": dest.name, "reason": "original location is already occupied"})
        else:
            try:
                validate_move_paths(dest, src, Path(output_root), Path(output_root))
                src.parent.mkdir(parents=True, exist_ok=True)
                move_file_no_overwrite(dest, src)
            except UnsafePathError as error:
                skipped.append({"file": dest.name, "reason": f"unsafe path: {error}"})
                continue
            except OSError as e:
                skipped.append({"file": dest.name, "reason": f"error: {e}"})
                continue
            m["restored"] = True
            restored += 1
            _prune_empty_dirs(dest.parent, Path(output_root))
            entry["undone_at"] = datetime.now().isoformat(timespec="seconds")
            try:
                _save(output_root, history)
            except UndoLogError:
                skipped.append({
                    "file": dest.name,
                    "reason": "restored, but the undo record could not be updated",
                })
                break

    return {"restored": restored, "skipped": skipped, "status": _status(entry, output_root)}


def undo_last_run(output_root: str) -> Optional[Dict[str, object]]:
    """Undo the newest run that still has something to restore; None if there is none."""
    for run in list_runs(output_root):
        if run["status"] != "undone":
            res = undo_run(output_root, run["id"])
            res["id"] = run["id"]
            return res
    return None


# --------------------------------------------------------------------------- folder registry
# A history screen usually spans every folder the user has organized. The executor only knows
# one output folder at a time, so we remember them here (a tiny JSON file in the user's home).


def _registry_path() -> Path:
    home = os.getenv("ORGANIZER_HOME") or str(Path.home() / ".file_organizer")
    return Path(home) / "folders.json"


def known_folders() -> List[str]:
    """Remembered output folders that still exist, most recently used first."""
    try:
        data = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [f for f in data if isinstance(f, str) and Path(f).is_dir()] if isinstance(data, list) else []


def remember_folder(output_root: str) -> None:
    """Call after a successful --apply so the folder shows up in the global history."""
    root = str(Path(output_root).resolve())
    folders = [root] + [f for f in known_folders() if f != root]
    p = _registry_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(folders[:50]), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass  # history is a convenience; never break an organize run over it


def all_runs() -> List[dict]:
    """Runs from every remembered folder, newest first; each run carries its `folder`."""
    out = []
    for folder in known_folders():
        for run in list_runs(folder):
            out.append({**run, "folder": folder})
    return sorted(out, key=lambda r: r["time"], reverse=True)

