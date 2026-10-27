"""Shared path containment, protected-location, and scan policy checks."""
from __future__ import annotations

import os
import ctypes
import errno
import hashlib
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

PARTIAL_SUFFIXES = (
    ".part", ".partial", ".download", ".crdownload", ".tmp", ".temp", "~"
)
PROJECT_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml")
PROTECTED_NAMES = {
    ".git", ".idea", ".venv", "venv", "__pycache__", "node_modules"
}


class UnsafePathError(ValueError):
    pass


class StaleSourceError(OSError):
    """The file approved in the preview is no longer the same regular file."""


@dataclass(frozen=True)
class FileSnapshot:
    sha256: str = ""
    identity: tuple[int, ...] = ()


def _stat_identity(info: os.stat_result) -> tuple[int, ...]:
    # Windows stat/lstat and fstat disagree on ctime in some Python versions.
    # Birth time is consistent there; POSIX ctime detects metadata-only changes.
    change_time = getattr(info, "st_birthtime_ns", info.st_ctime_ns) if os.name == "nt" else info.st_ctime_ns
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, change_time)


def file_identity(path: Path) -> tuple[int, ...]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or is_reparse_point(path):
        raise StaleSourceError("Source is not a regular file.")
    return _stat_identity(info)


def snapshot_file(path: Path) -> FileSnapshot:
    """Hash once, rejecting replacement or edits during the read, including links."""
    before = file_identity(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        if _stat_identity(os.fstat(handle.fileno())) != before:
            raise StaleSourceError("Source changed while opening it.")
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        if _stat_identity(os.fstat(handle.fileno())) != before or file_identity(path) != before:
            raise StaleSourceError("Source changed while reading it.")
    return FileSnapshot(digest.hexdigest(), before)


def verify_file_snapshot(path: Path, expected: FileSnapshot | None) -> None:
    if not expected or not expected.sha256 or file_identity(path) != expected.identity:
        raise StaleSourceError("Source changed since analysis; reanalysis is required.")
    if snapshot_file(path) != expected:
        raise StaleSourceError("Source content changed; reanalysis is required.")


@dataclass(frozen=True)
class ScanPolicy:
    max_files: int = 10_000
    max_file_bytes: int = 2 * 1024 * 1024 * 1024
    max_archive_entries: int = 1_000
    recent_seconds: int = 5
    skip_partial: bool = True
    skip_hidden_system: bool = True
    include_hidden_files: bool = False

    @classmethod
    def from_env(cls, recent_default: int = 5) -> "ScanPolicy":
        return cls(
            max_files=int(os.getenv("ORGANIZER_MAX_FILES", "10000")),
            max_file_bytes=int(float(os.getenv("ORGANIZER_MAX_FILE_MB", "2048")) * 1024 * 1024),
            max_archive_entries=int(os.getenv("ORGANIZER_MAX_ARCHIVE_ENTRIES", "1000")),
            recent_seconds=int(os.getenv("ORGANIZER_RECENT_SECONDS", str(recent_default))),
            skip_partial=os.getenv("ORGANIZER_SKIP_PARTIAL", "1").lower() not in {"0", "false", "no"},
            skip_hidden_system=os.getenv("ORGANIZER_SKIP_HIDDEN_SYSTEM", "1").lower() not in {"0", "false", "no"},
            include_hidden_files=os.getenv("ORGANIZER_INCLUDE_HIDDEN_FILES", "0").lower() in {"1", "true", "yes"},
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


def is_system_path(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4))
    except OSError:
        return True


def resolved_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError, RuntimeError):
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
    if any(part.casefold() in PROTECTED_NAMES for part in resolved.parts):
        raise UnsafePathError(f"Protected location cannot be scanned: {resolved}")
    for protected, descendants in _protected_roots():
        if resolved == protected or (descendants and protected in resolved.parents):
            raise UnsafePathError(f"Protected location cannot be scanned: {resolved}")
    project_root = next(
        (
            candidate
            for candidate in (resolved, *resolved.parents)
            if (
                # A lone ancestor manifest can belong to a home/downloads folder.
                # A repository or a manifest with its build files is stronger evidence.
                any((candidate / marker).exists() for marker in PROJECT_MARKERS)
                if candidate == resolved else _confirmed_project_root(candidate)
            )
        ),
        None,
    )
    if project_root is not None:
        raise UnsafePathError(
            f"The selected folder is inside a detected project root ({project_root}). "
            "Choose a dedicated folder to organize."
        )
    return resolved


