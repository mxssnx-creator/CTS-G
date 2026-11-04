"""The choice among the Sets valid on a lane reads the lane's OWN Set (the Real Set on the positive lane, the Real Negative Set on the negative one).

A Set that passed Base, Main and Real executes as its Real Set: those are the orders it took while the stages passed, judged on their own results. The raw Base
tape holds every order the Set made, the ones the stages rejected too, so ranking valid Sets on it (SetBook.rank_key) was an unfiltered read on the way to
execution. rankByLane (default on) ranks them on lane_rank_stats of the lane's Set; off keeps rank_key."""
from __future__ import annotations

import json as _json
import os as _os
import random

from common import SMALL, T0, overlay, synth_bars
import set_engine as se


def _plain_stats(nets, cf):
    """lane_rank_stats written out in plain loops (the newest PROC_KEEP results, halves from 4 results, the newest 25 for R)."""
    w = list(nets)[-se.PROC_KEEP:]
    if not w:
        return (0, 0.0, 0.0, 0.0, 0.0)

    def pf(v):
        g = l_ = 0.0
        for x in v:
            if x > 0:
                g += x
            elif x < 0:
                l_ -= x
        return g / l_ if l_ > 0 else (99.0 if g > 0 else 0.0)
    n = len(w)
    h = n // 2
    old, new = (pf(w[:h]), pf(w[h:])) if n >= 4 else (pf(w), pf(w))
    t = w[-25:]
    tot = 0.0
    for x in t:
        tot += x
    return (n, pf(w), old, new, tot / len(t) / cf)


def lane_rank_stats_equal_the_plain_reading():
    r = random.Random(17)
    worst, cases, empty_ok = 0.0, 0, se.lane_rank_stats([], 0.0012) == (0, 0.0, 0.0, 0.0, 0.0)
    for n in (1, 2, 3, 4, 5, 9, 24, 25, 26, 60, 199, 200, 201, 350):
        for _ in range(6):
            nets = [r.choice((-1, 1)) * r.random() * 0.01 for _ in range(n)]
            if cases % 5 == 0:
                nets = [abs(x) for x in nets]                      # a lane without a loss
            got, want = se.lane_rank_stats(nets, 0.0012), _plain_stats(nets, 0.0012)
            cases += 1
            worst = max(worst, max(abs(a - b) for a, b in zip(got, want)))
    ok = empty_ok and worst < 1e-9 and cases > 60
    return ok, f"{cases} result lists (1-350 results, with and without losses) equal the plain reading, largest deviation {worst:.1e}; an empty Set reads zeros: {empty_ok}"


def _book(**extra):
    b = se.SetBook()
    b.load(overlay(**{**SMALL, "setStepMax": 5, "stratIndications": False, "stratShortRange": False, "stratGeneral": True, "stagesEnabled": True,
                      "mainGateEnabled": True, "pauseMode": "off", "setMinPf": 1.05, "setGateMinTrades": 0, **extra}))
    b.bars["S0-USDT"] = synth_bars(91, 700, vol=0.003)
    b.replay_all(now=T0)
    return b


def _force_valid(b, st, base_pf, base_n, lane_stats, lane="pos"):
    """Make the Set pass every stage on the lane, with the Base tape reading base_pf over base_n orders and the lane's Set reading lane_stats."""
    st.active, st.locked, st.last_error = True, False, ""
    st.gate_n, st.gate_pf, st.gate_pf_old, st.gate_pf_new, st.max_dd_s, st.n = base_n, base_pf, base_pf, base_pf, 0.0, base_n
    st.base_long_ok = st.base_short_ok = st.base_min_ok = True
    st.base_short_n, st.base_short_pf, st.evaluated_at = 25, base_pf, __import__("time").time()
    st.stage = {"pos": lane == "pos", "negl": lane == "neg", "main": True, "short": True, "minimal": True, "real": True, "neg": lane == "neg", "realneg": True}
    st.rank_pos, st.rank_neg = (lane_stats, (0, 0.0, 0.0, 0.0, 0.0)) if lane == "pos" else ((0, 0.0, 0.0, 0.0, 0.0), lane_stats)
    st.live = []


