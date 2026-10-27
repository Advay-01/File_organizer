"""Preview identity regressions using only isolated temporary files and stub models."""
import errno
from contextlib import closing
import hashlib
import json
import os
import queue
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app import api
from app.core import executor, history, safety
from app.core.classifier import LLMClassifier
from app.core.content_cache import ContentIntelligenceCache
from app.core.content_intelligence import plan_content_intelligence, suggestion_to_move
from app.core.planner import Move, build_plan
from app.core.scanner import scan_directory


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ORGANIZER_HOME", str(tmp_path / "organizer-state"))


def make_move(root, name="source.txt"):
    source = root / name
    source.write_text("Content approved in the preview.", encoding="utf-8")
    return Move(str(source), str(root / "Docs" / "Notes" / name), "Docs", "preview", "Notes")


@pytest.mark.parametrize("change", ["unchanged", "modified", "missing", "replaced", "directory"])
def test_apply_checks_original_content_and_identity(tmp_path, change):
    move = make_move(tmp_path)
    source, destination = Path(move.src), Path(move.dest)
    original = source.read_bytes()
    assert move.source_snapshot.sha256 == hashlib.sha256(original).hexdigest()
    if change == "modified":
        info = source.stat()
        source.write_bytes(b"x" * len(original))
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
    elif change == "missing":
        source.unlink()
    elif change == "replaced":
        replacement = tmp_path / "replacement.txt"
        replacement.write_bytes(original)  # Identical contents are still a different file.
        os.replace(replacement, source)
    elif change == "directory":
        source.unlink()
        source.mkdir()

    result = executor.execute_plan([move], str(tmp_path), dry_run=False)

    if change == "unchanged":
        assert (result.moved, result.failed) == (1, 0)
        assert destination.read_bytes() == original
        assert history.undo_last_run(str(tmp_path))["restored"] == 1
        assert source.read_bytes() == original
    else:
        assert (result.moved, result.failed) == (0, 1)
        assert "reanalysis" in result.failures[0]["reason"]
        assert not destination.exists()


@pytest.mark.parametrize("missing", [False, True])
def test_change_after_journaling_is_caught_at_the_move_boundary(tmp_path, missing):
    move = make_move(tmp_path)
    real_update = executor._update_run

    def edit_after_intent(output_root, entry):
        real_update(output_root, entry)
        if entry["moves"][0]["started"]:
            if missing:
                Path(move.src).unlink()
            else:
                Path(move.src).write_text("Changed while Apply was saving its journal.")

    with patch.object(executor, "_update_run", side_effect=edit_after_intent):
        result = executor.execute_plan([move], str(tmp_path), dry_run=False)

    assert result.moved == 0
    assert "reanalysis" in result.failures[0]["reason"]
    assert Path(move.src).is_file() == (not missing)
    assert not Path(move.dest).exists()
    assert history.list_runs(str(tmp_path))[0]["files"] == 0


def test_user_edits_keep_the_original_snapshot_and_report_partial_failure(tmp_path):
    first, second = make_move(tmp_path, "first.txt"), make_move(tmp_path, "second.txt")
    plan = api.StoredPlan(tmp_path, [first, second])
    request = api.ApplyRequest(plan_id="unused", files=[
        api.ApplyFile(id="0", target_path="Research/Field Notes", destination_name="renamed.txt"),
        api.ApplyFile(id="1", target_path="Research/Field Notes"),
    ])
    Path(second.src).write_text("New content after the preview.")

    response = api._apply_plan(plan, request)
    payload = json.loads(response.body)

    assert response.status_code == 207
    assert (payload["moved"], payload["failed"]) == (1, 1)
    assert "reanalysis" in payload["failures"][0]["reason"]
    assert plan.moves[1].source_snapshot == second.source_snapshot
    assert (tmp_path / "Research" / "Field Notes" / "renamed.txt").is_file()
    assert Path(second.src).is_file()
    assert history.undo_last_run(str(tmp_path))["restored"] == 1
    assert Path(first.src).is_file()


