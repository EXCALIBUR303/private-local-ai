"""
Voice cloning.

Feed the assistant a short recording and it speaks in that voice, using
Chatterbox (Resemble AI, MIT) running locally on the Metal GPU. Nothing is
uploaded; the reference audio and the generated speech both stay on this Mac.

Deliberate choices:

  * Optional. Chatterbox pulls ~1.5 GB of PyTorch and roughly 1 GB of weights,
    and holding it in memory alongside an 8 B language model is tight on 16 GB.
    It installs into its own environment via scripts/install_voice_cloning.sh
    and is invoked as a subprocess, so the main app stays light and a crash in
    the cloner cannot take the server down.
  * The synthetic-audio watermark stays on. Chatterbox embeds an inaudible
    Perth watermark so generated speech can be identified as synthetic. It is
    not disabled here and should not be.
  * Consent is the operator's responsibility. Cloning your own voice, or one
    you have permission to use, is the intended case. Imitating someone else
    to deceive is not, and no amount of local-only processing changes that.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import wave
from pathlib import Path
from typing import Any

from . import config, db

VOICES_DIR = config.MODELS_DIR / "voices"
CLONE_ENV = config.DATA_DIR / "voiceclone-venv"
WORKER = Path(__file__).parent / "voiceclone_worker.py"

MIN_SECONDS = 4.0
MAX_SECONDS = 45.0
GENERATION_TIMEOUT = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS voices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    sample     TEXT    NOT NULL,
    seconds    REAL,
    created_at REAL    NOT NULL
);
"""


def init() -> None:
    conn = db.connect()
    conn.executescript(SCHEMA)
    conn.commit()
    VOICES_DIR.mkdir(parents=True, exist_ok=True)


def python_bin() -> Path:
    return CLONE_ENV / "bin" / "python"


def available() -> bool:
    return python_bin().exists() and WORKER.exists()


def status() -> dict[str, Any]:
    return {
        "available": available(),
        "env": str(CLONE_ENV),
        "voices": list_voices(),
        "install_hint": "./scripts/install_voice_cloning.sh",
        "note": (
            "Runs locally on the GPU. Generated speech carries an inaudible "
            "watermark identifying it as synthetic. Clone your own voice, or "
            "one you have permission to use."
        ),
    }


# ─────────────────────────────── profiles ───────────────────────────────
def _duration(path: Path) -> float:
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (wave.Error, OSError):
        return 0.0


def add_voice(name: str, wav_bytes: bytes) -> dict[str, Any]:
    """Store a reference recording under a name."""
    init()
    clean = "".join(ch for ch in name if ch.isalnum() or ch in " -_")[:40].strip()
    if not clean:
        raise ValueError("Give the voice a name.")

    dest = VOICES_DIR / f"{clean.replace(' ', '_')}.wav"
    dest.write_bytes(wav_bytes)
    seconds = _duration(dest)

    if seconds < MIN_SECONDS:
        dest.unlink(missing_ok=True)
        raise ValueError(
            f"That sample is {seconds:.1f}s. Record at least {MIN_SECONDS:.0f}s "
            "of natural speech for a usable clone."
        )
    if seconds > MAX_SECONDS:
        dest.unlink(missing_ok=True)
        raise ValueError(f"That sample is {seconds:.0f}s; keep it under {MAX_SECONDS:.0f}s.")

    with db.tx() as c:
        c.execute(
            "INSERT INTO voices (name, sample, seconds, created_at) VALUES (?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET sample=excluded.sample,"
            " seconds=excluded.seconds, created_at=excluded.created_at",
            (clean, str(dest), seconds, time.time()),
        )
    return {"name": clean, "seconds": round(seconds, 2), "path": str(dest)}


def list_voices() -> list[dict[str, Any]]:
    try:
        rows = db.connect().execute(
            "SELECT id, name, sample, seconds, created_at FROM voices ORDER BY created_at DESC"
        ).fetchall()
    except Exception:  # noqa: BLE001 — table may not exist yet
        return []
    return [dict(r) for r in rows if Path(r["sample"]).exists()]


def get_voice(name: str) -> dict[str, Any] | None:
    row = db.connect().execute("SELECT * FROM voices WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def delete_voice(name: str) -> bool:
    voice = get_voice(name)
    if not voice:
        return False
    Path(voice["sample"]).unlink(missing_ok=True)
    with db.tx() as c:
        c.execute("DELETE FROM voices WHERE name=?", (name,))
    return True


# ────────────────────────────── synthesis ──────────────────────────────
def synthesize(text: str, voice_name: str, out_path: Path | None = None) -> Path:
    """
    Speak `text` in the cloned voice. Runs the model in its own interpreter so
    a failure there cannot bring down the server.
    """
    if not available():
        raise RuntimeError(
            "Voice cloning is not installed. Run ./scripts/install_voice_cloning.sh"
        )
    voice = get_voice(voice_name)
    if not voice:
        raise RuntimeError(f"No voice named {voice_name!r}.")

    config.ensure_dirs()
    dest = out_path or (config.TMP_DIR / "cloned_speech.wav")
    payload = json.dumps({
        "text": text[:1200],
        "reference": voice["sample"],
        "out": str(dest),
    })

    proc = subprocess.run(
        [str(python_bin()), str(WORKER)],
        input=payload.encode("utf-8"),
        capture_output=True, timeout=GENERATION_TIMEOUT, check=False,
    )
    if proc.returncode != 0 or not dest.exists():
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = tail[-1] if tail else "no output"
        raise RuntimeError(f"Cloning failed: {detail[:300]}")
    return dest


def uninstall() -> dict[str, Any]:
    """Remove the cloning environment; voice samples are kept."""
    if CLONE_ENV.exists():
        shutil.rmtree(CLONE_ENV, ignore_errors=True)
    return {"removed": True}
