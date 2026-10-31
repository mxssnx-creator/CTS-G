#!/usr/bin/env python3
"""The offline parallel runner reports progress without changing result order."""
from __future__ import annotations

from contextlib import redirect_stderr
import io
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / "regression"))
import run as regression  # noqa: E402


def slow():
    time.sleep(0.2)
    return True, "finished"


def main():
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        results = regression._run_parallel([("first", slow), ("second", slow)], 2,
                                           progress_interval_s=0.04, queue_timeout_s=0.02)
    assert [r["name"] for r in results] == ["first", "second"]
    assert all(r["status"] == "PASS" for r in results)
    assert "regression progress:" in stderr.getvalue() and "workers active" in stderr.getvalue()
    print("parallel regression progress: heartbeat and ordered results passed")


if __name__ == "__main__":
    main()
