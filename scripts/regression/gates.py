"""Stage 1 gates: the engine's eligibility rules at their boundaries, one rule per check.

PF gate: gate PF >= setMinPf over the last setGateWindow orders, judged only with at least setGateMinTrades (0 = off, the
         default: a Set is judged on the orders it has; a Set without a closed order is never judged).
DDT gate: the longest drawdown over the last N positions, from the last equity high until net is back at or above
          that high, must be strictly LOWER than setMaxDdtHours (default 24 h).
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
    b.load(overlay(**{"mainGateEnabled": False, "stagesEnabled": False, **extra}))     # these fixtures judge the Base gate alone
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


def minimum_order_count_off_judges_on_available_orders():
    """setGateMinTrades 0 (the default, owner 2026-10-29) switches the minimum order count off: a Set with 5 orders at the minimum PF is
    eligible, the same Set under a minimum of 25 is "n<min", a Set with no closed order is never eligible, and the setting is taken as it
    is (no floor of 8, no replacement of an explicit 0 by a default). The adaptive pre-history does not read a Set with a few orders as
    short while the minimum is off."""
    def reason(n, pf, **ov):
        b = _book(setMinPf=1.25, setGateWindow=30, **ov)
        st = _set(b)
        st.gate_n, st.gate_pf = n, pf
        return b.gate_reason(st), b.gate_min
    off, off_min = reason(5, 2.0, setGateMinTrades=0)
    absent_ov = overlay(mainGateEnabled=False, setGateWindow=30)
    absent_ov.pop("setGateMinTrades", None)
    ab = SetBook()
    ab.load(absent_ov)
    on, on_min = reason(5, 2.0, setGateMinTrades=25)
    none_, _ = reason(0, 0.0, setGateMinTrades=0)
    five = SetBook()
    five.load(overlay(setGateMinTrades=5, setGateWindow=30))
    bare = SetBook()
    bare.load({})
    # adaptive pre-history: 30 Sets that traded with 3 orders each are short under a minimum of 25, not while the minimum is off
    def grown(**ov):
        b = _book(histLookbackAuto=True, histLookbackBars=500, **ov)
        for st in b.by_idx[:30]:
            st.source_n, st.gate_n = 3, 3
        l0 = b.lookback
        b._adapt_lookback()
        return b.lookback - l0
    g_off, g_on = grown(setGateMinTrades=0), grown(setGateMinTrades=25)
    ok = (off == "" and off_min == 1 and on == "n<min" and on_min == 25 and none_ == "n<min" and ab.gate_min == 1 and five.gate_min == 5
          and bare.gate_min == 1 and g_off == 0 and g_on > 0)
    return ok, f"5 orders: off={off!r} min=25 {on!r}; no order={none_!r}; absent key -> {ab.gate_min}; explicit 5 -> {five.gate_min}; bare load -> {bare.gate_min}; lookback growth off {g_off} / min 25 {g_on}"


def default_max_drawdown_time_is_24_hours():
    """The longest drawdown a Set may show over its gate window defaults to 24 h (owner 2026-10-29): the engine without the key, the four
    shipped overlays and the desk default all say so; 12 h and 36 h stay settable (2..36 in steps of 2)."""
    import json, os, re
    bare = SetBook()
    bare.load({})
    o12, o36, odd = SetBook(), SetBook(), SetBook()
    o12.load({"setMaxDdtHours": 12})
    o36.load({"setMaxDdtHours": 36})
    odd.load({"setMaxDdtHours": 49})
    shipped = {n: json.load(open(os.path.join(PULSE, n), encoding="utf-8")).get("setMaxDdtHours")
               for n in ("overlay-bingx-x01.json", "overlay-bingx-x02.json", "overlay-bybit-x01.json", "overlay-bybit-x02.json")}
    from common import ROOT
    src = open(os.path.join(ROOT, "src", "lib", "config-model.ts"), encoding="utf-8").read()
    desk = re.findall(r"setMaxDdtHours: (?:num\(cts\.setMaxDdtHours, )?(\d+)", src)
    ok = (bare.max_ddt_h == 24.0 and bare.max_dd_s == 24 * HOUR and o12.max_ddt_h == 12.0 and o36.max_ddt_h == 36.0 and odd.max_ddt_h == 36.0
          and set(shipped.values()) == {24} and desk == ["24", "24"])
    return ok, f"engine default {bare.max_ddt_h:g} h; 12 -> {o12.max_ddt_h:g}, 36 -> {o36.max_ddt_h:g}, 49 -> {odd.max_ddt_h:g}; overlays {sorted(set(shipped.values()))}; desk {desk}"


CHECKS = [
    ("gates.minimum-order-count-off-judges-on-available-orders", minimum_order_count_off_judges_on_available_orders),
    ("gates.default-max-drawdown-time-is-24-hours", default_max_drawdown_time_is_24_hours),
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
