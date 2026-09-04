#!/usr/bin/env python3
"""Run the complete VST 72-hour matrix and save a reviewable HTML report.

The report is intentionally generated from the same ``hist_calc.run_calc``
path used by the application.  It therefore contains the exact catalog,
coverage counters, per-direction/per-strategy/per-indication rollups and the
cost-net PF/DDT values used by the historic gate.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
from typing import Any, Dict, Iterable, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))

from hist_calc import DEFAULT_SYMBOLS, run_calc  # noqa: E402


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except Exception:
        return "—"


def duration(seconds: Any) -> str:
    try:
        value = max(0.0, float(seconds or 0))
    except Exception:
        return "—"
    if value < 60:
        return f"{value:.0f}s"
    minutes = value / 60.0
    if minutes < 60:
        return f"{minutes:.1f}m"
    return f"{minutes / 60.0:.1f}h"


def coverage_row(label: str, blob: Dict[str, Any]) -> str:
    return (
        f"<tr><th>{esc(label)}</th><td>{num(blob.get('completed'), 0)} / "
        f"{num(blob.get('requested'), 0)}</td><td>{num(blob.get('coveragePct'), 2)}%</td>"
        f"<td>{num(blob.get('failed'), 0)} failed · {num(blob.get('skipped'), 0)} skipped</td></tr>"
    )


def table_rows(items: Iterable[Dict[str, Any]], columns: List[tuple[str, str]], empty: str = "No rows") -> str:
    rows = list(items)
    if not rows:
        return f"<tr><td class=empty colspan={len(columns)}>{esc(empty)}</td></tr>"
    out: List[str] = []
    for item in rows:
        cells = []
        for key, kind in columns:
            value = item.get(key)
            if kind == "pf":
                value = num(value, 3)
            elif kind == "pct":
                value = num(value, 1) + "%"
            elif kind == "ddt":
                value = duration(value)
            elif kind == "int":
                value = num(value, 0)
            elif kind == "bool":
                value = "✓" if value else "—"
            elif kind == "text":
                value = esc(value)
            else:
                value = esc(value)
            cells.append(f"<td>{value}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    return "\n".join(out)


def stat_card(label: str, value: Any, note: str = "") -> str:
    return f"<article class=card><div class=eyebrow>{esc(label)}</div><div class=value>{esc(value)}</div><div class=note>{esc(note)}</div></article>"


def build_html(job: Dict[str, Any], symbols: List[str], workers: int) -> str:
    coverage = job.get("coverage") if isinstance(job.get("coverage"), dict) else {}
    catalog = coverage
    by_symbol = job.get("bySymbol") if isinstance(job.get("bySymbol"), list) else []
    by_direction = job.get("byDirection") if isinstance(job.get("byDirection"), dict) else {}
    by_strategy = job.get("byStrategy") if isinstance(job.get("byStrategy"), dict) else {}
    kinds = job.get("kinds") if isinstance(job.get("kinds"), dict) else {}
    rows = [r for r in (job.get("rows") or []) if isinstance(r, dict)]
    both = [r for r in rows if str(r.get("direction") or "BOTH") == "BOTH"]
    validated = [r for r in both if r.get("validated")]
    low_dd = sorted(validated, key=lambda r: (float(r.get("maxDdS") or 0), -float(r.get("last15Ratio") or 0)))[:20]
    generated = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    source = str(job.get("source") or "unknown")
    is_ready = str(job.get("phase") or "") == "ready" and not job.get("error")
    options = job.get("options") if isinstance(job.get("options"), dict) else {}
    progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
    empty_blob: Dict[str, Any] = {}
    data = json.dumps(job, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")

    direction_items = [dict(v, direction=k) for k, v in by_direction.items() if isinstance(v, dict)]
    strategy_items = [dict(v, strategy=k) for k, v in by_strategy.items() if isinstance(v, dict)]
    kind_items = [dict(v, kind=k) for k, v in kinds.items() if isinstance(v, dict)]
    set_count = catalog.get("setCount", catalog.get("product", 0))
    validated_count = job.get("validatedCount", catalog.get("validatedCount", len(validated)))
    status_class = "ok" if is_ready else "bad"
    status_text = "READY · all requested work completed" if is_ready else f"{job.get('phase', 'error').upper()} · review error"

    return f"""<!doctype html>
