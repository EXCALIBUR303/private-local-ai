"""
Local vector store.

Embeddings live as float32 BLOBs in the same SQLite file as everything else,
and search is exact (brute-force cosine over a NumPy matrix) rather than
approximate. For a personal corpus this is both faster to build and more
accurate than an ANN index, and it adds no native dependency.

Retrieval is hybrid: dense cosine similarity fused with a BM25 lexical score
via Reciprocal Rank Fusion. Lexical matching is what rescues exact names,
identifiers and numbers that embeddings tend to smear together.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from . import config, db

_TOKEN = re.compile(r"[a-z0-9_]+")

# Hybrid-fusion tuning. Dense retrieval understands paraphrase and carries the
# ranking; BM25 is a safety net for exact tokens (identifiers, SKUs, names).
DENSE_WEIGHT = 1.0
LEXICAL_WEIGHT = 0.5
RRF_K = 20.0
#: Ignore lexical candidates scoring below this fraction of the best hit.
LEXICAL_FLOOR = 0.35
#: A term appearing in more than this share of the corpus carries little
#: signal. Expressed as a ratio, not a raw IDF: on a 2-chunk index the highest
#: attainable IDF is 0.69, so an absolute IDF cutoff silently disables the
#: lexical channel exactly when the corpus is small.
MAX_DF_RATIO = 0.5
#: ...and only once the corpus is big enough for document frequency to mean
#: anything. In a one-chunk index every term occurs in 100% of documents.
MIN_DOCS_FOR_DF_GUARD = 5
_lock = threading.Lock()

# Cached dense matrix + lexical index, rebuilt whenever the corpus changes.
_cache: dict[str, Any] = {"stamp": None, "ids": None, "matrix": None, "lex": None}


#: Function words never carry retrieval signal. An IDF cutoff alone cannot
#: catch these: on a small corpus a stopword appearing in 2 of 12 chunks looks
#: statistically "rare" and would otherwise dominate the lexical score.
STOPWORDS = frozenset("""
a an and any are as at be been being but by can could did do does doing done
for from had has have having he her hers him his how i if in into is it its
me my no nor not of off on or our ours out over own she should so some such
than that the their theirs them then there these they this those to too us
was we were what when where which while who whom why will with would you your
about after again all also am because before below between during each few
more most other same through under until up very via what's don't isn't
""".split())


def _singularise(word: str) -> str:
    """
    Very light plural folding so "wrenches" matches "wrench".

    Deliberately not a real stemmer: aggressive stemming hurts the exact-token
    rescue this channel exists for (a SKU must stay intact).
    """
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    out = []
    for raw in _TOKEN.findall(text.lower()):
        if len(raw) < 2 or raw in STOPWORDS:
            continue
        token = _singularise(raw)
        if token and token not in STOPWORDS:
            out.append(token)
    return out


# ------------------------------------------------------------------- packing
def pack_vector(vec: Sequence[float]) -> bytes:
    """L2-normalise then store as float32 so cosine reduces to a dot product."""
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm > 0:
        arr = arr / norm
    return arr.astype(np.float32).tobytes()


def unpack_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


# ------------------------------------------------------------------- sources
@dataclass
class SourceRef:
    id: int
    path: str
    filename: str


def upsert_source(
    path: Path,
    origin: str,
    sha256: str,
    embed_model: str,
) -> int:
    path = Path(path)
    now = time.time()
    with db.tx() as c:
        row = c.execute("SELECT id FROM sources WHERE path=?", (str(path),)).fetchone()
        if row:
            sid = int(row["id"])
            c.execute("DELETE FROM chunks WHERE source_id=?", (sid,))
            c.execute(
                "UPDATE sources SET bytes=?, sha256=?, origin=?, embed_model=?,"
                " chunk_count=0, indexed_at=NULL WHERE id=?",
                (path.stat().st_size, sha256, origin, embed_model, sid),
            )
            return sid
        cur = c.execute(
            "INSERT INTO sources (path, filename, ext, bytes, sha256, origin,"
            " chunk_count, embed_model, added_at) VALUES (?,?,?,?,?,?,0,?,?)",
            (
                str(path),
                path.name,
                path.suffix.lower(),
                path.stat().st_size,
                sha256,
                origin,
                embed_model,
                now,
            ),
        )
        return int(cur.lastrowid)


def store_chunks(source_id: int, chunks: list, vectors: list[Sequence[float]]) -> int:
    """Persist chunk text + embeddings. Lengths must match."""
    if len(chunks) != len(vectors):
        raise ValueError("chunk/vector count mismatch")
    now = time.time()
    with db.tx() as c:
        for ch, vec in zip(chunks, vectors):
            blob = pack_vector(vec)
            c.execute(
                "INSERT INTO chunks (source_id, ordinal, text, locator, page,"
                " section, embedding, dim, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    source_id,
                    ch.ordinal,
                    ch.text,
                    ch.locator,
                    ch.page,
                    ch.section,
                    blob,
                    len(blob) // 4,
                    now,
                ),
            )
        c.execute(
            "UPDATE sources SET chunk_count=?, indexed_at=? WHERE id=?",
            (len(chunks), now, source_id),
        )
    invalidate()
    return len(chunks)


def list_sources() -> list[dict[str, Any]]:
    rows = db.connect().execute(
        "SELECT * FROM sources ORDER BY added_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_source(source_id: int) -> dict[str, Any] | None:
    row = db.connect().execute(
        "SELECT * FROM sources WHERE id=?", (source_id,)
    ).fetchone()
    return dict(row) if row else None


def remove_source(source_id: int) -> bool:
    with db.tx() as c:
        cur = c.execute("DELETE FROM sources WHERE id=?", (source_id,))
        removed = cur.rowcount > 0
    invalidate()
    return removed


def clear_index() -> dict[str, int]:
    with db.tx() as c:
        n_chunks = c.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        n_src = c.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
        c.execute("DELETE FROM chunks")
        c.execute("DELETE FROM sources")
    invalidate()
    return {"sources": n_src, "chunks": n_chunks}


def stats() -> dict[str, Any]:
    conn = db.connect()
    row = conn.execute(
        "SELECT COUNT(*) AS chunks, COALESCE(SUM(LENGTH(text)),0) AS chars FROM chunks"
    ).fetchone()
    nsrc = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
    return {"sources": nsrc, "chunks": row["chunks"], "characters": row["chars"]}


# --------------------------------------------------------------------- index
def invalidate() -> None:
    with _lock:
        _cache.update(stamp=None, ids=None, matrix=None, lex=None)


def _corpus_stamp() -> tuple[int, int]:
    row = db.connect().execute(
        "SELECT COUNT(*) AS n, COALESCE(MAX(id),0) AS m FROM chunks"
    ).fetchone()
    return int(row["n"]), int(row["m"])


def _build_index() -> tuple[list[int], np.ndarray | None, dict[str, Any]]:
    rows = db.connect().execute(
        "SELECT id, text, embedding, dim FROM chunks ORDER BY id"
    ).fetchall()
    ids = [int(r["id"]) for r in rows]

    vectors, dims = [], set()
    for r in rows:
        if r["embedding"]:
            v = unpack_vector(r["embedding"])
            vectors.append(v)
            dims.add(v.shape[0])
    matrix = None
    if vectors and len(dims) == 1:
        matrix = np.vstack(vectors).astype(np.float32)

    # BM25 statistics
    docs = [tokenize(r["text"]) for r in rows]
    df: Counter[str] = Counter()
    for toks in docs:
        df.update(set(toks))
    n_docs = max(len(docs), 1)
    avgdl = sum(len(d) for d in docs) / n_docs if docs else 0.0
    idf = {
        t: math.log(1 + (n_docs - c + 0.5) / (c + 0.5)) for t, c in df.items()
    }
    tfs = [Counter(d) for d in docs]
    lex = {
        "tfs": tfs,
        "lens": [len(d) for d in docs],
        "idf": idf,
        "df": dict(df),
        "avgdl": avgdl,
    }
    return ids, matrix, lex


def _ensure_index() -> tuple[list[int], np.ndarray | None, dict[str, Any]]:
    stamp = _corpus_stamp()
    with _lock:
        if _cache["stamp"] == stamp and _cache["ids"] is not None:
            return _cache["ids"], _cache["matrix"], _cache["lex"]
    ids, matrix, lex = _build_index()
    with _lock:
        _cache.update(stamp=stamp, ids=ids, matrix=matrix, lex=lex)
    return ids, matrix, lex


def _bm25_scores(query: str, lex: dict[str, Any], k1: float = 1.5, b: float = 0.75):
    """
    BM25 over *discriminative* query terms only.

    The lexical channel exists to rescue exact rare tokens — a SKU, an invoice
    number, a surname — that embeddings blur together. Common words are
    actively harmful here: a query like "how many torque wrenches are in stock"
    would otherwise reward every chunk containing "stock", outranking the one
    chunk the dense retriever correctly identified. Function words are removed
    by the stopword list during tokenisation, and any term occurring in more
    than MAX_DF_RATIO of the corpus is skipped here; the rest is left to the
    dense side.
    """
    q = tokenize(query)
    n = len(lex["lens"])
    scores = np.zeros(n, dtype=np.float32)
    if not q or n == 0:
        return scores
    avgdl = lex["avgdl"] or 1.0
    n_docs = max(n, 1)
    for term in set(q):
        idf = lex["idf"].get(term)
        if not idf:
            continue
        if (
            n_docs >= MIN_DOCS_FOR_DF_GUARD
            and lex["df"].get(term, 0) > MAX_DF_RATIO * n_docs
        ):
            continue
        for i, tf in enumerate(lex["tfs"]):
            f = tf.get(term)
            if not f:
                continue
            dl = lex["lens"][i] or 1
            scores[i] += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
    return scores


# -------------------------------------------------------------------- search
@dataclass
class Hit:
    chunk_id: int
    source_id: int
    filename: str
    path: str
    text: str
    locator: str
    page: int | None
    section: str | None
    score: float
    dense_rank: int | None = None
    lexical_rank: int | None = None


def search(
    query_text: str,
    query_vector: Sequence[float] | None,
    k: int | None = None,
    pool: int | None = None,
) -> list[Hit]:
    """
    Hybrid retrieval. Returns at most `k` hits, best first.

    Dense and lexical candidate lists are fused with Reciprocal Rank Fusion,
    which needs no score calibration between the two very different scales.
    """
    k = k or config.RETRIEVE_K
    pool = pool or config.RETRIEVE_POOL
    ids, matrix, lex = _ensure_index()
    if not ids:
        return []

    dense_rank: dict[int, int] = {}
    if query_vector is not None and matrix is not None and len(matrix) == len(ids):
        q = np.asarray(query_vector, dtype=np.float32)
        norm = float(np.linalg.norm(q))
        if norm > 0:
            q = q / norm
            sims = matrix @ q
            order = np.argsort(-sims)[:pool]
            for rank, idx in enumerate(order):
                dense_rank[ids[int(idx)]] = rank

    lexical_rank: dict[int, int] = {}
    lscores = _bm25_scores(query_text, lex)
    if lscores.size:
        best = float(lscores.max())
        if best > 0:
            # Only keep lexically *meaningful* candidates. On a small corpus
            # almost every chunk scores something, and admitting that noise
            # lets a weak keyword overlap outrank a strong semantic match.
            floor = best * LEXICAL_FLOOR
            order = np.argsort(-lscores)[:pool]
            for rank, idx in enumerate(order):
                if lscores[int(idx)] >= floor:
                    lexical_rank[ids[int(idx)]] = rank

    if not dense_rank and not lexical_rank:
        return []

    # Weighted Reciprocal Rank Fusion. Dense carries more weight because it
    # understands paraphrase; lexical exists to rescue exact identifiers, so
    # it contributes but cannot dominate. A small K sharpens the top ranks.
    fused: dict[int, float] = {}
    for cid, r in dense_rank.items():
        fused[cid] = fused.get(cid, 0.0) + DENSE_WEIGHT / (RRF_K + r)
    for cid, r in lexical_rank.items():
        fused[cid] = fused.get(cid, 0.0) + LEXICAL_WEIGHT / (RRF_K + r)

    top = sorted(fused.items(), key=lambda kv: -kv[1])[:k]
    if not top:
        return []

    placeholders = ",".join("?" for _ in top)
    rows = db.connect().execute(
        f"SELECT c.id, c.source_id, c.text, c.locator, c.page, c.section,"
        f" s.filename, s.path FROM chunks c JOIN sources s ON s.id=c.source_id"
        f" WHERE c.id IN ({placeholders})",
        [cid for cid, _ in top],
    ).fetchall()
    by_id = {int(r["id"]): r for r in rows}

    hits: list[Hit] = []
    for cid, score in top:
        r = by_id.get(cid)
        if r is None:
            continue
        hits.append(
            Hit(
                chunk_id=cid,
                source_id=int(r["source_id"]),
                filename=r["filename"],
                path=r["path"],
                text=r["text"],
                locator=r["locator"] or "",
                page=r["page"],
                section=r["section"],
                score=float(score),
                dense_rank=dense_rank.get(cid),
                lexical_rank=lexical_rank.get(cid),
            )
        )
    return hits