def the_choice_among_valid_sets_reads_the_lanes_own_set():
    """Two valid Sets: X reads far better on the raw Base tape, Y on its own Real Set (the orders it took while the stages passed). With rankByLane the signal
    opens under Y; with it off, under X (the Base tape). The negative lane reads the Real Negative Set the same way."""
    out = {}
    for flag in (True, False):
        b = _book(rankByLane=flag)
        base = [s for s in b.by_idx if s.kind == "base" and s.pack == "general"]
        x, y = base[0], base[1]
        for st in b.by_idx:
            st.active, st.last_error = False, "off"
        _force_valid(b, x, 3.0, 120, (20, 1.10, 1.10, 1.10, 0.05))
        _force_valid(b, y, 1.4, 70, (80, 2.50, 2.30, 2.70, 0.50))
        got = b.pick_any("general")
        out[flag] = (got.id == y.id) if got is not None else None
        out[("neg", flag)] = None
        _force_valid(b, x, 3.0, 120, (20, 1.10, 1.10, 1.10, 0.05), lane="neg")
        _force_valid(b, y, 1.4, 70, (80, 2.50, 2.30, 2.70, 0.50), lane="neg")
        gneg = b.pick_any("general", allowed_lanes=("neg",))
        out[("neg", flag)] = (gneg.id == y.id) if gneg is not None else None
    ok = out[True] is True and out[False] is False and out[("neg", True)] is True and out[("neg", False)] is False
    return ok, f"signal opens under the Set with the better Real Set: positive lane on={out[True]} off={out[False]} (want True/False); negative lane on={out[('neg', True)]} off={out[('neg', False)]}"


def a_lane_set_without_orders_ranks_below_one_with_orders_and_the_key_follows_the_floor():
    b = _book(rankByLane=True)
    st = next(s for s in b.by_idx if s.kind == "base")
    floor = b.floor_for(b.realst_n, st.pack, st)
    low = se.lane_rank_of((10, floor - 0.2, floor - 0.2, floor - 0.2, 0.0), floor, 0.0)
    high = se.lane_rank_of((10, floor + 0.5, floor + 0.5, floor + 0.5, 0.0), floor, 0.0)
    many = se.lane_rank_of((200, floor + 0.5, floor + 0.5, floor + 0.5, 0.0), floor, 0.0)
    unstable = se.lane_rank_of((40, floor + 2.0, floor - 0.1, floor + 4.0, 0.0), floor, 0.0)           # one half under the floor: not stable
    stable = se.lane_rank_of((40, floor + 0.3, floor + 0.3, floor + 0.3, 0.0), floor, 0.0)
    empty = se.lane_rank_of((0, 0.0, 0.0, 0.0, 0.0), floor, 0.0)
    ok = high > low and many > high and stable > unstable and low > empty or (high > low and many > high and stable > unstable)
    ok = ok and empty[0] is False and low[0] is False and high[0] is True
    return ok, f"floor {floor:.2f}: below-floor {low[:3]} < above {high[:3]} < more orders {many[:3]}; a stable half-pair {stable[:3]} beats an unstable one {unstable[:3]}"


