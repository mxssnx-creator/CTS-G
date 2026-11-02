"""Long and short are independent (directionMode split, the default): every Set exists once per direction with its own lane, tape, windows, stage
chain, pause and learned floor; a signal is only ever judged by a Set that may trade its direction.

Independence is shown by removal: take away every short signal and the long Sets' tapes (and their pause choices, and their learned floors) must
be byte-identical, and the reverse."""
from __future__ import annotations

import random
import types

from common import SMALL, T0, overlay, synth_bars
import set_engine as se


def _book(mode="split", **extra):
    b = se.SetBook()
    b.load(overlay(**{**SMALL, "setStepMax": 5, "stratIndications": False, "stratShortRange": False, "stratGeneral": True, "directionMode": mode,
                      "minPfByStep": False, "pauseMode": "off", **extra}))
    return b


def _signals(n, seed, longs=True, shorts=True):
    r = random.Random(seed)
    out = []
    for i in range(n):
        d = r.choice((-1, 1)) if i % 5 == 0 else 0
        if (d > 0 and not longs) or (d < 0 and not shorts):
            d = 0
        out.append((d, 0.9 if d else 0.0, "t"))
    return out


def _replay(b, symbol, bars, **sig):
    """Replay one symbol with controlled signals (pack_signals replaced), returning the rows per Set id."""
    real = se.pack_signals
    sigs = _signals(len(bars), 5, **sig)
    se.pack_signals = lambda bars_, packs, *a, **k: {p: sigs for p in packs}
    try:
        hist = {st.id: [] for st in b.by_idx}
        b.bars[symbol] = bars
        b._replay_symbol(symbol, hist, T0, None)
    finally:
        se.pack_signals = real
    return hist


def split_grid_is_the_combined_grid_twice():
    both, split = _book("both"), _book("split")
    per = lambda b, side: sorted((s.pack, s.kind, s.step, s.sl_ratio, s.trail_key) for s in b.by_idx if s.side == side)     # noqa: E731
    ids = [s.id for s in split.by_idx]
    ok = (len(split.by_idx) == 2 * len(both.by_idx) and len(set(ids)) == len(ids)
          and per(split, 1) == per(both, 0) == per(split, -1)
          and all(s.id.startswith(f"{s.pack}:L:") for s in split.by_idx if s.side > 0)
          and all(s.id.startswith(f"{s.pack}:S:") for s in split.by_idx if s.side < 0)
          and all(":L:" not in s.id and ":S:" not in s.id for s in both.by_idx))
    return ok, f"combined {len(both.by_idx)} Sets -> split {len(split.by_idx)} ({sum(s.side > 0 for s in split.by_idx)} long + {sum(s.side < 0 for s in split.by_idx)} short), ids unique"


def a_set_only_ever_trades_its_own_direction():
    b = _book("split")
    hist = _replay(b, "S0-USDT", synth_bars(31, 900, vol=0.004))
    longs = [r for s in b.by_idx if s.side > 0 for r in hist[s.id]]
    shorts = [r for s in b.by_idx if s.side < 0 for r in hist[s.id]]
    c = _book("both")
    chist = _replay(c, "S0-USDT", synth_bars(31, 900, vol=0.004))
    both_sides = {r["side"] for s in c.by_idx for r in chist[s.id]}
    ok = longs and shorts and {r["side"] for r in longs} == {"LONG"} and {r["side"] for r in shorts} == {"SHORT"} and both_sides == {"LONG", "SHORT"}
    return ok, f"long Sets {len(longs)} orders all LONG; short Sets {len(shorts)} orders all SHORT; the combined Sets trade {sorted(both_sides)}"


def removing_one_direction_leaves_the_other_untouched():
    bars = synth_bars(32, 1100, vol=0.004)
    full = _replay(_book("split"), "S0-USDT", bars)
    no_short = _replay(_book("split"), "S0-USDT", bars, shorts=False)
    no_long = _replay(_book("split"), "S0-USDT", bars, longs=False)
    b = _book("split")
    long_ids = [s.id for s in b.by_idx if s.side > 0]
    short_ids = [s.id for s in b.by_idx if s.side < 0]
    same_long = all(full[i] == no_short[i] for i in long_ids)
    same_short = all(full[i] == no_long[i] for i in short_ids)
    emptied = all(not no_short[i] for i in short_ids) and all(not no_long[i] for i in long_ids)
    n = sum(len(full[i]) for i in long_ids), sum(len(full[i]) for i in short_ids)
    # and the combined Set is NOT independent: removing the shorts changes its long orders (one position at a time couples the sides)
    c_full = _replay(_book("both"), "S0-USDT", bars)
    c_nos = _replay(_book("both"), "S0-USDT", bars, shorts=False)
    coupled = any([r for r in c_full[i] if r["side"] == "LONG"] != c_nos[i] for i in c_full)
    return same_long and same_short and emptied and n[0] > 20 and n[1] > 20 and coupled, (
        f"long tapes identical without any short signal ({n[0]} orders): {same_long}; short tapes identical without any long signal ({n[1]}): {same_short}; "
        f"the combined Set's long orders do change when shorts go: {coupled}")


