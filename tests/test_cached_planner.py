import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.cache import OrganizeCache
from app.core.cached_planner import plan_with_cache
from app.core.planner import Move
from app.models.schemas import FileInfo


def file_info(path: Path) -> FileInfo:
    return FileInfo(path=str(path), filename=path.name, extension=path.suffix,
                    size_kb=1, date_created="2026-01-01", snippet="notes")


class CachedPlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = OrganizeCache(self.root / "cache.db")

    def tearDown(self):
        self.cache.close()
        self.temp.cleanup()

    def test_already_sorted_cached_file_is_not_planned_again(self):
        source = self.root / "notes.txt"
        source.write_text("binary tree notes")
        first = file_info(source)
        planned = Move(str(source), str(self.root / "DSA" / "notes.txt"), "DSA", "content match")
        with patch("app.core.cached_planner.plan_files_fast", return_value=SimpleNamespace(moves=[planned], stats={})) as planner:
            initial = plan_with_cache([first], str(self.root), ["model"], cache=self.cache)
            self.assertEqual(len(initial.moves), 1)
            self.assertEqual(planner.call_count, 1)

            sorted_file = self.root / "DSA" / "notes.txt"
            sorted_file.parent.mkdir()
            source.rename(sorted_file)
            again = plan_with_cache([file_info(sorted_file)], str(self.root), ["model"], cache=self.cache)

        self.assertEqual(again.moves, [])
        self.assertEqual(again.already_organized, [sorted_file])
        self.assertEqual(again.stats["sent_to_llm"], 0)

    def test_reorganize_option_ignores_cache(self):
        source = self.root / "notes.txt"
        source.write_text("notes")
        self.cache.put_many([(source, "DSA", "cached")])
        planned = Move(str(source), str(self.root / "AI" / "notes.txt"), "AI", "fresh")
        with patch("app.core.cached_planner.plan_files_fast", return_value=SimpleNamespace(moves=[planned], stats={})) as planner:
            result = plan_with_cache([file_info(source)], str(self.root), ["model"], cache=self.cache, use_cache=False)

        self.assertEqual(planner.call_count, 1)
        self.assertEqual(result.moves[0].folder, "AI")


if __name__ == "__main__":
    unittest.main()
