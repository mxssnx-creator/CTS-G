#!/usr/bin/env python3
"""24h historic sim: default settings, 5 most-volatile symbols, steps 3–12.

Uses hist_calc.run_calc (same path as the desk). Does not persist into the
live/VST hist-calc job and does not touch running engines.
"""
from __future__ import annotations

import html
import json
import os
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PULSE = os.path.join(ROOT, "server", "pulse")
sys.path.insert(0, PULSE)

from hist_calc import (  # noqa: E402
    HIST_WARMUP_BARS,
    KLINE_URL,
    _public_json,
    catalog_listings,
    direction_rollup,
    fetch_klines,
    hours_to_bars,
    parse_klines,
    pick_winner_row,
    set_row,
    step_rollup,
    strategy_rollup,
    symbol_rollup,
    _rank_set_rows,
)
from position_cost import POSITIVE_PF  # noqa: E402
from set_engine import SetBook  # noqa: E402

TICKER_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"

OUT_DIR = os.path.join(ROOT, "reports", "step-sweep-24h")
PUBLIC_JSON = os.path.join(ROOT, "public", "step-sweep-24h.json")
PUBLIC_HTML = os.path.join(ROOT, "public", "step-sweep-24h.html")
JOB_PATH = os.path.join(OUT_DIR, "hist-calc.json")
SUMMARY_PATH = os.path.join(OUT_DIR, "summary.json")

STEP_LO = 3
STEP_HI = 12
HOURS = 24
TOP_N = 5
VOL_CANDIDATES = 18
MIN_QUOTE_VOLUME = 1_000_000.0


def _ensure_dir() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)