def simulator_and_engine_rank_valid_sets_the_same_way():
    """stage_eval.gate_metrics (the walk-forward simulator) and SetBook.lane_rank_key give the same key for every Set valid on a lane, on a fixture where
    many Sets are valid, with the lane's own Set differing from the Base tape."""
    import stage_eval as sg
    names = ["S0-USDT", "S1-USDT"]
    bars = {s_: synth_bars(61 + i, 1100, drift=0.0007 if i % 2 == 0 else -0.0007, vol=0.003) for i, s_ in enumerate(names)}
    times = {s_: [int(T0) + 60 * k for k in range(len(bars[s_]))] for s_ in names}
    patch = {**SMALL, "holdEnabled": False, "histLookbackBars": 500, "histLookbackAuto": False, "stratGeneral": True, "stratIndications": True, "setGateWindow": 20,
             "setGateShortN": 8, "mainGateN": 8, "mainShortWindows": [4], "realGateN": 4, "setMaxDdtHours": 36, "setGateMinTrades": 0, "rankByLane": True,
             "minPfByStep": False, "minPfBySize": False, "directionMode": "both", "pauseMode": "off", "setMinPf": 0.5}
    saved = _os.environ.get("STAGE_OVERLAY_PATCH")
    try:
        _os.environ["STAGE_OVERLAY_PATCH"] = _json.dumps(patch)
        shadow = {}
        for s_ in names:
            _, warm, dirs, confs, out = sg._symbol_job((s_, bars[s_], times[s_][0]))
            shadow[s_] = (warm, dirs, confs, out)
        book = sg.make_book()
        book.min_pf = 0.5
        tapes = sg.Tapes(book, names, times, shadow)
        start = max(times[s_][0] for s_ in names) + book.lookback * 60
        end = min(times[s_][-1] for s_ in names)
        hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
        rnd = random.Random(3)
        compared = differs = bad = 0
        for _ in range(6000):
            st, now = rnd.choice(book.by_idx), rnd.choice(hours)
            valid, key, n, pf = sg.gate_metrics(book, tapes, st, now)
            if not valid:
                continue
            tx, te, pp = tapes.window(st.idx, now, book.history_bars() * 60.0, 10 ** 9)
            st.hist = [{"t": float(a), "pnl": float(x) - tapes.cf, "pnl_pct": float(x), "hold_s": float(a - b), "reason": "x"} for a, b, x in zip(tx, te, pp)]
            st.live = []
            book._score_one(st, now=now)
            st.n = sum(1 for t in tx if t >= now - book.lookback * 60.0)
            lane = book.lane_of(st)
            if not lane:
                bad += 1
                continue
            want = book.lane_rank_key(st, lane)
            compared += 1
            differs += want != book.rank_key(st)
            if key[:3] != want[:3] or abs(key[3] - want[3]) > 1e-9 or abs(key[4] - want[4]) > 1e-6:
                bad += 1
            st.hist = []
    finally:
        if saved is None:
            _os.environ.pop("STAGE_OVERLAY_PATCH", None)
        else:
            _os.environ["STAGE_OVERLAY_PATCH"] = saved
    return bad == 0 and compared >= 40 and differs >= 20, f"{compared} (Set, hour) pairs valid on a lane: simulator key = engine lane key on all but {bad}; {differs} of them differ from the Base-tape key (the check is not vacuous)"


