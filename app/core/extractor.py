"""Pull a short text snippet out of a file so the LLM can judge its topic.

Supports plain text/code, PDF (pypdf) and DOCX (python-docx). Anything else
returns an empty string and the classifier falls back to the filename.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

from app.models.schemas import FileInfo

TEXT_EXTS = {
    ".txt", ".md", ".rst", ".csv", ".json", ".yaml", ".yml", ".toml", ".ini", ".log",
    ".py", ".java", ".c", ".cpp", ".h", ".js", ".ts", ".go", ".rs", ".html", ".css",
    ".sql", ".ipynb", ".tex",
}


def _clean(text: str, max_chars: int) -> str:
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def _read_text(path: Path, max_chars: int) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read(max_chars * 4)


def _read_pdf(path: Path, max_chars: int) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    out: List[str] = []
    total = 0
    for page in reader.pages[:5]:
        t = page.extract_text() or ""
        out.append(t)
        total += len(t)
        if total >= max_chars:
            break
    return " ".join(out)


def _read_docx(path: Path, max_chars: int) -> str:
    import docx

    d = docx.Document(str(path))
    out: List[str] = []
    total = 0
    for para in d.paragraphs:
        out.append(para.text)
        total += len(para.text)
        if total >= max_chars:
            break
    return " ".join(out)


def extract_snippet(path: str, max_chars: int = 600) -> str:
    """Best-effort snippet; never raises."""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext in TEXT_EXTS:
            raw = _read_text(p, max_chars)
        elif ext == ".pdf":
            raw = _read_pdf(p, max_chars)
        elif ext == ".docx":
            raw = _read_docx(p, max_chars)
        else:
            return ""
    except Exception:
        return ""
    return _clean(raw, max_chars)


def attach_snippets(files: List[FileInfo], max_chars: int = 600) -> List[FileInfo]:
    for f in files:
        f.snippet = extract_snippet(f.path, max_chars)
    return files
