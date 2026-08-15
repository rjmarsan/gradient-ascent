#!/usr/bin/env bash
set -euo pipefail

PLUGIN_NAME="gradient-ascent"
PLUGIN_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)"
MARKETPLACE_ROOT="${GRADIENT_ASCENT_MARKETPLACE_ROOT:-$HOME/code/codex-local-marketplace}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
CONFIG_FILE="${CODEX_CONFIG_FILE:-$CODEX_HOME/config.toml}"

mkdir -p "$MARKETPLACE_ROOT/plugins" "$MARKETPLACE_ROOT/.agents/plugins" "$CODEX_HOME"

PLUGIN_DIR="$PLUGIN_DIR" \
PLUGIN_BUNDLE_DIR="$MARKETPLACE_ROOT/plugins/$PLUGIN_NAME" \
python3 - <<'PY'
import os
import shutil
from pathlib import Path

source = Path(os.environ["PLUGIN_DIR"])
destination = Path(os.environ["PLUGIN_BUNDLE_DIR"])
staging = destination.with_name(destination.name + ".staging")

if staging.exists() or staging.is_symlink():
    if staging.is_symlink() or staging.is_file():
        staging.unlink()
    else:
        shutil.rmtree(staging)
staging.mkdir(parents=True)

for name in (".codex-plugin", "skills"):
    shutil.copytree(source / name, staging / name)
for name in ("LICENSE", "README.md"):
    candidate = source / name
    if candidate.exists():
        shutil.copy2(candidate, staging / name)

if destination.exists() or destination.is_symlink():
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
    else:
        shutil.rmtree(destination)
staging.rename(destination)
PY

MARKETPLACE_JSON="$MARKETPLACE_ROOT/.agents/plugins/marketplace.json" \
PLUGIN_NAME="$PLUGIN_NAME" \
python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["MARKETPLACE_JSON"]).expanduser()
plugin_name = os.environ["PLUGIN_NAME"]
entry = {
    "name": plugin_name,
    "source": {"source": "local", "path": f"./plugins/{plugin_name}"},
    "policy": {"installation": "AVAILABLE"},
    "category": "Productivity",
}

if path.exists():
    payload = json.loads(path.read_text())
else:
    payload = {"name": "local", "interface": {"displayName": "Local Plugins"}, "plugins": []}

payload.setdefault("name", "local")
payload.setdefault("interface", {}).setdefault("displayName", "Local Plugins")
plugins = payload.setdefault("plugins", [])
for index, plugin in enumerate(plugins):
    if isinstance(plugin, dict) and plugin.get("name") == plugin_name:
        plugins[index] = entry
        break
else:
    plugins.append(entry)

path.write_text(json.dumps(payload, indent=2) + "\n")
PY

CONFIG_FILE="$CONFIG_FILE" \
PLUGIN_ID="$PLUGIN_NAME@local" \
python3 - <<'PY'
import os
import re
from pathlib import Path

path = Path(os.environ["CONFIG_FILE"]).expanduser()
plugin_id = os.environ["PLUGIN_ID"]
path.parent.mkdir(parents=True, exist_ok=True)
text = path.read_text() if path.exists() else ""
header = f'[plugins."{plugin_id}"]'

if header not in text:
    if text and not text.endswith("\n"):
        text += "\n"
    text += f"\n{header}\nenabled = true\n"
else:
    escaped = re.escape(header)
    section_re = re.compile(rf"(?ms)^({escaped}\n)(.*?)(?=^\[|\Z)")

    def update(match: re.Match[str]) -> str:
        prefix, body = match.groups()
        if re.search(r"(?m)^enabled\s*=", body):
            body = re.sub(r"(?m)^enabled\s*=.*$", "enabled = true", body, count=1)
        else:
            if body and not body.endswith("\n"):
                body += "\n"
            body += "enabled = true\n"
        return prefix + body

    text = section_re.sub(update, text, count=1)

path.write_text(text)
PY

MARKETPLACE_REGISTRATION="manual verification needed"
REGISTRATION_FAILURE=""
CODEX_CANDIDATES=()
if [[ -n "${GRADIENT_ASCENT_CODEX_BIN:-}" ]]; then
  CODEX_CANDIDATES+=("$GRADIENT_ASCENT_CODEX_BIN")
fi
CODEX_CANDIDATES+=(
  "/Applications/Codex.app/Contents/Resources/codex"
)
if command -v codex >/dev/null 2>&1; then
  CODEX_CANDIDATES+=("$(command -v codex)")
fi

