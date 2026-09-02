"""
Central configuration.

Every path used by this application resolves under a single, clearly named
local data directory so that "where is my data?" has exactly one answer.
Nothing here points at a remote service other than the loopback address of
the local Ollama runtime.
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: All application state lives here. Override with PRIVATE_AI_DATA_DIR.
DATA_DIR = Path(
    os.environ.get("PRIVATE_AI_DATA_DIR", PROJECT_ROOT / "local_data")
).resolve()

#: Written by mac/build.sh into the bundled copy. It lets the app tell whether
#: an already-running server is the one it just installed or a stale process
#: from a previous build — adopting the latter means a rebuild silently has no
#: effect, which is a miserable thing to debug.
def _build_id() -> str:
    stamp = PROJECT_ROOT / "BUILD_ID"
    try:
        return stamp.read_text().strip()[:64] or "dev"
    except OSError:
        return "dev"


BUILD_ID = _build_id()

DB_PATH = DATA_DIR / "private_ai.sqlite3"
UPLOADS_DIR = DATA_DIR / "uploads"          # copies of files the user uploads
MODELS_DIR = DATA_DIR / "models"            # whisper ggml weights
WHISPER_DIR = MODELS_DIR / "whisper"
TMP_DIR = DATA_DIR / "tmp"                  # scratch audio for transcription

# --------------------------------------------------------------------------
# Local runtime (Ollama)
# --------------------------------------------------------------------------
#: Loopback only. A non-loopback host is rejected at startup (see guards.py).
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")

CHAT_MODEL = os.environ.get("PRIVATE_AI_CHAT_MODEL", "qwen3:8b")
FAST_MODEL = os.environ.get("PRIVATE_AI_FAST_MODEL", "phi4-mini")
EMBED_MODEL = os.environ.get("PRIVATE_AI_EMBED_MODEL", "nomic-embed-text")

#: Deliberately modest. 16 GB of unified memory is shared with all of macOS;
#: a large context is the fastest way to make the whole machine swap.
NUM_CTX = int(os.environ.get("PRIVATE_AI_NUM_CTX", "4096"))
NUM_CTX_MAX = 8192

#: Unload the model from memory after this long idle.
KEEP_ALIVE = os.environ.get("PRIVATE_AI_KEEP_ALIVE", "5m")

# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
HOST = "127.0.0.1"        # never configurable — see guards.py
PORT = int(os.environ.get("PRIVATE_AI_PORT", "8765"))

# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150
RETRIEVE_K = 5              # passages fed to the model
RETRIEVE_POOL = 24          # candidates considered before fusion

SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt", ".text", ".pdf", ".docx", ".csv"}

#: Hard ceiling on a single ingested file (bytes).
MAX_FILE_BYTES = 64 * 1024 * 1024

# --------------------------------------------------------------------------
# Speech
# --------------------------------------------------------------------------
WHISPER_BIN = os.environ.get("PRIVATE_AI_WHISPER_BIN", "/opt/homebrew/bin/whisper-cli")
WHISPER_MODEL = Path(
    os.environ.get("PRIVATE_AI_WHISPER_MODEL", WHISPER_DIR / "ggml-base.en.bin")
)
SAY_BIN = "/usr/bin/say"
AFPLAY_BIN = "/usr/bin/afplay"

#: Piper neural voice (local, ONNX). Falls back to `say` when absent.
PIPER_DIR = MODELS_DIR / "piper"
PIPER_VOICE = Path(
    os.environ.get("PRIVATE_AI_PIPER_VOICE", PIPER_DIR / "en_GB-alan-medium.onnx")
)
OPEN_BIN = "/usr/bin/open"

#: Refuse absurd audio uploads (push-to-talk clips are seconds long).
MAX_AUDIO_BYTES = 25 * 1024 * 1024


def ensure_dirs() -> None:
    """Create the data directory tree. Safe to call repeatedly."""
    for d in (DATA_DIR, UPLOADS_DIR, MODELS_DIR, WHISPER_DIR, PIPER_DIR, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)
