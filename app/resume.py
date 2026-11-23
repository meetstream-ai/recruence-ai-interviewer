"""Resume text extraction from uploaded files (PDF, DOCX, plain text).

The file is untrusted: it is size-capped, parsed with pure-Python libraries, and only
its text is kept (the file itself is not stored). Scanned PDFs have no text layer, so
they are reported instead of silently producing an empty resume.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from pathlib import PurePath

logger = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 30
MAX_CHARS = 60_000  # the setup form's resume limit
MAX_DOCX_UNPACKED = 40 * 1024 * 1024  # a .docx is a zip: refuse one that would unpack to more (a "zip bomb")


class ResumeError(ValueError):
    pass


@dataclass(frozen=True)
class ExtractedResume:
    text: str
    kind: str  # "pdf" | "docx" | "text"
    truncated: bool


def _kind(filename: str, data: bytes) -> str:
    ext = PurePath(filename or "").suffix.lower()
    if data.startswith(b"%PDF") or ext == ".pdf":
        return "pdf"
    if ext == ".docx" or (data.startswith(b"PK") and ext not in (".txt", ".md")):
        return "docx"
    if ext in (".txt", ".md", ""):
        return "text"
    raise ResumeError(f"Unsupported file type {ext!r}: upload a PDF, Word (.docx) or text file")


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ResumeError("This PDF is password-protected; upload an unprotected copy or paste the text")
    return "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES])


def _docx_text(data: bytes) -> str:
    import zipfile

    import docx
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            unpacked = sum(i.file_size for i in z.infolist())
    except zipfile.BadZipFile as e:
        raise ResumeError("Couldn't read this DOCX file; paste the resume text instead") from e
    if unpacked > MAX_DOCX_UNPACKED:  # sizes come from the zip directory; checked before anything is unpacked
        raise ResumeError("This Word file is unusually large inside; upload a PDF or paste the text")

    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:  # many resumes lay out experience in tables
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells if cell.text.strip()))
    return "\n".join(parts)


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def extract_resume(filename: str, data: bytes) -> ExtractedResume:
    if not data:
        raise ResumeError("The file is empty")
    if len(data) > MAX_BYTES:
        raise ResumeError(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    kind = _kind(filename, data)
    try:
        if kind == "pdf":
            raw = _pdf_text(data)
        elif kind == "docx":
            raw = _docx_text(data)
        else:
            raw = data.decode("utf-8", errors="replace")
    except ResumeError:
        raise
    except Exception as e:  # malformed or hostile file: report, don't crash
        logger.info("could not read uploaded %s resume: %s", kind, e)
        raise ResumeError(f"Couldn't read this {kind.upper()} file; paste the resume text instead") from e
    text = _tidy(raw)
    if not text:
        hint = " It may be a scanned document without a text layer." if kind == "pdf" else ""
        raise ResumeError(f"No text was found in the file.{hint} Paste the resume text instead")
    return ExtractedResume(text=text[:MAX_CHARS], kind=kind, truncated=len(text) > MAX_CHARS)
