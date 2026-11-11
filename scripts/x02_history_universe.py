#!/usr/bin/env python3
"""Public VST symbol preflight. No credentials, signing, writes or account access.

Oldest-minute probes reject obviously incomplete listings before
native full-history verification. These probes alone are not a complete tape.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request

from x02_low_steps_session import plan_for
from x02_native_session import HOST, context_requirement, frozen_overlay


def public(path, params=None):
    url = HOST + path + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(url, timeout=20) as response:
        result = json.load(response)
    if str(result.get('code')) != '0':
        raise RuntimeError('public VST read failed')
    return result['data']


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--overlay', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--history-hours', type=int, default=18)
    args = ap.parse_args()
    if args.out.exists():
        ap.error('output already exists')
    raw = json.loads(args.overlay.read_text())
    needed = context_requirement(frozen_overlay(raw, [f'S{i}-USDT' for i in range(50)], args.history_hours))
    now = int(public('/openApi/swap/v2/server/time')['serverTime'])
    boundary = now // 60000 * 60000 - needed * 60000
    contracts = public('/openApi/swap/v2/quote/contracts')
    tickers = public('/openApi/swap/v2/quote/ticker')
    candidates = [r for r in plan_for(contracts, tickers, 100) if not r['symbol'].startswith('NC')]
    def probe(row):
        symbol = row['symbol']
        try:
            old = public('/openApi/swap/v2/quote/klines', {
                'symbol': symbol, 'interval': '1m', 'limit': 3,
                'startTime': boundary, 'endTime': boundary + 120000})
            times = sorted(int(r['time']) for r in old)
            ok = bool(times and times[0] <= boundary + 60000)
            return dict(symbol=symbol, historyProbe=ok, oldestProbe=times[0] if times else None)
        except Exception as exc:
            return dict(symbol=symbol, historyProbe=False, errorType=type(exc).__name__)
    checks = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for row in pool.map(probe, candidates):
            checks.append(row)
    selected = [r['symbol'] for r in checks if r['historyProbe']][:50]
    if len(selected) != 50:
        raise RuntimeError(f'Only {len(selected)} eligible histories; no session started')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(selected, indent=2)+'\n')
    args.out.with_suffix('.preflight.json').write_text(json.dumps({
        'host': HOST, 'at': now, 'requiredHistoryBars': needed,
        'boundary': boundary, 'selected': selected, 'checks': checks,
        'scope': 'public oldest-minute probe; full native tape and account fees still required'}, indent=2)+'\n')
    print(json.dumps({'selected': len(selected), 'requiredHistoryBars': needed,
                      'rejected': sum(not r['historyProbe'] for r in checks)}))


if __name__ == '__main__':
    main()
