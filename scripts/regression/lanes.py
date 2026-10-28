"""Lanes suite: a Set position, run by the live trader, closes exactly as the replay that validated its Set (parity), and
no other strategy (Exit lanes, Block, DCA, the coordinator's rearrange) changes that outcome (independence).

The live side is a real Pulse on a stub venue, driven bar by bar with a fake clock. Each bar is fed as four ticks in the
pessimistic order the replay assumes (open, the adverse extreme, the favourable extreme, close), the close tick at the
bar's end. The replay side is SetLane.scan over the same bars with the book's own replay settings."""
from __future__ import annotations

import os
import random
import tempfile
import time as _real_time
import types

from common import PULSE, overlay, SMALL, synth_bars  # noqa: F401
import pulse_trader as pt
import set_engine as se


def _offline_contracts(want=None):
    """Tests never reach the venue: the contract list is a fixed, offline one (the requested names, else two)."""
    names = sorted(want) if want else ["BTC-USDT", "ETH-USDT"]
    return {s: pt.Contract(symbol=s, min_qty=0.001, step=0.001, qprec=3, pprec=6, min_usdt=5.0, max_lev=50) for s in names}


pt.load_contracts = _offline_contracts

SYM = "BTC-USDT"
T_BASE = 1_800_000_000.0          # minute-aligned
CONF = 0.9


class _Clock:
    """Stands in for the time module inside pulse_trader: time() is the test's clock, everything else is the real one."""

    def __init__(self, t: float) -> None:
        self.t = t

    def time(self) -> float:
        return self.t

    def __getattr__(self, k):
        return getattr(_real_time, k)


class _Venue:
    """Fills every order at 100 (the signal bar's close), accepts everything else. Nothing leaves the process."""

    path_cd: dict = {}

    def __init__(self) -> None:
        self.posts: list = []
        self.price = lambda: 100.0         # set to the current tick once the Pulse exists

    def get(self, path, extra=None):
        return {"code": 0, "data": {"balance": {"equity": "1000", "availableMargin": "900"}, "maxLongLeverage": "50",
                                    "maxShortLeverage": "50", "longLeverage": "50", "shortLeverage": "50"}}

    def post(self, path, body):
        self.posts.append((path, dict(body)))
        if path.endswith("/trade/order"):
            q = str(body.get("quantity") or "1")
            return {"code": 0, "data": {"order": {"orderId": str(len(self.posts)), "avgPrice": repr(float(self.price())), "executedQty": q,
                                                  "quantity": q, "clientOrderID": body.get("clientOrderID", "")}}}
        return {"code": 0, "data": {}}

    def batch_place(self, orders):
        return {"code": 0, "data": {"orders": [{"orderId": "9", "clientOrderID": o.get("clientOrderID", ""), "code": 0}
                                               for o in orders]}}

    def public(self, path, extra=None):
        return {"code": 0, "data": []}

    def delete(self, path, extra=None):
        return {"code": 0}

    def gather_public(self, *a, **k):
        return []


def _book(**extra):
    b = se.SetBook()
    b.load(overlay(**{**SMALL, **extra}))
    return b


def _replay_cfg(b):
    return {"entry_conf": b.entry_conf, "time_bars": b.hist_time_bars, "scratch_bars": b.scratch_bars,
            "scratch_min": b.scratch_min, "cooldown": b.cooldown_bars, "cost_pct": b.cost_pct,
            "honor_tp": bool(getattr(b, "hist_honor_tp", True)), "sl_floor_steps": b.sl_min_steps}


def _scaled(bars, i):
    """The same series scaled so bar i closes at exactly 100 (the venue's fill price)."""
    k = 100.0 / float(bars[i][3])
    return [[r[0] * k, r[1] * k, r[2] * k, r[3] * k, r[4]] for r in bars]


def replay(b, st, bars, i, d):
    """SetLane over the bars with one entry signal at bar i. (levels at entry, trade or None)"""
    lane = se.SetLane(st, SYM)
    dirs = [0] * len(bars)
    confs = [0.0] * len(bars)
    dirs[i], confs[i] = d, CONF
    cfg = _replay_cfg(b)
    lane.scan(0, i + 1, bars, dirs, confs, True, cfg, [i])
    o = dict(lane.open or {})
    trades = lane.scan(i + 1, len(bars), bars, dirs, confs, True, cfg, [])
    return (o.get("sl"), o.get("tp")), (trades[0] if trades else None)


