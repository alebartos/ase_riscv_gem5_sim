# ASE Studio.app – build per macOS

`build_app.py` genera `dist/ASE Studio.app` e `dist/ASE-Studio-<versione>.dmg`:
un'app autonoma (Apple Silicon) che contiene ASE Studio, gem5, Python 3.10,
il compilatore RISC-V e GNU make, e funziona su un Mac senza Homebrew né
Command Line Tools.

## Requisiti (solo sulla macchina di build)

```sh
brew install python@3.10 riscv64-elf-gcc riscv64-elf-binutils make
```

Serve anche `clang` (Command Line Tools).

## Build

```sh
utils/macOS/build_gem5.sh                                                        # gem5.opt
/opt/homebrew/opt/python@3.10/bin/python3.10 utils/macOS/build_app.py            # app + dmg
/opt/homebrew/opt/python@3.10/bin/python3.10 utils/macOS/build_app.py --no-dmg   # solo app
```

`build_gem5.sh` scarica gem5 in `tools/gem5` (se manca), applica le patch di
`gem5_patches/` e lo compila. Le patch servono: su macOS `uint_fast16_t` è a
16 bit, e senza `0001` ogni operazione float di gem5 legge operandi troncati
(`1.5 * 2.0` dà NaN). `build_app.py` rifiuta un `gem5.opt` che non supera
`gem5_selftest.py`.

## Da dove arriva ASE Studio

L'ASE Studio incluso nell'app non è il submodule `ase_studio`: è il branch
`ase-studio-macos` di questo repository (`alebartos/ase_riscv_gem5_sim`), cioè
`cad-polito-it/ase-studio` più le funzioni per il Portale della Didattica
(upload degli elaborati, elenco con ritiro/ripristino, riepilogo prima
dell'upload, "Save ZIP for manual upload"). `build_app.py` lo scarica da lì
(`--studio-dir PATH` per compilare un checkout locale) e l'"Update" dell'app
segue lo stesso branch. Il workflow `sync-upstream.yml` fonde ogni giorno
`cad-polito-it/ase-studio` in quel branch e, prima del push, esegue
`tests/run_checks.sh`: sintassi di Python e JavaScript, elementi della pagina
usati da `studio.js`, funzioni del Portale contro un portale finto
(`tests/fake_portal.py`) e avvio del backend. Se c'è un conflitto o un
controllo fallisce, il branch non viene aggiornato (le app restano all'ultima
versione buona) e GitHub manda un'email: il merge va fatto a mano.

Per eseguire gli stessi controlli in locale:

```sh
utils/macOS/tests/run_checks.sh /percorso/ase-studio
```

Pubblicare una modifica ad ASE Studio (le app installate la ricevono con
"Update", senza un nuovo DMG):

```sh
git -C /percorso/ase-studio push git@github.com:alebartos/ase_riscv_gem5_sim.git HEAD:ase-studio-macos
```

## Nuova versione dell'app

Python, compilatore e finestra nativa cambiano solo con un nuovo DMG. Le app
annunciano una release più recente con tag `v<versione>-macos` (la versione è
quella di `VERSION` di ASE Studio):

```sh
gh release create v1.2.5-macos dist/ASE-Studio-1.2.5.dmg \
    --title "ASE Studio 1.2.5 per macOS (Apple Silicon)" --notes "..."
```

## Aggiornare gem5 nelle app già installate

`build_app.py` produce anche `dist/gem5-macos-arm64.tar.gz` e `dist/gem5.json`.
Caricati nella release GitHub `gem5-macos-arm64` di questo repository, l'app
li trova con "Update": scarica il nuovo `gem5.opt`, ne verifica checksum e
self-test e lo installa in `~/Library/Application Support/ASE Studio/gem5`
(se il test fallisce resta il gem5 incluso nell'app).

```sh
gh release create gem5-macos-arm64 --title "gem5 per ASE Studio (macOS)" \
    --notes "gem5.opt per l'aggiornamento in-app" || true
gh release upload gem5-macos-arm64 dist/gem5-macos-arm64.tar.gz dist/gem5.json --clobber
```

Se il gem5 in uso non supera il self-test, l'output di ogni simulazione inizia
con un avviso.

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
| `ase_studio_app.py` | avvia `ase_studio/backend.py` senza controlli git; update da GitHub senza git (anche di gem5); "Open with" e "mostra consegna" con `open` di macOS |
| `build_gem5.sh`, `gem5_patches/` | compila `tools/gem5` per macOS con le patch necessarie |
| `gem5_selftest.py` | verifica che gem5 calcoli correttamente in virgola mobile (build, update e avvio) |
| `tests/` | controlli eseguiti prima di aggiornare `ase-studio-macos` (`run_checks.sh`) |
| `Info.plist`, `AppIcon.icns`, `LEGGIMI.txt` | bundle e contenuto del DMG |

## Dove stanno i dati

- Progetto dell'utente: `~/ASE Studio` (creato al primo avvio, mai sovrascritto)
- ASE Studio aggiornato: `~/Library/Application Support/ASE Studio`
- gem5 aggiornato: `~/Library/Application Support/ASE Studio/gem5`
- Log: `~/Library/Logs/ASE Studio.log`
