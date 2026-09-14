#!/usr/bin/env python3
"""CLI wrapper for the independent historic test (desk-side, engines untouched)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server", "pulse"))

from hist_test import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