def _write_json(path: str, payload: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def publish(summary: Dict[str, Any]) -> None:
    _ensure_dir()
    _write_json(SUMMARY_PATH, summary)
    _write_json(PUBLIC_JSON, summary)
    html = render_html(summary)
    for dest in (PUBLIC_HTML, os.path.join(OUT_DIR, "report.html")):
        with open(dest, "w", encoding="utf-8") as handle:
            handle.write(html)


def fetch_ticker() -> List[Dict[str, Any]]:
    body = _public_json(TICKER_URL, timeout=20.0)
    rows = body.get("data") if isinstance(body, dict) else []
    out: List[Dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol.endswith("-USDT"):
            continue
        try:
            last = float(row.get("lastPrice") or 0)
            hi = float(row.get("highPrice") or 0)
            lo = float(row.get("lowPrice") or 0)
            qv = float(row.get("quoteVolume") or 0)
            chg = float(row.get("priceChangePercent") or 0)
        except (TypeError, ValueError):
            continue
        if last <= 0 or hi <= 0 or lo <= 0 or hi < lo:
            continue
        if qv < MIN_QUOTE_VOLUME:
            continue
        out.append({
            "symbol": symbol,
            "last": last,
            "vol24h": round((hi - lo) / last * 100.0, 4),
            "quoteVolume": qv,
            "changePct": chg,
            "vol1h": 0.0,
        })
    out.sort(key=lambda r: -r["vol24h"])
    return out


def fetch_1h_vol(symbol: str) -> float:
    params = {"symbol": symbol, "interval": "1h", "limit": "2"}
    try:
        body = _public_json(KLINE_URL + "?" + urllib.parse.urlencode(params), timeout=12.0)
    except Exception:
        return 0.0
    bars = parse_klines(body.get("data") if isinstance(body, dict) else body)
    if not bars:
        return 0.0
    bar = bars[-1]
    try:
        hi, lo, last = float(bar[1]), float(bar[2]), float(bar[3] or 0)
    except (TypeError, ValueError, IndexError):
        return 0.0
    if last <= 0 or hi < lo:
        return 0.0
    return round((hi - lo) / last * 100.0, 4)


def rank_volatile(n: int = TOP_N) -> List[Dict[str, Any]]:
    universe = fetch_ticker()
    if not universe:
        raise RuntimeError("BingX ticker returned no USDT perps")
    candidates = universe[:VOL_CANDIDATES]
    for row in candidates:
        row["vol1h"] = fetch_1h_vol(row["symbol"])
    candidates.sort(key=lambda r: (-float(r["vol1h"] or 0), -float(r["vol24h"] or 0)))
    picked = [r for r in candidates if r["vol1h"] > 0][:n]
    if len(picked) < n:
        picked = candidates[:n]
    return picked, universe[:40]


def compact_job(job: Dict[str, Any], ranked: List[Dict[str, Any]], universe: List[Dict[str, Any]]) -> Dict[str, Any]:
    step_blob = job.get("byStep") if isinstance(job.get("byStep"), dict) else {}
    by_step = step_blob.get("byStep") if isinstance(step_blob.get("byStep"), dict) else {}
    ranges = step_blob.get("ranges") if isinstance(step_blob.get("ranges"), list) else []
    heatmap = step_blob.get("heatmap") if isinstance(step_blob.get("heatmap"), list) else []
    steps = [int(s) for s in (step_blob.get("steps") or sorted(by_step, key=lambda x: int(x)))]
    coverage = job.get("coverage") if isinstance(job.get("coverage"), dict) else {}
    options = job.get("options") if isinstance(job.get("options"), dict) else {}
    winner = job.get("winner") if isinstance(job.get("winner"), dict) else {}
    by_symbol = job.get("bySymbol") if isinstance(job.get("bySymbol"), list) else []
    by_dir = job.get("byDirection") if isinstance(job.get("byDirection"), dict) else {}
    by_strat = job.get("byStrategy") if isinstance(job.get("byStrategy"), dict) else {}
    step_rows = []
    for step in steps:
        item = dict(by_step.get(str(step)) or {})
        item.pop("evaluationWindows", None)
        step_rows.append(item)
    range_rows = []
    for item in ranges:
        row = dict(item)
        row.pop("evaluationWindows", None)
        range_rows.append(row)
    heat = []
    for cell in heatmap:
        heat.append({
            "step": cell.get("step"),
            "slRatio": cell.get("slRatio"),
            "pf": cell.get("pf"),
            "maxDdS": cell.get("maxDdS"),
            "pfDdRatio": cell.get("pfDdRatio"),
            "n": cell.get("n"),
            "wr": cell.get("wr"),
            "validated": cell.get("validated"),
        })
    best_step = None
    if step_rows:
        best_step = sorted(
            step_rows,
            key=lambda r: (0 if r.get("validated") else 1, -float(r.get("pfDdRatio") or 0), -float(r.get("pf") or 0), float(r.get("maxDdS") or 9e9)),
        )[0]
    out = {
        "phase": str(job.get("phase") or "ready"),
        "ready": bool(job.get("ready")),
        "error": str(job.get("error") or ""),
        "source": str(job.get("source") or ""),
        "hours": HOURS,
        "stepLo": STEP_LO,
        "stepHi": STEP_HI,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsedMs": job.get("elapsedMs"),
        "workers": job.get("workers"),
        "timings": job.get("timings") or {},
        "options": options,
        "positivePf": POSITIVE_PF,
        "costPct": 0.10,
        "symbols": [r["symbol"] for r in ranked],
        "ranked": ranked,
        "universePreview": universe[:12],
        "coverage": {
            "sets": (coverage.get("sets") or {}),
            "symbols": (coverage.get("symbols") or {}),
            "bars": (coverage.get("bars") or {}),
            "setCount": coverage.get("setCount") or coverage.get("product"),
            "packs": coverage.get("packs"),
            "slRatios": coverage.get("slRatios"),
            "steps": coverage.get("steps"),
            "trails": len(coverage.get("trails") or []),
        },
        "validatedCount": job.get("validatedCount"),
        "rowCount": job.get("rowCount"),
        "winner": {
            "id": winner.get("id"),
            "step": winner.get("step"),
            "slRatio": winner.get("slRatio"),
            "pack": winner.get("pack"),
            "trailKey": winner.get("trailKey"),
            "last15Ratio": winner.get("last15Ratio"),
            "maxDdS": winner.get("maxDdS"),
            "n": winner.get("n"),
        } if winner else {},
        "bySymbol": [
            {k: v for k, v in row.items() if k != "evaluationWindows"}
            for row in by_symbol
        ],
        "byDirection": {
            k: {kk: vv for kk, vv in (v or {}).items() if kk != "evaluationWindows"}
            for k, v in by_dir.items() if isinstance(v, dict)
        },
        "byStrategy": {
            k: {kk: vv for kk, vv in (v or {}).items() if kk != "evaluationWindows"}
            for k, v in list(by_strat.items())[:16] if isinstance(v, dict)
        },
        "steps": steps,
        "byStep": step_rows,
        "ranges": range_rows,
        "heatmap": heat,
        "bestStep": best_step,
        "detail": job.get("detail") or "",
        "pct": job.get("pct") or 100,
    }
    _drop_windows(out)
    return out


def _drop_windows(value: Any) -> None:
    if isinstance(value, dict):
        value.pop("evaluationWindows", None)
        for child in value.values():
            _drop_windows(child)
    elif isinstance(value, list):
        for child in value:
            _drop_windows(child)


def svg_polyline(values: List[float], width: int, height: int, pad: int = 28) -> str:
    if not values:
        return ""
    lo = min(values)
    hi = max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = pad + (width - 2 * pad) * (i / max(1, n - 1))
        y = height - pad - (height - 2 * pad) * ((v - lo) / span)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _dd(value: Any) -> str:
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.2f}h"


def _cls(ok: Any) -> str:
    return "good" if ok else "bad"


def render_html(data: Dict[str, Any]) -> str:
    phase = str(data.get("phase") or "running")
    ready = bool(data.get("ready")) and phase == "ready" and not data.get("error")
    steps = data.get("byStep") or []
    ranges = data.get("ranges") or []
    ranked = data.get("ranked") or []
    heat = data.get("heatmap") or []
    symbols = data.get("symbols") or []
    pf_pts = svg_polyline([float(s.get("pf") or 0) for s in steps], 640, 220)
    dd_pts = svg_polyline([float(s.get("maxDdS") or 0) / 60.0 for s in steps], 640, 220)
    ratio_pts = svg_polyline([float(s.get("pfDdRatio") or 0) for s in steps], 640, 220)
    wr_pts = svg_polyline([float(s.get("wr") or 0) for s in steps], 640, 220)
    labels = "".join(
        f'<text x="{28 + (640 - 56) * (i / max(1, len(steps) - 1)):.1f}" y="214" text-anchor="middle">{_esc(s.get("step"))}</text>'
        for i, s in enumerate(steps)
    )
    step_rows = []
    for s in steps:
        step_rows.append(
            "<tr>"
            f"<td>{_esc(s.get('step'))}</td>"
            f"<td>{_num(s.get('tpPct'), 2)}%</td>"
            f"<td class={_cls(s.get('validated'))}>{_num(s.get('pf'), 3)}</td>"
            f"<td>{_num(s.get('classicPf'), 2)}</td>"
            f"<td>{_num(s.get('pfDdRatio'), 3)}</td>"
            f"<td>{_dd(s.get('maxDdS'))}</td>"
            f"<td>{_num(s.get('wr'), 1)}%</td>"
            f"<td>{_num(s.get('n'), 0)}</td>"
            f"<td>{_num(s.get('validatedSets'), 0)}/{_num(s.get('sets'), 0)}</td>"
            f"<td>{_num(s.get('netAvg'), 4)}</td>"
            "</tr>"
        )
    range_rows = []
    for s in ranges:
        range_rows.append(
            "<tr>"
            f"<td>{_esc(s.get('label'))}</td>"
            f"<td>{_esc(s.get('stepCount'))}</td>"
            f"<td class={_cls(s.get('validated'))}>{_num(s.get('pf'), 3)}</td>"
            f"<td>{_num(s.get('pfDdRatio'), 3)}</td>"
            f"<td>{_dd(s.get('maxDdS'))}</td>"
            f"<td>{_num(s.get('wr'), 1)}%</td>"
            f"<td>{_num(s.get('n'), 0)}</td>"
            f"<td>{_num(s.get('validatedSets'), 0)}/{_num(s.get('sets'), 0)}</td>"
            f"<td>{_num(s.get('netAvg'), 4)}</td>"
            "</tr>"
        )
    listing_blocks = []
    for s in steps:
        lines = []
        for i, row in enumerate(s.get("listings") or [], 1):
            lines.append(
                "<tr>"
                f"<td>{i}</td>"
                f"<td class=mono>{_esc(row.get('id'))}</td>"
                f"<td>{_esc(row.get('pack'))}</td>"
                f"<td>{_esc(row.get('kind'))}</td>"
                f"<td>{_num(row.get('slRatio'), 1)}</td>"
                f"<td>{_esc(row.get('trailKey') or 'base')}</td>"
                f"<td class={_cls(row.get('validated'))}>{_num(row.get('pf'), 3)}</td>"
                f"<td>{_dd(row.get('maxDdS'))}</td>"
                f"<td>{_num(row.get('n'), 0)}</td>"
                f"<td>{_num(row.get('wr'), 1)}%</td>"
                "</tr>"
            )
        listing_blocks.append(
            f"<article class=panel><h3>Step { _esc(s.get('step')) } · TP {_num(s.get('tpPct'), 2)}% · "
            f"{_num(s.get('validatedSets'), 0)} validated / {_num(s.get('listingCount') or len(s.get('listings') or []), 0)} sets</h3>"
            "<table><thead><tr><th>#</th><th>Set</th><th>Pack</th><th>Kind</th><th>SL</th><th>Trail</th><th>PF</th><th>DD</th><th>Fills</th><th>WR</th></tr></thead>"
            f"<tbody>{''.join(lines) or '<tr><td class=empty colspan=10>No fills</td></tr>'}</tbody></table></article>"
        )
    sls = sorted({round(float(c.get("slRatio") or 0), 1) for c in heat})
    step_ids = sorted({int(c.get("step") or 0) for c in heat})
    heat_map = {(int(c.get("step") or 0), round(float(c.get("slRatio") or 0), 1)): c for c in heat}
    pfs = [float(c.get("pf") or 0) for c in heat] or [1]
    pf_lo, pf_hi = min(pfs), max(pfs)
    heat_cells = []
    for step in step_ids:
        row = [f"<th>{step}</th>"]
        for sl in sls:
            cell = heat_map.get((step, sl))
            pf = float((cell or {}).get("pf") or 0)
            t = 0 if pf_hi == pf_lo else (pf - pf_lo) / (pf_hi - pf_lo)
            hue = 152 if (cell or {}).get("validated") else 8
            row.append(
                f'<td title="step {step} SL {sl} PF {pf:.3f}" style="background:hsla({hue},70%,{22 + t * 28:.0f}%,.92)">{_num(pf, 2)}</td>'
            )
        heat_cells.append("<tr>" + "".join(row) + "</tr>")
    scatter = []
    w, h, pad = 640, 280, 36
    pfs_s = [float(s.get("pf") or 0) for s in steps] or [1]
    dds_s = [float(s.get("maxDdS") or 0) / 60.0 for s in steps] or [1]
    for i, s in enumerate(steps):
        pf = float(s.get("pf") or 0)
        dd = float(s.get("maxDdS") or 0) / 60.0
        x = pad + (w - 2 * pad) * ((pf - min(pfs_s)) / ((max(pfs_s) - min(pfs_s)) or 1))
        y = h - pad - (h - 2 * pad) * ((dd - min(dds_s)) / ((max(dds_s) - min(dds_s)) or 1))
        r = 6 + min(18, (float(s.get("n") or 0) ** 0.5) / 8)
        scatter.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="rgba(61,207,142,.85)" />'
            f'<text x="{x:.1f}" y="{y - r - 6:.1f}" text-anchor="middle">{_esc(s.get("step"))}</text>'
        )
    radar = []
    keys = ("pf", "pfDdRatio", "wr", "validatedSets")
    maxes = {k: max((float(s.get(k) or 0) for s in steps), default=1) or 1 for k in keys}
    cx, cy, rr, nax = 180, 170, 110, len(keys)
    for i, k in enumerate(keys):
        ang = -3.14159 / 2 + i * 6.28318 / nax
        x, y = cx + rr * __import__("math").cos(ang), cy + rr * __import__("math").sin(ang)
        radar.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="#1d3a32" />')
        radar.append(f'<text x="{x:.1f}" y="{y:.1f}">{_esc(k)}</text>')
    colors = ["#3dcf8e", "#62e6ff", "#ffd166", "#ef6f63", "#c084fc", "#f472b6", "#34d399", "#fbbf24", "#38bdf8", "#a3e635"]
    for si, s in enumerate(steps):
        pts = []
        for i, k in enumerate(keys):
            ang = -3.14159 / 2 + i * 6.28318 / nax
            t = float(s.get(k) or 0) / maxes[k]
            x, y = cx + rr * t * __import__("math").cos(ang), cy + rr * t * __import__("math").sin(ang)
            pts.append(f"{x:.1f},{y:.1f}")
        radar.append(f'<polygon points="{" ".join(pts)}" fill="{colors[si % len(colors)]}" fill-opacity=".12" stroke="{colors[si % len(colors)]}" />')
    vol_rows = "".join(
        f"<tr><td>{i}</td><td>{_esc(r.get('symbol'))}</td><td>{_num(r.get('vol1h'), 3)}%</td>"
        f"<td>{_num(r.get('vol24h'), 3)}%</td><td>{_num(r.get('quoteVolume')/1e6, 1)}m</td>"
        f"<td>{_num(r.get('changePct'), 2)}%</td></tr>"
        for i, r in enumerate(ranked, 1)
    )
    sym_rows = "".join(
        f"<tr><td>{_esc(r.get('symbol'))}</td><td class={_cls(r.get('validated'))}>{_num(r.get('pf'), 3)}</td>"
        f"<td>{_dd(r.get('maxDdS'))}</td><td>{_num(r.get('n'), 0)}</td><td>{_num(r.get('wr'), 1)}%</td></tr>"
        for r in (data.get("bySymbol") or [])
    )
    best = data.get("bestStep") or {}
    status = "READY" if ready else ("ERROR" if data.get("error") else str(data.get("phase") or "RUNNING").upper())
    status_cls = "ok" if ready else ("bad" if data.get("error") else "warn")
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CTS-G · 24h step sweep 3–12</title>
<style>
:root {{ color-scheme:dark; --bg:#07110e; --fg:#d9f0e6; --muted:#7f9d90; --line:#1d3a32; --good:#3dcf8e; --bad:#ef6f63; --warn:#e0b15a; --cyan:#62e6ff; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:radial-gradient(circle at 12% 0,#16302a,#07110e 46%); color:var(--fg); font:14px/1.5 "IBM Plex Sans",ui-sans-serif,system-ui; }}
main {{ width:min(1280px,calc(100% - 32px)); margin:0 auto; padding:36px 0 80px; }}
h1 {{ font-size:clamp(28px,4vw,48px); letter-spacing:-.04em; margin:0 0 8px; }} h2 {{ margin:0 0 12px; font-size:18px; }} h3 {{ margin:0 0 10px; font-size:14px; }}
p {{ color:var(--muted); }} .badge {{ display:inline-flex; border:1px solid var(--line); border-radius:999px; padding:4px 10px; color:var(--cyan); font-size:11px; letter-spacing:.08em; text-transform:uppercase; }}
.hero {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-end; margin-bottom:22px; }}
.status {{ border:1px solid var(--line); border-radius:14px; padding:12px 14px; min-width:240px; }}
.status.ok {{ color:var(--good); background:#0b2a22; }} .status.bad {{ color:var(--bad); background:#2b1014; }} .status.warn {{ color:var(--warn); }}
.grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:16px 0 24px; }}
.card,.panel {{ background:linear-gradient(160deg,#0f221c,#0b1914); border:1px solid var(--line); border-radius:16px; }}
.card {{ padding:14px; }} .eyebrow {{ color:var(--muted); font-size:11px; letter-spacing:.1em; text-transform:uppercase; }}
.value {{ font-size:26px; font-weight:700; margin:6px 0 2px; letter-spacing:-.03em; }} .note {{ color:var(--muted); font-size:12px; }}
.panel {{ padding:18px; margin:14px 0; overflow:auto; }} .two {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ padding:8px 7px; border-bottom:1px solid rgba(125,170,150,.16); text-align:left; }}
th {{ color:var(--muted); font-size:11px; letter-spacing:.06em; text-transform:uppercase; }} .mono {{ font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:12px; }}
.good {{ color:var(--good); }} .bad {{ color:var(--bad); }} .empty {{ text-align:center; color:var(--muted); padding:18px; }}
svg {{ width:100%; height:auto; }} svg text {{ fill:var(--muted); font-size:10px; }}
.heat td {{ font-size:11px; text-align:center; font-variant-numeric:tabular-nums; }}
@media(max-width:900px) {{ .grid,.two,.hero {{ display:grid; grid-template-columns:1fr; }} }}
</style></head><body><main>
<header class=hero>
  <div><span class=badge>CTS-G · historic 24h · steps 3–12</span>
  <h1>Step-range simulation</h1>
  <p>Default hist book on the five most volatile USDT perps. TP step = N × 0.10% cost. Cost-net PF, intern 1.00, live floor { _esc(data.get("positivePf")) }. Axes off. Block on, DCA off.</p></div>
  <div class="status {status_cls}"><strong>{_esc(status)}</strong><br><span>{_esc(data.get("generatedAt"))} · {_esc(data.get("source") or "fetching")}</span></div>
</header>
<section class=grid>
  <article class=card><div class=eyebrow>Window</div><div class=value>24h</div><div class=note>1-minute bars + 30 warmup</div></article>
  <article class=card><div class=eyebrow>Symbols</div><div class=value>{len(symbols)}</div><div class=note>{_esc(", ".join(symbols) or "ranking…")}</div></article>
  <article class=card><div class=eyebrow>Best step</div><div class=value>{_esc((best or {}).get("step") or "—")}</div><div class=note>PF {_num((best or {}).get("pf"), 3)} · PF/DD {_num((best or {}).get("pfDdRatio"), 3)}</div></article>
  <article class=card><div class=eyebrow>Validated</div><div class=value>{_num(data.get("validatedCount"), 0)}</div><div class=note>{_num(data.get("rowCount"), 0)} ranked rows · {_esc(data.get("detail"))}</div></article>
</section>
<section class=panel><h2>Most volatile book</h2>
<table><thead><tr><th>#</th><th>Symbol</th><th>1H vol</th><th>24H range</th><th>Quote vol</th><th>24H chg</th></tr></thead><tbody>{vol_rows or '<tr><td class=empty colspan=6>Ranking…</td></tr>'}</tbody></table>
</section>
<section class="two">
  <article class=panel><h2>PF and drawdown vs step</h2>
    <svg viewBox="0 0 640 220" role="img" aria-label="PF and drawdown by step">
      <polyline fill="none" stroke="#3dcf8e" stroke-width="2.4" points="{pf_pts}"/>
      <polyline fill="none" stroke="#ef6f63" stroke-width="2" stroke-dasharray="5 4" points="{dd_pts}"/>
      {labels}
    </svg>
    <p>Green = last-N cost-net PF · dashed red = max drawdown time (minutes)</p>
  </article>
  <article class=panel><h2>PF/DD ratio and win rate</h2>
    <svg viewBox="0 0 640 220" role="img" aria-label="PF over DD and win rate">
      <polyline fill="none" stroke="#62e6ff" stroke-width="2.4" points="{ratio_pts}"/>
      <polyline fill="none" stroke="#ffd166" stroke-width="2" stroke-dasharray="4 4" points="{wr_pts}"/>
      {labels}
    </svg>
    <p>Cyan = PF / (hours of max DD + 0.05) · gold = win rate %</p>
  </article>
</section>
<section class="two">
  <article class=panel><h2>Multi-dimension · PF vs drawdown</h2>
    <svg viewBox="0 0 640 280">{"".join(scatter)}</svg>
    <p>X = PF · Y = max DD minutes · radius = fill count · label = step</p>
  </article>
  <article class=panel><h2>Radar · PF, PF/DD, WR, validated sets</h2>
    <svg viewBox="0 0 360 320">{"".join(radar)}</svg>
    <p>One polygon per step, normalized to the sweep max on each axis.</p>
  </article>
</section>
<section class=panel><h2>Line-by-line · each step</h2>
<table><thead><tr><th>Step</th><th>TP</th><th>PF</th><th>Classic</th><th>PF/DD</th><th>Max DD</th><th>WR</th><th>Fills</th><th>Valid/sets</th><th>Net avg</th></tr></thead>
<tbody>{"".join(step_rows) or '<tr><td class=empty colspan=10>Waiting for replay…</td></tr>'}</tbody></table>
</section>
<section class=panel><h2>Line-by-line · step range count (3→N)</h2>
<p>Each row adds the next TP step onto the same 24h tape. Range 3–12 is the full default-style grid used for this sweep.</p>
<table><thead><tr><th>Range</th><th>Count</th><th>PF</th><th>PF/DD</th><th>Max DD</th><th>WR</th><th>Fills</th><th>Valid/sets</th><th>Net avg</th></tr></thead>
<tbody>{"".join(range_rows) or '<tr><td class=empty colspan=9>Waiting for replay…</td></tr>'}</tbody></table>
</section>
<section class=panel><h2>Heatmap · step × SL:TP</h2>
<table class=heat><thead><tr><th>Step \\ SL</th>{"".join(f"<th>{sl:.1f}</th>" for sl in sls)}</tr></thead>
<tbody>{"".join(heat_cells) or '<tr><td class=empty>Heatmap fills after score</td></tr>'}</tbody></table>
</section>
<section class=panel><h2>Per-symbol 24h tape</h2>
<table><thead><tr><th>Symbol</th><th>PF</th><th>Max DD</th><th>Fills</th><th>WR</th></tr></thead>
<tbody>{sym_rows or '<tr><td class=empty colspan=5>—</td></tr>'}</tbody></table>
</section>
{"".join(listing_blocks)}
<p class=note>Source: independent hist_calc · persist off the live/VST job · intern PF 1.00 identity · live floor { _esc(data.get("positivePf")) } · no foreign flatten · simulated closes only.</p>
<script type="application/json" id="report-data">{payload}</script>
</main></body></html>"""


def main() -> int:
    _ensure_dir()
    os.environ["CTS_HIST_CALC_PATH"] = JOB_PATH
    publish({
        "phase": "rank",
        "ready": False,
        "pct": 2,
        "detail": "ranking 1H volatility",
        "hours": HOURS,
        "stepLo": STEP_LO,
        "stepHi": STEP_HI,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "symbols": [],
        "ranked": [],
        "byStep": [],
        "ranges": [],
        "heatmap": [],
        "positivePf": POSITIVE_PF,
    })
    ranked, universe = rank_volatile(TOP_N)
    symbols = [r["symbol"] for r in ranked]
    publish({
        "phase": "fetch",
        "ready": False,
        "pct": 8,
        "detail": f"top {len(symbols)} by 1H vol: {', '.join(symbols)}",
        "hours": HOURS,
        "stepLo": STEP_LO,
        "stepHi": STEP_HI,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "symbols": symbols,
        "ranked": ranked,
        "universePreview": universe[:12],
        "byStep": [],
        "ranges": [],
        "heatmap": [],
        "positivePf": POSITIVE_PF,
    })
    lookback = hours_to_bars(HOURS)
    warmup = HIST_WARMUP_BARS
    fetch_bars = lookback + warmup
    ov = {
        "histEnabled": True,
        "histLookbackBars": lookback,
        "histMinBars": min(60, lookback),
        "histWarmup": warmup,
        "histExactWindow": True,
        "setUseHistoricGate": True,
        "setStrictGate": True,
        "setAutoDeact": True,
        "setReactivate": True,
        "baseEvalPosCount": 30,
        "setPfWindow": 30,
        "setMinSamples": 30,
        "setMinPf": POSITIVE_PF,
        "minPf": POSITIVE_PF,
        "baseMinPf": POSITIVE_PF,
        "mainMinPf": POSITIVE_PF,
        "realMinPf": POSITIVE_PF,
        "setMaxDdTimeS": 57600,
        "setMinStep": STEP_LO,
        "setStepMax": STEP_HI,
        "stratTrailing": True,
        "stratIndications": True,
        "stratGeneral": True,
        "stratBlock": True,
        "blockEnabled": True,
        "blockActive": True,
        "dcaEnabled": False,
        "stratDca": False,
        "histSimulateBlock": True,
        "histSimulateDca": False,
        "blockVolumeRatio": 0.25,
        "blockMaxStack": 3,
        "trailArmMin": 0.3,
        "trailArmMax": 1.5,
        "trailGiveMin": 0.1,
        "trailGiveMax": 0.1,
        "exitIgnoreTp": True,
        "setHonorTp": True,
        "positionCostPct": 0.10,
        "positionCostFallbackPct": 0.10,
        "axisPrevEnabled": False,
        "axisLastEnabled": False,
        "axisContEnabled": False,
        "axisPauseEnabled": False,
        "indTypeState": True,
        "indTypeSignals": True,
        "indTypeDirection": True,
        "indTypeMove": True,
        "indTypeActive": True,
        "indTypeCommon": True,
        "indTypeTrend": True,
        "indTypeBreak": True,
        "slToTpRatios": [0.3, 0.6, 0.9, 1.2, 1.5, 1.8, 2.1, 2.4, 2.7, 3.0],
        "controlOrders": True,
        "normalExecutionEnabled": True,
    }
    sources = []
    t0 = time.time()
    book = SetBook()
    book.load(ov)
    for i, symbol in enumerate(symbols):
        publish({
            "phase": "fetch",
            "ready": False,
            "pct": 10 + int(20 * i / max(1, len(symbols))),
            "detail": f"fetch {symbol} {i + 1}/{len(symbols)} · {fetch_bars} 1m bars",
            "hours": HOURS,
            "stepLo": STEP_LO,
            "stepHi": STEP_HI,
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "symbols": symbols,
            "ranked": ranked,
            "universePreview": universe[:12],
            "byStep": [],
            "ranges": [],
            "heatmap": [],
            "positivePf": POSITIVE_PF,
            "coverage": {"setCount": len(book.by_idx), "steps": list(book.steps), "trails": len(book.trails)},
        })
        bars = fetch_klines(symbol, fetch_bars)
        src = "live"
        if len(bars) < min(80, lookback // 2):
            from set_engine import synth_trend
            bars = synth_trend(fetch_bars, 20.0 + i * 3.0, 0.08 if i % 2 == 0 else -0.07, 0.04)
            src = "synth"
        book.ingest_bars(symbol, bars)
        sources.append(src)
    source = "synth" if sources and all(x == "synth" for x in sources) else ("mixed" if any(x != "live" for x in sources) else "live")
    publish({
        "phase": "replay",
        "ready": False,
        "pct": 35,
        "detail": f"replay {len(book.by_idx)} sets × {len(symbols)} symbols · {source}",
        "hours": HOURS,
        "stepLo": STEP_LO,
        "stepHi": STEP_HI,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "symbols": symbols,
        "ranked": ranked,
        "source": source,
        "universePreview": universe[:12],
        "byStep": [],
        "ranges": [],
        "heatmap": [],
        "positivePf": POSITIVE_PF,
        "coverage": {"setCount": len(book.by_idx), "steps": list(book.steps), "trails": len(book.trails)},
    })
    workers = 1
    book.replay_all(symbols=symbols, workers=workers, merge=True, progress_total=len(symbols), score=True)
    ranked_sets = _rank_set_rows(book)
    by_step = step_rollup(book)
    by_sym = symbol_rollup(book)
    by_dir = direction_rollup(book)
    by_strat = strategy_rollup(book)
    winner = pick_winner_row(book, ranked_sets)
    listings = catalog_listings(book, ranked_sets, symbols)
    job = {
        "phase": "ready",
        "ready": True,
        "error": book.progress.error or "",
        "source": source,
        "hours": HOURS,
        "elapsedMs": round((time.time() - t0) * 1000.0, 1),
        "workers": workers,
        "timings": {"totalMs": round((time.time() - t0) * 1000.0, 1)},
        "options": {
            "hours": HOURS,
            "minStep": STEP_LO,
            "stepMax": STEP_HI,
            "trailing": True,
            "stratBlock": True,
            "stratDca": False,
            "stratIndications": True,
            "stratGeneral": True,
            "slToTpRatios": ov["slToTpRatios"],
            "trailArmMin": 0.3,
            "trailArmMax": 1.5,
            "trailGiveMin": 0.1,
            "trailGiveMax": 0.1,
            "blockMaxStack": 3,
            "axes": False,
            "costPct": 0.10,
            "setMinPf": POSITIVE_PF,
            "baseEvalPosCount": 30,
        },
        "coverage": {
            **book.coverage(),
            "setCount": len(book.by_idx),
            "trails": book.trails,
        },
        "validatedCount": sum(1 for item in ranked_sets if item[3]),
        "rowCount": len(ranked_sets),
        "rows": [set_row(st, side) for _k, st, side, _v, _l in ranked_sets[:80]],
        "winner": winner or {},
        "bySymbol": by_sym,
        "byDirection": by_dir,
        "byStrategy": by_strat,
        "byStep": by_step,
        "listings": listings,
        "detail": (
            f"{sum(1 for item in ranked_sets if item[3])}/{len(ranked_sets)} validated · "
            f"{sum(int(s.n or 0) for s in book.sets.values())} fills · {source}"
        ),
        "pct": 100,
    }
    summary = compact_job(job, ranked, universe)
    publish(summary)
    print(json.dumps({
        "ok": bool(summary.get("ready")) and not summary.get("error"),
        "symbols": symbols,
        "phase": summary.get("phase"),
        "bestStep": (summary.get("bestStep") or {}).get("step"),
        "validated": summary.get("validatedCount"),
        "elapsedMs": summary.get("elapsedMs"),
        "sets": len(book.by_idx),
        "html": PUBLIC_HTML,
    }, indent=2))
    return 0 if summary.get("ready") and not summary.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
