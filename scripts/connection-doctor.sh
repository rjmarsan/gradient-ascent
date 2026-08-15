#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
CALLER_DIR="$(pwd)"
if [[ -z "${COACH_WORKSPACE_DIR:-}" && -z "${COACH_DATA_DIR:-}" && "$CALLER_DIR" != "$REPO_DIR" ]]; then
  export COACH_WORKSPACE_DIR="$CALLER_DIR"
fi
if [[ -n "${PYTHON_BIN:-}" ]]; then
  RUNNER="$PYTHON_BIN"
elif [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
  RUNNER="$REPO_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  RUNNER="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  RUNNER="$(command -v python)"
else
  echo "No python interpreter found for connection doctor." >&2
  exit 1
fi

cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
"$RUNNER" -m gradient_ascent.cli connections-status --json
