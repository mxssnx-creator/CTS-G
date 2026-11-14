#!/usr/bin/env python3
"""Read-only: do the published stage figures equal a plain reading of the per-Set index?

For a running engine's stats file it recounts, with plain loops over `sets.index`, the Sets that pass Base, Main (Main window orders), the positive lane (Real), the negative lane (Real Negative) and
the union (valid on any lane), the shares of all configured Sets, and the pooled Base PF (gains over losses of the passing Sets' window orders), and compares them with `sets.pfBase.stages`
and `stageSets`, which the desk's Stages card shows. It also checks the lane relations: a Set on a lane passed Base; `valid` is the union of the two lanes; lane values are known.
Nothing is written, no API is called.   usage: stage_stats_audit.py [--stats /opt/grok-x01-pulse/stats-bingx-x01.json] [--tol 0.002]"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", default="/opt/grok-x01-pulse/stats-bingx-x01.json")
    ap.add_argument("--tol", type=float, default=0.002)
    a = ap.parse_args()
    d = json.load(open(a.stats))
    s = d.get("sets") or {}
    idx = s.get("index") or []
    pb = s.get("pfBase") or {}
    st = pb.get("stages") or {}
    ss = pb.get("stageSets") or {}
    bad, notes = [], []

    def check(name, got, want, tol=0.0):
        ok = (got == want) if isinstance(want, (int, str)) or want is None else abs(got - want) <= tol
        (notes if ok else bad).append(f"{'ok ' if ok else 'BAD'} {name}: published {got} recount {want}")

    n_all = len(idx)
    base = [r for r in idx if r.get("baseV")]
    lanes = {"pos": [], "neg": [], "both": [], "": []}
    unknown = []
    for r in idx:
        lane = r.get("lane") or ""
        (lanes[lane] if lane in lanes else unknown).append(r)
    on_pos = lanes["pos"] + lanes["both"]
    on_neg = lanes["neg"] + lanes["both"]
    valid = lanes["pos"] + lanes["neg"] + lanes["both"]
    check("configured Sets (allSets)", st.get("allSets", n_all), n_all)
    check("Base passing Sets", (st.get("base") or {}).get("sets", 0), len(base))
    check("Real (positive lane) Sets", (st.get("real") or {}).get("sets", 0), len(on_pos))
    check("Real Negative Sets", (st.get("realNeg") or {}).get("sets", 0), len(on_neg))
    check("valid on any lane", (st.get("valid") or {}).get("sets", 0), len(valid))
    if ss:
        check("stageSets.base", ss.get("base", 0), len(base))
        check("stageSets.real", ss.get("real", 0), len(on_pos))
        check("stageSets.realNeg", ss.get("realNeg", 0), len(on_neg))
    for key, rows in (("base", base), ("real", on_pos), ("realNeg", on_neg), ("valid", valid)):
        pct = (st.get(key) or {}).get("pct")
        if pct is not None and n_all:
            check(f"share of all, {key}", pct, 100.0 * len(rows) / n_all, 0.011)
    if unknown:
        bad.append(f"BAD lane values outside pos/neg/both/'': {sorted({r.get('lane') for r in unknown})} on {len(unknown)} Sets")
    off_base = [r["id"] for r in valid if not r.get("baseV")]
    (bad if off_base else notes).append(f"{'BAD' if off_base else 'ok '} every Set on a lane passed Base: {len(off_base)} that did not {off_base[:3]}")
    # post-Base PF must clear more than 1 on every stage a Set is valid through (owner 2026-11-14): Base (gate window), Main (its Main Set window), Real (positive lane), Real Negative (negative lane)
    def over_one(label, rows, n_key, pf_key):
        below = [r["id"] for r in rows if float(r.get(n_key) or 0) > 0 and 0 < float(r.get(pf_key) or 0) <= 1.0]
        unknown = sum(1 for r in rows if float(r.get(n_key) or 0) > 0 and not float(r.get(pf_key) or 0))
        (bad if below else notes).append(f"{'BAD' if below else 'ok '} {label}: {len(below)} Sets with PF <= 1 on their window {below[:3]}; {unknown} with orders but no finite PF (no loss in the window)")
    over_one("Base window PF over 1 (Sets passing Base)", base, "gn", "pf")
    over_one("Main window PF over 1 (Sets on a lane)", valid, "mainWindowN", "mainPf")
    over_one("Real window PF over 1 (positive lane)", on_pos, "realWindowN", "realPf")
    over_one("Real Negative window PF over 1 (negative lane)", on_neg, "realNegWindowN", "realNegPf")
    gp = sum(float(r.get("gp") or 0) for r in base)
    gl = sum(float(r.get("gl") or 0) for r in base)
    pf_pub = (st.get("base") or {}).get("pf")
    if base and gl > 0 and pf_pub:
        check("pooled Base PF (gains/losses of the window orders)", pf_pub, gp / gl, a.tol * max(1.0, gp / gl))
    elif base:
        notes.append(f"-- pooled Base PF not comparable (gains {gp:.4f}, losses {gl:.4f}, published {pf_pub})")
    print(f"{a.stats}: {n_all} Sets in the index, uptime {d.get('uptimeS')} s")
    for line in notes + bad:
        print(line)
    print("RESULT:", "stage figures equal the recount" if not bad else f"{len(bad)} mismatch(es)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
