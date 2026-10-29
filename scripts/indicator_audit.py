#!/usr/bin/env python3
"""Indicator x relation audit: every indicator name (indication_extra.INDICATORS) alone and with every relation of
indication_rel (Trend / Break / earlier move / last-part activity / volume / pullback / contraction / tactics), each also inverted,
on stored 1m history.

Per (name, variant) and horizon h (5 / 15 / 30 bars): fires, mean signed gross move (percent of price), hit rate, and the net PF of a
plain trade per fire (enter at the signal bar's close, leave h bars later, minus the PositionCost): sum of the positive nets over the
sum of the negative ones. That is not the engine's Set result (no stop, no TP, no Base gate) but the same for every variant, so it
ranks what the signal itself carries before any gate.

Selection bias: with tens of thousands of variants the best one is partly luck. The development window (the first two thirds of
the data; --sealed reads the last third once) is split in halves A and B: rank on A, read B. Only variants that hold on B mean
anything.

usage: indicator_audit.py --data DIR [--symbols 20] [--step 30] [--jobs 4] [--sealed] [--out reports/indicator_audit]
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
H = (5, 15, 30)
RELS = ("T5", "T15", "T30", "t5", "t15", "t30", "N5", "N15", "B5", "B15", "b5", "b15", "M30", "m30", "M60", "m60",
        "A", "a", "V", "v", "D", "d", "X", "C", "c", "G", "K", "F")


def _variants():
    """'' (the indicator alone), 'I' (inverted), each relation, and 'I@'+relation (inverted, then the relation on the faded side)."""
    return [""] + ["I"] + list(RELS) + ["I@" + r for r in RELS if r not in ("K", "F")]


def run_symbol(args):
    path, step, sealed, cost_pct = args
    import indication_extra as ix
    import indication_rel as rel
    raw = json.load(open(path))
    rows = [[float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in raw]
    names = sorted(ix.INDICATORS)
    win = {n: ix.window_of(n) for n in names}
    preds = {t: rel._relation(t)[0] for t in RELS if t not in ("K", "F")}
    variants = _variants()
    n_all = len(rows)
    split = n_all * 2 // 3
    lo, hi = (split, n_all - max(H) - 1) if sealed else (2000, split - max(H) - 1)
    mid = (lo + hi) // 2
    cf = cost_pct
    # acc[(name, variant)] = per half (A, B) per horizon: [n, sum move, hits, net gain, net loss]
    acc = {}
    big = 1920                                  # the longest history any name or relation reads (indication_lib.ROWS_CAP)
    for i in range(lo, hi, step):
        half = 0 if i < mid else 1
        c0 = rows[i][3]
        mv = [(rows[i + h][3] / c0 - 1.0) * 100.0 for h in H]
        slices = {}
        hist = rows[max(0, i + 1 - big): i + 1]
        relv = {}                                   # (token, d) -> bool, once per sample and direction
        for nm in names:
            w = win[nm]
            if w not in slices:
                slices[w] = rows[max(0, i + 1 - w): i + 1]
            try:
                sig = ix.INDICATORS[nm](slices[w])
            except Exception:                       # noqa: BLE001  (a raising indicator is counted as silent, as extra_votes does)
                sig = None
            if not sig:
                continue
            d0 = int(sig[0])
            prev_same = None
            for v in variants:
                toks = v.split("@") if v else []
                d = d0
                ok = True
                for t in toks:
                    if t == "I":
                        d = -d
                        continue
                    if t in ("K", "F"):
                        if prev_same is None:
                            pw = rows[max(0, i - w): i]
                            try:
                                ps = ix.INDICATORS[nm](pw)
                            except Exception:          # noqa: BLE001
                                ps = None
                            prev_same = bool(ps) and int(ps[0]) == d0
                        ok = prev_same if t == "K" else not prev_same
                    else:
                        key = (t, d)
                        if key not in relv:
                            relv[key] = bool(preds[t](hist, d, None, d0)[0])
                        ok = relv[key]
                    if not ok:
                        break
                if not ok:
                    continue
                a = acc.setdefault((nm, v), [[[0, 0.0, 0, 0.0, 0.0] for _ in H] for _ in (0, 1)])[half]
                for j, m in enumerate(mv):
                    x = d * m
                    net = x - cf
                    s = a[j]
                    s[0] += 1
                    s[1] += x
                    s[2] += 1 if x > 0 else 0
                    if net > 0:
                        s[3] += net
                    else:
                        s[4] -= net
    return os.path.basename(path)[:-5], {f"{k[0]}|{k[1]}": v for k, v in acc.items()}


def merge(parts):
    out = {}
    for _sym, acc in parts:
        for k, halves in acc.items():
            o = out.setdefault(k, [[[0, 0.0, 0, 0.0, 0.0] for _ in H] for _ in (0, 1)])
            for hh in (0, 1):
                for j in range(len(H)):
                    for q in range(5):
                        o[hh][j][q] += halves[hh][j][q]
    return out


def stat(s):
    n, sm, hits, g, l_ = s
    return {"n": n, "mv": round(sm / n, 4) if n else None, "hit": round(hits / n, 3) if n else None,
            "pf": (round(g / l_, 3) if l_ > 0 else (99.0 if g > 0 else None)) if n else None}


def both(o, j):
    """Halves A and B merged (the whole window) at horizon index j."""
    return stat([o[0][j][q] + o[1][j][q] for q in range(5)])


def pf_color(x):
    if x is None:
        return "var(--none)"
    v = max(-1.0, min(1.0, math.log(max(x, 1e-3)) / math.log(1.6)))
    k = min(4, int(abs(v) * 5))
    return "var(--mid)" if k == 0 else f"var(--{'pos' if v > 0 else 'neg'}{k})"


def html_report(res, meta, path):
    css = """
