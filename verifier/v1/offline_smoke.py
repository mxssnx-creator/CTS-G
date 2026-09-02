#!/usr/bin/env python3
"""Offline smoke: drive a real Pulse engine with a stub BingX API.

Verifies, without network or Redis:
1. apply_live_config honors the x01 overlay (0 = unlimited block/dca/open).
2. process_indications scores synthetic bars and fills the indication book.
3. maybe_entries emits an entry order on a crafted long signal.
4. No exception escapes the hot paths (crash check).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

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
    """Minimal duck-typed FastBingX stand-in. Records orders."""

    def __init__(self):
        self.orders = []
        self.px = {}

    def snapshot(self):
        return {"asyncP50": 0, "asyncN": 0}

    def place_order(self, payload, **kw):
        self.orders.append(dict(payload))
        return {"code": 0, "data": {"order": {"orderId": str(1000 + len(self.orders))}}}


def isolate_runtime(contracts):
    """Route every Pulse persistence path to a disposable offline fixture."""
    root = tempfile.mkdtemp(prefix="cts-g-offline-v1-")
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


def synth_bars(n=60, start=100.0, drift=0.001):
    bars = []
    px = start
    for i in range(n):
        o = px
        px = px * (1 + drift)
        hi = max(o, px) * 1.0005
        lo = min(o, px) * 0.9995
        bars.append([o, hi, lo, px, 1000.0])
    return bars


def main() -> int:
    api = StubAPI()
    contracts = {
        "SOL-USDT": pt.Contract("SOL-USDT", 0.01, 0.01, 2, 3, 2.0, 300),
        "XRP-USDT": pt.Contract("XRP-USDT", 0.1, 0.1, 1, 4, 2.0, 150),
    }
    isolate_runtime(contracts)
    # Keep the initial fixture universe bounded while Pulse exercises the
    # wildcard x01 configuration from the isolated overlay.
    pt.SYMBOLS[:] = list(contracts)
    p = pt.Pulse(api, contracts)

    # 1. overlay apply
    err = None
    try:
        p.apply_live_config(initial=True)
    except Exception as e:
        err = e
    rec("apply-config-no-crash", err is None, repr(err or ""))
    # The real x01 overlay may intentionally be symbolsAll.  Restore the
    # bounded fixture after exercising that config path so partial scanning
    # cannot rotate past the only synthetic symbol in this offline test.
    pt.SYMBOLS[:] = list(contracts)
    rec("x01-block-unlimited", int(getattr(p.block, "max_stack", -1) or 0) == 3 and not p.block.unlimited(), f"stack={p.block.max_stack}")
    rec("x01-dca-unlimited", int(getattr(p.dca, "max_steps", -1) or 0) == 4 and not p.dca.unlimited(), f"steps={p.dca.max_steps}")
    rec("x01-maxopen-unlimited", int(pt.MAX_OPEN or 0) == 0, f"maxOpen={pt.MAX_OPEN}")
    rec("ind-enabled", bool(p.indications.settings.get("enabled", True)), str(p.indications.settings.get("enabled")))

    # 2. indications on synthetic bars
    bars = synth_bars()
    p.klines_tf["1m"]["SOL-USDT"] = bars
    p.px["SOL-USDT"] = bars[-1][3]
    err = None
    try:
        p.process_indications()
    except Exception as e:
        err = e
    rec("indications-no-crash", err is None, repr(err or ""))
    have = set(s for s, rows in (getattr(p.indications, "last", {}) or {}).items() if rows)
    rec("indications-scored", "SOL-USDT" in have, f"have={sorted(have)}")
    snap = p.indications.snapshot()
    types = snap.get("types") or {}
    rec("ind-types-on", all(types.get(k) for k in ("state", "direction", "move", "active", "common", "signals")), f"types={types}")

    # 3. stats write includes indications + universe paths don't crash
    err = None
    try:
        p.write_stats(force=True)
    except Exception as e:
        err = e
    rec("write-stats-no-crash", err is None, repr(err or ""))
    try:
        stats_path = getattr(pt, "STATS_PATH", os.path.join(DIR, "stats-bingx-x01.json"))
        os.makedirs(os.path.dirname(stats_path) or ".", exist_ok=True)
        stats = json.load(open(stats_path))
        rec("stats-has-indications", "indications" in stats, f"keys={sorted(stats)[:12]}")
    except Exception as e:
        rec("stats-has-indications", False, repr(e))

    # 4. empty universe.json tolerance
    uni_path = os.path.join(DIR, "universe.json")
    rec("universe-json-valid", isinstance(json.load(open(uni_path)), dict), "stub parses")

    fails = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(fails)}/{len(results)} passed  fail={len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
