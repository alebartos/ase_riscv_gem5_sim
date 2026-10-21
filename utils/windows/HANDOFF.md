# Handoff: ASE Studio per Windows (.exe)

Documento di passaggio per continuare il lavoro su un PC Windows (Claude Code in
VS Code). Le spiegazioni in chat sono in italiano; codice, commenti, commit e
testi dell'interfaccia sono in inglese, come nel resto del progetto.

## Obiettivo

Un installer `ASE-Studio-Setup.exe` per Windows 10 (2004+) / 11 che porta ASE
Studio sul PC dello studente **senza mai aprire un terminale**. Stesso livello
di comodità dell'app macOS (`utils/macOS`), che è il riferimento da imitare.

## Vincoli

- **Nessuna attribuzione a Claude** nei commit e nelle PR: niente
  `Co-Authored-By: Claude` né "Generated with Claude Code" (il Politecnico non
  lo vuole).
- Non rompere la versione macOS e il flusso Linux esistenti.
- Repository: fork `alebartos/ase_riscv_gem5_sim`.
  - `main`: simulatore + `utils/macOS` + (nuovo) `utils/windows`.
  - `ase-studio-macos`: ASE Studio (storia di `cad-polito-it/ase-studio` più
    le funzioni del Portale della Didattica). È anche l'ASE Studio da usare su
    Windows. `.github/workflows/sync-upstream.yml` lo aggiorna ogni giorno da
    upstream ed esegue `utils/macOS/tests/run_checks.sh` prima del push.
- Le release dell'app sono taggate per piattaforma: `v<versione>-macos`;
  per Windows usare `v<versione>-windows` (la versione è il `VERSION` di ASE
  Studio).

## Architettura decisa

gem5 non si compila nativamente su Windows (POSIX, niente MSVC/MinGW), e il
backend di ASE Studio usa bash, gruppi di processi e `make`. Quindi:

1. **Linux leggero in WSL2** (distribuzione privata dell'app, nome
   `ASEStudio`), costruito da noi come rootfs:
   - base **Debian 12 bookworm slim** (scelta dell'utente al posto di Ubuntu);
   - `python3` (3.11), `make`, `curl`, `ca-certificates`;
   - compilatore: pacchetti `gcc-riscv64-unknown-elf` e
     `binutils-riscv64-unknown-elf` (**verificare** che accettino
     `-march=rv32imafd_zicsr_zifencei -mabi=ilp32`, serve GCC ≥ 12; ASE Studio
     riconosce già il prefisso `riscv64-unknown-elf`);
   - **gem5** `cad-polito-it/gem5`, ramo `stable` (oggi `fc0ef46`), compilato
     `build/RISCV/gem5.opt` in uno stage di build e copiato nel rootfs finale
     con le sole librerie di runtime (ricavarle con `ldd` + `dpkg -S`). gem5 è
     la 22.1: **verificare** che compili con Python 3.11 (gli script ufficiali
     usano 3.10). Su Linux **non servono** le patch di `utils/macOS/gem5_patches`;
   - ASE Studio dal branch `ase-studio-macos` e il "progetto modello" (come
     `PROJECT_PATHS` in `utils/macOS/build_app.py`: `programs`, `gem5`
     (configurazioni Python del simulatore), `setup_default`,
     `ase_studio_branches.json`, `simulate.py`, `README.md`, `LICENSE`);
   - utente predefinito non-root `ase` (`/etc/wsl.conf`: `[user] default=ase`;
     lasciare attivi interop e `appendWindowsPath`).
   Layout proposto nel rootfs: `/opt/ase/{ase_studio, project, gem5/build,
   ase_studio_wsl.py, gem5_selftest.py, start.sh, versions.json}`.

2. **Adattatore `ase_studio_wsl.py`** (gira in WSL, equivalente di
   `utils/macOS/ase_studio_app.py`, da cui copiare la logica):
   - avvia `backend.py` senza i controlli git (`require_startup_repositories`);
   - **update senza git**: ASE Studio dal branch (tarball GitHub), file del
     simulatore da `cad-polito-it/ase_riscv_gem5_sim`, gem5 da una release
     `gem5-linux-x86_64` (tarball + `gem5.json` con id e sha256, installato solo
     se passa `gem5_selftest.py`), avviso di nuove release `v*-windows`;
   - **percorso di gem5**: sostituire il gem5 incluso con quello aggiornato sia
     se le impostazioni indicano la cartella di build sia l'eseguibile (vedi il
     wrapper di `settings_env` in `ase_studio_app.py` e
     `utils/macOS/tests/test_gem5_path.py`);
   - **spazi nei percorsi**: i Makefile usano `$(CC)` e
     `include $(ASE_STUDIO_DEMO_MK)` senza virgolette, e il nome utente Windows
     può contenere spazi. Usare un link senza spazi
     (`~/workspace` → `/mnt/c/Users/<utente>/ASE Studio`) e la stessa
     mappatura di `space_free()` del Mac;
   - "Open with" → Blocco note di Windows (`notepad.exe` con `wslpath -w`);
     "mostra cartella della consegna" → `explorer.exe /select,<percorso Windows>`.
   - `utils/macOS/gem5_selftest.py` cerca `riscv64-elf-gcc`: farlo accettare
     anche `riscv64-unknown-elf-gcc` (il file è condiviso con il Mac).

