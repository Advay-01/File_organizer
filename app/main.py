"""CLI entry point.

Examples:
    python -m app.main ./Downloads                       # dry run (default)
    python -m app.main ./Downloads --apply               # really move files
    python -m app.main ./Downloads -o ./Sorted --apply
    python -m app.main ./Downloads -m ollama/llama3.2 -m gemini/gemini-2.5-flash
    python -m app.main ./Sorted --undo                   # revert the last run
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.core.classifier import LLMClassifier
from app.core.executor import execute_plan, undo_last
from app.core.extractor import attach_snippets
from app.core.planner import build_plan
from app.core.scanner import scan_directory

DEFAULT_MODELS = ["gemini/gemini-2.5-flash", "ollama/llama3.2"]


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AI file organizer: sorts files into topic folders by content.")
    p.add_argument("source", help="Folder to organize (or output folder when using --undo)")
    p.add_argument("-o", "--output", help="Where topic folders are created (default: inside source)")
    p.add_argument("-m", "--model", action="append",
                   help="litellm model string; repeat for fallbacks. Default: env ORGANIZER_MODELS or built-in")
    p.add_argument("--apply", action="store_true", help="Actually move files (default is a dry run)")
    p.add_argument("--recursive", action="store_true", help="Include files in subfolders")
    p.add_argument("--batch-size", type=int, default=20)
    p.add_argument("--snippet-chars", type=int, default=600)
    p.add_argument("--undo", action="store_true", help="Revert the last --apply run")
    return p.parse_args(argv)


def resolve_models(cli_models) -> list:
    if cli_models:
        return cli_models
    env = os.getenv("ORGANIZER_MODELS", "")
    if env.strip():
        return [m.strip() for m in env.split(",") if m.strip()]
    return DEFAULT_MODELS


def main(argv=None) -> int:
    load_dotenv()
    args = parse_args(argv)
    out_root = args.output or args.source

    if args.undo:
        n = undo_last(out_root)
        print(f"Restored {n} file(s).")
        return 0

    print(f"Scanning {args.source} ...")
    files = scan_directory(args.source, recursive=args.recursive)
    # Don't re-sort files already inside the output folder when it lives in source.
    out_abs = Path(out_root).resolve()
    files = [f for f in files if not (args.recursive and Path(f.path).resolve().is_relative_to(out_abs)
                                       and out_abs != Path(args.source).resolve())]
    print(f"Found {len(files)} file(s).")
    if not files:
        return 0
    attach_snippets(files, args.snippet_chars)
    models = resolve_models(args.model)
    print(f"Models (in fallback order): {', '.join(models)}")

    existing = sorted(d.name for d in Path(out_root).iterdir() if d.is_dir() and not d.name.startswith(".")) \
        if Path(out_root).is_dir() else []

    try:
        moves = build_plan(files, out_root, LLMClassifier(models), args.batch_size, existing)
    except RuntimeError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1

    print(f"\n{'APPLYING' if args.apply else 'DRY RUN (nothing will move; pass --apply)'}:")
    done = execute_plan(
        moves,
        out_root,
        dry_run=not args.apply,
        source_root=args.source,
    )
    if args.apply:
        print(f"\nMoved {len(done)} file(s). Undo with: python -m app.main {out_root} --undo")
        for failure in done.failures:
            print(f"Skipped {failure['file']}: {failure['reason']}")
        if done.log_error:
            print(done.log_error)
        if done.failed or done.log_error:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
