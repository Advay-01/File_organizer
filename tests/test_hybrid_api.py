"""Hybrid preview/chat integration uses fixture models and disposable files only."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import api
from app.core import history
from app.core.content_cache import ContentIntelligenceCache
from app.core.content_intelligence import plan_content_intelligence


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "selected"
    root.mkdir()
    monkeypatch.setenv("ORGANIZER_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ORGANIZER_RECENT_SECONDS", "0")
    monkeypatch.setenv("ORGANIZER_SEMANTIC_ENABLED", "0")
    (root / "scan001.txt").write_text("Heat transfer lecture notes explain thermodynamics. " * 8)
    (root / "scan002.txt").write_text("Entropy practice exercises explain thermodynamics. " * 8)
    (root / "credentials.txt").write_text("API key sk-abcdefghijklmnopqrstuvwx. " * 5)
    cache = ContentIntelligenceCache(tmp_path / "cache.sqlite")
    requests = []

    def complete(messages):
        payload = json.loads(messages[-1]["content"])
        requests.append(payload)
        rows = []
        for card in payload["files"]:
            exercises = "Entropy practice" in card["content"]
            title = "Entropy_Practice" if exercises else "Heat_Transfer_Lecture"
            quote = "Entropy practice exercises" if exercises else "Heat transfer lecture notes"
            rows.append({"id": card["id"], "category": "Physics",
                         "subcategory": "Exercises" if exercises else "Lecture Notes",
                         "confidence": "high", "reasoning": "Subject and purpose are supported.",
                         "evidence_quote": quote, "filename_conflict": "generic",
                         "suggested_name": title + ".txt", "name_evidence": quote})
        return json.dumps({"files": rows})

    def planner(*args, **kwargs):
        return plan_content_intelligence(*args, **kwargs, cache=cache, completion_fn=complete)

    api.plans.clear()
    with (
        patch.object(api.provider_store, "activate_runtime", return_value=["fixture/local"]),
        patch.object(api, "plan_content_intelligence", side_effect=planner),
    ):
        yield TestClient(api.app), root, requests
    api.plans.clear()
    cache.close()


def preview(client, root, **kwargs):
    response = client.post("/api/preview", json={"source": str(root), "rename": True, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def chat(client, plan_id, rule=None, question=None):
    reply = json.dumps({"reply": "Review the updated proposal.", "rules": [rule] if rule else [],
                        "needs_clarification": question})
    with patch("app.core.llm_http.direct_call_sync", return_value=reply):
        return client.post("/api/chat", json={"plan_id": plan_id, "message": "Update this preview"})


def rename_rule(**policy):
    return {"version": 1, "action": "rename", "match": {"extension": ".txt"},
            "target": None, "reason": "User naming preference.", "naming": policy}


def test_initial_chat_instructions_reach_planning_before_refinement(workspace):
    client, root, requests = workspace
    plan = preview(client, root, instructions=["Organize by subject; keep exercises separate."])
    assert requests[0]["organization_instructions"] == ["Organize by subject; keep exercises separate."]
    assert "sk-abcdefghijklmnopqrstuvwx" not in json.dumps(requests)
    assert len(requests[0]["files"]) == 2
    assert api.plans[plan["plan_id"]].organization_instructions
    assert plan["analysis_stats"]["embedding_status"] == "disabled"
    assert plan["analysis_stats"]["scan_seconds"] >= 0
    assert all((root / file["name"]).exists() for file in plan["files"])


def test_instruction_changes_invalidate_decisions_but_keep_extraction_cache(workspace):
    client, root, requests = workspace
    first = preview(client, root, instructions=["Use subject-first folders."])
    again = preview(client, root, instructions=["Use subject-first folders."])
    assert len(requests) == 1
    assert again["analysis_stats"]["cache_hits"] == 2
    changed = preview(client, root, instructions=["Use purpose-first folders."])
    assert len(requests) == 2
    assert first["files"][0]["name"] == changed["files"][0]["name"]


def test_naming_chat_keeps_semantic_index_and_fingerprints_and_supports_apply_undo(workspace):
    client, root, _ = workspace
    initial = preview(client, root)
    original = api.plans[initial["plan_id"]]
    fingerprints = [move.source_snapshot for move in original.moves]
    response = chat(client, initial["plan_id"], rename_rule(enabled=False))
    assert response.status_code == 200, response.text
    updated = response.json()["preview"]
    stored = api.plans[updated["plan_id"]]
    assert updated["rules_revision"] == 1
    assert stored.chat_rules[-1].version == 1
    assert stored.semantic_index is original.semantic_index
    assert [move.source_snapshot for move in stored.moves] == fingerprints
    assert all(file["destination_name"] == file["name"] for file in updated["files"])
    assert not next(file for file in updated["files"] if file["sensitive"])["selected"]
    applied = client.post("/api/apply", json={"plan_id": updated["plan_id"], "files": [
        {"id": file["id"], "accepted": file["selected"], "target_path": file["target_path"],
         "destination_name": file["destination_name"]} for file in updated["files"]
    ]})
    assert applied.status_code == 200, applied.text
    assert applied.json()["moved"] == 2
    assert history.undo_last_run(str(root))["restored"] == 2
    assert all((root / name).exists() for name in ("scan001.txt", "scan002.txt", "credentials.txt"))


def test_clarification_blocks_apply_until_a_valid_rule_resolves_it(workspace):
    client, root, _ = workspace
    initial = preview(client, root)
    question = chat(client, initial["plan_id"], question="Which course should these belong to?")
    assert question.status_code == 200
    blocked = client.post("/api/apply", json={"plan_id": initial["plan_id"], "selected_targets": ["Physics/Exercises"]})
    assert blocked.status_code == 409
    assert "clarification" in blocked.json()["detail"]
    resolved = chat(client, initial["plan_id"], rename_rule(max_length=40)).json()["preview"]
    assert api.plans[resolved["plan_id"]].pending_clarification is None
    assert not api.plans[initial["plan_id"]].attempted


def test_chat_can_explicitly_request_names_when_initial_rename_toggle_was_off(workspace):
    client, root, requests = workspace
    initial = preview(client, root, rename=False)
    assert all(file["destination_name"] == file["name"] for file in initial["files"])
    response = chat(client, initial["plan_id"], rename_rule(enabled=True, generic_only=True))
    assert response.status_code == 200, response.text
    files = response.json()["preview"]["files"]
    names = {file["name"]: file["destination_name"] for file in files}
    assert names["scan001.txt"] == "Heat_Transfer_Lecture.txt"
    assert names["scan002.txt"] == "Entropy_Practice.txt"
    assert names["credentials.txt"] == "credentials.txt"
    assert len(requests) == 2
    assert all((root / name).exists() for name in names)


def test_folder_merge_uses_a_deterministic_full_scope_without_topic_calls(workspace):
    client, root, _ = workspace
    initial = preview(client, root)
    rule = {"version": 1, "action": "move", "match": {"folder": "Physics"},
            "target": "My Course/Study Material", "reason": "Merge the proposed course folders."}
    reply = json.dumps({"reply": "Review the merged folders.", "rules": [rule], "needs_clarification": None})
    with patch("app.core.llm_http.direct_call_sync", return_value=reply) as model:
        response = client.post("/api/chat", json={"plan_id": initial["plan_id"], "message": "Combine the Physics folders"})
    assert response.status_code == 200, response.text
    assert model.call_count == 1
    files = response.json()["preview"]["files"]
    assert {file["target_path"] for file in files if not file["sensitive"]} == {"My Course/Study Material"}
    assert not next(file for file in files if file["sensitive"])["selected"]


@pytest.mark.parametrize("instructions", [[""], ["x" * 4001], ["x"] * 21])
def test_instruction_budgets_are_enforced_before_planning(workspace, instructions):
    client, root, requests = workspace
    response = client.post("/api/preview", json={"source": str(root), "instructions": instructions})
    assert response.status_code == 422
    assert requests == []
