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


def stage_short_window_judges_the_orders_it_has():
    """The Base short window (setGateShortN) is judged on the orders a Set has: a Set with 8 orders against a short window of 15 passes it at the
    minimum PF (the long window already judges those orders), a PF under the minimum fails it, and with stageFullWindow it needs its 15 orders."""
    def reason(full, n, pf):
        b = SetBook()
        b.load(overlay(stagesEnabled=True, mainGateEnabled=False, realGateEnabled=False, mainNegEnabled=False, setGateShortN=15, setGateWindow=50,
                       setMinPf=1.25, stageFullWindow=full))
        st = _set(b)
        st.gate_n, st.gate_pf, st.max_dd_s = n, 2.0, 0.0
        st.base_short_n, st.base_short_pf = n, pf
        return b.gate_reason(st)
    avail, low, strict, full15 = reason(False, 8, 2.0), reason(False, 8, 1.0), reason(True, 8, 2.0), reason(True, 15, 2.0)
    ok = avail == "" and low == "short" and strict == "short" and full15 == ""
    return ok, f"8 orders: available={avail!r} PF 1.0={low!r}; strict 8 orders={strict!r}, strict 15 orders={full15!r}"


def stage_windows_accept_one_and_two():
    """The ongoing (Real) window may be one result and the Negative windows the last one or two results (owner 2026-10-29): realGateN 1 is read as 1
    (not raised to 3), mainNegWindows "1,2" and [2, 1] as (1, 2), a value under 1 as 1; the Main static window keeps its floor of 8."""
    def load(**ov):
        b = SetBook()
        b.load(overlay(**ov))
        return b
    a, b2, c = load(realGateN=1, mainNegWindows="1,2"), load(realGateN=0, mainNegWindows=[2, 1, 2]), load(mainGateN=1)
    ok = a.realst_n == 1 and a.neg_windows == (1, 2) and b2.realst_n == 1 and b2.neg_windows == (1, 2) and c.main_n == 8
    return ok, f"Real 1 -> {a.realst_n}, Negative '1,2' -> {a.neg_windows}; Real 0 -> {b2.realst_n}, [2,1,2] -> {b2.neg_windows}; Main static 1 -> {c.main_n}"


def short_window_is_at_most_half_of_the_long_window():
    """The Base short window is capped at setGateShortMaxRatio (default 0.5) of the long window (owner 2026-10-29): long 40 allows at most 20, long 50 allows
    the default 25 (also the most it can be), a ratio of 0.3 caps long 40 at 12, a ratio of 1 at the long window, 0 stays off, the ratio is held to 0.1..1."""
    def short(**ov):
        b = SetBook()
        b.load(overlay(**{"stagesEnabled": True, **ov}))
        return b.base_short
    got = {"40/25": short(setGateWindow=40, setGateShortN=25), "40/20": short(setGateWindow=40, setGateShortN=20), "50/default": short(setGateWindow=50),
           "50/40": short(setGateWindow=50, setGateShortN=40), "40 ratio .3": short(setGateWindow=40, setGateShortN=25, setGateShortMaxRatio=0.3),
           "40 ratio 1": short(setGateWindow=40, setGateShortN=25, setGateShortMaxRatio=1.0), "40 ratio 9": short(setGateWindow=40, setGateShortN=39, setGateShortMaxRatio=9),
           "off": short(setGateWindow=40, setGateShortN=0), "20/15": short(setGateWindow=20, setGateShortN=15), "stages off": short(setGateWindow=40, setGateShortN=20, stagesEnabled=False)}
    want = {"40/25": 20, "40/20": 20, "50/default": 25, "50/40": 25, "40 ratio .3": 12, "40 ratio 1": 25, "40 ratio 9": 39, "off": 0, "20/15": 10, "stages off": 0}
    bare = SetBook()                                           # no overlay at all: the engine's own default share is 0.5
    bare.load({"setGateWindow": 40, "setGateShortN": 25})
    got["engine default"] = bare.base_short
    want["engine default"] = 20
    return got == want, f"{got}"