.r{color-scheme:light;--bg:#f9f9f7;--card:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--grid:#e1e0d9;--none:#f0efec;--mid:#f0efec;
--pos1:#cde2fb;--pos2:#86b6ef;--pos3:#3987e5;--pos4:#1c5cab;--neg1:#fbd5d4;--neg2:#f2a3a2;--neg3:#e34948;--neg4:#a82c2b}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .r{color-scheme:dark;--bg:#0d0d0d;--card:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--grid:#2c2c2a;--none:#383835;--mid:#383835;--pos1:#104281;--pos2:#1c5cab;--pos3:#2a78d6;--pos4:#5598e7;--neg1:#5c1f1f;--neg2:#8c2a2a;--neg3:#c43c3c;--neg4:#e66767}}
:root[data-theme="dark"] .r{color-scheme:dark;--bg:#0d0d0d;--card:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;--none:#383835;--mid:#383835;
--pos1:#104281;--pos2:#1c5cab;--pos3:#2a78d6;--pos4:#5598e7;--neg1:#5c1f1f;--neg2:#8c2a2a;--neg3:#c43c3c;--neg4:#e66767}
html,body{margin:0;background:var(--bg)}
.r{font:13px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:var(--bg);padding:16px;max-width:1400px;margin:0 auto}
h1{font-size:20px;margin:4px 0}h2{font-size:15px;margin:22px 0 8px}p{color:var(--ink2);margin:4px 0 10px}
.card{background:var(--card);border:1px solid var(--grid);border-radius:8px;padding:10px;margin:8px 0;overflow-x:auto}
table{border-collapse:separate;border-spacing:1px;font-variant-numeric:tabular-nums}
th{font-weight:500;color:var(--ink2);font-size:11px;padding:3px 5px;text-align:right;white-space:nowrap}
td{padding:3px 5px;text-align:right;font-size:11px;white-space:nowrap}td.l,th.l{text-align:left}
td.c{border-radius:3px;min-width:34px}
.list td{border-bottom:1px solid var(--grid)}
"""
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Indicator Audit</title><style>{css}</style></head><body><div class='r'>",
             f"<h1>Indicator × relation audit · {meta['symbols']} symbols · {meta['window']}</h1>",
             f"<p>{meta['names']} indicator names × {meta['variants']} variants (alone, inverted, each relation, inverted + relation) · one sample every "
             f"{meta['step']} bars · plain trade per fire: enter at the close, leave after h bars, minus PositionCost {meta['cost']} %. "
             f"Ranked on half A, read on half B: only what holds on B carries. Not the engine's Set result (no stop, TP or Base gate).</p>"]
    top = res["top"]
    parts.append(f"<h2>Best variants on half A (at least {meta['min_fires']} fires per half), read on half B</h2><div class='card'><table class='list'><tr>"
                 "<th class='l'>indicator @ variant</th><th>fires A</th><th>PF30 A</th><th>fires B</th><th>PF30 B</th><th>move30 B %</th>"
                 "<th>hit30 B</th><th>PF15 B</th><th>PF5 B</th><th>alone PF30 B</th></tr>")
    for r in top:
        parts.append(f"<tr><td class='l'>{html.escape(r['name'])}</td><td>{r['A']['n']}</td><td>{r['A']['pf']}</td><td>{r['B']['n']}</td>"
                     f"<td style='background:{pf_color(r['B']['pf'])}'>{r['B']['pf']}</td><td>{r['B']['mv']}</td><td>{r['B']['hit']}</td>"
                     f"<td>{r['B15']['pf']}</td><td>{r['B5']['pf']}</td><td>{r['alone_B']['pf']}</td></tr>")
    parts.append("</table></div>")
    rels = res["rel_cols"]
    parts.append("<h2>Heatmap: net PF after 30 bars, whole window, the most active indicators × every variant</h2><div class='card'><table><tr><th class='l'>indicator</th>"
                 + "".join(f"<th>{html.escape(v or 'alone')}</th>" for v in rels) + "</tr>")
    for nm in res["heat_rows"]:
        cells = []
        for v in rels:
            s = res["grid"].get(f"{nm}|{v}")
            pf = s["pf"] if s else None
            cells.append(f"<td class='c' style='background:{pf_color(pf)}' title='{html.escape(nm)}@{html.escape(v)} · fires {s['n'] if s else 0} · PF {pf}'>"
                         f"{'' if pf is None else ('∞' if pf >= 99 else f'{pf:.2f}')}</td>")
        parts.append(f"<tr><td class='l'>{html.escape(nm)}</td>{''.join(cells)}</tr>")
    parts.append("</table></div>")
    parts.append("<h2>Each relation over every indicator (pooled, whole window)</h2><div class='card'><table class='list'><tr><th class='l'>variant</th>"
                 "<th>fires</th><th>PF5</th><th>PF15</th><th>PF30</th><th>move30 %</th><th>hit30</th></tr>")
    for v, s in res["by_variant"]:
        parts.append(f"<tr><td class='l'>{html.escape(v or 'alone')}</td><td>{s[2]['n']}</td><td>{s[0]['pf']}</td><td>{s[1]['pf']}</td>"
                     f"<td style='background:{pf_color(s[2]['pf'])}'>{s[2]['pf']}</td><td>{s[2]['mv']}</td><td>{s[2]['hit']}</td></tr>")
    parts.append("</table></div><p>scripts/indicator_audit.py</p></div></body></html>")
    open(path, "w").write("".join(parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--step", type=int, default=30)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--sealed", action="store_true")
    ap.add_argument("--min-fires", type=int, default=150)
    ap.add_argument("--cost", type=float, default=0.12)
    ap.add_argument("--out", default=os.path.join(HERE, "..", "reports", "indicator_audit"))
    a = ap.parse_args()
    import indication_extra as ix
    files = sorted(f for f in os.listdir(a.data) if f.endswith(".json"))[: a.symbols]
    t0 = time.time()
    with ProcessPoolExecutor(a.jobs) as ex:
        parts = []
        for sym, acc in ex.map(run_symbol, [(os.path.join(a.data, f), a.step, a.sealed, a.cost) for f in files]):
            parts.append((sym, acc))
            print(f"{sym} done [{time.time() - t0:.0f}s]", flush=True)
    out = merge(parts)
    rows = []
    for k, o in out.items():
        nm, v = k.split("|", 1)
        A, B = stat(o[0][2]), stat(o[1][2])
        rows.append({"name": f"{nm}@{v}" if v else nm, "base": nm, "v": v, "A": A, "B": B, "B15": stat(o[1][1]), "B5": stat(o[1][0])})
    alone = {r["base"]: r for r in rows if r["v"] == ""}
    for r in rows:
        r["alone_B"] = alone.get(r["base"], {}).get("B", {"pf": None})
    elig = [r for r in rows if r["A"]["n"] >= a.min_fires and r["B"]["n"] >= a.min_fires and r["A"]["pf"] is not None]
    top = sorted(elig, key=lambda r: -r["A"]["pf"])[:60]
    variants = _variants()
    by_variant = []
    for v in variants:
        tot = [[0, 0.0, 0, 0.0, 0.0] for _ in H]
        for k, o in out.items():
            if k.split("|", 1)[1] == v:
                for j in range(len(H)):
                    for q in range(5):
                        tot[j][q] += o[0][j][q] + o[1][j][q]
        by_variant.append((v, [stat(t) for t in tot]))
    busiest = sorted(alone.values(), key=lambda r: -(r["A"]["n"] + r["B"]["n"]))[:45]
    grid = {k: both(o, 2) for k, o in out.items()}
    res = {"top": top, "rel_cols": variants, "heat_rows": [r["base"] for r in busiest], "grid": grid, "by_variant": by_variant}
    held = [r for r in top if (r["B"]["pf"] or 0) >= 1.0]
    meta = {"symbols": len(files), "window": "sealed third" if a.sealed else "development (two thirds), halves A / B", "names": len(ix.INDICATORS),
            "variants": len(variants), "step": a.step, "cost": a.cost, "min_fires": a.min_fires}
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    html_report(res, meta, a.out + ".html")
    json.dump({"meta": meta, "top": top, "by_variant": by_variant, "held_on_B": held}, open(a.out + ".json", "w"), indent=1)
    print(f"variants with {a.min_fires}+ fires in both halves: {len(elig)}; top 60 on A, of which {len(held)} hold PF30 >= 1 on B")
    for r in top[:25]:
        print(f"  {r['name']:42s} A {r['A']['n']:>5} PF {r['A']['pf']!s:>6}  B {r['B']['n']:>5} PF {r['B']['pf']!s:>6} move {r['B']['mv']}  alone B {r['alone_B']['pf']}")
    print("pooled by variant (PF30, fires):", [(v or "alone", s[2]["pf"], s[2]["n"]) for v, s in by_variant])
    print(f"done [{time.time() - t0:.0f}s]")


if __name__ == "__main__":
    main()