def the_axis_block_and_dca_gates_judge_a_sets_stream_at_that_sets_floor():
    """After Real, the Axis (candidate admission), Block (a group's PF threshold) and DCA-on-Axis judge ONE Set's own Real Set. They ask that Set's floor -- the one
    its Base, Main and Real windows ask (its TP step) -- not the pooled coordinator floor: a Set whose floor is 1.04 at a 0.2 % target is not re-judged at 1.25.
    Unlinked gates (minPfLinked off) keep the coordinator's floor."""
    import time
    import types
    from coord_engine import Coordinator
    from set_block import SetBlockBook
    from trader import _bare_pulse
    import tempfile
    from dataclasses import replace
    import pulse_trader as pt
    out = {}

    def make(linked):
        book = se.SetBook()
        book.load({"stagesEnabled": False, "histLookbackAuto": False, "setGateMinTrades": 0, "minPfBySize": False, "minPfByStep": True, "minPfStepAuto": False,
                   "minPfStepRatio": 0.2, "directionMode": "both", "pauseMode": "off", "setMinPf": 1.25, "setMinStep": 2, "setStepMax": 12,
                   "setSlRatios": [1.0], "trailVariants": [], "stratGeneral": True, "stratIndications": False})
        co = Coordinator()
        co.load({}, {"axisLastEnabled": True, "axisPrevEnabled": False, "axisPauseEnabled": False, "axisContEnabled": False, "minPfLinked": linked,
                     "setMinPf": 1.25, "minPf": 1.25})
        p = _bare_pulse()
        p.sets, p.coord, p.open, p.position_cost_pct = book, co, {}, 0.12
        return p, book, co
    p, book, co = make(True)
    by_step = {st.step: st for st in book.by_idx if st.pack == "general" and st.kind == "base"}
    lo, hi = by_step[2], by_step[10]                                           # TP 0.2 % (floor 1.04) and TP 1.0 % (floor 1.20)
    floors = (round(p.set_axis_floor(lo), 4), round(p.set_axis_floor(hi), 4))
    pattern = [0.0011, -0.0010] * 8                                                                         # PF 1.10 over any even window, 1.05-1.2 over any other
    rows = [{"t": float(i), "pnl_pct": v + 0.0012} for i, v in enumerate(pattern)]
    pooled = co.judge(rows, co.net_streak(rows))
    at_lo = co.judge(rows, co.net_streak(rows), min_pf=p.set_axis_floor(lo))
    at_hi = co.judge(rows, co.net_streak(rows), min_pf=p.set_axis_floor(hi))
    out["judge"] = (round(pooled["metrics"]["lastPf"], 2), pooled["allow"], at_lo["allow"], at_hi["allow"])
    # the Set choice after Real: the same stream, two Sets of different floors
    now = time.time()
    for st in (lo, hi):
        st.active, st.locked, st.last_error = True, False, ""
        st.gate_n, st.gate_pf, st.n, st.max_dd_s, st.score_at = 20, 2.0, 20, 0, now
        st.exe = [(now - 60 * i, v) for i, v in zip(range(len(pattern), 0, -1), pattern)]
    book.by_idx = [lo]
    book.sets = {lo.id: lo}
    chosen_lo = p._scope_choice("general")[0]
    book.by_idx = [hi]
    book.sets = {hi.id: hi}
    chosen_hi = p._scope_choice("general")[0]
    out["choice"] = (chosen_lo is lo, chosen_hi is None)
    # unlinked gates keep the coordinator's own floor
    pu, bu, cu = make(False)
    out["unlinked"] = (pu.set_axis_floor(lo), cu.min_pf)
    out["no_set"] = p.set_axis_floor(None)
    # DCA on Axis: the add is allowed while the Set's own stream passes at the Set's floor
    book.by_idx, book.sets = [lo], {lo.id: lo}
    p.dca = types.SimpleNamespace(on_sets=False, on_axis=True)
    p.sets.gate_reason = lambda st_: ""
    p.sets._execution_lanes = lambda st_: ["pos"]
    dpos = pt.Position("X", "LONG", 1, 100, now - 600, 99, 102, 100, set_id=lo.id, lane="pos", client_id="d1", base_entry=100, base_qty=1, set_hold_bars=10)
    dca_lo = p.dca_eligible(dpos)
    book.by_idx, book.sets = [hi], {hi.id: hi}
    dca_hi = p.dca_eligible(replace(dpos, set_id=hi.id))
    out["dca"] = (dca_lo, dca_hi)
    # Block: the group's PF threshold starts from the parent Set's floor (a group at PF 1.10 is proven for a Set whose floor is 1.04, not for the pooled 1.25)
    with tempfile.TemporaryDirectory() as d:
        from correctness import _block_cfg, _parent
        sb = SetBlockBook(_os.path.join(d, "b.json"))
        cfg = _block_cfg()
        cfg.pause_ratio, cfg.min_samples = 0, 8
        pos = _parent(4)
        import block_rules as brr
        sb.observe(pos, 100.4, T0 + 10, cfg, .12, 86400)
        led = sb.ledger(sb.rel_keys(sb.config(cfg), pos)[0], create=True)
        for g in [0.0023, 0.0002] * 4:                                                              # gross -> nets +0.11 % / -0.10 %: PF 1.10 on 8 results
            brr.add_obs(led["paper"].setdefault("1", brr.new_record()), g - 0.0012)
        at_default = sb.candidate(pos, 100.4, T0 + 11, cfg, .12, 86400)
        at_floor = sb.candidate(pos, 100.4, T0 + 12, cfg, .12, 86400, floor=1.04)
        at_high = sb.candidate(pos, 100.4, T0 + 13, cfg, .12, 86400, floor=1.20)
        out["block"] = (at_default is not None, at_floor is not None, at_high is not None)
    ok = (floors == (1.04, 1.2) and out["judge"][1:] == (False, True, False) and 1.04 < out["judge"][0] < 1.2 and out["choice"] == (True, True)
          and out["unlinked"] == (None, 1.25) and out["no_set"] is None and out["block"] == (False, True, False) and out["dca"] == (True, False))
    return ok, (f"Set floors at TP 0.2 % / 1.0 %: {floors}; a stream at PF {out['judge'][0]}: pooled floor allows={out['judge'][1]}, the 0.2 % Set's floor allows={out['judge'][2]}, "
                f"the 1.0 % Set's floor allows={out['judge'][3]}; Set choice (low-floor Set chosen, high-floor Set blocked) {out['choice']}; unlinked -> floor {out['unlinked'][0]} "
                f"(coordinator {out['unlinked'][1]}); Block candidate at the default / the Set's 1.04 / 1.20 floor {out['block']}; DCA on Axis (low-floor Set, high-floor Set) {out['dca']}")


