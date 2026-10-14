#!/usr/bin/env python3
"""Freeze public BingX candles/contracts/funding for a causal six-hour audit.

Requests start at least 1.05 seconds apart across workers. Earlier candles
are indicator warmup only; the manifest pins separate preparation/test bounds.
No keys, account access, synthetic candles or order endpoints are used.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server' / 'pulse'))
from history_store import parse_row

BASE = 'https://open-api.bingx.com'
LOCK = threading.Lock()
NEXT_REQUEST = 0.0


def public_json(url):
    global NEXT_REQUEST
    for attempt in range(3):
        with LOCK:
            due = max(time.monotonic(), NEXT_REQUEST)
            NEXT_REQUEST = due + 1.05
        time.sleep(max(0.0, due - time.monotonic()))
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'CTS-G-public-replay-audit/1'})
            with urllib.request.urlopen(req, timeout=25) as response:
                result = json.load(response)
            if not isinstance(result, dict) or str(result.get('code', 0)) != '0':
                raise ValueError(f'public response code {result.get("code") if isinstance(result, dict) else "invalid"}')
            return result
        except Exception:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def write_json(path, value):
    data = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def fetch_symbol(symbol, out, start, end):
    bars = {}
    errors = []
    for cursor in range(start, end, 1000 * 60000):
        stop = min(end, cursor + 1000 * 60000)
        params = dict(symbol=symbol, interval='1m', startTime=cursor, endTime=stop - 1, limit=1000)
        url = BASE + '/openApi/swap/v3/quote/klines?' + urllib.parse.urlencode(params)
        try:
            payload = public_json(url)
            for raw in payload.get('data') or []:
                parsed = parse_row(raw)
                if parsed:
                    minute, bar, _, _ = parsed
                    stamp = minute * 60000
                    if start <= stamp < end:
                        bars[stamp] = bar
        except Exception as exc:
            errors.append(f'candles {cursor}: {type(exc).__name__}: {exc}')
    rows = [[t, bars[t]] for t in sorted(bars)]
    gaps = sum(b[0] - a[0] != 60000 for a, b in zip(rows, rows[1:]))
    test_start, preparation_start = end - 360 * 60000, end - 720 * 60000
    required = set(range(preparation_start, end, 60000))
    missing = len(required - set(bars))
    sha = write_json(out / f'{symbol}.json', dict(symbol=symbol, source=BASE,
        start=start, end=end, rows=rows))
    funding = []
    funding_complete = False
    params = dict(symbol=symbol, startTime=preparation_start, endTime=end - 1, limit=1000)
    try:
        funding = public_json(BASE + '/openApi/swap/v2/quote/fundingRate?' + urllib.parse.urlencode(params)).get('data') or []
        funding_complete = True
    except Exception as exc:
        errors.append(f'funding: {type(exc).__name__}: {exc}')
    funding_sha = write_json(out / 'funding' / f'{symbol}.json', funding)
    return dict(symbol=symbol, candles=len(rows), gaps=gaps, missingRequiredMinutes=missing,
        sha256=sha, fundingSha256=funding_sha, fundingSamples=len(funding), fundingComplete=funding_complete, errors=errors,
        first=rows[0][0] if rows else None, last=rows[-1][0] if rows else None,
        preparationStart=preparation_start, executionStart=test_start,
        complete=missing == 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--end', required=True, help='UTC ISO end, exclusive and minute aligned')
    parser.add_argument('--hours', type=int, default=48, help='includes warmup; preparation remains6h')
    parser.add_argument('--overlay', default=str(ROOT / 'server/pulse/overlay-bingx-x02.json'))
    parser.add_argument('--workers', type=int, default=6)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'funding').mkdir(exist_ok=True)
    end = int(datetime.fromisoformat(args.end.replace('Z', '+00:00')).timestamp() * 1000)
    if end % 60000 or args.hours < 12:
        raise ValueError('at least12h and minute-aligned end required')
    start = end - args.hours * 3600000
    overlay = json.loads(Path(args.overlay).read_text())
    symbols = list(dict.fromkeys(overlay['symbols']))[:int(overlay.get('symbolCap') or len(overlay['symbols']))]
    manifest = dict(endUtc=datetime.fromtimestamp(end / 1000, timezone.utc).isoformat(),
        end=end, preparationStart=end - 12 * 3600000, executionStart=end - 6 * 3600000,
        historyStart=start, requestedSymbols=symbols, overlaySha256=write_json(out / 'overlay.json', overlay),
        source='BingX public REST; static committed overlay universe; no hindsight symbol ranking',
        fetchedAt=datetime.now(timezone.utc).isoformat(), symbols=[])
    contracts = public_json(BASE + '/openApi/swap/v2/quote/contracts')
    manifest['contractsSha256'] = write_json(out / 'contracts.json', {'vst': {'source': BASE,
        'note': 'public mainnet instrument specifications; simulation uses no exchange account', 'data': contracts.get('data') or []}})
    with ThreadPoolExecutor(max_workers=max(1, min(8, args.workers))) as pool:
        futures = {pool.submit(fetch_symbol, symbol, out, start, end): symbol for symbol in symbols}
        for future in as_completed(futures):
            row = future.result()
            manifest['symbols'].append(row)
            write_json(out / 'manifest.json', manifest)
            print(json.dumps({k: row[k] for k in ('symbol', 'candles', 'missingRequiredMinutes', 'fundingSamples', 'errors')}), flush=True)
    manifest['symbols'].sort(key=lambda row: symbols.index(row['symbol']))
    manifest['completeSymbols'] = sum(row['complete'] for row in manifest['symbols'])
    write_json(out / 'manifest.json', manifest)
    print(json.dumps({'completeSymbols': manifest['completeSymbols'], 'requested': len(symbols)}), flush=True)


if __name__ == '__main__':
    main()
