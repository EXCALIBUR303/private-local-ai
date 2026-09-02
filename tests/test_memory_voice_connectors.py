"""Self-learning memory, cloned voices, and the app connectors."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app import connectors, memory, power, voiceclone
from app.power import Risk


@pytest.fixture(autouse=True)
def _init_tables():
    memory.init()
    voiceclone.init()


# ───────────────────────────── memory ─────────────────────────────
def test_learning_is_on_by_default_and_can_be_switched_off():
    """
    The operator asked for an assistant that learns, so this ships on. What
    matters then is that it stays visible and reversible: the setting is
    readable, switchable, and everything learned can be listed and deleted.
    """
    assert memory.enabled() is True
    memory.set_enabled(False)
    assert memory.enabled() is False
    memory.set_enabled(True)


def test_learning_only_reads_the_operators_own_words():
    """A document must never be able to write itself into long-term memory."""
    import inspect

    from app import main
    source = inspect.getsource(main.chat)
    assert "if not req.use_documents:" in source
    assert "learn_in_background" in source


def test_learning_toggle_round_trips():
    memory.set_enabled(True)
    assert memory.enabled() is True
    memory.set_enabled(False)
    assert memory.enabled() is False


def test_remember_and_forget():
    r = asyncio.run(memory.remember("The operator writes Rust.", source="test"))
    assert r["stored"] is True
    assert memory.count() == 1
    assert memory.forget(r["id"]) is True
    assert memory.count() == 0


def test_blank_memory_is_rejected():
    with pytest.raises(ValueError):
        asyncio.run(memory.remember("   "))


def test_memory_text_is_bounded():
    r = asyncio.run(memory.remember("x " * 500, source="test"))
    stored = memory.list_memories()[0]["text"]
    assert len(stored) <= 400


def test_learning_disabled_means_nothing_is_learned():
    memory.set_enabled(False)
    learned = asyncio.run(memory.learn_from_exchange("I prefer tabs.", "Noted."))
    assert learned == []
    assert memory.count() == 0


def test_forget_all():
    asyncio.run(memory.remember("fact one", source="test"))
    asyncio.run(memory.remember("a completely different statement", source="test"))
    assert memory.forget_all() >= 1
    assert memory.count() == 0


def test_recall_returns_nothing_on_an_empty_store():
    assert asyncio.run(memory.recall("anything")) == []


def test_prompt_block_is_empty_without_hits():
    assert memory.as_prompt_block([]) == ""
    block = memory.as_prompt_block([{"text": "Writes Rust."}])
    assert "Writes Rust." in block
    assert "Do not recite it back" in block


# ─────────────────────────── voice cloning ───────────────────────────
def test_short_samples_are_rejected(tmp_path: Path):
    """A two-second clip produces a bad clone; better to refuse it."""
    import struct, wave
    short = tmp_path / "short.wav"
    with wave.open(str(short), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", 0) for _ in range(16000)))  # 1s
    with pytest.raises(ValueError, match="at least"):
        voiceclone.add_voice("TooShort", short.read_bytes())


def test_unknown_voice_cannot_be_synthesised():
    with pytest.raises(RuntimeError):
        voiceclone.synthesize("hello", "no-such-voice")


def test_voice_name_is_sanitised(tmp_path: Path):
    import struct, wave
    sample = tmp_path / "ok.wav"
    with wave.open(str(sample), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", 0) for _ in range(16000 * 6)))
    r = voiceclone.add_voice("../../etc/passwd", sample.read_bytes())
    assert "/" not in r["name"] and ".." not in r["name"]
    assert Path(r["path"]).parent == voiceclone.VOICES_DIR
    voiceclone.delete_voice(r["name"])


def test_status_reports_the_watermark_policy():
    note = voiceclone.status()["note"]
    assert "watermark" in note.lower()
    assert "permission" in note.lower()


# ─────────────────────────── connectors ───────────────────────────
def test_every_connector_declares_an_app_and_risk():
    for name, c in connectors.CONNECTORS.items():
        assert c.app, name
        assert c.risk in (Risk.READ, Risk.WRITE, Risk.DESTRUCTIVE), name


def test_sending_a_message_is_destructive():
    """It leaves the machine, so it must stop for a human below full autonomy."""
    assert connectors.CONNECTORS["messages_send"].risk == Risk.DESTRUCTIVE


def test_mail_draft_does_not_send():
    assert "never sends" in connectors.CONNECTORS["mail_draft"].summary.lower()
    assert connectors.CONNECTORS["mail_draft"].risk == Risk.WRITE


def test_reading_connectors_are_read_risk():
    for name in ("mail_unread", "calendar_events", "safari_tabs",
                 "reminders_list", "finder_selection", "now_playing"):
        assert connectors.CONNECTORS[name].risk == Risk.READ, name


def test_applescript_strings_are_quoted_safely():
    from app.connectors import _q
    assert _q('say "hi"') == '"say \\"hi\\""'
    assert '\\\\' in _q("back\\slash")


def test_connectors_join_the_agent_toolset():
    power.register_connectors()
    for name in connectors.CONNECTORS:
        assert name in power.TOOLS
    assert len(power.ollama_tool_schema()) == len(power.TOOLS)


def test_permission_hint_is_actionable():
    hint = connectors.permission_hint("Error -1743: not authorized to send Apple events")
    assert "Privacy & Security" in hint
