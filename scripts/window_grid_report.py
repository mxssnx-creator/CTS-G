#!/usr/bin/env python3
"""HTML report with heatmaps for scripts/window_grid.py: the Base long window x the Base short window, for each ongoing (Real) last N and each
Main static last N. Self-contained (inline CSS and JS, no external request), light and dark, the heatmaps rendered server-side so the page reads
without JS (the JS only switches tabs, sorts the table and shows the tooltip).

usage: window_grid_report.py GRID.json [GRID2.json ...] OUT.html   (several files of the same test window are merged into one report)"""
from __future__ import annotations

import html
import json
import math
import sys
import time
from typing import Any, Dict, List, Optional

METRICS = [
    ("exec_pf", "Executed PF", "pf", "{:.2f}", "The number that counts: the walk-forward PF of the positions the engine would have opened and closed under the Set's own lane."),
    ("eff_pf", "Effective PF (in-sample, pooled)", "pf", "{:.2f}", "Gains over losses of ALL the long-window orders of the Sets that pass every gate, over all test hours (not a median of hourly figures), measured on the tapes they were selected on (in-sample)."),
    ("eff_orders", "Orders behind the effective PF", "seq", "{:.0f}", "The long-window orders of the valid Sets added over all test hours: the count the effective PF divides (an order that stays in a Set's window counts once per hour)."),
    ("valid_pct", "Validated Sets %", "seq", "{:.1f}", "Share of the 1,538 Sets that pass every gate, mean over the test hours."),
    ("exec_orders", "Executed orders", "seq", "{:.0f}", "Positions opened and closed in the test window."),
    ("net_mean", "Net % per position", "net", "{:+.3f}", "Mean net result of an executed position, percent of its notional, after PositionCost: the same net the PF divides (PF above 1 exactly when this is positive)."),
    ("net_pct", "Net % summed over positions", "net", "{:+.1f}", "Net result of all executed positions added up (percent of one position's notional), after PositionCost: the per-position net times the orders."),
    ("pf1", "Executed PF, first 12 h", "pf", "{:.2f}", "Executed PF of the positions opened in the first half of the test window."),
    ("pf2", "Executed PF, second 12 h", "pf", "{:.2f}", "Executed PF of the positions opened in the second half of the test window."),
    ("pos_pf", "Executed PF · positive lane", "pf", "{:.2f}", "Executed PF of the positions opened on the positive lane (Sets valid through Main and Real)."),
    ("neg_pf", "Executed PF · Negative lane", "pf", "{:.2f}", "Executed PF of the positions opened on the Real Negative lane (Main dips: the last Negative-window results below zero)."),
    ("neg_orders", "Orders on the Negative lane", "seq", "{:.0f}", "Positions opened on the Real Negative lane."),
]
THIRDS = [("real_third", "real", "Ongoing (Real) last N", "Main long as shipped"), ("main_third", "main", "Main long last N", "Real as shipped")]
STRONG = 0.78                      # a cell this far along its scale takes the inverted ink


def val(c: Dict[str, Any], key: str) -> Optional[float]:
    if key == "pf1":
        return c["half1"]["pf"]
    if key == "pf2":
        return c["half2"]["pf"]
    if key == "net_mean":
        return c.get("net_per_order_pct") if c.get("net_per_order_pct") is not None else (c["net_pct"] / c["exec_orders"] if c["exec_orders"] else 0.0)
    if key == "pos_pf":
        return (c.get("pos_lane") or {}).get("pf")
    if key == "neg_pf":
        return (c.get("neg_lane") or {}).get("pf")
    if key == "neg_orders":
        return float((c.get("neg_lane") or {}).get("orders", c.get("neg_lane_orders", 0)))
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
        parts.append(f"Main long {c['main_eff']} / Real {c['real_eff']}" + (f" / Negative windows {','.join(str(w) for w in c['neg_windows'])}" if c.get("neg_windows") else ""))
    parts += [f"executed PF {c['exec_pf']} on {c['exec_orders']} orders ({wins:.0f}% wins)",
              f"halves {c['half1']['pf']} ({c['half1']['orders']}) / {c['half2']['pf']} ({c['half2']['orders']})",
              f"effective PF {c['eff_pf']} on {c.get('eff_orders', '–')} orders (pooled over all hours, in-sample), validated {c['valid_pct']}%",
              f"net {val(c, 'net_mean'):+.3f}% per position ({c['net_pct']:+.1f}% summed), max drawdown {c['max_dd_pct']}%, hours positive {c['hours_pos']}"]
    if c.get("min_n"):
        parts.append(f"Base minimal window {min_label(c)} (the newest {c['min_n']} result(s) all {'positive' if c.get('min_mode') == 'positive' else 'negative'})")
    if c.get("main_min_n"):
        parts.append(f"Main minimal window {main_min_label(c)} (the newest {c['main_min_n']} Main result(s) all {'positive' if c.get('main_min_mode') == 'positive' else 'negative'})")
    if c.get("neg_lane"):
        parts.append(f"positive lane PF {c['pos_lane']['pf']} on {c['pos_lane']['orders']} orders, Negative lane PF {c['neg_lane']['pf']} on {c['neg_lane']['orders']}")
    elif c.get("neg_lane_orders"):
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
    """A number for display; 99 is the engine's PF of a window without a loss, shown as such rather than as 99.00."""
    if v is None:
        return "–"
    return "no loss" if f == "{:.2f}" and v >= 99.0 else f.format(v)


