#!/usr/bin/env python3
"""One HTML report over many simulation windows (scripts/sim_12h_account.py runs).

Each window directory holds one or more named runs (``<root>/<window>/<run>/sim.json``).
The report pools them: equity per window and cumulative across windows
(multi-series), an hourly P&L heat strip, per-distinct-signal results
(unfiltered vs admitted vs Base+Main+Real), strategies, kind lanes and axis
children, and links to every window's own hourly report.

    python3 scripts/sim_multi_window_report.py --root /tmp/s3 --run fix1 \
        --compare default --out reports/sim-3h-... --title "..."
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sim_12h_account as sim  # noqa: E402

START = 10.0
SIGNAL_ROWS = (
    ("unfiltered", "per distinct signal · unfiltered"),
    ("admitted", "per distinct signal · admitted (top in-sample PF per signal)"),
    ("Base+Main+Real", "per distinct signal · Base+Main+Real"),
)


def esc(v: Any) -> str:
    return html.escape(str(v))


def short(window: str) -> str:
    """'t1005-21' / 'w1005-17' -> '05-21' (day-hour of the window end)."""
    tail = window[1:] if window[:1].isalpha() else window
    return tail[2:] if len(tail) >= 7 else tail


def load_run(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    j = json.load(open(path, encoding="utf-8"))
    run = next((r for r in j.get("runs") or [] if r.get("run") == "post-base"), None)
    if run is None:
        return None
    t = run["totals"]
    hours = [float(h.get("pnl") or 0) for h in run.get("hourly") or []]
    labels = [str(h.get("startUtc") or "") for h in run.get("hourly") or []]
    sig = {}
    for name, key in SIGNAL_ROWS:
        v = (j.get("tradeLevel") or {}).get(key) or {}
        sig[name] = (int(v.get("signals") or 0), float(v.get("netAvgPct") or 0.0))
    return dict(j=j, end=float(t["equityEnd"]), hours=hours, labels=labels, dd=float(t.get("ddMaxPct") or 0),
                entries=int((t.get("orders") or {}).get("entry") or 0), closed=t.get("closed") or {},
                by_strategy=t.get("byStrategy") or {}, gate=j.get("gate") or {}, engine=j.get("engine") or {},
                window=j.get("window") or {}, sig=sig, trade_level=j.get("tradeLevel") or {})


def heat_strip(windows: List[str], rows: List[List[float]], title: str) -> str:
    """Window x hour grid coloured by P&L (green up, red down)."""
    if not rows:
        return ""
    cols = max(len(r) for r in rows)
    vmax = max([abs(v) for r in rows for v in r] + [1e-9])
    cw, ch, left, top = 46, 16, 92, 18
    w, h = left + cols * cw + 8, top + len(rows) * ch + 8
    parts = [f"<svg viewBox='0 0 {w} {h}' width='{w}' role='img' aria-label='{esc(title)}' class='chart' style='width:{w}px;max-width:100%'>"]
    for c in range(cols):
        parts.append(f"<text x='{left + c * cw + cw / 2:.0f}' y='12' class='ax' text-anchor='middle'>h{c + 1}</text>")
    for r, (name, vals) in enumerate(zip(windows, rows)):
        y = top + r * ch
        parts.append(f"<text x='{left - 6}' y='{y + 12}' class='ax' text-anchor='end'>{esc(name)}</text>")
        for c, v in enumerate(vals):
            a = min(1.0, abs(v) / vmax) * 0.85 + (0.08 if v else 0.0)
            col = "var(--pos)" if v > 0 else ("var(--neg)" if v < 0 else "var(--line)")
            parts.append(f"<rect x='{left + c * cw + 1}' y='{y + 1}' width='{cw - 2}' height='{ch - 2}' rx='2' fill='{col}' "
                         f"fill-opacity='{a if v else 0.35:.2f}'><title>{esc(name)} h{c + 1}: {v:+.3f} USDT</title></rect>")
    parts.append("</svg>")
    return f"<figure><figcaption>{esc(title)}</figcaption>{''.join(parts)}</figure>"


def pooled_strategies(runs: List[Dict[str, Any]]) -> List[Tuple[str, int, int, float]]:
    acc: Dict[str, List[float]] = {}
    for r in runs:
        for key, v in r["by_strategy"].items():
            if not isinstance(v, dict) or not v.get("n"):
                continue
            a = acc.setdefault(key, [0, 0, 0.0])
            a[0] += int(v.get("n") or 0)
            a[1] += int(v.get("wins") or 0)
            a[2] += float(v.get("netUsdt") or 0.0)
    return sorted(((k, int(a[0]), int(a[1]), a[2]) for k, a in acc.items()), key=lambda x: -x[1])


def pooled_trade_level(runs: List[Dict[str, Any]], prefix: str) -> List[Tuple[str, int, float]]:
    acc: Dict[str, List[float]] = {}
    for r in runs:
        for key, v in r["trade_level"].items():
            if not key.startswith(prefix) or not isinstance(v, dict):
                continue
            n = int(v.get("signals") or v.get("trades") or 0)
            if not n:
                continue
            a = acc.setdefault(key[len(prefix):].strip(), [0, 0.0])
            a[0] += n
            a[1] += float(v.get("netAvgPct") or 0.0) * n
    return sorted(((k, int(a[0]), a[1] / a[0]) for k, a in acc.items() if a[0]), key=lambda x: -x[2])


def summary(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    hrs = [v for r in runs for v in r["hours"]]
    sig = {}
    for name, _ in SIGNAL_ROWS:
        n = sum(r["sig"][name][0] for r in runs)
        s = sum(r["sig"][name][0] * r["sig"][name][1] for r in runs)
        sig[name] = (n, s / n if n else 0.0)
    return dict(total=sum(r["end"] - START for r in runs), pos_windows=sum(r["end"] > START + 1e-9 for r in runs),
                windows=len(runs), pos_hours=sum(v > 0 for v in hrs), hours=len(hrs),
                entries=sum(r["entries"] for r in runs), dd=max([r["dd"] for r in runs] + [0.0]), sig=sig)


def build(root: str, run: str, out: str, title: str, compare: List[str], note: str, pages: bool) -> str:
    windows = sorted(d for d in os.listdir(root) if not d.startswith("_") and os.path.isdir(os.path.join(root, d)))
    loaded: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for name in [run] + compare:
        for w in windows:
            r = load_run(os.path.join(root, w, name, "sim.json"))
            if r is not None:
                loaded.setdefault(name, {})[w] = r
    main = loaded.get(run) or {}
    if not main:
        raise SystemExit(f"no runs named {run!r} under {root}")
    order = [w for w in windows if w in main]
    runs = [main[w] for w in order]
    os.makedirs(out, exist_ok=True)
    links = {}
    if pages:
        for w in order:
            src = os.path.join(root, w, run, "sim.html")
            if os.path.exists(src):
                shutil.copy(src, os.path.join(out, f"{w}.html"))
                links[w] = f"{w}.html"
    s = summary(runs)
    eng = runs[0]["engine"]
    hours_per = max(len(r["hours"]) for r in runs)
    # charts
    hl = [f"h{i + 1}" for i in range(hours_per)]
    eq_series = []
    for w, r in zip(order, runs):
        acc, ys = 0.0, []
        for v in r["hours"]:
            acc += v
            ys.append(round(acc, 4))
        eq_series.append((short(w), ys))
    half = (len(eq_series) + 1) // 2
    charts = [sim.svg_lines("P&L within each window (USDT, start 0)", hl, eq_series[:half], ref=0.0)]
    if len(eq_series) > half:
        charts.append(sim.svg_lines("P&L within each window (continued)", hl, eq_series[half:], ref=0.0))
    cum_labels = [short(w) for w in order]
    cum_series = []
    for name in [run] + compare:
        res = loaded.get(name) or {}
        acc, ys = 0.0, []
        for w in order:
            r = res.get(w)
            acc += (r["end"] - START) if r else 0.0
            ys.append(round(acc, 4) if r else None)
        cum_series.append((name, ys))
    charts.append(sim.svg_lines("Cumulative result across windows (USDT)", cum_labels, cum_series, ref=0.0))
    sig_series = [(name, [r["sig"][name][1] if r["sig"][name][0] else None for r in runs]) for name, _ in SIGNAL_ROWS]
    charts.append(sim.svg_lines("Net % per distinct signal by window", cum_labels, sig_series, ref=0.0))
    charts.append(sim.svg_bars("Entries per window", cum_labels, [(name, [((loaded.get(name) or {}).get(w) or {}).get("entries", 0) for w in order])
                                                              for name in [run] + compare]))
    charts.append(heat_strip([short(w) for w in order], [r["hours"] for r in runs], "Hourly P&L by window (USDT)"))
    # tables
    def cell(v: float, nd: int = 2) -> str:
        cls = "pos" if v > 0 else ("neg" if v < 0 else "")
        return f"<td class='{cls}'>{v:+.{nd}f}</td>"
    wrows = []
    for w, r in zip(order, runs):
        link = f"<a href='{esc(links[w])}'>{esc(w)}</a>" if w in links else esc(w)
        g = r["gate"]
        sg = r["sig"]
        wrows.append(
            f"<tr><td>{link}</td><td>{esc(r['window'].get('startUtc', ''))}</td>{cell(r['end'] - START)}"
            f"<td>{sum(v > 0 for v in r['hours'])}/{len(r['hours'])}</td><td>{r['dd']:.1f}</td><td>{r['entries']}</td>"
            f"<td>{sim.fmt(r['closed'].get('pfNormal'))}</td>"
            + "".join(cell(sg[n][1], 3) if sg[n][0] else "<td>–</td>" for n, _ in SIGNAL_ROWS)
            + f"<td>{(g.get('evidenceDepthAtWindowStart') or {}).get('median', '–')}</td><td>{g.get('baseOk', 0):,}</td>"
              f"<td>{g.get('realOk', 0):,}</td><td>{g.get('admitted', 0):,}</td>"
            + "".join(cell(v) for v in r["hours"]) + "</tr>")
    cmp_rows = []
    for name in [run] + compare:
        res = [loaded[name][w] for w in order if w in (loaded.get(name) or {})]
        if not res:
            continue
        ss = summary(res)
        cmp_rows.append(f"<tr><td>{esc(name)}</td>{cell(ss['total'])}<td>{ss['pos_windows']}/{ss['windows']}</td>"
                        f"<td>{ss['pos_hours']}/{ss['hours']}</td><td>{ss['entries']}</td>"
                        + "".join(cell(ss['sig'][n][1], 4) for n, _ in SIGNAL_ROWS) + "</tr>")
    strat_rows = "".join(f"<tr><td>{esc(k)}</td><td>{n}</td><td>{100.0 * w / n:.1f}</td>{cell(net, 3)}</tr>"
                         for k, n, w, net in pooled_strategies(runs))
    kind_rows = "".join(f"<tr><td>{esc(k)}</td><td>{n}</td>{cell(v, 4)}</tr>"
                        for k, n, v in pooled_trade_level(runs, "kind lane ") if k.endswith("unfiltered"))
    axis_rows = "".join(f"<tr><td>{esc(k)}</td><td>{n}</td>{cell(v, 4)}</tr>"
                        for k, n, v in pooled_trade_level(runs, "per distinct signal · admitted & axis "))
    sig_cards = "".join(f"<div class='card'><span>{esc(n)} · net / signal</span><strong class='{'pos' if v > 0 else 'neg'}'>{v:+.3f}%</strong>"
                        f"<span class='meta'>{k:,} signals · gross {v + float(eng.get('costPct') or 0.1):+.3f}%</span></div>"
                        for n, (k, v) in s["sig"].items())
    css = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--mut:#6b6b66;--line:#e3e2dd;--acc:#2d5bd7;--pos:#1f7a3a;--neg:#b3261e;--tot:#f1f0ec}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#161615;--fg:#ecebe6;--mut:#9b9a94;--line:#34332f;--acc:#8fb0ff;--pos:#6fcf8a;--neg:#ff8a80;--tot:#22211f}}
:root[data-theme=dark]{--bg:#161615;--fg:#ecebe6;--mut:#9b9a94;--line:#34332f;--acc:#8fb0ff;--pos:#6fcf8a;--neg:#ff8a80;--tot:#22211f}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1500px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 6px}
.meta,.note{color:var(--mut)}.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:12.5px}th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{position:sticky;top:0;background:var(--bg);font-weight:600}td:first-child,th:first-child{text-align:left}.pos{color:var(--pos)}.neg{color:var(--neg)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,190px),1fr));gap:10px;margin:12px 0}
.card{border:1px solid var(--line);border-radius:6px;padding:8px 10px;display:flex;flex-direction:column;gap:2px}.card strong{font-size:20px}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr));gap:12px;margin:10px 0 14px}
figure{margin:0;border:1px solid var(--line);border-radius:6px;padding:8px 10px}figcaption{font-weight:600;font-size:12.5px;margin-bottom:4px}
.chart{width:100%;height:auto}.chart .grid{stroke:var(--line);stroke-width:1}.chart .ref{stroke:var(--mut);stroke-dasharray:4 3}.chart .ax{fill:var(--mut);font-size:10px}
.legend{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:11.5px;color:var(--mut)}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
ul{padding-left:18px}li{margin:3px 0}
"""
    floors = eng.get("floors") or {}
    cfg = (f"Base {eng.get('baseN')} / Main {eng.get('mainN')} / Real {eng.get('realN')} · PF "
           f"{'/'.join(f'{float(floors.get(k, 0)):.2f}' for k in ('base', 'main', 'real'))} · DDT ≤ {float(eng.get('maxDdS') or 0) / 3600:.0f}h · "
           f"catalog {eng.get('catalogSets', '–'):,} Sets · lookback {eng.get('lookbackBars')} bars · cost {eng.get('costPct')}% · "
           f"volume factor {eng.get('volumeFactor')} · axes {', '.join(k.replace('axis', '').replace('Enabled', '') for k, v in (eng.get('axes') or {}).items() if v) or 'off'}")
    hours_txt = f"{len(order)} windows × {hours_per}h = {len(order) * hours_per}h"
    page = f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{esc(title)}</title><style>{css}</style></head><body><main>
