#!/usr/bin/env python3
"""HTML report with heatmaps for scripts/window_grid.py: the Base long window x the Base short window, for each ongoing (Real) last N and each
Main static last N. Self-contained (inline CSS and JS, no external request), light and dark, the heatmaps rendered server-side so the page reads
without JS (the JS only switches tabs, sorts the table and shows the tooltip).

usage: window_grid_report.py GRID.json OUT.html"""
from __future__ import annotations

import html
import json
import math
import sys
import time
from typing import Any, Dict, List, Optional

METRICS = [
    ("exec_pf", "Executed PF", "pf", "{:.2f}", "The number that counts: the walk-forward PF of the positions the engine would have opened and closed under the Set's own lane."),
    ("eff_pf", "Effective PF (in-sample)", "pf", "{:.2f}", "PF of the last-N orders of the Sets that pass every gate, measured on the tapes they were selected on."),
    ("valid_pct", "Validated Sets %", "seq", "{:.1f}", "Share of the 1,538 Sets that pass every gate, mean over the test hours."),
    ("exec_orders", "Executed orders", "seq", "{:.0f}", "Positions opened and closed in the test window."),
    ("net_pct", "Net % (sum of position results)", "net", "{:+.1f}", "Sum of the net results of the executed positions (percent of one position's notional), after PositionCost."),
    ("pf1", "Executed PF, first 12 h", "pf", "{:.2f}", "Executed PF of the positions opened in the first half of the test window."),
    ("pf2", "Executed PF, second 12 h", "pf", "{:.2f}", "Executed PF of the positions opened in the second half of the test window."),
]
THIRDS = [("real_third", "real", "Ongoing (Real) last N", "Main static as shipped"), ("main_third", "main", "Main static last N", "Real as shipped")]
STRONG = 0.78                      # a cell this far along its scale takes the inverted ink


def val(c: Dict[str, Any], key: str) -> Optional[float]:
    if key == "pf1":
        return c["half1"]["pf"]
    if key == "pf2":
        return c["half2"]["pf"]
    v = c.get(key)
    return None if v is None else float(v)


def scale_t(kind: str, v: Optional[float], hi: float) -> Optional[float]:
    """Position on the colour scale, 0..1 along one arm; the sign says which arm of a diverging scale."""
    if v is None:
        return None
    if kind == "pf":
        return max(-1.0, min(1.0, math.log2(max(v, 0.25)) / 1.0))      # PF 0.5 -> -1, 2 -> +1, 1 -> 0
    if kind == "net":
        return max(-1.0, min(1.0, v / hi)) if hi else 0.0
    return max(0.0, min(1.0, v / hi)) if hi else 0.0                   # seq


def cell_style(kind: str, t: Optional[float]) -> str:
    if t is None:
        return ""
    a = abs(t)
    if kind == "seq":
        return f"--t:{a * 100:.0f}%;--k:var(--seq-hi);--m:var(--seq-lo)" + (";--ink:var(--on-strong)" if a > STRONG else "")
    return f"--t:{a * 100:.0f}%;--k:{'var(--hi)' if t >= 0 else 'var(--lo)'};--m:var(--mid)" + (";--ink:var(--on-strong)" if a > STRONG else "")


def esc(x: Any) -> str:
    return html.escape(str(x), quote=True)


def tip(c: Dict[str, Any], floor: int) -> str:
    wins = 100.0 * c["wins"] / c["exec_orders"] if c["exec_orders"] else 0.0
    parts = [f"Base long {c['long']}" + (f" / short {c['short_eff']}" if c["stages"] else " alone (stages off)")]
    if c["stages"]:
        parts.append(f"Main static {c['main_eff']} / Real {c['real_eff']}")
    parts += [f"executed PF {c['exec_pf']} on {c['exec_orders']} orders ({wins:.0f}% wins)",
              f"halves {c['half1']['pf']} ({c['half1']['orders']}) / {c['half2']['pf']} ({c['half2']['orders']})",
              f"effective PF {c['eff_pf']} (in-sample), validated {c['valid_pct']}%",
              f"net {c['net_pct']:+.1f}%, max drawdown {c['max_dd_pct']}%, hours positive {c['hours_pos']}"]
    if c.get("neg_lane_orders"):
        parts.append(f"{c['neg_lane_orders']} orders on the Negative lane")
    if c["exec_orders"] < floor:
        parts.append(f"under {floor} orders: not ranked")
    return " · ".join(parts)


def pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) if sx and sy else None


def fmt(v: Optional[float], f: str) -> str:
    return "–" if v is None else f.format(v)


def rank_cells(cells, third, floor):
    pool = [c for c in cells if c["stages"] and c.get(third) and c["exec_pf"] is not None and c["exec_orders"] >= floor]
    return sorted(pool, key=lambda c: (c["exec_pf"], c["exec_orders"]), reverse=True)


def render(data: Dict[str, Any]) -> str:
    meta, cells = data["meta"], data["cells"]
    floor = int(meta["order_floor"])
    longs, shorts = meta["longs"], meta["shorts"]
    ship = meta["shipped"]
    staged = [c for c in cells if c["stages"]]
    best = {t: (rank_cells(cells, t, floor) or [None])[0] for t, *_ in THIRDS}
    best_ids = {c["id"] for c in best.values() if c}
    shipped = next((c for c in staged if (c["long"], c["short"], c["main"], c["real"]) == (ship["long"], ship["short"], ship["main"], ship["real"])), None)
    scale_hi = {"exec_orders": max([c["exec_orders"] for c in cells] or [1]), "valid_pct": max([c["valid_pct"] for c in cells] or [1]),
                "net_pct": max([abs(c["net_pct"]) for c in cells] or [1])}
    start, end = meta["hours"][0], meta["hours"][-1] + 3600
    span = f"{time.strftime('%d %b %H:%M', time.gmtime(start))} – {time.strftime('%d %b %H:%M', time.gmtime(end))} UTC"

    # headline numbers and the reading
    ranked_all = sorted({c["id"]: c for t, *_ in THIRDS for c in rank_cells(cells, t, floor)}.values(), key=lambda c: (c["exec_pf"], c["exec_orders"]), reverse=True)
    eff = [c["eff_pf"] for c in staged if c["eff_pf"] is not None and c["exec_pf"] is not None]
    exe = [c["exec_pf"] for c in staged if c["eff_pf"] is not None and c["exec_pf"] is not None]
    r_ = pearson(eff, exe)
    above = [c for c in ranked_all if c["exec_pf"] >= 1.0]
    n_rank = len(ranked_all)
    exec_all = [c["exec_pf"] for c in staged if c["exec_pf"] is not None]
    reading = []
    for t, _, label, _ in THIRDS:
        b = best[t]
        if b:
            reading.append(f"<li><b>Best, {esc(label.lower())}:</b> Base long {b['long']} / short {b['short_eff']}, Main static {b['main_eff']}, Real {b['real_eff']}: "
                           f"executed PF <b>{b['exec_pf']}</b> on {b['exec_orders']} orders (halves {b['half1']['pf']} / {b['half2']['pf']}), effective PF {b['eff_pf']} (in-sample), "
                           f"{b['valid_pct']}% of the Sets validated.</li>")
    if shipped:
        reading.append(f"<li><b>Shipped setting</b> (long {ship['long']}, short {ship['short']}, Main {ship['main']}, Real {ship['real']}): executed PF <b>{shipped['exec_pf']}</b> "
                       f"on {shipped['exec_orders']} orders, effective PF {shipped['eff_pf']}, {shipped['valid_pct']}% validated.</li>")
    reading.append(f"<li><b>Spread:</b> executed PF runs from {min(exec_all):.2f} to {max(exec_all):.2f} over {len(exec_all)} cells; "
                   f"{len(above)} of {n_rank} cells with at least {floor} orders are at or above 1.00.</li>")
    if r_ is not None:
        reading.append(f"<li><b>In-sample against executed:</b> the effective PF averages {sum(eff) / len(eff):.2f}, the executed PF {sum(exe) / len(exe):.2f}; "
                       f"across the cells the two correlate at r = {r_:+.2f}. A window setting that lifts the effective PF is not thereby better executed.</li>")
    reading.append(f"<li><b>Noise:</b> one {len(meta['hours'])} h window and a few hundred orders per cell: a PF difference of 0.1 between neighbouring cells is inside the noise, "
                   f"and the best cell is chosen on this same window, so it is in-sample for that choice. Cells under {floor} orders (†) are not ranked.</li>")

    def matrix(third: str, mkey: str, kind: str, f: str, vary: str, value: int) -> str:
        sel = {(c["long"], c["short"]): c for c in staged if c.get(third) and c[vary] == value}
        out = ['<table class="hm" role="grid"><thead><tr><th class="corner" scope="col">long \\ short</th>']
        out += [f'<th scope="col">{s}</th>' for s in shorts] + ["</tr></thead><tbody>"]
        for L in longs:
            out.append(f'<tr><th scope="row">{L}</th>')
            for s in shorts:
                c = sel.get((L, s))
                if c is None:
                    out.append('<td class="na" title="short window at or over the long window: the long window itself">n/a</td>')
                    continue
                v = val(c, mkey)
                t = scale_t(kind, v, scale_hi.get(mkey, 1.0))
                cls = ["cell"]
                low = c["exec_orders"] < floor
                if low:
                    cls.append("low")
                if c["id"] in best_ids and best[third] is not None and c["id"] == best[third]["id"]:
                    cls.append("best")
                if shipped is not None and c["id"] == shipped["id"]:
                    cls.append("ship")
                badge = ("<i class='b' title='best'>★</i>" if "best" in cls else "") + ("<i class='s' title='shipped setting'>●</i>" if "ship" in cls else "")
                sec = f"{c['exec_orders']} orders" if mkey != "exec_orders" else f"PF {fmt(c['exec_pf'], '{:.2f}')}"
                best_attr = 'data-best="1"' if "best" in cls else ""
                out.append(f'<td class="{" ".join(cls)}" data-cell="{esc(c["id"])}" {best_attr} tabindex="0" '
                           f'style="{cell_style(kind, t)}" data-tip="{esc(tip(c, floor))}">'
                           f'<span class="v">{fmt(v, f)}{"†" if low else ""}</span><span class="n">{sec}</span>{badge}</td>')
            out.append("</tr>")
        out.append("</tbody></table>")
        return "".join(out)

    panels = []
    for third, vary, label, fixed in THIRDS:
        values = meta["mains"] if vary == "main" else meta["reals"]
        for mkey, mlabel, kind, f, mdesc in METRICS:
            blocks = []
            for v in values:
                fixed_txt = f"Real {meta['real_default']}" if vary == "main" else f"Main static {meta['main_default']}"
                blocks.append(f'<figure class="mx"><figcaption><b>{esc(label)} {v}</b><span>{esc(fixed_txt)} · Base long (rows) × short (columns)</span></figcaption>'
                              f'{matrix(third, mkey, kind, f, vary, v)}</figure>')
            panels.append(f'<section class="panel" data-third="{vary}" data-metric="{mkey}" hidden><p class="mdesc">{esc(mdesc)}</p><div class="mxs">{"".join(blocks)}</div></section>')

    # reference row: Base long window alone
    alone = [c for c in cells if not c["stages"]]
    alone_html = "".join(
        f'<div class="chip" data-tip="{esc(tip(c, floor))}" tabindex="0"><b>long {c["long"]}</b><span>executed PF <b>{fmt(c["exec_pf"], "{:.2f}")}</b> · {c["exec_orders"]} orders</span>'
        f'<span>effective PF {fmt(c["eff_pf"], "{:.2f}")} · {c["valid_pct"]}% validated</span></div>' for c in alone)

    # table
    rows = []
    for c in sorted(cells, key=lambda c: (c["exec_pf"] is None, -(c["exec_pf"] or 0), -c["exec_orders"])):
        wins = 100.0 * c["wins"] / c["exec_orders"] if c["exec_orders"] else 0.0
        tag = "alone" if not c["stages"] else ("real+main" if c.get("real_third") and c.get("main_third") else ("real" if c.get("real_third") else "main"))
        low = c["exec_orders"] < floor
        rows.append(f'<tr class="{"low" if low else ""}{" best" if c["id"] in best_ids and any(b and b["id"] == c["id"] for b in best.values()) else ""}">'
                    f'<td data-v="{c["long"]}">{c["long"]}</td><td data-v="{c["short_eff"]}">{c["short_eff"] if c["stages"] else "–"}</td>'
                    f'<td data-v="{c["main_eff"] or 0}">{c["main_eff"] or "–"}</td><td data-v="{c["real_eff"] or 0}">{c["real_eff"] or "–"}</td>'
                    f'<td data-v="{c["exec_pf"] or 0}"><b>{fmt(c["exec_pf"], "{:.2f}")}</b></td><td data-v="{c["exec_orders"]}">{c["exec_orders"]}{"†" if low else ""}</td>'
                    f'<td data-v="{wins:.1f}">{wins:.0f}%</td><td data-v="{c["net_pct"]}">{c["net_pct"]:+.1f}</td><td data-v="{c["max_dd_pct"]}">{c["max_dd_pct"]}</td>'
                    f'<td data-v="{c["hours_pos"].split("/")[0]}">{c["hours_pos"]}</td><td data-v="{c["eff_pf"] or 0}">{fmt(c["eff_pf"], "{:.2f}")}</td>'
                    f'<td data-v="{c["valid_pct"]}">{c["valid_pct"]}</td><td data-v="{c["half1"]["pf"] or 0}">{fmt(c["half1"]["pf"], "{:.2f}")}</td>'
                    f'<td data-v="{c["half2"]["pf"] or 0}">{fmt(c["half2"]["pf"], "{:.2f}")}</td><td data-v="{c.get("neg_lane_orders", 0)}">{c.get("neg_lane_orders", 0)}</td>'
                    f'<td>{tag}</td></tr>')
    heads = ["Long", "Short", "Main", "Real", "Exec PF", "Orders", "Wins", "Net %", "Max DD %", "Hours +", "Eff PF", "Valid %", "PF 1st 12 h", "PF 2nd 12 h", "Neg lane", "Grid"]
    thead = "".join(f'<th scope="col" data-col="{i}" aria-sort="none"><button type="button">{esc(h)}</button></th>' for i, h in enumerate(heads))

    # hourly strips: the best five over both thirds, the shipped setting, and the best Base-alone window
    strip_cells: List[Dict[str, Any]] = ranked_all[:5]
    if shipped and shipped["id"] not in {c["id"] for c in strip_cells}:
        strip_cells.append(shipped)
    ba = [c for c in alone if c["exec_pf"] is not None]
    if ba:
        strip_cells.append(max(ba, key=lambda c: c["exec_pf"]))

    def strip(kind: str, key: str, title: str, f: str) -> str:
        hi = max([h[key] or 0 for c in strip_cells for h in c["hourly"]] or [1]) or 1
        lines = [f'<div class="strip"><h4>{esc(title)}</h4>']
        hrs = [h["hour"] for h in strip_cells[0]["hourly"]] if strip_cells else []
        lines.append('<div class="srow head"><span class="lab"></span><span class="cells">' + "".join(
            f'<span class="hc">{time.strftime("%H", time.gmtime(h))}</span>' for h in hrs) + "</span></div>")
        for c in strip_cells:
            label = (f"long {c['long']} / short {c['short_eff']} / M{c['main_eff']} / R{c['real_eff']}" if c["stages"] else f"long {c['long']} alone")
            if shipped and c["id"] == shipped["id"]:
                label += " (shipped)"
            cs = []
            for h in c["hourly"]:
                v = h[key]
                t = scale_t(kind, v, hi)
                txt = f"{time.strftime('%d %b %H:00', time.gmtime(h['hour']))} UTC · {h['orders']} orders · PF {fmt(h['pf'], '{:.2f}')} · valid {fmt(h['valid_pct'], '{:.1f}')}%"
                cs.append(f'<span class="hc cell" style="{cell_style(kind, t)}" data-tip="{esc(txt)}" tabindex="0"><span class="v">{fmt(v, f)}</span></span>')
            lines.append(f'<div class="srow"><span class="lab">{esc(label)}</span><span class="cells">{"".join(cs)}</span></div>')
        lines.append("</div>")
        return "".join(lines)

    strips = (strip("pf", "pf", "Executed PF per hour of the test window (blank: no order that hour)", "{:.1f}")
              + strip("seq", "orders", "Executed orders per hour", "{:.0f}")
              + strip("seq", "valid_pct", "Validated Sets % per hour", "{:.0f}"))

    third_tabs = "".join(f'<button type="button" class="tab" data-third="{v}" aria-pressed="{"true" if i == 0 else "false"}">{esc(l)} {" / ".join(str(x) for x in (meta["mains"] if v == "main" else meta["reals"]))}</button>'
                         for i, (_, v, l, _f) in enumerate(THIRDS))
    metric_tabs = "".join(f'<button type="button" class="tab" data-metric="{k}" aria-pressed="{"true" if i == 0 else "false"}">{esc(l)}</button>' for i, (k, l, *_r) in enumerate(METRICS))
    sc = meta.get("self_check") or []
    chk = (f"direct gate = engine on {sum(x['samples'] - x['bad'] for x in sc)}/{sum(x['samples'] for x in sc)} sampled (Set, hour) pairs" if sc else "self-check not run")
    created = time.strftime("%d %b %Y %H:%M UTC", time.gmtime(meta["created"]))
    data_json = json.dumps({"meta": {k: v for k, v in meta.items() if k != "symbols"}, "cells": [{k: v for k, v in c.items() if k != "hourly"} for c in cells]}, separators=(",", ":")).replace("</", "<\\/")
    return TEMPLATE.format(span=esc(span), n_sym=len(meta["symbols"]), pre=f"{meta['prehistory_h']:g}", n_hours=len(meta["hours"]), n_cells=len(cells),
                           floor=floor, min_orders=("off" if not meta["min_orders"] else meta["min_orders"]), ddt=f"{meta['max_ddt_h']:g}", min_pf=meta["min_pf"],
                           cost=meta["cost_pct"], sets=meta["sets"], reading="".join(reading), third_tabs=third_tabs, metric_tabs=metric_tabs,
                           panels="".join(panels), alone=alone_html, table_head=thead, table_rows="".join(rows), strips=strips, chk=esc(chk), created=esc(created),
                           data=data_json, symbols=esc(", ".join(meta["symbols"])), ship=f"long {ship['long']}, short {ship['short']}, Main {ship['main']}, Real {ship['real']}")


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Base window grid</title>
<style>
:root {{
  color-scheme: light dark;
  --bg:#fcfcfb; --surface:#ffffff; --line:#e3e2de; --t1:#0b0b0b; --t2:#52514e; --t3:#6f6e69;
  --hi:#256abf; --lo:#c93a39; --mid:#f0efec; --seq-lo:#e8f1fd; --seq-hi:#1c5cab; --on-strong:#ffffff; --focus:#2a78d6; --warn:#8a5a00;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --bg:#1a1a19; --surface:#222221; --line:#383835; --t1:#f0efec; --t2:#c3c2b7; --t3:#9d9c92;
    --hi:#3987e5; --lo:#e66767; --mid:#383835; --seq-lo:#262b33; --seq-hi:#5598e7; --on-strong:#0b0b0b; --focus:#6da7ec; --warn:#e0b050;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --bg:#1a1a19; --surface:#222221; --line:#383835; --t1:#f0efec; --t2:#c3c2b7; --t3:#9d9c92;
  --hi:#3987e5; --lo:#e66767; --mid:#383835; --seq-lo:#262b33; --seq-hi:#5598e7; --on-strong:#0b0b0b; --focus:#6da7ec; --warn:#e0b050;
}}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--t1); font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:24px 16px 64px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:32px 0 8px; }} h4 {{ font-size:13px; margin:16px 0 6px; color:var(--t2); font-weight:600; }}
.sub {{ color:var(--t2); margin:0 0 16px; }}
.card {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:12px 16px; }}
.card ul {{ margin:6px 0 0; padding-left:18px; }} .card li {{ margin:4px 0; }}
.facts {{ display:flex; flex-wrap:wrap; gap:6px 18px; color:var(--t2); font-size:13px; margin:8px 0 16px; }}
.bar {{ display:flex; flex-wrap:wrap; gap:8px; margin:12px 0; }}
.tab {{ font:inherit; font-size:13px; color:var(--t2); background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:6px 10px; cursor:pointer; }}
.tab[aria-pressed="true"] {{ color:var(--t1); border-color:var(--t2); font-weight:600; }}
.tab:focus-visible, .cell:focus-visible, .chip:focus-visible, th button:focus-visible {{ outline:2px solid var(--focus); outline-offset:2px; }}
.mdesc {{ color:var(--t2); margin:4px 0 10px; font-size:13px; }}
.mxs {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); gap:16px; }}
figure.mx {{ margin:0; }} figcaption {{ display:flex; flex-direction:column; margin-bottom:6px; }} figcaption span {{ color:var(--t3); font-size:12px; }}
table.hm {{ border-collapse:separate; border-spacing:2px; width:100%; table-layout:fixed; }}
table.hm th {{ font-weight:500; color:var(--t3); font-size:12px; padding:2px; text-align:center; }}
table.hm th.corner {{ text-align:left; font-size:11px; }}
td.cell {{ position:relative; text-align:center; padding:8px 2px; border-radius:6px; min-width:56px; cursor:default;
  background:color-mix(in oklab, var(--k) var(--t), var(--m)); color:var(--ink,var(--t1)); }}
