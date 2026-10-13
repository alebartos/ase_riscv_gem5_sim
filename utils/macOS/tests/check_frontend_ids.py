#!/usr/bin/env python3
"""Check that every element a frontend script looks up exists in its page.

A merge with cad-polito-it/ase-studio can drop markup without a conflict;
studio.js would then fail at startup with a null element.

Usage: check_frontend_ids.py FRONTEND_DIR
"""
import re
import sys
from pathlib import Path

PAGES = {"studio.js": "index.html", "memory-window.js": "memory-window.html"}


def main() -> int:
    frontend = Path(sys.argv[1])
    failed = False
    for script, page in PAGES.items():
        if not (frontend / script).is_file():
            continue
        used = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', (frontend / script).read_text()))
        defined = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', (frontend / page).read_text()))
        missing = sorted(used - defined)
        if missing:
            failed = True
            print(f"{script}: missing in {page}: {', '.join(missing)}")
        else:
            print(f"{script}: {len(used)} elements found in {page}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
