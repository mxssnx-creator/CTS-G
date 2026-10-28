#!/usr/bin/env python3
"""Turns sim12h.json into the hourly report: a self-contained HTML page (inline SVG charts, themed), hourly.csv, trades.csv.
usage: sim12h_report.py --in sim12h.json --out DIR"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import time


def fmt_pf(v):
    return "—" if v is None else ("no loss" if v >= 99 else f"{v:.2f}")


def scale(lo, hi, a, b):
    span = (hi - lo) or 1.0
    return lambda v: a + (v - lo) / span * (b - a)


def nice(lo, hi, n=4):
    import math
    span = (hi - lo) or 1.0
    mag = 10 ** math.floor(math.log10(span / n))
    step = mag
    for m in (1, 2, 2.5, 5, 10):
        step = mag * m
        if span / step <= n:
            break
    v = math.floor(lo / step) * step
    ticks = []
    while v <= hi + 1e-9:
        ticks.append(round(v, 10))
        v += step
    return ticks


W, H, PL, PR, PT, PB = 640, 230, 46, 26, 14, 30


def chart(title, sub, series, t0, t1, ylab="", bars=None, ymin=None, ymax=None, ref=None, area=None, legend=True):
    """series: [(name, css var, [(t, v)], 'line'|'step')]; bars: [(name, css var, [(t, v)])] drawn behind as columns per hour."""
    vals = [v for _, _, pts, _ in series for _, v in pts if v is not None]
    if bars:
        vals += [v for _, _, pts in bars for _, v in pts]
    if ref is not None:
        vals.append(ref)
    lo = min(vals) if ymin is None else ymin
    hi = max(vals) if ymax is None else ymax
    if hi - lo < 1e-9:
        hi = lo + 1
    pad = (hi - lo) * 0.06
    lo2, hi2 = (lo if ymin is not None else lo - pad), (hi if ymax is not None else hi + pad)
    sx = scale(t0, t1, PL, W - PR)
    sy = scale(lo2, hi2, H - PB, PT)
    o = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(title)}">']
    for tk in nice(lo2, hi2):
        y = sy(tk)
        if PT - 1 <= y <= H - PB + 1:
            o.append(f'<line class="grid" x1="{PL}" x2="{W - PR}" y1="{y:.1f}" y2="{y:.1f}"/><text class="tick" x="{PL - 6}" y="{y + 3:.1f}" text-anchor="end">{tk:g}</text>')
    for k in range(0, int((t1 - t0) // 3600) + 1, 2):
        x = sx(t0 + k * 3600)
        o.append(f'<text class="tick" x="{x:.1f}" y="{H - 10}" text-anchor="middle">{time.strftime("%H:%M", time.gmtime(t0 + k * 3600))}</text>')
    o.append(f'<line class="axis" x1="{PL}" x2="{W - PR}" y1="{H - PB}" y2="{H - PB}"/>')
    if ref is not None:
        o.append(f'<line class="ref" x1="{PL}" x2="{W - PR}" y1="{sy(ref):.1f}" y2="{sy(ref):.1f}"/>')
    if bars:
        bw = (sx(t0 + 3600) - sx(t0)) / (len(bars) + 0.6) * 0.85
        for bi, (name, var, pts) in enumerate(bars):
            for t, v in pts:
                x = sx(t) + bi * bw + 2
                y0, y1 = sy(0), sy(v)
                o.append(f'<rect x="{x:.1f}" y="{min(y0, y1):.1f}" width="{max(bw - 2, 1):.1f}" height="{abs(y1 - y0):.1f}" rx="2" style="fill:var({var})"><title>{html.escape(name)} {time.strftime("%H:%M", time.gmtime(t))}: {v:g}</title></rect>')
    if area:
        pts = area
        d = f'M{sx(pts[0][0]):.1f},{sy(0):.1f} ' + ' '.join(f'L{sx(t):.1f},{sy(v):.1f}' for t, v in pts) + f' L{sx(pts[-1][0]):.1f},{sy(0):.1f}Z'
        o.append(f'<path d="{d}" class="area"/>')
    for name, var, pts, kind in series:
        pts = [(t, v) for t, v in pts if v is not None]
        if not pts:
            continue
        if kind == "step":
            d = f'M{sx(pts[0][0]):.1f},{sy(pts[0][1]):.1f}' + ''.join(f' L{sx(t):.1f},{sy(pts[i][1]):.1f} L{sx(t):.1f},{sy(v):.1f}' for i, (t, v) in enumerate(pts[1:]))
        else:
            d = 'M' + ' L'.join(f'{sx(t):.1f},{sy(v):.1f}' for t, v in pts)
        o.append(f'<path d="{d}" class="ln" style="stroke:var({var})"/>')
        t, v = pts[-1]
        o.append(f'<circle cx="{sx(t):.1f}" cy="{sy(v):.1f}" r="3.5" style="fill:var({var})"/>')
    o.append('</svg>')
    leg = ''
    if legend:
        items = [(n, v) for n, v, _, _ in series] + [(n, v) for n, v, _ in (bars or [])]
        leg = '<div class="leg">' + ''.join(f'<span><i style="background:var({v})"></i>{html.escape(n)}</span>' for n, v in items) + '</div>'
    return f'<figure><figcaption><b>{html.escape(title)}</b><span>{html.escape(sub)}</span></figcaption>{"".join(o)}{leg}</figure>'


CSS = """
:root{--bg:#f6f7f9;--surface:#ffffff;--ink:#14171c;--ink2:#4a5260;--mute:#7a8493;--rule:#dfe3ea;--good:#17803d;--bad:#c2331f;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#e87ba4;--area:rgba(42,120,214,.12)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1216;--surface:#171b21;--ink:#eceff4;--ink2:#b4bcc9;--mute:#8791a1;--rule:#2a303a;--good:#4cc27a;--bad:#ef7a68;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#d55181;--area:rgba(57,135,229,.16);color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0f1216;--surface:#171b21;--ink:#eceff4;--ink2:#b4bcc9;--mute:#8791a1;--rule:#2a303a;--good:#4cc27a;--bad:#ef7a68;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#d55181;--area:rgba(57,135,229,.16);color-scheme:dark}
body{background:var(--bg);color:var(--ink);font:14px/1.5 "IBM Plex Sans",system-ui,sans-serif;padding-block:28px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
main{max-width:1180px;margin:0 auto;display:flex;flex-direction:column;gap:28px}
h1{font:600 26px/1.2 "IBM Plex Sans",system-ui,sans-serif;margin:0;text-wrap:balance}
h2{font:600 16px/1.3 "IBM Plex Sans",system-ui,sans-serif;margin:0 0 10px}
p.lede{margin:6px 0 0;color:var(--ink2);max-width:75ch}
.mono,td,th,.tick,.kpi b{font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kpi{background:var(--surface);border:1px solid var(--rule);border-radius:8px;padding:12px 14px;min-width:0}
.kpi span{display:block;color:var(--mute);font-size:12px;text-transform:uppercase;letter-spacing:.06em}
.kpi b{display:block;font-size:20px;margin-top:2px}.kpi small{color:var(--mute);display:block}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,480px),1fr));gap:16px}
figure{margin:0;background:var(--surface);border:1px solid var(--rule);border-radius:8px;padding:12px 14px 8px;min-width:0}
figcaption{display:flex;flex-direction:column;gap:2px;margin-bottom:6px}figcaption span{color:var(--mute);font-size:12px}
svg{width:100%;height:auto;display:block}.grid{stroke:var(--rule);stroke-width:1}.axis{stroke:var(--mute);stroke-width:1}
.ref{stroke:var(--mute);stroke-width:1;stroke-dasharray:4 4}.tick{fill:var(--mute);font-size:10.5px}
.ln{fill:none;stroke-width:2;stroke-linejoin:round}.area{fill:var(--area)}
.leg{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--ink2);padding:2px 0 4px}.leg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px}
.tbl{overflow-x:auto;background:var(--surface);border:1px solid var(--rule);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:12.5px}th,td{padding:6px 9px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--rule)}
th{color:var(--mute);font-weight:500;position:sticky;top:0;background:var(--surface)}td:first-child,th:first-child{text-align:left}
.pos{color:var(--good)}.neg{color:var(--bad)}
details summary{cursor:pointer;color:var(--ink2);margin-bottom:8px}.note{color:var(--ink2);font-size:13px;max-width:95ch}
a{color:var(--s1)}
"""


def build(d, out):
    tot, hr = d["total"], d["hourly"]
    st = tot["settings"]
    t0 = hr[0]["t"]
    t1 = hr[-1]["t"] + 3600
    ev = d["evals15"]
    eqpts = [(t0, st["balance"])] + [(r[0], r[1]) for r in d["equity"]] + [(t1, hr[-1]["equity"])]
    eqalt = [(t0, st["balance"])] + [(r[0], r[2]) for r in d["equity"]] + [(t1, hr[-1]["equity_alt"])]
    ddp = [(t0, 0.0)] + [(r[0], -r[4]) for r in d["equity"]] + [(t1, -hr[-1]["dd_pct"])]

    def hp(key, cap=6.0):
        return [(r["t"] + 3600, None if r[key] is None else min(cap, r[key])) for r in hr]
    k = lambda label, val, sub="": f'<div class="kpi"><span>{label}</span><b>{val}</b><small>{sub}</small></div>'
    last = hr[-1]
    kp = ''.join([
        k("Equity", f'{tot["equity_end"]:.3f}', f'from {st["balance"]:g} · {tot["equity_end"] - st["balance"]:+.3f} at {st["notional"]:g} per order'),
        k("Max drawdown", f'{tot["max_dd_pct"]:.2f}%', f'{tot["max_dd_usd"]:.3f} from peak · longest {tot["equity_ddt_h"]:g} h under water'),
        k("PF executed", fmt_pf(tot["pf_exec"]), f'{tot["trades"]} positions · win rate {tot["win_rate"]}%'),
        k("PF without Axis", fmt_pf(tot["pf_exec_no_axis"]), f'{tot["trades_no_axis"]} positions'),
        k("PF after Base", fmt_pf(last["pf_post"]), f'{last["sets_valid"]} of {last["sets_total"]} Sets valid ({last["validated_pct"]}%) · {last["orders_post"]} orders · last hour'),
        k("PF before Base", fmt_pf(last["pf_pre"]), f'{last["orders_pre"]} orders (every Set) · by design under 1'),
        k("Block legs PF", fmt_pf(tot["block_pf"]), f'{tot["block_legs"]} legs · idle in the shipped config'),
        k("DCA adds PF", fmt_pf(tot["dca_pf"]), f'{tot["dca_adds"]} adds · off in the shipped config'),
    ])
    ch = []
    ch.append(chart("Equity", f"balance {st['balance']:g}; {st['notional']:g} per order (x02) and {st['alt_notional']:g} (x01)", [
        ("x02 size", "--s1", eqpts, "step"), ("x01 size", "--s2", eqalt, "step")], t0, t1, ref=st["balance"]))
    ch.append(chart("Equity drawdown", "percent below the running peak (x02 size)", [("drawdown %", "--s2", ddp, "step")], t0, t1, ymax=0, area=None))
    ch.append(chart("PF before and after Base", "pooled over the gate window, every 15 minutes (capped at 6)", [
        ("before Base (every Set)", "--s2", [(e["t"], e["pf_pre"]) for e in ev], "line"),
        ("after Base (valid Sets)", "--s1", [(e["t"], None if e["pf_post"] is None else min(6.0, e["pf_post"])) for e in ev], "line")], t0, t1, ref=1.0))
    ch.append(chart("PF of the executed positions", "hour, cumulative and last 15 (capped at 6; no loss shows at the cap)", [
        ("cumulative", "--s1", hp("cum_pf"), "line"), ("last 15", "--s3", hp("last15_pf"), "line"), ("hour", "--s2", hp("hour_pf"), "line")], t0, t1, ref=1.0))
    ch.append(chart("Sets", "valid Sets (left) over all 1174; validated percent in the table", [
        ("valid Sets", "--s1", [(e["t"], e["valid"]) for e in ev], "line"), ("all Sets", "--s2", [(e["t"], e["total"]) for e in ev], "line")], t0, t1, ymin=0))
    ch.append(chart("Validated share", "percent of Sets that pass Base, every 15 minutes", [
        ("validated %", "--s3", [(e["t"], e["validated_pct"]) for e in ev], "line")], t0, t1, ymin=0, ymax=100))
    ch.append(chart("Positions per hour", "opened and closed", [], t0, t1, ymin=0, bars=[
        ("opened", "--s1", [(r["t"], r["opened"]) for r in hr]), ("closed", "--s3", [(r["t"], r["closed"]) for r in hr])]))
    ch.append(chart("Win rate", "percent of the hour's closes with a positive net", [
        ("win rate %", "--s3", [(r["t"] + 1800, r["win_rate"]) for r in hr], "line")], t0, t1, ymin=0, ymax=100, ref=50))
    ch.append(chart("Drawdown time of valid Sets", "hours, mean and longest (the limit is %g h)" % st["max_ddt_h"], [
        ("mean", "--s1", [(e["t"], e["ddt_mean_h"]) for e in ev], "line"), ("longest", "--s2", [(e["t"], e["ddt_max_h"]) for e in ev], "line")], t0, t1, ymin=0))
    ch.append(chart("Add-on lanes and Axis", "cumulative PF per lane (capped at 6); each is scored on its own results", [
        ("with Axis (executed)", "--s1", hp("cum_pf"), "line"), ("without Axis", "--s2", hp("noaxis_pf_cum"), "line"),
        ("Block legs", "--s3", hp("block_pf_cum"), "line"), ("DCA adds", "--s4", hp("dca_pf_cum"), "line")], t0, t1, ref=1.0))
    cols = [("hour", "Hour (UTC)"), ("sets", "Sets valid / all"), ("validated_pct", "Valid %"), ("pf_pre", "PF before Base"), ("pf_post", "PF after Base"),
            ("orders_post", "Orders (valid)"), ("ddt", "DDT mean / max h"), ("opened", "Opened"), ("closed", "Closed"), ("open_at_end", "Open at end"),
            ("orders", "Orders"), ("win", "Win %"), ("net", "Hour net %"), ("hour_pf", "Hour PF"), ("cum_pf", "Cum PF"), ("last15", "Last 15 PF"),
            ("eq", "Equity"), ("dd", "DD %"), ("blk", "Block PF"), ("dca", "DCA PF")]
    rows = []
    for r in hr:
        net = r["hour_net_pct"]
        rows.append("<tr>" + ''.join(f"<td>{c}</td>" for c in [
            r["hour"], f'{r["sets_valid"]} / {r["sets_total"]}', f'{r["validated_pct"]:.1f}', fmt_pf(r["pf_pre"]), fmt_pf(r["pf_post"]), r["orders_post"],
            "—" if r["ddt_mean_h"] is None else f'{r["ddt_mean_h"]:.1f} / {r["ddt_max_h"]:.1f}', r["opened"], r["closed"], r["open_at_end"], r["orders"],
            "—" if r["win_rate"] is None else f'{r["win_rate"]:.0f}', f'<span class="{"pos" if net > 0 else "neg" if net < 0 else ""}">{net:+.2f}</span>',
            fmt_pf(r["hour_pf"]), fmt_pf(r["cum_pf"]), fmt_pf(r["last15_pf"]), f'{r["equity"]:.3f}', f'{r["dd_pct"]:.2f}', fmt_pf(r["block_pf_cum"]),
            fmt_pf(r["dca_pf_cum"])]) + "</tr>")
    thead = "<tr>" + ''.join(f"<th>{c[1]}</th>" for c in cols) + "</tr>"
    trs = []
    for t in d["trades"]:
        trs.append(f'<tr><td>{time.strftime("%m-%d %H:%M", time.gmtime(t["t_in"]))}</td><td>{time.strftime("%m-%d %H:%M", time.gmtime(t["t"]))}</td><td>{t["symbol"]}</td><td>{html.escape(t["set"])}</td><td>{t["reason"]}</td><td class="{"pos" if t["net_pct"] > 0 else "neg"}">{t["net_pct"]:+.3f}</td></tr>')
    note = (f'Walk-forward on stored 1-minute history of {len(st["symbols"])} symbols: {st["pre_hours"]} h of pre-history, then {st["hours"]} simulated hours, '
            f'{st["sets"]} Sets from the {html.escape(st["overlay"])} settings, PositionCost {st["cost_pct"]}%. Each hour every Set is scored from its own tape (the gate: '
            f'at least {st["gate_min_orders"]} orders, PF over the last {st["gate_window"]} at or above {st["min_pf"]}, drawdown time under {st["max_ddt_h"]:g} h; checked against the engine on {st["gate_check"]} samples), '
            f'valid Sets are ranked and positions open under the best one and leave by its own lane. Axis is the coordinator gate on the executed closes (last 4 PF at least 1.2, pause after 8 losses in a row). '
            f'Block and DCA are shown as add-on lanes on those positions (Block: up to 6 legs, ratio 1; DCA: the auto plan), each scored on its own legs or adds; both are off or idle in the shipped config. '
            f'Twelve hours and a few dozen positions are a small sample: read the shape, not the decimals.')
    body = f"""<title>12 Hour Simulation</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;600&display=swap">
