"""Functional suite: lane entry and exit rules, cooldown, trailing, and the base gate.

Lanes are driven with hand-built bars so every expected price and reason is known in advance.
"""
from __future__ import annotations

from common import PULSE, lane_cfg  # noqa: F401
from set_engine import SetBook, SetLane


def _book_one(**extra):
    b = SetBook()
    b.load({"histEnabled": True, "stratGeneral": True, "stratIndications": False, "setMinStep": 5,
            "setStepMax": 5, "setSlRatios": [1.0], "trailVariants": ["0.3:0.1"], **extra})
    return b


def _set(book, kind="base"):
    return next(st for st in book.by_idx if st.kind == kind)


def _bar(o, h, l, c):
    return [float(o), float(h), float(l), float(c), 1.0]


def _run(lane, bars, dirs, confs, cfg, allowed=True):
    cand = [i for i in range(len(bars)) if dirs[i] != 0 and confs[i] >= cfg["entry_conf"]]
    return lane.scan(0, len(bars), bars, dirs, confs, allowed, cfg, cand)


def _long_entry_levels():
    b = _book_one()
    st = _set(b)
    close = 100.0
    sl = close * (1 - max(0.0015, st.tp_pct * st.sl_ratio))
    tp = close * (1 + max(0.0020, st.tp_pct))
    return st, close, sl, tp


def sl_wins_same_bar_long():
    st, close, sl, tp = _long_entry_levels()
    bars = [_bar(100, 100, 100, 100), _bar(100, tp + 0.5, sl - 0.5, 100)]
    recs = _run(SetLane(st, "X"), bars, [1, 0], [0.9, 0.0], lane_cfg())
    ok = len(recs) == 1 and recs[0]["reason"] == "sl" and abs(recs[0]["exit_px"] - sl) < 1e-9
    return ok, f"reason={recs[0]['reason'] if recs else None} px={recs[0]['exit_px'] if recs else None:.4f} sl={sl:.4f}" if recs else "no exit"


def tp_when_only_tp_hit_long():
    st, close, sl, tp = _long_entry_levels()
    bars = [_bar(100, 100, 100, 100), _bar(100, tp + 0.2, 100, 100)]
    recs = _run(SetLane(st, "X"), bars, [1, 0], [0.9, 0.0], lane_cfg())
    ok = len(recs) == 1 and recs[0]["reason"] == "tp" and abs(recs[0]["exit_px"] - tp) < 1e-9
    return ok, f"reason={recs[0]['reason'] if recs else None}"


def sl_wins_same_bar_short():
    b = _book_one()
    st = _set(b)
    close = 100.0
    sl = close * (1 + max(0.0015, st.tp_pct * st.sl_ratio))
    tp = close * (1 - max(0.0020, st.tp_pct))
    bars = [_bar(100, 100, 100, 100), _bar(100, sl + 0.5, tp - 0.5, 100)]
    recs = _run(SetLane(st, "X"), bars, [-1, 0], [0.9, 0.0], lane_cfg())
    ok = len(recs) == 1 and recs[0]["reason"] == "sl" and recs[0]["side"] == "SHORT"
    return ok, f"reason={recs[0]['reason'] if recs else None} side={recs[0]['side'] if recs else None}"


def tp_ignored_when_honor_off():
    st, close, sl, tp = _long_entry_levels()
    bars = [_bar(100, 100, 100, 100), _bar(100, tp + 0.2, 100, 100), _bar(100, 100, 100, 100)]
    recs = _run(SetLane(st, "X"), bars, [1, 0, 0], [0.9, 0.0, 0.0], lane_cfg(honor_tp=False))
    ok = not recs
    return ok, f"exits={len(recs)} (no exit: TP ignored, price stayed inside)"


def time_exit_after_time_bars():
    st, close, sl, tp = _long_entry_levels()
    flat = _bar(100.05, 100.1, 99.95, 100.0)
    bars = [_bar(100, 100, 100, 100)] + [flat] * 5
    recs = _run(SetLane(st, "X"), bars, [1, 0, 0, 0, 0, 0], [0.9, 0, 0, 0, 0, 0], lane_cfg(time_bars=3, scratch_bars=99))
    ok = len(recs) == 1 and recs[0]["reason"] == "time" and recs[0]["exit_i"] == 3 and recs[0]["hold_bars"] == 3  # entry bar 0 + time_bars 3
    return ok, f"reason={recs[0]['reason'] if recs else None} exit_i={recs[0]['exit_i'] if recs else None}"


def scratch_exit_on_positive_move():
    st, close, sl, tp = _long_entry_levels()
    up = _bar(100.0, 100.3, 100.0, 100.3)
    bars = [_bar(100, 100, 100, 100), up, up, up]
    recs = _run(SetLane(st, "X"), bars, [1, 0, 0, 0], [0.9, 0, 0, 0], lane_cfg(time_bars=50, scratch_bars=2, scratch_min=0.001))
    ok = len(recs) == 1 and recs[0]["reason"] == "scratch+" and recs[0]["hold_bars"] == 2
    return ok, f"reason={recs[0]['reason'] if recs else None} hold={recs[0]['hold_bars'] if recs else None}"


