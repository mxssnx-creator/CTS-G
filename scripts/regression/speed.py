"""Speed without changing a result: the Low-Range state is computed once per bar (not once per tactic) and only for the bandwidths the rank reads, and
Sets that traded the same orders share their stored columns.

Every faster path is read against the plain one: a verbatim copy of the earlier implementation, bit for bit."""
from __future__ import annotations

import math

from common import SMALL, T0, overlay, synth_bars
import indication_extra as ie
import indication_lib as il
import set_engine as se


def _plain_low_range_state(rows, n=24, bw_len=20, rank_len=120, lag=0):
    """The implementation before the memo and the narrowed series (copied, not imported)."""
    r = ie._tail(rows)
    if len(r) < max(n, rank_len // 2) + 12:
        return None
    box = r[-n - 1: -1]
    hi = max(float(b[1]) for b in box)
    lo = min(float(b[2]) for b in box)
    c = float(r[-1][3])
    if lo <= 0 or hi <= lo:
        return None
    atr = ie._atr(r)
    closes = [float(b[3]) for b in r]
    widths = []
    for i in range(bw_len - 1, len(closes)):
        w = closes[i - bw_len + 1: i + 1]
        m = sum(w) / bw_len
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / bw_len)
        widths.append(4.0 * sd / m if m > 0 else 0.0)
    used = widths[: len(widths) - lag] if lag else widths
    hist = used[-rank_len:]
    rank = sum(1 for x in hist if x <= hist[-1]) / len(hist)
    e50 = ie._ema_list(closes, 50)
    slope = (e50[-1] - e50[-11]) / e50[-11] * 100.0 if e50[-11] > 0 else 0.0
    return {
        "hi": hi, "lo": lo, "close": c,
        "width_pct": (hi - lo) / lo * 100.0,
        "width_atr": (hi - lo) / atr if atr > 0 else 99.0,
        "pos": (c - lo) / (hi - lo),
        "bw_rank": rank,
        "slope_pct": slope,
        "atr_pct": atr / c * 100.0 if c > 0 else 0.0,
    }


def low_range_state_equals_the_plain_series_bit_for_bit():
    bars = synth_bars(71, 900, vol=0.002) + synth_bars(72, 400, start=60.0, vol=0.0004)       # a wide and a narrow regime
    cases = [dict(), dict(lag=1), dict(lag=3), dict(lag=0, rank_len=60), dict(n=12, bw_len=10, rank_len=40, lag=2), dict(rank_len=300), dict(bw_len=30, lag=5)]
    checked = mismatch = nones = 0
    first = []
    for end in range(150, len(bars), 7):
        window = bars[max(0, end - 300): end]
        il._memo().clear()          # per window, not per case: the parameter sets of one bar share the memo exactly as the four tactics do
        for kw in cases:
            got, want = ie.low_range_state(window, **kw), _plain_low_range_state(window, **kw)
            checked += 1
            nones += want is None
            if got != want:
                mismatch += 1
                first.append((end, kw))
    return mismatch == 0 and checked > 800 and nones < checked, f"{checked} (window, parameters) pairs identical to the plain implementation; {nones} give None; mismatches {first[:2]}"


def one_state_per_bar_for_all_four_tactics_and_never_a_stale_one():
    rows = synth_bars(73, 400, vol=0.0008)
    il._memo().clear()
    calls = []
    real = ie._low_range_state
    ie._low_range_state = lambda *a, **k: (calls.append(1) or real(*a, **k))
    try:
        for fn in (ie.lr_fade, ie.lr_break, ie.lr_trendbreak, ie.lr_inside):
            fn(rows)
        per_bar = len(calls)
        # the same list changed in place (its newest row replaced) is a new state, never the earlier bar's
        before = ie.low_range_state(rows)
        rows[-1] = [rows[-1][0], rows[-1][1] * 1.02, rows[-1][2], rows[-1][3] * 1.015, rows[-1][4]]
        after = ie.low_range_state(rows)
        # and a caller that edits what it got cannot poison the memo
        got = ie.low_range_state(rows)
        got["bw_rank"] = -1.0
        again = ie.low_range_state(rows)
    finally:
        ie._low_range_state = real
    ok = per_bar <= 3 and before != after and after == _plain_low_range_state(rows) and again == after and again["bw_rank"] != -1.0
    return ok, f"the four tactics computed {per_bar} state(s) for one bar (distinct parameter sets); a replaced newest row gives a new state; an edited result does not reach the memo"