td.na {{ text-align:center; color:var(--t3); font-size:11px; border-radius:6px; background:repeating-linear-gradient(135deg,transparent 0 5px,var(--line) 5px 6px); }}
.cell .v {{ display:block; font-weight:600; font-size:14px; font-variant-numeric:tabular-nums; }}
.cell .n {{ display:block; font-size:10.5px; opacity:.85; font-variant-numeric:tabular-nums; }}
.cell.low {{ outline:1px dashed var(--t3); outline-offset:-2px; }}
.cell.best {{ outline:3px solid var(--t1); outline-offset:-1px; }}
.cell i {{ position:absolute; top:1px; font-style:normal; font-size:10px; line-height:1; }} .cell i.b {{ left:3px; }} .cell i.s {{ right:3px; }}
.chips {{ display:flex; flex-wrap:wrap; gap:8px; }} .chip {{ background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:6px 10px; display:flex; flex-direction:column; font-size:12.5px; color:var(--t2); }}
.chip b:first-child {{ color:var(--t1); }}
.legend {{ display:flex; flex-wrap:wrap; gap:6px 18px; color:var(--t2); font-size:12px; margin:10px 0 0; align-items:center; }}
.ramp {{ display:inline-block; width:120px; height:10px; border-radius:5px; vertical-align:middle; margin:0 6px; }}
.ramp.div {{ background:linear-gradient(90deg,var(--lo),var(--mid),var(--hi)); }} .ramp.seq {{ background:linear-gradient(90deg,var(--seq-lo),var(--seq-hi)); }}
.wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:10px; background:var(--surface); }}
table.t {{ border-collapse:collapse; width:100%; font-size:12.5px; font-variant-numeric:tabular-nums; }}
table.t th, table.t td {{ padding:5px 8px; text-align:right; white-space:nowrap; border-bottom:1px solid var(--line); }}
table.t th button {{ all:unset; cursor:pointer; font-weight:600; color:var(--t2); }} table.t th[aria-sort="ascending"] button::after {{ content:" ▲"; }} table.t th[aria-sort="descending"] button::after {{ content:" ▼"; }}
table.t td:last-child, table.t th:last-child {{ text-align:left; }}
table.t tr.low td {{ color:var(--t3); }} table.t tr.best td {{ font-weight:600; background:color-mix(in oklab, var(--hi) 12%, transparent); }}
.strip {{ overflow-x:auto; }} .srow {{ display:flex; align-items:center; gap:8px; min-width:760px; margin:2px 0; }}
.srow .lab {{ flex:0 0 250px; font-size:12px; color:var(--t2); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.srow .cells {{ display:grid; grid-template-columns:repeat(24,1fr); gap:2px; flex:1; }}
.srow .hc {{ text-align:center; font-size:11px; padding:4px 0; border-radius:4px; color:var(--t3); }} .srow .hc.cell {{ min-width:0; padding:4px 0; color:var(--ink,var(--t1)); }}
.srow .hc .v {{ font-size:10.5px; font-weight:500; }}
.tip {{ position:fixed; z-index:10; max-width:min(420px,calc(100vw - 24px)); background:var(--t1); color:var(--bg); padding:8px 10px; border-radius:8px; font-size:12px; line-height:1.4; pointer-events:none; display:none; }}
.foot {{ color:var(--t3); font-size:12px; margin-top:28px; }}
@media (max-width:560px) {{ .mxs {{ grid-template-columns:1fr; }} td.cell {{ min-width:0; }} .cell .v {{ font-size:13px; }} }}
@media (forced-colors:active) {{ td.cell {{ border:1px solid CanvasText; }} .cell.best {{ outline:3px solid Highlight; }} }}
</style>
</head>
<body>
<main>
<h1>Base window grid · fresh {n_hours} h test</h1>
<p class="sub">Base long window × Base short window × ongoing (Real) last N, walked forward hour by hour like the live engine · {span}</p>
<div class="facts">
  <span>{n_sym} symbols (the live universe)</span><span>{pre} h pre-history before every hour</span><span>{n_cells} cells ({sets} Sets each)</span>
  <span>PositionCost {cost}%</span><span>minimum PF {min_pf}</span><span>minimum orders {min_orders}</span><span>drawdown time under {ddt} h</span><span>hold Auto off</span>
</div>
<section class="card" aria-label="Reading">
  <b>What the grid says</b>
  <ul>{reading}</ul>
</section>

<h2>Heatmaps</h2>
<div class="bar" role="group" aria-label="Third axis">{third_tabs}</div>
<div class="bar" role="group" aria-label="Metric">{metric_tabs}</div>
{panels}
<div class="legend">
  <span>★ best cell by executed PF (at least {floor} orders)</span><span>● shipped setting ({ship})</span><span>† under {floor} orders, not ranked</span><span>n/a short window at or over the long window</span>
  <span>PF scale <span class="ramp div"></span> 0.5 · 1.0 · 2.0</span><span>count / share scale <span class="ramp seq"></span> low · high</span>
</div>

<h2>Base long window alone (stages off)</h2>
<div class="chips">{alone}</div>

<h2>Hour by hour</h2>
<p class="sub">The best five cells over both grids, the shipped setting and the best Base-alone window. Executed PF per hour on a handful of orders swings widely: read the shape, not a single hour.</p>
{strips}

<h2>All cells</h2>
<div class="wrap"><table class="t" id="tbl"><thead><tr>{table_head}</tr></thead><tbody>{table_rows}</tbody></table></div>

<p class="foot">Executed PF is net of PositionCost over positions opened under the best valid Set per signal, closed by that Set's own lane; effective PF is the pooled PF of the last-N orders of the Sets that pass every gate, measured on the tapes they were selected on (in-sample).
Main static, the Main Auto window (5/10/15), the Negative lane and the Real stage are as shipped except where a column says otherwise; the engine clamps a window to the long window. {chk}. Symbols: {symbols}. Generated {created} by scripts/window_grid.py.</p>
</main>
<div class="tip" id="tip" role="tooltip"></div>
<script type="application/json" id="data">{data}</script>
<script>
(function () {{
  var state = {{ third: "real", metric: "exec_pf" }};
  function show() {{
    document.querySelectorAll(".panel").forEach(function (p) {{ p.hidden = !(p.dataset.third === state.third && p.dataset.metric === state.metric); }});
    document.querySelectorAll(".tab").forEach(function (b) {{
      var on = (b.dataset.third && b.dataset.third === state.third) || (b.dataset.metric && b.dataset.metric === state.metric);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    }});
  }}
  document.querySelectorAll(".tab").forEach(function (b) {{
    b.addEventListener("click", function () {{ if (b.dataset.third) state.third = b.dataset.third; if (b.dataset.metric) state.metric = b.dataset.metric; show(); }});
  }});
  show();
  var tip = document.getElementById("tip");
  function move(e, el) {{
    var r = el.getBoundingClientRect(), x = e && e.clientX ? e.clientX : r.left + r.width / 2, y = e && e.clientY ? e.clientY : r.bottom;
    tip.style.left = Math.max(8, Math.min(window.innerWidth - tip.offsetWidth - 8, x + 12)) + "px";
    tip.style.top = Math.max(8, Math.min(window.innerHeight - tip.offsetHeight - 8, y + 14)) + "px";
  }}
  document.addEventListener("mouseover", function (e) {{ var el = e.target.closest("[data-tip]"); if (el) {{ tip.textContent = el.dataset.tip; tip.style.display = "block"; move(e, el); }} }});
  document.addEventListener("mousemove", function (e) {{ var el = e.target.closest("[data-tip]"); if (el) move(e, el); }});
  document.addEventListener("mouseout", function (e) {{ if (e.target.closest("[data-tip]")) tip.style.display = "none"; }});
  document.addEventListener("focusin", function (e) {{ var el = e.target.closest("[data-tip]"); if (el) {{ tip.textContent = el.dataset.tip; tip.style.display = "block"; move(null, el); }} }});
  document.addEventListener("focusout", function () {{ tip.style.display = "none"; }});
  var tbl = document.getElementById("tbl"), heads = tbl.querySelectorAll("th");
  heads.forEach(function (th, i) {{
    th.querySelector("button").addEventListener("click", function () {{
      var asc = th.getAttribute("aria-sort") !== "ascending";
      heads.forEach(function (h) {{ h.setAttribute("aria-sort", "none"); }});
      th.setAttribute("aria-sort", asc ? "ascending" : "descending");
      var rows = Array.prototype.slice.call(tbl.tBodies[0].rows);
      rows.sort(function (a, b) {{
        var x = a.cells[i].dataset.v, y = b.cells[i].dataset.v;
        var r = x !== undefined && y !== undefined ? parseFloat(x) - parseFloat(y) : a.cells[i].textContent.localeCompare(b.cells[i].textContent);
        return asc ? r : -r;
      }});
      rows.forEach(function (r) {{ tbl.tBodies[0].appendChild(r); }});
    }});
  }});
}})();
</script>
</body>
</html>
"""


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    data = json.load(open(sys.argv[1]))
    open(sys.argv[2], "w", encoding="utf-8").write(render(data))
    print("wrote", sys.argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main())