def cooldown_blocks_reentry():
    st, close, sl, tp = _long_entry_levels()
    bars = [_bar(100, 100, 100, 100), _bar(100, 100, sl - 1, 100)]   # exit by SL at bar 1
    bars += [_bar(100, 100, 100, 100)] * 4
    dirs = [1, 0, 1, 1, 1, 1]
    confs = [0.9, 0.0, 0.9, 0.9, 0.9, 0.9]
    lane = SetLane(st, "X")
    recs = _run(lane, bars, dirs, confs, lane_cfg(cooldown=2))
    ok = len(recs) == 1 and lane.open is not None and lane.open["i"] == 4
    return ok, f"open_at={lane.open['i'] if lane.open else None} (expected 4: bars 2,3 spent cooldown)"


def entry_needs_confidence_and_direction():
    st, close, sl, tp = _long_entry_levels()
    bars = [_bar(100, 100, 100, 100)] * 4
    lane = SetLane(st, "X")
    _run(lane, bars, [1, 1, 0, 1], [0.4, 0.5 - 1e-9, 0.9, 0.0], lane_cfg(entry_conf=0.5))
    ok = lane.open is None
    return ok, "conf below threshold, conf exactly under, dir 0, and conf-only bars never entered"


def entry_long_levels_sit_around_close():
    st, close, sl, tp = _long_entry_levels()
    lane = SetLane(st, "X")
    _run(lane, [_bar(100, 100, 100, 100), _bar(100, 100, 100, 100)], [1, 0], [0.9, 0.0], lane_cfg())
    o = lane.open
    ok = o is not None and abs(o["sl"] - sl) < 1e-9 and abs(o["tp"] - tp) < 1e-9 and o["sl"] < o["entry"] < o["tp"]
    return ok, f"sl={o['sl']:.4f} entry={o['entry']:.4f} tp={o['tp']:.4f}" if o else "no entry"


def trail_arms_then_gives_back():
    b = SetBook()
    b.load({"histEnabled": True, "stratGeneral": True, "stratIndications": False, "setMinStep": 5, "setStepMax": 5,
            "setSlRatios": [3.5], "trailVariants": ["0.3:0.1"]})
    st = next(s for s in b.by_idx if s.kind == "trail")
    lane = SetLane(st, "X")
    bars = [_bar(100, 100, 100, 100), _bar(100, 100.4, 100.0, 100.35), _bar(100.3, 100.31, 100.1, 100.2)]
    recs = _run(lane, bars, [1, 0, 0], [0.9, 0, 0], lane_cfg(time_bars=99, scratch_bars=99))
    # arm 0.3%: peak 100.4 arms the stop at 100.4 * (1 - 0.1%) = 100.2996, the low 100.1 gives it back
    ok = len(recs) == 1 and recs[0]["reason"] == "sl" and abs(recs[0]["exit_px"] - 100.4 * 0.999) < 1e-6
    return ok, f"reason={recs[0]['reason'] if recs else None} px={recs[0]['exit_px'] if recs else None}"


def gate_boundary_and_sample():
    b = _book_one()
    st = _set(b)
    st.active = True
    cases = [
        ("n29", (29, 2.0), False),
        ("n30-pf1.10", (30, 1.10), True),
        ("n30-pf1.0999", (30, 1.0999), False),
        ("n50-pf1.30", (50, 1.30), True),
    ]
    bad = []
    for name, (n, pf), want in cases:
        st.gate_n, st.gate_pf = n, pf
        if bool(b.is_eligible(st)) != want:
            bad.append(name)
    return not bad, f"bad={bad}" if bad else f"{len(cases)} boundary cases"


def gate_window_caps_sample():
    b = _book_one()
    st = _set(b)
    rows = [{"t": T, "symbol": "X", "pnl": 0.001, "pnl_pct": 0.0025, "side": "LONG", "reason": "tp", "hold_s": 60}
            for T in range(60)]
    st.hist = rows
    b._score_one(st, now=1e9)
    ok = st.gate_n == min(50, 60) and st.gate_n == b.gate_window
    return ok, f"gate_n={st.gate_n} window={b.gate_window}"


CHECKS = [
    ("lane.sl-wins-same-bar-long", sl_wins_same_bar_long),
    ("lane.tp-only-hit-long", tp_when_only_tp_hit_long),
    ("lane.sl-wins-same-bar-short", sl_wins_same_bar_short),
    ("lane.tp-ignored-when-honor-off", tp_ignored_when_honor_off),
    ("lane.time-exit-after-time-bars", time_exit_after_time_bars),
    ("lane.scratch-exit-positive-move", scratch_exit_on_positive_move),
    ("lane.cooldown-blocks-reentry", cooldown_blocks_reentry),
    ("lane.entry-confidence-and-direction", entry_needs_confidence_and_direction),
    ("lane.entry-levels-around-close", entry_long_levels_sit_around_close),
    ("lane.trail-arms-then-gives-back", trail_arms_then_gives_back),
    ("gate.boundary-pf-and-sample", gate_boundary_and_sample),
    ("gate.window-caps-sample", gate_window_caps_sample),
]
