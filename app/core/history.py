"""Run history on top of the undo log the executor already writes.

The executor appends one entry per --apply run to `<output>/.organizer_undo.json`:
    [{"time": "...", "moves": [{"src": "...", "dest": "..."}]}, ...]
This module lists those runs, shows what each one did, and can undo ANY run (not just the
last) safely. History is never destroyed: undone runs stay listed with a status, and each
move keeps a `restored` flag so a partly-undone run can be retried.

Safety rules (all covered by tests):
  * never overwrites: if the original spot is taken the file stays where it is, with a reason
  * never touches files the user already deleted or moved again (reported, not failed)
  * moves stay within the recorded source/output boundaries; legacy logs stay in output
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
from typing import Callable, Dict, List, Optional, TypeVar

from app.core.safety import (
    UnsafePathError,
    move_file_no_overwrite,
    validate_move_paths,
)

UNDO_LOG = ".organizer_undo.json"
SAMPLE_SIZE = 5
_JOURNAL_LOCK = threading.RLock()
T = TypeVar("T")


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
    with _JOURNAL_LOCK:
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


def update_history(output_root: str, update: Callable[[List[dict]], T]) -> T:
    """Serialize a complete journal read-modify-write transaction."""
    with _JOURNAL_LOCK:
        entries = load_history_for_update(output_root)
        result = update(entries)
        write_history(output_root, entries)
        return result


def _load(output_root: str) -> List[dict]:
    """Read the log; a missing, unreadable or corrupt file simply means 'no history'."""
    try:
        data = load_history_for_update(output_root)
    except UndoLogError:
        return []
    return data

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
        return Path(dest).parent.resolve().relative_to(Path(output_root).resolve()).as_posix() or "."
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


def _undo_roots(entry: dict, output_root: str) -> tuple[Path, Path]:
    """Reverse only explicitly recorded boundaries; never infer roots from a move."""
    output = Path(output_root).expanduser().resolve(strict=True)
    if "source_root" not in entry and "output_root" not in entry:
        return output, output  # Older journals only authorized moves within output.
    source_value, output_value = entry.get("source_root"), entry.get("output_root")
    if not all(isinstance(value, str) and Path(value).is_absolute()
               for value in (source_value, output_value)):
        raise UnsafePathError("Invalid recorded root boundaries.")
    if Path(output_value).resolve(strict=True) != output:
        raise UnsafePathError("The journal belongs to another output root.")
    return Path(source_value), output


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
            "undone_at": entry.get("undone_at"), "moves": moves,
            "analysis_stats": entry.get("analysis_stats", {}), "execution_stats": entry.get("execution_stats", {})}


def undo_run(output_root: str, run_id: int) -> Dict[str, object]:
    """Move one run's files back. Raises KeyError (unknown id) or ValueError (already undone).
    Returns {"restored": n, "skipped": [{"file", "reason"}], "status": ...}."""
    history = _load(output_root)
    if not (0 <= run_id < len(history)):
        raise KeyError(run_id)
    entry = history[run_id]
    if _status(entry, output_root) == "undone":
        raise ValueError("this run was already undone")

    moves = _all_moves(entry)
    if not any(move.get("started") or move.get("completed", True) for move in moves):
        def abandon(entries: List[dict]) -> None:
            current = entries[run_id]
            current_moves = _all_moves(current)
            if any(move.get("started") or move.get("completed", True) for move in current_moves):
                raise UndoLogError("The interrupted run became active during recovery.")
            current["state"] = "abandoned"
            current["abandoned_at"] = datetime.now().isoformat(timespec="seconds")

        update_history(output_root, abandon)
        return {
            "restored": 0,
            "skipped": [],
            "status": "abandoned",
            "abandoned": True,
        }

    restored, skipped = 0, []
    for m in reversed(_moves(entry, output_root)):
        if m.get("restored"):
            continue
        src, dest = Path(m["src"]), Path(m["dest"])
        try:
            if not _inside(m["dest"], output_root):
                skipped.append({"file": dest.name, "reason": "outside the output folder (log ignored)"})
                continue
            if not dest.exists():
                skipped.append({"file": dest.name, "reason": "file is no longer there"})
                continue
            if os.path.lexists(src):
                skipped.append({"file": dest.name, "reason": "original location is already occupied"})
                continue
            try:
                original_source_root, original_output_root = _undo_roots(entry, output_root)
                validate = lambda: validate_move_paths(dest, src, original_source_root, original_output_root)
                validate()
                move_file_no_overwrite(dest, src, validate=validate)
            except UnsafePathError:
                skipped.append({"file": dest.name, "reason": "the path did not pass safety checks"})
                continue
            except OSError as error:
                reason = (
                    "file is locked or access was denied"
                    if isinstance(error, PermissionError)
                    else "the file could not be moved safely"
                )
                skipped.append({"file": dest.name, "reason": reason})
                continue

            def mark_restored(entries: List[dict]) -> None:
                current = entries[run_id]
                for move in _all_moves(current):
                    if move["src"] == m["src"] and move["dest"] == m["dest"]:
                        move["started"] = False
                        move["completed"] = True
                        move["restored"] = True
                        current["undone_at"] = datetime.now().isoformat(timespec="seconds")
                        return
                raise UndoLogError("The completed move is missing from the undo record.")

            restored += 1
            update_history(output_root, mark_restored)
            _prune_empty_dirs(dest.parent, Path(output_root))
        except UndoLogError:
            skipped.append({
                "file": dest.name,
                "reason": "restored, but the undo record could not be updated",
            })
            break

    latest = _load(output_root)
    status = (
        _status(latest[run_id], output_root)
        if 0 <= run_id < len(latest)
        else _status(entry, output_root)
    )
    return {"restored": restored, "skipped": skipped, "status": status}


def undo_last_run(output_root: str) -> Optional[Dict[str, object]]:
    """Undo the newest run that still has something to restore; None if there is none."""
    for run in list_runs(output_root):
        if run["status"] not in {"undone", "abandoned"}:
            res = undo_run(output_root, run["id"])
            if res.get("abandoned"):
                continue
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

