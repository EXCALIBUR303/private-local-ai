"""Document ingestion: extraction, locators, chunking and path refusal."""
from __future__ import annotations

from pathlib import Path

import pytest

from app import config, ingest
from app.ingest import RefusedPath, UnsupportedFile
from tests.fixtures import make_docx, make_pdf


# --------------------------------------------------------------- markdown
def test_markdown_sections_become_locators(tmp_docs: Path):
    p = tmp_docs / "notes.md"
    p.write_text(
        "# Overview\nAtlas is the billing engine.\n\n"
        "## Timeline\nBeta ships in March.\n\n"
        "## Risks\nSupplier concentration in Osaka.\n",
        encoding="utf-8",
    )
    segments = ingest.extract(p)
    sections = [s.section for s in segments]
    assert "Overview" in sections
    assert "Timeline" in sections
    assert "Risks" in sections
    assert any("Osaka" in s.text for s in segments)
    assert all(s.locator for s in segments)


def test_markdown_headings_inside_code_fences_are_not_sections(tmp_docs: Path):
    p = tmp_docs / "code.md"
    p.write_text(
        "# Real Heading\ntext\n\n```\n# not a heading\n```\n\nmore text\n",
        encoding="utf-8",
    )
    sections = {s.section for s in ingest.extract(p)}
    assert sections == {"Real Heading"}


def test_plain_text(tmp_docs: Path):
    p = tmp_docs / "plain.txt"
    p.write_text("The total budget is 42000 dollars.\n", encoding="utf-8")
    segs = ingest.extract(p)
    assert len(segs) == 1
    assert "42000" in segs[0].text
    assert segs[0].page is None


# ------------------------------------------------------------------- pdf
def test_pdf_pages_are_tracked(tmp_docs: Path):
    p = make_pdf(
        tmp_docs / "report.pdf",
        [
            ["Quarterly Report", "Revenue grew by 12 percent."],
            ["Risks", "Supplier concentration in Osaka is the main risk."],
            ["Outlook", "We expect steady growth."],
        ],
    )
    segs = ingest.extract(p)
    assert [s.page for s in segs] == [1, 2, 3]
    osaka = [s for s in segs if "Osaka" in s.text]
    assert len(osaka) == 1
    assert osaka[0].page == 2
    assert osaka[0].locator == "page 2"


def test_pdf_without_text_is_rejected(tmp_docs: Path):
    p = make_pdf(tmp_docs / "blank.pdf", [[]])
    with pytest.raises(UnsupportedFile, match="No selectable text"):
        ingest.extract(p)


# ------------------------------------------------------------------ docx
def test_docx_headings_become_sections(tmp_docs: Path):
    p = make_docx(
        tmp_docs / "spec.docx",
        [
            ("Heading1", "Project Atlas"),
            ("", "Atlas is the internal codename for the billing engine."),
            ("Heading1", "Timeline"),
            ("", "Beta ships in March. GA follows in June."),
        ],
    )
    segs = ingest.extract(p)
    assert {s.section for s in segs} == {"Project Atlas", "Timeline"}
    timeline = [s for s in segs if s.section == "Timeline"][0]
    assert "March" in timeline.text


def test_docx_with_special_characters(tmp_docs: Path):
    p = make_docx(tmp_docs / "amp.docx", [("", "Profit & loss <not a tag> \"quoted\"")])
    segs = ingest.extract(p)
    assert "Profit & loss <not a tag>" in segs[0].text


def test_corrupt_docx_raises_cleanly(tmp_docs: Path):
    p = tmp_docs / "broken.docx"
    p.write_bytes(b"this is not a zip file")
    with pytest.raises(UnsupportedFile):
        ingest.extract(p)


# ------------------------------------------------------------------- csv
def test_csv_rows_are_cited_by_range(tmp_docs: Path):
    p = tmp_docs / "sales.csv"
    rows = ["region,amount,quarter"]
    rows += [f"north,{100+i},Q{(i % 4) + 1}" for i in range(30)]
    p.write_text("\n".join(rows), encoding="utf-8")
    segs = ingest.extract(p)
    assert len(segs) == 2                      # 30 rows, 25 per segment
    assert segs[0].section == "rows 2–26"
    assert segs[1].section == "rows 27–31"
    assert "Columns: region | amount | quarter" in segs[0].text
    assert "row 2:" in segs[0].text


