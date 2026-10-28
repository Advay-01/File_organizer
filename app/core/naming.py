"""Deterministic preservation of identifiers in grounded rename suggestions."""
from __future__ import annotations

from pathlib import Path
import re

_IDENTIFIER = re.compile(
    r"(?<!\d)(?:\d{4}[-_.]\d{2}[-_.]\d{2}|\d{2}[-_.]\d{2}[-_.]\d{4})(?!\d)"
    r"|(?<![A-Za-z0-9])v\d+(?:\.\d+)*(?![A-Za-z0-9])", re.I,
)


def preserve_name_tokens(proposed: str, original: str, limit: int = 80, *, preserve_dates: bool = True) -> str | None:
    """Shorten descriptive wording before dropping an original date or version.

    A name that cannot fit its protected tokens requires review. File contents
    and generated names never supply new protected identifiers.
    """
    extension = next((proposed[-len(suffix):] for suffix in (".tar.gz", ".tar.bz2", ".tar.xz")
                      if proposed.lower().endswith(suffix)), Path(proposed).suffix)
    stem = proposed[:-len(extension)] if extension else proposed
    tokens = list(dict.fromkeys(match.group() for match in _IDENTIFIER.finditer(Path(original).stem)
                                if preserve_dates or match.group().lower().startswith("v")))
    missing = [token for token in tokens if not re.search(
        r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])", stem, re.I,
    )]
    name = stem + ("_" + "_".join(missing) if missing else "")
    budget = limit - len(extension)
    if len(name) <= budget:
        return name + extension
    if not tokens:
        return None
    suffix = "_".join(tokens)
    available = budget - len(suffix) - 1
    if available < 1:
        return None
    for token in tokens:
        stem = re.sub(re.escape(token), "", stem, flags=re.I)
    prefix = stem[:available].rstrip(" ._-")
    return f"{prefix}_{suffix}{extension}" if prefix else None
