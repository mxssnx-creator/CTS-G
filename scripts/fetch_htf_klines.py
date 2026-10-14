#!/usr/bin/env python3
"""Public BingX 1h candles for the 1h (HTF) lane validation.

Writes one ``<output>/<SYMBOL>.json`` per symbol: {"symbol", "interval": "1h",
"start", "end", "rows": [[t_ms, [o, h, l, c, v]], ...]}. Closed bars only, no
synthetic fill; a symbol listed later than ``--days`` simply has fewer bars.
"""
import argparse
import json
import pathlib
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from hist_calc import KLINE_URL_V3, _public_json, _timed_klines  # noqa: E402

HOUR_MS = 3_600_000
PAGE = 1000
DEFAULT_SYMBOLS = (
    "BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,DOGE-USDT,BNB-USDT,ADA-USDT,AVAX-USDT,LINK-USDT,LTC-USDT,SUI-USDT,TRX-USDT,"
    "DOT-USDT,NEAR-USDT,APT-USDT,ARB-USDT,OP-USDT,PEPE-USDT,WIF-USDT,SEI-USDT,TIA-USDT,INJ-USDT,FIL-USDT,"
    "ATOM-USDT,AAVE-USDT,UNI-USDT,ETC-USDT,BCH-USDT,HBAR-USDT,TON-USDT"
)


def fetch(symbol: str, start: int, end: int) -> list:
    candles = {}
    cursor = start
    while cursor < end:
        stop = min(end, cursor + PAGE * HOUR_MS)
        params = dict(symbol=symbol, interval="1h", startTime=cursor, endTime=stop - 1, limit=PAGE)
        for attempt in range(4):
            try:
                resp = _public_json(KLINE_URL_V3 + "?" + urllib.parse.urlencode(params), timeout=20)
                if not isinstance(resp, dict) or resp.get("code") != 0:
                    raise ValueError(f"code {resp.get('code') if isinstance(resp, dict) else resp}")
                for t, bar in _timed_klines(resp.get("data")):
                    if start <= t < end and len(bar) == 5 and min(bar[:4]) > 0:
                        candles[int(t)] = [float(x) for x in bar]
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 3:
                    raise RuntimeError(f"{symbol} {cursor}: {exc}") from exc
                time.sleep(2 ** attempt)
        cursor = stop
        time.sleep(0.4)
    return sorted(candles.items())


def one(args):
    symbol, start, end, out = args
    try:
        rows = fetch(symbol, start, end)
    except Exception as exc:  # noqa: BLE001
        return symbol, f"FAILED {exc}", 0
    if not rows:
        return symbol, "EMPTY", 0
    blob = dict(symbol=symbol, interval="1h", source=KLINE_URL_V3, start=start, end=end,
                fetchedAt=time.time(), rows=[[t, b] for t, b in rows])
    (out / f"{symbol}.json").write_text(json.dumps(blob, separators=(",", ":")))
    return symbol, time.strftime("%Y-%m-%d", time.gmtime(rows[0][0] / 1000)), len(rows)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=730)
    p.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    end = int(time.time() // 3600) * HOUR_MS  # exclude the forming bar
    start = end - a.days * 24 * HOUR_MS
    out = pathlib.Path(a.output)
    out.mkdir(parents=True, exist_ok=True)
    syms = [s for s in a.symbols.split(",") if s]
    with ThreadPoolExecutor(4) as ex:
        for r in ex.map(one, [(s, start, end, out) for s in syms]):
            print(r, flush=True)


if __name__ == "__main__":
    main()
