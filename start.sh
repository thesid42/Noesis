#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

export UV_CACHE_DIR="${ROOT_DIR}/.runtime/uv-cache"
PYTHON="${ROOT_DIR}/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  uv sync --python 3.12 --extra flower --extra dev
fi

echo "Noesis control room: http://127.0.0.1:8765"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: ./start.sh [--PreviewOnly] [--OBS]

  --PreviewOnly  Start only the dashboard with deterministic fallback.
  --OBS          Request OBS integration (requires a reachable OBS WebSocket).
EOF
  exit 0
fi

PREVIEW_ONLY=false
OBS=false
for arg in "$@"; do
  case "$arg" in
    --PreviewOnly) PREVIEW_ONLY=true ;;
    --OBS) OBS=true ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ "$PREVIEW_ONLY" == true ]]; then
  exec "$PYTHON" -m noesis
fi

ARGS=(scripts/run_demo.py)
if [[ "$OBS" == true ]]; then
  ARGS+=(--obs)
fi
exec "$PYTHON" "${ARGS[@]}"
