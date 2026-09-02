"""
Private Local AI — FastAPI application.

Network posture:
  * Binds to 127.0.0.1 only (enforced in start.sh and asserted at startup).
  * Rejects requests whose Host header is not loopback (DNS-rebinding guard).
  * Sends a Content-Security-Policy that forbids every remote origin, so even
    a bug in the frontend cannot pull a font, script, or beacon from the web.
  * The only outbound socket the process opens is to 127.0.0.1:11434 (Ollama).
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal

from . import (
    __version__,
    actions,
    agent,
    coder,
    config,
    db,
    devservices,
    egress,
    indexing,
    ingest,
    connectors,
    llm,
    memory,
    naming,
    persona,
    power,
    rag,
    router,
    skills,
    stt,
    tts,
    vectorstore,
    vitals,
    voiceclone,
    watchers,
)
from .api_models import (
    BuildRequest,
    ChatRequest,
    DeleteAllRequest,
    ExecuteRequest,
    FolderRequest,
    NewConversation,
    ProposeRequest,
    EgressToggle,
    RenameConversation,
    ScanRequest,
    SearchRequest,
    SettingRequest,
    SpeakRequest,
    TokenRequest,
    WatcherPatch,
    WatcherRequest,
)
from .guards import PrivacyViolation, assert_loopback

STATIC_DIR = Path(__file__).parent / "static"
DELETE_PHRASE = "DELETE ALL MY LOCAL DATA"

#: How much prior conversation to replay, in characters. Keeps us inside the
#: modest context window rather than silently overflowing it.
HISTORY_BUDGET_CHARS = 6000


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    assert_loopback(config.OLLAMA_HOST)
    config.ensure_dirs()
    db.init_db()
    memory.init()
    voiceclone.init()
    egress.init()
    watchers.init()
    skills.ensure_dir()
    power.register_connectors()      # app bridges, coding tools and skills
    watchers.SCHEDULER.start()       # background polling for mail and services
    try:
        yield
    finally:
        await watchers.SCHEDULER.stop()


app = FastAPI(
    title="Private Local AI",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,       # no interactive docs endpoint
    redoc_url=None,
    openapi_url=None,
)


# --------------------------------------------------------------- middleware
@app.middleware("http")
async def local_only(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0]
    if host and host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
        return JSONResponse(
            {"detail": f"Refused: this app answers only on localhost (got Host: {host})."},
            status_code=421,
        )
    # A cross-origin browser request must never reach the API.
    origin = request.headers.get("origin")
    if origin:
        oh = origin.split("//")[-1].split(":")[0]
        if oh not in ("127.0.0.1", "localhost"):
            return JSONResponse({"detail": "Cross-origin requests are refused."}, status_code=403)

    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; media-src 'self' blob:; "
        "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; object-src 'none'"
    )
    # Revalidate the app's own assets. They are served from disk and edited in
    # place; a cached stylesheet silently hiding an edit is pure confusion.
    # ETag/Last-Modified still make the revalidation a cheap 304.
    if request.url.path.startswith("/static") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, must-revalidate"

    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = (
        "geolocation=(), camera=(), microphone=(self), interest-cohort=()"
    )
    return response


@app.exception_handler(PrivacyViolation)
async def _privacy_handler(_r: Request, exc: PrivacyViolation):
    return JSONResponse({"detail": str(exc), "kind": "privacy"}, status_code=400)


def _sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


SSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


# -------------------------------------------------------------------- status
@app.get("/api/status")
async def status() -> dict[str, Any]:
    health = await llm.health()
    speech = stt.availability()
    counts = vectorstore.stats()
    models = health.get("models", [])

    def present(name: str) -> bool:
        return name in models or f"{name}:latest" in models

    return {
        "app": "Private Local AI",
        "version": __version__,
        "build": config.BUILD_ID,
        "local_only": True,
        "network": {
            "bound_to": f"http://{config.HOST}:{config.PORT}",
            "model_runtime": health.get("endpoint"),
            "outbound_allowed": ["127.0.0.1:11434 (Ollama)"],
            "cloud_apis": "none",
            "telemetry": "none",
        },
        "runtime": {
            "reachable": health.get("ok", False),
            "error": health.get("error"),
            "version": health.get("version"),
            "models": models,
        },
        "models": {
            "chat": config.CHAT_MODEL,
            "chat_available": present(config.CHAT_MODEL),
            "fast": config.FAST_MODEL,
            "fast_available": present(config.FAST_MODEL),
            "embed": config.EMBED_MODEL,
            "embed_available": present(config.EMBED_MODEL),
            "num_ctx": config.NUM_CTX,
        },
        "speech": {
            "stt": speech,
            "tts": {"available": tts.available(), "speaking": tts.is_speaking()},
        },
        "documents": counts,
        "data_dir": str(config.DATA_DIR),
    }


# ─────────────────────────── automation ───────────────────────────
class AgentRequest(BaseModel):
    task: str = Field(min_length=1, max_length=8000)
    autonomy: Literal["off", "safe", "full"] = "off"
    model: str | None = None


class ApprovalRequest(BaseModel):
    run_id: str = Field(min_length=4, max_length=40)
    call_id: str = Field(min_length=4, max_length=40)
    approved: bool


@app.get("/api/connectors")
async def connector_catalog_grouped() -> dict[str, Any]:
    """Connectors as the interface shows them: by category, local first."""
    return {
        "categories": connectors.grouped(),
        "egress": egress.summary(),
        "watchers": watchers.status(),
    }


@app.get("/api/connectors/flat")
async def connector_catalog() -> dict[str, Any]:
    return {"apps": connectors.catalog()}


@app.get("/api/power")
async def power_catalog() -> dict[str, Any]:
    return {
        "tools": power.catalog(),
        "connectors": connectors.catalog(),
        "autonomy_levels": list(agent.AUTONOMY_LEVELS),
        "autonomy": db.get_setting("autonomy", "off"),
        "risk_levels": {
            "read": "Observes only.",
            "write": "Changes files, apps or system state.",
            "destructive": "Irreversible — deletes or overwrites.",
        },
        "warning": (
            "This toolset includes an unrestricted shell and AppleScript. With "
            "autonomy at 'full' the model can run anything without asking. "
            "Documents are untrusted input; keep autonomy low while indexing "
            "files you did not write."
        ),
    }


@app.post("/api/power/autonomy")
async def set_autonomy(body: dict[str, str] = Body(...)) -> dict[str, Any]:
    level = body.get("level", "off")
    if level not in agent.AUTONOMY_LEVELS:
        raise HTTPException(400, f"Level must be one of {agent.AUTONOMY_LEVELS}")
    db.set_setting("autonomy", level)
    return {"autonomy": level}


@app.post("/api/agent")
async def run_agent(req: AgentRequest) -> StreamingResponse:
    model = req.model or config.CHAT_MODEL
    try:
        await llm.ensure_model(model)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Model unavailable: {exc}") from exc

    async def stream() -> AsyncIterator[str]:
        try:
            async for ev in agent.run_agent(req.task, model=model,
                                            autonomy=req.autonomy):
                yield _sse(ev)
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "detail": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/api/agent/approve")
async def approve_call(body: ApprovalRequest) -> dict[str, Any]:
    run = agent.RUNS.get(body.run_id)
    if run is None:
        raise HTTPException(404, "That run has finished.")
    ok = run.resolve(body.call_id, body.approved)
    if not ok:
        raise HTTPException(404, "That approval is no longer pending.")
    return {"ok": True, "approved": body.approved}


@app.post("/api/agent/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, Any]:
    run = agent.RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "That run has finished.")
    run.cancel()
    return {"cancelled": True}


class ParallelRequest(BaseModel):
    tasks: list[str] = Field(min_length=1, max_length=3)
    autonomy: Literal["off", "safe", "full"] = "off"
    mode: str = "Atlas"


class SkillRequest(BaseModel):
    name: str = Field(min_length=2, max_length=41)
    text: str = Field(min_length=10, max_length=20000)


@app.get("/api/modes")
async def work_modes() -> dict[str, Any]:
    try:
        installed = await llm.local_model_names()
    except Exception:  # noqa: BLE001
        installed = []
    out = naming.modes()
    for meta in out.values():
        meta["available"] = (
            meta["model"] in installed or f"{meta['model']}:latest" in installed
        )
    return {"modes": out, "default": "Core"}


@app.post("/api/agent/parallel")
async def run_parallel_agent(req: ParallelRequest) -> StreamingResponse:
    """Run two or three tasks at once, in the heavy mode's lanes."""
    profile = naming.mode(req.mode)
    model = profile["model"]
    try:
        await llm.ensure_model(model)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Model unavailable: {exc}") from exc

    async def stream() -> AsyncIterator[str]:
        try:
            async for ev in agent.run_parallel(
                req.tasks, model=model, autonomy=req.autonomy,
                lanes=int(profile.get("lanes", 1)),
            ):
                yield _sse(ev)
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "detail": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/api/skills")
async def list_skills() -> dict[str, Any]:
    return {
        "skills": skills.catalog(),
        "dir": str(skills.SKILLS_DIR),
        "note": (
            "Markdown files you can edit directly. Instruction skills shape how "
            "the model works; command skills become named tools it can run."
        ),
    }


