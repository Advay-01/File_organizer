"""Fast CLI for big folders.

    python -m app.fast_run C:\\Downloads                 # dry run, prints a summary
    python -m app.fast_run C:\\Downloads --apply         # move files (undo with app.main --undo)
    python -m app.fast_run C:\\Downloads -m deepseek/deepseek-flash -m gemini/gemini-3.8-flash
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

from app.core import history
from app.core.cache import OrganizeCache
from app.core.cached_planner import plan_with_cache
from app.core.executor import execute_plan
from app.core.fast_planner import config_from_env
from app.core.scanner import scan_directory


def _models(cli):
    if cli:
        return cli
    env = os.getenv("ORGANIZER_MODELS", "")
    return [m.strip() for m in env.split(",") if m.strip()] or ["gemini/gemini-3.8-flash"]


def _history_command(out: str, a) -> int:
    """--history / --undo-run / --undo-last: no scanning, no LLM, no API key needed."""
    if a.undo_run is not None or a.undo_last:
        try:
            res = history.undo_last_run(out) if a.undo_last else history.undo_run(out, a.undo_run)
        except KeyError:
            print(f"No run with id {a.undo_run}. See: python -m app.fast_run {out} --history", file=sys.stderr)
            return 1
        except ValueError as e:
            print(f"Nothing to do: {e}", file=sys.stderr)
            return 1
        if res is None:
            print("Nothing left to undo.")
            return 0
        print(f"Restored {res['restored']} file(s); run is now: {res['status']}")
        for s in res["skipped"]:
            print(f"  skipped {s['file']}: {s['reason']}")
        return 0

    runs = history.list_runs(out)
    if not runs:
        print("No runs recorded for this folder yet.")
        return 0
    print(f"{'ID':>3}  {'WHEN':<19}  {'FILES':>5}  {'RENAMED':>7}  STATUS            TOP FOLDERS")
    for r in runs:
        top = ", ".join(f"{k} ({v})" for k, v in list(r["folders"].items())[:3])
        print(f"{r['id']:>3}  {r['time'][:19]:<19}  {r['files']:>5}  {r['renamed']:>7}  {r['status']:<16}  {top}")
    return 0


def main(argv=None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description="Fast AI file organizer for thousands of files.")
    p.add_argument("source")
    p.add_argument("-o", "--output")
    p.add_argument("-m", "--model", action="append", help="repeat to share work across models")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--batch-size", type=int)
    p.add_argument("--show-all", action="store_true", help="print every move in a dry run")
    p.add_argument("--rename", action="store_true", help="also suggest clean names for unclear file names")
    p.add_argument("--no-cache", action="store_true", help="reclassify every scanned file")
    p.add_argument("--clear-cache", action="store_true", help="forget saved classifications, then exit")
    p.add_argument("--history", action="store_true", help="list past runs for this folder, then exit")
    p.add_argument("--undo-run", type=int, metavar="ID", help="undo one past run (see --history), then exit")
    p.add_argument("--undo-last", action="store_true", help="undo the newest run that can still be undone")
    a = p.parse_args(argv)

    out = a.output or a.source
    if a.clear_cache:
        OrganizeCache().clear()
        print("Saved classifications cleared.")
        return 0
    if a.history or a.undo_run is not None or a.undo_last:
        return _history_command(out, a)
    t0 = time.monotonic()
    files = scan_directory(a.source, recursive=a.recursive)
    print(f"Scanned {len(files)} files in {time.monotonic() - t0:.1f}s")
    if not files:
        return 0

    cfg = config_from_env()
    if a.batch_size:
        cfg.batch_size = a.batch_size
    if a.rename:
        cfg.rename = True
    last = {"stage": None}

    def progress(stage, done, total):
        if stage != last["stage"] or done == total or done % 1000 == 0:
            print(f"  {stage}: {done}/{total}", flush=True)
            last["stage"] = stage

    existing = sorted(d.name for d in Path(out).iterdir() if d.is_dir() and not d.name.startswith(".")) \
        if Path(out).is_dir() else []
    try:
        res = plan_with_cache(files, out, _models(a.model), existing, cfg, progress,
                              use_cache=not a.no_cache)
    except RuntimeError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1

    s = res.stats
    print(f"\nPlanned {s['files']} files -> {s['folders']} folders in {s['seconds']}s "
          f"({s['calls']} LLM calls, {s['rate_limited']} rate-limit hits, "
          f"{s['unclassified_units']} unclassified)")
    if s.get("renamed"):
        print(f"  {s['renamed']} file(s) will also get a clearer name")
    if s.get("already_organized"):
        print(f"  {s['already_organized']} already organized file(s) skipped")
    for folder, n in Counter(m.folder for m in res.moves).most_common(25):
        print(f"  {n:6d}  {folder}")

    if a.apply:
        with contextlib.redirect_stdout(io.StringIO()):  # executor prints one line per file
            done = execute_plan(
                res.moves,
                out,
                dry_run=False,
                source_root=a.source,
            )
        if done:
            history.remember_folder(out)  # makes this folder appear in the global history view
        runs = history.list_runs(out)
        hint = f"--undo-run {runs[0]['id']}" if runs else "--undo-last"
        print(f"\nMoved {len(done)} files. Undo with: python -m app.fast_run {out} {hint}")
    else:
        if a.show_all:
            execute_plan(res.moves, out, dry_run=True)
        print("\nDry run only. Re-run with --apply to move files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