<html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>CTS-G · 72h VST matrix report</title>
<style>
:root {{ color-scheme:dark; --bg:#07111f; --panel:#0d1b2d; --line:#1b3550; --text:#e8f1fb; --muted:#91a7bd; --cyan:#62e6ff; --green:#63e6a6; --amber:#ffd166; --red:#ff7186; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:radial-gradient(circle at 8% 0%,#163653 0,#07111f 42%,#050b14 100%); color:var(--text); font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif; }}
main {{ width:min(1440px,calc(100% - 40px)); margin:0 auto; padding:42px 0 72px; }}
h1 {{ font-size:clamp(28px,4vw,54px); line-height:1.05; margin:0 0 12px; letter-spacing:-.04em; }} h2 {{ font-size:20px; margin:0 0 16px; }} h3 {{ font-size:15px; margin:0 0 10px; }}
p {{ color:var(--muted); }} .hero {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-end; margin-bottom:28px; }} .hero-copy {{ max-width:840px; }}
.badge {{ display:inline-flex; border:1px solid var(--line); border-radius:999px; padding:5px 10px; color:var(--cyan); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }} .status {{ border-radius:14px; padding:12px 15px; border:1px solid var(--line); min-width:280px; }} .status.ok {{ color:var(--green); border-color:#236e55; background:#0b2a27; }} .status.bad {{ color:var(--red); border-color:#713344; background:#2b101c; }}
.grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:18px 0 28px; }} .card,.panel {{ background:linear-gradient(145deg,rgba(17,39,62,.95),rgba(8,21,36,.96)); border:1px solid var(--line); border-radius:16px; box-shadow:0 14px 42px rgba(0,0,0,.16); }} .card {{ padding:16px; min-height:112px; }} .eyebrow {{ color:var(--muted); font-size:11px; letter-spacing:.1em; text-transform:uppercase; }} .value {{ font-size:28px; margin:8px 0 2px; font-weight:720; letter-spacing:-.03em; }} .note {{ color:var(--muted); font-size:12px; }}
.panel {{ padding:20px; margin:16px 0; overflow:hidden; }} .panel p:first-child {{ margin-top:0; }} .two {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }} .three {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ border-bottom:1px solid rgba(130,170,205,.14); padding:9px 8px; text-align:left; vertical-align:top; }} th {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.07em; font-weight:600; }} td {{ color:#dbe8f4; }} .empty {{ text-align:center; color:var(--muted); padding:24px; }} .good {{ color:var(--green); }} .warn {{ color:var(--amber); }} .badtext {{ color:var(--red); }}
pre {{ white-space:pre-wrap; overflow:auto; background:#06101b; border:1px solid var(--line); border-radius:12px; padding:14px; color:#b9d7ec; font-size:12px; }} code {{ color:var(--cyan); }} .foot {{ color:var(--muted); font-size:12px; margin-top:24px; }}
@media(max-width:980px) {{ .grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .hero,.two,.three {{ grid-template-columns:1fr; display:grid; }} .status {{ min-width:0; }} }} @media(max-width:600px) {{ main {{ width:min(100% - 24px,1440px); padding-top:26px; }} .grid {{ grid-template-columns:1fr; }} .panel {{ padding:14px; overflow:auto; }} table {{ min-width:620px; }} }}
</style></head><body><main>
<header class=hero><div class=hero-copy><span class=badge>CTS-G · VST / offline</span><h1>72-hour configuration matrix</h1><p>Complete historic progress across the maximum configured symbol set, all SL:TP ratios, TP steps, independent trailing combinations, both directions, eight indication types, Block and DCA strategy tapes. Values are cost-net and use the same PF/DDT gate as the application.</p></div><div class="status {status_class}"><strong>{esc(status_text)}</strong><br><span>Generated {esc(generated)} · {esc(source)}</span></div></header>
<section class=grid>
{stat_card('Window', f"{job.get('hours', 72)}h", f"{job.get('lookback', 0):,} one-minute bars per symbol")}
{stat_card('Symbols', f"{len(symbols):,}", ', '.join(symbols))}
{stat_card('Independent sets', f"{set_count:,}", f"{len(catalog.get('packs') or [])} packs · {len(catalog.get('slRatios') or [])} SL ratios · {len(catalog.get('steps') or [])} steps")}
{stat_card('Validated', f"{validated_count:,}", f"{job.get('rowCount', 0):,} expanded rows · {num(100 * float(validated_count or 0) / max(1, float(job.get('rowCount') or 1)), 1)}% visible row rate")}
</section>
<section class=panel><h2>Execution and coverage</h2><div class=two><div><table><thead><tr><th>Domain</th><th>Completed</th><th>Coverage</th><th>Issues</th></tr></thead><tbody>
{coverage_row('Symbols', coverage.get('symbols') or empty_blob)}{coverage_row('Bars', coverage.get('bars') or empty_blob)}{coverage_row('Sets', coverage.get('sets') or empty_blob)}{coverage_row('Evaluations', coverage.get('evaluations') or empty_blob)}
</tbody></table></div><div><h3>Catalog dimensions</h3><table><tbody>
<tr><th>Packs</th><td>{esc(', '.join(catalog.get('packs') or []))}</td></tr><tr><th>SL:TP ratios</th><td>{esc(', '.join(num(x,1) for x in (catalog.get('slRatios') or [])))}</td></tr><tr><th>Trailing</th><td>{len(catalog.get('trails') or []):,} independent arm/give variants</td></tr><tr><th>TP steps</th><td>{esc(', '.join(str(x) for x in (catalog.get('steps') or [])))}</td></tr><tr><th>Directions</th><td>{esc(', '.join(catalog.get('directions') or ['LONG','SHORT']))} · independent</td></tr><tr><th>Workers / elapsed</th><td>{workers} / {duration(job.get('elapsedMs', 0) / 1000.0)}</td></tr>
</tbody></table></div></div></section>
<section class=panel><h2>Direction controls</h2><table><thead><tr><th>Direction</th><th>All fills</th><th>Last-N PF</th><th>Last-N</th><th>Win rate</th><th>DD time</th><th>Validated</th></tr></thead><tbody>
{table_rows(direction_items, [('direction','text'),('n','int'),('pf','pf'),('last15N','int'),('wr','pct'),('maxDdS','ddt'),('validated','bool')])}
</tbody></table></section>
<section class=panel><h2>Strategy groups</h2><p>Groups are independently attributed. Block/DCA rows are not allowed to inflate the normal Base/Real Set tape.</p><table><thead><tr><th>Strategy / pack</th><th>Fills</th><th>PF</th><th>Last-N</th><th>Net avg</th><th>Win rate</th><th>DD time</th><th>Validated</th></tr></thead><tbody>
{table_rows(strategy_items, [('strategy','text'),('n','int'),('pf','pf'),('last15N','int'),('netAvg','pf'),('wr','pct'),('maxDdS','ddt'),('validated','bool')])}
</tbody></table></section>
<section class=panel><h2>Indication coverage and gates</h2><p>Every enabled indication kind is represented separately and evaluated with its own side-aware tape.</p><table><thead><tr><th>Kind</th><th>Fills</th><th>PF</th><th>Last-N</th><th>Net avg</th><th>DD time</th><th>Validated</th><th>Gate</th></tr></thead><tbody>
{table_rows(kind_items, [('kind','text'),('n','int'),('pf','pf'),('last15N','int'),('netAvg','pf'),('maxDdS','ddt'),('validated','bool'),('ok','bool')])}
</tbody></table></section>
<section class=panel><h2>Per-symbol results</h2><table><thead><tr><th>Symbol</th><th>Fills</th><th>PF</th><th>Last-N</th><th>Net avg</th><th>Win rate</th><th>Max DD time</th><th>Validated</th></tr></thead><tbody>
{table_rows(by_symbol, [('symbol','text'),('n','int'),('pf','pf'),('last15N','int'),('netAvg','pf'),('wr','pct'),('maxDdS','ddt'),('validated','bool')])}
</tbody></table></section>
<section class=panel><h2>Lowest drawdown validated configurations</h2><p>These are the lowest-DDT validated rows available in the returned ranking slice. DDT is calculated per symbol/set, so independent markets cannot create a false cross-symbol episode.</p><table><thead><tr><th>Set</th><th>Pack</th><th>Side</th><th>SL ratio</th><th>Step</th><th>Trail</th><th>PF</th><th>DD time</th><th>Fills</th></tr></thead><tbody>
{table_rows(low_dd, [('id','text'),('pack','text'),('direction','text'),('slRatio','pf'),('step','int'),('trailKey','text'),('last15Ratio','pf'),('maxDdS','ddt'),('n','int')], empty='No validated configuration in the returned slice')}
</tbody></table></section>
<section class=panel><h2>Runtime contract</h2><div class=three><div><h3>Ownership</h3><p class=good>Only this connection's client prefix is included in live accounting. Foreign orders/positions are excluded from PnL, fills, controls and close reconciliation.</p></div><div><h3>Accounting</h3><p class=good>Partial and cumulative fills are applied as deltas. Repeated exchange snapshots are idempotent; position quantity and realized PnL remain separate.</p></div><div><h3>Control response</h3><p class=good>Control orders are quantity-matched per range group. A failed close is kept pending and re-protected; no duplicate close request is emitted while one is in flight.</p></div></div><pre>{esc(json.dumps({'options': options, 'independence': job.get('independence') or {}, 'progress': progress}, ensure_ascii=False, indent=2))}</pre></section>
<p class=foot>Source: CTS-G historic calculator. This report is synthetic VST/offline evidence and does not represent live exchange profitability or submit any order. Use the remote deployment verification separately for service health and exchange connectivity.</p>
<script type="application/json" id="report-data">{data}</script></main></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=os.path.join(HERE, "..", "reports", "cts-g-72h-report.html"))
    parser.add_argument("--symbols", type=int, default=len(DEFAULT_SYMBOLS))
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    symbols = list(DEFAULT_SYMBOLS[: max(1, min(len(DEFAULT_SYMBOLS), args.symbols))])
    body = {
        "hours": 72,
        "symbols": symbols,
        "allSymbols": False,
        "allConfigs": True,
        "trailing": True,
        "stratBlock": True,
        "stratDca": True,
        "stratIndications": True,
        "stratGeneral": True,
        "indTypeSignals": True,
        "indTypeState": True,
        "indTypeDirection": True,
        "indTypeMove": True,
        "indTypeActive": True,
        "indTypeCommon": True,
        "indTypeTrend": True,
        "indTypeBreak": True,
        "workers": max(1, min(8, int(args.workers or 4))),
        "synth": True,
    }
    job = run_calc(body, persist=False)
    output = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        handle.write(build_html(job, symbols, body["workers"]))
    print(json.dumps({
        "output": output,
        "phase": job.get("phase"),
        "ready": bool(job.get("ready")),
        "hours": job.get("hours"),
        "symbols": len(symbols),
        "setCount": (job.get("coverage") or {}).get("setCount"),
        "rowCount": job.get("rowCount"),
        "validatedCount": job.get("validatedCount"),
        "elapsedMs": job.get("elapsedMs"),
        "error": job.get("error") or "",
    }, separators=(",", ":")))
    return 0 if job.get("phase") == "ready" and not job.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