SIGN = {"positive": "+", "negative": "-", "both": "±"}


def min_label(c: Dict[str, Any]) -> str:
    """The Base minimal window of a cell: off, +N (the newest N results all positive) or -N (all negative)."""
    n = c.get("min_n") or 0
    return "off" if not n else f"{SIGN.get(c.get('min_mode'), '-')}{n}"


def main_min_label(c: Dict[str, Any]) -> str:
    """The Main minimal window of a cell, the same way."""
    n = c.get("main_min_n") or 0
    return "off" if not n else f"{SIGN.get(c.get('main_min_mode'), '-')}{n}"


def rank_cells(cells, third, floor, neg=None, neg_default="", mn=None, km=None):
    pool = [c for c in cells if c["stages"] and c.get(third) and c["exec_pf"] is not None and c["exec_orders"] >= floor
            and (neg is None or (c.get("neg") or neg_default) == neg) and (mn is None or min_label(c) == mn) and (km is None or main_min_label(c) == km)]
    return sorted(pool, key=lambda c: (c["exec_pf"], c["exec_orders"]), reverse=True)


WINDOW_ROWS = [("baseLong", "Base · long window"), ("baseShort", "Base · short window"), ("baseMinimal", "Base · minimal window"), ("mainLong", "Main · long window"),
               ("mainShort", "Main · short window"), ("mainMinimal", "Main · minimal window"), ("negative", "Main · Negative (dip) windows"), ("real", "Real · last N"),
               ("realNeg", "Real · Negative lane last N")]