CODEX_REGISTRATION_BIN=""
if [[ "${GRADIENT_ASCENT_SKIP_CLI_REGISTRATION:-0}" != "1" ]]; then
  for candidate in "${CODEX_CANDIDATES[@]}"; do
    if [[ ! -x "$candidate" ]] || ! "$candidate" --version >/dev/null 2>&1; then
      continue
    fi
    if ! registration_output=$("$candidate" plugin marketplace add "$MARKETPLACE_ROOT" 2>&1); then
      REGISTRATION_FAILURE="$candidate plugin marketplace add failed: $registration_output"
      continue
    fi
    if ! registration_output=$("$candidate" plugin add "$PLUGIN_NAME@local" 2>&1); then
      REGISTRATION_FAILURE="$candidate plugin add $PLUGIN_NAME@local failed: $registration_output"
      continue
    fi
    if ! registration_output=$("$candidate" plugin list 2>&1); then
      REGISTRATION_FAILURE="$candidate plugin list failed: $registration_output"
      continue
    fi
    if ! printf '%s\n' "$registration_output" | grep -F "$PLUGIN_NAME@local" | grep -F "installed, enabled" >/dev/null; then
      REGISTRATION_FAILURE="$candidate plugin list did not report $PLUGIN_NAME@local as installed and enabled"
      continue
    fi
    CODEX_REGISTRATION_BIN="$candidate"
    MARKETPLACE_REGISTRATION="installed and enabled through codex CLI"
    break
  done
fi

if [[ -z "$CODEX_REGISTRATION_BIN" ]]; then
  if PLUGIN_BUNDLE_DIR="$MARKETPLACE_ROOT/plugins/$PLUGIN_NAME" \
    PLUGIN_NAME="$PLUGIN_NAME" \
    PLUGIN_ID="$PLUGIN_NAME@local" \
    CODEX_HOME="$CODEX_HOME" \
    CONFIG_FILE="$CONFIG_FILE" \
    python3 - <<'PY'
import hashlib
import json
import os
import re
import sys
from pathlib import Path

bundle = Path(os.environ["PLUGIN_BUNDLE_DIR"])
manifest = json.loads((bundle / ".codex-plugin" / "plugin.json").read_text())
version = str(manifest.get("version") or "").strip()
cache = Path(os.environ["CODEX_HOME"]) / "plugins" / "cache" / "local" / os.environ["PLUGIN_NAME"] / version
config_path = Path(os.environ["CONFIG_FILE"])


def inventory(root: Path):
    if not root.is_dir():
        return None
    records = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            return None
        if path.is_dir():
            records.append(("dir", relative, ""))
        elif path.is_file():
            records.append(("file", relative, hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            return None
    return records


if not version or inventory(bundle) != inventory(cache) or not config_path.is_file():
    sys.exit(1)
text = config_path.read_text()
header = re.escape(f'[plugins."{os.environ["PLUGIN_ID"]}"]')
section = re.search(rf"(?ms)^{header}\n(.*?)(?=^\[|\Z)", text)
if section is None or re.search(r"(?m)^enabled\s*=\s*true\s*$", section.group(1)) is None:
    sys.exit(1)
PY
  then
    MARKETPLACE_REGISTRATION="installed cache matches current bundle and is enabled in config"
  elif [[ "${GRADIENT_ASCENT_SKIP_CLI_REGISTRATION:-0}" != "1" ]]; then
    echo "No working Codex CLI could install and verify the plugin. A clean local bundle and config were written; restart Codex, open Plugins, and install gradient-ascent from the Local Plugins marketplace."
  fi
fi

if [[ "$MARKETPLACE_REGISTRATION" == "manual verification needed" ]]; then
  cat <<EOF
Prepared $PLUGIN_NAME for Codex, but it is not verified as installed.
- plugin checkout: $PLUGIN_DIR
- marketplace root: $MARKETPLACE_ROOT
- clean plugin bundle: $MARKETPLACE_ROOT/plugins/$PLUGIN_NAME
- config: $CONFIG_FILE
- marketplace registration: $MARKETPLACE_REGISTRATION
EOF
  if [[ -n "$REGISTRATION_FAILURE" ]]; then
    printf 'Codex CLI registration failed: %s\n' "$REGISTRATION_FAILURE" >&2
  fi
  if [[ "${GRADIENT_ASCENT_SKIP_CLI_REGISTRATION:-0}" == "1" ]]; then
    exit 0
  fi
  echo "Gradient Ascent is not active yet. Complete installation from the Local Plugins marketplace, then rerun this installer to verify it." >&2
  exit 1
fi

cat <<EOF
Installed $PLUGIN_NAME for Codex.
- plugin checkout: $PLUGIN_DIR
- marketplace root: $MARKETPLACE_ROOT
- clean plugin bundle: $MARKETPLACE_ROOT/plugins/$PLUGIN_NAME
- config: $CONFIG_FILE
- marketplace registration: $MARKETPLACE_REGISTRATION

Open a new Codex thread or restart Codex Desktop to load newly installed plugin skills.
EOF
