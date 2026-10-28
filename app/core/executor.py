"""Apply a plan to disk, with dry-run and an undo log."""
from __future__ import annotations

import logging
import errno
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List
from uuid import uuid4
from time import perf_counter

from app.core.history import (
    UndoLogError,
    update_history,
)
from app.core.planner import Move
from app.core.safety import (
    StaleSourceError, UnsafePathError, file_identity, move_file_no_overwrite, validate_move_paths,
)

logger = logging.getLogger(__name__)


def _safe_print(message: str) -> None:
    """Write diagnostics without allowing the console encoding to abort Apply."""
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_message = message.encode(encoding, "replace").decode(encoding, "replace")
        print(safe_message)


class ExecutionResult(list[dict]):
    """Completed moves plus a safe, user-visible summary of what failed."""

    def __init__(self) -> None:
        super().__init__()
        self.failures: list[dict] = []
        self.state = "failed"
        self.log_error: str | None = None
        self.undo_available = False

    @property
    def moved(self) -> int:
        return len(self)

    @property
    def failed(self) -> int:
        return len(self.failures)


def _failure(name: str, reason: str) -> dict:
    return {"file": name, "reason": reason}


def _move_failure_reason(error: OSError) -> str:
    if isinstance(error, PermissionError) or getattr(error, "winerror", None) in {5, 32, 33}:
        return "file is locked or access was denied"
    if isinstance(error, FileExistsError) or error.errno == errno.EEXIST:
        return "destination already exists"
    return "the file could not be moved safely"


def _journal_write_failure(result: ExecutionResult, action: str, error: UndoLogError) -> None:
    logger.exception("%s; no further files will be moved", action)
    result.log_error = (
        "The undo record could not be updated safely. "
        "No further files will be moved; review History or Undo before trying again."
    )
    result.state = "partial" if result.moved else "failed"


def _append_run(output_root: str, entry: dict) -> None:
    update_history(output_root, lambda entries: entries.append(entry))


def _update_run(output_root: str, entry: dict) -> None:
    def replace(entries: list[dict]) -> None:
        for index, current in enumerate(entries):
            if current.get("run_id") == entry["run_id"]:
                entries[index] = entry
                return
        raise UndoLogError("The pending undo record is missing.")

    update_history(output_root, replace)