def test_csv_semicolon_delimiter(tmp_docs: Path):
    p = tmp_docs / "eu.csv"
    p.write_text("name;value\nalpha;1\nbeta;2\n", encoding="utf-8")
    segs = ingest.extract(p)
    assert "name | value" in segs[0].text


# -------------------------------------------------------------- chunking
def test_long_text_is_split_with_overlap(tmp_docs: Path):
    paragraph = ("Sentence number %d about supplier risk. " % 0) * 1
    body = "\n\n".join(f"Paragraph {i}. " + ("filler text " * 30) for i in range(12))
    p = tmp_docs / "long.txt"
    p.write_text(paragraph + body, encoding="utf-8")
    chunks = ingest.load_and_chunk(p)
    assert len(chunks) > 1
    assert all(len(c.text) <= config.CHUNK_CHARS + 50 for c in chunks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_chunks_preserve_page_locators(tmp_docs: Path):
    p = make_pdf(
        tmp_docs / "multi.pdf",
        [["Page one text about revenue."], ["Page two text about Osaka risk."]],
    )
    chunks = ingest.load_and_chunk(p)
    pages = {c.page for c in chunks}
    assert pages == {1, 2}
    for c in chunks:
        assert c.locator == f"page {c.page}"


def test_empty_file_produces_no_chunks(tmp_docs: Path):
    p = tmp_docs / "empty.md"
    p.write_text("   \n\n  ", encoding="utf-8")
    assert ingest.load_and_chunk(p) == []


# --------------------------------------------------------- privacy rules
def test_unsupported_extension_rejected(tmp_docs: Path):
    p = tmp_docs / "photo.jpg"
    p.write_bytes(b"\xff\xd8\xff")
    with pytest.raises(UnsupportedFile):
        ingest.extract(p)


@pytest.mark.parametrize(
    "folder",
    [Path.home(), Path.home() / "Desktop", Path.home() / "Documents",
     Path.home() / "Downloads", Path.home() / "Library", Path("/")],
)
def test_private_locations_are_refused(folder: Path):
    with pytest.raises(RefusedPath):
        ingest.check_path_allowed(folder)


@pytest.mark.parametrize(
    "path",
    [
        "~/Library/Mail/inbox.txt",
        "~/Library/Application Support/Firefox/profile/places.txt",
        "~/Dropbox/private/notes.md",
        "~/Library/Mobile Documents/com~apple~CloudDocs/secret.md",
        "~/.ssh/id_rsa.txt",
        "~/Library/CloudStorage/OneDrive/report.pdf",
    ],
)
def test_cloud_mail_and_credential_paths_are_refused(path: str):
    with pytest.raises(RefusedPath):
        ingest.check_path_allowed(Path(path).expanduser())


def test_a_normal_project_folder_is_allowed(tmp_docs: Path):
    ingest.check_path_allowed(tmp_docs)          # must not raise


def test_discover_skips_hidden_and_dependency_dirs(tmp_docs: Path):
    (tmp_docs / "keep.md").write_text("real content", encoding="utf-8")
    (tmp_docs / "node_modules").mkdir()
    (tmp_docs / "node_modules" / "dep.md").write_text("noise", encoding="utf-8")
    (tmp_docs / ".git").mkdir()
    (tmp_docs / ".git" / "config.txt").write_text("noise", encoding="utf-8")
    (tmp_docs / ".hidden.md").write_text("noise", encoding="utf-8")
    (tmp_docs / "image.png").write_bytes(b"x")

    found = {p.name for p in ingest.discover_files(tmp_docs)}
    assert found == {"keep.md"}


def test_sha256_is_stable(tmp_docs: Path):
    p = tmp_docs / "a.txt"
    p.write_text("hello", encoding="utf-8")
    assert ingest.sha256_file(p) == ingest.sha256_file(p)
    assert len(ingest.sha256_file(p)) == 64
