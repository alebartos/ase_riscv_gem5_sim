#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export ASE_STUDIO_HOST_ROOT="$root_dir"

# The GTK/WebKitGTK native window is Linux-only. On macOS, serve the same
# Studio frontend through the browser.
if [[ "$(uname -s)" == "Darwin" ]]; then
  exec python3 "$root_dir/ase_studio/backend.py" "${ASE_STUDIO_PORT:-8765}" --open
fi

exec "$root_dir/ase_studio/launch.sh"