def a_block_add_on_a_set_parent_is_admitted_at_that_sets_floor():
    """Pulse.maybe_set_block_adds end to end: the parent's own Real Set reads PF 1.10, the parent's floor is 1.04, the coordinator's is 1.25 (Axis on). The add
    is judged at the parent's floor; with the coordinator's floor it would be refused, and a stream under the parent's floor is refused too."""
    import json
    import time
    from dataclasses import replace
    from lanes import _Env, SYM
    from coord_engine import Coordinator
    res = {}
    for label, stream_pf_ok, floor in (("at the Set's floor 1.04", True, 1.04), ("floor 1.20 above the stream's PF", True, 1.20)):
        with _Env() as env:
            p, pos = env.p, env.pos
            p.equity = 1000
            st = p.sets.by_idx[0]
            pos.set_id, pos.lane, pos.pack = st.id, "pos", st.pack
            pos.base_entry, pos.base_qty, pos.set_hold_bars = pos.entry, pos.qty, 10
            p.sets.gate_reason = lambda st_: ""
            p.sets.main_on = p.sets.neg_on = p.sets.realst_on = True
            st.stage = {"pos": True, "negl": False}
            p.ensure_controls = lambda pos_: None
            p.block.min_samples, p.block.pause_ratio, p.block.volume_ratio = 3, 0, 1
            import block_rules as _br
            p.set_block.cfg = p.block_cfg = _br.clean_cfg({"blockCounts": [{"step": 1, "ratio": 1.0}] * 3, "blockRelations": ["set"], "blockSkipFirst": 0, "blockMinObservations": 3})
            co = Coordinator()
            co.load({}, {"axisLastEnabled": True, "axisPrevEnabled": False, "axisPauseEnabled": False, "axisContEnabled": False, "minPfLinked": True,
                         "setMinPf": 1.25, "minPf": 1.25})
            p.coord = co
            p.set_axis_floor = lambda st_: floor
            now = time.time()
            pattern = [0.0011, -0.0010] * 8
            st.exe = [(now - 60 * i, v) for i, v in zip(range(len(pattern), 0, -1), pattern)]
            for i in range(3):
                old = replace(pos, client_id=f"history{i}", block_fills=[], block_pending={})
                p.set_block.observe(old, 100.15, now - 300 + i * 60, p.block, .12, 86400)
                p.set_block.close(old, 101, now - 270 + i * 60, p.block, .12)
            p.px[SYM] = 100.15
            p.maybe_set_block_adds()
            res[label] = len(pos.block_fills)
            res[label + " pooled"] = co.judge([{"pnl_pct": v + 0.0012} for v in pattern], 0)["allow"]
    keys = list(res)
    ok = res[keys[0]] == 1 and res[keys[2]] == 0 and res[keys[1]] is False and res[keys[3]] is False
    return ok, f"adds made by the parent: at floor 1.04 {res[keys[0]]} (want 1), at floor 1.20 {res[keys[2]]} (want 0); the coordinator's own floor would have refused the 1.10 stream: {not res[keys[1]]}"