<h1>{esc(title)}</h1>
<p class='meta'>{esc(hours_txt)} · 12 symbols · start {START:.0f} USDT per window · {esc(cfg)}</p>
<div class='cards'>
<div class='card'><span>Result over all windows</span><strong class='{'pos' if s['total'] > 0 else 'neg'}'>{s['total']:+.2f} USDT</strong><span class='meta'>{s['pos_windows']}/{s['windows']} windows positive</span></div>
<div class='card'><span>Positive hours</span><strong>{s['pos_hours']}/{s['hours']}</strong><span class='meta'>max DD in a window {s['dd']:.1f}%</span></div>
<div class='card'><span>Entries</span><strong>{s['entries']:,}</strong><span class='meta'>{s['entries'] / max(1, s['hours']):.1f} per hour</span></div>
{sig_cards}
</div>
{note}
<h2>Diagrams</h2><div class='charts'>{''.join(charts)}</div>
<h2>Runs compared</h2><div class='scroll'><table><thead><tr><th>Run</th><th>Result</th><th>+windows</th><th>+hours</th><th>Entries</th>
{''.join(f'<th>{esc(n)} net/signal</th>' for n, _ in SIGNAL_ROWS)}</tr></thead><tbody>{''.join(cmp_rows)}</tbody></table></div>
<h2>Windows</h2><div class='scroll'><table><thead><tr><th>Window</th><th>Start (UTC)</th><th>Result</th><th>+h</th><th>DD%</th><th>Entries</th><th>Acct PF</th>
{''.join(f'<th>{esc(n)}</th>' for n, _ in SIGNAL_ROWS)}<th>Depth</th><th>Base ok</th><th>Real ok</th><th>Admitted</th>
{''.join(f'<th>h{i + 1}</th>' for i in range(hours_per))}</tr></thead><tbody>{''.join(wrows)}</tbody></table></div>
<p class='note'>Per-signal columns: net % per distinct signal (one trade per signal, what an account can trade). Depth = median closes per Set × side at the window start. Base/Real ok and Admitted count walk-forward candidates.</p>
<h2>Strategies (pooled account trades)</h2><div class='scroll'><table><thead><tr><th>Strategy</th><th>Trades</th><th>Win %</th><th>Net USDT</th></tr></thead><tbody>{strat_rows}</tbody></table></div>
<h2>Indication kind lanes (pooled, unfiltered)</h2><div class='scroll'><table><thead><tr><th>Lane</th><th>Trades</th><th>Net % / trade</th></tr></thead><tbody>{kind_rows}</tbody></table></div>
<h2>Axis children as filters (pooled, per distinct signal)</h2><div class='scroll'><table><thead><tr><th>Admitted &amp; axis child</th><th>Signals</th><th>Net % / signal</th></tr></thead><tbody>{axis_rows}</tbody></table></div>
</main></body></html>"""
    path = os.path.join(out, "index.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return path


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", required=True, help="directory of window directories")
    ap.add_argument("--run", required=True, help="run name inside each window directory")
    ap.add_argument("--compare", default="", help="comma list of other run names to compare")
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="Multi-window simulation")
    ap.add_argument("--note-file", default="", help="HTML fragment inserted under the summary cards")
    ap.add_argument("--no-pages", action="store_true", help="do not copy the per-window reports")
    a = ap.parse_args(argv)
    note = open(a.note_file, encoding="utf-8").read() if a.note_file else ""
    path = build(a.root, a.run, a.out, a.title, [c for c in a.compare.split(",") if c], note, not a.no_pages)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
