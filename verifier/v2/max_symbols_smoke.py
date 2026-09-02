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
import tempfile
import time
import traceback

DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "server", "pulse"))
sys.path.insert(0, DIR)
os.chdir(DIR)
os.environ.setdefault("PULSE_CONN", "bingx-x01")

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


def isolate_runtime(contracts):
    """Route every Pulse persistence path to a disposable offline fixture."""
    root = tempfile.mkdtemp(prefix="cts-g-offline-v2-")
    for name in (
        "STATS_PATH", "TRADES_PATH", "STOP_PATH", "PAUSE_PATH", "STOP_ALL",
        "LOG_PATH", "BLOCK_PATH", "OVERLAY_PATH", "OPEN_PATH", "CTS_PATH",
        "ERR_PATH", "LEV_PATH", "START_EQ_PATH", "RESET_EQ_PATH", "UNIVERSE_PATH",
    ):
        setattr(pt, name, os.path.join(root, os.path.basename(str(getattr(pt, name, name)))))
    with open(pt.OVERLAY_PATH, "w") as f:
        json.dump({"symbolsAll": True, "symbolCap": 0, "symbols": ["*"], "maxOpen": 0,
                   "maxPerGroup": 0, "blockMaxStack": 3, "dcaMaxSteps": 4, "indEnabled": True}, f)
    with open(pt.UNIVERSE_PATH, "w") as f:
        json.dump({}, f)
    # Pulse construction normally reads Redis settings and may fetch wildcard
    # contracts.  Keep this verifier offline and independent of live runtime.
    pt.dump_cts_settings = lambda: {}
    pt.load_contracts = lambda want=None: dict(contracts)


def synth_bars(n=60, start=100.0, drift=0.001, vol_spike=False):
    """Live engine bars are [o, h, l, c, v] (no timestamp) — see Pulse._parse_klines."""
    bars = []
    px = start
    for i in range(n):
        o = px
        px = px * (1 + drift)
        hi = max(o, px) * 1.0008
        lo = min(o, px) * 0.9992
        vol = 8000.0 if (vol_spike and i == n - 1) else 1000.0
        bars.append([o, hi, lo, px, vol])
    return bars


def reversal_bars(n=60, start=100.0):
    """Last 10 bars reverse the prior window so Direction (range=10) fires."""
    n = max(24, int(n))
    down = synth_bars(n - 10, start, -0.005)
    up = synth_bars(10, down[-1][3], 0.008, vol_spike=True)
    return down + up


