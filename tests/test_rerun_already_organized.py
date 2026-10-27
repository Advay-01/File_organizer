"""Analyzing an already-organized folder must not rename files to 'name-1.ext'."""
from __future__ import annotations

import tempfile
import unittest
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import api
from app.core.content_intelligence import ContentSuggestion


def suggestion(path: Path, category: str, subcategory: str, name: str | None = None) -> ContentSuggestion:
    return ContentSuggestion(
        id=0, path=str(path), current_name=path.name, category=category, subcategory=subcategory,
        confidence="high", reasoning="r", evidence_quote="", name_evidence="", evidence_excerpt="",
        evidence_sources=[], warnings=[], filename_conflict="none", suggested_name=name or path.name,
        rename_reason="", sufficient_for_change=True, detected_type=".txt",
        extraction_status="sufficient", sensitive=False, selected=True,
    )


class RerunOnOrganizedFolder(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        api.plans.clear()

    def tearDown(self):
        api.plans.clear()
        self.temp.cleanup()

    def test_file_already_in_its_target_keeps_its_name_and_is_a_noop(self):
        placed = self.root / "AI" / "Machine Learning" / "cnn_notes.txt"
        placed.parent.mkdir(parents=True)
        placed.write_text("x")
        moves = api._content_moves([suggestion(placed, "AI", "Machine Learning")], self.root)
        self.assertEqual(Path(moves[0].dest), placed)
        self.assertFalse(moves[0].selected)

    def test_real_collision_still_gets_a_suffix(self):
        (self.root / "AI" / "Machine Learning").mkdir(parents=True)
        (self.root / "AI" / "Machine Learning" / "notes.txt").write_text("existing")
        incoming = self.root / "notes.txt"
        incoming.write_text("new")
        moves = api._content_moves([suggestion(incoming, "AI", "Machine Learning")], self.root)
        self.assertEqual(Path(moves[0].dest).name, "notes-1.txt")
        self.assertTrue(moves[0].selected)

    def test_noop_file_reserves_its_name_for_others(self):
        placed = self.root / "A" / "B" / "same.txt"
        placed.parent.mkdir(parents=True)
        placed.write_text("x")
        other = self.root / "same.txt"
        other.write_text("y")
        moves = api._content_moves(
            [suggestion(placed, "A", "B"), suggestion(other, "A", "B")], self.root,
        )
        self.assertEqual(Path(moves[0].dest), placed)
        self.assertEqual(Path(moves[1].dest).name, "same-1.txt")

    def test_existing_collision_suffix_is_not_incremented_on_rerun(self):
        placed = self.root / "AI" / "Machine Learning" / "notes-1.txt"
        placed.parent.mkdir(parents=True)
        placed.write_text("second set of notes")
        placed.with_name("notes.txt").write_text("first set of notes")
        moves = api._content_moves([suggestion(placed, "AI", "Machine Learning", "notes.txt")], self.root)
        self.assertEqual(Path(moves[0].dest), placed)
        self.assertFalse(moves[0].selected)
        self.assertFalse(placed.with_name("notes-2.txt").exists())

    def test_two_file_analyze_apply_rerun_is_a_noop_without_a_model(self):
        contents = {"budget.txt": b"Annual budget", "notes.txt": b"Neural network notes"}
        targets = {"budget.txt": ("Finance", "Planning"), "notes.txt": ("AI", "Machine Learning")}
        for name, content in contents.items():
            (self.root / name).write_bytes(content)

        def classify(files, *_args, **_kwargs):
            return SimpleNamespace(suggestions=[
                suggestion(Path(file.path), *targets[file.filename]) for file in files
            ], stats={})

        with patch.dict(os.environ, {"ORGANIZER_RECENT_SECONDS": "0"}), \
             patch.object(api.provider_store, "activate_runtime", return_value=["test/model"]), \
             patch.object(api, "plan_content_intelligence", side_effect=classify), \
             patch.object(api.history, "remember_folder"):
            first = api.preview(api.PreviewRequest(source=str(self.root), recursive=True, rename=True))
            self.assertEqual(first.already_organized, 0)
            applied = api.apply(api.ApplyRequest(plan_id=first.plan_id, files=[
                api.ApplyFile(id=file.id, accepted=True, target_path=file.target_path, destination_name=file.destination_name)
                for file in first.files
            ]))
            self.assertEqual(applied["moved"], 2)
            journal = (self.root / ".organizer_undo.json").read_bytes()

            second = api.preview(api.PreviewRequest(source=str(self.root), recursive=True, rename=True))
            self.assertEqual(second.already_organized, 2)
            self.assertTrue(all(not file.selected and not file.renamed for file in second.files))
            rerun = api.apply(api.ApplyRequest(plan_id=second.plan_id, files=[
                api.ApplyFile(id=file.id, accepted=True, target_path=file.target_path, destination_name=file.destination_name)
                for file in second.files
            ]))
            self.assertEqual((rerun["requested"], rerun["moved"], rerun["failed"]), (0, 0, 0))
            self.assertEqual((self.root / ".organizer_undo.json").read_bytes(), journal)
        for name, content in contents.items():
            self.assertEqual(self.root.joinpath(*targets[name], name).read_bytes(), content)
        self.assertFalse(list(self.root.rglob("*-1.txt")))

    def test_folder_group_selection_does_not_reenable_a_noop(self):
        placed = self.root / "AI" / "Machine Learning" / "notes.txt"
        placed.parent.mkdir(parents=True)
        placed.write_text("notes")
        item = suggestion(placed, "AI", "Machine Learning")
        plan = api.StoredPlan(self.root, api._content_moves([item], self.root), [item])
        edited = api._apply_user_edits(plan, api.ApplyRequest(plan_id="unused", selected_targets=["AI/Machine Learning"]))
        self.assertFalse(edited[0].selected)
        self.assertEqual(Path(edited[0].dest), placed)

    def test_chat_matching_current_folder_keeps_names_and_reports_noops(self):
        placed = self.root / "AI" / "Machine Learning" / "notes.txt"
        placed.parent.mkdir(parents=True)
        placed.write_text("notes")
        item = suggestion(placed, "AI", "Machine Learning")
        api.plans["chat"] = api.StoredPlan(self.root, api._content_moves([item], self.root), [item])
        output = json.dumps({"reply": "Grouped notes.", "needs_clarification": None, "rules": [{
            "action": "move", "match": {"extension": ".txt"}, "target": "AI/Machine Learning", "reason": "user request",
        }]})
        with patch.object(api.provider_store, "activate_runtime", return_value=["test/model"]), \
             patch("app.core.llm_http.direct_call_sync", return_value=output):
            response = api.chat(api.ChatRequest(plan_id="chat", message="Put notes in AI/Machine Learning", files=[
                api.ApplyFile(id="0", accepted=True, target_path="AI/Machine Learning", destination_name=placed.name),
            ]))
        self.assertEqual(response["preview"]["already_organized"], 1)
        self.assertFalse(response["preview"]["files"][0]["selected"])
        self.assertEqual(response["preview"]["files"][0]["destination_name"], placed.name)
        self.assertEqual(placed.read_text(), "notes")


if __name__ == "__main__":
    unittest.main()
