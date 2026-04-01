#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"

if [ ! -d ".venv" ]; then
  PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3.12}"
  "$PYTHON_BIN" -m venv .venv
fi

source .venv/bin/activate
python -m pip install -r requirements.txt

exec litellm \
  --config "${LITELLM_PROXY_CONFIG:-configs/litellm_proxy.yaml}" \
  --host "${LITELLM_PROXY_HOST:-127.0.0.1}" \
  --port "${LITELLM_PROXY_PORT:-4000}"