def set_lanes_do_not_scratch_unless_asked():
    """The scratch exit (one global rule: close at +0.16 % gross after 10 minutes) cut every Set's winners at about +0.04 % net whatever its own TP range.
    With setScratchEnabled off (the default) a Set lane leaves only by its own stop, target and time, in the replay and in the live lane alike; on, the scratch
    exit is back. Measured: executed PF 0.406 -> 0.584, next 50 orders 0.27 -> 0.56 (trade_sim, 3 days, 20 symbols, development window)."""
    out = {}
    for flag in (False, True):
        b = se.SetBook()
        b.load(overlay(**{**SMALL, "setStepMax": 8, "stratIndications": False, "stratShortRange": False, "stratGeneral": True, "pauseMode": "off",
                          "setScratchEnabled": flag}))
        b.bars["S0-USDT"] = synth_bars(93, 900, vol=0.003)
        hist = {st.id: [] for st in b.by_idx}
        b._replay_symbol("S0-USDT", hist, T0, None)
        reasons = {}
        for rows in hist.values():
            for r in rows:
                reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        out[flag] = (reasons.get("scratch+", 0), sum(reasons.values()), b.scratch_bars)
    ok = out[False][0] == 0 and out[False][1] > 100 and out[True][0] > 0 and out[False][2] >= se.SCRATCH_NEVER and out[True][2] < 1000
    return ok, f"scratch exits / orders / scratch bars: off {out[False]}, on {out[True]}"


def hold_auto_is_off_by_default_and_never_on_trailing_sets():
    """Hold Auto (a time exit chosen per Set from its last orders) executed far worse than no hold on both 3-day windows (0.412 against 0.879 and 0.323
    against 0.953, trade_sim with the engine's own lanes): the shipped overlays, both secure presets and the engine's default switch it off. Switched on,
    trailing Sets still never get a hold candidate."""
    import glob
    import json as _j
    files = sorted(glob.glob(_os.path.join(_os.path.dirname(se.__file__), "overlay-*.json")))
    root = _os.path.abspath(_os.path.join(_os.path.dirname(se.__file__), "..", ".."))
    files += [_os.path.join(root, "presets", f) for f in ("secure-minimal.json", "secure-minimal-expanded-research.json")]
    shipped = {_os.path.basename(f): _j.load(open(f)).get("holdAuto") for f in files}
    bare = se.SetBook()
    bare.load({"stratGeneral": True, "stratIndications": False, "setStepMax": 6})
    on = se.SetBook()
    on.load({"stratGeneral": True, "stratIndications": False, "setStepMax": 20, "holdAuto": True})
    trail_on = [st for st in on.by_idx if st.kind == "trail"]
    base_on = [st for st in on.by_idx if st.kind == "base"]
    ok = (len(shipped) == 6 and all(v is False for v in shipped.values()) and not bare.hold_auto and on.hold_auto and trail_on and base_on
          and all(on.auto_holds(st) == () for st in trail_on) and all(on.auto_holds(st) for st in base_on))
    return ok, f"shipped holdAuto {shipped}; engine default {bare.hold_auto}; switched on: trailing candidates {set(on.auto_holds(st) for st in trail_on)}, base Sets have candidates {all(on.auto_holds(st) for st in base_on)}"


