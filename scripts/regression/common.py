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
COST = 0.12                                 # PositionCost in percent, as shipped in the overlays
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
    # fixtures judge every window at one floor (setMinPf) unless they ask for the size classes (minPfBySize), which have their own checks
    ov["minPfBySize"] = False
    ov["directionMode"] = "both"  # fixtures run the combined Sets (pinned counts, golden); the direction checks and the split golden ask for "split"
    ov["minPfByStep"] = False     # likewise the per-step floor: fixtures judge every window at setMinPf; the step_pf checks ask for the step floors
    ov["rankByLane"] = False      # the choice among valid Sets reads the Base tape in fixtures (pinned picks, golden); the lane-rank checks ask for the lane's own Set
    ov["holdAuto"] = True         # fixtures keep the hold Auto Mode (pinned counts, golden, the hold checks); the shipped default is off
    ov["setScratchEnabled"] = True  # fixtures keep the Set-lane scratch exit (pinned counts, golden); the scratch checks ask for it off
    ov["exitSetsByPack"] = {}     # fixtures keep every pack's Exit Sets (pinned counts, golden); the shipped overlays switch the indications pack's off
    ov["tpRanges"] = {"wide": False}  # fixtures keep the earlier grid (shipped grid ends at 3.2 %); the range checks switch Wide on themselves
    ov["slMaxByRange"] = {}       # fixtures keep every SL ratio in every TP range (pinned counts, golden); the shipped overlays cap Long / Wide at 1.0
    ov["volumeByPackKind"] = {}   # fixtures size every Set at the volume factor (the shipped overlays: indications trailing x 3)
    ov["setStepAdapt"] = True     # fixtures keep the step-adapt behaviour their checks pin; the shipped default is off
    # fixtures keep the Block reading their pinned counts were written for (the Set's own record, every count at its own PositionCost step, 3 observations); the
    # shipped overlays run Block v2's measured defaults (per-symbol evidence, steps of 3, the first count only observes)
    ov["blockCounts"] = [{"step": 1, "ratio": 1.0, "on": True} for _ in range(3)]
    ov["blockRelations"], ov["blockSkipFirst"], ov["blockMinObservations"] = ["set"], 0, 3
    ov["stratTrend"], ov["stratBreakout"] = False, False   # fixtures keep the packs their pinned counts were written for (general, indications, Short Range); the shipped x01 / x02 run trend + breakout (owner 2026-11-04)
    ov["pauseMode"] = "off"       # fixtures replay without the pause (pinned counts, golden); the pause checks and the auto golden ask for it
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
