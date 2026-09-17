#!/usr/bin/env python3
"""70-hour complete trade simulation on 5 symbols.

Public 1m candles when BingX is reachable, synth fallback otherwise.
Does not start Live/VST engines or flatten positions.

Computes every indication kind, strategy, pack, side, SL/step/trail type
and last-N position eval windows 10..70 step 10.
"""
from __future__ import annotations

import html
import json
import math
import os
import sys
import time
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "pulse"))

from combo_eval import (  # noqa: E402
    INDICATIONS,
    KIND_LANES,
    MATRIX_EMPTY,
    PF_FAMILIES,
    STRATEGIES,
    _Acc,
    _config_of,
    _indication_of,
    _lane_of,
    _score,
    _strategy_of,
    _unwrap,
    evaluate_book,
    iter_book_fills,
)
from hist_calc import fetch_klines, hours_to_bars  # noqa: E402
from hist_test import test_overlay  # noqa: E402
from position_cost import (  # noqa: E402
    POSITIVE_PF,
    evaluation_windows,
    is_positive_pf,
    last_n_cost_pf,
    overall_last_pos_eval,
)
from set_engine import IND_KINDS, SetBook, synth_trend  # noqa: E402

SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BCH-USDT"]
HOURS = 70
LAST_N = (10, 20, 30, 40, 50, 60, 70)
OUT_DIR = ROOT / "reports"
PUBLIC = ROOT / "public"
JSON_PATH = OUT_DIR / "sim-70h-five.json"
HTML_PATH = OUT_DIR / "sim-70h-five.html"
PROGRESS_PATH = OUT_DIR / "sim-70h-five.progress.json"
PUBLIC_JSON = PUBLIC / "sim-70h.json"
PUBLIC_HTML = PUBLIC / "sim-70h.html"
PUBLIC_PROGRESS = PUBLIC / "sim-70h.progress.json"

