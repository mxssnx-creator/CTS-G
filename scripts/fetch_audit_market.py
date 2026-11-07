#!/usr/bin/env python3
"""Read-only BingX market snapshot for reproducible offline engine audits."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
from pathlib import Path
import time
import urllib.parse
import urllib.request

BASE = "https://open-api.bingx.com"
SYMBOLS = "BTC,ETH,SOL,XRP,BNB,DOGE,ADA,AVAX,LINK,DOT,BCH,LTC"


def get(path, **params):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=25) as response:
                value = json.load(response)
            if int(value.get("code", -1)) != 0:
                raise ValueError(f"exchange code {value.get('code')}")
            return value["data"]
        except Exception:
            if attempt == 3:
                raise
            time.sleep(0.5 * (attempt + 1))


def history(symbol, start, end):
    found = {}
    cursor = start
    while cursor < end:
        stop = min(end, cursor + 1000 * 60000)
        rows = get("/openApi/swap/v2/quote/klines", symbol=symbol, interval="1m",
                   startTime=cursor, endTime=stop - 1, limit=1000)
        for r in rows:
            t = int(r["time"])
            if start <= t < end:
                found[t] = [t] + [float(r[k]) for k in ("open", "high", "low", "close", "volume")]
        cursor = stop
    result = [found[t] for t in sorted(found)]
    expected = list(range(start, end, 60000))
    if [r[0] for r in result] != expected:
        raise ValueError(f"{symbol}: incomplete minute series ({len(result)}/{len(expected)})")
    for r in result:
        _, o, h, l, c, v = r
        if not all(math.isfinite(x) for x in r) or min(o, h, l, c) <= 0 or v < 0 or not l <= min(o, c) <= max(o, c) <= h:
            raise ValueError(f"{symbol}: invalid OHLCV row")
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--hours", type=int, default=49)
    p.add_argument("--symbols", default=SYMBOLS)
    p.add_argument("--jobs", type=int, default=4)
    a = p.parse_args()
    if not 25 <= a.hours <= 72:
        p.error("history must be between 25 and 72 hours")
    target = Path(a.out)
    target.mkdir(parents=True, exist_ok=True)
    names = [s.strip() if s.strip().endswith("-USDT") else s.strip() + "-USDT" for s in a.symbols.split(",")]
    contracts = {r["symbol"]: r for r in get("/openApi/swap/v2/quote/contracts")}
    tickers = get("/openApi/swap/v2/quote/ticker")
    # Fixed symbols are selected before seeing forward returns. End on a complete UTC hour.
    end = int(time.time() // 3600) * 3600000
    start = end - a.hours * 3600000
    active = [r for r in tickers if r["symbol"] in contracts and contracts[r["symbol"]].get("apiStateOpen") == "true"]
    if not active:
        active = [r for r in tickers if r["symbol"] in contracts and str(contracts[r["symbol"]].get("apiStateOpen")).lower() == "true"]
    active.sort(key=lambda r: (-float(r.get("quoteVolume") or 0), r["symbol"]))
    checks = []
    def one(symbol):
        rows = history(symbol, start, end)
        raw = json.dumps(rows, separators=(",", ":")).encode()
        (target / (symbol + ".json")).write_bytes(raw)
        c = contracts[symbol]
        return {"symbol": symbol, "bars": len(rows), "sha256": hashlib.sha256(raw).hexdigest(),
                "maker_rate": float(c["makerFeeRate"]), "taker_rate": float(c["takerFeeRate"])}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, a.jobs))) as pool:
        for row in pool.map(one, names):
            checks.append(row)
            print(json.dumps(row), flush=True)
    universe = [{"symbol": r["symbol"], "quote_volume_24h": float(r.get("quoteVolume") or 0),
                 "maker_rate": float(contracts[r["symbol"]]["makerFeeRate"]),
                 "taker_rate": float(contracts[r["symbol"]]["takerFeeRate"]),
                 "minimum_usdt": contracts[r["symbol"]].get("tradeMinUSDT"),
                 "api_open": contracts[r["symbol"]].get("apiStateOpen"),
                 "api_close": contracts[r["symbol"]].get("apiStateClose")} for r in active[:200]]
    meta = {"source": BASE, "fetched_at": time.time(), "start_ms": start, "end_ms": end,
            "hours": a.hours, "selection": "fixed 12-symbol universe, chosen before forward results",
            "symbols": names, "checks": checks, "universe200": universe,
            "account_authenticated": False, "orders_submitted": 0}
    (target / "_meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps({"symbols": len(checks), "universe": len(universe), "start_ms": start, "end_ms": end}), flush=True)


if __name__ == "__main__":
    main()
