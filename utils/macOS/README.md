# ASE Studio.app – build per macOS

`build_app.py` genera `dist/ASE Studio.app` e `dist/ASE-Studio-<versione>.dmg`:
un'app autonoma (Apple Silicon) che contiene ASE Studio, gem5, Python 3.10,
il compilatore RISC-V e GNU make, e funziona su un Mac senza Homebrew né
Command Line Tools.

## Requisiti (solo sulla macchina di build)

```sh
brew install python@3.10 riscv64-elf-gcc riscv64-elf-binutils make
```

e gem5 già compilato in `tools/gem5/build/RISCV/gem5.opt` (vedi il README
principale). Serve anche `clang` (Command Line Tools).

## Build

```sh
/opt/homebrew/opt/python@3.10/bin/python3.10 utils/macOS/build_app.py            # app + dmg
/opt/homebrew/opt/python@3.10/bin/python3.10 utils/macOS/build_app.py --no-dmg   # solo app
```

Lo script copia le librerie non di sistema in `Contents/Frameworks`, le
ricollega (`@executable_path` per gli eseguibili, `@loader_path` per librerie
e moduli Python), verifica che non restino riferimenti a `/opt/homebrew` e
firma tutto ad-hoc. La versione minima di macOS dell'app è la più alta tra
quelle dei binari inclusi (i bottle di Homebrew usano quella della macchina
di build).

## File

| File | Ruolo |
| --- | --- |
| `launcher.m` | eseguibile dell'app: finestra nativa (WKWebView), avvia e ferma il server |
| `launcher.sh` | ambiente (solo strumenti dell'app), crea `~/ASE Studio`, esegue il server |
| `ase_studio_app.py` | avvia `ase_studio/backend.py` senza controlli git; update da GitHub senza git; "Open with" e "mostra consegna" con `open` di macOS |
| `Info.plist`, `AppIcon.icns`, `LEGGIMI.txt` | bundle e contenuto del DMG |

## Dove stanno i dati

- Progetto dell'utente: `~/ASE Studio` (creato al primo avvio, mai sovrascritto)
- ASE Studio aggiornato: `~/Library/Application Support/ASE Studio`
- Log: `~/Library/Logs/ASE Studio.log`
