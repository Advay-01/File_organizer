"""History + per-run undo, using the real executor to write the log."""
import io
import contextlib
import json
import tempfile
import unittest
from pathlib import Path

from app.core import history
from app.core.executor import execute_plan
from app.core.planner import Move


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, spec):
        """spec: [(filename, folder, new_name)] -> creates files, runs executor, returns moves."""
        moves = []
        for name, folder, new in spec:
            src = self.root / name
            src.write_text(name)
            moves.append(Move(str(src), str(self.root / folder / (new or name)), folder, "t"))
        with contextlib.redirect_stdout(io.StringIO()):
            execute_plan(moves, str(self.root), dry_run=False)
        return moves

    def test_empty_and_corrupt_log(self):
        self.assertEqual(history.list_runs(str(self.root)), [])
        (self.root / history.UNDO_LOG).write_text("{not json")
        self.assertEqual(history.list_runs(str(self.root)), [])
        self.assertIsNone(history.undo_last_run(str(self.root)))

    def test_list_newest_first_with_details(self):
        self.apply([("a.txt", "AI", None), ("b.txt", "AI", "Better_B.txt"), ("c.txt", "DSA", None)])
        self.apply([("d.txt", "Misc", None)])
        runs = history.list_runs(str(self.root))
        self.assertEqual([r["id"] for r in runs], [1, 0])
        first = runs[1]
        self.assertEqual(first["files"], 3)
        self.assertEqual(first["renamed"], 1)
        self.assertEqual(first["folders"], {"AI": 2, "DSA": 1})
        self.assertEqual(first["status"], "applied")
        self.assertEqual(first["sample"][0], {"from": "a.txt", "to": "a.txt", "folder": "AI"})
        detail = history.get_run(str(self.root), 0)
        self.assertEqual(len(detail["moves"]), 3)
        self.assertEqual(detail["moves"][1]["from_name"], "b.txt")
        self.assertEqual(detail["moves"][1]["to_name"], "Better_B.txt")
        with self.assertRaises(KeyError):
            history.get_run(str(self.root), 9)

    def test_undo_restores_names_prunes_empty_folders_and_keeps_history(self):
        self.apply([("a.txt", "AI/Notes", None), ("b.txt", "AI", "Better_B.txt")])
        res = history.undo_run(str(self.root), 0)
        self.assertEqual((res["restored"], res["skipped"], res["status"]), (2, [], "undone"))
        self.assertTrue((self.root / "a.txt").exists() and (self.root / "b.txt").exists())
        self.assertFalse((self.root / "AI").exists())                     # empty folders removed
        self.assertEqual(history.list_runs(str(self.root))[0]["status"], "undone")  # still listed
        with self.assertRaises(ValueError):
            history.undo_run(str(self.root), 0)                           # not twice

    def test_undo_never_overwrites_and_reports_why(self):
        self.apply([("a.txt", "AI", None), ("b.txt", "AI", None), ("c.txt", "AI", None)])
        (self.root / "AI" / "b.txt").unlink()                  # user deleted it
        (self.root / "c.txt").write_text("NEW FILE same name")  # original spot taken
        res = history.undo_run(str(self.root), 0)
        self.assertEqual(res["restored"], 1)
        reasons = {s["file"]: s["reason"] for s in res["skipped"]}
        self.assertEqual(reasons["b.txt"], "file is no longer there")
        self.assertEqual(reasons["c.txt"], "original location is already occupied")
        self.assertEqual((self.root / "c.txt").read_text(), "NEW FILE same name")   # untouched
        self.assertTrue((self.root / "AI" / "c.txt").exists())                       # kept safe
        self.assertEqual(res["status"], "partially undone")

    def test_nothing_restored_leaves_run_applied(self):
        self.apply([("a.txt", "AI", None)])
        (self.root / "AI" / "a.txt").unlink()
        res = history.undo_run(str(self.root), 0)
        self.assertEqual(res["restored"], 0)
        self.assertEqual(history.list_runs(str(self.root))[0]["status"], "applied")

    def test_undo_older_run_independent_of_newer(self):
        self.apply([("a.txt", "AI", None)])
        self.apply([("b.txt", "DSA", None)])
        history.undo_run(str(self.root), 0)
        self.assertTrue((self.root / "a.txt").exists())
        self.assertTrue((self.root / "DSA" / "b.txt").exists())       # run 1 untouched

    def test_undo_last_run_skips_already_undone(self):
        self.apply([("a.txt", "AI", None)])
        self.apply([("b.txt", "DSA", None)])
        self.assertEqual(history.undo_last_run(str(self.root))["id"], 1)
        self.assertEqual(history.undo_last_run(str(self.root))["id"], 0)
        self.assertIsNone(history.undo_last_run(str(self.root)))

    def test_tampered_log_cannot_touch_files_outside_output_folder(self):
        outside = tempfile.TemporaryDirectory()
        victim = Path(outside.name) / "secret.txt"
        victim.write_text("keep me")
        log = [{"time": "t", "moves": [{"src": str(self.root / "stolen.txt"), "dest": str(victim)}]}]
        (self.root / history.UNDO_LOG).write_text(json.dumps(log))
        res = history.undo_run(str(self.root), 0)
        self.assertEqual(res["restored"], 0)
        self.assertTrue(victim.exists() and not (self.root / "stolen.txt").exists())
        outside.cleanup()


if __name__ == "__main__":
    unittest.main()

