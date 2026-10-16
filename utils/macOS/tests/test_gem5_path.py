"""Check the gem5 path contract the installed macOS apps rely on.

ase_studio_app.py (inside every installed app, updated only with a new
.dmg) runs the gem5 installed by "Update" only when settings_env() returns
the app's bundled gem5 *build directory*. If ASE Studio returned the
executable instead, installed apps would silently keep the bundled gem5.

Environment: ASE_STUDIO_DIR (the ase-studio checkout), ASE_STUDIO_HOST_ROOT.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.environ["ASE_STUDIO_DIR"])
import backend  # noqa: E402

# Neither a RISC-V toolchain nor a setup_default is needed for this check.
backend.toolchain_executables = lambda value: (Path("/usr/bin/gcc"), Path("/usr/bin/objdump"))
backend.environment_overrides = lambda: {}
configured_gem5 = {}
backend.setup_environment = lambda: {
    "RISCV_TOOLCHAIN_PATH": "/usr/bin",
    "GEM5_SIMULATION_SCRIPT": "gem5/riscv_minor_custom.py",
    "GEM5_INSTALLATION_PATH": configured_gem5["value"],
}

with tempfile.TemporaryDirectory() as tmp:
    build = Path(tmp) / "gem5" / "build"
    executable = build / backend.GEM5_ISA / f"gem5.{backend.GEM5_VARIANT}"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)

    configured_gem5["value"] = str(build) + "/"
    configured = Path(backend.settings_env()["GEM5_INSTALLATION_PATH"])
    assert configured.resolve() == build.resolve(), (
        f"settings_env() returned {configured}, not the build directory {build}: "
        "installed macOS apps would ignore the gem5 installed by Update")

    # An explicit executable or wrapper path is kept as it is.
    configured_gem5["value"] = str(executable)
    configured = Path(backend.settings_env()["GEM5_INSTALLATION_PATH"])
    assert configured.resolve() == executable.resolve(), configured

print("gem5 build directory kept for the macOS app OK")
