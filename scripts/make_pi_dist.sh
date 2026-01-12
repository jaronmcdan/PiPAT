#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$ROOT/dist"
mkdir -p "$DIST_DIR"

# Version string: git short SHA + dirty marker (if available), else timestamp
if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  SHA="$(git -C "$ROOT" rev-parse --short HEAD)"
  DIRTY=""
  if ! git -C "$ROOT" diff --quiet || ! git -C "$ROOT" diff --cached --quiet; then
    DIRTY="-dirty"
  fi
  VER="${SHA}${DIRTY}"
else
  VER="$(date +%Y%m%d-%H%M%S)"
fi

OUT="$DIST_DIR/roi-$VER.tar.gz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Copy a clean tree (avoid venv/cache/git)
rsync -a \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude "venv" \
  --exclude "__pycache__" \
  --exclude "*.pyc" \
  --exclude ".pytest_cache" \
  --exclude "dist" \
  "$ROOT/" "$TMP/roi/"

# Ensure we ship install helpers even if user runs this script standalone
chmod +x "$TMP/roi/scripts/"*.sh 2>/dev/null || true

tar -C "$TMP" -czf "$OUT" "roi"

echo "Built: $OUT"
