"""Pull useful text or metadata from a file so the LLM can judge its topic.

Archives are only inspected for their member names; they are never extracted.
Image files contribute their format, dimensions, and available EXIF description.
"""
from __future__ import annotations

import gzip
import os
import tarfile
import re
import zipfile
from pathlib import Path
from typing import List

from app.models.schemas import FileInfo

def _archive_entry_limit() -> int:
    return max(1, int(os.getenv("ORGANIZER_MAX_ARCHIVE_ENTRIES", "1000")))

TEXT_EXTS = {
    ".txt", ".md", ".rst", ".csv", ".json", ".yaml", ".yml", ".toml", ".ini", ".log",
    ".py", ".java", ".c", ".cpp", ".h", ".js", ".ts", ".go", ".rs", ".html", ".css",
    ".sql", ".ipynb", ".tex", ".srt", ".vtt", ".svg",
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


def _read_pptx(path: Path, max_chars: int) -> str:
    from pptx import Presentation

    presentation = Presentation(str(path))
    text = []
    for slide in presentation.slides:
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text:
                text.append(shape.text)
                if len(" ".join(text)) >= max_chars:
                    return " ".join(text)
    return " ".join(text)


def _read_spreadsheet(path: Path, max_chars: int) -> str:
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    text = []
    try:
        for sheet in workbook.worksheets:
            text.append(f"Sheet: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                values = [str(value) for value in row if value is not None]
                if values:
                    text.append(" | ".join(values))
                if len(" ".join(text)) >= max_chars:
                    return " ".join(text)
    finally:
        workbook.close()
    return " ".join(text)


def _strip_markup(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text)


def _read_epub(path: Path, max_chars: int) -> str:
    text = []
    with zipfile.ZipFile(path) as archive:
        names = [
            name for name in archive.namelist()
            if name.lower().endswith((".xhtml", ".html", ".htm"))
        ]
        for name in names[:10]:
            with archive.open(name) as entry:
                text.append(_strip_markup(entry.read(max_chars * 4).decode("utf-8", errors="ignore")))
            if len(" ".join(text)) >= max_chars:
                break
    return " ".join(text)


def _read_zip_members(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        names = [item.filename for item in archive.infolist() if not item.is_dir()]
    return "Archive contents: " + ", ".join(names[: _archive_entry_limit()])


def _read_tar_members(path: Path) -> str:
    names = []
    with tarfile.open(path, mode="r:*") as archive:
        for index, item in enumerate(archive):
            if item.isfile():
                names.append(item.name)
            if index + 1 >= _archive_entry_limit():
                break
    return "Archive contents: " + ", ".join(names)


def _read_gzip(path: Path, max_chars: int) -> str:
    with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as archive:
        return archive.read(max_chars * 4)


def _read_rar_members(path: Path) -> str:
    import rarfile

    with rarfile.RarFile(path) as archive:
        names = [item.filename for item in archive.infolist() if not item.is_dir()]
    return "Archive contents: " + ", ".join(names[: _archive_entry_limit()])


def _read_image_metadata(path: Path) -> str:
    from PIL import Image

    if path.suffix.lower() == ".heic":
        import pillow_heif

        pillow_heif.register_heif_opener()
    with Image.open(path) as image:
        parts = [f"Image: {image.format or path.suffix[1:].upper()}", f"{image.width}x{image.height}"]
        description = image.getexif().get(270)
        if description:
            parts.append(f"Description: {description}")
    return "; ".join(parts)


def extract_snippet(path: str, max_chars: int = 600) -> str:
    """Best-effort snippet; never raises."""
    p = Path(path)
    ext = p.suffix.lower()
    name = p.name.lower()
    try:
        if ext in TEXT_EXTS:
            raw = _read_text(p, max_chars)
        elif ext == ".pdf":
            raw = _read_pdf(p, max_chars)
        elif ext == ".docx":
            raw = _read_docx(p, max_chars)
        elif ext == ".pptx":
            raw = _read_pptx(p, max_chars)
        elif ext in {".xlsx", ".xlsm"}:
            raw = _read_spreadsheet(p, max_chars)
        elif ext == ".epub":
            raw = _read_epub(p, max_chars)
        elif ext == ".zip":
            raw = _read_zip_members(p)
        elif ext in {".tar", ".tgz"} or name.endswith(".tar.gz"):
            raw = _read_tar_members(p)
        elif ext == ".gz":
            raw = _read_gzip(p, max_chars)
        elif ext == ".rar":
            raw = _read_rar_members(p)
        elif ext in {".jpg", ".jpeg", ".png", ".heic", ".webp", ".tiff", ".tif", ".bmp", ".gif"}:
            raw = _read_image_metadata(p)
        else:
            return ""
    except Exception:
        return ""
    return _clean(raw, max_chars)


def attach_snippets(files: List[FileInfo], max_chars: int = 600) -> List[FileInfo]:
    for f in files:
        f.snippet = extract_snippet(f.path, max_chars)
    return files
