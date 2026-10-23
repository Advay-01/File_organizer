"""Tests for the fast planner using fake async LLMs (no network)."""
import asyncio
import json
import unittest
from pathlib import Path

from app.core.fast_planner import FastConfig, ModelLimits, plan_files_fast
from app.models.schemas import FileInfo

TOPICS = {
    "AI": ["neural", "transformer"],
    "DSA": ["binary tree", "linked list"],
    "Finance": ["invoice", "budget"],
}
TAXONOMY = ["AI", "DSA", "Finance"]


def make_files(n_per_topic=40, n_images=30):
    files = []
    for topic, kws in TOPICS.items():
        for i in range(n_per_topic):
            files.append(FileInfo(path=f"/in/{topic}_{i}.txt", filename=f"doc_{topic}_{i}.txt",
                                  extension=".txt", size_kb=1, date_created="2026-01-01",
                                  snippet=f"about {kws[i % 2]} number {i}"))
    for i in range(n_images):
        files.append(FileInfo(path=f"/in/IMG_{i:04d}.jpg", filename=f"IMG_{i:04d}.jpg",
                              extension=".jpg", size_kb=1, date_created="2026-01-01"))
    return files


def parse_lines(messages):
    return [ln.split("|", 2) for ln in messages[1]["content"].split("\n")]


def keyword_llm(calls=None, latency=0.0):
    async def fn(model, messages):
        if calls is not None:
            calls.append(model)
        await asyncio.sleep(latency)
        if "folder structure" in messages[0]["content"]:
            return json.dumps({"folders": TAXONOMY})
        rows = []
        for uid, name, snip in parse_lines(messages):
            text = (name + snip).lower()
            pick = 0
            for n, (topic, kws) in enumerate(TOPICS.items(), 1):
                if any(k in text for k in kws):
                    pick = n
            if "img_" in text:
                pick = 0
            rows.append([int(uid), pick])
        return json.dumps({"r": rows})
    return fn


CFG = dict(batch_size=25, backoff_floor=0.001, backoff_cap=0.01, request_timeout=5)


