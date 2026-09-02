"""
Local model runtime client (Ollama over loopback HTTP).

Nothing in this module can reach the public internet: the base URL is
validated as a loopback address at import time and on every call.
"""
from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

import httpx

from . import config
from .guards import PrivacyViolation, assert_local_model, assert_loopback

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def base_url() -> str:
    host = config.OLLAMA_HOST
    assert_loopback(host)
    if not host.startswith("http"):
        host = f"http://{host}"
    return host.rstrip("/")


#: A JSON object that looks like a tool call: exactly a name and its arguments.
_CALL_KEYS = ({"name", "arguments"}, {"name", "parameters"}, {"name", "args"})


def _json_objects(text: str) -> list[dict[str, Any]]:
    """
    Pull every complete top-level JSON object out of a blob of text.

    Written by hand rather than with a regex because these objects nest, and a
    regex that balances braces is a regex you will regret. Scans once, tracking
    depth and string state so a brace inside a string literal does not confuse
    the count.
    """
    out: list[dict[str, Any]] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        parsed = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, dict):
                        out.append(parsed)
                    start = -1
    return out


def recover_tool_calls(message: dict[str, Any]) -> dict[str, Any]:
    """
    Rescue a tool call that the model emitted as text instead of structured
    output.

    Some local models produce a perfectly correct call — the right name, the
    right arguments — but their Ollama chat template does not wrap it in the
    markers the API needs, so it arrives in `content` and `tool_calls` is
    empty. `qwen2.5-coder:7b` does exactly this despite advertising a `tools`
    capability, which would otherwise make it useless as an agent: the model
    knows what to do and the plumbing drops it on the floor.

    The match is deliberately strict — an object must have a string `name` and
    a dict of arguments and nothing else of substance — so that a model
    answering a question *about* JSON does not get its answer eaten.
    """
    if message.get("tool_calls"):
        return message
    content = message.get("content") or ""
    if "{" not in content or '"name"' not in content:
        return message

    calls = []
    for obj in _json_objects(content):
        keys = set(obj)
        if not any(keys == shape or keys <= shape | {"id", "type"} for shape in _CALL_KEYS):
            continue
        name = obj.get("name")
        args = obj.get("arguments", obj.get("parameters", obj.get("args")))
        if not isinstance(name, str) or not name:
            continue
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                continue
        if not isinstance(args, dict):
            continue
        calls.append({"function": {"name": name, "arguments": args}})

    if not calls:
        return message
    return {**message, "tool_calls": calls, "content": "", "recovered": True}


def strip_thinking(text: str) -> str:
    """Remove reasoning blocks some models emit even when thinking is off."""
    cleaned = _THINK_BLOCK.sub("", text)
    # An unterminated opening tag means the stream was cut mid-thought.
    if "<think>" in cleaned and "</think>" not in cleaned:
        cleaned = cleaned.split("<think>")[0]
    return cleaned.strip()


class OllamaError(RuntimeError):
    pass


async def _get(path: str, timeout: float = 10.0) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"{base_url()}{path}")
        r.raise_for_status()
        return r.json()


async def health() -> dict[str, Any]:
    """Runtime reachability + which local models exist."""
    try:
        version = await _get("/api/version", timeout=4.0)
        tags = await _get("/api/tags", timeout=8.0)
    except PrivacyViolation:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the UI
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "endpoint": base_url(),
            "models": [],
        }
    models = [m.get("name", "") for m in tags.get("models", [])]
    return {
        "ok": True,
        "endpoint": base_url(),
        "version": version.get("version"),
        "models": sorted(models),
    }


async def local_model_names() -> list[str]:
    tags = await _get("/api/tags", timeout=8.0)
    return [m.get("name", "") for m in tags.get("models", [])]


async def ensure_model(name: str) -> None:
    """Raise unless `name` is a plain, locally-present model."""
    assert_local_model(name)
    assert_local_model(name, await local_model_names())


async def chat_stream(
    messages: list[dict[str, str]],
    model: str,
    num_ctx: int | None = None,
    temperature: float = 0.6,
) -> AsyncIterator[str]:
    """
    Yield response text chunks from the local model.

    `think=False` asks reasoning-capable models (Qwen3) to answer directly;
    `strip_thinking` is still applied downstream as a belt-and-braces measure.
    """
    assert_local_model(model)
    ctx = min(int(num_ctx or config.NUM_CTX), config.NUM_CTX_MAX)
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "think": False,
        "keep_alive": config.KEEP_ALIVE,
        "options": {"num_ctx": ctx, "temperature": temperature},
    }
    url = f"{base_url()}/api/chat"
    timeout = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")[:400]
                raise OllamaError(f"Ollama returned {resp.status_code}: {body}")
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("error"):
                    raise OllamaError(str(obj["error"]))
                piece = (obj.get("message") or {}).get("content") or ""
                if piece:
                    yield piece
                if obj.get("done"):
                    break


#: Nomic Embed is trained with task prefixes; omitting them measurably
#: degrades retrieval. Models that do not use prefixes get an empty pair.
_TASK_PREFIXES = {"nomic-embed": ("search_document: ", "search_query: ")}


def _prefixes(model: str) -> tuple[str, str]:
    for key, pair in _TASK_PREFIXES.items():
        if model.lower().startswith(key):
            return pair
    return ("", "")


async def embed_documents(texts: list[str], model: str | None = None) -> list[list[float]]:
    """Embed passages for storage, applying the model's document prefix."""
    name = model or config.EMBED_MODEL
    doc_prefix, _ = _prefixes(name)
    return await embed([doc_prefix + t for t in texts], model=name)


async def embed_query(text: str, model: str | None = None) -> list[float]:
    """Embed a search query, applying the model's query prefix."""
    name = model or config.EMBED_MODEL
    _, q_prefix = _prefixes(name)
    vectors = await embed([q_prefix + text], model=name)
    return vectors[0] if vectors else []


async def chat_once(
    messages: list[dict[str, Any]],
    model: str,
    tools: list[dict[str, Any]] | None = None,
    num_ctx: int | None = None,
    temperature: float = 0.4,
    think: bool = False,
) -> dict[str, Any]:
    """
    One non-streaming turn, used by the agent loop.

    Tool calls are far easier to handle reliably in a single response than
    reassembled from a token stream, and an agent step is short anyway.
    """
    assert_local_model(model)
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": think,
        "keep_alive": config.KEEP_ALIVE,
        "options": {
            "num_ctx": min(int(num_ctx or config.NUM_CTX), config.NUM_CTX_MAX),
            "temperature": temperature,
        },
    }
    if tools:
        payload["tools"] = tools
    timeout = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{base_url()}/api/chat", json=payload)
        if r.status_code >= 400:
            raise OllamaError(f"Ollama returned {r.status_code}: {r.text[:400]}")
        data = r.json()
    if data.get("error"):
        raise OllamaError(str(data["error"]))
    message = data.get("message") or {}
    return recover_tool_calls(message) if tools else message


async def embed(texts: list[str], model: str | None = None) -> list[list[float]]:
    """Embed a batch of texts verbatim with the local embedding model."""
    name = model or config.EMBED_MODEL
    assert_local_model(name)
    if not texts:
        return []
    url = f"{base_url()}/api/embed"
    timeout = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json={"model": name, "input": texts})
        if r.status_code >= 400:
            raise OllamaError(f"Embedding failed ({r.status_code}): {r.text[:300]}")
        data = r.json()
    vectors = data.get("embeddings")
    if not vectors or len(vectors) != len(texts):
        raise OllamaError("Embedding response did not match the number of inputs.")
    return vectors
