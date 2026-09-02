"""
Safe Mac actions — a deliberately tiny allowlist behind human approval.

Design rules, in order of importance:

  1. There are exactly four actions. There is no generic "run command",
     no shell, no AppleScript, no Mail/Keychain/browser automation.
  2. Anything that opens, writes, or changes something requires an explicit
     approval step: the caller must first `propose()`, show the operator the
     exact resolved parameters, and then `execute()` with the single-use token
     that proposal returned. A token cannot be minted and spent in one call.
  3. Model output never reaches `execute()` on its own. A proposal originating
     from the model is tagged `model-proposal`, re-validated from scratch, and
     still needs the operator to approve it in the UI.
  4. `open_file` is restricted to files already in the document index, with a
     document extension, compared by fully-resolved path so a symlink cannot
     point somewhere else.
  5. `draft_reminder` writes a row in this app's own SQLite file. It does not
     touch Reminders.app, Calendar, or any Apple service.
"""
from __future__ import annotations

import json
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import config, db, vectorstore

TOKEN_TTL_SECONDS = 300

ALLOWED_ACTIONS = ("search_indexed_files", "open_file", "open_url", "draft_reminder")

#: Actions that open, write, or change something. These require approval.
MUTATING_ACTIONS = ("open_file", "open_url", "draft_reminder")

ACTION_INFO: dict[str, dict[str, str]] = {
    "search_indexed_files": {
        "title": "Search indexed files",
        "effect": "Reads the local document index. Changes nothing.",
        "requires_approval": "no",
    },
    "open_file": {
        "title": "Open a file",
        "effect": "Opens one indexed document in its default macOS application.",
        "requires_approval": "yes",
    },
    "open_url": {
        "title": "Open a URL",
        "effect": "Opens one http/https address in your default browser.",
        "requires_approval": "yes",
    },
    "draft_reminder": {
        "title": "Draft a reminder",
        "effect": "Saves a draft in this app's local database. Does not touch Reminders.app.",
        "requires_approval": "yes",
    },
}


class ActionError(ValueError):
    """Validation failed. The message is safe to show the operator."""


class ApprovalRequired(PermissionError):
    pass


@dataclass
class Pending:
    token: str
    kind: str
    params: dict[str, Any]
    summary: str
    origin: str
    created_at: float
    used: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


_pending: dict[str, Pending] = {}
_lock = threading.Lock()


def _purge_expired() -> None:
    now = time.time()
    with _lock:
        for tok in [
            t for t, p in _pending.items() if now - p.created_at > TOKEN_TTL_SECONDS
        ]:
            _pending.pop(tok, None)


