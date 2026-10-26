"""Model fallback when a reply arrives but cannot be used (prose, wrong JSON, truncated JSON).

Regression: such replies used to escape as ValueError/ValidationError, skip the remaining models and
surface as the generic "unexpected error" banner instead of falling back or showing the real cause.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import content_intelligence as ci
from app.core import llm_http
from app.models.schemas import FileInfo

GOOD = json.dumps({"files": [{
    "id": 0,
    "category": "AI",
    "subcategory": "Lecture Notes",
    "confidence": "high",
    "reasoning": "Machine learning notes.",
    "evidence_quote": "gradient descent",
    "filename_conflict": "none",
    "suggested_name": "",
    "name_evidence": "",
}]})
MODELS = ["anthropic/claude-haiku-5-5", "deepseek/deepseek-chat"]


def scripted(replies):
    """direct_call_sync replacement: model name -> reply text, or an Exception to raise."""
    calls = []

    def fake(model, messages, timeout=0, extra_body=None):
        calls.append(model)
        value = replies[model]
        if isinstance(value, Exception):
            raise value
        return value

    return fake, calls


class ModelFallbackTests(unittest.TestCase):
    def run_call(self, replies, expected_ids={0}):
        fake, calls = scripted(replies)
        with mock.patch.object(llm_http, "direct_call_sync", fake):
            return ci._model_call(MODELS, [], expected_ids), calls

    def test_unparseable_reply_falls_back_to_next_model(self):
        for bad in (
            "Sure! Here is my analysis of the files.",       # prose, no JSON
            json.dumps({"results": []}),                       # wrong shape
            '{"files": [{"id": 0, "category": "AI", "sub',     # truncated
            json.dumps({"files": []}),                         # no usable files
        ):
            with self.subTest(bad=bad[:30]):
                raw, calls = self.run_call({MODELS[0]: bad, MODELS[1]: GOOD})
                self.assertEqual(raw, GOOD)
                self.assertEqual(calls, MODELS)

    def test_first_good_reply_stops_the_chain(self):
        raw, calls = self.run_call({MODELS[0]: GOOD, MODELS[1]: "never called"})
        self.assertEqual(raw, GOOD)
        self.assertEqual(calls, [MODELS[0]])

    def test_model_added_evidence_does_not_reject_a_valid_reply(self):
        payload = json.loads(GOOD)
        payload['files'][0]['subcategory_evidence'] = ''
        payload['files'][0]['extra_explanation'] = 'Optional provider commentary'
        payload['summary'] = 'One file classified'
        reply = json.dumps(payload)
        raw, calls = self.run_call({MODELS[0]: reply, MODELS[1]: 'never called'})
        parsed = ci._parse_response(raw, {0})
        self.assertEqual(calls, [MODELS[0]])
        self.assertEqual(parsed[0]['subcategory'], 'Lecture Notes')
        self.assertNotIn('subcategory_evidence', parsed[0])
        self.assertNotIn('extra_explanation', parsed[0])

    def test_annotations_do_not_hide_invalid_decision_fields(self):
        for changes in (
            {'confidence': 'certain'},
            {'category': ''},
            {'id': 99},
        ):
            payload = json.loads(GOOD)
            payload['files'][0].update(changes, subcategory_evidence='')
            with self.subTest(changes=changes):
                raw, calls = self.run_call({MODELS[0]: json.dumps(payload), MODELS[1]: GOOD})
                self.assertEqual(raw, GOOD)
                self.assertEqual(calls, MODELS)

    def test_missing_required_field_with_annotations_is_still_rejected(self):
        payload = json.loads(GOOD)
        del payload['files'][0]['subcategory']
        payload['files'][0]['subcategory_evidence'] = ''
        raw, calls = self.run_call({MODELS[0]: json.dumps(payload), MODELS[1]: GOOD})
        self.assertEqual(raw, GOOD)
        self.assertEqual(calls, MODELS)

    def test_duplicate_ids_with_annotations_are_still_rejected(self):
        payload = json.loads(GOOD)
        payload['files'][0]['subcategory_evidence'] = ''
        payload['files'].append(dict(payload['files'][0]))
        raw, calls = self.run_call({MODELS[0]: json.dumps(payload), MODELS[1]: GOOD})
        self.assertEqual(raw, GOOD)
        self.assertEqual(calls, MODELS)

    def test_transport_error_still_falls_back(self):
        raw, calls = self.run_call({
            MODELS[0]: llm_http.LLMHTTPError("anthropic HTTP 529", 529),
            MODELS[1]: GOOD,
        })
        self.assertEqual(raw, GOOD)

    def test_all_unusable_raises_runtime_error_naming_each_model(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_call({MODELS[0]: "not json", MODELS[1]: json.dumps({"results": []})})
        message = str(ctx.exception)
        self.assertIn("All content-intelligence models failed", message)
        self.assertIn("anthropic/claude-haiku-5-5: unusable reply", message)
        self.assertIn("deepseek/deepseek-chat: unusable reply", message)

    def test_without_expected_ids_the_reply_is_returned_unvalidated(self):
        fake, _calls = scripted({MODELS[0]: "not json", MODELS[1]: GOOD})
        with mock.patch.object(llm_http, "direct_call_sync", fake):
            self.assertEqual(ci._model_call(MODELS, []), "not json")

    def test_planner_uses_good_fallback_suggestion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "notes.txt"
            source.write_text("gradient descent trains a neural network", encoding="utf-8")
            info = FileInfo(
                path=str(source),
                filename=source.name,
                extension=".txt",
                size_kb=1,
                date_created="2026-01-01",
                snippet="gradient descent trains a neural network",
            )
            fake, calls = scripted({MODELS[0]: "I cannot format JSON.", MODELS[1]: GOOD})
            with mock.patch.object(llm_http, "direct_call_sync", fake):
                result = ci.plan_content_intelligence(
                    [info],
                    str(root),
                    MODELS,
                    use_cache=False,
                )

        self.assertEqual(calls, MODELS)
        self.assertEqual(len(result.suggestions), 1)
        self.assertEqual(result.suggestions[0].category, "AI")
        self.assertEqual(result.suggestions[0].subcategory, "Lecture Notes")


if __name__ == "__main__":
    unittest.main()
