"""
Text-to-speech using the macOS `say` command.

Text is passed on stdin, never interpolated into a shell string, so no input
can turn into a command. One utterance at a time; starting a new one stops
the previous.
"""
from __future__ import annotations

import re
import subprocess
import threading
from typing import Any

from . import config

_proc: subprocess.Popen | None = None
_lock = threading.Lock()
_piper_voice = None          # lazily loaded PiperVoice
_piper_lock = threading.Lock()

MAX_CHARS = 8000
_VOICE_OK = re.compile(r"^[A-Za-z][A-Za-z0-9 ()._-]{0,40}$")


def available() -> bool:
    from pathlib import Path
    return Path(config.SAY_BIN).exists()


# ─────────────────────────── neural voice ───────────────────────────
def piper_available() -> bool:
    """A local Piper voice gives a far better result than `say`."""
    try:
        import piper  # noqa: F401
    except ImportError:
        return False
    return config.PIPER_VOICE.exists()


def _load_piper():
    global _piper_voice
    with _piper_lock:
        if _piper_voice is None:
            from piper import PiperVoice
            _piper_voice = PiperVoice.load(str(config.PIPER_VOICE))
        return _piper_voice


def engines() -> dict[str, Any]:
    return {
        "piper": {
            "available": piper_available(),
            "voice": config.PIPER_VOICE.stem if config.PIPER_VOICE.exists() else None,
            "quality": "neural",
        },
        "say": {"available": available(), "quality": "system"},
    }


def _speak_piper(text: str) -> dict[str, Any]:
    """Synthesise to a WAV in the data folder, then play it with afplay."""
    global _proc
    import wave

    voice = _load_piper()
    config.ensure_dirs()
    out = config.TMP_DIR / "speech.wav"
    with wave.open(str(out), "wb") as w:
        voice.synthesize_wav(text, w)

    stop()
    with _lock:
        _proc = subprocess.Popen(
            [config.AFPLAY_BIN, str(out)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
        )
    return {"speaking": True, "characters": len(text),
            "voice": config.PIPER_VOICE.stem, "engine": "piper"}


def list_voices(limit: int = 60) -> list[dict[str, str]]:
    if not available():
        return []
    try:
        out = subprocess.run(
            [config.SAY_BIN, "-v", "?"], capture_output=True, timeout=10, check=False
        ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return []
    voices: list[dict[str, str]] = []
    for line in out.splitlines():
        m = re.match(r"^(.+?)\s{2,}([a-z]{2}[-_][A-Z]{2})\s*#?\s*(.*)$", line.strip())
        if m:
            voices.append(
                {"name": m.group(1).strip(), "locale": m.group(2), "sample": m.group(3)}
            )
    return voices[:limit]


def stop() -> None:
    global _proc
    with _lock:
        if _proc and _proc.poll() is None:
            _proc.terminate()
            try:
                _proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _proc.kill()
        _proc = None


def speak(
    text: str,
    voice: str | None = None,
    rate: int | None = None,
    engine: str | None = None,
) -> dict[str, Any]:
    """
    Speak `text` aloud. Returns immediately; audio plays in the background.

    Prefers the local Piper neural voice when one is installed — it is what
    makes the assistant sound like an assistant rather than a 2005 screen
    reader — and falls back to macOS `say`.
    """
    global _proc
    clean = (text or "").replace("\x00", "").strip()
    if not clean:
        raise ValueError("Nothing to speak.")
    if len(clean) > MAX_CHARS:
        clean = clean[:MAX_CHARS] + "…"

    want = engine or ("piper" if piper_available() else "say")
    if want == "piper" and piper_available():
        try:
            return _speak_piper(clean)
        except Exception:  # noqa: BLE001 — never lose the voice entirely
            pass

    if not available():
        raise RuntimeError("No speech engine is available.")

    argv = [config.SAY_BIN]
    if voice:
        if not _VOICE_OK.match(voice):
            raise ValueError("That voice name is not valid.")
        argv += ["-v", voice]
    if rate:
        argv += ["-r", str(max(90, min(400, int(rate))))]

    stop()
    with _lock:
        _proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        assert _proc.stdin is not None
        _proc.stdin.write(clean.encode("utf-8"))
        _proc.stdin.close()
    return {"speaking": True, "characters": len(clean), "voice": voice or "system default"}


def play_file(path) -> dict[str, Any]:
    """Play a WAV that something else synthesised (e.g. a cloned voice)."""
    global _proc
    stop()
    with _lock:
        _proc = subprocess.Popen(
            [config.AFPLAY_BIN, str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
        )
    return {"speaking": True, "file": str(path)}


def is_speaking() -> bool:
    with _lock:
        return _proc is not None and _proc.poll() is None
