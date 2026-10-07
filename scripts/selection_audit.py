#!/usr/bin/env python3
"""Is the Set selection evidence predictive? (walk-forward, trade level)

Uses the replay caches that ``sim_12h_account.py`` writes (one or more window
dirs). For every Set trade entering in a window, the evidence is the closes of
the same Set x side (pooled over symbols, as the gate uses it, and per symbol)
that exited strictly before the entry. The trades are bucketed by that
evidence (last-N mean net move per trade, several N), and the next-trade net
of each bucket is reported. A useful key has its top bucket clearly above the
all-trades mean in every window.

Usage
  python3 scripts/selection_audit.py --window-dir /tmp/claude-0/wfroot/w1005-00 [--window-dir ...] \\
      --hours 20 --out audit.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim_12h_account as sa  # noqa: E402


def evidence_means(group, exit_bar, net, q_group, q_bar, ns):
    """Mean of the last n net moves of the same group with exit < bar, per n."""
    o = np.lexsort((exit_bar, group))
    g, e, m = group[o], exit_bar[o], net[o]
    cs = np.concatenate([[0.0], np.cumsum(m)])
    key = g.astype(np.int64) * 1_000_000 + e
    idx = np.searchsorted(key, q_group.astype(np.int64) * 1_000_000 + q_bar, side="left") - 1
    ug, first = np.unique(g, return_index=True)
    pos = np.clip(np.searchsorted(ug, q_group), 0, len(ug) - 1)
    gstart = np.where(ug[pos] == q_group, first[pos], 10 ** 15)
    ok_any = (idx >= 0) & (idx >= gstart)
    out = {}
    for n in ns:
        ok = ok_any & (idx - gstart + 1 >= n)
        hi = np.where(ok, idx + 1, 0)
        lo = np.where(ok, idx + 1 - n, 0)
        out[n] = np.where(ok, (cs[hi] - cs[lo]) / n, np.nan)
    return out


def bucket_table(key, net, nb=10):
    sel = ~np.isnan(key)
    k, v = key[sel], net[sel]
    if len(k) < nb * 50:
        return None
    qs = np.quantile(k, np.linspace(0, 1, nb + 1))
    rows = []
    for i in range(nb):
        m = (k >= qs[i]) & ((k < qs[i + 1]) if i < nb - 1 else (k <= qs[i + 1]))
        if m.sum() == 0:
            continue
        rows.append({"bucket": i + 1, "keyLo": round(float(qs[i]) * 100, 4), "keyHi": round(float(qs[i + 1]) * 100, 4),
                     "n": int(m.sum()), "nextNetPct": round(float(v[m].mean()) * 100, 4),
                     "win": round(float((v[m] > 0).mean()) * 100, 1)})
    return {"covered": int(sel.sum()), "rows": rows}


def audit_window(wdir: str, hours: int, ns, sample: int, seed: int):
    cache = os.path.join(wdir, "_cache")
    symbols = sorted(f[:-4] for f in os.listdir(cache) if f.endswith(".pkl"))
    caches = sa.load_cache(cache, symbols)
    cost = 0.0018
    parts = {k: [] for k in ("uid", "side", "entry", "exit", "raw", "sym", "reason")}
    n_all = None
    for si, s in enumerate(symbols):
        c = caches[s]["core"]
        n_all = caches[s]["n_all"]
        for k in ("uid", "side", "entry", "exit", "raw", "reason"):
            parts[k].append(c[k])
        parts["sym"].append(np.full(len(c["uid"]), si, dtype=np.int16))
    a = {k: np.concatenate(v) for k, v in parts.items()}
    sim_start = n_all - hours * 60
    net = a["raw"] - cost
    closed = a["exit"] >= 0
    cand = np.flatnonzero(closed & (a["entry"] >= sim_start))
    rng = np.random.default_rng(seed)
    if sample and len(cand) > sample:
        cand = np.sort(rng.choice(cand, sample, replace=False))
    pooled = a["uid"].astype(np.int64) * 2 + (a["side"] > 0)
    per_sym = pooled * 64 + a["sym"]
    ev = closed
    res = {"window": os.path.basename(wdir), "trades": int(len(cand)),
           "allNextNetPct": round(float(net[cand].mean()) * 100, 4),
           "allGrossPct": round(float(a["raw"][cand].mean()) * 100, 4),
           "exitMix": {r: int((a["reason"][cand] == i).sum()) for i, r in enumerate(sa.COST_REASONS)}}
    for label, grp in (("pooled", pooled), ("perSymbol", per_sym)):
        means = evidence_means(grp[ev], a["exit"][ev], net[ev], grp[cand], a["entry"][cand], ns)
        res[label] = {str(n): bucket_table(means[n], net[cand]) for n in ns}
    # the deployed gate: pooled last-50 mean net >= cost (cost-PF 1.10)
    gate = evidence_means(pooled[ev], a["exit"][ev], net[ev], pooled[cand], a["entry"][cand], [50])[50]
    sel = gate >= cost - 1e-12
    res["deployedGate"] = {"admitted": int(np.nansum(sel)),
                           "nextNetPct": round(float(net[cand][sel].mean()) * 100, 4) if sel.any() else None}
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window-dir", action="append", required=True)
    ap.add_argument("--hours", type=int, default=20)
    ap.add_argument("--n", default="10,30,50,100,200")
    ap.add_argument("--sample", type=int, default=400000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    ns = [int(x) for x in args.n.split(",")]
    out = [audit_window(w, args.hours, ns, args.sample, args.seed) for w in args.window_dir]
    for r in out:
        print(f"== {r['window']} trades={r['trades']} all next net={r['allNextNetPct']}% gross={r['allGrossPct']}% exits={r['exitMix']}")
        print(f"   deployed gate: {r['deployedGate']}")
        for label in ("pooled", "perSymbol"):
            for n, t in r[label].items():
                if not t:
                    continue
                cells = " ".join(f"{x['nextNetPct']:+.3f}" for x in t["rows"])
                print(f"   {label:9s} last-{n:>3s} (cov {t['covered']}): {cells}")
    if args.out:
        json.dump(out, open(args.out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
