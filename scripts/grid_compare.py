#!/usr/bin/env python3
"""Compare the Base x Main x Real last-N grids (independent_progress_audit.py --grid) of two consecutive test windows.

usage: grid_compare.py FIRST.json SECOND.json [--min-orders 150] [--top 8]

Selection is walk-forward: the combinations are ranked on the FIRST (earlier) window only, and the ones that rank best there are read on the SECOND window,
which did not take part in the choice. Also reported: the pooled PF of each setting over all combinations that use it, the rank correlation between the two
windows' combination PFs, and how many combinations hold PF >= 1 in each window. Every PF is gains over losses of the pooled net results (one PositionCost per order)."""
from __future__ import annotations

import json
import sys
from collections import defaultdict


def pf(g, l_):
    return g / l_ if l_ > 0 else (99.0 if g > 0 else 0.0)


def spearman(x, y):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, i in enumerate(order):
            r[i] = pos
        return r
    rx, ry = ranks(x), ranks(y)
    n = len(x)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else 0.0


def main() -> int:
    a, b = json.load(open(sys.argv[1]))["grid"], json.load(open(sys.argv[2]))["grid"]
    min_orders = int(sys.argv[sys.argv.index("--min-orders") + 1]) if "--min-orders" in sys.argv else 150
    top = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 8
    keys = sorted(set(a) & set(b))
    print(f"{len(keys)} combinations in both windows; ranked on the first, read on the second; at least {min_orders} orders in the first window to rank")
    for lane in ("pos", "neg"):
        ks = [k for k in keys if k.startswith(lane + "|")]
        both = [k for k in ks if a[k][2] >= 30 and b[k][2] >= 30]
        pa = [pf(a[k][0], a[k][1]) for k in both]
        pb = [pf(b[k][0], b[k][1]) for k in both]
        print(f"\n== {'positive' if lane == 'pos' else 'Negative'} lane: {len(both)} combinations with >= 30 orders in both windows; rank correlation of their PFs between the windows "
              f"{spearman(pa, pb):+.2f}; PF >= 1 in the first window: {sum(1 for x in pa if x >= 1)}, in the second: {sum(1 for x in pb if x >= 1)}")
        ranked = sorted((k for k in ks if a[k][2] >= min_orders), key=lambda k: -pf(a[k][0], a[k][1]))
        print(f"   top {top} on the first window (>= {min_orders} orders), then the same combinations on the second:")
        print(f"   {'combination':<38s}{'1st PF':>8s}{'orders':>8s}{'2nd PF':>9s}{'orders':>8s}")
        for k in ranked[:top]:
            print(f"   {k:<38s}{pf(a[k][0], a[k][1]):>8.3f}{int(a[k][2]):>8d}{pf(b[k][0], b[k][1]):>9.3f}{int(b[k][2]):>8d}")
        gp1 = sum(a[k][0] for k in ranked[:top]); gl1 = sum(a[k][1] for k in ranked[:top]); n1 = sum(a[k][2] for k in ranked[:top])
        gp2 = sum(b[k][0] for k in ranked[:top]); gl2 = sum(b[k][1] for k in ranked[:top]); n2 = sum(b[k][2] for k in ranked[:top])
        allp1 = pf(sum(a[k][0] for k in ks), sum(a[k][1] for k in ks)); allp2 = pf(sum(b[k][0] for k in ks), sum(b[k][1] for k in ks))
        print(f"   the top {top} pooled: first window PF {pf(gp1, gl1):.3f} ({int(n1)} orders), second window PF {pf(gp2, gl2):.3f} ({int(n2)} orders); every combination pooled: {allp1:.3f} / {allp2:.3f}")
        for dim, idx in (("Base", 1), ("Main", 2), ("Real", 3)):
            agg = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0, 0])
            for k in ks:
                v = agg[k.split("|")[idx]]
                v[0] += a[k][0]; v[1] += a[k][1]; v[2] += b[k][0]; v[3] += b[k][1]; v[4] += int(a[k][2]); v[5] += int(b[k][2])
            print(f"   by {dim} setting (pooled over every combination using it): " + "; ".join(f"{name} {pf(v[0], v[1]):.2f}/{pf(v[2], v[3]):.2f}" for name, v in sorted(agg.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
