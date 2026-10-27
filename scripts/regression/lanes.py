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


def live(b, st, bars, i, d, others=False, on_cycle=None):
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
                        trace.append((bi, k, round(q.qty, 9), round(q.entry, 9), round(q.sl_pct, 12), round(q.tp_pct, 12),
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
    """A Pulse on the venue stub with private state files, for the Block checks."""

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
        self.venue.price = lambda: p.px.get(SYM) or 100.0
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


CHECKS = [
    ("trader.lanes-parity-rule-by-rule", set_lane_matches_the_replay_rule_by_rule),
    ("trader.lanes-parity-random-series", set_lane_matches_the_replay_on_random_series),
    ("trader.lanes-own-stop-level", set_lane_stops_at_its_own_level_when_the_venue_stop_sits_farther),
    ("trader.lanes-recovered-set-keeps-its-lane", a_recovered_set_position_keeps_its_own_lane),
    ("processing.explicit-trail-variants-keep-their-arm", explicit_trail_variants_keep_their_own_arm),
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
