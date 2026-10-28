"""Large chat previews keep complete matching scope and bounded intent prompts."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.core import chat
from app.core.chat_resolution import TOPIC_MAX_CHARS, parser_context, resolve_topic


def context(size=120):
    return {
        "folder_tree": {"incoming": {"count": size, "samples": ["notes.txt"]}},
        "existing_categories": ["Courses/Physics", "Purchases/Invoices"],
        "active_rules": [{"version": 1, "action": "move", "match": {"extension": ".txt"},
                          "target": "Courses/Physics", "reason": "user", "naming": None}],
        "rules_version": 3,
        "files": [{"id": index, "path": f"incoming/file-{index}.txt", "extension": ".txt",
                   "destination_name": f"file-{index}.txt", "target": "Courses/Physics",
                   "reason": "Lecture notes", "evidence": "physics" if index % 2 == 0 else "invoice"}
                  for index in range(size)],
    }


def physics_matcher(calls):
    def completion(models, messages, schema):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        return chat.TopicMatches(ids=[item["id"] for item in payload["files"] if item["evidence"] == "physics"])
    return completion


def test_all_files_are_checked_across_bounded_batches():
    full = context(125)
    calls = []
    with patch("app.core.chat.call_json", side_effect=physics_matcher(calls)):
        result = resolve_topic("all physics files", full, ["fixture/local"])
    assert result.complete
    assert result.ids == list(range(0, 125, 2))
    seen = [item["id"] for payload in calls for item in payload["files"]]
    assert seen == list(range(125))
    assert all(len(payload["files"]) <= 48 for payload in calls)
    assert all(len(json.dumps(payload, sort_keys=True)) <= TOPIC_MAX_CHARS for payload in calls)


def test_semantic_order_keeps_every_nonembedded_file_in_scope():
    full = context(100)
    index = SimpleNamespace(search=Mock(return_value=[(80, .95), (2, .8), (80, .5), (999, 1.0)]))
    calls = []
    with patch("app.core.chat.call_json", side_effect=physics_matcher(calls)):
        result = resolve_topic("all physics files", full, ["fixture/local"], semantic_index=index)
    assert result.ids == list(range(0, 100, 2))
    seen = [item["id"] for payload in calls for item in payload["files"]]
    assert seen[:2] == [80, 2]
    assert sorted(seen) == list(range(100))
    index.search.assert_called_once_with("all physics files", allowed_ids=set(range(100)), limit=None)


def test_missing_semantic_backend_falls_back_to_complete_id_order():
    index = SimpleNamespace(search=Mock(side_effect=RuntimeError("offline")))
    calls = []
    with patch("app.core.chat.call_json", side_effect=physics_matcher(calls)):
        result = resolve_topic("physics", context(5), ["fixture/local"], semantic_index=index)
    assert result.ids == [0, 2, 4]
    assert [item["id"] for item in calls[0]["files"]] == list(range(5))


def test_complete_resolution_cache_is_order_independent_and_folder_aware():
    full = context(50)
    cache = {}
    calls = []
    with patch("app.core.chat.call_json", side_effect=physics_matcher(calls)):
        initial = resolve_topic("physics", full, ["fixture/local"], cache=cache)
        assert len(calls) == 2
        full["files"].reverse()
        cached = resolve_topic("physics", full, ["fixture/local"], cache=cache)
        assert cached == initial
        assert len(calls) == 2
        full["files"][0]["target"] = "Purchases/Invoices"
        resolve_topic("physics", full, ["fixture/local"], cache=cache)
        assert len(calls) == 4


@pytest.mark.parametrize("field", ["path", "destination_name", "evidence", "reason", "extension"])
def test_changes_to_full_file_context_invalidate_cache(field):
    full = context(2)
    calls, cache = [], {}
    with patch("app.core.chat.call_json", side_effect=physics_matcher(calls)):
        resolve_topic("physics", full, ["fixture/local"], cache=cache)
        full["files"][0][field] += " changed"
        resolve_topic("physics", full, ["fixture/local"], cache=cache)
    assert len(calls) == 2


def test_each_batch_rejects_ids_from_other_batches():
    cache = {}
    with patch("app.core.chat.call_json", return_value=chat.TopicMatches(ids=[60])):
        with pytest.raises(ValueError, match="unknown file IDs"):
            resolve_topic("physics", context(100), ["fixture/local"], cache=cache)
    assert cache == {}


def test_duplicate_matching_ids_and_identical_context_records_are_deduplicated():
    full = context(2)
    full["files"].append(dict(full["files"][0]))
    with patch("app.core.chat.call_json", return_value=chat.TopicMatches(ids=[0, 0])) as model:
        result = resolve_topic("physics", full, ["fixture/local"])
    assert result.ids == [0]
    assert len(json.loads(model.call_args.args[1][-1]["content"])["files"]) == 2


def test_conflicting_records_for_one_id_are_rejected():
    full = context(2)
    full["files"].append({**full["files"][0], "evidence": "different evidence"})
    with pytest.raises(ValueError, match="conflicting"):
        resolve_topic("physics", full, ["fixture/local"])


def test_character_budget_splits_a_batch_without_dropping_evidence():
    full = context(15)
    for item in full["files"]:
        item["evidence"] = "x" * 6_000
    calls = []
    with patch("app.core.chat.call_json", side_effect=physics_matcher(calls)):
        result = resolve_topic("physics", full, ["fixture/local"])
    assert result.complete
    assert len(calls) > 1
    assert sum(len(payload["files"]) for payload in calls) == 15
    assert all(len(json.dumps(payload, sort_keys=True)) <= TOPIC_MAX_CHARS for payload in calls)


def test_one_oversized_file_fails_before_any_provider_call():
    full = context(2)
    full["files"][1]["evidence"] = "x" * TOPIC_MAX_CHARS
    with patch("app.core.chat.call_json") as model:
        with pytest.raises(ValueError, match="exceeds"):
            resolve_topic("physics", full, ["fixture/local"])
    model.assert_not_called()


def test_empty_context_needs_no_model_or_semantic_call():
    index = SimpleNamespace(search=Mock())
    with patch("app.core.chat.call_json") as model:
        result = resolve_topic("physics", context(0), [], semantic_index=index)
    assert result.complete and result.ids == []
    model.assert_not_called()
    index.search.assert_not_called()


def test_parser_context_is_bounded_and_explicitly_sampled():
    full = context(300)
    for item in full["files"]:
        item["evidence"] *= 100
    parsed = parser_context(full, max_chars=5_000)
    assert len(json.dumps(parsed, sort_keys=True)) <= 5_000
    assert parsed["file_count"] == 300
    assert parsed["files_are_samples"]
    assert 0 < len(parsed["files"]) < 300
    assert parsed["active_rules"] == full["active_rules"]
    assert parsed["existing_categories"] == full["existing_categories"]
    assert parsed["rules_version"] == 3


def test_parser_small_context_preserves_all_file_ids():
    full = context(3)
    parsed = parser_context(full)
    assert [item["id"] for item in parsed["files"]] == [0, 1, 2]
    assert not parsed["files_are_samples"]
    assert parsed["folder_tree"] == full["folder_tree"]


def test_parser_ranking_is_only_sampling_not_complete_scope():
    full = context(200)
    index = SimpleNamespace(search=Mock(return_value=[(198, .9), (0, .8)]))
    parsed = parser_context(full, "physics", index, max_chars=2_000)
    assert parsed["files"][0]["id"] == 198
    assert parsed["file_count"] == 200
    assert parsed["files_are_samples"]


def test_sensitive_records_and_unknown_sensitive_fields_never_reach_prompts():
    full = context(4)
    full["secret"] = "private top-level data"
    full["files"][0]["private_metadata"] = "private embedded data"
    full["files"][1].update(sensitive=True, evidence="private record")
    full["folder_tree"]["incoming"]["private"] = "private folder data"
    full["active_rules"][0]["private"] = "private rule data"
    parsed = parser_context(full)
    assert "private" not in json.dumps(parsed)
    calls = []
    with patch("app.core.chat.call_json", side_effect=physics_matcher(calls)):
        result = resolve_topic("physics", full, ["fixture/local"])
    assert result.ids == [0, 2]
    assert "private" not in json.dumps(calls)


def test_huge_folder_and_rule_summaries_have_explicit_omission_counts():
    full = context(2)
    full["folder_tree"] = {f"folder-{index}": {"count": 1, "samples": ["sample.txt"]} for index in range(500)}
    full["active_rules"] *= 300
    full["existing_categories"] = [f"Category {index}" for index in range(500)]
    parsed = parser_context(full, max_chars=4_000)
    assert len(json.dumps(parsed, sort_keys=True)) <= 4_000
    assert parsed["omitted_rule_count"] > 0
    assert parsed["omitted_folder_count"] > 0
    assert parsed["omitted_category_count"] > 0
