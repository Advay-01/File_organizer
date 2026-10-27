"""Pull useful text or metadata from a file so the LLM can judge its topic.

Archives are only inspected for their member names; they are never extracted.
Image files contribute their format, dimensions, and available EXIF description.
"""
from __future__ import annotations

import codecs
import csv
import gzip
import io
import json
import os
import tarfile
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List
from xml.etree import ElementTree

from app.models.schemas import FileInfo

def _archive_entry_limit() -> int:
    return max(1, int(os.getenv("ORGANIZER_MAX_ARCHIVE_ENTRIES", "1000")))

SNIFF_BYTES = 1024

TEXT_EXTS = {
    ".txt", ".md", ".rst", ".csv", ".json", ".yaml", ".yml", ".toml", ".ini", ".log",
    ".env", ".tf", ".hcl",
    ".py", ".java", ".c", ".cpp", ".h", ".js", ".ts", ".go", ".rs", ".html", ".css",
    ".sql", ".ipynb", ".tex", ".srt", ".vtt", ".svg", ".xml", ".sh", ".ps1",
    ".pgn", ".patch", ".diff",
}

HTML_MARKER = re.compile(r"(?is)<!doctype\s+html\b|<html(?:\s|>)|<script(?:\s|>)|<body(?:\s|>)")
SHEBANG_MARKER = re.compile(r"^#![^\r\n]*(?:bash|zsh|/sh)\b", re.I)
PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
ENV_ASSIGNMENT = re.compile(
    r"(?m)^[ \t]*(?P<export>export[ \t]+)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(?:[^\s#]+|[^\r\n]+)"
)
INI_SECTION = re.compile(r"(?m)^[ \t]*\[[^\]\r\n]+\][ \t]*\r?$")
PGN_TAG = re.compile(r'(?m)^\[(?:Event|Site|Date|Round|White|Black|Result|FEN|SetUp)\s+"[^"\r\n]*"\]\s*$')
PGN_MOVES = re.compile(r"(?m)(?:^|\s)\d+\s*\.(?:\.\.)?\s*(?:[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8]|O-O)")
PATCH_HEADER = re.compile(r"(?m)^diff --git a/[^\r\n]+ b/[^\r\n]+\s*$")
PATCH_PAIR = re.compile(r"(?m)^--- (?:a/[^\r\n]+|/dev/null)\r?\n\+\+\+ (?:b/[^\r\n]+|/dev/null)\s*$")
PATCH_HUNK = re.compile(r"(?m)^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
TOML_TABLE = re.compile(r"(?m)^[ \t]*\[\[?[A-Za-z0-9_.\"-]+\]?\][ \t]*\r?$")
TOML_TYPED_VALUE = re.compile(
    r"(?m)^[ \t]*[A-Za-z0-9_.\"-]+[ \t]*=[ \t]*"
    r"(?:true|false|[-+]?\d+(?:\.\d+)?|\[[^\r\n]*\]|\{[^\r\n]*\})"
    r"[ \t]*(?:#.*)?\r?$"
)
TERRAFORM_BLOCK = re.compile(
    r"(?im)^[ \t]*(?:terraform[ \t]*\{|"
    r"(?:provider|resource|variable|module|output|data)[ \t]+(?:\"[^\"\r\n]+\"|"
    r"[A-Za-z_][A-Za-z0-9_-]*)[ \t]*\{)"
)
SQL_MARKERS = (
    re.compile(r"(?is)\bselect\b.{0,500}\bfrom\b"),
    re.compile(r"(?is)\binsert\s+into\b|\bupdate\s+\w+\s+set\b|\bdelete\s+from\b"),
    re.compile(r"(?is)\bcreate\s+(?:table|index|view)\b|\balter\s+table\b|\bdrop\s+table\b"),
    re.compile(r"(?is)\bunion(?:\s+all)?\s+select\b"),
    re.compile(r"(?is)(?:'\s*or\s*'?1'?\s*=\s*'?1|;\s*(?:select|insert|update|delete|drop)\b)"),
)
CPP_MARKERS = (
    (re.compile(r"(?im)^\s*#include\s*[<\"][^>\"]+[>\"]"), 2),
    (re.compile(r"(?m)^\s*int\s+main\s*\("), 2),
    (re.compile(r"\bstd::|\busing\s+namespace\s+std\b|\bstd::(?:cin|cout|cerr|string)\b"), 2),
    (re.compile(r"\b(?:memcpy|strcpy|strcat|printf|malloc|free)\s*\("), 1),
    (re.compile(r"\b(?:char|int|void|bool)\s+[A-Za-z_]\w*\s*(?:\[|=|;)"), 1),
)
C_MARKERS = (
    (re.compile(r"(?im)^\s*#include\s*<(?:stdio|stdlib|string)\.h>"), 2),
    (re.compile(r"(?m)^\s*int\s+main\s*\("), 2),
    (re.compile(r"\b(?:memcpy|strcpy|strcat|printf|malloc|free)\s*\("), 1),
)


@dataclass(frozen=True)
class FormatDetection:
    extension: str = "unknown"
    confidence: str = "unknown"
    signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContentExtraction:
    text: str = ""
    parser_status: str = "unavailable"
    detected_type: str = "unknown"
    detection_confidence: str = "unknown"
    signals: tuple[str, ...] = ()
    warning: str = ""


def _clean(text: str, max_chars: int) -> str:
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def _decode_text(data: bytes) -> str | None:
    if not data:
        return ""
    encodings = (
        ("utf-16",)
        if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
        else ("utf-8-sig", "cp1252")
    )
    for encoding in encodings:
        try:
            decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
            text = decoder.decode(data, final=False)
        except (UnicodeDecodeError, UnicodeError):
            continue
        if not text:
            continue
        control = sum(
            1 for character in text
            if ord(character) < 32 and character not in "\t\r\n"
        )
        if "\x00" not in text and control / max(1, len(text)) <= 0.02:
            return text
    return None


def _read_sample(path: Path) -> tuple[bytes, str | None]:
    try:
        with path.open("rb") as source:
            sample = source.read(SNIFF_BYTES)
    except OSError:
        return b"", None
    return sample, _decode_text(sample)


def _read_text(path: Path, max_chars: int) -> str:
    return _decode_text(_read_bytes(path, max_chars)) or ""


def _read_bytes(path: Path, max_chars: int) -> bytes:
    try:
        with path.open("rb") as source:
            return source.read(max(SNIFF_BYTES, max_chars * 8))
    except OSError:
        return b""


def _csv_detection(text: str) -> FormatDetection | None:
    lines = [line for line in text.splitlines()[:12] if line.strip()]
    if len(lines) < 2:
        return None
    sample = "\n".join(lines)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        rows = [row for row in csv.reader(io.StringIO(sample), dialect) if row]
    except csv.Error:
        return None
    if len(rows) < 2:
        return None
    widths = {len(row) for row in rows}
    if len(widths) != 1 or next(iter(widths)) < 2:
        return None
    header = rows[0]
    header_words = sum(bool(re.search(r"[A-Za-z]", cell)) for cell in header)
    if header_words < max(2, len(header) // 2):
        return None
    return FormatDetection(".csv", "high", ("consistent delimited rows", "textual header"))


def detect_text_format(text: str, allow_generic_text: bool = False) -> FormatDetection:
    """Infer a supported text format from bounded, decoded content."""
    value = (text or "").lstrip()
    if not value:
        return FormatDetection()

    if len(PGN_TAG.findall(value)) >= 2 and PGN_MOVES.search(value):
        return FormatDetection(".pgn", "high", ("PGN game tags and chess moves",))
    if (PATCH_HEADER.search(value) or PATCH_PAIR.search(value)) and PATCH_HUNK.search(value):
        return FormatDetection(".patch", "high", ("unified diff header and hunk",))
    if SHEBANG_MARKER.search(value):
        return FormatDetection(".sh", "high", ("shell shebang",))
    if PRIVATE_KEY_MARKER.search(value):
        return FormatDetection(".pem", "high", ("PEM private-key header",))
    if HTML_MARKER.search(value):
        return FormatDetection(".html", "high", ("HTML document markup",))
    if re.match(r"(?is)^<svg(?:\s|>)", value):
        return FormatDetection(".svg", "high", ("SVG document markup",))
    if re.match(r"(?is)^<\?xml\b", value):
        return FormatDetection(".xml", "high", ("XML declaration",))

    if value[:1] in "[{":
        try:
            json.loads(text)
            return FormatDetection(".json", "high", ("valid JSON document",))
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
            pass

    if value.startswith("<"):
        try:
            ElementTree.fromstring(text)
            return FormatDetection(".xml", "high", ("valid XML document",))
        except (ElementTree.ParseError, RecursionError, LookupError, ValueError):
            pass

    if TERRAFORM_BLOCK.search(value):
        return FormatDetection(".tf", "high", ("Terraform/HCL block syntax",))

    env_assignments = list(ENV_ASSIGNMENT.finditer(value))
    has_ini_section = bool(INI_SECTION.search(value))
    has_toml_table = bool(TOML_TABLE.search(value))
    has_typed_toml = bool(TOML_TYPED_VALUE.search(value))
    env_keys = [match.group("key") for match in env_assignments]
    env_key_style = bool(env_keys) and all(
        re.fullmatch(
            r"(?:[A-Z][A-Z0-9_]*|"
            r"[A-Za-z][A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|URL|HOST|PORT|"
            r"ENV|MODE|DEBUG|ROOT|PATH|USER|NAME|ID))",
            key,
        )
        for key in env_keys
    )
    if has_toml_table and (
        "[[" in value
        or re.search(r"(?m)^[ \t]*\[[A-Za-z0-9_-]+\.[A-Za-z0-9_.-]+\]", value)
    ):
        return FormatDetection(".toml", "high", ("TOML table and typed values",))
    if has_ini_section and env_assignments:
        return FormatDetection(".ini", "high", ("INI section and key assignments",))
    if len(env_assignments) >= 2 and (
        any(match.group("export") for match in env_assignments) or env_key_style
    ):
        return FormatDetection(".env", "high", ("environment-variable assignments",))
    if has_toml_table and not env_assignments:
        return FormatDetection(".toml", "medium", ("TOML-style table header",))
    if has_typed_toml and len(env_assignments) >= 2:
        return FormatDetection(".toml", "medium", ("typed key/value configuration",))

    sql_signals = sum(bool(pattern.search(value)) for pattern in SQL_MARKERS)
    if sql_signals:
        return FormatDetection(
            ".sql",
            "high" if sql_signals >= 2 or re.search(r"(?i);\s*(?:select|drop|insert|update|delete)\b", value) else "medium",
            (f"{sql_signals} SQL syntax signals",),
        )

    cpp_signals = sum(weight for pattern, weight in CPP_MARKERS if pattern.search(value))
    c_signals = sum(weight for pattern, weight in C_MARKERS if pattern.search(value))
    if cpp_signals >= 3 and re.search(r"#include|std::|using\s+namespace\s+std", value, re.I):
        return FormatDetection(".cpp", "high", (f"{cpp_signals} C/C++ syntax signals",))
    if c_signals >= 4:
        return FormatDetection(".c", "high", (f"{c_signals} C syntax signals",))

    csv_result = _csv_detection(text)
    if csv_result is not None:
        return csv_result
    if allow_generic_text and len(value.strip()) >= 20:
        return FormatDetection(".txt", "high", ("readable bounded text",))
    return FormatDetection()


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


def _fallback_content(
    path: Path,
    max_chars: int,
    expected_type: str,
    parser_name: str,
) -> ContentExtraction:
    text = _read_text(path, max_chars)
    if not text.strip():
        return ContentExtraction(
            parser_status="unavailable",
            warning=(
                f"{parser_name} parsing failed and the bounded sample was not readable text."
            ),
        )
    detection = detect_text_format(text, allow_generic_text=True)
    return ContentExtraction(
        text=_clean(text, max_chars),
        parser_status="fallback",
        detected_type=detection.extension,
        detection_confidence=detection.confidence,
        warning=(
            f"File extension {expected_type} disagreed with its {parser_name} parser; "
            "readable text was inspected with bounded fallback extraction."
        ),
    )


def _parsed_content(
    text: str,
    max_chars: int,
    detected_type: str,
    signals: tuple[str, ...],
) -> ContentExtraction:
    return ContentExtraction(
        text=_clean(text, max_chars),
        parser_status="parsed",
        detected_type=detected_type,
        detection_confidence="high",
        signals=signals,
    )


def extract_content(path: str, max_chars: int = 600) -> ContentExtraction:
    """Extract bounded content and retain parser/fallback evidence.

    Format-specific parsing is attempted only for the declared structured format.
    A readable text fallback is used after a parser/format exception, but arbitrary
    programming errors are allowed to surface.
    """
    p = Path(path)
    ext = p.suffix.lower()
    name = p.name.lower()
    try:
        sample, sample_text = _read_sample(p)
        if not sample:
            return ContentExtraction()

        if ext in {".json", ".ipynb"}:
            raw = _read_text(p, max_chars)
            try:
                json.loads(raw)
            except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
                return _fallback_content(p, max_chars, ext, "JSON")
            if ext == ".ipynb":
                return _parsed_content(raw, max_chars, ".ipynb", ("valid notebook JSON",))
            return _parsed_content(raw, max_chars, ".json", ("valid JSON document",))

        if ext == ".xml":
            data = _read_bytes(p, max_chars)
            try:
                root = ElementTree.fromstring(data)
            except (ElementTree.ParseError, RecursionError, LookupError, ValueError):
                return _fallback_content(p, max_chars, ext, "XML")
            text = " ".join(part.strip() for part in root.itertext() if part.strip())
            return _parsed_content(text or _decode_text(data) or "", max_chars, ".xml", ("valid XML document",))

        if ext == ".csv":
            raw = _read_text(p, max_chars)
            detection = _csv_detection(raw)
            if detection is None:
                return ContentExtraction(
                    text=_clean(raw, max_chars),
                    parser_status="fallback",
                    detected_type="unknown",
                    detection_confidence="unknown",
                    warning="CSV structure was uncertain; bounded text was retained.",
                )
            return _parsed_content(raw, max_chars, ".csv", detection.signals)

        if ext in TEXT_EXTS:
            raw = _read_text(p, max_chars)
            if not raw.strip():
                return ContentExtraction()
            detection = detect_text_format(raw)
            return ContentExtraction(
                text=_clean(raw, max_chars),
                parser_status="text",
                detected_type=detection.extension,
                detection_confidence=detection.confidence,
                warning="",
            )

        if ext == ".pdf":
            from pypdf.errors import DependencyError, PdfReadError

            try:
                return _parsed_content(_read_pdf(p, max_chars), max_chars, ".pdf", ("PDF signature",))
            except (OSError, ValueError, DependencyError, PdfReadError):
                return _fallback_content(p, max_chars, ext, "PDF")

        if ext == ".docx":
            from docx.opc.exceptions import PackageNotFoundError

            try:
                return _parsed_content(_read_docx(p, max_chars), max_chars, ".docx", ("DOCX package",))
            except (OSError, KeyError, ValueError, zipfile.BadZipFile, PackageNotFoundError):
                return _fallback_content(p, max_chars, ext, "DOCX")

        if ext == ".pptx":
            from pptx.exc import PackageNotFoundError

            try:
                return _parsed_content(_read_pptx(p, max_chars), max_chars, ".pptx", ("PPTX package",))
            except (OSError, KeyError, ValueError, zipfile.BadZipFile, PackageNotFoundError):
                return _fallback_content(p, max_chars, ext, "PPTX")

        if ext in {".xlsx", ".xlsm"}:
            from openpyxl.utils.exceptions import InvalidFileException

            try:
                return _parsed_content(_read_spreadsheet(p, max_chars), max_chars, ".xlsx", ("XLSX package",))
            except (OSError, KeyError, ValueError, zipfile.BadZipFile, InvalidFileException):
                return _fallback_content(p, max_chars, ext, "XLSX")

        if ext == ".epub":
            try:
                return _parsed_content(_read_epub(p, max_chars), max_chars, ".epub", ("EPUB package",))
            except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                return _fallback_content(p, max_chars, ext, "EPUB")

        if ext == ".zip":
            try:
                return _parsed_content(_read_zip_members(p), max_chars, ".zip", ("ZIP archive",))
            except (OSError, ValueError, zipfile.BadZipFile):
                return _fallback_content(p, max_chars, ext, "ZIP")

        if ext in {".tar", ".tgz"} or name.endswith(".tar.gz"):
            try:
                return _parsed_content(_read_tar_members(p), max_chars, ".tar", ("TAR archive",))
            except (OSError, ValueError, tarfile.TarError):
                return _fallback_content(p, max_chars, ext, "TAR")

        if ext == ".gz":
            try:
                return _parsed_content(_read_gzip(p, max_chars), max_chars, ".gz", ("GZIP stream",))
            except (OSError, EOFError, ValueError, gzip.BadGzipFile):
                return _fallback_content(p, max_chars, ext, "GZIP")

        if ext == ".rar":
            try:
                import rarfile

                return _parsed_content(_read_rar_members(p), max_chars, ".rar", ("RAR archive",))
            except (OSError, ValueError, rarfile.Error):
                return _fallback_content(p, max_chars, ext, "RAR")

        if ext in {".jpg", ".jpeg", ".png", ".heic", ".webp", ".tiff", ".tif", ".bmp", ".gif"}:
            try:
                return ContentExtraction(
                    text=_clean(_read_image_metadata(p), max_chars),
                    parser_status="parsed",
                    detected_type=ext,
                    detection_confidence="high",
                )
            except (OSError, ValueError):
                return _fallback_content(p, max_chars, ext, "image")

        if sample_text is not None and len(sample_text.strip()) >= 20:
            raw = _read_text(p, max_chars)
            detection = detect_text_format(raw, allow_generic_text=True)
            return ContentExtraction(
                text=_clean(raw, max_chars),
                parser_status="sniffed_text",
                detected_type=detection.extension,
                detection_confidence=detection.confidence,
                warning="Unknown extension contained readable text; inspected with bounded sniffing.",
            )
        return ContentExtraction()
    except OSError:
        return ContentExtraction()


def extract_snippet(path: str, max_chars: int = 600) -> str:
    """Best-effort bounded snippet with structured-parser fallback."""
    return extract_content(path, max_chars=max_chars).text


def attach_snippets(files: List[FileInfo], max_chars: int = 600) -> List[FileInfo]:
    for f in files:
        f.snippet = extract_snippet(f.path, max_chars)
    return files
