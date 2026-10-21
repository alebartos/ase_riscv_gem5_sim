; ASE Studio for Windows — Inno Setup script.
; Per-user install (no admin needed for the installer itself; the one-time
; "wsl --install" elevation, if WSL is missing, is requested by the launcher
; on first run, see WslManager.cs).
;
; Expected layout at compile time (produced by the build workflow):
;   dist\launcher\ASEStudio.exe + its .NET Framework 4.8 build output
;   dist\ase-studio-rootfs.tar  (utils/windows/rootfs/build_rootfs.py)
;
; Usage: iscc utils\windows\installer\setup.iss /DAppVersion=1.5.0
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define RepoRoot ".."  + "\..\.."
#define DistDir RepoRoot + "\dist"

[Setup]
AppId={{B6E2B6B1-6E7C-4C83-9B8E-ASESTUDIOWIN}}
AppName=ASE Studio
AppVersion={#AppVersion}
AppPublisher=Politecnico di Torino
DefaultDirName={localappdata}\Programs\ASE Studio
DefaultGroupName=ASE Studio
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir={#DistDir}
OutputBaseFilename=ASE-Studio-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Unsigned installer: Windows SmartScreen will warn ("Ulteriori informazioni"
; -> "Esegui comunque") until/unless a code-signing certificate is added.
UninstallDisplayIcon={app}\ASEStudio.exe

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#DistDir}\launcher\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "{#DistDir}\ase-studio-rootfs.tar"; DestDir: "{app}\rootfs"; Flags: ignoreversion

[Icons]
Name: "{group}\ASE Studio"; Filename: "{app}\ASEStudio.exe"
Name: "{autodesktop}\ASE Studio"; Filename: "{app}\ASEStudio.exe"

[Run]
Filename: "{app}\ASEStudio.exe"; Description: "Avvia ASE Studio"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Removes the WSL distro only; C:\Users\<user>\ASE Studio (the student's
; project) is never touched.
Filename: "{sys}\wsl.exe"; Parameters: "--unregister ASEStudio"; Flags: runhidden; RunOnceId: "UnregisterASEStudio"
