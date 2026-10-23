"""Deterministic tests for Content Intelligence planning and application."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app import api
from app.core import history
from app.core.content_cache import ContentIntelligenceCache
from app.core.content_intelligence import (
    ContentSuggestion,
    plan_content_intelligence,
)
from app.core.content_intelligence import sanitize_windows_filename
from app.core.evidence import content_fingerprint, extract_evidence
from app.models.schemas import FileInfo


def file_info(path: Path) -> FileInfo:
    return FileInfo(
        path=str(path),
        filename=path.name,
        extension=path.suffix.lower(),
        size_kb=1,
        date_created="2026-01-01",
    )


def response_for(rows):
    return json.dumps({"files": rows})


class ContentIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = ContentIntelligenceCache(self.root / "intelligence.db")

    def tearDown(self):
        self.cache.close()
        self.temp.cleanup()

    def test_misleading_filename_is_overridden_by_strong_content(self):
        source = self.root / "holiday_notes.txt"
        source.write_text(
            "University Fee Receipt Invoice Number INV-2026-044. "
            "Student tuition payment received. Total amount paid and payment date. " * 5,
            encoding="utf-8",
        )
        calls = []

        def completion(_messages):
            calls.append(1)
            return response_for([
                {
                    "id": 0,
                    "category": "Finance",
                    "subcategory": "Receipts",
                    "confidence": "high",
                    "reasoning": "The content is a university fee receipt.",
                    "evidence_quote": "University Fee Receipt Invoice Number",
                    "filename_conflict": "misleading",
                    "suggested_name": "University_Fee_Receipt.txt",
                    "name_evidence": "University Fee Receipt",
                }
            ])

        result = plan_content_intelligence(
            [file_info(source)],
            str(self.root),
            ["fake/model"],
            cache=self.cache,
            completion_fn=completion,
            allow_sensitive_cloud=True,
        )
        item = result.suggestions[0]
        self.assertEqual(calls, [1])
        self.assertEqual(item.target_path, "Finance/Receipts")
        self.assertEqual(item.suggested_name, "University_Fee_Receipt.txt")
        self.assertEqual(item.confidence, "high")
        self.assertEqual(item.filename_conflict, "misleading")
        self.assertTrue(item.sufficient_for_change)

    def test_correctly_named_file_remains_unchanged(self):
        source = self.root / "Calculus_Notes.txt"
        source.write_text("Derivative and integral calculus notes. " * 10, encoding="utf-8")
        result = plan_content_intelligence(
            [file_info(source)],
            str(self.root),
            ["fake/model"],
            cache=self.cache,
            completion_fn=lambda _: response_for([
                {
                    "id": 0,
                    "category": "Mathematics",
                    "subcategory": "Calculus",
                    "confidence": "high",
                    "reasoning": "Calculus content is explicit.",
                    "evidence_quote": "Derivative and integral calculus",
                    "filename_conflict": "none",
                    "suggested_name": "",
                    "name_evidence": "",
                }
            ]),
        )
        self.assertEqual(result.suggestions[0].suggested_name, "Calculus_Notes.txt")
        self.assertEqual(result.suggestions[0].rename_reason, "")

    def test_generic_name_receives_grounded_name(self):
        source = self.root / "IMG_1234.txt"
        source.write_text("Chemistry lab report experiment observations and results. " * 5, encoding="utf-8")
        result = plan_content_intelligence(
            [file_info(source)],
            str(self.root),
            ["fake/model"],
            cache=self.cache,
            completion_fn=lambda _: response_for([
                {
                    "id": 0,
                    "category": "Chemistry",
                    "subcategory": "Lab Reports",
                    "confidence": "high",
                    "reasoning": "The content is a chemistry lab report.",
                    "evidence_quote": "Chemistry lab report experiment observations",
                    "filename_conflict": "generic",
                    "suggested_name": "Chemistry_Lab_Report.txt",
                    "name_evidence": "Chemistry lab report",
                }
            ]),
        )
        self.assertEqual(result.suggestions[0].suggested_name, "Chemistry_Lab_Report.txt")
        self.assertEqual(result.suggestions[0].filename_conflict, "generic")

    def test_insufficient_or_unreadable_content_skips_model_and_rename(self):
        source = self.root / "blob.bin"
        source.write_bytes(b"\x00\x01\x02\x03" * 20)
        calls = []
        result = plan_content_intelligence(
            [file_info(source)],
            str(self.root),
            ["fake/model"],
            cache=self.cache,
            completion_fn=lambda _: calls.append(1) or "{}",
        )
        self.assertEqual(calls, [])
        self.assertEqual(result.suggestions[0].confidence, "insufficient")
        self.assertEqual(result.suggestions[0].suggested_name, "blob.bin")
        self.assertEqual(result.suggestions[0].target_path, "Misc/Unreadable")

    def test_weak_content_does_not_get_a_high_confidence_rename(self):
        source = self.root / "misc.txt"
        source.write_text("Chemistry", encoding="utf-8")
        result = plan_content_intelligence(
            [file_info(source)],
            str(self.root),
            ["fake/model"],
            cache=self.cache,
            completion_fn=lambda _: response_for([
                {
                    "id": 0,
                    "category": "Chemistry",
                    "subcategory": "Notes",
                    "confidence": "high",
                    "reasoning": "Chemistry is mentioned.",
                    "evidence_quote": "Chemistry",
                    "filename_conflict": "generic",
                    "suggested_name": "Chemistry_Notes.txt",
                    "name_evidence": "Chemistry",
                }
            ]),
        )
        self.assertEqual(result.suggestions[0].confidence, "low")
        self.assertEqual(result.suggestions[0].suggested_name, "misc.txt")

    def test_unsupported_name_evidence_is_rejected(self):
        source = self.root / "notes.txt"
        source.write_text("General meeting agenda and team updates. " * 10, encoding="utf-8")
        result = plan_content_intelligence(
            [file_info(source)],
            str(self.root),
            ["fake/model"],
            cache=self.cache,
            completion_fn=lambda _: response_for([
                {
                    "id": 0,
                    "category": "Work",
                    "subcategory": "Meetings",
                    "confidence": "medium",
                    "reasoning": "Meeting content.",
                    "evidence_quote": "General meeting agenda",
                    "filename_conflict": "none",
                    "suggested_name": "Quarterly_Revenue_Report.txt",
                    "name_evidence": "Quarterly revenue figures",
                }
            ]),
        )
        self.assertEqual(result.suggestions[0].suggested_name, "notes.txt")
        self.assertTrue(any("rejected" in value for value in result.suggestions[0].warnings))

    def test_extension_mismatch_is_detected_and_warned(self):
        source = self.root / "looks_like_pdf.pdf"
        from PIL import Image

        Image.new("RGB", (4, 4), "white").save(source, format="PNG")
        evidence = extract_evidence(file_info(source))
        self.assertEqual(evidence.detected_type, ".png")
        self.assertTrue(evidence.extension_mismatch)
        self.assertTrue(any("conflicts with detected" in warning for warning in evidence.warnings))

    def test_windows_filename_validation(self):
        self.assertEqual(
            sanitize_windows_filename('bad<name>:"/\\|?*.txt', "original.txt"),
            "bad_name.txt",
        )
        self.assertEqual(sanitize_windows_filename("CON", "x.txt"), "CON_File.txt")
        self.assertEqual(sanitize_windows_filename("LPT9.pdf", "x.txt"), "LPT9_File.txt")
        self.assertEqual(sanitize_windows_filename("", "original.txt"), "original.txt")
        long_name = sanitize_windows_filename("A" * 200, "x.txt")
        self.assertLessEqual(len(long_name), 120)
        self.assertTrue(long_name.endswith(".txt"))

    def test_content_change_at_same_path_invalidates_cache(self):
        source = self.root / "changing.txt"
        source.write_text("alpha content about mathematics " * 10, encoding="utf-8")
        calls = []

        def completion(_messages):
            calls.append(1)
            category = "Mathematics" if len(calls) == 1 else "History"
            return response_for([
                {
                    "id": 0,
                    "category": category,
                    "subcategory": "Notes",
                    "confidence": "medium",
                    "reasoning": "Grounded content.",
                    "evidence_quote": "content",
                    "filename_conflict": "none",
                    "suggested_name": "",
                    "name_evidence": "",
                }
            ])

        first = plan_content_intelligence(
            [file_info(source)], str(self.root), ["fake/model"], cache=self.cache, completion_fn=completion
        )
        source.write_text("beta evidence about ancient history " * 10, encoding="utf-8")
        second = plan_content_intelligence(
            [file_info(source)], str(self.root), ["fake/model"], cache=self.cache, completion_fn=completion
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(first.suggestions[0].category, "Mathematics")
        self.assertEqual(second.suggestions[0].category, "History")

    def test_renamed_file_reuses_extraction_cache(self):
        first_path = self.root / "first.txt"
        first_path.write_text("Shared evidence about biology cells and genetics. " * 8, encoding="utf-8")
        completion = lambda _: response_for([{
            "id": 0,
            "category": "Biology",
            "subcategory": "Notes",
            "confidence": "high",
            "reasoning": "Biology content.",
            "evidence_quote": "biology cells and genetics",
            "filename_conflict": "none",
            "suggested_name": "",
            "name_evidence": "",
        }])
        plan_content_intelligence(
            [file_info(first_path)], str(self.root), ["fake/model"], cache=self.cache, completion_fn=completion
        )
        fingerprint = content_fingerprint(first_path)
        evidence_rows = self.cache._db.execute("SELECT COUNT(*) FROM evidence_cache").fetchone()[0]
        second_path = self.root / "renamed.txt"
        first_path.rename(second_path)
        plan_content_intelligence(
            [file_info(second_path)], str(self.root), ["fake/model"], cache=self.cache, completion_fn=completion
        )
        self.assertIsNotNone(self.cache.get_evidence(fingerprint or ""))
        self.assertEqual(
            self.cache._db.execute("SELECT COUNT(*) FROM evidence_cache").fetchone()[0],
            evidence_rows,
        )


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.saved_home = os.environ.get("ORGANIZER_HOME")
        os.environ["ORGANIZER_HOME"] = str(self.root / "home")
        api.plans.clear()

    def tearDown(self):
        api.plans.clear()
        if self.saved_home is None:
            os.environ.pop("ORGANIZER_HOME", None)
        else:
            os.environ["ORGANIZER_HOME"] = self.saved_home
        self.temp.cleanup()

    def suggestion(self, index, path, target, name, reason="content"):
        return ContentSuggestion(
            id=index,
            path=str(path),
            current_name=path.name,
            category=target.split("/")[0],
            subcategory=target.split("/")[1],
            confidence="high",
            reasoning=reason,
            evidence_quote="evidence",
            name_evidence="evidence",
            evidence_excerpt="evidence",
            evidence_sources=["extracted_content"],
            warnings=[],
            filename_conflict="none",
            suggested_name=name,
            rename_reason="grounded",
            sufficient_for_change=True,
            detected_type=".txt",
            extraction_status="sufficient",
        )

    def test_user_edits_rejection_combined_move_and_undo(self):
        first = self.root / "one.txt"
        second = self.root / "two.txt"
        first.write_text("one")
        second.write_text("two")
        suggestions = [
            self.suggestion(0, first, "Science/Notes", "renamed-one.txt"),
            self.suggestion(1, second, "Science/Notes", "renamed-two.txt"),
        ]
        moves = api._content_moves(suggestions, self.root)
        api.plans["plan"] = api.StoredPlan(self.root, moves, suggestions)

        result = api.apply(api.ApplyRequest(
            plan_id="plan",
            files=[
                api.ApplyFile(
                    id="0",
                    accepted=True,
                    target_path="Edited/Reports",
                    destination_name="Final Name.txt",
                ),
                api.ApplyFile(id="1", accepted=False),
            ],
        ))

        self.assertEqual(result["moved"], 1)
        self.assertTrue((self.root / "Edited" / "Reports" / "Final Name.txt").exists())
        self.assertTrue(second.exists())
        undo = history.undo_last_run(str(self.root))
        self.assertEqual(undo["restored"], 1)
        self.assertTrue(first.exists())
        self.assertFalse((self.root / "Edited").exists())

    def test_case_insensitive_collision_suffixes(self):
        first = self.root / "a.txt"
        second = self.root / "b.txt"
        first.write_text("a")
        second.write_text("b")
        suggestions = [
            self.suggestion(0, first, "Docs/Notes", "Photo.txt"),
            self.suggestion(1, second, "Docs/Notes", "photo.txt"),
        ]
        moves = api._content_moves(suggestions, self.root)
        self.assertEqual(Path(moves[0].dest).name, "Photo.txt")
        self.assertEqual(Path(moves[1].dest).name, "photo-1.txt")

    def test_destination_created_after_preview_is_not_overwritten(self):
        source = self.root / "notes.txt"
        source.write_text("receipt", encoding="utf-8")
        suggestions = [
            self.suggestion(0, source, "Finance/Receipts", "Receipt.txt")
        ]
        moves = api._content_moves(suggestions, self.root)
        collision = Path(moves[0].dest)
        collision.parent.mkdir(parents=True)
        collision.write_text("existing file", encoding="utf-8")
        api.plans["collision"] = api.StoredPlan(self.root, moves, suggestions)

        result = api.apply(api.ApplyRequest(
            plan_id="collision",
            files=[api.ApplyFile(
                id="0",
                accepted=True,
                target_path="Finance/Receipts",
                destination_name="Receipt.txt",
            )],
        ))

        self.assertEqual(result["moved"], 1)
        self.assertFalse(source.exists())
        self.assertTrue((self.root / "Finance" / "Receipts" / "Receipt-1.txt").exists())
        self.assertEqual(collision.read_text(encoding="utf-8"), "existing file")

    def test_suspicious_plan_requires_explicit_api_confirmation(self):
        suggestions = []
        moves = []
        for index in range(6):
            source = self.root / f"misc-{index}.txt"
            source.write_text("unknown content", encoding="utf-8")
            suggestions.append(self.suggestion(index, source, "Misc/General", source.name))
        moves = api._content_moves(suggestions, self.root)
        api.plans["suspicious"] = api.StoredPlan(self.root, moves, suggestions)
        request = api.ApplyRequest(
            plan_id="suspicious",
            files=[api.ApplyFile(id=str(index), accepted=True) for index in range(6)],
        )

        with self.assertRaises(api.HTTPException) as caught:
            api.apply(request)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(all(path.exists() for path in (self.root / "misc-0.txt", self.root / "misc-5.txt")))

        request.risk_acknowledged = True
        result = api.apply(request)
        self.assertEqual(result["moved"], 6)


if __name__ == "__main__":
    unittest.main()
