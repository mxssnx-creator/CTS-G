"""Independent paper/execution, Axis, Set Block and price-feature regression oracles."""
import json
import math
import os
import random
import tempfile
import time
import types
from dataclasses import replace

from common import SMALL, T0, overlay
import pulse_trader as pt
from set_engine import SetBook
from set_block import SetBlockBook
from market_features import price_window, multi_price_windows


def paper_actual_isolation():
    b = SetBook()
    b.load(overlay(**{**SMALL, "setGateShortN": 0, "realGateN": 10}))
    st = b.by_idx[0]
    rows = [(T0 + i * 60, "X", "LONG", .004, .0052, 30, "tp", st.id) for i in range(50)]
    b.set_processed(st, rows)
    keys = ("t", "symbol", "side", "pnl", "pnl_pct", "hold_s", "reason", "set_id")
    st.hist = [dict(zip(keys, r)) for r in rows]
    b._score_one(st, now=T0 + 3000)
    before = (st.gate_n, st.gate_pf, b._streams(st), b._pf_split()["stages"])
    for i in range(10):
        rec = {"set_id": st.id, "t": T0 + (40 + i) * 60, "symbol": "X", "side": "LONG",
               "pnl_pct": -.01, "lane": "pos", "hold_s": 30, "client_id": f"fill{i}"}
        b.on_live_close(rec)
        b.on_live_close(rec)
    after = (st.gate_n, st.gate_pf, b._streams(st), b._pf_split()["stages"])
    stats = b.execution_stats(st, "pos")
    ok = before == after and stats["n"] == 10 and stats["pf"] == 0 and not stats["allow"] and st.live_n == 10
    return ok, f"paper unchanged={before == after}; actual n={stats['n']} PF={stats['pf']} allow={stats['allow']}"


def config_change_invalidates_old_evidence():
    b = SetBook()
    ov = overlay(**SMALL)
    b.load(ov)
    st = b.by_idx[0]
    st.hist = [{"t": T0, "pnl_pct": .1, "pnl": .0988}]
    st.gate_pf = 99
    b.load(dict(ov))
    retained = bool(st.hist)
    b.load({**ov, "positionCostPct": .2})
    dirty = bool(st.last_error) and not st.hist and b.gate_reason(st) == "error" and st.gate_pf == 0
    return retained and dirty, f"identical settings retain data={retained}; cost change invalidates={dirty}"


