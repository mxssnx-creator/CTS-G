#!/usr/bin/env python3
"""Read saved public contracts through the native parser; size without a trader/API.

This is an offline feasibility diagnostic, never an account quote or order.
"""
from __future__ import annotations
import argparse
import io
import json
import math
from pathlib import Path
import sys
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / ".." / "server" / "pulse"))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", required=True)
    ap.add_argument("--contracts", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--equity", type=float, default=3)
    ap.add_argument("--volume-factor", type=float, default=.05)
    ap.add_argument("--size-percent", type=float, default=5)
    args = ap.parse_args()
    if any(not math.isfinite(v) or v <= 0 for v in [args.equity, args.volume_factor, args.size_percent]):
        ap.error("Sizing inputs must be finite and positive")
    def no_network(event, values):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise RuntimeError("Sizing audit cannot contact a network")
    sys.addaudithook(no_network)
    import pulse_trader as pt
    market = Path(args.market)
    meta = json.loads((market / "_meta.json").read_text())
    source = json.loads(Path(args.contracts).read_text())
    symbols = set(meta["symbols"])
    # Use production parsing, but satisfy its public HTTP read from immutable saved bytes.
    payload = json.dumps({"data": list(source["contracts"].values())}).encode()
    with patch.object(pt.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(payload)):
        contracts = pt.load_contracts(symbols)
    assert set(contracts) == symbols
    pulse = pt.Pulse.__new__(pt.Pulse)  # No constructor, credentials, client, state, or scheduler.
    pulse.equity = pulse.available = args.equity
    pulse.volume_factor = pt.volume_factor_of(args.volume_factor)
    pulse.size_pct, pulse.size_max = args.size_percent, 25.0
    pulse.leverage_for = lambda c: max(1, c.max_lev)
    pulse.strict_size_cap = True
    strict_budget = pulse.strict_entry_budget()
    rows = []
    for symbol in sorted(symbols):
        c = contracts[symbol]
        price = float(json.loads((market / (symbol + ".json")).read_text())[-1][4])
        assert math.isfinite(price) and price > 0
        minimum = pulse.min_order_qty(c, price)
        pulse.strict_size_cap = True
        strict_qty = pulse.size_qty(c, price)
        pulse.strict_size_cap = False
        legacy_qty = pulse.size_qty(c, price)
        rows.append({"symbol": symbol, "price": price, "quantityStep": c.step,
            "minimumQuantity": minimum, "minimumNotional": minimum * price,
            "strictQuantity": strict_qty, "strictNotional": strict_qty * price,
            "legacyQuantity": legacy_qty, "legacyNotional": legacy_qty * price,
            "minimumToRequestedRatio": minimum * price / strict_budget})
    out = {"mode": "offline-only", "accountAuthenticated": False, "ordersSubmitted": 0,
        "balanceAssumptionUSDT": args.equity, "volumeFactor": pulse.volume_factor,
        "sizePercent": args.size_percent, "requestedNotional": strict_budget,
        "nativeSizingFloor": pulse.sized_notional(), "equityGuardMinimum": pt.EQ_MIN,
        "priceTime": meta["end_ms"] / 1000, "contractQuoteTime": source["fetchedAt"],
        "symbols": len(rows), "strictSizePass": sum(r["strictQuantity"] > 0 for r in rows),
        "legacySizePass": sum(r["legacyQuantity"] > 0 for r in rows),
        "minimumNotionalRange": [min(r["minimumNotional"] for r in rows), max(r["minimumNotional"] for r in rows)],
        "scope": "Native size_qty only; other portfolio, strategy, margin, account and venue gates may reject. Public contract maximum leverage is used only for this arithmetic comparison, never sent to a venue.",
        "rows": rows}
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}))


if __name__ == "__main__":
    main()