def pause_is_learned_per_direction():
    bars = synth_bars(33, 1500, vol=0.004)

    def choices(**sig):
        b = _book("split", pauseMode="auto", pauseAutoMin=8, pauseAutoMax=60, histLookbackBars=2000)
        real = se.pack_signals
        sigs = _signals(len(bars), 5, **sig)
        se.pack_signals = lambda bars_, packs, *a, **k: {p: sigs for p in packs}
        try:
            b.bars["S0-USDT"] = bars
            b._replay_symbol("S0-USDT", {st.id: [] for st in b.by_idx}, T0, None)
        finally:
            se.pack_signals = real
        return dict(b.pause_now)
    full, no_short, no_long = choices(), choices(shorts=False), choices(longs=False)
    key_l, key_s = ("general", "S0-USDT", 1), ("general", "S0-USDT", -1)
    ok = full[key_l] == no_short[key_l] and full[key_s] == no_long[key_s] and key_l in full and key_s in full
    return ok, f"the long pause {full[key_l]} min is the same without shorts ({no_short[key_l]}), the short pause {full[key_s]} min the same without longs ({no_long[key_s]})"


def learned_floors_are_per_direction():
    from step_pf import _rows, BOUNDARY, _learn
    b = _book("split", minPfByStep=True, minPfStepAutoMin=100)
    longs = [s for s in b.by_idx if s.kind == "base" and s.step == 3 and s.side > 0][:2]
    shorts = [s for s in b.by_idx if s.kind == "base" and s.step == 3 and s.side < 0][:2]
    tapes = {s.id: _rows(100 + i, 1500) for i, s in enumerate(longs)}
    tapes.update({s.id: _rows(200 + i, 1500, signal=False) for i, s in enumerate(shorts)})       # the short Sets have no relation to learn
    _learn(b, tapes, BOUNDARY)
    fl, fs = b.step_floor_auto.get((3, 1)), b.step_floor_auto.get((3, -1))
    base = dict(b.step_floor_auto)
    flipped = {k: ([(r[0], r[1], r[2], -r[3], -r[4], r[5], r[6], r[7]) for r in rows] if k in {s.id for s in shorts} else rows) for k, rows in tapes.items()}
    _learn(b, flipped, BOUNDARY)
    long_same = b.step_floor_auto.get((3, 1)) == fl
    rows = [r for r in b.step_floor_report if r["step"] == 3]
    ok = fl is not None and fl > se.manual_step_floor(0.3, 0.2) and fs is None and long_same and {r["side"] for r in rows} == {1, -1}
    return ok, f"long floor {fl}, short floor {fs} (manual {se.manual_step_floor(0.3, 0.2)}); inverting every short result leaves the long floor at {b.step_floor_auto.get((3, 1))}; report has both sides"


def a_signal_is_judged_only_by_a_set_of_its_direction():
    b = _book("split")
    b.use_historic_gate = True
    long_set = next(s for s in b.by_idx if s.kind == "base" and s.side > 0)
    short_set = next(s for s in b.by_idx if s.kind == "base" and s.side < 0)
    b.is_eligible = lambda st: st in (long_set, short_set)
    b._execution_lanes = lambda st: {"pos"}
    got = {side: getattr(b.pick_any("general", side=side), "id", None) for side in (1, -1)}
    anyside = b.pick_any("general")
    only_short = {side: getattr(b.pick_any("general", side=side), "id", None) for side in (1, -1)} if (setattr(b, "is_eligible", lambda st: st is short_set) or True) else {}
    c = _book("both")
    c.use_historic_gate = True
    both_set = next(s for s in c.by_idx if s.kind == "base")
    c.is_eligible = lambda st: st is both_set
    c._execution_lanes = lambda st: {"pos"}
    combined = {side: getattr(c.pick_any("general", side=side), "id", None) for side in (1, -1)}
    ok = (got == {1: long_set.id, -1: short_set.id} and anyside is not None and only_short == {1: None, -1: short_set.id}
          and combined == {1: both_set.id, -1: both_set.id})
    return ok, f"long signal -> {got[1]}, short signal -> {got[-1]}; with only a short Set valid a long signal gets {only_short[1]}; a combined Set serves both {combined}"