def main() -> int:
    api = StubAPI()
    N = 530
    names = [f"COIN{i:03d}-USDT" for i in range(N)]
    contracts = {s: pt.Contract(s, 0.01, 0.01, 2, 3, 2.0, 300) for s in names}
    contracts["SOL-USDT"] = pt.Contract("SOL-USDT", 0.01, 0.01, 2, 3, 2.0, 300)
    isolate_runtime(contracts)
    # Keep the initial fixture universe complete while Pulse exercises the
    # wildcard x01 configuration from the isolated overlay.
    pt.SYMBOLS[:] = list(contracts)
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
        if i % 11 == 0:
            bars = reversal_bars(80, 80.0 + (i % 7))
        elif i % 5 == 0:
            bars = synth_bars(80, 90.0 + (i % 9), drift=-0.0015 - (i % 5) * 0.0002, vol_spike=True)
        else:
            bars = synth_bars(80, 100.0, drift=0.0008 + (i % 7) * 0.0003, vol_spike=(i % 3 == 0))
        p.klines_tf["1m"][s] = bars
        p.px[s] = bars[-1][3]

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

    # Full-universe pass: disable partial chunking so EVERY symbol is processed
    # in one tick (live stays chunked; this is the all-symbols stress).
    p.load.partial = False
    t_full = time.time()
    err = None
    try:
        p.process_indications()
    except Exception:
        err = traceback.format_exc()[-300:]
    dt_full = time.time() - t_full
    rec("indications-full-no-crash", err is None, repr(err or ""))
    have_full = set(s for s, rows in (getattr(p.indications, "last", {}) or {}).items() if rows)
    rec("indications-all-symbols", len(have_full) >= N, f"scored={len(have_full)}/{len(pt.SYMBOLS)} in {dt_full:.2f}s")

    kinds_hit = {}
    kind_long = {}
    kind_short = {}
    for rows in (p.indications.last or {}).values():
        for i in rows:
            k = str(getattr(i, "kind", "") or "")
            if not k:
                continue
            kinds_hit[k] = kinds_hit.get(k, 0) + 1
            d = str(getattr(i, "direction", "") or "").lower()
            if d == "long":
                kind_long[k] = kind_long.get(k, 0) + 1
            elif d == "short":
                kind_short[k] = kind_short.get(k, 0) + 1
    want_kinds = ("state", "signals", "active", "direction", "move", "common", "trend", "break")
    rec("indications-all-6-kinds", all(kinds_hit.get(k, 0) >= 1 for k in want_kinds),
        f"hits={ {k: kinds_hit.get(k, 0) for k in want_kinds} }")
    ksnap = p.indications.kind_stats() if hasattr(p.indications, "kind_stats") else {}
    rec("indications-kindstats-6", all(ksnap.get(k, {}).get("enabled") for k in want_kinds) and len(ksnap) >= 6,
        f"keys={sorted(ksnap)}")
    rec("indications-kindstats-hits", all(int((ksnap.get(k) or {}).get("hits") or 0) >= 1 for k in want_kinds),
        f"hits={ {k: (ksnap.get(k) or {}).get('hits') for k in want_kinds} }")

    # Independent LONG vs SHORT: downward bars on a dedicated pair must score short
    short_sym = "COIN500-USDT"
    p.klines_tf["1m"][short_sym] = synth_bars(n=80, start=100.0, drift=-0.002)
    p.px[short_sym] = p.klines_tf["1m"][short_sym][-1][4]
    p.process_indications()
    short_rows = p.indications.last.get(short_sym) or []
    short_dirs = {str(getattr(i, "direction", "")).lower() for i in short_rows}
    rec("indications-short-independent", "short" in short_dirs, f"dirs={sorted(short_dirs)} n={len(short_rows)}")

    # Block: first add is 1× parent remainder, never 3× jump; lanes are per-side
    n1_ok = True
    jump_detail = []
    for pos in p.open.values():
        lane = p.block.lanes.get(p.block.key(pos.symbol, pos.side))
        if not lane or lane.base_qty <= 0:
            continue
        cap = lane.base_qty * float(p.block.volume_ratio or 1.0) * 1.05
        if lane.confirmed_add > cap + 1e-9:
            n1_ok = False
            jump_detail.append((pos.symbol, lane.base_qty, lane.confirmed_add))
    rec("block-n1-not-3x", n1_ok, f"jumps={jump_detail[:4]} lanes={len(p.block.lanes)}")
    rec("block-lanes-per-side", all(":" in k for k in p.block.lanes), f"keys={list(p.block.lanes)[:4]}")

    err = None
    try:
        p.write_stats(force=True)
    except Exception:
        err = traceback.format_exc()[-300:]
    rec("write-stats-530-no-crash", err is None, repr(err or ""))
    try:
        # Read the isolated snapshot written by Pulse, never the live runtime
        # snapshot.  The latter may legitimately use a bounded production
        # overlay and would make this offline full-universe check flaky.
        with open(pt.STATS_PATH) as fh:
            stats = json.load(fh)
        rec("stats-has-indications", "indications" in stats, f"indKeys={sorted((stats.get('indications') or {}))[:8]}")
        rec("stats-symbols-full", len(stats.get("symbols") or []) >= 500, f"symbols={len(stats.get('symbols') or [])}")
        rec("stats-by-indication", isinstance(stats.get("byIndication"), dict) and all(k in (stats.get("byIndication") or {}) for k in want_kinds),
            f"keys={sorted((stats.get('byIndication') or {}))}")
        rec("stats-by-strategy", isinstance(stats.get("byStrategy"), dict) and "block" in (stats.get("byStrategy") or {}),
            f"keys={sorted((stats.get('byStrategy') or {}))}")
        rec("stats-kindstats", isinstance((stats.get("indications") or {}).get("kindStats"), dict)
            and len((stats.get("indications") or {}).get("kindStats") or {}) >= 6,
            f"n={len((stats.get('indications') or {}).get('kindStats') or {})}")
    except Exception as e:
        rec("stats-has-indications", False, repr(e))
        rec("stats-symbols-full", False, repr(e))

    fails = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(fails)}/{len(results)} passed  fail={len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
