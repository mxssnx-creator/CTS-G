#!/usr/bin/env python3
"""Base window grid: the long last N (setGateWindow) x the short last N (setGateShortN), each cell a full trade_sim run on the same
data and tapes, with an HTML report (heatmaps, hourly results, diagrams).

The standard test (owner, 2026-10-29): 20 symbols, the last 24 hours simulated, 24 hours of pre-history before them (the Sets'
lookback, histLookbackBars 1440). Per cell it reads what a validation is worth on the orders that follow it (the next 15 and next
25 orders of every validated Set) next to the in-sample effective PF and the executed walk-forward (per-order validation).

usage: window_grid.py --data DIR --work DIR [--long 20,30,40,50] [--short 0,5,10,15,20,25] [--next 15,25] [--hours 24]
                      [--pre-hours 24] [--jobs 4] [--patch JSON] [--html reports/window_grid.html]
Cells already in --work are reused (a run can be resumed); delete the work dir to start over.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))


def run_cell(a, long_n: int, short_n: int) -> dict:
    out = os.path.join(a.work, f"L{long_n}_S{short_n}")
    if not os.path.exists(out + ".json"):
        patch = {"holdEnabled": False, "histLookbackBars": int(a.pre_hours * 60), "histLookbackAuto": False,
                 "setGateWindow": long_n, "setGateShortN": short_n, **(json.loads(a.patch) if a.patch else {})}
        cmd = [sys.executable, "-B", os.path.join(HERE, "trade_sim.py"), "--data", a.data, "--cache", os.path.join(a.work, "shadow.pkl"),
               "--jobs", "2", "--eval-every", "3600", "--last-hours", str(a.hours), "--next", a.next, "--patch", json.dumps(patch),
               "--out", out]
        with open(out + ".txt", "w") as fh:
            p = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=ROOT)
        if p.returncode != 0 or not os.path.exists(out + ".json"):
            raise RuntimeError(f"cell L{long_n} S{short_n} failed, see {out}.txt")
    return json.load(open(out + ".json"))


def pf_txt(x) -> str:
    return "–" if x is None else ("∞" if x >= 99 else f"{x:.2f}")


def pf_color(x) -> str:
    """Diverging blue (PF above 1) / red (below 1) on a log scale, gray at 1; CSS variables so dark mode has its own steps."""
    if x is None:
        return "var(--cell-none)"
    v = max(-1.0, min(1.0, math.log(max(x, 1e-3)) / math.log(2.0)))      # PF 0.5 .. 2 maps to -1 .. 1
    k = min(4, int(abs(v) * 5))                                         # 0 = neutral, 1..4 = ramp steps
    return "var(--div-mid)" if k == 0 else f"var(--div-{'pos' if v > 0 else 'neg'}-{k})"


def seq_color(x, hi) -> str:
    if not x or not hi:
        return "var(--cell-none)"
    k = 1 + min(4, int(4.999 * x / hi))
    return f"var(--seq-{k})"


def score(c) -> float:
    """The cell ranking: the geometric mean of the executed PF and the PF of the next 15 and next 25 orders after a validation
    (each capped at 5), only with at least 100 executed orders and 200 next-25 orders behind it; else 0."""
    a = c["all"]
    n15, n25 = a["next"].get("15", {}), a["next"].get("25", {})
    if a["orders"] < 100 or (n25.get("orders") or 0) < 200:
        return 0.0
    vals = [min(5.0, v or 0.0) for v in (a["exec_pf"], n15.get("pf"), n25.get("pf"))]
    return round(math.exp(sum(math.log(max(v, 1e-6)) for v in vals) / 3), 4) if all(vals) else 0.0


CSS = """
.viz-root{color-scheme:light;--surface-1:#fcfcfb;--page:#f9f9f7;--text-primary:#0b0b0b;--text-secondary:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--cell-none:#f0efec;--div-mid:#f0efec;
--div-pos-1:#cde2fb;--div-pos-2:#86b6ef;--div-pos-3:#3987e5;--div-pos-4:#1c5cab;--div-neg-1:#fbd5d4;--div-neg-2:#f2a3a2;--div-neg-3:#e34948;--div-neg-4:#a82c2b;
--seq-1:#cde2fb;--seq-2:#9ec5f4;--seq-3:#6da7ec;--seq-4:#3987e5;--seq-5:#1c5cab;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--good:#006300;--bad:#d03b3b}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;--surface-1:#1a1a19;--page:#0d0d0d;
--text-primary:#f0efec;--text-secondary:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--cell-none:#383835;--div-mid:#383835;
--div-pos-1:#104281;--div-pos-2:#1c5cab;--div-pos-3:#2a78d6;--div-pos-4:#5598e7;--div-neg-1:#5c1f1f;--div-neg-2:#8c2a2a;--div-neg-3:#c43c3c;--div-neg-4:#e66767;
--seq-1:#104281;--seq-2:#184f95;--seq-3:#256abf;--seq-4:#3987e5;--seq-5:#6da7ec;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--good:#0ca30c;--bad:#e66767}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface-1:#1a1a19;--page:#0d0d0d;--text-primary:#f0efec;--text-secondary:#c3c2b7;
--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--cell-none:#383835;--div-mid:#383835;
--div-pos-1:#104281;--div-pos-2:#1c5cab;--div-pos-3:#2a78d6;--div-pos-4:#5598e7;--div-neg-1:#5c1f1f;--div-neg-2:#8c2a2a;--div-neg-3:#c43c3c;--div-neg-4:#e66767;
--seq-1:#104281;--seq-2:#184f95;--seq-3:#256abf;--seq-4:#3987e5;--seq-5:#6da7ec;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--good:#0ca30c;--bad:#e66767}
html,body{margin:0;background:var(--page)}
.viz-root{font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--text-primary);background:var(--page);padding:16px;max-width:1200px;margin:0 auto}
h1{font-size:22px;margin:4px 0 2px}h2{font-size:16px;margin:28px 0 8px}p.sub{color:var(--text-secondary);margin:0 0 12px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:12px;margin:8px 0;overflow-x:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,520px),1fr));gap:12px}
table{border-collapse:separate;border-spacing:2px;font-variant-numeric:tabular-nums}
th{font-weight:500;color:var(--text-secondary);font-size:12px;padding:4px 6px;text-align:right}
td{padding:4px 6px;text-align:right;font-size:12px}
.hm td.c{min-width:56px;border-radius:4px;color:var(--text-primary);font-weight:500}
.hm td.c.dark{color:#fff}
.list td,.list th{border-bottom:1px solid var(--grid)}
.list tr.best td{font-weight:600}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--text-secondary);margin:6px 0}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.note{color:var(--text-secondary);font-size:12px}
#tip{position:fixed;pointer-events:none;background:var(--surface-1);color:var(--text-primary);border:1px solid var(--border);border-radius:6px;
padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.15);display:none;z-index:9;white-space:pre}
svg text{fill:var(--muted);font-size:11px}
"""

JS = """
const tip=document.getElementById('tip');
document.addEventListener('mousemove',e=>{const t=e.target.closest('[data-tip]');if(!t){tip.style.display='none';return}
tip.textContent=t.getAttribute('data-tip');tip.style.display='block';
const x=Math.min(e.clientX+14,window.innerWidth-tip.offsetWidth-8),y=Math.min(e.clientY+14,window.innerHeight-tip.offsetHeight-8);
tip.style.left=x+'px';tip.style.top=y+'px'});
"""


def heatmap(title, cells, longs, shorts, value, color, fmt, note=""):
    rows = [f'<div class="card"><b>{html.escape(title)}</b><table class="hm"><tr><th>Base long \\ short</th>'
            + "".join(f"<th>{'off' if s == 0 else s}</th>" for s in shorts) + "</tr>"]
    for L in longs:
        tr = [f"<tr><th>{L}</th>"]
        for S in shorts:
            c = cells.get((L, S))
            v = value(c) if c else None
            col = color(v)
            dark = any(k in col for k in ("-3)", "-4)", "seq-4", "seq-5"))
            a = (c or {}).get("all", {})
            tipt = (f"long {L} · short {'off' if S == 0 else S}\\n{title}: {fmt(v)}\\nexecuted PF {pf_txt(a.get('exec_pf'))} on {a.get('orders', 0)} orders"
                    f"\\nnext 15 PF {pf_txt((a.get('next') or {}).get('15', {}).get('pf'))} · next 25 PF {pf_txt((a.get('next') or {}).get('25', {}).get('pf'))}"
                    f"\\nvalidated {a.get('valid_pct', 0)} % · effective PF {pf_txt(a.get('eff_pf'))}") if c else "not run"
            tr.append(f'<td class="c{" dark" if dark else ""}" style="background:{col}" data-tip="{html.escape(tipt)}">{fmt(v)}</td>')
        rows.append("".join(tr) + "</tr>")
    rows.append("</table>" + (f'<div class="note">{html.escape(note)}</div>' if note else "") + "</div>")
    return "".join(rows)


def line_chart(title, series, hours, w=560, h=220):
    """Cumulative net % per hour, one line per series (at most six: the categorical slots), legend + direct end labels."""
    pad_l, pad_r, pad_t, pad_b = 44, (70 if len(series) <= 4 else 12), 10, 24
    vals = [v for _, ys in series for v in ys]
    lo, hi = min(vals + [0.0]), max(vals + [0.0])
    if hi - lo < 1e-9:
        hi = lo + 1
    xs = lambda i: pad_l + (w - pad_l - pad_r) * i / max(1, len(hours) - 1)
    ys_ = lambda v: pad_t + (h - pad_t - pad_b) * (hi - v) / (hi - lo)
    out = [f'<div class="card"><b>{html.escape(title)}</b><div class="legend">'
           + "".join(f'<span><i style="background:var(--s{k + 1})"></i>{html.escape(n)}</span>' for k, (n, _) in enumerate(series))
           + f'</div><svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{html.escape(title)}">']
    for t in range(5):
        v = lo + (hi - lo) * t / 4
        out.append(f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{ys_(v):.1f}" y2="{ys_(v):.1f}" stroke="var(--grid)" stroke-width="1"/>'
                   f'<text x="{pad_l - 6}" y="{ys_(v) + 4:.1f}" text-anchor="end">{v:.1f}</text>')
    out.append(f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{ys_(0):.1f}" y2="{ys_(0):.1f}" stroke="var(--axis)" stroke-width="1"/>')
    for i in range(0, len(hours), max(1, len(hours) // 6)):
        out.append(f'<text x="{xs(i):.1f}" y="{h - 6}" text-anchor="middle">{time.strftime("%H:00", time.gmtime(hours[i]))}</text>')
    for k, (n, ys) in enumerate(series):
        pts = " ".join(f"{xs(i):.1f},{ys_(v):.1f}" for i, v in enumerate(ys))
        out.append(f'<polyline points="{pts}" fill="none" stroke="var(--s{k + 1})" stroke-width="2" stroke-linejoin="round"/>')
        if len(series) <= 4:                # direct end labels for a few lines; more lines are read from the legend
            out.append(f'<text x="{w - pad_r + 6}" y="{ys_(ys[-1]) + 4:.1f}">{html.escape(n)} {ys[-1]:+.1f}</text>')
        for i, v in enumerate(ys):
            out.append(f'<circle cx="{xs(i):.1f}" cy="{ys_(v):.1f}" r="7" fill="transparent" '
                       f'data-tip="{html.escape(n)} · {time.strftime("%m-%d %H:00", time.gmtime(hours[i]))} UTC\\ncumulative net {v:+.2f} %"/>')
    out.append("</svg></div>")
    return "".join(out)


def bars_chart(title, hourly, w=560, h=200):
    pad_l, pad_r, pad_t, pad_b = 44, 10, 10, 24
    vals = [x["net_pct"] for x in hourly]
    lo, hi = min(vals + [0.0]), max(vals + [0.0])
    if hi - lo < 1e-9:
        hi = lo + 1
    bw = (w - pad_l - pad_r) / max(1, len(hourly))
    ys_ = lambda v: pad_t + (h - pad_t - pad_b) * (hi - v) / (hi - lo)
    out = [f'<div class="card"><b>{html.escape(title)}</b><div class="legend"><span><i style="background:var(--s1)"></i>net ≥ 0</span>'
           f'<span><i style="background:var(--s2)"></i>net &lt; 0</span></div><svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{html.escape(title)}">']
    for t in range(5):
        v = lo + (hi - lo) * t / 4
        out.append(f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{ys_(v):.1f}" y2="{ys_(v):.1f}" stroke="var(--grid)"/>'
                   f'<text x="{pad_l - 6}" y="{ys_(v) + 4:.1f}" text-anchor="end">{v:.1f}</text>')
    for i, x in enumerate(hourly):
        v = x["net_pct"]
        y0, y1 = sorted((ys_(0), ys_(v)))
        tipt = (f'{time.strftime("%m-%d %H:00", time.gmtime(x["t"]))} UTC\\norders {x["orders"]} · wins {x["wins"]}\\nnet {v:+.2f} % · PF {pf_txt(x["exec_pf"])}'
                f'\\nvalidated {x["valid_pct"]} % · effective PF {pf_txt(x["eff_pf"])}')
        out.append(f'<rect x="{pad_l + i * bw + 1:.1f}" y="{y0:.1f}" width="{max(1.0, bw - 2):.1f}" height="{max(1.0, y1 - y0):.1f}" rx="2" '
                   f'fill="var(--s{1 if v >= 0 else 2})" data-tip="{html.escape(tipt)}"/>')
        if i % max(1, len(hourly) // 6) == 0:
            out.append(f'<text x="{pad_l + (i + .5) * bw:.1f}" y="{h - 6}" text-anchor="middle">{time.strftime("%H:00", time.gmtime(x["t"]))}</text>')
    out.append(f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{ys_(0):.1f}" y2="{ys_(0):.1f}" stroke="var(--axis)"/></svg></div>')
    return "".join(out)


def build_html(a, cells, longs, shorts) -> str:
    ranked = sorted(cells.items(), key=lambda kv: (score(kv[1]), kv[1]["all"]["exec_pf"] or 0), reverse=True)
    best_key, best = ranked[0]
    any_c = best
    hours = [x["t"] for x in any_c["hourly"]]
    span = f'{time.strftime("%Y-%m-%d %H:%M", time.gmtime(hours[0]))} – {time.strftime("%H:%M", time.gmtime(hours[-1] + 3600))} UTC'
    hi_orders = max(c["all"]["orders"] for c in cells.values()) or 1
    hi_valid = max(c["all"]["valid_pct"] for c in cells.values()) or 1
    nx = lambda m: (lambda c: (c["all"]["next"].get(m) or {}).get("pf"))
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Base Window Grid</title><style>{CSS}</style></head><body><div class='viz-root'>",
             f"<h1>Base window grid · {len(any_c['symbols'])} symbols · {a.hours} h simulated · {a.pre_hours} h pre-history</h1>",
             f"<p class='sub'>{span} · {any_c['sets']} Sets · every cell: Base long window × short window (both must pass), the rest at the "
             f"defaults (Main static {html.escape(str(any_c['gate'].get('main_n', '')))}, minimum PF {any_c['gate']['min_pf']}). "
             f"Best cell by the geometric mean of the executed PF and the next 15 / next 25 orders PF: <b>long {best_key[0]} · short "
             f"{'off' if best_key[1] == 0 else best_key[1]}</b> (executed PF {pf_txt(best['all']['exec_pf'])} on {best['all']['orders']} orders, "
             f"next 15 {pf_txt(nx('15')(best))}, next 25 {pf_txt(nx('25')(best))}).</p>",
             "<div class='legend'><span><i style='background:var(--div-neg-4)'></i>PF ≤ 0.5</span><span><i style='background:var(--div-neg-2)'></i>PF &lt; 1</span>"
             "<span><i style='background:var(--div-mid)'></i>≈ 1</span><span><i style='background:var(--div-pos-2)'></i>PF &gt; 1</span>"
             "<span><i style='background:var(--div-pos-4)'></i>PF ≥ 2</span></div>",
             "<h2>Heatmaps</h2><div class='grid'>",
             heatmap("Executed PF (walk-forward, per-order validation)", cells, longs, shorts, lambda c: c["all"]["exec_pf"], pf_color, pf_txt),
             heatmap("Next 15 orders PF after a validation", cells, longs, shorts, nx("15"), pf_color, pf_txt),
             heatmap("Next 25 orders PF after a validation", cells, longs, shorts, nx("25"), pf_color, pf_txt),
             heatmap("Effective PF after Base (in-sample)", cells, longs, shorts, lambda c: c["all"]["eff_pf"], pf_color, pf_txt,
                     "measured on the orders the Sets were selected on: read it next to the executed PF"),
             heatmap("Validated Sets %", cells, longs, shorts, lambda c: c["all"]["valid_pct"], lambda v: seq_color(v, hi_valid), lambda v: "–" if v is None else f"{v:.1f}"),
             heatmap("Executed orders", cells, longs, shorts, lambda c: c["all"]["orders"], lambda v: seq_color(v, hi_orders), lambda v: "–" if v is None else str(v)),
             "</div>"]
    parts.append("<h2>Every cell</h2><div class='card'><table class='list'><tr><th>long</th><th>short</th><th>score</th><th>valid %</th><th>eff PF</th>"
                 "<th>next 15 PF</th><th>next 25 PF</th><th>exec PF</th><th>orders</th><th>net %</th><th>win %</th><th>blocks 50 ≥ 1</th></tr>")
    for (L, S), c in ranked:
        x = c["all"]
        parts.append(f"<tr class='{'best' if (L, S) == best_key else ''}'><td>{L}</td><td>{'off' if S == 0 else S}</td><td>{score(c):.3f}</td>"
                     f"<td>{x['valid_pct']:.2f}</td><td>{pf_txt(x['eff_pf'])}</td><td>{pf_txt(nx('15')(c))}</td><td>{pf_txt(nx('25')(c))}</td>"
                     f"<td>{pf_txt(x['exec_pf'])}</td><td>{x['orders']}</td><td style='color:var({'--good' if x['net_pct'] >= 0 else '--bad'})'>{x['net_pct']:+.2f}</td>"
                     f"<td>{x['win_pct'] if x['win_pct'] is not None else '–'}</td><td>{x['blocks50_pf1']}</td></tr>")
    parts.append("</table></div><h2>Hourly progress</h2><div class='grid'>")
    for L in longs:
        series = []
        for S in shorts[:6]:
            c = cells.get((L, S))
            if not c:
                continue
            cum, acc = [], 0.0
            for x in c["hourly"]:
                acc += x["net_pct"]
                cum.append(round(acc, 3))
            series.append((f"short {'off' if S == 0 else S}", cum))
        if series:
            parts.append(line_chart(f"Cumulative executed net % · long {L}", series, hours))
    parts.append(bars_chart(f"Best cell (long {best_key[0]} · short {'off' if best_key[1] == 0 else best_key[1]}) · net % per hour", best["hourly"]))
    parts.append("</div><h2>Best cell · hour by hour</h2><div class='card'><table class='list'><tr><th>hour UTC</th><th>valid %</th><th>eff PF</th>"
                 "<th>next 15 PF</th><th>next 25 PF</th><th>orders</th><th>wins</th><th>net %</th><th>exec PF</th></tr>")
    for x in best["hourly"]:
        parts.append(f"<tr><td>{time.strftime('%m-%d %H:00', time.gmtime(x['t']))}</td><td>{x['valid_pct']:.2f}</td><td>{pf_txt(x['eff_pf'])}</td>"
                     f"<td>{pf_txt((x.get('next') or {}).get('15'))}</td><td>{pf_txt((x.get('next') or {}).get('25'))}</td><td>{x['orders']}</td>"
                     f"<td>{x['wins']}</td><td style='color:var({'--good' if x['net_pct'] >= 0 else '--bad'})'>{x['net_pct']:+.2f}</td><td>{pf_txt(x['exec_pf'])}</td></tr>")
    parts.append("</table></div><h2>Best cell · by strategy type</h2><div class='card'><table class='list'><tr><th>type</th><th>sets</th><th>valid %</th>"
                 "<th>eff PF</th><th>next 15 PF</th><th>next 25 PF</th><th>exec PF</th><th>orders</th><th>net %</th></tr>")
    for x in best["types"]:
        parts.append(f"<tr><td style='text-align:left'>{html.escape(x['group'])}</td><td>{x['sets']}</td><td>{x['valid_pct']:.2f}</td><td>{pf_txt(x['eff_pf'])}</td>"
                     f"<td>{pf_txt((x['next'].get('15') or {}).get('pf'))}</td><td>{pf_txt((x['next'].get('25') or {}).get('pf'))}</td>"
                     f"<td>{pf_txt(x['exec_pf'])}</td><td>{x['orders']}</td><td>{x['net_pct']:+.2f}</td></tr>")
    parts.append(f"</table></div><p class='note'>scripts/window_grid.py · exits of the best cell {html.escape(json.dumps(best['exits']))} · "
                 f"PositionCost {best['config']['cost_pct']} % · the effective PF is in-sample; the executed and next-N PFs are what a validation carries.</p>"
                 f"<div id='tip'></div><script>{JS}</script></div></body></html>")
    return "".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--long", default="20,30,40,50")
    ap.add_argument("--short", default="0,5,10,15,20,25")
    ap.add_argument("--next", default="15,25")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--pre-hours", type=int, default=24)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--patch", default="")
    ap.add_argument("--html", default=os.path.join(ROOT, "reports", "window_grid.html"))
    a = ap.parse_args()
    os.makedirs(a.work, exist_ok=True)
    longs = [int(x) for x in a.long.split(",")]
    shorts = [int(x) for x in a.short.split(",")]
    t0 = time.time()
    first = (longs[0], shorts[0])
    cells = {first: run_cell(a, *first)}            # builds the shared tape cache once
    todo = [(L, S) for L in longs for S in shorts if (L, S) != first]
    with concurrent.futures.ThreadPoolExecutor(a.jobs) as ex:
        for key, c in zip(todo, ex.map(lambda k: run_cell(a, *k), todo)):
            cells[key] = c
            x = c["all"]
            print(f"long {key[0]:>3} short {key[1]:>3}: valid {x['valid_pct']:5.2f}% eff {pf_txt(x['eff_pf']):>5} next15 "
                  f"{pf_txt((x['next'].get('15') or {}).get('pf')):>5} next25 {pf_txt((x['next'].get('25') or {}).get('pf')):>5} "
                  f"exec {pf_txt(x['exec_pf']):>5} on {x['orders']} orders  [{time.time() - t0:.0f}s]", flush=True)
    os.makedirs(os.path.dirname(a.html), exist_ok=True)
    open(a.html, "w").write(build_html(a, cells, longs, shorts))
    json.dump({f"L{k[0]}_S{k[1]}": {"all": v["all"], "score": score(v)} for k, v in cells.items()},
              open(os.path.splitext(a.html)[0] + ".json", "w"), indent=1)
    print(f"html {a.html}  [{time.time() - t0:.0f}s]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