def test_legacy_planner_binds_the_file_before_the_model_call(tmp_path):
    move = make_move(tmp_path)
    files = scan_directory(str(tmp_path))

    def completion(_model, _messages):
        Path(move.src).write_text("Different file content during classification.")
        return json.dumps({"actions": [{"path": move.src, "target_path": "Research/Field Notes"}]})

    moves = build_plan(files, str(tmp_path), LLMClassifier(["stub"], completion))
    assert moves[0].source_snapshot == files[0].source_snapshot
    result = executor.execute_plan(moves, str(tmp_path), dry_run=False)
    assert result.moved == 0
    assert "reanalysis" in result.failures[0]["reason"]


def intelligence_reply(_messages):
    return json.dumps({"files": [{
        "id": 0, "category": "Research", "subcategory": "Field Notes", "confidence": "high",
        "reasoning": "Content evidence", "evidence_quote": "Content approved in the preview",
    }]})


def test_scan_analysis_preview_edits_and_apply_hash_only_twice(tmp_path):
    move = make_move(tmp_path)
    # Move construction above is fixture setup, before the actual scan/analysis.
    with (
        patch("app.core.scanner.snapshot_file", wraps=safety.snapshot_file) as scan_hash,
        patch("app.core.safety.snapshot_file", wraps=safety.snapshot_file) as apply_hash,
        patch("app.core.content_intelligence.content_fingerprint") as extra_hash,
        closing(ContentIntelligenceCache(tmp_path / "cache.sqlite")) as cache,
    ):
        files = [f for f in scan_directory(str(tmp_path)) if Path(f.path).resolve() == Path(move.src).resolve()]
        result = plan_content_intelligence(files, str(tmp_path), ["stub"], cache=cache,
                                           completion_fn=intelligence_reply)
        suggestion = result.suggestions[0]
        planned = suggestion_to_move(suggestion, Path(move.dest))
        edited = api._apply_user_edits(api.StoredPlan(tmp_path, [planned]), api.ApplyRequest(
            plan_id="unused", files=[api.ApplyFile(id="0", destination_name="renamed.txt")],
        ))
        applied = executor.execute_plan(edited, str(tmp_path), dry_run=False)

    assert applied.moved == 1
    assert extra_hash.call_count == 0
    assert sum(call.args[0].resolve() == Path(move.src).resolve() for call in scan_hash.call_args_list) == 1
    assert sum(call.args[0].resolve() == Path(move.src).resolve() for call in apply_hash.call_args_list) == 1
    assert edited[0].source_snapshot == files[0].source_snapshot


def test_copy_fallback_detects_same_size_content_change_and_preserves_source(tmp_path):
    move = make_move(tmp_path)
    source = Path(move.src)

    def change_then_cross_device(src, dest):
        info = src.stat()
        src.write_bytes(b"x" * info.st_size)
        os.utime(src, ns=(info.st_atime_ns, info.st_mtime_ns))
        raise OSError(errno.EXDEV, "simulated separate volumes")

    with patch.object(safety, "_rename_no_replace", side_effect=change_then_cross_device):
        result = executor.execute_plan([move], str(tmp_path), dry_run=False)

    assert result.moved == 0
    assert source.is_file()
    assert not Path(move.dest).exists()
    assert "reanalysis" in result.failures[0]["reason"]


def test_replaced_source_symlink_is_never_followed(tmp_path):
    move = make_move(tmp_path)
    source = Path(move.src)
    victim = tmp_path / "victim.txt"
    victim.write_text("Untouched")
    source.unlink()
    try:
        source.symlink_to(victim)
    except OSError:
        pytest.skip("Creating file symlinks is unavailable")
    result = executor.execute_plan([move], str(tmp_path), dry_run=False)
    assert result.moved == 0
    assert victim.read_text() == "Untouched"
    assert source.is_symlink()


def test_source_parent_link_within_root_is_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "source.txt").write_text("Safe")
    link = tmp_path / "link"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is unavailable")
    with pytest.raises(safety.UnsafePathError):
        safety.validate_move_paths(link / "source.txt", tmp_path / "Docs" / "source.txt", tmp_path)


