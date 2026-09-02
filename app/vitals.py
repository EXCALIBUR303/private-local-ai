"""
Live machine vitals.

Everything here is measured, never estimated. The interface leans on these
numbers to make "this is running on your Mac" something you can watch rather
than a claim you have to take on trust.
"""
from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from . import config
from .guards import assert_loopback

_mem_cache: dict[str, Any] = {"at": 0.0, "value": None}
MEM_TTL = 4.0


def _host_memory() -> dict[str, Any]:
    """Physical memory and free share, from vm_stat. Cached briefly."""
    now = time.time()
    if _mem_cache["value"] is not None and now - _mem_cache["at"] < MEM_TTL:
        return _mem_cache["value"]

    total = 0
    try:
        total = int(
            subprocess.run(
                ["/usr/sbin/sysctl", "-n", "hw.memsize"],
                capture_output=True, timeout=3, check=False,
            ).stdout.decode().strip()
            or 0
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        total = 0

    free_pct = None
    try:
        out = subprocess.run(
            ["/usr/bin/vm_stat"], capture_output=True, timeout=3, check=False
        ).stdout.decode()
        page = 4096
        m = re.search(r"page size of (\d+) bytes", out)
        if m:
            page = int(m.group(1))
        stats = {
            k.strip(): int(v.strip().rstrip("."))
            for k, v in (
                line.split(":", 1) for line in out.splitlines() if ":" in line
            )
            if v.strip().rstrip(".").isdigit()
        }
        free_pages = stats.get("Pages free", 0) + stats.get("Pages inactive", 0)
        if total:
            free_pct = round(100.0 * free_pages * page / total, 1)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        free_pct = None

    value = {"total_bytes": total, "free_percent": free_pct}
    _mem_cache.update(at=now, value=value)
    return value


async def loaded_models() -> list[dict[str, Any]]:
    """Which models the local runtime currently holds in memory."""
    host = config.OLLAMA_HOST
    assert_loopback(host)
    url = f"http://{host}".rstrip("/") if not host.startswith("http") else host
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(f"{url}/api/ps")
            r.raise_for_status()
            data = r.json()
    except Exception:  # noqa: BLE001 — vitals must never break the page
        return []

    out = []
    for m in data.get("models", []):
        expires_in = None
        raw = m.get("expires_at")
        if raw:
            try:
                when = datetime.fromisoformat(raw)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                expires_in = max(
                    0, int((when - datetime.now(timezone.utc)).total_seconds())
                )
            except ValueError:
                expires_in = None
        details = m.get("details") or {}
        out.append(
            {
                "name": m.get("name"),
                "bytes": m.get("size_vram") or m.get("size") or 0,
                "context_length": m.get("context_length"),
                "parameters": details.get("parameter_size"),
                "quantization": details.get("quantization_level"),
                "unloads_in": expires_in,
            }
        )
    return out


async def snapshot() -> dict[str, Any]:
    return {"loaded": await loaded_models(), "memory": _host_memory()}