3. **`start.sh`** in WSL, chiamato dal launcher con porta e profilo Windows:
   crea `C:\Users\<utente>\ASE Studio` se manca copiando `project/` senza mai
   sovrascrivere, crea il link, esporta `ASE_STUDIO_HOST_ROOT`,
   `ASE_STUDIO_WORKSPACE_LINK`, `RISCV_TOOLCHAIN_PATH=/usr/bin`,
   `GEM5_INSTALLATION_PATH`, sceglie l'ASE Studio aggiornato se si avvia
   (come `utils/macOS/launcher.sh`) ed esegue l'adattatore. Stampa
   `ASE Studio: http://127.0.0.1:PORTA`.

4. **Launcher Windows** (C#, .NET Framework 4.8 WinForms + WebView2, niente
   runtime da installare su Windows 10/11), equivalente di
   `utils/macOS/launcher.m`:
   - primo avvio: se WSL manca, dialogo → `wsl --install --no-distribution`
     con richiesta di elevazione (UAC), poi proposta di riavvio; se la distro
     `ASEStudio` manca, `wsl --import ASEStudio "%LOCALAPPDATA%\ASE Studio\wsl"
     <rootfs> --version 2` con barra di avanzamento (se WSL2 non è disponibile,
     es. virtualizzazione spenta, provare `--version 1`). Tutti i processi
     `wsl.exe` con finestra nascosta. Attenzione: l'output di `wsl -l` è UTF-16;
   - avvia `start.sh` nascosto, legge l'URL e lo mostra in WebView2;
   - link esterni nel browser predefinito; `window.open` verso `127.0.0.1`
     (finestra della memoria) in una finestra dell'app **con lo stesso
     ambiente WebView2** (altrimenti `BroadcastChannel` non sincronizza);
   - download (Save ZIP, Export CSV) con la finestra "Salva con nome";
   - alla chiusura chiede conferma se ci sono modifiche non salvate
     (`window.aseHasUnsavedChanges()`), poi `POST /api/shutdown` e chiude
     il processo WSL;
   - se manca il runtime WebView2, messaggio con il link per installarlo.

5. **Installer** Inno Setup: installazione per utente
   (`PrivilegesRequired=lowest`, `%LOCALAPPDATA%\Programs\ASE Studio`),
   collegamenti nel menu Start e sul desktop, include il rootfs compresso;
   la disinstallazione esegue `wsl --unregister ASEStudio` ma **non** tocca
   `C:\Users\<utente>\ASE Studio`. Installer non firmato: SmartScreen chiederà
   "Esegui comunque" (da spiegare nelle note della release).

6. **Build** in `.github/workflows/windows-app.yml` (`workflow_dispatch`):
   rootfs su runner Linux (gem5 in cache per commit), launcher + installer su
   runner Windows, smoke test del rootfs con WSL1 sul runner Windows (WSL2 lì
   non c'è), artifact e bozza di release. In locale su Windows si può fare
   tutto con WSL2 e Docker/`wsl --import`.

## Piano a passi

1. Rootfs Debian: Dockerfile a due stage in `utils/windows/rootfs/`, build,
   `gem5_selftest.py` dentro il rootfs, e `utils/macOS/tests/run_checks.sh`
   sull'ASE Studio incluso.
2. Import in WSL2 sul PC, `start.sh` a mano, simulazione del progetto
   `example` e del caso WaR (`fdiv.s f1,f2,f3` seguita da una scrittura di
   `f2`: deve dare 3.0 con gem5 `fc0ef46`).
3. Launcher C#: avvio, finestra, link, finestra della memoria, download,
   chiusura.
4. Primo avvio da zero (WSL assente o distro assente), installer,
   disinstallazione.
5. Adattatore: update di ASE Studio, del simulatore e di gem5; avviso nuova
   release.
6. Workflow GitHub e prima release `v<versione>-windows` (bozza, da
   confermare con l'utente prima di pubblicarla).

## Già verificato (dal Mac, ottobre 2026)

- Il flusso Mac (`utils/macOS`) è la base: update senza git, self-test di
  gem5, link per gli spazi, finestre e download.
- ASE Studio 1.5.0 sul branch `ase-studio-macos` passa `run_checks.sh` e il
  test completo dell'interfaccia (login e funzioni del Portale contro
  `utils/macOS/tests/fake_portal.py`, pipeline dopo una simulazione vera).
- gem5 `fc0ef46` corregge un errore WaR sui float nella CPU in-order.

## Domande aperte per l'utente

- Va bene `C:\Users\<utente>\ASE Studio` per i progetti?
- Nome della distribuzione WSL: `ASEStudio`?
