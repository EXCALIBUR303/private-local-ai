#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Optional: local voice cloning (Chatterbox, MIT — Resemble AI).
#
# Installs into its own virtual environment so the main app stays light.
# Needs the internet once; afterwards cloning runs entirely on this Mac.
#
#   PyTorch + Chatterbox   ~1.5 GB
#   model weights          ~1 GB, fetched on first use
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")/.."

BOLD=$'\033[1m'; GREEN=$'\033[32m'; DIM=$'\033[2m'; RESET=$'\033[0m'
DATA="${PRIVATE_AI_DATA_DIR:-$(pwd)/local_data}"
ENVDIR="$DATA/voiceclone-venv"

printf '\n%sLocal voice cloning%s\n' "$BOLD" "$RESET"
printf '  %s~1.5 GB of dependencies, plus ~1 GB of weights on first use.%s\n' "$DIM" "$RESET"
printf '  %sGenerated speech carries an inaudible watermark marking it synthetic.%s\n\n' "$DIM" "$RESET"

PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3; do
  command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
done
[ -n "$PY" ] || { echo "No python3 found." >&2; exit 1; }

mkdir -p "$DATA"
[ -d "$ENVDIR" ] || "$PY" -m venv "$ENVDIR"
"$ENVDIR/bin/python" -m pip install --quiet --upgrade pip

echo "  installing chatterbox-tts (this takes a few minutes)…"
"$ENVDIR/bin/pip" install --quiet chatterbox-tts
# perth (the watermarker) still imports pkg_resources, removed in setuptools 81+
"$ENVDIR/bin/pip" install --quiet "setuptools<81"

"$ENVDIR/bin/python" - <<'PY'
import perth, torch
assert getattr(perth, "PerthImplicitWatermarker", None) is not None, "watermarker unavailable"
print(f"  torch {torch.__version__}, metal: {torch.backends.mps.is_available()}")
print("  watermarker: enabled")
PY

printf '\n%s%sVoice cloning installed.%s\n' "$GREEN" "$BOLD" "$RESET"
printf '  Add a voice in the app under %sVoice%s, then pick it as the speaking voice.\n\n' "$BOLD" "$RESET"
