"""Shared harness for the regression suites.

Every suite module exposes CHECKS: a list of (name, fn). A check returns (ok, detail), or raises Skip when
it cannot run here (for example git objects are missing from a shallow clone). Any other exception is a
failure. Synthetic data is seeded, so every run replays the same bars.

CTSG_PULSE_DIR points the suites at another copy of server/pulse (used for fault injection).
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from typing import Any, Callable, Dict, List, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
PULSE = os.path.abspath(os.environ.get("CTSG_PULSE_DIR") or os.path.join(ROOT, "server", "pulse"))
if PULSE not in sys.path:
    sys.path.insert(0, PULSE)

T0 = float((1_700_000_000 // 60) * 60)     # minute-aligned origin for every replay
COST = 0.15                                 # PositionCost in percent, the engine default
SMALL = {                                   # a small Set grid for checks that test behaviour, not grid size
    "setSlRatios": [0.5, 1.0],
    "setMinStep": 2,
    "setStepMax": 4,
    "trailVariants": ["0.3:0.1"],
}


class Skip(Exception):
    """The check cannot run in this environment."""


CheckFn = Callable[[], Tuple[bool, str]]


def synth_bars(seed: int, count: int, start: float = 50.0, drift: float = 0.0, vol: float = 0.002) -> List[List[float]]:
    """Deterministic 1-minute OHLCV rows [o, h, l, c, v] with a random walk and optional drift."""
    r = random.Random(seed)
    px = start
    rows: List[List[float]] = []
    for _ in range(count):
        o = px
        hi = o * (1 + abs(r.gauss(drift, vol)))
        lo = o * (1 - abs(r.gauss(-drift, vol)))
        c = lo + (hi - lo) * r.random()
        px = c
        rows.append([o, hi, lo, c, 1.0])
    return rows


def overlay(name: str = "overlay-bingx-x01.json", **extra: Any) -> Dict[str, Any]:
    ov = json.load(open(os.path.join(PULSE, name)))
    ov.update(extra)
    return ov


def git(*args: str, no_match_ok: bool = False) -> str:
    """Run git in the repo. Exit 1 from grep-style commands means "no match"; it is not an error."""
    try:
        out = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True, timeout=120)
    except FileNotFoundError as exc:
        raise Skip("git is not installed") from exc
    if out.returncode == 1 and no_match_ok:
        return ""
    if out.returncode != 0:
        raise Skip(f"git {' '.join(args)} failed ({out.returncode}): {out.stderr.strip()[:120]}")
    return out.stdout


def lane_cfg(**over: Any) -> Dict[str, Any]:
    cfg = {"entry_conf": 0.5, "time_bars": 10, "scratch_bars": 5, "scratch_min": 0.001,
           "cooldown": 2, "cost_pct": COST, "honor_tp": True}
    cfg.update(over)
    return cfg


def fmt_pairs(pairs: Sequence[Tuple[str, bool]]) -> str:
    return " ".join(f"{k}={'ok' if v else 'NO'}" for k, v in pairs)
