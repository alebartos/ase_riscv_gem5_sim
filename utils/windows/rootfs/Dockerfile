# Builds the WSL2 rootfs for ASE Studio on Windows ("ASEStudio" distro).
#
# gem5 does not build on Windows (POSIX, no MSVC/MinGW) and the ASE Studio
# backend uses bash/make/process groups, so the Windows app ships a small
# Debian rootfs that runs everything inside WSL2 instead.
#
# Two stages:
#   builder -> compiles gem5.opt for RISCV (the slow part, cached by Docker
#              as long as the pinned commit below does not change).
#   final   -> the actual rootfs: runtime libs only, the RISC-V toolchain,
#              gem5.opt + configs, ASE Studio and the project template.
#
# Usage: utils/windows/rootfs/build_rootfs.sh (see that script: it runs this
# Dockerfile and exports the final stage as a flat tar for `wsl --import`).
#
# Build context: must be the repository root (REPO), because this Dockerfile
# copies simulator files straight out of the checkout:
#   docker build -f utils/windows/rootfs/Dockerfile \
#     --build-arg ASE_STUDIO_DIR=ase_studio -t ase-studio-rootfs .

ARG DEBIAN_RELEASE=bookworm-slim

########################################################################
# Stage 1: build gem5.opt (RISCV ISA) from cad-polito-it/gem5, stable.
########################################################################
FROM debian:${DEBIAN_RELEASE} AS builder

# Pin the commit the Windows app ships, same gem5 fork/branch used by the
# macOS build (see HANDOFF.md). Bump this to pick up a newer gem5.
ARG GEM5_REPO=https://github.com/cad-polito-it/gem5.git
ARG GEM5_REF=stable
ARG GEM5_COMMIT=fc0ef46

RUN apt-get update && apt-get install -y --no-install-recommends \
      git build-essential m4 scons zlib1g-dev \
      libprotobuf-dev protobuf-compiler libprotoc-dev \
      libgoogle-perftools-dev libboost-all-dev libhdf5-dev \
      python3 python3-dev python3-six pybind11-dev \
      pkg-config libpng-dev libelf-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --branch "${GEM5_REF}" --single-branch "${GEM5_REPO}" gem5 \
    && cd gem5 && git checkout "${GEM5_COMMIT}"

# Python 3.11 (Debian 12) compatibility patches on top of this gem5 commit,
# which predates 3.11 support (the official build scripts target 3.10):
#   0001/0002: two ply lexers (ARM fastmodel's, the RISCV ISA parser's) use
#     an inline "(?m)" flag that, once ply concatenates it with the other
#     token regexes, lands away from position 0 -- `re` only started
#     rejecting that in Python 3.11.
#   0003: those same (?m) flags were not just redundant -- on pre-3.11
#     Python, an inline global flag found *anywhere* in ply's combined
#     master regex applied to the *whole* regex. The ISA parser relies on
#     that quirk: three of its token rules use '^' (requires MULTILINE) but
#     never declare the flag themselves. Simply deleting the (?m)s (0001/
#     0002) would silently break those rules, so grammar.py now requests
#     MULTILINE explicitly via ply.lex.lex()'s reflags.
#   0004: inspect.getargspec(), used by SimObject.py, was removed in 3.11;
#     replaced with its documented successor, getfullargspec().
# The vendored ext/pybind11 is also too old for Python 3.11's opaque
# PyFrameObject/PyCodeObject; dropping it falls back to the Debian
# pybind11-dev package (2.10.3, 3.11-compatible) on the include path.
COPY utils/windows/gem5_patches/ /tmp/gem5_patches/
RUN cd /src/gem5 && rm -rf ext/pybind11/include \
    && git apply /tmp/gem5_patches/*.patch

# RISCV-only build keeps this reasonable (still slow: ~20-40 min).
RUN cd /src/gem5 && scons build/RISCV/gem5.opt -j"$(nproc)" --ignore-style

########################################################################
# Stage 2: the rootfs shipped inside the Windows app.
########################################################################
FROM debian:${DEBIAN_RELEASE} AS final

ARG ASE_STUDIO_DIR=ase_studio
ARG GEM5_COMMIT=fc0ef46

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-minimal libpython3.11 make curl ca-certificates \
      gcc-riscv64-unknown-elf binutils-riscv64-unknown-elf \
      libprotobuf32 libgoogle-perftools4 libhdf5-103-1 libhdf5-cpp-103-1 \
      libpng16-16 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash ase \
    && mkdir -p /opt/ase

# gem5.opt + the gem5 source's own config scripts (common/, needed on
# PYTHONPATH by the course's custom RISCV configs below).
COPY --from=builder /src/gem5/build/RISCV/gem5.opt /opt/ase/gem5/build/RISCV/gem5.opt
COPY --from=builder /src/gem5/configs/ /opt/ase/gem5/configs/

# Project template handed to new workspaces (mirrors PROJECT_PATHS in
# utils/macOS/build_app.py); student projects are never overwritten.
# "gem5/" here is this repo's top-level dir (the course's RISCV configs),
# not the gem5 source tree copied above.
COPY programs/ /opt/ase/project/programs/
COPY gem5/ /opt/ase/project/gem5/
COPY setup_default /opt/ase/project/setup_default
COPY ase_studio_branches.json /opt/ase/project/ase_studio_branches.json
COPY simulate.py /opt/ase/project/simulate.py
COPY simulate.sh /opt/ase/project/simulate.sh
COPY README.md /opt/ase/project/README.md
COPY LICENSE /opt/ase/project/LICENSE

# ASE Studio itself (the ase_studio submodule checkout, ase-studio-macos
# branch -- that branch is also the one used on Windows, see HANDOFF.md).
COPY ${ASE_STUDIO_DIR}/ /opt/ase/ase_studio/

COPY utils/windows/ase_studio_wsl.py /opt/ase/ase_studio_wsl.py
COPY utils/windows/start.sh /opt/ase/start.sh
COPY utils/macOS/gem5_selftest.py /opt/ase/gem5_selftest.py
COPY utils/windows/rootfs/versions.json /opt/ase/versions.json
COPY utils/windows/rootfs/wsl.conf /etc/wsl.conf

RUN chmod +x /opt/ase/start.sh \
    && find /opt/ase/project/gem5 /opt/ase/gem5/configs -name "*.sh" -exec chmod +x {} \; \
    && rm -rf /opt/ase/ase_studio/.git /opt/ase/ase_studio/__pycache__ \
    && chown -R ase:ase /opt/ase

USER ase
WORKDIR /home/ase