def main_short_windows_are_at_most_half_of_the_long_window():
    """The Main short windows (the former Auto window) are capped at mainShortMaxRatio (default 0.5) of the Main long window (owner 2026-10-29): long 25 keeps
    short 5 and 10 and drops 15, long 40 keeps up to 20, long 8 (cap 4) falls back to the cap itself, a ratio of 1 keeps every candidate, the ratio is held
    to 0.1..1. The earlier mainAuto* keys still load when their mainShort* twin is absent, and the new key wins when both are given."""
    def shorts(**ov):
        b = SetBook()
        b.load(overlay(**{"stagesEnabled": True, "setGateWindow": 50, **ov}))
        return b.short_windows
    got = {"25 default": shorts(mainGateN=25, mainShortWindows=[5, 10, 15]), "40": shorts(mainGateN=40, mainShortWindows=[5, 10, 15, 20, 25]),
           "8": shorts(mainGateN=8, mainShortWindows=[5, 10, 15]), "25 ratio 1": shorts(mainGateN=25, mainShortWindows=[5, 10, 15], mainShortMaxRatio=1.0),
           "25 ratio .3": shorts(mainGateN=25, mainShortWindows=[5, 10, 15], mainShortMaxRatio=0.3), "shipped": shorts(),
           "legacy keys": shorts(mainGateN=25, mainAutoWindows=[5, 10, 15], mainAutoMaxRatio=0.3),
           "new key wins": shorts(mainGateN=25, mainShortWindows=[5, 10], mainAutoWindows=[5]) }
    want = {"25 default": (5, 10), "40": (5, 10, 15, 20), "8": (4,), "25 ratio 1": (5, 10, 15), "25 ratio .3": (5,), "shipped": (5, 10),
            "legacy keys": (5,), "new key wins": (5, 10)}
    bare = SetBook()                                           # no overlay at all: the engine's own default share is 0.5
    bare.load({"mainGateN": 25, "mainShortWindows": [5, 10, 15]})
    got["engine default"] = bare.short_windows
    want["engine default"] = (5, 10)
    leg = SetBook()
    leg.load(overlay(mainAutoEnabled=False, mainAutoMin=5, mainAutoMargin=1.1))
    new = SetBook()
    new.load(overlay(mainShortEnabled=True, mainShortMin=6, mainShortMargin=1.2, mainAutoEnabled=False, mainAutoMin=5))
    got["legacy enabled/min/margin"] = (leg.short_on, leg.short_min, leg.short_margin)
    want["legacy enabled/min/margin"] = (False, 5, 1.1)
    got["new enabled/min/margin win"] = (new.short_on, new.short_min, new.short_margin)
    want["new enabled/min/margin win"] = (True, 6, 1.2)
    return got == want, f"{ {k: v for k, v in got.items() if v != want[k]} or 'every case as asked'}"


