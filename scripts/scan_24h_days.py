#!/usr/bin/env python3
"""Three consecutive 24h days: discover on day 1, keep what is still positive
on day 2, judge only on day 3 (out of sample).

Signal families and exits come from ``edge_scan.py`` (same first-touch exit
model: entry at the next 1m open, SL before TP, time stop). Days are the three
24h blocks that end at the end of the tape; earlier bars are indicator warm-up.

Usage
  python3 scripts/scan_24h_days.py --data-dir <dir with */SYMBOL.json> --cost-pct 0.07 --out scan.json
"""
from __future__ import annotations

import argparse
import json
import sys
import os
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edge_scan as es  # noqa: E402

DAY = 86_400_000


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--families", default="")
    ap.add_argument("--cost-pct", type=float, default=0.07)
    ap.add_argument("--tfs", type=es.ints, default=[1, 5, 15])
    ap.add_argument("--tps", type=es.floats, default=[0.1, 0.15, 0.2, 0.3, 0.5, 0.8])
    ap.add_argument("--sl-ratios", type=es.floats, default=[0.5, 1.0, 1.5, 2.0])
    ap.add_argument("--holds", type=es.ints, default=[3, 5, 10, 15, 30])
    ap.add_argument("--one-position", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--min-day-trades", type=int, default=40)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    tapes = es.load_tapes(args.data_dir, args.symbols.split(",") if args.symbols else None)
    t_end = min(int(t[-1, 0]) for t in tapes.values()) + 60_000
    starts = [t_end - (3 - k) * DAY for k in range(3)]
    families = set(args.families.split(",")) if args.families else None
    agg = {}
    t0 = time.time()
    for sym in sorted(tapes):
        t = tapes[sym]
        ex = es.Exits(t, max(args.holds))
        day = np.full(len(t), -1, dtype=np.int64)
        for k, s0 in enumerate(starts):
            day[(t[:, 0] >= s0) & (t[:, 0] < s0 + DAY)] = k
        cache = {}
        nets = {}
        for tp in args.tps:
            for r in args.sl_ratios:
                for hold in args.holds:
                    nets[(tp, r, hold)] = {s: ex.net(tp, round(tp * r, 4), hold, s, args.cost_pct, cache) for s in (1, -1)}
        for fname, prefix, hold, sides, idx in es.symbol_entries(t, sym, tapes, args, families):
            d = day[idx]
            ok = d >= 0
            if not ok.any():
                continue
            d, sd, ix = d[ok], sides[ok], idx[ok]
            cnt = np.bincount(d, minlength=3)
            for tp in args.tps:
                for r in args.sl_ratios:
                    by = nets[(tp, r, hold)]
                    net = np.where(sd > 0, by[1][ix], by[-1][ix])
                    key = (fname, f"{prefix}|tp{tp}|r{r}|h{hold}")
                    a = agg.setdefault(key, np.zeros((3, 3)))
                    a[:, 0] += cnt
                    a[:, 1] += np.bincount(d, weights=net, minlength=3)
                    a[:, 2] += np.bincount(d, weights=net + args.cost_pct, minlength=3)  # gross
        print(f"  {sym} configs={len(agg)} {time.time() - t0:.0f}s", flush=True)
    keys = list(agg)
    mat = np.stack([agg[k] for k in keys])
    n, net, gross = mat[:, :, 0], mat[:, :, 1], mat[:, :, 2]
    stage1 = (n[:, 0] >= args.min_day_trades) & (net[:, 0] > 0)
    stage2 = stage1 & (n[:, 1] >= args.min_day_trades) & (net[:, 1] > 0)
    surv = np.flatnonzero(stage2)

    def row(i):
        return {"family": keys[i][0], "config": keys[i][1],
                "trades": [int(x) for x in n[i]], "netPctSum": [round(float(x), 3) for x in net[i]],
                "netPerTrade": [round(float(net[i, d] / max(1, n[i, d])), 4) for d in range(3)],
                "grossPerTrade": [round(float(gross[i, d] / max(1, n[i, d])), 4) for d in range(3)]}
    day3 = net[surv, 2]
    out = {"costPct": args.cost_pct, "days": [time.strftime("%m-%d %H:%M", time.gmtime(s / 1000)) for s in starts],
           "configs": len(keys), "positiveDay1": int(stage1.sum()), "positiveDay1and2": int(stage2.sum()),
           "day3OfSurvivors": {"n": int(len(surv)), "positive": int((day3 > 0).sum()),
                               "sumNetPct": round(float(day3.sum()), 2), "trades": int(n[surv, 2].sum())},
           "survivors": sorted((row(i) for i in surv), key=lambda r: -r["netPctSum"][2])[:60],
           "byFamily": {}}
    for f in sorted({k[0] for k in keys}):
        m = np.array([k[0] == f for k in keys])
        s = surv[m[surv]]
        out["byFamily"][f] = {"configs": int(m.sum()), "day1": int((stage1 & m).sum()), "day1and2": int(len(s)),
                              "day3Positive": int((net[s, 2] > 0).sum()), "day3SumNet": round(float(net[s, 2].sum()), 2)}
    json.dump(out, open(args.out, "w"), indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "survivors"}, indent=1))
    for r in out["survivors"][:15]:
        print(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
