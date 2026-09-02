"""
Action-approval safeguards.

`actions._run` is replaced everywhere below so that no test can actually open
a file or a browser. What is asserted is *whether* it would have been called,
and with exactly which argument vector.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app import actions, db, ingest, vectorstore
from app.actions import ActionError, ApprovalRequired


@pytest.fixture
def spy(monkeypatch):
    """Capture argv instead of launching anything."""
    calls: list[list[str]] = []
    monkeypatch.setattr(actions, "_run", lambda argv: calls.append(list(argv)))
    return calls


@pytest.fixture
def indexed_pdf(tmp_docs: Path, fake_embed) -> Path:
    from tests.fixtures import make_pdf

    p = make_pdf(tmp_docs / "indexed.pdf", [["Revenue and risk content."]])
    chunks = ingest.load_and_chunk(p)
    sid = vectorstore.upsert_source(p, "folder", ingest.sha256_file(p), "fake")
    vectorstore.store_chunks(sid, chunks, fake_embed([c.text for c in chunks]))
    return p


# ------------------------------------------------------------- allowlist
def test_only_four_actions_exist():
    assert set(actions.ALLOWED_ACTIONS) == {
        "search_indexed_files", "open_file", "open_url", "draft_reminder",
    }


@pytest.mark.parametrize(
    "kind",
    ["run_shell", "applescript", "read_mail", "keychain_dump", "delete_file",
     "send_email", "exec", "eval", "open_app", ""],
)
def test_unknown_actions_are_rejected(kind: str):
    with pytest.raises(ActionError, match="not an allowed action"):
        actions.validate(kind, {})


def test_mutating_actions_require_approval():
    for kind in ("open_file", "open_url", "draft_reminder"):
        assert actions.requires_approval(kind) is True
    assert actions.requires_approval("search_indexed_files") is False


# --------------------------------------------------- propose ≠ execute
def test_propose_does_not_execute(spy, indexed_pdf: Path):
    proposal = actions.propose("open_file", {"path": str(indexed_pdf)})
    assert proposal["requires_approval"] is True
    assert proposal["token"]
    assert spy == [], "proposing must never touch the machine"


def test_execute_requires_a_valid_token(spy):
    with pytest.raises(ApprovalRequired):
        actions.execute("not-a-real-token", approved=True)
    assert spy == []


def test_denial_does_not_execute(spy, indexed_pdf: Path):
    proposal = actions.propose("open_file", {"path": str(indexed_pdf)})
    result = actions.execute(proposal["token"], approved=False)
    assert result["executed"] is False
    assert result["reason"] == "denied"
    assert spy == []


def test_token_is_single_use(spy, indexed_pdf: Path):
    proposal = actions.propose("open_file", {"path": str(indexed_pdf)})
    assert actions.execute(proposal["token"], approved=True)["executed"] is True
    with pytest.raises(ApprovalRequired):
        actions.execute(proposal["token"], approved=True)
    assert len(spy) == 1, "replaying a token must not act twice"


def test_denied_token_cannot_be_replayed_as_approved(spy, indexed_pdf: Path):
    proposal = actions.propose("open_file", {"path": str(indexed_pdf)})
    actions.execute(proposal["token"], approved=False)
    with pytest.raises(ApprovalRequired):
        actions.execute(proposal["token"], approved=True)
    assert spy == []


def test_expired_token_is_refused(spy, indexed_pdf: Path, monkeypatch):
    proposal = actions.propose("open_file", {"path": str(indexed_pdf)})
    pending = actions._pending[proposal["token"]]
    pending.created_at = time.time() - (actions.TOKEN_TTL_SECONDS + 5)
    with pytest.raises(ApprovalRequired, match="expired"):
        actions.execute(proposal["token"], approved=True)
    assert spy == []


def test_approved_action_runs_exactly_once(spy, indexed_pdf: Path):
    proposal = actions.propose("open_file", {"path": str(indexed_pdf)})
    out = actions.execute(proposal["token"], approved=True)
    assert out["executed"] is True
    assert len(spy) == 1
    assert spy[0][0] == "/usr/bin/open"
    assert spy[0][-1] == str(indexed_pdf.resolve())


# ------------------------------------------------------------ open_file
def test_open_file_refuses_paths_outside_the_index(spy, tmp_docs: Path):
    stray = tmp_docs / "not_indexed.md"
    stray.write_text("secret", encoding="utf-8")
    with pytest.raises(ActionError, match="not in the document index"):
        actions.propose("open_file", {"path": str(stray)})
    assert spy == []


def test_open_file_refuses_nonexistent_paths(spy):
    with pytest.raises(ActionError, match="does not exist"):
        actions.propose("open_file", {"path": "/tmp/definitely-missing-9f3a.md"})
    assert spy == []


@pytest.mark.parametrize(
    "name", ["evil.sh", "evil.app", "evil.command", "evil.py", "evil.dylib"]
)
def test_open_file_refuses_executable_extensions(spy, tmp_docs: Path, name: str):
    p = tmp_docs / name
    p.write_text("#!/bin/sh\necho pwned", encoding="utf-8")
    with pytest.raises(ActionError, match="Refusing to open"):
        actions.propose("open_file", {"path": str(p)})
    assert spy == []


def test_symlink_to_sensitive_file_is_refused(spy, tmp_docs: Path, indexed_pdf: Path):
    """A symlink named like an indexed file must not smuggle another target."""
    secret = tmp_docs / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    link = tmp_docs / "decoy.pdf"
    link.symlink_to(secret)
    with pytest.raises(ActionError):
        actions.propose("open_file", {"path": str(link)})
    assert spy == []


def test_path_traversal_is_refused(spy, tmp_docs: Path):
    with pytest.raises(ActionError):
        actions.propose("open_file", {"path": str(tmp_docs / ".." / ".." / "etc" / "passwd")})
    assert spy == []


def test_removing_a_source_revokes_open_permission(spy, indexed_pdf: Path):
    proposal = actions.propose("open_file", {"path": str(indexed_pdf)})
    src = vectorstore.list_sources()[0]
    vectorstore.remove_source(src["id"])
    # Re-validation happens at execute time, not only at propose time.
    with pytest.raises(ActionError, match="not in the document index"):
        actions.execute(proposal["token"], approved=True)
    assert spy == []


# ------------------------------------------------------------- open_url
@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html,<script>alert(1)</script>",
        "ftp://example.com/x",
        "vnc://192.168.1.5",
        "ssh://root@example.com",
        "x-apple.systempreferences:com.apple.preference",
        "/etc/passwd",
    ],
)
def test_only_http_and_https_urls_are_allowed(spy, url: str):
    with pytest.raises(ActionError):
        actions.propose("open_url", {"url": url})
    assert spy == []


def test_urls_with_credentials_are_refused(spy):
    with pytest.raises(ActionError, match="credentials"):
        actions.propose("open_url", {"url": "https://user:pass@example.com/"})
    assert spy == []


def test_https_url_is_allowed_and_shown_in_full(spy):
    proposal = actions.propose("open_url", {"url": "https://example.com/docs?a=1"})
    assert proposal["params"]["url"] == "https://example.com/docs?a=1"
    assert "https://example.com/docs?a=1" in proposal["summary"]
    assert spy == []
    actions.execute(proposal["token"], approved=True)
    assert spy[0] == ["/usr/bin/open", "--", "https://example.com/docs?a=1"]


def test_plain_http_is_flagged_as_a_warning():
    proposal = actions.propose("open_url", {"url": "http://example.com"})
    assert any("unencrypted" in w for w in proposal["detail"]["warnings"])


def test_punycode_lookalike_host_is_flagged():
    proposal = actions.propose("open_url", {"url": "https://exаmple.com"})  # Cyrillic а
    assert any("non-ASCII" in w for w in proposal["detail"]["warnings"])


def test_shell_metacharacters_are_not_interpreted(spy):
    nasty = "https://example.com/?q=$(whoami)&x=`id`;rm%20-rf"
    proposal = actions.propose("open_url", {"url": nasty})
    actions.execute(proposal["token"], approved=True)
    # Passed as a single argv element; no shell ever sees it.
    assert spy[0] == ["/usr/bin/open", "--", nasty]


# -------------------------------------------------------- draft_reminder
def test_reminder_is_only_a_local_draft(spy):
    proposal = actions.propose("draft_reminder", {"text": "Email the Q3 summary", "due": "Friday"})
    assert "Reminders.app is never touched" not in proposal["summary"]
    out = actions.execute(proposal["token"], approved=True)
    assert out["executed"] is True
    assert spy == [], "drafting must not launch any macOS app"

    rows = actions.list_reminders()
    assert len(rows) == 1
    assert rows[0]["text"] == "Email the Q3 summary"
    assert rows[0]["status"] == "draft"


def test_reminder_requires_approval(spy):
    proposal = actions.propose("draft_reminder", {"text": "Do the thing"})
    assert proposal["requires_approval"] is True
    actions.execute(proposal["token"], approved=False)
    assert actions.list_reminders() == []


def test_control_characters_are_stripped():
    proposal = actions.propose("draft_reminder", {"text": "hello\x00\x07 world"})
    assert proposal["params"]["text"] == "hello world"


def test_empty_reminder_is_rejected():
    with pytest.raises(ActionError, match="cannot be empty"):
        actions.propose("draft_reminder", {"text": "   "})


def test_overlong_input_is_rejected():
    with pytest.raises(ActionError, match="longer than"):
        actions.propose("draft_reminder", {"text": "x" * 501})


# ------------------------------------------------------------ read-only
def test_search_is_read_only_and_needs_no_approval(spy, indexed_pdf: Path):
    out = actions.run_readonly("search_indexed_files", {"query": "revenue"})
    assert out["executed"] is True
    assert "results" in out["result"]
    assert spy == []


def test_readonly_path_refuses_mutating_actions(spy, indexed_pdf: Path):
    with pytest.raises(ApprovalRequired):
        actions.run_readonly("open_file", {"path": str(indexed_pdf)})
    assert spy == []


# ------------------------------------------------- model-originated asks
def test_model_proposals_are_tagged_and_still_need_approval(spy, indexed_pdf: Path):
    proposal = actions.propose(
        "open_url", {"url": "https://evil.test/steal"}, origin="model-proposal"
    )
    assert proposal["origin"] == "model-proposal"
    assert proposal["requires_approval"] is True
    assert spy == [], "a model proposal must never self-execute"


def test_unknown_origin_falls_back_to_user():
    proposal = actions.propose("draft_reminder", {"text": "hi"}, origin="somewhere-else")
    assert proposal["origin"] == "user"


# ----------------------------------------------------------- audit trail
def test_every_outcome_is_logged(spy, indexed_pdf: Path):
    approved = actions.propose("open_file", {"path": str(indexed_pdf)})
    actions.execute(approved["token"], approved=True)
    denied = actions.propose("open_url", {"url": "https://example.com"})
    actions.execute(denied["token"], approved=False)

    log = actions.recent_log()
    outcomes = [e["outcome"] for e in log]
    assert "ok" in outcomes
    assert "denied" in outcomes
    assert outcomes.count("proposed") == 2
    assert {e["kind"] for e in log} == {"open_file", "open_url"}


def test_log_records_the_exact_parameters(spy):
    proposal = actions.propose("open_url", {"url": "https://example.com/x"})
    actions.execute(proposal["token"], approved=True)
    entry = next(e for e in actions.recent_log() if e["outcome"] == "ok")
    assert entry["params"]["url"] == "https://example.com/x"


def test_wipe_clears_the_audit_log_and_reminders(spy):
    p = actions.propose("draft_reminder", {"text": "keep me"})
    actions.execute(p["token"], approved=True)
    assert actions.list_reminders()
    db.delete_all_data()
    assert actions.list_reminders() == []
    assert actions.recent_log() == []
