#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Behnam Farnaghinejad <behnam.farnaghinejad@polito.it>
# SPDX-License-Identifier: GPL-2.0-only
"""Small localhost-only backend for the experimental ASE Studio."""
from __future__ import annotations

import ast
import atexit
import base64
import binascii
import csv
from copy import deepcopy
import hashlib
import io
import json
import os
import re
import signal
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import warnings
import webbrowser
import zipfile
from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from math import gcd
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STUDIO_ROOT = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ASE_STUDIO_HOST_ROOT", STUDIO_ROOT.parent)).resolve()
PROGRAMS = ROOT / "programs"
SUBMISSIONS = ROOT / "submissions"
RESULTS = ROOT / "results"
FRONTEND = STUDIO_ROOT / "frontend"
STUDIO_VERSION = (STUDIO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
ENVIRONMENT_CONFIG = ROOT / ".ase-studio-env.json"
ENVIRONMENT_FIELDS = (
    "PROGRAMS_DIRECTORY", "SUBMISSION_DIRECTORY",
    "RISCV_TOOLCHAIN_PATH", "OPTIMIZATION_FLAGS",
    "GEM5_INSTALLATION_PATH",
    "PIPELINE_DISPLAY_CYCLE_LIMIT", "SUBMISSION_NAME_PREFIX",
    "SUBMISSION_NAME_SUFFIX",
)
OPTIONAL_ENVIRONMENT_FIELDS = {
    "OPTIMIZATION_FLAGS", "SUBMISSION_NAME_PREFIX", "SUBMISSION_NAME_SUFFIX",
}
ENVIRONMENT_PATH_FIELDS = {
    "PROGRAMS_DIRECTORY", "SUBMISSION_DIRECTORY",
    "RISCV_TOOLCHAIN_PATH", "GEM5_INSTALLATION_PATH",
}
PORTABLE_ENVIRONMENT_VARIABLES = {
    "HOME": str(Path.home()),
    "USER": os.environ.get("USER", ""),
    "ASE_STUDIO_ROOT": str(ROOT),
}
ASSIGNMENT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
STUDIO_API_VERSION = 2
PIPELINE_DISPLAY_CYCLE_LIMIT = 3000
MAX_SUBMISSION_PROJECTS = 20
MAX_SUBMISSION_ATTACHMENTS = 20
MAX_SUBMISSION_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_SUBMISSION_TOTAL_ATTACHMENT_BYTES = 100 * 1024 * 1024
GEM5_ISA = "RISCV"
GEM5_VARIANT = "opt"
OFFICIAL_GEM5_REPOSITORY = "github.com/cad-polito-it/gem5"
REQUIRED_BRANCHES_FILE = ROOT / "ase_studio_branches.json"
DEVELOPER_MODE_ENVIRONMENT_VARIABLE = "ASE_STUDIO_DEVELOPER"


def developer_mode_enabled(environment=None):
    """Return whether the opt-in developer environment variable is defined."""
    environment = os.environ if environment is None else environment
    return DEVELOPER_MODE_ENVIRONMENT_VARIABLE in environment


DEVELOPER_MODE = developer_mode_enabled()
ENABLE_MEMORY_CONFIGURATION = DEVELOPER_MODE
ENABLE_MULTI_ISSUE_CPU = True

_ACTIVE_COMMANDS = set()
_ACTIVE_COMMANDS_LOCK = threading.Lock()
_COMMAND_SHUTDOWN = threading.Event()

LOCAL_SETUP_PATHSPEC = "setup_default*"
LOCAL_PROGRAM_PATHSPEC = "programs/**"
LOCAL_PARENT_PATHSPECS = (LOCAL_SETUP_PATHSPEC, LOCAL_PROGRAM_PATHSPEC)
SCAFFOLD_COMMENTS = {
    "# The text section contains the instructions that the CPU runs.",
    "# Make _start visible as the point where the program begins.",
    "# The End block stops the program and returns control to the simulator.",
}
DEFAULT_PREDICTOR_PARAMETERS = {
    "local": {"localPredictorSize": 32, "localCtrBits": 2, "BTBEntries": 256},
    "tournament": {
        "localPredictorSize": 2048, "localCtrBits": 2,
        "localHistoryTableSize": 2048, "globalPredictorSize": 8192,
        "globalCtrBits": 2, "choicePredictorSize": 8192,
        "choiceCtrBits": 2, "BTBEntries": 256,
    },
    "bimode": {
        "globalPredictorSize": 8192, "globalCtrBits": 2,
        "choicePredictorSize": 8192, "choiceCtrBits": 2,
        "BTBEntries": 256,
    },
    "tage": {
        "minHist": 5, "maxHist": 130,
        "tagTableCounterBits": 3, "tagTableUBits": 2,
        "tagTableTagWidths": [0, 9, 9, 10, 10, 11, 11, 12],
        "logTagTableSizes": [13, 9, 9, 9, 9, 9, 9, 9],
        "BTBEntries": 4096,
    },
}
DEFAULT_CONFIG = {
    "cpu": "in-order", "intAlu": 1, "intMul": 1, "intDiv": 1,
    "floatAlu": 2, "floatMul": 7, "floatDiv": 8,
    "intAluPipelined": True, "intMulPipelined": True,
    "intDivPipelined": True, "floatAluPipelined": True,
    "floatMulPipelined": True, "floatDivPipelined": False,
    "forwarding": True, "compressedInstructions": False,
    "floatingPointPrecision": "single",
    "memoryMode": "direct", "cacheStalls": False,
    "instructionMemoryLatency": 1, "dataReadLatency": 1,
    "dataWriteLatency": 1,
    "iCacheSize": "32kB", "dCacheSize": "32kB", "cacheLine": 64,
    "cacheLatency": 2, "memoryLatency": 30,
    "fetchWidth": 2, "decodeWidth": 2, "renameWidth": 2,
    "dispatchWidth": 2, "issueWidth": 2, "writebackWidth": 2,
    "commitWidth": 2, "robEntries": 64, "iqEntries": 128,
    "lqEntries": 32, "sqEntries": 32,
    "intAluUnits": 1, "intMulUnits": 1, "intDivUnits": 1,
    "floatAluUnits": 1, "floatMulUnits": 1, "floatDivUnits": 1,
    "memoryUnits": 1,
    "o3VisibleStages": ["issue", "execute", "memory", "cdb", "commit"],
    "branchPredictor": "local",
    "branchPredictorParameters": DEFAULT_PREDICTOR_PARAMETERS,
    "speculativeExecution": True,
    "outOfOrderExecution": True, "o3EvaluationLabel": "",
}

# The multiple-issue table models the examination architecture. gem5 still
# needs finite queues to generate the architectural instruction/value trace;
# these internal sizes never constrain the table's unlimited ROB and RS.
O3_FIXED_CONFIG = {
    "issueWidth": 8,
    "robEntries": 64, "iqEntries": 128, "lqEntries": 32, "sqEntries": 32,
    "speculativeExecution": True,
    "outOfOrderExecution": True, "forwarding": True,
}
DEFAULT_O3_CONFIG = {
    **DEFAULT_CONFIG, **O3_FIXED_CONFIG,
    "cpu": "out-of-order", "floatMul": 6, "floatDiv": 6,
    "intMul": 1, "intDiv": 1,
    "intMulPipelined": True, "intDivPipelined": True,
    "branchPredictor": "ideal",
}


def fail(message, status=400):
    raise ValueError((message, status))


def validate_predictor_parameters(value):
    """Keep project predictor settings within this gem5 build's safe limits."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(DEFAULT_PREDICTOR_PARAMETERS):
        fail("Invalid branch predictor parameters.")
    parameters = deepcopy(DEFAULT_PREDICTOR_PARAMETERS)
    table_sizes = {"localPredictorSize", "localHistoryTableSize",
                   "globalPredictorSize", "choicePredictorSize", "BTBEntries"}
    counter_bits = {"localCtrBits", "globalCtrBits", "choiceCtrBits",
                    "tagTableCounterBits", "tagTableUBits"}
    for predictor, supplied in value.items():
        if not isinstance(supplied, dict) or set(supplied) - set(parameters[predictor]):
            fail(f"Invalid {predictor} predictor parameters.")
        for key, number in supplied.items():
            if key in {"tagTableTagWidths", "logTagTableSizes"}:
                if (not isinstance(number, list) or len(number) != 8
                        or any(not isinstance(item, int) or isinstance(item, bool)
                               for item in number)):
                    fail("TAGE needs eight integer values for each table array (base plus seven tagged tables).")
                parameters[predictor][key] = number.copy()
                continue
            if not isinstance(number, int) or isinstance(number, bool):
                fail(f"{predictor} {key} must be a whole number.")
            if key in table_sizes:
                if not 2 <= number <= 65536 or number & (number - 1):
                    fail(f"{predictor} {key} must be a power of two from 2 to 65536.")
            elif key in counter_bits:
                maximum = 2 if key == "tagTableUBits" else 8
                if not 1 <= number <= maximum:
                    fail(f"{predictor} {key} must be between 1 and {maximum} bits.")
            elif key in {"minHist", "maxHist"}:
                if not 1 <= number <= 8192:
                    fail("TAGE history lengths must be between 1 and 8192.")
            parameters[predictor][key] = number
    local = parameters["local"]
    local_entries = local["localPredictorSize"] // local["localCtrBits"]
    if (local["localPredictorSize"] % local["localCtrBits"]
            or not local_entries or local_entries & (local_entries - 1)):
        fail("Local predictor storage divided by counter bits must give a power-of-two number of entries.")
    tage = parameters["tage"]
    if tage["maxHist"] <= tage["minHist"]:
        fail("TAGE maximum history must exceed its minimum history.")
    tags = tage["tagTableTagWidths"]
    sizes = tage["logTagTableSizes"]
    if tags[0] != 0 or any(not 2 <= width <= 16 for width in tags[1:]):
        fail("TAGE base tag width must be 0; tagged widths must be 2–16 bits.")
    if any(not max(4, index + 1) <= size <= 16
           for index, size in enumerate(sizes)):
        fail("TAGE table log2 sizes must be 4–16 and exceed their tagged-table index.")
    return parameters


def validate_project_name(name: str) -> str:
    """Validate one portable UI label backed by a Linux directory entry."""
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or "/" in name or "\0" in name):
        fail("Enter a non-empty project name without '/'.")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        fail("Project names cannot contain control characters.")
    if len(os.fsencode(name)) > 255:
        fail("Project names cannot exceed 255 bytes.")
    return name


def active_programs_directory() -> Path:
    """Return the configured project-library directory."""
    configured = environment_overrides().get("PROGRAMS_DIRECTORY", "")
    return (resolve_environment_path(configured)
            if configured else PROGRAMS.resolve())


def active_submission_directory() -> Path:
    """Return the configured root directory for generated submission ZIPs."""
    configured = environment_overrides().get("SUBMISSION_DIRECTORY", "")
    return (resolve_environment_path(configured)
            if configured else SUBMISSIONS.resolve())


def project_path(name: str) -> Path:
    name = validate_project_name(name)
    programs = active_programs_directory()
    path = (programs / name).resolve()
    if path.parent != programs:
        fail("Invalid project name.")
    return path


def project_results_directory(name: str) -> Path:
    """Keep results from separate project libraries from colliding."""
    name = validate_project_name(name)
    programs = active_programs_directory()
    if programs == PROGRAMS.resolve():
        root = RESULTS
    else:
        library_id = hashlib.sha256(os.fsencode(programs)).hexdigest()[:12]
        root = RESULTS / "program-libraries" / library_id
    return (root / name).resolve()


def project_dir(name: str) -> Path:
    path = project_path(name)
    if not path.is_dir():
        fail("Project not found.", 404)
    return path


def artifact_stem(folder: Path) -> str:
    """Return the Makefile artifact stem, independent of the project folder."""
    return source_file(folder).stem


def rename_project(old_name: str, new_name: str):
    source = project_dir(old_name)
    destination = project_path(new_name)
    if source == destination:
        return {"ok": True, "name": new_name}
    if destination.exists():
        fail("A project with that name already exists.")
    old_results = project_results_directory(old_name)
    new_results = project_results_directory(new_name)
    if new_results.exists():
        fail("Generated results already exist for that project name.")
    source.rename(destination)
    if old_results.is_dir():
        old_results.rename(new_results)
    return {"ok": True, "name": new_name}


def duplicate_project(source_name: str, new_name: str):
    """Copy an editable project without carrying over build artifacts."""
    source = project_dir(source_name)
    destination = project_path(new_name)
    if destination.exists():
        fail("A project with that name already exists.")
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("*.elf", "*.dump", "*.o", "__pycache__"),
    )
    return {"ok": True, "name": new_name}


def source_file(folder: Path) -> Path:
    files = sorted(list(folder.glob("*.s")) + list(folder.glob("*.S")))
    if not files:
        fail("This project has no assembly source file.")
    # Match the assembly selected by the Makefile when it declares ASM = ./file.s.
    makefile = folder / "Makefile"
    if makefile.exists():
        match = re.search(r"^\s*ASM\s*=\s*\.?/?([^\s#]+)", makefile.read_text(errors="replace"), re.M)
        if match:
            selected = (folder / match.group(1)).resolve()
            if selected in files:
                return selected
    return files[0]


def validate_config(value):
    if not isinstance(value, dict):
        fail("Invalid CPU configuration.")
    if value.get("cpu") not in {"in-order", "out-of-order"}:
        fail("Select Five-Stage Pipeline or Superscalar.")
    multiple_issue = ENABLE_MULTI_ISSUE_CPU and value["cpu"] == "out-of-order"
    config = (DEFAULT_O3_CONFIG if multiple_issue else DEFAULT_CONFIG).copy()
    if multiple_issue:
        value = {**DEFAULT_O3_CONFIG, **value, **O3_FIXED_CONFIG}
    config["cpu"] = (value["cpu"] if ENABLE_MULTI_ISSUE_CPU
                     else DEFAULT_CONFIG["cpu"])
    for key in ("intAlu", "intMul", "intDiv", "floatAlu", "floatMul", "floatDiv"):
        number = value.get(key)
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 100:
            fail("Operation latencies must be whole numbers from 1 to 100 cycles.")
        config[key] = number
    legacy_pipelining = ({
        "intAluPipelined": True, "intMulPipelined": True,
        "intDivPipelined": True, "floatAluPipelined": True,
        "floatMulPipelined": True, "floatDivPipelined": False,
    } if config["cpu"] == "in-order" else {
        "intAluPipelined": False, "intMulPipelined": False,
        "intDivPipelined": False, "floatAluPipelined": True,
        "floatMulPipelined": True, "floatDivPipelined": True,
    })
    for key in legacy_pipelining:
        option = value.get(key, legacy_pipelining[key])
        if not isinstance(option, bool):
            fail("Functional-unit pipelining options must be enabled or disabled.")
        config[key] = option
    if not isinstance(value.get("forwarding"), bool):
        fail("Invalid CPU option.")
    config["forwarding"] = value["forwarding"]
    compressed = value.get("compressedInstructions", False)
    if not isinstance(compressed, bool):
        fail("The compressed-instruction option must be enabled or disabled.")
    config["compressedInstructions"] = compressed
    precision = value.get("floatingPointPrecision", "single")
    if precision not in {"single", "double"}:
        fail("Select single- or double-precision floating point.")
    config["floatingPointPrecision"] = precision
    memory_mode = value.get("memoryMode")
    legacy_ideal = (memory_mode == "ideal"
                    or (memory_mode is None and not value.get("cacheStalls")))
    if memory_mode is None:
        memory_mode = "cache" if value.get("cacheStalls") else "direct"
    elif legacy_ideal:
        # Ideal memory was identical to Direct memory with all latencies set
        # to one. Keep old project files usable while removing the duplicate
        # mode from the current configuration model.
        memory_mode = "direct"
    if memory_mode not in {"direct", "cache"}:
        fail("Select Direct memory or Cache memory mode.")
    config["memoryMode"] = memory_mode
    # Retain the old field so existing project files and older frontends can
    # still understand whether the detailed cache model is active.
    config["cacheStalls"] = memory_mode == "cache"
    for key in ("instructionMemoryLatency", "dataReadLatency", "dataWriteLatency"):
        number = value.get(key, config[key])
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 1000:
            fail("Direct-memory latencies must be whole numbers from 1 to 1000 cycles.")
        config[key] = number
    if legacy_ideal:
        config["instructionMemoryLatency"] = 1
        config["dataReadLatency"] = 1
        config["dataWriteLatency"] = 1
    for key in ("iCacheSize", "dCacheSize"):
        size = value.get(key, config[key])
        if not isinstance(size, str) or not re.fullmatch(r"[1-9][0-9]*(?:kB|MB)", size):
            fail("Cache sizes must use values such as 32kB or 1MB.")
        config[key] = size
    for key, minimum, maximum in (("cacheLine", 16, 512), ("cacheLatency", 1, 100),
                                  ("memoryLatency", 1, 1000)):
        number = value.get(key, config[key])
        if not isinstance(number, int) or isinstance(number, bool) or not minimum <= number <= maximum:
            fail("Memory configuration contains an invalid numeric value.")
        config[key] = number
    for key in ("fetchWidth", "decodeWidth", "renameWidth", "dispatchWidth",
                "issueWidth", "writebackWidth", "commitWidth"):
        number = value.get(key, config[key])
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 8:
            fail("Multiple-issue pipeline widths must be whole numbers from 1 to 8.")
        config[key] = number
    for key in ("intAluUnits", "intMulUnits", "intDivUnits",
                "floatAluUnits", "floatMulUnits", "floatDivUnits",
                "memoryUnits"):
        number = value.get(key, config[key])
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 8:
            fail("Functional-unit counts must be whole numbers from 1 to 8.")
        config[key] = number
    for key, minimum, maximum in (("robEntries", 16, 512), ("iqEntries", 8, 2048),
                                  ("lqEntries", 4, 256), ("sqEntries", 4, 256)):
        number = value.get(key, config[key])
        if not isinstance(number, int) or isinstance(number, bool) or not minimum <= number <= maximum:
            fail("Multiple-issue queue sizes contain an invalid value.")
        config[key] = number
    allowed_o3_stages = {
        "fetch", "decode", "rename", "issue", "execute", "memory", "cdb", "commit"
    }
    visible_stages = value.get("o3VisibleStages", config["o3VisibleStages"])
    if (not isinstance(visible_stages, list) or not visible_stages
            or any(not isinstance(stage, str) or stage not in allowed_o3_stages
                   for stage in visible_stages)
            or len(set(visible_stages)) != len(visible_stages)):
        fail("Select at least one valid multiple-issue pipeline stage to display.")
    config["o3VisibleStages"] = [stage for stage in (
        "fetch", "decode", "rename", "issue", "execute", "memory", "cdb", "commit"
    ) if stage in visible_stages]
    predictor_name = value.get("branchPredictor", config["branchPredictor"])
    if predictor_name not in {"ideal", "local", "tournament", "bimode", "tage"}:
        fail("Select a supported branch predictor.")
    config["branchPredictor"] = predictor_name
    config["branchPredictorParameters"] = validate_predictor_parameters(
        value.get("branchPredictorParameters"))
    speculation = value.get("speculativeExecution", config["speculativeExecution"])
    if not isinstance(speculation, bool):
        fail("The speculative-execution option must be enabled or disabled.")
    config["speculativeExecution"] = speculation
    out_of_order = value.get("outOfOrderExecution", config["outOfOrderExecution"])
    if not isinstance(out_of_order, bool):
        fail("The out-of-order execution option must be enabled or disabled.")
    config["outOfOrderExecution"] = out_of_order
    # Accept the former key while migrating existing per-project settings.
    start_label = value.get(
        "o3EvaluationLabel",
        value.get("o3LectureStartLabel", config["o3EvaluationLabel"]),
    )
    if (not isinstance(start_label, str)
            or (start_label and not re.fullmatch(r"[A-Za-z_.$][A-Za-z0-9_.$]*", start_label))):
        fail("The evaluation start label is not a valid assembly label.")
    config["o3EvaluationLabel"] = start_label
    if config["cacheLine"] & (config["cacheLine"] - 1):
        fail("Cache-line size must be a power of two.")
    if not ENABLE_MEMORY_CONFIGURATION:
        for key in (
            "memoryMode", "cacheStalls", "instructionMemoryLatency",
            "dataReadLatency", "dataWriteLatency", "iCacheSize",
            "dCacheSize", "cacheLine", "cacheLatency", "memoryLatency",
        ):
            config[key] = DEFAULT_CONFIG[key]
    if config["cpu"] == "out-of-order":
        config["forwarding"] = True
    return config


def project_config(folder):
    path = folder / ".ase-studio.json"
    if not path.exists():
        return DEFAULT_CONFIG.copy()
    try:
        return validate_config(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        fail("The project CPU configuration is invalid.")


def riscv_isa_for_config(configuration):
    """Return the RISC-V ISA used by ASE Studio for one project config."""
    floating_extensions = ("fd" if configuration["floatingPointPrecision"] == "double"
                           else "f")
    compressed_extension = "c" if configuration["compressedInstructions"] else ""
    march = f"rv32ima{floating_extensions}{compressed_extension}_zicsr_zifencei"
    extensions = ["i", "m", "a", *floating_extensions]
    if compressed_extension:
        extensions.append("c")
    extensions.extend(("zicsr", "zifencei"))
    abi = "ilp32d" if "d" in floating_extensions else "ilp32f"
    return {"march": march, "abi": abi, "extensions": extensions}


def save_project_config(folder, value):
    config = validate_config(value)
    (folder / ".ase-studio.json").write_text(json.dumps(config, indent=2) + "\n")
    return config


def clean_source(source: str) -> str:
    """Remove markers created by the first prototype; they are no longer needed."""
    source = re.sub(r"(?m)^\s*# ASE-(?:BEGIN|END)-STUDENT\s*\n?", "", source)
    trailing_newline = source.endswith("\n")
    lines = source.splitlines()

    def add_before(pattern, comment):
        if comment in lines:
            return
        for index, line in enumerate(lines):
            if re.match(pattern, line.split("#", 1)[0].strip(), re.I):
                lines.insert(index, comment)
                return

    add_before(r"^(?:\.section\s+\.text(?:\b|,)|\.text(?:\b|,))",
               "# The text section contains the instructions that the CPU runs.")
    entry_comment = "# Make _start visible as the point where the program begins."
    add_before(r"^\.glob(?:l|al)\s+_start\b", entry_comment)
    if entry_comment not in lines:
        add_before(r"^_start:\s*$", entry_comment)
    add_before(r"^End:\s*$",
               "# The End block stops the program and returns control to the simulator.")
    return "\n".join(lines) + ("\n" if trailing_newline else "")


def protected_lines(source: str):
    """Return the zero-based lines containing the mandatory entry/exit statements."""
    lines = source.splitlines()
    protected = set()
    for index, line in enumerate(lines):
        code = line.split("#", 1)[0].strip()
        if (re.match(r"^(?:\.section\s+\.text(?:\b|,)|\.text(?:\b|,))", code)
                or re.match(r"^\.glob(?:l|al)\s+_start\b", code)
                or re.match(r"^_start:\s*$", code)
                or line.strip() in SCAFFOLD_COMMENTS):
            protected.add(index)

    # Protect only the final exit sequence, not ecalls students may add to the body.
    significant = [(index, line.split("#", 1)[0].strip())
                   for index, line in enumerate(lines)
                   if line.split("#", 1)[0].strip()]
    for position in range(len(significant) - 1, -1, -1):
        if significant[position][1] != "ecall":
            continue
        tail = significant[max(0, position - 3):position + 1]
        codes = [code for _, code in tail]
        if (len(codes) >= 3
                and re.match(r"^li\s+(?:a7|x17)\s*,\s*93$", codes[-2])
                and re.match(r"^li\s+(?:a0|x10)\s*,\s*0$", codes[-3])):
            protected.update(index for index, _ in tail[-3:])
            if len(codes) == 4 and re.match(r"^End:\s*$", codes[0], re.I):
                protected.add(tail[0][0])
            break
    return sorted(protected)


def simulation_scaffold_issues(source: str):
    """Describe missing or ambiguous labels required to run a project."""
    labels = {"_start": [], "End": []}
    statements = []
    start_is_global = False
    label_pattern = re.compile(r"^([A-Za-z_.$][\w.$]*)\s*:\s*")

    for line_number, raw_line in enumerate(source.splitlines(), 1):
        code = raw_line.split("#", 1)[0].strip()
        if re.match(r"^\.(?:globl|global)\s+_start\b", code, re.I):
            start_is_global = True
        while match := label_pattern.match(code):
            label = match.group(1)
            if label == "_start":
                labels["_start"].append((line_number, len(statements)))
            elif label.lower() == "end":
                labels["End"].append((line_number, len(statements)))
            code = code[match.end():].lstrip()
        if code:
            statements.append(code)

    issues = []
    for name in ("_start", "End"):
        found = labels[name]
        if not found:
            issues.append(f"Missing required {name}: label.")
        elif len(found) > 1:
            lines = ", ".join(str(line) for line, _ in found)
            issues.append(f"Found {name}: on lines {lines}; keep only one.")

    if len(labels["_start"]) == 1 and not start_is_global:
        issues.append("The _start label must be exported with .globl _start.")

    if len(labels["End"]) == 1:
        line_number, statement_index = labels["End"][0]
        exit_block = statements[statement_index:statement_index + 3]
        if (len(exit_block) < 3
                or not re.fullmatch(r"li\s+(?:a0|x10)\s*,\s*0", exit_block[0], re.I)
                or not re.fullmatch(r"li\s+(?:a7|x17)\s*,\s*93", exit_block[1], re.I)
                or not re.fullmatch(r"ecall", exit_block[2], re.I)):
            issues.append(
                f"End: on line {line_number} must be followed by li a0, 0; "
                "li a7, 93; and ecall."
            )
    return issues


def mandatory_signature(source: str):
    lines = clean_source(source).splitlines()
    return [re.sub(r"\s+", "", lines[index].split("#", 1)[0]).lower()
            for index in protected_lines("\n".join(lines))]


def save_source(folder: Path, submitted, allow_scaffold_edit=False):
    if not isinstance(submitted, str):
        fail("Invalid assembly source.")
    if not isinstance(allow_scaffold_edit, bool):
        fail("Invalid scaffold protection option.")
    src = source_file(folder)
    existing = clean_source(src.read_text())
    submitted = clean_source(submitted)
    if (not allow_scaffold_edit
            and mandatory_signature(existing) != mandatory_signature(submitted)):
        fail("The mandatory entry or exit instructions cannot be changed.")
    src.write_text(submitted)
    return src


def open_with_editor(name):
    """Open Linux's native application chooser for a project source file."""
    folder = project_dir(name)
    source = source_file(folder)
    helper = STUDIO_ROOT / "open_with.py"
    if not helper.is_file():
        fail("The system application chooser is not installed.", 500)
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        fail("A Linux desktop session is required to choose an application.")
    try:
        subprocess.Popen(
            [sys.executable, str(helper), str(source)],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        fail(f"The system application chooser could not be opened: {error}", 500)
    return {"ok": True, "output": f"Opened the Linux application chooser for {source.name}."}


def setup_environment():
    """Load the repository's setup_default without Studio overrides."""
    setup = ROOT / "setup_default"
    command = ["bash", "-c", 'source "$1" >/dev/null; env -0', "ase-studio", str(setup)]
    result = subprocess.run(command, cwd=ROOT, capture_output=True)
    env = os.environ.copy()
    for item in result.stdout.split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            env[key.decode(errors="ignore")] = value.decode(errors="replace")
    if not env.get("RISCV_TOOLCHAIN_PATH") and env.get("CC"):
        env["RISCV_TOOLCHAIN_PATH"] = str(Path(env["CC"]).expanduser().parent)
    env["program"] = ""  # set per project below
    return env


def environment_overrides():
    """Read user-specific Studio settings kept outside the submodule."""
    if not ENVIRONMENT_CONFIG.exists():
        return {}
    try:
        values = json.loads(ENVIRONMENT_CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(values, dict):
        return {}
    overrides = {key: value for key, value in values.items()
                 if key in ENVIRONMENT_FIELDS and isinstance(value, str)}
    # Migrate the separate compiler field written by ASE Studio 1.0.
    if ("RISCV_TOOLCHAIN_PATH" not in overrides
            and isinstance(values.get("CC"), str) and "/" in values["CC"]):
        overrides["RISCV_TOOLCHAIN_PATH"] = str(Path(values["CC"]).expanduser().parent)
    return overrides


def settings_env():
    env = setup_environment()
    env.update(environment_overrides())
    compiler, objdump = toolchain_executables(env["RISCV_TOOLCHAIN_PATH"])
    env["RISCV_TOOLCHAIN_PATH"] = str(compiler.parent)
    env["CC"] = str(compiler)
    env["OBJDUMP"] = str(objdump)
    env["CC_INSTALLATION_PATH"] = str(compiler.parent) + os.sep
    env["GEM5_INSTALLATION_PATH"] = str(
        gem5_executable_path(env["GEM5_INSTALLATION_PATH"]))
    env["ASE_STUDIO_DEMO_MK"] = str((PROGRAMS / "demo.mk").resolve())
    # ASE Studio supports the RISC-V optimized gem5 build. Keeping these
    # fixed avoids asking users for path components that are not choices in
    # this frontend.
    env["GEM5_ISA"] = GEM5_ISA
    env["GEM5_VARIANT"] = GEM5_VARIANT
    env["GEM5_SIMULATION_SCRIPT"] = str(
        resolve_environment_path(env["GEM5_SIMULATION_SCRIPT"]))
    return env


def environment_settings():
    base = setup_environment()
    overrides = environment_overrides()
    values = {}
    for key in ENVIRONMENT_FIELDS:
        item = overrides.get(key, base.get(key, ""))
        if not item and key == "PROGRAMS_DIRECTORY":
            item = str(PROGRAMS)
        elif not item and key == "SUBMISSION_DIRECTORY":
            item = str(SUBMISSIONS)
        if key == "GEM5_INSTALLATION_PATH" and item:
            item = str(gem5_executable_path(item))
        values[key] = (portable_environment_value(item)
                       if key in ENVIRONMENT_PATH_FIELDS else item)
    return {
        "values": values,
        "overridden": sorted(overrides),
        "configPath": str(ENVIRONMENT_CONFIG),
    }


def import_environment_settings(filename, content):
    """Read supported Settings values from a user-selected shell setup file."""
    if not isinstance(filename, str) or not filename.strip():
        fail("Select a setup file to import.")
    if not isinstance(content, str) or not content.strip():
        fail("The selected setup file is empty.")
    if "\0" in content:
        fail("The selected setup file contains invalid data.")
    if len(content.encode("utf-8")) > 512 * 1024:
        fail("The selected setup file is too large.")

    display_name = Path(filename).name
    local_file = ROOT / display_name
    setup_path = None
    temporary_path = None
    try:
        if (local_file.is_file()
                and local_file.read_text(encoding="utf-8") == content):
            setup_path = local_file
        else:
            handle = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix="ase-studio-import-",
                suffix=".setup", delete=False,
            )
            with handle:
                handle.write(content)
            setup_path = Path(handle.name)
            temporary_path = setup_path

        imported_process_env = os.environ.copy()
        for key in (*ENVIRONMENT_FIELDS, "CC", "OBJDUMP", "CC_INSTALLATION_PATH",
                    "WORK_DIR", "GEM5_SRC"):
            imported_process_env.pop(key, None)
        command = [
            "bash", "--noprofile", "--norc", "-c",
            'source "$1" >/dev/null; status=$?; '
            'if (( status != 0 )); then exit "$status"; fi; env -0',
            "ase-studio-import", str(setup_path),
        ]
        result = subprocess.run(
            command, cwd=ROOT, env=imported_process_env,
            capture_output=True, timeout=15,
        )
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip().splitlines()
            fail("The setup file could not be imported"
                 + (f": {detail[-1]}" if detail else "."))

        imported = {}
        for item in result.stdout.split(b"\0"):
            if b"=" not in item:
                continue
            key, value = item.split(b"=", 1)
            imported[key.decode(errors="ignore")] = value.decode(errors="replace")

        values = environment_settings()["values"]
        for key in ENVIRONMENT_FIELDS:
            if key in imported:
                values[key] = imported[key]
        if "RISCV_TOOLCHAIN_PATH" not in imported:
            compiler = imported.get("CC", "")
            if compiler and "/" in compiler:
                values["RISCV_TOOLCHAIN_PATH"] = str(Path(compiler).expanduser().parent)

        # An uploaded setup_default is evaluated from a temporary directory.
        # Translate paths it derived from BASH_SOURCE back to this repository.
        if temporary_path is not None and "BASH_SOURCE[0]" in content:
            temporary_root = str(temporary_path.parent)
            for key in ENVIRONMENT_PATH_FIELDS:
                value = values.get(key, "")
                if value == temporary_root or value.startswith(temporary_root + os.sep):
                    values[key] = str(ROOT) + value[len(temporary_root):]
        for key in ENVIRONMENT_PATH_FIELDS:
            if key == "GEM5_INSTALLATION_PATH" and values.get(key):
                values[key] = str(gem5_executable_path(values[key]))
            values[key] = portable_environment_value(values.get(key, ""))
        return {"values": values, "source": display_name}
    except subprocess.TimeoutExpired:
        fail("The setup file took too long to import.")
    except UnicodeDecodeError:
        fail("The selected setup file is not valid UTF-8 text.")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def expand_portable_environment_variables(value):
    """Expand only documented path variables, not arbitrary process values."""
    def replacement(match):
        name = match.group(1) or match.group(2)
        return PORTABLE_ENVIRONMENT_VARIABLES.get(name, match.group(0))

    expanded = re.sub(
        r"\$\{(HOME|USER|ASE_STUDIO_ROOT)\}|\$(HOME|USER|ASE_STUDIO_ROOT)\b",
        replacement, value,
    )
    if "$" in expanded:
        fail("Only $HOME, $USER, and $ASE_STUDIO_ROOT may be used in paths.")
    return expanded


def portable_environment_value(value):
    """Represent paths below the user's home without a hard-coded username."""
    if not isinstance(value, str) or not value:
        return value
    if "$" in value or not Path(value).expanduser().is_absolute():
        return value
    root = str(ROOT)
    if value == root:
        return "$ASE_STUDIO_ROOT"
    if value.startswith(root + os.sep):
        return "$ASE_STUDIO_ROOT" + value[len(root):]
    home = str(Path.home())
    if value == home:
        return "$HOME"
    if value.startswith(home + os.sep):
        return "$HOME" + value[len(home):]
    return value


def resolve_environment_path(value):
    path = Path(expand_portable_environment_variables(value)).expanduser()
    return (ROOT / path).resolve() if not path.is_absolute() else path.resolve()


def gem5_executable_path(value):
    """Resolve the configured gem5 executable, migrating legacy build paths."""
    requested = resolve_environment_path(value)
    legacy_executable = requested / GEM5_ISA / f"gem5.{GEM5_VARIANT}"
    if requested.is_dir() and legacy_executable.is_file():
        return legacy_executable.resolve()
    return requested


def toolchain_executables(value):
    """Find a matching GCC/objdump pair in a RISC-V toolchain directory."""
    requested = resolve_environment_path(value)
    directories = [requested]
    if (requested / "bin").is_dir():
        directories.insert(0, requested / "bin")
    known_prefixes = (
        "riscv-none-elf", "riscv64-unknown-elf", "riscv32-unknown-elf",
        "riscv64-linux-gnu", "riscv32-linux-gnu",
    )
    for directory in directories:
        for prefix in known_prefixes:
            compiler = directory / f"{prefix}-gcc"
            objdump = directory / f"{prefix}-objdump"
            if (compiler.is_file() and objdump.is_file()
                    and os.access(compiler, os.X_OK) and os.access(objdump, os.X_OK)):
                return compiler.resolve(), objdump.resolve()
        if directory.is_dir():
            for compiler in sorted(directory.glob("riscv*-gcc")):
                objdump = compiler.with_name(compiler.name[:-3] + "objdump")
                if (os.access(compiler, os.X_OK) and objdump.is_file()
                        and os.access(objdump, os.X_OK)):
                    return compiler.resolve(), objdump.resolve()
    fail(f"No matching RISC-V GCC and objdump executables were found in {requested}.")


def command_version(command, label):
    try:
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True,
                                timeout=8)
    except (OSError, subprocess.TimeoutExpired) as error:
        fail(f"Could not run {label}: {error}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        fail(f"{label} did not run successfully"
             + (f": {detail[0]}" if detail else "."))
    lines = (result.stdout or result.stderr).strip().splitlines()
    return lines[0] if lines else f"{label} is available."


def gem5_version(executable):
    """Exercise gem5 and read its banner (gem5 22 has no --version flag)."""
    try:
        result = subprocess.run([str(executable), "-v"], cwd=ROOT, text=True,
                                capture_output=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired) as error:
        fail(f"Could not run gem5: {error}")
    output = result.stdout + result.stderr
    match = re.search(r"gem5 version[^\r\n]*", output, re.IGNORECASE)
    if not match:
        detail = output.strip().splitlines()
        fail("gem5 did not produce a version banner"
             + (f": {detail[0]}" if detail else "."))
    return match.group(0)


def clean_environment_values(value, required_fields=None):
    if not isinstance(value, dict):
        fail("Invalid environment settings.")
    required = set(ENVIRONMENT_FIELDS if required_fields is None else required_fields)
    cleaned = {}
    for key in ENVIRONMENT_FIELDS:
        item = value.get(key, "")
        if not isinstance(item, str):
            if key in required:
                fail(f"Missing environment value: {key}.")
            item = ""
        item = item.strip()
        if key in required and key not in OPTIONAL_ENVIRONMENT_FIELDS and not item:
            fail(f"{key} cannot be empty.")
        if "\0" in item or "\n" in item or "\r" in item:
            fail(f"{key} contains an invalid character.")
        cleaned[key] = item
    return cleaned


def validate_environment_field(key, values):
    """Validate one field, including related values needed to exercise it."""
    if key not in ENVIRONMENT_FIELDS:
        fail("Unknown environment field.")
    if key in {"PROGRAMS_DIRECTORY", "SUBMISSION_DIRECTORY"}:
        required = {key}
    elif key in {"RISCV_TOOLCHAIN_PATH", "OPTIMIZATION_FLAGS"}:
        required = {"RISCV_TOOLCHAIN_PATH", "OPTIMIZATION_FLAGS"}
    elif key == "PIPELINE_DISPLAY_CYCLE_LIMIT":
        required = {key}
    elif key in {"SUBMISSION_NAME_PREFIX", "SUBMISSION_NAME_SUFFIX"}:
        required = {key}
    else:
        required = {"GEM5_INSTALLATION_PATH"}
    cleaned = clean_environment_values(values, required)

    if key == "PROGRAMS_DIRECTORY":
        programs = resolve_environment_path(cleaned[key])
        if not programs.is_dir():
            fail(f"The programs folder does not exist or is not a directory: {programs}")
        if not os.access(programs, os.R_OK | os.W_OK | os.X_OK):
            fail(f"The programs folder must be readable and writable: {programs}")
        return f"Projects will be loaded from {programs}."

    if key == "SUBMISSION_DIRECTORY":
        submissions = resolve_environment_path(cleaned[key])
        if submissions.exists():
            if not submissions.is_dir():
                fail(f"The submission path is not a directory: {submissions}")
            writable_parent = submissions
        else:
            writable_parent = submissions.parent
            while not writable_parent.exists() and writable_parent != writable_parent.parent:
                writable_parent = writable_parent.parent
            if not writable_parent.is_dir():
                fail(f"No usable parent directory exists for: {submissions}")
        if not os.access(writable_parent, os.W_OK | os.X_OK):
            fail(f"The submission folder cannot be created or written: {submissions}")
        return f"Submission ZIPs will be saved under {submissions}."

    if key in {"RISCV_TOOLCHAIN_PATH", "OPTIMIZATION_FLAGS"}:
        compiler, objdump = toolchain_executables(cleaned["RISCV_TOOLCHAIN_PATH"])
        try:
            flags = shlex.split(cleaned["OPTIMIZATION_FLAGS"])
        except ValueError as error:
            fail(f"Compiler optimization flags are invalid: {error}")
        compiler_version = command_version(
            [str(compiler), *flags, "--version"], "RISC-V compiler")
        objdump_version = command_version(
            [str(objdump), "--version"], "RISC-V objdump")
        return f"{compiler_version}; {objdump_version}"

    if key == "PIPELINE_DISPLAY_CYCLE_LIMIT":
        try:
            limit = int(cleaned[key])
        except ValueError:
            fail("Maximum pipeline display cycles must be a whole number.")
        if not 100 <= limit <= 20000:
            fail("Maximum pipeline display cycles must be from 100 to 20,000.")
        return f"Pipeline tables up to {limit:,} cycles will be displayed."

    if key in {"SUBMISSION_NAME_PREFIX", "SUBMISSION_NAME_SUFFIX"}:
        part = cleaned[key]
        if not re.fullmatch(r"[A-Za-z0-9._-]*", part):
            fail("Submission name parts may contain letters, digits, '.', '_' and '-'.")
        return (f"Submission name part: {part}" if part
                else "No text will be added in this position.")

    gem5 = gem5_executable_path(cleaned["GEM5_INSTALLATION_PATH"])
    if not gem5.is_file() or not os.access(gem5, os.X_OK):
        fail(f"The gem5 executable does not exist or is not executable: {gem5}")
    return gem5_version(gem5)


def check_environment_field(key, values):
    try:
        message = validate_environment_field(key, values)
        if key == "GEM5_INSTALLATION_PATH":
            checkout = configured_gem5_checkout(values)
            if not checkout["managed"]:
                return {"key": key, "ok": True, "warning": True,
                        "message": f"{message}. {checkout['message']}"}
        return {"key": key, "ok": True, "warning": False,
                "message": message}
    except ValueError as error:
        message, _status = error.args[0]
        return {"key": key, "ok": False, "message": message}


def save_environment_settings(value):
    """Validate and persist the global toolchain/gem5 settings."""
    cleaned = clean_environment_values(value)
    # Saving is intentionally strict: every field must be usable.
    for key in ENVIRONMENT_FIELDS:
        validate_environment_field(key, cleaned)

    # ASE Studio 1.4 and earlier stored the gem5 build directory. Saving the
    # dialog once upgrades that value to the executable/wrapper path.
    cleaned["GEM5_INSTALLATION_PATH"] = str(
        gem5_executable_path(cleaned["GEM5_INSTALLATION_PATH"]))

    stored = {
        key: portable_environment_value(item) if key in ENVIRONMENT_PATH_FIELDS else item
        for key, item in cleaned.items()
    }
    ENVIRONMENT_CONFIG.write_text(json.dumps(stored, indent=2) + "\n")
    return environment_settings()


def reset_environment_settings():
    if ENVIRONMENT_CONFIG.exists():
        ENVIRONMENT_CONFIG.unlink()
    return environment_settings()


def launch_component_installer(component):
    """Open an interactive terminal for a supported long-running installer."""
    if component not in {"toolchain", "gem5"}:
        fail("Unsupported installation component.")
    installer = ROOT / "utils" / "installation.sh"
    if not installer.is_file():
        fail(f"Installation utility not found: {installer}", 404)
    base_command = ["bash", str(installer), component]
    terminal_commands = []
    if shutil.which("xfce4-terminal"):
        terminal_commands.append([
            "xfce4-terminal", "--title", f"Install {component}", "--hold",
            "--command", shlex.join(base_command),
        ])
    if shutil.which("gnome-terminal"):
        terminal_commands.append(["gnome-terminal", "--", *base_command])
    if shutil.which("konsole"):
        terminal_commands.append(["konsole", "--hold", "-e", *base_command])
    if shutil.which("x-terminal-emulator"):
        terminal_commands.append(["x-terminal-emulator", "-e", *base_command])
    if not terminal_commands:
        fail(f"No supported terminal was found. Run: {shlex.join(base_command)}")
    try:
        subprocess.Popen(terminal_commands[0], cwd=ROOT, start_new_session=True)
    except OSError as error:
        fail(f"Could not open the installer terminal: {error}", 500)
    return {"ok": True,
            "output": f"Opened an interactive terminal to install {component}."}


def _terminate_process_groups(processes, grace_seconds=2.0):
    """Stop complete command trees without targeting unrelated processes."""
    running = [process for process in processes if process.poll() is None]
    for process in running:
        try:
            # run_command starts a new session, so its PID is also the process
            # group ID shared by gem5/make and all of their children.
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as error:
            print(f"Could not terminate command group {process.pid}: {error}",
                  file=sys.stderr, flush=True)

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if all(process.poll() is not None for process in running):
            return
        time.sleep(0.05)

    for process in running:
        if process.poll() is not None:
            continue
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            print(f"Could not kill command group {process.pid}: {error}",
                  file=sys.stderr, flush=True)
    for process in running:
        try:
            process.wait(timeout=0.5)
        except (subprocess.TimeoutExpired, OSError):
            pass


def terminate_active_commands():
    """Prevent new commands and terminate every active build/simulation tree."""
    _COMMAND_SHUTDOWN.set()
    with _ACTIVE_COMMANDS_LOCK:
        processes = tuple(_ACTIVE_COMMANDS)
    _terminate_process_groups(processes)


atexit.register(terminate_active_commands)


def run_command(command, cwd, env):
    # Keep diagnostics in their real order. Appending separately captured
    # stderr after stdout made gem5's startup message appear after the final
    # trace event, which looked like an accidental second simulation.
    with _ACTIVE_COMMANDS_LOCK:
        if _COMMAND_SHUTDOWN.is_set():
            return 130, "$ " + " ".join(command) + "\nASE Studio is shutting down.\n"
        process = subprocess.Popen(
            command, cwd=cwd, env=env, text=True, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        _ACTIVE_COMMANDS.add(process)
    try:
        stdout, _stderr = process.communicate()
    except BaseException:
        _terminate_process_groups((process,))
        raise
    finally:
        with _ACTIVE_COMMANDS_LOCK:
            _ACTIVE_COMMANDS.discard(process)
    output = "$ " + " ".join(command) + "\n" + (stdout or "")
    return process.returncode, output


def simulation_output_for_display(output, trace):
    """Hide the verbose MinorGUI event block while retaining its trace file."""
    lines = output.splitlines()
    start = next((index for index, line in enumerate(lines)
                  if line.strip() == "**** REAL SIMULATION ****"), None)
    if start is None:
        return output
    end = next((index for index in range(len(lines) - 1, start, -1)
                if "Log4GUI:" in lines[index]), None)
    if end is None:
        return output
    try:
        shown_path = trace.relative_to(ROOT)
    except ValueError:
        shown_path = trace
    notice = (
        f"[Detailed pipeline events omitted from this view. Full gem5 log: "
        f"{shown_path}]"
    )
    return "\n".join([*lines[:start], notice, *lines[end + 1:]]).strip() + "\n"


def build(name):
    folder = project_dir(name)
    issues = simulation_scaffold_issues(source_file(folder).read_text())
    if issues:
        output = "Simulation scaffold is invalid:\n" + "\n".join(
            f"- {issue}" for issue in issues) + "\n"
        return {"ok": False, "output": output, "advancedOutput": output,
                "source": source_file(folder).name}
    env = settings_env()
    cpu_config = project_config(folder)
    env["ASE_RISCV_MARCH"] = riscv_isa_for_config(cpu_config)["march"]
    env["program"] = artifact_stem(folder)
    clean_rc, clean_output = run_command(["make", "clean"], folder, env)
    rc, output = run_command(["make"], folder, env)
    advanced = clean_output + output
    normal_lines = []
    for line in advanced.splitlines():
        stripped = line.strip()
        if (stripped in {"$ make", "$ make clean"} or stripped.startswith("CLEAN ")
                or re.match(r"^rm\s+-f\s+", stripped)
                or stripped.startswith("make: ***")):
            continue
        normal_lines.append(line)
    normal = "\n".join(normal_lines).strip()
    normal += ("\n\nBuild succeeded.\n" if rc == 0 else "\n\nBuild failed.\n")
    return {"ok": rc == 0, "output": normal, "advancedOutput": advanced,
            "source": source_file(folder).name}


def configured_path(value: str) -> str:
    path = Path(value).expanduser()
    return str((ROOT / path).resolve()) if not path.is_absolute() else str(path)


def simulate(name):
    folder = project_dir(name)
    cpu_config = project_config(folder)
    elf = folder / f"{artifact_stem(folder)}.elf"
    if not elf.exists():
        return {"ok": False, "output": "Build failed: expected ELF was not created.\n"}
    env = settings_env()
    result_dir = project_results_directory(name)
    result_dir.mkdir(parents=True, exist_ok=True)
    for old_trace in (result_dir / "gem5_inorder.log", result_dir / "trace.out"):
        if old_trace.exists():
            old_trace.unlink()
    in_order = cpu_config["cpu"] == "in-order"
    gem5 = Path(configured_path(env["GEM5_INSTALLATION_PATH"]))
    config = (configured_path(env["GEM5_SIMULATION_SCRIPT"]) if in_order
              else str(ROOT / "gem5" / "riscv_o3_custom.py"))
    latency_options = [
        "--ase-int-alu-latency", str(cpu_config["intAlu"]),
        "--ase-int-mul-latency", str(cpu_config["intMul"]),
        "--ase-int-div-latency", str(cpu_config["intDiv"]),
        "--ase-float-alu-latency", str(cpu_config["floatAlu"]),
        "--ase-float-mul-latency", str(cpu_config["floatMul"]),
        "--ase-float-div-latency", str(cpu_config["floatDiv"]),
        "--ase-int-alu-pipelined", "on" if cpu_config["intAluPipelined"] else "off",
        "--ase-int-mul-pipelined", "on" if cpu_config["intMulPipelined"] else "off",
        "--ase-int-div-pipelined", "on" if cpu_config["intDivPipelined"] else "off",
        "--ase-float-alu-pipelined", "on" if cpu_config["floatAluPipelined"] else "off",
        "--ase-float-mul-pipelined", "on" if cpu_config["floatMulPipelined"] else "off",
        "--ase-float-div-pipelined", "on" if cpu_config["floatDivPipelined"] else "off",
        "--ase-memory-mode", cpu_config["memoryMode"],
        "--ase-cache-stalls", "on" if cpu_config["memoryMode"] == "cache" else "off",
        "--ase-instruction-memory-latency", str(cpu_config["instructionMemoryLatency"]),
        "--ase-data-read-latency", str(cpu_config["dataReadLatency"]),
        "--ase-data-write-latency", str(cpu_config["dataWriteLatency"]),
        "--ase-cache-latency", str(cpu_config["cacheLatency"]),
        "--ase-memory-latency", str(cpu_config["memoryLatency"]),
    ]
    o3_options = [
        "--ase-fetch-width", str(cpu_config["fetchWidth"]),
        "--ase-decode-width", str(cpu_config["decodeWidth"]),
        "--ase-rename-width", str(cpu_config["renameWidth"]),
        "--ase-dispatch-width", str(cpu_config["dispatchWidth"]),
        "--ase-issue-width", str(cpu_config["issueWidth"]),
        "--ase-writeback-width", str(cpu_config["writebackWidth"]),
        "--ase-commit-width", str(cpu_config["commitWidth"]),
        "--ase-int-alu-units", str(cpu_config["intAluUnits"]),
        "--ase-int-mul-units", str(cpu_config["intMulUnits"]),
        "--ase-int-div-units", str(cpu_config["intDivUnits"]),
        "--ase-float-alu-units", str(cpu_config["floatAluUnits"]),
        "--ase-float-mul-units", str(cpu_config["floatMulUnits"]),
        "--ase-float-div-units", str(cpu_config["floatDivUnits"]),
        "--ase-memory-units", str(cpu_config["memoryUnits"]),
        "--ase-rob-entries", str(cpu_config["robEntries"]),
        "--ase-iq-entries", str(cpu_config["iqEntries"]),
        "--ase-lq-entries", str(cpu_config["lqEntries"]),
        "--ase-sq-entries", str(cpu_config["sqEntries"]),
        # "ideal" is a teaching projection. A real local predictor produces
        # the architectural trace, while the table deliberately applies no
        # prediction misses or recovery penalty.
        "--ase-branch-predictor", (
            "local" if cpu_config["branchPredictor"] == "ideal"
            else cpu_config["branchPredictor"]),
        "--ase-branch-predictor-parameters", json.dumps(
            cpu_config.get("branchPredictorParameters", DEFAULT_PREDICTOR_PARAMETERS)[
                "local" if cpu_config["branchPredictor"] == "ideal"
                else cpu_config["branchPredictor"]], separators=(",", ":")),
    ]
    if in_order:
        command = [str(gem5), "--debug-flags=MinorGUI,Exec", f"--outdir={result_dir}", "--verbose", config,
                   "--caches", "--cpu-type", "MinorCPU", "--l1d_size", cpu_config["dCacheSize"],
                   "--l1i_size", cpu_config["iCacheSize"],
                   "--cacheline_size", str(cpu_config["cacheLine"]),
                   "--cpu-clock", "1GHz", "--sys-clock", "1GHz", "-c", str(elf),
                   *latency_options, "--ase-forwarding", "on" if cpu_config["forwarding"] else "off"]
        trace = result_dir / "gem5_inorder.log"
    else:
        trace = result_dir / "trace.out"
        command = [str(gem5), "--debug-flags=O3PipeView,O3CPUAll,Exec", f"--debug-file={trace}", f"--outdir={result_dir}",
                   "--verbose", config, "--caches", "--l1i_size", cpu_config["iCacheSize"],
                   "--l1d_size", cpu_config["dCacheSize"], "--cacheline_size", str(cpu_config["cacheLine"]),
                   "-c", str(elf), f"--directory={result_dir}",
                   f"--errout={result_dir / 'programm.err'}", *latency_options, *o3_options]
    rc, output = run_command(command, ROOT, env)
    if in_order:
        trace.write_text(output)
        displayed_output = simulation_output_for_display(output, trace)
    else:
        displayed_output = output
    message = "Simulation completed.\n" if rc == 0 else "Simulation failed.\n"
    return {"ok": rc == 0, "output": message,
            "advancedOutput": displayed_output + "\n" + message,
            "trace": trace.name}


STAGES = {"fetch1": "F", "decode": "D", "execute": "E", "memory": "M", "writeback": "W"}
EVENT = re.compile(r"^\s*(\d+): .*?Log4GUI: (fetch1|decode|execute|memory|writeback): \d+: (\d+): ([0-9a-fA-F]+): (.*)$")
REGISTER_EVENT = re.compile(r"^\s*(\d+): global: ([xf]\d+)=([0-9a-fA-Fx]+)$")
EXEC_EVENT = re.compile(
    r"^\s*(\d+): .*?: T\d+ : 0x([0-9a-fA-F]+).*?\s+:\s+(.+?)\s+:\s+(\w+)\s+:\s+(.*)$"
)

REGISTER_ALIASES = {
    "zero": "x0", "ra": "x1", "sp": "x2", "gp": "x3", "tp": "x4",
    **{f"t{i}": f"x{5 + i}" for i in range(3)},
    **{f"s{i}": f"x{8 + i}" for i in range(2)},
    **{f"a{i}": f"x{10 + i}" for i in range(8)},
    **{f"s{i}": f"x{16 + i}" for i in range(2, 12)},
    "t3": "x28", "t4": "x29", "t5": "x30", "t6": "x31",
    **{f"ft{i}": f"f{i}" for i in range(8)},
    "fs0": "f8", "fs1": "f9",
    **{f"fa{i}": f"f{10 + i}" for i in range(8)},
    **{f"fs{i}": f"f{16 + i}" for i in range(2, 12)},
    "ft8": "f28", "ft9": "f29", "ft10": "f30", "ft11": "f31",
}


def normalize_instruction(text):
    text = text.split("#", 1)[0].strip().lower()
    if not text or text.endswith(":") or text.startswith("."):
        return ""
    text = re.sub(r"\b(" + "|".join(REGISTER_ALIASES) + r")\b",
                  lambda match: REGISTER_ALIASES[match.group(1)], text)
    text = re.sub(r"\bbnez\s+(x\d+)\s*,", r"bne \1, x0,", text)
    text = re.sub(r"\bbeqz\s+(x\d+)\s*,", r"beq \1, x0,", text)
    text = re.sub(r"\b(f(?:add|sub|mul|div|mv))_s\b", r"\1.s", text)
    text = re.sub(r"\bfmv[._]([wx])[._]([wx])\b", r"fmv.\1.\2", text)
    text = re.sub(r"^c[._]", "", text)
    text = re.sub(r"^(?:fld|flw|ld|lw|fsd|fsw|sd|sw)sp\b",
                  lambda match: match.group(0)[:-2], text)
    text = re.sub(r"\s+", "", text)
    # gem5 prints the common `li` pseudo-instruction as its real addi form.
    match = re.fullmatch(r"li(x\d+),(.+)", text)
    if match:
        text = f"addi{match.group(1)},x0,{match.group(2)}"
    return text


def display_instruction(text):
    """Use standard assembly spelling for gem5's internal mnemonics."""
    text = text.strip()
    parts = text.split(None, 1)
    if not parts:
        return text
    mnemonic = parts[0]
    atomic = re.fullmatch(r"(amo[a-z]+|lr|sc)_w(?:\[[^]]+\])?", mnemonic,
                          flags=re.I)
    if mnemonic.lower().startswith("c_"):
        mnemonic = "c." + mnemonic[2:].replace("_", ".")
    elif atomic:
        mnemonic = atomic.group(1) + ".w"
    elif mnemonic.lower() == "fence_i":
        mnemonic = "fence.i"
    elif mnemonic.lower().startswith("f"):
        mnemonic = mnemonic.replace("_", ".")
    return mnemonic + ((" " + parts[1]) if len(parts) > 1 else "")


def cycle_for_nearest_tick(tick, ordered_ticks):
    position = bisect_right(ordered_ticks, tick)
    return max(1, min(len(ordered_ticks), position))


def destination_register(instruction):
    operands = display_instruction(instruction).split(None, 1)
    if len(operands) < 2:
        return None
    candidate = operands[1].split(",", 1)[0].strip().lower()
    candidate = REGISTER_ALIASES.get(candidate, candidate)
    return candidate if re.fullmatch(r"[xf]\d+", candidate) else None


MEMORY_ACCESS_BYTES = {
    "lb": 1, "lbu": 1, "sb": 1,
    "lh": 2, "lhu": 2, "sh": 2,
    "lw": 4, "sw": 4, "lwsp": 4, "swsp": 4,
    "flw": 4, "fsw": 4, "flwsp": 4, "fswsp": 4,
    "fld": 8, "fsd": 8, "fldsp": 8, "fsdsp": 8,
}


def memory_access_bytes(instruction):
    """Return how many bytes a load/store/atomic instruction transfers."""
    opcode = display_instruction(instruction).split(None, 1)[0].lower()
    if opcode.startswith("c."):
        opcode = opcode[2:]
    return MEMORY_ACCESS_BYTES.get(opcode, 4)


def parse_exec_playback(lines, ordered_ticks):
    register_deltas, memory_deltas = defaultdict(dict), defaultdict(dict)
    for line in lines:
        match = EXEC_EVENT.match(line)
        if not match:
            continue
        tick_text, pc, instruction, op_class, result = match.groups()
        cycle = str(cycle_for_nearest_tick(int(tick_text), ordered_ticks))
        data_match = re.search(r"\bD=(0x[0-9a-fA-F]+)", result)
        address_match = re.search(r"\bA=(0x[0-9a-fA-F]+)", result)
        data_digits = data_match.group(1)[2:].lower() if data_match else ""
        if data_match and "MemWrite" not in op_class:
            destination = destination_register(instruction)
            # Exec traces report a computed D= value for nop/addi x0 too, but
            # RISC-V x0 is hardwired to zero and never receives a write.
            if destination and destination != "x0":
                width = 16 if destination.startswith("f") else 8
                register_deltas[cycle][destination] = (
                    "0x" + data_digits[-width:].zfill(width))
        if address_match:
            # Keep only the bytes actually transferred: stores report just
            # the stored data, while loads report the sign-extended result.
            memory_bits = memory_access_bytes(instruction) * 8
            memory_digits = memory_bits // 4
            display_value = ("0x" + data_digits[-memory_digits:].zfill(memory_digits)
                             if data_match else "—")
            memory_deltas[cycle][address_match.group(1).lower()] = {
                "value": display_value,
                "bits": memory_bits,
                "access": "write" if "MemWrite" in op_class else "read",
                "pc": "0x" + pc.lower(),
            }
    return dict(register_deltas), dict(memory_deltas)


def parse_taken_jumps(lines, ordered_ticks):
    """Return control-flow transfers supported by the completed instruction trace."""
    executed = []
    for line in lines:
        match = EXEC_EVENT.match(line)
        if not match:
            continue
        tick, address, instruction, _op_class, _result = match.groups()
        raw_mnemonic = instruction.split(None, 1)[0].lower()
        instruction_bytes = 2 if raw_mnemonic.startswith(("c_", "c.")) else 4
        executed.append((int(tick), address.lower(), display_instruction(instruction),
                         instruction_bytes))
    jumps = []
    control_instruction = re.compile(
        r"^(?:c\.)?(?:b(?:eq|eqz|ne|nez|lt|ge|ltu|geu)|j|jr|jal|jalr|ret)\b",
        re.I,
    )
    conditional_branch = re.compile(
        r"^(?:c\.)?b(?:eq|eqz|ne|nez|lt|ge|ltu|geu)\b", re.I,
    )
    branch_offset = re.compile(
        r",\s*([+-]?(?:0x[0-9a-f]+|\d+))\s*$", re.I,
    )
    for ((tick, source, instruction, instruction_bytes),
         (_next_tick, target, _next_instruction, _next_bytes)) in zip(executed, executed[1:]):
        if not control_instruction.match(instruction.strip()):
            continue
        source_pc, target_pc = int(source, 16), int(target, 16)
        if target_pc == source_pc + instruction_bytes:
            continue
        if conditional_branch.match(instruction):
            offset = branch_offset.search(instruction)
            if offset:
                value = offset.group(1)
                displacement = (int(value, 16) if "0x" in value.lower()
                                else int(value, 10))
                # Exec records completion order. A long-running instruction
                # immediately after a not-taken branch can complete after a
                # younger instruction, making that younger PC look like a
                # branch target. A real taken branch must land at its encoded
                # PC-relative destination.
                if target_pc != source_pc + displacement:
                    continue
        jumps.append({
            "fromAddress": source,
            "toAddress": target,
            "cycle": cycle_for_nearest_tick(tick, ordered_ticks),
        })
    return jumps


def add_source_lines(rows, source_body):
    by_instruction = defaultdict(list)
    source_lines = list(enumerate(source_body.splitlines(), 1))
    for line_number, line in source_lines:
        normalized = normalize_instruction(line)
        if normalized:
            by_instruction[normalized].append(line_number)
    rows_by_instruction = defaultdict(list)
    for row in rows:
        rows_by_instruction[normalize_instruction(row["instruction"])].append(row)
    for normalized, matching_rows in rows_by_instruction.items():
        matching_source_lines = by_instruction.get(normalized, [])
        matching_rows.sort(key=lambda row: int(row.get("address") or "0", 16))
        for index, row in enumerate(matching_rows):
            row["sourceLine"] = (matching_source_lines[index]
                                 if index < len(matching_source_lines) else None)

    # Recover mappings for common pseudo-instructions and symbolic branches.
    # Their trace spelling differs from the source (`la` becomes AUIPC+ADDI,
    # a large `li` becomes LUI, and a label becomes a numeric branch offset).
    # Match destinations and static address order so repeated-looking source
    # instructions still select the correct editor line.
    ordered_rows = sorted(rows, key=lambda row: int(row.get("address") or "0", 16))
    normalized_rows = [normalize_instruction(row["instruction"]) for row in ordered_rows]
    for line_number, line in source_lines:
        normalized = normalize_instruction(line)
        la_match = re.match(r"^la(x\d+),", normalized)
        if la_match:
            destination = la_match.group(1)
            for index, row_text in enumerate(normalized_rows[:-1]):
                next_text = normalized_rows[index + 1]
                if (ordered_rows[index].get("sourceLine") is None
                        and ordered_rows[index + 1].get("sourceLine") is None
                        and row_text.startswith(f"auipc{destination},")
                        and next_text.startswith(f"addi{destination},{destination},")):
                    ordered_rows[index]["sourceLine"] = line_number
                    ordered_rows[index + 1]["sourceLine"] = line_number
                    break
            continue
        li_match = re.match(r"^addi(x\d+),x0,", normalized)
        if li_match:
            destination = li_match.group(1)
            for index, row_text in enumerate(normalized_rows):
                if (ordered_rows[index].get("sourceLine") is None
                        and row_text.startswith(f"lui{destination},")):
                    ordered_rows[index]["sourceLine"] = line_number
                    if (index + 1 < len(ordered_rows)
                            and ordered_rows[index + 1].get("sourceLine") is None
                            and normalized_rows[index + 1].startswith(
                                f"addi{destination},{destination},")):
                        ordered_rows[index + 1]["sourceLine"] = line_number
                    break
            continue
        branch_match = re.match(r"^(b(?:eq|ne|lt|ge|ltu|geu))([^,]+),([^,]+),", normalized)
        if branch_match:
            prefix = (branch_match.group(1) + branch_match.group(2)
                      + "," + branch_match.group(3) + ",")
            for index, row_text in enumerate(normalized_rows):
                if (ordered_rows[index].get("sourceLine") is None
                        and row_text.startswith(prefix)):
                    ordered_rows[index]["sourceLine"] = line_number
                    break


def compact_iterations(rows):
    """Merge dynamic loop iterations into one row per static instruction address."""
    compacted = []
    by_address = {}
    for row in rows:
        key = row.get("address") or normalize_instruction(row["instruction"])
        target = by_address.get(key)
        if target is None:
            target = {"instruction": row["instruction"], "address": row.get("address"),
                      "cycles": {}, "iterations": 0,
                      "squashedFetches": 0,
                      "mispredictions": 0,
                      "cacheEvents": [],
                      "sourceLine": row.get("sourceLine")}
            by_address[key] = target
            compacted.append(target)
        target["cycles"].update(row["cycles"])
        target["cacheEvents"].extend(row.get("cacheEvents", []))
        if row.get("squashed"):
            target["squashedFetches"] += 1
        else:
            target["iterations"] += 1
        if row.get("mispredicted"):
            target["mispredictions"] += 1
        if target.get("sourceLine") is None:
            target["sourceLine"] = row.get("sourceLine")
    for target in compacted:
        if target["squashedFetches"] and not target["iterations"]:
            target["squashed"] = True
    return compacted


def attach_dynamic_source_lines(dynamic_rows, compacted_rows):
    """Copy each static source mapping onto every executed occurrence."""
    by_address = {row.get("address"): row.get("sourceLine")
                  for row in compacted_rows if row.get("address")}
    by_instruction = {normalize_instruction(row["instruction"]): row.get("sourceLine")
                      for row in compacted_rows}
    for row in dynamic_rows:
        row["sourceLine"] = (by_address.get(row.get("address"))
                             or by_instruction.get(normalize_instruction(row["instruction"])))
        row["iterations"] = 0 if row.get("squashed") else 1


def parse_minor(path: Path, source_body="", include_fetch_stalls=True):
    fetched, rows, by_address = defaultdict(deque), [], defaultdict(deque)
    pending_decode_stalls = defaultdict(list)
    last_fetch_address = None
    raw = []
    register_events = defaultdict(dict)
    lines = path.read_text(errors="replace").splitlines()
    for line in lines:
        match = EVENT.match(line)
        if match:
            tick, stage, stalled, address, instruction = match.groups()
            raw.append((int(tick), stage, int(stalled) != 0, address.lower(), instruction.strip()))
            continue
        register_match = REGISTER_EVENT.match(line)
        if register_match:
            tick, register, value = register_match.groups()
            register_events[int(tick)][register] = value
    if not include_fetch_stalls:
        # MinorCPU's timing ports necessarily spend cycles requesting and
        # returning an instruction line, even with zero-contention Direct-1
        # memory. That model defines those plumbing waits away. Removing their
        # events also removes cycles in which no CPU stage did useful work,
        # while preserving decode/execute stalls caused by real hazards.
        raw = [event for event in raw
               if not (event[1] == "fetch1" and event[2])]
    if not raw:
        return {"instructions": [], "dynamicInstructions": [], "cycles": 0,
                "format": "minor", "registerDeltas": {},
                "pcDeltas": {}, "memoryDeltas": {}, "jumps": []}
    ticks = sorted({item[0] for item in raw})
    cycle_for_tick = {tick: index + 1 for index, tick in enumerate(ticks)}
    # A fetch-only wait is the instruction cache servicing that fetch, so it
    # remains F.  If a downstream stage is stalled in the same cycle, the
    # front end is instead held by pipeline back-pressure and remains S.
    downstream_stall_cycles = {
        cycle_for_tick[tick]
        for tick, stage, stalled, _address, _instruction in raw
        if stalled and stage != "fetch1"
    }

    def is_memory_access(instruction):
        displayed = display_instruction(instruction).strip().lower()
        opcode = displayed.split(None, 1)[0] if displayed else ""
        return bool(re.fullmatch(
            r"(?:l(?:b|bu|h|hu|w|wu|d)|fl[wdq]|s[bhwdq]|fs[wdq])",
            opcode,
        ))

    for tick, stage, stalled, address, instruction in raw:
        cycle = cycle_for_tick[tick]
        if stage == "fetch1":
            if not stalled:
                # MinorGUI can report the same redirect fetch on consecutive
                # clocks before decode accepts it. It is one fetch, and its
                # first clock is the architectural redirect point.
                if address != last_fetch_address or not fetched[address]:
                    fetched[address].append({"first": cycle, "stalls": []})
                last_fetch_address = address
            elif fetched[address]:
                fetched[address][-1]["stalls"].append((
                    cycle,
                    "S" if cycle in downstream_stall_cycles else "F",
                ))
            continue
        if stage == "decode":
            if stalled:
                row = next((candidate for candidate in by_address[address]
                            if "E" not in candidate["cycles"].values()), None)
                if row:
                    row["cycles"][str(cycle)] = "S"
                else:
                    pending_decode_stalls[address].append(cycle)
                continue
            row = {"instruction": display_instruction(instruction), "address": address, "cycles": {}}
            # A branch can speculatively fetch an address many times without
            # decoding it.  The decode belongs to the most recent fetch group.
            fetch = fetched[address].pop() if fetched[address] else None
            # Older fetches of this address were wrong-path instructions that
            # never reached Decode. Keep their Fetch cells so branch flushing
            # is visible instead of silently deleting them.
            for discarded in fetched[address]:
                rows.append({
                    "instruction": display_instruction(instruction),
                    "address": address,
                    "cycles": {str(discarded["first"]): "F"},
                    "squashed": True,
                })
            fetched[address].clear()
            if fetch:
                row["cycles"][str(fetch["first"])] = "F"
                for stall_cycle, fetch_stage in fetch["stalls"]:
                    row["cycles"][str(stall_cycle)] = fetch_stage
            for stall_cycle in pending_decode_stalls.pop(address, []):
                row["cycles"][str(stall_cycle)] = "S"
            row["cycles"][str(cycle)] = "D"
            rows.append(row)
            by_address[address].append(row)
            continue
        candidates = by_address[address]
        if stage == "execute":
            row = next((candidate for candidate in candidates
                        if "M" not in candidate["cycles"].values()
                        and "W" not in candidate["cycles"].values()), None)
        elif stage == "memory":
            row = next((candidate for candidate in candidates
                        if "E" in candidate["cycles"].values()
                        and "W" not in candidate["cycles"].values()), None)
        else:
            row = next((candidate for candidate in candidates
                        if "M" in candidate["cycles"].values()
                        and "W" not in candidate["cycles"].values()), None)
        if row:
            if stalled:
                visible_stage = ("M" if stage == "memory"
                                 and is_memory_access(instruction) else "S")
            else:
                visible_stage = STAGES[stage]
            row["cycles"][str(cycle)] = visible_stage
            if stage == "writeback":
                while candidates and "W" in candidates[0]["cycles"].values():
                    candidates.popleft()
    # Minor is an in-order, fixed-stage pipeline. Once an instruction has
    # entered Fetch, an unreported cycle before its Writeback means that it is
    # held in an inter-stage buffer by backpressure. MinorGUI reports the busy
    # downstream instruction rather than the buffered one in a few cases
    # (notably F->D and a returning cache miss), so make those genuine waits
    # explicit. Do not fill outside the instruction's lifetime, and do not
    # apply this rule to O3 where an empty cell can mean normal queue residence.
    for row in rows:
        occupied = sorted(int(cycle) for cycle in row["cycles"])
        if not occupied:
            continue
        for cycle in range(occupied[0], occupied[-1] + 1):
            cycle_text = str(cycle)
            if cycle_text not in row["cycles"]:
                row["cycles"][cycle_text] = "S"
                row.setdefault("inferredGaps", []).append(cycle)
        # MinorGUI does not always emit a final stalled-memory event on the
        # response cycle.  Once a load/store has entered M, every cycle until
        # W is cache/memory occupancy rather than an unexplained stall.
        if is_memory_access(row["instruction"]):
            memory = min((int(cycle) for cycle, stage in row["cycles"].items()
                          if stage == "M"), default=None)
            writeback = min((int(cycle) for cycle, stage in row["cycles"].items()
                             if stage == "W" and int(cycle) > (memory or 0)),
                            default=None)
            if memory is not None and writeback is not None:
                for cycle in range(memory, writeback):
                    row["cycles"][str(cycle)] = "M"
    rows.sort(key=lambda row: min(map(int, row["cycles"]), default=0))
    compacted = compact_iterations(rows)
    add_source_lines(compacted, source_body)
    attach_dynamic_source_lines(rows, compacted)
    # MinorGUI emits a complete register snapshot at the first writeback.
    # Compare it with the architectural reset state so unchanged zero-valued
    # registers are not all presented as writes by the first instruction.
    register_deltas = {}
    previous_registers = {
        **{f"x{index}": "0x00000000" for index in range(32)},
        **{f"f{index}": "0x00000000" for index in range(32)},
    }
    for tick in sorted(register_events):
        if tick not in cycle_for_tick:
            continue
        snapshot = register_events[tick]
        changed = {register: value for register, value in snapshot.items()
                   if register != "x0"
                   and previous_registers.get(register) != value}
        previous_registers.update(snapshot)
        if changed:
            register_deltas[str(cycle_for_tick[tick])] = changed
    exec_registers, memory_deltas = parse_exec_playback(lines, ticks)
    for cycle, changes in exec_registers.items():
        register_deltas.setdefault(cycle, {}).update(changes)
    pc_deltas = {}
    for tick, stage, stalled, address, _instruction in raw:
        if stage == "fetch1" and not stalled:
            pc_deltas[str(cycle_for_tick[tick])] = "0x" + address
    return {"instructions": compacted, "dynamicInstructions": rows,
            "cycles": len(ticks), "format": "minor",
            "registerDeltas": register_deltas, "pcDeltas": pc_deltas,
            "memoryDeltas": memory_deltas,
            "jumps": parse_taken_jumps(lines, ticks)}


def parse_o3(path: Path, source_body=""):
    rows, current = [], None
    stages = {"fetch": "F", "decode": "D", "rename": "R", "dispatch": "I", "issue": "E", "complete": "C", "retire": "W"}
    ticks = set()
    lines = path.read_text(errors="replace").splitlines()
    # O3PipeView emits a row only when a DynInst object is destroyed. At
    # simulation exit, a predictor-dependent number of already committed
    # objects can still be retained in gem5 buffers. O3CPUAll records commit
    # events immediately and is therefore the authoritative source for the
    # complete teaching stream. Keep O3PipeView below for older traces that
    # do not contain the sequence-numbered debug events.
    debug_data = parse_o3_debug_trace(lines, source_body)
    if any(not row.get("squashed")
           for row in debug_data.get("dynamicInstructions", [])):
        return debug_data
    for line in lines:
        parts = line.strip().split(":")
        if len(parts) < 3 or parts[0] != "O3PipeView":
            continue
        stage, tick = parts[1], int(parts[2])
        if tick > 0:
            ticks.add(tick)
        if stage == "fetch" and len(parts) >= 7:
            current = {"instruction": display_instruction(parts[6]), "address": parts[3].lower().removeprefix("0x"), "cycles": {}}
            rows.append(current)
        if current and stage in stages and tick > 0:
            key = str(tick)
            value = stages[stage]
            existing = current["cycles"].get(key, "")
            current["cycles"][key] = existing + value if value not in existing else existing
    rows = [row for row in rows if row["cycles"]]
    ticks = {int(tick) for row in rows for tick in row["cycles"]}
    if not rows or not ticks:
        return parse_o3_debug_trace(lines, source_body)
    sorted_ticks = sorted(ticks)
    intervals = [right - left for left, right in zip(sorted_ticks, sorted_ticks[1:]) if right > left]
    tick_period = 1
    for interval in intervals:
        tick_period = gcd(tick_period, interval) if tick_period > 1 else interval
    tick_period = max(1, tick_period)

    # O3PipeView writes one record when a dynamic instruction is destroyed.
    # A record without retire is a wrong-path instruction. The record has no
    # explicit squash time, so place X immediately after its last reported
    # stage; O3CPUAll's fallback parser uses the exact ROB squash event.
    for row in rows:
        if any("W" in stage for stage in row["cycles"].values()):
            continue
        row["squashed"] = True
        squash_tick = max(int(tick) for tick in row["cycles"]) + tick_period
        row["cycles"][str(squash_tick)] = "X"
        ticks.add(squash_tick)
    sorted_ticks = sorted(ticks)
    first_tick = sorted_ticks[0]
    ordered = {tick: ((tick - first_tick) // tick_period) + 1 for tick in sorted_ticks}
    for row in rows:
        row["cycles"] = {str(ordered[int(tick)]): value for tick, value in row["cycles"].items()}
    complete_o3_waits(rows)
    pc_deltas = {}
    for row in rows:
        for cycle, stage in row["cycles"].items():
            if stage == "F":
                pc_deltas[cycle] = "0x" + row["address"]
    compacted = compact_iterations(rows)
    add_source_lines(compacted, source_body)
    attach_dynamic_source_lines(rows, compacted)
    full_timeline = list(range(first_tick, sorted_ticks[-1] + tick_period, tick_period))
    register_deltas, memory_deltas = parse_exec_playback(lines, full_timeline)
    return {"instructions": compacted, "dynamicInstructions": rows,
            "cycles": ordered[sorted_ticks[-1]], "format": "o3",
            "registerDeltas": register_deltas, "pcDeltas": pc_deltas,
            "memoryDeltas": memory_deltas,
            "jumps": parse_taken_jumps(lines, full_timeline)}


def complete_o3_waits(rows):
    """Show O3 queue/ROB waits without mislabelling execution latency.

    O3PipeView reports transitions, not every occupied cycle. A gap from the
    combined Dispatch/Issue stage to Execute is an issue-queue wait and is a
    stall. Execute-to-Complete is functional-unit occupancy. One cycle after
    Complete is normal commit transport; any longer wait is a ROB stall.
    """
    for row in rows:
        positions = {}
        for cycle_text, stage in row["cycles"].items():
            for marker in ("I", "E", "C", "W"):
                if marker in stage:
                    positions.setdefault(marker, int(cycle_text))
        issue, execute = positions.get("I"), positions.get("E")
        complete, writeback = positions.get("C"), positions.get("W")
        if issue is not None and execute is not None:
            for cycle in range(issue + 1, execute):
                row["cycles"].setdefault(str(cycle), "S")
        if execute is not None and complete is not None:
            for cycle in range(execute + 1, complete):
                row["cycles"].setdefault(str(cycle), "E")
        if complete is not None and writeback is not None:
            for cycle in range(complete + 1, writeback):
                row["cycles"].setdefault(
                    str(cycle), "C" if cycle == complete + 1 else "S")


def schedule_lecture_o3_pipeline(data, configuration, start_address=None):
    """Build the multiple-issue Tomasulo/ROB table used in the lectures.

    gem5 supplies the committed dynamic instruction stream and architectural
    values. The exam model uses unlimited queues, configurable prediction and
    independent functional units, without gem5's internal transport delays.
    Every control transfer occupies an Issue cycle of its own.
    """
    if data.get("format") != "o3":
        return False
    configuration = {**DEFAULT_O3_CONFIG, **configuration, **O3_FIXED_CONFIG}

    def row_first_cycle(row):
        return min((int(cycle) for cycle in row.get("cycles", {})),
                   default=10**12)

    observed_squashes = [row for row in data.get("dynamicInstructions", [])
                         if row.get("squashed")]
    all_committed = sorted(
        (row for row in data.get("dynamicInstructions", [])
         if not row.get("squashed")),
        key=row_first_cycle,
    )
    if start_address is not None:
        start_index = next(
            (index for index, row in enumerate(all_committed)
             if row.get("address")
             and int(row["address"], 16) == start_address),
            None,
        )
        if start_index is None:
            fail("The evaluation start label was not executed in this trace.")
        committed = all_committed[start_index:]
    else:
        committed = all_committed
    if not committed:
        return False

    # gem5 uses a real local predictor to generate the architectural stream
    # for Ideal mode. Its raw miss annotations are implementation details of
    # that trace generator and must not appear in the perfect-prediction
    # teaching projection, summary, Control/cache column, or CSV export.
    if configuration.get("branchPredictor") == "ideal":
        for row in committed:
            row.pop("mispredicted", None)
            row.pop("mispredictTick", None)
            row.pop("predictionPenalty", None)

    original_cycles = {
        id(row): sorted(int(cycle) for cycle in row.get("cycles", {}))
        for row in all_committed
    }

    # gem5 supplies which speculative instructions were fetched on each wrong
    # path. Schedule those identities below using the same classroom clock,
    # dependencies, widths and units as the committed instructions.
    wrong_candidates = defaultdict(list)
    miss_branches = [row for row in committed
                     if row.get("mispredicted")
                     and row.get("mispredictCycle") is not None
                     and row.get("sequence") is not None]
    for wrong in observed_squashes:
        squash = min((int(cycle) for cycle, markers in wrong.get("cycles", {}).items()
                      if "X" in markers), default=None)
        if wrong.get("sequence") is None or squash is None:
            continue
        causes = [branch for branch in miss_branches
                  if branch["sequence"] < wrong["sequence"]
                  and branch["mispredictCycle"] <= squash]
        if causes:
            cause = max(causes, key=lambda branch: branch["sequence"])
            wrong_candidates[id(cause)].append(wrong)

    def instruction_info(instruction):
        text_value = display_instruction(instruction).strip().lower()
        parts = text_value.split(None, 1)
        opcode = parts[0] if parts else ""
        operands = parts[1] if len(parts) > 1 else ""
        operands = re.sub(
            r"\b(" + "|".join(map(re.escape, REGISTER_ALIASES)) + r")\b",
            lambda match: REGISTER_ALIASES[match.group(1)], operands,
        )
        registers = [register for register in re.findall(r"\b[xf]\d+\b", operands)
                     if register != "x0"]
        load = bool(re.fullmatch(r"(?:l(?:b|bu|h|hu|w|wu|d)|fl[wdq])", opcode))
        store = bool(re.fullmatch(r"(?:s[bhwdq]|fs[wdq])", opcode))
        control = bool(re.fullmatch(
            r"(?:b(?:eq|ne|lt|ge|ltu|geu|eqz|nez|ltz|gez|lez|gtz)|j|jr|jal|jalr|ret)", opcode))
        no_destination = store or opcode.startswith("b") or opcode in {"j", "jr", "ret"}
        destination = registers[0] if registers and not no_destination else None
        sources = registers if no_destination else registers[1:]
        store_data = registers[0] if store and registers else None
        execute_sources = registers[1:] if store else sources

        if control:
            unit, latency, pipelined = "jump", 1, True
        elif load or store:
            unit, latency, pipelined = "address", 1, True
        elif re.match(r"^fmul", opcode):
            unit, latency, pipelined = (
                "float-multiply", configuration["floatMul"],
                configuration["floatMulPipelined"])
        elif re.match(r"^fdiv", opcode):
            unit, latency, pipelined = (
                "float-divide", configuration["floatDiv"],
                configuration["floatDivPipelined"])
        elif re.match(r"^f", opcode):
            unit, latency, pipelined = (
                "float-alu", configuration["floatAlu"],
                configuration["floatAluPipelined"])
        elif re.match(r"^mul", opcode):
            unit, latency, pipelined = (
                "integer-multiply", configuration["intMul"],
                configuration["intMulPipelined"])
        elif re.match(r"^(?:div|rem)", opcode):
            unit, latency, pipelined = (
                "integer-divide", configuration["intDiv"],
                configuration["intDivPipelined"])
        else:
            unit, latency, pipelined = (
                "integer-alu", configuration["intAlu"],
                configuration["intAluPipelined"])
        return {
            "opcode": opcode, "destination": destination, "sources": sources,
            "storeData": store_data, "executeSources": execute_sources,
            "load": load, "store": store, "control": control, "unit": unit,
            "latency": max(1, int(latency)), "pipelined": bool(pipelined),
        }

    information = {}
    for row in committed:
        information[id(row)] = instruction_info(row["instruction"])

    issue_width = max(1, int(configuration["dispatchWidth"]))
    issue_cycles = {}
    front_end_cycles = {}
    visible_stages = set(configuration.get(
        "o3VisibleStages", DEFAULT_CONFIG["o3VisibleStages"]))
    show_front_end = bool(visible_stages & {"fetch", "decode", "rename"})
    stage_uses = {stage: defaultdict(int)
                  for stage in ("F", "D", "R", "I")}

    def reserve_front_stage(stage, earliest, width, exclusive=False, deadline=None):
        cycle = earliest
        while stage_uses[stage][cycle] >= (1 if exclusive else width):
            cycle += 1
        if deadline is not None and cycle > deadline:
            return None
        stage_uses[stage][cycle] += 1
        return cycle

    previous_stage = {stage: 1 for stage in ("F", "D", "R", "I")}
    fetch_floor = issue_floor = 1

    def allocate_front_end(info, deadline=None):
        nonlocal fetch_floor, issue_floor
        slots = {}
        previous = None
        for stage, width in (("F", configuration["fetchWidth"]),
                             ("D", configuration["decodeWidth"]),
                             ("R", configuration["renameWidth"]),
                             ("I", issue_width)):
            earliest = max(previous_stage[stage],
                           previous + 1 if previous is not None else fetch_floor,
                           issue_floor if stage == "I" else 1)
            slot = reserve_front_stage(
                stage, earliest, max(1, int(width)),
                exclusive=stage == "I" and info["control"], deadline=deadline)
            if slot is None:
                break
            slots[stage] = slot
            previous_stage[stage] = previous = slot
        if info["control"]:
            if "F" in slots:
                fetch_floor = slots["F"] + 1
            if "I" in slots:
                issue_floor = slots["I"] + 1
        return slots

    # Perfect-prediction exams retain their accepted schedule. Other predictors
    # allocate the frontend as branches resolve, including wrong-path work.
    real_prediction = configuration.get("branchPredictor") != "ideal"
    for row in ([] if real_prediction else committed):
        info = information[id(row)]
        slots = allocate_front_end(info)
        # Front-end bandwidth constrains Issue even when its stages are
        # hidden in the teaching table. Only render stages the user chose.
        front_end_cycles[id(row)] = (
            {stage: slots[stage] for stage in ("F", "D", "R")}
            if show_front_end else {})
        issue_cycles[id(row)] = slots["I"]
    cdb_width = max(1, int(configuration["writebackWidth"]))
    cdb_uses = defaultdict(int)
    # Each physical unit may put at most one result on a shared CDB per cycle.
    unit_counts = {
        "integer-alu": configuration["intAluUnits"],
        "integer-multiply": configuration["intMulUnits"],
        "integer-divide": configuration["intDivUnits"],
        "float-alu": configuration["floatAluUnits"],
        "float-multiply": configuration["floatMulUnits"],
        "float-divide": configuration["floatDivUnits"],
        "address": configuration["memoryUnits"],
        "jump": 1,
    }
    unit_cdb_uses = defaultdict(lambda: defaultdict(int))
    unit_busy = defaultdict(lambda: defaultdict(int))
    memory_busy = defaultdict(int)
    latest_producer = {}
    resolved_branches = []
    timing = {}

    def reserve_execution(info, earliest, deadline=None):
        cycle = earliest
        while True:
            if deadline is not None and cycle > deadline:
                return None
            occupied = ({cycle} if info["pipelined"]
                        else set(range(cycle, cycle + info["latency"])))
            if deadline is not None:
                occupied = {slot for slot in occupied if slot <= deadline}
            # No global EXE-queue/width limit: independent functional units
            # may start together. Each unit still enforces its own capacity.
            if all(unit_busy[info["unit"]][slot] < unit_counts[info["unit"]]
                   for slot in occupied):
                for slot in occupied:
                    unit_busy[info["unit"]][slot] += 1
                return cycle
            cycle += 1

    def reserve_cdb(earliest, unit, deadline=None):
        cycle = earliest
        while (cdb_uses[cycle] >= cdb_width
               or unit_cdb_uses[unit][cycle] >= unit_counts[unit]):
            cycle += 1
        if deadline is not None and cycle > deadline:
            return None
        cdb_uses[cycle] += 1
        unit_cdb_uses[unit][cycle] += 1
        return cycle

    speculation = bool(configuration.get("speculativeExecution", True))
    out_of_order_execution = bool(
        configuration.get("outOfOrderExecution", True))
    previous_execution_start = 0
    commit_width = max(1, int(configuration["commitWidth"]))
    commit_uses = defaultdict(int)
    previous_commit = 0
    wrong_path_by_branch = defaultdict(list)
    recovery_front_end = []
    pending_recovery = False

    def schedule_wrong_path(branch, resolution):
        """Run speculative work until the branch's ROB Commit check."""
        producers = latest_producer.copy()
        previous_start = previous_execution_start
        for wrong in sorted(wrong_candidates[id(branch)],
                            key=lambda row: row["sequence"]):
            info = instruction_info(wrong["instruction"])
            front = allocate_front_end(info, deadline=resolution)
            if not front:
                break
            cycles = {str(cycle): stage + "*" for stage, cycle in front.items()}
            issue = front.get("I")
            if issue is not None:
                # Waiting for operands, a unit or the ROB is a real wait. The
                # squash edge itself is X, never an invented S*/X completion.
                for cycle in range(issue + 1, resolution):
                    cycles[str(cycle)] = "S*"
                earliest = issue + 1
                for register in info["executeSources"]:
                    producer = producers.get(register)
                    if producer is not None:
                        earliest = max(earliest, (producer.get("C") or resolution) + 1)
                if not speculation:
                    earliest = resolution + 1
                if not out_of_order_execution:
                    earliest = max(earliest, previous_start)
                execute = reserve_execution(info, earliest, deadline=resolution)
                cdb = None
                if execute is not None:
                    previous_start = execute
                    execute_end = execute + info["latency"] - 1
                    for cycle in range(execute, min(execute_end, resolution) + 1):
                        cycles[str(cycle)] = "E*"
                    result_end = execute_end
                    if info["load"] and execute_end < resolution:
                        latency = (configuration["dataReadLatency"]
                                   if configuration["memoryMode"] == "direct"
                                   else configuration["cacheLatency"])
                        memory_start = execute_end + 1
                        while (memory_start <= resolution and any(
                                memory_busy[cycle] >= configuration["memoryUnits"]
                                for cycle in range(memory_start, min(
                                    memory_start + latency, resolution + 1)))):
                            memory_start += 1
                        result_end = memory_start + latency - 1
                        for cycle in range(memory_start, min(result_end, resolution) + 1):
                            memory_busy[cycle] += 1
                            cycles[str(cycle)] = "M*"
                    if info["destination"] is not None:
                        cdb = reserve_cdb(result_end + 1, info["unit"],
                                          deadline=resolution)
                        if cdb is not None:
                            cycles[str(cdb)] = "C*"
                if info["destination"] is not None:
                    producers[info["destination"]] = {"C": cdb}
            cycles[str(resolution)] = cycles.get(str(resolution), "") + "X"
            wrong_path_by_branch[id(branch)].append({
                **wrong, "cycles": cycles, "mispredicted": False,
                "squashCause": branch.get("address"),
            })

    for row in committed:
        info = information[id(row)]
        if real_prediction:
            front = allocate_front_end(info)
            if pending_recovery:
                recovery_front_end.extend(
                    {"stage": stage, "cycle": front[stage]}
                    for stage in ("F", "D", "R"))
                pending_recovery = False
            issue = front["I"]
            effective_front_end = ({stage: front[stage] for stage in ("F", "D", "R")}
                                   if show_front_end else {})
        else:
            issue = issue_cycles[id(row)]
            effective_front_end = front_end_cycles.get(id(row), {})
        earliest = issue + 1
        for register in info["executeSources"]:
            producer = latest_producer.get(register)
            if producer is not None and producer.get("C") is not None:
                earliest = max(earliest, producer["C"] + 1)
        if not speculation and resolved_branches:
            earliest = max(earliest, max(resolved_branches) + 1)
        if not out_of_order_execution:
            # Multiple instructions may start together when the configured
            # EXE width permits it, but no younger instruction may overtake
            # an older instruction that is waiting for operands or a unit.
            earliest = max(earliest, previous_execution_start)

        execute = reserve_execution(info, earliest)
        previous_execution_start = execute
        execute_end = execute + info["latency"] - 1
        memory_start = memory_end = None
        if info["load"]:
            memory_latency = (configuration["dataReadLatency"]
                              if configuration["memoryMode"] == "direct"
                              else configuration["cacheLatency"])
            memory_start = execute_end + 1
            while any(memory_busy[cycle] >= configuration["memoryUnits"]
                      for cycle in range(memory_start,
                                         memory_start + memory_latency)):
                memory_start += 1
            memory_end = memory_start + memory_latency - 1
            for cycle in range(memory_start, memory_end + 1):
                memory_busy[cycle] += 1

        produces_result = info["destination"] is not None
        cdb = (reserve_cdb((memory_end if info["load"] else execute_end) + 1,
                           info["unit"])
               if produces_result else None)
        record = {
            **info, "row": row, "I": issue, "E": execute,
            "EEnd": execute_end, "M": memory_start, "MEnd": memory_end,
            "C": cdb, "frontEnd": effective_front_end,
            "storeDataProducer": latest_producer.get(info["storeData"]),
        }
        timing[id(row)] = record
        if produces_result:
            latest_producer[info["destination"]] = record

        # Older instructions have already been scheduled, so the branch's
        # in-order Commit check is known before scheduling its wrong path.
        # This also preserves the accepted perfect-prediction Commit timing.
        ready = cdb + 1 if cdb is not None else execute_end + 1
        if info["store"]:
            producer = record["storeDataProducer"]
            if producer is not None and producer.get("C") is not None:
                ready = max(ready, producer["C"] + 1)
        commit = max(ready, previous_commit)
        while commit_uses[commit] >= commit_width:
            commit += 1
        commit_uses[commit] += 1
        record["W"] = commit
        previous_commit = commit

        if info["control"]:
            resolved_branches.append(execute_end)
            if (configuration.get("branchPredictor") != "ideal"
                    and row.get("mispredicted")):
                schedule_wrong_path(row, commit)
                # The outcome is known after EXE, but the book's ROB model
                # flushes younger work when the branch reaches the Commit
                # check. Correct-path F starts next cycle, followed by D/R/I.
                row["predictionPenalty"] = commit - execute_end + 4
                fetch_floor = max(fetch_floor, commit + 1)
                issue_floor = max(issue_floor, commit + 1)
                pending_recovery = True

    # Preserve actual architectural values but move their visible updates to
    # lecture Commit (and load/store accesses to MEM/Commit respectively).
    register_events = defaultdict(list)
    for cycle, changes in data.get("registerDeltas", {}).items():
        for register, value in changes.items():
            register_events[register].append((int(cycle), value))
    first_original_cycle = min(original_cycles[id(row)][0] for row in committed
                               if original_cycles[id(row)])
    initial_registers = {}
    for register, events in register_events.items():
        for cycle, value in sorted(events):
            if cycle >= first_original_cycle:
                break
            initial_registers[register] = value
    new_registers = defaultdict(dict)
    used_register_events = set()
    for row in committed:
        info = information[id(row)]
        destination = info["destination"]
        if destination is None:
            continue
        bounds = original_cycles[id(row)]
        for event_index, (cycle, value) in enumerate(register_events[destination]):
            event_key = (destination, event_index)
            if (event_key not in used_register_events and bounds
                    and bounds[0] <= cycle <= bounds[-1] + 2):
                new_registers[str(timing[id(row)]["W"])][destination] = value
                used_register_events.add(event_key)
                break

    memory_events = defaultdict(deque)
    for cycle, changes in sorted(data.get("memoryDeltas", {}).items(),
                                 key=lambda item: int(item[0])):
        for address, event in changes.items():
            memory_events[event.get("pc", "")].append((address, event))
    new_memory = defaultdict(dict)
    for row in committed:
        queue = memory_events["0x" + row.get("address", "")]
        if not queue:
            continue
        address, event = queue.popleft()
        current = timing[id(row)]
        event_cycle = current["MEnd"] if information[id(row)]["load"] else current["W"]
        if event_cycle is not None:
            new_memory[str(event_cycle)][address] = event

    for row in committed:
        current = timing[id(row)]
        info = information[id(row)]
        first_cycle = current["frontEnd"].get("F", current["I"])
        cycles = {str(cycle): "S" for cycle in range(first_cycle, current["W"] + 1)}
        for stage, cycle in current["frontEnd"].items():
            cycles[str(cycle)] = stage
        cycles[str(current["I"])] = "I"
        for cycle in range(current["E"], current["EEnd"] + 1):
            cycles[str(cycle)] = "E"
        if current["M"] is not None:
            for cycle in range(current["M"], current["MEnd"] + 1):
                cycles[str(cycle)] = "M"
        if current["C"] is not None:
            cycles[str(current["C"])] = "C"
        cycles[str(current["W"])] = "MW" if info["store"] else "W"
        row["cycles"] = cycles

    branch_rows = defaultdict(deque)
    for row in committed:
        if information[id(row)]["control"]:
            branch_rows[row.get("address")].append(row)
    relocated_jumps = []
    for jump in data.get("jumps", []):
        candidates = branch_rows[jump.get("fromAddress")]
        if candidates:
            # Exec records control transfers at retirement. Match that dynamic
            # occurrence rather than consuming an earlier untaken iteration.
            branch = min(candidates, key=lambda candidate: abs(
                max(original_cycles[id(candidate)], default=0) - jump["cycle"]))
            candidates.remove(branch)
            relocated_jumps.append({
                **jump, "cycle": timing[id(branch)]["W"],
                "instructionSequence": branch.get("sequence"),
            })

    displayed_rows = []
    for row in committed:
        displayed_rows.append(row)
        displayed_rows.extend(sorted(
            wrong_path_by_branch[id(row)],
            key=lambda wrong: (row_first_cycle(wrong), wrong["sequence"])))

    data["dynamicInstructions"] = displayed_rows
    data["instructions"] = compact_iterations(displayed_rows)
    data["registerDeltas"] = dict(new_registers)
    data["initialRegisters"] = initial_registers
    data["memoryDeltas"] = dict(new_memory)
    data["jumps"] = relocated_jumps
    data["recoveryFrontEnd"] = recovery_front_end
    data["cycles"] = max(
        max(current["W"] for current in timing.values()),
        max((int(cycle) for row in displayed_rows for cycle in row["cycles"]),
            default=0),
    )
    data["lectureTimingApplied"] = True
    return True


def configure_o3_stage_display(data, configuration):
    """Project gem5 O3 events onto the lecture-level speculative pipeline.

    gem5 dispatch corresponds to the lectures' in-order Issue/Dispatch step;
    gem5 issue starts out-of-order execution; complete is the CDB/write-result
    event; and retire is in-order Commit. For memory operations, execution
    cycles after address generation are labelled M. Hidden front-end stages
    are removed from cells without changing instruction timing relative to
    other instructions.
    """
    if data.get("format") != "o3":
        return
    visible = set(configuration.get("o3VisibleStages", DEFAULT_CONFIG["o3VisibleStages"]))
    marker_stage = {
        "F": "fetch", "D": "decode", "R": "rename", "I": "issue",
        "E": "execute", "M": "memory", "C": "cdb", "W": "commit",
        "S": "stall", "X": "squash",
    }
    load_opcode = re.compile(r"^(?:l(?:b|bu|h|hu|w|wu|d)|fl[wdq])$", re.I)
    store_opcode = re.compile(r"^(?:s[bhwdq]|fs[wdq])$", re.I)
    conditional_branch = re.compile(r"^b(?:eq|ne|lt|ge|ltu|geu)$", re.I)
    rows = data.get("dynamicInstructions", [])
    for row in rows:
        opcode = display_instruction(row.get("instruction", "")).split(None, 1)[0]

        # A CDB count is broadcast bandwidth, not result latency: one result
        # occupies one C cell. Stores and control transfers do not write a
        # register result on the CDB in the lecture-level Tomasulo model.
        cdb_cycles = sorted(
            int(cycle) for cycle, markers in row.get("cycles", {}).items()
            if "C" in markers)
        destination = destination_register(row.get("instruction", ""))
        writes_register = (not store_opcode.match(opcode)
                           and not conditional_branch.match(opcode)
                           and destination not in {None, "x0"})
        retained_cdb = 1 if writes_register else 0
        for cycle in cdb_cycles[retained_cdb:]:
            markers = row["cycles"][str(cycle)]
            row["cycles"][str(cycle)] = (
                markers.replace("C", "S") if writes_register
                else re.sub(r"C\*?", "", markers))

        # A load first calculates its address in EXE and then occupies MEM
        # until its value can be broadcast. A store updates memory only when
        # it commits, so show MEM and Commit together for that architectural
        # event instead of inventing a store CDB write.
        if load_opcode.match(opcode):
            execute_cycles = sorted(
                int(cycle) for cycle, markers in row.get("cycles", {}).items()
                if "E" in markers)
            for cycle in execute_cycles[1:]:
                markers = row["cycles"][str(cycle)]
                row["cycles"][str(cycle)] = markers.replace("E", "M")
        elif store_opcode.match(opcode):
            for cycle, markers in list(row.get("cycles", {}).items()):
                if "W" in markers and "M" not in markers:
                    row["cycles"][cycle] = markers.replace("W", "MW")

        filtered = {}
        for cycle, markers in row.get("cycles", {}).items():
            kept = "".join(token for token in re.findall(r"[A-Z]\*?", markers)
                           if marker_stage.get(token[0]) in visible
                           or token[0] in {"S", "X"})
            if kept:
                filtered[cycle] = kept
        row["cycles"] = filtered

    occupied = [int(cycle) for row in rows for cycle in row.get("cycles", {})]
    if not occupied:
        return
    offset = min(occupied) - 1

    # Omit hidden refill clocks from the browser's columns. Preserve the full
    # timeline for playback, CPI, register/memory updates and full CSV results.
    data["collapsedRecoveryCycles"] = sorted({
        event["cycle"] - offset
        for event in data.get("recoveryFrontEnd", [])
        if marker_stage[event["stage"]] not in visible
        and event["cycle"] > offset
    })

    def shift_cycle_map(values):
        return {str(int(cycle) - offset): value for cycle, value in values.items()
                if int(cycle) > offset}

    if offset:
        for row in rows:
            row["cycles"] = shift_cycle_map(row.get("cycles", {}))
        for key in ("registerDeltas", "memoryDeltas"):
            data[key] = shift_cycle_map(data.get(key, {}))
        for jump in data.get("jumps", []):
            jump["cycle"] = max(1, jump["cycle"] - offset)

    # In an O3 view with Fetch hidden, associate the PC monitor with the
    # instruction's first visible teaching stage instead of a hidden fetch.
    data["pcDeltas"] = {
        str(min(int(cycle) for cycle in row["cycles"])): "0x" + row["address"]
        for row in rows if row.get("address") and row.get("cycles")
    }
    data["instructions"] = compact_iterations(rows)
    data["cycles"] = max(
        (int(cycle) for row in rows for cycle in row.get("cycles", {})), default=0)


def parse_o3_debug_trace(lines, source_body=""):
    """Build an O3 timeline from O3CPUAll when O3PipeView emits no records.

    gem5 22 writes O3PipeView records from a dynamic instruction's destructor.
    A short program can finish while its committed instructions are still retained,
    leaving an otherwise useful trace with no O3PipeView lines.  O3CPUAll contains
    the same sequence-numbered stage transitions, so use those as a reliable
    fallback. Retain committed instructions and mark wrong-path speculative
    instructions at the cycle in which the ROB squashes them.
    """
    exec_instructions = {}
    fetched_instructions = {}
    for line in lines:
        match = EXEC_EVENT.match(line)
        if match:
            _tick, address, instruction, _op_class, _result = match.groups()
            exec_instructions.setdefault(address.lower(), display_instruction(instruction))
        pipe_fetch = re.match(
            r"^O3PipeView:fetch:\d+:0x([0-9a-fA-F]+):\d+:(\d+):(.*)$",
            line,
        )
        if pipe_fetch:
            fetched_instructions[int(pipe_fetch.group(2))] = display_instruction(
                pipe_fetch.group(3))

    rows = {}
    event_ticks = set()

    def record(sequence, address, tick, stage):
        address = address.lower()
        row = rows.setdefault(sequence, {
            "instruction": fetched_instructions.get(
                int(sequence), exec_instructions.get(
                    address, f"instruction @ 0x{address}")),
            "address": address,
            "sequence": int(sequence),
            "ticks": {},
        })
        # Preserve both labels when zero-latency stages share a clock edge.
        existing = row["ticks"].get(tick, "")
        row["ticks"][tick] = existing + stage if stage not in existing else existing
        event_ticks.add(tick)

    patterns = (
        (re.compile(r"^\s*(\d+): .*?fetch: .*?Instruction PC \(0x([0-9a-fA-F]+).*?created \[sn:(\d+)\]"), "F", (3, 2)),
        (re.compile(r"^\s*(\d+): .*?decode: .*?Processing instruction \[sn:(\d+)\] with PC \(0x([0-9a-fA-F]+)"), "D", (2, 3)),
        (re.compile(r"^\s*(\d+): .*?rename: .*?Processing instruction \[sn:(\d+)\] with PC \(0x([0-9a-fA-F]+)"), "R", (2, 3)),
        (re.compile(r"^\s*(\d+): .*?commit: .*?\[sn:(\d+)\] Inserting PC \(0x([0-9a-fA-F]+).*?into ROB"), "I", (2, 3)),
        (re.compile(r"^\s*(\d+): .*?iq: .*?Issuing instruction PC \(0x([0-9a-fA-F]+).*?\[sn:(\d+)\]"), "E", (3, 2)),
        (re.compile(r"^\s*(\d+): .*?iew: Sending instructions to commit, \[sn:(\d+)\] PC \(0x([0-9a-fA-F]+)"), "C", (2, 3)),
        (re.compile(r"^\s*(\d+): .*?commit: .*?\[sn:(\d+)\] Committing instruction with PC \(0x([0-9a-fA-F]+)"), "W", (2, 3)),
    )
    for line in lines:
        for pattern, stage, (sequence_group, address_group) in patterns:
            match = pattern.match(line)
            if match:
                record(match.group(sequence_group), match.group(address_group),
                       int(match.group(1)), stage)
                break

        misprediction = re.match(
            r"^\s*(\d+): .*?(?:iew: .*?\[sn:(\d+)\] Execute: Branch "
            r"mispredict detected|decode: .*?\[sn:(\d+)\] Squashing due to "
            r"incorrect branch prediction detected at decode)",
            line,
        )
        if misprediction:
            tick, execute_sequence, decode_sequence = misprediction.groups()
            sequence = execute_sequence or decode_sequence
            row = rows.get(sequence)
            if row is not None:
                row["mispredicted"] = True
                row["mispredictTick"] = int(tick)

        squash = re.match(
            r"^\s*(\d+): .*?rob: .*?Squashing instruction PC "
            r"\(0x([0-9a-fA-F]+).*?seq num (\d+)\.",
            line,
        )
        if not squash:
            # Instructions still in Fetch/Decode/Rename never enter the ROB,
            # but gem5 reports their individual squash with a sequence number.
            squash = re.match(
                r"^\s*(\d+): .*?cpu: Squashing instruction, "
                r"\[tid:\d+\] \[sn:(\d+)\] PC \(0x([0-9a-fA-F]+)",
                line,
            )
            if squash:
                tick, sequence, address = squash.groups()
                squash = (tick, address, sequence)
        if squash:
            tick, address, sequence = (squash.groups()
                                       if hasattr(squash, "groups") else squash)
            if not any("X" in markers for markers in
                       rows.get(sequence, {}).get("ticks", {}).values()):
                record(sequence, address, int(tick), "X")
            rows[sequence]["squashed"] = True

    visible_rows = [row for row in rows.values()
                    if row.get("squashed")
                    or any("W" in stage for stage in row["ticks"].values())]
    if not visible_rows or not event_ticks:
        return {"instructions": [], "dynamicInstructions": [], "cycles": 0,
                "format": "o3", "registerDeltas": {},
                "pcDeltas": {}, "memoryDeltas": {}, "jumps": []}

    sorted_ticks = sorted(event_ticks)
    intervals = [right - left for left, right in zip(sorted_ticks, sorted_ticks[1:]) if right > left]
    tick_period = intervals[0] if intervals else 1
    for interval in intervals[1:]:
        tick_period = gcd(tick_period, interval)
    tick_period = max(1, tick_period)
    first_tick = min(min(row["ticks"]) for row in visible_rows)
    last_tick = max(max(row["ticks"]) for row in visible_rows)

    dynamic_rows = []
    for row in sorted(visible_rows, key=lambda item: min(item["ticks"])):
        if row.get("squashed"):
            squash_tick = min(tick for tick, markers in row["ticks"].items()
                              if "X" in markers)
            # Debug messages for instructions *skipped* by Decode may arrive
            # after the frontend has already discarded them. Those are not
            # actual Decode occupancy and must not appear after squash.
            row["ticks"] = {tick: markers for tick, markers in row["ticks"].items()
                            if tick <= squash_tick}
        cycles = {str(((tick - first_tick) // tick_period) + 1): stage
                  for tick, stage in row.pop("ticks").items()}
        row["cycles"] = cycles
        if row.get("mispredictTick") is not None:
            row["mispredictCycle"] = (
                (row["mispredictTick"] - first_tick) // tick_period) + 1
        dynamic_rows.append(row)
    complete_o3_waits(dynamic_rows)
    pc_deltas = {cycle: "0x" + row["address"] for row in dynamic_rows
                 for cycle, stage in row["cycles"].items() if stage == "F"}
    compacted = compact_iterations(dynamic_rows)
    add_source_lines(compacted, source_body)
    attach_dynamic_source_lines(dynamic_rows, compacted)
    timeline = list(range(first_tick, last_tick + tick_period, tick_period))
    register_deltas, memory_deltas = parse_exec_playback(lines, timeline)
    return {"instructions": compacted, "dynamicInstructions": dynamic_rows,
            "cycles": ((last_tick - first_tick) // tick_period) + 1,
            "format": "o3", "registerDeltas": register_deltas,
            "pcDeltas": pc_deltas, "memoryDeltas": memory_deltas,
            "jumps": parse_taken_jumps(lines, timeline)}


DATA_DIRECTIVE_BYTES = {
    ".byte": 1, ".ascii": 1, ".asciz": 1, ".string": 1,
    ".space": 1, ".skip": 1, ".zero": 1,
    ".half": 2, ".hword": 2, ".short": 2, ".2byte": 2,
    ".word": 4, ".long": 4, ".int": 4, ".4byte": 4,
    ".float": 4, ".single": 4,
    ".dword": 8, ".quad": 8, ".8byte": 8, ".double": 8,
}


def split_assembly_operands(text):
    """Split comma-separated directive operands, keeping quoted strings."""
    operands, current, quoted, escaped = [], "", False, False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            current += character
        elif character == '"':
            quoted = True
            current += character
        elif character == ",":
            operands.append(current.strip())
            current = ""
        else:
            current += character
    if current.strip():
        operands.append(current.strip())
    return operands


def strip_assembly_comment(line):
    """Drop a ``#`` comment unless the hash sits inside a string literal."""
    quoted = escaped = False
    for index, character in enumerate(line):
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character == "#":
            return line[:index]
    return line


def data_directive_layout(directive, operands):
    """Return (element bytes, emitted bytes or None) for a data directive."""
    arguments = split_assembly_operands(operands)
    if directive == ".fill":
        try:
            repeat = int(arguments[0], 0)
            size = int(arguments[1], 0) if len(arguments) > 1 else 1
        except (IndexError, ValueError):
            return 1, None
        return (size if size in {1, 2, 4, 8} else 1), repeat * size
    element = DATA_DIRECTIVE_BYTES[directive]
    if directive in {".space", ".skip", ".zero"}:
        try:
            return element, int(arguments[0], 0)
        except (IndexError, ValueError):
            return element, None
    if directive in {".ascii", ".asciz", ".string"}:
        total = 0
        for argument in arguments:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    text = ast.literal_eval(argument)
            except (ValueError, SyntaxError):
                return element, None
            if not isinstance(text, str):
                return element, None
            total += len(text.encode("latin-1", "replace"))
            total += directive != ".ascii"
        return element, total
    return element, len(arguments) * element


def source_data_layouts(folder):
    """Map data labels to the element width declared in the assembly source.

    The ELF only records where a label starts, so ``v: .byte 1, 2, 3`` and
    ``v: .word 0x030201`` look alike there.  Reading the directive that
    follows each label lets the memory tab show ``v[0]``, ``v[1]``... with
    the declared element width.  Consecutive data directives after one label
    extend the same variable.
    """
    try:
        source = source_file(folder).read_text(errors="replace")
    except (OSError, ValueError):
        return {}
    label = re.compile(r"\s*([A-Za-z_.$][\w.$]*)\s*:")
    statement_pattern = re.compile(r"(\S+)\s*(.*)")
    layouts, pending, open_labels = {}, [], []
    for raw_line in source.splitlines():
        line = strip_assembly_comment(raw_line)
        while match := label.match(line):
            pending.append(match.group(1))
            line = line[match.end():]
        statement = statement_pattern.match(line.strip())
        if not statement:
            continue
        directive = statement.group(1).lower()
        if directive not in DATA_DIRECTIVE_BYTES and directive != ".fill":
            open_labels = []
            if not directive.startswith("."):
                pending = []
            continue
        element, byte_count = data_directive_layout(directive,
                                                    statement.group(2))
        untyped_storage = directive in {".space", ".skip", ".zero"}
        if pending:
            for name in pending:
                layouts[name] = {"elementSize": element,
                                 "byteCount": byte_count,
                                 "untypedStorage": untyped_storage}
            open_labels, pending = pending, []
            continue
        for name in open_labels:
            layout = layouts[name]
            if layout["elementSize"] != element:
                layout["elementSize"] = 1
            layout["untypedStorage"] = (
                layout.get("untypedStorage", False) and untyped_storage)
            layout["byteCount"] = (
                None if layout["byteCount"] is None or byte_count is None
                else layout["byteCount"] + byte_count)
    return layouts


def elf_section_headers(image):
    """Return (byte order, section headers) from an ELF image."""
    try:
        if image[:4] != b"\x7fELF" or image[5] not in {1, 2}:
            return None, []
        elf_class = image[4]
        endian = "<" if image[5] == 1 else ">"
        if elf_class == 1:
            section_offset = struct.unpack_from(endian + "I", image, 32)[0]
            section_entry_size = struct.unpack_from(endian + "H", image, 46)[0]
            section_count = struct.unpack_from(endian + "H", image, 48)[0]
            section_format = endian + "IIIIIIIIII"
        elif elf_class == 2:
            section_offset = struct.unpack_from(endian + "Q", image, 40)[0]
            section_entry_size = struct.unpack_from(endian + "H", image, 58)[0]
            section_count = struct.unpack_from(endian + "H", image, 60)[0]
            section_format = endian + "IIQQQQIIQQ"
        else:
            return None, []
        if section_entry_size < struct.calcsize(section_format):
            return None, []
        sections = []
        for index in range(section_count):
            offset = section_offset + index * section_entry_size
            fields = struct.unpack_from(section_format, image, offset)
            sections.append({
                "type": fields[1], "address": fields[3],
                "offset": fields[4], "size": fields[5],
            })
    except (IndexError, struct.error):
        return None, []
    return ("little" if endian == "<" else "big"), sections


def containing_section(sections, start, size=1):
    return next(
        (section for section in sections
         if section["address"] <= start
         and start + size <= section["address"] + section["size"]),
        None,
    )


def data_symbols(folder):
    """Return watchable variables from the project's current compiled ELF.

    GNU nm omits the size column for assembly labels that have no explicit
    ``.size`` directive.  Parsing that output with one optional hexadecimal
    group is ambiguous because symbol-type letters such as ``b`` and ``d``
    are themselves hexadecimal digits.  Split the fields first so ordinary
    assembly variables remain discoverable.

    Data placed before ``_start`` without an explicit ``.data`` directive is
    emitted into ``.text`` by the assembler.  Treat those pre-entry symbols
    as watchable data too, while excluding code labels at and after _start.

    Without ``.size`` a variable extends to the next symbol or to the end of
    its section, whichever comes first, so short byte buffers never overlap
    their neighbours.  The source directive then provides the element width.
    """
    elf = folder / f"{artifact_stem(folder)}.elf"
    if not elf.exists():
        return []
    env = settings_env()
    objdump = Path(env.get("OBJDUMP", "objdump"))
    nm_name = (objdump.name[:-len("objdump")] + "nm"
               if objdump.name.endswith("objdump") else "nm")
    nm = str(objdump.with_name(nm_name))
    try:
        result = subprocess.run([nm, "-n", "-S", str(elf)], cwd=folder, env=env,
                                text=True, capture_output=True)
        _byte_order, sections = elf_section_headers(elf.read_bytes())
    except OSError:
        return []
    if result.returncode:
        return []
    raw_symbols = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3:
            address, kind, symbol = fields
            size = None
        elif len(fields) == 4:
            address, size, kind, symbol = fields
        else:
            continue
        if (not re.fullmatch(r"[0-9a-fA-F]+", address)
                or (size is not None and not re.fullmatch(r"[0-9a-fA-F]+", size))
                or not re.fullmatch(r"[A-Za-z?]", kind)):
            continue
        raw_symbols.append({
            "name": symbol,
            "numericAddress": int(address, 16),
            "declaredSize": int(size, 16) if size else 0,
            "kind": kind,
        })
    raw_symbols.sort(key=lambda symbol: symbol["numericAddress"])
    entry_address = next(
        (symbol["numericAddress"] for symbol in raw_symbols
         if symbol["name"] == "_start"),
        None,
    )
    layouts = source_data_layouts(folder)
    symbols = []
    for symbol in raw_symbols:
        if symbol["name"].startswith(("_", "$")):
            continue
        is_data_section = symbol["kind"] in "bBdDgGrRsS"
        is_pre_entry_data = (symbol["kind"] in "tT"
                             and entry_address is not None
                             and symbol["numericAddress"] < entry_address)
        if not is_data_section and not is_pre_entry_data:
            continue
        address = symbol["numericAddress"]
        limits = [candidate["numericAddress"] for candidate in raw_symbols
                  if candidate["numericAddress"] > address][:1]
        section = containing_section(sections, address)
        if section is not None:
            limits.append(section["address"] + section["size"])
        size = symbol["declaredSize"] or (min(limits) - address if limits else 0)
        layout = layouts.get(symbol["name"], {})
        if layout.get("byteCount"):
            size = min(size, layout["byteCount"]) if size else layout["byteCount"]
        element = layout.get("elementSize") or (4 if size % 4 == 0 else 1)
        size = size or element
        if size % element:
            element = 1
        symbols.append({
            "name": symbol["name"],
            "address": f"0x{address:x}",
            "size": size,
            "kind": symbol["kind"],
            "elementSize": element,
            "elementCount": size // element,
            # .space/.skip/.zero reserve bytes without declaring a data type.
            # The memory tab may therefore group this storage according to
            # the selected numeric view (for example four bytes per float).
            "untypedStorage": bool(layout.get("untypedStorage")),
        })
    return symbols


def elf_initial_memory(folder, symbols=None):
    """Read initial watched bytes from ELF sections, including zeroed BSS.

    Runtime traces only contain memory transactions. Seeding playback from
    the linked image lets a student inspect a declared variable even when the
    program never loads or stores it.  Each symbol address maps to its bytes
    in memory order, hex-encoded, so the frontend can rebuild elements of any
    width and apply byte or halfword stores on top of them.
    """
    elf = folder / f"{artifact_stem(folder)}.elf"
    if not elf.exists():
        return {}
    symbols = data_symbols(folder) if symbols is None else symbols
    if not symbols:
        return {}
    try:
        image = elf.read_bytes()
    except OSError:
        return {}
    _byte_order, sections = elf_section_headers(image)

    initial = {}
    for symbol in symbols:
        start = int(symbol["address"], 16)
        size = max(1, min(int(symbol.get("size", 4)), 1024 * 1024))
        section = containing_section(sections, start, size)
        if section is None:
            continue
        if section["type"] == 8:  # SHT_NOBITS (.bss/.sbss)
            chunk = bytes(size)
        else:
            file_offset = section["offset"] + start - section["address"]
            chunk = image[file_offset:file_offset + size]
            if len(chunk) != size:
                continue
        initial[symbol["address"]] = chunk.hex()
    return initial


def elf_memory_map(folder):
    """Return allocated ELF sections for the educational memory-map panel."""
    elf = folder / f"{artifact_stem(folder)}.elf"
    if not elf.exists():
        return []
    env = settings_env()
    try:
        result = subprocess.run(
            [env.get("OBJDUMP", "objdump"), "-h", str(elf)],
            cwd=folder, env=env, text=True, capture_output=True,
        )
    except OSError:
        return []
    if result.returncode:
        return []
    symbol_addresses = {}
    objdump = Path(env.get("OBJDUMP", "objdump"))
    nm_name = (objdump.name[:-len("objdump")] + "nm"
               if objdump.name.endswith("objdump") else "nm")
    try:
        symbols = subprocess.run(
            [str(objdump.with_name(nm_name)), "-n", str(elf)], cwd=folder,
            env=env, text=True, capture_output=True,
        )
        if symbols.returncode == 0:
            for line in symbols.stdout.splitlines():
                fields = line.split()
                if (len(fields) >= 3
                        and re.fullmatch(r"[0-9a-fA-F]+", fields[0])):
                    symbol_addresses[fields[-1]] = int(fields[0], 16)
    except OSError:
        pass

    sections = []
    lines = result.stdout.splitlines()
    header = re.compile(
        r"^\s*\d+\s+(\S+)\s+([0-9a-fA-F]+)\s+"
        r"([0-9a-fA-F]+)\s+[0-9a-fA-F]+\s+[0-9a-fA-F]+\s+\S+\s*$")
    for index, line in enumerate(lines):
        match = header.match(line)
        if not match:
            continue
        name, size_text, start_text = match.groups()
        size, start = int(size_text, 16), int(start_text, 16)
        flags = (lines[index + 1].strip().split(", ")
                 if index + 1 < len(lines) else [])
        if not size or "ALLOC" not in flags:
            continue
        if "CODE" in flags:
            kind = "code"
        elif "DATA" in flags and "READONLY" in flags:
            kind = "read-only data"
        elif "DATA" in flags:
            kind = "data"
        elif "LOAD" not in flags:
            kind = "zero-initialized data"
        else:
            kind = "allocated"
        section = {
            "name": name,
            "kind": kind,
            "start": f"0x{start:x}",
            "end": f"0x{start + size - 1:x}",
            "size": size,
        }
        if name == ".text":
            entry = symbol_addresses.get("_start")
            end = symbol_addresses.get("End")
            stop = start + size
            if (entry is not None and end is not None
                    and start <= entry <= end <= stop):
                parts = []
                if entry > start:
                    parts.append({
                        "name": "Before _start",
                        "start": f"0x{start:x}", "end": f"0x{entry - 1:x}",
                        "size": entry - start,
                    })
                parts.append({
                    "name": "Pipeline-visible code",
                    "start": f"0x{entry:x}",
                    "end": f"0x{max(entry, end) - 1:x}" if end > entry else "—",
                    "size": end - entry,
                })
                if end < stop:
                    parts.append({
                        "name": "Protected End block / remainder",
                        "start": f"0x{end:x}", "end": f"0x{stop - 1:x}",
                        "size": stop - end,
                    })
                section["parts"] = parts
        sections.append(section)
    return sorted(sections, key=lambda section: int(section["start"], 16))


def gem5_statistics(path: Path):
    """Read scalar gem5 statistics without depending on their comments."""
    if not path.exists():
        return {}
    statistics = {}
    for line in path.read_text(errors="replace").splitlines():
        match = re.match(r"^(\S+)\s+(\S+)", line)
        if not match:
            continue
        name, value = match.groups()
        try:
            statistics[name] = float(value)
        except ValueError:
            continue
    return statistics


def branch_predictor_report(result_dir, configuration, teaching_cycles=None):
    """Summarize the real predictor outcome independently of table timing."""
    if configuration.get("cpu") != "out-of-order":
        return ""
    names = {
        "ideal": "Ideal (teaching baseline)",
        "local": "Local counter (2-bit baseline)",
        "tournament": "Tournament (local + global)",
        "bimode": "Bi-mode", "tage": "TAGE (tagged hybrid)",
    }
    if configuration.get("branchPredictor") == "ideal":
        return "\n".join([
            "Branch prediction:",
            "  Predictor: Ideal (teaching baseline)",
        ])
    statistics = gem5_statistics(result_dir / "stats.txt")
    predictions = int(statistics.get("system.cpu.branchPred.condPredicted", 0))
    incorrect = int(statistics.get("system.cpu.branchPred.condIncorrect", 0))
    correct = max(0, predictions - incorrect)
    accuracy = (100 * correct / predictions) if predictions else 0
    gem5_cycles = int(statistics.get("system.cpu.numCycles", 0))
    committed = int(statistics.get("system.cpu.committedInsts", 0))
    gem5_cpi = (gem5_cycles / committed) if committed else 0
    lines = [
        "Branch prediction:",
        f"  Predictor: {names.get(configuration.get('branchPredictor'), 'Local counter (2-bit baseline)')}",
        f"  Conditional predictions: {predictions}",
        f"  Correct: {correct} · Incorrect: {incorrect} · Accuracy: {accuracy:.1f}%",
    ]
    if gem5_cycles:
        lines.append(
            f"  Detailed gem5 completion: {gem5_cycles} cycles · CPI {gem5_cpi:.3f}")
    lines.append(
        "  The teaching pipeline uses gem5's real prediction outcomes. "
        "BP miss labels and recovery cycles show their timing effect. "
        "Wrong-path instruction identities come from gem5; their starred "
        "stages use the same teaching scheduler as committed instructions. "
        "X marks the squash at the branch's Commit check; the outcome "
        "is known earlier at Execute.")
    lines.append(
        "  The statistics may also count predictions made on speculative "
        "paths that an older misprediction later squashed.")
    return "\n".join(lines)


def cache_size_bytes(value):
    """Convert gem5 sizes such as 1kB or 32KiB to bytes."""
    match = re.fullmatch(r"\s*(\d+)\s*([kKmMgG]i?[bB]|[bB])?\s*", str(value))
    if not match:
        return 0
    amount = int(match.group(1))
    suffix = (match.group(2) or "B").lower()
    multiplier = 1
    if suffix.startswith("k"):
        multiplier = 1024
    elif suffix.startswith("m"):
        multiplier = 1024 ** 2
    elif suffix.startswith("g"):
        multiplier = 1024 ** 3
    return amount * multiplier


class CacheAccessTracker:
    """Small LRU mirror of gem5's two-way L1 caches for trace annotation."""

    def __init__(self, size, line_size, associativity=2):
        self.line_size = max(1, int(line_size))
        lines = max(1, cache_size_bytes(size) // self.line_size)
        self.associativity = max(1, min(associativity, lines))
        self.set_count = max(1, lines // self.associativity)
        self.sets = defaultdict(list)
        self.seen_lines = set()

    def access(self, address):
        line_number = int(address) // self.line_size
        set_index = line_number % self.set_count
        tag = line_number // self.set_count
        ways = self.sets[set_index]
        resident_before = sorted(
            (resident_tag * self.set_count + resident_set) * self.line_size
            for resident_set, resident_ways in self.sets.items()
            for resident_tag in resident_ways
        )
        hit = tag in ways
        if hit:
            ways.remove(tag)
            reason = "the cache line is already resident"
        elif len(ways) >= self.associativity:
            ways.pop(0)
            reason = ("cold miss (first access to this cache line)"
                      if line_number not in self.seen_lines
                      else "conflict/capacity miss (the line was evicted)")
        else:
            reason = ("cold miss (first access to this cache line)"
                      if line_number not in self.seen_lines
                      else "miss (the line is no longer resident)")
        ways.append(tag)
        self.seen_lines.add(line_number)
        return hit, line_number * self.line_size, reason, resident_before


def cache_symbol_label(address, symbols):
    """Use a compiled symbol (and vector index) when one owns an address."""
    for symbol in symbols:
        start = int(symbol["address"], 16)
        end = start + int(symbol.get("size", 0))
        if start <= address < end:
            offset = address - start
            element = int(symbol.get("elementSize", 4)) or 4
            if symbol.get("elementCount", 1) > 1 and offset % element == 0:
                return f"{symbol['name']}[{offset // element}]"
            return (symbol["name"] if not offset
                    else f"{symbol['name']}+0x{offset:x}")
    return f"0x{address:x}"


def add_cache_analysis(data, configuration, result_dir, symbols):
    """Attach cache misses to rows and produce a concise gem5 cache report.

    gem5's scalar statistics are authoritative for totals and latency.  The
    trace does not contain a per-address hit/miss flag, so row annotations use
    the configured two-way L1 geometry and the observed instruction/data
    addresses to mirror the same cold/conflict decisions.
    """
    if configuration.get("memoryMode") != "cache":
        data["cacheEvents"] = []
        data["cacheReport"] = ""
        return

    dynamic = sorted(
        data.get("dynamicInstructions", []),
        key=lambda row: min((int(cycle) for cycle in row.get("cycles", {})),
                            default=10 ** 12),
    )
    for row in dynamic:
        row["cacheEvents"] = []

    line_size = int(configuration["cacheLine"])
    instruction_cache = CacheAccessTracker(
        configuration["iCacheSize"], line_size)
    data_cache = CacheAccessTracker(configuration["dCacheSize"], line_size)
    events = []

    # Minor fetches one complete line and reuses it until control flow moves
    # to another line. Record only those real L1 requests, not every opcode.
    previous_fetch_line = None
    for row in dynamic:
        if not row.get("address") or not row.get("cycles"):
            continue
        address = int(row["address"], 16)
        fetch_line = address - address % line_size
        if fetch_line == previous_fetch_line:
            continue
        prior_fetch_line = previous_fetch_line
        previous_fetch_line = fetch_line
        hit, line_address, reason, resident_lines = instruction_cache.access(address)
        fetch_cycles = [int(cycle) for cycle, stage in row["cycles"].items()
                        if stage == "F"]
        event = {
            "cache": "I", "result": "hit" if hit else "miss",
            "access": "fetch", "address": f"0x{address:x}",
            "lineAddress": f"0x{line_address:x}",
            "label": f"0x{address:x}",
            "reason": reason,
            "previousLineAddress": (f"0x{prior_fetch_line:x}"
                                    if prior_fetch_line is not None else None),
            "residentLines": [f"0x{line:x}" for line in resident_lines],
            "cycle": min(fetch_cycles, default=0),
            "stageCells": len(fetch_cycles),
            "waitCells": sum(stage == "S" for stage in row["cycles"].values()),
        }
        row["cacheEvents"].append(event)
        events.append(event)

    # Exec supplies effective addresses and the instruction PC. Queue them by
    # PC so repeated loop instructions are matched to the correct occurrence.
    memory_by_pc = defaultdict(deque)
    for cycle, changes in sorted(data.get("memoryDeltas", {}).items(),
                                 key=lambda item: int(item[0])):
        for address, details in changes.items():
            memory_by_pc[details.get("pc", "").lower()].append(
                (int(cycle), int(address, 16), details))
    for row in dynamic:
        pc = "0x" + row.get("address", "").lower()
        if not memory_by_pc[pc]:
            continue
        _event_cycle, address, details = memory_by_pc[pc].popleft()
        hit, line_address, reason, resident_lines = data_cache.access(address)
        memory_cycles = [int(cycle) for cycle, stage in row["cycles"].items()
                         if stage == "M"]
        event = {
            "cache": "D", "result": "hit" if hit else "miss",
            "access": details.get("access", "access"),
            "address": f"0x{address:x}",
            "lineAddress": f"0x{line_address:x}",
            "label": cache_symbol_label(address, symbols),
            "reason": reason,
            "residentLines": [f"0x{line:x}" for line in resident_lines],
            "cycle": min(memory_cycles, default=_event_cycle),
            "stageCells": len(memory_cycles),
            "waitCells": 0,
        }
        row["cacheEvents"].append(event)
        events.append(event)

    statistics = gem5_statistics(result_dir / "stats.txt")
    clock_ticks = statistics.get("system.cpu_clk_domain.clock", 0)

    def cache_total(cache, metric):
        return int(statistics.get(
            f"system.cpu.{cache}.demand{metric}::total", 0))

    def cache_timing(cache):
        accesses = cache_total(cache, "Accesses")
        hits = cache_total(cache, "Hits")
        misses = cache_total(cache, "Misses")
        latency_ticks = statistics.get(
            f"system.cpu.{cache}.demandAvgMissLatency::total", 0)
        total_latency_ticks = statistics.get(
            f"system.cpu.{cache}.demandMissLatency::total", 0)
        latency = latency_ticks / clock_ticks if clock_ticks else 0
        return {
            "accesses": accesses,
            "hits": hits,
            "averageTicks": latency_ticks,
            "totalTicks": total_latency_ticks,
            "cycles": latency,
            "misses": misses,
        }

    cache_timings = {
        "I": cache_timing("icache"),
        "D": cache_timing("dcache"),
    }
    misses = sorted(
        (event for event in events if event["result"] == "miss"),
        key=lambda event: (event["cycle"], event["cache"]),
    )
    report = ["Cache miss summary:"]

    def line_range(line_address):
        start = int(line_address, 16)
        return f"0x{start:x}–0x{start + line_size - 1:x}"

    for event in misses:
        requested_range = line_range(event["lineAddress"])
        if event["cache"] == "I":
            previous = event.get("previousLineAddress")
            if previous is None:
                explanation = "no instruction line was resident yet"
            else:
                explanation = (
                    f"PC {event['address']} is outside the previous fetch line "
                    f"{line_range(previous)}")
            subject = f"instruction {event['address']}"
        else:
            resident = event.get("residentLines", [])
            if resident:
                ranges = ", ".join(line_range(line) for line in resident[-3:])
                if len(resident) > 3:
                    ranges = f"{ranges} (+{len(resident) - 3} more)"
                explanation = f"requested line was not among resident lines {ranges}"
            else:
                explanation = "no data line was resident yet"
            subject = f"{event['access']} {event['label']} at {event['address']}"
        occupancy = f"{event['stageCells']} {event['cache'] == 'I' and 'F' or 'M'}"
        if event.get("waitCells"):
            occupancy += f" + {event['waitCells']} S"
        miss_reason = ("first access to this line"
                       if event["reason"].startswith("cold miss")
                       else event["reason"])
        report.append(
            f"  cycle {event['cycle']}: {event['cache']}$ miss — {subject}; "
            f"{line_size}-byte line {requested_range}; {explanation}; "
            f"{miss_reason}; pipeline {occupancy}.")

    if clock_ticks and any(timing["misses"] for timing in cache_timings.values()):
        lookup = configuration["cacheLatency"]
        memory = configuration["memoryLatency"]
        response = configuration["cacheLatency"]
        reference = (cache_timings["I"]["cycles"]
                     or cache_timings["D"]["cycles"])
        transport = max(0, reference - lookup - memory - response)
        first_i_miss = next((event for event in misses
                             if event["cache"] == "I"
                             and event["result"] == "miss"), None)
        d_miss_cells = [event["stageCells"] for event in misses
                        if event["cache"] == "D"]
        timing_summary = [
            "",
            "Timing explanation:",
            (f"  • L1 lookup ({lookup} cycles): when the CPU sends a cache "
             "request, L1 checks the address tag to find the requested "
             f"{line_size}-byte line."),
            (f"  • Main memory ({memory} cycles): after a miss, the missing "
             "line is read from the backing memory."),
            (f"  • L1 response ({response} cycles): the returned line is "
             "installed in L1 and delivered to the CPU."),
            (f"  • gem5 transport ({transport:g} cycles here): the request and "
             "response cross the simulated interconnect and are aligned with "
             "CPU clock events."),
            (f"Therefore one uncontended miss needs {lookup} + {memory} + "
             f"{response} + {transport:g} = {reference:g} service cycles."),
        ]
        if first_i_miss:
            timing_summary.append(
                f"The first instruction shows {first_i_miss['stageCells']} F "
                f"cells: one F cell starts the request, followed by "
                f"{reference:g} cache-miss service cycles.")
        if d_miss_cells:
            shown = ", ".join(map(str, d_miss_cells))
            timing_summary.append(
                f"The D$ misses occupy {shown} M cells. The first M cell is "
                "the address-translation, LSQ, and D-cache request/handoff "
                f"cycle; the following cells show the cache service. Thus an "
                f"uncontended miss uses 1 request + {reference:g} service "
                "cycles.")
            timing_summary.append(
                f"An uncontended cache hit skips main memory and needs only "
                f"the {lookup}-cycle L1 lookup. A load hit therefore normally "
                f"appears as {lookup} M cells before W.")
            if max(d_miss_cells) > min(d_miss_cells):
                timing_summary.append(
                    "Lower-memory connection: I$ and D$ are separate L1 "
                    "caches, but both connect to the same interconnect and "
                    "backing memory below L1. If an instruction miss and a "
                    "data miss arrive together, the interconnect must "
                    "arbitrate between them; one request can wait an extra "
                    "cycle. That is why the final D$ miss has one more M cell.")
        report.extend(timing_summary)

    data["cacheEvents"] = events
    data["cacheReport"] = "\n".join(report)
    data["instructions"] = compact_iterations(dynamic)


def normalize_non_cache_memory_rows(data, configuration):
    """Replace timing-port plumbing with the selected architectural latency."""
    dynamic_rows = data.get("dynamicInstructions", [])

    def relocate_change(collection, old_cycle, new_cycle, predicate):
        old_key, new_key = str(old_cycle), str(new_cycle)
        changes = collection.get(old_key)
        if not changes:
            return
        moving = {key: value for key, value in changes.items()
                  if predicate(key, value)}
        for key in moving:
            changes.pop(key, None)
        if not changes:
            collection.pop(old_key, None)
        if moving:
            collection.setdefault(new_key, {}).update(moving)

    for row in dynamic_rows:
        opcode = row["instruction"].strip().split(None, 1)[0].lower()
        is_load = bool(re.fullmatch(r"(?:l[bhwdu]|fl[wd])", opcode))
        if is_load:
            latency = configuration["dataReadLatency"]
        elif re.fullmatch(r"(?:s[bhwd]|fs[wd])", opcode):
            latency = configuration["dataWriteLatency"]
        else:
            continue
        memory_cycles = sorted(int(cycle) for cycle, stage in row["cycles"].items()
                               if "M" in stage)
        writeback_cycles = sorted(int(cycle) for cycle, stage in row["cycles"].items()
                                  if "W" in stage)
        if not memory_cycles or not writeback_cycles:
            continue
        memory_cycle = memory_cycles[-1]
        writeback_cycle = next((cycle for cycle in writeback_cycles
                                if cycle > memory_cycle), None)
        target_cycle = memory_cycle + latency
        if writeback_cycle is None or writeback_cycle == target_cycle:
            continue
        for cycle in range(memory_cycle + 1,
                           max(writeback_cycle, target_cycle) + 1):
            if row["cycles"].get(str(cycle)) in {"S", "W"}:
                row["cycles"].pop(str(cycle), None)
        for cycle in range(memory_cycle + 1, target_cycle):
            row["cycles"][str(cycle)] = "S"
        row["cycles"][str(target_cycle)] = "W"
        destination = destination_register(row["instruction"]) if is_load else None
        if destination:
            relocate_change(
                data.get("registerDeltas", {}), writeback_cycle, target_cycle,
                lambda register, _value: register == destination)
        pc = "0x" + row["address"]
        relocate_change(
            data.get("memoryDeltas", {}), writeback_cycle, target_cycle,
            lambda _address, event: event.get("pc") == pc)

    if dynamic_rows:
        data["instructions"] = compact_iterations(dynamic_rows)


def normalize_non_cache_timeline(data, configuration):
    """Remove timing-port plumbing beyond the configured visible latency.

    MinorCPU uses timing requests even for zero-delay memory. Those request and
    response handshakes can freeze every active stage for a few clocks. They
    are implementation plumbing, not the architectural latency selected in
    Studio, so collapse only cycles containing stalls and no useful stage.
    Cache mode remains untouched because those cycles represent real hits and
    misses from gem5's cache hierarchy.
    """
    if (configuration["memoryMode"] == "cache" or not data["instructions"]
            or data.get("lectureTimingApplied")):
        return

    useful_cycle = {}
    for cycle in range(1, data.get("cycles", 0) + 1):
        stages = [row["cycles"].get(str(cycle), "") for row in data["instructions"]]
        useful_cycle[cycle] = any(stage and stage != "S" for stage in stages)

    removable = set()

    def stage_cycles(row, stage):
        return sorted(int(cycle) for cycle, value in row["cycles"].items()
                      if stage in value)

    def pairs(starts, ends):
        result, end_position = [], 0
        for start in starts:
            while end_position < len(ends) and ends[end_position] <= start:
                end_position += 1
            if end_position == len(ends):
                break
            result.append((start, ends[end_position]))
            end_position += 1
        return result

    def collapse_gap(start, end, visible_wait):
        gap = list(range(start + 1, end))
        excess = max(0, len(gap) - visible_wait)
        candidates = [cycle for cycle in gap if not useful_cycle.get(cycle, False)]
        removable.update(candidates[:excess])

    instruction_wait = configuration["instructionMemoryLatency"] - 1
    for row in data["instructions"]:
        for fetch, decode in pairs(stage_cycles(row, "F"), stage_cycles(row, "D")):
            collapse_gap(fetch, decode, instruction_wait)

    # Once a taken branch executes, retain only the configured instruction
    # response time before its target fetch. Direct-1 therefore redirects on
    # the next clock, while slower Direct memory keeps its selected wait.
    fetches_by_address = {
        row["address"]: stage_cycles(row, "F") for row in data["instructions"]
    }
    used_fetches = defaultdict(set)
    for jump in data.get("jumps", []):
        target_fetches = fetches_by_address.get(jump["toAddress"], [])
        target = next((cycle for cycle in target_fetches
                       if cycle > jump["cycle"]
                       and cycle not in used_fetches[jump["toAddress"]]), None)
        if target is None:
            continue
        used_fetches[jump["toAddress"]].add(target)
        collapse_gap(jump["cycle"], target, instruction_wait)

    if not removable:
        normalize_non_cache_memory_rows(data, configuration)
        return
    removed = sorted(removable)

    def new_cycle(cycle):
        return cycle - bisect_left(removed, cycle)

    for row_group in (data["instructions"], data.get("dynamicInstructions", [])):
        for row in row_group:
            normalized = {}
            for cycle_text, stage in row["cycles"].items():
                cycle = int(cycle_text)
                if cycle in removable and stage == "S":
                    continue
                target = str(new_cycle(cycle))
                existing = normalized.get(target, "")
                normalized[target] = existing + stage if stage not in existing else existing
            row["cycles"] = normalized

    for key in ("registerDeltas", "pcDeltas", "memoryDeltas"):
        normalized = {}
        for cycle_text, value in data.get(key, {}).items():
            target = str(new_cycle(int(cycle_text)))
            if isinstance(value, dict):
                normalized.setdefault(target, {}).update(value)
            else:
                normalized[target] = value
        data[key] = normalized
    for jump in data.get("jumps", []):
        jump["cycle"] = new_cycle(jump["cycle"])
    data["cycles"] = max(0, data.get("cycles", 0) - len(removed))
    normalize_non_cache_memory_rows(data, configuration)


def normalize_direct_in_order_control(data, configuration):
    """Present Direct-1 branches as the teaching five-stage pipeline.

    MinorCPU correctly reports its internal fetch buffers, but those buffers
    can keep a redirected target waiting long after its first fetch event.
    Direct memory with one-cycle accesses removes that implementation delay.
    A branch immediately
    consuming the preceding ALU result receives the one Decode wait used by
    Studio's classroom model, while its wrong-path fetch remains visible.
    """
    if (configuration["cpu"] != "in-order" or not configuration["forwarding"]
            or configuration["memoryMode"] != "direct"
            or any(configuration[key] != 1 for key in (
                "instructionMemoryLatency", "dataReadLatency", "dataWriteLatency"))):
        return
    dynamic = data.get("dynamicInstructions", [])
    if not dynamic:
        return

    def first_stage(row, stage):
        return min((int(cycle) for cycle, value in row["cycles"].items()
                    if stage in value), default=None)

    def shift_row(row, cutoff, amount, inclusive=False):
        shifted = {}
        for cycle_text, stage in row["cycles"].items():
            cycle = int(cycle_text)
            if cycle > cutoff or (inclusive and cycle == cutoff):
                cycle += amount
            key = str(cycle)
            existing = shifted.get(key, "")
            shifted[key] = existing + stage if stage not in existing else existing
        row["cycles"] = shifted

    def executed_rows():
        return sorted((row for row in dynamic if not row.get("squashed")),
                      key=lambda row: (first_stage(row, "D") or 10**12,
                                       first_stage(row, "F") or 10**12))

    original_timing = {
        id(row): {stage: first_stage(row, stage) for stage in ("M", "W")}
        for row in dynamic
    }

    # Collapse only the redirected instruction's artificial Fetch-to-Decode
    # wait, then move its younger instructions by the same amount.
    used_targets = set()
    used_branches = set()
    jump_branches = {}
    for jump in sorted(data.get("jumps", []), key=lambda item: item["cycle"]):
        candidates = [row for row in executed_rows()
                      if row["address"] == jump["fromAddress"]
                      and re.match(r"^b(?:eq|ne|lt|ge|ltu|geu)\b",
                                   row["instruction"], re.I)
                      and id(row) not in used_branches
                      and original_timing[id(row)]["W"] is not None]
        if not candidates:
            continue
        branch = min(candidates,
                     key=lambda row: abs(original_timing[id(row)]["W"]
                                         - jump["cycle"]))
        used_branches.add(id(branch))
        jump_branches[id(jump)] = branch
        branch_execute = first_stage(branch, "E")
        candidates = [row for row in executed_rows()
                      if row.get("address") == jump["toAddress"]
                      and id(row) not in used_targets
                      and (first_stage(row, "F") or 0) >= (branch_execute or 0)]
        target = min(candidates, key=lambda row: first_stage(row, "F") or 10**12,
                     default=None)
        if target is None:
            continue
        used_targets.add(id(target))
        fetch = first_stage(target, "F")
        decode = first_stage(target, "D")
        if fetch is None or decode is None or decode <= fetch + 1:
            continue
        amount = decode - fetch - 1
        ordered = executed_rows()
        target_index = ordered.index(target)
        target["cycles"] = {
            cycle: stage for cycle, stage in target["cycles"].items()
            if not (fetch < int(cycle) < decode and stage == "S")
        }
        shift_row(target, decode, -amount, inclusive=True)
        for row in ordered[target_index + 1:]:
            shift_row(row, fetch, -amount)
        for row in dynamic:
            if row.get("squashed") and (first_stage(row, "F") or 0) > fetch:
                shift_row(row, fetch, -amount)

    def branch_sources(instruction):
        match = re.match(r"^b(?:eq|ne|lt|ge|ltu|geu)\s+([^,]+),\s*([^,]+),",
                         display_instruction(instruction), re.I)
        if not match:
            return set()
        return {REGISTER_ALIASES.get(register.strip().lower(), register.strip().lower())
                for register in match.groups()}

    # This teaching model reads branch operands in Decode. If the immediately
    # preceding ALU instruction produces one of them, forwarding makes it
    # available after Execute, resulting in exactly one visible wait.
    ordered = executed_rows()
    for index, branch in enumerate(ordered):
        if index == 0 or not branch_sources(branch["instruction"]):
            continue
        producer = ordered[index - 1]
        destination = destination_register(producer["instruction"])
        if not destination or destination not in branch_sources(branch["instruction"]):
            continue
        fetch = first_stage(branch, "F")
        decode = first_stage(branch, "D")
        if fetch is None or decode is None or decode != fetch + 1:
            continue
        shift_row(branch, decode, 1, inclusive=True)
        branch["cycles"][str(decode)] = "S"
        for younger in ordered[index + 1:]:
            shift_row(younger, decode, 1)
        for row in dynamic:
            if row.get("squashed") and (first_stage(row, "F") or 0) > decode:
                shift_row(row, decode, 1)

    # Rebuild compact rows and fetch PCs from the normalized dynamic view.
    data["instructions"] = compact_iterations(dynamic)
    data["pcDeltas"] = {
        cycle: "0x" + row["address"]
        for row in sorted(dynamic, key=lambda item: first_stage(item, "F") or 10**12)
        for cycle, stage in row["cycles"].items() if stage == "F"
    }
    for jump in sorted(data.get("jumps", []), key=lambda item: item["cycle"]):
        branch = jump_branches.get(id(jump))
        if branch is not None:
            jump["cycle"] = first_stage(branch, "E") or jump["cycle"]

    # Keep cycle-by-cycle register and memory playback aligned with the rows
    # moved above. Read all values from the untouched map before relocating,
    # since the same register can be written in every loop iteration.
    original_registers = data.get("registerDeltas", {})
    relocated_registers = {cycle: values.copy()
                           for cycle, values in original_registers.items()}
    register_moves = []
    for row in dynamic:
        destination = destination_register(row["instruction"])
        old_cycle = original_timing[id(row)]["W"]
        new_cycle = first_stage(row, "W")
        if (not destination or old_cycle is None or new_cycle is None
                or old_cycle == new_cycle):
            continue
        value = original_registers.get(str(old_cycle), {}).get(destination)
        if value is not None:
            register_moves.append((str(old_cycle), str(new_cycle), destination, value))
    for old_cycle, _new_cycle, register, _value in register_moves:
        relocated_registers.get(old_cycle, {}).pop(register, None)
    for _old_cycle, new_cycle, register, value in register_moves:
        relocated_registers.setdefault(new_cycle, {})[register] = value
    data["registerDeltas"] = {
        cycle: values for cycle, values in relocated_registers.items() if values
    }

    original_memory = data.get("memoryDeltas", {})
    relocated_memory = {cycle: values.copy() for cycle, values in original_memory.items()}
    memory_moves = []
    for row in dynamic:
        old_cycle = original_timing[id(row)]["M"]
        new_cycle = first_stage(row, "M")
        if old_cycle is None or new_cycle is None or old_cycle == new_cycle:
            continue
        pc = "0x" + row["address"]
        for address, event in original_memory.get(str(old_cycle), {}).items():
            if event.get("pc") == pc:
                memory_moves.append((str(old_cycle), str(new_cycle), address, event))
    for old_cycle, _new_cycle, address, _event in memory_moves:
        relocated_memory.get(old_cycle, {}).pop(address, None)
    for _old_cycle, new_cycle, address, event in memory_moves:
        relocated_memory.setdefault(new_cycle, {})[address] = event
    data["memoryDeltas"] = {
        cycle: values for cycle, values in relocated_memory.items() if values
    }


def normalize_forwarded_dependencies(data, configuration):
    """Compact only redundant scoreboard waits in a Direct-1 trace.

    The original gem5 visualizer keeps every reported stage in place and
    renders every otherwise empty cycle between Fetch and Writeback as a
    stall.  Follow that rule here.  When Minor retains extra Decode waits even
    though forwarding has made an ALU/FP result available, remove only those
    wait cycles and shift the complete younger pipeline wave by the same
    amount.  Never rebuild individual rows or move Decode independently.
    """
    if (configuration["cpu"] != "in-order"
            or not configuration["forwarding"]
            or configuration["memoryMode"] != "direct"
            or any(configuration[key] != 1 for key in (
                "instructionMemoryLatency", "dataReadLatency", "dataWriteLatency"))):
        return
    dynamic = data.get("dynamicInstructions", [])
    if not dynamic:
        return

    def first_stage(row, stage):
        return min((int(cycle) for cycle, value in row["cycles"].items()
                    if stage in value), default=None)

    def writes_register(instruction):
        text = normalize_instruction(display_instruction(instruction))
        mnemonic = re.split(r"[,\s]", text, maxsplit=1)[0]
        if re.match(r"^(?:b(?:eq|ne|lt|ge|ltu|geu)|j|jr|ret|ecall)$", mnemonic):
            return None
        if re.match(r"^(?:s[bhwdq]|fs[wdq])$", mnemonic):
            return None
        return destination_register(instruction)

    def source_registers(instruction):
        # Keep the operand boundary before extracting registers.  The compact
        # normalized form joins the mnemonic suffix to its destination
        # (``fadd.sf5``), which would hide that first register.
        text = display_instruction(instruction).strip().lower()
        operands = text.split(None, 1)[1] if " " in text else ""
        operands = re.sub(r"\b(" + "|".join(REGISTER_ALIASES) + r")\b",
                          lambda match: REGISTER_ALIASES[match.group(1)], operands)
        registers = re.findall(r"\b[xf]\d+\b", operands)
        destination = writes_register(instruction)
        if destination in registers:
            registers.remove(destination)
        return set(registers)

    def is_control(instruction):
        return bool(re.match(
            r"^(?:b(?:eq|ne|lt|ge|ltu|geu)|j|jr|jal|jalr|ret)\b",
            display_instruction(instruction), re.I))

    def is_load(instruction):
        return bool(re.match(
            r"^(?:l(?:b|bu|h|hu|w|wu|d)|fl[wdq])\b",
            display_instruction(instruction), re.I))

    def is_store(instruction):
        return bool(re.match(r"^(?:s[bhwdq]|fs[wdq])\b",
                             display_instruction(instruction), re.I))

    def shifted_cycles(cycles, cutoff, amount):
        shifted = {}
        for cycle_text, stage in cycles.items():
            cycle = int(cycle_text)
            if cycle >= cutoff:
                cycle -= amount
            key = str(cycle)
            existing = shifted.get(key, "")
            if not existing or existing == stage:
                shifted[key] = stage
            else:
                # A valid compaction may merge two identical stall cells, but
                # it must never put two real stages in the same row/cycle.
                return None
        return shifted

    ordered = sorted((row for row in dynamic if not row.get("squashed")),
                     key=lambda row: (first_stage(row, "F") or 10**12,
                                      first_stage(row, "D") or 10**12))
    original_timing = {
        id(row): {stage: first_stage(row, stage) for stage in ("M", "W")}
        for row in dynamic
    }
    original_registers = data.get("registerDeltas", {})
    original_memory = data.get("memoryDeltas", {})
    latest_producer = {}

    for row_index, row in enumerate(ordered):
        consumer_execute = first_stage(row, "E")
        if (consumer_execute is not None
                and not is_control(row["instruction"])
                and not is_store(row["instruction"])):
            producer_candidates = [latest_producer[register]
                                   for register in source_registers(row["instruction"])
                                   if register in latest_producer]
            producer = max(
                producer_candidates,
                key=lambda candidate: first_stage(candidate, "F") or -1,
                default=None,
            )
            if producer is not None:
                producer_memory = first_stage(producer, "M")
                producer_execute = max(
                    (int(cycle) for cycle, stage in producer["cycles"].items()
                     if stage == "E"),
                    default=None,
                )
                forwarding_cycle = (
                    producer_memory + 1
                    if producer_memory is not None and is_load(producer["instruction"])
                    else producer_memory
                    if producer_memory is not None
                    else producer_execute + 1
                    if producer_execute is not None else None
                )
                decode = first_stage(row, "D")
                if forwarding_cycle is not None and decode is not None:
                    forwarding_cycle = max(forwarding_cycle, decode + 1)
                amount = (consumer_execute - forwarding_cycle
                          if forwarding_cycle is not None else 0)
                redundant_waits = (
                    amount > 0
                    and all(row["cycles"].get(str(cycle)) == "S"
                            for cycle in range(forwarding_cycle,
                                               consumer_execute))
                )
                if redundant_waits:
                    # Remove the consumer's redundant waits, move its E/M/W
                    # stages left, then move every younger instruction at the
                    # same cutoff.  This preserves the original visualizer's
                    # Decode/Fetch backpressure pattern.
                    consumer_cycles = {
                        cycle: stage for cycle, stage in row["cycles"].items()
                        if not (forwarding_cycle <= int(cycle) < consumer_execute
                                and stage == "S")
                    }
                    consumer_plan = shifted_cycles(
                        consumer_cycles, consumer_execute, amount
                    )
                    younger_plans = [
                        (younger, shifted_cycles(younger["cycles"],
                                                 forwarding_cycle, amount))
                        for younger in ordered[row_index + 1:]
                    ]
                    consumer_fetch = first_stage(row, "F") or -1
                    speculative_rows = [
                        speculative for speculative in dynamic
                        if (speculative.get("squashed")
                            and (first_stage(speculative, "F") or -1)
                            >= consumer_fetch)
                    ]
                    speculative_plans = [
                        (speculative,
                         shifted_cycles(speculative["cycles"],
                                        forwarding_cycle, amount))
                        for speculative in speculative_rows
                    ]
                    all_plans = younger_plans + speculative_plans
                    if (consumer_plan is not None
                            and all(plan is not None for _item, plan in all_plans)):
                        row["cycles"] = consumer_plan
                        for item, plan in all_plans:
                            item["cycles"] = plan

        destination = writes_register(row["instruction"])
        if destination:
            latest_producer[destination] = row

    data["instructions"] = compact_iterations(dynamic)
    data["pcDeltas"] = {
        cycle: "0x" + row["address"]
        for row in sorted(dynamic, key=lambda item: first_stage(item, "F") or 10**12)
        for cycle, stage in row["cycles"].items() if stage == "F"
    }

    # Keep register and memory playback on the writeback/memory cycle after a
    # dependent row is moved earlier.
    relocated_registers = {cycle: values.copy()
                           for cycle, values in original_registers.items()}
    register_moves = []
    for row in dynamic:
        destination = writes_register(row["instruction"])
        old_cycle = original_timing[id(row)]["W"]
        new_cycle = first_stage(row, "W")
        if destination and old_cycle is not None and new_cycle is not None and old_cycle != new_cycle:
            value = original_registers.get(str(old_cycle), {}).get(destination)
            if value is not None:
                register_moves.append((str(old_cycle), str(new_cycle), destination, value))
    for old_cycle, _new_cycle, register, _value in register_moves:
        relocated_registers.get(old_cycle, {}).pop(register, None)
    for _old_cycle, new_cycle, register, value in register_moves:
        relocated_registers.setdefault(new_cycle, {})[register] = value
    data["registerDeltas"] = {
        cycle: values for cycle, values in relocated_registers.items() if values
    }

    relocated_memory = {cycle: values.copy() for cycle, values in original_memory.items()}
    memory_moves = []
    for row in dynamic:
        old_cycle = original_timing[id(row)]["M"]
        new_cycle = first_stage(row, "M")
        if old_cycle is None or new_cycle is None or old_cycle == new_cycle:
            continue
        pc = "0x" + row["address"]
        for address, event in original_memory.get(str(old_cycle), {}).items():
            if event.get("pc") == pc:
                memory_moves.append((str(old_cycle), str(new_cycle), address, event))
    for old_cycle, _new_cycle, address, _event in memory_moves:
        relocated_memory.get(old_cycle, {}).pop(address, None)
    for _old_cycle, new_cycle, address, event in memory_moves:
        relocated_memory.setdefault(new_cycle, {})[address] = event
    data["memoryDeltas"] = {
        cycle: values for cycle, values in relocated_memory.items() if values
    }


def schedule_direct_in_order_pipeline(data, configuration):
    """Schedule the direct-memory teaching pipeline with a real scoreboard.

    MinorCPU exposes implementation-specific buffer waits in its trace.  For
    direct memory, Studio instead presents the five-stage pipeline used in
    class and applies the selected fixed instruction/read/write latencies:

    * instructions enter execution in program order, at most one per cycle;
    * an instruction remains in F for the configured instruction latency;
    * the integer/address, FP ALU, FP multiply, and FP divide units are
      independent;
    * a load/store remains in M for its configured data latency;
    * a memory instruction keeps the address unit until it reaches M;
    * each arithmetic unit observes its configured pipelined/non-pipelined
      issue interval;
    * forwarded operands become usable at the producer's M boundary (one
      cycle later for a load), and store data is needed only at M.

    This is deliberately a scheduler rather than a blanket "copy every stall
    to every younger row" pass.  An independent multiply can therefore run
    while an older store waits for its value, matching the reference pipeline
    diagrams used by the course.
    """
    if (configuration["cpu"] != "in-order"
            or configuration["memoryMode"] != "direct"):
        return False
    dynamic = data.get("dynamicInstructions", [])
    if not dynamic:
        return False

    def first_stage(row, stage):
        return min((int(cycle) for cycle, value in row.get("cycles", {}).items()
                    if stage in value), default=None)

    def instruction_info(instruction):
        text = display_instruction(instruction).strip().lower()
        parts = text.split(None, 1)
        opcode = parts[0] if parts else ""
        operands = parts[1] if len(parts) > 1 else ""
        operands = re.sub(
            r"\b(" + "|".join(map(re.escape, REGISTER_ALIASES)) + r")\b",
            lambda match: REGISTER_ALIASES[match.group(1)], operands,
        )
        registers = [register for register in
                     re.findall(r"\b[xf]\d+\b", operands)
                     if register != "x0"]
        is_load = bool(re.fullmatch(
            r"(?:l(?:b|bu|h|hu|w|wu|d)|fl[wdq])", opcode))
        is_store = bool(re.fullmatch(r"(?:s[bhwdq]|fs[wdq])", opcode))
        is_branch = bool(re.fullmatch(
            r"(?:b(?:eq|ne|lt|ge|ltu|geu)|j|jr|jal|jalr|ret)", opcode))

        if is_load or is_store:
            # Loads/stores calculate their address in the same integer ALU
            # used by ordinary integer instructions.  A waiting memory
            # operation therefore prevents a younger add/addi from entering
            # E until that operation advances to M.
            unit, latency = "integer-alu", configuration["intAlu"]
        elif re.match(r"^fmul", opcode):
            unit, latency = "float-multiply", configuration["floatMul"]
        elif re.match(r"^fdiv", opcode):
            unit, latency = "float-divide", configuration["floatDiv"]
        elif re.match(r"^f", opcode):
            unit, latency = "float-alu", configuration["floatAlu"]
        elif re.match(r"^mul", opcode):
            unit, latency = "integer-multiply", configuration["intMul"]
        elif re.match(r"^(?:div|rem)", opcode):
            unit, latency = "integer-divide", configuration["intDiv"]
        else:
            unit, latency = "integer-alu", configuration["intAlu"]

        pipelined_key = {
            "integer-alu": "intAluPipelined",
            "integer-multiply": "intMulPipelined",
            "integer-divide": "intDivPipelined",
            "float-alu": "floatAluPipelined",
            "float-multiply": "floatMulPipelined",
            "float-divide": "floatDivPipelined",
        }[unit]

        conditional_branch = opcode.startswith("b")
        no_destination = (is_store or conditional_branch
                          or opcode in {"j", "jr", "ret"})
        destination = (registers[0] if registers and not no_destination
                       else None)
        sources = (registers if no_destination else registers[1:])
        store_data = registers[0] if is_store and registers else None
        store_base = registers[1:] if is_store else sources
        return {
            "opcode": opcode,
            "unit": unit,
            "latency": max(1, int(latency)),
            "pipelined": bool(configuration[pipelined_key]),
            "destination": destination,
            "sources": sources,
            "storeData": store_data,
            "executeSources": store_base,
            "load": is_load,
            "store": is_store,
            "control": is_branch,
        }

    executed = sorted(
        (row for row in dynamic if not row.get("squashed")),
        key=lambda row: (first_stage(row, "F") or 10**12,
                         first_stage(row, "D") or 10**12),
    )
    if not executed:
        return False

    original_timing = {
        id(row): {stage: first_stage(row, stage)
                  for stage in ("F", "D", "E", "M", "W")}
        for row in dynamic
    }
    original_registers = data.get("registerDeltas", {})
    original_memory = data.get("memoryDeltas", {})

    # Associate each recorded taken transfer with its dynamic branch before
    # replacing any stage cycles.  This lets the redirected instruction fetch
    # on the branch's new Execute cycle on every loop iteration.
    taken_rows = set()
    jump_rows = {}
    unused_branches = set(map(id, executed))
    for jump in sorted(data.get("jumps", []), key=lambda item: item["cycle"]):
        candidates = [
            row for row in executed
            if row.get("address") == jump.get("fromAddress")
            and instruction_info(row.get("instruction", ""))["control"]
            and id(row) in unused_branches
        ]
        branch = min(
            candidates,
            key=lambda row: abs((original_timing[id(row)]["E"] or 10**12)
                                - jump["cycle"]),
            default=None,
        )
        if branch is not None:
            unused_branches.discard(id(branch))
            taken_rows.add(id(branch))
            jump_rows[id(jump)] = branch

    latest_producer = {}
    memory_stage_busy = set()
    unit_ready = defaultdict(lambda: -10**12)
    scheduled = {}
    previous = None
    previous_issue = -10**12
    forwarding = bool(configuration.get("forwarding"))
    out_of_order_execution = bool(
        configuration.get("outOfOrderExecution", True))

    def forwarded_ready(producer):
        # A load produces its value at the end of M; other functional units
        # may forward as they enter M.
        return producer["MEnd"] + (1 if producer["load"] else 0)

    for index, row in enumerate(executed):
        info = instruction_info(row["instruction"])
        if index == 0:
            fetch = original_timing[id(row)]["F"] or 1
        elif id(previous["row"]) in taken_rows:
            fetch = previous["E"]
        else:
            # Fetch may accept the next instruction in the cycle in which the
            # current one leaves Decode.
            fetch = previous["D"]

        decode = fetch + configuration["instructionMemoryLatency"]
        if previous is not None:
            decode = max(decode, previous["E"])

        # Decode happens before a RAW hazard is resolved.  The instruction is
        # already resident in D while it waits for the register file, so a
        # no-forwarding dependency must be drawn as D -> S ... -> E rather
        # than F -> S ... -> D.  Branch operands are the exception in the
        # forwarding model used by the course: the branch consumes them while
        # it is held in Decode.
        if forwarding and info["control"]:
            for register in info["sources"]:
                producer = latest_producer.get(register)
                if producer is not None:
                    decode = max(decode, forwarded_ready(producer))

        execute = max(decode + 1, previous_issue + 1,
                      unit_ready[info["unit"]])
        if not forwarding:
            # Register-file operands become usable after the producer's
            # Writeback cycle.  Keep the original Decode marker and express
            # this wait in the intervening S cells.
            for register in info["sources"]:
                producer = latest_producer.get(register)
                if producer is not None:
                    execute = max(execute, producer["W"] + 1)
        if previous is not None and not out_of_order_execution:
            # With dynamic execution disabled, a younger instruction cannot
            # begin until the older instruction has left its functional unit.
            execute = max(execute, previous["EEnd"] + 1)
        # With forwarding, normal ALU/address operands may wait after Decode
        # until the producer can supply them.  Store data is intentionally not
        # included: it is consumed by the store only when that row reaches M.
        if forwarding:
            for register in info["executeSources"]:
                producer = latest_producer.get(register)
                if producer is not None:
                    execute = max(execute, forwarded_ready(producer))

        execute_end = execute + info["latency"] - 1
        memory = execute_end + 1
        if info["store"]:
            producer = latest_producer.get(info["storeData"])
            if producer is not None:
                store_data_ready = (producer["W"] + 1 if not forwarding
                                    else forwarded_ready(producer))
                memory = max(memory, store_data_ready)
        if previous is not None and not out_of_order_execution:
            # Preserve program order at the shared memory-stage boundary too;
            # this prevents a short younger operation from passing a long one.
            memory = max(memory, previous["M"] + 1)
        if info["load"]:
            memory_latency = configuration["dataReadLatency"]
        elif info["store"]:
            memory_latency = configuration["dataWriteLatency"]
        else:
            memory_latency = 1
        # Direct memory is a single non-pipelined M stage.  A load/store owns
        # it for its complete configured latency, and even an ordinary
        # instruction cannot pass through M until that interval has ended.
        while any(cycle in memory_stage_busy
                  for cycle in range(memory, memory + memory_latency)):
            memory += 1
        memory_end = memory + memory_latency - 1
        writeback = memory_end + 1
        if previous is not None and not out_of_order_execution:
            writeback = max(writeback, previous["W"] + 1)
        memory_stage_busy.update(range(memory, memory_end + 1))

        record = {
            **info,
            "row": row,
            "F": fetch,
            "D": decode,
            "E": execute,
            "EEnd": execute_end,
            "M": memory,
            "MEnd": memory_end,
            "W": writeback,
        }
        scheduled[id(row)] = record
        if info["destination"]:
            latest_producer[info["destination"]] = record

        # Memory operations keep the shared integer/address path until their
        # M transfer. A non-pipelined arithmetic unit accepts its next issue
        # only after the current operation has completed all E cycles.
        if info["load"] or info["store"]:
            unit_ready["integer-alu"] = memory
        elif not info["pipelined"]:
            unit_ready[info["unit"]] = execute + info["latency"]
        else:
            unit_ready[info["unit"]] = execute + 1
        previous_issue = execute
        previous = record

    for row in executed:
        timing = scheduled[id(row)]
        cycles = {str(cycle): "S"
                  for cycle in range(timing["F"], timing["W"] + 1)}
        fetch_end = (timing["F"]
                     + configuration["instructionMemoryLatency"] - 1)
        for cycle in range(timing["F"], fetch_end + 1):
            cycles[str(cycle)] = "F"
        cycles[str(timing["D"])] = "D"
        for cycle in range(timing["E"], timing["EEnd"] + 1):
            cycles[str(cycle)] = "E"
        for cycle in range(timing["M"], timing["MEnd"] + 1):
            cycles[str(cycle)] = "M"
        cycles[str(timing["W"])] = "W"
        row["cycles"] = cycles

    # Put each wrong-path fetch under the Decode of its associated taken
    # branch. It remains a single F marker and never executes.
    unused_squashed = set(id(row) for row in dynamic if row.get("squashed"))
    for jump in sorted(data.get("jumps", []), key=lambda item: item["cycle"]):
        branch = jump_rows.get(id(jump))
        if branch is None:
            continue
        branch_timing = scheduled[id(branch)]
        expected_address = f"{int(branch['address'], 16) + 4:x}"
        candidates = [
            row for row in dynamic
            if row.get("squashed") and id(row) in unused_squashed
            and row.get("address") == expected_address
        ]
        squashed = min(
            candidates,
            key=lambda row: abs((original_timing[id(row)]["F"] or 10**12)
                                - (original_timing[id(branch)]["D"] or 0)),
            default=None,
        )
        if squashed is not None:
            unused_squashed.discard(id(squashed))
            squashed["cycles"] = {str(branch_timing["D"]): "F"}
        jump["cycle"] = branch_timing["E"]

    data["instructions"] = compact_iterations(dynamic)
    data["pcDeltas"] = {
        cycle: "0x" + row["address"]
        for row in sorted(dynamic, key=lambda item: first_stage(item, "F") or 10**12)
        for cycle, stage in row["cycles"].items() if stage == "F"
    }

    # Register and memory playback must follow the rescheduled W/M stage of
    # each dynamic instruction.
    relocated_registers = {cycle: values.copy()
                           for cycle, values in original_registers.items()}
    register_moves = []
    for row in executed:
        destination = scheduled[id(row)]["destination"]
        old_cycle = original_timing[id(row)]["W"]
        new_cycle = scheduled[id(row)]["W"]
        if not destination or old_cycle is None or old_cycle == new_cycle:
            continue
        value = original_registers.get(str(old_cycle), {}).get(destination)
        if value is not None:
            register_moves.append((str(old_cycle), str(new_cycle),
                                   destination, value))
    for old_cycle, _new_cycle, register, _value in register_moves:
        relocated_registers.get(old_cycle, {}).pop(register, None)
    for _old_cycle, new_cycle, register, value in register_moves:
        relocated_registers.setdefault(new_cycle, {})[register] = value
    data["registerDeltas"] = {
        cycle: values for cycle, values in relocated_registers.items() if values
    }

    # Exec memory events occur when gem5 completes an access, which need not
    # equal the first raw MinorGUI M event. Associate them by dynamic PC and
    # program order, then place each access at the rescheduled M completion.
    # This also prevents late events from being trimmed as if they belonged
    # beyond the visible teaching pipeline.
    memory_events = defaultdict(deque)
    for _cycle, changes in sorted(original_memory.items(),
                                  key=lambda item: int(item[0])):
        for address, event in changes.items():
            memory_events[event.get("pc", "").lower()].append((address, event))
    relocated_memory = defaultdict(dict)
    for row in executed:
        timing = scheduled[id(row)]
        if not (timing["load"] or timing["store"]):
            continue
        events = memory_events["0x" + row["address"].lower()]
        if not events:
            continue
        address, event = events.popleft()
        relocated_memory[str(timing["MEnd"])][address] = event
    data["memoryDeltas"] = dict(relocated_memory)
    return True


def normalize_in_order_taken_branches(data, configuration, dump_text="",
                                      source_body=""):
    """Show one sequential wrong-path fetch for every taken branch.

    Minor sometimes resolves a conditional branch before its sequential fetch
    is emitted by MinorGUI. Unconditional jumps normally do emit that row,
    which previously made otherwise equivalent redirects look inconsistent.
    Reuse gem5's squashed row when one exists; otherwise recover the static
    instruction from the disassembly and synthesize the single Fetch marker
    used by the teaching pipeline.
    """
    if configuration["cpu"] != "in-order":
        return
    dynamic = data.get("dynamicInstructions", [])
    if not dynamic:
        return

    def first_stage(row, stage):
        return min((int(cycle) for cycle, value in row["cycles"].items()
                    if stage in value), default=None)

    def is_control(instruction):
        return bool(re.match(
            r"^(?:b(?:eq|ne|lt|ge|ltu|geu)|j|jr|jal|jalr|ret)\b",
            display_instruction(instruction), re.I))

    def numeric_registers(instruction):
        return re.sub(
            r"\b(" + "|".join(map(re.escape, REGISTER_ALIASES)) + r")\b",
            lambda match: REGISTER_ALIASES[match.group(1).lower()],
            display_instruction(instruction), flags=re.I,
        )

    static_instructions = {}
    end_match = re.search(r"^\s*([0-9a-fA-F]+)\s+<End>:\s*$",
                          dump_text, re.M | re.I)
    end_address = int(end_match.group(1), 16) if end_match else None
    for line in dump_text.splitlines():
        match = re.match(
            r"^\s*([0-9a-fA-F]+):\s+[0-9a-fA-F]+\s+(.+?)\s*$", line)
        if not match:
            continue
        address, instruction = match.groups()
        if end_address is not None and int(address, 16) >= end_address:
            continue
        instruction = instruction.split("#", 1)[0].strip()
        instruction = re.sub(r"\s+<[^>]+>\s*$", "", instruction)
        static_instructions[address.lower()] = numeric_registers(instruction)

    # A trace can provide the same static instruction from another loop
    # iteration even when objdump is unavailable, as in frozen fixtures.
    for row in dynamic:
        if row.get("address") and not row.get("squashed"):
            static_instructions.setdefault(row["address"].lower(),
                                           row["instruction"])
    static_addresses = sorted(int(address, 16)
                              for address in static_instructions)

    executed_controls = [
        row for row in dynamic
        if not row.get("squashed") and row.get("address")
        and is_control(row.get("instruction", ""))
    ]
    unused_controls = set(map(id, executed_controls))
    unused_squashed = {
        id(row) for row in dynamic
        if row.get("squashed") and row.get("address")
    }
    additions = defaultdict(list)

    # Keep one speculative Fetch event to make each redirect visible. It is
    # aligned with the branch Decode cycle and never advances to Decode.
    for jump in sorted(data.get("jumps", []), key=lambda item: item["cycle"]):
        candidates = [
            row for row in executed_controls
            if id(row) in unused_controls
            and row["address"].lower() == jump.get("fromAddress", "").lower()
        ]
        branch = min(
            candidates,
            key=lambda row: abs((first_stage(row, "E") or 10**12)
                                - jump["cycle"]),
            default=None,
        )
        if branch is None:
            continue
        unused_controls.discard(id(branch))
        decode = first_stage(branch, "D")
        branch_address = int(branch["address"], 16)
        sequential_address = next(
            (address for address in static_addresses
             if address > branch_address), None)
        if decode is None or sequential_address is None:
            continue
        expected_address = f"{sequential_address:x}"
        candidates = [
            row for row in dynamic
            if id(row) in unused_squashed
            and row.get("address", "").lower() == expected_address
        ]
        squashed = min(
            candidates,
            key=lambda row: abs((first_stage(row, "F") or 10**12) - decode),
            default=None,
        )
        if squashed is None:
            squashed = {
                "instruction": static_instructions[expected_address],
                "address": expected_address,
                "cycles": {},
                "squashed": True,
                "iterations": 0,
            }
            additions[id(branch)].append(squashed)
        else:
            unused_squashed.discard(id(squashed))
        squashed["cycles"] = {str(decode): "F"}

    if additions:
        expanded = []
        for row in dynamic:
            expanded.append(row)
            expanded.extend(additions.get(id(row), []))
        dynamic[:] = expanded

    # The fetch map is used by the PC monitor, so keep it consistent with the
    # adjusted row rather than leaving the old speculative cycle.
    data["instructions"] = compact_iterations(dynamic)
    if source_body:
        add_source_lines(data["instructions"], source_body)
        attach_dynamic_source_lines(dynamic, data["instructions"])
    data["pcDeltas"] = {
        cycle: "0x" + row["address"]
        for row in sorted(dynamic,
                          key=lambda item: first_stage(item, "F") or 10**12)
        for cycle, stage in row["cycles"].items() if stage == "F"
    }


def fill_in_order_stall_gaps(data, configuration):
    """Fill in-order holds and identify otherwise-unlabelled memory requests."""
    if configuration["cpu"] != "in-order":
        return
    dynamic = data.get("dynamicInstructions", [])
    for row in dynamic:
        if row.get("squashed"):
            continue
        occupied = sorted(int(cycle) for cycle in row.get("cycles", {}))
        if not occupied:
            continue
        opcode = row["instruction"].strip().split(None, 1)[0].lower()
        memory_access = bool(re.fullmatch(
            r"(?:l(?:b|bu|h|hu|w|wu|d)|fl[wdq]|s[bhwdq]|fs[wdq])", opcode))
        first_memory = min(
            (int(cycle) for cycle, stage in row["cycles"].items()
             if stage == "M"),
            default=None,
        )
        execute_before_memory = max(
            (int(cycle) for cycle, stage in row["cycles"].items()
             if stage == "E"
             and (first_memory is None or int(cycle) < first_memory)),
            default=None,
        )
        for cycle in range(occupied[0], occupied[-1] + 1):
            if (configuration.get("memoryMode") == "cache"
                    and memory_access and execute_before_memory is not None
                    and first_memory is not None
                    and execute_before_memory < cycle < first_memory
                    and cycle in row.get("inferredGaps", [])):
                row["cycles"][str(cycle)] = "M"
                continue
            row["cycles"].setdefault(str(cycle), "S")
    data["instructions"] = compact_iterations(dynamic)


def enforce_five_stage_in_order_completion(data, configuration):
    """Prevent younger cache-mode instructions from passing older work.

    Direct memory is scheduled explicitly above. Cache traces retain gem5's
    measured F/M service durations, so this pass moves only E/M/W boundaries
    when dynamic execution is disabled. The cache hit/miss latency itself is
    preserved.
    """
    if (configuration["cpu"] != "in-order"
            or configuration.get("outOfOrderExecution", True)
            or configuration["memoryMode"] != "cache"):
        return

    def stage_cycles(row, marker):
        return sorted(int(cycle) for cycle, value in row.get("cycles", {}).items()
                      if marker in value)

    rows = sorted(
        (row for row in data.get("dynamicInstructions", [])
         if not row.get("squashed") and stage_cycles(row, "E")),
        key=lambda row: min(
            stage_cycles(row, "F") or stage_cycles(row, "D"),
            default=10**12),
    )
    if not rows:
        return

    original_registers = data.get("registerDeltas", {})
    original_memory = data.get("memoryDeltas", {})
    moved_registers = {cycle: values.copy()
                       for cycle, values in original_registers.items()}
    moved_memory = {cycle: values.copy()
                    for cycle, values in original_memory.items()}
    timing = {}
    previous = None

    for row in rows:
        execute_cells = stage_cycles(row, "E")
        memory_cells = stage_cycles(row, "M")
        writeback_cells = stage_cycles(row, "W")
        execute_duration = max(1, len(execute_cells))
        memory_duration = max(1, len(memory_cells))
        execute = execute_cells[0]
        if previous is not None:
            execute = max(execute, previous["EEnd"] + 1)
        execute_end = execute + execute_duration - 1
        memory = max(memory_cells[0] if memory_cells else execute_end + 1,
                     execute_end + 1)
        if previous is not None:
            memory = max(memory, previous["MEnd"] + 1)
        memory_end = memory + memory_duration - 1
        writeback = max(writeback_cells[0] if writeback_cells else memory_end + 1,
                        memory_end + 1)
        if previous is not None:
            writeback = max(writeback, previous["W"] + 1)

        first = min(int(cycle) for cycle in row["cycles"])
        prefix = {
            cycle: value for cycle, value in row["cycles"].items()
            if int(cycle) < execute_cells[0]
        }
        cycles = {str(cycle): "S" for cycle in range(first, writeback + 1)}
        cycles.update(prefix)
        for cycle in range(execute, execute_end + 1):
            cycles[str(cycle)] = "E"
        for cycle in range(memory, memory_end + 1):
            cycles[str(cycle)] = "M"
        cycles[str(writeback)] = "W"
        row["cycles"] = cycles

        old_writeback = writeback_cells[0] if writeback_cells else None
        destination = destination_register(row.get("instruction", ""))
        if (destination and destination != "x0" and old_writeback is not None
                and old_writeback != writeback):
            value = moved_registers.get(str(old_writeback), {}).pop(
                destination, None)
            if value is not None:
                moved_registers.setdefault(str(writeback), {})[destination] = value

        pc = "0x" + row.get("address", "")
        memory_event = None
        memory_event_cycle = None
        for old_cycle in memory_cells:
            for address, event in list(moved_memory.get(str(old_cycle), {}).items()):
                if event.get("pc") == pc:
                    memory_event = (address, event)
                    memory_event_cycle = old_cycle
                    break
            if memory_event:
                break
        if memory_event and memory_event_cycle != memory:
            address, event = memory_event
            moved_memory[str(memory_event_cycle)].pop(address, None)
            moved_memory.setdefault(str(memory), {})[address] = event

        timing[id(row)] = {
            "oldE": execute_cells[0], "E": execute, "EEnd": execute_end,
            "M": memory, "MEnd": memory_end, "W": writeback,
        }
        previous = timing[id(row)]

    branch_rows = defaultdict(deque)
    for row in rows:
        opcode = display_instruction(row.get("instruction", "")).split(None, 1)[0]
        if re.fullmatch(r"(?:b(?:eq|ne|lt|ge|ltu|geu)|j|jr|jal|jalr|ret)", opcode):
            branch_rows[row.get("address")].append(row)
    for jump in sorted(data.get("jumps", []), key=lambda item: item["cycle"]):
        candidates = branch_rows[jump.get("fromAddress")]
        if candidates:
            row = min(candidates,
                      key=lambda candidate: abs(
                          timing[id(candidate)]["oldE"] - jump["cycle"]))
            candidates.remove(row)
            jump["cycle"] = timing[id(row)]["E"]

    data["registerDeltas"] = {
        cycle: values for cycle, values in moved_registers.items() if values
    }
    data["memoryDeltas"] = {
        cycle: values for cycle, values in moved_memory.items() if values
    }
    data["instructions"] = compact_iterations(data["dynamicInstructions"])


def pipeline(name):
    folder = project_dir(name)
    source = clean_source(source_file(folder).read_text())
    result_dir = project_results_directory(name)
    minor, o3 = result_dir / "gem5_inorder.log", result_dir / "trace.out"
    configuration = project_config(folder)
    configured_cpu = configuration["cpu"]
    selected_trace = minor if configured_cpu == "in-order" else o3
    show_fetch_stalls = (configuration["memoryMode"] == "cache"
                         or (configuration["memoryMode"] == "direct"
                             and configuration["instructionMemoryLatency"] > 1))
    data = (parse_minor(selected_trace, source,
                        include_fetch_stalls=show_fetch_stalls)
            if configured_cpu == "in-order" and selected_trace.exists()
            else parse_o3(selected_trace, source) if selected_trace.exists() else None)
    if data is None:
        fail("No trace has been generated for this project.", 404)
    dump = folder / f"{artifact_stem(folder)}.dump"
    end_address = None
    start_address = None
    dump_text = ""
    if dump.exists():
        dump_text = dump.read_text(errors="replace")
        match = re.search(r"^\s*([0-9a-fA-F]+)\s+<End>:\s*$",
                          dump_text, re.M | re.I)
        if match:
            end_address = int(match.group(1), 16)
        match = re.search(r"^\s*([0-9a-fA-F]+)\s+<_start>:\s*$",
                          dump_text, re.M | re.I)
        if match:
            start_address = int(match.group(1), 16)
    evaluation_start_address = None
    evaluation_start_label = configuration.get("o3EvaluationLabel", "")
    if configured_cpu == "out-of-order" and evaluation_start_label:
        label_match = re.search(
            rf"^\s*([0-9a-fA-F]+)\s+<{re.escape(evaluation_start_label)}>:\s*$",
            dump_text, re.M,
        )
        if not label_match:
            fail(f"Evaluation start label '{evaluation_start_label}' was not found in the compiled program.")
        evaluation_start_address = int(label_match.group(1), 16)
    if end_address is None:
        # If End immediately follows _start (an empty project), both labels
        # have the same address and objdump prints only <_start>. Source-line
        # mapping still identifies the first exit instruction reliably.
        end_line = next((index for index, line in enumerate(source.splitlines(), 1)
                         if re.match(r"^\s*End:\s*(?:#.*)?$", line, re.I)), None)
        exit_addresses = [
            int(row["address"], 16)
            for row in data.get("instructions", [])
            if end_line is not None and row.get("address")
            and row.get("sourceLine") is not None
            and row["sourceLine"] >= end_line
        ]
        if exit_addresses:
            end_address = min(exit_addresses)
    if end_address is not None:
        # Keep speculative visits to the exit block to illustrate a bad
        # prediction; the final committed exit sequence stays outside the
        # teaching stream and must not supply register or memory updates.
        for row_key in ("instructions", "dynamicInstructions"):
            data[row_key] = [row for row in data.get(row_key, [])
                             if row.get("address")
                             and (int(row["address"], 16) < end_address
                                  or (configured_cpu == "out-of-order"
                                      and row.get("squashed")))]
        data["pcDeltas"] = {
            cycle: address for cycle, address in data.get("pcDeltas", {}).items()
            if int(address, 16) < end_address
        }
    schedule_lecture_o3_pipeline(data, configuration, evaluation_start_address)
    normalize_non_cache_timeline(data, configuration)
    if not schedule_direct_in_order_pipeline(data, configuration):
        normalize_direct_in_order_control(data, configuration)
        normalize_forwarded_dependencies(data, configuration)
    normalize_in_order_taken_branches(data, configuration, dump_text, source)
    fill_in_order_stall_gaps(data, configuration)
    enforce_five_stage_in_order_completion(data, configuration)
    configure_o3_stage_display(data, configuration)
    visible_cycles = [int(cycle) for row in data["instructions"]
                      for cycle in row["cycles"]]
    data["cycles"] = max(visible_cycles, default=0)
    visible_addresses = {row["address"] for row in data["instructions"]}
    data["codeBytes"] = (
        end_address - start_address
        if (end_address is not None and start_address is not None
            and end_address >= start_address)
        else len(visible_addresses) * 4
    )
    data["jumps"] = [jump for jump in data.get("jumps", [])
                     if jump["fromAddress"] in visible_addresses
                     and jump["toAddress"] in visible_addresses
                     and jump["cycle"] <= data["cycles"]]
    for key in ("registerDeltas", "pcDeltas", "memoryDeltas"):
        data[key] = {cycle: value for cycle, value in data.get(key, {}).items()
                     if int(cycle) <= data["cycles"]}
    data["configuration"] = configuration
    data["dataSymbols"] = data_symbols(folder)
    data["initialMemory"] = elf_initial_memory(folder, data["dataSymbols"])
    data["memoryMap"] = elf_memory_map(folder)
    add_cache_analysis(data, configuration, result_dir, data["dataSymbols"])
    data["branchReport"] = branch_predictor_report(
        result_dir, configuration, data["cycles"])
    return data


def pipeline_statistics(data):
    rows = [row for row in (data.get("dynamicInstructions") or data.get("instructions", []))
            if not row.get("squashed")]
    instruction_count = len(rows)
    stalls = sum(stage == "S" for row in rows
                 for stage in row.get("cycles", {}).values())
    cycles = data.get("cycles", 0)
    return {
        "cycles": cycles,
        "instructions": instruction_count,
        "codeBytes": data.get("codeBytes", 0),
        "stalls": stalls,
        "cpi": round(cycles / instruction_count, 3) if instruction_count else 0,
    }


def write_pipeline_csv(data, output, expand_loops=False):
    """Stream a pipeline table so large traces do not require a second copy."""
    rows = (data.get("dynamicInstructions") or data["instructions"]
            if expand_loops else data["instructions"])
    writer = csv.writer(output)
    statistics = pipeline_statistics(data)
    writer.writerow(["Pipeline summary", "Value"])
    writer.writerow(["Total cycles", statistics["cycles"]])
    writer.writerow(["Executed instructions", statistics["instructions"]])
    writer.writerow(["Pipeline code size (bytes)", statistics["codeBytes"]])
    writer.writerow(["Stalls", statistics["stalls"]])
    writer.writerow(["CPI", statistics["cpi"]])
    writer.writerow([])
    writer.writerow(["PC Address", "Instruction", "Control flow / cache",
                     *[f"Cycle {cycle}" for cycle in range(1, data["cycles"] + 1)]])
    for row in rows:
        row_cycles = [int(cycle) for cycle in row["cycles"]]
        first_cycle = min(row_cycles, default=0)
        last_cycle = max(row_cycles, default=0)
        jumps = [jump for jump in data.get("jumps", [])
                 if jump["fromAddress"] == row.get("address")
                 and (not expand_loops or (
                     jump["instructionSequence"] == row["sequence"]
                     if jump.get("instructionSequence") is not None
                     and row.get("sequence") is not None
                     else first_cycle <= jump["cycle"] <= last_cycle))]
        targets = defaultdict(int)
        for jump in jumps:
            targets[jump["toAddress"]] += 1
        flow_parts = [
            f"0x{row['address']} -> 0x{target}{f' x{count}' if count > 1 else ''}"
            for target, count in targets.items()
        ]
        cache_events = [event for event in row.get("cacheEvents", [])
                        if int(event.get("cycle", 0)) <= data["cycles"]]
        cache_groups = defaultdict(int)
        for event in cache_events:
            cache_groups[(event.get("cache"), event.get("result"))] += 1
        flow_parts.extend(
            f"{cache}$ {result}{f' x{count}' if count > 1 else ''}"
            for (cache, result), count in cache_groups.items()
        )
        prediction_misses = row.get("mispredictions", 0) or int(
            bool(row.get("mispredicted")))
        if prediction_misses:
            flow_parts.append(
                f"BP miss{f' x{prediction_misses}' if prediction_misses > 1 else ''}")
        if data.get("format") == "o3" and row.get("squashed"):
            flow_parts.append("Squashed")
        flow = "; ".join(flow_parts)
        stage_labels = {"C": "CDB", "W": "C"} if data.get("format") == "o3" else {}
        cycle_cells = []
        for cycle in range(1, data["cycles"] + 1):
            stage = row["cycles"].get(str(cycle), "")
            if stage_labels and stage:
                stage = "/".join(
                    stage_labels.get(token[0], token[0]) + token[1:]
                    for token in re.findall(r"[A-Z]\*?", stage))
            cycle_cells.append(stage)
        writer.writerow([f"0x{row['address']}", row["instruction"], flow,
                         *cycle_cells])


def pipeline_csv(data, expand_loops=False):
    """Return a pipeline CSV string for downloads and submission archives."""
    output = io.StringIO(newline="")
    write_pipeline_csv(data, output, expand_loops)
    return output.getvalue()


def pipeline_display_cycle_limit():
    """Return the validated UI limit, falling back safely for old settings."""
    raw = environment_overrides().get(
        "PIPELINE_DISPLAY_CYCLE_LIMIT",
        setup_environment().get("PIPELINE_DISPLAY_CYCLE_LIMIT",
                                str(PIPELINE_DISPLAY_CYCLE_LIMIT)),
    )
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return PIPELINE_DISPLAY_CYCLE_LIMIT
    return limit if 100 <= limit <= 20000 else PIPELINE_DISPLAY_CYCLE_LIMIT


def pipeline_for_display(name):
    """Return a browser-safe trace or save oversized traces directly to CSV."""
    data = pipeline(name)
    display_limit = pipeline_display_cycle_limit()
    if data["cycles"] <= display_limit:
        return data
    result_dir = project_results_directory(name)
    result_dir.mkdir(parents=True, exist_ok=True)
    destination = (result_dir / "pipeline-full.csv").resolve()
    with destination.open("w", encoding="utf-8", newline="") as output:
        write_pipeline_csv(data, output)
    return {
        "tooLarge": True,
        "limit": display_limit,
        "csvPath": str(destination),
        "cacheReport": data.get("cacheReport", ""),
        "branchReport": data.get("branchReport", ""),
        "dataSymbols": data.get("dataSymbols", []),
        "initialMemory": data.get("initialMemory", {}),
        "memoryMap": data.get("memoryMap", []),
        **pipeline_statistics(data),
    }


def submission_archive_stem(assignment):
    values = setup_environment()
    values.update(environment_overrides())
    prefix = values.get("SUBMISSION_NAME_PREFIX", "YOUR_NAME_").strip()
    suffix = values.get("SUBMISSION_NAME_SUFFIX", "").strip()
    for part in (prefix, suffix):
        if not re.fullmatch(r"[A-Za-z0-9._-]*", part):
            fail("Submission name parts contain unsupported filename characters.")
    stem = f"{prefix}{assignment}{suffix}"
    if not stem or len(os.fsencode(stem)) > 200:
        fail("The structured submission filename is too long.")
    return stem


def submission_archive_component(name, index):
    """Return a portable, collision-resistant directory name for a ZIP."""
    safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return f"{index:02d}-{safe_name or 'project'}"


def decode_submission_attachments(attachments):
    """Validate and decode browser-provided files before creating the ZIP."""
    if attachments is None:
        return []
    if not isinstance(attachments, list):
        fail("Invalid submission attachments.")
    if len(attachments) > MAX_SUBMISSION_ATTACHMENTS:
        fail(f"Choose no more than {MAX_SUBMISSION_ATTACHMENTS} additional files.")
    decoded = []
    seen_names = set()
    total_size = 0
    for attachment in attachments:
        if not isinstance(attachment, dict):
            fail("Invalid submission attachment.")
        name = attachment.get("name")
        if (not isinstance(name, str) or not name or name in {".", ".."}
                or "/" in name or "\\" in name or "\0" in name
                or any(ord(character) < 32 or ord(character) == 127
                       for character in name)):
            fail("Additional filenames must be plain filenames without folders.")
        if len(os.fsencode(name)) > 255:
            fail(f"The additional filename '{name}' is too long.")
        folded_name = name.casefold()
        if folded_name in seen_names:
            fail(f"The additional filename '{name}' is duplicated.")
        seen_names.add(folded_name)
        content = attachment.get("contentBase64")
        if not isinstance(content, str):
            fail(f"The additional file '{name}' has invalid content.")
        try:
            file_bytes = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError, UnicodeEncodeError):
            fail(f"The additional file '{name}' could not be decoded.")
        if len(file_bytes) > MAX_SUBMISSION_ATTACHMENT_BYTES:
            fail(f"The additional file '{name}' exceeds the 25 MiB limit.")
        total_size += len(file_bytes)
        if total_size > MAX_SUBMISSION_TOTAL_ATTACHMENT_BYTES:
            fail("Additional files exceed the combined 100 MiB limit.")
        mime_type = attachment.get("mimeType") or "application/octet-stream"
        if not isinstance(mime_type, str) or len(mime_type) > 255:
            mime_type = "application/octet-stream"
        decoded.append({"name": name, "bytes": file_bytes,
                        "mimeType": mime_type})
    return decoded


def create_submission(projects, assignment, attachments=None, expand_loops=False):
    """Build, simulate, and package selected projects and supporting files."""
    if not isinstance(assignment, str) or not ASSIGNMENT_NAME.fullmatch(assignment):
        fail("Assignment names may contain letters, digits, '_' and '-'.")
    if not isinstance(expand_loops, bool):
        fail("Invalid loop export option.")
    if not isinstance(projects, list) or not projects:
        fail("Select at least one project for the submission.")
    if len(projects) > MAX_SUBMISSION_PROJECTS:
        fail(f"Select no more than {MAX_SUBMISSION_PROJECTS} projects.")

    selected_projects = []
    selected_names = set()
    for entry in projects:
        if not isinstance(entry, dict):
            fail("Invalid submission project.")
        name = validate_project_name(entry.get("name"))
        if name in selected_names:
            fail(f"The project '{name}' was selected more than once.")
        selected_names.add(name)
        source = entry.get("source")
        if source is not None and not isinstance(source, str):
            fail(f"The source for project '{name}' is invalid.")
        allow_scaffold_edit = entry.get("allowScaffoldEdit", False)
        if not isinstance(allow_scaffold_edit, bool):
            fail(f"The scaffold protection option for project '{name}' is invalid.")
        selected_projects.append({"name": name, "source": source,
                                  "allowScaffoldEdit": allow_scaffold_edit})

    decoded_attachments = decode_submission_attachments(attachments)
    archive_entries = []
    manifest_projects = []
    manifest_attachments = []
    manifest_files = []
    normal_sections = []
    advanced_sections = []

    def add_archive_file(archive_path, content):
        digest = hashlib.sha256(content).hexdigest()
        archive_entries.append((archive_path, content))
        record = {"path": archive_path, "hash": digest, "size": len(content)}
        manifest_files.append(record)
        return record

    for index, entry in enumerate(selected_projects, start=1):
        name = entry["name"]
        folder = project_dir(name)
        source_path = (source_file(folder) if entry["source"] is None
                       else save_source(folder, entry["source"],
                                        entry["allowScaffoldEdit"]))
        built = build(name)
        normal_sections.append(f"[{name}]\n{built['output']}")
        advanced_sections.append(f"[{name}]\n{built['advancedOutput']}")
        if not built["ok"]:
            return {"ok": False, "phase": "build", "project": name,
                    "output": "\n\n".join(normal_sections),
                    "advancedOutput": "\n\n".join(advanced_sections)}
        simulated = simulate(name)
        normal_sections[-1] += "\n" + simulated["output"]
        advanced_sections[-1] += "\n" + simulated["advancedOutput"]
        if not simulated["ok"]:
            return {"ok": False, "phase": "simulate", "project": name,
                    "output": "\n\n".join(normal_sections),
                    "advancedOutput": "\n\n".join(advanced_sections)}

        data = pipeline(name)
        project_root = f"projects/{submission_archive_component(name, index)}"
        archive_source_name = re.sub(
            r"[^A-Za-z0-9._ -]+", "_", source_path.name).strip(" .") or "program.s"
        source_record = add_archive_file(
            f"{project_root}/{archive_source_name}", source_path.read_bytes())
        pipeline_record = add_archive_file(
            f"{project_root}/pipeline.csv",
            pipeline_csv(data, expand_loops).encode("utf-8"))
        config_record = add_archive_file(
            f"{project_root}/ase-studio-config.json",
            (json.dumps(project_config(folder), indent=2, sort_keys=True)
             + "\n").encode("utf-8"))
        manifest_projects.append({
            "name": name,
            "archivePath": project_root,
            "source": source_record["path"],
            "pipeline": pipeline_record["path"],
            "configuration": config_record["path"],
        })

    for attachment in decoded_attachments:
        archive_path = f"attachments/{attachment['name']}"
        record = add_archive_file(archive_path, attachment["bytes"])
        manifest_attachments.append({
            "name": attachment["name"],
            "path": archive_path,
            "mimeType": attachment["mimeType"],
            "size": record["size"],
        })

    submission_root = active_submission_directory()
    try:
        submission_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        fail(f"The submission folder could not be created: {error}")
    destination = (submission_root / assignment).resolve()
    if destination.parent != submission_root:
        fail("Invalid submission destination.")
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        fail(f"The assignment submission folder could not be created: {error}")
    archive_stem = submission_archive_stem(assignment)
    archive = destination / f"{archive_stem}.zip"
    submitted_at = (datetime.now(timezone.utc).isoformat(timespec="seconds")
                    .replace("+00:00", "Z"))
    canonical_files = json.dumps(
        sorted(manifest_files, key=lambda item: item["path"]),
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    content_hash = hashlib.sha256(canonical_files).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "generatedBy": f"ASE Studio {STUDIO_VERSION}",
        "submittedAt": submitted_at,
        "assignment": assignment,
        "submissionName": archive_stem,
        "hashAlgorithm": "SHA-256",
        "projects": manifest_projects,
        "attachments": manifest_attachments,
        "files": manifest_files,
        "contentHash": content_hash,
        "contenthash": content_hash,
    }
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for archive_path, content in archive_entries:
            bundle.writestr(archive_path, content)
        bundle.writestr(".ase-submission.json",
                        json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    display_archive = portable_environment_value(str(archive))
    attachment_summary = (f" and {len(decoded_attachments)} additional file(s)"
                          if decoded_attachments else "")
    normal_sections.append(
        f"Submission ready: {display_archive}\n"
        f"Contains {len(selected_projects)} project(s){attachment_summary}.")
    advanced_sections.append(f"Created {display_archive}.")
    return {"ok": True, "phase": "submission",
            "output": "\n\n".join(normal_sections),
            "advancedOutput": "\n\n".join(advanced_sections),
            "archive": str(archive),
            "projects": [entry["name"] for entry in selected_projects]}


def reveal_submission_folder(archive_path):
    """Open the file manager at a completed submission's directory."""
    if not isinstance(archive_path, str) or not archive_path:
        fail("The submission archive path is missing.")
    submissions_root = active_submission_directory()
    archive = resolve_environment_path(archive_path)
    try:
        archive.relative_to(submissions_root)
    except ValueError:
        fail("Invalid submission archive path.")
    if not archive.is_file() or archive.suffix.lower() != ".zip":
        fail("The submission ZIP could not be found.", 404)
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        fail("A Linux desktop session is required to open the submission folder.")
    opener = shutil.which("xdg-open")
    if not opener:
        fail("The system file manager opener (xdg-open) is not installed.", 500)
    try:
        subprocess.Popen(
            [opener, str(archive.parent)],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        fail(f"The submission folder could not be opened: {error}", 500)
    return {"ok": True,
            "folder": portable_environment_value(str(archive.parent))}


def git_run(args, timeout=20, cwd=ROOT):
    """Run a narrowly scoped git command and return its text output."""
    try:
        result = subprocess.run(["git", *args], cwd=cwd, text=True,
                                capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def required_repository_branches():
    """Load release branches, including configs written before `studio`."""
    try:
        values = json.loads(REQUIRED_BRANCHES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"Required-branch configuration is missing: {REQUIRED_BRANCHES_FILE}", 500)
    except (OSError, json.JSONDecodeError) as error:
        fail(f"Required-branch configuration cannot be read: {error}", 500)
    if not isinstance(values, dict):
        fail("Required-branch configuration must be a JSON object.", 500)

    # A checkout on a retired parent branch contains that branch's old config.
    # Prefer the cached configuration on origin's default branch so startup can
    # migrate the parent without requiring a manual checkout. This remains
    # offline-friendly because it reads an existing remote-tracking ref.
    current_branch = repository_current_branch(ROOT)
    ok, remote_head = git_run(
        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=ROOT)
    if ok and remote_head and current_branch != remote_head.removeprefix("origin/"):
        found, remote_text = git_run(
            ["show", f"{remote_head}:{REQUIRED_BRANCHES_FILE.name}"], cwd=ROOT)
        if found:
            try:
                remote_values = json.loads(remote_text)
            except json.JSONDecodeError:
                remote_values = None
            if isinstance(remote_values, dict):
                values = remote_values

    branches = {}
    for key in ("simulator", "studio", "gem5"):
        # `studio` was added after the first public branch-config format. Old
        # installations therefore use the public ASE Studio default branch.
        branch = values.get(key, "main" if key == "studio" else None)
        if (not isinstance(branch, str) or not branch.strip()
                or branch.startswith("-") or "\0" in branch
                or any(character.isspace() for character in branch)):
            fail(f"Required branch '{key}' is invalid.", 500)
        branches[key] = branch.strip()
    return branches


def repository_current_branch(repository):
    ok, branch = git_run(["branch", "--show-current"], cwd=repository)
    if not ok:
        return None
    return branch.strip() or "(detached HEAD)"


def ensure_repository_branch(repository, label, required_branch):
    """Switch a checkout to its deployment branch without discarding work."""
    repository = Path(repository).resolve()
    current_branch = repository_current_branch(repository)
    if current_branch is None:
        return f"{label} is not a Git checkout: {repository}", False
    if current_branch == required_branch:
        return None, False

    local_ref = f"refs/heads/{required_branch}"
    remote_ref = f"refs/remotes/origin/{required_branch}"
    local_exists, _ = git_run(
        ["show-ref", "--verify", "--quiet", local_ref], cwd=repository)
    if not local_exists:
        remote_exists, _ = git_run(
            ["show-ref", "--verify", "--quiet", remote_ref], cwd=repository)
        if not remote_exists:
            refspec = f"+refs/heads/{required_branch}:{remote_ref}"
            fetched, fetch_output = git_run(
                ["fetch", "origin", refspec], timeout=60, cwd=repository)
            if not fetched:
                detail = fetch_output.splitlines()[-1] if fetch_output else "fetch failed"
                return (f"{label} requires branch '{required_branch}', but ASE Studio "
                        f"could not fetch it from origin: {detail}"), False
        # A single-branch clone may have the remote ref without a matching
        # remote fetch rule, so create from the explicit ref without --track.
        # ASE Studio's updater always names origin and the branch explicitly.
        command = [
            "switch",
            "--no-recurse-submodules",
            "-c",
            required_branch,
            f"origin/{required_branch}",
        ]
    else:
        command = [
            "switch",
            "--no-recurse-submodules",
            required_branch,
        ]

    switched, switch_output = git_run(command, timeout=60, cwd=repository)
    if not switched:
        detail = (switch_output.splitlines()[-1]
                  if switch_output else "Git refused the branch switch")
        return (
            f"{label} requires branch '{required_branch}', but '{current_branch}' "
            f"is checked out and the automatic switch failed: {detail}. "
            "Commit or stash conflicting tracked changes, then start ASE Studio again."
        ), False
    actual_branch = repository_current_branch(repository)
    if actual_branch != required_branch:
        return (f"{label} did not reach required branch '{required_branch}' "
                f"after switching; current branch is '{actual_branch}'."), False
    return None, True


def align_studio_submodule_branch(required_branch, expected_commit=None):
    """Attach a detached Studio checkout to its deployment branch safely.

    ``git submodule update`` intentionally leaves a submodule detached. Prefer
    the fetched deployment branch when it contains the commit pinned by the
    parent; otherwise attach the branch to the pinned commit itself. A Studio
    checkout already on the required branch is left alone, because it may have
    been updated independently to a newer commit.
    """
    if expected_commit is None:
        found, expected_commit = git_run(
            ["rev-parse", "HEAD:ase_studio"], cwd=ROOT)
        if not found:
            return None, False
    expected_commit = expected_commit.strip()

    found, _current_commit = git_run(["rev-parse", "HEAD"], cwd=STUDIO_ROOT)
    if not found:
        return f"ASE Studio is not a Git checkout: {STUDIO_ROOT}", False
    current_branch = repository_current_branch(STUDIO_ROOT)
    if current_branch == required_branch:
        return None, False

    found, _ = git_run(
        ["cat-file", "-e", f"{expected_commit}^{{commit}}"], cwd=STUDIO_ROOT)
    if not found:
        return ("ASE Studio cannot find the commit pinned by the simulator "
                f"repository ({expected_commit[:12]})."), False

    inspected, dirty = git_run(
        ["status", "--porcelain", "--untracked-files=no"], cwd=STUDIO_ROOT)
    if not inspected:
        return "ASE Studio could not inspect its tracked files before alignment.", False
    if dirty:
        return (
            "ASE Studio is at the parent-pinned release, but its branch cannot "
            "be reattached because tracked files have local changes. Commit or "
            "stash them, then start ASE Studio again."
        ), False

    remote_ref = f"origin/{required_branch}"
    remote_exists, remote_commit = git_run(
        ["rev-parse", "--verify", remote_ref], cwd=STUDIO_ROOT)
    target_commit = expected_commit
    if remote_exists:
        contains_expected, _ = git_run(
            ["merge-base", "--is-ancestor", expected_commit,
             remote_commit.strip()], cwd=STUDIO_ROOT)
        if contains_expected:
            target_commit = remote_commit.strip()

    local_ref = f"refs/heads/{required_branch}"
    local_exists, local_commit = git_run(
        ["rev-parse", "--verify", local_ref], cwd=STUDIO_ROOT)
    if local_exists and local_commit.strip() != target_commit:
        compared, ahead_text = git_run(
            ["rev-list", "--count", f"{target_commit}..{local_ref}"],
            cwd=STUDIO_ROOT)
        if not compared:
            return ("ASE Studio could not compare its local deployment branch "
                    "with the release selected by the simulator."), False
        if int(ahead_text or "0") > 0:
            short_commit = local_commit.strip()[:12]
            branch_label = required_branch.replace("/", "-")
            backup_branch = f"backup/ase-studio-{branch_label}-{short_commit}"
            backup_exists, _ = git_run(
                ["show-ref", "--verify", "--quiet",
                 f"refs/heads/{backup_branch}"], cwd=STUDIO_ROOT)
            if not backup_exists:
                backed_up, backup_output = git_run(
                    ["branch", backup_branch, local_commit.strip()],
                    cwd=STUDIO_ROOT)
                if not backed_up:
                    return ("ASE Studio could not preserve its divergent local "
                            f"branch before reattachment: {backup_output}"), False
                print(f"Preserved divergent ASE Studio commits on {backup_branch}.",
                      flush=True)

    switched, switch_output = git_run(
        ["switch", "-C", required_branch, target_commit],
        timeout=60, cwd=STUDIO_ROOT)
    if not switched:
        detail = switch_output.splitlines()[-1] if switch_output else "Git refused"
        return f"ASE Studio could not reattach '{required_branch}': {detail}", False
    if remote_exists:
        # Update checks name origin/<branch> explicitly, so a failure here is
        # harmless (for example in a single-branch clone with unusual config).
        git_run(
            ["branch", "--set-upstream-to", remote_ref, required_branch],
            cwd=STUDIO_ROOT)
    return None, True


def filesystem_git_checkout(path):
    """Find the checkout containing *path* without invoking Git."""
    path = Path(path).resolve()
    candidates = ((path, *path.parents) if path.is_dir()
                  else (path.parent, *path.parents))
    for candidate in candidates:
        marker = candidate / ".git"
        if marker.is_dir() or marker.is_file():
            return candidate
    return None


def git_metadata_path(repository):
    """Resolve a checkout's .git directory using filesystem operations only."""
    marker = Path(repository) / ".git"
    if marker.is_dir():
        return marker
    if not marker.is_file():
        return None
    try:
        declaration = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not declaration.lower().startswith(prefix):
        return None
    metadata = Path(declaration[len(prefix):].strip())
    if not metadata.is_absolute():
        metadata = marker.parent / metadata
    return metadata.resolve()


def gem5_checkout_is_writable(repository, build_dir):
    """Return whether gem5 is owned and can be maintained by this user.

    Git intentionally rejects repositories owned by another account unless
    they are explicitly listed as safe directories. ASE Studio must not add
    that trust globally on a student's behalf. Treat such checkouts as
    administrator-managed even when group permissions or ACLs make them
    writable, and use their existing gem5 executable without running Git.
    """
    metadata = git_metadata_path(repository)
    try:
        effective_uid = os.geteuid()
        checkout_owned = Path(repository).stat().st_uid == effective_uid
        metadata_owned = (metadata is not None
                          and metadata.stat().st_uid == effective_uid)
    except (AttributeError, OSError):
        checkout_owned = False
        metadata_owned = False
    return bool(
        metadata is not None
        and metadata.is_dir()
        and checkout_owned
        and metadata_owned
        and os.access(repository, os.W_OK)
        and os.access(metadata, os.W_OK)
        and os.access(build_dir, os.W_OK)
    )


def configured_gem5_location(values=None):
    """Locate the configured executable and its managed checkout, if any."""
    if values is None:
        values = setup_environment()
        values.update(environment_overrides())
    try:
        executable = gem5_executable_path(values["GEM5_INSTALLATION_PATH"])
    except (KeyError, ValueError) as error:
        return {"error": f"gem5 update checking is unavailable: {error}"}

    standard_build = (
        executable.name == f"gem5.{GEM5_VARIANT}"
        and executable.parent.name == GEM5_ISA
        and executable.parent.parent.name == "build"
    )
    build_dir = executable.parent.parent if standard_build else executable.parent
    candidate = filesystem_git_checkout(executable)
    # A wrapper can live in the parent simulator repository. It must not make
    # that repository look like a gem5 checkout to branch/update management.
    expected = (candidate / "build" / GEM5_ISA / f"gem5.{GEM5_VARIANT}"
                if candidate is not None else None)
    repository = (candidate if standard_build and expected is not None
                  and executable == expected.resolve() else None)
    source_dir = repository if repository is not None else executable.parent
    return {
        "buildDir": str(build_dir),
        "executable": str(executable),
        "sourceDir": str(source_dir),
        "repository": str(repository) if repository is not None else "",
        "filesystemWritable": bool(
            source_dir.is_dir()
            and os.access(source_dir, os.W_OK)
            and executable.parent.is_dir()
            and os.access(executable.parent, os.W_OK)
        ),
        "writable": (gem5_checkout_is_writable(repository, build_dir)
                     if repository is not None else False),
    }


def ensure_configured_gem5_branch(location, required_branch):
    """Select writable gem5, or request action for a shared checkout."""
    if not location.get("repository"):
        # A wrapper or binary-only installation has no reliable branch
        # metadata. It remains runnable, but update management is unavailable.
        return None, False

    # A shared checkout may be owned by root or an administrator. Running Git
    # in it can fail with "detected dubious ownership". Its installed
    # executable is still valid, so leave branch management to the owner.
    if not location.get("writable"):
        return None, False

    gem5_root = Path(location["repository"])
    ok, top = git_run(["rev-parse", "--show-toplevel"], cwd=gem5_root)
    if not ok:
        return f"The configured gem5 source is not a Git checkout: {gem5_root}", False
    actual_root = Path(top).resolve()
    current_branch = repository_current_branch(actual_root)
    if current_branch is None:
        return f"gem5 is not a Git checkout: {actual_root}", False
    if current_branch == required_branch:
        return None, False
    return ensure_repository_branch(actual_root, "gem5", required_branch)


def require_startup_repositories():
    """Select supported parent/Studio branches and validate the gem5 branch."""
    if DEVELOPER_MODE:
        print(
            "ASE Studio developer mode: startup repository alignment skipped.",
            flush=True,
        )
        return {}

    required = required_repository_branches()
    problems = []
    switched_labels = []

    problem, switched = ensure_repository_branch(
        ROOT, "Simulator", required["simulator"])
    if problem:
        problems.append(problem)
    elif switched:
        switched_labels.append(f"Simulator -> {required['simulator']}")

    studio_problem, studio_aligned = align_studio_submodule_branch(
        required["studio"])
    if studio_problem:
        problems.append(studio_problem)
    elif studio_aligned:
        switched_labels.append(f"ASE Studio -> {required['studio']}")
    else:
        problem, switched = ensure_repository_branch(
            STUDIO_ROOT, "ASE Studio", required["studio"])
        if problem:
            problems.append(problem)
        elif switched:
            switched_labels.append(f"ASE Studio -> {required['studio']}")

    # Repository mismatches are unsafe and still block startup. Tool paths are
    # user configuration, however, so an invalid first-run gem5 path must not
    # prevent the user from opening Settings and correcting it.
    configuration_warnings = []
    values = setup_environment()
    values.update(environment_overrides())
    location = configured_gem5_location(values)
    if location.get("error"):
        configuration_warnings.append(
            f"The configured gem5 path is invalid: {location['error']}")
    elif (not Path(location["executable"]).is_file()
          or not os.access(location["executable"], os.X_OK)):
        configuration_warnings.append(
            "The configured gem5 executable or wrapper does not exist or is "
            f"not executable: {location['executable']}")
    elif location.get("repository") and not location.get("writable"):
        configuration_warnings.append(
            "The configured gem5 checkout is owned or managed by another "
            f"account: {location['repository']}. ASE Studio will use its "
            "existing executable, but Git branch and update checks are "
            "disabled; ask the administrator to maintain this checkout.")
    else:
        problem, switched = ensure_configured_gem5_branch(
            location, required["gem5"])
        if problem:
            problems.append(problem)
        elif switched:
            switched_labels.append(f"gem5 -> {required['gem5']}")
    if problems:
        detail = "\n".join(f"- {problem}" for problem in problems)
        fail(
            "ASE Studio cannot start because its repositories do not match "
            "the supported deployment.\n"
            f"{detail}\n"
            f"Change required branch names in {REQUIRED_BRANCHES_FILE}.",
            500,
        )
    if switched_labels:
        print("ASE Studio selected required branches: "
              + ", ".join(switched_labels), flush=True)
    if configuration_warnings:
        print(
            "ASE Studio started with configuration warnings. "
            "Open Settings to correct these paths:\n- "
            + "\n- ".join(configuration_warnings),
            file=sys.stderr, flush=True,
        )
    return required


def normalized_git_remote(value):
    """Normalize common HTTPS/SSH GitHub URLs for identity checks."""
    remote = value.strip().removesuffix(".git").removesuffix("/")
    remote = re.sub(r"^git@github\.com:", "github.com/", remote)
    remote = re.sub(r"^(?:https?|ssh)://(?:git@)?", "", remote)
    return remote.lower()


def configured_gem5_checkout(values=None):
    """Describe the Git checkout owning the configured gem5 executable."""
    location = configured_gem5_location(values)
    if location.get("error"):
        return {"managed": False,
                "message": location["error"]}
    build_dir = Path(location["buildDir"])
    executable = Path(location["executable"])
    if not executable.is_file() or not os.access(executable, os.X_OK):
        return {"managed": False, "buildDir": str(build_dir),
                "executable": str(executable),
                "message": ("The configured gem5 executable or wrapper does "
                            "not exist or is not executable; updates cannot "
                            "be checked.")}
    if not location["repository"]:
        return {"managed": False, "buildDir": str(build_dir),
                "executable": str(executable),
                "message": ("The executable or wrapper works, but ASE Studio "
                            "cannot identify its gem5 Git checkout; gem5 "
                            "updates and automatic rebuilds are disabled.")}
    repository = Path(location["repository"])
    if not location["writable"]:
        return {"managed": False, "path": str(repository),
                "buildDir": str(build_dir), "executable": str(executable),
                "message": ("The gem5 checkout is owned or managed by "
                            "another account. Its existing executable remains "
                            "available, but Git branch and update checks were "
                            "skipped; ask the administrator to maintain it.")}
    ok, top = git_run(["rev-parse", "--show-toplevel"], cwd=repository)
    if not ok or Path(top).resolve() != repository:
        return {"managed": False, "path": str(repository),
                "buildDir": str(build_dir), "executable": str(executable),
                "message": ("The executable works, but its source directory "
                            "is not a valid Git checkout; updates cannot be "
                            "checked.")}
    ok, origin = git_run(["remote", "get-url", "origin"], cwd=repository)
    if not ok or normalized_git_remote(origin) != OFFICIAL_GEM5_REPOSITORY:
        shown = origin if ok and origin else "no origin remote"
        return {"managed": False, "path": str(repository),
                "buildDir": str(build_dir), "executable": str(executable),
                "message": ("The executable is not connected to the official "
                            f"cad-polito-it/gem5 repository ({shown}); "
                            "updates cannot be checked.")}
    try:
        target = str(executable.relative_to(repository))
    except ValueError:
        return {"managed": False, "path": str(repository),
                "buildDir": str(build_dir), "executable": str(executable),
                "message": ("The configured gem5 executable is outside its "
                            "Git checkout; updates cannot be rebuilt safely.")}
    return {"managed": True, "path": str(repository),
            "buildDir": str(build_dir), "executable": str(executable),
            "target": target, "origin": origin}


def gem5_update_status(refresh=False):
    checkout = configured_gem5_checkout()
    if not checkout["managed"]:
        return {**checkout, "label": "gem5", "managed": False,
                "available": False, "canUpdate": False, "warning": True,
                "message": f"gem5: {checkout['message']}"}
    status = repository_update_status(
        Path(checkout["path"]), "gem5", refresh)
    status.update({key: value for key, value in checkout.items()
                   if key != "managed"})
    status["warning"] = False
    return status


def repository_update_status(path, label, refresh=False, ignored_paths=()):
    path = Path(path).resolve()
    ok, top = git_run(["rev-parse", "--show-toplevel"], cwd=path)
    if not ok or Path(top).resolve() != path:
        return {"label": label, "managed": False, "available": False,
                "canUpdate": False, "message": f"{label}: Git repository not configured."}
    _, branch = git_run(["branch", "--show-current"], cwd=path)
    remote_ref = f"origin/{branch}" if branch else ""
    if refresh:
        fetched, fetch_message = git_run(["fetch", "--quiet", "origin"], cwd=path)
        if not fetched:
            return {"label": label, "managed": True, "available": False,
                    "canUpdate": False,
                    "message": f"{label}: could not check for updates: {fetch_message}"}
    if not remote_ref:
        ok, remote_ref = git_run(
            ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            cwd=path,
        )
        if not ok:
            remote_ref = next((candidate for candidate in ("origin/main", "origin/master")
                               if git_run(["rev-parse", "--verify", candidate], cwd=path)[0]), "")
    exists = bool(remote_ref) and git_run(["rev-parse", "--verify", remote_ref], cwd=path)[0]
    if not exists:
        return {"label": label, "managed": True, "available": False,
                "canUpdate": False, "message": f"{label}: remote branch unavailable."}
    compared, counts = git_run(
        ["rev-list", "--left-right", "--count", f"HEAD...{remote_ref}"], cwd=path)
    if not compared:
        return {"label": label, "managed": True, "available": False,
                "canUpdate": False, "message": f"{label}: {counts}"}
    ahead, behind = (int(value) for value in counts.split())
    # Only tracked changes can be overwritten by a normal pull. Untracked
    # student projects and generated output are intentionally preserved and
    # do not disable updates. Shared tracked files such as programs/demo.mk
    # must remain visible to the preflight check.
    status_args = ["status", "--porcelain", "--untracked-files=no", "--", "."]
    status_args.extend(f":(exclude){item}" for item in ignored_paths)
    _, dirty_output = git_run(status_args, cwd=path)
    dirty_lines = dirty_output.splitlines()
    dirty = bool(dirty_lines)
    available = behind > 0
    message = (f"{label}: {behind} update{'s' if behind != 1 else ''} available."
               if available else f"{label}: up to date.")
    if ahead:
        message += f" Local checkout is {ahead} commit{'s' if ahead != 1 else ''} ahead."
    if dirty:
        message += " Local tracked changes require a choice before updating."
    return {"label": label, "managed": True, "available": available,
            "canUpdate": available and not dirty, "dirty": dirty, "branch": branch,
            "remoteRef": remote_ref, "detached": not bool(branch),
            "behind": behind, "message": message, "path": str(path),
            "changes": dirty_lines,
            "restorePathspec": [".", *(f":(exclude){item}"
                                           for item in ignored_paths)]}


def rebuild_configured_gem5(repository):
    """Rebuild the configured gem5 target after its source is updated."""
    checkout = configured_gem5_checkout()
    if not checkout["managed"] or Path(checkout.get("path", "")) != Path(repository):
        return False, checkout.get(
            "message", "The configured gem5 checkout could not be rebuilt.")
    repository = Path(repository)
    scons_candidates = (
        repository.parent / "myenv" / "bin" / "scons",
        ROOT / "tools" / "myenv" / "bin" / "scons",
    )
    scons = next((path for path in scons_candidates
                  if path.is_file() and os.access(path, os.X_OK)), None)
    if scons is None:
        system_scons = shutil.which("scons")
        scons = Path(system_scons) if system_scons else None
    if scons is None:
        return False, ("gem5 was updated, but SCons was not found. Install "
                       "SCons or reinstall gem5 from Settings.")

    python = scons.parent / "python"
    if not python.is_file():
        python = Path(sys.executable)
    version = subprocess.run(
        [str(python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        text=True, capture_output=True, timeout=10,
    )
    if version.returncode != 0:
        return False, f"Could not determine the gem5 build Python: {version.stderr.strip()}"
    python_config_name = f"python{version.stdout.strip()}-config"
    python_config = shutil.which(python_config_name)
    if not python_config:
        candidate = Path("/usr/bin") / python_config_name
        python_config = str(candidate) if candidate.is_file() else ""
    if not python_config:
        return False, (f"gem5 was updated, but {python_config_name} was not found. "
                       f"Install the Python {version.stdout.strip()} development package.")
    compiler = shutil.which("gcc")
    cxx = shutil.which("g++")
    if not compiler or not cxx:
        return False, "gem5 was updated, but the native gcc/g++ compiler was not found."

    command = [str(scons), checkout["target"], f"-j{max(1, os.cpu_count() or 1)}"]
    environment = os.environ.copy()
    environment.update({"PYTHON": str(python), "PYTHON_CONFIG": python_config,
                        "CC": compiler, "CXX": cxx})
    try:
        result = subprocess.run(command, cwd=repository, env=environment,
                                text=True, capture_output=True)
    except OSError as error:
        return False, f"Could not start the gem5 rebuild: {error}"
    output = (f"$ {shlex.join(command)}\n" + result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return False, "gem5 rebuild failed:\n" + output
    return True, "gem5 rebuild completed successfully:\n" + output


def update_status(refresh=False):
    repositories = [
        repository_update_status(
            ROOT,
            "Simulator",
            refresh,
            # Source projects and setup_default variants are local student or
            # deployment data. They must survive simulator updates.
            ignored_paths=("ase_studio", *LOCAL_PARENT_PATHSPECS),
        ),
        repository_update_status(STUDIO_ROOT, "ASE Studio", refresh),
        gem5_update_status(refresh),
    ]
    available = any(item["available"] for item in repositories)
    # Pulling the parent can move the Studio submodule, so local work in
    # either checkout blocks the combined update even if only the other
    # repository currently has new commits.
    blocked = [item for item in repositories if item.get("dirty")]
    return {
        "available": available,
        "canUpdate": available and not blocked,
        "repositories": repositories,
        "message": " ".join(item["message"] for item in repositories),
        "checkedAt": int(time.time()),
    }


def discard_tracked_repository_changes(repository, pathspec=(".",)):
    """Restore tracked files after the user explicitly chooses discard."""
    repository = Path(repository)
    restored, output = git_run(
        ["restore", "--source=HEAD", "--staged", "--worktree", "--", *pathspec],
        cwd=repository,
    )
    if not restored:
        return False, output or "Git could not restore the tracked files."
    return True, output


def preserve_local_parent_files():
    """Temporarily prepare local-only data while the parent repository pulls.

    ``programs`` is a user workspace after installation, so preserve its whole
    tree, including unchanged tracked files and untracked projects. This stops
    later repository updates from modifying, adding, or deleting any program.
    Deployment-local setup_default changes are preserved separately.
    """
    listed, output = git_run(
        ["diff", "--name-only", "HEAD", "--", LOCAL_SETUP_PATHSPEC],
        cwd=ROOT,
    )
    if not listed:
        return None, output or "Could not inspect local setup files."
    setup_snapshots = []
    for relative_name in output.splitlines():
        relative_path = Path(relative_name)
        candidate = (ROOT / relative_name).resolve()
        is_setup = (relative_path.parent == Path(".")
                    and relative_path.name.startswith("setup_default"))
        if (not candidate.is_relative_to(ROOT)
                or not is_setup):
            return None, f"Refusing to preserve an unexpected setup path: {relative_name}"
        exists = candidate.is_file()
        setup_snapshots.append({
            "path": candidate,
            "relative": relative_name,
            "exists": exists,
            "contents": candidate.read_bytes() if exists else None,
            "mode": candidate.stat().st_mode if exists else None,
        })

    programs_path = ROOT / "programs"
    if programs_path.is_symlink():
        return None, "Refusing to update while the programs directory is a symbolic link."
    if programs_path.exists() and not programs_path.is_dir():
        return None, "The programs path exists but is not a directory."

    try:
        backup_root = Path(tempfile.mkdtemp(
            prefix=".ase-studio-programs-backup-", dir=ROOT))
        programs_backup = backup_root / "programs"
        programs_existed = programs_path.exists()
        if programs_existed:
            shutil.copytree(programs_path, programs_backup, symlinks=True)
            shutil.rmtree(programs_path)
    except OSError as error:
        if "backup_root" in locals():
            shutil.rmtree(backup_root, ignore_errors=True)
        return None, f"Could not back up the programs directory: {error}"

    snapshot = {
        "setupFiles": setup_snapshots,
        "programsBackupRoot": backup_root,
        "programsBackup": programs_backup,
        "programsExisted": programs_existed,
    }
    restored, restore_output = git_run(
        ["restore", "--source=HEAD", "--staged", "--worktree", "--",
         LOCAL_SETUP_PATHSPEC, "programs"],
        cwd=ROOT,
    )
    if not restored:
        recovered, recovery_error = restore_local_parent_files(snapshot)
        message = (restore_output
                   or "Could not prepare local files for the repository update.")
        if not recovered:
            message += "\n" + recovery_error
        return None, message
    return snapshot, ""


def restore_local_parent_files(snapshot):
    """Restore the exact local programs tree and saved setup files."""
    backup_root = snapshot["programsBackupRoot"]
    programs_backup = snapshot["programsBackup"]
    programs_path = ROOT / "programs"
    try:
        if programs_path.is_symlink() or programs_path.is_file():
            programs_path.unlink()
        elif programs_path.exists():
            shutil.rmtree(programs_path)
        if snapshot["programsExisted"]:
            shutil.copytree(programs_backup, programs_path, symlinks=True)

        for setup_snapshot in snapshot["setupFiles"]:
            path = setup_snapshot["path"]
            if setup_snapshot["exists"]:
                path.write_bytes(setup_snapshot["contents"])
                path.chmod(setup_snapshot["mode"] & 0o7777)
            elif path.exists():
                path.unlink()
    except OSError as error:
        return False, (
            f"Could not restore local program/configuration files: {error}\n"
            f"The recovery copy is available at {backup_root}")
    shutil.rmtree(backup_root, ignore_errors=True)
    return True, ""


def pull_update(discard_local_changes=False):
    status = update_status(refresh=True)
    if not status["available"]:
        return {"ok": True, "output": status["message"], "advancedOutput": status["message"]}
    discard_outputs = []
    if not status["canUpdate"]:
        blocked = [item for item in status["repositories"] if item.get("dirty")]
        if not discard_local_changes:
            return {"ok": False, "needsDecision": True,
                    "repositories": blocked, "output": status["message"],
                    "advancedOutput": status["message"]}
        for item in blocked:
            restored, restore_output = discard_tracked_repository_changes(
                item["path"], item.get("restorePathspec", ["."]))
            if not restored:
                message = (f"Could not discard tracked changes in {item['label']}:\n"
                           f"{restore_output}")
                return {"ok": False, "output": message, "advancedOutput": message}
            changed = "\n".join(item.get("changes", [])) or "tracked files"
            discard_outputs.append(
                f"{item['label']}: discarded these local tracked changes before update:\n"
                f"{changed}")
        status = update_status(refresh=False)
        if not status["canUpdate"]:
            message = ("Tracked changes were restored, but the update is still blocked.\n"
                       + status["message"])
            return {"ok": False, "output": message, "advancedOutput": message}
    outputs = discard_outputs
    ok = True
    parent = status["repositories"][0]
    if parent["available"]:
        local_snapshots, preservation_error = preserve_local_parent_files()
        if local_snapshots is None:
            message = ("Could not preserve local project/configuration files before update:\n"
                       + preservation_error)
            return {"ok": False, "output": message, "advancedOutput": message}
        command = (["pull", "--ff-only", "origin", parent["branch"]]
                   if parent["branch"] else
                   ["merge", "--ff-only", parent["remoteRef"]])
        pulled, output = git_run(command, timeout=90, cwd=ROOT)
        local_restored, local_restore_error = restore_local_parent_files(local_snapshots)
        if not local_restored:
            pulled = False
            output = ((output + "\n") if output else "") + local_restore_error
        else:
            preserved = ["the complete programs directory"]
            setup_files = local_snapshots["setupFiles"]
            if setup_files:
                preserved.append(
                    "local setup files ("
                    + ", ".join(item["relative"] for item in setup_files) + ")")
            output = ((output + "\n") if output else "") + (
                "Preserved " + " and ".join(preserved) + ".")
        ok = ok and pulled
        if output:
            outputs.append(f"Simulator:\n{output}")
    if ok and (ROOT / ".gitmodules").exists():
        submodule_ok, submodule_output = git_run(
            ["submodule", "update", "--init", "--recursive", "ase_studio"],
            timeout=90,
            cwd=ROOT,
        )
        ok = ok and submodule_ok
        if submodule_output:
            outputs.append(f"Submodule:\n{submodule_output}")
        if submodule_ok:
            branch_problem, branch_aligned = align_studio_submodule_branch(
                required_repository_branches()["studio"])
            if branch_problem:
                ok = False
                outputs.append(f"ASE Studio:\n{branch_problem}")
            elif branch_aligned:
                outputs.append("ASE Studio:\nReattached the local branch to the parent-pinned release.")
    studio = repository_update_status(STUDIO_ROOT, "ASE Studio", refresh=True)
    if ok and studio["available"]:
        command = (["pull", "--ff-only", "origin", studio["branch"]]
                   if studio["branch"] else ["merge", "--ff-only", studio["remoteRef"]])
        pulled, output = git_run(command, timeout=90, cwd=STUDIO_ROOT)
        ok = ok and pulled
        if output:
            outputs.append(f"ASE Studio:\n{output}")
    gem5 = status["repositories"][2]
    if ok and gem5.get("available"):
        command = (["pull", "--ff-only", "origin", gem5["branch"]]
                   if gem5["branch"] else
                   ["merge", "--ff-only", gem5["remoteRef"]])
        pulled, output = git_run(command, timeout=90, cwd=Path(gem5["path"]))
        ok = ok and pulled
        if output:
            outputs.append(f"gem5:\n{output}")
        if pulled:
            rebuilt, build_output = rebuild_configured_gem5(Path(gem5["path"]))
            ok = ok and rebuilt
            outputs.append(build_output)
    message = "\n\n".join(outputs) or ("Repositories updated." if ok
                                             else "Repository update failed.")
    if ok:
        message += "\n\nUpdate completed. Restart ASE Studio to load the new version."
    return {"ok": ok, "output": message, "advancedOutput": message,
            "restartRequired": ok}


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass
    def translate_path(self, path):
        # Static files are deliberately limited to the Studio frontend directory.
        requested = Path(urlparse(path).path.lstrip("/"))
        if requested in {Path("logo.png"), Path("icon.png")}:
            return str(FRONTEND / requested)
        candidate = (FRONTEND / requested).resolve()
        return str(candidate) if candidate.is_relative_to(FRONTEND.resolve()) else str(FRONTEND / "index.html")
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()
    def send_json(self, value, status=200):
        data = json.dumps(value).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def read_json(self):
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size))
    def do_GET(self):
        try:
            url = urlparse(self.path)
            if url.path == "/api/projects":
                programs = active_programs_directory()
                if not programs.is_dir():
                    fail(f"The configured programs folder is unavailable: {programs}", 500)
                return self.send_json({
                    "projects": sorted(p.name for p in programs.iterdir() if p.is_dir()),
                    "directory": portable_environment_value(str(programs)),
                })
            if url.path == "/api/health":
                return self.send_json({"ok": True, "apiVersion": STUDIO_API_VERSION,
                                       "version": STUDIO_VERSION,
                                       "o3Defaults": DEFAULT_O3_CONFIG,
                                       "features": {
                                           "developerMode": DEVELOPER_MODE,
                                           "memoryConfiguration":
                                               ENABLE_MEMORY_CONFIGURATION,
                                           "multiIssueCpu": ENABLE_MULTI_ISSUE_CPU,
                                       }})
            if url.path == "/api/update-status":
                return self.send_json(update_status(refresh=True))
            if url.path == "/api/environment":
                return self.send_json(environment_settings())
            if url.path == "/api/project":
                name = parse_qs(url.query).get("name", [""])[0]
                folder = project_dir(name)
                src = source_file(folder)
                source = clean_source(src.read_text())
                configuration = project_config(folder)
                return self.send_json({"name": name, "sourceName": src.name,
                                       "text": source,
                                       "protectedLines": protected_lines(source),
                                       "isa": riscv_isa_for_config(configuration)})
            if url.path == "/api/symbols":
                folder = project_dir(parse_qs(url.query).get("name", [""])[0])
                symbols = data_symbols(folder)
                return self.send_json({
                    "symbols": symbols,
                    "initialMemory": elf_initial_memory(folder, symbols),
                    "memoryMap": elf_memory_map(folder),
                })
            if url.path == "/api/pipeline":
                return self.send_json(pipeline_for_display(
                    parse_qs(url.query).get("name", [""])[0]))
            if url.path == "/api/config":
                folder = project_dir(parse_qs(url.query).get("name", [""])[0])
                return self.send_json(project_config(folder))
            if url.path == "/": self.path = "/index.html"
            if url.path.startswith("/api/"): return self.send_json({"error": "Not found"}, 404)
            return super().do_GET()
        except ValueError as error:
            message, status = error.args[0]; return self.send_json({"error": message}, status)
        except Exception as error:
            return self.send_json({"error": str(error)}, 500)
    def do_POST(self):
        try:
            data = self.read_json()
            if self.path == "/api/save":
                folder = project_dir(data.get("name"))
                save_source(folder, data.get("text"),
                            data.get("allowScaffoldEdit", False))
                return self.send_json({"ok": True})
            if self.path == "/api/run":
                name = data.get("name")
                save_source(project_dir(name), data.get("text"),
                            data.get("allowScaffoldEdit", False))
                built = build(name)
                if not built["ok"]:
                    return self.send_json({"ok": False, "phase": "build", "output": built["output"],
                                           "advancedOutput": built["advancedOutput"]})
                simulated = simulate(name)
                return self.send_json({"ok": simulated["ok"], "phase": "simulate",
                                       "output": built["output"] + "\n" + simulated["output"],
                                       "advancedOutput": built["advancedOutput"] + "\n" + simulated["advancedOutput"]})
            if self.path == "/api/projects":
                name = validate_project_name(data.get("name"))
                destination = project_path(name)
                if destination.exists(): fail("A project with that name already exists.")
                destination.mkdir(); (destination / "main.s").write_text(TEMPLATE); (destination / "Makefile").write_text(MAKEFILE)
                return self.send_json({"ok": True, "name": name})
            if self.path == "/api/projects/duplicate":
                return self.send_json(duplicate_project(
                    data.get("name"), data.get("newName")))
            if self.path == "/api/config":
                return self.send_json(save_project_config(project_dir(data.get("name")), data.get("config")))
            if self.path == "/api/environment":
                if data.get("reset") is True:
                    return self.send_json(reset_environment_settings())
                return self.send_json(save_environment_settings(data.get("values")))
            if self.path == "/api/environment/check":
                return self.send_json(check_environment_field(
                    data.get("key"), data.get("values")))
            if self.path == "/api/environment/import":
                return self.send_json(import_environment_settings(
                    data.get("filename"), data.get("content")))
            if self.path == "/api/install-tool":
                return self.send_json(launch_component_installer(data.get("component")))
            if self.path == "/api/update":
                discard = data.get("discardLocalChanges", False)
                if not isinstance(discard, bool):
                    fail("Invalid update option.")
                return self.send_json(pull_update(discard))
            if self.path == "/api/open-with":
                return self.send_json(open_with_editor(data.get("name")))
            if self.path == "/api/submission/reveal":
                return self.send_json(reveal_submission_folder(data.get("archive")))
            if self.path == "/api/submit":
                projects = data.get("projects")
                # Retain compatibility with clients from before submission v2.
                if projects is None and data.get("name") is not None:
                    projects = [{"name": data.get("name"),
                                 "source": data.get("text"),
                                 "allowScaffoldEdit": data.get(
                                     "allowScaffoldEdit", False)}]
                return self.send_json(create_submission(
                    projects, data.get("assignment"), data.get("attachments"),
                    data.get("expandLoops", False)))
            if self.path == "/api/shutdown":
                self.send_json({"ok": True, "output": "ASE Studio stopped."})

                def stop_server():
                    terminate_active_commands()
                    self.server.shutdown()

                threading.Thread(target=stop_server, daemon=True).start()
                return
            fail("Not found.", 404)
        except ValueError as error:
            message, status = error.args[0]; return self.send_json({"error": message}, status)
        except Exception as error:
            return self.send_json({"error": str(error)}, 500)
    def do_PATCH(self):
        try:
            if self.path != "/api/projects":
                fail("Not found.", 404)
            data = self.read_json()
            return self.send_json(rename_project(data.get("name"), data.get("newName")))
        except ValueError as error:
            message, status = error.args[0]
            return self.send_json({"error": message}, status)
        except Exception as error:
            return self.send_json({"error": str(error)}, 500)
    def do_DELETE(self):
        try:
            if self.path != "/api/projects":
                fail("Not found.", 404)
            data = self.read_json()
            name = data.get("name")
            if data.get("confirmation") != name:
                fail("Project deletion was not confirmed.")
            folder = project_dir(name)
            shutil.rmtree(folder)
            return self.send_json({"ok": True})
        except ValueError as error:
            message, status = error.args[0]
            return self.send_json({"error": message}, status)
        except Exception as error:
            return self.send_json({"error": str(error)}, 500)
TEMPLATE = "# Add an optional .data section here.\n\n# The text section contains the instructions that the CPU runs.\n.section .text\n# Make _start visible as the point where the program begins.\n.globl _start\n_start:\n\n    # Write your RISC-V assembly here.\n\n# The End block stops the program and returns control to the simulator.\nEnd:\n    li a0, 0\n    li a7, 93\n    ecall\n"
MAKEFILE = "ASM = ./main.s\ninclude $(ASE_STUDIO_DEMO_MK)\n"

if __name__ == "__main__":
    try:
        require_startup_repositories()
    except ValueError as error:
        message, _status = error.args[0]
        print(message, file=sys.stderr)
        raise SystemExit(1)
    if "--check-startup" in sys.argv:
        if DEVELOPER_MODE:
            print("ASE Studio startup check completed in developer mode.")
        else:
            print("ASE Studio repository branches are valid.")
        raise SystemExit(0)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        print(f"Port {port} is already occupied; using a new port.", flush=True)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}"
    print(f"ASE Studio: {url}", flush=True)
    if "--open" in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        terminate_active_commands()
        server.server_close()