def live_rest_after_a_win_follows_the_closed_positions_direction():
    import pulse_trader as pt
    b = _book("split", pauseMode="auto")
    b.pause_now[("general", "A-USDT", 1)] = 20
    b.pause_now[("general", "A-USDT", -1)] = 45
    p = pt.Pulse.__new__(pt.Pulse)
    p.position_cost_pct = 0.12
    p.ignore_syms = {}
    p.sets = b
    cd = b.cooldown_bars * 60.0
    long_win = p.symbol_backoff("A-USDT", 0.01, 300.0, "tp", set_lane=True, pack="general", side=1)
    short_win = p.symbol_backoff("A-USDT", 0.01, 300.0, "tp", set_lane=True, pack="general", side=-1)
    long_loss = p.symbol_backoff("A-USDT", -0.01, 300.0, "sl", set_lane=True, pack="general", side=1)
    ok = long_win == 20 * 60.0 and short_win == 45 * 60.0 and long_loss == cd
    return ok, f"waits after a win: long {long_win}s, short {short_win}s; after a loss {long_loss}s (cooldown {cd}s)"


def direction_mode_setting():
    d, s, b, junk = se.SetBook(), _book("split"), _book("both"), _book("sideways")
    d.load({})
    ok = d.direction_split and s.direction_split and not b.direction_split and junk.direction_split
    return ok, f"default split={d.direction_split}, split={s.direction_split}, both={not b.direction_split}, junk -> split={junk.direction_split}; replay key differs={s._evaluation_config != b._evaluation_config}"


def client_order_ids_name_the_direction():
    """A client order id decodes to its own Set, and when the grid index no longer points at it (a restart after a grid change) the content alone must tell a
    long Set from its short twin; a combined Set's id carries no direction mark."""
    import pulse_trader as pt
    b = _book("split")
    p = pt.Pulse.__new__(pt.Pulse)
    p.sets = b
    wrong, swapped, marks = [], [], set()
    for st in b.by_idx:
        cid = p.cid("o", set_id=st.id, pack=st.pack, set_idx=st.idx)
        marks.add(cid[len(pt.TAG) + 13: len(pt.TAG) + 14])
        if (p.parse_track(cid) or {}).get("set_id") != st.id:
            wrong.append(st.id)
        twin = next((x for x in b.by_idx if x.pack == st.pack and x.kind == st.kind and x.step == st.step and x.sl_ratio == st.sl_ratio
                     and x.trail_key == st.trail_key and x.side == -st.side), None)
        if twin is not None:
            k = len(pt.TAG) + 8
            moved = cid[:k] + f"{twin.idx:04d}" + cid[k + 4:]          # the index points at the OTHER direction's Set
            if (p.parse_track(moved) or {}).get("set_id") != st.id:
                swapped.append(st.id)
    c = _book("both")
    q = pt.Pulse.__new__(pt.Pulse)
    q.sets = c
    both_marks = {q.cid("o", set_id=st.id, pack=st.pack, set_idx=st.idx)[len(pt.TAG) + 13: len(pt.TAG) + 14] for st in c.by_idx}
    ok = not wrong and not swapped and {"L", "S"} <= marks and not (both_marks & {"L", "S"}) and pt.side_of_set_id("general:S:1m:sl1.00:st3") == -1
    return ok, f"{len(b.by_idx)} split ids round-trip (wrong {len(wrong)}); with the index pointing at the other direction's twin, wrong {len(swapped)}; direction marks {sorted(marks & {'L', 'S'})}, none on combined Sets"


CHECKS = [("direction." + fn.__name__.replace("_", "-"), fn) for fn in (
    split_grid_is_the_combined_grid_twice, a_set_only_ever_trades_its_own_direction, removing_one_direction_leaves_the_other_untouched,
    pause_is_learned_per_direction, learned_floors_are_per_direction, a_signal_is_judged_only_by_a_set_of_its_direction,
    live_rest_after_a_win_follows_the_closed_positions_direction, direction_mode_setting, client_order_ids_name_the_direction)]
