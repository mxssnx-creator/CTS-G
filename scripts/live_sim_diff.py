#!/usr/bin/env python3
"""Read-only: simulated against live results of the settled Set closes in a trades file (see server/pulse/sim_diff.py).

usage: live_sim_diff.py [--trades PATH] [--overlay PATH] [--cost-pct 0.12] [--json]
Nothing is written and no API is called. PositionCost comes from --cost-pct, else the overlay's positionCostPct, else 0.12 (percent)."""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
from sim_diff import diff_note, diff_report, new_diff  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default="/opt/grok-x01-pulse/trades-bingx-x02.jsonl")
    ap.add_argument("--overlay")
    ap.add_argument("--cost-pct", type=float)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cost = a.cost_pct
    if cost is None and a.overlay:
        cost = json.load(open(a.overlay)).get("positionCostPct")
    cost = 0.12 if cost is None else float(cost)
    agg = new_diff()
    for line in open(a.trades, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("ours") is False or not r.get("set_id"):
            continue
        diff_note(agg, r, cost / 100.0)
    rep = diff_report(agg)
    if a.json:
        print(json.dumps(rep, indent=1))
        return 0
    t = rep["total"]
    print(f"{rep['closes']} settled Set closes ({rep['unknown']} unscored/unknown) at PositionCost {cost:.3f} %")
    print(f"  live PF {t['livePf']}  sim PF {t['simPf']}  | live net {t['liveNetPct']} %  sim net {t['simNetPct']} %  gap {t['netGapPct']} % "
          f"(fill {t['fillGapPct']} %, cost {t['costGapPct']} %)")
    for k, v in rep["byExit"].items():
        print(f"  {k:6s} n={v['n']:4d} livePF={v['livePf']} simPF={v['simPf']} fill gap {v['fillGapPct']} % cost gap {v['costGapPct']} %")
    s, g = rep["stopFactor"], rep["targetFactor"]
    print(f"  stop fill factor (live loss / planned stop): n={s['n']} median={s['median']} mean={s['mean']} p90={s['p90']}")
    print(f"  target fill factor: n={g['n']} median={g['median']} mean={g['mean']} p10={g['p10']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
