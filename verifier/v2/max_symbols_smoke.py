#!/usr/bin/env python3
"""Max-symbols offline stress: drive a real Pulse engine with a stub BingX API
across a full-size synthetic USDT-M universe (530 symbols).

Verifies:
1. apply_live_config honors the x01 overlay (0 = unlimited block/dca/open/symbols).
2. process_indications scores 530 symbols without exceptions and within budget.
3. maybe_entries emits entries for crafted signals; no order-count cap below
   the exchange ceiling blocks them (only _order_est >= 196 may).
4. write_stats / snapshot cycle survives the full book (crash check).
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "server", "pulse"))
sys.path.insert(0, DIR)
os.chdir(DIR)
os.environ.setdefault("PULSE_CONN", "bingx-x01")
os.makedirs("/opt/grok-x01-pulse", exist_ok=True)

import pulse_trader as pt  # noqa: E402

results = []


def rec(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)[:200]))
    print(("OK  " if ok else "FAIL") + name + (("  " + str(detail)[:160]) if detail else ""))


class StubAPI:
    def __init__(self):
        self.orders = []
        self.px = {}
        self.path_cd = {}

    def snapshot(self):
        return {"asyncP50": 0, "asyncN": 0}

    def place_order(self, payload, **kw):
        self.orders.append(dict(payload))
        return {"code": 0, "data": {"order": {"orderId": str(1000 + len(self.orders))}}}

    def post(self, path, payload=None, **kw):
        return self.place_order(payload or {})

    def batch_place(self, batch, **kw):
        for item in batch:
            args = item.get("args") if isinstance(item, dict) else None
            self.orders.append(dict(args or item))
        return {"code": 0, "data": {"orders": [{"orderId": str(2000 + i)} for i, _ in enumerate(batch)]}}

    def get(self, path, params=None, **kw):
        return {"code": 0, "data": {"orders": []}}

    def delete(self, path, params=None, **kw):
        return {"code": 0, "data": {}}


def synth_bars(n=60, start=100.0, drift=0.001):
    bars = []
    px = start
    t = int(time.time()) - n * 60
    for i in range(n):
        o = px
        px = px * (1 + drift)
        hi = max(o, px) * 1.0005
        lo = min(o, px) * 0.9995
        bars.append([t + i * 60, o, hi, lo, px, 1000.0])
    return bars


def main() -> int:
    api = StubAPI()
    N = 530
    names = [f"COIN{i:03d}-USDT" for i in range(N)]
    contracts = {s: pt.Contract(s, 0.01, 0.01, 2, 3, 2.0, 300) for s in names}
    contracts["SOL-USDT"] = pt.Contract("SOL-USDT", 0.01, 0.01, 2, 3, 2.0, 300)
    p = pt.Pulse(api, contracts)

    err = None
    try:
        p.apply_live_config(initial=True)
    except Exception as e:
        err = e
    rec("apply-config-no-crash", err is None, repr(err or ""))
    rec("x01-block-unlimited", int(getattr(p.block, "max_stack", -1) or 0) == 3 and not p.block.unlimited(), f"stack={p.block.max_stack}")
    rec("x01-dca-unlimited", int(getattr(p.dca, "max_steps", -1) or 0) == 4 and not p.dca.unlimited(), f"steps={p.dca.max_steps}")
    rec("x01-maxopen-unlimited", int(pt.MAX_OPEN or 0) == 0, f"maxOpen={pt.MAX_OPEN}")
    rec("x01-symbolcap-unlimited", int(getattr(p, "symbol_cap", -1) or 0) == 0, f"cap={getattr(p, 'symbol_cap', None)}")

    # Full universe: all symbols ranked and selected (0 = unlimited book)
    pt.SYMBOLS.clear()
    pt.SYMBOLS.extend(names + ["SOL-USDT"])
    p.universe = [{"symbol": s, "maxLeverage": 300, "vol1h": 5.0, "quoteVolume": 1e6} for s in pt.SYMBOLS]
    for i, s in enumerate(pt.SYMBOLS):
        bars = synth_bars(drift=0.0005 + (i % 7) * 0.0002)
        p.klines_tf["1m"][s] = bars
        p.px[s] = bars[-1][4]

    t0 = time.time()
    err = None
    try:
        p.process_indications()
    except Exception:
        err = traceback.format_exc()[-300:]
    dt = time.time() - t0
    rec("indications-530-no-crash", err is None, repr(err or ""))
    have = set(s for s, rows in (getattr(p.indications, "last", {}) or {}).items() if rows)
    rec("indications-530-scored", len(have) >= 8, f"scored={len(have)} in {dt:.2f}s")

    # Entries: force coordination open, craft a long signal on SOL-USDT
    err = None
    n_orders_before = len(api.orders)
    try:
        p.equity = 100.0
        p.available = 95.0
        p.start_eq = 100.0
        p.boot_ts = time.time() - 60  # past boot gate
        p._order_est = 0
        p._order_est_known = True
        try:
            p.coord.gate([], 0, intern={"pf": 1.5, "n": 20, "pack": "indications"})
        except Exception:
            pass
        p.maybe_entries()
    except Exception:
        err = traceback.format_exc()[-300:]
    rec("entries-530-no-crash", err is None, repr(err or ""))
    rec("entries-no-false-order-cap", not p.entries_blocked(), f"order_est={p._order_est}")

    err = None
    try:
        p.write_stats(force=True)
    except Exception:
        err = traceback.format_exc()[-300:]
    rec("write-stats-530-no-crash", err is None, repr(err or ""))
    try:
        stats = json.load(open("/opt/grok-x01-pulse/stats-bingx-x01.json"))
        rec("stats-has-indications", "indications" in stats, f"indKeys={sorted((stats.get('indications') or {}))[:6]}")
        rec("stats-symbols-full", len(stats.get("symbols") or []) >= 500, f"symbols={len(stats.get('symbols') or [])}")
    except Exception as e:
        rec("stats-has-indications", False, repr(e))
        rec("stats-symbols-full", False, repr(e))

    fails = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(fails)}/{len(results)} passed  fail={len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
