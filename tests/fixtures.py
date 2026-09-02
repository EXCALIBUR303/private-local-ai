"""
Builders for real test documents.

These write genuine PDF and DOCX files using only the standard library, so
the test suite exercises the actual parsers without pulling in reportlab or
python-docx.
"""
from __future__ import annotations

import zipfile
from pathlib import Path


# --------------------------------------------------------------------- PDF
def _esc(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_pdf(path: Path, pages: list[list[str]]) -> Path:
    """
    Write a minimal but valid multi-page PDF. `pages` is a list of pages,
    each a list of text lines. pypdf extracts these back out verbatim.
    """
    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)          # 1-based object number

    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    page_ids: list[int] = []
    content_ids: list[int] = []
    for lines in pages:
        body = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
        for line in lines:
            body.append(f"({_esc(line)}) Tj")
            body.append("T*")
        body.append("ET")
        stream = "\n".join(body).encode("latin-1", "replace")
        cid = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        content_ids.append(cid)
        page_ids.append(0)           # placeholder, filled below

    pages_id = len(objects) + len(pages) + 1

    real_page_ids: list[int] = []
    for cid in content_ids:
        pid = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, font_id, cid)
        )
        real_page_ids.append(pid)

    kids = b" ".join(b"%d 0 R" % p for p in real_page_ids)
    actual_pages_id = add(
        b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(real_page_ids), kids)
    )
    assert actual_pages_id == pages_id, "page-tree id calculation drifted"
    catalog_id = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += b"%010d 00000 n \n" % off
    out += (
        b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, catalog_id, xref_at)
    )

    path.write_bytes(bytes(out))
    return path


# -------------------------------------------------------------------- DOCX
_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(path: Path, blocks: list[tuple[str, str]]) -> Path:
    """
    `blocks` is a list of (style, text). Use style "Heading1"/"Heading2" for
    headings and "" (or "Normal") for body paragraphs.
    """
    def xml_escape(s: str) -> str:
        return (
            s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    paras = []
    for style, text in blocks:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        paras.append(
            f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{xml_escape(text)}</w:t></w:r></w:p>'
        )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}"><w:body>{"".join(paras)}</w:body></w:document>'
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("word/document.xml", document)
    return path
