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
  echo "No python interpreter found for post-sync refresh." >&2
  exit 1
fi

cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"

DATA_DIR="$("$RUNNER" - <<'PY'
from gradient_ascent.config import load_config
print(load_config().data_dir)
PY
)"
ARGS=("$@")
for ((i = 0; i < ${#ARGS[@]}; i++)); do
  case "${ARGS[$i]}" in
    --data-dir)
      if (( i + 1 >= ${#ARGS[@]} )); then
        echo "--data-dir requires a value" >&2
        exit 2
      fi
      DATA_DIR="${ARGS[$((i + 1))]}"
      ;;
    --data-dir=*)
      DATA_DIR="${ARGS[$i]#--data-dir=}"
      ;;
  esac
done
export COACH_WORKSPACE_DIR="$DATA_DIR"
export COACH_DATA_DIR="$DATA_DIR"

"$RUNNER" "$REPO_DIR/scripts/post_sync_refresh.py" "$@"