def test_destination_redirect_after_journaling_cannot_create_outside_directories(tmp_path):
    root, outside = tmp_path / "selected", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    move = make_move(root)
    redirect = root / "Docs"
    probe = tmp_path / "probe"
    try:
        probe.symlink_to(outside, target_is_directory=True)
        probe.unlink()
    except OSError:
        pytest.skip("Creating directory symlinks is unavailable")
    real_update = executor._update_run

    def redirect_after_intent(output_root, entry):
        real_update(output_root, entry)
        if entry["moves"][0]["started"]:
            redirect.symlink_to(outside, target_is_directory=True)

    with patch.object(executor, "_update_run", side_effect=redirect_after_intent):
        result = executor.execute_plan([move], str(root), dry_run=False)
    assert result.moved == 0
    assert list(outside.iterdir()) == []
    assert Path(move.src).is_file()


def test_evidence_changed_during_extraction_is_not_cached_under_the_old_hash(tmp_path):
    from app.core import content_intelligence

    move = make_move(tmp_path)
    files = scan_directory(str(tmp_path))
    original_hash = files[0].source_snapshot.sha256
    real_extract = content_intelligence.extract_evidence

    def change_during_extraction(file, **kwargs):
        Path(file.path).write_text("Changed evidence must not poison the original digest's cache.")
        return real_extract(file, **kwargs)

    with closing(ContentIntelligenceCache(tmp_path / "cache.sqlite")) as cache:
        with patch.object(content_intelligence, "extract_evidence", side_effect=change_during_extraction):
            result = plan_content_intelligence(files, str(tmp_path), ["stub"], cache=cache,
                                               completion_fn=intelligence_reply)
        assert cache.get_evidence(original_hash, ".txt") is None
    assert not result.suggestions[0].selected
    assert any("reanalysis" in warning for warning in result.suggestions[0].warnings)
    assert result.suggestions[0].source_snapshot.sha256 == original_hash


@pytest.mark.parametrize("as_path", [False, True])
def test_cached_classification_keeps_the_original_snapshot(tmp_path, as_path):
    from app.core.cache import OrganizeCache
    from app.core.cached_planner import plan_with_cache

    move = make_move(tmp_path)
    file = Path(move.src) if as_path else scan_directory(str(tmp_path))[0]
    with closing(OrganizeCache(tmp_path / "legacy-cache.sqlite")) as cache:
        cache.put_many([(Path(move.src), "Research/Field Notes", "cached classification")])
        result = plan_with_cache([file], str(tmp_path), ["stub"], cache=cache)
    assert result.stats["cache_hits"] == 1
    Path(move.src).write_text("Changed since cached preview")
    applied = executor.execute_plan(result.moves, str(tmp_path), dry_run=False)
    assert applied.moved == 0
    assert "reanalysis" in applied.failures[0]["reason"]


@pytest.mark.parametrize("fast", [False, True])
def test_cli_reports_stale_files_and_returns_failure(tmp_path, capsys, fast):
    from app import fast_run, main

    move = make_move(tmp_path)

    def stale_plan(*args, **kwargs):
        Path(move.src).write_text("Changed after analysis")
        if not fast:
            return [move]
        return SimpleNamespace(moves=[move], stats={
            "files": 1, "folders": 1, "seconds": 0, "calls": 0,
            "rate_limited": 0, "unclassified_units": 0,
        })

    module, planner = (fast_run, "plan_with_cache") if fast else (main, "build_plan")
    with patch.object(module, planner, side_effect=stale_plan):
        result = module.main([str(tmp_path), "--apply"])
    assert result == 1
    assert "reanalysis" in capsys.readouterr().out
    assert Path(move.src).is_file()


def test_legacy_gui_reports_an_empty_failed_execution():
    pytest.importorskip("tkinter")  # Optional legacy GUI runtime on native CI hosts.
    from app.gui import OrganizerApp

    result = executor.ExecutionResult()
    result.failures.append({"file": "stale.txt", "reason": "reanalysis is required"})
    events = queue.Queue()
    events.put(("success", result))
    window = SimpleNamespace(events=events, _set_busy=Mock(), _clear_plan=Mock(),
                             status_var=Mock(), _show_plan=Mock(), after=Mock(), _check_events=Mock())
    with patch("app.gui.messagebox.showwarning") as warning:
        OrganizerApp._check_events(window)
    assert "reanalysis" in warning.call_args.args[1]
    window._show_plan.assert_not_called()
    window._clear_plan.assert_called_once()
