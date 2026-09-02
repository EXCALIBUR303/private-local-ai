"""
Document ingestion: extraction, chunking and citation locators.

Only files the user explicitly picks are ever read. There is no directory
walk that starts anywhere but a folder the user chose in the UI, and the
walker refuses well-known private locations outright.

DOCX is parsed with the standard library (zipfile + ElementTree) rather than
python-docx so the app carries no lxml/compiler dependency.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import config


class UnsupportedFile(ValueError):
    pass


class RefusedPath(PermissionError):
    """Raised for locations this app declines to index on privacy grounds."""


# Folders we refuse to index wholesale even if the user points us at them.
# These are the "never scan my whole Mac" guarantees, enforced in code.
_REFUSED_EXACT = {
    Path.home(),
    Path.home() / "Desktop",
    Path.home() / "Documents",
    Path.home() / "Downloads",
    Path.home() / "Library",
    Path.home() / "Movies",
    Path.home() / "Music",
    Path.home() / "Pictures",
    Path("/"),
    Path("/Users"),
    Path("/System"),
    Path("/Applications"),
    Path("/Library"),
}

# Any path containing one of these components is refused at any depth:
# cloud-sync roots, mail stores, browser profiles, credential stores.
_REFUSED_COMPONENTS = {
    "Library",
    "Mail",
    ".ssh",
    ".gnupg",
    ".aws",
    ".config",
    "Keychains",
    "iCloud Drive (Archive)",
    "Mobile Documents",
    "com~apple~CloudDocs",
    "Dropbox",
    "Google Drive",
    "GoogleDrive",
    "OneDrive",
    "CloudStorage",
    "Photos Library.photoslibrary",
    ".Trash",
}

_SKIP_DIR_NAMES = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".next",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".DS_Store",
}


@dataclass
class Segment:
    """A contiguous piece of a document with a human-meaningful location."""
    text: str
    page: int | None = None
    section: str | None = None

    @property
    def locator(self) -> str:
        bits = []
        if self.page is not None:
            bits.append(f"page {self.page}")
        if self.section:
            bits.append(self.section)
        return " · ".join(bits)


@dataclass
class Chunk:
    text: str
    ordinal: int
    page: int | None = None
    section: str | None = None
    locator: str = ""
    meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------- path safety
def check_path_allowed(path: Path) -> None:
    """Raise RefusedPath for locations this app will not index."""
    p = path.expanduser().resolve()
    if p in _REFUSED_EXACT:
        raise RefusedPath(
            f"Refusing to index {p} — that is a whole-account or system location. "
            "Choose a specific project folder instead."
        )
    for part in p.parts:
        if part in _REFUSED_COMPONENTS:
            raise RefusedPath(
                f"Refusing to index {p} — it lies inside '{part}', which this app "
                "treats as private (cloud sync, mail, browser or credential data)."
            )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def discover_files(folder: Path, max_files: int = 2000) -> list[Path]:
    """
    List supported files under a folder the user explicitly chose.
    Hidden directories and dependency folders are skipped.
    """
    folder = folder.expanduser().resolve()
    check_path_allowed(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"{folder} is not a folder")

    found: list[Path] = []
    for p in sorted(folder.rglob("*")):
        if len(found) >= max_files:
            break
        if p.is_dir():
            continue
        if any(part in _SKIP_DIR_NAMES or part.startswith(".") for part in p.parts[len(folder.parts):-1]):
            continue
        if p.name.startswith("."):
            continue
        if p.suffix.lower() not in config.SUPPORTED_EXTENSIONS:
            continue
        try:
            if p.stat().st_size > config.MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        found.append(p)
    return found


# ----------------------------------------------------------------- extractors
def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", " ").replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_markdown(raw: str) -> list[Segment]:
    """Split on ATX headings, carrying the heading down as the section name."""
    segments: list[Segment] = []
    current_heading: str | None = None
    buf: list[str] = []

    def flush() -> None:
        body = _clean("\n".join(buf))
        if body:
            segments.append(Segment(text=body, section=current_heading))
        buf.clear()

    in_fence = False
    for line in raw.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        m = re.match(r"^(#{1,6})\s+(.*)$", line) if not in_fence else None
        if m:
            flush()
            current_heading = m.group(2).strip()[:120]
            buf.append(line)
        else:
            buf.append(line)
    flush()
    if not segments:
        body = _clean(raw)
        if body:
            segments.append(Segment(text=body))
    return segments


def extract_text_file(raw: str) -> list[Segment]:
    body = _clean(raw)
    return [Segment(text=body)] if body else []


def extract_pdf(path: Path) -> list[Segment]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise UnsupportedFile(f"{path.name} is password-protected") from exc

    segments: list[Segment] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - a bad page must not kill the file
            text = ""
        text = _clean(text)
        if text:
            segments.append(Segment(text=text, page=i))
    if not segments:
        raise UnsupportedFile(
            f"No selectable text in {path.name} (it may be a scanned image PDF)."
        )
    return segments


_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract_docx(path: Path) -> list[Segment]:
    """Parse word/document.xml with the standard library only."""
    try:
        with zipfile.ZipFile(path) as zf:
            with zf.open("word/document.xml") as fh:
                tree = ET.parse(fh)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise UnsupportedFile(f"{path.name} is not a readable .docx file") from exc

    root = tree.getroot()
    body = root.find(f"{_W_NS}body")
    if body is None:
        raise UnsupportedFile(f"{path.name} has no document body")

    segments: list[Segment] = []
    heading: str | None = None
    buf: list[str] = []

    def flush() -> None:
        joined = _clean("\n".join(buf))
        if joined:
            segments.append(Segment(text=joined, section=heading))
        buf.clear()

    def para_text(p: ET.Element) -> str:
        parts: list[str] = []
        for node in p.iter():
            if node.tag == f"{_W_NS}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_W_NS}tab":
                parts.append("\t")
            elif node.tag in (f"{_W_NS}br", f"{_W_NS}cr"):
                parts.append("\n")
        return "".join(parts)

    def para_style(p: ET.Element) -> str:
        ppr = p.find(f"{_W_NS}pPr")
        if ppr is None:
            return ""
        st = ppr.find(f"{_W_NS}pStyle")
        return (st.get(f"{_W_NS}val") or "") if st is not None else ""

    for elem in body:
        if elem.tag == f"{_W_NS}p":
            style = para_style(elem)
            text = para_text(elem).strip()
            if not text:
                continue
            if style.lower().startswith("heading") or style.lower() == "title":
                flush()
                heading = text[:120]
                buf.append(text)
            else:
                buf.append(text)
        elif elem.tag == f"{_W_NS}tbl":
            rows: list[str] = []
            for tr in elem.findall(f"{_W_NS}tr"):
                cells = [
                    " ".join(para_text(p).split())
                    for tc in tr.findall(f"{_W_NS}tc")
                    for p in tc.findall(f"{_W_NS}p")
                ]
                cells = [c for c in cells if c]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                buf.append("\n".join(rows))
    flush()
    if not segments:
        raise UnsupportedFile(f"No text found in {path.name}")
    return segments


def extract_csv(path: Path, rows_per_segment: int = 25) -> list[Segment]:
    """Group rows into segments, citing the row range."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    sample = raw[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(raw), dialect)
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise UnsupportedFile(f"{path.name} could not be parsed as CSV: {exc}") from exc
    if not rows:
        raise UnsupportedFile(f"{path.name} is empty")

    header = rows[0]
    data = rows[1:] or rows
    header_line = " | ".join(h.strip() for h in header)

    segments: list[Segment] = []
    for start in range(0, len(data), rows_per_segment):
        block = data[start : start + rows_per_segment]
        lines = [f"Columns: {header_line}"]
        for offset, row in enumerate(block):
            n = start + offset + 2  # 1-based, +1 for the header row
            cells = " | ".join(c.strip() for c in row)
            if cells.strip():
                lines.append(f"row {n}: {cells}")
        first = start + 2
        last = start + len(block) + 1
        segments.append(
            Segment(text=_clean("\n".join(lines)), section=f"rows {first}–{last}")
        )
    return segments


