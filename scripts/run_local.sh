#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"

PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3.12}"

if [ ! -d ".venv" ]; then
  "$PYTHON_BIN" -m venv .venv
fi

source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/seed_demo_data.py

PIDS=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
}

terminate_matching_listener() {
  local port="$1"
  local pid
  while read -r pid; do
    [ -n "$pid" ] || continue
    kill "$pid" >/dev/null 2>&1 || true
  done < <(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)

  for _ in $(seq 1 20); do
    if ! lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done

  while read -r pid; do
    [ -n "$pid" ] || continue
    kill -9 "$pid" >/dev/null 2>&1 || true
  done < <(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
}

wait_for_http() {
  local url="$1"
  local label="$2"
  local header="${3:-}"
  for _ in $(seq 1 40); do
    if [ -n "$header" ]; then
      if curl -fsS -H "$header" "$url" >/dev/null 2>&1; then
        return 0
      fi
    elif curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  echo "Timed out waiting for $label at $url" >&2
  return 1
}

trap cleanup EXIT INT TERM

DOTENV_RUN=()
if [ -f ".env" ]; then
  DOTENV_RUN=(dotenv -f .env run --)
fi

if [ "${KILL_EXISTING_LOCAL_SERVICES:-true}" = "true" ]; then
  terminate_matching_listener "${APP_PORT:-7777}"
  terminate_matching_listener "${LITELLM_PROXY_PORT:-4000}"
  terminate_matching_listener "${CODEX_PREMIUM_ADAPTER_PORT:-4101}"
fi

if [ "${START_CODEX_ADAPTER:-true}" = "true" ]; then
  "${DOTENV_RUN[@]}" uvicorn app.adapters.codex_subscription_adapter:app \
    --host "${CODEX_PREMIUM_ADAPTER_HOST:-127.0.0.1}" \
    --port "${CODEX_PREMIUM_ADAPTER_PORT:-4101}" \
    >/tmp/agno-codex-adapter.log 2>&1 &
  PIDS+=("$!")
  wait_for_http "http://${CODEX_PREMIUM_ADAPTER_HOST:-127.0.0.1}:${CODEX_PREMIUM_ADAPTER_PORT:-4101}/health" "coder-premium adapter"
fi

if [ "${START_LITELLM_PROXY:-true}" = "true" ]; then
  "${DOTENV_RUN[@]}" bash scripts/run_litellm_proxy.sh >/tmp/agno-litellm-proxy.log 2>&1 &
  PIDS+=("$!")
  wait_for_http \
    "${LITELLM_PROXY_BASE_URL:-http://127.0.0.1:4000}/v1/models" \
    "LiteLLM Proxy" \
    "Authorization: Bearer ${LITELLM_MASTER_KEY:-local-litellm-master-key}"
fi

"${DOTENV_RUN[@]}" uvicorn app.main:app --host "${APP_HOST:-0.0.0.0}" --port "${APP_PORT:-7777}" --reload
