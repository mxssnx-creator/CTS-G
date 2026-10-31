"""Independent bar-by-bar checks of automatic-hold replay and stage floors."""
from __future__ import annotations

import random
import json
from pathlib import Path
from dataclasses import replace

from common import ROOT, SMALL, T0, lane_cfg, overlay, synth_bars
import set_engine as se


def _pf(rows, cost):
    values = [row[4] - cost for row in rows]
    gains = sum(x for x in values if x > 0)
    losses = -sum(x for x in values if x < 0)
    return gains / losses if losses else (99.0 if gains else 0.0)


def _choose_slow(candidates, at, lookback, max_n, min_n, margin, cost):
    windows = [[r for r in rows if at - lookback <= r[0] < at][-max_n:] for _, rows in candidates]
    ref = _pf(windows[0], cost)
    best, choice = ref, 0
    for i, window in enumerate(windows[1:], 1):
        pf = _pf(window, cost)
        if len(window) >= min_n and pf > best and pf >= ref * margin:
            best, choice = pf, i
    return candidates[choice][0]


def _row(st, symbol, base_ts, result):
    return (base_ts + result["exit_i"] * se.BAR_S, symbol, result["side"], result["pnl"],
            result["pnl_pct"], result["hold_s"], result["reason"], st.id)


def _fixture(kind):
    b = se.SetBook()
    b.load(overlay(**SMALL, histLookbackBars=300, histLookbackAuto=False))
    b.hold_auto_min, b.hold_auto_max = 3, 12
    st = next(x for x in b.by_idx if x.kind == kind and b.step_of(x) == 3)
    # Keep enough positions open for the different time stops to matter.
    st.tp_pct = 0.04
    if kind == "trail":
        st.trail_arm, st.trail_give = 1.2, 0.5
    cfg = lane_cfg(time_bars=37, scratch_bars=70, scratch_min=0.003, cooldown=2)
    rng = random.Random(843)
    symbols = ["A-USDT", "B-USDT"]
    base_ts = T0 - 420 * se.BAR_S
    holds = (0,) + b.auto_holds(st)
    b._hist_rows = {}
    b._hist_inputs = {}
    for j, sym in enumerate(symbols):
        bars = synth_bars(921 + j, 420, drift=0.0001, vol=0.0025)
        dirs = [rng.choice((-1, 1)) if rng.random() < 0.85 else 0 for _ in bars]
        confs = [0.8 if d else 0.0 for d in dirs]
        cand = [i for i, d in enumerate(dirs) if d]
        b._hist_inputs[sym] = {"bars": bars, "base_ts": base_ts, "warmup": 0, "cfg": cfg,
                               "dirs": {st.pack: dirs}, "confs": {st.pack: confs},
                               "cand": {st.pack: cand}, "acc": se.BarIndex(bars)}
        evidence = {}
        for hold in holds:
            lane = se.SetLane(st, sym)
            lane.hold = hold
            records = []
            for i, bar in enumerate(bars):
                result = lane.step(i, bar, dirs[i], confs[i], True, cfg)
                if result:
                    records.append(_row(st, sym, base_ts, result))
            evidence[st.id if not hold else f"{st.id}{se.HOLD_KEY}{hold}"] = records
        b._hist_rows[sym] = evidence
    return b, st, symbols, holds, base_ts


def dynamic_hold_matches_independent_walk():
    comparisons, choices, orders = 0, set(), 0
    for kind in ("base", "trail"):
        b, st, symbols, holds, base_ts = _fixture(kind)
        until, cutoff = T0, T0 - b.lookback * se.BAR_S
        candidates = [(h, b._tape_rows(st.id if not h else f"{st.id}{se.HOLD_KEY}{h}", symbols, float("-inf"), until)) for h in holds]
        actual = b._choose_hold(st, b._tape_rows(st.id, symbols, cutoff, until), symbols, cutoff, until)
        expected = []
        for sym in symbols:
            data = b._hist_inputs[sym]
            lane = se.SetLane(st, sym)
            for i, bar in enumerate(data["bars"]):
                d, conf = data["dirs"][st.pack][i], data["confs"][st.pack][i]
                if lane.open is None and lane.cool == 0 and d and conf >= data["cfg"]["entry_conf"]:
                    lane.hold = _choose_slow(candidates, base_ts + i * se.BAR_S, b.lookback * se.BAR_S,
                                             b.hold_auto_max, b.hold_auto_min, se.HOLD_AUTO_MARGIN, b.cost_pct / 100)
                    choices.add(lane.hold)
                result = lane.step(i, bar, d, conf, True, data["cfg"])
                if result:
                    row = _row(st, sym, base_ts, result)
                    if cutoff <= row[0] <= until:
                        expected.append(row)
        expected.sort(key=lambda r: r[0])
        if actual != expected:
            return False, f"{kind}: engine and independent per-bar walks differ ({len(actual)} vs {len(expected)})"
        for sym in symbols:
            previous_exit = None
            for r in (x for x in actual if x[1] == sym):
                entry = r[0] - r[5]
                if previous_exit is not None and entry <= previous_exit:
                    return False, f"{kind}/{sym}: positions overlap or re-enter on the exit bar"
                previous_exit = r[0]
        comparisons += 1
        orders += len(actual)
    return comparisons == 2 and orders > 0 and len(choices) > 1, f"{comparisons} base/trail walks, {orders} rows, holds selected {sorted(choices)}"


