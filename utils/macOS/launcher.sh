#!/bin/bash
# Avvia il server di ASE Studio per la finestra nativa (Contents/MacOS/ASEStudio).
# Tutto ciò che serve (Python, gem5, compilatore RISC-V, make) è dentro l'app;
# il progetto dell'utente vive in ~/ASE Studio, creato al primo avvio.
#  1. prepara l'ambiente che punta agli strumenti inclusi nell'app
#  2. crea/completa la cartella di lavoro copiando il progetto modello
#  3. esegue il server (stampa "ASE Studio: http://127.0.0.1:PORTA")

RES="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PATH="$(cd "$RES/../.." && pwd)"
FW="$(cd "$RES/../Frameworks" && pwd)"
PY="$FW/Python.framework/Versions/3.10/bin/python3.10"
SUPPORT="$HOME/Library/Application Support/ASE Studio"
WS="${ASE_STUDIO_WORKSPACE:-$HOME/ASE Studio}"
LOG="$HOME/Library/Logs/ASE Studio.log"
mkdir -p "$(dirname "$LOG")"
exec > >(tee "$LOG") 2>&1

# ---- 1. ambiente: solo strumenti dell'app e di sistema (niente Homebrew)
# I Makefile usano $(CC) senza virgolette: gli strumenti vanno esposti tramite
# un percorso senza spazi (l'app si chiama "ASE Studio.app").
LINK_DIR="$HOME/.ase-studio"
mkdir -p "$LINK_DIR" && ln -sfn "$RES" "$LINK_DIR/app"
TOOLS="$LINK_DIR/app"
[[ "$TOOLS" == *" "* ]] && TOOLS="$RES"
export ASE_STUDIO_TOOLS="$TOOLS"
export PATH="$TOOLS/bin:$TOOLS/toolchain/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONHOME="$FW/Python.framework/Versions/3.10"
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
unset PYTHONPATH PYTHONSTARTUP
export RISCV_TOOLCHAIN_PATH="$TOOLS/toolchain/bin"
export GEM5_INSTALLATION_PATH="$TOOLS/gem5/build/"
export GEM5_SRC="$TOOLS/gem5/"

# Se l'app è stata scaricata, togli la quarantena dagli eseguibili interni
# (gem5, cc1, ...), altrimenti Gatekeeper li blocca al primo utilizzo.
if xattr -p com.apple.quarantine "$APP_PATH" >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine "$APP_PATH" 2>/dev/null || true
fi

# ---- 2. cartella di lavoro: copia solo i file mancanti, mai sovrascrivere
mkdir -p "$WS" && /bin/cp -Rn "$RES/project/." "$WS/" 2>/dev/null
[[ -f "$WS/setup_default" ]] || { echo "Impossibile creare la cartella di lavoro: $WS"; exit 1; }
cd "$WS" || exit 1
export ASE_STUDIO_HOST_ROOT="$WS"

# ---- 3. ASE Studio aggiornato (se presente e funzionante), altrimenti quello incluso
export ASE_STUDIO_DIR="$RES/ase_studio"
if [[ -f "$SUPPORT/ase_studio/backend.py" ]]; then
  if ASE_STUDIO_DIR="$SUPPORT/ase_studio" "$PY" "$RES/ase_studio_app.py" --check-startup; then
    export ASE_STUDIO_DIR="$SUPPORT/ase_studio"
  else
    echo "L'aggiornamento di ASE Studio non si avvia: uso la versione inclusa nell'app."
  fi
fi

exec "$PY" "$RES/ase_studio_app.py" "${ASE_STUDIO_PORT:-8765}"
