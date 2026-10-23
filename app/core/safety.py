"""Shared path containment, protected-location, and scan policy checks."""
from __future__ import annotations

import os
import errno
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PARTIAL_SUFFIXES = (
    ".part", ".partial", ".download", ".crdownload", ".tmp", ".temp", "~"
)
PROJECT_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml")
PROTECTED_NAMES = {
    ".git", ".idea", ".venv", "venv", "__pycache__", "node_modules"
}


class UnsafePathError(ValueError):
    pass


@dataclass(frozen=True)
class ScanPolicy:
    max_files: int = 10_000
    max_file_bytes: int = 2 * 1024 * 1024 * 1024
    max_archive_entries: int = 1_000
    recent_seconds: int = 5
    skip_partial: bool = True
    skip_hidden_system: bool = True

    @classmethod
    def from_env(cls, recent_default: int = 5) -> "ScanPolicy":
        return cls(
            max_files=int(os.getenv("ORGANIZER_MAX_FILES", "10000")),
            max_file_bytes=int(float(os.getenv("ORGANIZER_MAX_FILE_MB", "2048")) * 1024 * 1024),
            max_archive_entries=int(os.getenv("ORGANIZER_MAX_ARCHIVE_ENTRIES", "1000")),
            recent_seconds=int(os.getenv("ORGANIZER_RECENT_SECONDS", str(recent_default))),
            skip_partial=os.getenv("ORGANIZER_SKIP_PARTIAL", "1").lower() not in {"0", "false", "no"},
            skip_hidden_system=os.getenv("ORGANIZER_SKIP_HIDDEN_SYSTEM", "1").lower() not in {"0", "false", "no"},
        )


def is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return True


def is_hidden_or_system(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(), "st_file_attributes", 0)
        hidden = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
        system = getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4)
        return bool(attributes & (hidden | system))
    except OSError:
        return True


def resolved_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _protected_roots() -> list[tuple[Path, bool]]:
    candidates: list[tuple[Path, bool]] = []
    for variable in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        value = os.getenv(variable)
        if value:
            candidates.append((Path(value), True))
    candidates.append((Path.home(), False))
    app_home = os.getenv("ORGANIZER_HOME")
    if app_home:
        candidates.append((Path(app_home), True))
    for variable in ("APPDATA", "LOCALAPPDATA"):
        value = os.getenv(variable)
        if value:
            candidates.extend(
                [
                    (Path(value) / "com.sortly.organizer", True),
                    (Path(value) / "Sortly", True),
                ]
            )
    return [
        (candidate.resolve(strict=False), descendants)
        for candidate, descendants in candidates
    ]


def assert_safe_root(root: Path) -> Path:
    raw = root.expanduser()
    if not raw.exists():
        raise FileNotFoundError(f"Path does not exist: {raw}")
    if is_reparse_point(raw):
        raise UnsafePathError("The selected root is a symlink or junction.")
    resolved = raw.resolve(strict=True)
    if resolved.name.casefold() in PROTECTED_NAMES:
        raise UnsafePathError(f"Protected location cannot be scanned: {resolved}")
    for protected, descendants in _protected_roots():
        if resolved == protected or (descendants and protected in resolved.parents):
            raise UnsafePathError(f"Protected location cannot be scanned: {resolved}")
    project_root = next(
        (
            candidate
            for candidate in (resolved, *resolved.parents)
            if any((candidate / marker).exists() for marker in PROJECT_MARKERS)
        ),
        None,
    )
    if project_root is not None:
        raise UnsafePathError(
            f"The selected folder is inside a detected project root ({project_root}). "
            "Choose a dedicated folder to organize."
        )
    return resolved


def is_protected_path(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    if resolved.name.casefold() in PROTECTED_NAMES:
        return True
    for protected, descendants in _protected_roots():
        if resolved == protected or (descendants and protected in resolved.parents):
            return True
    return False


def should_skip_directory(path: Path) -> bool:
    if is_protected_path(path):
        return True
    if is_reparse_point(path) or is_hidden_or_system(path):
        return True
    return any((path / marker).exists() for marker in PROJECT_MARKERS[1:])


def is_partial_download(path: Path) -> bool:
    name = path.name.casefold()
    return name.startswith(("~$", ".~")) or name.endswith(PARTIAL_SUFFIXES)


def validate_move_paths(
    source: Path,
    destination: Path,
    output_root: Path,
    source_root: Path | None = None,
) -> None:
    try:
        root = assert_safe_root(output_root)
    except FileNotFoundError:
        root = output_root.expanduser().resolve(strict=False)
        if is_protected_path(root) or any(is_reparse_point(parent) for parent in root.parents if parent != root):
            raise UnsafePathError("The approved output root is in a protected or reparse-point location.")
    approved_source_root = assert_safe_root(source_root or output_root)
    try:
        source_resolved = source.resolve(strict=True)
        destination_resolved = destination.resolve(strict=False)
    except OSError as error:
        raise UnsafePathError(f"Could not resolve file operation path: {error}") from error
    if not resolved_inside(source_resolved, approved_source_root):
        raise UnsafePathError("Source file resolves outside its approved root.")
    if not resolved_inside(destination_resolved, root):
        raise UnsafePathError("Destination resolves outside the approved root.")
    if is_reparse_point(source) or (destination.exists() and is_reparse_point(destination)) or any(
        parent.exists() and is_reparse_point(parent)
        for parent in destination.parents
        if root in parent.parents or parent == root
    ):
        raise UnsafePathError("File operations may not traverse symlinks or junctions.")
    relative_parts = destination.relative_to(root).parts
    if any(part in {".", ".."} for part in relative_parts):
        raise UnsafePathError("Destination contains an unsafe relative path.")
    if any(part.casefold() in PROTECTED_NAMES for part in relative_parts):
        raise UnsafePathError("Destination uses a protected directory name.")


def move_file_no_overwrite(source: Path, destination: Path) -> None:
    """Move a file without a check-then-overwrite race."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(source, destination)
        return
    except FileExistsError:
        raise
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise

    created = False
    try:
        with source.open("rb") as input_file, destination.open("xb") as output_file:
            created = True
            shutil.copyfileobj(input_file, output_file, 1024 * 1024)
        shutil.copystat(source, destination)
        source.unlink()
    except Exception:
        if created:
            try:
                destination.unlink()
            except OSError:
                pass
        raise
