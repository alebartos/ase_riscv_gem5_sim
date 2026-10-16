#!/bin/bash
# Checks run on an ASE Studio checkout before the ase-studio-macos branch is
# updated (sync-upstream.yml): the installed apps download that branch with
# "Update", so a merge with cad-polito-it/ase-studio must not reach them
# untested.
#
#  1. Python compiles, JavaScript parses
#  2. every element studio.js looks up exists in index.html
#  3. Portale della Didattica functions against a fake portal
#  4. the gem5 path contract of the installed macOS apps
#  5. the backend starts and serves the page and the API
#
# Usage: utils/macOS/tests/run_checks.sh PATH_TO_ASE_STUDIO
set -euo pipefail

studio="$(cd "${1:?usage: run_checks.sh PATH_TO_ASE_STUDIO}" && pwd)"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tmp="$(mktemp -d)"
pids=()
cleanup() {
  for pid in ${pids[@]+"${pids[@]}"}; do
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  rm -rf "$tmp"
}
trap cleanup EXIT
mkdir -p "$tmp/host" "$tmp/config"

wait_for() {
  for _ in $(seq 50); do curl -fsS "$1" >/dev/null 2>&1 && return 0; sleep 0.2; done
  echo "Not reachable: $1" >&2
  return 1
}

echo "==> Syntax"
python3 -m py_compile "$studio/backend.py" "$studio/native.py"
for script in "$studio"/frontend/*.js; do node --check "$script"; done

echo "==> Frontend elements"
python3 "$here/check_frontend_ids.py" "$studio/frontend"

echo "==> Portale della Didattica"
python3 "$here/fake_portal.py" 18911 >"$tmp/fake.log" 2>&1 &
pids+=($!)
wait_for http://127.0.0.1:18911/api/_state
ASE_STUDIO_DIR="$studio" ASE_STUDIO_PORTAL_API=http://127.0.0.1:18911/api \
ASE_STUDIO_HOST_ROOT="$tmp/host" XDG_CONFIG_HOME="$tmp/config" \
  python3 "$here/test_portal.py"

echo "==> gem5 path used by the macOS app"
ASE_STUDIO_DIR="$studio" ASE_STUDIO_HOST_ROOT="$tmp/host" XDG_CONFIG_HOME="$tmp/config" \
  python3 "$here/test_gem5_path.py"

echo "==> Backend server"
ASE_STUDIO_DEVELOPER=1 ASE_STUDIO_HOST_ROOT="$tmp/host" XDG_CONFIG_HOME="$tmp/config" \
  python3 "$studio/backend.py" 18912 >"$tmp/server.log" 2>&1 &
pids+=($!)
wait_for http://127.0.0.1:18912/api/health || { cat "$tmp/server.log"; exit 1; }
curl -fsS http://127.0.0.1:18912/api/health
echo
curl -fsS http://127.0.0.1:18912/ | grep -q '<script src="studio.js">'
curl -fsS http://127.0.0.1:18912/api/portal/status | grep -q '"loggedIn": false'
echo "page and API served"

echo "==> All checks passed"
