"""Tests for the React API bridge without contacting an LLM provider."""
import tempfile
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import api
from app.core.content_intelligence import ContentSuggestion
from app.core.planner import Move
from app.models.schemas import FileInfo


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.first = self.root / "lecture.txt"
        self.second = self.root / "exam.txt"
        self.first.write_text("notes")
        self.second.write_text("exam")
        api.plans.clear()

    def tearDown(self):
        api.plans.clear()
        self.tmp.cleanup()

    def test_apply_uses_saved_plan_and_keeps_unselected_targets_at_root(self):
        api.plans["saved-plan"] = api.StoredPlan(
            source=self.root,
            moves=[
                Move(str(self.first), str(self.root / "Physics" / "Notes" / "lecture.txt"), "Physics", "notes", "Notes"),
                Move(str(self.second), str(self.root / "Physics" / "Exams" / "exam.txt"), "Physics", "exam", "Exams"),
            ],
        )

        result = api.apply(api.ApplyRequest(plan_id="saved-plan", selected_targets=["Physics/Notes"]))

        self.assertEqual(result["moved"], 1)
        self.assertEqual(result["kept_in_root"], 1)
        self.assertEqual(result["hierarchy"]["folders"][0]["name"], "Physics")
        self.assertTrue((self.root / "Physics" / "Notes" / "lecture.txt").exists())
        self.assertTrue(self.second.exists())
        self.assertTrue(api.plans["saved-plan"].applied)

    def test_apply_resolves_root_prefixed_jpg_path_once(self):
        root = self.root / "Sortly_TestFolder"
        root.mkdir()
        source = root / "DSC_0042.jpg"
        source.write_bytes(b"image")
        suggestion = ContentSuggestion(
            id=0,
            path=str(Path(root.name) / source.name),
            current_name=source.name,
            category="Photos",
            subcategory="Travel",
            confidence="medium",
            reasoning="photo",
            evidence_quote="",
            name_evidence="",
            evidence_excerpt="",
            evidence_sources=[],
            warnings=[],
            filename_conflict="none",
            suggested_name=source.name,
            rename_reason="",
            sufficient_for_change=True,
            detected_type=".jpg",
            extraction_status="sufficient",
        )
        api.plans["jpg-path"] = api.StoredPlan(root, api._content_moves([suggestion], root), [suggestion])

        result = api.apply(api.ApplyRequest(
            plan_id="jpg-path",
            files=[api.ApplyFile(id="0", accepted=True)],
        ))

        self.assertEqual(result["moved"], 1)
        self.assertTrue((root / "Photos" / "Travel" / source.name).exists())

    def test_apply_resolves_nested_relative_epub_path(self):
        root = self.root / "Sortly_TestFolder"
        source = root / "Education" / "Astronomy" / "Astronomy_Planets_Stars_Galaxies.epub"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"book")
        suggestion = ContentSuggestion(
            id=0,
            path="Education/Astronomy/Astronomy_Planets_Stars_Galaxies.epub",
            current_name=source.name,
            category="Books",
            subcategory="Science",
            confidence="medium",
            reasoning="book",
            evidence_quote="",
            name_evidence="",
            evidence_excerpt="",
            evidence_sources=[],
            warnings=[],
            filename_conflict="none",
            suggested_name=source.name,
            rename_reason="",
            sufficient_for_change=True,
            detected_type=".epub",
            extraction_status="sufficient",
        )
        api.plans["epub-path"] = api.StoredPlan(root, api._content_moves([suggestion], root), [suggestion])

        result = api.apply(api.ApplyRequest(
            plan_id="epub-path",
            files=[api.ApplyFile(id="0", accepted=True)],
        ))

        self.assertEqual(result["moved"], 1)
        self.assertTrue((root / "Books" / "Science" / source.name).exists())

    def test_missing_source_is_reported_while_unchanged_file_moves(self):
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        first.write_text("first")
        second.write_text("second")
        api.plans["stale-plan"] = api.StoredPlan(
            self.root,
            [
                Move(str(first), str(self.root / "Docs" / "first.txt"), "Docs", "first"),
                Move(str(second), str(self.root / "Docs" / "second.txt"), "Docs", "second"),
            ],
        )
        second.unlink()

        response = api.apply(api.ApplyRequest(
            plan_id="stale-plan",
            files=[
                api.ApplyFile(id="0", accepted=True),
                api.ApplyFile(id="1", accepted=True),
            ],
        ))

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 207)
        self.assertEqual((payload["moved"], payload["failed"]), (1, 1))
        self.assertIn("reanalysis", payload["failures"][0]["reason"])
        self.assertTrue((self.root / "Docs" / "first.txt").exists())

    def test_preview_parse_failure_is_a_readable_502(self):
        from fastapi.testclient import TestClient

        file_info = FileInfo(
            path=str(self.first),
            filename=self.first.name,
            extension=".txt",
            size_kb=1,
            date_created="2026-01-01",
            snippet="notes",
        )
        scan_summary = SimpleNamespace(files_seen=1, skipped=0, reasons={}, truncated=False)
        client = TestClient(api.app)
        with (
            patch("app.api.scan_directory_with_report", return_value=([file_info], scan_summary)),
            patch("app.api.provider_store.activate_runtime", return_value=[]),
            patch(
                "app.api.plan_content_intelligence",
                side_effect=ValueError("Model returned unusable JSON reply."),
            ),
        ):
            response = client.post(
                "/api/preview",
                json={"source": str(self.root), "models": ["anthropic/claude-haiku-5-5"]},
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("unusable JSON reply", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