def extract(path: Path) -> list[Segment]:
    """Dispatch on file extension. Raises UnsupportedFile for anything else."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in config.SUPPORTED_EXTENSIONS:
        raise UnsupportedFile(f"Unsupported file type: {ext or path.name}")
    if path.stat().st_size > config.MAX_FILE_BYTES:
        raise UnsupportedFile(f"{path.name} is larger than the 64 MB limit")

    if ext in (".md", ".markdown"):
        return extract_markdown(path.read_text(encoding="utf-8", errors="replace"))
    if ext in (".txt", ".text"):
        return extract_text_file(path.read_text(encoding="utf-8", errors="replace"))
    if ext == ".pdf":
        return extract_pdf(path)
    if ext == ".docx":
        return extract_docx(path)
    if ext == ".csv":
        return extract_csv(path)
    raise UnsupportedFile(f"Unsupported file type: {ext}")


# ------------------------------------------------------------------- chunking
def _split_long(text: str, size: int, overlap: int) -> list[str]:
    """Window a long string, preferring paragraph then sentence boundaries."""
    if len(text) <= size:
        return [text]
    out: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            window = text[start:end]
            cut = window.rfind("\n\n")
            if cut < size * 0.4:
                m = list(re.finditer(r"(?<=[.!?])\s", window))
                cut = m[-1].end() if m and m[-1].end() > size * 0.4 else -1
            if cut > 0:
                end = start + cut
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return out


def chunk_segments(
    segments: Iterable[Segment],
    size: int | None = None,
    overlap: int | None = None,
) -> list[Chunk]:
    """
    Turn segments into embedding-sized chunks, preserving the locator so a
    citation can always name a page or section.
    """
    size = size or config.CHUNK_CHARS
    overlap = overlap if overlap is not None else config.CHUNK_OVERLAP

    chunks: list[Chunk] = []
    pending: list[Segment] = []
    pending_len = 0

    def flush_pending() -> None:
        nonlocal pending_len
        if not pending:
            return
        text = "\n\n".join(s.text for s in pending).strip()
        if text:
            head = pending[0]
            chunks.append(
                Chunk(
                    text=text,
                    ordinal=len(chunks),
                    page=head.page,
                    section=head.section,
                    locator=head.locator,
                )
            )
        pending.clear()
        pending_len = 0

    for seg in segments:
        if not seg.text.strip():
            continue
        if len(seg.text) > size:
            flush_pending()
            for piece in _split_long(seg.text, size, overlap):
                chunks.append(
                    Chunk(
                        text=piece,
                        ordinal=len(chunks),
                        page=seg.page,
                        section=seg.section,
                        locator=seg.locator,
                    )
                )
            continue
        same_place = (
            pending
            and pending[0].page == seg.page
            and pending[0].section == seg.section
        )
        if pending and (pending_len + len(seg.text) > size or not same_place):
            flush_pending()
        pending.append(seg)
        pending_len += len(seg.text) + 2
    flush_pending()
    return chunks


def load_and_chunk(path: Path) -> list[Chunk]:
    return chunk_segments(extract(Path(path)))