def windows_table(c: Dict[str, Any], title: str) -> str:
    """The cell's last-N windows each on its own, pooled over every test hour: the Sets judged and passing per hour, the PF of all the orders the window read and
    how many they were. A window that is off reads off."""
    w = c.get("windows")
    if not w:
        return ""
    rows = []
    for key, label in WINDOW_ROWS:
        x = w.get(key)
        if not x:
            continue
        setting = "off" if not x["on"] else (" · ".join(str(i) for i in x["n"]) if isinstance(x["n"], list) else str(x["n"]))
        if not x["on"]:
            rows.append(f'<tr class="low"><td>{esc(label)}</td><td>off</td><td colspan="5">–</td></tr>')
            continue
        pf = x["pf"]
        rows.append(f'<tr><td>{esc(label)}</td><td>{esc(setting)}</td><td>{x["judged_per_hour"]:.0f}</td><td>{x["sets_per_hour"]:.1f}</td><td>{x["pct"]:.1f}%</td>'
                    f'<td><b>{fmt(pf, "{:.2f}")}</b></td><td>{x["orders"]}</td></tr>')
    return (f'<h4>{esc(title)}</h4><div class="wrap"><table class="t"><thead><tr><th>Window</th><th>Setting</th><th>Sets judged / h</th><th>Passing / h</th><th>% passing</th>'
            f'<th>PF of its orders</th><th>Orders (all hours)</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def render(data: Dict[str, Any]) -> str:
    meta, cells = data["meta"], data["cells"]
    floor = int(meta["order_floor"])
    longs, shorts = meta["longs"], meta["shorts"]
    ship = meta["shipped"]
    staged = [c for c in cells if c["stages"]]
    negs = meta.get("negs") or ["5,10"]
    neg_default = meta.get("neg_default") or negs[0]
    thirds = [t for t in THIRDS if (t[1] == "real" and meta["reals"]) or (t[1] == "main" and meta["mains"])]

    def cneg(c: Dict[str, Any]) -> str:
        return c.get("neg") or neg_default
    minimals = [m["label"] for m in (meta.get("minimals") or [{"label": "off"}])]
    kmins = [m["label"] for m in (meta.get("main_minimals") or [{"label": "off"}])]
    best = {(t[0], n, m, k): (rank_cells(cells, t[0], floor, n, neg_default, m, k) or [None])[0] for t in thirds for n in negs for m in minimals for k in kmins}
    best_ids = {c["id"] for c in best.values() if c}
    shipped = next((c for c in staged if (c["long"], c["short"], c["main"], c["real"], cneg(c), min_label(c), main_min_label(c)) == (ship["long"], ship["short"], ship["main"], ship["real"], neg_default, "off", "off")), None)
    scale_hi = {"exec_orders": max([c["exec_orders"] for c in cells] or [1]), "valid_pct": max([c["valid_pct"] for c in cells] or [1]),
                "net_pct": max([abs(c["net_pct"]) for c in cells] or [1]), "net_mean": max([abs(val(c, "net_mean") or 0.0) for c in cells] or [1]) or 1.0,
                "neg_orders": max([val(c, "neg_orders") or 0 for c in cells] or [1]) or 1.0}
    start, end = meta["hours"][0], meta["hours"][-1] + 3600
    span = f"{time.strftime('%d %b %H:%M', time.gmtime(start))} – {time.strftime('%d %b %H:%M', time.gmtime(end))} UTC"

    # headline numbers and the reading
    ranked_all = sorted({c["id"]: c for t in thirds for c in rank_cells(cells, t[0], floor)}.values(), key=lambda c: (c["exec_pf"], c["exec_orders"]), reverse=True)
    eff = [c["eff_pf"] for c in staged if c["eff_pf"] is not None and c["exec_pf"] is not None]
    exe = [c["exec_pf"] for c in staged if c["eff_pf"] is not None and c["exec_pf"] is not None]
    r_ = pearson(eff, exe)
    above = [c for c in ranked_all if c["exec_pf"] >= 1.0]
    n_rank = len(ranked_all)
    exec_all = [c["exec_pf"] for c in staged if c["exec_pf"] is not None]
    reading = []
    for t, _, label, _ in thirds:
      for nn in negs:
        b = (rank_cells(cells, t, floor, nn, neg_default) or [None])[0]
        if b:
            reading.append(f"<li><b>Best, {esc(label.lower())}, Negative windows {esc(nn)}:</b> Base long {b['long']} / short {b['short_eff']} / minimal {min_label(b)}, Main static {b['main_eff']}, Real {b['real_eff']}: "
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

    ratio = meta.get("short_max_ratio")
    na_title = (f"short window over {100 * ratio:g}% of the long window (setGateShortMaxRatio): not allowed" if ratio is not None and ratio < 1
                else "short window at or over the long window: the long window itself")

    def matrix(third: str, vary: str, value: int, neg: str, mn: str, km: str) -> str:
        """One heatmap: every cell carries the figures of all metrics (data-v, data-t) so that a tab only repaints it; the page shows the first metric without JS."""
        sel = {(c["long"], c["short"]): c for c in staged if c.get(third) and c[vary] == value and cneg(c) == neg and min_label(c) == mn and main_min_label(c) == km}
        mk0, _l0, kind0, f0, _d0 = METRICS[0]
        out = ['<table class="hm" role="grid"><thead><tr><th class="corner" scope="col">long \\ short</th>']
        out += [f'<th scope="col">{s}</th>' for s in shorts] + ["</tr></thead><tbody>"]
        for L in longs:
            out.append(f'<tr><th scope="row">{L}</th>')
            for s in shorts:
                c = sel.get((L, s))
                if c is None:
                    out.append(f'<td class="na" title="{esc(na_title)}">n/a</td>')
                    continue
                low = c["exec_orders"] < floor
                vs, ts = [], []
                for mk, _ml, kd, ff, _dd in METRICS:
                    v = val(c, mk)
                    vs.append(fmt(v, ff) + ("†" if low else ""))
                    t_ = scale_t(kd, v, scale_hi.get(mk, 1.0))
                    ts.append("" if t_ is None else f"{t_:.3f}")
                cls = ["cell"]
                if low:
                    cls.append("low")
                if best.get((third, neg, mn, km)) is not None and c["id"] == best[(third, neg, mn, km)]["id"]:
                    cls.append("best")
                if shipped is not None and c["id"] == shipped["id"]:
                    cls.append("ship")
                badge = ("<i class='b' title='best'>★</i>" if "best" in cls else "") + ("<i class='s' title='shipped setting'>●</i>" if "ship" in cls else "")
                best_attr = 'data-best="1"' if "best" in cls else ""
                v0 = val(c, mk0)
                out.append(f'<td class="{" ".join(cls)}" data-cell="{esc(c["id"])}" {best_attr} tabindex="0" data-v="{"|".join(vs)}" data-t="{"|".join(ts)}" '
                           f'style="{cell_style(kind0, scale_t(kind0, v0, scale_hi.get(mk0, 1.0)))}">'
                           f'<span class="v">{vs[0]}</span><span class="n">{c["exec_orders"]} orders</span>{badge}</td>')
            out.append("</tr>")
        out.append("</tbody></table>")
        return "".join(out)

    panels = []
    for third, vary, label, fixed in thirds:
        values = meta["mains"] if vary == "main" else meta["reals"]
        for nn in negs:
          for mn in minimals:
           for km in kmins:
            blocks = []
            for v in values:
                fixed_txt = f"Real {meta['real_default']}" if vary == "main" else f"Main long {meta['main_default']}"
                blocks.append(f'<figure class="mx"><figcaption><b>{esc(label)} {v}</b><span>{esc(fixed_txt)} · Negative windows {esc(nn)} · Base minimal {esc(mn)} · Main minimal {esc(km)} · Base long (rows) × short (columns)</span></figcaption>'
                              f'{matrix(third, vary, v, nn, mn, km)}</figure>')
            panels.append(f'<section class="panel" data-third="{vary}" data-neg="{esc(nn)}" data-min="{esc(mn)}" data-kmin="{esc(km)}" hidden><div class="mxs">{"".join(blocks)}</div></section>')
    metrics_json = json.dumps([{"key": k, "label": l, "kind": kd, "desc": d_, "fmt": ff} for k, l, kd, ff, d_ in METRICS], separators=(",", ":"))

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
                    f'<td data-v="{c["main_eff"] or 0}">{c["main_eff"] or "–"}</td><td data-v="{c["real_eff"] or 0}">{c["real_eff"] or "–"}</td><td>{esc(cneg(c)) if c["stages"] else "–"}</td><td>{esc(min_label(c)) if c["stages"] else "–"}</td><td>{esc(main_min_label(c)) if c["stages"] else "–"}</td>'
                    f'<td data-v="{c["exec_pf"] or 0}"><b>{fmt(c["exec_pf"], "{:.2f}")}</b></td><td data-v="{c["exec_orders"]}">{c["exec_orders"]}{"†" if low else ""}</td>'
                    f'<td data-v="{wins:.1f}">{wins:.0f}%</td><td data-v="{val(c, "net_mean")}">{val(c, "net_mean"):+.3f}</td><td data-v="{c["net_pct"]}">{c["net_pct"]:+.1f}</td><td data-v="{c["max_dd_pct"]}">{c["max_dd_pct"]}</td>'
                    f'<td data-v="{c["hours_pos"].split("/")[0]}">{c["hours_pos"]}</td><td data-v="{c["eff_pf"] or 0}">{fmt(c["eff_pf"], "{:.2f}")} ({c.get("eff_orders", "–")})</td>'
                    f'<td data-v="{c["valid_pct"]}">{c["valid_pct"]}</td><td data-v="{c["half1"]["pf"] or 0}">{fmt(c["half1"]["pf"], "{:.2f}")}</td>'
                    f'<td data-v="{c["half2"]["pf"] or 0}">{fmt(c["half2"]["pf"], "{:.2f}")}</td><td data-v="{val(c, "pos_pf") or 0}">{fmt(val(c, "pos_pf"), "{:.2f}")}</td>'
                    f'<td data-v="{val(c, "neg_pf") or 0}">{fmt(val(c, "neg_pf"), "{:.2f}")}</td><td data-v="{val(c, "neg_orders") or 0}">{val(c, "neg_orders") or 0:.0f}</td>'
                    f'<td>{tag}</td></tr>')
    heads = ["Long", "Short", "Main", "Real", "Neg win", "Min win", "Main min", "Exec PF", "Orders", "Wins", "Net / pos %", "Net Σ %", "Max DD %", "Hours +", "Eff PF (orders)", "Valid %", "PF 1st 12 h", "PF 2nd 12 h",
             "PF pos lane", "PF neg lane", "Neg orders", "Grid"]
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
            label = (f"long {c['long']} / short {c['short_eff']} / M{c['main_eff']} / R{c['real_eff']}" + (f" / N{cneg(c)}" if len(negs) > 1 else "") if c["stages"]
                     else f"long {c['long']} alone")
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
                         for i, (_, v, l, _f) in enumerate(thirds))
    neg_tabs = "".join(f'<button type="button" class="tab" data-neg="{esc(n)}" aria-pressed="{"true" if i == 0 else "false"}">Negative windows {esc(n)}</button>' for i, n in enumerate(negs)) if len(negs) > 1 else ""
    # marginals: the PF of ALL the orders of every cell that has this value of one dimension (gains over losses added over the cells: the complete, order-weighted
    # figure), next to the plain mean of the cells' PFs; the other settings are averaged out. A cell without the exact sums (an older grid file) falls back to its PF.
    def pooled(cs: List[Dict[str, Any]], gp_key: str, gl_key: str, pf_key: str) -> Optional[float]:
        gp = sum(c[gp_key] for c in cs if c.get(gp_key) is not None)
        gl = sum(c[gl_key] for c in cs if c.get(gl_key) is not None)
        if gl > 0 and all(c.get(gp_key) is not None for c in cs):
            return gp / gl
        vals = [c[pf_key] for c in cs if c.get(pf_key) is not None]
        return sum(vals) / len(vals) if vals else None

    def marginal(dim: str, values: List[Any], label: str, get) -> str:
        rows_ = []
        for v in values:
            cs = [c for c in staged if get(c) == v and c["exec_pf"] is not None]
            if not cs:
                continue
            n_ = sum(c["exec_orders"] for c in cs)
            e_n = sum(c.get("eff_orders") or 0 for c in cs)
            rows_.append(f"<tr><td>{esc(label)} {esc(v)}</td><td>{len(cs)}</td><td><b>{fmt(pooled(cs, 'exec_gp_pct', 'exec_gl_pct', 'exec_pf'), '{:.3f}')}</b></td>"
                         f"<td>{sum(c['exec_pf'] for c in cs) / len(cs):.3f}</td><td>{n_}</td>"
                         f"<td>{fmt(pooled(cs, 'eff_gp', 'eff_gl', 'eff_pf'), '{:.2f}')}</td><td>{e_n}</td><td>{sum(c['valid_pct'] for c in cs) / len(cs):.2f}</td>"
                         f"<td>{sum(val(c, 'neg_orders') or 0 for c in cs)}</td></tr>")
        return "".join(rows_)
    best_rows = []
    for nn in negs:
        for mn in minimals:
          for km in kmins:
            for t in thirds:
                b = (rank_cells(cells, t[0], floor, nn, neg_default, mn, km) or [None])[0]
                if b:
                    best_rows.append(f"<tr><td>{esc(nn)}</td><td>{esc(mn)}</td><td>{esc(km)}</td><td>{b['long']}</td><td>{b['short_eff']}</td><td>{b['real_eff']}</td><td><b>{b['exec_pf']}</b></td><td>{b['exec_orders']}</td>"
                                     f"<td>{b['eff_pf']} ({b.get('eff_orders', '–')})</td><td>{b['valid_pct']}</td><td>{b['half1']['pf']} / {b['half2']['pf']}</td><td>{fmt(val(b, 'neg_pf'), '{:.2f}')} on {val(b, 'neg_orders') or 0:.0f}</td></tr>")
    best_table = ("<h2>Best cell per Negative windows × minimal windows</h2><p class=\"sub\">The cell with the highest executed PF (at least " + str(floor) + " orders) for each combination: "
                  "a minimal window decides which of the newest 1-3 results must be positive or negative before a Set is valid (Base: the Set's own results; Main: its Main Set's).</p>"
                  "<div class=\"wrap\"><table class=\"t\"><thead><tr><th>Negative windows</th><th>Base minimal</th><th>Main minimal</th><th>Long</th><th>Short</th><th>Real</th><th>Exec PF</th><th>Orders</th><th>Eff PF (orders)</th><th>Valid %</th>"
                  "<th>PF 1st / 2nd 12 h</th><th>Negative lane</th></tr></thead><tbody>" + "".join(best_rows) + "</tbody></table></div>") if best_rows and (len(minimals) > 1 or len(kmins) > 1) else ""
    marg = ""
    if staged:
        body = (marginal("long", longs, "Base long", lambda c: c["long"]) + marginal("short", shorts, "Base short", lambda c: c["short_eff"])
                + marginal("real", meta["reals"], "Real last N", lambda c: c["real_eff"] if c.get("real_third") else None)
                + marginal("main", meta["mains"], "Main long", lambda c: c["main_eff"] if c.get("main_third") and not c.get("real_third") else None)
                + (marginal("neg", negs, "Negative windows", cneg) if len(negs) > 1 else "")
                + (marginal("min", minimals, "Base minimal window", min_label) if len(minimals) > 1 else "")
                + (marginal("kmin", kmins, "Main minimal window", main_min_label) if len(kmins) > 1 else ""))
        marg = ('<h2>What each setting does, over all its orders</h2><p class="sub">For every setting value, all the cells that carry it (the other settings averaged out) taken together: the executed PF of ALL their orders (gains over losses added over the cells, '
                'so a cell with many orders weighs more), the plain mean of the cells\' PFs beside it, the executed orders behind them, the effective PF of all their effective orders (in-sample), '
                'the mean validated share and the orders on the Negative lane. The same orders appear in many cells (every cell walks the same hours), so these are comparisons between settings, not independent samples; single cells are noisy.</p>'
                f'<div class="wrap"><table class="t"><thead><tr><th>Setting</th><th>Cells</th><th>Exec PF (all orders)</th><th>Mean of cell PFs</th><th>Exec orders</th><th>Eff PF (all orders)</th><th>Eff orders</th><th>Mean valid %</th><th>Neg-lane orders</th></tr></thead><tbody>{body}</tbody></table></div>')
    min_tabs = "".join(f'<button type="button" class="tab" data-min="{esc(m)}" aria-pressed="{"true" if i == 0 else "false"}">Base minimal {esc(m)}</button>' for i, m in enumerate(minimals)) if len(minimals) > 1 else ""
    kmin_tabs = "".join(f'<button type="button" class="tab" data-kmin="{esc(m)}" aria-pressed="{"true" if i == 0 else "false"}">Main minimal {esc(m)}</button>' for i, m in enumerate(kmins)) if len(kmins) > 1 else ""
    win_tables = ""
    show_cells = ([("Shipped setting", shipped)] if shipped else []) + [(f"Best by executed PF, rank {i + 1}", c) for i, c in enumerate(ranked_all[:3])]
    for ttl, c in show_cells:
        win_tables += windows_table(c, f"{ttl}: long {c['long']} / short {c['short_eff']} / Main {c['main_eff']} / Real {c['real_eff']}" + (f" / Base minimal {min_label(c)}" if c.get("min_n") else "") + (f" / Main minimal {main_min_label(c)}" if c.get("main_min_n") else ""))
    if win_tables:
        win_tables = ("<h2>Every last-N window on its own</h2><p class=\"sub\">For a cell, each window judged by itself over all test hours: how many Sets of its stage's population pass it (per hour), the PF of all the orders it reads, "
                      "and how many orders that is. A stage passes when all of its windows do; the Sets that pass a window alone are more than those that pass the stage.</p>" + win_tables)
    metric_tabs = "".join(f'<button type="button" class="tab" data-metric="{k}" aria-pressed="{"true" if i == 0 else "false"}">{esc(l)}</button>' for i, (k, l, *_r) in enumerate(METRICS))
    sc = meta.get("self_check") or []
    chk = (f"direct gate = engine on {sum(x['samples'] - x['bad'] for x in sc)}/{sum(x['samples'] for x in sc)} sampled (Set, hour) pairs" if sc else "self-check not run")
    created = time.strftime("%d %b %Y %H:%M UTC", time.gmtime(meta["created"]))
    data_json = json.dumps({"meta": {k: v for k, v in meta.items() if k != "symbols"}, "cells": [{k: v for k, v in c.items() if k != "hourly"} for c in cells]}, separators=(",", ":")).replace("</", "<\\/")
    return TEMPLATE.format(span=esc(span), n_sym=len(meta["symbols"]), pre=f"{meta['prehistory_h']:g}", n_hours=len(meta["hours"]), n_cells=len(cells),
                           floor=floor, min_orders=("off" if not meta["min_orders"] else meta["min_orders"]), ddt=f"{meta['max_ddt_h']:g}", min_pf=meta["min_pf"],
                           cost=meta["cost_pct"], sets=meta["sets"], ratio_txt=(f"{100 * meta['short_max_ratio']:g}%" if meta.get("short_max_ratio") is not None else "100%"), reading="".join(reading), third_tabs=third_tabs, metrics=metrics_json, neg_tabs=neg_tabs, min_tabs=min_tabs, kmin_tabs=kmin_tabs, windows_tables=win_tables, marginals=marg, best_table=best_table, metric_tabs=metric_tabs,
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
  <span>{n_sym} symbols (the live universe)</span><span>short window at most {ratio_txt} of the long window</span><span>{pre} h pre-history before every hour</span><span>{n_cells} cells ({sets} Sets each)</span>
  <span>PositionCost {cost}%</span><span>minimum PF {min_pf}</span><span>minimum orders {min_orders}</span><span>drawdown time under {ddt} h</span><span>hold Auto off</span>
</div>
<section class="card" aria-label="Reading">
  <b>What the grid says</b>
  <ul>{reading}</ul>
</section>

<h2>Heatmaps</h2>
<div class="bar" role="group" aria-label="Third axis">{third_tabs}</div>
<div class="bar" role="group" aria-label="Negative windows">{neg_tabs}</div>
<div class="bar" role="group" aria-label="Base minimal window">{min_tabs}</div>
<div class="bar" role="group" aria-label="Main minimal window">{kmin_tabs}</div>
<div class="bar" role="group" aria-label="Metric">{metric_tabs}</div>
<p class="mdesc" id="mdesc"></p>
{panels}
<div class="legend">
  <span>★ best cell by executed PF (at least {floor} orders)</span><span>● shipped setting ({ship})</span><span>† under {floor} orders, not ranked</span><span>n/a short window over the allowed share of the long window</span>
  <span>PF scale <span class="ramp div"></span> 0.5 · 1.0 · 2.0</span><span>count / share scale <span class="ramp seq"></span> low · high</span>
</div>

{best_table}

{marginals}

{windows_tables}

<h2>Base long window alone (stages off)</h2>
<div class="chips">{alone}</div>

<h2>Hour by hour</h2>
<p class="sub">The best five cells over both grids, the shipped setting and the best Base-alone window. Executed PF per hour on a handful of orders swings widely: read the shape, not a single hour.</p>
{strips}

<h2>All cells</h2>
<div class="wrap"><table class="t" id="tbl"><thead><tr>{table_head}</tr></thead><tbody>{table_rows}</tbody></table></div>

<p class="foot">Executed PF is net of PositionCost over positions opened under the best valid Set per signal, closed by that Set's own lane; effective PF is the gains over the losses of ALL the long-window orders of the Sets that pass every gate over all test hours, measured on the tapes they were selected on (in-sample); every average of cells is taken over all their orders, not as a mean of PFs.
Main long, the Main short window (5/10), the Negative lane and the Real stage are as shipped except where a column says otherwise; the engine clamps a window to the long window. {chk}. Symbols: {symbols}. Generated {created} by scripts/window_grid.py.</p>
</main>
<div class="tip" id="tip" role="tooltip"></div>
<script type="application/json" id="data">{data}</script>
<script type="application/json" id="metrics">{metrics}</script>
<script>
(function () {{
  var first = document.querySelector(".panel");
  var state = {{ third: first ? first.dataset.third : "real", neg: first ? first.dataset.neg : "", min: first ? first.dataset.min : "", kmin: first ? first.dataset.kmin : "", metric: "exec_pf" }};
  var D = JSON.parse(document.getElementById("data").textContent);
  var METRICS = JSON.parse(document.getElementById("metrics").textContent);
  var CELLS = {{}};
  D.cells.forEach(function (c) {{ CELLS[c.id] = c; }});
  function paint(td, kind, t) {{
    var a = Math.abs(t);
    td.style.cssText = kind === "seq" ? "--t:" + (a * 100).toFixed(0) + "%;--k:var(--seq-hi);--m:var(--seq-lo)" : "--t:" + (a * 100).toFixed(0) + "%;--k:" + (t >= 0 ? "var(--hi)" : "var(--lo)") + ";--m:var(--mid)";
    if (a > 0.78) td.style.setProperty("--ink", "var(--on-strong)");
  }}
  function repaint() {{
    var mi = METRICS.findIndex(function (m) {{ return m.key === state.metric; }});
    if (mi < 0) mi = 0;
    var m = METRICS[mi];
    document.getElementById("mdesc").textContent = m.desc;
    document.querySelectorAll(".panel:not([hidden]) td.cell[data-v]").forEach(function (td) {{
      var v = td.dataset.v.split("|"), t = td.dataset.t.split("|")[mi];
      td.querySelector(".v").textContent = v[mi];
      var c = CELLS[td.dataset.cell];
      td.querySelector(".n").textContent = m.key === "exec_orders" ? "PF " + (c.exec_pf === null ? "–" : c.exec_pf.toFixed(2)) : c.exec_orders + " orders";
      if (t === "") {{ td.style.cssText = ""; }} else {{ paint(td, m.kind, parseFloat(t)); }}
    }});
  }}
  function tipText(c) {{
    var f = function (x, d) {{ return x === null || x === undefined ? "–" : Number(x).toFixed(d); }};
    var p = ["Base long " + c.long + (c.stages ? " / short " + c.short_eff : " alone (stages off)")];
    if (c.stages) p.push("Main long " + c.main_eff + " / Real " + c.real_eff + (c.neg_windows ? " / Negative windows " + c.neg_windows.join(",") : " / Negative lane off"));
    if (c.min_n) p.push("Base minimal window " + (c.min_mode === "positive" ? "+" : c.min_mode === "both" ? "±" : "-") + c.min_n + " (the newest " + c.min_n + " result(s) " + (c.min_mode === "both" ? "share one sign" : "all " + c.min_mode) + ")");
    if (c.main_min_n) p.push("Main minimal window " + (c.main_min_mode === "positive" ? "+" : c.main_min_mode === "both" ? "±" : "-") + c.main_min_n + " (the newest " + c.main_min_n + " Main result(s) " + (c.main_min_mode === "both" ? "share one sign" : "all " + c.main_min_mode) + ")");
    p.push("executed PF " + f(c.exec_pf, 3) + " on " + c.exec_orders + " orders (" + (c.exec_orders ? Math.round(100 * c.wins / c.exec_orders) : 0) + "% wins)");
    p.push("halves " + f(c.half1.pf, 2) + " (" + c.half1.orders + ") / " + f(c.half2.pf, 2) + " (" + c.half2.orders + ")");
    p.push("effective PF " + f(c.eff_pf, 2) + " on " + (c.eff_orders === undefined ? "–" : c.eff_orders) + " orders (pooled over all hours, in-sample), validated " + f(c.valid_pct, 2) + "%");
    p.push("net " + f(c.net_per_order_pct, 3) + "% per position (" + f(c.net_pct, 1) + "% summed), max drawdown " + f(c.max_dd_pct, 1) + "%, hours positive " + c.hours_pos);
    if (c.neg_lane) p.push("positive lane PF " + f(c.pos_lane.pf, 2) + " on " + c.pos_lane.orders + ", Negative lane PF " + f(c.neg_lane.pf, 2) + " on " + c.neg_lane.orders);
    return p.join(" · ");
  }}
  function tipOf(el) {{ return el.dataset.tip || (el.dataset.cell && CELLS[el.dataset.cell] ? tipText(CELLS[el.dataset.cell]) : ""); }}
  function show() {{
    document.querySelectorAll(".panel").forEach(function (p) {{ p.hidden = !(p.dataset.third === state.third && p.dataset.neg === state.neg && p.dataset.min === state.min && p.dataset.kmin === state.kmin); }});
    document.querySelectorAll(".tab").forEach(function (b) {{
      var on = (b.dataset.third && b.dataset.third === state.third) || (b.dataset.neg && b.dataset.neg === state.neg) || (b.dataset.min && b.dataset.min === state.min) || (b.dataset.kmin && b.dataset.kmin === state.kmin) || (b.dataset.metric && b.dataset.metric === state.metric);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    }});
    repaint();
  }}
  document.querySelectorAll(".tab").forEach(function (b) {{
    b.addEventListener("click", function () {{
      if (b.dataset.third) state.third = b.dataset.third;
      if (b.dataset.neg) state.neg = b.dataset.neg;
      if (b.dataset.min) state.min = b.dataset.min;
      if (b.dataset.kmin) state.kmin = b.dataset.kmin;
      if (b.dataset.metric) state.metric = b.dataset.metric;
      show();
    }});
  }});
  show();
  var tip = document.getElementById("tip");
  function move(e, el) {{
    var r = el.getBoundingClientRect(), x = e && e.clientX ? e.clientX : r.left + r.width / 2, y = e && e.clientY ? e.clientY : r.bottom;
    tip.style.left = Math.max(8, Math.min(window.innerWidth - tip.offsetWidth - 8, x + 12)) + "px";
    tip.style.top = Math.max(8, Math.min(window.innerHeight - tip.offsetHeight - 8, y + 14)) + "px";
  }}
  var HOV = "[data-tip],[data-cell]";
  document.addEventListener("mouseover", function (e) {{ var el = e.target.closest(HOV); if (el) {{ tip.textContent = tipOf(el); tip.style.display = "block"; move(e, el); }} }});
  document.addEventListener("mousemove", function (e) {{ var el = e.target.closest(HOV); if (el) move(e, el); }});
  document.addEventListener("mouseout", function (e) {{ if (e.target.closest(HOV)) tip.style.display = "none"; }});
  document.addEventListener("focusin", function (e) {{ var el = e.target.closest(HOV); if (el) {{ tip.textContent = tipOf(el); tip.style.display = "block"; move(null, el); }} }});
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


def merge(datas: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Several grid files of the same test window in one report: the cells are added (an id twice is an error), the lists of values in the meta are united in
    first-seen order (a heatmap panel shows n/a where a file has no cell for its combination). A reference cell several runs share (Base alone, the shipped
    setting) is kept once when the runs agree on every figure (the wall-clock `secs` of the run does not count); an id with different figures in two files is refused."""
    out = {"meta": dict(datas[0]["meta"]), "cells": []}
    seen: Dict[str, Dict[str, Any]] = {}
    for d in datas:
        for c in d["cells"]:
            if c["id"] in seen:
                if {k: v for k, v in seen[c["id"]].items() if k != "secs"} != {k: v for k, v in c.items() if k != "secs"}:
                    raise SystemExit(f"cell {c['id']} is in two grid files with different figures")
                continue
            seen[c["id"]] = c
            out["cells"].append(c)
    m = out["meta"]
    for key in ("longs", "shorts", "mains", "reals", "negs"):
        m[key] = sorted({x for d in datas for x in d["meta"].get(key, [])}, key=lambda x: (str(x).count(","), x)) if key != "negs" else list(dict.fromkeys(x for d in datas for x in d["meta"].get("negs", [])))
    for key in ("minimals", "main_minimals"):
        by = {}
        for d in datas:
            for x in d["meta"].get(key) or [{"n": 0, "mode": "", "label": "off"}]:
                by.setdefault(x["label"], x)
        m[key] = sorted(by.values(), key=lambda x: (x["n"] != 0, x["mode"] != "positive", x["n"]))
    m["self_check"] = [x for d in datas for x in (d["meta"].get("self_check") or [])]
    m["groups"] = sum(d["meta"].get("groups") or 0 for d in datas)
    m["inconsistent_cells"] = sum(d["meta"].get("inconsistent_cells") or 0 for d in datas)
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    datas = [json.load(open(f)) for f in sys.argv[1:-1]]
    data = datas[0] if len(datas) == 1 else merge(datas)
    open(sys.argv[-1], "w", encoding="utf-8").write(render(data))
    print("wrote", sys.argv[-1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
