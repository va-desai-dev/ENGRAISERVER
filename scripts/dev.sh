#!/usr/bin/env bash
# Rebuild the React interface in place while the canonical gateway keeps
# serving it. There is deliberately no second gateway or Vite web port: the
# UI, control API, and OpenAI-compatible API all remain on the gateway port
# (8400 by default), including behind any reverse proxy or tunnel.
set -euo pipefail

HOME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HOME_DIR/ui"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to watch the interface. On Fedora: sudo dnf install nodejs npm" >&2
  exit 1
fi

# Keep dependency installation consistent with the one-shot production build.
# Dependencies are installed only on a fresh checkout. Re-running npm on every
# watcher start adds delay and turns an ordinary save workflow into maintenance.
if [ ! -d node_modules ]; then
  npm ci --silent
fi

cat <<INFO

  ENGRAISERVER UI watcher
  ───────────────────────────────────────────────
  url       the existing gateway/tunnel on port 8400
  watching  ui/src/ → src/engrai_server/static/

  The running gateway, credentials, routes, and model state stay unchanged.
  Ctrl+C stops only this UI watcher.

INFO

exec npm run dev
