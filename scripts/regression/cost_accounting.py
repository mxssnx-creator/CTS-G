"""Fee, slippage and cross-consumer accounting oracles. No network or orders."""
import types

from common import T0
from execution_cost import FeeSchedule, fill_fee, execution_result, adverse_slippage
from position_cost import last_n_cost_pf
from stats_report import enrich, _row


def fee_and_slippage_oracle():
    # Fees use each leg's own notional. Actual prices already include slippage.
    for side, entry, exit_ in (("LONG", 100.02, 101), ("SHORT", 99.98, 99)):
        e, _ = fill_fee({}, 2, entry, .0002, .0005, .12)
        x, _ = fill_fee({}, 2, exit_, .0002, .0005, .12)
        got = execution_result(entry, exit_, 2, side, e, x)
        expected = 2 * (exit_ - entry) * (1 if side == "LONG" else -1) - 2 * (entry + exit_) * .0005
        if abs(got["net"] - expected) > 1e-12:
            return False, str(got)
    maker = fill_fee({"isMaker": True}, 1, 100, .0002, .0005, .12)
    limit = fill_fee({"type": "LIMIT"}, 1, 100, .0002, .0005, .12)
    zero = fill_fee({"commission": 0}, 1, 100, .0002, .0005, .12)
    slip = adverse_slippage(100, 100.02, "BUY")
    ok = maker == (.02, "maker-rate") and limit == (.05, "taker-rate") and zero == (0, "reported-fee") and abs(slip - .0002) < 1e-12
    return ok, "entry and exit notionals, both sides, reported zero, maker proof, slippage counted in prices once"


def net_result_survives_consumers_and_cost_changes():
    from coord_engine import Coordinator
    from exit_engine import ExitBook
    from risk_variants import VariantBook
    from dca_engine import DcaBook
    rows = [{"t": T0 + i, "pnl": n * 100, "qty": 1, "entry": 100, "exit": 100.1,
             "pnl_pct": .001, "net_pnl_pct": n, "symbol": "X", "side": "LONG", "reason": "time",
             "hold_s": 60, "dca_n": 1} for i, n in enumerate((.0004, -.0002))]
    values = []
    for cost in (.12, .3):
        values.append(last_n_cost_pf(rows, 2, cost)["pf"])
        export = [enrich(_row(r), cost) for r in rows]
        if any(abs(r["netPnl"] - n["pnl"]) > 1e-12 for r,n in zip(export, rows)):
            return False, "report charged again"
        co = Coordinator(); co.position_cost_pct = cost
        if co.net_streak(rows) != 1:
            return False, "Axis uses different net"
        ex = ExitBook(); ex.cost_pct = cost
        vr = VariantBook(); vr.cost_pct = cost
        dc = DcaBook(); dc.cost_pct = cost
        for r in rows:
            ex.on_close(r); vr.on_close(r); dc.on_close(r)
        values.append(last_n_cost_pf(next(v.rows for v in ex.lanes.values() if v.rows), 2, cost)["pf"])
        values.append(last_n_cost_pf(next(iter(vr.sl_rows.values())), 2, cost)["pf"])
        values.append(last_n_cost_pf(dc.closes, 2, cost)["pf"])
    return values == [2.0] * 8, f"PF across costs and consumers={values}"


def rate_lookup_and_bybit_translation():
    calls = []
    api = types.SimpleNamespace(get=lambda path, params: calls.append((path, params)) or
        {"code": 0, "data": {"commission": {"takerCommissionRate": "0.0005", "makerCommissionRate": "0"}}})
    fees = FeeSchedule()
    fees.refresh(api, ["BTC-USDT"], T0)
    fees.refresh(api, ["BTC-USDT"], T0 + 1)
    rates = fees.rates("BTC-USDT", now=T0 + 1)
    from bybit_fast import FastBybit
    by = FastBybit.__new__(FastBybit)
    seen = []
    by.call = lambda *a, **k: seen.append((a,k)) or {"retCode": 0, "result": {"list": [
        {"symbol": "BTCUSDT", "makerFeeRate": "0.0002", "takerFeeRate": "0.00055"}]}}
    response = by.get("/openApi/swap/v2/user/commissionRate", {"symbol": "BTC-USDT"})
    ok = len(calls) == 1 and rates == (0.0, .0005, "account-rate") and response["data"]["commission"]["takerCommissionRate"] == "0.00055"
    return ok, f"one cached read; zero maker fee preserved; Bybit request={seen[0][0][1:]}"


