"""Long names use the existing rename setting and confirmation workflow."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import api
from app.core import history
from app.core.content_cache import ContentIntelligenceCache
from app.core.content_intelligence import _shorten_long_filename, plan_content_intelligence
from app.models.schemas import FileInfo


class LongFilenameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = ContentIntelligenceCache(self.root / "cache.db")
        api.plans.clear()

    def tearDown(self):
        api.plans.clear()
        self.cache.close()
        self.temp.cleanup()

    def make_file(self, name, content="Research notes about plant habitats and soil conditions. " * 4):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def plan(self, paths, rename=True, proposed="", calls=None):
        def complete(_messages):
            if calls is not None:
                calls.append(1)
            return json.dumps({"files": [{
                "id": index, "category": "Research", "subcategory": "Notes", "confidence": "high",
                "reasoning": "Plant habitat research notes.", "evidence_quote": "Research notes about plant habitats",
                "filename_conflict": "none", "suggested_name": proposed,
                "name_evidence": "Research notes about plant habitats" if proposed else "",
            } for index in range(len(paths))]})
        files = [FileInfo(path=str(path), filename=path.name, extension=path.suffix.lower(), size_kb=1, date_created="2026-01-01") for path in paths]
        return plan_content_intelligence(files, str(self.root), ["fake/model"], cache=self.cache,
                                         allow_rename=rename, completion_fn=complete).suggestions

    def test_long_name_gets_word_boundary_suggestion_before_any_file_is_changed(self):
        source = self.make_file("Research_notes_" + "plant_habitats_and_soil_conditions_" * 4 + ".txt")
        item = self.plan([source])[0]
        self.assertLessEqual(len(item.suggested_name), 80)
        self.assertNotEqual(item.suggested_name, source.name)
        self.assertTrue(source.stem.startswith(Path(item.suggested_name).stem))
        self.assertIn(source.stem[len(Path(item.suggested_name).stem)], " _-.")
        self.assertEqual(Path(item.suggested_name).suffix, ".txt")
        self.assertIn("Long filename reduced", item.rename_reason)
        self.assertTrue(source.exists())
        self.assertFalse((source.parent / item.suggested_name).exists())

    def test_grounded_concise_name_is_preferred(self):
        source = self.make_file("Very_long_research_filename_" * 5 + ".txt")
        item = self.plan([source], proposed="Plant_Habitat_Research.txt")[0]
        self.assertEqual(item.suggested_name, "Plant_Habitat_Research.txt")
        self.assertIn("Grounded in evidence", item.rename_reason)

    def test_existing_rename_toggle_and_cache_are_respected(self):
        source = self.make_file("Research_notes_" * 10 + ".txt")
        disabled = self.plan([source], rename=False)[0]
        self.assertEqual(disabled.suggested_name, source.name)
        self.assertEqual(disabled.rename_reason, "")
        enabled = self.plan([source], rename=True)[0]
        self.assertLessEqual(len(enabled.suggested_name), 80)
        calls = []
        again = self.plan([source], rename=True, calls=calls)[0]
        self.assertEqual(calls, [])
        self.assertEqual(again.suggested_name, enabled.suggested_name)

    def test_normal_names_stay_unchanged_when_model_has_no_rename(self):
        source = self.make_file("Plant_Habitat_Research.txt")
        self.assertEqual(self.plan([source])[0].suggested_name, source.name)

    def test_disabled_rename_setting_preserves_long_name_when_applying_folder_move(self):
        source = self.make_file("Research_notes_" * 10 + ".txt")
        contents = source.read_bytes()
        items = self.plan([source], rename=False)
        api.plans["keep-long-name"] = api.StoredPlan(self.root, api._content_moves(items, self.root), items)
        with patch.object(api.history, "remember_folder"):
            result = api.apply(api.ApplyRequest(plan_id="keep-long-name", files=[
                api.ApplyFile(id="0", accepted=True, target_path="Research/Notes", destination_name=source.name),
            ]))
        self.assertEqual(result["moved"], 1)
        self.assertEqual((self.root / "Research" / "Notes" / source.name).read_bytes(), contents)

    def test_unicode_and_compound_extension_are_preserved(self):
        name = "植物研究_" * 24 + ".tar.gz"
        shortened = _shorten_long_filename(name)
        self.assertLessEqual(len(shortened), 80)
        self.assertTrue(shortened.endswith(".tar.gz"))
        self.assertTrue(shortened.startswith("植物研究"))
        self.assertEqual(_shorten_long_filename(shortened), shortened)

    def test_sensitive_and_unreadable_files_keep_original_names(self):
        secret = self.make_file("private_" * 18 + ".txt", "-----BEGIN PRIVATE KEY-----\nfixture-only\n-----END PRIVATE KEY-----")
        binary = self.root / ("unreadable_" * 13 + ".bin")
        binary.write_bytes(b"\x00\xff\x01" * 100)
        calls = []
        items = self.plan([secret, binary], calls=calls)
        self.assertEqual(calls, [])
        self.assertEqual([item.suggested_name for item in items], [secret.name, binary.name])
        self.assertTrue(all(not item.selected for item in items))

    def test_shortened_name_collisions_and_undo_preserve_both_files(self):
        prefix = "Plant_habitat_research_notes_" * 4
        first = self.make_file(prefix + "first.txt")
        second = self.make_file(prefix + "second.txt", "Research notes about plant habitats and a different soil sample. " * 4)
        originals = {path.name: path.read_bytes() for path in (first, second)}
        items = self.plan([first, second])
        self.assertEqual(items[0].suggested_name, items[1].suggested_name)
        moves = api._content_moves(items, self.root)
        self.assertNotEqual(moves[0].dest, moves[1].dest)
        self.assertTrue(first.exists() and second.exists())
        api.plans["long-names"] = api.StoredPlan(self.root, moves, items)
        with patch.object(api.history, "remember_folder"):
            result = api.apply(api.ApplyRequest(plan_id="long-names", files=[
                api.ApplyFile(id=str(index), accepted=True, target_path="Research/Notes", destination_name=Path(move.dest).name)
                for index, move in enumerate(moves)
            ]))
        self.assertEqual(result["moved"], 2)
        for move in moves:
            self.assertEqual(Path(move.dest).read_bytes(), originals[Path(move.src).name])
        restored = history.undo_last_run(str(self.root))
        self.assertEqual(restored["restored"], 2)
        for name, content in originals.items():
            self.assertEqual((self.root / name).read_bytes(), content)
