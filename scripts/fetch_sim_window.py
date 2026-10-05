"""Public 1m candles for the 12h account simulation (all desk symbols, exact UTC window, no synthetic fill)."""
import argparse, datetime as dt, hashlib, json, pathlib, sys, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fetch_historic_window import fetch, KLINE_URL_V3


def one(args):
    symbol, start, end, out = args
    for attempt in range(4):
        try:
            rows = fetch(symbol, start - 3600000, end)
            blob = dict(symbol=symbol, source=KLINE_URL_V3, start=start, end=end, warmup=60,
                        fetchedAt=time.time(), rows=rows)
            raw = json.dumps(blob, separators=(',', ':'), allow_nan=False).encode()
            (out / (symbol + '.json')).write_bytes(raw)
            return symbol, hashlib.sha256(raw).hexdigest()[:12], len(rows)
        except Exception as exc:  # noqa: BLE001
            err = exc
            time.sleep(2 ** attempt)
    return symbol, 'FAILED ' + str(err), 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--hours', type=int, default=60, help='total window incl. lookback')
    p.add_argument('--symbols', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    end = int(time.time() // 3600) * 3600 * 1000
    start = end - a.hours * 3600000
    out = pathlib.Path(a.output); out.mkdir(parents=True, exist_ok=True)
    syms = [s for s in a.symbols.split(',') if s]
    with ThreadPoolExecutor(6) as ex:
        for r in ex.map(one, [(s, start, end, out) for s in syms]):
            print(r, flush=True)
    print('window', dt.datetime.utcfromtimestamp(start / 1000), '->', dt.datetime.utcfromtimestamp(end / 1000))


if __name__ == '__main__':
    main()