def pulse_fill_to_stats():
    from lanes import _Env, SYM
    import pulse_trader as pt
    with _Env(slip=.0002) as env:
        p = env.p
        p.open.clear(); p.last_entry_ts = 0; p.cooldown = {}; p.equity = 1000
        contract = p.contracts[SYM]
        contract.maker_fee, contract.taker_fee = .0002, .0005
        st = p.sets.by_idx[0]
        p.sets.pick_any = lambda pack, **kwargs: st
        p.place(SYM, 1, "set:indications:fee-test", .9)
        pos = p.open[SYM]
        p.px[SYM] = 101
        p.close_pos(pos, 101, "time")
        rec = p.closed[-1]
        expected = rec.qty * (rec.exit - rec.entry) - rec.qty * (rec.entry + rec.exit) * .0005
        own = p.sets.sets[st.id].live[-1]
        exported = enrich(_row(rec), .3)
        ok = abs(rec.pnl - expected) < 1e-12 and abs(exported["netPnl"] - expected) < 1e-8
        ok = ok and abs(own["net_pnl_pct"] - expected / (rec.qty * rec.entry)) < 1e-12
        ok = ok and rec.fee_source == "exchange-rate-estimate" and rec.fees_usdt > 0
        return ok, f"cash error={rec.pnl-expected:.2g}; exported and Set net match; {rec.fee_source}"


def partial_close_retry_and_unfilled_close():
    import json
    import pulse_trader as pt
    from trader import _isolated, _PartialCloseVenue, _enter
    def run():
        venue = _PartialCloseVenue(fail_rest=True)
        p, sym = _enter(venue)
        pos = p.open[sym]
        qty = pos.qty
        pos.fee_maker, pos.fee_taker = .0002, .0005
        pos.entry_fee_usdt = pos.base_entry_fee_usdt = qty * pos.entry * .0005
        p.close_pos(pos, 101, "time")
        partial = abs(pos.qty - .6 * qty) < 1e-12 and not p.closed
        saved = json.load(open(pt.OPEN_PATH))[sym]
        recovered = pt.Position(**saved)
        p.open[sym] = recovered
        venue.fail_rest = False
        p.close_pos(recovered, 102, "time")
        rec = p.closed[-1]
        want = qty * (101.6 - 100) - qty * (100 + 101.6) * .0005
        ok = partial and abs(rec.qty - qty) < 1e-12 and abs(rec.exit - 101.6) < 1e-10 and abs(rec.pnl - want) < 1e-10
        q = pt.Position(sym, "LONG", 1, 100, T0, 99, 102, 100)
        p.api.post = lambda *args: {"code": 0, "data": {"order": {"avgPrice": "100", "executedQty": "0"}}}
        closed, _ = p.market_close(q)
        return ok and not closed and q.qty == 1, f"partial persisted={partial}; full close price={rec.exit}; cash error={rec.pnl-want:.2g}; zero fill stays open={not closed}"
    return _isolated(run)


def execution_outcomes_survive_caps_and_emergency_closes():
    from lanes import _Env, SYM
    from dataclasses import replace
    import pulse_trader as pt
    with _Env() as env:
        p, pos = env.p, env.pos
        p.px[SYM] = 99
        p.close_pos(pos, 99, "no-ctrl")
        rec = p.closed[-1]
        # Shrinking an entry budget or adding volume cannot erase past losses.
        big = replace(rec, qty=100, client_id=p.cid("o", set_id=rec.set_id), reason="oversized")
        p.closed.append(big)
        p.max_book_notional = lambda: .1
        retained = len(p.strategy_closes()) == 2 and all(r.pnl < 0 for r in p.strategy_closes())
        p.closed.clear()
        p._load_trade_history()
        restored = len(p.strategy_closes()) == 1
        return retained and restored, f"verified emergency loss retained={retained}; restart retains loss={restored}"


