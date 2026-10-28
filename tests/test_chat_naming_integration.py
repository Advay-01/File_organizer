"""Regression cases for explicit naming intent, no-ops, and policy-aware collisions."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import api
from app.core import history
from app.core.content_intelligence import ContentSuggestion, IntelligenceResult
from app.core.planner import Move


@pytest.fixture(autouse=True)
def isolated_plans(tmp_path, monkeypatch):
    api.plans.clear()
    monkeypatch.setenv("ORGANIZER_HOME", str(tmp_path / "runtime"))
    yield
    api.plans.clear()


def stored_plan(root, names, *, proposed="Thermodynamics_Lecture_Notes.txt", grounded=True):
    moves, suggestions = [], []
    for index, name in enumerate(names):
        source = root / "Physics" / "Notes" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("Thermodynamics lecture notes explain heat transfer and entropy. " * 5)
        suggestions.append(ContentSuggestion(
            id=index, path=str(source), current_name=name, category="Physics", subcategory="Notes",
            confidence="high", reasoning="Thermodynamics lecture content.", evidence_quote="heat transfer",
            name_evidence="Thermodynamics lecture notes" if grounded else "", evidence_excerpt="heat transfer",
            evidence_sources=["extracted_content"], warnings=[], filename_conflict="generic",
            suggested_name=proposed if grounded else name,
            rename_reason="Grounded in evidence: Thermmodynamics lecture notes" if grounded else "",
            sufficient_for_change=True, detected_type=".txt", extraction_status="sufficient", selected=False,
        ))
        moves.append(Move(str(source), str(source), "Physics", "content", "Notes", False))
    plan = api.StoredPlan(root, moves, suggestions)
    api.plans["initial"] = plan
    return plan


def refine(policy):
    response = {"reply": "Review the filenames.", "needs_clarification": None, "rules": [{
        "version": 1, "action": "rename", "match": {"folder": "Physics"}, "target": None,
        "reason": "Explicit user naming instruction.", "naming": policy,
    }]}
    with (patch.object(api.provider_store, "activate_runtime", return_value=["fixture/local"]),
          patch("app.core.llm_http.direct_call_sync", return_value=json.dumps(response))):
        result = TestClient(api.app).post("/api/chat", json={"plan_id": "initial", "message": "Update names"})
    assert result.status_code == 200, result.text
    return result.json()["preview"]


def apply(preview):
    return TestClient(api.app).post("/api/apply", json={"plan_id": preview["plan_id"], "files": [
        {"id": file["id"], "accepted": file["selected"], "target_path": file["target_path"],
         "destination_name": file["destination_name"]} for file in preview["files"]
    ]})


def test_explicit_rename_of_previously_organized_file_is_selected_applied_and_undoable(tmp_path):
    root = tmp_path / "selected"
    plan = stored_plan(root, ["scan001.txt"])
    original = Path(plan.moves[0].src)
    snapshot = plan.moves[0].source_snapshot
    preview = refine({"enabled": True})
    assert preview["files"][0]["selected"]
    assert preview["files"][0]["destination_name"] == "Thermodynamics_Lecture_Notes.txt"
    assert api.plans[preview["plan_id"]].moves[0].source_snapshot == snapshot
    result = apply(preview)
    assert result.status_code == 200, result.text
    assert result.json()["moved"] == 1 and not original.exists()
    assert history.undo_last_run(str(root))["restored"] == 1
    assert original.is_file()


def test_explicit_exclusion_is_not_reselected_by_a_naming_policy(tmp_path):
    plan = stored_plan(tmp_path / "selected", ["scan001.txt"])
    plan.chat_decisions[0] = ("exclude", None)
    preview = refine({"enabled": True})
    assert not preview["files"][0]["selected"]
    assert Path(plan.moves[0].src).is_file()


def test_collision_suffixes_respect_the_name_cap_in_preview_and_apply(tmp_path):
    root = tmp_path / "selected"
    stored_plan(root, ["scan001.txt", "scan002.txt"], proposed="Supported_Thermodynamics_Lecture_Notes.txt")
    preview = refine({"enabled": True, "max_length": 30})
    names = [file["destination_name"] for file in preview["files"]]
    assert len(set(names)) == 2 and all(len(name) <= 30 and name.endswith(".txt") for name in names)
    result = apply(preview)
    assert result.status_code == 200, result.text
    assert result.json()["moved"] == 2
    assert history.undo_last_run(str(root))["restored"] == 2


def test_dated_generic_name_gets_a_missing_grounded_proposal(tmp_path):
    root = tmp_path / "selected"
    plan = stored_plan(root, ["scan001_2026-10-27.txt"], grounded=False)
    generated = deepcopy(plan.suggestions[0])
    generated.suggested_name = "Thermodynamics.txt"
    generated.name_evidence = "Thermodynamics lecture notes"
    generated.rename_reason = "Grounded in evidence: Thermodynamics lecture notes"
    with patch.object(api, "plan_content_intelligence", return_value=IntelligenceResult([generated])) as planner:
        preview = refine({"enabled": True, "generic_only": True})
    assert planner.call_count == 1
    name = preview["files"][0]["destination_name"]
    assert name == "Thermodynamics_2026-10-27.txt"
    assert preview["files"][0]["selected"]


def test_impossible_date_and_length_constraint_is_visible_and_unselected(tmp_path):
    root = tmp_path / "selected"
    plan = stored_plan(root, ["scan001_2020-01-01_2021-01-01.txt"])
    preview = refine({"enabled": True, "max_length": 20, "preserve_dates": True})
    file = preview["files"][0]
    assert not file["selected"]
    assert any("length limit" in warning for warning in file["warnings"])
    file["selected"] = True  # Accepting the unchanged impossible proposal cannot bypass its cap.
    result = apply(preview)
    assert result.status_code == 409
    assert Path(plan.moves[0].src).is_file()


def test_collision_preserves_dates_and_complete_compound_extension():
    original = "scan001_2026-10-27.tar.gz"
    name = api.chat_engine.collision_filename("Lecture_2026-10-27.tar.gz", original,
                                             {"max_length": 28, "preserve_dates": True}, "-1")
    assert name is not None and len(name) <= 28
    assert "2026-10-27" in name and name.endswith(".tar.gz")


@pytest.mark.parametrize("policy", [{"enabled": False, "max_length": 20},
                                    {"generic_only": True, "max_length": 20}])
def test_keep_original_policy_does_not_reapply_an_older_length_limit(policy):
    original = "Detailed_Thermodynamics_Research_Record.txt"
    assert api.chat_engine.collision_filename(original, original, policy) == original
    assert api.chat_engine.collision_filename(original, original, policy, "-1") is None
