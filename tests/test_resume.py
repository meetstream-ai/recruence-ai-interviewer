import io
from dataclasses import replace

import docx
import pytest
from fastapi.testclient import TestClient

from app.llm import OfflineAI
from app.resume import MAX_BYTES, ResumeError, extract_resume
from app.server import create_app
from app.store import Store


def make_pdf(lines: list[str]) -> bytes:
    """A minimal valid one-page PDF with the given text lines (empty list: a page with no text layer)."""
    ops = "BT /F1 12 Tf 72 720 Td " + " ".join(f"({line}) Tj 0 -16 Td" for line in lines) + " ET" if lines else ""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(ops)} >>\nstream\n{ops}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for n, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n{body}\nendobj\n".encode())
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def make_docx() -> bytes:
    d = docx.Document()
    d.add_paragraph("Priya Raman - Senior Backend Engineer")
    d.add_paragraph("Led the ledger migration to PostgreSQL with zero downtime.")
    table = d.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "2021-2026"
    table.rows[0].cells[1].text = "Payments company: on-call lead"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_pdf_text_is_extracted():
    r = extract_resume("cv.pdf", make_pdf(["Priya Raman", "Python and PostgreSQL, 7 years"]))
    assert r.kind == "pdf" and "Priya Raman" in r.text and "PostgreSQL" in r.text and not r.truncated


def test_docx_paragraphs_and_tables_are_extracted():
    r = extract_resume("cv.docx", make_docx())
    assert r.kind == "docx"
    assert "ledger migration" in r.text and "2021-2026 | Payments company: on-call lead" in r.text


def test_text_file_and_tidying():
    r = extract_resume("cv.txt", "Line one   with   spaces\r\n\r\n\r\n\r\nLine two".encode())
    assert r.kind == "text" and r.text == "Line one with spaces\n\nLine two"


@pytest.mark.parametrize(
    "name, data, match",
    [
        ("scan.pdf", make_pdf([]), "scanned"),
        ("broken.pdf", b"%PDF-1.4 this is not really a pdf", "Couldn't read this PDF"),
        ("fake.docx", b"PK\x03\x04 not a real docx", "Couldn't read this DOCX"),
        ("cv.doc", b"\xd0\xcf\x11\xe0 old word", "Unsupported file type"),
        ("cv.txt", b"", "empty"),
        ("cv.txt", b"x" * (MAX_BYTES + 1), "larger than"),
    ],
)
def test_bad_files_get_a_clear_message(name, data, match):
    with pytest.raises(ResumeError, match=match):
        extract_resume(name, data)


def test_long_resumes_are_truncated():
    r = extract_resume("cv.txt", ("word " * 20_000).encode())
    assert r.truncated and len(r.text) == 60_000


def test_upload_endpoint(settings):
    app = create_app(settings=settings, store=Store(settings.database), ai=OfflineAI(), run_loops=False)
    with TestClient(app) as c:
        ok = c.post("/api/resume/extract", files={"file": ("cv.docx", make_docx(), "application/octet-stream")})
        assert ok.status_code == 200 and ok.json()["kind"] == "docx" and "PostgreSQL" in ok.json()["text"]
        bad = c.post("/api/resume/extract", files={"file": ("scan.pdf", make_pdf([]), "application/pdf")})
        assert bad.status_code == 422 and "scanned" in bad.json()["detail"]


def test_upload_needs_sign_in(settings):
    app = create_app(settings=replace(settings, app_access_token="s3cret"), store=Store(settings.database), ai=OfflineAI(), run_loops=False)
    with TestClient(app) as c:
        files = {"file": ("cv.txt", b"hello", "text/plain")}
        assert c.post("/api/resume/extract", files=files).status_code == 401
        assert c.post("/api/resume/extract", files=files, headers={"Authorization": "Bearer s3cret"}).status_code == 200
