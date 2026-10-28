"""Versioned chat naming policies refine previews using existing file evidence."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.core.chat import ChatResult, ChatRule, TopicResolution, context_for, refine
from app.core.content_intelligence import ContentSuggestion
from app.core.planner import Move


def preview(tmp_path, names=None):
    suggestions, moves = [], []
    for index, original in enumerate(names or ["scan001.txt", "lecture-notes.txt", "private.txt"]):
        source = tmp_path / original
        source.write_text("Thermodynamics lecture notes discuss heat transfer and entropy.")
        proposed = "Thermodynamics_Heat_Transfer_and_Entropy_Lecture_Notes.txt"
        suggestion = ContentSuggestion(
            id=index, path=str(source), current_name=original, category="Physics", subcategory="Notes",
            confidence="high", reasoning="Content discusses thermodynamics", evidence_quote="heat transfer",
            name_evidence="Thermodynamics lecture notes", evidence_excerpt="Thermodynamics lecture notes",
            evidence_sources=["extracted_content"], warnings=[], filename_conflict="generic",
            suggested_name=proposed, rename_reason="Grounded in evidence: Thermodynamics lecture notes",
            sufficient_for_change=True, detected_type=".txt", extraction_status="sufficient",
            sensitive=original == "private.txt",
        )
        suggestions.append(suggestion)
        moves.append(Move(str(source), str(tmp_path / "Physics" / "Notes" / proposed), "Physics", "content", "Notes"))
    return SimpleNamespace(source=tmp_path, moves=moves, suggestions=suggestions,
                           chat_decisions={}, chat_rules=[])


def naming_rule(policy, selector=None):
    return ChatRule(action="rename", match=selector or {"extension": ".txt"}, target=None,
                    naming=policy, reason="User naming preference")


def apply_rules(plan, rules, **kwargs):
    result = ChatResult(reply="Updated naming policy.", rules=rules, needs_clarification=None)
    moves, suggestions, conflict, affected, decisions = refine(plan, result, context_for(plan), ["fixture/local"], **kwargs)
    updated = SimpleNamespace(source=plan.source, moves=moves, suggestions=suggestions,
                              chat_decisions=decisions, chat_rules=[*plan.chat_rules, *rules])
    return updated, conflict, affected


def test_legacy_rules_default_to_schema_version_one():
    rule = ChatRule(action="move", match={"extension": ".txt"}, target="Physics/Notes", reason="user")
    assert rule.version == 1
    assert rule.model_dump()["version"] == 1
    with pytest.raises(ValidationError):
        ChatRule(action="move", version=2, match={"extension": ".txt"}, target="Physics", reason="user")


@pytest.mark.parametrize("policy", [{}, {"enabled": None}, {"max_length": 19}, {"max_length": 121},
                                    {"max_length": "60"}, {"enabled": "true"}, {"invented": "name"}])
def test_invalid_naming_policy_is_rejected(policy):
    with pytest.raises(ValidationError):
        naming_rule(policy)


def test_naming_policy_is_restricted_to_rename_action():
    with pytest.raises(ValidationError):
        ChatRule(action="move", match={"extension": ".txt"}, target="Physics", reason="user",
                 naming={"max_length": 60})
    with pytest.raises(ValidationError):
        ChatRule(action="rename", match={"extension": ".txt"}, target="Physics", reason="user",
                 naming={"max_length": 60})
    with pytest.raises(ValidationError):
        ChatRule(action="rename", match={"extension": ".txt"}, target=None, reason="user")


def test_chat_cannot_automatically_release_assets_requiring_relationship_review(tmp_path):
    plan = preview(tmp_path)
    plan.suggestions[0].requires_relationship_review = True
    original_name = Path(plan.moves[0].dest).name
    rules = [ChatRule(action="move", match={"folder": "Physics"}, target="Research/Notes", reason="user"),
             naming_rule({"enabled": False})]
    moves, suggestions, *_ = refine(plan, ChatResult(reply="Review the plan.", rules=rules, needs_clarification=None),
                                     context_for(plan), ["fixture/local"])
    assert not moves[0].selected and not suggestions[0].selected
    assert Path(moves[0].dest).name == original_name
    assert moves[0].folder == "Physics"
    assert moves[1].folder == "Research"


@pytest.mark.parametrize("folder", ["../Escape", "/Absolute", "C:/Windows", "A/B/C", ".git", "CON"])
def test_folder_selector_rejects_unsafe_scopes(folder):
    with pytest.raises(ValidationError):
        ChatRule(action="move", match={"folder": folder}, target="Research", reason="user")


def test_disable_renames_preserves_snapshot_and_selection(tmp_path):
    plan = preview(tmp_path)
    plan.moves[1].selected = False
    updated, _, affected = apply_rules(plan, [naming_rule({"enabled": False})])
    assert affected == {0, 1}
    assert Path(updated.moves[0].dest).name == "scan001.txt"
    assert updated.moves[0].source_snapshot == plan.moves[0].source_snapshot
    assert not updated.moves[1].selected
    assert not updated.moves[2].selected
    assert Path(updated.moves[2].dest).name == Path(plan.moves[2].dest).name
    assert Path(plan.moves[0].dest).name != "scan001.txt"
    assert Path(updated.moves[0].src).is_file()
    assert not Path(updated.moves[0].dest).exists()
    assert updated.suggestions[0].reasoning == plan.suggestions[0].reasoning


def test_newer_naming_setting_merges_with_previous_policy(tmp_path):
    plan = preview(tmp_path)
    first, _, _ = apply_rules(plan, [naming_rule({"enabled": False, "max_length": 35})])
    second, conflict, _ = apply_rules(first, [naming_rule({"enabled": True})])
    assert conflict
    assert len(Path(second.moves[0].dest).name) <= 35
    assert Path(second.moves[0].dest).name != "scan001.txt"
    assert "naming:0" in second.chat_decisions


def test_folder_and_naming_decisions_do_not_conflict(tmp_path):
    plan = preview(tmp_path)
    move = ChatRule(action="move", match={"extension": ".txt"}, target="Courses/Physics", reason="course")
    first, _, _ = apply_rules(plan, [move])
    second, conflict, _ = apply_rules(first, [naming_rule({"max_length": 30})])
    assert not conflict
    assert second.moves[0].folder == "Courses"
    assert len(Path(second.moves[0].dest).name) <= 30


def test_generic_only_keeps_descriptive_source_names(tmp_path):
    plan = preview(tmp_path)
    updated, _, _ = apply_rules(plan, [naming_rule({"enabled": True, "generic_only": True})])
    assert Path(updated.moves[0].dest).name != "scan001.txt"
    assert Path(updated.moves[1].dest).name == "lecture-notes.txt"


def test_ungrounded_suggestion_cannot_be_enabled_by_chat(tmp_path):
    plan = preview(tmp_path, ["scan001.txt"])
    plan.suggestions[0].rename_reason = "Unsupported suggestion"
    plan.suggestions[0].suggested_name = "Invented_Title.txt"
    updated, _, _ = apply_rules(plan, [naming_rule({"enabled": True})])
    assert Path(updated.moves[0].dest).name == "scan001.txt"


def test_shortening_preserves_dates_and_extension(tmp_path):
    plan = preview(tmp_path, ["Thermodynamics_2026-10-27_lecture_notes_for_revision.txt"])
    plan.moves[0].dest = str(tmp_path / "Physics" / "Notes" / Path(plan.moves[0].src).name)
    updated, _, _ = apply_rules(plan, [naming_rule({"max_length": 32})])
    name = Path(updated.moves[0].dest).name
    assert len(name) <= 32
    assert "2026-10-27" in name
    assert name.endswith(".txt")


def test_impossible_date_limit_is_reported_without_dropping_dates(tmp_path):
    original = "Report_2025-01-01_to_2026-12-31.txt"
    plan = preview(tmp_path, [original])
    plan.moves[0].dest = str(tmp_path / "Physics" / "Notes" / original)
    updated, _, _ = apply_rules(plan, [naming_rule({"max_length": 20})])
    assert Path(updated.moves[0].dest).name == original
    assert any("could not be met" in warning for warning in updated.suggestions[0].warnings)


def test_complete_topic_resolver_covers_all_matching_files(tmp_path):
    plan = preview(tmp_path, [f"scan{index:03d}.txt" for index in range(24)])
    seen = []
    def resolver(topic, context):
        seen.extend(file["id"] for file in context["files"])
        return TopicResolution(ids=seen, complete=True)
    with patch("app.core.chat.call_json") as model:
        updated, _, affected = apply_rules(plan, [naming_rule({"max_length": 30}, {"topic": "all physics files"})],
                                           topic_resolver=resolver)
    model.assert_not_called()
    assert affected == set(range(24))
    assert seen == list(range(24))
    assert all(len(Path(move.dest).name) <= 30 for move in updated.moves)


def test_incomplete_topic_resolution_falls_back_to_all_files(tmp_path):
    plan = preview(tmp_path)
    with patch("app.core.chat.call_json", return_value=SimpleNamespace(ids=[0, 1])) as model:
        updated, _, affected = apply_rules(plan, [naming_rule({"enabled": False}, {"topic": "physics"})],
                                           topic_resolver=lambda topic, context: {"ids": [0], "complete": False})
    assert affected == {0, 1}
    sent = model.call_args.args[1][-1]["content"]
    assert '"id": 1' in sent
    assert "private.txt" not in sent
    assert not updated.moves[2].selected


def test_unknown_resolver_ids_are_rejected_without_mutating_plan(tmp_path):
    plan = preview(tmp_path)
    original = deepcopy(plan.moves)
    with pytest.raises(ValueError, match="unknown file IDs"):
        apply_rules(plan, [naming_rule({"enabled": False}, {"topic": "physics"})],
                    topic_resolver=lambda topic, context: {"ids": [999], "complete": True})
    assert plan.moves == original


def test_rename_clarification_cannot_include_rules():
    with pytest.raises(ValidationError):
        ChatResult(reply="Which files?", needs_clarification="Which files?",
                   rules=[naming_rule({"enabled": False})])


def test_compound_extension_survives_length_policy(tmp_path):
    original = "An_Archive_With_A_Long_Descriptive_Name.tar.gz"
    plan = preview(tmp_path, [original])
    plan.moves[0].dest = str(tmp_path / "Physics" / "Notes" / original)
    updated, _, _ = apply_rules(plan, [naming_rule({"max_length": 24}, {"glob": "*.tar.gz"})])
    assert Path(updated.moves[0].dest).name.endswith(".tar.gz")
    assert len(Path(updated.moves[0].dest).name) <= 24


def test_date_preservation_warning_clears_after_relaxing_limit(tmp_path):
    original = "Report_2025-01-01_to_2026-12-31.txt"
    plan = preview(tmp_path, [original])
    plan.moves[0].dest = str(tmp_path / "Physics" / "Notes" / original)
    first, _, _ = apply_rules(plan, [naming_rule({"max_length": 20})])
    second, _, _ = apply_rules(first, [naming_rule({"max_length": 60})])
    assert not any("could not be met" in warning for warning in second.suggestions[0].warnings)


@pytest.mark.parametrize("ids,complete", [([True], True), (["0"], True), ([0], "true")])
def test_resolver_cannot_coerce_untrusted_ids_or_completeness(ids, complete):
    with pytest.raises(ValidationError):
        TopicResolution(ids=ids, complete=complete)