class FastPlannerTests(unittest.TestCase):
    def test_correct_folders_and_groups(self):
        files = make_files()
        res = plan_files_fast(files, "/out", ["m"], config=FastConfig(**CFG),
                              acompletion_fn=keyword_llm(), extract=False)
        self.assertEqual(len(res.moves), len(files))
        by_src = {m.src: m for m in res.moves}
        for f in files:
            if f.filename.startswith("doc_"):
                topic = f.filename.split("_")[1]
                self.assertEqual(by_src[f.path].folder, topic)
        # 30 similar images collapse into ONE unit and share the answer
        self.assertEqual({by_src[f.path].folder for f in files if f.filename.startswith("IMG_")}, {"Misc"})
        self.assertEqual(res.stats["units"], 120 + 1)
        dests = [m.dest for m in res.moves]
        self.assertEqual(len(dests), len(set(dests)))

    def test_new_folder_by_name_and_tiny_folder_collapses(self):
        files = make_files(n_per_topic=5, n_images=0)  # 15 files -> small run, no taxonomy
        async def llm(model, messages):
            rows = []
            for uid, name, snip in parse_lines(messages):
                rows.append([int(uid), "Brand New/Thing" if "invoice" in snip else "AI"])
            return json.dumps({"r": rows})
        cfg = FastConfig(**{**CFG, "batch_size": 100, "min_folder_files": 3})
        res = plan_files_fast(files, "/out", ["m"], config=cfg, acompletion_fn=llm, extract=False)
        folders = {m.folder for m in res.moves}
        self.assertNotIn("../x", folders)
        self.assertTrue(all("/" not in f or f.count("/") == 1 for f in folders))

    def test_path_escape_is_neutralised(self):
        files = make_files(n_per_topic=2, n_images=0)
        async def llm(model, messages):
            return json.dumps({"r": [[int(u), "../../etc/evil"] for u, _, _ in parse_lines(messages)]})
        res = plan_files_fast(files, "/out", ["m"], config=FastConfig(**{**CFG, "min_folder_files": 1}),
                              acompletion_fn=llm, extract=False)
        for m in res.moves:
            self.assertTrue(Path(m.dest).resolve().is_relative_to(Path("/out").resolve()), m.dest)
            self.assertNotIn("..", m.dest)

    def test_rate_limit_then_success(self):
        state = {"n": 0}
        inner = keyword_llm()
        class RateLimitError(Exception):
            status_code = 429
        async def flaky(model, messages):
            state["n"] += 1
            if state["n"] in (2, 3, 4):
                raise RateLimitError("slow down")
            return await inner(model, messages)
        res = plan_files_fast(make_files(), "/out", ["m"], config=FastConfig(**CFG),
                              acompletion_fn=flaky, extract=False)
        self.assertGreater(res.stats["rate_limited"], 0)
        self.assertEqual(res.stats["unclassified_units"], 0)

    def test_dead_model_falls_over_to_second(self):
        calls = []
        class NotFoundError(Exception):
            status_code = 404
        inner = keyword_llm(calls)
        async def fn(model, messages):
            if model == "gemini/old":
                raise NotFoundError("model retired")
            return await inner(model, messages)
        res = plan_files_fast(make_files(), "/out", ["gemini/old", "deepseek/x"],
                              config=FastConfig(**CFG), acompletion_fn=fn, extract=False)
        self.assertEqual(res.stats["unclassified_units"], 0)
        self.assertIn("deepseek/x", calls)

    def test_all_models_dead_raises_clear_error(self):
        class AuthenticationError(Exception):
            status_code = 401
        async def fn(model, messages):
            raise AuthenticationError("bad key")
        with self.assertRaises(RuntimeError) as cm:
            plan_files_fast(make_files(), "/out", ["a", "b"], config=FastConfig(**CFG),
                            acompletion_fn=fn, extract=False)
        self.assertIn("bad key", str(cm.exception))

    def test_bad_json_is_split_and_missing_ids_requeued(self):
        inner = keyword_llm()
        async def sloppy(model, messages):
            if "folder structure" in messages[0]["content"]:
                return await inner(model, messages)
            n = len(messages[1]["content"].split("\n"))
            if n > 6:
                return "totally not json"           # big batches fail -> must split
            raw = json.loads(await inner(model, messages))
            raw["r"] = raw["r"][:-1] if n > 1 else raw["r"]   # drops one id per call
            return json.dumps(raw)
        res = plan_files_fast(make_files(), "/out", ["m"], config=FastConfig(**CFG),
                              acompletion_fn=sloppy, extract=False)
        self.assertEqual(res.stats["unclassified_units"], 0)
        by_src = {m.src: m.folder for m in res.moves}
        self.assertEqual(by_src["/in/AI_3.txt"], "AI")

    def test_work_is_shared_across_models(self):
        calls = []
        res = plan_files_fast(make_files(n_per_topic=200), "/out", ["deepseek/a", "gemini/b"],
                              config=FastConfig(**{**CFG, "batch_size": 10}),
                              acompletion_fn=keyword_llm(calls, latency=0.01), extract=False)
        self.assertEqual(res.stats["unclassified_units"], 0)
        self.assertIn("deepseek/a", calls)
        self.assertIn("gemini/b", calls)

    def test_rpm_pacing_is_respected(self):
        import time
        calls = []
        cfg = FastConfig(**{**CFG, "batch_size": 10, "limits": {"m": ModelLimits(4, rpm=1200)}})
        t = time.monotonic()
        plan_files_fast(make_files(n_per_topic=20, n_images=0), "/out", ["m"], config=cfg,
                        acompletion_fn=keyword_llm(calls), extract=False)
        elapsed = time.monotonic() - t
        # 1200 rpm = 0.05s between calls; N calls need >= (N-1)*0.05s
        self.assertGreaterEqual(elapsed, (len(calls) - 2) * 0.05 * 0.9)


