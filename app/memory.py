"""
Self-learning memory.

After an exchange, a small model is asked whether anything durable was learned
about the operator — preferences, facts, working context. What comes back is
de-duplicated against what is already known and stored with an embedding, so
later conversations can recall it.

Three deliberate constraints:

  * Learning is opt-in and can be switched off. An assistant that silently
    accumulates a profile of you is a different product from one that does it
    because you asked.
  * Extraction runs on the *fast* model, in the background, after the reply has
    already been streamed. Learning must never slow down answering.
  * Nothing is learned from document passages. Retrieved text is untrusted; a
    PDF must not be able to write itself into long-term memory.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import numpy as np

from . import config, db, llm, vectorstore

MAX_MEMORIES = 500
DEDUPE_THRESHOLD = 0.88     # cosine similarity above which two facts are "the same"
RECALL_K = 5
RECALL_FLOOR = 0.42         # below this a memory is not relevant enough to inject

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT    NOT NULL,
    kind       TEXT    NOT NULL DEFAULT 'fact',
    source     TEXT,
    embedding  BLOB,
    uses       INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL,
    last_used  REAL
);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC);
"""

EXTRACT_PROMPT = """From the exchange below, extract only durable facts about \
the operator worth remembering in future conversations: their preferences, \
their tools, their projects, decisions they made, constraints they stated.

Rules:
- Ignore anything transient (greetings, one-off questions, small talk).
- Ignore facts about the world; only facts about this operator and their work.
- Write each as one short standalone sentence in the third person.
- Return a JSON array of strings. Return [] if nothing is worth keeping.
- Never return more than 3.

Exchange:
USER: {user}
ASSISTANT: {assistant}

JSON array:"""

_JSON_ARRAY = re.compile(r"\[.*?\]", re.S)


def init() -> None:
    conn = db.connect()
    conn.executescript(SCHEMA)
    conn.commit()


def enabled() -> bool:
    return db.get_setting("learning", "on") == "on"


def set_enabled(on: bool) -> None:
    db.set_setting("learning", "on" if on else "off")


# ─────────────────────────────── storage ───────────────────────────────
def _all_with_vectors() -> tuple[list[dict[str, Any]], np.ndarray | None]:
    rows = db.connect().execute(
        "SELECT id, text, kind, embedding, uses, created_at FROM memories ORDER BY id"
    ).fetchall()
    items = [dict(r) for r in rows]
    vecs = [vectorstore.unpack_vector(r["embedding"]) for r in rows if r["embedding"]]
    if not vecs or len({v.shape[0] for v in vecs}) != 1:
        return items, None
    return items, np.vstack(vecs).astype(np.float32)


def list_memories(limit: int = 200) -> list[dict[str, Any]]:
    rows = db.connect().execute(
        "SELECT id, text, kind, source, uses, created_at, last_used"
        " FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def count() -> int:
    return int(db.connect().execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])


def forget(memory_id: int) -> bool:
    with db.tx() as c:
        return c.execute("DELETE FROM memories WHERE id=?", (memory_id,)).rowcount > 0


def forget_all() -> int:
    with db.tx() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
        c.execute("DELETE FROM memories")
    return int(n)


async def remember(text: str, kind: str = "fact", source: str = "manual") -> dict[str, Any]:
    """Store one fact, unless something close enough is already known."""
    text = " ".join((text or "").split())[:400]
    if not text:
        raise ValueError("Nothing to remember.")

    vector = None
    try:
        vector = await llm.embed_query(text)
    except Exception:  # noqa: BLE001 — memory still works without recall
        vector = None

    if vector is not None:
        items, matrix = _all_with_vectors()
        if matrix is not None and len(matrix) == len(items):
            q = np.asarray(vector, dtype=np.float32)
            norm = float(np.linalg.norm(q))
            if norm:
                sims = matrix @ (q / norm)
                best = int(np.argmax(sims))
                if float(sims[best]) >= DEDUPE_THRESHOLD:
                    return {"stored": False, "duplicate_of": items[best]["id"],
                            "text": items[best]["text"]}

    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO memories (text, kind, source, embedding, created_at)"
            " VALUES (?,?,?,?,?)",
            (text, kind, source,
             vectorstore.pack_vector(vector) if vector else None, time.time()),
        )
        rid = int(cur.lastrowid)
        # keep the store bounded: drop the least-used, oldest entries
        c.execute(
            "DELETE FROM memories WHERE id IN ("
            "  SELECT id FROM memories ORDER BY uses ASC, created_at ASC"
            "  LIMIT MAX(0, (SELECT COUNT(*) FROM memories) - ?))",
            (MAX_MEMORIES,),
        )
    return {"stored": True, "id": rid, "text": text}


# ─────────────────────────────── recall ───────────────────────────────
async def recall(query: str, k: int = RECALL_K) -> list[dict[str, Any]]:
    """Memories relevant to the current message."""
    items, matrix = _all_with_vectors()
    if not items or matrix is None or len(matrix) != len(items):
        return []
    try:
        vector = await llm.embed_query(query)
    except Exception:  # noqa: BLE001
        return []
    q = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(q))
    if not norm:
        return []
    sims = matrix @ (q / norm)
    order = np.argsort(-sims)[:k]

    hits = []
    for idx in order:
        score = float(sims[int(idx)])
        if score < RECALL_FLOOR:
            continue
        item = items[int(idx)]
        hits.append({**item, "score": round(score, 4)})
    if hits:
        now = time.time()
        with db.tx() as c:
            for h in hits:
                c.execute("UPDATE memories SET uses=uses+1, last_used=? WHERE id=?",
                          (now, h["id"]))
    return hits


def as_prompt_block(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return ""
    lines = "\n".join(f"- {h['text']}" for h in hits)
    return (
        "What you already know about this operator (from earlier "
        f"conversations):\n{lines}\n"
        "Use it only when relevant. Do not recite it back."
    )


# ────────────────────────────── learning ──────────────────────────────
async def learn_from_exchange(user_text: str, assistant_text: str) -> list[str]:
    """
    Ask the fast model what is worth keeping. Returns the facts stored.

    Never called with document passages: only the operator's own words and the
    assistant's reply, so an untrusted PDF cannot write itself into memory.
    """
    if not enabled():
        return []
    if len(user_text.strip()) < 12:
        return []

    prompt = EXTRACT_PROMPT.format(user=user_text[:2000], assistant=assistant_text[:2000])
    try:
        message = await llm.chat_once(
            [{"role": "user", "content": prompt}],
            model=config.FAST_MODEL, temperature=0.1, num_ctx=4096,
        )
    except Exception:  # noqa: BLE001 — learning is best-effort
        return []

    raw = llm.strip_thinking(message.get("content") or "")
    match = _JSON_ARRAY.search(raw)
    if not match:
        return []
    try:
        facts = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []

    stored = []
    for fact in facts[:3]:
        if not isinstance(fact, str):
            continue
        clean = " ".join(fact.split())[:400]
        if len(clean) < 8:
            continue
        try:
            result = await remember(clean, kind="learned", source="conversation")
        except Exception:  # noqa: BLE001
            continue
        if result.get("stored"):
            stored.append(clean)
    return stored


def learn_in_background(user_text: str, assistant_text: str) -> None:
    """Fire-and-forget: never make the operator wait for learning."""
    if not enabled():
        return
    try:
        asyncio.create_task(learn_from_exchange(user_text, assistant_text))
    except RuntimeError:
        pass
