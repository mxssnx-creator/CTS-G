"""Bounded Redis reads shared by the engine, HTTP recovery and installer.

No credential values are printed. This module performs no reads on import.
"""
from __future__ import annotations
import re
import subprocess
import sys

def read_output(args: list[str], timeout: float) -> str | None:
    """Read once with a deadline. Error replies are never configuration."""
    try:
        p = subprocess.run(["redis-cli", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = p.stdout or ""
    if p.returncode != 0 or output.lstrip().startswith((
            "(error)", "ERR ", "NOAUTH ", "WRONGTYPE ", "NOPERM ", "DENIED ", "OOM ",
            "LOADING ", "BUSY ", "MISCONF ", "CLUSTERDOWN ", "MASTERDOWN ",
            "READONLY ", "MOVED ", "ASK ", "TRYAGAIN ")):
        return None
    return output



def credentials_available(connection_key: str) -> bool:
    for field in ("api_key", "api_secret"):
        output = read_output(["HGET", connection_key, field], 6)
        if output is None or output.strip() in ("", "(nil)"):
            return False
    return True


if __name__ == "__main__":
    if len(sys.argv) != 2 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", sys.argv[1]):
        raise SystemExit(2)
    raise SystemExit(0 if credentials_available("connection:" + sys.argv[1]) else 1)
