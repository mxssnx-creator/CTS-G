#!/usr/bin/env python3
"""Validate the 1h (HTF) lane on our own BingX 1h data before it trades.

Method (CTS-A-O docs/family-1h.md, docs/matrix.md), with CTS-G costs:
  * research year = last 365 days, split into half A (select) and half B
    (validate); prior year = the 365 days before, never used for selection;
  * per kind, the exit config and volRegime setting with the best half-A PF
    (min trades) is selected; kinds whose half-A PF < --select-pf are dropped;
  * a family (rsi-mom / robust) passes only if, pooled over its selected kinds:
      half-B PF >= --min-pf-b, prior-year PF >= --min-pf-prior,
      >= --min-trades trades in half B and in the prior year,
      half-B PF > 1 with any single symbol removed;
  * the CTS-A-O last-N gate (take a trade only when the config's last N
    closes before its entry have PF >= 1) is reported as a second variant.

Usage
  python3 scripts/htf_validate.py --data-dir /tmp/claude-0/htf1h \
      --out reports/htf-validation-x/htf.json --html reports/htf-validation-x/index.html
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import math
import os
import pathlib
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Dict, List, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
import htf_engine as he  # noqa: E402

DAY_MS = 86_400_000


def load(data_dir: str) -> Dict[str, he.Bars]:
    out = {}
    for path in sorted(glob.glob(os.path.join(data_dir, "*.json"))):
        blob = json.load(open(path))
        rows = blob.get("rows") or []
        if len(rows) < 2000:
            continue
        out[blob.get("symbol") or os.path.basename(path)[:-5]] = he.Bars.from_rows(rows, 60, blob.get("symbol", ""))
    return out


def _sym_trades(args) -> List[Tuple]:
    path, cost, vol_modes = args
    blob = json.load(open(path))
    sym = blob.get("symbol")
    b = he.Bars.from_rows(blob["rows"], 60, sym)
    grid = he.exit_grid()
    rows = []
    for vr in vol_modes:
        sigs = he.entry_signals(b, he.HTF_KINDS, vol_regime=vr)
        for kind, sig in sigs.items():
            for key, trades in he.simulate_kind(b, kind, sig, grid, cost).items():
                for t in trades:
                    rows.append((kind, int(vr), key, sym, t.side, t.entry_t, t.exit_t, t.r, t.reason))
    return rows


def pf(rs):
    return he.pf_classic(rs) if rs else 0.0


def last_n_gate(trades: List[Tuple], n: int, floor: int) -> List[Tuple]:
    """Keep a trade only when the config's last ``n`` closes before its entry
    have PF >= 1 (fewer than ``floor`` closes: not taken)."""
    closed = sorted(trades, key=lambda x: x[6])
    kept = []
    j = 0
    hist: List[float] = []
    for tr in sorted(trades, key=lambda x: x[5]):
        while j < len(closed) and closed[j][6] <= tr[5]:
            hist.append(closed[j][7])
            j += 1
        tail = hist[-n:]
        if len(tail) >= floor and pf(tail) >= 1.0:
            kept.append(tr)
    return kept


def evaluate(rows, t_end, args) -> Dict:
    prior_lo = t_end - 2 * 365 * DAY_MS
    res_lo = t_end - 365 * DAY_MS
    mid = res_lo + int(182.5 * DAY_MS)

    def period(t):
        if t < prior_lo:
            return None
        if t < res_lo:
            return "prior"
        return "A" if t < mid else "B"

    by_cfg: Dict[Tuple, List[Tuple]] = {}
    for r in rows:
        by_cfg.setdefault((r[0], r[1], r[2]), []).append(r)

    def split(trs):
        out = {"A": [], "B": [], "prior": []}
        for tr in trs:
            p = period(tr[5])
            if p:
                out[p].append(tr)
        return out

    kinds_out = {}
    selected: Dict[str, Dict[str, Tuple]] = {"plain": {}, "lastN": {}}
    for variant in ("plain", "lastN"):
        for kind in he.HTF_KINDS:
            best = None
            for (k, vr, key), trs in by_cfg.items():
                if k != kind:
                    continue
                use = last_n_gate(trs, args.last_n, args.last_n_floor) if variant == "lastN" else trs
                sp = split(use)
                a = [x[7] for x in sp["A"]]
                if len(a) < args.min_select_trades:
                    continue
                score = pf(a)
                if best is None or score > best[0]:
                    best = (score, vr, key, sp)
            if best is None:
                continue
            score, vr, key, sp = best
            rec = {
                "volRegime": bool(vr), "exit": key, "pfA": round(score, 3), "nA": len(sp["A"]),
                "pfB": round(pf([x[7] for x in sp["B"]]), 3), "nB": len(sp["B"]),
                "pfPrior": round(pf([x[7] for x in sp["prior"]]), 3), "nPrior": len(sp["prior"]),
                "meanB": round(100 * sum(x[7] for x in sp["B"]) / max(1, len(sp["B"])), 3),
                "selected": score >= args.select_pf,
            }
            kinds_out.setdefault(kind, {})[variant] = rec
            if rec["selected"]:
                selected[variant][kind] = sp

    families = {}
    for variant in ("plain", "lastN"):
        for fam in he.FAMILIES:
            sp = {"A": [], "B": [], "prior": []}
            members = [k for k in selected[variant] if he.KINDS[k].family == fam]
            for k in members:
                for p in sp:
                    sp[p].extend(selected[variant][k][p])
            b = [x[7] for x in sp["B"]]
            pr = [x[7] for x in sp["prior"]]
            syms = sorted({x[3] for x in sp["B"]})
            loo = {s: round(pf([x[7] for x in sp["B"] if x[3] != s]), 3) for s in syms}
            loo_min = min(loo.values()) if loo else 0.0
            days_b = max(1.0, 182.5)
            hours: Dict[int, float] = {}
            for x in sp["B"]:
                hk = int(x[6] // 3_600_000)
                hours[hk] = hours.get(hk, 0.0) + x[7]
            checks = {
                "pfB": pf(b) >= args.min_pf_b,
                "pfPrior": pf(pr) >= args.min_pf_prior,
                "tradesB": len(b) >= args.min_trades,
                "tradesPrior": len(pr) >= args.min_trades,
                "leaveOneOut": bool(loo) and loo_min > 1.0,
            }
            families[f"{fam}|{variant}"] = {
                "family": fam, "variant": variant, "kinds": members,
                "pfA": round(pf([x[7] for x in sp["A"]]), 3), "nA": len(sp["A"]),
                "pfB": round(pf(b), 3), "nB": len(b), "pfPrior": round(pf(pr), 3), "nPrior": len(pr),
                "costPfB": round(he.pf_cost(b, args.cost), 4),
                "meanB": round(100 * sum(b) / max(1, len(b)), 3),
                "netB": round(100 * sum(b), 2), "netPrior": round(100 * sum(pr), 2),
                "tradesPerDayB": round(len(b) / days_b, 2),
                "closeHoursPositivePct": round(100 * sum(1 for v in hours.values() if v > 0) / max(1, len(hours)), 1),
                "leaveOneOutMin": loo_min, "leaveOneOut": loo,
                "checks": checks, "passes": all(checks.values()),
            }
    return {"kinds": kinds_out, "families": families,
            "periods": {"prior": [prior_lo, res_lo], "A": [res_lo, mid], "B": [mid, t_end]}}


def render(rep: Dict) -> str:
    def d(ms):
        return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))
    per = rep["periods"]
    fam_rows = ""
    for name, f in sorted(rep["families"].items()):
        ok = "<b class='g'>passes</b>" if f["passes"] else "<b class='r'>fails</b>"
        fails = ", ".join(k for k, v in f["checks"].items() if not v)
        fam_rows += (f"<tr class='{'ok' if f['passes'] else ''}'><td>{html.escape(f['family'])}</td><td>{f['variant']}</td><td>{len(f['kinds'])}</td>"
                     f"<td>{f['pfA']} ({f['nA']})</td><td>{f['pfB']} ({f['nB']})</td><td>{f['pfPrior']} ({f['nPrior']})</td>"
                     f"<td>{f['costPfB']}</td><td>{f['meanB']:+.3f}</td><td>{f['tradesPerDayB']}</td><td>{f['leaveOneOutMin']}</td>"
                     f"<td>{ok} {html.escape(fails)}</td></tr>")
    kind_rows = ""
    for k, v in sorted(rep["kinds"].items()):
        for variant, r in sorted(v.items()):
            kind_rows += (f"<tr class='{'ok' if r['selected'] else ''}'><td>{html.escape(k)}</td><td>{variant}</td><td>{'on' if r['volRegime'] else 'off'}</td>"
                          f"<td>{html.escape(r['exit'])}</td><td>{r['pfA']} ({r['nA']})</td><td>{r['pfB']} ({r['nB']})</td>"
                          f"<td>{r['pfPrior']} ({r['nPrior']})</td><td>{r['meanB']:+.3f}</td><td>{'yes' if r['selected'] else 'no'}</td></tr>")
    a = rep["args"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>1h Lane Validation</title><style>
:root{{--bg:#fff;--fg:#1d2330;--mut:#5f6b7a;--line:#e3e7ee;--ok:#e8f6ee;--g:#1d7a4a;--r:#b23a3a}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151b;--fg:#e6e9ef;--mut:#9aa3b2;--line:#2a303b;--ok:#17301f;--g:#5bc58a;--r:#e07a7a}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1180px;padding:16px}}
table{{border-collapse:collapse;width:100%;margin:8px 0 20px;font-variant-numeric:tabular-nums}}td,th{{border-bottom:1px solid var(--line);padding:4px 6px;text-align:right;white-space:nowrap}}
td:first-child,th:first-child{{text-align:left}}tr.ok{{background:var(--ok)}}.g{{color:var(--g)}}.r{{color:var(--r)}}.mut{{color:var(--mut)}}.wrap{{overflow-x:auto}}
</style></head><body><h1>1h lane validation</h1>
<p class="mut">{len(rep['symbols'])} symbols · prior year {d(per['prior'][0])} → {d(per['prior'][1])} (unseen) · half A {d(per['A'][0])} → {d(per['A'][1])} (select) ·
half B {d(per['B'][0])} → {d(per['B'][1])} (validate) · cost {a['cost'] * 100:.2f}% round trip · PF = gross win / gross loss after cost
(cost PF = CTS-G 1 + 0.1·mean/cost) · generated {rep['generatedAt']}</p>
<p class="mut">Pass rule (fixed before the run): half-B PF ≥ {a['min_pf_b']}, prior-year PF ≥ {a['min_pf_prior']}, ≥ {a['min_trades']} trades in half B and the prior year,
half-B PF &gt; 1 with any one symbol removed. Kinds enter a family when half-A PF ≥ {a['select_pf']} on ≥ {a['min_select_trades']} trades. lastN = CTS-A-O gate: last {a['last_n']} closes PF ≥ 1.</p>
<h2>Families</h2><div class="wrap"><table><tr><th>family</th><th>variant</th><th>kinds</th><th>PF A (n)</th><th>PF B (n)</th><th>PF prior (n)</th><th>cost PF B</th><th>mean B %</th><th>trades/day B</th><th>LOO min</th><th>verdict</th></tr>{fam_rows}</table></div>
<h2>Kinds (best half-A config)</h2><div class="wrap"><table><tr><th>kind</th><th>variant</th><th>volRegime</th><th>exit</th><th>PF A (n)</th><th>PF B (n)</th><th>PF prior (n)</th><th>mean B %</th><th>selected</th></tr>{kind_rows}</table></div>
</body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--cost", type=float, default=0.0018)
    ap.add_argument("--select-pf", type=float, default=1.1)
    ap.add_argument("--min-select-trades", type=int, default=30)
    ap.add_argument("--min-pf-b", type=float, default=1.05)
    ap.add_argument("--min-pf-prior", type=float, default=1.0)
    ap.add_argument("--min-trades", type=int, default=100)
    ap.add_argument("--last-n", type=int, default=12)
    ap.add_argument("--last-n-floor", type=int, default=5)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", required=True)
    ap.add_argument("--html", default="")
    args = ap.parse_args()
    paths = sorted(glob.glob(os.path.join(args.data_dir, "*.json")))
    rows: List[Tuple] = []
    syms = []
    t_end = 0
    with ProcessPoolExecutor(args.workers) as ex:
        for path, part in zip(paths, ex.map(_sym_trades, [(p, args.cost, (True, False)) for p in paths])):
            rows.extend(part)
            syms.append(os.path.basename(path)[:-5])
            print(f"  {syms[-1]}: {len(part)} trade rows", flush=True)
    for p in paths:
        r = json.load(open(p))["rows"]
        t_end = max(t_end, int(r[-1][0]) + 3_600_000)
    rep = evaluate(rows, t_end, args)
    rep.update(generatedAt=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), symbols=syms, args=vars(args))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump(rep, open(args.out, "w"), indent=1)
    if args.html:
        open(args.html, "w").write(render(rep))
    for name, f in sorted(rep["families"].items()):
        print(f"{name:18s} kinds={len(f['kinds']):2d} A={f['pfA']}({f['nA']}) B={f['pfB']}({f['nB']}) prior={f['pfPrior']}({f['nPrior']}) "
              f"meanB={f['meanB']:+.3f}% /day={f['tradesPerDayB']} loo={f['leaveOneOutMin']} {'PASS' if f['passes'] else 'fail ' + ','.join(k for k, v in f['checks'].items() if not v)}")


if __name__ == "__main__":
    main()