def unattributed_aggregate_fee_does_not_score_base():
    from common import SMALL, overlay
    from set_engine import SetBook
    b = SetBook(); b.load(overlay(**SMALL))
    st = b.by_idx[0]
    b.on_live_close({"set_id": st.id, "symbol": "X", "side": "LONG", "t": T0,
                     "pnl_pct": -.1, "net_pnl_pct": -.2, "base_entry": 100, "base_qty": 1, "exit": 101})
    return abs(st.live[-1]["pnl"] - .0088) < 1e-12, "old aggregate net cannot overwrite base's 1% gross less 0.12% estimate"


def execution_scope_counts_survive_restart():
    import json
    import time
    import pulse_trader as pt
    from trader import _isolated
    def run():
        p = pt.Pulse.__new__(pt.Pulse)
        p.track_prefix = pt.TAG
        rows = [{"t": time.time(), "set_id": "own", "pack": "general", "lane": lane,
                 "pnl": value, "conn": pt.CONN_SHORT, "block_fills": ([{}] if lane == "neg" else [])}
                for lane, value in (("pos", 1), ("pos", 0), ("neg", -2))]
        rows.append({**rows[0], "conn": "different-connection", "pnl": 100})
        with open(pt.TRADES_PATH, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        p._exec_boot()
        ok = p.exec_n["set"] == 3 and p.exec_n["lanePos"] == 2 and p.exec_n.get("lanePosLoss", 0) == 0
        ok = ok and p.exec_n["laneNegLoss"] == 1 and p.exec_n["blockAddFills"] == 1
        return ok and p.exec_scope["set:own:neg"] == [1, 0, 0.0, 2.0], f"restored counters={p.exec_n}; own negative scope={p.exec_scope['set:own:neg']}"
    return _isolated(run)


def acknowledged_close_waits_for_the_same_order():
    import pulse_trader as pt
    from trader import _isolated, _StubExchange, _enter
    def run():
        venue = _StubExchange(executed="1")
        p, sym = _enter(venue)
        pos = p.open[sym]
        sent = []
        pending = {"orderId": "delayed", "symbol": sym, "status": "NEW", "origQty": "1", "price": "100"}
        p.api.post = lambda path, body: sent.append(body) or {"code": 0, "data": {"order": pending}}
        p.api.get = lambda path, params=None: {"code": 0, "data": {"order": pending}}
        p.close_pos(pos, 100, "time")
        p.close_pos(pos, 100, "time")
        held = sym in p.open and not p.closed and len(sent) == 1 and bool(pos.close_pending)
        pending.update(status="FILLED", executedQty="1", avgPrice="101")
        p.close_pos(pos, 101, "time")
        return held and len(sent) == 1 and len(p.closed) == 1 and p.closed[-1].exit == 101, f"unknown held={held}; POST count={len(sent)}; final fills={len(p.closed)}"
    return _isolated(run)


def pending_close_survives_position_refresh_and_restart():
    import json
    import pulse_trader as pt
    from trader import _isolated, _StubExchange, _enter
    def run():
        p, sym = _enter(_StubExchange(executed="1"))
        pos = p.open[sym]
        pos.opened_at -= 300
        pos.entry_fee_usdt = pos.base_entry_fee_usdt = .05
        pos.fee_taker = .0005
        sent = []
        pending = {"orderId": "delayed", "symbol": sym, "status": "PARTIALLY_FILLED",
                   "executedQty": "0.4", "avgPrice": "101"}
        positions = [{"symbol": sym, "positionSide": "LONG", "positionAmt": ".6", "avgPrice": "100"}]
        original_get = p.api.get
        def get(path, params=None):
            if path.endswith("/user/positions"):
                return {"code": 0, "data": positions}
            if path.endswith("/trade/order"):
                return {"code": 0, "data": {"order": pending}}
            return original_get(path, params)
        p.api.get = get
        p.api.post = lambda path, body: sent.append(body) or {"code": 0, "data": {"order": pending}}
        p.our_orders = lambda sym: []
        p.close_pos(pos, 101, "time")
        p.adopt_exchange_positions()
        intact = pos.qty == 1 and bool(pos.close_pending) and len(sent) == 1
        if not intact:
            return False, f"pending quantity overwritten: qty={pos.qty}, POSTs={len(sent)}"
        # A restart and an empty position list must settle the saved order,
        # rather than lose it as a ghost after two flat reads.
        recovered = pt.Position(**json.load(open(pt.OPEN_PATH))[sym])
        p.open[sym] = recovered
        positions.clear()
        p.reconcile_restored_book()
        retained = sym in p.open and bool(recovered.close_pending) and not p.closed
        pending.update(status="FILLED", executedQty="1", avgPrice="101.6", commission=".0508")
        p.reconcile_restored_book()
        rec = p.closed[-1] if p.closed else None
        ok = retained and rec is not None and rec.qty == 1 and abs(rec.pnl - 1.4992) < 1e-10
        ok = ok and rec.reason == "time" and len(sent) == 1
        return ok, f"pending quantity kept={intact}; flat unconfirmed retained={retained}; final net={getattr(rec, 'pnl', None)}; POSTs={len(sent)}"
    return _isolated(run)


def pending_partial_is_settled_once_then_manager_closes_remainder():
    import pulse_trader as pt
    from trader import _isolated, _StubExchange, _enter
    def run():
        p, sym = _enter(_StubExchange(executed="1"))
        pos = p.open[sym]
        pos.entry_fee_usdt = pos.base_entry_fee_usdt = .05
        pos.fee_taker = .0005
        sent = []
        pending = {"orderId": "delayed", "symbol": sym, "status": "PARTIALLY_FILLED",
                   "executedQty": "0.4", "avgPrice": "101"}
        def post(path, body):
            sent.append(body)
            row = pending if len(sent) == 1 else {"orderId": "remaining", "status": "FILLED", "executedQty": ".6", "avgPrice": "102"}
            return {"code": 0, "data": {"order": row}}
        original_get = p.api.get
        def get(path, params=None):
            if path.endswith("/user/positions"):
                # A stale venue snapshot must not restore the closed 40%.
                return {"code": 0, "data": [{"symbol": sym, "positionSide": "LONG", "positionAmt": "1", "avgPrice": "100"}]}
            if path.endswith("/trade/order"):
                return {"code": 0, "data": {"order": pending}}
            return original_get(path, params)
        p.api.post, p.api.get = post, get
        p.our_orders = lambda sym: []
        p.close_pos(pos, 101, "time")
        pending.update(status="CANCELED")
        p.px.clear()   # a pending fill can settle even without a new price tick
        p.manage()
        p.adopt_exchange_positions()
        partial = abs(pos.qty - .6) < 1e-12 and abs(pos.close_filled_qty - .4) < 1e-12 and not p.closed and len(sent) == 1
        p.manage()
        rec = p.closed[-1] if p.closed else None
        ok = partial and rec is not None and rec.qty == 1 and abs(rec.exit - 101.6) < 1e-10 and abs(rec.pnl - 1.4992) < 1e-10
        return ok and len(sent) == 2, f"partial intact after refresh={partial}; final exit={getattr(rec, 'exit', None)}; POSTs={len(sent)}"
    return _isolated(run)


def restart_does_not_seed_foreign_exit_lanes():
    from exit_engine import ExitBook
    from risk_variants import VariantBook
    base = {"t": T0, "pnl_pct": .01, "pnl": 1, "reason": "time", "sl_ratio": 1, "trail_key": "0.3:0.1"}
    ex, vr = ExitBook(), VariantBook()
    ex.seed([{**base, "set_id": "own-set"}, base])
    vr.seed_history([{**base, "set_id": "own-set"}, base])
    counts = (sum(v.n for v in ex.lanes.values()), sum(map(len, vr.sl_rows.values())), sum(map(len, vr.trail_rows.values())))
    return counts == (1, 1, 1), f"own default-lane rows only: Exit/SL/trailing={counts}"


CHECKS = [("cost-accounting." + fn.__name__.replace("_", "-"), fn) for fn in (
    fee_and_slippage_oracle, net_result_survives_consumers_and_cost_changes,
    rate_lookup_and_bybit_translation, pulse_fill_to_stats, partial_close_retry_and_unfilled_close,
    execution_outcomes_survive_caps_and_emergency_closes, unattributed_aggregate_fee_does_not_score_base,
    execution_scope_counts_survive_restart, acknowledged_close_waits_for_the_same_order,
    pending_close_survives_position_refresh_and_restart, pending_partial_is_settled_once_then_manager_closes_remainder,
    restart_does_not_seed_foreign_exit_lanes)]