def live(b, st, bars, i, d, others=False, on_cycle=None, dca=None, after_open=None):
    """The live trader on the same bars. others=True switches every other strategy on around the Set position: the Exit
    lanes (with a reversal signal against it), Block and DCA add-ons with a lane on the same symbol and side, and the
    coordinator's rearrange with a stronger signal waiting. Returns (levels at entry, (reason, bar, px) or None, trace)."""
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.time, pt.TRADES_PATH)
    clock = _Clock(T_BASE + (i + 1) * 60.0)
    with tempfile.TemporaryDirectory() as td:
        pt.TRADES_PATH = os.path.join(td, "trades.jsonl")
        pt.BLOCK_PATH = os.path.join(td, "block.json")
        pt.LEV_PATH = os.path.join(td, "lev.json")
        pt.OPEN_PATH = os.path.join(td, "open.json")
        pt.dump_cts_settings = lambda: {}
        pt.time = clock
        try:
            contracts = {SYM: pt.Contract(symbol=SYM, min_qty=0.001, step=0.001, qprec=3, pprec=8, min_usdt=5.0, max_lev=50)}
            venue = _Venue()
            p = pt.Pulse(venue, contracts)
            venue.price = lambda: p.px.get(SYM) or 100.0      # market orders fill at the tick
            live.pulse = p
            p.sets = b
            b.pick_any = lambda pack, _st=st: _st
            b.pick_trail = lambda pack: None
            p.boot_ts = 0.0
            p.px[SYM] = 100.0
            p.available = 900.0
            p.halted = False
            p.entry_sense = lambda *a, **k: None
            p.control_orders = False
            p.replace_sl = lambda pos_, price: True          # the venue accepts every stop move
            p.place(SYM, d, "ind:state:tf_combined:0.60:a2:src", CONF)
            pos = p.open.get(SYM)
            if pos is None:
                return None, None, []
            if after_open is not None:
                after_open(p, pos)
            side = 1 if pos.side == "LONG" else -1
            levels = (pos.entry * (1 - side * pos.sl_pct), pos.entry * (1 + side * pos.tp_pct))
            if others:
                p.exits.enabled = True
                p.exits.rev_on = p.exits.time_on = True
                p.exits.ignore_tp = True
                against = "short" if side > 0 else "long"
                p.indications = types.SimpleNamespace(primary=lambda s: types.SimpleNamespace(direction=against),
                                                      best=lambda s: None, settings={"enabled": True}, last={})
                p.block.enabled = True
                p.strat_block = True
                p.block.register_parent(SYM, pos.side, pos.qty, pos.entry)   # a lane left on this symbol and side
                p.dca.enabled = True
                p.coord.rearrange = True
                p.score = lambda s_: (side, "x", 0.99)
            if dca is not None:                    # the DCA lane on, with these overlay keys (cooldown 0)
                p.dca.load({"dcaEnabled": True, "dcaCooldownSeconds": 0, **dca})
                p.dca.enabled = True
            trace = []
            for bi in range(i + 1, len(bars)):
                o_, h_, l_, c_ = (float(x) for x in bars[bi][:4])
                ticks = (o_, l_, h_, c_) if side > 0 else (o_, h_, l_, c_)
                for k, (dt, px) in enumerate(zip((1.0, 20.0, 40.0, 60.0), ticks)):
                    clock.t = T_BASE + bi * 60.0 + dt
                    if k == 3:
                        p.last_closed_1m = {SYM: (int((T_BASE + bi * 60.0) // 60), c_)}
                    p.px[SYM] = px
                    p.manage()
                    if dca is not None and SYM in p.open:
                        p.maybe_dca_adds()
                    if others and SYM in p.open:
                        p.maybe_block_adds()
                        p.maybe_dca_adds()
                        swap = p.coord.pick_rearrange(p.rearrange_opens(), [(0.99, "ZZZ-USDT", 1, "x")], 1)
                        if swap and swap["from"] in p.open:
                            q = p.open[swap["from"]]
                            p.close_pos(q, p.px.get(q.symbol) or q.entry, "rearr")
                        if on_cycle:
                            on_cycle(p)
                    q = p.open.get(SYM)
                    if q is not None:
                        trace.append((bi, k, round(q.qty, 9), round(q.entry, 9), round(q.sl_pct, 12), round(q.tp_pct, 12), round(q.sl, 9), round(q.tp, 9),
                                      None if q.trail is None else round(q.trail, 9)))
                    else:
                        c = p.closed[-1]
                        return levels, (c.reason, bi, float(c.exit)), trace
            return levels, None, trace
        finally:
            pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.time, pt.TRADES_PATH = saved
            for k in ("pick_any", "pick_trail"):
                b.__dict__.pop(k, None)


def _same_exit(rep, liv, bars):
    """Same reason and bar; the price is the replay's for a close-priced exit, inside the bar for a level exit."""
    if rep is None or liv is None:
        return rep is None and liv is None
    reason, bi, px = liv
    if reason != rep["reason"] or bi != rep["exit_i"]:
        return False
    if reason in ("time", "scratch+"):
        return abs(px - rep["exit_px"]) < 1e-9
    lo, hi = float(bars[bi][2]), float(bars[bi][1])
    return lo - 1e-9 <= px <= hi + 1e-9


def _scenario_bars(kind, d):
    """Hand-made series around an entry at bar 5 (close 100) that end in one exit rule each."""
    flat = [[100.0, 100.01, 99.99, 100.0, 1.0] for _ in range(6)]
    s = d

    def bar(c, up=0.01, dn=0.01):
        return [c, c + up, c - dn, c, 1.0]
    if kind == "tp":
        return flat + [bar(100 + s * 0.05)] * 3 + [bar(100 + s * 3.0, 0.2, 0.2)] + [bar(100 + s * 3.0)] * 80
    if kind == "sl":
        return flat + [bar(100 - s * 0.05)] * 3 + [bar(100 - s * 3.0, 0.2, 0.2)] + [bar(100 - s * 3.0)] * 80
    if kind == "time":
        return flat + [bar(100 + s * 0.02)] * 420
    if kind == "scratch":
        return flat + [bar(100 + s * 0.02)] * 9 + [bar(100 + s * 0.20)] * 80
    if kind == "scratch-short":
        return flat + [bar(100 + s * 0.20)] * 80
    if kind == "trail":
        # rises past the arm in steps, then falls back through the trailing stop in a later bar
        up = [bar(100 + s * k * 0.15) for k in range(1, 9)]
        top = 100 + s * 8 * 0.15
        return flat + up + [bar(top - s * 0.6, 0.05, 0.05)] + [bar(top - s * 0.6)] * 80
    raise ValueError(kind)


def _pick(b, kind, want="base"):
    sets = [st for st in b.by_idx if st.pack == "indications" and st.kind == want]
    return sets[len(sets) // 2]


def set_lane_matches_the_replay_rule_by_rule():
    """Each exit rule, long and short: the live trader opens at the replay's levels and closes on the same bar with the
    same reason (and the same price for close-priced exits), with every other strategy switched on around it."""
    out, bad = {}, []
    for kind in ("tp", "sl", "time", "scratch", "scratch-short", "trail"):
        ignore_tp = kind == "trail"           # trailing Sets exist on lanes that ignore TP
        # scratchS under 8 minutes: the replay waits its floor of 8 bars, and so must live
        extra = {"scratchS": 180} if kind == "scratch-short" else {}
        for d in (1, -1):
            b = _book(exitIgnoreTp=ignore_tp, **extra)
            st = _pick(b, kind, "trail" if kind == "trail" else "base")
            bars = _scenario_bars(kind, d)
            (rsl, rtp), rep = replay(b, st, bars, 5, d)
            lv, liv, _ = live(_book(exitIgnoreTp=ignore_tp, **extra), st, bars, 5, d, others=True)
            lv_ok = lv is not None and abs(lv[0] - rsl) < 1e-9 and abs(lv[1] - rtp) < 1e-9
            ok = lv_ok and _same_exit(rep, liv, bars)
            out[f"{kind}{'+' if d > 0 else '-'}"] = (rep and rep["reason"], rep and rep["exit_i"], liv and liv[:2])
            if not ok:
                bad.append((kind, d, (rsl, rtp), lv, rep and (rep["reason"], rep["exit_i"], rep["exit_px"]), liv))
    return not bad, f"{out}" + (f" MISMATCH {bad}" if bad else "")


def set_lane_matches_the_replay_on_random_series():
    """Random series and every base Set of a pack: live and replay agree on the entry levels and on the exit (reason and
    bar, and the price for close-priced exits), TP honoured and ignored."""
    compared, bad, reasons = 0, [], {}
    for honor in (True, False):
        b0 = _book(exitIgnoreTp=not honor)
        sets = [st for st in b0.by_idx if st.pack == "indications" and st.kind == "base"]
        for seed in range(12):
            # calm, normal and fast markets, both drifts: every exit rule shows up
            vol = (0.0004, 0.0012, 0.0025)[seed % 3]
            raw = synth_bars(100 + seed, 420, start=100.0, drift=0.0003 * (1 if seed % 2 else -1) * (seed % 3 != 0), vol=vol)
            bars = _scaled(raw, 5)
            d = 1 if seed % 4 < 2 else -1
            for st in sets:
                (rsl, rtp), rep = replay(b0, st, bars, 5, d)
                lv, liv, _ = live(_book(exitIgnoreTp=not honor), st, bars, 5, d, others=False)
                compared += 1
                ok = lv is not None and abs(lv[0] - rsl) < 1e-9 and abs(lv[1] - rtp) < 1e-9 and _same_exit(rep, liv, bars)
                if rep:
                    reasons[rep["reason"]] = reasons.get(rep["reason"], 0) + 1
                if not ok:
                    bad.append((st.id, seed, honor, rep and (rep["reason"], rep["exit_i"]), liv and liv[:2]))
    return compared > 0 and not bad, f"compared={compared} exits by reason={reasons} mismatches={bad[:4]}"


def dca_is_an_optin_lane_that_leaves_the_set_lane_alone():
    """DCA is off by default and, even on, never adds to a Set position unless dcaOnSets says so. With it on, the add
    comes at a rung of the position's own stop distance, the Set keeps its replay stop and exit bar, the Set is scored on
    its base result (exit against the entry it opened at) and the DCA lane on its own add."""
    b0 = _book()
    st = _pick(b0, "sl")
    sl_f, _ = se.lane_fracs(st, b0.cost_pct, b0.sl_min_steps)
    flat = [[100.0, 100.01, 99.99, 100.0, 1.0] for _ in range(6)]

    def bar(c):
        return [c, c + 0.01, c - 0.01, c, 1.0]
    rung = 100 * (1 - 0.5 * sl_f)                           # past the auto rung (0.4 x the stop distance), above the stop
    stop = 100 * (1 - sl_f)
    # the final fall ends between the stop of the entry (99.76) and the stop of the averaged entry: only the Set's own level exits
    path = [100 - (100 - rung) * k / 6 for k in range(1, 7)] + [rung] * 3 + [stop - 0.02] + [stop - 0.02] * 40
    bars = flat + [bar(c) for c in path]
    (rsl, rtp), rep = replay(b0, st, bars, 5, 1)
    out = {}
    for name, cfg in (("off", None), ("on, Sets not allowed", {}), ("on, onSets", {"dcaOnSets": True})):
        lv, liv, trace = live(_book(), st, bars, 5, 1, dca=cfg)
        p = live.pulse
        qtys = sorted({t[2] for t in trace})
        out[name] = {"exit": liv and liv[:2], "qty": qtys, "dca closes": len(p.dca.closes), "set n": st.n if False else None,
                     "dca pnl": [round(r.get("pnl_pct"), 5) for r in p.dca.closes]}
    # on: the stop and TP on the position never move, and the DCA lane is scored on its add alone
    b2 = _book()
    seen = []
    orig = b2.on_live_close
    b2.on_live_close = lambda r: (seen.append(r), orig(r))[1]
    def far_venue_stop(p_, pos_):               # the venue stop sits far out: only the lane's own level can close the position
        pos_.sl = pos_.entry * 0.98
    lv2, liv2, trace2 = live(b2, st, bars, 5, 1, dca={"dcaOnSets": True}, after_open=far_venue_stop)
    p2 = live.pulse
    levels_fixed = len({(t[6], t[7]) for t in trace2}) == 1 and abs(trace2[0][6] - 98.0) < 1e-6
    c = p2.closed[-1]
    q0 = min(t[2] for t in trace2)
    qa = c.qty - q0
    fill = (c.entry * c.qty - 100.0 * q0) / qa if qa > 0 else 0.0
    expect = (c.exit - fill) / fill if fill else None
    scored = bool(p2.dca.closes) and expect is not None and abs(p2.dca.closes[-1]["pnl_pct"] - expect) < 1e-9 and abs(expect - c.pnl_pct) > 1e-5
    # the Set is scored on its own lane: exit against the entry it opened at, the base size, and the USDT result of that size
    sr = seen[-1] if seen else None
    base_ok = sr is not None and abs(sr.entry - 100.0) < 1e-9 and abs(sr.qty - q0) < 1e-9 and abs(sr.pnl_pct - (c.exit - 100.0) / 100.0) < 1e-9 \
        and abs(sr.pnl - pt.net_pnl_usdt(sr.pnl_pct, q0, 100.0, p2.position_cost_pct)) < 1e-12
    scored = scored and base_ok
    same_exit = levels_fixed and scored and all(v["exit"] == (rep["reason"], rep["exit_i"]) for v in out.values())
    untouched = len(out["off"]["qty"]) == 1 and len(out["on, Sets not allowed"]["qty"]) == 1 and out["on, Sets not allowed"]["dca closes"] == 0
    added = len(out["on, onSets"]["qty"]) == 2 and out["on, onSets"]["dca closes"] == 1 and out["on, onSets"]["dca pnl"][0] < 0
    return same_exit and untouched and added, f"replay exit={rep and (rep['reason'], rep['exit_i'])}; {out}"


def dca_switch_reaches_the_module_flag():
    """The overlay switch is what turns the DCA module on (off by default, off under the build-time kill switch)."""
    import modules
    import dca_engine
    on = modules.resolve({"dcaEnabled": True}).get("strategy.dca")
    off = modules.resolve({}).get("strategy.dca")
    saved = dca_engine.DCA_HARD_OFF
    dca_engine.DCA_HARD_OFF = True
    try:
        killed = modules.resolve({"dcaEnabled": True}).get("strategy.dca")
    finally:
        dca_engine.DCA_HARD_OFF = saved
    return on is True and off is False and killed is False, f"overlay on={on} default={off} kill switch={killed}"


def dca_auto_keeps_the_stop_of_the_entry_and_manual_follows_the_average():
    """Auto plan: the stop and TP of the entry stay put after an add (bounded risk, as the walk-forward measured it). The static
    plan keeps its documented behaviour: the stop and TP are rebuilt around the new average entry."""
    b0 = _book()
    st = _pick(b0, "sl")
    sl_f, _ = se.lane_fracs(st, b0.cost_pct, b0.sl_min_steps)
    flat = [[100.0, 100.01, 99.99, 100.0, 1.0] for _ in range(6)]
    rung = 100 * (1 - 0.5 * sl_f)
    path = [100 - (100 - rung) * k / 6 for k in range(1, 7)] + [rung] * 6
    bars = flat + [[c, c + 0.01, c - 0.01, c, 1.0] for c in path]

    def no_set(p, pos):                         # an Exit-lane position: it has no Set
        pos.set_id, pos.set_idx = "", -1
    out = {}
    for name, cfg in (("auto", {}), ("manual", {"dcaAuto": False, "dcaMaxSteps": 1, "dcaStepDistancesPct": [0.1], "dcaStepVolumeMultipliers": [3.0]})):
        lv, liv, trace = live(_book(), st, bars, 5, 1, dca=cfg, after_open=no_set)
        qty = sorted({t[2] for t in trace})
        levels = {(t[6], t[7]) for t in trace}
        out[name] = (len(qty), len(levels))
    ok = out["auto"] == (2, 1) and out["manual"][0] == 2 and out["manual"][1] >= 2
    return ok, f"(sizes seen, distinct (stop, TP) pairs) auto={out['auto']} manual={out['manual']}"


def per_step_hold_time_is_the_same_in_replay_and_live():
    """A step with its hold on leaves on its own time exit (minutes after the entry), replay and live on the same bar; the steps
    with the hold off keep the global time stop. The desk table: stepHoldOn / stepHoldMin, indexed by step - 1."""
    b0 = _book()
    st0 = _pick(b0, "time")
    step = int(st0.step)
    out, bad = {}, []
    for hold in (3, 12, 30):
        on = [False] * 12
        mins = [10] * 12
        on[step - 1], mins[step - 1] = True, hold
        for d in (1, -1):
            kw = {"stepHoldOn": on, "stepHoldMin": mins}
            b = _book(**kw)
            st = b.sets[st0.id]
            bars = _scenario_bars("time", d)
            (rsl, rtp), rep = replay(b, st, bars, 5, d)
            lv, liv, _ = live(_book(**kw), st, bars, 5, d, others=False)
            ok = rep is not None and liv is not None and rep["reason"] == "time" and liv[0] == "time" and liv[1] == rep["exit_i"] \
                and rep["exit_i"] - 5 == hold and st.hold_bars == hold
            out[f"{hold}{'+' if d > 0 else '-'}"] = (rep and rep["exit_i"] - 5, liv and liv[1] - 5)
            if not ok:
                bad.append((hold, d, rep and (rep["reason"], rep["exit_i"]), liv and liv[:2], st.hold_bars))
    # a step with the hold off keeps the global stop
    off_book = _book(stepHoldOn=[False] * 12, stepHoldMin=[5] * 12)
    off_ok = all(x.hold_bars == 0 for x in off_book.by_idx)
    return not bad and off_ok, f"step {step}: bars held (replay, live) by hold and side {out}; hold off keeps global={off_ok}" + (f" MISMATCH {bad}" if bad else "")


def auto_hold_chosen_for_a_set_is_the_exit_in_replay_and_live():
    """The hold the Auto Mode chose for one Set (st.hold_bars, hold_auto) is the time exit of the live position and of the replay on
    the same bar; another Set of the same step keeps the global time stop."""
    out, bad = {}, []
    for hold in (4, 20):
        for d in (1, -1):
            b, bl = _book(), _book()
            st, stl = _pick(b, "time"), None
            stl = bl.sets[st.id]
            other = next(x for x in b.by_idx if x.step == st.step and x.kind == "base" and x.id != st.id)
            st.hold_bars, st.hold_auto = hold, True
            stl.hold_bars, stl.hold_auto = hold, True
            bars = _scenario_bars("time", d)
            (rsl, rtp), rep = replay(b, st, bars, 5, d)
            lv, liv, _ = live(bl, stl, bars, 5, d, others=False)
            ok = rep is not None and liv is not None and rep["reason"] == "time" and liv[0] == "time" and liv[1] == rep["exit_i"] \
                and rep["exit_i"] - 5 == hold and other.hold_bars == 0
            out[f"{hold}{'+' if d > 0 else '-'}"] = (rep and rep["exit_i"] - 5, liv and liv[1] - 5)
            if not ok:
                bad.append((hold, d, rep and (rep["reason"], rep["exit_i"]), liv and liv[:2]))
    return not bad, f"bars held (replay, live) by chosen hold and side {out}" + (f" MISMATCH {bad}" if bad else "")


def other_strategies_never_change_a_set_position():
    """Differential: the same Set position on the same random series, once alone and once with the Exit lanes, Block, DCA
    and rearrange all active around it. Exit, every tick's size, entry, levels and trailing stop are identical."""
    compared, diffs, reasons = 0, [], {}
    for seed in range(12):
        vol = (0.0004, 0.0012, 0.003)[seed % 3]
        raw = synth_bars(300 + seed, 420, start=100.0, drift=0.0004 * (1 if seed % 2 else -1) * (seed % 3 != 0), vol=vol)
        bars = _scaled(raw, 5)
        d = 1 if seed % 2 else -1
        for want, ignore_tp in (("base", False), ("base", True), ("trail", True)):
            b0 = _book(exitIgnoreTp=ignore_tp)
            pool = [x for x in b0.by_idx if x.pack == "indications" and x.kind == want]
            st = pool[seed % len(pool)]
            alone = live(_book(exitIgnoreTp=ignore_tp), st, bars, 5, d, others=False)
            crowd = live(_book(exitIgnoreTp=ignore_tp), st, bars, 5, d, others=True)
            compared += 1
            reasons[alone[1] and alone[1][0]] = reasons.get(alone[1] and alone[1][0], 0) + 1
            if alone[1] != crowd[1] or alone[2] != crowd[2]:
                diffs.append((st.id, seed, alone[1], crowd[1]))
    return compared > 0 and not diffs, f"positions compared={compared} exits={reasons} differing={diffs[:3]}"


def exit_lane_closes_never_reach_a_set_and_a_set_close_reaches_only_its_own():
    """PF after Base is computed from each Set's own closes. A close of a position without a Set changes no Set; a close
    of a Set position changes that Set alone (count and gross profit or loss), and the Exit lanes score none of it."""
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH)
    with tempfile.TemporaryDirectory() as td:
        pt.TRADES_PATH = os.path.join(td, "trades.jsonl")
        pt.BLOCK_PATH = os.path.join(td, "block.json")
        pt.LEV_PATH = os.path.join(td, "lev.json")
        pt.OPEN_PATH = os.path.join(td, "open.json")
        pt.dump_cts_settings = lambda: {}
        try:
            b = _book()
            bars = _scaled(synth_bars(7, 900, start=100.0, vol=0.003), 0)
            b.bars = {SYM: bars}
            b.replay_all(now=T_BASE)

            def snap():
                return {st.id: (st.n, round(st.gp, 12), round(st.gl, 12), round(st.gate_pf, 12)) for st in b.by_idx}
            contracts = {SYM: pt.Contract(symbol=SYM, min_qty=0.001, step=0.001, qprec=3, pprec=8, min_usdt=5.0, max_lev=50)}
            p = pt.Pulse(_Venue(), contracts)
            p.sets = b
            p.px[SYM] = 100.0
            exit_n0 = sum(v.n for v in p.exits.lanes.values())
            before = snap()
            free = pt.Position(symbol=SYM, side="LONG", qty=1.0, entry=100.0, opened_at=_real_time.time() - 900, sl=99.0,
                               tp=101.0, peak=100.0, sl_pct=0.01, tp_pct=0.01, pack="indications", client_id="x1")
            p.open[SYM] = free
            p.close_pos(free, 99.0, "exit:hard", exchange=False)
            after_free = snap()
            exit_n1 = sum(v.n for v in p.exits.lanes.values())
            target = b.by_idx[3]
            setpos = pt.Position(symbol=SYM, side="LONG", qty=1.0, entry=100.0, opened_at=_real_time.time() - 900, sl=99.0,
                                 tp=101.0, peak=100.0, sl_pct=0.01, tp_pct=0.01, pack=target.pack, set_id=target.id,
                                 client_id="x2")
            p.open[SYM] = setpos
            p.close_pos(setpos, 99.0, "sl", exchange=False)
            after_set = snap()
            exit_n2 = sum(v.n for v in p.exits.lanes.values())
        finally:
            pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH = saved
    changed = [k for k in before if after_set[k] != after_free[k]]
    ok = after_free == before and changed == [target.id] and exit_n1 == exit_n0 + 1 and exit_n2 == exit_n1
    return ok, (f"Sets changed by an Exit-lane close={sum(1 for k in before if after_free[k] != before[k])}; by a Set close="
                f"{changed}; Exit lane orders {exit_n0}->{exit_n1}->{exit_n2}")


def effective_pf_is_the_pool_of_the_validated_sets():
    """The effective PF (PF after Base) is the gross profit over the gross loss of the validated Sets' own gate windows,
    the validated share is valid / total, and both hold per pack too."""
    b = _book(setMinPf=0.25, setGateMinTrades=10, setGateWindow=40, setMaxDdtHours=36)   # accounting, not edge: some Sets pass
    b.bars = {f"S{k}-USDT": _scaled(synth_bars(40 + k, 1500, start=100.0, vol=0.0015, drift=0.0003 * (k - 1)), 0)
              for k in range(4)}
    b.replay_all(now=T_BASE)
    snap = b.snapshot()
    pb = (snap.get("pfBase") or {})
    valid = [st for st in b.by_idx if b.gate_reason(st) == ""]
    gp = sum(st.gate_gp for st in valid)
    gl = sum(st.gate_gl for st in valid)
    want_pf = round(gp / gl, 4) if gl > 0 else None
    got = pb.get("base") or {}
    share = round(100.0 * len(valid) / max(1, len(b.by_idx)), 2)
    ok_pool = (want_pf is None or abs(float(got.get("pf") or 0) - want_pf) < 2e-4) and int(got.get("sets") or 0) == len(valid)
    ok_share = abs(float(pb.get("validatedPct") or 0) - share) < 0.01
    per_pack = []
    for pack, row in (pb.get("byPack") or {}).items():
        vs = [st for st in valid if st.pack == pack]
        n_all = sum(1 for st in b.by_idx if st.pack == pack)
        per_pack.append(int(row.get("valid") or 0) == len(vs) and int(row.get("total") or 0) == n_all)
    ok = ok_pool and ok_share and all(per_pack) and len(per_pack) > 0 and 0 < len(valid) < len(b.by_idx)
    return ok, f"effective PF={got.get('pf')} want={want_pf} sets={got.get('sets')}/{len(valid)} validated%={pb.get('validatedPct')} want={share} packs ok={per_pack}"


def set_lane_stops_at_its_own_level_when_the_venue_stop_sits_farther():
    """clamp_ctrl_price keeps a venue stop at least 0.2 % from the price, so a stop re-placed near the price sits farther
    out than the Set's level. The lane closes at the Set's own level (entry x (1 - sl_pct)), as the replay does."""
    out = {}
    for d in (1, -1):
        b = _book(exitIgnoreTp=False)
        st = _pick(b, "sl")
        flat = [[100.0, 100.01, 99.99, 100.0, 1.0] for _ in range(6)]
        lvl = 100.0 * (1 - d * b.lane_fracs(st)[0])
        # one bar that touches the Set's stop by a hair and closes back above it; then nothing happens for a while
        if d < 0:
            touch = [100.0, lvl + 0.001, 99.99, 100.0, 1.0]
        else:
            touch = [100.0, 100.01, lvl - 0.001, 100.0, 1.0]
        bars = flat + [touch] + [[100.0, 100.01, 99.99, 100.0, 1.0]] * 30

        def widen(p):                     # the venue stop pushed out by 1 %, as a clamp would
            q = p.open.get(SYM)
            if q is not None:
                q.sl = q.entry * (1 - d * (q.sl_pct + 0.01))
        _, rep = replay(b, st, bars, 5, d)
        _, liv, _ = live(_book(exitIgnoreTp=False), st, bars, 5, d, others=True, on_cycle=widen)
        out[d] = (rep and (rep["reason"], rep["exit_i"]), liv and liv[:2])
    ok = all(v[0] == ("sl", 6) and v[1] == ("sl", 6) for v in out.values())
    return ok, f"replay vs live (long, short): {out}"


def a_recovered_set_position_keeps_its_own_lane():
    """After a restart the trader adopts its positions from the venue. A Set position comes back with its Set's levels
    (set_engine.lane_fracs) and its own trailing: none for a base Set, its own arm and give for a trailing Set; never
    the default variants' trailing."""
    out = {}
    for want, ignore_tp in (("base", False), ("trail", True)):
        b = _book(exitIgnoreTp=ignore_tp)
        st = [x for x in b.by_idx if x.pack == "indications" and x.kind == want][0]
        saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH)
        with tempfile.TemporaryDirectory() as td:
            pt.TRADES_PATH = os.path.join(td, "trades.jsonl")
            pt.BLOCK_PATH = os.path.join(td, "block.json")
            pt.LEV_PATH = os.path.join(td, "lev.json")
            pt.OPEN_PATH = os.path.join(td, "open.json")
            pt.dump_cts_settings = lambda: {}
            try:
                venue = _Venue()
                contracts = {SYM: pt.Contract(symbol=SYM, min_qty=0.001, step=0.001, qprec=3, pprec=8, min_usdt=5.0, max_lev=50)}
                p = pt.Pulse(venue, contracts)
                p.sets = b
                p.px[SYM] = 100.0
                p.control_orders = False
                p.open = {}
                cid = p.cid("o", set_id=st.id, pack=st.pack, set_idx=st.idx)
                rows = [{"symbol": SYM, "positionSide": "LONG", "positionAmt": "1", "avgPrice": "100", "leverage": "0"}]
                venue.get = lambda path, extra=None: ({"code": 0, "data": rows} if path.endswith("/user/positions")
                                                      else _Venue.get(venue, path, extra))
                p.our_orders = lambda s_: [{"clientOrderID": cid, "positionSide": "LONG", "type": "STOP_MARKET"}]
                p.adopt_exchange_positions()
                pos = p.open.get(SYM)
            finally:
                pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH = saved
        sl_f, tp_f = b.lane_fracs(st)
        if pos is None:
            out[want] = None
            continue
        levels_ok = abs(pos.sl_pct - sl_f) < 1e-12 and abs(pos.tp_pct - tp_f) < 1e-12 and pos.set_id == st.id
        if want == "base":
            trail_ok = pos.trail_set_id == "" and not pos.trail_arm and not pos.trail_give
        else:
            trail_ok = (pos.trail_set_id == st.id and abs(pos.trail_arm - st.trail_arm / 100.0) < 1e-12
                        and abs(pos.trail_give - st.trail_give / 100.0) < 1e-12)
        out[want] = (levels_ok, trail_ok, pos.set_id, pos.trail_set_id, round(pos.trail_arm, 6), round(pos.trail_give, 6))
    ok = all(v and v[0] and v[1] for v in out.values())
    return ok, f"{out}"


def explicit_trail_variants_keep_their_own_arm():
    """An explicit trailVariants entry is lifted to the trailing floor (give at least PositionCost + trailMinSteps steps,
    arm at least the give). The trailing Set's id names the arm and give it trails with, to the cent: "0.3:0.1" lifted to
    0.36:0.36 must not be named 0.4:0.36 (the key once rounded the arm to 0.1 %)."""
    b = _book(trailVariants=["0.3:0.1", "0.6:0.48"], exitIgnoreTp=True)
    trails = [(st.id, st.trail_arm, st.trail_give) for st in b.by_idx if st.kind == "trail" and st.pack == "indications"]
    named = all(sid.endswith(f"tr{arm:.2f}:{give:.2f}") for sid, arm, give in trails)
    ok = len(trails) == 2 and named
    return ok, f"trailing Sets={trails}"

class _Env:
    """A Pulse on the venue stub with private state files, for the Block checks. slip: the venue fills market orders this
    far (a fraction) from the price the engine saw."""

    def __init__(self, slip: float = 0.0, carry: int = 0) -> None:
        self.slip = slip
        self.carry = carry              # partials Block carries into the parent that opens in __enter__

    def __enter__(self):
        self.saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH)
        self.td = tempfile.TemporaryDirectory()
        d = self.td.name
        pt.TRADES_PATH = os.path.join(d, "trades.jsonl")
        pt.BLOCK_PATH = os.path.join(d, "block.json")
        pt.LEV_PATH = os.path.join(d, "lev.json")
        pt.OPEN_PATH = os.path.join(d, "open.json")
        pt.dump_cts_settings = lambda: {}
        contracts = {SYM: pt.Contract(symbol=SYM, min_qty=0.001, step=0.001, qprec=3, pprec=8, min_usdt=1.0, max_lev=50)}
        self.venue = _Venue()
        p = pt.Pulse(self.venue, contracts)
        self.venue.price = lambda: (p.px.get(SYM) or 100.0) * (1.0 + self.slip)
        p.sets = _book()
        p.sets.pick_any = lambda pack: None            # no Set: the position belongs to the Exit lane
        p.sets.pick_trail = lambda pack: None
        p.boot_ts = 0.0
        p.px[SYM] = 100.0
        p.available = 900.0
        p.halted = False
        p.entry_sense = lambda *a, **k: None
        p.control_orders = False
        p.closed = []
        p.block.enabled = True
        p.strat_block = True
        p.block.max_stack = 3
        p.block_last_emit = 0.0
        p.cooldown = {}
        p.entries_blocked = lambda: False
        p.missing_controls = lambda q: False
        p.max_book_notional = lambda: 10_000.0
        p.notional_cap = lambda: 1_000.0
        if self.carry:
            import block_engine as be
            lane = be.BlockLane(symbol=SYM, side="LONG", base_qty=0.0, base_entry=100.0, active=False,
                                carried=self.carry, top=self.carry, v0=1.0, net=-1.0)
            p.block.lanes[p.block.key(SYM, "LONG")] = lane
            p.block.volume_ratio = 1.0
        p.place(SYM, 1, "gen:x", CONF)
        self.p = p
        self.pos = p.open.get(SYM)
        p.score = lambda s_: (1, "x", 0.9)
        return self

    def exit_closes(self, n, pnl_pct, set_id=""):
        for k in range(n):
            self.p.closed.append(pt.Closed(t=_real_time.time(), symbol="ETH-USDT", side="LONG", qty=1.0, entry=100.0,
                                           exit=100.0 * (1 + pnl_pct), pnl=pnl_pct * 100, pnl_pct=pnl_pct, reason="x",
                                           hold_s=60.0, set_id=set_id))

    def __exit__(self, *exc):
        pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH = self.saved
        self.td.cleanup()


def block_stacks_only_on_its_own_proven_lane():
    """Block's evidence is the PF of the positions it stacks on (closes without a Set), net of PositionCost. Winning Set
    closes are no evidence; too few Exit-lane closes is no evidence; enough winning Exit-lane closes are."""
    out = {}
    with _Env() as e:
        e.p.px[SYM] = 100.5
        e.exit_closes(30, 0.01, set_id="indications:1m:sl1.00:st3")      # strong Set results: not Block's lane
        e.p.maybe_block_adds()
        out["set evidence only"] = (e.pos.qty, e.p.exit_lane_pf())
        e.exit_closes(3, 0.01)                                            # 3 Exit-lane wins: under the minimum samples
        e.p.block_last_emit = 0.0
        e.p.maybe_block_adds()
        out["3 exit closes"] = (e.pos.qty, e.p.exit_lane_pf())
        e.exit_closes(5, 0.01)                                            # now proven
        e.p.block_last_emit = 0.0
        e.p.maybe_block_adds()
        out["8 exit closes"] = (e.pos.qty, e.p.exit_lane_pf())
    q0 = out["set evidence only"][0]
    ok = out["3 exit closes"][0] == q0 and out["8 exit closes"][0] > q0 and out["set evidence only"][1] == 0.0
    return ok, f"{out}"


def block_adds_only_into_strength():
    """A parent under one step (one PositionCost) of profit gets no add; from one step on it does."""
    out = {}
    for move in (-0.001, 0.0005, 0.0013, 0.004):
        with _Env() as e:
            e.exit_closes(10, 0.01)
            e.p.px[SYM] = 100.0 * (1 + move)
            q0 = e.pos.qty
            e.p.maybe_block_adds()
            out[move] = e.pos.qty > q0
    ok = out == {-0.001: False, 0.0005: False, 0.0013: True, 0.004: True}
    return ok, f"added by parent move: {out}"


def block_fill_updates_the_position_and_its_stop():
    """An add moves the position to the summed size and the size-weighted entry, records the leg and the confirmed add,
    and the stop and target follow the new entry at the position's own distances."""
    with _Env() as e:
        e.exit_closes(10, 0.01)
        e.p.px[SYM] = 100.5
        q0, en0 = e.pos.qty, e.pos.entry
        e.p.maybe_block_adds()
        lane = e.p.block.lanes.get(e.p.block.key(SYM, "LONG"))
        added = e.pos.qty - q0
        want_entry = (q0 * en0 + added * 100.5) / (q0 + added)
        sl, tp = e.p.security_prices(e.pos)
        ok = (added > 0 and abs(e.pos.entry - want_entry) < 1e-9 and lane is not None
              and abs(lane.confirmed_add - added) < 1e-9 and len(lane.legs) == 1
              and abs(sl - e.pos.entry * (1 - e.pos.sl_pct)) < 1e-6 * e.pos.entry)
        return ok, (f"qty {q0}->{e.pos.qty} entry {en0}->{round(e.pos.entry, 6)} want {round(want_entry, 6)} "
                    f"legs={len(lane.legs) if lane else None} confirmed={lane and lane.confirmed_add} stop={round(sl, 6)}")


def block_lane_resets_on_its_parent_close_and_ignores_set_closes():
    """The parent's close records its result for Block, resets the lane (no legs, no confirmed add) and pauses the counts
    it used. A Set position's close on the same symbol and side changes nothing in Block or DCA."""
    with _Env() as e:
        e.exit_closes(10, 0.01)
        e.p.px[SYM] = 100.5
        e.p.maybe_block_adds()
        k = e.p.block.key(SYM, "LONG")
        lane = e.p.block.lanes[k]
        legs_before = len(lane.legs)
        ring0 = list(lane.parent_pf_ring)
        dca0 = len(e.p.dca.closes)
        setpos = pt.Position(symbol=SYM, side="LONG", qty=1.0, entry=100.0, opened_at=_real_time.time() - 900, sl=99.0,
                             tp=101.0, peak=100.0, sl_pct=0.01, tp_pct=0.01, pack="indications",
                             set_id="indications:1m:sl1.00:st3", client_id="s1")
        parent = e.p.open.pop(SYM)
        e.p.open[SYM] = setpos
        e.p.close_pos(setpos, 99.0, "sl", exchange=False)
        untouched = list(lane.parent_pf_ring) == ring0 and len(lane.legs) == legs_before and len(e.p.dca.closes) == dca0
        e.p.open[SYM] = parent
        e.p.close_pos(parent, 100.8, "exit:peak", exchange=False)
        lane = e.p.block.lanes[k]
        reset = (lane.legs == [] and lane.confirmed_add == 0.0 and lane.base_qty == 0.0 and not lane.active
                 and len(lane.parent_pf_ring) == len(ring0) + 1 and lane.pause_remaining.get(1, 0) >= 1)
    ok = legs_before == 1 and untouched and reset
    return ok, f"legs before={legs_before} Set close left Block/DCA untouched={untouched} parent close reset the lane={reset}"


def block_stack_stays_inside_the_book_cap():
    """However many counts pass, the stacked position never exceeds the book cap (max_book_notional, 5 % slack)."""
    with _Env() as e:
        e.exit_closes(25, 0.01)
        e.p.block.max_stack = 0                       # unlimited counts
        e.p.max_book_notional = lambda: 400.0
        e.p.notional_cap = lambda: 150.0
        e.pos.qty = 3.0                               # a 300 USDT parent: the 400 USDT cap binds after one add
        e.p.block.lanes[e.p.block.key(SYM, "LONG")].base_qty = 3.0
        for k in range(12):
            e.p.px[SYM] = 100.5 + 0.1 * k
            e.p.block_last_emit = 0.0
            e.p.maybe_block_adds()
        notional = e.pos.qty * e.p.px[SYM]
    ok = 400.0 * 0.5 < notional <= 400.0 * 1.05 and e.pos.qty > 3.0
    return ok, f"stack qty={round(e.pos.qty, 3)} notional={round(notional, 2)} cap=400"


def block_min_order_bump_does_not_break_the_book_cap():
    """Near the cap the computed add is under the venue's minimum order, so the add is bumped to the minimum. A bump that
    would carry the position more than 5 % over the book cap is refused."""
    with _Env() as e:
        e.exit_closes(10, 0.01)
        e.p.contracts[SYM].min_qty = 0.3              # a 30 USDT minimum order at 100
        e.p.max_book_notional = lambda: 400.0
        e.p.notional_cap = lambda: 150.0
        e.pos.qty = 3.98                              # 398 USDT: the room is 2 USDT, the bump would end at about 430
        e.p.block.lanes[e.p.block.key(SYM, "LONG")].base_qty = 3.98
        e.p.px[SYM] = 100.5
        e.p.maybe_block_adds()
        notional = e.pos.qty * e.p.px[SYM]
    ok = abs(e.pos.qty - 3.98) < 1e-9 and notional <= 400.0 * 1.05
    return ok, f"qty={round(e.pos.qty, 3)} notional={round(notional, 2)} cap=400 (+5 %)"


def a_venue_order_at_the_level_is_left_to_fill():
    """With a live venue stop (or TP) at the lane's level, a tick at the level sends no market close: the venue fills
    there, and a second close would race it ("position not exist" errors, which the venue counts toward an order-API ban).
    One PositionCost beyond the level the software close acts as a backstop. A venue stop that sits farther out than the
    lane's level does not hold the lane back. A close the venue answers "No position to close" counts as closed."""
    out = {}
    for case in ("stop at level", "stop backstop", "stop farther out", "tp at level", "tp backstop"):
        with _Env() as e:
            e.p.control_orders = True
            pos = e.pos
            pos.set_id = "indications:1m:sl1.00:st3"
            pos.sl_pct, pos.tp_pct = 0.004, 0.004
            pos.entry = 100.0
            pos.sl, pos.tp = 99.6, 100.4
            pos.sl_oid = pos.tp_oid = "123"
            e.p.sets.hist_honor_tp = True
            cost = 0.0012
            if case == "stop at level":
                px = 99.6
            elif case == "stop backstop":
                px = 99.6 * (1 - cost) - 1e-6
            elif case == "stop farther out":
                pos.sl = 99.0                         # the venue stop was pushed out by the 0.2 % clamp
                px = 99.6
            elif case == "tp at level":
                px = 100.4
            else:
                px = 100.4 * (1 + cost) + 1e-6
            e.p.px[SYM] = px
            e.p.manage()
            out[case] = SYM not in e.p.open
    with _Env() as e:
        e.venue.post = lambda path, body: {"code": 101205, "msg": "No position to close"}
        ok_closed, _ = e.p.market_close(e.pos)
    want = {"stop at level": False, "stop backstop": True, "stop farther out": True, "tp at level": False, "tp backstop": True}
    ok = out == want and ok_closed
    return ok, f"closed by software: {out}; 'No position to close' counts as closed={ok_closed}"


class _FilledOrderVenue(_Venue):
    """The venue after a position's own stop or TP filled: the position is gone, the order reports FILLED."""

    def __init__(self, filled_oid: str, fill_px: float) -> None:
        super().__init__()
        self.filled_oid = filled_oid
        self.fill_px = fill_px
        self.flat = False

    def get(self, path, extra=None):
        if path.endswith("/user/positions"):
            return {"code": 0, "data": [] if self.flat else [{"symbol": SYM, "positionSide": "LONG", "positionAmt": "1"}]}
        if path.endswith("/trade/order"):
            oid = str((extra or {}).get("orderId") or "")
            st = "FILLED" if oid == self.filled_oid else "NEW"
            return {"code": 0, "data": {"order": {"orderId": oid, "status": st, "avgPrice": str(self.fill_px)}}}
        if path.endswith("/trade/allOrders"):
            return {"code": 0, "data": {"orders": [{"orderId": self.filled_oid, "status": "FILLED", "symbol": SYM,
                                                    "positionSide": "LONG", "executedQty": "1", "avgPrice": str(self.fill_px),
                                                    "clientOrderID": self.cid}]}}
        return _Venue.get(self, path, extra)


def a_venue_close_is_recorded_as_its_lanes_close():
    """When the venue closes a position by its own stop or TP, the trader records a normal close of that lane: reason sl
    or tp at the fill price, in the trade book and the stats, and the Set gets exactly one live close. This holds on all
    three paths that find the position gone: the ghost check, the stale-book drop and the fill sync."""
    out = {}
    for path in ("ghost", "stale", "stale-other-live", "sync"):
        with _Env() as e:
            pos = e.pos
            st = e.p.sets.by_idx[3]
            pos.set_id, pos.pack = st.id, st.pack
            pos.sl_oid, pos.tp_oid = "501", "502"
            pos.opened_at = _real_time.time() - 600
            venue = _FilledOrderVenue("502", 101.3)
            venue.cid = e.p.cid("t", pos=pos)
            e.p.api = venue
            e.p.control_orders = True
            seen = []
            orig = e.p.sets.on_live_close
            e.p.sets.on_live_close = lambda rec, _o=orig: (seen.append(rec), _o(rec))[1]
            if path == "ghost":
                venue.flat = True
                e.p.drop_ghost(pos, "ctrl-no-position")
            elif path == "stale":                      # the venue lists no position at all (third empty pass)
                venue.flat = True
                e.p._adopt_empty_n = 2
                e.p.adopt_exchange_positions()
            elif path == "stale-other-live":           # the venue lists other positions, not this one
                base_get = venue.get
                other = {"code": 0, "data": [{"symbol": "ETH-USDT", "positionSide": "LONG", "positionAmt": "1"}]}
                venue.get = lambda p_, x=None, _b=base_get: other if p_.endswith("/user/positions") else _b(p_, x)
                e.p.our_orders = lambda s_: []
                e.p.adopt_exchange_positions()
            else:
                e.p.sync_own_fills()
                e.p.sync_own_fills()                       # a second pass must not count it again
                venue.flat = True
                e.p.drop_ghost(pos, "ctrl-no-position")    # nor a later ghost check
            rec = [c for c in e.p.closed if c.symbol == SYM]
            out[path] = (SYM in e.p.open, [(c.reason, round(c.exit, 4)) for c in rec], len(seen))
    ok = all(v == (False, [("tp", 101.3)], 1) for v in out.values())
    return ok, f"(still open, recorded closes, Set live closes): {out}"


def a_fill_far_from_its_reference_price_is_closed_and_the_symbol_rests():
    """A fill far from the price the decision was made at (a broken or empty book: AMEMECOIN on x02 filled 11 % away and
    stopped out in 3 s, 18 times in an hour) is closed at once, not scored against any Set, and the symbol is kept off
    for 30 minutes. An ordinary fill stays open."""
    out = {}
    for slip in (0.0004, 0.11):
        with _Env(slip=slip) as e:
            seen = []
            e.p.sets.on_live_close = lambda rec: seen.append(rec)
            out[slip] = (SYM in e.p.open, len(e.p.closed), len(seen),
                         round(e.p.ignore_syms.get(SYM, 0.0) - _real_time.time()) if SYM in e.p.ignore_syms else 0)
    ok = out[0.0004][0] is True and out[0.0004][3] == 0 \
        and out[0.11][0] is False and out[0.11][1] == 0 and out[0.11][2] == 0 and out[0.11][3] > 1700
    return ok, f"slip -> (still open, scored closes, Set closes, ban seconds): {out}"


def a_symbol_that_keeps_losing_rests_longer_each_time():
    """The wait before a symbol may be entered again: COOLDOWN_S after a win; four times longer after each loss in a row
    (9, 36, 144, 576 s, capped at 30 min); a stop-out within 30 s of the entry bans it for 30 minutes."""
    p = pt.Pulse.__new__(pt.Pulse)
    p.position_cost_pct = 0.12
    p.ignore_syms = {}
    waits = [p.symbol_backoff("A-USDT", -0.01, 300.0, "sl") for _ in range(6)]
    after_win = p.symbol_backoff("A-USDT", 0.01, 300.0, "tp")
    loss_after_win = p.symbol_backoff("A-USDT", -0.01, 300.0, "sl")       # the count starts over after a win
    p.symbol_backoff("B-USDT", -0.0005, 300.0, "scratch+")             # a gross gain under the cost is a loss
    small = p.symbol_backoff("B-USDT", -0.0005, 300.0, "scratch+")
    p.symbol_backoff("C-USDT", -0.1, 3.0, "sl")                         # fast stop-out
    banned = p.ignore_syms.get("C-USDT", 0.0) - _real_time.time()
    ok = waits == [9.0, 36.0, 144.0, 576.0, 1800.0, 1800.0] and after_win == 9.0 and loss_after_win == 9.0 \
        and small == 36.0 and banned > 1700 and "A-USDT" not in p.ignore_syms
    return ok, f"waits after 6 losses={waits}; after a win={after_win}, next loss={loss_after_win}; cost-eaten gain twice={small}; fast stop-out ban={round(banned)}s"


def position_size_has_a_hard_ceiling():
    """sizeMaxNotional caps one position's notional whatever the balance or the factor. x02's demo balance (474,849 USDT)
    at 1 % once sized a position at 4,748 USDT, 2,000 times the 2.15 USDT it was validated at."""
    p = pt.Pulse.__new__(pt.Pulse)
    p.volume_factor, p.size_pct, p.equity = 1.0, 1.0, 474849.0
    p.size_max = 25.0
    capped = round(p.sized_notional(), 6)
    p.size_pct, p.volume_factor = 0.0, 20.0
    capped_fixed = round(p.sized_notional(), 6)
    p.size_max = 1e9
    p.size_pct, p.volume_factor = 1.0, 1.0
    free = round(p.sized_notional(), 6)
    ok = capped == 25.0 and capped_fixed == 25.0 and abs(free - 4748.49) < 1e-6
    return ok, f"capped={capped}; fixed target x20 capped={capped_fixed}; without the cap={free}"


def _eligible_set(b, idx, **fields):
    st = b.by_idx[idx]
    st.active, st.locked, st.last_error = True, False, ""
    st.gate_n, st.max_dd_s, st.evaluated_at = 50, 0.0, _real_time.time()
    for k, v in fields.items():
        setattr(st, k, v)
    return st


def stable_sets_with_many_orders_are_chosen_first():
    """Among the eligible Sets the first choice is one that passed on both halves of its gate window and has many orders
    behind its PF; a higher PF on few orders, or on one lucky half, ranks below it."""
    b = _book(setMinPf=1.1)
    base = [i for i, st in enumerate(b.by_idx) if st.pack == "indications" and st.kind == "base"]
    solid = _eligible_set(b, base[0], gate_pf=2.0, gate_pf_old=2.2, gate_pf_new=1.8, n=400)
    thin = _eligible_set(b, base[1], gate_pf=2.0, gate_pf_old=2.2, gate_pf_new=1.8, n=35)
    lucky = _eligible_set(b, base[2], gate_pf=6.0, gate_pf_old=11.0, gate_pf_new=1.0, n=400)     # one half under the floor
    fewer = _eligible_set(b, base[3], gate_pf=1.6, gate_pf_old=1.5, gate_pf_new=1.7, n=120)
    for st in (solid, thin, lucky, fewer):
        assert b.gate_reason(st) == "", (st.id, b.gate_reason(st))
    order = [st.id for st in b.valid_for("indications", "base")]
    want = [solid.id, fewer.id, thin.id, lucky.id][:4]
    ok_rank = order[:4] == want
    # the same ranking picks the Set for a new entry
    picked = b.pick("indications", kind="base")
    ok_pick = picked is not None and picked.id == solid.id
    return ok_rank and ok_pick, f"order={[o.split(':')[-1] for o in order[:4]]} want={[o.split(':')[-1] for o in want]} pick={picked and picked.id.split(':')[-1]}"


def replay_starts_with_the_symbols_that_hold_the_most_orders():
    """A pass that is cut short refreshed the tapes the Sets are judged on most: symbols are replayed by the number of
    orders they already hold, most first. A first pass (no orders yet) keeps the given order."""
    names = [f"S{k}-USDT" for k in range(4)]
    b = _book()
    b.bars = {s: _scaled(synth_bars(10 + k, 700, start=100.0, vol=0.002 + 0.001 * k), 0) for k, s in enumerate(names)}
    first_pass = []
    b.replay_all(now=T_BASE, on_step=lambda: first_pass.append(b.progress.symbol))
    counts = {s: sum(len(v) for v in b._hist_rows[s].values()) for s in names}
    want_first = max(names, key=lambda s: counts[s])
    seen = []
    b.last_run = 0.0
    b.replay_all(now=T_BASE, on_step=lambda: seen.append(b.progress.symbol), abort=lambda: len(seen) >= 1)
    ok = bool(seen) and seen[0] == want_first and first_pass[0] == names[0]
    return ok, f"orders={counts}; first replayed in a cut-short pass={seen[:1]} want={want_first}; first pass started with {first_pass[:1]}"


def scoring_does_not_depend_on_the_replay_order():
    """Equal exit times from different symbols are ordered by symbol, so the gate PF, drawdown time and rank of every Set
    are the same however the symbols were replayed."""
    names = [f"S{k}-USDT" for k in range(4)]
    bars = {s: _scaled(synth_bars(20 + k, 800, start=100.0, vol=0.002), 0) for k, s in enumerate(names)}
    out = []
    for order in (names, list(reversed(names))):
        b = _book(setMinPf=0.2, setGateMinTrades=10)
        b.bars = {s: bars[s] for s in order}
        b.replay_all(now=T_BASE, symbols=order)
        out.append([(st.id, round(st.gate_pf, 6), st.max_dd_s, st.gate_n, st.n) for st in b.by_idx])
    same = out[0] == out[1]
    return same, f"Sets compared={len(out[0])}; identical={same}"


def the_desk_index_reproduces_the_engines_pooled_pf():
    """The compact Set index carries the gate-window gross profit and loss and the orders, so a desk selection pools the PF
    exactly as SetBook._pf_split does: over every Set, per pack, with the validated share."""
    b = _book(setMinPf=0.25, setGateMinTrades=10, setGateWindow=40, setMaxDdtHours=36)
    b.bars = {f"S{k}-USDT": _scaled(synth_bars(40 + k, 1500, start=100.0, vol=0.0015, drift=0.0003 * (k - 1)), 0) for k in range(4)}
    b.replay_all(now=T_BASE)
    snap = b.snapshot()
    index, pb = snap["index"], snap["pfBase"]

    def pool(rows):
        gp, gl = sum(r["gp"] for r in rows), sum(r["gl"] for r in rows)
        pf = (gp / gl) if gl > 0 else (99.0 if gp > 0 else 0.0)
        return round(pf, 4), sum(r["gn"] for r in rows), len(rows)
    valid = [r for r in index if r["on"] == 1]
    got_base, want_base = pool(valid), (pb["base"]["pf"], pb["base"]["n"], pb["base"]["sets"])
    got_all, want_all = pool(index), (pb["unfiltered"]["pf"], pb["unfiltered"]["n"], pb["unfiltered"]["sets"])
    pct = round(100.0 * len(valid) / len(index), 2)
    packs_ok = True
    for pack, row in pb["byPack"].items():
        rows = [r for r in index if r["pack"] == pack]
        vs = [r for r in rows if r["on"] == 1]
        packs_ok &= pool(vs) == (row["base"]["pf"], row["base"]["n"], row["base"]["sets"]) and pool(rows)[0] == row["unfiltered"]["pf"]
        packs_ok &= abs(row["validatedPct"] - round(100.0 * len(vs) / len(rows), 2)) < 1e-9
    ranked = sorted((r for r in index if r["on"] == 1), key=lambda r: r["rk"])
    ranks_ok = [r["rk"] for r in ranked] == list(range(len(ranked))) and all(r["rk"] == -1 for r in index if r["on"] != 1)
    ok = got_base == want_base and got_all == want_all and abs(pb["validatedPct"] - pct) < 1e-9 and packs_ok and ranks_ok and len(valid) > 0
    return ok, f"effective {got_base} vs engine {want_base}; unfiltered {got_all} vs {want_all}; validated {pct}% vs {pb['validatedPct']}; packs ok={packs_ok}; ranks ok={ranks_ok}"


def _block(**cfg):
    import block_engine as be
    base = {"blockMaxStack": 6, "blockVolumeRatio": 0.5, "defaultMinPF": 1.1, "blockProfitFactorRatio": 0.8, "prevPosMinCount": 5}
    base.update(cfg)
    return be.BlockBook(os.path.join(tempfile.mkdtemp(prefix="ctsg-block-"), "block.json"), base)


def _stack(b, lane, groups_ok, max_orders=20):
    """Fill the lane the way the trader does, one order per evaluation, until Block stops asking. groups_ok: the groups
    whose PF decision passes (the others carry a losing observed PF). Returns the size of each order."""
    sizes = []
    for n in range(1, int(b.max_stack) + 1):
        lane.pf_ring[n] = [3.0] * 6 if n in groups_ok else [-1.0] * 6
    for _ in range(max_orders):
        row = b.pick_emit(b.evaluate_counts(lane, live_n=1, intern_pf=1.5))
        if not row:
            break
        sizes.append(round(row["requestedAddQty"], 9))
        b.record_fill(lane, row, row["requestedAddQty"], f"c{len(sizes)}", f"o{len(sizes)}")
    return sizes


def block_adds_one_partial_ratio_per_valid_group():
    """Additive from the original volume: every valid group adds ratio x V0 (here 0.5 x 10 = 5), never a cumulative jump,
    whichever groups before it were not valid. All 6 valid: 6 orders of 5, V0 x (1 + 6 x 0.5) = 40 in total."""
    out = {}
    for name, ok in (("all 6 valid", range(1, 7)), ("1 and 3 valid", {1, 3}), ("only 5 valid", {5}), ("none valid", set())):
        b = _block()
        lane = b.register_parent("X-USDT", "LONG", 10.0, 100.0)
        sizes = _stack(b, lane, set(ok))
        out[name] = (sizes, round(lane.base_qty + lane.confirmed_add, 6))
    want = {"all 6 valid": ([5.0] * 6, 40.0), "1 and 3 valid": ([5.0, 5.0], 20.0), "only 5 valid": ([5.0], 15.0),
            "none valid": ([], 10.0)}
    return out == want, f"orders and final volume by valid groups: {out}"


def block_no_row_asks_for_more_than_one_partial():
    """Whichever group is picked, one order is one partial ratio of V0: no row of the evaluation (a later group's target is
    cumulative) may request more, even when nothing was added yet."""
    b = _block()
    lane = b.register_parent("X-USDT", "LONG", 10.0, 100.0)
    for n in range(1, 7):
        lane.pf_ring[n] = [3.0] * 6
    rows = [r for r in b.evaluate_counts(lane, live_n=1, intern_pf=1.5) if r["kind"] == "regular"]
    asks = [round(r["requestedAddQty"], 9) for r in rows]
    return bool(rows) and max(asks) <= 5.0 + 1e-9, f"requested per group={asks} (a partial is 0.5 x 10 = 5)"


def history_survives_live_built_tail():
    """Rows built from live ticks (zero volume, sampled highs and lows) never equal the exchange's rows. Ingested as a tail
    after a full history they used to replace it with the last few minutes (1,920 bars -> 40), and every Set's tape with it."""
    import set_engine as se
    sb = se.SetBook.__new__(se.SetBook)
    sb.bars, sb.lookback, sb._lock = {}, 3000, __import__("threading").RLock()
    full = [[10.0 + 0.001 * (i % 97), 10.5, 9.5, 10.1 + 0.001 * (i % 89), 5.0] for i in range(1920)]
    sb.ingest_bars("X-USDT", full)
    tail = [[10.0 + 0.0003 * i, 10.2, 9.9, 10.05, 0.0] for i in range(40)]
    sb.ingest_bars("X-USDT", tail, tail=True)
    n = len(sb.bars["X-USDT"])
    sb.bars = {}
    sb.ingest_bars("Y-USDT", tail, tail=True)
    boot = len(sb.bars["Y-USDT"])
    return n == 1920 and boot == 40, f"history after a live-built tail: {n} (want 1920); bootstrap with no history: {boot} (want 40)"


def block_stack_is_capped_at_six_groups():
    """The default stack is 6 groups at ratio 1 of the original volume, and it never goes past maxStack: with every group
    valid the 7th partial is never asked for. Groups 2, 4, 6 valid add three partials of ratio x V0, not cumulative orders."""
    import block_engine as be
    d = be.BlockBook(os.path.join(tempfile.mkdtemp(prefix="ctsg-block-"), "b.json"), {})
    b = _block(blockMaxStack=6, blockVolumeRatio=1.0)
    lane = b.register_parent("X-USDT", "LONG", 4.0, 100.0)
    sizes = _stack(b, lane, set(range(1, 40)), max_orders=40)
    rows = b.evaluate_counts(lane, live_n=1, intern_pf=1.5)
    groups = max(r["blockCount"] for r in rows if r["kind"] == "regular")
    b2 = _block(blockMaxStack=6, blockVolumeRatio=1.0)
    lane2 = b2.register_parent("Y-USDT", "LONG", 4.0, 100.0)
    sizes2 = _stack(b2, lane2, {2, 4, 6})
    ok = d.max_stack == 6 and d.volume_ratio == 1.0 and len(sizes) == 6 and groups == 6 \
        and abs(lane.base_qty + lane.confirmed_add - 4.0 * 7) < 1e-9 \
        and sizes2 == [4.0] * 3 and abs(lane2.base_qty + lane2.confirmed_add - 16.0) < 1e-9
    return ok, (f"defaults stack={d.max_stack} ratio={d.volume_ratio}; orders={len(sizes)} groups evaluated={groups} final volume="
                f"{lane.base_qty + lane.confirmed_add} (V0 x 7 = 28); groups 2,4,6 valid -> orders {sizes2}")


def block_threshold_rises_with_the_valid_group_count():
    """The k-th valid group must clear the PF threshold of the stack it creates, so each valid group asks for more."""
    b = _block()
    lane = b.register_parent("X-USDT", "LONG", 10.0, 100.0)
    rows = [r for r in b.evaluate_counts(lane, live_n=1, intern_pf=9.0) if r["kind"] == "regular"]
    mins = [round(r["blockMinPF"], 4) for r in rows]
    inc = [round(r["cumulativeRatio"], 4) for r in rows]
    ok = mins == sorted(mins) and len(set(mins)) == 6 and inc == [round(0.5 * k, 4) for k in range(1, 7)]
    return ok, f"min PF by valid rank={mins}; cumulative ratio={inc}"


def block_stack_stays_increased_until_the_groups_net_is_back_to_equal():
    """The stack the last group reached stays for the next parents: a losing parent keeps the level and the original V0,
    the group's net accumulates over its parents, and only when that net is back to equal (>= 0) the next parent opens at
    the base volume again. The level never shrinks while the net is negative."""
    b = _block(blockVolumeRatio=0.5)
    steps = {}
    lane = b.register_parent("X-USDT", "LONG", 10.0, 100.0)
    _stack(b, lane, {1, 2, 3})                                          # three partials: 10 + 3 x 5 = 25
    steps["parent 1 stacked"] = (round(lane.base_qty + lane.confirmed_add, 6), len(lane.legs))
    b.on_parent_close("X-USDT", "LONG", -10.0)                          # loss
    steps["after loss -10"] = (b.lanes["X-USDT:LONG"].carried, round(b.lanes["X-USDT:LONG"].net, 6), b.parent_scale("X-USDT", "LONG"))
    lane = b.register_parent("X-USDT", "LONG", 25.0, 100.0)             # the parent opens at V0 x 2.5
    steps["parent 2 opens"] = (lane.base_qty, round(lane.confirmed_add, 6))
    b.on_parent_close("X-USDT", "LONG", +4.0)                           # net -6: still not equal
    steps["after win +4"] = (b.lanes["X-USDT:LONG"].carried, round(b.lanes["X-USDT:LONG"].net, 6), b.parent_scale("X-USDT", "LONG"))
    lane = b.register_parent("X-USDT", "LONG", 25.0, 100.0)
    sizes = _stack(b, lane, {1, 2, 3, 4})                               # the 4th valid group adds one more partial
    steps["parent 3 adds"] = (sizes, b.lanes["X-USDT:LONG"].carried)
    b.on_parent_close("X-USDT", "LONG", -1.0)                           # net -7, level 4 now
    steps["after loss -1"] = (b.lanes["X-USDT:LONG"].carried, round(b.lanes["X-USDT:LONG"].net, 6), b.parent_scale("X-USDT", "LONG"))
    lane = b.register_parent("X-USDT", "LONG", 30.0, 100.0)
    b.on_parent_close("X-USDT", "LONG", +7.0)                           # net 0: back to equal
    steps["after win +7 (net 0)"] = (b.lanes["X-USDT:LONG"].carried, b.lanes["X-USDT:LONG"].v0, b.lanes["X-USDT:LONG"].net, b.parent_scale("X-USDT", "LONG"))
    lane = b.register_parent("X-USDT", "LONG", 12.0, 100.0)             # a fresh base volume
    steps["parent 5 opens"] = (lane.base_qty, lane.confirmed_add)
    want = {
        "parent 1 stacked": (25.0, 3),
        "after loss -10": (3, -10.0, 2.5),
        "parent 2 opens": (10.0, 15.0),
        "after win +4": (3, -6.0, 2.5),
        "parent 3 adds": ([5.0], 3),                      # one more partial; the level (4) is carried when the parent closes
        "after loss -1": (4, -7.0, 3.0),
        "after win +7 (net 0)": (0, 0.0, 0.0, 1.0),
        "parent 5 opens": (12.0, 0.0),
    }
    bad = {k: (steps[k], want[k]) for k in want if steps[k] != want[k]}
    return not bad, f"{steps}" + (f" MISMATCH {bad}" if bad else "")


def block_winning_group_never_carries_and_the_memory_survives_parents():
    """A stacked parent that closes in profit starts the next one at the base volume (nothing to recover). The observed PF
    and parent ring of the lane survive from one parent to the next: they were thrown away with every new parent."""
    b = _block()
    lane = b.register_parent("X-USDT", "LONG", 10.0, 100.0)
    _stack(b, lane, {1, 2})
    b.on_parent_close("X-USDT", "LONG", +3.0)
    carried_after_win = b.lanes["X-USDT:LONG"].carried
    ring_before = {n: list(v) for n, v in b.lanes["X-USDT:LONG"].pf_ring.items()}
    parent_ring = list(b.lanes["X-USDT:LONG"].parent_pf_ring)
    same_object = b.register_parent("X-USDT", "LONG", 10.0, 100.0) is b.lanes["X-USDT:LONG"]
    kept = b.lanes["X-USDT:LONG"].pf_ring == ring_before and b.lanes["X-USDT:LONG"].parent_pf_ring == parent_ring
    ok = carried_after_win == 0 and b.parent_scale("X-USDT", "LONG") == 1.0 and same_object and kept and len(parent_ring) == 1
    return ok, f"carried after a win={carried_after_win}; lane reused={same_object}; observed PF and parent ring kept={kept} ({parent_ring})"


def block_carried_stack_survives_a_restart():
    """The carried level, the original volume and the group's net are saved with the lane and loaded again."""
    b = _block()
    lane = b.register_parent("X-USDT", "SHORT", 8.0, 100.0)
    _stack(b, lane, {1, 2})
    b.on_parent_close("X-USDT", "SHORT", -2.5)
    b2 = _block()
    b2.path = b.path
    b2.load()
    l2 = b2.lanes.get("X-USDT:SHORT")
    ok = l2 is not None and l2.carried == 2 and abs(l2.v0 - 8.0) < 1e-9 and abs(l2.net + 2.5) < 1e-9 and b2.parent_scale("X-USDT", "SHORT") == 2.0
    return ok, f"loaded carried={l2 and l2.carried} v0={l2 and l2.v0} net={l2 and l2.net} scale={b2.parent_scale('X-USDT', 'SHORT')}"


def a_parent_opens_at_the_carried_stack_level_and_each_add_is_one_partial():
    """With a stack carried (3 partials of ratio 1), the next parent without a Set opens at 4 x the base volume and the lane
    keeps the group's original V0; a Block add is one partial of that V0, not the cumulative increment."""
    sizes = {}
    for carry in (0, 3):
        with _Env(carry=carry) as e:
            lane = e.p.block.lanes.get(e.p.block.key(SYM, "LONG"))
            sizes[carry] = (round(e.pos.qty, 6), lane and round(lane.base_qty, 6), lane and round(lane.confirmed_add, 6))
    base_qty = sizes[0][0]
    with _Env() as e:
        e.exit_closes(10, 0.01)
        e.p.block.volume_ratio = 1.0
        e.p.px[SYM] = 100.5
        e.p.maybe_block_adds()
        add1 = round(e.pos.qty - base_qty, 6)
        lane = e.p.block.lanes[e.p.block.key(SYM, "LONG")]
        partial = round(lane.base_qty * 1.0, 6)
    ok = abs(sizes[3][0] - 4.0 * base_qty) <= 0.002 and sizes[3][1] > 0 and add1 <= partial * 1.06 and add1 > 0
    return ok, f"parent qty with no carry={sizes[0]}, with 3 carried={sizes[3]}; first add={add1} (one partial = {partial})"


CHECKS = [
    ("trader.lanes-parity-rule-by-rule", set_lane_matches_the_replay_rule_by_rule),
    ("trader.lanes-parity-random-series", set_lane_matches_the_replay_on_random_series),
    ("trader.lanes-own-stop-level", set_lane_stops_at_its_own_level_when_the_venue_stop_sits_farther),
    ("trader.lanes-venue-order-left-to-fill", a_venue_order_at_the_level_is_left_to_fill),
    ("trader.lanes-venue-close-recorded", a_venue_close_is_recorded_as_its_lanes_close),
    ("trader.lanes-recovered-set-keeps-its-lane", a_recovered_set_position_keeps_its_own_lane),
    ("processing.explicit-trail-variants-keep-their-arm", explicit_trail_variants_keep_their_own_arm),
    ("processing.stable-sets-with-many-orders-first", stable_sets_with_many_orders_are_chosen_first),
    ("processing.replay-starts-with-the-most-orders", replay_starts_with_the_symbols_that_hold_the_most_orders),
    ("processing.scoring-independent-of-replay-order", scoring_does_not_depend_on_the_replay_order),
    ("processing.desk-index-reproduces-pooled-pf", the_desk_index_reproduces_the_engines_pooled_pf),
    ("trader.badfill-closed-and-symbol-rests", a_fill_far_from_its_reference_price_is_closed_and_the_symbol_rests),
    ("trader.symbol-backoff-grows-with-losses", a_symbol_that_keeps_losing_rests_longer_each_time),
    ("trader.size-has-a-hard-ceiling", position_size_has_a_hard_ceiling),
    ("trader.block-carried-parent-and-partial-add", a_parent_opens_at_the_carried_stack_level_and_each_add_is_one_partial),
    ("block.one-partial-ratio-per-valid-group", block_adds_one_partial_ratio_per_valid_group),
    ("block.one-partial-per-order", block_no_row_asks_for_more_than_one_partial),
    ("trader.per-step-hold-same-in-replay-and-live", per_step_hold_time_is_the_same_in_replay_and_live),
    ("trader.auto-hold-same-in-replay-and-live", auto_hold_chosen_for_a_set_is_the_exit_in_replay_and_live),
    ("trader.dca-auto-keeps-stop-manual-follows-average", dca_auto_keeps_the_stop_of_the_entry_and_manual_follows_the_average),
    ("dca.overlay-switch-reaches-the-module", dca_switch_reaches_the_module_flag),
    ("trader.dca-optin-lane-leaves-the-set-lane-alone", dca_is_an_optin_lane_that_leaves_the_set_lane_alone),
    ("block.stack-capped-at-six-groups", block_stack_is_capped_at_six_groups),
    ("processing.history-not-truncated-by-live-rows", history_survives_live_built_tail),
    ("block.threshold-rises-with-valid-groups", block_threshold_rises_with_the_valid_group_count),
    ("block.stack-stays-until-net-is-back-to-equal", block_stack_stays_increased_until_the_groups_net_is_back_to_equal),
    ("block.win-never-carries-memory-survives", block_winning_group_never_carries_and_the_memory_survives_parents),
    ("block.carried-stack-survives-a-restart", block_carried_stack_survives_a_restart),
    ("trader.block-stacks-only-on-its-own-proven-lane", block_stacks_only_on_its_own_proven_lane),
    ("trader.block-adds-only-into-strength", block_adds_only_into_strength),
    ("trader.block-fill-updates-position-and-stop", block_fill_updates_the_position_and_its_stop),
    ("trader.block-lane-resets-and-ignores-set-closes", block_lane_resets_on_its_parent_close_and_ignores_set_closes),
    ("trader.block-stack-inside-book-cap", block_stack_stays_inside_the_book_cap),
    ("trader.block-min-order-bump-inside-book-cap", block_min_order_bump_does_not_break_the_book_cap),
    ("trader.lanes-differential-independence", other_strategies_never_change_a_set_position),
    ("trader.lanes-set-stats-own-closes-only", exit_lane_closes_never_reach_a_set_and_a_set_close_reaches_only_its_own),
    ("trader.lanes-effective-pf-is-validated-pool", effective_pf_is_the_pool_of_the_validated_sets),
]