def execute_plan(
    moves: List[Move],
    output_root: str,
    dry_run: bool = True,
    source_root: str | None = None,
    analysis_stats: dict | None = None,
) -> ExecutionResult:
    """Move files through a durable per-run undo journal.

    A validated intent record is written before the first move. Each move marks
    itself started and completed in the journal before the next file begins. If
    journal persistence fails, no additional destructive operation is attempted.
    The returned list contains only files that were actually moved.
    """
    started = perf_counter()
    result = ExecutionResult()
    selected = [
        m
        for m in moves
        if m.selected
    ]
    for m in moves:
        if not m.selected:
            src = Path(m.src)
            category = "/".join(part for part in (m.folder, m.subfolder) if part)
            _safe_print(f"  {src.name}  ->  kept in place (unselected: {category})")
    if dry_run:
        result.state = "success"
        return result
    if not selected:
        result.state = "success"
        return result

    # Retain lexical roots for validation so resolving them cannot hide a junction.
    approved_output = Path(output_root).expanduser().absolute()
    approved_source = Path(source_root).expanduser().absolute() if source_root else approved_output
    journal_moves: list[dict] = []
    move_plan: list[tuple[Move, Path, Path, dict]] = []
    for m in selected:
        src, dest = Path(m.src), Path(m.dest)
        category = "/".join(part for part in (m.folder, m.subfolder) if part)
        _safe_print(f"  {src.name}  ->  {category}/{dest.name}   ({m.reason})")
        try:
            if not m.source_snapshot or not m.source_snapshot.sha256 or file_identity(src) != m.source_snapshot.identity:
                raise StaleSourceError("Source changed since analysis.")
            validate_move_paths(src, dest, approved_output, approved_source)
        except (StaleSourceError, FileNotFoundError):
            result.failures.append(_failure(src.name, "source changed or is missing; reanalysis is required"))
            continue
        except (UnsafePathError, OSError) as error:
            logger.warning("Skipped unsafe move for %s: %s", src, error)
            result.failures.append(_failure(src.name, "the path did not pass safety checks"))
            continue
        if os.path.lexists(dest):
            result.failures.append(_failure(src.name, "destination already exists"))
            continue
        record = {"src": str(src.absolute()), "dest": str(dest.absolute()),
                  "source_sha256": m.source_snapshot.sha256, "started": False, "completed": False}
        journal_moves.append(record)
        move_plan.append((m, src, dest, record))

    if not move_plan:
        result.state = "failed"
        return result

    run_key = uuid4().hex
    entry = {
        "run_id": run_key,
        "time": datetime.now().isoformat(timespec="seconds"),
        "state": "applying",
        "source_root": str(approved_source.resolve(strict=False)),
        "output_root": str(approved_output.resolve(strict=False)),
        "moves": journal_moves,
        "failures": list(result.failures),
    }
    if analysis_stats is not None:
        entry["analysis_stats"] = {
            key: value for key, value in analysis_stats.items()
            if type(value) in {str, int, float, bool} and (
                key.startswith(("embedding_", "llm_", "folder_plan_"))
                or key in {"files", "cache_hits", "extraction_seconds", "scan_seconds", "total_seconds", "classification_workers"}
            )
        }
    try:
        _append_run(output_root, entry)
        result.undo_available = True
    except UndoLogError as error:
        _journal_write_failure(
            result,
            "Refused to move files because the initial undo record could not be saved",
            error,
        )
        return result

    for m, src, dest, record in move_plan:
        record["started"] = True
        try:
            _update_run(output_root, entry)
        except UndoLogError as error:
            _journal_write_failure(result, "Could not save move progress before moving a file", error)
            break

        try:
            move_file_no_overwrite(
                src, dest, expected_snapshot=m.source_snapshot,
                validate=lambda: validate_move_paths(src, dest, approved_output, approved_source),
            )
        except (OSError, UnsafePathError) as error:
            logger.exception("Could not move %s to %s", src, dest)
            record["started"] = False
            reason = (
                "source changed or is missing; reanalysis is required"
                if isinstance(error, (StaleSourceError, FileNotFoundError)) else
                "the path did not pass safety checks"
                if isinstance(error, UnsafePathError) else _move_failure_reason(error)
            )
            result.failures.append(_failure(src.name, reason))
            entry["failures"] = list(result.failures)
            try:
                _update_run(output_root, entry)
            except UndoLogError as journal_error:
                _journal_write_failure(result, "Could not save a failed move to the undo record", journal_error)
                break
            continue

        record["started"] = False
        record["completed"] = True
        result.append({"src": str(src), "dest": str(dest)})
        try:
            _update_run(output_root, entry)
        except UndoLogError as error:
            _journal_write_failure(result, "A file moved but its completed state could not be saved", error)
            break

    entry["state"] = "completed" if not result.failures and not result.log_error else (
        "partial" if result.moved else "failed"
    )
    entry["failures"] = list(result.failures)
    entry["execution_stats"] = {"apply_seconds": round(perf_counter() - started, 4)}
    try:
        _update_run(output_root, entry)
    except UndoLogError as error:
        _journal_write_failure(result, "Could not finalize the undo record", error)

    result.state = "success" if entry["state"] == "completed" and not result.log_error else (
        "partial" if result.moved else "failed"
    )
    return result

def undo_last(output_root: str) -> int:
    """Reverse the most recent run using the hardened history implementation."""
    from app.core.history import undo_last_run

    result = undo_last_run(output_root)
    if result is None:
        print("Nothing to undo.")
        return 0
    return int(result["restored"])