def main_minimal_window_positive_and_negative():
    """The Main minimal window (mainMinN 1-3, mainMinMode), a third last-N next to the Main long and short window: the newest results of the Set's Main Set
    (the orders it opened while Base passed) must all be positive (or all negative) after PositionCost. Judged through _score_one and gate_reason on a tape
    whose Base and Main long windows pass throughout, so the minimal window is the only stage that can stop it; off by default; the count is held to 0..3
    and to the smallest short window; a Main Set with fewer results is judged on the ones it has and, with stageFullWindow, is not; reason "mainminimal"."""
    from set_engine import StageParams, stage_verdicts
    def reason(nets, **ov):
        b = SetBook()
        b.load(overlay(**{"stagesEnabled": True, "mainGateEnabled": True, "mainGateN": 8, "mainShortEnabled": False, "realGateEnabled": False, "mainNegEnabled": False,
                          "setGateShortN": 0, "setGateWindow": 50, "setGateMinTrades": 1, "setMinPf": 0.01, "setMaxDdtHours": 36, **ov}))
        st = _set(b)
        cf = b.cost_pct / 100.0
        st.hist = [{"t": 1000.0 + 60.0 * i, "pnl": x, "pnl_pct": x + cf, "hold_s": 30.0, "reason": "tp", "symbol": "X", "side": "LONG"} for i, x in enumerate(nets)]
        st.live = []
        b._score_one(st, now=1000.0 + 60.0 * len(nets))
        return b.gate_reason(st)
    W, L = 0.006, -0.004
    base = [W, W, W, W, W, W, W, W, L, W, W, W]               # Base and the Main long window pass at the end of this
    cases = {
        "off by default": reason(base + [L]),
        "+1 last win": reason(base + [W], mainMinN=1, mainMinMode="positive"),
        "+1 last loss": reason(base + [L], mainMinN=1, mainMinMode="positive"),
        "+2 last win, loss before": reason(base + [L, W], mainMinN=2, mainMinMode="positive"),
        "+2 last two wins": reason(base + [L, W, W], mainMinN=2, mainMinMode="positive"),
        "-1 last loss": reason(base + [W, L], mainMinN=1, mainMinMode="negative"),
        "-1 last win": reason(base + [W], mainMinN=1, mainMinMode="negative"),
        "-2 last two losses": reason(base + [W, L, L], mainMinN=2, mainMinMode="negative"),
        "-3 loss loss win": reason(base + [W, L, L, W], mainMinN=3, mainMinMode="negative"),
        "-3 three losses": reason(base + [W, W, L, L, L], mainMinN=3, mainMinMode="negative"),
        "count held to 3 (9 -> 3)": reason(base + [W, W, W], mainMinN=9, mainMinMode="positive"),
        "Main stage off": reason(base + [L], mainMinN=1, mainMinMode="positive", mainGateEnabled=False),
        "stages off": reason(base + [L], mainMinN=1, mainMinMode="positive", stagesEnabled=False),
    }
    want = {"off by default": "", "+1 last win": "", "+1 last loss": "mainminimal", "+2 last win, loss before": "mainminimal", "+2 last two wins": "",
            "-1 last loss": "", "-1 last win": "mainminimal", "-2 last two losses": "", "-3 loss loss win": "mainminimal", "-3 three losses": "",
            "count held to 3 (9 -> 3)": "", "Main stage off": "", "stages off": ""}
    b9, b0, bs = SetBook(), SetBook(), SetBook()
    b9.load(overlay(stagesEnabled=True, mainMinN=9))
    b0.load(overlay())
    bs.load(overlay(stagesEnabled=True, mainGateN=25, mainShortWindows=[2], mainMinN=3))     # the smallest short window (2) caps the minimal window
    bo = SetBook()
    bo.load(overlay(stagesEnabled=True, mainGateEnabled=False, mainMinN=2))
    # the verdict on its own: two Main results against a window of 3, the results it has or the strict rule
    P = StageParams(base_long=50, base_short=0, gate_min=1, floor=1.1, max_dd_s=1e12, lookback_s=1e12, consistency=0.0, main_on=True, main_n=8, short_on=False,
                    short_windows=(4,), short_min=8, short_margin=1.02, neg_on=False, neg_windows=(5, 10), real_on=False, real_n=15, full_window=False,
                    main_min_n=3, main_min_positive=True)
    from dataclasses import replace
    v_av = stage_verdicts([W, W], [], [], 8, P)
    v_st = stage_verdicts([W, W], [], [], 8, replace(P, full_window=True))
    ok = (cases == want and b9.main_min_n == 3 and b0.main_min_n == 0 and b0.main_min_positive and bs.main_min_n == 2 and bo.main_min_n == 0
          and v_av["minimal"] and v_av["minimal_n"] == 2 and not v_st["minimal"])
    return ok, (f"{ {k: v for k, v in cases.items() if v != want[k]} or 'every case as asked'}; count 9 -> {b9.main_min_n}; default {b0.main_min_n}; "
                f"capped by short window 2 -> {bs.main_min_n}; Main off -> {bo.main_min_n}; 2 results against 3: available {v_av['minimal']}, strict {v_st['minimal']}")