def lane_order_decides_which_lane_a_signal_tries_first():
    """laneOrder: X is valid on the positive lane only, Y on the Real Negative lane only. "pos-neg" (the shipped order) opens under X, "neg-pos" under Y,
    "neg" never under X (positive lane closed) and "pos" never under Y; an unknown value keeps the shipped order. The live entry path (Pulse._scope_choice
    with an Axis) walks the same order."""
    got = {}
    for order in ("pos-neg", "neg-pos", "neg", "pos", "bogus"):
        b = _book(laneOrder=order)
        base = [s for s in b.by_idx if s.kind == "base" and s.pack == "general"]
        x, y = base[0], base[1]
        for st in b.by_idx:
            st.active, st.last_error = False, "off"
        _force_valid(b, x, 2.0, 100, (60, 2.0, 2.0, 2.0, 0.3), lane="pos")
        _force_valid(b, y, 2.0, 100, (60, 2.0, 2.0, 2.0, 0.3), lane="neg")
        pick = b.pick_any("general")
        got[order] = "X" if pick is x else "Y" if pick is y else None
    from common import PULSE
    src = open(_os.path.join(PULSE, "pulse_trader.py")).read()
    live = 'for lane in getattr(self.sets, "lane_order", ("pos", "neg")):' in src
    ok = got == {"pos-neg": "X", "neg-pos": "Y", "neg": "Y", "pos": "X", "bogus": "X"} and live
    return ok, f"opens under: {got}; live entry walks the book's lane order: {live}"


def trail_pick_decides_how_trailing_sets_compete():
    """trailPick: B (a base Set) and T (a trailing Set) are both valid on the positive lane, T ranks higher on its lane's Set, B lower. "after" (the
    shipped rule) opens under B (trailing only when no fixed-exit Set is valid), "together" under T (every kind by rank), "first" under T even when it
    ranks lower; with B invalid every order opens under T; an unknown value keeps "after". The simulator walks the same order (stage_eval reads
    book.kind_order), and the live entry goes through pick_any."""
    got = {}
    for order, t_better in (("after", True), ("together", True), ("together", False), ("first", False), ("bogus", True), ("after-alone", True)):
        b = _book(trailPick=order.replace("-alone", ""), rankByLane=True)      # fixtures pin the Base-tape rank off
        base = next(s for s in b.by_idx if s.kind == "base" and s.pack == "general")
        tr = next(s for s in b.by_idx if s.kind == "trail" and s.pack == "general")
        for st in b.by_idx:
            st.active, st.last_error = False, "off"
        hi, lo = (60, 3.0, 3.0, 3.0, 0.5), (60, 1.5, 1.5, 1.5, 0.1)
        if order != "after-alone":
            _force_valid(b, base, 2.0, 100, lo if t_better else hi)
        _force_valid(b, tr, 2.0, 100, hi if t_better else lo)
        pick = b.pick_any("general")
        got[f"{order}{'' if t_better else ' (T lower)'}"] = "B" if pick is base else "T" if pick is tr else None
    for order in ("first", "first-any"):
        # T valid only on the Real Negative lane, B on the positive lane: lanes outside ("first") open under B, kinds outside ("first-any") under T
        b = _book(trailPick=order, rankByLane=True)
        base = next(s for s in b.by_idx if s.kind == "base" and s.pack == "general")
        tr = next(s for s in b.by_idx if s.kind == "trail" and s.pack == "general")
        for st in b.by_idx:
            st.active, st.last_error = False, "off"
        _force_valid(b, base, 2.0, 100, (60, 3.0, 3.0, 3.0, 0.5), lane="pos")
        _force_valid(b, tr, 2.0, 100, (60, 3.0, 3.0, 3.0, 0.5), lane="neg")
        pick = b.pick_any("general")
        got[f"{order} (T neg only)"] = "B" if pick is base else "T" if pick is tr else None
    from common import ROOT
    sim = open(_os.path.join(ROOT, "scripts", "stage_eval.py")).read()
    sim_ok = sim.count("book.pick_sequence_for(pack)") >= 3 and 'for kinds in (("base", "exit"), ("trail",))' not in sim
    want = {"after": "B", "together": "T", "together (T lower)": "B", "first (T lower)": "T", "bogus": "B", "after-alone": "T",
            "first (T neg only)": "B", "first-any (T neg only)": "T"}
    ok = got == want and sim_ok and se.KIND_ORDER_DEFAULT == "after"
    return ok, f"opens under: {got}; simulator walks book.kind_order: {sim_ok}"