class ExtractionTests(unittest.TestCase):
    def test_extract_parallel_reads_light_and_heavy(self):
        import tempfile
        from pathlib import Path
        import docx
        from app.core.fast_planner import extract_parallel
        from app.core.scanner import scan_directory
        with tempfile.TemporaryDirectory() as d:
            for i in range(5):
                (Path(d) / f"t{i}.txt").write_text(f"neural networks {i}")
            for i in range(6):
                doc = docx.Document(); doc.add_paragraph(f"binary tree {i}"); doc.save(Path(d) / f"d{i}.docx")
            files = scan_directory(d)
            extract_parallel(files, 100, workers=2, min_heavy_for_pool=3)  # forces the process pool
            snips = {f.filename: f.snippet for f in files}
        self.assertTrue(all("neural" in snips[f"t{i}.txt"] for i in range(5)))
        self.assertTrue(all("binary tree" in snips[f"d{i}.docx"] for i in range(6)))



class ProfileTests(unittest.TestCase):
    def _run(self, n, **cfg):
        seen = {"tax": 0, "cls": 0, "max_line": 0, "batch_sizes": []}

        async def llm(model, messages):
            if "folder structure" in messages[0]["content"]:
                seen["tax"] += 1
                return json.dumps({"folders": TAXONOMY})
            seen["cls"] += 1
            lines = messages[1]["content"].split("\n")
            seen["batch_sizes"].append(len(lines))
            seen["max_line"] = max(seen["max_line"], max(len(l) for l in lines))
            return json.dumps({"r": [[int(l.split("|")[0]), 1] for l in lines]})

        files = [FileInfo(path=f"/in/f{i}.txt", filename=f"f{i}.txt", extension=".txt", size_kb=1,
                          date_created="2026-01-01", snippet="x" * 1000) for i in range(n)]
        res = plan_files_fast(files, "/out", ["m"], config=FastConfig(backoff_floor=0.001, **cfg),
                              acompletion_fn=llm, extract=False)
        return res, seen

    def test_typical_100_files_gets_accuracy_first_profile(self):
        res, seen = self._run(100)
        self.assertEqual(res.stats["profile"], "balanced")
        self.assertEqual(seen["tax"], 1)                       # folders designed once, from ALL files
        self.assertEqual(seen["batch_sizes"], [25, 25, 25, 25])  # small batches, run in parallel
        self.assertGreater(seen["max_line"], 600)              # 600 chars of each file are sent
        self.assertEqual(len(res.moves), 100)

    def test_tiny_run_is_single_request(self):
        res, seen = self._run(5)
        self.assertEqual(res.stats["profile"], "tiny")
        self.assertEqual((seen["tax"], seen["cls"]), (0, 1))

    def test_big_run_uses_fast_profile(self):
        res, seen = self._run(1000)
        self.assertEqual(res.stats["profile"], "fast")
        self.assertEqual(max(seen["batch_sizes"]), 100)
        self.assertLess(seen["max_line"], 400)

    def test_explicit_settings_beat_the_profile(self):
        res, seen = self._run(100, batch_size=50, snippet_chars=100)
        self.assertEqual(seen["batch_sizes"], [50, 50])
        self.assertLess(seen["max_line"], 200)



