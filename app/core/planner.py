"""Turn scanned files into a validated, safe list of moves."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from app.core.classifier import LLMClassifier
from app.models.schemas import FileInfo

SYSTEM_PROMPT = """You are a smart file-system architect. Look at each file's name, format, and content, then generate a dynamic two-tier folder structure (Category/Subcategory) to organize it.
You get existing topic folders, their existing subfolders, and files (filename, extension, and a content snippet).

CRITICAL GROUPING RULE (prevent sprawl):
- Do NOT create hyper-specific folders for minor differences.
- Group vague, fuzzy, or related topics under broader, universally understood umbrella concepts.
- If a topic is vague but leans toward a concept, put it in that category's subfolder rather than creating a new one.
- Prefer an existing category or subcategory when it fits. Create a concise new subcategory only when it provides a genuinely useful broad grouping.

Rules:
1. Return every input file exactly once and copy its "path" exactly as given.
2. "target_path" must contain exactly two plain folder names separated by one slash, for example "Physics/Lecture Notes". It must never include a filename, a leading slash, or extra levels.
3. If content is unclear, use the filename; if still unclear use "Misc/General".
4. "new_name" is optional: a cleaner filename with the same extension, or "" to keep the name.
5. Respond only with valid JSON in this batch format:
{"actions":[{"path":"...","target_path":"Main Category/Subcategory","reasoning":"Why this grouping makes sense","new_name":""}]}"""

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_folder(name: str) -> str:
    name = _BAD.sub("", (name or "").strip()).strip(". ")
    return name[:40] or "Misc"


def sanitize_subfolder(name: str) -> str:
    """Allow an empty optional subfolder, but never a path."""
    if not (name or "").strip():
        return ""
    return sanitize_folder(name)


def sanitize_target_path(target_path: str) -> tuple[str, str]:
    """Accept exactly Category/Subcategory and turn malformed output into Misc/General."""
    parts = [part.strip() for part in re.split(r"[\\/]+", target_path or "") if part.strip()]
    if len(parts) != 2 or any(part in {".", ".."} for part in parts):
        return "Misc", "General"
    category = sanitize_folder(parts[0])
    subcategory = sanitize_subfolder(parts[1])
    return category, subcategory or "General"


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
    subfolder: str = ""
    selected: bool = True


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
    if not folders and root.is_dir():
        folders = sorted(item.name for item in root.iterdir() if item.is_dir() and not item.name.startswith("."))
    subfolders_by_topic = {
        folder: sorted(
            item.name
            for item in (root / folder).iterdir()
            if item.is_dir() and not item.name.startswith(".")
        )
        for folder in folders
        if (root / folder).is_dir()
    }

    for batch in _batches(files, batch_size):
        payload = {
            "existing_folders": folders,
            "existing_subfolders": subfolders_by_topic,
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
            if a.target_path:
                folder, subfolder = sanitize_target_path(a.target_path)
            else:
                folder = sanitize_folder(a.folder)
                subfolder = sanitize_subfolder(a.subfolder)
            if folder not in folders:
                folders.append(folder)
            if subfolder and subfolder not in subfolders_by_topic.setdefault(folder, []):
                subfolders_by_topic[folder].append(subfolder)
            name = sanitize_filename(a.new_name, f.filename)
            destination_folder = root / folder / subfolder if subfolder else root / folder
            dest = _unique(destination_folder / name, taken)
            taken.add(str(dest))
            chosen[a.path] = Move(a.path, str(dest), folder, a.reasoning or a.reason, subfolder)

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
