#!/usr/bin/env python3
"""Build a self-contained "ASE Studio.app" and its .dmg (macOS, Apple Silicon).

The app is a native window (WKWebView, launcher.m) around the ASE Studio
backend and bundles the project template, gem5.opt, Python 3.10, the RISC-V
toolchain and GNU make. Every non-system library is copied into
Contents/Frameworks and relinked (executables via @executable_path, libraries
and Python modules via @loader_path), then everything is ad-hoc signed.

Build requirements (on the build machine only):
  brew install python@3.10 riscv64-elf-gcc riscv64-elf-binutils make
  tools/gem5/build/RISCV/gem5.opt already compiled

Usage:  utils/macOS/build_app.py [--no-dmg]
Output: dist/ASE Studio.app, dist/ASE-Studio-<version>.dmg
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DIST = REPO / "dist"
APP = DIST / "ASE Studio.app"
CONTENTS = APP / "Contents"
RES = CONTENTS / "Resources"
FRAMEWORKS = CONTENTS / "Frameworks"
PYFW = FRAMEWORKS / "Python.framework"
PYVER = PYFW / "Versions" / "3.10"

BREW = Path(subprocess.run(["brew", "--prefix"], capture_output=True, text=True,
                           check=True).stdout.strip())
BREW_PYFW = BREW / "opt/python@3.10/Frameworks/Python.framework/Versions/3.10"
GCC = BREW / "opt/riscv64-elf-gcc"
BINUTILS = BREW / "opt/riscv64-elf-binutils"
GMAKE = BREW / "opt/make/bin/gmake"
GEM5 = REPO / "tools/gem5"

# Files of the project that are copied into the user's workspace.
PROJECT_PATHS = ["programs", "gem5", "setup_default", "ase_studio_branches.json",
                 "simulate.py", "simulate.sh", "README.md", "LICENSE"]
# Parts of the toolchain a C/asm-only course never uses.
TOOLCHAIN_SKIP = {"cc1plus", "lto1", "riscv64-elf-lto-dump", "riscv64-elf-c++",
                  "riscv64-elf-g++", "plugin", "install-tools"}

LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")
MACHO_MAGICS = {b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}
SYSTEM_PREFIXES = ("/usr/lib/", "/System/")


def log(message: str) -> None:
    print(f"\033[1;34m==>\033[0m {message}", flush=True)


def run(*cmd, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def output(*cmd) -> str:
    return subprocess.run([str(c) for c in cmd], check=True, capture_output=True,
                          text=True).stdout


def copytree(src: Path, dst: Path, skip=frozenset()) -> None:
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True,
                    ignore=lambda _d, names: [n for n in names
                                              if n in skip or n in {"__pycache__", ".DS_Store"}])


# --------------------------------------------------------------- bundle layout
def check_inputs() -> None:
    missing = [p for p in (BREW_PYFW / "Python", GCC / "bin/riscv64-elf-gcc",
                           BINUTILS / "riscv64-elf/bin/as", GMAKE,
                           GEM5 / "build/RISCV/gem5.opt", GEM5 / "configs")
               if not p.exists()]
    if missing:
        sys.exit("Mancano:\n  " + "\n  ".join(map(str, missing)))


def studio_version() -> str:
    return (REPO / "ase_studio/VERSION").read_text().strip()


def make_skeleton() -> None:
    log(f"Creo {APP.relative_to(REPO)}")
    if APP.exists():
        shutil.rmtree(APP)
    for d in (CONTENTS / "MacOS", RES / "bin", FRAMEWORKS):
        d.mkdir(parents=True)
    run("clang", "-fobjc-arc", "-O2", "-arch", "arm64", "-mmacosx-version-min=11.3",
        "-framework", "Cocoa", "-framework", "WebKit",
        "-o", CONTENTS / "MacOS/ASEStudio", HERE / "launcher.m")
    for name in ("launcher.sh", "ase_studio_app.py"):
        shutil.copy2(HERE / name, RES / name)
    shutil.copy2(HERE / "AppIcon.icns", RES / "AppIcon.icns")


def copy_project() -> None:
    log("Progetto modello e ASE Studio")
    tracked = output("git", "-C", REPO, "ls-files", "--", *PROJECT_PATHS).splitlines()
    for rel in tracked:
        src = REPO / rel
        if src.is_file():
            dst = RES / "project" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    copytree(REPO / "ase_studio", RES / "ase_studio", skip={".git"})
    write_versions()


def github_repo(path: Path) -> str:
    url = output("git", "-C", path, "remote", "get-url", "origin").strip()
    return re.sub(r"^.*github\.com[:/]|\.git$", "", url)


def write_versions() -> None:
    """Sources the app was built from; the in-app updater compares against them."""
    branches = json.loads((REPO / "ase_studio_branches.json").read_text())
    versions = {
        "studio": {"label": "ASE Studio", "repo": github_repo(REPO / "ase_studio"),
                   "branch": branches["studio"], "commit": ""},
        "simulator": {"label": "Simulator", "repo": github_repo(REPO),
                      "branch": branches["simulator"], "commit": ""},
    }
    for key, path in (("studio", REPO / "ase_studio"), ("simulator", REPO)):
        versions[key]["commit"] = output("git", "-C", path, "rev-parse", "HEAD").strip()
    (RES / "versions.json").write_text(json.dumps(versions, indent=2) + "\n")


def copy_gem5() -> None:
    log("gem5")
    dst = RES / "gem5/build/RISCV"
    dst.mkdir(parents=True)
    shutil.copy2(GEM5 / "build/RISCV/gem5.opt", dst / "gem5.opt")
    copytree(GEM5 / "configs", RES / "gem5/configs")


def copy_python() -> None:
    log("Python 3.10")
    copytree(BREW_PYFW, PYVER, skip={"test", "idlelib", "IDLE 3.app"})
    site = PYVER / "lib/python3.10/site-packages"
    if site.is_symlink():          # Homebrew points it outside the keg
        site.unlink()
        site.mkdir()
    (PYFW / "Versions/Current").symlink_to("3.10")
    for name in ("Python", "Resources", "Headers"):
        (PYFW / name).symlink_to(f"Versions/Current/{name}")
    (RES / "bin/python3").symlink_to("../../Frameworks/Python.framework/Versions/3.10/bin/python3.10")


def copy_toolchain() -> None:
    log("Toolchain RISC-V (gcc + binutils) e GNU make")
    tc = RES / "toolchain"
    for sub in ("bin", "lib", "libexec"):
        copytree(GCC / sub, tc / sub, skip=TOOLCHAIN_SKIP)
    copytree(BINUTILS / "bin", tc / "bin")
    copytree(BINUTILS / "riscv64-elf", tc / "riscv64-elf")
    for la in tc.rglob("*.la"):
        la.unlink()
    shutil.copy2(GMAKE, RES / "bin/make")
    # /usr/bin/git is a stub that pops up the Command Line Tools installer.
    git = RES / "bin/git"
    git.write_text('#!/bin/bash\n'
                   '/usr/bin/xcode-select -p >/dev/null 2>&1 && exec /usr/bin/git "$@"\n'
                   'echo "git non disponibile (Command Line Tools non installati)" >&2\n'
                   'exit 1\n')
    git.chmod(0o755)


def write_info_plist(min_os: str) -> None:
    text = (HERE / "Info.plist").read_text().replace("__VERSION__", studio_version())
    info = plistlib.loads(text.encode())
    info["LSMinimumSystemVersion"] = min_os
    (CONTENTS / "Info.plist").write_bytes(plistlib.dumps(info))


# --------------------------------------------------------------- relinking
def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with open(path, "rb") as f:
        return f.read(4) in MACHO_MAGICS


def macho_type(path: Path) -> str:
    header = output("otool", "-hv", path).splitlines()[-1].split()
    return header[4]                     # EXECUTE / DYLIB / BUNDLE ...


def linked_libs(path: Path) -> list[str]:
    lines = output("otool", "-L", path).splitlines()[1:]
    return [line.strip().split(" (")[0] for line in lines]


def rpaths(path: Path) -> list[str]:
    text = output("otool", "-l", path)
    return re.findall(r"cmd LC_RPATH\n\s+cmdsize \d+\n\s+path (.+?) \(offset", text)


def bundled_target(dep: str) -> Path | None:
    """Where a non-system dependency lives inside the bundle (copying it if needed)."""
    if dep.startswith(SYSTEM_PREFIXES) or dep.startswith("@"):
        return None
    if "Python.framework/Versions/3.10/Python" in dep:
        return PYVER / "Python"
    src = Path(dep).resolve()
    if not src.exists():
        raise SystemExit(f"Libreria non trovata: {dep}")
    dst = FRAMEWORKS / src.name
    if not dst.exists():
        shutil.copy2(src, dst)
        dst.chmod(0o755)
    return dst


def relink_all() -> None:
    log("Ricollego le librerie (@executable_path / @loader_path)")
    done: set[Path] = set()
    while True:
        pending = [p for p in APP.rglob("*") if p not in done and is_macho(p)]
        if not pending:
            break
        for path in pending:
            done.add(path)
            kind = macho_type(path)
            anchor = "@executable_path" if kind == "EXECUTE" else "@loader_path"
            args = []
            if kind == "DYLIB":
                own_id = linked_libs(path)[0] if path.name != "ASEStudio" else ""
                if path == PYVER / "Python":
                    args += ["-id", "@rpath/Python.framework/Versions/3.10/Python"]
                elif own_id and not own_id.startswith(SYSTEM_PREFIXES):
                    args += ["-id", f"@rpath/{path.name}"]
            for dep in linked_libs(path):
                target = bundled_target(dep)
                if target is None or target == path:
                    continue
                rel = os.path.relpath(target, path.parent)
                args += ["-change", dep, f"{anchor}/{rel}"]
            for rp in rpaths(path):
                if not rp.startswith("@"):
                    args += ["-delete_rpath", rp]
            if args:
                subprocess.run(["install_name_tool", *args, str(path)], check=True,
                               stderr=subprocess.DEVNULL)


def min_macos_version() -> str:
    versions = set()
    for path in APP.rglob("*"):
        if is_macho(path):
            versions.update(re.findall(r"minos (\d+\.\d+)", output("otool", "-l", path)))
    return max(versions | {"11.3"}, key=lambda v: tuple(map(int, v.split("."))))


# --------------------------------------------------------------- signing
def sign() -> None:
    log("Firma ad-hoc")
    machos = [p for p in APP.rglob("*") if is_macho(p)]
    # Inside-out: deepest files first, then nested bundles, then the app.
    for path in sorted(machos, key=lambda p: -len(p.parts)):
        if path.parent.name == "MacOS" and path.parent.parent.name == "Contents":
            continue                           # main executables: signed with their bundle
        run("codesign", "--force", "--sign", "-", "--timestamp=none", path,
            stderr=subprocess.DEVNULL)
    for bundle in (PYVER / "Resources/Python.app", PYVER, APP):
        run("codesign", "--force", "--sign", "-", "--timestamp=none", bundle,
            stderr=subprocess.DEVNULL)
    run("codesign", "--verify", "--deep", "--strict", APP)
    # The bundle folder existed before Info.plist and the icon were written:
    # make Finder/Dock pick up the icon instead of a cached generic one.
    run("touch", APP)
    subprocess.run([LSREGISTER, "-f", str(APP)], check=False)


def audit() -> None:
    log("Verifica: nessun riferimento a percorsi esterni")
    bad = []
    for path in APP.rglob("*"):
        if is_macho(path):
            for dep in linked_libs(path)[1:] + rpaths(path):
                if not dep.startswith(SYSTEM_PREFIXES) and not dep.startswith("@"):
                    bad.append(f"{path.relative_to(APP)} -> {dep}")
    if bad:
        raise SystemExit("Riferimenti non ricollegati:\n  " + "\n  ".join(bad))


# --------------------------------------------------------------- dmg
def make_dmg() -> Path:
    dmg = DIST / f"ASE-Studio-{studio_version()}.dmg"
    log(f"Creo {dmg.relative_to(REPO)}")
    staging = DIST / "dmg"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    run("ditto", APP, staging / APP.name)
    (staging / "Applications").symlink_to("/Applications")
    shutil.copy2(HERE / "LEGGIMI.txt", staging / "LEGGIMI.txt")
    dmg.unlink(missing_ok=True)
    run("hdiutil", "create", "-volname", "ASE Studio", "-srcfolder", staging,
        "-fs", "HFS+", "-format", "ULFO", "-ov", dmg, stdout=subprocess.DEVNULL)
    shutil.rmtree(staging)
    run("codesign", "--force", "--sign", "-", dmg)
    return dmg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-dmg", action="store_true", help="build only the .app")
    args = parser.parse_args()

    check_inputs()
    make_skeleton()
    copy_project()
    copy_gem5()
    copy_python()
    copy_toolchain()
    relink_all()
    audit()
    min_os = min_macos_version()
    write_info_plist(min_os)
    sign()
    size = output("du", "-sh", APP).split()[0]
    log(f"App pronta: {APP} ({size}, richiede macOS {min_os}+)")
    if not args.no_dmg:
        dmg = make_dmg()
        log(f"DMG pronto: {dmg} ({output('du', '-sh', dmg).split()[0]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
