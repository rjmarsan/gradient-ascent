#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
VERSION="0.1.0"
OUTPUT="${1:-$ROOT/dist/gradient-ascent-$VERSION.zip}"

if [[ -n "$(git -C "$ROOT" status --porcelain)" ]]; then
  echo "Refusing to package a dirty checkout. Commit the reviewed snapshot first." >&2
  exit 1
fi

mkdir -p "$(dirname -- "$OUTPUT")"
git -C "$ROOT" archive \
  --format=zip \
  --prefix="gradient-ascent-$VERSION/" \
  --output="$OUTPUT" \
  HEAD

echo "$OUTPUT"