def _confirmed_project_root(path: Path) -> bool:
    if (path / ".git").exists():
        return True
    companions = {
        "package.json": ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "src"),
        "pyproject.toml": ("uv.lock", "poetry.lock", "src"),
        "Cargo.toml": ("Cargo.lock", "src"),
    }
    return any(
        (path / manifest).is_file() and any((path / name).exists() for name in names)
        for manifest, names in companions.items()
    )


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
    return any((path / marker).exists() for marker in PROJECT_MARKERS)


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
        if (
            any(part.casefold() in PROTECTED_NAMES for part in root.parts)
            or is_protected_path(root)
            or any(parent.exists() and (is_reparse_point(parent) or _confirmed_project_root(parent))
                   for parent in root.parents)
        ):
            raise UnsafePathError("The approved output root is in a protected or reparse-point location.")
    approved_source_root = assert_safe_root(source_root or output_root)
    try:
        source_resolved = source.resolve(strict=True)
        destination_resolved = destination.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise UnsafePathError(f"Could not resolve file operation path: {error}") from error
    if not resolved_inside(source_resolved, approved_source_root):
        raise UnsafePathError("Source file resolves outside its approved root.")
    if not resolved_inside(destination_resolved, root):
        raise UnsafePathError("Destination resolves outside the approved root.")
    if not source_resolved.is_file():
        raise UnsafePathError("Source is not a regular file.")
    if is_reparse_point(source) or any(
        parent.exists() and is_reparse_point(parent)
        for parent in source.parents
        if resolved_inside(parent, approved_source_root)
    ) or (destination.exists() and is_reparse_point(destination)) or any(
        parent.exists() and is_reparse_point(parent)
        for parent in destination.parents
        if resolved_inside(parent, root)
    ):
        raise UnsafePathError("File operations may not traverse symlinks or junctions.")
    relative_parts = destination_resolved.relative_to(root).parts
    if any(part in {".", ".."} for part in destination.parts):
        raise UnsafePathError("Destination contains an unsafe relative path.")
    if any(part.casefold() in PROTECTED_NAMES for part in relative_parts):
        raise UnsafePathError("Destination uses a protected directory name.")
    source_parts = source_resolved.relative_to(approved_source_root).parts
    if any(part.casefold() in PROTECTED_NAMES for part in source_parts):
        raise UnsafePathError("Source uses a protected directory name.")
    for path, boundary in ((source.parent, approved_source_root), (destination.parent, root)):
        while path != boundary and resolved_inside(path, boundary):
            if any((path / marker).exists() for marker in PROJECT_MARKERS):
                raise UnsafePathError("File operations may not enter a software project.")
            path = path.parent


def _rename_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(source, destination)
        return
    # POSIX os.rename replaces existing files. Use native exclusive rename
    # flags, including when a destination is created after validation.
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = getattr(library, "renamex_np", None)
        if rename is None:
            raise OSError(errno.ENOSYS, "Exclusive rename is unavailable")
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(source), os.fsencode(destination), 0x00000004)  # RENAME_EXCL
    else:
        rename = getattr(library, "renameat2", None)
        if rename is None:
            raise OSError(errno.ENOSYS, "Exclusive rename is unavailable")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # AT_FDCWD, RENAME_NOREPLACE
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def move_file_no_overwrite(
    source: Path,
    destination: Path,
    *,
    expected_snapshot: FileSnapshot | None = None,
    validate: Callable[[], None] | None = None,
) -> None:
    """Move a file without a check-then-overwrite race."""
    if expected_snapshot is not None and file_identity(source) != expected_snapshot.identity:
        raise StaleSourceError("Source changed immediately before moving it.")
    if validate:
        validate()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if validate:
        validate()
    if expected_snapshot is not None:
        verify_file_snapshot(source, expected_snapshot)
    # Recheck paths after hashing as well: a parent may have changed during I/O.
    if validate:
        validate()
    if expected_snapshot is not None and file_identity(source) != expected_snapshot.identity:
        raise StaleSourceError("Source changed immediately before moving it.")
    try:
        _rename_no_replace(source, destination)
        return
    except FileExistsError:
        raise
    except OSError as error:
        if error.errno not in {errno.EXDEV, errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
            raise

    created = False
    try:
        with source.open("rb") as input_file, destination.open("xb") as output_file:
            created = True
            before = _stat_identity(os.fstat(input_file.fileno()))
            if expected_snapshot is not None and before != expected_snapshot.identity:
                raise StaleSourceError("Source changed before copying it.")
            if expected_snapshot is None:
                shutil.copyfileobj(input_file, output_file, 1024 * 1024)
            else:
                copied_digest = hashlib.sha256()
                for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                    output_file.write(chunk)
                    copied_digest.update(chunk)
                if copied_digest.hexdigest() != expected_snapshot.sha256:
                    raise StaleSourceError("Source content changed during copying.")
            if _stat_identity(os.fstat(input_file.fileno())) != before or file_identity(source) != before:
                raise StaleSourceError("Source changed during copying.")
        if validate:
            validate()
        shutil.copystat(source, destination)
        if file_identity(source) != before:
            raise StaleSourceError("Source changed before removal.")
        source.unlink()
    except Exception:
        if created:
            try:
                destination.unlink()
            except OSError:
                pass
        raise