@app.post("/api/skills")
async def save_skill(body: SkillRequest) -> dict[str, Any]:
    try:
        result = skills.save(body.name, body.text)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    power.register_connectors()          # pick up a new command skill as a tool
    return result


@app.delete("/api/skills/{name}")
async def delete_skill(name: str) -> dict[str, Any]:
    return {"deleted": skills.delete(name)}


@app.post("/api/skills/{name}/run")
async def run_skill(name: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    try:
        return skills.run(name, body or {})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/names")
async def model_names() -> dict[str, Any]:
    """Short, role-based names for the models, for the interface to lead with."""
    return {"names": naming.catalog()}


@app.get("/api/router")
async def router_catalog() -> dict[str, Any]:
    try:
        installed = await llm.local_model_names()
    except Exception:  # noqa: BLE001
        installed = []
    cat = router.catalog()
    for key, prof in cat["profiles"].items():
        model, note = router.available_model(router.PROFILES[key], installed)
        prof["resolved_model"] = model
        prof["note"] = note
    return cat


@app.post("/api/router/preview")
async def router_preview(body: SearchRequest) -> dict[str, Any]:
    """Show which category a prompt would take, without answering it."""
    try:
        installed = await llm.local_model_names()
    except Exception:  # noqa: BLE001
        installed = []
    d = router.route(body.query, False, installed)
    return {"category": d.category, "label": d.profile.label,
            "model": d.model, "reason": d.reason,
            "temperature": d.profile.temperature, "think": d.profile.think}


# ───────────────────────────── memory ─────────────────────────────
class RememberRequest(BaseModel):
    text: str = Field(min_length=3, max_length=400)


@app.get("/api/memory")
async def memory_state() -> dict[str, Any]:
    return {
        "learning": memory.enabled(),
        "count": memory.count(),
        "memories": memory.list_memories(),
        "note": (
            "Learned from your own messages only. Document passages are never "
            "written to memory."
        ),
    }


@app.post("/api/memory/learning")
async def set_learning(body: dict[str, bool] = Body(...)) -> dict[str, Any]:
    memory.set_enabled(bool(body.get("enabled")))
    return {"learning": memory.enabled()}


@app.post("/api/memory")
async def add_memory(body: RememberRequest) -> dict[str, Any]:
    return await memory.remember(body.text, source="manual")


@app.delete("/api/memory/{memory_id}")
async def delete_memory(memory_id: int) -> dict[str, Any]:
    return {"forgotten": memory.forget(memory_id)}


@app.delete("/api/memory")
async def clear_memory() -> dict[str, Any]:
    return {"forgotten": memory.forget_all()}


@app.get("/api/personas")
async def personas() -> dict[str, Any]:
    return {"default": persona.DEFAULT, "personas": persona.catalog()}


@app.get("/api/vitals")
async def machine_vitals() -> dict[str, Any]:
    """Live, measured state of the local runtime. Polled by the interface."""
    return await vitals.snapshot()


@app.get("/api/models")
async def models() -> dict[str, Any]:
    try:
        names = await llm.local_model_names()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Local model runtime unreachable: {exc}") from exc
    return {
        "models": sorted(names),
        "default": config.CHAT_MODEL,
        "fast": config.FAST_MODEL,
    }


# --------------------------------------------------------------- chat / SSE
def _history_for(conversation_id: int) -> list[dict[str, str]]:
    msgs = db.get_messages(conversation_id)
    trimmed: list[dict[str, str]] = []
    budget = HISTORY_BUDGET_CHARS
    for m in reversed(msgs):
        if m["role"] not in ("user", "assistant"):
            continue
        cost = len(m["content"]) + 16
        if cost > budget:
            break
        budget -= cost
        trimmed.append({"role": m["role"], "content": m["content"]})
    return list(reversed(trimmed))


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    routed = None
    if not req.model or req.model == "auto":
        try:
            installed = await llm.local_model_names()
        except Exception:  # noqa: BLE001
            installed = []
        routed = router.route(req.message, req.use_documents, installed)
        model = routed.model
    else:
        model = req.model
    try:
        await llm.ensure_model(model)
    except PrivacyViolation as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Local model runtime unreachable: {exc}") from exc

    conversation_id = req.conversation_id
    if not conversation_id:
        title = req.message.strip().split("\n")[0][:60] or "New chat"
        conversation_id = db.create_conversation(title=title, model=model)
    db.add_message(conversation_id, "user", req.message)

    history = _history_for(conversation_id)[:-1]

    async def stream() -> AsyncIterator[str]:
        citations: list[dict[str, Any]] = []
        try:
            yield _sse({
                "type": "meta",
                "conversation_id": conversation_id,
                "model": model,
                "friendly": naming.friendly(model),
                "route": None if routed is None else {
                    "category": routed.category,
                    "label": routed.profile.label,
                    "reason": routed.reason,
                    "why": routed.profile.why,
                },
            })

            if req.use_documents:
                yield _sse({"type": "status", "text": "Searching your indexed documents…"})
                hits = await rag.retrieve(req.message)
                messages, citations = rag.build_messages(
                    req.message, hits, history, persona_name=req.persona
                )
                yield _sse(
                    {
                        "type": "citations",
                        "citations": citations,
                        "note": (
                            "Passages are supplied to the model as untrusted data."
                            if citations
                            else "No indexed passage matched; answering without documents."
                        ),
                    }
                )
            else:
                system = rag.system_prompt(False, req.persona)
                recalled = await memory.recall(req.message)
                if recalled:
                    system += "\n\n" + memory.as_prompt_block(recalled)
                    yield _sse({
                        "type": "recall",
                        "memories": [
                            {"text": m["text"], "score": m["score"]} for m in recalled
                        ],
                    })
                messages = [{"role": "system", "content": system}]
                messages.extend(history)
                messages.append({"role": "user", "content": req.message})

            if req.use_watchers:
                report = watchers.context_block()
                if report and messages and messages[0].get("role") == "system":
                    messages[0]["content"] += "\n\n" + report
                    yield _sse({"type": "status",
                                "text": "Including the current watch report."})

            buffer: list[str] = []
            in_think = False
            temp = routed.profile.temperature if routed else 0.6
            ctx = routed.profile.num_ctx if routed else config.NUM_CTX
            async for piece in llm.chat_stream(
                messages, model=model, num_ctx=ctx, temperature=temp
            ):
                buffer.append(piece)
                joined = "".join(buffer[-4:])
                if "<think>" in joined and "</think>" not in joined:
                    in_think = True
                if in_think:
                    if "</think>" in "".join(buffer):
                        in_think = False
                    continue
                yield _sse({"type": "token", "text": piece})

            answer = llm.strip_thinking("".join(buffer))
            used = rag.used_citations(answer, citations) if citations else []
            db.add_message(
                conversation_id, "assistant", answer, model=model, citations=used or citations
            )
            # Learn only from the operator's own words, never from passages.
            if not req.use_documents:
                memory.learn_in_background(req.message, answer)
            yield _sse(
                {
                    "type": "done",
                    "conversation_id": conversation_id,
                    "citations": used or citations,
                    "answer": answer,
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "detail": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


# ------------------------------------------------------------- conversations
@app.get("/api/conversations")
async def conversations() -> dict[str, Any]:
    return {"conversations": db.list_conversations()}


@app.post("/api/conversations")
async def new_conversation(body: NewConversation) -> dict[str, Any]:
    cid = db.create_conversation(body.title, config.CHAT_MODEL)
    return {"id": cid, "title": body.title}


@app.get("/api/conversations/{cid}")
async def conversation(cid: int) -> dict[str, Any]:
    msgs = db.get_messages(cid)
    if not msgs and not any(c["id"] == cid for c in db.list_conversations()):
        raise HTTPException(404, "No such conversation")
    return {"id": cid, "messages": msgs}


@app.patch("/api/conversations/{cid}")
async def rename(cid: int, body: RenameConversation) -> dict[str, Any]:
    db.rename_conversation(cid, body.title)
    return {"ok": True}


@app.delete("/api/conversations/{cid}")
async def drop_conversation(cid: int) -> dict[str, Any]:
    db.delete_conversation(cid)
    return {"ok": True}


# ------------------------------------------------------------------ documents
@app.get("/api/documents")
async def documents() -> dict[str, Any]:
    return {"sources": vectorstore.list_sources(), "stats": vectorstore.stats()}


@app.post("/api/documents/preview")
async def preview_folder(body: FolderRequest) -> dict[str, Any]:
    folder = Path(body.path).expanduser()
    try:
        files = ingest.discover_files(folder)
    except ingest.RefusedPath as exc:
        raise HTTPException(403, str(exc)) from exc
    except (NotADirectoryError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "folder": str(folder.resolve()),
        "count": len(files),
        "files": [
            {"name": f.name, "path": str(f), "bytes": f.stat().st_size, "ext": f.suffix.lower()}
            for f in files[:500]
        ],
    }


@app.post("/api/documents/index_folder")
async def index_folder(body: FolderRequest) -> StreamingResponse:
    folder = Path(body.path).expanduser()
    try:
        files = ingest.discover_files(folder)
    except ingest.RefusedPath as exc:
        raise HTTPException(403, str(exc)) from exc
    except (NotADirectoryError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    if not files:
        raise HTTPException(400, "No supported documents found in that folder.")

    async def stream() -> AsyncIterator[str]:
        try:
            async for ev in indexing.index_paths(files, origin="folder"):
                yield _sse(ev)
        except Exception as exc:  # noqa: BLE001
            yield _sse({"event": "error", "detail": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/api/documents/upload")
async def upload(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    staged: list[Path] = []
    errors: list[dict[str, str]] = []
    for f in files:
        try:
            data = await f.read()
            staged.append(indexing.stage_upload(f.filename or "upload", data))
        except ingest.UnsupportedFile as exc:
            errors.append({"file": f.filename or "?", "error": str(exc)})
    results = []
    for p in staged:
        results.append(await indexing.index_file(p, origin="upload"))
    return {
        "indexed": [r for r in results if r["ok"]],
        "failed": [r for r in results if not r["ok"]] + errors,
        "stats": vectorstore.stats(),
    }


@app.post("/api/documents/{source_id}/reindex")
async def reindex(source_id: int) -> dict[str, Any]:
    src = vectorstore.get_source(source_id)
    if not src:
        raise HTTPException(404, "No such source")
    result = await indexing.index_file(Path(src["path"]), origin=src["origin"])
    if not result["ok"]:
        raise HTTPException(400, result["error"] or "Re-index failed")
    return {"ok": True, "result": result, "stats": vectorstore.stats()}


@app.post("/api/documents/reindex_all")
async def reindex_all() -> dict[str, Any]:
    results = []
    for src in vectorstore.list_sources():
        results.append(await indexing.index_file(Path(src["path"]), origin=src["origin"]))
    return {
        "reindexed": sum(1 for r in results if r["ok"]),
        "failed": [r for r in results if not r["ok"]],
        "stats": vectorstore.stats(),
    }


@app.delete("/api/documents/{source_id}")
async def remove_document(source_id: int) -> dict[str, Any]:
    out = indexing.forget_source(source_id)
    if not out["removed"]:
        raise HTTPException(404, "No such source")
    return {**out, "stats": vectorstore.stats()}


@app.delete("/api/documents")
async def clear_documents() -> dict[str, Any]:
    removed = vectorstore.clear_index()
    files = indexing.purge_uploads()
    return {"removed": removed, "upload_copies_deleted": files, "stats": vectorstore.stats()}


@app.post("/api/documents/search")
async def search_documents(body: SearchRequest) -> dict[str, Any]:
    hits = await rag.retrieve(body.query, k=body.k)
    return {
        "query": body.query,
        "results": [rag.format_citation(h, i) for i, h in enumerate(hits, start=1)],
    }


# --------------------------------------------------------------------- voice
@app.post("/api/stt")
async def transcribe(audio: UploadFile = File(...)) -> dict[str, Any]:
    data = await audio.read()
    try:
        return stt.transcribe(data)
    except stt.SpeechError as exc:
        raise HTTPException(400, str(exc)) from exc


# ───────────────────────── cloned voices ─────────────────────────
@app.get("/api/voice")
async def voice_status() -> dict[str, Any]:
    return voiceclone.status()


@app.post("/api/voice")
async def add_voice(
    name: str = Form(...), audio: UploadFile = File(...)
) -> dict[str, Any]:
    data = await audio.read()
    if len(data) > config.MAX_AUDIO_BYTES:
        raise HTTPException(400, "That recording is too large.")
    try:
        return voiceclone.add_voice(name, data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/voice/{name}")
async def remove_voice(name: str) -> dict[str, Any]:
    return {"deleted": voiceclone.delete_voice(name)}


@app.post("/api/voice/speak")
async def speak_cloned(body: dict[str, str] = Body(...)) -> dict[str, Any]:
    text = (body.get("text") or "").strip()
    name = body.get("voice") or ""
    if not text:
        raise HTTPException(400, "Nothing to speak.")
    if db.get_setting("speech_muted", "0") == "1":
        return {"speaking": False, "muted": True}
    try:
        path = await asyncio.to_thread(voiceclone.synthesize, text, name)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    tts.play_file(path)
    return {"speaking": True, "voice": name, "engine": "chatterbox (cloned)"}


@app.get("/api/tts/voices")
async def voices() -> dict[str, Any]:
    return {"available": tts.available(), "voices": tts.list_voices()}


@app.post("/api/tts/speak")
async def speak(body: SpeakRequest) -> dict[str, Any]:
    if db.get_setting("speech_muted", "0") == "1":
        return {"speaking": False, "muted": True}
    try:
        return tts.speak(body.text, voice=body.voice, rate=body.rate)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/tts/mute")
async def set_mute(body: dict[str, bool] = Body(...)) -> dict[str, Any]:
    """Silence spoken replies until switched back on."""
    muted = bool(body.get("muted"))
    db.set_setting("speech_muted", "1" if muted else "0")
    if muted:
        tts.stop()
    return {"muted": muted}


@app.get("/api/tts/status")
async def tts_status() -> dict[str, Any]:
    """Polled while a reply is being spoken, to drive the interface."""
    return {
        "speaking": tts.is_speaking(),
        "muted": db.get_setting("speech_muted", "0") == "1",
    }


@app.post("/api/tts/stop")
async def stop_speaking() -> dict[str, Any]:
    tts.stop()
    return {"speaking": False}


# ------------------------------------------------------------------- actions
@app.get("/api/actions")
async def action_catalog() -> dict[str, Any]:
    return {
        "allowed": list(actions.ALLOWED_ACTIONS),
        "mutating": list(actions.MUTATING_ACTIONS),
        "info": actions.ACTION_INFO,
        "policy": (
            "Only these four actions exist. There is no shell, AppleScript, "
            "Mail, Keychain, or arbitrary filesystem access. Every action that "
            "opens, writes, or changes something requires you to approve the "
            "exact parameters first."
        ),
        "reminders": actions.list_reminders(20),
        "log": actions.recent_log(20),
    }


@app.post("/api/actions/propose")
async def propose_action(body: ProposeRequest) -> dict[str, Any]:
    try:
        return actions.propose(body.kind, body.params, origin=body.origin)
    except actions.ActionError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/actions/execute")
async def execute_action(body: ExecuteRequest) -> dict[str, Any]:
    try:
        return actions.execute(body.token, body.approved)
    except actions.ApprovalRequired as exc:
        raise HTTPException(403, str(exc)) from exc
    except actions.ActionError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/actions/search")
async def action_search(body: SearchRequest) -> dict[str, Any]:
    try:
        return actions.run_readonly("search_indexed_files", {"query": body.query})
    except actions.ActionError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/reminders")
async def reminders() -> dict[str, Any]:
    return {"reminders": actions.list_reminders()}


@app.delete("/api/reminders/{rid}")
async def delete_reminder(rid: int) -> dict[str, Any]:
    return {"deleted": actions.delete_reminder(rid)}


# ------------------------------------------------------------------- privacy
@app.get("/api/privacy")
async def privacy_report() -> dict[str, Any]:
    data_files = []
    if config.DATA_DIR.exists():
        for p in sorted(config.DATA_DIR.rglob("*")):
            if p.is_file():
                data_files.append(
                    {"path": str(p.relative_to(config.DATA_DIR)), "bytes": p.stat().st_size}
                )
    return {
        "data_dir": str(config.DATA_DIR),
        "file_count": len(data_files),
        "total_bytes": sum(f["bytes"] for f in data_files),
        "files": data_files[:200],
        "guarantees": [
            "The web server binds to 127.0.0.1 and refuses non-loopback Host headers.",
            "The only outbound connection is to the Ollama runtime on 127.0.0.1:11434.",
            "No analytics, no remote fonts, no CDNs, no third-party JavaScript.",
            "Model weights, embeddings, transcripts and chat history stay in the data folder.",
            "Document text is passed to the model as untrusted data, never as instructions.",
            "Mac actions are limited to four allowlisted operations behind explicit approval.",
        ],
        "delete_phrase": DELETE_PHRASE,
    }


@app.post("/api/privacy/delete_all")
async def delete_all(body: DeleteAllRequest) -> dict[str, Any]:
    if body.confirm_phrase.strip() != DELETE_PHRASE:
        raise HTTPException(
            400, f'Type exactly "{DELETE_PHRASE}" to confirm.'
        )
    tts.stop()
    uploads = indexing.purge_uploads()
    # voice samples are recordings of a person; delete the audio, not just the row
    voices_removed = 0
    for v in voiceclone.list_voices():
        if voiceclone.delete_voice(v["name"]):
            voices_removed += 1
    counts = db.delete_all_data()
    vectorstore.invalidate()
    return {
        "deleted": True,
        "rows_removed": counts,
        "upload_copies_deleted": uploads,
        "voice_samples_deleted": voices_removed,
        "note": (
            "Chats, documents, embeddings, reminders and the action log are gone. "
            "Downloaded model weights were kept — remove them with 'ollama rm' "
            "and by deleting local_data/models."
        ),
    }


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return {
        "voice": db.get_setting("voice"),
        "default_model": db.get_setting("default_model", config.CHAT_MODEL),
        "use_documents": db.get_setting("use_documents", "0"),
    }


@app.post("/api/settings")
async def put_setting(body: SettingRequest) -> dict[str, Any]:
    db.set_setting(body.key, body.value)
    return {"ok": True}


# ------------------------------------------------------------------ watchers
@app.get("/api/watch")
async def watch_state() -> dict[str, Any]:
    return {
        "watchers": watchers.list_watchers(),
        "status": watchers.status(),
        "events": watchers.events(limit=40),
        "egress": egress.summary(),
    }


@app.get("/api/watch/catalog")
async def watch_catalog() -> dict[str, Any]:
    return watchers.catalog()


@app.post("/api/watch")
async def watch_create(body: WatcherRequest) -> dict[str, Any]:
    try:
        watcher_id = watchers.create(
            body.kind, body.label, body.settings, body.interval_s, body.notify
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": watcher_id, "watcher": watchers.get(watcher_id)}


@app.patch("/api/watch/{watcher_id}")
async def watch_update(watcher_id: int, body: WatcherPatch) -> dict[str, Any]:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "Nothing to change.")
    if not watchers.update(watcher_id, **changes):
        raise HTTPException(404, "No such watcher.")
    return {"watcher": watchers.get(watcher_id)}


@app.delete("/api/watch/{watcher_id}")
async def watch_delete(watcher_id: int) -> dict[str, Any]:
    if not watchers.delete(watcher_id):
        raise HTTPException(404, "No such watcher.")
    return {"deleted": watcher_id}


@app.post("/api/watch/{watcher_id}/run")
async def watch_run_now(watcher_id: int) -> dict[str, Any]:
    """Poll one watcher immediately — the 'test this' button."""
    result = await watchers.run_watcher(watcher_id, announce=False)
    return {**result, "watcher": watchers.get(watcher_id)}


@app.get("/api/watch/events")
async def watch_events(limit: int = 60, unseen: bool = False) -> dict[str, Any]:
    return {
        "events": watchers.events(limit=max(1, min(limit, 200)), unseen_only=unseen),
        "unseen": watchers.unseen_count(),
    }


@app.post("/api/watch/events/seen")
async def watch_mark_seen(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    ids = body.get("ids")
    marked = watchers.mark_seen([int(i) for i in ids] if ids else None)
    return {"marked": marked, "unseen": watchers.unseen_count()}


# -------------------------------------------------------------------- egress
@app.get("/api/egress")
async def egress_state() -> dict[str, Any]:
    return {
        "summary": egress.summary(),
        "recent": egress.recent(40),
        "providers": devservices.catalog(),
    }


@app.post("/api/egress/enable")
async def egress_enable(body: EgressToggle) -> dict[str, Any]:
    """
    Turn outbound access on or off.

    This is the one switch that changes what "local only" means, so it is
    deliberately a separate, explicit call rather than a side effect of adding
    a watcher.
    """
    egress.set_enabled(body.enabled)
    return {"enabled": egress.enabled(), "summary": egress.summary()}


@app.post("/api/egress/token")
async def egress_put_token(body: TokenRequest) -> dict[str, Any]:
    if body.provider not in devservices.PROVIDERS:
        raise HTTPException(400, f"Unknown service: {body.provider}")
    egress.put_secret(f"devservice:{body.provider}", body.token)
    # The value is never returned — only a hint that it is there.
    return {"provider": body.provider, "hint": egress.mask(body.token)}


@app.delete("/api/egress/token/{provider}")
async def egress_drop_token(provider: str) -> dict[str, Any]:
    return {"removed": egress.drop_secret(f"devservice:{provider}")}


# -------------------------------------------------------------------- coding
@app.post("/api/code/scan")
async def code_scan(body: ScanRequest) -> dict[str, Any]:
    try:
        return coder.project_map(body.path)
    except power.PowerError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/code/build")
async def code_build(req: BuildRequest) -> StreamingResponse:
    """
    Implement a change and keep going until the project's own tests pass.

    Streams every step: the plan, each tool call, the verification run, and
    the next attempt when verification fails.
    """
    model = req.model or naming.model_for("Forge") or config.CHAT_MODEL
    try:
        await llm.ensure_model(model)
    except PrivacyViolation as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Local model runtime unreachable: {exc}") from exc

    async def stream() -> AsyncIterator[str]:
        try:
            async for event in coder.build(
                req.task, req.path, model=model, autonomy=req.autonomy,
                max_rounds=req.rounds, verify_command=req.verify_command,
            ):
                yield _sse(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "detail": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


# -------------------------------------------------------------------- static
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
async def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")
