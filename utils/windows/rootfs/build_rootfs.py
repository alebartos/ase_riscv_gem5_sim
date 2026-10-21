#!/usr/bin/env python3
"""Build the WSL2 rootfs for the Windows app and export it as a flat tar.

Mirrors utils/macOS/build_app.py's version bookkeeping (write_versions,
simulator_commit) but produces a Docker-built Debian rootfs instead of an
.app bundle: gem5 cannot be compiled on Windows, so everything (gem5, the
RISC-V toolchain, ASE Studio) runs inside WSL2 instead (see HANDOFF.md).

Build requirements (on the build machine only): Docker.

Usage: utils/windows/rootfs/build_rootfs.py [--studio-repo OWNER/REPO]
           [--studio-branch BRANCH] [--studio-dir PATH]
           [--simulator-repo OWNER/REPO] [--gem5-repo OWNER/REPO]
Output: dist/ase-studio-rootfs.tar (fed to `wsl --import`),
        dist/gem5-linux-x86_64.tar.gz + dist/gem5.json (gem5 in-app update,
        to attach to a GitHub release named GEM5_RELEASE)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # utils/windows/rootfs
WINDOWS_DIR = HERE.parent
REPO = WINDOWS_DIR.parents[1]
DIST = REPO / "dist"
STUDIO_SRC = DIST / "ase_studio-src"
IMAGE = "ase-studio-rootfs"
CONTAINER = "ase-studio-rootfs-export"

# Pinned in the Dockerfile; kept here so versions.json records the same id.
GEM5_REF = "stable"
GEM5_COMMIT = "fc0ef46"
GEM5_PATCHES = WINDOWS_DIR / "gem5_patches"


def gem5_build_id() -> str:
    """gem5 source commit plus the Python 3.11 patches applied on top of it."""
    patches = hashlib.sha256()
    for patch in sorted(GEM5_PATCHES.glob("*.patch")):
        patches.update(patch.read_bytes())
    return f"{GEM5_REF}@{GEM5_COMMIT}+{patches.hexdigest()[:12]}"

SIMULATOR_REPO = "cad-polito-it/ase_riscv_gem5_sim"
STUDIO_REPO = "alebartos/ase_riscv_gem5_sim"
STUDIO_BRANCH = "ase-studio-macos"          # also the branch used on Windows
GEM5_RELEASE = "gem5-linux-x86_64"
GEM5_ASSET = DIST / f"{GEM5_RELEASE}.tar.gz"
GEM5_MANIFEST = DIST / "gem5.json"
APP_RELEASE_TAG = "v{version}-windows"
_studio: dict = {}


def log(message: str) -> None:
    print(f"\033[1;34m==>\033[0m {message}", flush=True)


def run(*cmd, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def output(*cmd) -> str:
    return subprocess.run([str(c) for c in cmd], check=True, capture_output=True,
                          text=True).stdout


def studio_version() -> str:
    return (STUDIO_SRC / "VERSION").read_text().strip()


def prepare_studio(repo: str, branch: str, local_dir: str | None) -> None:
    """Export the ASE Studio sources the rootfs is built from into STUDIO_SRC.

    Same approach as build_app.py: by default fetch `branch` from `repo` so
    the bundled commit is one the in-app updater can find; --studio-dir
    builds a local checkout's committed HEAD instead."""
    if local_dir:
        source = Path(local_dir).resolve()
        commit = output("git", "-C", source, "rev-parse", "HEAD").strip()
        if output("git", "-C", source, "status", "--porcelain", "--untracked-files=no").strip():
            log(f"Warning: {source} has uncommitted changes, using HEAD only")
    else:
        source = REPO
        for remote in output("git", "-C", REPO, "remote").split():
            url = output("git", "-C", REPO, "remote", "get-url", remote).strip()
            if re.sub(r"^.*github\.com[:/]|\.git$", "", url) == repo:
                run("git", "-C", REPO, "fetch", "--quiet", remote, branch)
                break
        else:
            sys.exit(f"No remote of this clone points to {repo}: add it or use --studio-dir")
        commit = output("git", "-C", REPO, "rev-parse", "FETCH_HEAD").strip()
    log(f"ASE Studio from {repo}@{branch} ({commit[:7]})")
    archive = subprocess.run(["git", "-C", str(source), "archive", "--format=tar", commit],
                             check=True, capture_output=True).stdout
    if STUDIO_SRC.exists():
        shutil.rmtree(STUDIO_SRC)
    STUDIO_SRC.mkdir(parents=True)
    with tarfile.open(fileobj=__import__("io").BytesIO(archive)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(STUDIO_SRC, filter="data")
        else:
            tar.extractall(STUDIO_SRC)
    if not (STUDIO_SRC / "backend.py").is_file():
        sys.exit(f"{repo}@{branch} does not contain ASE Studio (backend.py is missing)")
    _studio.update(repo=repo, branch=branch, commit=commit)


def simulator_commit(repo: str, branch: str) -> str:
    """The commit of `repo` this clone is based on (see build_app.py)."""
    for remote in output("git", "-C", REPO, "remote").split():
        url = output("git", "-C", REPO, "remote", "get-url", remote).strip()
        if re.sub(r"^.*github\.com[:/]|\.git$", "", url) == repo:
            run("git", "-C", REPO, "fetch", "--quiet", remote, branch)
            return output("git", "-C", REPO, "merge-base", "HEAD", "FETCH_HEAD").strip()
    return output("git", "-C", REPO, "rev-parse", "HEAD").strip()


def github_repo(path: Path) -> str:
    url = output("git", "-C", path, "remote", "get-url", "origin").strip()
    return re.sub(r"^.*github\.com[:/]|\.git$", "", url)


def write_versions(simulator_repo: str, gem5_repo: str) -> None:
    branches = json.loads((REPO / "ase_studio_branches.json").read_text())
    versions = {
        "studio": {"label": "ASE Studio", "repo": _studio["repo"],
                   "branch": _studio["branch"], "commit": _studio["commit"]},
        "simulator": {"label": "Simulator", "repo": simulator_repo,
                      "branch": branches["simulator"],
                      "commit": simulator_commit(simulator_repo, branches["simulator"])},
        "gem5": {"label": "gem5", "repo": gem5_repo, "release": GEM5_RELEASE,
                 "commit": gem5_build_id()},
        "app": {"label": "ASE Studio app", "repo": gem5_repo,
                "tag": APP_RELEASE_TAG.format(version=studio_version())},
    }
    (HERE / "versions.json").write_text(json.dumps(versions, indent=2) + "\n")


def build_image() -> None:
    log(f"docker build ({IMAGE})")
    run("docker", "build", "-f", HERE / "Dockerfile",
        "--build-arg", f"ASE_STUDIO_DIR={STUDIO_SRC.relative_to(REPO).as_posix()}",
        "--build-arg", f"GEM5_REF={GEM5_REF}", "--build-arg", f"GEM5_COMMIT={GEM5_COMMIT}",
        "-t", IMAGE, REPO, cwd=REPO)


def export_rootfs() -> Path:
    log("Exporting the rootfs tar")
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    run("docker", "create", "--name", CONTAINER, IMAGE)
    DIST.mkdir(exist_ok=True)
    tar_path = DIST / "ase-studio-rootfs.tar"
    with open(tar_path, "wb") as f:
        subprocess.run(["docker", "export", CONTAINER], check=True, stdout=f)
    run("docker", "rm", CONTAINER)
    return tar_path


def make_gem5_update() -> None:
    """Package gem5.opt from the built image for the in-app gem5 update."""
    log(f"gem5 update: {GEM5_ASSET.relative_to(REPO)}")
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    run("docker", "create", "--name", CONTAINER, IMAGE)
    DIST.mkdir(exist_ok=True)
    extracted = DIST / "gem5.opt"
    run("docker", "cp", f"{CONTAINER}:/opt/ase/gem5/build/RISCV/gem5.opt", extracted)
    run("docker", "rm", CONTAINER)
    with tarfile.open(GEM5_ASSET, "w:gz") as tar:
        tar.add(extracted, arcname="gem5.opt")
    extracted.unlink()
    manifest = {"id": gem5_build_id(), "asset": GEM5_ASSET.name,
                "sha256": hashlib.sha256(GEM5_ASSET.read_bytes()).hexdigest(),
                "app": studio_version()}
    GEM5_MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--simulator-repo", default=SIMULATOR_REPO)
    parser.add_argument("--gem5-repo", default=None)
    parser.add_argument("--studio-repo", default=STUDIO_REPO)
    parser.add_argument("--studio-branch", default=STUDIO_BRANCH)
    parser.add_argument("--studio-dir", default=None)
    parser.add_argument("--skip-build", action="store_true",
                        help="only (re)write versions.json, reuse the last docker build")
    args = parser.parse_args()

    prepare_studio(args.studio_repo, args.studio_branch, args.studio_dir)
    write_versions(args.simulator_repo, args.gem5_repo or github_repo(REPO))
    if not args.skip_build:
        build_image()
    rootfs = export_rootfs()
    log(f"rootfs ready: {rootfs} ({output('du', '-sh', rootfs).split()[0] if shutil.which('du') else rootfs.stat().st_size})")
    make_gem5_update()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
