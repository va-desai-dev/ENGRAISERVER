#!/usr/bin/env bash
# Build the control interface into src/engrai_server/static/, which the gateway serves.
#
# The output is committed so a checkout or installed Python wheel can
# serve the interface without Node installed. Run this after changing anything
# under ui/, and commit src/engrai_server/static/ along with the source.
#
#   ./scripts/build-ui.sh            # production build
#   ENGRAI_SOURCEMAP=1 ./scripts/build-ui.sh   # with source maps for debugging
#
# To iterate continuously, run ./scripts/dev.sh. It rebuilds into src/engrai_server/static/
# while the existing gateway continues serving everything on port 8400.
set -euo pipefail

HOME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HOME_DIR/ui"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to build the interface. On Fedora: sudo dnf install nodejs npm" >&2
  exit 1
fi

# The lockfile is part of the source contract. A mismatch is a build failure,
# not permission to rewrite dependency selection on the developer's machine.
npm ci --silent

npm run build

echo
echo "Built into src/engrai_server/static/. Commit that directory with your ui/ changes."
