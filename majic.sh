#!/bin/sh
# Wrapper that always uses the .venv Python if it exists
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [ -f "$VENV_PYTHON" ]; then
    exec "$VENV_PYTHON" "$SCRIPT_DIR/majic" "$@"
else
    exec python3 "$SCRIPT_DIR/majic" "$@"
fi