# ------------------------------------------------------------------ validation
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_text(value: Any, field_name: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise ActionError(f"'{field_name}' must be text.")
    text = _CONTROL.sub("", value).strip()
    if not text:
        raise ActionError(f"'{field_name}' cannot be empty.")
    if len(text) > max_len:
        raise ActionError(f"'{field_name}' is longer than {max_len} characters.")
    return text


def _validate_open_file(params: dict[str, Any]) -> tuple[dict[str, Any], str, dict]:
    raw = params.get("path") or params.get("file") or ""
    if not isinstance(raw, str) or not raw.strip():
        raise ActionError("An indexed file path is required.")

    target = Path(raw).expanduser()
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ActionError(f"That file does not exist: {raw}") from exc

    if resolved.suffix.lower() not in config.SUPPORTED_EXTENSIONS:
        raise ActionError(
            f"Refusing to open '{resolved.suffix}'. Only indexed document types "
            f"({', '.join(sorted(config.SUPPORTED_EXTENSIONS))}) may be opened."
        )

    indexed = {}
    for src in vectorstore.list_sources():
        try:
            indexed[str(Path(src["path"]).resolve())] = src
        except (OSError, RuntimeError):
            continue

    match = indexed.get(str(resolved))
    if match is None:
        raise ActionError(
            "Refusing to open a file that is not in the document index. "
            "Add its folder under Documents first."
        )
    if not resolved.is_file():
        raise ActionError("That path is not a regular file.")

    summary = f"Open “{resolved.name}” in its default macOS application."
    return (
        {"path": str(resolved)},
        summary,
        {"filename": resolved.name, "source_id": match["id"], "bytes": match["bytes"]},
    )


def _validate_open_url(params: dict[str, Any]) -> tuple[dict[str, Any], str, dict]:
    raw = _clean_text(params.get("url", ""), "url", 2048)
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        raise ActionError(
            f"Refusing scheme '{parts.scheme or '(none)'}'. Only http and https "
            "addresses may be opened."
        )
    if not parts.netloc:
        raise ActionError("That URL has no host.")
    if "@" in parts.netloc:
        raise ActionError("Refusing a URL that embeds credentials.")

    host = parts.hostname or ""
    warnings: list[str] = []
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        warnings.append(
            "The hostname contains non-ASCII characters, which can be used to "
            "imitate a familiar domain."
        )
    if parts.scheme.lower() == "http":
        warnings.append("This is an unencrypted http:// address.")

    summary = f"Open {raw} in your default browser."
    return {"url": raw}, summary, {"host": host, "warnings": warnings}


def _validate_draft_reminder(params: dict[str, Any]) -> tuple[dict[str, Any], str, dict]:
    text = _clean_text(params.get("text", ""), "text", 500)
    due_raw = params.get("due") or ""
    due = _CONTROL.sub("", due_raw).strip()[:100] if isinstance(due_raw, str) else ""
    summary = f"Save a reminder draft: “{text}”" + (f" (due {due})" if due else "")
    return {"text": text, "due": due}, summary, {}


def _validate_search(params: dict[str, Any]) -> tuple[dict[str, Any], str, dict]:
    query = _clean_text(params.get("query", ""), "query", 400)
    return {"query": query}, f"Search indexed files for “{query}”.", {}


_VALIDATORS = {
    "search_indexed_files": _validate_search,
    "open_file": _validate_open_file,
    "open_url": _validate_open_url,
    "draft_reminder": _validate_draft_reminder,
}


def validate(kind: str, params: dict[str, Any]) -> tuple[dict[str, Any], str, dict]:
    if kind not in ALLOWED_ACTIONS:
        raise ActionError(
            f"'{kind}' is not an allowed action. Permitted: "
            + ", ".join(ALLOWED_ACTIONS)
        )
    if not isinstance(params, dict):
        raise ActionError("Action parameters must be an object.")
    return _VALIDATORS[kind](params)


def requires_approval(kind: str) -> bool:
    return kind in MUTATING_ACTIONS


# -------------------------------------------------------------------- propose
def propose(kind: str, params: dict[str, Any], origin: str = "user") -> dict[str, Any]:
    """
    Validate an action and mint a single-use approval token.
    Nothing happens to the machine here.
    """
    _purge_expired()
    clean, summary, detail = validate(kind, params)
    token = secrets.token_urlsafe(32)
    pending = Pending(
        token=token,
        kind=kind,
        params=clean,
        summary=summary,
        origin=origin if origin in ("user", "model-proposal") else "user",
        created_at=time.time(),
        detail=detail,
    )
    with _lock:
        _pending[token] = pending
    _log(kind, clean, approved=False, outcome="proposed", origin=pending.origin)
    return {
        "token": token,
        "kind": kind,
        "params": clean,
        "summary": summary,
        "detail": detail,
        "origin": pending.origin,
        "requires_approval": requires_approval(kind),
        "expires_in": TOKEN_TTL_SECONDS,
        "info": ACTION_INFO[kind],
    }


# -------------------------------------------------------------------- execute
def _run(argv: list[str]) -> None:
    """Launch a macOS helper with an argument vector. Never a shell string."""
    subprocess.run(
        argv,
        check=True,
        shell=False,
        timeout=20,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _do_open_file(params: dict[str, Any]) -> dict[str, Any]:
    _run([config.OPEN_BIN, "--", params["path"]])
    return {"opened": params["path"]}


def _do_open_url(params: dict[str, Any]) -> dict[str, Any]:
    _run([config.OPEN_BIN, "--", params["url"]])
    return {"opened": params["url"]}


def _do_draft_reminder(params: dict[str, Any]) -> dict[str, Any]:
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO reminders (text, due, status, created_at) VALUES (?,?,?,?)",
            (params["text"], params.get("due") or None, "draft", time.time()),
        )
        rid = int(cur.lastrowid)
    return {"reminder_id": rid, "text": params["text"], "due": params.get("due") or None}


def _do_search(params: dict[str, Any]) -> dict[str, Any]:
    hits = vectorstore.search(params["query"], None, k=8)
    return {
        "query": params["query"],
        "results": [
            {
                "filename": h.filename,
                "locator": h.locator,
                "snippet": h.text[:220].strip(),
                "source_id": h.source_id,
                "path": h.path,
            }
            for h in hits
        ],
    }


_EXECUTORS = {
    "search_indexed_files": _do_search,
    "open_file": _do_open_file,
    "open_url": _do_open_url,
    "draft_reminder": _do_draft_reminder,
}


def execute(token: str, approved: bool) -> dict[str, Any]:
    """
    Carry out a previously proposed action.

    Requires a live, unused token AND an explicit `approved=True`. Tokens are
    consumed whatever the outcome, so a denied action cannot be replayed.
    """
    _purge_expired()
    with _lock:
        pending = _pending.get(token)
        if pending is None:
            raise ApprovalRequired(
                "That approval has expired or was already used. Propose the action again."
            )
        if pending.used:
            _pending.pop(token, None)
            raise ApprovalRequired("That approval token was already used.")
        pending.used = True
        _pending.pop(token, None)

    if time.time() - pending.created_at > TOKEN_TTL_SECONDS:
        _log(pending.kind, pending.params, False, "expired", pending.origin)
        raise ApprovalRequired("That approval expired. Propose the action again.")

    if not approved:
        _log(pending.kind, pending.params, False, "denied", pending.origin)
        return {"executed": False, "reason": "denied", "kind": pending.kind}

    if requires_approval(pending.kind) and approved is not True:
        raise ApprovalRequired("Explicit approval is required for this action.")

    # Re-validate immediately before acting: the index may have changed since
    # the proposal was minted.
    clean, _summary, _detail = validate(pending.kind, pending.params)

    try:
        result = _EXECUTORS[pending.kind](clean)
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode("utf-8", "replace")[:300]
        _log(pending.kind, clean, True, f"error: {err}", pending.origin)
        raise ActionError(f"macOS refused the action: {err or exc}") from exc
    except subprocess.TimeoutExpired as exc:
        _log(pending.kind, clean, True, "timeout", pending.origin)
        raise ActionError("The action timed out.") from exc

    _log(pending.kind, clean, True, "ok", pending.origin)
    return {"executed": True, "kind": pending.kind, "result": result}


def run_readonly(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """Convenience path for non-mutating actions (search only)."""
    if requires_approval(kind):
        raise ApprovalRequired(f"'{kind}' requires explicit approval.")
    clean, _s, _d = validate(kind, params)
    result = _EXECUTORS[kind](clean)
    _log(kind, clean, True, "ok", "user")
    return {"executed": True, "kind": kind, "result": result}


# ------------------------------------------------------------------ audit log
def _log(kind: str, params: dict, approved: bool, outcome: str, origin: str) -> None:
    try:
        with db.tx() as c:
            c.execute(
                "INSERT INTO action_log (kind, params, approved, outcome, origin,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (kind, json.dumps(params)[:2000], int(approved), outcome, origin, time.time()),
            )
    except Exception:  # noqa: BLE001 — auditing must never break the action
        pass


def recent_log(limit: int = 50) -> list[dict[str, Any]]:
    rows = db.connect().execute(
        "SELECT * FROM action_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["params"] = json.loads(d["params"])
        except Exception:  # noqa: BLE001
            pass
        out.append(d)
    return out


def list_reminders(limit: int = 100) -> list[dict[str, Any]]:
    rows = db.connect().execute(
        "SELECT * FROM reminders ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def delete_reminder(reminder_id: int) -> bool:
    with db.tx() as c:
        return c.execute("DELETE FROM reminders WHERE id=?", (reminder_id,)).rowcount > 0