def base_minimal_window_positive_and_negative():
    """The Base minimal window (setGateMinN 1-3, setGateMinMode): the newest results of the Set's own tape must all be positive (or all negative) after
    PositionCost. Judged on the end of a real tape through _score_one and gate_reason; off by default; the count is held to 0..3; a Set with fewer results than
    the count is judged on the ones it has, and with stageFullWindow it is not; the reason is "minimal"."""
    def reason(nets, **ov):
        b = SetBook()
        b.load(overlay(**{"stagesEnabled": True, "mainGateEnabled": False, "realGateEnabled": False, "mainNegEnabled": False, "setGateShortN": 0,
                          "setGateWindow": 50, "setMinPf": 0.01, "setMaxDdtHours": 36, **ov}))
        st = _set(b)
        cf = b.cost_pct / 100.0
        st.hist = [{"t": 1000.0 + 60.0 * i, "pnl": x, "pnl_pct": x + cf, "hold_s": 60.0, "reason": "tp", "symbol": "X", "side": "LONG"} for i, x in enumerate(nets)]
        st.live = []
        b._score_one(st, now=1000.0 + 60.0 * len(nets))
        return b.gate_reason(st)
    W, L = 0.004, -0.004
    base = [W] * 8 + [L] * 4
    cases = {
        "off by default": reason(base + [L], ),
        "+1 last win": reason(base + [W], setGateMinN=1, setGateMinMode="positive"),
        "+1 last loss": reason(base + [L], setGateMinN=1, setGateMinMode="positive"),
        "+2 last win, loss before": reason(base + [L, W], setGateMinN=2, setGateMinMode="positive"),
        "+2 last two wins": reason(base + [L, W, W], setGateMinN=2, setGateMinMode="positive"),
        "-1 last loss": reason(base + [W, L], setGateMinN=1, setGateMinMode="negative"),
        "-1 last win": reason(base + [W], setGateMinN=1, setGateMinMode="negative"),
        "-2 last two losses": reason(base + [W, L, L], setGateMinN=2, setGateMinMode="negative"),
        "-3 loss loss win": reason(base + [W, L, L, W], setGateMinN=3, setGateMinMode="negative"),
        "-3 three losses": reason(base + [W, L, L, L], setGateMinN=3, setGateMinMode="negative"),
        "count held to 3 (9 -> 3)": reason(base + [W, W, W], setGateMinN=9, setGateMinMode="positive"),
        "two results, N 3, available": reason([W, W], setGateMinN=3, setGateMinMode="positive"),
        "two results, N 3, full window": reason([W, W], setGateMinN=3, setGateMinMode="positive", stageFullWindow=True),
        "stages off": reason(base + [L], setGateMinN=1, setGateMinMode="positive", stagesEnabled=False),
    }
    want = {"off by default": "", "+1 last win": "", "+1 last loss": "minimal", "+2 last win, loss before": "minimal", "+2 last two wins": "",
            "-1 last loss": "", "-1 last win": "minimal", "-2 last two losses": "", "-3 loss loss win": "minimal", "-3 three losses": "",
            "count held to 3 (9 -> 3)": "", "two results, N 3, available": "", "two results, N 3, full window": "minimal", "stages off": ""}
    b9 = SetBook()
    b9.load(overlay(stagesEnabled=True, setGateMinN=9))
    b0 = SetBook()
    b0.load(overlay())
    ok = cases == want and b9.base_min_n == 3 and b0.base_min_n == 0 and b0.base_min_positive
    return ok, f"{ {k: v for k, v in cases.items() if v != want[k]} or 'every case as asked'}; count 9 -> {b9.base_min_n}; default {b0.base_min_n}"


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
    ("gates.base-minimal-window-positive-and-negative", base_minimal_window_positive_and_negative),
    ("gates.main-short-windows-are-at-most-half-of-the-long-window", main_short_windows_are_at_most_half_of_the_long_window),
    ("gates.main-minimal-window-positive-and-negative", main_minimal_window_positive_and_negative),
    ("gates.short-window-is-at-most-half-of-the-long-window", short_window_is_at_most_half_of_the_long_window),
    ("gates.stage-short-window-judges-the-orders-it-has", stage_short_window_judges_the_orders_it_has),
    ("gates.stage-windows-accept-one-and-two", stage_windows_accept_one_and_two),
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