class RenameTests(unittest.TestCase):
    def _files(self):
        fs = [FileInfo(path="/in/Untitled.txt", filename="Untitled.txt", extension=".txt", size_kb=1,
                       date_created="2026-01-01", snippet="binary tree traversal notes"),
              FileInfo(path="/in/scan001.txt", filename="scan001.txt", extension=".txt", size_kb=1,
                       date_created="2026-01-01", snippet="neural network lecture"),
              FileInfo(path="/in/clear_name.txt", filename="clear_name.txt", extension=".txt", size_kb=1,
                       date_created="2026-01-01", snippet="invoice total due")]
        # a look-alike photo group (no snippet): must never be renamed
        fs += [FileInfo(path=f"/in/IMG_{i:04d}.jpg", filename=f"IMG_{i:04d}.jpg", extension=".jpg",
                        size_kb=1, date_created="2026-01-01") for i in range(6)]
        # one lone content-less file: has no text, so it must not be renamed either
        fs.append(FileInfo(path="/in/blob.bin", filename="blob.bin", extension=".bin", size_kb=1,
                           date_created="2026-01-01"))
        return fs

    def _llm(self, seen):
        async def llm(model, messages):
            seen.append(messages[0]["content"])
            rows = []
            for ln in messages[1]["content"].split("\n"):
                uid, name, snip = ln.split("|", 2)
                if "binary" in snip:
                    rows.append([int(uid), "DSA", "Binary Tree Notes.txt"])      # model adds extension: fine
                elif "neural" in snip:
                    rows.append([int(uid), "AI", "../../evil/Neural Net Lecture"])  # path trick: neutralised
                elif "invoice" in snip:
                    rows.append([int(uid), "Finance", ""])                       # clear name: no rename
                else:
                    rows.append([int(uid), "Misc", "Should Not Apply"])          # content-less: ignored
            return json.dumps({"r": rows})
        return llm

    def test_rename_on(self):
        seen = []
        res = plan_files_fast(self._files(), "/out", ["m"], config=FastConfig(rename=True, min_folder_files=1, backoff_floor=0.001),
                              acompletion_fn=self._llm(seen), extract=False)
        dest = {m.src: m.dest for m in res.moves}
        self.assertEqual(Path(dest["/in/Untitled.txt"]), Path("/out/DSA/Binary_Tree_Notes.txt"))
        self.assertEqual(Path(dest["/in/scan001.txt"]), Path("/out/AI/evil_Neural_Net_Lecture.txt"))
        self.assertEqual(Path(dest["/in/clear_name.txt"]), Path("/out/Finance/clear_name.txt"))
        self.assertTrue(all(Path(dest[f"/in/IMG_{i:04d}.jpg"]).name == f"IMG_{i:04d}.jpg" for i in range(6)))
        self.assertEqual(Path(dest["/in/blob.bin"]).name, "blob.bin")
        self.assertEqual(res.stats["renamed"], 2)
        self.assertIn("file name WITHOUT extension", seen[0])
        for d in dest.values():
            self.assertTrue(Path(d).resolve().is_relative_to(Path("/out").resolve()) and ".." not in d)

    def test_rename_off_by_default_keeps_prompt_short_and_names(self):
        seen = []
        res = plan_files_fast(self._files(), "/out", ["m"], config=FastConfig(min_folder_files=1, backoff_floor=0.001),
                              acompletion_fn=self._llm(seen), extract=False)
        dest = {m.src: m.dest for m in res.moves}
        self.assertEqual(Path(dest["/in/Untitled.txt"]).name, "Untitled.txt")
        self.assertEqual(res.stats["renamed"], 0)
        self.assertNotIn("file name WITHOUT extension", seen[0])

    def test_clean_filename(self):
        from app.core.fast_planner import clean_filename
        self.assertEqual(clean_filename("My Notes!.pdf", "x.pdf"), "My_Notes.pdf")
        self.assertEqual(clean_filename("a", "x.pdf"), "x.pdf")
        self.assertEqual(clean_filename("???", "x.pdf"), "x.pdf")
        self.assertEqual(clean_filename("nul", "x.pdf"), "nul_.pdf")
        self.assertEqual(clean_filename("A" * 200, "x.md"), "A" * 80 + ".md")



class SmallRunFoldersTests(unittest.TestCase):
    """Regression: on a small run every model-named folder has few files; none may be folded to Misc."""

    def test_tiny_run_keeps_one_file_folders(self):
        files = [FileInfo(path=f"/in/{n}.txt", filename=f"{n}.txt", extension=".txt", size_kb=1,
                          date_created="2026-01-01", snippet=s)
                 for n, s in (("a", "binary tree"), ("b", "neural net"), ("c", "invoice total"))]

        async def llm(model, messages):
            names = {"binary": "DSA", "neural": "AI", "invoice": "Finance"}
            rows = []
            for ln in messages[1]["content"].split("\n"):
                uid, _, snip = ln.split("|", 2)
                rows.append([int(uid), next(v for k, v in names.items() if k in snip)])
            return json.dumps({"r": rows})

        res = plan_files_fast(files, "/out", ["m"], config=FastConfig(backoff_floor=0.001),
                              acompletion_fn=llm, extract=False)
        self.assertEqual({m.src: m.folder for m in res.moves},
                         {"/in/a.txt": "DSA", "/in/b.txt": "AI", "/in/c.txt": "Finance"})
        self.assertEqual(res.stats["profile"], "tiny")


if __name__ == "__main__":
    unittest.main()

