#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

if [[ -n "$(git -C "$ROOT" status --porcelain)" ]]; then
  echo "Refusing to package a dirty checkout. Commit the reviewed snapshot first." >&2
  exit 1
fi

REVISION="$(git -C "$ROOT" rev-parse --verify HEAD)"
VERSION="$("$PYTHON" - "$ROOT" "$REVISION" <<'PY'
import json
import re
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:
    raise SystemExit("Release packaging needs Python 3.11 or newer; set PYTHON accordingly.")

root, revision = sys.argv[1:]

def committed(path):
    return subprocess.check_output(
        ["git", "-C", root, "show", f"{revision}:{path}"], text=True
    )

package = tomllib.loads(committed("pyproject.toml"))["project"]
plugin = json.loads(committed(".codex-plugin/plugin.json"))
version = package.get("version")
if (
    package.get("name") != "gradient-ascent"
    or plugin.get("name") != "gradient-ascent"
    or not isinstance(version, str)
    or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None
    or plugin.get("version") != version
):
    raise SystemExit("Refusing to package invalid or mismatched release versions.")
print(version)
PY
)"
OUTPUT="${1:-$ROOT/dist/gradient-ascent-$VERSION.zip}"

mkdir -p "$(dirname -- "$OUTPUT")"
git -C "$ROOT" archive \
  --format=zip \
  --prefix="gradient-ascent-$VERSION/" \
  --output="$OUTPUT" \
  "$REVISION"

echo "$OUTPUT"
