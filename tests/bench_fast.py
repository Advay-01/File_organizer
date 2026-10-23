"""Benchmark the fast planner on 10,000 files.

Run:  python tests/bench_fast.py
Part 1 simulates LLM latency (no network). Part 2 measures REAL disk scanning/reading.
Assumptions are printed; change TTFT / TPS to match your provider.
"""
import asyncio
import json
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.extractor import extract_snippet
from app.core.fast_planner import FastConfig, ModelLimits, plan_files_fast, extract_parallel
from app.core.scanner import scan_directory
from app.models.schemas import FileInfo

TTFT = 1.0          # seconds until first token
TPS = 100.0         # output tokens / second
TOK_NEW = 9         # tokens per file in the compact "[id, n]" format
TOK_OLD = 35        # tokens per file in the old verbose JSON (path, folder, name, reason)

TOPICS = {
    "AI/Machine Learning": ["neural network", "gradient descent", "transformer attention"],
    "AI/NLP": ["tokenization", "language model", "embeddings"],
    "DSA": ["binary tree", "linked list", "dynamic programming", "graph bfs"],
    "Finance/Invoices": ["invoice total", "tax amount", "payment due"],
    "Projects/Hackathon": ["hackathon", "demo day", "team roadmap"],
    "Physics": ["thermodynamics", "lab report", "wave optics"],
    "Personal": ["resume", "cover letter", "travel itinerary"],
}
NAMES = list(TOPICS)


def make_files(n=10_000, image_share=0.15):
    rnd = random.Random(1)
    files, truth = [], {}
    for i in range(n):
        if rnd.random() < image_share:
            f = FileInfo(path=f"/in/IMG_{i:05d}.jpg", filename=f"IMG_{i:05d}.jpg", extension=".jpg",
                         size_kb=2000, date_created="2026-01-01")
            truth[f.path] = "Misc"
        else:
            topic = rnd.choice(NAMES)
            kw = rnd.choice(TOPICS[topic])
            f = FileInfo(path=f"/in/file_{i}.txt", filename=f"file_{i}.txt", extension=".txt",
                         size_kb=4, date_created="2026-01-01",
                         snippet=f"Notes on {kw}. Section {i}: lorem ipsum dolor sit amet consectetur "
                                 f"adipiscing elit sed do eiusmod tempor incididunt ut labore.")
            truth[f.path] = topic
        files.append(f)
    return files, truth


def fake_llm(counter):
    async def fn(model, messages):
        counter[model] = counter.get(model, 0) + 1
        speed = 0.6 if model.startswith("gemini") else 1.0
        if "folder structure" in messages[0]["content"]:
            await asyncio.sleep(TTFT + 150 / TPS)
            return json.dumps({"folders": NAMES})
        lines = messages[1]["content"].split("\n")
        await asyncio.sleep((TTFT + len(lines) * TOK_NEW / TPS) / speed)
        rows = []
        for ln in lines:
            uid, name, snip = ln.split("|", 2)
            pick = 0
            for n, (t, kws) in enumerate(TOPICS.items(), 1):
                if any(k in snip.lower() for k in kws):
                    pick = n
            rows.append([int(uid), pick])
        return json.dumps({"r": rows})
    return fn


def run_scenario(title, models, limits):
    files, truth = make_files()
    counter = {}
    cfg = FastConfig(batch_size=100, limits=limits)
    res = plan_files_fast(files, "/out", models, config=cfg, acompletion_fn=fake_llm(counter), extract=False)
    right = sum(1 for m in res.moves if m.folder == truth[m.src])
    print(f"\n{title}")
    print(f"  total time (LLM part): {res.stats['seconds']:.1f}s   LLM calls: {res.stats['calls']}   "
          f"units sent: {res.stats['units']}   calls per model: {counter}")
    print(f"  agreement with ground truth: {right / len(files):.1%}   unclassified: {res.stats['unclassified_units']}")


def old_style_estimate(n=10_000, batch=20):
    calls = n / batch
    per_call = TTFT + batch * TOK_OLD / TPS
    t = calls * per_call
    print(f"\nOLD style (sequential, {batch}/call, verbose output) - computed, not run:")
    print(f"  {calls:.0f} calls x {per_call:.1f}s = {t / 60:.0f} min")


def real_disk_bench(n=10_000):
    print(f"\nREAL DISK: creating {n} small text files ...")
    with tempfile.TemporaryDirectory() as d:
        for i in range(n):
            (Path(d) / f"f{i}.txt").write_text(f"Notes on neural networks, file {i}. " * 40)
        t = time.monotonic()
        files = scan_directory(d)
        t_scan = time.monotonic() - t
        t = time.monotonic()
        for f in files:
            f.snippet = extract_snippet(f.path, 250)
        t_serial = time.monotonic() - t
        for f in files:
            f.snippet = ""
        t = time.monotonic()
        extract_parallel(files, 250, 16)
        t_par = time.monotonic() - t
    print(f"  scan {len(files)} files: {t_scan:.2f}s | read snippets serial: {t_serial:.2f}s | "
          f"parallel(16 threads): {t_par:.2f}s")


if __name__ == "__main__":
    print(f"Assumptions: first token {TTFT}s, {TPS:.0f} output tok/s, 10,000 files (15% content-less photos)")
    old_style_estimate()
    run_scenario("NEW, one DeepSeek-class model (48 concurrent, no RPM cap)",
                 ["deepseek/deepseek-flash"], {})
    run_scenario("NEW, DeepSeek + Gemini sharing the queue (Gemini capped at 10 RPM)",
                 ["gemini/g", "deepseek/deepseek-flash"],
                 {"gemini/g": ModelLimits(6, rpm=10)})
    real_disk_bench()

