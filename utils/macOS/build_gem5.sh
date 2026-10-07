#!/bin/bash
# Compila tools/gem5/build/RISCV/gem5.opt per macOS (Apple Silicon), il gem5
# che build_app.py mette nell'app.
#  1. scarica gem5 (cad-polito-it/gem5, ramo di ase_studio_branches.json) se manca
#  2. applica le patch di gem5_patches/ (quelle già applicate vengono saltate)
#  3. compila con SCons e verifica il risultato con gem5_selftest.py
#
# Uso:  utils/macOS/build_gem5.sh [-jN]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
GEM5="$REPO/tools/gem5"
VENV="$REPO/tools/myenv"
BREW="$(brew --prefix)"
PY="$BREW/opt/python@3.10/bin/python3.10"
JOBS="${1:--j$(sysctl -n hw.ncpu)}"

BRANCH="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["gem5"])' \
          "$REPO/ase_studio_branches.json")"

if [[ ! -d "$GEM5/.git" ]]; then
  echo "==> Scarico gem5 ($BRANCH)"
  git clone --depth 1 --branch "$BRANCH" --single-branch \
      https://github.com/cad-polito-it/gem5.git "$GEM5"
fi

echo "==> Patch per macOS"
for patch in "$HERE"/gem5_patches/*.patch; do
  name="$(basename "$patch")"
  if git -C "$GEM5" apply --reverse --check "$patch" 2>/dev/null; then
    echo "    $name: già applicata"
  else
    git -C "$GEM5" apply "$patch"
    echo "    $name: applicata"
  fi
done

if [[ ! -x "$VENV/bin/scons" ]]; then
  echo "==> SCons"
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet "scons==4"
fi

# Alcune versioni dei Command Line Tools non cercano la libc++ dell'SDK:
# in quel caso clang++ passa da un wrapper che la aggiunge.
CXX=clang++
if ! echo '#include <cstddef>' | clang++ -x c++ -fsyntax-only - 2>/dev/null; then
  LIBCXX="$(xcrun --show-sdk-path)/usr/include/c++/v1"
  CXX="$REPO/tools/bin/clang++"
  mkdir -p "$(dirname "$CXX")"
  printf '#!/bin/bash\nexec /usr/bin/clang++ -isystem "%s" "$@"\n' "$LIBCXX" > "$CXX"
  chmod +x "$CXX"
  echo "==> clang++ con la libc++ di $LIBCXX"
fi

echo "==> Compilo gem5.opt ($JOBS)"
cd "$GEM5"
CC=clang CXX="$CXX" PYTHON="$PY" \
  PYTHON_CONFIG="$BREW/opt/python@3.10/bin/python3.10-config" \
  "$VENV/bin/scons" build/RISCV/gem5.opt "$JOBS"

echo "==> Verifica"
"$PY" "$HERE/gem5_selftest.py" "$GEM5/build/RISCV/gem5.opt" \
    --toolchain "$BREW/opt/riscv64-elf-gcc/bin"