TYPES = (
    "indication",
    "strategy",
    "pack",
    "side",
    "sl",
    "step",
    "trail",
    "symbol",
    "combo",
)


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_progress(blob: Dict[str, Any]) -> None:
    payload = dict(blob)
    payload["t"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload.setdefault("hours", HOURS)
    payload.setdefault("lastN", list(LAST_N))
    payload.setdefault("symbols", SYMBOLS)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    _atomic(PROGRESS_PATH, text)
    _atomic(PUBLIC_PROGRESS, text)
    slim = {k: payload[k] for k in payload if k in (
        "phase", "pct", "detail", "done", "total", "hours", "t", "elapsedS",
        "histFills", "validated", "ok", "source",
    )}
    print(json.dumps(slim, ensure_ascii=False), flush=True)


def fetch_one(symbol: str, bars_n: int) -> tuple[str, List[List[float]], str]:
    try:
        rows = fetch_klines(symbol, bars_n)
        return symbol, rows if isinstance(rows, list) else [], ""
    except Exception as exc:
        return symbol, [], f"{type(exc).__name__}: {exc}"


def _get(row: Any, *keys: str, default: Any = None) -> Any:
    if hasattr(row, "get"):
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                return value
    extra = getattr(row, "_extra", None) or {}
    for key in keys:
        if key in extra and extra[key] not in (None, ""):
            return extra[key]
        if hasattr(row, key):
            value = getattr(row, key)
            if value not in (None, ""):
                return value
    return default


def side_of(row: Any) -> str:
    return str(_get(row, "side", default="") or "").upper()


def kind_of(row: Any) -> str:
    return str(_get(row, "ind_kind", "indKind", default="") or "")


def symbol_of(row: Any) -> str:
    return str(_get(row, "symbol", default="") or "")


def windows_of(rows: Sequence[Any], cost: float, min_pf: float, required: int = 8) -> Dict[str, Any]:
    blob = evaluation_windows(rows, cost, windows=LAST_N, required_samples=required, ordered=False)
    out: Dict[str, Any] = {}
    for n in LAST_N:
        cell = blob.get(f"last{n}") or {}
        pf = cell.get("pf")
        count = int(cell.get("n") or 0)
        out[f"last{n}"] = {
            "requestedN": n,
            "n": count,
            "pf": None if pf is None else round(float(pf), 4),
            "classicPf": None if cell.get("classicPf") in (None, 0) and not count else cell.get("classicPf"),
            "wr": cell.get("wr"),
            "netAvg": cell.get("netAvg"),
            "available": bool(cell.get("available")),
            "validated": bool(count >= min(n, required) and is_positive_pf(pf, min_pf)),
        }
    overall = overall_last_pos_eval(rows, max(LAST_N), cost)
    out["full"] = {
        "n": int(overall.get("count") or 0),
        "pf": None if overall.get("ratio") is None else round(float(overall["ratio"]), 4),
        "classicPf": overall.get("classicPf"),
        "wr": overall.get("wr"),
        "maxDdS": overall.get("maxDdS") if overall.get("maxDdS") is not None else 0.0,
        "avgDdS": overall.get("avgDdS") if overall.get("avgDdS") is not None else 0.0,
    }
    return out


def downsample_closes(bars: List[List[float]], points: int = 140) -> List[Dict[str, Any]]:
    if not bars:
        return []
    step = max(1, len(bars) // points)
    out: List[Dict[str, Any]] = []
    for i in range(0, len(bars), step):
        row = bars[i]
        if not isinstance(row, (list, tuple)) or len(row) < 4:
            continue
        out.append({"i": i, "o": float(row[0]), "h": float(row[1]), "l": float(row[2]), "c": float(row[3])})
    last = bars[-1]
    if out and isinstance(last, (list, tuple)) and len(last) >= 4:
        last_pt = {"i": len(bars) - 1, "o": float(last[0]), "h": float(last[1]), "l": float(last[2]), "c": float(last[3])}
        if last_pt["c"] != out[-1]["c"] or last_pt["i"] != out[-1]["i"]:
            out.append(last_pt)
    return out


def combo_multi(book: Any, min_pf: float, cost: float) -> Dict[str, Any]:
    combo_acc: Dict[Tuple[str, str, str, str], _Acc] = {}
    matrix_acc: Dict[Tuple[str, str], _Acc] = {}
    family_acc: Dict[str, _Acc] = {key: _Acc() for key in PF_FAMILIES}
    with_acc: Dict[str, Dict[str, _Acc]] = {
        "block": {"with": _Acc(), "without": _Acc()},
        "dca": {"with": _Acc(), "without": _Acc()},
    }
    for item in iter_book_fills(book):
        row, meta = _unwrap(item)
        if row is None:
            continue
        indication = _indication_of(row, meta)
        if indication not in set(INDICATIONS):
            indication = "combined"
        strategy = _strategy_of(row, meta)
        if strategy not in set(STRATEGIES):
            strategy = "normal"
        config = _config_of(row, meta)
        set_id = str((meta or {}).get("set_id") or "")
        if not set_id:
            set_id = f"{indication}:{config}:{strategy}"
        lane = _lane_of(row, meta)
        t = float(_get(row, "t", default=0) or 0)
        pnl_pct = float(_get(row, "pnl_pct", "pnlPct", default=0) or 0)
        hold = float(_get(row, "hold_s", "holdS", default=0) or 0)
        key = (indication, config, strategy, set_id)
        acc = combo_acc.get(key)
        if acc is None:
            acc = _Acc()
            combo_acc[key] = acc
        acc.add(t, pnl_pct, hold)
        mkey = (indication, strategy)
        matt = matrix_acc.get(mkey)
        if matt is None:
            matt = _Acc()
            matrix_acc[mkey] = matt
        matt.add(t, pnl_pct, hold)
        if lane not in KIND_LANES:
            family_acc["overall"].add(t, pnl_pct, hold)
            if strategy in family_acc:
                family_acc[strategy].add(t, pnl_pct, hold)
            with_acc["block"]["with"].add(t, pnl_pct, hold)
            with_acc["dca"]["with"].add(t, pnl_pct, hold)
            if strategy != "block":
                with_acc["block"]["without"].add(t, pnl_pct, hold)
            if strategy != "dca":
                with_acc["dca"]["without"].add(t, pnl_pct, hold)

    def score_map(acc: _Acc) -> Dict[str, Any]:
        if acc.n <= 0:
            return {f"last{n}": dict(MATRIX_EMPTY, requestedN=n) for n in LAST_N}
        return {f"last{n}": {**_score(acc, cost, n, min_pf), "requestedN": n} for n in LAST_N}

    matrix = []
    for indication in INDICATIONS:
        for strategy in STRATEGIES:
            acc = matrix_acc.get((indication, strategy)) or _Acc()
            matrix.append({"indication": indication, "strategy": strategy, "n": acc.n, "windows": score_map(acc)})
    families = {name: {"n": family_acc[name].n, "windows": score_map(family_acc[name])} for name in PF_FAMILIES}
    with_without = {
        name: {
            "with": {"n": pair["with"].n, "windows": score_map(pair["with"])},
            "without": {"n": pair["without"].n, "windows": score_map(pair["without"])},
        }
        for name, pair in with_acc.items()
    }
    successful_by_n: Dict[str, List[Dict[str, Any]]] = {f"last{n}": [] for n in LAST_N}
    for (indication, config, strategy, set_id), acc in combo_acc.items():
        scored = score_map(acc)
        for n in LAST_N:
            cell = scored[f"last{n}"]
            if cell.get("validated"):
                successful_by_n[f"last{n}"].append({
                    "setId": set_id,
                    "indication": indication,
                    "strategy": strategy,
                    "config": config,
                    "n": cell.get("evalN") or cell.get("n"),
                    "pf": cell.get("pf"),
                    "wr": cell.get("wr"),
                    "maxDdS": cell.get("maxDdS"),
                })
    for key, rows in successful_by_n.items():
        rows.sort(key=lambda r: (-float(r.get("pf") or 0), -int(r.get("n") or 0)))
        successful_by_n[key] = rows[:40]
    kinds_seen = {k[0] for k in combo_acc}
    strats_seen = {k[2] for k in combo_acc}
    return {
        "matrix": matrix,
        "families": families,
        "withWithout": with_without,
        "successfulByN": successful_by_n,
        "comboCount": len(combo_acc),
        "coverage": {
            "indications": {k: k in kinds_seen for k in INDICATIONS},
            "strategies": {k: k in strats_seen or family_acc[k].n > 0 for k in STRATEGIES},
            "families": {k: family_acc[k].n > 0 for k in PF_FAMILIES},
            "types": {k: True for k in TYPES},
        },
    }


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def num(value: Any, digits: int = 2) -> str:
    try:
        if value is None:
            return "—"
        return f"{float(value):.{digits}f}"
    except Exception:
        return "—"


def pf_tone(pf: Any, validated: bool = False) -> str:
    try:
        v = float(pf)
    except Exception:
        return "faint"
    if validated or v >= 1.15:
        return "good"
    if v >= 1.0:
        return "warn"
    return "bad"


def svg_line(series: Dict[str, List[float]], xs: Sequence[int], *, width: int = 720, height: int = 240, y0: float = 0.6, y1: float = 1.8) -> str:
    pad_l, pad_r, pad_t, pad_b = 44, 16, 16, 28
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b
    palette = ["#3dcf8e", "#62e6ff", "#e0b15a", "#ef6f63", "#9b8cff", "#7f9d90", "#f27bbd", "#d9f0e6"]
    def x_at(i: int) -> float:
        return pad_l + (iw * i / max(1, len(xs) - 1))
    def y_at(v: float) -> float:
        v = min(y1, max(y0, float(v)))
        return pad_t + ih * (1 - (v - y0) / (y1 - y0))
    grid = []
    for g in (1.0, 1.15, 1.5):
        if y0 <= g <= y1:
            yy = y_at(g)
            grid.append(f'<line x1="{pad_l}" x2="{width-pad_r}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="#1d3a32" stroke-dasharray="3 3"/>')
            grid.append(f'<text x="6" y="{yy+4:.1f}" fill="#7f9d90" font-size="10">{g:.2f}</text>')
    ticks = []
    for i, x in enumerate(xs):
        ticks.append(f'<text x="{x_at(i):.1f}" y="{height-8}" text-anchor="middle" fill="#7f9d90" font-size="10">{x}</text>')
    paths = []
    legend = []
    for idx, (name, vals) in enumerate(series.items()):
        color = palette[idx % len(palette)]
        pts = []
        for i, v in enumerate(vals):
            if v is None:
                continue
            pts.append(f"{x_at(i):.1f},{y_at(float(v)):.1f}")
        if len(pts) >= 2:
            paths.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{" ".join(pts)}"/>')
        legend.append(f'<circle cx="{pad_l + idx*92}" cy="10" r="3.5" fill="{color}"/><text x="{pad_l + 8 + idx*92}" y="13" fill="#d9f0e6" font-size="10">{esc(name)}</text>')
    return f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="PF versus last-N">{"".join(grid)}{"".join(paths)}{"".join(ticks)}{"".join(legend)}</svg>'


def svg_bars(items: List[Tuple[str, float]], *, width: int = 720, height: int = 220, unit: str = "") -> str:
    if not items:
        return '<p class="empty">No bars</p>'
    pad_l, pad_r, pad_t, pad_b = 90, 16, 12, 16
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b
    mx = max((abs(v) for _, v in items), default=1.0) or 1.0
    bh = max(8, ih / max(1, len(items)) - 4)
    rows = []
    for i, (label, value) in enumerate(items):
        y = pad_t + i * (ih / max(1, len(items)))
        w = (abs(value) / mx) * iw
        color = "#3dcf8e" if value >= 1.15 else "#e0b15a" if value >= 1.0 else "#ef6f63"
        rows.append(
            f'<text x="8" y="{y+bh*0.7:.1f}" fill="#7f9d90" font-size="11">{esc(label)}</text>'
            f'<rect x="{pad_l}" y="{y:.1f}" width="{w:.1f}" height="{bh:.1f}" rx="3" fill="{color}"/>'
            f'<text x="{pad_l+w+6:.1f}" y="{y+bh*0.7:.1f}" fill="#d9f0e6" font-size="11">{value:.2f}{esc(unit)}</text>'
        )
    return f'<svg viewBox="0 0 {width} {height}" role="img">{"".join(rows)}</svg>'


def heatmap_table(matrix: List[Dict[str, Any]], n: int) -> str:
    indications = list(INDICATIONS)
    strategies = list(STRATEGIES)
    lookup = {(r.get("indication"), r.get("strategy")): r for r in matrix}
    head = "".join(f"<th>{esc(s)}</th>" for s in strategies)
    body = []
    for ind in indications:
        cells = [f"<th>{esc(ind)}</th>"]
        for strat in strategies:
            row = lookup.get((ind, strat)) or {}
            cell = (row.get("windows") or {}).get(f"last{n}") or {}
            pf = cell.get("pf")
            count = int(cell.get("evalN") or cell.get("n") or row.get("n") or 0)
            tone = pf_tone(pf, bool(cell.get("validated")))
            cells.append(
                f'<td class="{tone}"><strong>{num(pf, 2)}</strong><span class="sub">{count} fills</span></td>'
            )
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table class=heat><thead><tr><th>Indication \\ Strategy</th>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def window_table(label: str, groups: Dict[str, Dict[str, Any]]) -> str:
    heads = "".join(f"<th>Last {n}</th>" for n in LAST_N)
    rows = []
    for name, blob in groups.items():
        cells = [f"<th>{esc(name)}</th>"]
        for n in LAST_N:
            cell = (blob.get("windows") or blob).get(f"last{n}") or {}
            tone = pf_tone(cell.get("pf"), bool(cell.get("validated")))
            avail = "ok" if cell.get("available") else "partial"
            cells.append(
                f'<td class="{tone}"><strong>{num(cell.get("pf"), 2)}</strong>'
                f'<span class="sub">{cell.get("n") or 0}/{n} · {avail}</span></td>'
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr><th>{esc(label)}</th>{heads}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def build_html(report: Dict[str, Any]) -> str:
    generated = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lastn = report.get("lastN") or list(LAST_N)
    overall = report.get("overallWindows") or {}
    overall_line = [float((overall.get(f"last{n}") or {}).get("pf") or 1) for n in lastn]
    fam_series: Dict[str, List[float]] = {}
    for name, blob in (report.get("combo", {}).get("families") or {}).items():
        fam_series[name] = [float(((blob.get("windows") or {}).get(f"last{n}") or {}).get("pf") or 1) for n in lastn]
    ind_series: Dict[str, List[float]] = {}
    for name, blob in (report.get("byIndication") or {}).items():
        ind_series[name] = [float(((blob.get("windows") or blob).get(f"last{n}") or {}).get("pf") or 1) for n in lastn]
    strat_bars = [(k, float((((v.get("windows") or v).get("last30") or {}).get("pf") or 0))) for k, v in (report.get("byStrategy") or {}).items()]
    ind_bars = [(k, float((((v.get("windows") or v).get("last30") or {}).get("pf") or 0))) for k, v in (report.get("byIndication") or {}).items()]
    type_bars = [(k, float((((v.get("windows") or v).get("last30") or {}).get("pf") or 0))) for k, v in (report.get("byType") or {}).items()]
    validated_line = [int((report.get("validatedByN") or {}).get(f"last{n}") or 0) for n in lastn]
    status = "ok" if report.get("ok") else "bad"
    issues = report.get("issues") or []
    source = report.get("source") or "unknown"
    top_blocks = []
    for n in lastn:
        rows = (report.get("combo", {}).get("successfulByN") or {}).get(f"last{n}") or []
        body = "".join(
            f"<tr><td>{esc(r.get('setId'))}</td><td>{esc(r.get('indication'))}</td>"
            f"<td>{esc(r.get('strategy'))}</td><td>{num(r.get('pf'), 3)}</td>"
            f"<td>{esc(r.get('n'))}</td><td>{num(r.get('wr'), 1)}%</td></tr>"
            for r in rows[:12]
        ) or '<tr><td class=empty colspan=6>No validated positive configs at this last-N</td></tr>'
        top_blocks.append(
            f"<h3>Last {n}</h3><table><thead><tr><th>Set</th><th>Indication</th><th>Strategy</th><th>PF</th><th>N</th><th>WR</th></tr></thead><tbody>{body}</tbody></table>"
        )
    price_svgs = []
    for symbol, path in (report.get("pricePaths") or {}).items():
        if not path:
            continue
        xs = list(range(len(path)))
        closes = [float(p.get("c") or 0) for p in path]
        if not any(closes):
            continue
        base = closes[0] or 1
        norm = [c / base for c in closes]
        # fake LAST_N-shaped x for reuse? custom mini chart
        width, height, pad = 720, 140, 28
        mn, mx = min(norm), max(norm)
        span = (mx - mn) or 0.01
        pts = []
        for i, v in enumerate(norm):
            x = pad + (720 - 2 * pad) * i / max(1, len(norm) - 1)
            y = 16 + (height - 36) * (1 - (v - mn) / span)
            pts.append(f"{x:.1f},{y:.1f}")
        price_svgs.append(
            f'<figure><figcaption>{esc(symbol)}</figcaption>'
            f'<svg viewBox="0 0 {width} {height}"><polyline fill="none" stroke="#3dcf8e" stroke-width="1.6" points="{" ".join(pts)}"/>'
            f'<text x="8" y="14" fill="#7f9d90" font-size="10">start {closes[0]:.4g} · last {closes[-1]:.4g}</text></svg></figure>'
        )
    data = json.dumps({k: report[k] for k in report if k not in ("blockRows",)}, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CTS-G · 70h complete trade test</title>
<style>
:root {{ color-scheme:dark; --bg:#07110e; --panel:#0f221c; --line:#1d3a32; --text:#d9f0e6; --muted:#7f9d90; --cyan:#3dcf8e; --warn:#e0b15a; --bad:#ef6f63; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.5 "IBM Plex Sans",ui-sans-serif,system-ui,sans-serif; }}
main {{ width:min(1280px,calc(100% - 32px)); margin:0 auto; padding:36px 0 72px; }}
h1 {{ font-size:clamp(28px,4vw,48px); letter-spacing:-.04em; margin:8px 0 12px; }} h2 {{ font-size:18px; margin:0 0 12px; }} h3 {{ font-size:14px; color:var(--muted); }}
p {{ color:var(--muted); }} .hero {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-end; flex-wrap:wrap; }}
.badge {{ display:inline-flex; border:1px solid var(--line); border-radius:999px; padding:4px 10px; color:var(--cyan); font-size:11px; letter-spacing:.08em; text-transform:uppercase; }}
.status {{ border:1px solid var(--line); border-radius:14px; padding:12px 14px; min-width:240px; }} .status.ok {{ color:var(--cyan); }} .status.bad {{ color:var(--bad); }}
.grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:20px 0; }}
.card,.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:16px; }} .card {{ padding:16px; }} .panel {{ padding:20px; margin:16px 0; overflow:auto; }}
.eyebrow {{ color:var(--muted); font-size:11px; letter-spacing:.1em; text-transform:uppercase; }} .value {{ font-size:26px; font-weight:700; margin:6px 0 2px; letter-spacing:-.03em; }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ border-bottom:1px solid rgba(127,157,144,.18); padding:8px; text-align:left; vertical-align:top; }}
th {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.06em; font-weight:600; }}
.sub {{ display:block; color:var(--muted); font-size:10px; }} .good {{ color:var(--cyan); }} .warn {{ color:var(--warn); }} .bad {{ color:var(--bad); }} .faint {{ color:var(--muted); }}
.heat td {{ text-align:center; }} .two {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }} .prices {{ display:grid; grid-template-columns:1fr; gap:10px; }}
svg {{ width:100%; height:auto; background:#0b1914; border-radius:12px; }} figure {{ margin:0; }} figcaption {{ color:var(--muted); font-size:12px; margin:0 0 6px; }}
.empty {{ text-align:center; color:var(--muted); padding:18px; }} .foot {{ font-size:12px; color:var(--muted); }}
@media(max-width:900px) {{ .grid,.two {{ grid-template-columns:1fr 1fr; }} }} @media(max-width:640px) {{ .grid,.two {{ grid-template-columns:1fr; }} main {{ width:calc(100% - 20px); }} }}
</style></head><body><main>
<header class="hero">
  <div>
    <span class="badge">CTS-G · 70-hour simulation · last-N 10–70</span>
    <h1>Complete trade test · 5 symbols</h1>
    <p>All indication kinds, strategies, packs, sides, SL/step/trail types and last-N position windows. Cost-net PF. Independent tapes — no cross-set averaging.</p>
  </div>
  <div class="status {status}">
    <strong>{"READY" if report.get("ok") else "REVIEW"}</strong><br>
    {esc(generated)} · {esc(source)} · {esc(report.get("elapsedS"))}s
  </div>
</header>
<section class="grid">
  <article class="card"><div class="eyebrow">Window</div><div class="value">70h</div><div class="eyebrow">{int(report.get("lookback") or 0):,} 1m bars / symbol</div></article>
  <article class="card"><div class="eyebrow">Symbols</div><div class="value">{len(report.get("symbols") or [])}</div><div class="eyebrow">{esc(", ".join(report.get("symbols") or []))}</div></article>
  <article class="card"><div class="eyebrow">Sets processed</div><div class="value">{int(report.get("processedCount") or 0):,}</div><div class="eyebrow">{int(report.get("setCount") or 0):,} catalog · {int(report.get("histFills") or 0):,} fills</div></article>
  <article class="card"><div class="eyebrow">Validated @ last 30</div><div class="value">{int((report.get("validatedByN") or {}).get("last30") or 0):,}</div><div class="eyebrow">floor PF {esc(report.get("minPf"))}</div></article>
</section>
<section class="panel"><h2>Last-N position eval · overall PF</h2>
<p>Each point is the cost-net last-N of the merged independent tapes. Qualification uses the named window itself — last-70 cannot veto last-10.</p>
{svg_line({"overall": overall_line, **{k: v for k, v in fam_series.items() if k in ("overall", "normal", "trailing", "block", "dca")}}, lastn)}
{window_table("Overall / families", {"overall": {"windows": overall}, **(report.get("combo", {}).get("families") or {})})}
</section>
<section class="panel"><h2>Indications · PF vs last-N</h2>
<p>Eight kinds plus general/combined. Each kind keeps its own tape.</p>
{svg_line(ind_series, lastn, y0=0.5, y1=2.0)}
{window_table("Indication", report.get("byIndication") or {})}
{svg_bars(ind_bars)}
</section>
<section class="panel"><h2>Strategies · last 30 PF</h2>
{svg_bars(strat_bars)}
{window_table("Strategy", report.get("byStrategy") or {})}
</section>
<section class="panel"><h2>Indication × strategy heatmaps</h2>
<p>Last 30 (canonical live window) and last 70 (full requested evidence).</p>
<div class="two"><div><h3>Last 30</h3>{heatmap_table(report.get("combo", {}).get("matrix") or [], 30)}</div>
<div><h3>Last 70</h3>{heatmap_table(report.get("combo", {}).get("matrix") or [], 70)}</div></div>
</section>
<section class="panel"><h2>Types and possibilities</h2>
<p>Sides, packs, trail vs base, SL ratios, TP steps, with/without Block and DCA.</p>
<div class="two">
  <div><h3>Sides</h3>{window_table("Side", report.get("bySide") or {})}</div>
  <div><h3>Packs</h3>{window_table("Pack", report.get("byPack") or {})}</div>
</div>
<div class="two">
  <div><h3>Trail types</h3>{window_table("Trail", report.get("byTrail") or {})}</div>
  <div><h3>SL:TP</h3>{window_table("SL", report.get("bySl") or {})}</div>
</div>
<h3>TP steps</h3>{window_table("Step", report.get("byStep") or {})}
<h3>Type rollup @ last 30</h3>{svg_bars(type_bars)}
<h3>With / without Block and DCA</h3>
{window_table("Block with", {"block-with": ((report.get("combo") or {}).get("withWithout") or {}).get("block", {}).get("with") or {}, "block-without": ((report.get("combo") or {}).get("withWithout") or {}).get("block", {}).get("without") or {}, "dca-with": ((report.get("combo") or {}).get("withWithout") or {}).get("dca", {}).get("with") or {}, "dca-without": ((report.get("combo") or {}).get("withWithout") or {}).get("dca", {}).get("without") or {}})}
</section>
<section class="panel"><h2>Per-symbol results</h2>
{window_table("Symbol", report.get("bySymbol") or {})}
<div class="prices">{"".join(price_svgs)}</div>
</section>
<section class="panel"><h2>Validated configs by last-N</h2>
<p>Positive-PF configs at each window. Count trend: {esc(", ".join(str(v) for v in validated_line))}.</p>
{"".join(top_blocks)}
</section>
<section class="panel"><h2>Coverage contract</h2>
<table><tbody>
<tr><th>Indications</th><td>{esc(", ".join(k for k,v in ((report.get("combo") or {}).get("coverage") or {}).get("indications", {}).items() if v) or "none")}</td></tr>
<tr><th>Strategies</th><td>{esc(", ".join(k for k,v in ((report.get("combo") or {}).get("coverage") or {}).get("strategies", {}).items() if v) or "none")}</td></tr>
<tr><th>Families</th><td>{esc(", ".join(k for k,v in ((report.get("combo") or {}).get("coverage") or {}).get("families", {}).items() if v) or "none")}</td></tr>
<tr><th>Issues</th><td>{esc("; ".join(issues) if issues else "none")}</td></tr>
<tr><th>Fetch errors</th><td>{esc(json.dumps(report.get("errors") or {}))}</td></tr>
</tbody></table>
</section>
<p class="foot">Offline historic simulation. Cost-net PF. Does not place, close or flatten live orders. Last-N windows 10, 20, 30, 40, 50, 60, 70 evaluated independently.</p>
<script type="application/json" id="report-data">{data}</script>
</main></body></html>"""


def group_windows(buckets: Dict[str, List[Any]], cost: float, min_pf: float) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, rows in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        if not name:
            continue
        out[str(name)] = {"n": len(rows), "windows": windows_of(rows, cost, min_pf)}
    return out


def main() -> int:
    hours = int(os.environ.get("SIM_HOURS") or HOURS)
    symbols = [s.strip().upper() for s in (os.environ.get("SIM_SYMBOLS") or ",".join(SYMBOLS)).split(",") if s.strip()]
    min_pf = float(os.environ.get("SIM_MIN_PF") or POSITIVE_PF)
    mode = str(os.environ.get("SIM_MODE") or "live").strip().lower()
    overlay = test_overlay(min(64, hours), min_pf)
    overlay["histLookbackBars"] = hours_to_bars(hours)
    overlay["histMinBars"] = min(120, overlay["histLookbackBars"])
    overlay["histTestHours"] = hours
    overlay["blockEvalPosCount"] = 50
    overlay["blockOverall"] = True
    overlay["histSimulateBlock"] = True
    overlay["histSimulateDca"] = True
    overlay["stratBlock"] = True
    overlay["blockEnabled"] = True
    overlay["dcaEnabled"] = True
    overlay["stratDca"] = True
    overlay["axisPrevEnabled"] = True
    overlay["axisLastEnabled"] = True
    overlay["axisContEnabled"] = True
    overlay["axisPauseEnabled"] = True
    lookback = int(overlay["histLookbackBars"])
    warmup = int(overlay.get("histWarmup") or 60)
    fetch_n = lookback + warmup
    t0 = time.monotonic()
    bars_by: Dict[str, List[List[float]]] = {}
    errors: Dict[str, str] = {}
    source = "bingx"
    if mode == "synth":
        source = "synth"
        steps = {"BTC-USDT": 0.22, "ETH-USDT": 0.18, "SOL-USDT": 0.16, "XRP-USDT": 0.12, "BCH-USDT": 0.14}
        write_progress({"phase": "synth", "pct": 3, "detail": f"synth {len(symbols)} × {fetch_n} bars", "source": source})
        for i, symbol in enumerate(symbols, 1):
            bars_by[symbol] = synth_trend(fetch_n, start=80.0 + i * 4, step=steps.get(symbol, 0.12), noise=0.035)
            write_progress({"phase": "synth", "pct": 3 + int(14 * i / max(1, len(symbols))), "detail": f"synth {symbol}", "done": i, "total": len(symbols), "source": source})
    else:
        write_progress({"phase": "fetch", "pct": 2, "detail": f"fetch {len(symbols)} × {fetch_n} 1m bars · {hours}h", "source": source})
        with ThreadPoolExecutor(max_workers=min(5, len(symbols))) as pool:
            futs = [pool.submit(fetch_one, s, fetch_n) for s in symbols]
            for i, fut in enumerate(as_completed(futs), 1):
                symbol, rows, err = fut.result()
                if err or len(rows) < min(180, fetch_n // 4):
                    errors[symbol] = err or f"bars {len(rows)}"
                else:
                    bars_by[symbol] = rows
                write_progress({"phase": "fetch", "pct": 2 + int(16 * i / max(1, len(symbols))), "detail": f"fetched {symbol} · {len(rows)} bars", "done": i, "total": len(symbols), "errors": errors, "source": source})
        if len(bars_by) < len(symbols):
            source = "mixed-synth" if bars_by else "synth"
            write_progress({"phase": "synth", "pct": 18, "detail": "BingX incomplete — synth remainder", "errors": errors, "source": source})
            steps = {"BTC-USDT": 0.22, "ETH-USDT": 0.18, "SOL-USDT": 0.16, "XRP-USDT": 0.12, "BCH-USDT": 0.14}
            for i, symbol in enumerate(symbols, 1):
                if symbol in bars_by:
                    continue
                bars_by[symbol] = synth_trend(fetch_n, start=80.0 + i * 4, step=steps.get(symbol, 0.12), noise=0.035)
                errors[symbol] = (errors.get(symbol) or "synth-fallback")
    if not bars_by:
        write_progress({"phase": "error", "pct": 100, "detail": "no candles", "errors": errors})
        return 2

    book = SetBook()
    book.load(overlay)
    for symbol, rows in bars_by.items():
        book.ingest_bars(symbol, rows)
    names = list(bars_by)

    def on_symbol(sym: str, done: int, total: int) -> None:
        write_progress({"phase": "replay", "pct": 22 + int(46 * done / max(1, total)), "detail": f"replay {sym} · {done}/{total}", "done": done, "total": total, "source": source})

    workers = max(1, min(2, os.cpu_count() or 2))
    write_progress({"phase": "replay", "pct": 22, "detail": f"replay {len(names)} symbols · {len(book.by_idx)} sets · {hours}h", "setCount": len(book.by_idx), "source": source})
    book.replay_all(symbols=names, workers=workers, merge=False, score=True, on_symbol=on_symbol)

    write_progress({"phase": "score", "pct": 72, "detail": "last-N 10–70 · combo matrix · types", "source": source})
    block_eval = book.score_block_main()
    cost = float(book.cost_pct or 0.10)

    by_kind: Dict[str, List[Any]] = defaultdict(list)
    by_pack: Dict[str, List[Any]] = defaultdict(list)
    by_strat: Dict[str, List[Any]] = defaultdict(list)
    by_side: Dict[str, List[Any]] = defaultdict(list)
    by_symbol: Dict[str, List[Any]] = defaultdict(list)
    by_sl: Dict[str, List[Any]] = defaultdict(list)
    by_step: Dict[str, List[Any]] = defaultdict(list)
    by_trail: Dict[str, List[Any]] = defaultdict(list)
    closed: List[Any] = []
    processed = 0
    for st in book.by_idx:
        tape = list(st.hist or [])
        if not tape:
            continue
        processed += 1
        pack = str(st.pack or "general")
        strategy = "trailing" if str(st.kind or "") in ("trail", "trailing") else "normal"
        sl_key = f"{float(st.sl_ratio):.1f}"
        step_key = str(int(st.step or 0))
        trail_key = "trail" if strategy == "trailing" else "base"
        by_pack[pack].extend(tape)
        by_strat[strategy].extend(tape)
        by_sl[sl_key].extend(tape)
        by_step[step_key].extend(tape)
        by_trail[trail_key].extend(tape)
        closed.extend(tape)
        by_kind["combined" if pack == "indications" else "general"].extend(tape)
        for row in tape:
            k = kind_of(row)
            if k:
                by_kind[k].append(row)
            sd = side_of(row)
            if sd in ("LONG", "SHORT"):
                by_side[sd].append(row)
            sym = symbol_of(row)
            if sym:
                by_symbol[sym].append(row)
    for name, tape in (book.strategy_hist or {}).items():
        rows = list(tape or [])
        closed.extend(rows)
        key = str(name or "core").lower()
        if key.startswith("dca"):
            by_strat["dca"].extend(rows)
        elif "block" in key:
            by_strat["block"].extend(rows)
        elif key in STRATEGIES:
            by_strat[key].extend(rows)
    for kind, tape in (getattr(book, "ind_hist", None) or {}).items():
        indication = str(kind).partition("|")[0]
        if indication in IND_KINDS:
            by_kind[indication].extend(list(tape or []))

    write_progress({"phase": "combo", "pct": 84, "detail": "combo indication × strategy × last-N", "histFills": len(closed), "source": source})
    combo = combo_multi(book, min_pf, cost)
    combo30 = evaluate_book(book, min_pf=min_pf, pf_n=30)
    combo70 = evaluate_book(book, min_pf=min_pf, pf_n=70)

    overall_w = windows_of(closed, cost, min_pf)
    by_ind = group_windows(by_kind, cost, min_pf)
    # ensure all IND_KINDS appear
    for kind in list(INDICATIONS):
        by_ind.setdefault(kind, {"n": 0, "windows": windows_of([], cost, min_pf)})
    by_strategy = group_windows(by_strat, cost, min_pf)
    for strat in STRATEGIES:
        by_strategy.setdefault(strat, {"n": 0, "windows": windows_of([], cost, min_pf)})
    by_side_w = group_windows(by_side, cost, min_pf)
    by_pack_w = group_windows(by_pack, cost, min_pf)
    by_sl_w = group_windows(by_sl, cost, min_pf)
    by_step_w = group_windows(by_step, cost, min_pf)
    by_trail_w = group_windows(by_trail, cost, min_pf)
    by_symbol_w = group_windows(by_symbol, cost, min_pf)
    by_type = {
        "long": by_side_w.get("LONG") or {"n": 0, "windows": windows_of([], cost, min_pf)},
        "short": by_side_w.get("SHORT") or {"n": 0, "windows": windows_of([], cost, min_pf)},
        "general-pack": by_pack_w.get("general") or {"n": 0, "windows": windows_of([], cost, min_pf)},
        "indications-pack": by_pack_w.get("indications") or {"n": 0, "windows": windows_of([], cost, min_pf)},
        "trail": by_trail_w.get("trail") or {"n": 0, "windows": windows_of([], cost, min_pf)},
        "base": by_trail_w.get("base") or {"n": 0, "windows": windows_of([], cost, min_pf)},
        "block": by_strategy.get("block") or {"n": 0, "windows": windows_of([], cost, min_pf)},
        "dca": by_strategy.get("dca") or {"n": 0, "windows": windows_of([], cost, min_pf)},
    }

    validated_by_n: Dict[str, int] = {}
    for n in LAST_N:
        count = len((combo.get("successfulByN") or {}).get(f"last{n}") or [])
        for groups in (by_ind, by_strategy, by_symbol_w, by_side_w, by_pack_w, by_sl_w, by_step_w, by_trail_w):
            for blob in groups.values():
                if ((blob.get("windows") or {}).get(f"last{n}") or {}).get("validated"):
                    count += 1
        validated_by_n[f"last{n}"] = count
    price_paths = {s: downsample_closes(b) for s, b in bars_by.items()}

    issues: List[str] = []
    if book.progress.error:
        issues.append(f"replay-error {book.progress.error}")
    if not processed:
        issues.append("no-configs-processed")
    missing_ind = [k for k in IND_KINDS if (by_ind.get(k) or {}).get("n", 0) <= 0]
    if missing_ind:
        issues.append(f"indication-tapes-empty {missing_ind}")
    if set(by_side) != {"LONG", "SHORT"} and closed:
        issues.append(f"direction-gap {sorted(by_side)}")
    if not any(validated_by_n.values()):
        issues.append("no-validated-positive-configs")
    notes: List[str] = []
    missing_fam = [k for k, v in (combo.get("coverage") or {}).get("families", {}).items() if not v]
    if "axis" in missing_fam:
        notes.append("axis is a count coordinator; this 70h book has no independent axis fill tape")
        missing_fam = [k for k in missing_fam if k != "axis"]
    if missing_fam:
        issues.append(f"pf-families-missing {missing_fam}")

    elapsed = round(time.monotonic() - t0, 2)
    ok = not book.progress.error and processed > 0
    report = {
        "ok": ok,
        "phase": "ready",
        "pct": 100,
        "hours": hours,
        "lookback": lookback,
        "minPf": min_pf,
        "lastN": list(LAST_N),
        "symbols": names,
        "source": source,
        "fetchBars": {s: len(b) for s, b in bars_by.items()},
        "errors": errors,
        "elapsedS": elapsed,
        "workers": workers,
        "setCount": len(book.by_idx),
        "processedCount": processed,
        "histFills": len(closed),
        "comboCount": combo.get("comboCount"),
        "validatedByN": validated_by_n,
        "overallWindows": overall_w,
        "byIndication": by_ind,
        "byStrategy": by_strategy,
        "bySide": by_side_w,
        "byPack": by_pack_w,
        "bySl": by_sl_w,
        "byStep": by_step_w,
        "byTrail": by_trail_w,
        "bySymbol": by_symbol_w,
        "byType": by_type,
        "combo": combo,
        "comboCanonical": {
            "pfN30": {"pfStats": combo30.get("pfStats"), "meta": combo30.get("meta"), "successful": (combo30.get("successful") or [])[:20]},
            "pfN70": {"pfStats": combo70.get("pfStats"), "meta": combo70.get("meta"), "successful": (combo70.get("successful") or [])[:20]},
        },
        "pricePaths": price_paths,
        "block": {
            "evalPosCount": int(getattr(book, "block_eval_pos", 50) or 50),
            "keys": len(block_eval or {}),
        },
        "progress": {
            "phase": book.progress.phase,
            "ready": bool(book.progress.ready),
            "detail": book.progress.detail,
            "error": book.progress.error,
        },
        "issues": issues,
        "notes": notes,
        "typesCovered": list(TYPES),
        "indicationsCatalog": list(INDICATIONS),
        "strategiesCatalog": list(STRATEGIES),
    }
    text = json.dumps(report, ensure_ascii=False, default=str)
    _atomic(JSON_PATH, text)
    _atomic(PUBLIC_JSON, text)
    html_doc = build_html(report)
    _atomic(HTML_PATH, html_doc)
    _atomic(PUBLIC_HTML, html_doc)
    write_progress({
        "phase": "ready", "pct": 100, "ok": ok, "source": source, "elapsedS": elapsed,
        "detail": f"done {elapsed}s · fills {len(closed):,} · validated@30 {validated_by_n.get('last30')} · issues {len(issues)}",
        "histFills": len(closed), "validated": validated_by_n, "issues": issues,
        "html": str(PUBLIC_HTML),
    })
    print(f"wrote {JSON_PATH} and {HTML_PATH}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        write_progress({"phase": "error", "pct": 100, "detail": traceback.format_exc()[-500:]})
        raise SystemExit(1)
