"""Root detection and undo boundaries, with real moves in temporary directories."""
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import executor, history, safety
from app.core.planner import Move
from app.core.scanner import scan_directory


@pytest.mark.parametrize("marker", ["package.json", "pyproject.toml", "Cargo.toml"])
def test_unrelated_ancestor_manifest_does_not_block_ordinary_folder(tmp_path, marker):
    (tmp_path / marker).write_text("{}")
    root = tmp_path / "Downloads" / "Documents"
    root.mkdir(parents=True)
    (root / "notes.txt").write_text("Ordinary notes")
    assert safety.assert_safe_root(root) == root.resolve()
    assert [file.filename for file in scan_directory(str(root))] == ["notes.txt"]


@pytest.mark.parametrize("marker", safety.PROJECT_MARKERS)
def test_selected_project_root_is_protected(tmp_path, marker):
    (tmp_path / marker).touch()
    with pytest.raises(safety.UnsafePathError):
        safety.assert_safe_root(tmp_path)


@pytest.mark.parametrize("marker,companion", [
    (".git", None), ("package.json", "package-lock.json"),
    ("pyproject.toml", "uv.lock"), ("Cargo.toml", "src"),
])
def test_confirmed_project_descendants_remain_protected(tmp_path, marker, companion):
    (tmp_path / marker).touch()
    if companion == "src":
        (tmp_path / companion).mkdir()
    elif companion:
        (tmp_path / companion).touch()
    root = tmp_path / "nested" / "data"
    root.mkdir(parents=True)
    with pytest.raises(safety.UnsafePathError):
        safety.assert_safe_root(root)


def test_sensitive_descendants_and_nested_git_projects_are_protected(tmp_path):
    protected = tmp_path / "node_modules" / "package" / "assets"
    protected.mkdir(parents=True)
    with pytest.raises(safety.UnsafePathError):
        safety.assert_safe_root(protected)
    project = tmp_path / "software"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "code.txt").write_text("Never organize this.")
    (tmp_path / "notes.txt").write_text("Ordinary notes")
    assert [file.filename for file in scan_directory(str(tmp_path), recursive=True)] == ["notes.txt"]


def apply_files(tmp_path, separate=True):
    source = tmp_path / "source" / "incoming"
    source.mkdir(parents=True)
    output = tmp_path / "destination" / "organized" if separate else source
    moves = []
    for name in ("first.txt", "second.txt", "third.txt"):
        src = source / "nested" / name
        src.parent.mkdir(exist_ok=True)
        src.write_text(name)
        dest = output / "Research" / "Notes" / f"renamed-{name}"
        moves.append(Move(str(src), str(dest), "Research", "model", "Notes"))
    result = executor.execute_plan(moves, str(output), dry_run=False, source_root=str(source))
    assert (result.moved, result.failed) == (3, 0)
    return source, output, moves


@pytest.mark.parametrize("separate", [False, True])
def test_move_rename_history_and_undo_preserve_both_roots(tmp_path, separate):
    source, output, moves = apply_files(tmp_path, separate)
    entry = history.load_history_for_update(str(output))[0]
    assert entry["source_root"] == str(source.resolve())
    assert entry["output_root"] == str(output.resolve())
    assert history.list_runs(str(output))[0]["renamed"] == 3
    result = history.undo_run(str(output), 0)
    assert (result["restored"], result["skipped"], result["status"]) == (3, [], "undone")
    assert all(Path(move.src).read_text() == Path(move.src).name for move in moves)
    assert not (output / "Research").exists()
    assert output.is_dir()


def test_cross_root_undo_conflicts_and_partial_failures_are_retryable(tmp_path):
    source, output, moves = apply_files(tmp_path)
    Path(moves[0].src).write_text("New user file must survive.")
    real_move = history.move_file_no_overwrite

    def locked(src, dest, **kwargs):
        if src == Path(moves[1].dest):
            raise PermissionError("simulated lock")
        return real_move(src, dest, **kwargs)

    with patch.object(history, "move_file_no_overwrite", side_effect=locked):
        partial = history.undo_run(str(output), 0)
    assert (partial["restored"], len(partial["skipped"]), partial["status"]) == (1, 2, "partially undone")
    assert Path(moves[0].src).read_text() == "New user file must survive."
    assert Path(moves[0].dest).exists() and Path(moves[1].dest).exists()
    Path(moves[0].src).unlink()
    retried = history.undo_run(str(output), 0)
    assert (retried["restored"], retried["status"]) == (2, "undone")
    assert all(Path(move.src).exists() for move in moves)


def test_cross_root_undo_can_recreate_missing_original_directories(tmp_path):
    source, output, moves = apply_files(tmp_path)
    (source / "nested").rmdir()
    source.rmdir()
    source.parent.rmdir()
    result = history.undo_run(str(output), 0)
    assert (result["restored"], result["status"]) == (3, "undone")
    assert all(Path(move.src).exists() for move in moves)


def test_old_journal_without_root_or_completion_fields_can_still_undo(tmp_path):
    src, dest = tmp_path / "old.txt", tmp_path / "Docs" / "renamed.txt"
    dest.parent.mkdir()
    dest.write_text("Legacy content")
    history.write_history(str(tmp_path), [{"time": "old", "moves": [{"src": str(src), "dest": str(dest)}]}])
    result = history.undo_run(str(tmp_path), 0)
    assert (result["restored"], result["status"]) == (1, "undone")
    assert src.read_text() == "Legacy content"


@pytest.mark.parametrize("tamper", ["source_path", "output_root", "invalid_root", "missing_root"])
def test_recorded_boundaries_are_validated_before_restoration(tmp_path, tamper):
    source, output, moves = apply_files(tmp_path)
    entries = history.load_history_for_update(str(output))
    outside = tmp_path / "outside.txt"
    if tamper == "source_path":
        for move in entries[0]["moves"]:
            move["src"] = str(outside)
    elif tamper == "output_root":
        entries[0]["output_root"] = str(tmp_path)
    elif tamper == "invalid_root":
        entries[0]["source_root"] = ["invalid"]
    else:
        del entries[0]["output_root"]
    history.write_history(str(output), entries)
    result = history.undo_run(str(output), 0)
    assert result["restored"] == 0
    assert len(result["skipped"]) == 3
    assert not outside.exists()
    assert all(Path(move.dest).is_file() for move in moves)


def test_cross_root_interrupted_completion_is_recovered(tmp_path):
    source, output, moves = apply_files(tmp_path)
    entries = history.load_history_for_update(str(output))
    entries[0]["state"] = "applying"
    entries[0]["moves"][-1].update(started=True, completed=False)
    history.write_history(str(output), entries)
    result = history.undo_run(str(output), 0)
    assert (result["restored"], result["status"]) == (3, "undone")


def test_execution_does_not_hide_a_symlink_selected_root_by_resolving_it(tmp_path):
    root = tmp_path / "real"
    root.mkdir()
    src = root / "notes.txt"
    src.write_text("Keep in place")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is unavailable")
    move = Move(str(src), str(root / "Docs" / src.name), "Docs", "preview")
    result = executor.execute_plan([move], str(alias), dry_run=False, source_root=str(alias))
    assert result.moved == 0
    assert src.read_text() == "Keep in place"
    assert not (root / history.UNDO_LOG).exists()
