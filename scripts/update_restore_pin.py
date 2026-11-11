#!/usr/bin/env python3
"""Regenerate restore/pulse_trader.py.patch from the pinned base blob and the current server/pulse/pulse_trader.py, and update the expected hash in
scripts/regression/logistics.py (PIN_WANT) and deploy/linux-common.sh (pt_want). Run after every edit of pulse_trader.py; the checks
logistics.deploy-pin-matches-engine and logistics.restore-patch-reproduces-pin then pass.

usage: python3 scripts/update_restore_pin.py"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ENGINE = os.path.join(ROOT, "server", "pulse", "pulse_trader.py")
PATCH = os.path.join(ROOT, "restore", "pulse_trader.py.patch")
LOGISTICS = os.path.join(ROOT, "scripts", "regression", "logistics.py")
COMMON = os.path.join(ROOT, "deploy", "linux-common.sh")


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True, check=True).stdout


def main() -> int:
    src = open(LOGISTICS, encoding="utf-8").read()
    base_blob = re.search(r'PIN_BASE = "([0-9a-f]{40})"', src).group(1)
    new_blob = git("hash-object", ENGINE).strip()
    with tempfile.TemporaryDirectory() as td:
        base = os.path.join(td, "base.py")
        with open(base, "w", encoding="utf-8") as fh:
            fh.write(git("show", base_blob))
        proc = subprocess.run(["diff", "-u", "--label", "a/server/pulse/pulse_trader.py", "--label", "b/server/pulse/pulse_trader.py", base, ENGINE],
                              capture_output=True, text=True)
        if proc.returncode not in (0, 1):
            print(proc.stderr)
            return 1
        with open(PATCH, "w", encoding="utf-8") as fh:
            fh.write(proc.stdout)
    src = re.sub(r'PIN_WANT = "[0-9a-f]{40}"', f'PIN_WANT = "{new_blob}"', src)
    open(LOGISTICS, "w", encoding="utf-8").write(src)
    common = open(COMMON, encoding="utf-8").read()
    common = re.sub(r'local pt_want="[0-9a-f]{40}"', f'local pt_want="{new_blob}"', common)
    open(COMMON, "w", encoding="utf-8").write(common)
    print(f"pin -> {new_blob}; patch {os.path.getsize(PATCH)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
