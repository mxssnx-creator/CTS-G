#!/usr/bin/env python3
"""Write the non-secret manifest for one CTS-G installation.

The Linux installer generates ``deploy/instance.json`` after rendering the
scoped units.  The file is intentionally derived from paths and Git metadata
only, so it can be used for ownership checks without copying credentials or
exchange settings into the project tree.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("CTS_G_ROOT", Path(__file__).resolve().parents[1])).resolve()
MANIFEST = Path(
    os.environ.get("CTS_INSTANCE_MANIFEST", str(ROOT / "deploy" / "instance.json"))
).resolve()


def _env(name: str, fallback: str) -> str:
    value = str(os.environ.get(name, "") or "").strip()
    return value or fallback


def _git_head() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _git_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def manifest() -> dict[str, Any]:
    name = _env("CTS_G_NAME", "cts-g")
    data_dir = _env("CTS_DATA_DIR", f"/var/lib/{name}")
    etc_dir = _env("ETC_DIR", f"/etc/{name}")
    log_dir = _env("LOG_DIR", f"/var/log/{name}")
    pulse_dir = _env("PULSE_DIR", str(ROOT / "server" / "pulse"))
    return {
        "schema": 1,
        "name": name,
        "root": str(ROOT),
        "pulse_dir": pulse_dir,
        "data_dir": data_dir,
        "etc_dir": etc_dir,
        "log_dir": log_dir,
        "desk_port": int(_env("PORT", "3102")),
        "pulse_port": int(_env("PULSE_PORT", "3015")),
        "repository": _env("REPO_URL", "https://github.com/mxssnx-creator/CTS-G.git"),
        "branch": _env("BRANCH", "main"),
        "git_head": _git_head(),
        "hostname": socket.gethostname(),
        "written_at": int(time.time()),
    }


def write_manifest() -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest(), indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".instance.", dir=str(MANIFEST.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, MANIFEST)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    print(str(MANIFEST))


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "write"
    if action == "write":
        write_manifest()
        return 0
    if action in {"read", "show"}:
        print(MANIFEST.read_text(encoding="utf-8"), end="")
        return 0
    if action == "check":
        current = json.loads(MANIFEST.read_text(encoding="utf-8"))
        expected = manifest()
        for key in ("schema", "name", "root", "pulse_dir", "data_dir", "etc_dir", "log_dir"):
            if current.get(key) != expected.get(key):
                raise SystemExit(f"manifest mismatch: {key}")
        return 0
    raise SystemExit(f"usage: {Path(argv[0]).name} [write|read|check]")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
