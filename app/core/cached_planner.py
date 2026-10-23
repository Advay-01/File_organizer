"""Cache-aware wrapper around the fast planner."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from app.core.cache import OrganizeCache
from app.core.fast_planner import FastConfig, plan_files_fast
from app.core.planner import Move


@dataclass
class CachedResult:
    moves: list[Move] = field(default_factory=list)
    already_organized: list[Path] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _file_path(file_info: object) -> Path:
    return Path(getattr(file_info, "path", file_info))


def _safe_folder(folder: str) -> str | None:
    parts = str(folder).replace("\\", "/").split("/")
    if not 1 <= len(parts) <= 2 or any(not part or part in {".", ".."} for part in parts):
        return None
    if any(any(character in '<>:"|?*' for character in part) for part in parts):
        return None
    return "/".join(parts)


def _target(move: Move) -> str:
    return "/".join(part for part in (move.folder, move.subfolder) if part)


def _unique_destination(destination: Path, taken: set[Path]) -> Path:
    candidate = destination
    index = 1
    while candidate in taken or candidate.exists():
        candidate = destination.with_name(f"{destination.stem}_{index}{destination.suffix}")
        index += 1
    taken.add(candidate)
    return candidate


def _base_stats(file_count: int) -> dict:
    return {
        "files": file_count,
        "folders": 0,
        "seconds": 0.0,
        "calls": 0,
        "rate_limited": 0,
        "unclassified_units": 0,
        "renamed": 0,
    }


def plan_with_cache(
    files: Iterable[object],
    output_root: str,
    models: list[str],
    known_folders: list[str] | None = None,
    config: FastConfig | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    cache: OrganizeCache | None = None,
    use_cache: bool = True,
) -> CachedResult:
    """Reuse prior classifications and leave already-sorted files out of the plan."""
    all_files = list(files)
    root = Path(output_root).resolve()
    if not use_cache:
        planned = plan_files_fast(all_files, str(root), models, known_folders, config, progress)
        return CachedResult(moves=list(planned.moves), stats={**planned.stats, "cache_hits": 0,
                            "already_organized": 0, "sent_to_llm": len(all_files)})

    owns_cache = cache is None
    cache = cache or OrganizeCache()
    result = CachedResult()
    todo = []
    taken: set[Path] = set()

    for file_info in all_files:
        source = _file_path(file_info)
        hit = cache.get(source)
        if hit is None:
            todo.append(file_info)
            continue
        folder, reason = hit
        folder = _safe_folder(folder)
        if folder is None:
            todo.append(file_info)
            continue
        destination_folder = root.joinpath(*folder.split("/"))
        if source.parent.resolve() == destination_folder:
            result.already_organized.append(source)
            continue
        destination = _unique_destination(destination_folder / source.name, taken)
        result.moves.append(Move(str(source), str(destination), folder, f"{reason} (cached)"))

    if todo:
        planned = plan_files_fast(todo, str(root), models, known_folders, config, progress)
        for move in planned.moves:
            destination = _unique_destination(Path(move.dest), taken)
            result.moves.append(Move(move.src, str(destination), move.folder, move.reason,
                                     move.subfolder, move.selected))
        result.stats = dict(planned.stats)
        cache.put_many([(_file_path(file_info), _target(move), move.reason)
                        for file_info, move in zip(todo, planned.moves)])
    else:
        result.stats = _base_stats(len(all_files))

    result.stats.update(
        files=len(all_files),
        folders=len({_target(move) for move in result.moves}),
        cache_hits=len(all_files) - len(todo),
        already_organized=len(result.already_organized),
        sent_to_llm=len(todo),
    )
    if owns_cache:
        cache.close()
    return result
