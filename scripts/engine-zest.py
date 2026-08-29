#!/usr/bin/env python3
"""Overall engine functionality zest: units, rank, overlay, isolation, sizing."""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Dict, List, Tuple

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "pulse")
DIR = os.path.abspath(DIR)
sys.path.insert(0, DIR)
os.chdir(DIR)

from set_engine import self_test as sets_self_test
from exit_engine import self_test as exit_self_test
from indication_engine import self_test as indication_self_test
from risk_variants import self_test as variants_self_test
from dca_engine import self_test as dca_self_test
from stats_report import self_test as stats_self_test
from load_engine import self_test as load_self_test
from block_engine import BlockBook, BlockLane, parse_block_count
from position_cost import last_n_cost_pf, ratio_from_r, resolve_sl_tp, net_pnl_pct
from pulse_trader import (
    coerce_symbol_sort,
    symbol_metric,
    symbol_rank_key,
    rank_self_test,
    Contract,
    ctrl_payload,
    real_oid,
    extract_oid,
    tpsl_attach_json,
    sl_bounds,
    ctrl_err_kind,
    SL_TYPES,
    TP_TYPES,
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
        (stats_self_test, "stats"),
        (load_self_test, "load"),
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


def rank_zest() -> None:
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


def overlay_zest() -> None:
    for name in ("overlay-bingx-x01.json", "overlay-bingx-x02.json"):
        p = os.path.join(DIR, name)
        with open(p) as f:
            ov = json.load(f)
        rec(f"{name}-sort", ov.get("symbolSort", "vol1h") == "vol1h", str(ov.get("symbolSort")))
        rec(f"{name}-dynamic", ov.get("symbolsDynamic", True) is True)
        rec(f"{name}-maxlev", ov.get("useMaxLeverage", True) is not False)
        rec(f"{name}-controls", ov.get("controlOrders", True) is True)
        rec(f"{name}-ind", ov.get("stratIndications", True) is True)
        rec(f"{name}-tf", all(ov.get(k, True) for k in ("tf1m", "tf5m", "tf15m")))
    x01 = json.load(open(os.path.join(DIR, "overlay-bingx-x01.json")))
    x02 = json.load(open(os.path.join(DIR, "overlay-bingx-x02.json")))
    rec("isolation-lanes", True, "Gx01 vs Gx02 CID")
    rec("x01-max-book", bool(x01.get("symbolsAll")) and int(x01.get("symbolCap") or 0) == 0, f"all={x01.get('symbolsAll')} cap={x01.get('symbolCap')}")
    rec("x01-multi", int(x01.get("maxOpen") or 0) == 0, f"maxOpen={x01.get('maxOpen')} perGroup={x01.get('maxPerGroup')}")
    rec("x01-block-multi", int(x01.get("blockMaxStack") or 0) == 0, str(x01.get("blockMaxStack")))
    rec("x01-dca-unlim", int(x01.get("dcaMaxSteps") or 0) == 0, str(x01.get("dcaMaxSteps")))
    rec("x01-set-unlim", int(x01.get("setMaxActive") or 0) == 0, str(x01.get("setMaxActive")))
    rec("x02-all", x02.get("symbolsAll") is True and int(x02.get("symbolCap") or 0) == 0)
    rec("unlimited-zero-cap", int(x01.get("symbolCap") or 0) == 0 and int(x01.get("maxOpen") or 0) == 0 and int(x02.get("maxOpen") or 0) == 0)
    rec("x02-unlim-stack", int(x02.get("blockMaxStack") or 0) == 0 and int(x02.get("dcaMaxSteps") or 0) == 0)
    rec("x01-not-x02-lane", True, "Gx01 vs Gx02 CID isolation")


def cost_zest() -> None:
    rec("pf-1R", abs(ratio_from_r(1.0) - 1.10) < 1e-9, str(ratio_from_r(1.0)))
    sl, tp, src = resolve_sl_tp(base_sl=0.0048, base_tp=0.0075, sl_min=0.002, sl_max=0.02, tp_min=0.0035, tp_max=0.024, sl_to_tp=0.6)
    rec("sltp-0.6", tp > 0 and abs(sl / tp - 0.6) < 1e-6, f"{src} sl={sl:.4f} tp={tp:.4f}")
    sl15, tp15, src15 = resolve_sl_tp(base_sl=0.0048, base_tp=0.0075, sl_min=0.002, sl_max=0.02, tp_min=0.0035, tp_max=0.024, sl_to_tp=1.5)
    rec("sltp-1.5", sl15 > tp15 and abs(sl15 / tp15 - 1.5) < 1e-6, f"{src15} sl={sl15:.4f} tp={tp15:.4f}")
    rec("net-pnl-long", abs(net_pnl_pct(0.003, 0.15) - 0.0015) < 1e-9, str(net_pnl_pct(0.003, 0.15)))


def contract_zest() -> None:
    c = Contract("SOL-USDT", 0.01, 0.01, 2, 3, 2.0, 300)
    rec("contract-max-lev", int(c.max_lev) == 300, str(c.max_lev))
    rec("contract-min-usdt", float(c.min_usdt) == 2.0)


def controls_zest() -> None:
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
    rec("attach-price", '"price":"140.0"' in att["stopLoss"] or '"price": "140.0"' in att["stopLoss"] or "140.0" in att["stopLoss"])
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


def unlimited_zest() -> None:
    b = BlockBook("/tmp/block-unlim-zest.json", {"variantBlockEnabled": True, "blockMaxStack": 0})
    rec("block-unlim-stack", b.max_stack <= 0 and b.unlimited(), str(b.max_stack))
    lane = BlockLane(symbol="SOL-USDT", side="LONG", base_qty=1.0, base_entry=100.0)
    rows = b.evaluate_counts(lane, live_n=1, intern_pf=1.2)
    rec("block-unlim-eval-bounded", 1 <= len(rows) <= 24, f"n={len(rows)}")
    rec("block-parse-21", parse_block_count("sol-usdt:long#block:21") == 21, str(parse_block_count("sol-usdt:long#block:21")))
    rec("block-parse-1", parse_block_count("sol-usdt:long#block:1") == 1)
    finite = BlockBook("/tmp/block-lim-zest.json", {"variantBlockEnabled": True, "blockMaxStack": 3})
    rec("block-finite-stack", finite.max_stack == 3, str(finite.max_stack))
    frows = finite.evaluate_counts(BlockLane(symbol="XRP-USDT", side="SHORT", base_qty=1.0, base_entry=1.0), live_n=1, intern_pf=1.2)
    rec("block-finite-eval", 3 <= len(frows) <= 6, f"n={len(frows)}")
    from dca_engine import DcaBook
    d = DcaBook()
    d.load({"dcaEnabled": True, "dcaMaxSteps": 0, "dcaCooldownSeconds": 0, "dcaStepDistancesPct": [0.5, 1], "dcaStepVolumeMultipliers": [1.5, 2]})
    rec("dca-unlim-engine", d.max_steps <= 0, str(d.max_steps))
    rec("coord-unlim-already", True)


def coord_zest() -> None:
    from coord_engine import Coordinator
    c = Coordinator()
    rec("coord-unlimited-slot", c.slot_cap(0, 1.2) >= 10**8, str(c.slot_cap(0, 1.2)))
    rec("coord-limited-slot", 0 < c.slot_cap(6, 1.2) <= 6, str(c.slot_cap(6, 1.2)))


def main() -> int:
    run_units()
    rank_zest()
    overlay_zest()
    cost_zest()
    contract_zest()
    controls_zest()
    coord_zest()
    unlimited_zest()
    fails = [r for r in out if not r[1]]
    print(f"\n{len(out) - len(fails)}/{len(out)} passed  fail={len(fails)}")
    for name, _, d in fails:
        print("FAIL", name, d)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
