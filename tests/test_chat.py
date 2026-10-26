"""Chat refines stored previews and never performs moves on its own."""
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import api
from app.core.chat import ChatResult, ChatRule
from app.core.content_intelligence import ContentSuggestion
from app.core.planner import Move


@pytest.fixture
def workspace(tmp_path):
    api.plans.clear()
    suggestions = []
    for index, name in enumerate(["leetcode.txt", "notes.txt", "credentials.txt"]):
        path = tmp_path / name
        path.write_text("DSA notes" if index == 0 else "ordinary notes")
        suggestions.append(ContentSuggestion(
            id=index, path=str(path), current_name=name, category="Education", subcategory="Notes",
            confidence="medium", reasoning="Lecture notes", evidence_quote="", name_evidence="",
            evidence_excerpt="data structures and algorithms" if index == 0 else "ordinary notes",
            evidence_sources=[], warnings=[], filename_conflict="none", suggested_name=name,
            rename_reason="", sufficient_for_change=True, detected_type=".txt",
            extraction_status="sufficient", sensitive=index == 2,
        ))
    moves = [Move(item.path, str(tmp_path / "Education" / "Notes" / item.current_name),
                  "Education", item.reasoning, "Notes") for item in suggestions]
    api.plans["initial"] = api.StoredPlan(tmp_path, moves, suggestions, scan_summary={
        "files_seen": 4, "skipped": 1, "reasons": {"recent": 1}, "truncated": True,
    })
    with patch.object(api.provider_store, "activate_runtime", return_value=["ollama/test"]):
        yield TestClient(api.app), tmp_path
    api.plans.clear()


def result(rules=None, reply="The preview is ready.", question=None):
    return json.dumps({"reply": reply, "rules": rules or [], "needs_clarification": question})


def move_rule(target="DSA", selector=None):
    return {"action": "move", "match": selector or {"extension": ".txt"},
            "target": target, "reason": "user request"}


def send(client, output, plan="initial", message="Group my notes", **kwargs):
    with patch("app.core.llm_http.direct_call_sync", return_value=output):
        return client.post("/api/chat", json={"plan_id": plan, "message": message, **kwargs})


def test_chat_only_previews_and_apply_uses_single_level_target(workspace):
    client, root = workspace
    response = send(client, result([move_rule()]))
    assert response.status_code == 200
    preview = response.json()["preview"]
    assert preview["files"][0]["target_path"] == "DSA"
    assert preview["files"][2]["target_path"] == "Education/Notes"
    assert not preview["files"][2]["selected"]
    assert preview["scan_summary"]["truncated"]
    assert preview["scan_summary"]["skipped"] == 1
    assert (root / "leetcode.txt").exists()
    assert not (root / "DSA").exists()
    assert api.plans["initial"].moves[0].folder == "Education"
    applied = client.post("/api/apply", json={"plan_id": preview["plan_id"], "files": [
        {"id": "0", "accepted": True, "target_path": "DSA", "destination_name": "leetcode.txt"},
    ]})
    assert applied.status_code == 200
    assert applied.json()["moved"] == 1
    assert (root / "DSA" / "leetcode.txt").exists()
    assert (root / "credentials.txt").exists()


@pytest.mark.parametrize("question", [None, "Which college subjects do you mean?"])
def test_answer_and_clarification_do_not_change_preview(workspace, question):
    client, _ = workspace
    response = send(client, result(reply="These look like lecture notes.", question=question))
    assert response.status_code == 200
    assert response.json()["preview"] is None
    assert len(api.plans) == 1
    assert api.plans["initial"].chat_messages


def test_newer_move_overrides_exclude_and_reports_conflict(workspace):
    client, _ = workspace
    excluded = send(client, result([{"action": "exclude", "match": {"glob": "*.txt"},
                                    "target": None, "reason": "user request"}])).json()["preview"]
    assert not excluded["files"][0]["selected"]
    newer = send(client, result([move_rule()]), plan=excluded["plan_id"]).json()
    assert newer["preview"]["files"][0]["selected"]
    assert "replaces an earlier instruction" in newer["result"]["reply"]
    assert len(api.plans[newer["preview"]["plan_id"]].chat_rules) == 2


def test_topic_matching_receives_redacted_metadata_and_known_ids(workspace):
    client, _ = workspace
    with patch("app.core.llm_http.direct_call_sync", side_effect=[
        result([move_rule(selector={"topic": "data structures and algorithms, leetcode"})]),
        '{"ids":[0]}',
    ]) as completion:
        response = client.post("/api/chat", json={"plan_id": "initial", "message": "Group DSA"})
    assert response.status_code == 200
    files = response.json()["preview"]["files"]
    assert files[0]["target_path"] == "DSA"
    assert files[1]["target_path"] == "Education/Notes"
    assert "credentials.txt" not in json.dumps(completion.call_args_list)


def test_unknown_topic_ids_rejected_without_storing_plan(workspace):
    client, _ = workspace
    with patch("app.core.llm_http.direct_call_sync", side_effect=[
        result([move_rule(selector={"topic": "DSA"})]), '{"ids":[999]}',
    ]):
        response = client.post("/api/chat", json={"plan_id": "initial", "message": "Group DSA"})
    assert response.status_code == 502
    assert len(api.plans) == 1


@pytest.mark.parametrize("target", ["../Escape", "/Absolute", "C:/Windows", "A/B/C", "CON", "A/..", ".git"])
def test_unsafe_targets_rejected(target):
    with pytest.raises(ValidationError):
        ChatRule.model_validate(move_rule(target))


def test_clarification_cannot_sneak_in_rules():
    with pytest.raises(ValidationError):
        ChatResult.model_validate_json(result([move_rule()], question="Where?"))


def test_provider_failure_is_safe_and_plan_is_unchanged(workspace):
    client, _ = workspace
    with patch("app.core.llm_http.direct_call_sync", side_effect=RuntimeError("secret provider key")):
        response = client.post("/api/chat", json={"plan_id": "initial", "message": "Group notes"})
    assert response.status_code == 502
    assert "secret provider key" not in response.text
    assert len(api.plans) == 1


def test_manual_decisions_survive_chat(workspace):
    client, _ = workspace
    response = send(client, result([move_rule(selector={"glob": "leetcode.*"})]), files=[
        {"id": "0", "accepted": True, "destination_name": "solution.txt"},
        {"id": "1", "accepted": False},
        {"id": "2", "accepted": False},
    ])
    assert response.status_code == 200
    files = response.json()["preview"]["files"]
    assert files[0]["destination_name"] == "solution.txt"
    assert not files[1]["selected"]


def test_consumed_or_missing_plan_cannot_be_chatted_with(workspace):
    client, _ = workspace
    api.plans["initial"].attempted = True
    assert send(client, result()).status_code == 409
    assert send(client, result(), plan="missing").status_code == 404
