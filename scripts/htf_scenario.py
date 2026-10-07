#!/usr/bin/env python3
"""Engine-in-the-loop check of the 1h lane over the last N days.

Walks 1h bars forward for every symbol through HtfBook exactly as the trader
uses it: replay evidence from the trailing history window (refreshed daily),
entries decided at each bar close, the HtfBook gates (evidence PF, direction
acceptance, optional last-N), one lot per symbol x kind x side, max open lots,
preset exits on the following bars, and every close credited back as a live
exchange result (so live evidence takes over from replay as it accumulates).

Usage
  python3 scripts/htf_scenario.py --data-dir /tmp/claude-0/htf1h --days 30 \
      --symbols BTC-USDT,ETH-USDT,... --out reports/x/scenario.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import sys
import time
from typing import Dict, List

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
import htf_engine as he  # noqa: E402

H = he.HOUR_MS


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", default="BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,DOGE-USDT,BNB-USDT,ADA-USDT,AVAX-USDT,LINK-USDT,LTC-USDT,SUI-USDT,TRX-USDT")
    ap.add_argument("--cost", type=float, default=0.0018)
    ap.add_argument("--max-open", type=int, default=12)
    ap.add_argument("--no-gates", action="store_true", help="trade every preset signal (evidence and direction gates off)")
    ap.add_argument("--window", type=int, default=50)
    ap.add_argument("--last-n", type=int, default=0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    tapes: Dict[str, he.Bars] = {}
    for sym in args.symbols.split(","):
        path = os.path.join(args.data_dir, f"{sym}.json")
        if os.path.exists(path):
            tapes[sym] = he.Bars.from_rows(json.load(open(path))["rows"], 60, sym)
    if not tapes:
        sys.exit("no data")
    t_end = min(float(b.t[-1]) for b in tapes.values()) + H
    t_start = t_end - args.days * 24 * H
    book = he.HtfBook(he.HtfSettings(enabled=True, max_open=args.max_open, cost=args.cost, window=args.window,
                                     last_n=args.last_n, **({"min_n": 10 ** 9, "side_min_trades": 10 ** 9} if args.no_gates else {})))
    hist_n = book.s.history_bars
    # causal entry signals per symbol x kind on the full tape (same as bar-by-bar)
    sigs = {s: {k: he.entry_signals(b, [k], vol_regime=he.HTF_PRESETS[k][1])[k] for k in he.HTF_KINDS} for s, b in tapes.items()}
    idx = {s: {float(t): i for i, t in enumerate(b.t)} for s, b in tapes.items()}
    open_lots: List[Dict] = []
    closed: List[Dict] = []
    skipped: Dict[str, int] = {}
    last_replay_day = None
    t = t_start
    while t < t_end:
        day = int(t // (24 * H))
        if day != last_replay_day:
            for s, b in tapes.items():
                i = idx[s].get(t)
                if i is None:
                    continue
                book.bars[s] = b.slice(max(0, i - hist_n), i)  # closed bars before t
                book.replay(s)
            last_replay_day = day
        # exits of open lots up to this bar
        for lot in list(open_lots):
            if lot["exit_t"] <= t:
                open_lots.remove(lot)
                closed.append(lot)
                book.on_live_close({"exchange_confirmed": True, "ind_kind": lot["kind"], "client_id": lot["id"],
                                    "close_fill_id": lot["id"] + ":x", "t": lot["exit_t"] / 1000, "side": lot["side"],
                                    "symbol": lot["symbol"], "entry": 100.0, "qty": 1.0, "pnl": lot["r"] * 100.0})
        # entries decided at the close of the bar that opened at t - 1h
        for s, b in tapes.items():
            i = idx[s].get(t - H)
            if i is None or i + 1 >= b.n:
                continue
            for k in book.s.kinds:
                d = int(sigs[s][k][i])
                if not d:
                    continue
                side = "LONG" if d > 0 else "SHORT"
                ok, why, info = book.gate(k, side, t / 1000)
                if not ok:
                    skipped[why.split(" (")[0][:40]] = skipped.get(why.split(" (")[0][:40], 0) + 1
                    continue
                if any(l["symbol"] == s and l["kind"] == k and l["side"] == side for l in open_lots):
                    continue
                if len(open_lots) >= args.max_open:
                    skipped["max open"] = skipped.get("max open", 0) + 1
                    continue
                cfg, _ = he.preset(k)
                ex = he.exit_one(b, i + 1, d, cfg)
                if ex is None:
                    continue
                xi, px, why_x = ex
                entry = float(b.o[i + 1])
                r = d * (px - entry) / entry - args.cost
                open_lots.append({"id": f"{s}:{k}:{side}:{t}", "symbol": s, "kind": k, "side": side,
                                  "entry_t": float(b.t[i + 1]), "exit_t": float(b.t[xi]) + H, "r": r,
                                  "reason": why_x, "source": info.get("source"), "family": he.KINDS[k].family})
        t += H
    rs = [c["r"] for c in closed]
    by_day: Dict[str, float] = {}
    by_hour: Dict[int, float] = {}
    for c in closed:
        dk = time.strftime("%m-%d", time.gmtime(c["exit_t"] / 1000))
        by_day[dk] = by_day.get(dk, 0.0) + c["r"]
        hk = int(c["exit_t"] // H)
        by_hour[hk] = by_hour.get(hk, 0.0) + c["r"]
    fam = {}
    for f in he.FAMILIES:
        fr = [c["r"] for c in closed if c["family"] == f]
        fam[f] = {"n": len(fr), "pf": round(he.pf_classic(fr), 3) if fr else None, "netPct": round(100 * sum(fr), 2)}
    out = {
        "window": [time.strftime("%Y-%m-%d %H:%M", time.gmtime(t_start / 1000)), time.strftime("%Y-%m-%d %H:%M", time.gmtime(t_end / 1000))],
        "symbols": sorted(tapes), "closed": len(closed), "stillOpen": len(open_lots),
        "pf": round(he.pf_classic(rs), 3) if rs else None, "netPct": round(100 * sum(rs), 2),
        "meanPct": round(100 * sum(rs) / max(1, len(rs)), 3), "winRate": round(100 * sum(1 for r in rs if r > 0) / max(1, len(rs)), 1),
        "tradesPerDay": round(len(closed) / args.days, 2),
        "daysPositive": f"{sum(1 for v in by_day.values() if v > 0)}/{len(by_day)}",
        "closeHoursPositive": f"{sum(1 for v in by_hour.values() if v > 0)}/{len(by_hour)}",
        "families": fam, "skipped": dict(sorted(skipped.items(), key=lambda kv: -kv[1])),
        "liveTakeover": {k: len(v) for k, v in book.live.items()},
        "byDay": {k: round(100 * v, 2) for k, v in sorted(by_day.items())},
    }
    print(json.dumps({k: v for k, v in out.items() if k != "byDay"}, indent=1))
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        json.dump(out, open(args.out, "w"), indent=1)


if __name__ == "__main__":
    main()
