#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Private Local AI — start.
# Binds to 127.0.0.1 only and opens your browser. No internet required.
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
PORT="${PRIVATE_AI_PORT:-8765}"
HOST="127.0.0.1"

# Loopback only. Ollama must not listen on the network either.
export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
# Do not keep a shell history of prompts in the Ollama CLI.
export OLLAMA_NOHISTORY=1

if [ ! -x .venv/bin/python ]; then
  echo "No virtual environment found. Run ./setup.sh first." >&2
  exit 1
fi

if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama is not installed. Run ./setup.sh first." >&2
  exit 1
fi

# Start the local model runtime if it is not already answering.
if ! curl -s -m 2 "http://${OLLAMA_HOST}/api/version" >/dev/null 2>&1; then
  printf '%s\n' "${DIM}Starting the local model runtime...${RESET}"
  mkdir -p logs
  nohup ollama serve > logs/ollama.log 2>&1 &
  for _ in $(seq 1 30); do
    sleep 1
    curl -s -m 2 "http://${OLLAMA_HOST}/api/version" >/dev/null 2>&1 && break
  done
fi

if ! curl -s -m 2 "http://${OLLAMA_HOST}/api/version" >/dev/null 2>&1; then
  printf '%s\n' "${YELLOW}Warning: the model runtime is not responding on ${OLLAMA_HOST}.${RESET}"
  printf '%s\n' "${YELLOW}The app will still start and will show the runtime as offline.${RESET}"
fi

# Refuse to start if the port is taken by something else.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use. Set PRIVATE_AI_PORT to a free port." >&2
  exit 1
fi

URL="http://${HOST}:${PORT}"
printf '\n%sPrivate Local AI%s\n' "$BOLD" "$RESET"
printf '  %sLocal-only. Bound to %s — not reachable from your network.%s\n' "$DIM" "$URL" "$RESET"
printf '  %sData folder: %s/local_data%s\n\n' "$DIM" "$(pwd)" "$RESET"

# Open the browser once the server is actually accepting connections.
(
  for _ in $(seq 1 40); do
    sleep 0.25
    if curl -s -m 1 "${URL}/api/status" >/dev/null 2>&1; then
      open "$URL" 2>/dev/null || true
      printf '%s  Opened %s%s\n' "$GREEN" "$URL" "$RESET"
      break
    fi
  done
) &

exec ./.venv/bin/python -m uvicorn app.main:app \
  --host "$HOST" --port "$PORT" --log-level warning --no-access-log
