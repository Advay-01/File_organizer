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
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

UNDO_LOG = ".organizer_undo.json"
SAMPLE_SIZE = 5


# --------------------------------------------------------------------------- log io


def _log_path(output_root: str) -> Path:
    return Path(output_root) / UNDO_LOG


def _load(output_root: str) -> List[dict]:
    """Read the log; a missing, unreadable or corrupt file simply means 'no history'."""
    p = _log_path(output_root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _save(output_root: str, history: List[dict]) -> None:
    p = _log_path(output_root)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(history, indent=2), encoding="utf-8")
    os.replace(tmp, p)  # atomic: a crash never leaves a half-written log


# --------------------------------------------------------------------------- helpers


def _moves(entry: dict) -> List[dict]:
    return [m for m in entry.get("moves", []) if isinstance(m, dict) and "src" in m and "dest" in m]


def _folder_of(dest: str, output_root: str) -> str:
    try:
        return Path(dest).parent.relative_to(output_root).as_posix() or "."
    except ValueError:
        return Path(dest).parent.name or "."


def _status(entry: dict) -> str:
    moves = _moves(entry)
    done = sum(1 for m in moves if m.get("restored"))
    if not moves or done == 0:
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
        moves = _moves(entry)
        runs.append({
            "id": i,
            "time": entry.get("time", ""),
            "files": len(moves),
            "renamed": sum(1 for m in moves if Path(m["src"]).name != Path(m["dest"]).name),
            "folders": dict(Counter(_folder_of(m["dest"], output_root) for m in moves).most_common()),
            "status": _status(entry),
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
    } for m in _moves(entry)]
    return {"id": run_id, "time": entry.get("time", ""), "status": _status(entry),
            "undone_at": entry.get("undone_at"), "moves": moves}


def undo_run(output_root: str, run_id: int) -> Dict[str, object]:
    """Move one run's files back. Raises KeyError (unknown id) or ValueError (already undone).
    Returns {"restored": n, "skipped": [{"file", "reason"}], "status": ...}."""
    history = _load(output_root)
    if not (0 <= run_id < len(history)):
        raise KeyError(run_id)
    entry = history[run_id]
    if _status(entry) == "undone":
        raise ValueError("this run was already undone")

    restored, skipped = 0, []
    for m in reversed(_moves(entry)):
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
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dest), str(src))
            except OSError as e:
                skipped.append({"file": dest.name, "reason": f"error: {e}"})
                continue
            m["restored"] = True
            restored += 1
            _prune_empty_dirs(dest.parent, Path(output_root))

    if restored:
        entry["undone_at"] = datetime.now().isoformat(timespec="seconds")
        _save(output_root, history)
    return {"restored": restored, "skipped": skipped, "status": _status(entry)}


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

