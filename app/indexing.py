"""
Indexing orchestration: read a file, chunk it, embed the chunks locally,
store them. Emits progress events so the UI can show what is happening.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, AsyncIterator

from . import config, ingest, llm, vectorstore

EMBED_BATCH = 24


async def index_file(path: Path, origin: str = "folder") -> dict[str, Any]:
    """Ingest one file. Returns a per-file result record."""
    path = Path(path)
    record: dict[str, Any] = {
        "file": path.name,
        "path": str(path),
        "chunks": 0,
        "ok": False,
        "error": None,
    }
    try:
        ingest.check_path_allowed(path)
        chunks = ingest.load_and_chunk(path)
        if not chunks:
            record["error"] = "No extractable text"
            return record

        sha = ingest.sha256_file(path)
        source_id = vectorstore.upsert_source(path, origin, sha, config.EMBED_MODEL)

        vectors: list[list[float]] = []
        for i in range(0, len(chunks), EMBED_BATCH):
            batch = [c.text for c in chunks[i : i + EMBED_BATCH]]
            vectors.extend(await llm.embed_documents(batch))

        vectorstore.store_chunks(source_id, chunks, vectors)
        record.update(ok=True, chunks=len(chunks), source_id=source_id)
        return record
    except (ingest.UnsupportedFile, ingest.RefusedPath) as exc:
        record["error"] = str(exc)
        return record
    except Exception as exc:  # noqa: BLE001 — one bad file must not stop a batch
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record


async def index_paths(paths: list[Path], origin: str = "folder") -> AsyncIterator[dict]:
    """Index a list of files, yielding a progress event per file."""
    total = len(paths)
    started = time.time()
    ok = failed = chunks = 0
    yield {"event": "start", "total": total}
    for i, p in enumerate(paths, start=1):
        rec = await index_file(p, origin=origin)
        if rec["ok"]:
            ok += 1
            chunks += rec["chunks"]
        else:
            failed += 1
        yield {"event": "file", "index": i, "total": total, "result": rec}
    yield {
        "event": "done",
        "indexed": ok,
        "failed": failed,
        "chunks": chunks,
        "seconds": round(time.time() - started, 2),
    }


def stage_upload(filename: str, data: bytes) -> Path:
    """
    Copy an uploaded file into the app's own uploads folder.

    The original name is stripped to a safe basename so a crafted filename
    cannot write outside the uploads directory.
    """
    config.ensure_dirs()
    safe = Path(filename).name.replace("\x00", "").strip() or "upload"
    safe = "".join(ch for ch in safe if ch.isprintable() and ch not in '/\\:')[:150]
    ext = Path(safe).suffix.lower()
    if ext not in config.SUPPORTED_EXTENSIONS:
        raise ingest.UnsupportedFile(f"Unsupported file type: {ext or safe}")
    if len(data) > config.MAX_FILE_BYTES:
        raise ingest.UnsupportedFile(f"{safe} exceeds the 64 MB limit")

    dest = config.UPLOADS_DIR / safe
    counter = 1
    while dest.exists():
        dest = config.UPLOADS_DIR / f"{Path(safe).stem}-{counter}{ext}"
        counter += 1
    dest.write_bytes(data)
    return dest


def forget_source(source_id: int, delete_upload: bool = True) -> dict[str, Any]:
    """Remove a source from the index; delete our copy if we made one."""
    src = vectorstore.get_source(source_id)
    if not src:
        return {"removed": False, "reason": "not found"}
    removed_file = False
    if delete_upload and src["origin"] == "upload":
        p = Path(src["path"])
        try:
            if p.is_file() and p.parent == config.UPLOADS_DIR:
                p.unlink()
                removed_file = True
        except OSError:
            pass
    vectorstore.remove_source(source_id)
    return {"removed": True, "filename": src["filename"], "deleted_copy": removed_file}


def purge_uploads() -> int:
    """Delete every staged upload copy. Returns the number of files removed."""
    n = 0
    if config.UPLOADS_DIR.exists():
        for p in config.UPLOADS_DIR.iterdir():
            try:
                if p.is_file():
                    p.unlink()
                    n += 1
                elif p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    return n
