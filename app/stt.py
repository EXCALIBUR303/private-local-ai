"""
Local speech-to-text via whisper.cpp.

Audio is written to a scratch file inside the app's data directory, handed to
the `whisper-cli` binary, and deleted immediately afterwards. Nothing is sent
anywhere. The browser records raw PCM and encodes the WAV itself, so there is
no ffmpeg dependency and no intermediate transcoding service.
"""
from __future__ import annotations

import os
import struct
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from . import config


class SpeechError(RuntimeError):
    pass


def availability() -> dict[str, Any]:
    binary = Path(config.WHISPER_BIN)
    model = Path(config.WHISPER_MODEL)
    have_bin = binary.exists() and os.access(binary, os.X_OK)
    have_model = model.exists() and model.stat().st_size > 1_000_000
    reasons = []
    if not have_bin:
        reasons.append(f"whisper-cli not found at {binary} (brew install whisper-cpp)")
    if not have_model:
        reasons.append(f"speech model missing at {model} (re-run ./setup.sh)")
    return {
        "available": have_bin and have_model,
        "binary": str(binary),
        "model": str(model),
        "model_size": model.stat().st_size if have_model else 0,
        "reasons": reasons,
    }


def _validate_wav(data: bytes) -> tuple[int, int]:
    """Confirm a RIFF/WAVE container and return (sample_rate, channels)."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise SpeechError("Audio must be a WAV (RIFF) file.")
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4 : pos + 8])
        if chunk_id == b"fmt ":
            if pos + 8 + 16 > len(data):
                break
            fmt = struct.unpack("<HHIIHH", data[pos + 8 : pos + 8 + 16])
            channels, rate = fmt[1], fmt[2]
            return int(rate), int(channels)
        pos += 8 + size + (size & 1)
    raise SpeechError("WAV file has no format header.")


def transcribe(wav_bytes: bytes, language: str = "en") -> dict[str, Any]:
    """Transcribe WAV audio. Returns {'text': ..., 'meta': {...}}."""
    status = availability()
    if not status["available"]:
        raise SpeechError("; ".join(status["reasons"]) or "Speech-to-text unavailable.")
    if not wav_bytes:
        raise SpeechError("No audio was received.")
    if len(wav_bytes) > config.MAX_AUDIO_BYTES:
        raise SpeechError("That recording is too large.")

    rate, channels = _validate_wav(wav_bytes)
    config.ensure_dirs()

    work = Path(tempfile.mkdtemp(prefix="stt-", dir=config.TMP_DIR))
    wav_path = work / f"{uuid.uuid4().hex}.wav"
    out_prefix = work / "out"
    try:
        wav_path.write_bytes(wav_bytes)
        argv = [
            str(config.WHISPER_BIN),
            "-m", str(config.WHISPER_MODEL),
            "-f", str(wav_path),
            "-l", language if language.isalpha() and len(language) <= 5 else "en",
            "-np",            # suppress the ggml banner
            "-nt",            # no timestamps
            "-otxt",
            "-of", str(out_prefix),
            "-t", str(max(1, min(8, (os.cpu_count() or 4)))),
        ]
        proc = subprocess.run(
            argv, shell=False, capture_output=True, timeout=300, check=False
        )
        txt_file = out_prefix.with_suffix(".txt")
        if not txt_file.exists():
            err = proc.stderr.decode("utf-8", "replace")
            tail = "\n".join(
                ln for ln in err.splitlines()
                if not ln.startswith(("ggml_", "load_backend", "whisper_"))
            )[-400:]
            raise SpeechError(f"Transcription failed: {tail or 'no output produced'}")

        text = txt_file.read_text(encoding="utf-8", errors="replace").strip()
        text = " ".join(text.split())
        # whisper emits bracketed markers for non-speech audio.
        if text in ("[BLANK_AUDIO]", "[ Silence ]", "(silence)"):
            text = ""
        return {
            "text": text,
            "meta": {
                "sample_rate": rate,
                "channels": channels,
                "bytes": len(wav_bytes),
                "model": Path(config.WHISPER_MODEL).name,
                "engine": "whisper.cpp (local)",
            },
        }
    except subprocess.TimeoutExpired as exc:
        raise SpeechError("Transcription timed out.") from exc
    finally:
        for p in (wav_path, out_prefix.with_suffix(".txt")):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            work.rmdir()
        except OSError:
            pass
