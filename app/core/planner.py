"""Turn scanned files into a validated, safe list of moves."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from app.core.classifier import LLMClassifier
from app.models.schemas import FileInfo

SYSTEM_PROMPT = """You organize files into topic folders based on their CONTENT, not their extension.
You get a list of existing folders and a list of files (filename, extension, a content snippet).
Rules:
1. Prefer an existing folder when it fits. Create a new short folder name (1-2 words, e.g. "AI", "DSA", "Projects", "Finance") only when none fits.
2. Folder names are plain names, never paths, no slashes.
3. If a file's content is unclear, use the filename; if still unclear use "Misc".
4. Return EVERY input file exactly once. Copy "path" exactly as given.
5. "new_name" is optional: a cleaner filename WITH the same extension, or "" to keep the name.
Respond with ONLY JSON: {"actions":[{"path":"...","folder":"...","new_name":"","reason":"short"}]}"""

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_folder(name: str) -> str:
    name = _BAD.sub("", (name or "").strip()).strip(". ")
    return name[:40] or "Misc"


def sanitize_filename(name: str, original: str) -> str:
    """Keep the original extension; strip anything path-like."""
    name = _BAD.sub("", (name or "").strip()).strip(". ")
    if not name:
        return original
    ext = Path(original).suffix
    if Path(name).suffix.lower() != ext.lower():
        name = Path(name).stem + ext
    return name[:120]


@dataclass
class Move:
    src: str
    dest: str
    folder: str
    reason: str


def _batches(files: List[FileInfo], size: int) -> List[List[FileInfo]]:
    return [files[i : i + size] for i in range(0, len(files), size)]


def build_plan(
    files: List[FileInfo],
    output_root: str,
    classifier: LLMClassifier,
    batch_size: int = 20,
    known_folders: Optional[List[str]] = None,
) -> List[Move]:
    """Ask the LLM batch by batch, validate, and resolve name collisions."""
    folders: List[str] = list(known_folders or [])
    by_path: Dict[str, FileInfo] = {f.path: f for f in files}
    chosen: Dict[str, Move] = {}
    taken: set = set()
    root = Path(output_root)

    for batch in _batches(files, batch_size):
        payload = {
            "existing_folders": folders,
            "files": [
                {"path": f.path, "filename": f.filename, "extension": f.extension,
                 "size_kb": f.size_kb, "snippet": f.snippet}
                for f in batch
            ],
        }
        resp = classifier.classify(SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False))
        for a in resp.actions:
            f = by_path.get(a.path)
            if f is None or a.path in chosen:
                continue  # hallucinated or duplicate path: ignore
            folder = sanitize_folder(a.folder)
            if folder not in folders:
                folders.append(folder)
            name = sanitize_filename(a.new_name, f.filename)
            dest = _unique(root / folder / name, taken)
            taken.add(str(dest))
            chosen[a.path] = Move(a.path, str(dest), folder, a.reason)

    # Anything the model skipped goes to Misc rather than being left behind.
    for f in files:
        if f.path not in chosen:
            dest = _unique(root / "Misc" / f.filename, taken)
            taken.add(str(dest))
            chosen[f.path] = Move(f.path, str(dest), "Misc", "not classified by model")

    return [chosen[f.path] for f in files]


def _unique(dest: Path, taken: set) -> Path:
    if str(dest) not in taken and not dest.exists():
        return dest
    stem, suffix, i = dest.stem, dest.suffix, 1
    while True:
        cand = dest.with_name(f"{stem}_{i}{suffix}")
        if str(cand) not in taken and not cand.exists():
            return cand
        i += 1
