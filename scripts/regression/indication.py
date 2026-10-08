"""Indication suite: causal signals, the timeframe lanes and their overlay keys, and the combined vote.

The wiring check reads the settings the set signal really receives. It guards a defect found by the
indication sweep: tf5m / tf15m / tfCombined / tfMinAgree were not in the set engine's indication settings.
"""
from __future__ import annotations

from common import T0, overlay, synth_bars  # noqa: F401
from indication_engine import DEFAULT_SETTINGS, aggregate_bars, combine_timeframes, timeframe_evals
from set_engine import SetBook, pack_signals


def causal_signals_ignore_the_future():
    b = SetBook()
    b.load(overlay())
    bars = synth_bars(5, 420, drift=0.0003)
    m = 330
    full = pack_signals(bars, b.packs, b.ind_settings, T0, 30)
    head = pack_signals(bars[:m], b.packs, b.ind_settings, T0, 30)
    bad = [(p, i) for p in b.packs for i in range(30, m) if full[p][i] != head[p][i]]
    return not bad, f"packs={len(b.packs)} bars_compared={m - 30} mismatches={len(bad)}"


def timeframe_keys_reach_the_set_signal():
    ov = overlay(tfMinAgree=3, tf5m=False, tf15m=True, tfCombined=False)
    b = SetBook()
    b.load(ov)
    got = {k: b.ind_settings.get(k) for k in ("tfMinAgree", "tf5m", "tf15m", "tfCombined")}
    want = {"tfMinAgree": 3, "tf5m": False, "tf15m": True, "tfCombined": False}
    ok = got == want
    return ok, f"set signal sees {got} want {want}"


def timeframe_flag_removes_the_lane():
    settings = dict(DEFAULT_SETTINGS)
    settings["tf5m"] = False
    seen = set()
    for seed in range(12):
        bars = synth_bars(40 + seed, 400, drift=0.0006)
        rows = {"1m": bars[-60:], "5m": aggregate_bars(bars, 5), "15m": aggregate_bars(bars, 15)}
        for ev in timeframe_evals(rows, settings, T0):
            seen.add(ev.source_id)
    ok = "bingx-5m" not in seen
    return ok, f"lanes seen with tf5m off: {sorted(seen) or 'none'}"


def combined_vote_needs_min_agree():
    bars = synth_bars(77, 400, drift=0.0008)
    rows = {"1m": bars[-60:], "5m": aggregate_bars(bars, 5), "15m": aggregate_bars(bars, 15)}
    evs = timeframe_evals(rows, dict(DEFAULT_SETTINGS), T0)
    too_many = combine_timeframes(evs, len(evs) + 1, dict(DEFAULT_SETTINGS))
    ok = too_many is None
    return ok, f"lanes={len(evs)} min_agree={len(evs) + 1} -> {too_many}"


def signal_is_deterministic():
    b = SetBook()
    b.load(overlay())
    bars = synth_bars(9, 400, drift=0.0003)
    a = pack_signals(bars, b.packs, b.ind_settings, T0, 30)
    c = pack_signals(bars, b.packs, b.ind_settings, T0, 30)
    return a == c, "two runs identical" if a == c else "differs between runs"


CHECKS = [
    ("indication.causal-signals-ignore-future", causal_signals_ignore_the_future),
    ("indication.timeframe-keys-reach-set-signal", timeframe_keys_reach_the_set_signal),
    ("indication.timeframe-flag-removes-lane", timeframe_flag_removes_the_lane),
    ("indication.combined-vote-needs-min-agree", combined_vote_needs_min_agree),
    ("indication.signal-deterministic", signal_is_deterministic),
]
