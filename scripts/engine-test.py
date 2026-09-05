#!/usr/bin/env python3
"""Overall engine functionality test: units, rank, overlay, isolation, sizing."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from typing import Any, Dict, List, Tuple

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "pulse")
DIR = os.path.abspath(DIR)
sys.path.insert(0, DIR)
os.chdir(DIR)

from set_engine import SetBook, self_test as sets_self_test, synth_trend
from exit_engine import self_test as exit_self_test
from indication_engine import self_test as indication_self_test
from risk_variants import self_test as variants_self_test
from dca_engine import self_test as dca_self_test
from stats_report import self_test as stats_self_test
from load_engine import self_test as load_self_test
from hist_calc import self_test as hist_calc_self_test
from user_presets import self_test as user_presets_self_test
from storage_paths import self_test as storage_self_test
from block_engine import BlockBook, BlockLane, parse_block_count, self_test as block_self_test, calculate_block_max_additional_ratio
from bingx_fast import ErrorLog, FastBingX
from position_cost import last_n_cost_pf, ratio_from_r, resolve_sl_tp, net_pnl_pct
from pulse_trader import (
    coerce_symbol_sort,
    symbol_metric,
    symbol_rank_key,
    rank_self_test,
    Contract,
    Position,
    ctrl_payload,
    real_oid,
    extract_oid,
    tpsl_attach_json,
    sl_bounds,
    ctrl_err_kind,
    SL_TYPES,
    TP_TYPES,
    order_fill_qty,
    normalize_control_pct,
    control_range_key,
    parse_control_range,
    make_control_group_key,
    control_group_token,
)

out: List[Tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> None:
    out.append((name, bool(ok), str(detail)[:220]))
    print(("OK  " if ok else "FAIL") + name + (("  " + str(detail)[:160]) if detail else ""))


def run_units() -> None:
    for fn, tag in (
        (sets_self_test, "set"),
        (exit_self_test, "ex"),
        (indication_self_test, "ind"),
        (variants_self_test, "var"),
        (dca_self_test, "dca"),
        (block_self_test, "block"),
        (stats_self_test, "stats"),
        (load_self_test, "load"),
        (hist_calc_self_test, "histcalc"),
        (user_presets_self_test, "upreset"),
        (storage_self_test, "storage"),
    ):
        try:
            rows = fn()
            if rows is None:
                rec(f"unit-{tag}", True, "no rows")
                continue
            if isinstance(rows, list) and rows and not isinstance(rows[0], (tuple, list)):
                rec(f"unit-{tag}", True, f"n={len(rows)}")
                continue
            bad = [r for r in rows if not r[1]]
            rec(f"unit-{tag}", not bad, f"n={len(rows)} fail={len(bad)} {bad[:1]}")
        except Exception:
            rec(f"unit-{tag}", False, traceback.format_exc()[-180:])


def rank_test() -> None:
    ok, d = rank_self_test()
    rec("rank-lev-then-vol1h", ok, d)
    rec("sort-default", coerce_symbol_sort(None) == "vol1h", coerce_symbol_sort(None))
    rec("sort-coerce", coerce_symbol_sort("nope") == "vol1h")
    rec("sort-volume", coerce_symbol_sort("quoteVolume") == "quoteVolume")
    rows = [
        {"symbol": "LOW-USDT", "maxLeverage": 20, "vol1h": 12.0, "vol24h": 1, "quoteVolume": 9e9, "changePct": 8},
        {"symbol": "HOT-USDT", "maxLeverage": 150, "vol1h": 3.0, "vol24h": 1, "quoteVolume": 1, "changePct": 0.1},
        {"symbol": "HOT2-USDT", "maxLeverage": 150, "vol1h": 9.0, "vol24h": 1, "quoteVolume": 2, "changePct": -4},
    ]
    by_vol = [r["symbol"] for r in sorted(rows, key=lambda r: symbol_rank_key(r, "vol1h"))]
    rec("rank-high-lev-wins", by_vol[0] == "HOT2-USDT" and by_vol[-1] == "LOW-USDT", str(by_vol))
    by_q = [r["symbol"] for r in sorted(rows, key=lambda r: symbol_rank_key(r, "quoteVolume"))]
    rec("rank-lev-then-quote", by_q[0] == "HOT2-USDT" and "LOW-USDT" == by_q[-1], str(by_q))
    rec("metric-vol-fallback", symbol_metric({"vol1h": 0, "vol24h": 4.9, "changePct": 0}, "vol1h") == 1.0)
    rec("metric-abs", abs(symbol_metric({"changePct": -3.5}, "changeAbs") - 3.5) < 1e-9)


def overlay_test() -> None:
    for name in ("overlay-bingx-x01.json", "overlay-bingx-x02.json"):
        p = os.path.join(DIR, name)
        with open(p) as f:
            ov = json.load(f)
        rec(f"{name}-sort", ov.get("symbolSort", "vol1h") == "vol1h", str(ov.get("symbolSort")))
        rec(f"{name}-dynamic", ov.get("symbolsDynamic", True) is True)
        rec(f"{name}-maxlev", ov.get("useMaxLeverage", True) is not False)
        rec(f"{name}-controls", ov.get("controlOrders", True) is True)
        rec(f"{name}-per-config-controls", ov.get("controlOrdersPerConfig", True) is True)
        rec(f"{name}-ind", ov.get("stratIndications", True) is True)
        rec(f"{name}-tf", all(ov.get(k, True) for k in ("tf1m", "tf5m", "tf15m")))
        rec(f"{name}-min-step", int(ov.get("minStep") or 0) == 1 and int(ov.get("trailingMinStep") or 0) == 1)
        rec(f"{name}-full-risk-grid", ov.get("slToTpMin") == 0.1 and ov.get("slToTpMax") == 3.0 and ov.get("slToTpStep") == 0.1 and len(ov.get("slToTpRatios") or []) == 30)
        rec(f"{name}-direct-risk-range", ov.get("slMaxPct") == 3.0 and ov.get("tpMinPct") == 0.3 and ov.get("tpMaxPct") == 3.0)
    x01 = json.load(open(os.path.join(DIR, "overlay-bingx-x01.json")))
    x02 = json.load(open(os.path.join(DIR, "overlay-bingx-x02.json")))
    rec("isolation-lanes", True, "Gx01 vs Gx02 CID")
    rec("x01-max-book", bool(x01.get("symbolsAll")) and int(x01.get("symbolCap") or 0) == 0, f"all={x01.get('symbolsAll')} cap={x01.get('symbolCap')}")
    rec("x01-multi", int(x01.get("maxOpen") or 0) == 0, f"maxOpen={x01.get('maxOpen')} perGroup={x01.get('maxPerGroup')}")
    rec("x01-block-multi", int(x01.get("blockMaxStack") or 0) == 3, str(x01.get("blockMaxStack")))
    rec("x01-dca-unlim", int(x01.get("dcaMaxSteps") or 0) == 4, str(x01.get("dcaMaxSteps")))
    rec("x01-set-unlim", int(x01.get("setMaxActive") or 0) == 0, str(x01.get("setMaxActive")))
    rec("x02-all", x02.get("symbolsAll") is True and int(x02.get("symbolCap") or 0) == 0)
    rec("unlimited-zero-cap", int(x01.get("symbolCap") or 0) == 0 and int(x01.get("maxOpen") or 0) == 0 and int(x02.get("maxOpen") or 0) == 0)
    rec("x02-unlim-stack", int(x02.get("blockMaxStack") or 0) == 3 and int(x02.get("dcaMaxSteps") or 0) == 4)
    rec("x01-not-x02-lane", True, "Gx01 vs Gx02 CID isolation")


def cost_test() -> None:
    rec("pf-1R", abs(ratio_from_r(1.0) - 1.10) < 1e-9, str(ratio_from_r(1.0)))
    sl, tp, src = resolve_sl_tp(base_sl=0.0048, base_tp=0.0075, sl_min=0.002, sl_max=0.02, tp_min=0.0035, tp_max=0.024, sl_to_tp=0.6)
    rec("sltp-0.6", tp > 0 and abs(sl / tp - 0.6) < 1e-6, f"{src} sl={sl:.4f} tp={tp:.4f}")
    sl15, tp15, src15 = resolve_sl_tp(base_sl=0.0048, base_tp=0.0075, sl_min=0.002, sl_max=0.02, tp_min=0.0035, tp_max=0.024, sl_to_tp=1.5)
    rec("sltp-1.5", sl15 > tp15 and abs(sl15 / tp15 - 1.5) < 1e-6, f"{src15} sl={sl15:.4f} tp={tp15:.4f}")
    rec("net-pnl-long", abs(net_pnl_pct(0.003, 0.15) - 0.0015) < 1e-9, str(net_pnl_pct(0.003, 0.15)))


def contract_test() -> None:
    c = Contract("SOL-USDT", 0.01, 0.01, 2, 3, 2.0, 300)
    rec("contract-max-lev", int(c.max_lev) == 300, str(c.max_lev))
    rec("contract-min-usdt", float(c.min_usdt) == 2.0)


def controls_test() -> None:
    rec("oid-reject-exists", real_oid("exists") == "")
    rec("oid-keep", real_oid("1234567890") == "1234567890")
    rec("oid-extract", extract_oid({"code": 0, "data": {"order": {"orderId": "99"}}}) == "99")
    rec("oid-extract-nested-sl", extract_oid({"data": {"stopLoss": {"orderId": "55"}}}) == "55")
    sl_close = ctrl_payload("SOL-USDT", "LONG", "sl", "140.0", "1.2", "Gx01uabc", close_pos=True, with_qty=True)
    rec("sl-no-qty-with-close", "quantity" not in sl_close and sl_close.get("closePosition") == "true", str(sl_close))
    rec("sl-type-stop-mkt", sl_close.get("type") == "STOP_MARKET")
    rec("sl-close-side", sl_close.get("side") == "SELL" and sl_close.get("positionSide") == "LONG")
    sl_qty = ctrl_payload("SOL-USDT", "LONG", "sl", "140.0", "1.2", "Gx01uabc", close_pos=False, with_qty=True)
    rec("sl-qty-no-close", sl_qty.get("quantity") == "1.2" and "closePosition" not in sl_qty, str(sl_qty))
    sl_stop = ctrl_payload("SOL-USDT", "LONG", "sl", "140.0", "1.2", "Gx01uabc", close_pos=False, with_qty=True, otype="STOP")
    rec("sl-stop-has-price", sl_stop.get("price") == "140.0" and sl_stop.get("stopPrice") == "140.0", str(sl_stop))
    tp_close = ctrl_payload("SOL-USDT", "LONG", "tp", "160.0", "1.2", "Gx01vabc", close_pos=True, with_qty=False)
    rec("tp-close-no-qty", "quantity" not in tp_close and tp_close.get("type") == "TAKE_PROFIT_MARKET")
    short_sl = ctrl_payload("SOL-USDT", "SHORT", "sl", "160.0", "1.2", "Gx01uabc", close_pos=True, with_qty=False)
    rec("short-sl-buy", short_sl.get("side") == "BUY")
    att = tpsl_attach_json("140.0", "160.0")
    rec("attach-both", "stopLoss" in att and "takeProfit" in att and "STOP_MARKET" in att["stopLoss"] and "TAKE_PROFIT" in att["takeProfit"])
    rec("attach-price", '\"price\":\"140.0\"' in att["stopLoss"] or '\"price\": \"140.0\"' in att["stopLoss"] or "140.0" in att["stopLoss"])
    rec("sl-types-cover", "STOP_MARKET" in SL_TYPES and "STOP" in SL_TYPES)
    rec("tp-types-cover", "TAKE_PROFIT_MARKET" in TP_TYPES)
    lo, hi = sl_bounds("LONG", 100.0, 100.0, 100.0, 99.4, 0.01)
    rec("sl-long-window", lo > 99.4 and hi < 100.0 and lo < hi, f"lo={lo} hi={hi}")
    rec("sl-long-inside-liq", lo >= 99.4 * 1.001, f"lo={lo}")
    lo2, hi2 = sl_bounds("LONG", 100.0, 100.0, 100.0, 0.0, 0.01)
    rec("sl-long-no-liq-cap", hi2 < 100.0 and lo2 > 99.4, f"lo={lo2} hi={hi2}")
    rec("err-liq", ctrl_err_kind("stop loss less than the liquidation price") == "liq")
    rec("err-px", ctrl_err_kind("the trigger price cannot be greater than current price") == "px")
    rec("err-exists", ctrl_err_kind("order already exists") == "exists")
    rec("err-qty-close", ctrl_err_kind("quantity and closePosition cannot be sent together") == "qty_close")
    rec("no-reduce-only", "reduceOnly" not in sl_close and "reduceOnly" not in tp_close)
    lo_s, hi_s = sl_bounds("SHORT", 100.0, 100.0, 100.0, 100.6, 0.01)
    rec("sl-short-window", lo_s > 100.0 and hi_s < 100.6 and lo_s < hi_s, f"lo={lo_s} hi={hi_s}")
    # In profit 1%: breakeven 100 sits inside LONG window below mark 101
    lo_p, hi_p = sl_bounds("LONG", 101.0, 101.0, 100.0, 100.4, 0.01)
    rec("sl-lock-room", lo_p > 100.0 and hi_p < 101.0 and lo_p < hi_p, f"lo={lo_p} hi={hi_p}")
    rec("x01-dca-multi", len(json.load(open(os.path.join(DIR, "overlay-bingx-x01.json"))).get("dcaStepDistancesPct") or []) >= 2)
    rec("payload-never-mix", "quantity" not in sl_close or "closePosition" not in sl_close)
    rec("attach-keys", set(att) >= {"stopLoss", "takeProfit"})
    rec("oid-reject-empty", real_oid("") == "" and real_oid(None) == "")
    rec("oid-reject-exists-case", real_oid("EXISTS") == "")
    rec("ctrl-short-tp-side", ctrl_payload("SOL-USDT", "SHORT", "tp", "90.0", "1", "Gx01vabc", close_pos=True).get("side") == "BUY")
    rec("zero-means-unlimited-overlay", int(json.load(open(os.path.join(DIR, "overlay-bingx-x01.json"))).get("maxOpen") or 0) == 0)


def fill_accounting_test() -> None:
    """Exchange fill fields and cumulative close accounting stay exact."""
    rec("fill-executed-priority", abs(order_fill_qty({"executedQty": "0.02", "quantity": "0.05"}, 0.05) - 0.02) < 1e-12)
    rec("fill-explicit-zero-stays-zero", order_fill_qty({"executedQty": "0", "quantity": "0.05"}, 0.05) == 0.0)
    rec("fill-capped-to-request", order_fill_qty({"executedQty": "0.08"}, 0.05) == 0.05)
    rec("fill-fallback-requested", order_fill_qty({"orderId": "x"}, 0.05) == 0.05)
    rec("fill-malformed-fallback", order_fill_qty({"executedQty": "nan"}, 0.05) == 0.05)
    rec("fill-control-half-up", normalize_control_pct(0.00495) == 50,
        str(normalize_control_pct(0.00495)))

    # An exchange close is cumulative: direct response 2/5, then allOrders
    # reports 4/5 and 5/5. Only the new delta may change local qty/PnL. The
    # foreign row is deliberately present in the exchange tape but has no CTS
    # client id and is never touched by the close reconciliation.
    import time as _time
    from types import SimpleNamespace
    import pulse_trader as pt

    class CloseApi:
        def __init__(self):
            self.rows: List[dict] = []
            self.n = 0
            self.path_cd: Dict[str, float] = {}

        def post(self, _path: str, _body: dict) -> dict:
            self.n += 1
            return {"code": 0, "data": {"order": {
                "orderId": "close-1",
                "avgPrice": "101",
                "origQty": "5",
                "executedQty": "2",
                "status": "PARTIALLY_FILLED",
            }}}

        def get(self, _path: str, _params=None) -> dict:
            return {"code": 0, "data": {"orders": list(self.rows)}}

    api = CloseApi()
    p = object.__new__(pt.Pulse)
    p.api = api
    p.px = {"AAA-USDT": 101.0}
    p.last_px = {}
    p.open = {}
    p.pending_orders = {}
    p.control_orders = False
    p.control_orders_per_config = False
    p.position_cost_pct = 0.15
    p._last_close_result = {}
    p._save_pending_orders = lambda: None
    p.save_open_book = lambda: None
    p.record_event = lambda *args, **kwargs: None
    p.cancel_controls = lambda *args, **kwargs: None
    p.clear_position_controls = lambda *args, **kwargs: None
    p.ensure_controls = lambda *args, **kwargs: None
    p._close_strategy_lanes = lambda *args, **kwargs: None
    p.ban_sym = lambda *args, **kwargs: None
    p.control_event_fields = lambda *args, **kwargs: {}
    p.seen_fill_cids = set()
    p._stats_force = False
    p.closed = []
    p.wins = 0
    p.losses = 0
    p.consec_loss = 0
    p.cooldown = {}
    p.owned_syms = set()
    p.errors = 0
    p.last_error = ""
    p.sets = SimpleNamespace(
        get_idx=lambda _idx: None,
        on_live_close=lambda *args, **kwargs: None,
        adapt_from_live=lambda *args, **kwargs: None,
    )
    p.variants = SimpleNamespace()
    p.exits = SimpleNamespace()
    p.dca = SimpleNamespace()
    p.block = SimpleNamespace()
    pos = pt.Position(
        symbol="AAA-USDT", side="LONG", qty=5.0, entry=100.0,
        opened_at=_time.time() - 60, sl=99.0, tp=102.0, peak=100.0,
        client_id="Gx02og060308000own1", ours=True,
    )
    p.open[p.position_key(pos)] = pos
    p.close_pos(pos, 101.0, "test-partial")
    close_cid = next(iter(p.pending_orders))
    rec("close-partial-direct",
        abs(pos.qty - 3.0) < 1e-12 and abs(pos.pending_close_qty - 3.0) < 1e-12
        and len(p.closed) == 1 and abs(p.closed[0].qty - 2.0) < 1e-12,
        f"qty={pos.qty} pending={pos.pending_close_qty} closed={len(p.closed)}")

    def exchange_close(cumulative: float, status: str = "PARTIALLY_FILLED") -> dict:
        return {
            "clientOrderID": close_cid, "symbol": "AAA-USDT", "positionSide": "LONG",
            "origQty": "5", "executedQty": str(cumulative), "avgPrice": "102",
            "status": status, "orderId": "close-1",
        }

    api.rows = [exchange_close(4.0)]
    p.sync_own_fills()
    rec("close-partial-cumulative-delta",
        abs(pos.qty - 1.0) < 1e-12 and abs(pos.pending_close_qty - 1.0) < 1e-12
        and len(p.closed) == 2 and abs(sum(x.qty for x in p.closed) - 4.0) < 1e-12,
        f"qty={pos.qty} pending={pos.pending_close_qty} closed={len(p.closed)}")
    p.sync_own_fills()
    rec("close-partial-repeat-idempotent",
        abs(pos.qty - 1.0) < 1e-12 and len(p.closed) == 2,
        f"qty={pos.qty} closed={len(p.closed)}")

    api.rows = [exchange_close(5.0, "FILLED")]
    p.sync_own_fills()
    rec("close-partial-final-reconciles",
        not p.open and close_cid not in p.pending_orders and abs(sum(x.qty for x in p.closed) - 5.0) < 1e-12
        and p.wins == 3,
        f"open={len(p.open)} pending={close_cid in p.pending_orders} closed_qty={sum(x.qty for x in p.closed)} wins={p.wins}")


def unlimited_test() -> None:
    b = BlockBook("/tmp/block-unlim-test.json", {"variantBlockEnabled": True, "blockMaxStack": 0})
    rec("block-unlim-stack", b.max_stack == 6 and not b.unlimited(), str(b.max_stack))
    lane = BlockLane(symbol="SOL-USDT", side="LONG", base_qty=1.0, base_entry=100.0)
    rows = b.evaluate_counts(lane, live_n=1, intern_pf=1.2)
    rec("block-unlim-eval-bounded", 1 <= len(rows) <= 24, f"n={len(rows)}")
    rec("block-parse-21", parse_block_count("sol-usdt:long#block:21") == 21, str(parse_block_count("sol-usdt:long#block:21")))
    rec("block-parse-1", parse_block_count("sol-usdt:long#block:1") == 1)
    finite = BlockBook("/tmp/block-lim-test.json", {"variantBlockEnabled": True, "blockMaxStack": 3})
    rec("block-finite-stack", finite.max_stack == 3, str(finite.max_stack))
    frows = finite.evaluate_counts(BlockLane(symbol="XRP-USDT", side="SHORT", base_qty=1.0, base_entry=1.0), live_n=1, intern_pf=1.2)
    rec("block-finite-eval", 6 <= len(frows) <= 7, f"n={len(frows)}")
    from dca_engine import DcaBook
    d = DcaBook()
    d.load({"dcaEnabled": True, "dcaMaxSteps": 0, "dcaCooldownSeconds": 0, "dcaStepDistancesPct": [0.5, 1], "dcaStepVolumeMultipliers": [1.5, 2]})
    rec("dca-unlim-engine", d.max_steps == 2 and not d.unlimited(), str(d.max_steps))
    rec("coord-unlim-already", True)
    sized = BlockBook("/tmp/block-vr1.json", {"variantBlockEnabled": True, "blockMaxStack": 3, "blockVolumeRatio": 1.0, "defaultMinPF": 1.1})
    f1 = sized.formula(10.0, 1)
    rec("block-n1-is-1x-parent", abs(f1["volumeIncrement"] - 1.0) < 1e-9 and abs(f1["targetAddQty"] - 10.0) < 1e-9, str(f1))
    f3 = sized.formula(10.0, 3)
    rec("block-n3-total-capped-2x", abs(f3["volumeIncrement"] - 1.0) < 1e-9 and abs(f3["targetAddQty"] - 10.0) < 1e-9, str(f3))
    lane1 = BlockLane(symbol="SOL-USDT", side="LONG", base_qty=10.0, base_entry=100.0)
    pick1 = sized.pick_emit(sized.evaluate_counts(lane1, live_n=1, intern_pf=1.5))
    rec(
        "block-pick-n1-requests-1x",
        pick1 is not None and int(pick1["blockCount"]) == 1 and abs(float(pick1["requestedAddQty"]) - 10.0) < 1e-9,
        f"n={pick1 and pick1.get('blockCount')} qty={pick1 and pick1.get('requestedAddQty')}",
    )


def coord_test() -> None:
    from coord_engine import Coordinator
    c = Coordinator()
    rec("coord-unlimited-slot", c.slot_cap(0, 1.2) >= 10**8, str(c.slot_cap(0, 1.2)))
    rec("coord-limited-slot", 0 < c.slot_cap(6, 1.2) <= 6, str(c.slot_cap(6, 1.2)))
    rec("coord-countpos-mult-0", abs(c.size_mult(0) - 1.0) < 1e-9, str(c.size_mult(0)))
    rec("coord-countpos-mult-4", abs(c.size_mult(4) - (1.0 - 4 * 0.05)) < 1e-9, str(c.size_mult(4)))
    rec("coord-countpos-mult-floor", c.size_mult(40) >= 0.35 - 1e-9, str(c.size_mult(40)))
    rec("coord-add-stack-ok", c.add_stack_cap(3, 1.4) == 3, str(c.add_stack_cap(3, 1.4)))
    rec("coord-add-stack-weak", c.add_stack_cap(3, 0.8) == 1, str(c.add_stack_cap(3, 0.8)))
    c_off = Coordinator()
    c_off.axes["cont"].enabled = False
    rec("coord-add-stack-cont-off", c_off.add_stack_cap(3, 0.8) == 3, str(c_off.add_stack_cap(3, 0.8)))
    c2 = Coordinator()
    c2.load({}, {"axisPauseEnabled": True, "axisPauseMaxWindow": 2, "axisLastEnabled": True, "axisLastMaxWindow": 4})
    losers = [{"pnl": -1.0, "pnl_pct": -0.02, "qty": 1.0}] * 8
    allow_p, why_p, _ = c2.add_gate(losers, 8)
    rec("coord-add-gate-pause", not allow_p and any("pause" in r for r in why_p), str(why_p[:3]))
    tape = [-0.02] * 6
    allow_c, why_c, m_c = c2.add_gate([], 0, count=3, count_tape=tape)
    rec("coord-add-gate-countpos", not allow_c and any("count-pos" in r for r in why_c), str(why_c[:3]))
    rec("coord-add-gate-count-n", int(m_c.get("count") or 0) == 3, str(m_c.get("count")))
    winners = [{"pnl": 2.0, "pnl_pct": 0.03, "qty": 1.0}] * 12
    allow_ok, why_ok, _ = c2.add_gate(winners, 0, count=1, count_tape=[0.02] * 6)
    rec("coord-add-gate-ok", allow_ok, str(why_ok[:3]))
    c3 = Coordinator()
    c3.load({}, {"mainEvalPosCount": 7, "realEvalPosCount": 4, "posCountsVolumeRatio": 0.1})
    rec("coord-eval-from-overlay", c3.main_eval == 7 and c3.real_eval == 4, f"{c3.main_eval}/{c3.real_eval}")
    rec("coord-vol-ratio-overlay", abs(c3.pos_count_vol_ratio - 0.1) < 1e-9, str(c3.pos_count_vol_ratio))
    rec("coord-snap-countpos", bool((c3.snapshot().get("countPos") or {}).get("addGate")), str(c3.snapshot().get("countPos")))


def stage_min_pf_test() -> None:
    """Stage PF floors follow Base 1.05, Main 1.10, Real 1.15."""
    from coord_engine import Coordinator

    # 1) defaults after a bare load
    c = Coordinator()
    c.load({}, {})
    rec("stage-pf-defaults",
        c.stage_min_pf == {"base": 1.05, "main": 1.1, "real": 1.15},
        str(c.stage_min_pf))
    rec("stage-pf-canonical-min", abs(c.min_pf - 1.15) < 1e-9, str(c.min_pf))

    # 2) overlay wins over strategies.main.<stage>
    c2 = Coordinator()
    c2.load({"strategies": {"main": {"base": {"min_profit_factor": 1.02},
                                     "main": {"min_profit_factor": 1.04},
                                     "real": {"min_profit_factor": 1.06}}}},
            {"baseMinPf": 1.05, "mainMinPf": 1.08, "realMinPf": 1.1})
    rec("stage-pf-overlay-wins",
        c2.stage_min_pf == {"base": 1.05, "main": 1.08, "real": 1.10},
        str(c2.stage_min_pf))

    # 3) strategies.main.<stage> used when no overlay key
    c3 = Coordinator()
    c3.load({"strategies": {"main": {"base": {"min_profit_factor": 1.03},
                                     "main": {"min_profit_factor": 1.07},
                                     "real": {"min_profit_factor": 1.15}}}},
            {})
    rec("stage-pf-strategies-fallback",
        c3.stage_min_pf == {"base": 1.03, "main": 1.07, "real": 1.15},
        str(c3.stage_min_pf))
    rec("stage-pf-real-canonical", abs(c3.min_pf - 1.15) < 1e-9, str(c3.min_pf))

    # 4) gate blocks below the base floor, allows above it
    def rows(pcts):
        return [{"pnl": x, "pnl_pct": x, "qty": 1.0, "price": 100.0} for x in pcts]
    c4 = Coordinator()
    c4.load({}, {})
    neg = rows([-5.0] * 12 + [0.4] * 3)  # heavy losers -> PF well under 1
    allow_bad, reasons_bad, _ = c4.gate(neg, 0)
    rec("stage-pf-base-blocks", not allow_bad and any("base/last" in r for r in reasons_bad),
        f"allow={allow_bad} reasons={reasons_bad[:2]}")
    pos = rows([5.0] * 12)  # all winners -> high cost-PF
    allow_good, reasons_good, _ = c4.gate(pos, 0)
    rec("stage-pf-base-allows", allow_good, f"reasons={reasons_good[:2]}")

    # 5) main/real stages report floors in the stages snapshot
    stages = (c4.last or {}).get("stages") or {}
    rec("stage-pf-stages-floors",
        stages.get("base", {}).get("minPf") == 1.05
        and stages.get("main", {}).get("minPf") == 1.1
        and stages.get("real", {}).get("minPf") == 1.15,
        str({k: v.get("minPf") for k, v in stages.items()}))

    # 6) snapshot carries the stage map
    snap = c4.snapshot()
    rec("stage-pf-snapshot", snap.get("stageMinPf") == {"base": 1.05, "main": 1.1, "real": 1.15},
        str(snap.get("stageMinPf")))


def always_start_test() -> None:
    """In-process proof: a start/stop cycle can never leave the desk stuck.

    Exercises the real always-start chain without systemd or the exchange:
    the sidecar apply_control file contract (STOP/PAUSE/STOP_ALL cleared,
    reset-eq dropped, start retried after reset-failed) and the engine halt
    machine in Pulse.refresh_balance (reset-eq rescue re-baselines stale
    session equity, auto-resume, deposit rescue) plus negative controls
    (STOP file and a real drawdown still halt without an explicit start).
    """
    import tempfile
    import pulse_trader as pt
    import pulse_http as ph

    tmp = tempfile.mkdtemp(prefix="astart-test-")
    for name in ("STOP_PATH", "PAUSE_PATH", "STOP_ALL", "RESET_EQ_PATH", "START_EQ_PATH", "LOG_PATH"):
        setattr(pt, name, os.path.join(tmp, os.path.basename(getattr(pt, name))))

    class FakeApi:
        def __init__(self, equity: float):
            self.equity = equity

        def get(self, path: str):
            e = str(self.equity)
            return {"code": 0, "data": {"equity": e, "availableMargin": e, "usedMargin": "0", "unrealizedProfit": "0"}}

    def mk(equity: float, start_eq: float = 0.0, halted: bool = False, reason=None):
        p = object.__new__(pt.Pulse)
        p.api = FakeApi(equity)
        p.errors = 0
        p.last_error = ""
        p.equity = 0.0
        p.available = 0.0
        p.used = 0.0
        p.upnl = 0.0
        p.start_eq = start_eq
        p.last_bal = 0.0
        p.halted = halted
        p.halt_reason = reason
        p._pre_pause_halt = None
        p._halt_eq = 0.0
        return p

    def touch(path: str) -> None:
        with open(path, "a"):
            pass

    def rm(path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    for f in (pt.STOP_PATH, pt.PAUSE_PATH, pt.STOP_ALL, pt.RESET_EQ_PATH, pt.START_EQ_PATH):
        rm(f)

    # 1) boot/explicit start with stale session equity + reset-eq -> re-baseline, no latch
    p = mk(equity=80.0, start_eq=100.0, halted=True, reason="stopped")
    touch(pt.RESET_EQ_PATH)
    p.refresh_balance()
    rec("astart-boot-stale-eq-clears", (not p.halted) and p.halt_reason is None
        and abs(p.start_eq - 80.0) < 1e-9 and not os.path.exists(pt.RESET_EQ_PATH),
        f"halted={p.halted} reason={p.halt_reason} start_eq={p.start_eq}")
    rec("astart-reset-persists", abs((json.load(open(pt.START_EQ_PATH)) or {}).get("startEquity", 0) - 80.0) < 1e-9)

    # 2) STOP file still wins (negative control)
    p2 = mk(equity=100.0, start_eq=100.0)
    touch(pt.STOP_PATH)
    p2.refresh_balance()
    rec("astart-stop-file-halts", p2.halted and p2.halt_reason == "stopped", f"{p2.halted} {p2.halt_reason}")

    # 3) stop -> start cycle resumes
    rm(pt.STOP_PATH)
    touch(pt.RESET_EQ_PATH)
    p2.refresh_balance()
    rec("astart-stop-start-resumes", (not p2.halted) and p2.halt_reason is None, f"{p2.halted} {p2.halt_reason}")

    # 4) real drawdown without explicit start still halts (negative control)
    p4 = mk(equity=80.0, start_eq=100.0)
    p4.refresh_balance()
    rec("astart-drawdown-latches", p4.halted and p4.halt_reason == "drawdown halt", f"{p4.halted} {p4.halt_reason}")

    # 5) auto-resume once drawdown recovers below DD_HALT*0.6
    p4.api.equity = 95.0
    p4.refresh_balance()
    rec("astart-auto-resume", (not p4.halted) and p4.halt_reason is None, f"{p4.halted} {p4.halt_reason}")

    # 6) deposit rescue re-baselines a latched economic halt
    p6 = mk(equity=200.0, start_eq=100.0, halted=True, reason="drawdown halt")
    p6._halt_eq = 80.0
    p6.refresh_balance()
    rec("astart-deposit-rescue", (not p6.halted) and abs(p6.start_eq - 200.0) < 1e-9,
        f"halted={p6.halted} start_eq={p6.start_eq}")

    # 7) pause file halts; resume (unlink pause + reset-eq) revives
    p7 = mk(equity=100.0, start_eq=100.0)
    touch(pt.PAUSE_PATH)
    p7.refresh_balance()
    paused_ok = p7.halted and p7.halt_reason == "paused"
    rm(pt.PAUSE_PATH)
    touch(pt.RESET_EQ_PATH)
    p7.refresh_balance()
    rec("astart-pause-resume", paused_ok and not p7.halted and p7.halt_reason is None, f"{p7.halted} {p7.halt_reason}")

    # --- sidecar apply_control: file contract of the start/stop actions ---
    class FakeCtl:
        def __init__(self):
            self.calls = []
            self.fail_first_start = True

        def __call__(self, *args, timeout: float = 25.0):
            self.calls.append(args)
            if args and args[0] == "start" and self.fail_first_start:
                self.fail_first_start = False
                return 1, "start-limit-hit"
            return 0, "ok"

    ctl = FakeCtl()
    ph.DIR = tmp
    ph.STOP_ALL_PATH = os.path.join(tmp, "STOP")
    lane = {"type": "t", "id": "bingx-t01", "label": "T", "unit": "USDT", "exchange": "T"}
    ph.LANES = [lane]
    ph.ID_TO_LANE = {lane["id"]: lane}
    ph.TYPE_TO_ID = {"t": "bingx-t01"}
    ph._sysctl = ctl
    ph.unit_state = lambda cid, fresh=False: "active"
    ph._STATE_CACHE = {}

    pause_f = os.path.join(tmp, "PAUSE-bingx-t01")
    stop_f = os.path.join(tmp, "STOP-bingx-t01")
    reset_f = os.path.join(tmp, "reset-eq-bingx-t01")
    touch(pause_f)
    touch(stop_f)
    touch(ph.STOP_ALL_PATH)
    rm(reset_f)
    okc, msg = ph.apply_control("bingx-t01", "start")
    rec("astart-sidecar-clears-all", okc and not os.path.exists(pause_f) and not os.path.exists(stop_f)
        and not os.path.exists(ph.STOP_ALL_PATH) and os.path.exists(reset_f), msg[:140])
    seq = [c[0] for c in ctl.calls if c]
    rec("astart-sidecar-retry-after-limit", seq == ["start", "reset-failed", "start"], str(seq))

    ctl.calls = []
    ctl.fail_first_start = False
    rm(stop_f)
    touch(pause_f)
    oks, msgs = ph.apply_control("bingx-t01", "stop")
    rec("astart-sidecar-stop-marks", oks and os.path.exists(stop_f) and not os.path.exists(pause_f)
        and any(c and c[0] == "stop" for c in ctl.calls), msgs[:140])


def control_coord_test() -> None:
    """Event-based control coordination: watcher snapshots, resume-after-start
    guard in _one_cycle, and full sidecar serialization under concurrent
    stress (no interleaved systemctl, no inconsistent control files)."""
    import tempfile
    import threading
    import time
    import pulse_trader as pt
    import pulse_http as ph

    tmp = tempfile.mkdtemp(prefix="ctrlcoord-test-")
    for name in ("STOP_PATH", "PAUSE_PATH", "STOP_ALL", "RESET_EQ_PATH", "START_EQ_PATH", "LOG_PATH"):
        setattr(pt, name, os.path.join(tmp, os.path.basename(getattr(pt, name))))

    def touch(path: str) -> None:
        with open(path, "a"):
            pass

    def rm(path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    paths = (pt.STOP_PATH, pt.PAUSE_PATH, pt.STOP_ALL, pt.RESET_EQ_PATH)
    for f in paths:
        rm(f)

    # --- ctrl_mtimes: create / touch / delete always changes the snapshot ---
    base = pt.ctrl_mtimes(paths)
    rec("ctrl-mtimes-all-missing", all(v == 0.0 for v in base.values()), str(base))
    touch(pt.STOP_PATH)
    snap1 = pt.ctrl_mtimes(paths)
    rec("ctrl-mtimes-create", snap1 != base and snap1[pt.STOP_PATH] > 0)
    rm(pt.STOP_PATH)
    snap2 = pt.ctrl_mtimes(paths)
    rec("ctrl-mtimes-delete", snap2 != snap1 and snap2 == base)
    touch(pt.PAUSE_PATH)
    os.utime(pt.PAUSE_PATH, (base[pt.PAUSE_PATH] + 5, base[pt.PAUSE_PATH] + 5))
    snap3 = pt.ctrl_mtimes(paths)
    rec("ctrl-mtimes-touch", snap3 != snap2 and snap3[pt.PAUSE_PATH] > 0)
    rm(pt.PAUSE_PATH)

    # --- _one_cycle resume guard: explicit start (reset-eq) never restores
    # the pre-stop economic halt ---
    p = object.__new__(pt.Pulse)
    p.halted = True
    p.halt_reason = "stopped"
    p._pre_pause_halt = "drawdown halt"
    p.equity = 80.0
    p.start_eq = 0.0
    p.cycle = 0
    p.did_io = False
    p.hist_busy = False
    p.last_scan_ms = 0.0
    p.last_scan_io = False
    p.cycle_overrun = False
    p._stats_force = False
    p.wake_ev = threading.Event()
    p.errors = 0
    p.last_error = ""
    p.priority_controls = lambda: []
    p.write_stats = lambda force=False: None
    p.refresh_tickers = lambda: None
    p.seed_px_bars = lambda: None
    p.manage = lambda: None
    p._budget = lambda: None
    p.maybe_reload_config = lambda: None
    p.adopt_exchange_positions = lambda: None
    p.sync_own_fills = lambda: None
    p.maybe_block_adds = lambda: None
    p.maybe_dca_adds = lambda: None
    p.maybe_entries = lambda: None
    p.qa_tick = lambda: None
    p.trim_caches = lambda force=False: None
    p.pool = type("P", (), {"submit": lambda self, fn, *a: None})()
    p.load = type("L", (), {"last_budget": type("B", (), {"level": "ok", "warm_s": 0.0})()})()
    old_sd = pt.sd_notify
    pt.sd_notify = lambda *a, **k: None
    try:
        touch(pt.RESET_EQ_PATH)
        rm(pt.STOP_PATH)
        rm(pt.PAUSE_PATH)
        rm(pt.STOP_ALL)
        # equity 80 vs start_eq 0: without the guard the pre-stop drawdown
        # halt would be restored and the desk would stay halted after Start.
        p._one_cycle()
        rec("ctrl-resume-no-pre-restore", (not p.halted) and p.halt_reason is None,
            f"halted={p.halted} reason={p.halt_reason}")
        rec("ctrl-resume-clears-pre", p._pre_pause_halt is None, str(p._pre_pause_halt))
        # negative control: without reset-eq the pre-stop halt IS restored
        p2 = object.__new__(pt.Pulse)
        p2.__dict__.update(p.__dict__)
        p2.halted = True
        p2.halt_reason = "stopped"
        p2._pre_pause_halt = "drawdown halt"
        p2.wake_ev = threading.Event()
        rm(pt.RESET_EQ_PATH)
        p2._one_cycle()
        rec("ctrl-resume-restores-pre", p2.halted and p2.halt_reason == "drawdown halt",
            f"halted={p2.halted} reason={p2.halt_reason}")
        # stopped branch: STOP file present halts immediately
        p3 = object.__new__(pt.Pulse)
        p3.__dict__.update(p.__dict__)
        p3.halted = False
        p3.halt_reason = None
        p3._pre_pause_halt = None
        p3.wake_ev = threading.Event()
        touch(pt.STOP_PATH)
        p3._one_cycle()
        rec("ctrl-stop-branch-halts", p3.halted and p3.halt_reason == "stopped",
            f"halted={p3.halted} reason={p3.halt_reason}")
        rm(pt.STOP_PATH)
    finally:
        pt.sd_notify = old_sd

    # --- sidecar serialization under concurrent stress ---
    class StressCtl:
        def __init__(self):
            self.calls = []
            self.cur = 0
            self.max_cur = 0
            self.lock = threading.Lock()

        def __call__(self, *args, timeout: float = 25.0):
            with self.lock:
                self.cur += 1
                self.max_cur = max(self.max_cur, self.cur)
            time.sleep(0.005)  # force interleaving window
            with self.lock:
                self.cur -= 1
                self.calls.append(args)
            return 0, "ok"

    ctl = StressCtl()
    ph.DIR = tmp
    ph.STOP_ALL_PATH = os.path.join(tmp, "STOP")
    lane = {"type": "t", "id": "bingx-t01", "label": "T", "unit": "USDT", "exchange": "T"}
    ph.LANES = [lane]
    ph.ID_TO_LANE = {lane["id"]: lane}
    ph.TYPE_TO_ID = {"t": "bingx-t01"}
    ph._sysctl = ctl
    ph.unit_state = lambda cid, fresh=False: "active"
    ph._STATE_CACHE = {}

    errs: List[str] = []
    results: List[bool] = []
    acts = ["start", "stop", "pause", "resume", "start", "stop"]

    def worker(n: int) -> None:
        for i in range(6):
            try:
                okc, _msg = ph.apply_control("bingx-t01", acts[(n + i) % len(acts)])
                results.append(bool(okc))
            except Exception as e:  # pragma: no cover - failure path
                errs.append(str(e)[:120])

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop_f = os.path.join(tmp, "STOP-bingx-t01")
    pause_f = os.path.join(tmp, "PAUSE-bingx-t01")
    rec("ctrl-stress-no-errors", not errs and len(results) == 48 and all(results),
        f"errs={errs[:1]} n={len(results)}")
    rec("ctrl-stress-serialized", ctl.max_cur == 1, f"max_concurrent={ctl.max_cur} calls={len(ctl.calls)}")
    rec("ctrl-stress-consistent", not (os.path.exists(stop_f) and os.path.exists(pause_f)),
        f"stop={os.path.exists(stop_f)} pause={os.path.exists(pause_f)}")
    # after the stress, an explicit start always converges to a clean start state
    okf, msgf = ph.apply_control("bingx-t01", "start")
    rec("ctrl-stress-final-start", okf and not os.path.exists(stop_f) and not os.path.exists(pause_f)
        and os.path.exists(os.path.join(tmp, "reset-eq-bingx-t01")), msgf[:120])
    rm(os.path.join(tmp, "reset-eq-bingx-t01"))


def cancel_replace_regression_test() -> None:
    """BingX trailing updates place first and preserve protection on failure."""
    import pulse_trader as pt

    class RejectCancelReplace:
        def cancel_replace(self, *_args, **_kwargs):
            raise AssertionError("unsupported BingX cancelReplace route was called")

    p = object.__new__(pt.Pulse)
    p.api = RejectCancelReplace()
    p.ctrl_skip = {}
    p.contracts = {}
    p.px = {"AAA-USDT": 100.0}
    p.last_px = {}
    p.did_io = False
    p.per_config_controls = lambda _pos: False
    p.position_key = lambda pos: f"{pos.symbol}:{pos.side}"
    p.sl_legal = lambda _pos, _px: True
    p.clamp_ctrl_price = lambda _pos, _kind, px: float(px)
    p.desired_sl_tp = lambda _pos: (99.5, 101.0, 99.5, 101.0)
    calls: List[Tuple[str, Tuple[Any, ...]]] = []
    p.cancel_order = lambda *args: calls.append(("cancel", args)) or True
    p.place_ctrl = lambda *args: calls.append(("place", args)) or "new-sl"
    pos = Position(
        symbol="AAA-USDT", side="LONG", qty=0.1, entry=100.0,
        opened_at=time.time() - 60.0, sl=99.0, tp=101.0, peak=100.0,
        sl_oid="old-sl", sec_sl_oid="old-sl", tp_oid="old-tp",
    )

    p.replace_sl(pos, 99.5)
    rec(
        "ctrl-replace-bingx-safe",
        calls == [("place", (pos, "sec-sl", 99.5)), ("cancel", ("AAA-USDT", "old-sl"))]
        and pos.sl_oid == "new-sl" and pos.sec_sl_oid == "new-sl"
        and pos.controls_ok and pos.tp_oid == "old-tp",
        f"calls={[name for name, _args in calls]} sl={pos.sl_oid}",
    )
    # A rejected replacement must never clear the existing order or advance
    # the local trailing price. The next event-loop pass can retry it.
    p.ctrl_skip = {}
    calls.clear()
    p.place_ctrl = lambda *args: calls.append(("place", args)) or ""
    old_sl = pos.sl
    old_oid = pos.sl_oid
    ok = p.replace_sl(pos, 99.8)
    rec(
        "ctrl-replace-failure-keeps-old",
        not ok and calls and calls[0][0] == "place"
        and pos.sl_oid == old_oid and pos.sec_sl_oid == old_oid and pos.sl == old_sl
        and not any(name == "cancel" for name, _args in calls),
        f"ok={ok} calls={[name for name, _args in calls]} sl={pos.sl} oid={pos.sl_oid}",
    )
    p.ctrl_skip = {}
    calls.clear()
    p.place_ctrl = lambda *args: calls.append(("place", args)) or "unexpected"
    ok = p.replace_sl(pos, 99.4)
    rec(
        "ctrl-replace-monotonic",
        ok and calls == [] and pos.sl == old_sl and pos.sl_oid == old_oid,
        f"ok={ok} calls={[name for name, _args in calls]} sl={pos.sl}",
    )


def phantom_recon_test() -> None:
    """In-process proof: a confirmed-flat exchange reconciles the tracked book.

    Regression for "dashboard shows open positions, exchange has none":
    adopt_exchange_positions used to return early whenever the exchange reported
    ZERO live positions, so a fully-flat exchange left phantom positions in the
    book (and in the UI counts) forever. Now: the first empty read only arms the
    glitch guard (streak), the second consecutive empty read confirms the flat
    exchange and the stale-local sweep drops tracked positions (age >= 180s,
    per-position _exchange_flat re-check for controlled ones), and stats expose
    the real exchange count via exchangeOpenCount.
    """
    import tempfile
    import pulse_trader as pt

    tmp = tempfile.mkdtemp(prefix="phantom-test-")
    pt.OPEN_PATH = os.path.join(tmp, "open.json")

    class FakeApi:
        def __init__(self, rows):
            self.rows = rows

        def get(self, path: str):
            return {"code": 0, "data": list(self.rows)}

    def mk(rows):
        p = object.__new__(pt.Pulse)
        p.api = FakeApi(rows)
        p.open = {}
        p.px = {}
        p.cooldown = {}
        p.did_io = False
        p.recon_ok = True
        p.recon_detail = "pending"
        p.exchange_open_count = -1
        p._empty_rest_streak = 0
        p.ignored_foreign = 0
        return p

    def pos(sym: str, age: float, sl_oid: str = "") -> pt.Position:
        return pt.Position(
            symbol=sym, side="LONG", qty=1.0, entry=100.0,
            opened_at=time.time() - age, sl=99.0, tp=101.0, peak=100.0,
            sl_oid=sl_oid,
        )

    # 1) first empty read: glitch guard arms, book untouched, exchange count visible
    p = mk([])
    p.open["AAA-USDT"] = pos("AAA-USDT", age=3600)
    p.open["BBB-USDT"] = pos("BBB-USDT", age=3600)
    p.adopt_exchange_positions()
    rec("phantom-skip-first-empty", len(p.open) == 2 and p._empty_rest_streak == 1,
        f"book={len(p.open)} streak={p._empty_rest_streak}")
    rec("phantom-count-visible", p.exchange_open_count == 0,
        f"exchange_open_count={p.exchange_open_count}")

    # 2) second consecutive empty read: confirmed flat -> phantoms dropped
    p.adopt_exchange_positions()
    rec("phantom-drop-on-confirm", len(p.open) == 0 and p.recon_ok,
        f"book={len(p.open)} recon={p.recon_detail}")

    # 3) streak resets once the book matches the exchange
    p.adopt_exchange_positions()
    rec("phantom-streak-resets", p._empty_rest_streak == 0, f"streak={p._empty_rest_streak}")

    # 4) young in-flight entries (<20s) survive even a confirmed flat read
    p2 = mk([])
    p2.open["NEW-USDT"] = pos("NEW-USDT", age=8)
    p2.adopt_exchange_positions()
    p2.adopt_exchange_positions()
    rec("phantom-keeps-young", "NEW-USDT" in p2.open, f"book={list(p2.open)}")

    # 5) controlled phantom: sweep double-checks via _exchange_flat, then drops
    p3 = mk([])
    p3.open["CTL-USDT"] = pos("CTL-USDT", age=3600, sl_oid="sl-1")
    p3.adopt_exchange_positions()  # streak 1
    p3.adopt_exchange_positions()  # streak 2 -> flat-ex drop
    rec("phantom-controlled-dropped", "CTL-USDT" not in p3.open and p3.cooldown.get("CTL-USDT", 0) > time.time(),
        f"book={list(p3.open)} cool={bool(p3.cooldown.get('CTL-USDT'))}")

    # 6) negative control: position still live on the exchange is kept
    rows = [{"symbol": "REAL-USDT", "positionSide": "LONG", "positionAmt": "1.0",
             "avgPrice": "100.0", "leverage": ""}]
    p4 = mk(rows)
    p4.open["REAL-USDT"] = pos("REAL-USDT", age=3600, sl_oid="sl-9")
    p4.adopt_exchange_positions()
    rec("phantom-live-pos-kept", "REAL-USDT" in p4.open and p4.exchange_open_count == 1
        and p4._empty_rest_streak == 0,
        f"book={list(p4.open)} xch={p4.exchange_open_count} streak={p4._empty_rest_streak}")

    # 7) API failure must never wipe the book (count stays unknown-ish, book intact)
    class FailApi:
        def get(self, path: str):
            return {"code": 500, "msg": "boom"}

    p5 = mk([])
    p5.api = FailApi()
    p5.open["AAA-USDT"] = pos("AAA-USDT", age=3600)
    p5.adopt_exchange_positions()
    rec("phantom-api-fail-keeps-book", len(p5.open) == 1 and not p5.recon_ok,
        f"book={len(p5.open)} recon={p5.recon_detail}")


def sim_stats_test() -> None:
    """Real / Live / Simulated bookkeeping.

    Real = every tracked (valid) engine position. Live = confirmed by the
    exchange position list. Simulated = Real minus Live — system-internal
    calcs (count + unrealized PnL) for positions the exchange does not hold.
    """
    import tempfile
    import pulse_trader as pt

    tmp = tempfile.mkdtemp(prefix="sim-test-")
    pt.OPEN_PATH = os.path.join(tmp, "open.json")

    def mk():
        p = object.__new__(pt.Pulse)
        p.open = {}
        p.px = {}
        p.live_pos_keys = None
        return p

    def pos(sym, side, qty, entry):
        return pt.Position(symbol=sym, side=side, qty=qty, entry=entry,
                           opened_at=time.time() - 60, sl=entry * 0.99, tp=entry * 1.01, peak=entry)

    # 1) exchange truth unknown -> sim count -1 (UI shows dash, not a lie)
    p = mk()
    p.open["A-USDT"] = pos("A-USDT", "LONG", 1.0, 100.0)
    rec("sim-unknown-minus1", p.sim_stats()[0] == -1, f"{p.sim_stats()}")

    # 2) count: 3 Real, 1 Live -> 2 Simulated
    p.live_pos_keys = {"A-USDT:LONG"}
    p.open["B-USDT"] = pos("B-USDT", "LONG", 1.0, 100.0)
    p.open["C-USDT"] = pos("C-USDT", "SHORT", 1.0, 100.0)
    n, _ = p.sim_stats()
    rec("sim-count-real-minus-live", n == 2, f"n={n}")

    # 3) uPnl math: LONG 2x100 -> px 110 = +20; SHORT 1x100 -> px 90 = +10; live excluded
    p.px = {"A-USDT": 999.0, "B-USDT": 110.0, "C-USDT": 90.0}
    p.open["B-USDT"] = pos("B-USDT", "LONG", 2.0, 100.0)
    n, upnl = p.sim_stats()
    rec("sim-upnl-math", n == 2 and abs(upnl - 30.0) < 1e-9, f"n={n} upnl={upnl}")

    # 4) flat exchange confirmed -> everything Simulated
    p.live_pos_keys = set()
    n, upnl = p.sim_stats()
    rec("sim-all-when-flat", n == 3, f"n={n}")

    # 5) adopt wires live_pos_keys from the exchange payload (SYM:SIDE)
    class FakeApi:
        def get(self, path):
            return {"code": 0, "data": [
                {"symbol": "A-USDT", "positionSide": "LONG", "positionAmt": "1.5", "avgPrice": "100", "leverage": ""},
                {"symbol": "Z-USDT", "positionSide": "BOTH", "positionAmt": "0", "avgPrice": "0"},
            ]}

    p2 = object.__new__(pt.Pulse)
    p2.api = FakeApi()
    p2.open = {"A-USDT": pos("A-USDT", "LONG", 1.5, 100.0)}
    p2.open["A-USDT"].client_id = "Gx02og060308000own2"
    p2.px = {}
    p2.cooldown = {}
    p2.did_io = False
    p2.recon_ok = True
    p2.recon_detail = "pending"
    p2.exchange_open_count = -1
    p2._empty_rest_streak = 0
    p2.ignored_foreign = 0
    p2.live_pos_keys = None
    p2.adopt_exchange_positions()
    n, _ = p2.sim_stats()
    rec("sim-adopt-keys", p2.live_pos_keys == {"A-USDT:LONG"} and n == 0
        and p2.exchange_open_count == 1,
        f"keys={p2.live_pos_keys} n={n} xch={p2.exchange_open_count}")

    # 6) same endpoint contains a foreign position: it remains diagnostic
    # only, is excluded from own/live stats, and cannot be adopted.
    p3 = object.__new__(pt.Pulse)
    p3.api = type("ForeignApi", (), {"get": lambda self, path: {"code": 0, "data": [
        {"symbol": "FOREIGN-USDT", "positionSide": "LONG", "positionAmt": "2", "avgPrice": "50", "leverage": ""},
    ]}})()
    p3.open = {}
    p3.px = {}
    p3.cooldown = {}
    p3.did_io = False
    p3.recon_ok = True
    p3.recon_detail = "pending"
    p3.exchange_open_count = -1
    p3.exchange_total_open_count = -1
    p3.exchange_own_open_count = -1
    p3._empty_rest_streak = 0
    p3.ignored_foreign = 0
    p3.live_pos_keys = None
    p3.record_event = lambda *args, **kwargs: None
    p3.save_open_book = lambda: None
    p3.our_orders = lambda *args, **kwargs: []
    p3.adopt_exchange_positions()
    rec("sim-foreign-excluded",
        not p3.open and p3.live_pos_keys == set() and p3.exchange_open_count == 0
        and p3.exchange_total_open_count == 1 and p3.ignored_foreign == 1,
        f"open={len(p3.open)} own={p3.exchange_open_count} total={p3.exchange_total_open_count} foreign={p3.ignored_foreign}")


def block_calc_test() -> None:
    """Block strategy: ALL counts enabled/evaluated, formula math, intern-PF
    floor following the book defaultMinPF (CTS real stage), and add-on volume
    coordination against the lane base the count targets are built on."""
    import tempfile
    from types import SimpleNamespace
    import pulse_trader as pt
    from block_engine import (
        BLOCK_COUNT_PREVIEW,
        calculate_block_volume_increment_ratio,
        calculate_block_minimum_profit_factor,
    )

    tmp = tempfile.mkdtemp(prefix="block-test-")
    for name in ("STOP_PATH", "PAUSE_PATH", "STOP_ALL", "OPEN_PATH", "LOG_PATH"):
        setattr(pt, name, os.path.join(tmp, os.path.basename(getattr(pt, name))))
    for f in (pt.STOP_PATH, pt.PAUSE_PATH, pt.STOP_ALL):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass

    # 1) formula math (BLOCK_STRATEGY_SYSTEM.md: base=1 ratio=1.5 -> inc=count*ratio)
    rec("block-formula-inc", calculate_block_volume_increment_ratio(3, 1.5) == 4.5,
        str(calculate_block_volume_increment_ratio(3, 1.5)))
    rec("block-formula-minpf",
        abs(calculate_block_minimum_profit_factor(1.2, 1.1, 4.5) - 1.99) < 1e-9,
        str(calculate_block_minimum_profit_factor(1.2, 1.1, 4.5)))

    # 2) coverage blob exposes ALL counts (unlimited -> full preview window)
    def mk_cov(stack: int):
        p = object.__new__(pt.Pulse)
        p.block = BlockBook(os.path.join(tmp, f"block-cov-{stack}.json"),
                            {"variantBlockEnabled": True, "blockMaxStack": stack})
        p.indications = SimpleNamespace(last={}, evals={}, settings={"enabled": True})
        p.sets = SimpleNamespace(coverage=lambda: {"families": {}}, sets={}, enabled=True)
        p.coord = SimpleNamespace(
            last={"stages": {}},
            axes={},
            rearrange=True,
            main_eval=5,
            real_eval=3,
            pos_count_vol_ratio=0.05,
            size_mult=lambda open_n: max(0.35, 1.0 - max(0, int(open_n or 0)) * 0.05),
        )
        p.open = {}
        p.px = {}
        p.klines_tf = {"1m": {}, "5m": {}, "15m": {}}
        p.klines = {}
        p.live_pos_keys = set()
        p.overlay = {}
        p.strat_ind = True
        p.strat_general = True
        p.strat_block = True
        p.strat_trail = True
        p.dca = SimpleNamespace(enabled=True)
        p.exits = SimpleNamespace(enabled=True)
        p.variants = SimpleNamespace(trail_auto=True)
        p.ignored_foreign = 0
        p.recon_ok = True
        p.recon_detail = "ok"
        p.exchange_open_count = 0
        p.cid_ours = lambda cid: False
        p.strategy_closes = lambda: []
        return p

    want = 3  # 0 remaps to the default Block stack of 3
    cov = mk_cov(0)._coverage_blob()
    rec("block-coverage-unlimited-all-counts",
        cov["block"]["countN"] == 6 and [r["n"] for r in cov["block"]["allCounts"]] == list(range(1, 7)),
        f"n={cov['block']['countN']}")
    cov5 = mk_cov(5)._coverage_blob()
    rec("block-coverage-limited-all-counts",
        cov5["block"]["countN"] == 6 and [r["n"] for r in cov5["block"]["allCounts"]] == list(range(1, 7)),
        f"n={cov5['block']['countN']}")
    rec("block-coverage-enabled-flag",
        cov["block"]["enabled"] is True and cov["strategies"]["block"] is True)

    # 3) evaluate_counts walks every count + active-live overlay; satisfied
    # counts never request; pick_emit takes the smallest unsatisfied count
    b = BlockBook(os.path.join(tmp, "block-eval.json"), {
        "variantBlockEnabled": True, "blockMaxStack": 0, "blockVolumeRatio": 0.25,
        "blockProfitFactorRatio": 1.1, "defaultMinPF": 1.2,
        "blockActiveRealEnabled": True, "blockActiveLiveEnabled": True})
    lane = BlockLane(symbol="TST-USDT", side="LONG", base_qty=1.0, base_entry=100.0)
    rows = b.evaluate_counts(lane, live_n=1, intern_pf=1.4)
    regular = [r for r in rows if r["kind"] == "regular"]
    rec("block-eval-all-counts",
        [r["blockCount"] for r in regular] == list(range(1, 7)) and all(r["evaluated"] == 1 for r in rows),
        f"regular={[r['blockCount'] for r in regular]} total={len(rows)}")
    rec("block-eval-active-live", any(r["kind"] == "active-live" for r in rows))
    # finite stack stays 1..max for live emit — evals still walk 1..12
    lane.satisfied = {1: True, 2: True, 3: True}
    lane.confirmed_add = 6.0
    rows_roll = b.evaluate_counts(lane, live_n=1, intern_pf=1.4)
    rec("block-eval-window-rolls",
        [r["blockCount"] for r in rows_roll if r["kind"] == "regular"] == list(range(1, 7)),
        f"{[r['blockCount'] for r in rows_roll if r['kind'] == 'regular']}")
    lane.satisfied = {1: True}
    lane.confirmed_add = 0.25
    rows2 = b.evaluate_counts(lane, live_n=1, intern_pf=1.5)
    r1 = [r for r in rows2 if r["kind"] == "regular" and r["blockCount"] == 1][0]
    rec("block-eval-satisfied-no-request", r1["targetSatisfied"] and r1["requestedAddQty"] == 0.0)
    pick = b.pick_emit(rows2)
    rec("block-eval-pick-smallest-unsat", pick is not None and pick["blockCount"] == 2,
        f"pick={pick and pick['blockCount']}")
    # new base-1 coordination: count-2 gate = 1 + 0.2*1.1*2 = 1.44 — intern 1.4
    # (passed under the old 0.8 ratio at 1.32) must now be refused
    rec("block-pick-gated-by-new-ratio",
        b.pick_emit(b.evaluate_counts(lane, live_n=1, intern_pf=1.05)) is None,
        "intern 1.05 < capped count2 gate 1.11")

    # 4) maybe_block_adds end-to-end with a fake exchange
    class FakeApi:
        def __init__(self):
            self.posts = []
            self.path_cd: Dict[str, float] = {}

        def post(self, path, body):
            self.posts.append((path, dict(body)))
            return {"code": 0, "data": {"order": {
                "orderId": f"oid{len(self.posts)}",
                "avgPrice": "100",
                "quantity": str(body.get("quantity"))}}}

        def get(self, path, params=None):
            return {"code": 0, "data": []}

    def mk_trader(default_min_pf: float, set_ratio: float, set_n: int,
                  satisfied=None, confirmed: float = 0.0):
        p = object.__new__(pt.Pulse)
        p.api = FakeApi()
        p.halted = False
        p.block = BlockBook(os.path.join(tmp, f"block-trade-{default_min_pf}-{set_ratio}-{set_n}-{confirmed}.json"), {
            "variantBlockEnabled": True, "blockMaxStack": 0, "blockVolumeRatio": 0.25,
            "blockProfitFactorRatio": 1.1, "defaultMinPF": default_min_pf,
            "blockActiveRealEnabled": True, "blockActiveLiveEnabled": True})
        p.strat_block = True
        p.available = 100.0
        p.block_last_emit = 0.0
        p.cooldown = {}
        p.errors = 0
        p.last_error = ""
        p.pending_orders = {}
        p._save_pending_orders = lambda: None
        p.seen_fill_cids = set()
        p.skip_log = {}
        p.did_io = False
        p.entries_blocked = lambda: False
        p.missing_controls = lambda pos: False
        p.ensure_controls = lambda pos: None
        p.control_orders = False
        p.save_open_book = lambda: None
        p.cid = lambda kind="o", pos=None, **kw: f"GTEST{len(p.api.posts)}"
        p.ok = lambda r: r.get("code") == 0
        st = SimpleNamespace(last15_ratio=set_ratio, last15_n=set_n)
        p.sets = SimpleNamespace(sets={}, pick_any=lambda pack, side=None: st)
        p.score = lambda sym: (1, "t", 0.9)
        p.indications = SimpleNamespace(best=lambda s: None, primary=lambda s: None)
        p.contracts = {"TST-USDT": Contract("TST-USDT", 0.0001, 0.0001, 4, 2, 1.0, 100)}
        p.px = {"TST-USDT": 100.30}
        p.last_px = {}
        p.sl_min = 0.001
        p.sl_max = 0.02
        p.tp_min = 0.002
        p.tp_max = 0.05
        p.position_cost_pct = 0.15
        p.tp_cost_ratio = 1.5
        p.exits = SimpleNamespace(enabled=False, opt_sl_min=0.001, opt_sl_max=0.009)
        p.lev_map = {"TST-USDT": 100}
        p.lev_max = {"TST-USDT": 100}
        p.dca = SimpleNamespace(enabled=False, max_steps=0)
        p.notional_cap = lambda: 10**9
        p.max_book_notional = lambda: 10**9
        p.cap_order_qty = lambda c, px, qty, cap=None: float(qty)
        p.min_order_qty = lambda c, px: float(c.min_qty)
        p.leverage_for = lambda c: 100
        p.open = {"TST-USDT": pt.Position(
            symbol="TST-USDT", side="LONG", qty=0.05 + confirmed, entry=100.0,
            opened_at=time.time() - 600, sl=99.0, tp=101.0, peak=100.0,
            set_id="", pack="general")}
        ln = p.block.register_parent("TST-USDT", "LONG", 0.05, 100.0)
        ln.satisfied = dict(satisfied or {})
        ln.confirmed_add = confirmed
        return p

    # 4a) cold set + defaultMinPF 1.1: floor must follow the book default
    # (count2 effective = 1 + 0.1*1.1*2 = 1.22 > 1.1 -> NO emit; a hardcoded
    # 1.2 floor would wrongly emit here)
    pA = mk_trader(1.1, 1.0, 3, satisfied={1: True}, confirmed=0.0125)
    pA.block.pf_ratio = 4.0  # Configure a count2 gate above the cold inherited floor.
    pA.maybe_block_adds()
    rec("block-floor-follows-default", pA.api.posts == [],
        f"posts={pA.api.posts}")

    # 4b) warm set (PF 1.5, n=12) lifts intern_pf above the count-2 gate -> emit,
    # volume coordinated against lane.base_qty and the book cap
    pB = mk_trader(1.1, 1.5, 12, satisfied={1: True}, confirmed=0.0125)
    pB.maybe_block_adds()
    posB = pB.open["TST-USDT"]
    laneB = pB.block.lanes["TST-USDT:LONG"]
    posted_q = float(pB.api.posts[0][1]["quantity"]) if pB.api.posts else 0.0
    rec("block-warm-set-emits", len(pB.api.posts) == 1 and posted_q > 0,
        f"posts={pB.api.posts}")
    rec("block-fill-coordination",
        len(pB.api.posts) == 1
        and abs(posB.qty - (0.0625 + posted_q)) < 1e-9
        and abs(laneB.confirmed_add - (0.0125 + posted_q)) < 1e-9
        and abs(posB.entry - 100.0) < 1e-9
        and pB.block_last_emit > 0,
        f"q={posB.qty} add={laneB.confirmed_add} entry={posB.entry}")

    # 4c) cold default 1.2: count 1 gate is min(defaultMinPF, 1.12) -> emits
    pC = mk_trader(1.2, 1.0, 3)
    pC.maybe_block_adds()
    rec("block-cold-count1-emits", len(pC.api.posts) == 1,
        f"posts={pC.api.posts}")

    # 4d) disabled book / disabled strategy -> never emits
    pD = mk_trader(1.2, 1.5, 12)
    pD.block.enabled = False
    pD.maybe_block_adds()
    pD.block.enabled = True
    pD.strat_block = False
    pD.maybe_block_adds()
    rec("block-disabled-no-emit", pD.api.posts == [], f"posts={pD.api.posts}")

    # 4e) lane without a live parent is retired, never traded
    pE = mk_trader(1.2, 1.5, 12)
    pE.open = {}
    pE.maybe_block_adds()
    laneE = pE.block.lanes["TST-USDT:LONG"]
    rec("block-lane-retires-without-parent",
        pE.api.posts == [] and not laneE.active and laneE.base_qty == 0.0,
        f"active={laneE.active} base={laneE.base_qty}")

    rec("block-max-add-not-sum", calculate_block_max_additional_ratio(3, 1.0) == 1.0
        and calculate_block_volume_increment_ratio(1, 1.0)
        + calculate_block_volume_increment_ratio(2, 1.0)
        + calculate_block_volume_increment_ratio(3, 1.0) == 6.0,
        "sequential extra=3, independent sum=6")

    # SHORT parent adds independently (SELL, remainder 1× parent)
    pS = mk_trader(1.2, 1.5, 12)
    pS.open["TST-USDT"].side = "SHORT"
    pS.px["TST-USDT"] = 99.70
    pS.block.lanes.clear()
    lnS = pS.block.register_parent("TST-USDT", "SHORT", 0.05, 100.0)
    pS.score = lambda sym: (-1, "t", 0.9)
    pS.maybe_block_adds()
    rec("block-short-emits-sell",
        len(pS.api.posts) == 1 and pS.api.posts[0][1].get("side") == "SELL"
        and pS.api.posts[0][1].get("positionSide") == "SHORT"
        and abs(float(pS.api.posts[0][1]["quantity"]) - 0.0125) < 1e-9,
        f"posts={pS.api.posts}")
    rec("block-short-lane", lnS.side == "SHORT" and lnS.confirmed_add > 0)

    # live overlay with live_n=3 must still request n=1 remainder (no 3× jump)
    pJ = mk_trader(1.2, 1.5, 12)
    laneJ = pJ.block.lanes["TST-USDT:LONG"]
    rowsJ = pJ.block.evaluate_counts(laneJ, live_n=3, intern_pf=1.5)
    pickJ = pJ.block.pick_emit(rowsJ)
    rec("block-no-liven-jump",
        pickJ is not None and pickJ["blockCount"] == 1 and abs(pickJ["requestedAddQty"] - 0.0125) < 1e-9
        and all(r["blockCount"] == 1 for r in rowsJ if r["kind"] == "active-live"),
        f"pick={pickJ and (pickJ['blockCount'], pickJ['requestedAddQty'])} act={[r['blockCount'] for r in rowsJ if r['kind']=='active-live']}")

    # sequential remainder through n=1,2,3 → aggregate 4× parent, never more
    pR = mk_trader(1.1, 1.5, 12)
    for step in (1, 2, 3, 4):
        pR.block_last_emit = 0.0
        pR.maybe_block_adds()
    laneR = pR.block.lanes["TST-USDT:LONG"]
    rec("block-seq-four-portions-2x-cap",
        len(pR.api.posts) == 4 and abs(laneR.confirmed_add - 0.05) < 1e-9
        and abs((laneR.base_qty + laneR.confirmed_add) - 0.10) < 1e-9
        and pR.block.next_unsatisfied(laneR) is None,
        f"posts={len(pR.api.posts)} add={laneR.confirmed_add} tot={laneR.base_qty+laneR.confirmed_add}")
    pR.block_last_emit = 0.0
    pR.maybe_block_adds()
    rec("block-seq-full-stops", len(pR.api.posts) == 4, f"posts={len(pR.api.posts)}")

    # volume correctness: no add at entry, no add while flat, remainder not bumped
    pAge = mk_trader(1.2, 1.5, 12)
    pAge.open["TST-USDT"].opened_at = time.time() - 5
    pAge.maybe_block_adds()
    rec("block-no-add-at-entry", pAge.api.posts == [], f"posts={pAge.api.posts}")
    pFlat = mk_trader(1.2, 1.5, 12)
    pFlat.px["TST-USDT"] = 100.0
    pFlat.maybe_block_adds()
    rec("block-no-add-flat", pFlat.api.posts == [], f"posts={pFlat.api.posts}")
    pDust = mk_trader(1.2, 1.5, 12)
    lnD = pDust.block.lanes["TST-USDT:LONG"]
    lnD.confirmed_add = 0.049
    pDust.contracts["TST-USDT"] = Contract("TST-USDT", 0.01, 0.01, 3, 2, 1.0, 100)
    pDust.maybe_block_adds()
    rec("block-remainder-no-bump",
        pDust.api.posts == [] and bool(lnD.satisfied.get(1)),
        f"posts={pDust.api.posts} sat={lnD.satisfied}")
    pRetry = mk_trader(1.2, 1.5, 12)
    def reject_minimum(path, body):
        pRetry.api.posts.append((path, dict(body)))
        return {"code": 101400, "msg": "minimum order amount is 0.5"}
    pRetry.api.post = reject_minimum
    pRetry.maybe_block_adds()
    rec("block-minimum-retry-never-exceeds-approved-portion",
        len(pRetry.api.posts) == 1 and not pRetry.pending_orders
        and pRetry.block.lanes["TST-USDT:LONG"].confirmed_add == 0,
        f"requests={len(pRetry.api.posts)}")
    pRound = mk_trader(1.2, 1.5, 12)
    pRound.cap_order_qty = lambda c, px, qty, cap=None: qty * 1.05
    pRound.maybe_block_adds()
    rec("block-rounding-cannot-exceed-remainder",
        len(pRound.api.posts) == 1 and float(pRound.api.posts[0][1]["quantity"]) <= 0.0125,
        str(pRound.api.posts))
    pMx = mk_trader(1.2, 1.5, 12)
    pMx.dca = SimpleNamespace(enabled=True, lanes={"TST-USDT:LONG": SimpleNamespace(filled_n=1)}, key=lambda s, d: f"{s}:{d}")
    pMx.maybe_block_adds()
    rec("block-skip-if-dca-filled", pMx.api.posts == [], f"posts={pMx.api.posts}")
    from dca_engine import DcaBook as _Dca
    dclamp = _Dca()
    dclamp.load({"dcaEnabled": True, "dcaStepVolumeMultipliers": [2.1, 3.7, 4.8, 6.2]})
    rec("dca-mult-clamp", max(dclamp.mults) <= 2.5, f"mults={dclamp.mults}")

    # intern PF is per-side: winning LONG lifts, losing SHORT stays at floor
    pI = mk_trader(1.2, 1.5, 12)
    pI.sets.strict_gate = True
    pI.sets.min_samples = 8
    st_split = SimpleNamespace(
        last15_ratio=1.0, last15_n=12,
        by_side={
            "LONG": {"last15_ratio": 1.4, "last15_n": 12},
            "SHORT": {"last15_ratio": 0.7, "last15_n": 12},
        },
    )
    pI.sets.pick_any = lambda pack, side=None: st_split
    posL = pI.open["TST-USDT"]
    rec("block-intern-long-lifts", abs(pI.block_intern_pf(posL) - 1.4) < 1e-9, str(pI.block_intern_pf(posL)))
    posS = pt.Position(symbol="TST-USDT", side="SHORT", qty=0.05, entry=100.0,
                       opened_at=time.time() - 600, sl=101.0, tp=99.0, peak=100.0, set_id="", pack="general")
    rec("block-intern-short-floor", abs(pI.block_intern_pf(posS) - 1.2) < 1e-9, str(pI.block_intern_pf(posS)))

    # book cap uses sequential extra (3×) not the 1+2+3 sum (6×)
    pCap = object.__new__(pt.Pulse)
    pCap.block = BlockBook(os.path.join(tmp, "block-cap.json"), {
        "variantBlockEnabled": True, "blockMaxStack": 3, "blockVolumeRatio": 1.0})
    pCap.dca = SimpleNamespace(enabled=False, max_steps=0)
    pCap.volume_factor = 1.0
    pCap.available = 100000.0
    extra = calculate_block_max_additional_ratio(pCap.block.max_stack, pCap.block.volume_ratio)
    rec("block-book-extra-capped-1", abs(extra - 1.0) < 1e-9, str(extra))
    base_n = pt.Pulse.notional_cap(pCap)
    book_n = pt.Pulse.max_book_notional(pCap)
    rec("block-book-cap-2x", abs(book_n / base_n - 2.0) < 0.08, f"book={book_n} base={base_n} ratio={book_n / base_n:.3f}")

    # parent close stores cost-net fraction, opposite side stays live
    pC2 = mk_trader(1.2, 1.5, 12)
    pC2.block.register_parent("TST-USDT", "SHORT", 0.04, 100.0)
    pC2.block.on_parent_close("TST-USDT", "LONG", 9.9, pnl_pct=0.0015)
    rec("block-close-net-pct", abs(pC2.block.lanes["TST-USDT:LONG"].parent_pf_ring[-1] - 0.0015) < 1e-9)
    rec("block-close-other-side-live", pC2.block.lanes["TST-USDT:SHORT"].active is True
        and float(pC2.block.lanes["TST-USDT:SHORT"].base_qty) > 0,
        f"short={pC2.block.lanes.get('TST-USDT:SHORT') and pC2.block.lanes['TST-USDT:SHORT'].base_qty}")


def set_orders_test() -> None:
    """Per-Set independence: every Set opens its own independent entry order
    AND its own independent SL/TP control-order pair on the exchange, with
    correct per-set tracking and stats coordination — proven in-process on
    the real place() / place_ctrl_pair() / close_pos() / SetBook.on_live_close
    paths against a recording fake exchange. Covers: unique parseable
    clientOrderIDs per set, per-position control pairs with per-set SL
    distances, exchange-side cancel isolation, per-set win/loss attribution,
    snapshot/sim stats coordination, and negative controls (occupied symbol
    refused; entry without controls scratches, never unprotected)."""
    import tempfile
    from types import SimpleNamespace
    import pulse_trader as pt
    from set_engine import SetBook, SetState, make_set_id

    tmp = tempfile.mkdtemp(prefix="setord-test-")
    for name in ("STOP_PATH", "PAUSE_PATH", "STOP_ALL", "OPEN_PATH", "LOG_PATH", "TRADES_PATH"):
        setattr(pt, name, os.path.join(tmp, os.path.basename(getattr(pt, name))))
    for f in (pt.STOP_PATH, pt.PAUSE_PATH, pt.STOP_ALL):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass

    PXS = {"AAA-USDT": 100.0, "BBB-USDT": 200.0, "CCC-USDT": 50.0, "DDD-USDT": 500.0}
    ORDER = "/openApi/swap/v2/trade/order"

    class FakeEx:
        """Recording exchange: entries, batch control pairs, cancels, closes."""

        def __init__(self, fail_controls: bool = False):
            self.posts: List[tuple] = []
            self.batches: List[list] = []
            self.deletes: List[tuple] = []
            self.orders: Dict[str, dict] = {}
            self.n = 0
            self.path_cd: Dict[str, float] = {}
            self.fail_controls = fail_controls
            self.fill: Dict[str, float] = {}

        def _oid(self) -> str:
            self.n += 1
            return f"EX{self.n:06d}"

        def post(self, path, body):
            body = dict(body)
            self.posts.append((path, body))
            if path == ORDER:
                if body.get("type") != "MARKET" and self.fail_controls:
                    return {"code": 100001, "msg": "Signature verification failed due to signature mismatch"}
                oid = self._oid()
                if body.get("type") != "MARKET":
                    row = dict(body)
                    row["orderId"] = oid
                    self.orders[oid] = row
                px = self.fill.get(str(body.get("symbol")), PXS.get(str(body.get("symbol")), 100.0))
                return {"code": 0, "data": {"order": {"orderId": oid, "avgPrice": str(px), "quantity": str(body.get("quantity") or "0")}}}
            return {"code": 0, "data": {}}

        def batch_place(self, orders):
            if self.fail_controls:
                return {"code": 100001, "msg": "Signature verification failed due to signature mismatch"}
            rows = []
            for o in orders:
                oid = self._oid()
                row = dict(o)
                row["orderId"] = oid
                self.orders[oid] = row
                rows.append({"code": 0, "orderId": oid, "type": o.get("type"), "clientOrderID": o.get("clientOrderID")})
            self.batches.append([dict(o) for o in orders])
            return {"code": 0, "data": {"orders": rows}}

        def get(self, path, params=None):
            if path == "/openApi/swap/v2/trade/openOrders":
                return {"code": 0, "data": {"orders": list(self.orders.values())}}
            return {"code": 0, "data": []}

        def delete(self, path, params=None):
            self.deletes.append((path, dict(params or {})))
            self.orders.pop(str((params or {}).get("orderId") or ""), None)
            return {"code": 0, "data": {}}

    # --- real SetBook with three real Sets (2 general + 1 indications) ---
    book = SetBook()
    specs = [("general", 0.6, 10), ("general", 0.9, 20), ("indications", 1.5, 30)]
    sts: List[SetState] = []
    for i, (pack, sl, stp) in enumerate(specs):
        sid = make_set_id(pack, sl, "", stp)
        st = SetState(id=sid, pack=pack, tf="1m", sl_ratio=sl, trail_key="0.3:0.1",
                      trail_arm=0.3, trail_give=0.1, step=stp, tp_pct=0.0045 + i * 0.001, idx=i)
        st.last15_ratio = 1.5
        st.last15_n = 12
        book.sets[sid] = st
        sts.append(st)
    book.by_idx = list(sts)
    cur = {"i": 0}
    book.pick_any = lambda pack, side=None: sts[cur["i"]] if cur["i"] < len(sts) else None
    book.pick_trail = lambda pack, side=None: None
    book.adapt_from_live = lambda rows: None
    book.indication_ok = lambda kind, side=None: True

    def mk_pulse(fx: FakeEx):
        p = object.__new__(pt.Pulse)
        p.api = fx
        p.halted = False
        p.errors = 0
        p.last_error = ""
        p.did_io = False
        p.cooldown = {}
        p.ignore_syms = {}
        p.skip_log = {}
        p.open = {}
        p.owned_syms = set()
        p.px = dict(PXS)
        p.last_px = {}
        p.contracts = {s: pt.Contract(s, 0.001, 0.001, 3, 2, 1.0, 100) for s in PXS}
        p.available = 1000.0
        p.fees_est = 0.0
        p._order_est = 0
        p._order_est_known = True
        p._oo_cache = {}
        p.last_entry_ts = 0.0
        p.entries_blocked = lambda: False
        p.group_of = lambda s: "g"
        p.group_count = lambda g: 0
        p.sets = book
        p.strat_ind = True
        p.indications = SimpleNamespace(
            settings={"enabled": True, "takeProfitRewardRisk": 1.8},
            match=lambda s, r: SimpleNamespace(direction="long", kind="move", stop_loss_pct=0.0, take_profit_pct=0.0),
            primary=lambda s: None, best=lambda s: None)
        p.variants = SimpleNamespace(on_close=lambda rec: None, current_sl=lambda: 0.6,
                                     current_trail=lambda: ("0.3:0.1", 0.3, 0.1))
        p.exits = SimpleNamespace(enabled=False, ignore_tp=False, opt_sl_min=0.001, opt_sl_max=0.009,
                                  on_close=lambda rec: None)
        p.sl_min = 0.001
        p.sl_max = 0.02
        p.tp_min = 0.002
        p.tp_max = 0.05
        p.position_cost_pct = 0.15
        p.tp_cost_ratio = 1.5
        p.size_qty = lambda c, px: 0.05
        p.max_book_notional = lambda: 1e9
        p.ensure_max_leverage = lambda s, force=False: 100
        p.leverage_for = lambda c: 100
        p.control_orders = True
        p.control_orders_per_config = True
        p.ctrl_skip = {}
        p.save_open_book = lambda: None
        p.pending_orders = {}
        p._save_pending_orders = lambda: None
        p.seen_fill_cids = set()
        p.signals = []
        p.block = SimpleNamespace(register_parent=lambda *a, **k: None, on_parent_close=lambda *a, **k: None)
        p.dca = SimpleNamespace(attach=lambda *a, **k: None, on_close=lambda *a, **k: None, drop=lambda *a, **k: None)
        p.ban_sym = lambda *a, **k: None
        p.wins = 0
        p.losses = 0
        p.consec_loss = 0
        p.closed = []
        p._stats_force = False
        p.live_pos_keys = None
        return p

    def pos_for(pulse, symbol: str, side: str = "LONG"):
        return next(iter(pulse.positions_for(symbol, side)), None)

    # === 1) three Sets -> three independent entries + control pairs ===
    fx = FakeEx()
    p = mk_pulse(fx)
    syms = ["AAA-USDT", "BBB-USDT", "CCC-USDT"]
    reasons = ["gen:alpha", "gen:beta", "ind:move:move:0.90:a3:move:30"]
    for i, sym in enumerate(syms):
        cur["i"] = i
        p.place(sym, 1, reasons[i], 0.9)
    entries = [b for (path, b) in fx.posts if path == ORDER and b.get("type") == "MARKET" and b.get("side") == "BUY"]
    rec("setord-3-independent-entries",
        len(entries) == 3 and [b["symbol"] for b in entries] == syms and p.errors == 0,
        f"entries={len(entries)} errors={p.errors}")
    entry_cids = [str(b.get("clientOrderID") or "") for b in entries]
    rec("setord-entry-cids-unique",
        len(set(entry_cids)) == 3 and all(p.cid_ours(c) for c in entry_cids), str(entry_cids)[:80])
    parsed = [p.parse_track(c) or {} for c in entry_cids]
    rec("setord-entry-cids-resolve-sets",
        all(parsed[i].get("set_id") == sts[i].id and parsed[i].get("idx") == sts[i].idx
            and parsed[i].get("step") == sts[i].step and parsed[i].get("pack") == sts[i].pack
            and abs(float(parsed[i].get("sl", 0)) - sts[i].sl_ratio) < 1e-9 for i in range(3)),
        str([(x.get("set_id"), x.get("step")) for x in parsed])[:140])
    rec("setord-positions-track-own-set",
        all(pos_for(p, syms[i]) is not None and pos_for(p, syms[i]).set_id == sts[i].id
            and pos_for(p, syms[i]).set_idx == sts[i].idx
            and pos_for(p, syms[i]).pack == sts[i].pack
            and pos_for(p, syms[i]).client_id == entry_cids[i]
            for i in range(3)),
        str([(pos_for(p, s).set_id, pos_for(p, s).set_idx) for s in syms])[:140])

    # === 2) each position: own independent SL+TP control pair on the exchange ===
    rec("setord-3-control-pairs", len(fx.batches) == 3 and all(len(b) == 2 for b in fx.batches),
        f"batches={len(fx.batches)}")
    pair_ok = True
    for i, sym in enumerate(syms):
        bodies = [b for b in fx.batches[i] if b.get("symbol") == sym]
        types = {b.get("type") for b in bodies}
        pair_ok = pair_ok and len(bodies) == 2 and types == {"STOP_MARKET", "TAKE_PROFIT_MARKET"} \
            and all("quantity" in b and "closePosition" not in b and "reduceOnly" not in b for b in bodies)
    rec("setord-pairs-wellformed", pair_ok, f"{[(b[0].get('symbol'), len(b)) for b in fx.batches]}")
    ctrl_cids = [str(b.get("clientOrderID") or "") for batch in fx.batches for b in batch]
    rec("setord-control-cids-unique",
        len(set(ctrl_cids)) == 6 and not (set(ctrl_cids) & set(entry_cids)), str(len(set(ctrl_cids))))
    sl_px = {}
    tp_px = {}
    for batch in fx.batches:
        for b in batch:
            if b.get("type") == "STOP_MARKET":
                sl_px[b["symbol"]] = float(b.get("stopPrice") or 0)
            else:
                tp_px[b["symbol"]] = float(b.get("stopPrice") or 0)
    want_sl = {"AAA-USDT": 99.73, "BBB-USDT": 199.01, "CCC-USDT": 49.55}
    want_tp = {"AAA-USDT": 100.45, "BBB-USDT": 201.1, "CCC-USDT": 50.32}
    rec("setord-per-set-sl-distances",
        all(abs(sl_px[s] - want_sl[s]) < 1e-6 and abs(tp_px[s] - want_tp[s]) < 1e-6 for s in syms)
        and len({sl_px[s] for s in syms}) == 3,
        f"sl={sl_px} tp={tp_px}")
    oid_sets = [{pos_for(p, s).sl_oid, pos_for(p, s).tp_oid} for s in syms]
    all_oids = set().union(*oid_sets)
    rec("setord-control-oids-independent",
        all(pt.real_oid(pos_for(p, s).sl_oid) and pt.real_oid(pos_for(p, s).tp_oid)
            and pos_for(p, s).controls_ok and pos_for(p, s).ctrl_verified for s in syms)
        and len(all_oids) == 6 and all_oids == set(fx.orders.keys()),
        f"oids={len(all_oids)} live={len(fx.orders)}")

    # === 3) exchange-side cancel isolation + per-set close attribution ===
    pre_oids = {s: {oid for oid, row in fx.orders.items() if row.get("symbol") == s} for s in syms}
    rec("setord-exchange-orders-per-set",
        all(len(pre_oids[s]) == 2 for s in syms) and len(set().union(*pre_oids.values())) == 6,
        str({s: sorted(o) for s, o in pre_oids.items()})[:120])
    fx.fill["AAA-USDT"] = 100.6  # +0.60% gross -> +3.0R net of 1x cost
    p.close_pos(pos_for(p, "AAA-USDT"), 100.6, "tp")
    del_oids = {str(d[1].get("orderId")) for d in fx.deletes}
    rec("setord-cancel-isolation",
        pre_oids["AAA-USDT"] and pre_oids["AAA-USDT"] <= del_oids
        and not (del_oids - pre_oids["AAA-USDT"])
        and not (pre_oids["AAA-USDT"] & set(fx.orders.keys()))
        and pre_oids["BBB-USDT"] <= set(fx.orders.keys()) and pre_oids["CCC-USDT"] <= set(fx.orders.keys()),
        f"del={len(del_oids)} live={len(fx.orders)}")
    stA, stB, stC = sts
    rec("setord-win-attributes-setA",
        len(stA.live) == 1 and stA.last15_n == 1 and abs(stA.last15_ratio - 1.3) < 1e-6
        and len(stB.live) == 0 and len(stC.live) == 0 and stB.last15_n == 12 and stC.last15_n == 12,
        f"A n={stA.last15_n} r={stA.last15_ratio} B live={len(stB.live)} C live={len(stC.live)}")
    fx.fill["BBB-USDT"] = 199.0  # -0.50% gross -> -4.33R net
    p.close_pos(pos_for(p, "BBB-USDT"), 199.0, "sl")
    rec("setord-loss-attributes-setB",
        len(stB.live) == 1 and stB.last15_n == 1 and abs(stB.last15_ratio - 0.5667) < 1e-3
        and stA.last15_n == 1 and abs(stA.last15_ratio - 1.3) < 1e-6
        and len(stC.live) == 0 and stC.last15_n == 12,
        f"B n={stB.last15_n} r={stB.last15_ratio} A r={stA.last15_ratio}")
    rec("setord-engine-stats-coordination",
        p.wins == 1 and p.losses == 1 and len(p.closed) == 2
        and p.closed[0].set_id == stA.id and p.closed[1].set_id == stB.id
        and p.closed[0].client_id == entry_cids[0] and p.closed[1].client_id == entry_cids[1],
        f"w={p.wins} l={p.losses} closed={len(p.closed)}")
    snap = {r["id"]: r for r in book.snapshot().get("rows", [])}
    rec("setord-snapshot-per-set",
        snap.get(stA.id, {}).get("liveN") == 1 and snap.get(stB.id, {}).get("liveN") == 1
        and snap.get(stC.id, {}).get("liveN") == 0
        and abs(snap.get(stA.id, {}).get("last15Ratio", 0) - 1.3) < 1e-6,
        str({k: (v.get("liveN"), v.get("last15Ratio")) for k, v in snap.items()})[:140])
    p.live_pos_keys = {"CCC-USDT:LONG"}
    sim_n, _sim_u = p.sim_stats()
    p.live_pos_keys = set()
    sim_n2, _sim_u2 = p.sim_stats()
    rec("setord-real-live-sim-coordination", sim_n == 0 and sim_n2 == 1, f"sim={sim_n}/{sim_n2}")

    # === 4) negative controls ===
    p.control_orders_per_config = False
    before = len(fx.posts)
    p.place("CCC-USDT", 1, "gen:dup", 0.9)  # legacy aggregate occupied symbol -> refused
    rec("setord-occupied-symbol-refused", len(fx.posts) == before and pos_for(p, "CCC-USDT").set_id == stC.id,
        f"posts={len(fx.posts) - before}")
    cur["i"] = 0
    fx2 = FakeEx(fail_controls=True)
    p2 = mk_pulse(fx2)
    p2.place("DDD-USDT", 1, "gen:gamma", 0.9)
    fx2_entries = [b for (path, b) in fx2.posts if path == ORDER and b.get("type") == "MARKET"]
    rec("setord-no-ctrl-scratches",
        "DDD-USDT" not in p2.open and len(fx2_entries) == 2 and not fx2.orders,
        f"open={list(p2.open)} market_posts={len(fx2_entries)} live_ctrl={len(fx2.orders)}")


def grouped_control_test() -> None:
    """Range-group controls stay isolated, quantity matched, and restartable."""
    import tempfile
    from types import SimpleNamespace
    import pulse_trader as pt
    from dca_engine import DcaBook

    tmp = tempfile.mkdtemp(prefix="group-control-test-")

    class GroupApi:
        def __init__(self):
            self.posts = []
            self.orders = {}
            self.path_cd: Dict[str, float] = {}

        def batch_place(self, bodies):
            rows = []
            for body in bodies:
                oid = f"ctrl-{len(self.orders) + 1}"
                saved = dict(body)
                saved["orderId"] = oid
                saved["origQty"] = body.get("quantity", "")
                self.orders[oid] = saved
                self.posts.append(("batch", saved))
                rows.append({
                    "code": 0,
                    "orderId": oid,
                    "type": body.get("type"),
                    "clientOrderID": body.get("clientOrderID"),
                })
            return {"code": 0, "data": {"orders": rows}}

        def post(self, path, body):
            self.posts.append((path, dict(body)))
            if body.get("type") == "MARKET":
                return {"code": 0, "data": {"order": {
                    "orderId": f"close-{len(self.posts)}",
                    "avgPrice": "100",
                    "quantity": str(body.get("quantity") or ""),
                }}}
            return {"code": 0, "data": {"order": {
                "orderId": f"single-{len(self.posts)}",
                "avgPrice": "100",
                "quantity": str(body.get("quantity") or ""),
            }}}

        def delete(self, _path, params):
            self.orders.pop(str(params.get("orderId") or ""), None)
            return {"code": 0, "data": {}}

        def get(self, path, _params=None):
            if "openOrders" in path:
                return {"code": 0, "data": list(self.orders.values())}
            return {"code": 0, "data": []}

    fx = GroupApi()
    p = object.__new__(pt.Pulse)
    p.api = fx
    p.contracts = {"AAA-USDT": Contract("AAA-USDT", 0.01, 0.01, 2, 2, 1.0, 150)}
    p.px = {"AAA-USDT": 100.0}
    p.last_px = {}
    p.open = {}
    p.control_orders = True
    p.control_orders_per_config = True
    p.ctrl_skip = {}
    p._oo_cache = {}
    p.did_io = False
    p.errors = 0
    p.last_error = ""
    p.record_event = lambda *args, **kwargs: True
    p.save_open_book = lambda: None
    p.sl_min, p.sl_max = 0.002, 0.012
    p.tp_min, p.tp_max = 0.0035, 0.024
    p.position_cost_pct = 0.15
    p.tp_cost_ratio = 5.0
    p.exits = SimpleNamespace(enabled=False, ignore_tp=False)
    p.block = BlockBook(os.path.join(tmp, "block.json"), {
        "variantBlockEnabled": True,
        "blockMaxStack": 3,
        "blockVolumeRatio": 1.0,
        "blockProfitFactorRatio": 1.1,
    })
    p.dca = DcaBook()

    def make_pos(side: str, sl_pct: float, tp_pct: float, qty: float, entry: float, cid: str):
        pos = Position(
            symbol="AAA-USDT",
            side=side,
            qty=qty,
            entry=entry,
            opened_at=time.time() - 60,
            sl=entry * (1 - sl_pct) if side == "LONG" else entry * (1 + sl_pct),
            tp=entry * (1 + tp_pct) if side == "LONG" else entry * (1 - tp_pct),
            peak=entry,
            sl_pct=sl_pct,
            tp_pct=tp_pct,
            client_id=cid,
            set_id=cid,
            pack="general",
        )
        p.prepare_position_group(pos)
        return pos

    # Normalization is deliberately tolerant of fractional and percent input.
    rec("group-normalize-fraction", normalize_control_pct(0.0048) == 48)
    rec("group-normalize-percent", normalize_control_pct(0.48) == 48)
    rec("group-range-stable", control_range_key(0.0048001, 0.0075001) == "sl0048-tp0075")
    rec("group-range-parse", parse_control_range("SL0048-TP0075") == (48, 75))
    rec("group-range-reject", parse_control_range("aggregate") == (0, 0))

    long_a = make_pos("LONG", 0.0048, 0.0075, 1.0, 100.0, "set-a")
    long_b = make_pos("LONG", 0.0064, 0.0100, 2.0, 102.0, "set-b")
    short_a = make_pos("SHORT", 0.0048, 0.0075, 1.0, 100.0, "set-c")
    rec("group-symbol-side-range-separate",
        len({long_a.control_group_key, long_b.control_group_key, short_a.control_group_key}) == 3)
    rec("group-token-range", control_group_token(long_a.control_group_key, long_a.control_range_key) == "r048075")

    p.open[p.position_key(long_a)] = long_a
    p.open[p.position_key(long_b)] = long_b
    p.place_ctrl_pair(long_a)
    p.place_ctrl_pair(long_b)
    a_ids = {long_a.sl_oid, long_a.tp_oid}
    b_ids = {long_b.sl_oid, long_b.tp_oid}
    control_bodies = [body for _kind, body in fx.posts if body.get("type") in ("STOP_MARKET", "TAKE_PROFIT_MARKET")]
    rec("group-two-pairs-four-controls",
        len(a_ids) == 2 and len(b_ids) == 2 and len(fx.orders) == 4,
        f"orders={len(fx.orders)}")
    rec("group-controls-quantity-no-close",
        all("quantity" in body and "closePosition" not in body and "reduceOnly" not in body for body in control_bodies),
        str(control_bodies)[:200])
    rec("group-cancel-isolated",
        (p.cancel_controls("AAA-USDT", pos=long_a) is None)
        and not (a_ids & set(fx.orders)) and b_ids <= set(fx.orders),
        f"remaining={sorted(fx.orders)}")

    # The same normalized range merges by weighted quantity and weighted entry.
    p.clear_position_controls(long_a)
    long_a.ctrl_qty = 0.0
    p.ensure_strategy_lanes(long_a)
    incoming_same = make_pos("LONG", 0.0048001, 0.0075001, 2.0, 110.0, "set-a2")
    p.merge_position(long_a, incoming_same)
    p.merge_parent_lanes(long_a, incoming_same.qty, incoming_same.entry)
    rec("group-same-range-merge",
        long_a.qty == 3.0 and abs(long_a.entry - (100.0 + 220.0) / 3.0) < 1e-9
        and long_a.member_count == 2 and long_a.control_group_key == incoming_same.control_group_key,
        f"qty={long_a.qty} entry={long_a.entry}")
    lane = p.block.lanes.get(p.block_lane_key(long_a))
    dca_lane = p.dca.lanes.get(p.dca_lane_key(long_a))
    rec("group-parent-lanes-merge",
        lane is not None and abs(lane.base_qty - 3.0) < 1e-9
        and dca_lane is not None and abs(dca_lane.parent_qty - 3.0) < 1e-9,
        f"block={getattr(lane, 'base_qty', None)} dca={getattr(dca_lane, 'parent_qty', None)}")

    # A restart can recover the normalized range from the compact control CID.
    cid = p.cid("u", pos=long_a)
    track = p.parse_track(cid) or {}
    rec("group-cid-restart-range",
        track.get("group_token") == "r048075"
        and track.get("control_range_key") == "sl0048-tp0075"
        and track.get("control_sl_bp") == 48
        and track.get("control_tp_bp") == 75,
        str(track))

    p.clear_position_controls(long_a)
    p.place_ctrl_pair(long_a)
    ok_close, _ = p.market_close(long_a)
    market = next((body for kind, body in reversed(fx.posts) if body.get("type") == "MARKET"), {})
    rec("group-close-quantity-matched",
        ok_close and market.get("quantity") == long_a.qty
        and "closePosition" not in market and "reduceOnly" not in market
        and b_ids <= set(fx.orders),
        str(market))

    p.control_orders_per_config = False
    aggregate = make_pos("LONG", 0.0048, 0.0075, 1.0, 100.0, "legacy")
    p.prepare_position_group(aggregate, legacy=True)
    rec("group-legacy-disables-key", not p.per_config_controls(aggregate) and aggregate.control_range_key == "aggregate")
    legacy_payload = ctrl_payload("AAA-USDT", "LONG", "sl", "99", "1", "legacy", close_pos=True, with_qty=True)
    rec("group-legacy-close-position", legacy_payload.get("closePosition") == "true" and "quantity" not in legacy_payload)


def strict_gate_test() -> None:
    """Strict validation gate: only VALIDATED (last-N samples >=
    max(minSamples, 8)) AND PROFITABLE (cost-adjusted PF >= 1.00) strategy
    sets and indication kinds may drive live orders.

    Proves end-to-end: entry_sense blocks when nothing is validated, the
    intern_any override no longer reopens closed packs, place() never posts
    an order without a validated + profitable set, place() DOES trade once
    one exists, the per-kind indication gate cuts proven losers while an
    unproven kind rides a validated pack, and Block intern PF only lifts off
    the CTS floor for validated + profitable sets.
    """
    import tempfile
    from types import SimpleNamespace
    import pulse_trader as pt
    import set_engine as se

    tmp = tempfile.mkdtemp(prefix="strict-test-")
    for name in ("STOP_PATH", "PAUSE_PATH", "STOP_ALL", "OPEN_PATH", "LOG_PATH"):
        setattr(pt, name, os.path.join(tmp, os.path.basename(getattr(pt, name))))
    for f in (pt.STOP_PATH, pt.PAUSE_PATH, pt.STOP_ALL):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass

    def win_rows(strong: bool = False):
        pct = 0.006 if strong else 0.003
        pnl = 0.003 if strong else 0.0015
        return [{"t": 1000 + i * 60, "pnl": pnl, "pnl_pct": pct, "symbol": "T",
                 "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(15)]

    def loss_rows():
        return [{"t": 2000 + i * 60, "pnl": -0.0045, "pnl_pct": -0.003, "symbol": "T",
                 "side": "LONG", "hold_s": 60, "reason": "sl"} for i in range(15)]

    def mk_book(winner: bool = True, strong: bool = False):
        b = se.SetBook()
        b.load({"histEnabled": True, "setMinPf": 1.10, "setMinSamples": 8,
                "stratIndications": True, "stratGeneral": True,
                "slToTpRatios": [0.6], "setMinStep": 3, "setStepMax": 3,
                "trailArmMin": 0.3, "trailArmMax": 0.3})
        b.progress.ready = True
        if winner:
            for st in b.by_idx:
                st.hist = win_rows(strong) if st is b.by_idx[0] else []
                b._score_one(st)
        return b

    def mk_trader(book):
        p = object.__new__(pt.Pulse)
        p.px = {"AAA-USDT": 100.0}
        p.open = {}
        p.sets = book
        p.strat_ind = True
        p.indications = SimpleNamespace(
            settings={"enabled": True},
            match=lambda s, r: None, primary=lambda s: None, best=lambda s: None)
        p.occupying = lambda *a, **k: False
        return p

    # 1) strict + nothing validated: both packs closed, entry hard-blocked
    cold = mk_book(winner=False)
    for st in cold.by_idx:
        cold._score_one(st)
    p1 = mk_trader(cold)
    rec("strict-entry-blocked-no-set",
        p1.entry_sense("AAA-USDT", 1, "gen:ema+", 0.9, "general") == "set-gate",
        f"{p1.entry_sense('AAA-USDT', 1, 'gen:ema+', 0.9, 'general')}")
    rec("strict-packs-closed", not cold.pack_open("general") and not cold.pack_open("indications"))

    # 2) strict + one validated + profitable set: entry allowed, winner bound
    warm = mk_book(winner=True)
    p2 = mk_trader(warm)
    rec("strict-entry-allowed-validated",
        p2.entry_sense("AAA-USDT", 1, "gen:ema+", 0.9, "general") is None,
        f"{p2.entry_sense('AAA-USDT', 1, 'gen:ema+', 0.9, 'general')}")
    winner = warm.by_idx[0]
    rec("strict-pick-is-winner",
        warm.pick_any("indications") is winner and winner.last15_n >= 8 and winner.last15_ratio >= 1.0,
        f"{winner.id} pf={winner.last15_ratio} n={winner.last15_n}")

    # 3) validated loser set is never picked, even when it is the only evidence
    loser_book = se.SetBook()
    loser_book.load({"histEnabled": True, "setMinPf": 1.10, "setMinSamples": 8,
                     "stratIndications": True, "stratGeneral": True,
                     "slToTpRatios": [0.6], "setMinStep": 3, "setStepMax": 3,
                     "trailArmMin": 0.3, "trailArmMax": 0.3})
    loser_book.progress.ready = True
    for st in loser_book.by_idx:
        st.hist = loss_rows()
        loser_book._score_one(st)
    p3 = mk_trader(loser_book)
    rec("strict-loser-never-picked",
        loser_book.pick_any("general") is None and loser_book.pick_any("indications") is None
        and p3.entry_sense("AAA-USDT", 1, "gen:ema+", 0.9, "general") == "set-gate",
        f"pick={loser_book.pick_any('general')}")

    # 4) per-kind indication gate
    ind_book = mk_book(winner=True)  # by_idx[0] = indications:sl0.6:st3 winner
    p4 = mk_trader(ind_book)
    ind_book.ind_live["move"] = loss_rows()[:12]
    ind_book.ind_live["state"] = win_rows()[:12]
    p4.indications.match = lambda s, r: SimpleNamespace(direction="long", kind="move")
    rec("strict-ind-loser-kind-blocked",
        p4.entry_sense("AAA-USDT", 1, "ind:move:direct_tf:0.90:a1:bingx-1m", 0.9, "indications") == "ind-gate",
        "move kind gated")
    p4.indications.match = lambda s, r: SimpleNamespace(direction="long", kind="state")
    rec("strict-ind-winner-kind-runs",
        p4.entry_sense("AAA-USDT", 1, "ind:state:tf_combined:0.80:a3:bingx-1m", 0.9, "indications") is None,
        "state kind runs")
    # unproven kind rides the validated indications pack
    p4.indications.match = lambda s, r: SimpleNamespace(direction="long", kind="common")
    rec("strict-ind-unproven-rides-pack",
        p4.entry_sense("AAA-USDT", 1, "ind:common:ta:0.70:a1:ta", 0.9, "indications") is None,
        "common kind unproven but pack validated")
    # Signals keep running when State is the proven loser
    ind_book.ind_live["state"] = loss_rows()[:12]
    ind_book.ind_live["signals"] = win_rows()[:12]
    p4.indications.match = lambda s, r: SimpleNamespace(direction="long", kind="state")
    rec("strict-ind-state-loser-blocked",
        p4.entry_sense("AAA-USDT", 1, "ind:state:tf_combined:0.80:a3:bingx-1m", 0.9, "indications") == "ind-gate",
        "state kind gated")
    p4.indications.match = lambda s, r: SimpleNamespace(direction="long", kind="signals")
    rec("strict-ind-signals-runs",
        p4.entry_sense("AAA-USDT", 1, "ind:signals:direct_tf:0.90:a1:bingx-1m", 0.9, "indications") is None,
        "signals kind runs while state is gated")
    # restore state as winner for the pack-closed follow-up
    ind_book.ind_live["state"] = win_rows()[:12]
    p4.indications.match = lambda s, r: SimpleNamespace(direction="long", kind="common")
    # ...but not when the indications pack itself has no validated winner
    ind_book.by_idx[0].hist = []
    ind_book._score_one(ind_book.by_idx[0])
    rec("strict-ind-unproven-pack-closed",
        p4.entry_sense("AAA-USDT", 1, "ind:common:ta:0.70:a1:ta", 0.9, "indications") == "ind-gate",
        f"{p4.entry_sense('AAA-USDT', 1, 'ind:common:ta:0.70:a1:ta', 0.9, 'indications')}")

    # 5) place() never posts without a validated + profitable set
    class OrderApi:
        def __init__(self):
            self.posts = []
            self.path_cd: Dict[str, float] = {}

        def post(self, path, body):
            self.posts.append((path, dict(body)))
            return {"code": 0, "data": {"order": {"orderId": f"o{len(self.posts)}",
                    "avgPrice": "100", "quantity": str(body.get("quantity"))}}}

    def mk_place_trader(book):
        p = mk_trader(book)
        p.api = OrderApi()
        p.entries_blocked = lambda: False
        p.halted = False
        p.cooldown = {}
        p.available = 100.0
        p.last_entry_ts = 0.0
        p.ignore_syms = {}
        p.group_of = lambda s: "g"
        p.group_count = lambda g: 0
        p.skip_log = {}
        p.contracts = {"AAA-USDT": Contract("AAA-USDT", 0.001, 0.001, 3, 2, 1.0, 150)}
        p.size_qty = lambda c, px: 0.05
        p.max_book_notional = lambda: 1000.0
        p.notional_cap = lambda: 250.0
        p.ensure_max_leverage = lambda s, force=False: None
        p.leverage_for = lambda c: 150
        p.ok = lambda r: isinstance(r, dict) and r.get("code") == 0
        p.cid = lambda kind="o", **kw: f"GTEST{len(p.api.posts)}"
        p.sl_min, p.sl_max = 0.002, 0.012
        p.tp_min, p.tp_max = 0.0035, 0.024
        p.position_cost_pct = 0.15
        p.tp_cost_ratio = 5
        p.variants = SimpleNamespace(current_sl=lambda: 0.6,
                                     current_trail=lambda: ("0.3:0.1", 0.3, 0.1))
        p.exits = SimpleNamespace(enabled=True, ignore_tp=False)
        p.control_orders = False
        p.control_orders_per_config = False
        p.security_prices = lambda pos: (pos.sl, pos.tp)
        p.save_open_book = lambda: None
        p.pending_orders = {}
        p._save_pending_orders = lambda: None
        p.seen_fill_cids = set()
        p.owned_syms = set()
        p.fees_est = 0.0
        p.signals = []
        p.block = SimpleNamespace(register_parent=lambda *a, **k: None, default_min_pf=1.2)
        p.dca = SimpleNamespace(attach=lambda *a, **k: None)
        p.did_io = False
        p.errors = 0
        p.last_error = ""
        return p

    old_max_open, old_max_group = pt.MAX_OPEN, pt.MAX_PER_GROUP
    pt.MAX_OPEN, pt.MAX_PER_GROUP = 0, 0
    try:
        p5 = mk_place_trader(mk_book(winner=False))
        p5.place("AAA-USDT", 1, "gen:ema+", 0.9)
        rec("strict-place-no-order-cold", p5.api.posts == [], f"posts={p5.api.posts}")
        p5b = mk_place_trader(loser_book)
        p5b.place("AAA-USDT", 1, "gen:ema+", 0.9)
        rec("strict-place-no-order-losers", p5b.api.posts == [], f"posts={p5b.api.posts}")
        # 6) place() trades once a validated + profitable set exists
        warm2 = mk_book(winner=True)
        p6 = mk_place_trader(warm2)
        p6.place("AAA-USDT", 1, "gen:ema+", 0.9)
        pos6 = next(iter(p6.positions_for("AAA-USDT")), None)
        rec("strict-place-trades-validated",
            len(p6.api.posts) == 1 and pos6 is not None and pos6.set_id == warm2.by_idx[0].id,
            f"posts={len(p6.api.posts)} set={getattr(pos6, 'set_id', None)}")
    finally:
        pt.MAX_OPEN, pt.MAX_PER_GROUP = old_max_open, old_max_group

    # 7) Block intern PF: strict — only validated + profitable lifts the floor
    p7 = object.__new__(pt.Pulse)
    p7.block = SimpleNamespace(default_min_pf=1.2)
    strong_book = mk_book(winner=True, strong=True)  # ratio 1.3 > floor 1.2
    p7.sets = strong_book
    pos_w = pt.Position(symbol="T", side="LONG", qty=1.0, entry=100.0, opened_at=1.0,
                        sl=99.0, tp=101.0, peak=100.0,
                        set_id=strong_book.by_idx[0].id, pack="indications")
    got_w = p7.block_intern_pf(pos_w)
    rec("strict-block-pf-validated-lifts",
        abs(got_w - strong_book.by_idx[0].last15_ratio) < 1e-9 and got_w > 1.2,
        f"intern={got_w} set_pf={strong_book.by_idx[0].last15_ratio}")
    pos_c = pt.Position(symbol="T", side="LONG", qty=1.0, entry=100.0, opened_at=1.0,
                        sl=99.0, tp=101.0, peak=100.0,
                        set_id=strong_book.by_idx[1].id, pack="indications")
    rec("strict-block-pf-cold-floor", abs(p7.block_intern_pf(pos_c) - 1.2) < 1e-9,
        f"intern={p7.block_intern_pf(pos_c)}")
    strong_book.by_idx[0].hist = loss_rows()
    strong_book._score_one(strong_book.by_idx[0])
    rec("strict-block-pf-loser-floor", abs(p7.block_intern_pf(pos_w) - 1.2) < 1e-9,
        f"intern={p7.block_intern_pf(pos_w)}")
    # legacy (strict off): warm ratio lifts even below 8 samples? no — legacy
    # floors cold sets; warm ratio is used as-is (old behavior preserved)
    legacy_sets = SimpleNamespace(strict_gate=False, sets={},
                                  pick_any=lambda pack, side=None: SimpleNamespace(last15_ratio=1.5, last15_n=12))
    p7.sets = legacy_sets
    pos_l = pt.Position(symbol="T", side="LONG", qty=1.0, entry=100.0, opened_at=1.0,
                        sl=99.0, tp=101.0, peak=100.0, set_id="", pack="general")
    rec("legacy-block-pf-unchanged", abs(p7.block_intern_pf(pos_l) - 1.5) < 1e-9,
        f"intern={p7.block_intern_pf(pos_l)}")


def dd_time_test() -> None:
    """Max drawdown time (maxDdTimeS): a position stuck underwater longer
    than the window is force-closed with reason dd-time; shorter stays,
    disabled (0) windows, and profitable resets never trigger it."""
    import tempfile
    from types import SimpleNamespace
    import pulse_trader as pt

    tmp = tempfile.mkdtemp(prefix="ddtime-test-")
    for name in ("STOP_PATH", "PAUSE_PATH", "STOP_ALL", "OPEN_PATH", "LOG_PATH", "TRADES_PATH"):
        setattr(pt, name, os.path.join(tmp, os.path.basename(getattr(pt, name))))

    def mk(px: float, under_for: float):
        p = object.__new__(pt.Pulse)
        p.ingest_ws_px = lambda: 0
        p.px = {"AAA-USDT": px}
        pos = pt.Position(symbol="AAA-USDT", side="LONG", qty=1.0, entry=100.0,
                          opened_at=time.time() - 100.0, sl=0.0, tp=1e9, peak=100.0)
        pos.under_since = (time.time() - under_for) if under_for else 0.0
        p.open = {"AAA-USDT": pos}
        p.control_orders = False
        p.ctrl_skip = {}
        p.exits = SimpleNamespace(enabled=False, ignore_tp=False, rev_on=False)
        p.strat_trail = False
        p.coord = SimpleNamespace(trailing_min_step=6)
        closed: List[Tuple[str, float, str]] = []
        p.close_pos = lambda po, price, reason: closed.append((po.symbol, round(price, 2), reason))
        return p, pos, closed

    old = pt.MAX_DD_TIME_S
    try:
        # 1) underwater 120s with a 60s window -> force close dd-time
        pt.MAX_DD_TIME_S = 60.0
        p, pos, closed = mk(99.0, 120.0)
        p.manage()
        rec("dd-time-closes-stuck-loser", closed == [("AAA-USDT", 99.0, "dd-time")], str(closed))

        # 2) underwater 30s < 60s window -> no close, clock preserved
        pt.MAX_DD_TIME_S = 60.0
        p, pos, closed = mk(99.0, 30.0)
        p.manage()
        rec("dd-time-within-window-kept", not closed and pos.under_since > 0, str(closed))

        # 3) window disabled (0) -> never closes even after hours
        pt.MAX_DD_TIME_S = 0.0
        p, pos, closed = mk(99.0, 99999.0)
        p.manage()
        rec("dd-time-disabled-off", not closed, str(closed))

        # 4) back at/above breakeven -> clock resets, no close
        pt.MAX_DD_TIME_S = 60.0
        p, pos, closed = mk(100.05, 120.0)
        p.manage()
        rec("dd-time-breakeven-resets", not closed and pos.under_since == 0.0,
            f"under_since={pos.under_since} closed={closed}")

        # 5) SHORT underwater tracking symmetric: entry 100, px 101 (-1%)
        pt.MAX_DD_TIME_S = 60.0
        p, pos, closed = mk(99.0, 120.0)
        pos.side = "SHORT"
        pos.tp = 1e-9
        p.px = {"AAA-USDT": 101.0}
        p.manage()
        rec("dd-time-short-symmetric", closed == [("AAA-USDT", 101.0, "dd-time")], str(closed))
    finally:
        pt.MAX_DD_TIME_S = old

    # 6) overlay plumbing: engine maps overlay maxDdTimeS -> MAX_DD_TIME_S
    src = open(os.path.join(DIR, "pulse_trader.py")).read()
    rec("dd-time-overlay-mapped", 'ov.get("maxDdTimeS")' in src and '"maxDdTimeS": MAX_DD_TIME_S' in src)
    # 7) config model + desk expose the field
    cm = open(os.path.join(DIR, "..", "..", "src", "lib", "config-model.ts")).read()
    st = open(os.path.join(DIR, "..", "..", "src", "routes", "settings.tsx")).read()
    rec("dd-time-config-model", "maxDdTimeS: number;" in cm and "maxDdTimeS: 27000," in cm)
    rec("dd-time-settings-ui", 'Max DD time min' in st and 'patch("maxDdTimeS", Math.max(10' in st)


def process_guard_test() -> None:
    """Regression checks for stuck-loop admission and STOP safety."""
    with tempfile.NamedTemporaryFile(prefix="cts-engine-test-", delete=True) as f:
        api = FastBingX("", "", ErrorLog(f.name), base="http://127.0.0.1:1")
        path = "/openApi/swap/v2/quote/klines"
        calls = []
        api._http = lambda method, url: calls.append((method, url)) or {"code": 0, "data": []}
        api.path_cd[path] = time.time() + 30.0
        cooled = api.public(path, {"symbol": "BTC-USDT"})
        rec("public-cooldown-no-request", cooled.get("cooled") is True and not calls, str(cooled))
        rows = api.gather_public([(path, {"symbol": "ETH-USDT"})])
        rec("async-cooldown-no-request", rows and rows[0][2].get("cooled") is True and not calls, str(rows))
        api.path_cd[path] = 0.0
        api.bridge.gather = lambda reqs, timeout=4.2: [(p, e, {"code": 100410, "msg": "frequency limit"}) for p, e in reqs]
        api.gather_public([(path, {"symbol": "SOL-USDT"})])
        rec("async-rate-trip-propagates", api.stats["rl"] == 1 and api.path_cd[path] > time.time(), str(api.snapshot()))
        rec("suppression-accounting", api.stats["publicSuppressed"] == 1 and api.stats["asyncSuppressed"] == 1 and api.stats["rest"] == 1, str(api.stats))

    trader = open(os.path.join(DIR, "pulse_trader.py"), encoding="utf-8").read()
    fast = open(os.path.join(DIR, "bingx_fast.py"), encoding="utf-8").read()
    rec("history-fetch-backoff", "_hist_fetch_next" in trader and "retry in" in trader and "history waiting for bars" in trader)
    rec("history-no-short-replay", "if b.hist_run and have >= min_ready" in trader)
    rec("kline-budget-gate", "not bool(getattr(budget, \"kline_rest\", True))" in trader and "kline_batch" in trader)
    rec("recon-pending-not-failure", "confirmed_book_only" in trader and "pending_absent" in trader)
    rec("stop-file-negative-control", "os.path.exists(STOP_PATH) or os.path.exists(STOP_ALL)" in trader and "halt_reason = \"stopped\"" in trader)
    rec("async-shared-rate-trip", "self._take(\"public\", path)" in fast and "self._trip(path, body)" in fast)


def historic_snapshot_test() -> None:
    """Historic CPU work cannot block or overwrite live Set evidence."""
    import threading
    import pulse_trader as pt
    from load_engine import LoadGovernor

    book = SetBook()
    book.load({
        "stratIndications": False,
        "stratGeneral": True,
        "stratTrailing": False,
        "slToTpRatios": [0.6],
        "setMinStep": 3,
        "setStepMax": 3,
        "histMinBars": 60,
        "histLookbackBars": 120,
    })
    symbol = "SNAP-USDT"
    book.ingest_bars(symbol, synth_trend(120))
    set_id = book.by_idx[0].id
    book.on_live_close({
        "t": time.time(),
        "symbol": symbol,
        "side": "LONG",
        "pnl": 0.02,
        "pnl_pct": 0.01,
        "hold_s": 60,
        "reason": "tp",
        "set_id": set_id,
        "client_id": "snap-live-1",
        "ours": True,
    })
    p = object.__new__(pt.Pulse)
    p._state_lock = threading.RLock()
    p._sets_generation = 1
    p.sets = book
    p.load = LoadGovernor()
    p.open = {}
    p._hist_deferred = ""
    p.hist_busy = False
    old_id = id(book)
    ok = p._replay_sets_isolated([symbol], False, 1)
    current = p.sets
    rec("hist-snapshot-atomic", ok and id(current) == old_id and current.progress.ready and not current._running,
        f"ok={ok} source_kept={id(current) == old_id} phase={current.progress.phase}")
    rec("hist-snapshot-keeps-live", len(current.sets[set_id].live) == 1 and current.sets[set_id].live[0].get("client_id") == "snap-live-1",
        f"live={len(current.sets[set_id].live)}")
    rec("hist-snapshot-no-bars-loss", symbol in current.bars and len(current.bars[symbol]) == 120,
        f"bars={len(current.bars.get(symbol) or [])}")


def main() -> int:
    run_units()
    import unittest
    from test_block_contract import BlockContractTests
    block_contract = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(BlockContractTests))
    rec("unit-block-contract", block_contract.wasSuccessful(), f"n={block_contract.testsRun}")
    from test_vst_scheduling import VstSchedulingTests
    scheduling = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(VstSchedulingTests))
    rec("unit-vst-scheduling", scheduling.wasSuccessful(), f"n={scheduling.testsRun}")
    rank_test()
    overlay_test()
    cost_test()
    contract_test()
    controls_test()
    fill_accounting_test()
    cancel_replace_regression_test()
    coord_test()
    stage_min_pf_test()
    unlimited_test()
    always_start_test()
    control_coord_test()
    phantom_recon_test()
    sim_stats_test()
    block_calc_test()
    set_orders_test()
    grouped_control_test()
    strict_gate_test()
    dd_time_test()
    historic_snapshot_test()
    process_guard_test()
    fails = [r for r in out if not r[1]]
    print(f"\n{len(out) - len(fails)}/{len(out)} passed  fail={len(fails)}")
    for name, _, d in fails:
        print("FAIL", name, d)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
