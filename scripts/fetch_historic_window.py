"""Public candles only. Exact UTC coverage; no credentials or synthetic fallback."""
import argparse
import datetime as dt
import hashlib
import json
import math
import pathlib
import sys
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server' / 'pulse'))
from hist_calc import KLINE_URL_V3, _public_json, _timed_klines


def validate(rows, start, end):
    expected = list(range(start, end, 60000))
    if [r[0] for r in rows] != expected:
        raise ValueError('Incomplete, duplicate, unordered or out-of-window candle timestamps')
    for _, b in rows:
        if (len(b) != 5 or not all(math.isfinite(x) for x in b)
                or min(b[:4]) <= 0 or b[4] < 0
                or b[1] < max(b[0], b[2], b[3]) or b[2] > min(b[0], b[3])):
            raise ValueError('Invalid OHLCV')


def fetch(symbol, start, end):
    candles = {}
    for cursor in range(start, end, 1000 * 60000):
        stop = min(end, cursor + 1000 * 60000)
        params = dict(symbol=symbol, interval='1m', startTime=cursor,
                      endTime=stop - 1, limit=(stop - cursor) // 60000)
        response = _public_json(KLINE_URL_V3 + '?' + urllib.parse.urlencode(params), timeout=15)
        if not isinstance(response, dict) or response.get('code') != 0:
            raise ValueError('Public candle request failed: ' + str(response.get('code')))
        rows = _timed_klines(response.get('data'))
        validate(rows := sorted(rows), cursor, stop)
        candles.update(rows)
        print(json.dumps(dict(symbol=symbol, received=len(candles))), flush=True)
        time.sleep(1)
    rows = sorted(candles.items())
    validate(rows, start, end)
    return rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--end', required=True, help='Exclusive UTC date YYYY-MM-DD')
    p.add_argument('--days', type=int, default=5)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    if not 1 <= args.days <= 20:
        p.error('days must be between 1 and 20')
    end = int(dt.datetime.strptime(args.end, '%Y-%m-%d').replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    if end > int(time.time() // 60) * 60000:
        p.error('end must exclude the current open candle')
    start = end - args.days * 86400000
    out = pathlib.Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    for symbol in ('XRP-USDT', 'BCH-USDT', 'SOL-USDT'):
        path = out / (symbol + '.json')
        blob = dict(symbol=symbol, source=KLINE_URL_V3, start=start, end=end,
                    warmup=60, fetchedAt=time.time(), rows=fetch(symbol, start - 3600000, end))
        raw = json.dumps(blob, separators=(',', ':'), allow_nan=False).encode()
        path.write_bytes(raw)
        print(json.dumps(dict(file=path.name, sha256=hashlib.sha256(raw).hexdigest(), bars=len(blob['rows']))), flush=True)


if __name__ == '__main__':
    main()