def executed_restart_and_base_attribution():
    from trader import _isolated
    def run():
        p = pt.Pulse.__new__(pt.Pulse)
        p.sets = SetBook()
        p.sets.load(overlay(**{**SMALL, "realGateN": 200}))
        p.track_prefix = pt.TAG
        st = p.sets.by_idx[0]
        now = time.time()
        rows = []
        for i in range(220):
            row = {"t": now - 221 + i, "set_id": st.id, "symbol": "X", "side": "LONG", "lane": "pos",
                   "client_id": p.cid("o", set_id=st.id, set_idx=st.idx, pack=st.pack), "conn": pt.CONN_SHORT,
                   "pnl_pct": -.5, "base_entry": 100.0, "base_qty": 1.0, "exit": 101.0}
            rows.append(row)
        with open(pt.TRADES_PATH, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
            f.write("not json\n")
            for row in rows[-5:]:
                f.write(json.dumps(row) + "\n")
        p.seed_set_executions()
        p.seed_set_executions()
        stats = p.sets.execution_stats(st, "pos")
        return stats["n"] == 200 and st.live_n == 200 and all(abs(r["pnl_pct"] - .01) < 1e-12 for r in st.live), str(stats)
    return _isolated(run)


def axis_continuation_and_set_isolation():
    from coord_engine import Coordinator
    b = SetBook()
    b.load(overlay(**{**SMALL, "setGateShortN": 0, "mainGateEnabled": False,
                      "mainNegEnabled": False, "realGateEnabled": False}))
    p = pt.Pulse.__new__(pt.Pulse)
    p.sets, p.coord, p.position_cost_pct, p.open = b, Coordinator(), .12, {}
    p.coord.load({}, {"axisLastEnabled": True, "axisPrevEnabled": False, "axisPauseEnabled": False,
                      "axisContEnabled": True, "axisContMaxWindow": 2})
    pack = b.by_idx[0].pack
    bad, good = [x for x in b.by_idx if x.pack == pack and x.kind == "base"][:2]
    for st in b.by_idx:
        st.locked = st not in (bad, good)
    for st in (bad, good):
        st.gate_n, st.gate_pf, st.max_dd_s = 50, 3.0, 0
        st.score_at = T0
        st.exe = [(T0 - i * 60, -.01 if st is bad else .01) for i in range(10, 0, -1)]
    b.rank_key = lambda st: (100 if st is bad else 1,)
    chosen, _ = p._scope_pick(pack)
    p.open = {str(i): types.SimpleNamespace(set_id=good.id, lane="pos") for i in range(2)}
    capped, reason = p._scope_pick(pack)
    # Both lanes of one Set can validate. A failed/capped positive Axis must not
    # mask that SAME Set's independent Negative lane or relabel its execution.
    bad.locked = True
    b.main_on = b.neg_on = b.realst_on = True
    good.stage = {"pos": True, "negl": True, "main": True, "short": True, "real": True}
    good.neg = list(good.exe)
    good.exe = [(t, -.01) for t, _ in good.exe]
    alternate, lane, blocked = p._scope_choice(pack)
    both = alternate is good and lane == "neg" and not blocked
    return chosen is good and capped is None and bool(reason) and p.coord.slot_cap(0, 3) == 2 and both, f"independent alternative={chosen is good}; same-Set capacity={capped is None}; same-Set Negative fallback={both}"


def _block_cfg():
    return types.SimpleNamespace(max_stack=3, window=10, min_samples=3, volume_ratio=1.0,
                                 default_min_pf=1.25, pf_ratio=.8, pause_ratio=1)


def _parent(i=0, side="LONG"):
    return pt.Position("X", side, 1, 100, T0 + i * 180, 99, 102, 100, set_id="pack:1m:sl1:st4",
                       lane="pos", client_id=f"p{i}", base_entry=100, base_qty=1, set_hold_bars=10)


def block_independent_evidence_and_restart():
    with tempfile.TemporaryDirectory() as d:
        book = SetBlockBook(os.path.join(d, "b.json"))
        cfg = _block_cfg()
        for i in range(3):
            pos = _parent(i)
            book.observe(pos, 100.15, pos.opened_at + 10, cfg, .12, 86400)
            book.observe(pos, 100.4, pos.opened_at + 20, cfg, .12, 86400)
            book.close(pos, 100.35, pos.opened_at + 60, cfg, .12)
            book.close(pos, 100.35, pos.opened_at + 60, cfg, .12)  # duplicate close
        book = SetBlockBook(book.path)
        pos = _parent(4)
        first = book.candidate(pos, 100.4, pos.opened_at + 10, cfg, .12, 86400)
        pos.block_fills = [{"group": 1, "qty": 1, "price": 100.4}]
        second = book.candidate(pos, 100.4, pos.opened_at + 11, cfg, .12, 86400)
        other = replace(pos, set_id="other", block_fills=[])
        isolated = book.candidate(other, 100.4, pos.opened_at + 11, cfg, .12, 86400)
        lane = book.lanes[book.key(pos)]
        counts = [len(lane["paper"][str(n)]) for n in (1, 2)]
        return bool(first and first["group"] == 1) and second is None and isolated is None and counts == [3, 3], f"first={first}; group2/other blocked={second is None}/{isolated is None}; samples={counts}"


def block_actual_and_paper_costs():
    with tempfile.TemporaryDirectory() as d:
        b = SetBlockBook(os.path.join(d, "b.json"))
        cfg = _block_cfg()
        pos = _parent()
        pos.block_fills = [{"group": 1, "qty": 1, "price": 100.2}, {"group": 1, "qty": 2, "price": 100.5}]
        b.observe(pos, 100.15, T0 + 1, cfg, .12, 86400)
        b.close(pos, 101, T0 + 60, cfg, .12)
        lane = b.lanes[b.key(pos)]
        actual = lane["executed"]["1"][0][1]
        oracle = ((101 - 100.2) + 2 * (101 - 100.5)) / (100.2 + 2 * 100.5)
        stats = b.stats(lane, 1, "executed", T0 + 60, cfg, .12, 86400)
        return abs(actual - oracle) < 1e-12 and stats["n"] == 1 and stats["pf"] == 99, f"notional-weighted gross={actual:.9f} oracle={oracle:.9f}; one sample"


def block_pause_count_and_zero_are_respected():
    from block_engine import BlockBook
    with tempfile.TemporaryDirectory() as d:
        b = SetBlockBook(os.path.join(d, "sets.json"))
        cfg, pos = _block_cfg(), _parent()
        cfg.pause_ratio = 2
        lane = b.observe(pos, 100.4, T0 + 10, cfg, .12, 86400)
        lane["paper"]["1"] = [[T0 - 100 + i, .01] for i in range(5)] + [[T0 - 10, -.001], [T0 - 5, .01]]
        paused = b.candidate(pos, 100.4, T0 + 11, cfg, .12, 86400) is None
        lane["paper"]["1"].append([T0, .01])
        resumed = b.candidate(pos, 100.4, T0 + 12, cfg, .12, 86400) is not None
        loaded = BlockBook(os.path.join(d, "legacy.json"), {"blockPauseCountRatio": 0}).pause_ratio
        cfg.pause_ratio = loaded
        lane["paper"]["1"].append([T0 + 1, -.001])
        disabled = b.candidate(pos, 100.4, T0 + 13, cfg, .12, 86400) is not None
        return paused and resumed and disabled and loaded == 0, f"two-observation pause={paused}; resumed={resumed}; zero disables={disabled}"


def block_parent_unchanged_end_to_end():
    from lanes import _Env, SYM
    with _Env() as env:
        p, pos = env.p, env.pos
        p.equity = 1000
        st = p.sets.by_idx[0]
        pos.set_id, pos.lane, pos.pack = st.id, "pos", st.pack
        pos.base_entry, pos.base_qty, pos.set_hold_bars = pos.entry, pos.qty, 10
        p.sets.gate_reason = lambda st: ""
        p.sets.main_on = p.sets.neg_on = p.sets.realst_on = True
        st.stage = {"pos": True, "negl": True}
        p.ensure_controls = lambda pos: None
        p.block.min_samples = 3
        p.block.pause_ratio = 0
        p.block.volume_ratio = 1
        before = (pos.base_entry, pos.base_qty, pos.sl, pos.tp, pos.set_hold_bars, pos.opened_at)
        now = time.time()
        for i in range(3):
            old = replace(pos, client_id=f"history{i}", block_fills=[], block_pending={})
            p.set_block.observe(old, 100.15, now - 300 + i * 60, p.block, .12, 86400)
            p.set_block.close(old, 101, now - 270 + i * 60, p.block, .12)
        p.px[SYM] = 100.15
        # A different lane's eligibility cannot authorize this parent's add.
        st.stage = {"pos": False, "negl": True}
        p.maybe_set_block_adds()
        isolated = not pos.block_fills
        st.stage = {"pos": True, "negl": True}
        p.maybe_set_block_adds()
        after = (pos.base_entry, pos.base_qty, pos.sl, pos.tp, pos.set_hold_bars, pos.opened_at)
        grew = pos.qty > pos.base_qty and len(pos.block_fills) == 1
        saved = json.load(open(pt.OPEN_PATH))[SYM]
        recovered = pt.Position(**saved)
        p.close_pos(pos, 101, "tp", exchange=False)
        own = p.sets.sets[st.id].live[-1]
        rec = p.closed[-1]
        base_cash = (101 - before[0]) * before[1] - before[0] * before[1] * .0012
        add_cash = sum((101 - f["price"]) * f["qty"] - f["price"] * f["qty"] * .0012 for f in rec.block_fills)
        ok = isolated and grew and before == after and recovered.block_fills == pos.block_fills and abs(own["pnl_pct"] - .01) < 1e-12 and abs(rec.pnl - base_cash - add_cash) < 1e-10
        return ok, f"added={grew}, lane isolated={isolated}, parent unchanged={before == after}, base return={own['pnl_pct']:.6f}, aggregate cash error={rec.pnl-base_cash-add_cash:.2g}"


def drawdown_plain_oracle():
    rng = random.Random(15)
    closes = [100.0]
    for _ in range(300):
        closes.append(closes[-1] * (1 + rng.uniform(-.01, .01)))
    worst = 0
    for end in (100, 200, 300):
        data = closes[:end]
        for n, got in multi_price_windows(data).items():
            dd = [1 - data[k] / max(data[k - n + 1:k + 1]) for k in range(end - n, end)]
            du = [data[k] / min(data[k - n + 1:k + 1]) - 1 for k in range(end - n, end)]
            worst = max(worst, abs(got["drawdown"]-dd[-1]), abs(got["ulcer"]-math.sqrt(sum(x*x for x in dd)/n)),
                        abs(got["shortUlcer"]-math.sqrt(sum(x*x for x in du)/n)))
    flat = price_window([100] * 80, 20)
    return worst < 1e-12 and flat["ulcer"] == flat["shortUlcer"] == flat["efficiency"] == 0, f"12 windows vs quadratic oracle, max error={worst:.2g}; flat neutral"


def flat_rsi_and_relation_names():
    import indication_lib as lib
    import indication_extra as ix
    a = lib._rsi([100] * 100, 14)
    b = ix._rsi_series([100] * 100, 14)
    names = [f"{fam}_n{n}_t{tf}@DD20@LP3@I" for fam in ("ddresume", "ddpressure") for n in (5,8,14,21) for tf in (1,5,15,30)]
    return set(a) == set(b) == {50} and all(ix.known(n) for n in names), f"RSI flat=50; {len(names)} drawdown/last-part/inversion compositions resolve"


def fifty_symbol_processing():
    from common import synth_bars
    b = SetBook()
    b.load(overlay(**{**SMALL, "holdEnabled": False, "setStepAdapt": False,
                     **{k: True for k in ("stratShortRange", "stratTrend", "stratBreakout", "stratReversion", "stratFlow", "stratMulti")}}))
    for i in range(50):
        sym = f"S{i}-USDT"
        b.bars[sym] = synth_bars(i, 360, drift=.0005 if i % 2 else -.0005)
        b.bar_t[sym] = T0 - 60
    started = time.monotonic()
    b.replay_all(now=T0)
    checked = len(b.by_idx) * 50
    ids = [st.id for st in b.by_idx]
    ok = (b.progress.ready and b.progress.errors == 0 and b.progress.symbols_done == 50 and len(b.packs) == 8
          and len(ids) == len(set(ids)) and all(st.evaluated_at > 0 and not st.last_error for st in b.by_idx))
    return ok, f"50 symbols x {len(ids)} Sets = {checked} independent lanes, six hours each, {time.monotonic()-started:.2f}s"


def balance_sizing_and_block_budget():
    p = pt.Pulse.__new__(pt.Pulse)
    p.volume_factor, p.size_pct, p.size_max, p.available = .05, 1.0, 25.0, 10000
    p.dca = types.SimpleNamespace(enabled=False)
    p.block = types.SimpleNamespace(enabled=True, max_stack=6, volume_ratio=1.0)
    p.leverage_for = lambda c: 50
    amounts = []
    for bal in (10000, 20000, 100000):
        p.equity = bal
        amounts.append(p.sized_notional())
    p.equity = 10000
    budget = p.max_book_notional()
    return amounts == [5, 10, 25] and budget == 35, f"equity 10k/20k/100k -> notional {amounts}; six additive Block groups -> budget {budget}"


def frozen_hold_and_block_intent_failure():
    from lanes import _Env, SYM
    with _Env() as env:
        p, pos = env.p, env.pos
        st = p.sets.by_idx[0]
        pos.set_id, pos.base_entry, pos.base_qty, pos.set_hold_bars = st.id, pos.entry, pos.qty, 10
        st.hold_bars = 1
        p.software_exit = lambda *a: False
        p.last_closed_1m = {}
        calls = []
        p.close_pos = lambda *a, **k: calls.append(a[2])
        p.manage_set_lane(pos, pos.entry, time.time(), 120, 0)
        early = list(calls)
        p.manage_set_lane(pos, pos.entry, time.time(), 600, 0)
        p.sets.gate_reason = lambda st: ""
        p.set_block.candidate = lambda *a, **k: {"group": 1, "qty": pos.qty, "paper": {"pf": 2}}
        p.save_open_book = lambda **kw: False
        p.px[SYM] = pos.entry * 1.002
        before = len(env.venue.posts)
        p.maybe_set_block_adds()
        return not early and calls == ["time"] and len(env.venue.posts) == before, f"entry hold kept={not early and calls == ['time']}; no order without persisted intent={len(env.venue.posts)==before}"


def block_risk_budget_both_sides_and_invalid_stop():
    p = pt.Pulse.__new__(pt.Pulse)
    p.block_risk_pct, p.equity, p.position_cost_pct = 1.0, 1000.0, .12
    long = types.SimpleNamespace(side="LONG", entry=100.0, sl=99.0, qty=1.0)
    short = types.SimpleNamespace(side="SHORT", entry=100.0, sl=101.0, qty=1.0)
    want_long = (10.0 - 1.12) / (1.5 + 100.5 * .0012)
    want_short = (10.0 - 1.12) / (1.5 + 99.5 * .0012)
    got = (p.block_risk_qty(long, 100.5), p.block_risk_qty(short, 99.5))
    long.sl = 0.0
    invalid = p.block_risk_qty(long, 100.5)
    p.equity = .56
    tiny = p.block_risk_qty(short, 99.5)
    ok = abs(got[0] - want_long) < 1e-9 and abs(got[1] - want_short) < 1e-9 and invalid == tiny == 0.0
    return ok, f"long/short={got}, invalid stop={invalid}, tiny balance={tiny}"


def block_pending_recovery_requires_matching_order_and_position():
    from lanes import _Env
    with _Env() as env:
        p, pos = env.p, env.pos
        pos.set_id = p.sets.by_idx[0].id
        cid = p.cid("b", pos=pos)
        pos.block_pending = {"group": 1, "qty": .02, "client_id": cid, "t": time.time(),
                             "base_qty": pos.qty, "base_entry": pos.entry}
        original = dict(pos.block_pending)
        row = {"symbol": pos.symbol, "clientOrderId": "foreign", "side": "BUY", "positionSide": "LONG",
               "status": "FILLED", "executedQty": ".02", "avgPrice": "100.5"}
        p.confirmed_order = lambda *a: dict(row)
        foreign = not p.settle_set_block_pending(pos, pos.qty + .02) and not pos.block_fills
        row["clientOrderId"] = cid
        wrong_qty = not p.settle_set_block_pending(pos, pos.qty + .03) and not pos.block_fills
        accepted = p.settle_set_block_pending(pos, pos.qty + .02) and len(pos.block_fills) == 1
        pos.block_pending = original
        duplicate = p.settle_set_block_pending(pos, pos.qty + .02) and len(pos.block_fills) == 1
        return foreign and wrong_qty and accepted and duplicate, f"foreign={foreign}, qty mismatch={wrong_qty}, matched={accepted}, idempotent={duplicate}"


def strict_ownership_blocks_foreign_and_aggregate_changes():
    from lanes import _Env, SYM
    with _Env() as env:
        p, pos = env.p, env.pos
        p.strict_ownership = True
        p._ownership_verified.clear()
        p.our_orders = lambda *a, **k: []
        def snap(qty, include_foreign=False):
            rows = [{"symbol": SYM, "positionSide": "LONG", "positionAmt": str(qty), "avgPrice": str(pos.entry)}]
            if include_foreign:
                rows.append({"symbol": "FOREIGN-USDT", "positionSide": "SHORT", "positionAmt": "-1", "avgPrice": "20"})
            p.adopt_exchange_positions({"code": 0, "data": rows})
        snap(pos.qty, True)
        verified = p.ownership_safe(pos) and "FOREIGN-USDT" in p.foreign_symbols
        foreign_entry = pt.Pulse.entry_sense(p, "FOREIGN-USDT", 1, "gen:x", 1.0, "general") == "foreign-position"
        initial_qty, before = pos.qty, len(env.venue.posts)
        snap(pos.qty + .1, True)
        p.close_pos(pos, pos.entry, "time")
        p.ensure_controls(pos)
        conflict = pos.ownership_conflict and not p.ownership_safe(pos) and "ownership unresolved" in p.recon_detail
        untouched = pos.qty == initial_qty and len(env.venue.posts) == before
        return verified and foreign_entry and conflict and untouched, f"verified={verified}, foreign entry blocked={foreign_entry}, conflict held={conflict}, no order={untouched}"


def strict_control_cancellation_is_side_and_tag_scoped():
    from lanes import _Env, SYM
    with _Env() as env:
        p, pos = env.p, env.pos
        p.strict_ownership = True
        p._ownership_verified.add(SYM)
        own = p.cid("u", pos=pos)
        rows = [{"symbol": SYM, "positionSide": "LONG", "type": "STOP_MARKET", "orderId": "1", "clientOrderId": own},
                {"symbol": SYM, "positionSide": "SHORT", "type": "STOP_MARKET", "orderId": "2", "clientOrderId": own},
                {"symbol": SYM, "positionSide": "LONG", "type": "STOP_MARKET", "orderId": "3", "clientOrderId": "other-system"}]
        p.list_orders = lambda symbol=None: rows
        deleted = []
        p.api.delete = lambda path, body: deleted.append(body["orderId"]) or {"code": 0}
        p.cancel_controls(SYM)
        return deleted == ["1"], f"canceled order IDs={deleted}"


def strict_controls_are_quantity_bound_and_unsafe_existing_controls_are_held():
    from lanes import _Env, SYM
    with _Env() as env:
        p, pos = env.p, env.pos
        p.strict_ownership = True
        p._ownership_verified.add(SYM)
        p.control_orders = True
        pos.sl_oid = pos.tp_oid = pos.sec_sl_oid = pos.sec_tp_oid = ""
        requests = []
        def batch(orders):
            requests.extend(dict(o) for o in orders)
            return {"code": 0, "data": {"orders": [{"orderId": str(i+1), "type": o['type'],
                    "clientOrderID": o['clientOrderID'], "code": 0} for i,o in enumerate(orders)]}}
        p.api.batch_place = batch
        p.place_ctrl_pair(pos)
        quantity_bound = len(requests) == 2 and all("quantity" in o and "closePosition" not in o for o in requests)
        wrong = {"symbol": SYM, "positionSide": "LONG", "type": "STOP_MARKET", "orderId": "7",
                 "clientOrderId": p.cid("u", pos=pos), "closePosition": "true", "quantity": "0"}
        p.list_orders = lambda symbol=None: [wrong]
        deletes = []
        p.api.delete = lambda path, body: deletes.append(body) or {"code": 0}
        p.ctrl_skip = {}
        p.ensure_controls(pos)
        held = pos.ownership_conflict and not deletes
        return quantity_bound and held, f"quantity-bound pair={quantity_bound}; unsafe existing control held for review={held}"


def strict_block_add_waits_for_safe_control_resize():
    from lanes import _Env, SYM
    with _Env() as env:
        p, pos = env.p, env.pos
        p.strict_ownership = True
        p._ownership_verified.add(SYM)
        p.control_orders = True
        st = p.sets.by_idx[0]
        pos.set_id, pos.base_entry, pos.base_qty, pos.lane = st.id, pos.entry, pos.qty, "pos"
        p.px[SYM] = pos.entry * 1.01
        before = len(env.venue.posts)
        p.maybe_set_block_adds()
        ok = len(env.venue.posts) == before and not pos.block_pending and not pos.block_fills
        return ok, f"no add while protection cannot be resized safely={ok}"


def strict_close_does_not_fallback_to_entire_position():
    from lanes import _Env, SYM
    with _Env() as env:
        p, pos = env.p, env.pos
        p.strict_ownership = True
        p._ownership_verified.add(SYM)
        calls = []
        def rejected(path, body):
            calls.append(dict(body))
            return {"code": 100400, "msg": "quantity rejected"}
        p.api.post = rejected
        ok, _ = p.market_close(pos)
        safe = not ok and len(calls) == 1 and "quantity" in calls[0] and "closePosition" not in calls[0]
        return safe, f"attempts={len(calls)}, no whole-position fallback={safe}"


def strict_overall_control_fill_is_counted_only_when_complete():
    from lanes import _Env, SYM
    def run(partial):
        with _Env() as env:
            p, pos = env.p, env.pos
            p.strict_ownership = True
            p._ownership_verified.add(SYM)
            pos.sl_oid = pos.sec_sl_oid = "owned-stop-1"
            cid = p.cid("u", pos=pos)
            amount = pos.qty / 2 if partial else pos.qty
            row = {"symbol": SYM, "orderId": "owned-stop-1", "clientOrderId": cid, "positionSide": "LONG",
                   "status": "FILLED", "executedQty": str(amount), "avgPrice": "99"}
            p.api.get = lambda path, extra=None: {"code": 0, "data": {"orders": [row]}}
            cancels = []
            p.cancel_controls = lambda sym: cancels.append(sym)
            before = len(p.closed)
            p.sync_own_fills()
            return (SYM in p.open, pos.ownership_conflict, len(p.closed)-before, len(cancels))
    partial, full = run(True), run(False)
    ok = partial == (True, True, 0, 0) and full == (False, False, 1, 1)
    return ok, f"partial retained={partial}; complete overall-stop close={full}"


def strict_net_uses_only_tagged_connection_records():
    from lanes import _Env
    with _Env() as env:
        p = env.p
        p.strict_ownership = True
        cid = p.cid("o")
        base = {"t": time.time(), "symbol": "BTC-USDT", "side": "LONG", "reason": "time", "ours": True}
        rows = [{**base, "pnl": 2, "client_id": cid, "conn": pt.CONN_SHORT},
                {**base, "pnl": 20, "client_id": "other-system", "conn": pt.CONN_SHORT},
                {**base, "pnl": 30, "client_id": "", "conn": pt.CONN_SHORT},
                {**base, "pnl": 40, "client_id": cid, "conn": "other-connection"},
                {**base, "pnl": 50, "client_id": cid, "conn": pt.CONN_SHORT, "ours": False}]
        with open(pt.TRADES_PATH, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        p._exec_boot()
        ok = p.exec_n.get("exit") == 1 and p.exec_n.get("netUsdt") == 2
        return ok, f"verified closes={p.exec_n.get('exit')}, net={p.exec_n.get('netUsdt')}"


def manually_closed_position_is_quarantined_without_blocking_other_symbols():
    from lanes import _Env, SYM
    with _Env() as env:
        p, pos = env.p, env.pos
        p.strict_ownership = True
        p._ownership_verified.add(SYM)
        p.our_orders = lambda *a, **k: []
        p._exchange_flat = lambda held: True
        p.close_from_venue = lambda held, why: False
        p.cancel_controls = lambda *a, **k: None
        before_posts, before_closed = len(env.venue.posts), len(p.closed)
        p.adopt_exchange_positions({"code": 0, "data": [{"symbol": "OTHER-USDT", "positionSide": "SHORT", "positionAmt": "-1", "avgPrice": "20"}]})
        key = f"{SYM}:LONG"
        blocked = SYM not in p.open and key in p.external_closes
        p.place(SYM, 1, "gen:x", 1.0)
        no_reopen = len(env.venue.posts) == before_posts
        p.entries_blocked = pt.Pulse.entries_blocked.__get__(p)
        p.contracts["NEXT-USDT"] = pt.Contract(symbol="NEXT-USDT", min_qty=.001, step=.001, qprec=3, pprec=8, min_usdt=1, max_lev=50)
        p.px["NEXT-USDT"], p.price_ts["NEXT-USDT"] = 100.0, time.time()
        p.place("NEXT-USDT", 1, "gen:x", 1.0)
        clone = pt.Pulse.__new__(pt.Pulse)
        clone.external_closes, clone.external_close_state_bad = {}, False
        clone._load_external_closes()
        persistent = key in clone.external_closes and not clone.external_close_state_bad
        continued = p.recon_ok and "OTHER-USDT" in p.foreign_symbols and "NEXT-USDT" in p.open
        no_fake = no_reopen and len(p.closed) == before_closed and p.exec_n.get("dropped", 0) >= 1
        return blocked and persistent and continued and no_fake, f"blocked={blocked}, restart state={persistent}, different symbol opened={continued}, no fabricated result={no_fake}"


CHECKS = [("correctness." + fn.__name__.replace("_", "-"), fn) for fn in (
    paper_actual_isolation, config_change_invalidates_old_evidence, executed_restart_and_base_attribution,
    axis_continuation_and_set_isolation, block_independent_evidence_and_restart, block_actual_and_paper_costs,
    block_pause_count_and_zero_are_respected,
    block_parent_unchanged_end_to_end, drawdown_plain_oracle, flat_rsi_and_relation_names,
    fifty_symbol_processing, balance_sizing_and_block_budget, frozen_hold_and_block_intent_failure,
    block_risk_budget_both_sides_and_invalid_stop, block_pending_recovery_requires_matching_order_and_position,
    strict_ownership_blocks_foreign_and_aggregate_changes, strict_control_cancellation_is_side_and_tag_scoped,
    strict_controls_are_quantity_bound_and_unsafe_existing_controls_are_held, strict_block_add_waits_for_safe_control_resize,
    strict_close_does_not_fallback_to_entire_position, strict_overall_control_fill_is_counted_only_when_complete,
    strict_net_uses_only_tagged_connection_records,
    manually_closed_position_is_quarantined_without_blocking_other_symbols)]
