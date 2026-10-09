"""Stage 1 gates: the engine's eligibility rules at their boundaries, one rule per check.

PF gate: gate PF >= setMinPf over the last setGateWindow orders, judged only with at least setGateMinTrades.
DDT gate: the longest drawdown over the last N positions, from the last equity high until net is back at or above
          that high, must be strictly LOWER than setMaxDdtHours (default 12 h).
Lock: a locked Set is never eligible. Open drawdowns are measured to the clock, not cut short by idle time.
"""
from __future__ import annotations

import sys

from common import PULSE, Skip, overlay  # noqa: F401

if PULSE not in sys.path:
    sys.path.insert(0, PULSE)

from set_engine import SetBook, drawdown_time  # noqa: E402

HOUR = 3600.0
# epoch origin for the fixtures: drawdown_time ignores rows at t <= 0 by design, so a peak at t = 0 would be dropped
O = 1_700_000_000.0


def _book(**extra):
    b = SetBook()
    b.load(overlay(**extra))
    b.cost_pct = 0.15
    return b


def _set(b):
    st = next(s for s in b.by_idx if s.kind == "base")
    st.active = True
    st.locked = False
    st.gate_n = b.gate_min
    st.gate_pf = b.min_pf
    st.max_dd_s = 0.0
    return st


def pf_gate_boundary():
    """PF exactly at setMinPf with the minimum sample is eligible; one order short, or just below, is not."""
    b = _book(setMinPf=1.10, setGateMinTrades=30, setGateWindow=50)
    st = _set(b)
    st.gate_pf, st.gate_n = 1.10, 30
    at = b.is_eligible(st)
    st.gate_pf = 1.0999
    below = b.is_eligible(st)
    st.gate_pf, st.gate_n = 1.10, 29
    short = b.is_eligible(st)
    ok = at and not below and not short
    return ok, f"at_1.10_n30={at} below_1.0999={below} at_1.10_n29={short}"


def ddt_gate_boundary():
    """A longest drawdown of 11.9 h is eligible at a 12 h threshold; 12.0 h is not (strictly lower)."""
    b = _book(setMaxDdtHours=12)
    st = _set(b)
    peak = {"t": O, "pnl": 1.0}
    # drawdown starts at the peak (O), net is back at the peak after the given time
    rec_119 = [peak, {"t": O + 11.0 * HOUR, "pnl": -0.2}, {"t": O + 11.9 * HOUR, "pnl": 0.2}]
    rec_120 = [peak, {"t": O + 11.0 * HOUR, "pnl": -0.2}, {"t": O + 12.0 * HOUR, "pnl": 0.2}]
    d119 = drawdown_time(rec_119)["maxS"]
    d120 = drawdown_time(rec_120)["maxS"]
    st.max_dd_s = d119
    eligible_119 = b.is_eligible(st)
    st.max_dd_s = d120
    eligible_120 = b.is_eligible(st)
    ok = abs(d119 - 11.9 * HOUR) < 1 and abs(d120 - 12.0 * HOUR) < 1 and eligible_119 and not eligible_120
    return ok, f"dd119h={d119 / HOUR:.2f} dd120h={d120 / HOUR:.2f} eligible119={eligible_119} eligible120={eligible_120}"


def ddt_open_drawdown_to_clock():
    """An open drawdown keeps counting to the clock; a long idle gap does not shorten it."""
    rows = [{"t": O, "pnl": 1.0}, {"t": O + 1.0 * HOUR, "pnl": -0.3}]
    at_clock = drawdown_time(rows, now=O + 6.0 * HOUR)["maxS"]
    stale = drawdown_time(rows, now=O + 40.0 * HOUR)["maxS"]
    ok = abs(at_clock - 6.0 * HOUR) < 1 and abs(stale - 40.0 * HOUR) < 1
    return ok, f"at_6h={at_clock / HOUR:.2f} at_40h={stale / HOUR:.2f}"


def lock_blocks_at_once():
    """A locked Set is not eligible, before any replay rescores it."""
    b = _book()
    st = _set(b)
    unlocked = b.is_eligible(st)
    st.locked = True
    locked = b.is_eligible(st)
    ok = unlocked and not locked
    return ok, f"unlocked={unlocked} locked={locked}"


CHECKS = [
    ("gates.pf-gate-boundary", pf_gate_boundary),
    ("gates.ddt-gate-strictly-lower", ddt_gate_boundary),
    ("gates.ddt-open-drawdown-to-clock", ddt_open_drawdown_to_clock),
    ("gates.lock-blocks-at-once", lock_blocks_at_once),
]


if __name__ == "__main__":
    bad = 0
    for name, fn in CHECKS:
        ok, detail = fn()
        bad += 0 if ok else 1
        print(("PASS " if ok else "FAIL ") + name, detail)
    sys.exit(1 if bad else 0)