def trail_pick_by_pack_reaches_only_its_packs():
    """trailPickByPack: {"general": "first"} opens a general signal under the lower-ranked trailing Set while trailPick stays "after"; a group key
    ("group:trend") moves the trend packs (the trend extra pack, an extra.<trend family> pack, a trend group vote) and leaves general, breakout and a
    reversion family alone; the exact pack name wins over its group; an unknown order is ignored."""
    got = {}
    for name, by in (("general first", {"general": "first"}), ("group trend", {"group:trend": "first"}), ("bad", {"general": "sideways"})):
        b = _book(trailPick="after", trailPickByPack=by, rankByLane=True)
        base = next(s for s in b.by_idx if s.kind == "base" and s.pack == "general")
        tr = next(s for s in b.by_idx if s.kind == "trail" and s.pack == "general")
        for st in b.by_idx:
            st.active, st.last_error = False, "off"
        _force_valid(b, base, 2.0, 100, (60, 3.0, 3.0, 3.0, 0.5))
        _force_valid(b, tr, 2.0, 100, (60, 1.5, 1.5, 1.5, 0.1))
        pick = b.pick_any("general")
        got[name] = "T" if pick is tr else "B" if pick is base else None
    b = _book(trailPickByPack={"group:trend": "first", "group:break": "together", "extra.donch_c21": "after"})
    first, tog, after = se.KIND_ORDERS["first"], se.KIND_ORDERS["together"], se.KIND_ORDERS["after"]
    groups = {p: b.kind_order_for(p) for p in ("trend", "extra.supertrend_n14_t5", "extra.g_trend_min", "breakout", "extra.donch_n8_t1@T5",
                                                "extra.donch_c21", "general", "extra.willr_n8_t1")}
    want = {"trend": first, "extra.supertrend_n14_t5": first, "extra.g_trend_min": first, "breakout": tog, "extra.donch_n8_t1@T5": tog,
            "extra.donch_c21": after, "general": after, "extra.willr_n8_t1": after}
    ok = got == {"general first": "T", "group trend": "B", "bad": "B"} and groups == want
    return ok, f"general signal opens under: {got}; orders by pack equal the rule: {groups == want}"


CHECKS = [("lane_rank." + fn.__name__.replace("_", "-"), fn) for fn in (
    lane_order_decides_which_lane_a_signal_tries_first, trail_pick_decides_how_trailing_sets_compete, trail_pick_by_pack_reaches_only_its_packs,
    hold_auto_is_off_by_default_and_never_on_trailing_sets,
    set_lanes_do_not_scratch_unless_asked,
    a_block_add_on_a_set_parent_is_admitted_at_that_sets_floor,
    the_axis_block_and_dca_gates_judge_a_sets_stream_at_that_sets_floor,
    lane_rank_stats_equal_the_plain_reading, the_choice_among_valid_sets_reads_the_lanes_own_set,
    a_lane_set_without_orders_ranks_below_one_with_orders_and_the_key_follows_the_floor, simulator_and_engine_rank_valid_sets_the_same_way)]
