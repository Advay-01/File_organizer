"""Tests for the React API bridge without contacting an LLM provider."""
import tempfile
import unittest
from pathlib import Path

from app import api
from app.core.planner import Move


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


if __name__ == "__main__":
    unittest.main()
