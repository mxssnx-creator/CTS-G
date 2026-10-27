"""Extra indicator suite: hand-computed values, windowing, output domain, signal fixtures and the engine wiring.

The fixtures are built so that one indicator must fire in one direction; the mirrored fixture must fire the other
way, so a check cannot pass by returning a constant.
"""
from __future__ import annotations

from common import SMALL, T0, overlay, synth_bars  # noqa: F401
import indication_extra as ix
from set_engine import SetBook, indication_signal


def _rows(closes, vol=1.0, spread=0.05):
    out, prev = [], closes[0]
    for c in closes:
        o = prev
        out.append([o, max(o, c) + spread, min(o, c) - spread, c, vol])
        prev = c
    return out


def hand_computed_rsi_and_atr():
    rs = ix._rsi_series([10.0, 12.0, 11.0, 13.0], 2)
    rows = [[10, 11, 9, 10, 1], [10, 12, 10, 11, 1], [11, 11.5, 10.5, 11, 1]]
    tr = ix._true_ranges(rows)            # 2, max(2, |12-10|, |10-10|)=2, max(1, |11.5-11|, |10.5-11|)=1
    ok = (abs(rs[2] - 66.6667) < 1e-3 and abs(rs[3] - 85.7143) < 1e-3 and tr == [2.0, 2.0, 1.0]
          and abs(ix._atr(rows, 3) - 5.0 / 3.0) < 1e-12)
    return ok, f"rsi2={rs[2]:.4f},{rs[3]:.4f} tr={tr} atr={ix._atr(rows, 3):.4f}"


def only_the_tail_is_read():
    bars = synth_bars(11, 600, drift=0.0003)
    bad = []
    for name, fn in ix.INDICATORS.items():
        for m in range(ix.LOOKBACK + 5, 600, 37):
            if fn(bars[:m]) != fn(bars[m - ix.LOOKBACK : m]):
                bad.append((name, m))
                break
    return not bad, f"indicators={len(ix.INDICATORS)} reading beyond the window: {bad}"


def outputs_stay_in_their_domain():
    bad, fired = [], {}
    for seed in (3, 4, 5):
        bars = synth_bars(seed, 700, drift=0.0005 if seed % 2 else -0.0005)
        for i in range(120, 700, 3):
            for name, fn in ix.INDICATORS.items():
                s = fn(bars[i - 150 : i + 1])
                if s is None:
                    continue
                fired[name] = fired.get(name, 0) + 1
                if s[0] not in (1, -1) or not (0.5 <= s[1] <= 0.95):
                    bad.append((name, s))
    return not bad, f"fired={sum(fired.values())} across {len(fired)} indicators; out of domain={bad[:3]}"


def fixtures_fire_both_ways():
    flat = [100.0] * 40
    res = {}
    up = _rows(flat + [100.0 + 0.8])
    for r in up[:-1]:
        r[4] = 1.0
    up[-1][4] = 3.0
    dn = _rows(flat + [100.0 - 0.8])
    for r in dn[:-1]:
        r[4] = 1.0
    dn[-1][4] = 3.0
    res["donchian_break"] = (ix.donchian_break(up), ix.donchian_break(dn))
    base = [100.0 + 0.02 * i for i in range(70)]
    drop = [base[-1] - 0.15, base[-1] - 0.40]
    res["rsi2_reversion"] = (ix.rsi2_reversion(_rows(base + drop, 0.01)),
                             ix.rsi2_reversion(_rows([300.0 - x for x in base + drop], 0.01)))
    # engulfing: three down bars, then a large bar that engulfs
    d3 = [[100.4, 100.45, 100.0, 100.1, 1], [100.1, 100.15, 99.8, 99.85, 1], [99.85, 99.9, 99.5, 99.55, 1]]
    lead = _rows([100.0 + 0.0 * i for i in range(30)])
    long_fx = lead + d3 + [[99.4, 100.6, 99.35, 100.55, 2]]
    u3 = [[99.6, 99.9, 99.55, 99.85, 1], [99.85, 100.2, 99.8, 100.15, 1], [100.15, 100.5, 100.1, 100.45, 1]]
    short_fx = lead + u3 + [[100.6, 100.65, 99.4, 99.45, 2]]
    res["engulfing"] = (ix.engulfing(long_fx), ix.engulfing(short_fx))
    # streak: six rising closes on falling volume -> fade short; six falling -> long
    rise = _rows([100 + 0.1 * i for i in range(30)] + [103 + 0.1 * i for i in range(7)])
    for k, r in enumerate(rise[-6:]):
        r[4] = 6.0 - k
    fall = _rows([100 - 0.1 * i for i in range(30)] + [97 - 0.1 * i for i in range(7)])
    for k, r in enumerate(fall[-6:]):
        r[4] = 6.0 - k
    res["streak_exhaustion"] = (ix.streak_exhaustion(fall), ix.streak_exhaustion(rise))
    bad = {k: v for k, v in res.items() if not (v[0] and v[0][0] == 1 and v[1] and v[1][0] == -1)}
    return not bad, f"checked={list(res)} wrong={ {k: bad[k] for k in bad} }"


def a_failing_indicator_is_isolated():
    saved = dict(ix.INDICATORS)
    try:
        ix.INDICATORS["boom"] = lambda rows: 1 / 0
        ix.INDICATORS["yes"] = lambda rows: (1, 0.7)
        out = ix.extra_votes([[1, 1, 1, 1, 1]] * 5, ["boom", "yes", "unknown"])
    finally:
        ix.INDICATORS.clear()
        ix.INDICATORS.update(saved)
    return out == [(1, 0.7, "yes")], f"votes={out}"


def the_overlay_list_reaches_the_vote_and_default_is_off():
    b = SetBook()
    b.load(overlay())
    off = b.ind_settings["extraIndicators"]
    b.load(overlay(extraIndicators=["donchian_break", "nope", "donchian_break"]))
    on = b.ind_settings["extraIndicators"]
    flat = _rows([100.0] * 120)
    flat[-1] = [100.0, 100.9, 100.0, 100.85, 3.0]
    s_on = indication_signal(flat, dict(b.ind_settings), T0)
    s_off = indication_signal(flat, dict(b.ind_settings, extraIndicators=[]), T0)
    ok = off == [] and on == ["donchian_break"] and "donchian_break" in s_on[2] and "donchian_break" not in s_off[2]
    return ok, f"default={off} list={on} on={s_on} off={s_off}"


CHECKS = [
    ("extra.hand-computed-rsi-and-atr", hand_computed_rsi_and_atr),
    ("extra.only-the-tail-is-read", only_the_tail_is_read),
    ("extra.outputs-stay-in-domain", outputs_stay_in_their_domain),
    ("extra.fixtures-fire-both-ways", fixtures_fire_both_ways),
    ("extra.failing-indicator-isolated", a_failing_indicator_is_isolated),
    ("extra.overlay-list-reaches-vote", the_overlay_list_reaches_the_vote_and_default_is_off),
]
