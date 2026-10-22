"""End-to-end tests with a fake LLM (no network, no API keys)."""
import gzip
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.core.classifier import LLMClassifier, extract_json
from app.core.executor import execute_plan, undo_last
from app.core.extractor import attach_snippets, extract_snippet
from app.core.planner import (
    build_plan,
    sanitize_filename,
    sanitize_folder,
    sanitize_subfolder,
    sanitize_target_path,
)
from app.core.scanner import scan_directory


def fake_llm(model, messages):
    """Classifies by snippet keywords, like a tiny pretend LLM."""
    payload = json.loads(messages[1]["content"])
    actions = []
    for f in payload["files"]:
        s = (f["snippet"] + f["filename"]).lower()
        if "neural" in s or "transformer" in s:
            folder = "AI"
        elif "binary tree" in s or "linked list" in s:
            folder = "DSA"
        else:
            folder = "Misc"
        actions.append({"path": f["path"], "folder": folder, "new_name": "", "reason": "kw"})
    return json.dumps({"actions": actions})


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "notes1.txt").write_text("Neural networks and transformer attention")
        (self.root / "trees.md").write_text("Binary tree traversal, linked list")
        (self.root / "random.bin").write_bytes(b"\x00\x01")
        (self.root / ".hidden").write_text("x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_skips_hidden_and_reads_snippets(self):
        files = scan_directory(str(self.root))
        names = sorted(f.filename for f in files)
        self.assertEqual(names, ["notes1.txt", "random.bin", "trees.md"])
        attach_snippets(files)
        self.assertIn("Neural", next(f for f in files if f.filename == "notes1.txt").snippet)
        self.assertEqual(extract_snippet(str(self.root / "random.bin")), "")

    def test_subtitles_and_archives_provide_classifier_context(self):
        subtitle = self.root / "lesson.vtt"
        subtitle.write_text("WEBVTT\n\n00:00.000 --> 00:02.000\nTransformer attention overview")
        zipped = self.root / "course-files.zip"
        with zipfile.ZipFile(zipped, "w") as archive:
            archive.writestr("notes/neural-networks.txt", "content")
        tarred = self.root / "code.tar"
        with tarfile.open(tarred, "w") as archive:
            archive.add(subtitle, arcname="lesson.vtt")
        compressed = self.root / "topics.txt.gz"
        with gzip.open(compressed, "wt") as archive:
            archive.write("binary tree practice")

        self.assertIn("Transformer attention", extract_snippet(str(subtitle)))
        self.assertIn("neural-networks.txt", extract_snippet(str(zipped)))
        self.assertIn("lesson.vtt", extract_snippet(str(tarred)))
        self.assertIn("binary tree", extract_snippet(str(compressed)))

    def test_dry_run_moves_nothing(self):
        files = attach_snippets(scan_directory(str(self.root)))
        moves = build_plan(files, str(self.root), LLMClassifier(["x"], fake_llm))
        execute_plan(moves, str(self.root), dry_run=True)
        self.assertTrue((self.root / "notes1.txt").exists())
        self.assertFalse((self.root / "AI").exists())

    def test_apply_then_undo(self):
        files = attach_snippets(scan_directory(str(self.root)))
        moves = build_plan(files, str(self.root), LLMClassifier(["x"], fake_llm))
        done = execute_plan(moves, str(self.root), dry_run=False)
        self.assertEqual(len(done), 3)
        self.assertTrue((self.root / "AI" / "notes1.txt").exists())
        self.assertTrue((self.root / "DSA" / "trees.md").exists())
        self.assertTrue((self.root / "Misc" / "random.bin").exists())
        self.assertEqual(undo_last(str(self.root)), 3)
        self.assertTrue((self.root / "notes1.txt").exists())
        self.assertFalse((self.root / "AI" / "notes1.txt").exists())

    def test_apply_never_overwrites_a_destination_created_after_preview(self):
        files = attach_snippets(scan_directory(str(self.root)))
        moves = build_plan(files, str(self.root), LLMClassifier(["x"], fake_llm))
        planned_move = next(move for move in moves if move.src.endswith("notes1.txt"))
        destination = Path(planned_move.dest)
        destination.parent.mkdir(parents=True)
        destination.write_text("do not replace")

        done = execute_plan(moves, str(self.root), dry_run=False)

        self.assertTrue((self.root / "notes1.txt").exists())
        self.assertEqual(destination.read_text(), "do not replace")
        self.assertEqual(len(done), 2)

    def test_unselected_target_keeps_its_files_in_the_source_folder(self):
        files = attach_snippets(scan_directory(str(self.root)))
        moves = build_plan(files, str(self.root), LLMClassifier(["x"], fake_llm))
        notes_move = next(move for move in moves if move.src.endswith("notes1.txt"))
        notes_move.selected = False

        done = execute_plan(moves, str(self.root), dry_run=False)

        self.assertTrue((self.root / "notes1.txt").exists())
        self.assertFalse(Path(notes_move.dest).exists())
        self.assertEqual(len(done), 2)

    def test_name_collisions_get_suffix(self):
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "notes1.txt").write_text("Neural networks")
        files = attach_snippets(scan_directory(str(self.root), recursive=True))
        moves = build_plan(files, str(self.root), LLMClassifier(["x"], fake_llm))
        dests = [m.dest for m in moves if m.folder == "AI"]
        self.assertEqual(len(dests), len(set(dests)))
        self.assertEqual(len(dests), 2)

    def test_plan_supports_safe_topic_subfolders(self):
        def physics_llm(model, messages):
            payload = json.loads(messages[1]["content"])
            actions = []
            for item in payload["files"]:
                subfolder = "Formula Sheets" if item["filename"] == "notes1.txt" else "Lab Reports"
                actions.append({
                    "path": item["path"],
                    "target_path": f"Physics/{subfolder}",
                    "reasoning": "physics material",
                })
            return json.dumps({"actions": actions})

        files = scan_directory(str(self.root))
        moves = build_plan(files, str(self.root), LLMClassifier(["x"], physics_llm))
        notes = next(move for move in moves if move.src.endswith("notes1.txt"))
        exam = next(move for move in moves if move.src.endswith("trees.md"))
        self.assertEqual(notes.subfolder, "Formula Sheets")
        self.assertEqual(Path(notes.dest), self.root / "Physics" / "Formula Sheets" / "notes1.txt")
        self.assertEqual(Path(exam.dest), self.root / "Physics" / "Lab Reports" / "trees.md")

    def test_subfolder_sanitizer_never_returns_a_path(self):
        self.assertEqual(sanitize_subfolder(""), "")
        self.assertEqual(sanitize_subfolder("../../Exams/"), "Exams")
        self.assertEqual(sanitize_target_path("Physics/Lab Reports"), ("Physics", "Lab Reports"))
        self.assertEqual(sanitize_target_path("Physics/../../escape"), ("Misc", "General"))

    def test_hallucinated_and_missing_paths_handled(self):
        def bad_llm(model, messages):
            return json.dumps({"actions": [
                {"path": "/etc/passwd", "folder": "AI"},
                {"path": str(self.root / "notes1.txt"), "folder": "../../evil"},
            ]})
        files = scan_directory(str(self.root))
        moves = build_plan(files, str(self.root), LLMClassifier(["x"], bad_llm))
        self.assertEqual(len(moves), 3)  # every file still gets a move
        for m in moves:
            self.assertTrue(Path(m.dest).resolve().is_relative_to(self.root.resolve()))
            self.assertNotIn("passwd", m.dest)

    def test_fallback_to_second_model(self):
        calls = []
        def flaky(model, messages):
            calls.append(model)
            if model == "bad":
                raise ConnectionError("down")
            return fake_llm(model, messages)
        files = attach_snippets(scan_directory(str(self.root)))
        moves = build_plan(files, str(self.root), LLMClassifier(["bad", "good"], flaky))
        self.assertEqual(calls[0], "bad")
        self.assertIn("good", calls)
        self.assertEqual(len(moves), 3)

    def test_json_repair_and_all_fail(self):
        self.assertEqual(extract_json('```json\n{"a":1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Sure! {"a": 2} hope that helps'), {"a": 2})
        with self.assertRaises(RuntimeError):
            LLMClassifier(["x"], lambda m, msgs: "not json at all").classify("s", "u")

    def test_sanitizers(self):
        self.assertEqual(sanitize_folder("../../AI/"), "AI")
        self.assertEqual(sanitize_folder(""), "Misc")
        self.assertEqual(sanitize_filename("new/name", "a.pdf"), "newname.pdf")
        self.assertEqual(sanitize_filename("", "a.pdf"), "a.pdf")


if __name__ == "__main__":
    unittest.main()
