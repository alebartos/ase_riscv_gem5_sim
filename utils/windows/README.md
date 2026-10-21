# ASE Studio for Windows

Windows 10 (2004+) / 11 port of ASE Studio, matching the convenience of the
macOS app (`utils/macOS`): one installer, no terminal. See `HANDOFF.md` for
the full design rationale and open questions.

gem5 does not build on Windows and the backend uses bash/make, so everything
(gem5, the RISC-V toolchain, ASE Studio) runs inside a small Debian 12 WSL2
distro ("ASEStudio") built by this directory; a native WinForms + WebView2
window drives it.

## Layout

- `rootfs/Dockerfile`, `rootfs/build_rootfs.py` — builds the WSL2 rootfs
  (gem5 compiled in a builder stage, then a runtime-only Debian image) and
  exports it as `dist/ase-studio-rootfs.tar`, the file `wsl --import` needs.
  Also packages the in-app gem5 update (`dist/gem5-linux-x86_64.tar.gz` +
  `dist/gem5.json`, to attach to a GitHub release).
- `start.sh` — runs inside WSL2, prepares the environment and the workspace
  (`C:\Users\<user>\ASE Studio`), then execs `ase_studio_wsl.py`.
- `ase_studio_wsl.py` — the in-rootfs adapter (equivalent of
  `utils/macOS/ase_studio_app.py`): disables ASE Studio's Git checks, and
  replaces Git-based updates with tarball/release downloads of ASE Studio,
  the simulator files and gem5.
- `launcher/` — the Windows launcher (.NET Framework 4.8, WinForms +
  WebView2): installs WSL/the distro on first run, starts the backend,
  shows it in a native window, handles downloads and unsaved-changes
  confirmation on close.
- `installer/setup.iss` — Inno Setup script for `ASE-Studio-Setup.exe`
  (per-user install, no admin required).
- `../../.github/workflows/windows-app.yml` — builds the rootfs (Linux
  runner), the launcher + installer (Windows runner), smoke-tests the rootfs
  under WSL1 (the Windows runners have no WSL2 nesting), drafts a release.

## Building locally

```
python3 utils/windows/rootfs/build_rootfs.py      # needs Docker
dotnet publish utils/windows/launcher/Launcher.csproj -c Release -o dist/launcher
iscc utils/windows/installer/setup.iss /DAppVersion=<version>
```

## Manual test after `wsl --import`

```
wsl --import ASEStudio "%LOCALAPPDATA%\ASE Studio\wsl" dist\ase-studio-rootfs.tar --version 2
wsl -d ASEStudio -u ase -- /opt/ase/start.sh 8765 "C:\Users\<user>\ASE Studio"
```
Then open `http://127.0.0.1:8765` in a browser and try the `example` project
and the WaR case from `HANDOFF.md` (`fdiv.s f1,f2,f3` followed by a write to
`f2`: must give 3.0 with gem5 `fc0ef46`).

## Known gaps (need a full Windows+WSL2 run to verify)

- The launcher has not been compiled (this dev machine has the .NET runtime
  but not the SDK); CI (`windows-app.yml`) builds it on `windows-latest`.
- `wsl --import` itself has not been run end-to-end on this machine.
- No app icon yet (`AppIcon.ico` for the launcher, `.ico`/banner for the
  installer) — `utils/macOS/AppIcon.icns` would need converting.
