#!/bin/bash
# Starts the ASE Studio server inside the "ASEStudio" WSL2 distro, for the
# Windows launcher. Everything needed (Python, gem5, the RISC-V toolchain,
# make) is in the rootfs; the user's project lives on the Windows side, at
# C:\Users\<user>\ASE Studio, created on first run by copying the project
# template.
#   1. prepare the environment pointing at the rootfs tools
#   2. create/complete the workspace by copying the project template
#   3. run the server (prints "ASE Studio: http://127.0.0.1:PORT")
#
# Usage: start.sh PORT WINDOWS_PROJECT_PATH
#   WINDOWS_PROJECT_PATH is a native Windows path (e.g. C:\Users\Jane Doe\ASE
#   Studio), passed by the launcher, which already knows it; the WSL default
#   user (ase) is not the Windows user, so this script cannot derive it.
set -euo pipefail

PORT="${1:?usage: start.sh PORT WINDOWS_PROJECT_PATH}"
WIN_PROJECT_PATH="${2:?usage: start.sh PORT WINDOWS_PROJECT_PATH}"
RES="/opt/ase"
SUPPORT="${XDG_DATA_HOME:-$HOME/.local/share}/ASE Studio"
WS="$(wslpath -u "$WIN_PROJECT_PATH")"
LOG="$HOME/ase-studio.log"
exec > >(tee "$LOG") 2>&1

# ---- 1. environment: only rootfs + system tools
# The Makefiles use $(CC) and $(ASE_STUDIO_DEMO_MK) unquoted, and the mounted
# Windows paths ($WS) can contain spaces (the Windows user name can), so
# tools/workspace are also reachable through space-free symlinks.
LINK_DIR="$HOME/.ase-studio"
mkdir -p "$LINK_DIR" && ln -sfn "$RES" "$LINK_DIR/app"
export ASE_STUDIO_TOOLS="$LINK_DIR/app"
# Plus the Windows system directories (last, so they never shadow a Linux
# tool): "Open with" and "Show in folder" run notepad.exe and explorer.exe.
WINDOWS_DIRS="$(tr ':' '\n' <<<"$PATH" | grep -iE '^/mnt/[a-z]/windows(/system32)?$' | paste -sd: - || true)"
WINDOWS_DIRS="${WINDOWS_DIRS:-/mnt/c/Windows/System32:/mnt/c/Windows}"
export PATH="$RES/bin:/usr/bin:/bin:/usr/sbin:/sbin:$WINDOWS_DIRS"
export RISCV_TOOLCHAIN_PATH="/usr/bin"
export GEM5_INSTALLATION_PATH="$RES/gem5/build/"
export GEM5_SRC="$RES/gem5/"

# ---- 2. workspace: copy only the files that are missing, never overwrite
mkdir -p "$WS" && cp -Rn "$RES/project/." "$WS/" 2>/dev/null
[[ -f "$WS/setup_default" ]] || { echo "Could not create the workspace: $WS"; exit 1; }
cd "$WS" || exit 1
export ASE_STUDIO_HOST_ROOT="$WS"
ln -sfn "$WS" "$LINK_DIR/workspace" && export ASE_STUDIO_WORKSPACE_LINK="$LINK_DIR/workspace"

# bash ("source setup_default") and make reject CRLF, which Windows editors
# can introduce and which 1.5.0 workspaces got from a CRLF project template
# (never overwritten above). Only files that actually contain a CR are
# rewritten.
{ find "$WS" -maxdepth 1 -type f -name 'setup_default*' || true
  find "$WS/programs" -type f \( -name Makefile -o -name '*.mk' -o -name '*.sh' \) || true
} 2>/dev/null | while IFS= read -r file; do
  if grep -q $'\r' "$file"; then sed -i 's/\r$//' "$file"; fi
done

# ---- 3. updated ASE Studio (if present and working), otherwise the bundled one
export ASE_STUDIO_DIR="$RES/ase_studio"
if [[ -f "$SUPPORT/ase_studio/backend.py" ]]; then
  if ASE_STUDIO_DIR="$SUPPORT/ase_studio" python3 "$RES/ase_studio_wsl.py" --check-startup; then
    export ASE_STUDIO_DIR="$SUPPORT/ase_studio"
  else
    echo "The ASE Studio update does not start: using the version bundled in the app."
  fi
fi

exec python3 "$RES/ase_studio_wsl.py" "$PORT"