def sets_with_the_same_orders_store_their_columns_once():
    b = se.SetBook()
    b.load(overlay(**{**SMALL, "setStepMax": 6, "stratIndications": False, "stratShortRange": False, "pauseMode": "off", "directionMode": "both"}))
    b.bars["S0-USDT"] = synth_bars(74, 700, vol=0.0007)
    b.replay_all(now=T0)
    rows = b._hist_rows["S0-USDT"]
    by_cols = {}
    for sid, rc in rows.items():
        by_cols.setdefault(id(rc.t), []).append(sid)
    shared = [ids for ids in by_cols.values() if len(ids) > 1]
    ids_ok = all(rc.set_id == sid.split(se.HOLD_KEY, 1)[0] for sid, rc in rows.items())      # each Set keeps its own id on its rows (a hold-candidate tape its Set's id)
    plain = {sid: se.RowCols([{"t": r[0], "symbol": r[1], "side": r[2], "pnl": r[3], "pnl_pct": r[4], "hold_s": r[5], "reason": r[6], "set_id": r[7]} for r in rc]) for sid, rc in rows.items()}
    same_rows = all(list(rows[sid]) == list(plain[sid]) for sid in rows)           # and read exactly as an unshared copy would
    ok = bool(shared) and ids_ok and same_rows
    return ok, f"{len(rows)} Sets with orders store {len(by_cols)} column sets; {sum(len(x) for x in shared)} Sets share; every Set keeps its id and reads the same rows as an unshared copy"


def no_stored_row_ever_names_another_set():
    """Sets of different packs (and directions) that traded the same orders share columns, never rows: every stored row names its own Set."""
    b = se.SetBook()
    b.load(overlay(**{**SMALL, "setStepMax": 6, "stratIndications": False, "stratShortRange": False, "stratGeneral": True, "stratTrend": True,
                      "pauseMode": "off", "directionMode": "split"}))
    bars = synth_bars(75, 700, vol=0.0007)
    b.bars["S0-USDT"] = bars
    hist = {st.id: [] for st in b.by_idx}
    b._replay_symbol("S0-USDT", hist, T0, None)
    foreign = [(key, r["set_id"]) for key, rows in hist.items() for r in rows if r["set_id"] != key.split(se.HOLD_KEY, 1)[0]]
    packs_with_rows = {st.pack for st in b.by_idx if hist[st.id]}
    sides = {st.side for st in b.by_idx if hist[st.id]}
    # the same bars with the second pack switched off: the first pack's rows are the same rows
    c = se.SetBook()
    c.load(overlay(**{**SMALL, "setStepMax": 6, "stratIndications": False, "stratShortRange": False, "stratGeneral": True, "stratTrend": False,
                      "pauseMode": "off", "directionMode": "split"}))
    c.bars["S0-USDT"] = bars
    hist_c = {st.id: [] for st in c.by_idx}
    c._replay_symbol("S0-USDT", hist_c, T0, None)
    same = all(hist[i] == hist_c[i] for i in hist_c)
    return not foreign and len(packs_with_rows) >= 2 and sides == {1, -1} and same, (
        f"{sum(len(r) for r in hist.values())} stored rows over packs {sorted(packs_with_rows)} and both directions, rows naming another Set: {len(foreign)}; "
        f"the general pack's rows are identical with the trend pack off: {same}")


CHECKS = [("speed." + fn.__name__.replace("_", "-"), fn) for fn in (
    low_range_state_equals_the_plain_series_bit_for_bit, one_state_per_bar_for_all_four_tactics_and_never_a_stale_one,
    sets_with_the_same_orders_store_their_columns_once, no_stored_row_ever_names_another_set)]
