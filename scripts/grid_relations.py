#!/usr/bin/env python3
"""Relations a window grid must hold, read from its JSON (scripts/window_grid.py): every cell's orders, net and PF agree however they are cut (the flag the grid
sets), the cells are unique, the short window is within its allowed share of the long window, a cell with the Base minimal window never reports more Base-level
validity than its twin without it where the twin is comparable, and the marginal means per setting. Exit code 1 when a relation is broken.

usage: grid_relations.py GRID.json"""
from __future__ import annotations

import json
import sys
from collections import defaultdict


def main() -> int:
    data = json.load(open(sys.argv[1]))
    meta, cells = data["meta"], data["cells"]
    staged = [c for c in cells if c["stages"]]
    problems = []
    ids = [c["id"] for c in cells]
    if len(ids) != len(set(ids)):
        problems.append("duplicate cell ids")
    # the grid's own flag, except the sign relation at a PF rounded to 1.000 (break-even: PF and net can sit on either side of it)
    broken = [(c["id"], c["inconsistent"]) for c in cells if not c.get("consistent", True)
              and not (c["inconsistent"] == ["PF above 1 = net above 0"] and c["exec_pf"] is not None and abs(c["exec_pf"] - 1.0) < 0.0015)]
    if broken:
        problems.append(f"{len(broken)} cells whose orders, net and PF disagree: {broken[:3]}")
    ratio = meta.get("short_max_ratio", 1.0)
    over = [c["id"] for c in staged if c["short"] > int(c["long"] * ratio + 1e-9) or c["short"] >= c["long"]]
    if over:
        problems.append(f"short window over its allowed share: {over[:3]}")
    over_min = [c["id"] for c in staged if (c.get("min_n") or 0) > c["short"]]
    if over_min:
        problems.append(f"minimal window over the short window: {over_min[:3]}")
    # a twin of a cell: the same settings with the minimal window off
    key = lambda c: (c["long"], c["short"], c["main"], c["real"], c.get("neg"))  # noqa: E731
    off = {key(c): c for c in staged if not c.get("min_n")}
    worse_valid = []
    for c in staged:
        if c.get("min_n") and key(c) in off:
            t = off[key(c)]
            if c["valid_pct"] > t["valid_pct"] * 1.5 + 1.0:           # the stages after Base can re-admit a Set, so only a large rise is a defect
                worse_valid.append((c["id"], c["valid_pct"], t["valid_pct"]))
    if worse_valid:
        problems.append(f"{len(worse_valid)} minimal-window cells with a far higher validated share than their twin without it: {worse_valid[:3]}")
    print(f"{len(cells)} cells ({len(staged)} with stages), {meta.get('groups')} independent groups; relations: " + ("all hold" if not problems else "BROKEN"))
    for p in problems:
        print("  -", p)

    def marg(label, get):
        by = defaultdict(list)
        for c in staged:
            if c["exec_pf"] is not None:
                by[get(c)].append(c)
        print(f"\n{label}: cells, mean executed PF, mean effective PF, mean valid %, mean orders, mean orders on the Negative lane")
        for k in sorted(by, key=str):
            cs = by[k]
            n = len(cs)
            print(f"  {str(k):<14s} {n:>4d}  {sum(c['exec_pf'] for c in cs) / n:.3f}  {sum(c['eff_pf'] or 0 for c in cs) / n:.2f}  {sum(c['valid_pct'] for c in cs) / n:5.2f}  "
                  f"{sum(c['exec_orders'] for c in cs) / n:6.0f}  {sum((c.get('neg_lane') or {}).get('orders', 0) for c in cs) / n:6.0f}")
    lab = lambda c: "off" if not c.get("min_n") else f"{'+' if c['min_mode'] == 'positive' else '-'}{c['min_n']}"  # noqa: E731
    marg("minimal window", lab)
    marg("Negative windows", lambda c: c.get("neg") or meta.get("neg_default"))
    marg("Real last N", lambda c: c["real_eff"])
    marg("Base long", lambda c: c["long"])
    marg("Base short", lambda c: c["short_eff"])
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
