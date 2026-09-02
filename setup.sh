#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Private Local AI — one-time setup.
#
# Needs the internet ONLY while it runs (python wheels, model weights).
# After it finishes, the application never makes an outbound connection.
# ---------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
RED=$'\033[31m'; RESET=$'\033[0m'

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
die()  { printf '  %s✗%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

CHAT_MODEL="${PRIVATE_AI_CHAT_MODEL:-qwen3:8b}"
FAST_MODEL="${PRIVATE_AI_FAST_MODEL:-phi4-mini}"
EMBED_MODEL="${PRIVATE_AI_EMBED_MODEL:-nomic-embed-text}"
WHISPER_MODEL_FILE="local_data/models/whisper/ggml-base.en.bin"
WHISPER_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin?download=true"

ASSUME_YES=0
SKIP_VOICE=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes)        ASSUME_YES=1 ;;
    --skip-voice)    SKIP_VOICE=1 ;;
    -h|--help)
      cat <<'HELP'
usage: ./setup.sh [-y|--yes] [--skip-voice]

  -y, --yes       Do not prompt before downloads / installs.
      --skip-voice  Skip whisper.cpp and its speech model.
HELP
      exit 0 ;;
  esac
done

confirm() {  # confirm "question"
  [ "$ASSUME_YES" = "1" ] && return 0
  printf '  %s [y/N] ' "$1"
  read -r reply </dev/tty || return 1
  [[ "$reply" =~ ^[Yy] ]]
}

# --------------------------------------------------------------------------
say ""
say "${BOLD}Private Local AI — setup${RESET}"
say "${DIM}Everything installs under: $ROOT${RESET}"

step "1/6  Checking the machine"
[ "$(uname -s)" = "Darwin" ] || die "This setup targets macOS."
ok "macOS $(sw_vers -productVersion) on $(uname -m)"

PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    v="$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0.0)"
    maj="${v%%.*}"; min="${v##*.}"
    if [ "$maj" = "3" ] && [ "$min" -ge 10 ] 2>/dev/null; then PY="$cand"; break; fi
  fi
done
[ -n "$PY" ] || die "Need Python 3.10+. Try: brew install python@3.14"
ok "Python $("$PY" -c 'import platform;print(platform.python_version())') ($PY)"

command -v ollama >/dev/null 2>&1 || die \
  "Ollama not found. Install it first:  brew install ollama
   (~700 MB, MIT licence — the local model runtime this app talks to.)"
ok "ollama $(ollama --version 2>/dev/null | head -1 | tr -d '\r')"

step "2/6  Python virtual environment"
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
  ok "created .venv"
else
  ok ".venv already exists"
fi
# shellcheck disable=SC1091
./.venv/bin/python -m pip install --upgrade pip --quiet
say "  ${DIM}Installing: fastapi, uvicorn, httpx, numpy, pypdf, pytest (~40 MB)${RESET}"
./.venv/bin/pip install --quiet --only-binary=:all: -r requirements.txt
ok "python dependencies installed"

step "3/6  Local model runtime"
if ! curl -s -m 3 "http://127.0.0.1:11434/api/version" >/dev/null 2>&1; then
  warn "Ollama server is not responding; starting it in the background."
  OLLAMA_HOST=127.0.0.1:11434 nohup ollama serve >/dev/null 2>&1 &
  for _ in $(seq 1 30); do
    sleep 1
    curl -s -m 2 "http://127.0.0.1:11434/api/version" >/dev/null 2>&1 && break
  done
fi
curl -s -m 3 "http://127.0.0.1:11434/api/version" >/dev/null 2>&1 \
  || die "Could not reach Ollama on 127.0.0.1:11434. Run 'ollama serve' in another window."
ok "ollama reachable on 127.0.0.1:11434"

have_model() { ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$1"; }

pull_model() {  # pull_model <name> <approx size> <licence> <why>
  local name="$1" size="$2" lic="$3" why="$4"
  if have_model "$name" || have_model "$name:latest"; then ok "$name already present"; return; fi
  say ""
  say "  ${BOLD}Download required:${RESET} $name"
  say "    size    ~$size"
  say "    licence $lic"
  say "    why     $why"
  if confirm "    Download now?"; then
    ollama pull "$name"
    ok "$name downloaded"
  else
    warn "skipped $name — the app will flag it as unavailable"
  fi
}

step "4/6  Language + embedding models"
pull_model "$CHAT_MODEL"  "5.2 GB" "Apache-2.0" \
  "Primary chat model (8B, 4-bit). Fits comfortably in 16 GB."
pull_model "$FAST_MODEL"  "2.5 GB" "MIT" \
  "Fast fallback (3.8B, 4-bit) for quick replies."
pull_model "$EMBED_MODEL" "274 MB" "Apache-2.0" \
  "Local embeddings for document search. Never leaves this Mac."

step "5/6  Local speech-to-text (whisper.cpp)"
if [ "$SKIP_VOICE" = "1" ]; then
  warn "skipped by --skip-voice"
elif command -v whisper-cli >/dev/null 2>&1; then
  ok "whisper-cli present ($(command -v whisper-cli))"
else
  say ""
  say "  ${BOLD}Install required:${RESET} whisper-cpp (Homebrew)"
  say "    size    ~35 MB + dependencies (ggml, sdl2-compat, libomp)"
  say "    licence MIT"
  say "    why     Transcribes push-to-talk audio entirely on this Mac."
  if command -v brew >/dev/null 2>&1 && confirm "    Install now?"; then
    brew install whisper-cpp && ok "whisper-cpp installed"
  else
    warn "skipped — voice input will show as unavailable in the app"
  fi
fi

mkdir -p "$(dirname "$WHISPER_MODEL_FILE")"
if [ "$SKIP_VOICE" = "1" ]; then
  :
elif [ -s "$WHISPER_MODEL_FILE" ]; then
  ok "speech model present ($(du -h "$WHISPER_MODEL_FILE" | cut -f1))"
else
  say ""
  say "  ${BOLD}Download required:${RESET} ggml-base.en.bin"
  say "    size    ~148 MB"
  say "    licence MIT (whisper.cpp / OpenAI Whisper weights)"
  say "    why     English speech recognition, offline."
  if confirm "    Download now?"; then
    curl -L --fail --progress-bar -o "$WHISPER_MODEL_FILE" "$WHISPER_URL" \
      && ok "speech model downloaded"
  else
    warn "skipped — voice input will show as unavailable"
  fi
fi

step "6/6  Verifying"
if ./.venv/bin/python -m pytest tests -p no:warnings > /tmp/private-ai-tests.log 2>&1; then
  ok "$(grep -Eo '[0-9]+ passed' /tmp/private-ai-tests.log | tail -1) — all checks green"
else
  warn "some tests failed:"
  tail -20 /tmp/private-ai-tests.log
fi

say ""
say "${GREEN}${BOLD}Setup complete.${RESET}"
say ""
say "  Start the app with:   ${BOLD}./start.sh${RESET}"
say "  All data lives in:    ${DIM}$ROOT/local_data${RESET}"
say ""