<style>{CSS}</style>
<main>
<header><h1>12 hours, 12 symbols, hour by hour</h1><p class="lede">{time.strftime("%b %d %H:%M", time.gmtime(t0))} to {time.strftime("%b %d %H:%M", time.gmtime(t1))} UTC, balance {st["balance"]:g}. Post-Base PF, validated share and order counts come first, the unfiltered PF is shown only for contrast.</p></header>
<section class="kpis">{kp}</section>
<p class="note">{note}</p>
<section><h2>Hourly, line by line</h2><div class="tbl"><table><thead>{thead}</thead><tbody>{''.join(rows)}</tbody></table></div></section>
<section><h2>Diagrams</h2><div class="grid2">{''.join(ch)}</div></section>
<section><details><summary>All {len(d["trades"])} positions</summary><div class="tbl"><table><thead><tr><th>Opened</th><th>Closed</th><th>Symbol</th><th>Set</th><th>Exit</th><th>Net %</th></tr></thead><tbody>{''.join(trs)}</tbody></table></div></details></section>
</main>"""
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, "sim12h.html"), "w").write(body)
    with open(os.path.join(out, "hourly.csv"), "w", newline="") as f:
        w = csv.writer(f)
        keys = [x for x in hr[0] if x not in ("by_pack", "t")]
        w.writerow(keys)
        for r in hr:
            w.writerow([r[x] for x in keys])
    with open(os.path.join(out, "trades.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["opened_utc", "closed_utc", "symbol", "set", "kind", "exit", "net_pct"])
        for t in d["trades"]:
            w.writerow([time.strftime("%F %T", time.gmtime(t["t_in"])), time.strftime("%F %T", time.gmtime(t["t"])), t["symbol"], t["set"], t["kind"], t["reason"], t["net_pct"]])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    build(json.load(open(a.inp)), a.out)
