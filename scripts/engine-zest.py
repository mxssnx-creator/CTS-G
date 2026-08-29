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
from position_cost import last_n_cost_pf, ratio_from_r, resolve_sl_tp, net_pnl_pct
from pulse_trader import (
    coerce_symbol_sort,
    symbol_metric,
    symbol_rank_key,
    rank_self_test,
    Contract,
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
    rec("isolation-lists", x01.get("symbols") != x02.get("symbols") or x01.get("symbolsAll") != x02.get("symbolsAll"))
    rec("x01-capped", int(x01.get("symbolCap") or 0) == 26 and x01.get("symbolsAll") is False)
    rec("x02-all", x02.get("symbolsAll") is True and int(x02.get("symbolCap") or 0) == 0)
    rec("x01-not-x02-symbols", "SOL-USDT" in (x01.get("symbols") or []) and (x02.get("symbols") == ["*"] or x02.get("symbolsAll")))


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


def main() -> int:
    run_units()
    rank_zest()
    overlay_zest()
    cost_zest()
    contract_zest()
    fails = [r for r in out if not r[1]]
    print(f"\n{len(out) - len(fails)}/{len(out)} passed  fail={len(fails)}")
    for name, _, d in fails:
        print("FAIL", name, d)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
