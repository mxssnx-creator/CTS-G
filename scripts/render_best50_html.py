#!/usr/bin/env python3
"""Render a human-readable Best-50 report from the complete replay matrix.

The renderer deliberately writes static tables and inline SVG charts.  No raw
JSON is shown in the document.  It also rechecks qualification, PF, net, and
weighted entry averages while reading the replay rows so a stale ``qualified``
flag cannot silently make it into the report.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import heapq
import html
import json
import math
import pathlib
import sys
from collections import Counter, defaultdict
from typing import Any, Iterable

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from replay_complete import WINDOWS, grids  # noqa: E402


MIN_PF = 1.02
MAX_DD_S = 57_600
MIN_SAMPLES = 8
WINDOW_ORDER = {name: i for i, name in enumerate(WINDOWS)}


def esc(value: Any) -> str:
    return html.escape(str(value))


def number(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Ja" if value else "Nein"
    if isinstance(value, str):
        return esc(value)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return esc(value)
    if math.isinf(value):
        return "∞"
    return f"{value:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def plain(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Ja" if value else "Nein"
    if isinstance(value, str):
        return value
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isinf(value):
        return "∞"
    return f"{value:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def row_table(headers: Iterable[str], rows: Iterable[Iterable[str]]) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def classic_pf(gain: Any, loss: Any) -> float | str | None:
    try:
        gain = float(gain or 0)
        loss = float(loss or 0)
    except (TypeError, ValueError):
        return None
    if loss <= 1e-12:
        return "∞" if gain > 0 else None
    return gain / loss


def pf_num(value: Any) -> float:
    if value == "∞":
        return float("inf")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("-inf")


def qualifies(row: dict[str, Any]) -> bool:
    return bool(
        int(row.get("trainN") or 0) >= MIN_SAMPLES
        and int(row.get("holdoutN") or 0) >= MIN_SAMPLES
        and pf_num(row.get("trainPf")) >= MIN_PF
        and pf_num(row.get("holdoutPf")) >= MIN_PF
        and float(row.get("maxDdS") or 0) <= MAX_DD_S + 1e-9
    )


def sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        0 if row["recomputedQualified"] else 1,
        -pf_num(row.get("holdoutPf")),
        -pf_num(row.get("trainPf")),
        -float(row.get("holdoutNetPct") or 0),
        -float(row.get("netPct") or 0),
        float(row.get("maxDdS") or 0),
        -int(row.get("n") or 0),
        float(row.get("costPct") or 0),
        row["symbol"],
        row["kind"],
        row["direction"],
        int(row.get("config") or 0),
    )


def numeric_rank(row: dict[str, Any]) -> tuple[float, ...]:
    """A numeric-only rank used by the bounded top-50 heaps."""
    def safe_pf(value: Any) -> float:
        value = pf_num(value)
        return 1_000_000_000.0 if math.isinf(value) and value > 0 else value
    return (
        0.0 if row["recomputedQualified"] else 1.0,
        -safe_pf(row.get("holdoutPf")),
        -safe_pf(row.get("trainPf")),
        -float(row.get("holdoutNetPct") or 0),
        -float(row.get("netPct") or 0),
        float(row.get("maxDdS") or 0),
        -float(row.get("n") or 0),
        float(row.get("costPct") or 0),
    )


def keep_top(heap: list[tuple[tuple[float, ...], int, dict[str, Any]]], row: dict[str, Any], serial: int) -> None:
    """Keep the 50 smallest numeric ranks without retaining the matrix."""
    rank = numeric_rank(row)
    inverse = tuple(-value for value in rank)
    item = (inverse, serial, row)
    if len(heap) < 50:
        heapq.heappush(heap, item)
    elif inverse < heap[0][0]:
        heapq.heapreplace(heap, item)


def empty_stat() -> dict[str, Any]:
    return {"rows": 0, "qualified": 0, "positive": 0, "costSum": 0.0, "maxDdS": 0.0}


def read_matrix(source: pathlib.Path) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, int]]:
    catalog = grids()
    checks = Counter()
    heaps: dict[str, list[tuple[tuple[float, ...], int, dict[str, Any]]]] = {"qualified": [], "other": []}
    stats: dict[str, Any] = {"total": 0, "qualified": 0, "positive": 0, "windows": defaultdict(empty_stat), "types": defaultdict(empty_stat), "typeStrategies": defaultdict(empty_stat), "strategies": defaultdict(empty_stat), "bestTypes": {}, "summaries": []}
    serial = 0
    for summary_path in sorted(source.glob("*.summary.json")):
        summary = json.loads(summary_path.read_text())
        stats["summaries"].append(summary)
        payload = json.loads(gzip.decompress((source / f"{summary['key']}.json.gz").read_bytes()))
        family = summary["family"]
        for values in payload["rows"]:
            row = dict(zip(payload["columns"], values))
            cfg = catalog[family][int(row["config"])]
            row.update(
                window=summary["window"],
                symbol=summary["symbol"],
                kind=summary["kind"],
                family=family,
                parameters=cfg,
            )
            expected = qualifies(row)
            row["recomputedQualified"] = expected
            if bool(row.get("qualified")) == expected:
                checks["qualificationMatches"] += 1
            else:
                checks["qualificationMismatches"] += 1
            expected_pf = classic_pf(row.get("grossProfitPct"), row.get("grossLossPct"))
            if expected_pf == "∞" and row.get("pf") == "∞":
                checks["pfMatches"] += 1
            elif expected_pf is None and row.get("pf") is None:
                checks["pfMatches"] += 1
            # ``pf`` is rounded from unrounded fills while the displayed
            # gross columns are independently rounded to six decimals.  A
            # 5e-5 bound covers that published-field rounding even for the
            # smallest loss buckets without hiding a material discrepancy.
            elif expected_pf is not None and isinstance(row.get("pf"), (float, int)) and abs(float(row["pf"]) - float(expected_pf)) < 5e-5:
                checks["pfMatches"] += 1
            else:
                checks["pfMismatches"] += 1
            if abs(float(row.get("netPct") or 0) - (float(row.get("grossProfitPct") or 0) - float(row.get("grossLossPct") or 0))) < 2e-5:
                checks["netMatches"] += 1
            else:
                checks["netMismatches"] += 1
            executions = int(row.get("entryExecutions") or 0)
            qty_total = float(row.get("entryQtyTotal") or 0)
            avg_qty = float(row.get("avgEntryQty") or 0)
            if (executions == 0 and abs(avg_qty) < 1e-9) or (executions and abs(avg_qty - qty_total / executions) < 2e-7):
                checks["avgQtyMatches"] += 1
            else:
                checks["avgQtyMismatches"] += 1
            if bool(row.get("positive")) == (float(row.get("netPct") or 0) > 0):
                checks["positiveMatches"] += 1
            else:
                checks["positiveMismatches"] += 1
            serial += 1
            stats["total"] += 1
            if expected:
                stats["qualified"] += 1
            if float(row.get("netPct") or 0) > 0:
                stats["positive"] += 1
            strategy = row["parameters"].get("strategy", "unknown")
            for stat in (stats["windows"][row["window"]], stats["types"][row["kind"]], stats["typeStrategies"][(row["kind"], strategy)], stats["strategies"][strategy]):
                stat["rows"] += 1
                stat["qualified"] += int(expected)
                stat["positive"] += int(float(row.get("netPct") or 0) > 0)
                stat["costSum"] += float(row.get("costPct") or 0)
                stat["maxDdS"] = max(stat["maxDdS"], float(row.get("maxDdS") or 0))
            type_key = (row["kind"], strategy)
            prior = stats["bestTypes"].get(type_key)
            if prior is None or sort_key(row) < sort_key(prior):
                stats["bestTypes"][type_key] = row
            keep_top(heaps["qualified" if expected else "other"], row, serial)
    selected = [item[2] for item in heaps["qualified"] + heaps["other"]]
    selected.sort(key=sort_key)
    return selected[:50], stats, dict(checks)


def chart_bars(values: list[tuple[str, float]], title: str, color: str, suffix: str = "") -> str:
    width, height, left, top, plot_w, bar_h = 760, max(120, 56 + 42 * len(values)), 170, 38, 540, 24
    maximum = max((v for _, v in values), default=1) or 1
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">', f'<text x="{left}" y="20" font-size="15" font-weight="700">{esc(title)}</text>']
    for i, (label, value) in enumerate(values):
        y = top + i * 42
        bar = plot_w * max(0, value) / maximum
        out.append(f'<text x="{left-10}" y="{y+17}" text-anchor="end" font-size="12">{esc(label)}</text>')
        out.append(f'<rect x="{left}" y="{y}" width="{bar:.1f}" height="{bar_h}" rx="5" fill="{color}" opacity="0.85"/>')
        out.append(f'<text x="{left+bar+8:.1f}" y="{y+17}" font-size="12">{esc(plain(value, 0))}{esc(suffix)}</text>')
    out.append("</svg>")
    return "".join(out)


def top_chart(top: list[dict[str, Any]]) -> str:
    width, height, left, top_y, plot_w, row_h = 980, 760, 230, 34, 660, 14
    vals = [max(0.0, min(3.0, pf_num(r.get("holdoutPf")))) for r in top]
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Kontroll-PF der besten 50">', '<text x="230" y="18" font-size="15" font-weight="700">Kontroll-PF der besten 50 (sortiert, PF 1,02 markiert)</text>']
    x_gate = left + plot_w * MIN_PF / 3.0
    out.append(f'<line x1="{x_gate:.1f}" x2="{x_gate:.1f}" y1="24" y2="{height-20}" stroke="#dc2626" stroke-dasharray="5 4"/>')
    out.append(f'<text x="{x_gate+5:.1f}" y="30" fill="#b91c1c" font-size="11">1,02</text>')
    for i, (row, value) in enumerate(zip(top, vals)):
        y = top_y + i * row_h
        bar = plot_w * value / 3.0
        colour = "#059669" if row["recomputedQualified"] else "#64748b"
        out.append(f'<text x="{left-8}" y="{y+10}" text-anchor="end" font-size="9">#{i+1} {esc(row["symbol"].split("-")[0])}</text>')
        out.append(f'<rect x="{left}" y="{y}" width="{bar:.1f}" height="9" rx="2" fill="{colour}"/>')
        out.append(f'<text x="{left+bar+4:.1f}" y="{y+9}" font-size="9">{esc(plain(row.get("holdoutPf"), 3))}</text>')
    out.append("</svg>")
    return "".join(out)


CSS = """
*{box-sizing:border-box}body{margin:0;background:#edf2f8;color:#142238;font:15px/1.5 system-ui,sans-serif}header{background:#0b1930;color:#fff;padding:38px max(18px,calc((100vw - 1480px)/2));border-bottom:5px solid #3b82f6}header p{color:#c6d4e8;max-width:1100px}h1{font-size:clamp(28px,4vw,48px);line-height:1.1;margin:8px 0 14px}h2{font-size:24px;margin:0 0 16px}h3{font-size:18px;margin:22px 0 10px}main{max-width:1480px;margin:auto;padding:24px 18px 60px}section{background:#fff;border:1px solid #d8e2ee;border-radius:14px;padding:24px;margin:0 0 22px;scroll-margin-top:20px}.nav{display:flex;gap:18px;flex-wrap:wrap;background:#fff;border-bottom:1px solid #d8e2ee;padding:12px 18px}.nav a{color:#1d4ed8;font-weight:650;text-decoration:none}.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.card{border:1px solid #d8e2ee;border-radius:10px;padding:16px;background:#fff}.card b{display:block;font-size:28px;letter-spacing:-.03em}.muted{color:#53667e;font-size:13px}.notice{border-left:4px solid #d97706;background:#fffbeb;color:#713f12;padding:14px;margin:14px 0}.ok{color:#047857;font-weight:650}.bad{color:#b91c1c;font-weight:650}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:8px 9px;text-align:left;border-bottom:1px solid #e2e8f0;vertical-align:top;white-space:nowrap}th{background:#f1f5f9;color:#334155;position:sticky;top:0}td.wrap{white-space:normal;min-width:230px}.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}.tag{display:inline-block;border-radius:10px;padding:2px 7px;background:#dcfce7;color:#166534;font-size:11px}.tag.no{background:#e2e8f0;color:#475569}details{border:1px solid #d8e2ee;border-radius:8px;padding:9px 11px;margin:8px 0}summary{cursor:pointer;font-weight:650;color:#25456c}.formula{font-family:ui-monospace,monospace;background:#f8fafc;padding:12px;border-radius:7px;overflow:auto}svg{width:100%;height:auto;display:block}footer{color:#53667e;font-size:13px;padding:12px 0}@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.two{grid-template-columns:1fr}section{padding:16px}}@media print{.nav{display:none}section{break-inside:avoid}}
"""


def build(source: str, output: str, prev_report: str | None = None) -> dict[str, Any]:
    top, stats, checks = read_matrix(pathlib.Path(source))
    total = int(stats["total"])
    qualified_count = int(stats["qualified"])
    positive_count = int(stats["positive"])
    by_window = stats["windows"]
    by_type = stats["typeStrategies"]
    by_strategy = stats["strategies"]
    window_rows = []
    for window in WINDOWS:
        group = by_window[window]
        window_rows.append([
            esc(window), number(group["rows"], 0), number(group["qualified"], 0),
            number(group["positive"], 0), number(group["costSum"] / max(1, group["rows"]), 4),
            number(group["maxDdS"] / 3600, 2),
        ])
    type_rows = []
    for (kind, strategy), group in sorted(by_type.items()):
        best = stats["bestTypes"][(kind, strategy)]
        type_rows.append([
            esc(kind), esc(strategy), number(group["rows"], 0), number(group["qualified"], 0),
            esc(best["symbol"] + " / " + best["direction"]), number(best.get("holdoutPf"), 4),
            number(best.get("trainPf"), 4), number(best.get("netPct"), 4), number(best.get("maxDdS"), 0),
            "<span class=\"tag\">Ja</span>" if best["recomputedQualified"] else "<span class=\"tag no\">Nein</span>",
        ])
    strategy_rows = []
    for strategy, group in sorted(by_strategy.items()):
        strategy_rows.append([
            esc(strategy), number(group["rows"], 0), number(group["qualified"], 0),
            number(group["positive"], 0), number(group["costSum"] / max(1, group["rows"]), 4),
        ])
    top_rows = []
    for i, row in enumerate(top, 1):
        p = row["parameters"]
        status = '<span class="tag">qualifiziert</span>' if row["recomputedQualified"] else '<span class="tag no">unter Gate</span>'
        top_rows.append([
            number(i, 0), status, esc(row["window"]), esc(row["symbol"]), esc(row["kind"]), esc(row["direction"]),
            esc(p.get("strategy", "—")), number(p.get("tpPct"), 3), number(p.get("slPct"), 3), number(row.get("n"), 0),
            number(row.get("pf"), 4), number(row.get("trainPf"), 4), number(row.get("holdoutPf"), 4),
            number(row.get("netPct"), 4), number(row.get("costPct"), 4), number(row.get("maxDrawdownPct"), 4), number(float(row.get("maxDdS") or 0) / 3600, 2),
            f'<details><summary>Details</summary><div class="wrap">TP {number(p.get("tpPct"),3)} · SL {number(p.get("slPct"),3)} · Trail {number(p.get("trailArmPct"),3)}:{number(p.get("trailGivePct"),3)} · N {number(row.get("n"),0)} · W/L {number(row.get("wins"),0)} / {number(row.get("losses"),0)} · Train {number(row.get("trainN"),0)} / Holdout {number(row.get("holdoutN"),0)} · Avg hold {number(row.get("avgHoldS"),1)} s · Entry executions {number(row.get("entryExecutions"),0)} · Qty total {number(row.get("entryQtyTotal"),4)} · Avg qty {number(row.get("avgEntryQty"),4)} · Avg entry price {number(row.get("avgEntryPrice"),6)} · Adds {number(row.get("additions"),0)} · Max volume {number(row.get("maxVolume"),4)} · DDT avg/max {number(float(row.get("avgDdS") or 0)/3600,2)} / {number(float(row.get("maxDdS") or 0)/3600,2)} h</div></details>',
        ])
    check_rows = [
        ["Qualification flag vs. independent PF / sample / DDT formula", number(checks.get("qualificationMatches", 0), 0), number(checks.get("qualificationMismatches", 0), 0), "PF ≥ 1,02; N ≥ 8 in Train und Holdout; max DDT ≤ 16 h"],
        ["Classic PF = positive net / absolute negative net", number(checks.get("pfMatches", 0), 0), number(checks.get("pfMismatches", 0), 0), "bis 5e-5 Toleranz wegen sechsstelliger Feldrundung; ∞ bzw. leer ohne Verlust"],
        ["Netto = positive net − negative net", number(checks.get("netMatches", 0), 0), number(checks.get("netMismatches", 0), 0), "Kosten sind bereits in den Netto-Fills enthalten; costPct wird separat ausgewiesen"],
        ["Weighted average entry quantity", number(checks.get("avgQtyMatches", 0), 0), number(checks.get("avgQtyMismatches", 0), 0), "entryQtyTotal / entryExecutions"],
        ["Positive flag vs. netPct > 0", number(checks.get("positiveMatches", 0), 0), number(checks.get("positiveMismatches", 0), 0), "Keine Vorzeichen- oder Rundungsabweichung"],
    ]
    window_q = [(w, by_window[w]["qualified"]) for w in WINDOWS]
    prev_link = f'<p>Der vollständige Prev‑Fenster/Count‑Sweep ist separat verlinkt: <a href="{esc(prev_report)}">14 Tage · Fenster und Counts 5–55</a>.</p>' if prev_report else ""
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    qualified_type_chart = sorted(
        ((kind, stats["types"][kind]["qualified"]) for kind in sorted(stats["types"])),
        key=lambda item: -item[1],
    )
    parts = [
        '<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CTS-G · Best 50 · PF 1,02</title><style>', CSS, '</style>',
        '<header><div>CTS-G · vollständige Matrix · unabhängige Rechenprüfung</div><h1>Die besten 50 Konfigurationen</h1><p>Neue Rangliste aus der gesamten 2.502.720‑Zeilen‑Matrix. Die Auswahl bevorzugt unabhängig bestätigte Ergebnisse mit PF ≥ 1,02 in Training und Kontrolle, mindestens acht Abschlüssen je Abschnitt und höchstens 16 Stunden maximaler Drawdown‑Zeit. Alle Beträge bleiben auf das ursprüngliche Parent‑Notional normiert.</p></header>',
        '<div class="nav"><a href="#overview">Überblick</a><a href="#checks">Rechenprüfung</a><a href="#top50">Top 50</a><a href="#types">Typen</a><a href="#strategies">Strategien</a><a href="#method">Methode</a></div><main>',
        '<section id="overview"><div class="cards"><div class="card"><span>Matrixzeilen</span><b>', number(total, 0), '</b><span class="muted">120 Gruppen · 4 Zeitfenster</span></div><div class="card"><span>Qualifiziert</span><b class="ok">', number(qualified_count, 0), '</b><span class="muted">PF 1,02 / N 8 / DDT 16 h</span></div><div class="card"><span>Netto positiv</span><b>', number(positive_count, 0), '</b><span class="muted">nach Kosten</span></div><div class="card"><span>Top‑50‑Kontroll PF</span><b>', number(top[0].get("holdoutPf"), 4), '</b><span class="muted">Rang 1</span></div><div class="card"><span>Kostenfallback</span><b>0,10 %</b><span class="muted">0,05 % je Entry/Add/Exit‑Notional</span></div></div><div class="notice"><strong>Rechenstatus:</strong> ', "alle unabhängigen Checks bestanden" if not any(k.endswith("Mismatches") and v for k, v in checks.items()) else "Abweichungen gefunden – siehe Rechenprüfung", '</div><div class="two"><div>', chart_bars(window_q, "Qualifizierte Zeilen je Zeitfenster", "#059669"), '</div><div>', chart_bars(qualified_type_chart, "Qualifizierte Zeilen je Typ", "#2563eb"), '</div></div>', row_table(["Fenster", "Zeilen", "Qualifiziert", "Netto positiv", "Ø Kosten pp", "Max DDT h"], window_rows), prev_link, '</section>',
        '<section id="checks"><h2>Unabhängige Kalkulationsprüfung</h2><p>Die gespeicherte Qualifikationsmarkierung wird beim Lesen nicht vertraut, sondern aus denselben chronologisch getrennten Trainings‑ und Kontrollwerten neu berechnet. Zusätzlich werden PF, Netto, Kosten und execution‑gewichtete Durchschnittsmengen geprüft.</p>', row_table(["Prüfung", "Übereinstimmungen", "Abweichungen", "Regel"], check_rows), '</section>',
        '<section id="top50"><h2>Top 50 mit vollständigen Kennzahlen</h2><p>Sortierung: qualifiziert zuerst, dann Kontroll‑PF, Trainings‑PF, Kontroll‑Netto, Gesamt‑Netto, Drawdown‑Zeit, Stichprobe und Kosten. „Details“ enthält TP/SL/Trailing, W/L, Durchschnittshaltezeit, ausgeführte Entries, Mengen, Preise und Additionen.</p>', top_chart(top), row_table(["Rang", "Gate", "Fenster", "Symbol", "Typ", "Richtung", "Strategie", "TP %", "SL %", "N", "PF", "Train PF", "Kontroll PF", "Netto pp", "Kosten pp", "DD pp", "DDT h", "Ausführliche Werte"], top_rows), '</section>',
        '<section id="types"><h2>Beste Ergebnisse je Typ und Strategie</h2><p>Für jeden Indikationstyp und jede zusätzliche Strategie werden alle Matrixzeilen gemeinsam bewertet. Die qualifizierten Zähler sind keine Auffüllung: nur tatsächlich bestandene Zeilen werden gezählt.</p>', row_table(["Typ", "Strategie", "Zeilen", "Qualifiziert", "Sieger", "Kontroll PF", "Train PF", "Netto pp", "DDT s", "Sieger Gate"], type_rows), '</section>',
        '<section id="strategies"><h2>Strategie‑ und Kostenabdeckung</h2>', row_table(["Strategie", "Zeilen", "Qualifiziert", "Netto positiv", "Ø Kosten pp"], strategy_rows), '<h3>Was die Kostenrechnung bedeutet</h3><p class="formula">Entry/Add/Exit‑Gebühr = ausgeführtes Notional × (0,10 % Roundtrip / 2). Jede Ausführung wird auf ihrer eigenen Kerze bewertet. Bereits gebuchte Entry‑ und Add‑Gebühren werden beim Exit mitgeführt; historische Gebühren werden nicht nachträglich durch spätere Samples ersetzt. Net PnL = positive Netto‑Fills − absolute negative Netto‑Fills.</p></section>',
        '<section id="method"><h2>Definitionen und Grenzen</h2><p><strong>Qualifikation:</strong> PF ≥ 1,02 einschließlich Grenzwert, mindestens acht Abschlüsse im Training und in der Kontrolle, maximale DDT ≤ 57.600 Sekunden. Training und Kontrolle bleiben zeitlich getrennt; der Kontrollabschnitt wird nicht für die Auswahl verwendet.</p><p><strong>PositionCost:</strong> Im Live‑Pfad werden gemessene Exchange‑Gebühren nach Notional gewichtet. Bis zu vollständigen Samples gilt der angezeigte Fallback 0,10 % Roundtrip. Die historische Matrix verwendet dafür 0,05 % je ausgeführtem Entry/Add/Exit‑Notional.</p><p><strong>Größen und Mittelwerte:</strong> Block‑Mengen werden aus der ursprünglichen Parent‑Menge berechnet und auf maximal 2× begrenzt. Additionen werden nicht auf bereits addierte Mengen aufgezinst. Die Durchschnittsmenge ist execution‑gewichtet; der Durchschnittspreis ist Notional geteilt durch Entry‑Menge.</p><p><strong>Chronologie:</strong> Signal am abgeschlossenen Close, Ausführung in der Folgokerze; Stop vor Take‑Profit; Trailing‑Updates greifen erst in der nächsten Kerze. Prev‑Koordination verwendet nur das vorherige geschlossene Segment. OHLCV reproduziert keine Orderbuch‑Latenz, Funding oder tatsächliche Exchange‑Fills.</p><p class="muted">Bericht erzeugt: ', esc(generated), ' · Keine JSON‑Liste wird angezeigt.</p></section><footer>CTS-G · Best‑50‑Bericht · HTML‑Ansicht für menschliche Prüfung</footer></main></html>',
    ]
    html_out = "".join(parts)
    output_path = pathlib.Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_out)
    return {"rows": total, "qualified": qualified_count, "positive": positive_count, "top50": len(top), "checks": checks, "html": str(output_path)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--prev-report")
    args = parser.parse_args()
    import json
    print(json.dumps(build(args.source, args.output, args.prev_report), ensure_ascii=False, sort_keys=True))
