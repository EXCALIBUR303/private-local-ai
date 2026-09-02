"""
HTTP-layer privacy guarantees.

These run against the real ASGI app via TestClient. They do not need Ollama:
endpoints that require the model runtime are not exercised here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.guards import PrivacyViolation, assert_local_model, assert_loopback
from app.main import DELETE_PHRASE, app

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

#: Any absolute URL appearing in a frontend file.
URL_RE = re.compile(r'https?://[^\s\'"`)>\]]+')


@pytest.fixture
def client():
    # base_url matters: the app refuses any Host header that is not loopback,
    # and TestClient's default ("testserver") is correctly rejected.
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        yield c


# ------------------------------------------------------------ local only
def test_non_loopback_host_header_is_refused(client):
    r = client.get("/api/privacy", headers={"Host": "evil.example.com"})
    assert r.status_code == 421
    assert "localhost" in r.json()["detail"]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "127.0.0.1:8765"])
def test_loopback_hosts_are_accepted(client, host: str):
    assert client.get("/api/privacy", headers={"Host": host}).status_code == 200


def test_cross_origin_requests_are_refused(client):
    r = client.get("/api/privacy", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_security_headers_forbid_remote_resources(client):
    csp = client.get("/api/privacy").headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


def test_interactive_api_docs_are_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# --------------------------------------------------------- guard module
@pytest.mark.parametrize(
    "host", ["10.0.0.5:11434", "example.com:11434", "192.168.1.9", "8.8.8.8"]
)
def test_guard_blocks_remote_runtimes(host: str):
    with pytest.raises(PrivacyViolation):
        assert_loopback(host)


@pytest.mark.parametrize("host", ["127.0.0.1:11434", "localhost:11434", "[::1]:11434"])
def test_guard_allows_loopback(host: str):
    assert_loopback(host)


@pytest.mark.parametrize(
    "model", ["gpt-oss:120b-cloud", "qwen3-cloud", "someuser/private-model"]
)
def test_guard_blocks_hosted_models(model: str):
    with pytest.raises(PrivacyViolation):
        assert_local_model(model)


def test_guard_blocks_models_not_on_disk():
    with pytest.raises(PrivacyViolation, match="not downloaded locally"):
        assert_local_model("mystery:70b", ["qwen3:8b"])


def test_guard_accepts_a_present_local_model():
    assert_local_model("qwen3:8b", ["qwen3:8b", "nomic-embed-text:latest"])


# ------------------------------------------------- frontend has no CDNs
def test_frontend_loads_no_external_resources():
    """
    No asset may be fetched from off-machine. XML namespace URIs (w3.org) are
    identifiers, not requests, so they are allowed; everything else is not.
    """
    resource_ref = re.compile(
        r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//"""      # <script src="//cdn...">
        r"""|url\(\s*["']?\s*(?:https?:)?//"""               # css url(//cdn...)
        r"""|@import"""                                        # css @import
        r"""|fonts\.googleapis|fonts\.gstatic|cdn\.|unpkg|jsdelivr|cdnjs""",
        re.I,
    )
    for name in ("index.html", "app.css", "app.js"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert not resource_ref.search(text), f"{name} loads an external resource"


def test_frontend_contains_no_network_origins_beyond_namespaces():
    allowed = ("http://www.w3.org/",)          # SVG/XML namespace identifiers only
    for name in ("index.html", "app.css", "app.js"):
        text = (STATIC / name).read_text(encoding="utf-8")
        for m in re.finditer(URL_RE, text):
            url = m.group(0)
            if url.startswith(allowed):
                continue
            # Placeholder text shown to the user in form fields is not a fetch.
            assert "example.com" in url, f"{name} mentions external origin {url}"


def test_frontend_has_no_inline_script_blocks():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    # Only the external <script src="..."> tag is permitted by the CSP.
    for match in re.finditer(r"<script([^>]*)>(.*?)</script>", html, re.S | re.I):
        assert "src=" in match.group(1), "inline scripts violate the CSP"
        assert match.group(2).strip() == ""


def test_no_remote_font_or_import_rules():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "@import" not in css
    assert "@font-face" not in css


# ---------------------------------------------------------- delete-all
def test_delete_all_requires_the_exact_phrase(client):
    for wrong in ["", "delete", "DELETE ALL MY DATA", DELETE_PHRASE.lower()]:
        r = client.post("/api/privacy/delete_all", json={"confirm_phrase": wrong})
        assert r.status_code == 400
        assert DELETE_PHRASE in r.json()["detail"]


def test_delete_all_succeeds_with_the_exact_phrase(client):
    r = client.post("/api/privacy/delete_all", json={"confirm_phrase": DELETE_PHRASE})
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is True
    assert "rows_removed" in body


def test_privacy_report_lists_the_data_directory(client):
    body = client.get("/api/privacy").json()
    assert body["data_dir"] == str(config.DATA_DIR)
    assert any("127.0.0.1" in g for g in body["guarantees"])
    assert body["delete_phrase"] == DELETE_PHRASE


# ----------------------------------------------------------- documents
def test_preview_refuses_private_folders(client):
    r = client.post("/api/documents/preview", json={"path": str(Path.home())})
    assert r.status_code == 403
    assert "Refusing to index" in r.json()["detail"]


def test_preview_refuses_cloud_folders(client):
    r = client.post(
        "/api/documents/preview",
        json={"path": str(Path.home() / "Library" / "Mobile Documents")},
    )
    assert r.status_code == 403


def test_upload_rejects_unsupported_types(client):
    r = client.post(
        "/api/documents/upload",
        files={"files": ("photo.png", b"\x89PNG\r\n", "image/png")},
    )
    assert r.status_code == 200
    assert r.json()["failed"]
    assert r.json()["indexed"] == []


def test_action_catalog_advertises_only_four(client):
    body = client.get("/api/actions").json()
    assert len(body["allowed"]) == 4
    assert "no shell" in body["policy"].lower()


def test_action_execute_rejects_unknown_token(client):
    r = client.post(
        "/api/actions/execute", json={"token": "x" * 40, "approved": True}
    )
    assert r.status_code == 403


def test_propose_rejects_actions_outside_the_allowlist(client):
    r = client.post(
        "/api/actions/propose", json={"kind": "run_shell", "params": {"cmd": "ls"}}
    )
    assert r.status_code == 422        # rejected by the schema before any logic


# ─────────────────────────── live vitals ───────────────────────────
def test_vitals_endpoint_shape(client):
    """Vitals must always answer, even when the runtime is unreachable."""
    body = client.get("/api/vitals").json()
    assert "loaded" in body and isinstance(body["loaded"], list)
    assert "memory" in body
    assert set(body["memory"]) == {"total_bytes", "free_percent"}


def test_vitals_reports_real_host_memory(client):
    mem = client.get("/api/vitals").json()["memory"]
    # This Mac has physical memory; the value must be plausible, not invented.
    assert mem["total_bytes"] > 1_000_000_000
    if mem["free_percent"] is not None:
        assert 0 <= mem["free_percent"] <= 100


def test_vitals_refuses_a_non_loopback_runtime(monkeypatch):
    """The vitals poller must obey the same loopback guard as everything else."""
    import asyncio

    from app import config, vitals

    monkeypatch.setattr(config, "OLLAMA_HOST", "10.0.0.5:11434")
    with pytest.raises(PrivacyViolation):
        asyncio.run(vitals.loaded_models())


def test_vitals_survives_an_unreachable_runtime(monkeypatch):
    import asyncio

    from app import config, vitals

    # A closed loopback port: allowed by the guard, but nothing is listening.
    monkeypatch.setattr(config, "OLLAMA_HOST", "127.0.0.1:1")
    assert asyncio.run(vitals.loaded_models()) == []


# ───────────────────── interface contract ─────────────────────
def test_interface_declares_no_inline_event_handlers():
    """CSP forbids inline script; on* attributes would silently break."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)


def test_interface_assets_are_plain_text():
    """A stray NUL byte makes an asset parse as binary."""
    for name in ("index.html", "app.css", "app.js"):
        assert b"\x00" not in (STATIC / name).read_bytes(), f"{name} contains NUL bytes"