def dynamic_hold_retained_across_chunks():
    b, st, symbols, _holds, base_ts = _fixture("trail")
    data = b._hist_inputs[symbols[0]]
    bars, cfg = data["bars"], data["cfg"]
    choose = lambda i: (10 if (i // 17) % 2 else 30)
    args = (bars, data["dirs"][st.pack], data["confs"][st.pack], True, cfg, data["cand"][st.pack], data["acc"])
    one = se.SetLane(st, symbols[0]); whole = one.scan(0, len(bars), *args, select_hold=choose)
    two = se.SetLane(st, symbols[0]); chunked = []
    for a in range(0, len(bars), 13):
        chunked.extend(two.scan(a, min(a + 13, len(bars)), *args, select_hold=choose))
    return whole == chunked and one.open == two.open and one.cool == two.cool, f"{len(whole)} closes; same held state and cooldown: {one.open == two.open and one.cool == two.cool}"


def old_splice_can_overlap_and_lose_entries():
    b, st, symbols, holds, _ = _fixture("base")
    cutoff = T0 - b.lookback * se.BAR_S
    candidates = []
    for hold in holds:
        key = st.id if not hold else f"{st.id}{se.HOLD_KEY}{hold}"
        rows = []
        for sym in symbols:
            items = b._hist_rows[sym][key]
            rows.extend(r for r in (items[-50:] if hold else items) if cutoff <= r[0] <= T0)
        candidates.append((hold, sorted(rows, key=lambda r: r[0])))
    old = se.causal_hold_tape(candidates, b.hold_auto_max, b.hold_auto_min, se.HOLD_AUTO_MARGIN, b.cost_pct / 100)
    new = b._choose_hold(st, b._tape_rows(st.id, symbols, cutoff, T0), symbols, cutoff, T0)
    def overlaps(rows):
        count = 0
        for sym in symbols:
            previous = None
            for row in (r for r in rows if r[1] == sym):
                if previous is not None and row[0] - row[5] <= previous:
                    count += 1
                previous = max(previous if previous is not None else row[0], row[0])
        return count
    old_overlap, new_overlap = overlaps(old), overlaps(new)
    missing = len(set(new) - set(old))
    return old_overlap > 0 and new_overlap == 0 and missing > 0, f"legacy overlaps={old_overlap}, corrected overlaps={new_overlap}, corrected rows missing from splice={missing}"


def selection_excludes_same_time_and_expired_evidence():
    candidates = [(0, []), (10, [])]
    prefix = []
    for times, nets in (([98.0, 99.0], [-0.01, -0.01]), ([99.0, 100.0], [0.01, 0.01])):
        g, l = se.prefix_sums(nets)
        prefix.append((times, g, l))
    a = se.hold_choice_at(candidates, prefix, 100, 10, 2, 1.02, 10)
    b = se.hold_choice_at(candidates, prefix, 101, 10, 2, 1.02, 10)
    c = se.hold_choice_at(candidates, prefix, 200, 10, 2, 1.02, 10)
    return (a, b, c) == (0, 1, 0), f"before same-time close={a}; after close={b}; after expiry={c}"


def later_bars_cannot_change_earlier_dynamic_positions():
    b, st, symbols, _holds, base_ts = _fixture("trail")
    full = b._choose_hold(st, b._tape_rows(st.id, symbols, float("-inf"), T0), symbols, float("-inf"), T0)
    cut_i = 260
    cut_t = base_ts + cut_i * se.BAR_S
    expected = [r for r in full if r[0] < cut_t]
    for sym in symbols:
        data = b._hist_inputs[sym]
        data["bars"] = data["bars"][:cut_i]
        data["acc"] = se.BarIndex(data["bars"])
        for key in ("dirs", "confs"):
            data[key] = {pack: vals[:cut_i] for pack, vals in data[key].items()}
        data["cand"] = {pack: [i for i in vals if i < cut_i] for pack, vals in data["cand"].items()}
        b._hist_rows[sym] = {key: [r for r in rows if r[0] < cut_t] for key, rows in b._hist_rows[sym].items()}
    got = b._choose_hold(st, b._tape_rows(st.id, symbols, float("-inf"), cut_t), symbols, float("-inf"), cut_t)
    return got == expected and bool(got), f"{len(got)} earlier closes unchanged after future bars and candidate evidence removed"


def candidate_history_is_not_capped_to_the_final_last_50():
    b, st, _symbols, _holds, _ = _fixture("base")
    st.tp_pct = 0.001
    b.by_idx, b.sets, b.packs = [st], {st.id: st}, [st.pack]
    b.bars = {"A-USDT": synth_bars(21, 1500, drift=0.0004)}
    history = {st.id: []}
    b._replay_symbol("A-USDT", history, T0)
    counts = [len(rows) for key, rows in history.items() if se.HOLD_KEY in key]
    return bool(counts) and min(counts) > 50, f"candidate close counts={counts}; early entry-time evidence retained"


def signal_floor_applies_to_each_stage():
    b = se.SetBook(); b.load(overlay(minPfBySize=True, minPfSignals=1.3))
    p = b.stage_params("signal.example")
    p = replace(p, main_on=True, main_n=2, short_on=False, neg_on=False, real_on=True,
                real_n=2, full_window=True, main_min_n=0)
    good, weak = [0.014, -0.01], [0.0129, -0.01]
    all_pass = se.stage_verdicts(good, good, [], 2, p)
    weak_main = se.stage_verdicts(weak, good, [], 2, p)
    weak_real = se.stage_verdicts(good, weak, [], 2, p)
    short_sample = se.stage_verdicts(good, [0.014], [], 2, p)
    floors_ok = all(abs(p.floor_for(n) - 1.3) < 1e-9 for n in (2, 5, 25, 100))
    ok = floors_ok and all_pass["pos"] and not weak_main["pos"] and not weak_real["pos"] and not short_sample["pos"]
    return ok, f"signal floor 1.3 across windows={floors_ok}; pass={all_pass['pos']}; weak Main/Real and short sample blocked={not weak_main['pos'], not weak_real['pos'], not short_sample['pos']}"


def secure_preset_cannot_bypass_main_through_negative_lane():
    preset = json.loads((Path(ROOT) / "presets" / "secure-minimal.json").read_text())
    b = se.SetBook(); b.load(overlay(**preset))
    p = b.stage_params("signal.example")
    result = se.stage_verdicts([-0.01] * 25, [0.01] * 15, [0.01] * 15, p.main_n, p)
    return not p.neg_on and not result["pos"] and not result["negl"], f"negative enabled={p.neg_on}; losing Main admitted={result['pos'] or result['negl']}"


def memoized_replays_keep_inputs_and_invalidate_hold_changes():
    import memo
    def make(**options):
        b = se.SetBook(); b.load(overlay(**SMALL, **options))
        b.bars["A-USDT"] = synth_bars(992, 300, drift=0.0002)
        return b
    memo.clear()
    previous_mode = memo.MODE
    try:
        memo.MODE = "verify"
        one, two = make(), make()
        key = memo._key(one, "A-USDT", T0)
        changed_hold = memo._key(make(holdAuto=False), "A-USDT", T0)
        changed_stop = memo._key(make(slMinSteps=3), "A-USDT", T0)
        with memo.using():
            one.replay_all(now=T0)
            two.replay_all(now=T0)
        a = [(x.id, x.n, x.gate_pf, x.proc, x.exe, x.last_error) for x in one.by_idx]
        b = [(x.id, x.n, x.gate_pf, x.proc, x.exe, x.last_error) for x in two.by_idx]
        ok = (a == b and not one.progress.errors and not two.progress.errors and memo.STATS["verified"] >= 1
              and two._hist_inputs.get("A-USDT") is not None and key != changed_hold and key != changed_stop)
        return ok, f"fresh/cached equal={a == b}; errors={one.progress.errors}/{two.progress.errors}; verified hits={memo.STATS['verified']}; hold/stop invalidate={key != changed_hold}/{key != changed_stop}"
    finally:
        memo.MODE = previous_mode
        memo.clear()


CHECKS = [
    ("hold-causal.independent-dynamic-walk", dynamic_hold_matches_independent_walk),
    ("hold-causal.chunked-position-state", dynamic_hold_retained_across_chunks),
    ("hold-causal.splice-overlap-and-missed-entries", old_splice_can_overlap_and_lose_entries),
    ("hold-causal.same-time-and-expired-evidence", selection_excludes_same_time_and_expired_evidence),
    ("hold-causal.future-bars-do-not-change-past", later_bars_cannot_change_earlier_dynamic_positions),
    ("hold-causal.candidate-entry-history", candidate_history_is_not_capped_to_the_final_last_50),
    ("hold-causal.signal-stage-floor", signal_floor_applies_to_each_stage),
    ("hold-causal.secure-preset-requires-main", secure_preset_cannot_bypass_main_through_negative_lane),
    ("hold-causal.memoized-replay-inputs", memoized_replays_keep_inputs_and_invalidate_hold_changes),
]
