"""
The single door out of this machine.

Everything in this application is local. Watching Vercel, Railway or GitHub is
not — those are other people's servers, and reaching them is a real departure
from "nothing leaves this Mac". So rather than sprinkling `httpx.get` through
the codebase, every outbound request is funnelled through this one module,
which enforces four properties:

  1. **Off by default.** Egress stays disabled until the operator turns it on.
  2. **Allowlisted hosts.** A provider may only reach the hosts it declared.
     A compromised token or a confused model cannot redirect a poll somewhere
     else, because the destination is checked against the registry, not the
     argument.
  3. **Status only.** These calls fetch deployment and build state. Prompts,
     conversations, documents and memories are never given to this module —
     `fetch` has no parameter that could carry them, and request bodies are
     limited to the small JSON queries the providers require.
  4. **Written down.** Every request is logged with host, path, status and
     size, so "what left my machine?" has an answer you can read.

Credentials live in the local database and are never returned by the API; the
interface only ever sees a masked hint like `vrc_…8f21`.
"""
from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from . import db

#: Requests hang, machines sleep, Wi-Fi drops. Keep it short.
TIMEOUT = 20.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

SETTING_KEY = "egress_enabled"


class EgressBlocked(RuntimeError):
    """An outbound request was refused. The message is safe to display."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS egress_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    provider   TEXT    NOT NULL,
    host       TEXT    NOT NULL,
    path       TEXT    NOT NULL,
    status     INTEGER,
    bytes      INTEGER NOT NULL DEFAULT 0,
    detail     TEXT,
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_egress_time ON egress_log(created_at DESC);

CREATE TABLE IF NOT EXISTS secrets (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def init() -> None:
    conn = db.connect()
    conn.executescript(SCHEMA)
    conn.commit()


# ------------------------------------------------------------------ switch
def enabled() -> bool:
    return db.get_setting(SETTING_KEY, "0") == "1"


def set_enabled(on: bool) -> bool:
    db.set_setting(SETTING_KEY, "1" if on else "0")
    return on


# ------------------------------------------------------------- credentials
def put_secret(name: str, value: str) -> None:
    if not value or not value.strip():
        raise EgressBlocked("An empty credential is not a credential.")
    with db.tx() as c:
        c.execute(
            "INSERT INTO secrets (name, value, created_at) VALUES (?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET value=excluded.value,"
            " created_at=excluded.created_at",
            (name, value.strip(), time.time()),
        )


def get_secret(name: str) -> str | None:
    row = db.connect().execute(
        "SELECT value FROM secrets WHERE name=?", (name,)
    ).fetchone()
    return row["value"] if row else None


def drop_secret(name: str) -> bool:
    with db.tx() as c:
        return c.execute("DELETE FROM secrets WHERE name=?", (name,)).rowcount > 0


def mask(value: str | None) -> str | None:
    """`vrc_9f2b…8f21` — enough to recognise, not enough to reuse."""
    if not value:
        return None
    v = value.strip()
    if len(v) <= 10:
        return "…" * 3
    return f"{v[:4]}…{v[-4:]}"


def secret_hints() -> dict[str, str]:
    rows = db.connect().execute("SELECT name, value FROM secrets").fetchall()
    return {r["name"]: mask(r["value"]) or "" for r in rows}


# ----------------------------------------------------------------- the door
def _check(provider: str, url: str, allowed_hosts: tuple[str, ...]) -> tuple[str, str]:
    if not enabled():
        raise EgressBlocked(
            "Outbound network access is switched off. Turn on 'Reach the "
            "internet for watchers' in Connectors to let this poll run."
        )
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise EgressBlocked(f"Refusing a non-HTTPS request to {url!r}.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise EgressBlocked(f"Could not read a hostname from {url!r}.")
    ok = any(host == h or host.endswith("." + h) for h in allowed_hosts)
    if not ok:
        raise EgressBlocked(
            f"{provider} may only reach {', '.join(allowed_hosts)} — "
            f"refused a request to {host}."
        )
    return host, parsed.path or "/"


def _log(provider: str, host: str, path: str, status: int | None,
         size: int, detail: str = "") -> None:
    try:
        with db.tx() as c:
            c.execute(
                "INSERT INTO egress_log (provider, host, path, status, bytes,"
                " detail, created_at) VALUES (?,?,?,?,?,?,?)",
                (provider, host, path[:200], status, size, detail[:200], time.time()),
            )
    except Exception:  # noqa: BLE001 — logging must not break a poll
        pass


async def fetch(
    provider: str,
    url: str,
    allowed_hosts: tuple[str, ...],
    headers: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    method: str = "GET",
) -> Any:
    """
    Perform one allowlisted outbound request and return parsed JSON.

    `query` is the only way a caller can put data into the request, and it is
    limited to the small structured queries providers need (a GraphQL document,
    a project id). There is deliberately no parameter through which prompt,
    document or conversation text could travel.
    """
    host, path = _check(provider, url, allowed_hosts)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            response = await client.request(
                method.upper(), url,
                headers={"accept": "application/json",
                         "user-agent": "PrivateLocalAI/watcher",
                         **(headers or {})},
                json=query if method.upper() != "GET" else None,
                params=query if method.upper() == "GET" else None,
            )
    except httpx.HTTPError as exc:
        _log(provider, host, path, None, 0, f"{type(exc).__name__}: {exc}")
        raise EgressBlocked(f"Could not reach {host}: {exc}") from exc

    body = response.content[:MAX_RESPONSE_BYTES]
    _log(provider, host, path, response.status_code, len(body),
         f"{(time.perf_counter() - started) * 1000:.0f}ms")

    if response.status_code == 401 or response.status_code == 403:
        raise EgressBlocked(
            f"{provider} rejected the token ({response.status_code}). "
            "Check that it is still valid and has the right scope."
        )
    if response.status_code >= 400:
        raise EgressBlocked(
            f"{provider} returned {response.status_code}: "
            f"{body[:180].decode('utf-8', 'replace')}"
        )
    try:
        return json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise EgressBlocked(f"{provider} did not return JSON.") from exc


def recent(limit: int = 60) -> list[dict[str, Any]]:
    rows = db.connect().execute(
        "SELECT * FROM egress_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def summary() -> dict[str, Any]:
    row = db.connect().execute(
        "SELECT COUNT(*) AS n, MAX(created_at) AS last,"
        " COALESCE(SUM(bytes),0) AS total FROM egress_log"
    ).fetchone()
    hosts = [
        r["host"] for r in db.connect().execute(
            "SELECT DISTINCT host FROM egress_log ORDER BY host"
        ).fetchall()
    ]
    return {
        "enabled": enabled(),
        "requests": int(row["n"] or 0),
        "last_at": row["last"],
        "bytes": int(row["total"] or 0),
        "hosts": hosts,
    }
