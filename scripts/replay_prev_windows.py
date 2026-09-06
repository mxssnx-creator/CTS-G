"""14-day Prev-window/count sweep with a human-readable HTML report.

The sweep uses one best 14-day parent per symbol/type and replays each lane
with its own causal closed tape.  Prev is the segment immediately before the
current segment; current-bar closes are only visible on the next bar.
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import pathlib
import statistics
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "server" / "pulse"))
from replay_complete import grids
from replay_five_days import replay
from coord_engine import Coordinator

WINDOWS = tuple(range(5, 56, 5))
POLICY = dict(min_pf=1.02, max_dd_s=57600, cost_pct=.10)


def number(value):
    return float("inf") if value == "∞" else float(value or 0)


def load_candidates(source: pathlib.Path):
    catalog = grids()
    chosen = {}
    for path in sorted(source.glob("14d_*.json.gz")):
        if ".embedded." in path.name:
            continue
        summary = json.loads(path.with_name(path.name.replace(".json.gz", ".summary.json")).read_text())
        packed = json.loads(gzip.decompress(path.read_bytes()))
        ix = {key: i for i, key in enumerate(packed["columns"])}
        family = summary["family"]
        for values in packed["rows"]:
            if values[ix["trainN"]] < 8 or values[ix["holdoutN"]] < 8:
                continue
            cfg = catalog[family][int(values[ix["config"]])]
            key = (summary["symbol"], summary["kind"])
            candidate = dict(
                symbol=summary["symbol"], kind=summary["kind"],
                direction=values[ix["direction"]], family=family,
                config=int(values[ix["config"]]), parameters=cfg,
                selection=dict(
                    trainPf=values[ix["trainPf"]], holdoutPf=values[ix["holdoutPf"]],
                    trainN=values[ix["trainN"]], holdoutN=values[ix["holdoutN"]],
                    netPct=values[ix["netPct"]], maxDrawdownPct=values[ix["maxDrawdownPct"]],
                    qualified=bool(values[ix["qualified"]]),
                ),
            )
            rank = (
                1 if candidate["selection"]["qualified"] else 0,
                number(candidate["selection"]["trainPf"]),
                number(candidate["selection"]["holdoutPf"]),
                candidate["selection"]["netPct"],
                -candidate["selection"]["maxDrawdownPct"],
                -candidate["config"],
            )
            if key not in chosen or rank > chosen[key][0]:
                chosen[key] = (rank, candidate)
    return [row[1] for row in sorted(chosen.values(), key=lambda item: (item[1]["symbol"], item[1]["kind"]))]


def human_parameters(cfg):
    return "; ".join(f"{key}={cfg[key]}" for key in (
        "strategy", "step", "slRatio", "tpPct", "slPct", "trailArmPct", "trailGivePct"
    ) if key in cfg)


def evaluate_candidate(args):
    candidate, source, overlay = args
    blob = json.loads((pathlib.Path(source) / f"{candidate['symbol']}.prepared.json").read_text())
    bars = blob["bars"][-(20160 + 60):]
    signals = blob["signals"][candidate["kind"]][-(20160 + 60):]
    side = 1 if candidate["direction"] == "LONG" else -1
    cfg = candidate["parameters"]
    baseline = replay(bars, signals, side, [cfg], cost_pct=POLICY["cost_pct"],
                      min_pf=POLICY["min_pf"], max_dd_s=POLICY["max_dd_s"])[0]
    # Run all 11×11 lanes as one vectorized replay.  Each lane still keeps a
    # separate closed tape and gate state; this removes only repeated candle
    # traversal, not the causal accounting.
    combinations = [(window, minimum) for window in WINDOWS for minimum in WINDOWS]
    configs = [dict(cfg, prevWindow=window, prevMinCount=minimum) for window, minimum in combinations]
    histories = [[] for _ in combinations]
    pending = []
    cursor = 0
    blocks = np.zeros(len(combinations), dtype=int)
    cost_pct = POLICY["cost_pct"]

    def on_close(raw):
        cost = float(raw["costFraction"])
        gross = float(raw["netFraction"]) + cost
        parent_price = float(raw["parentPrice"] or 0)
        pending.append((int(raw["bar"]), int(raw["config"]), dict(
            bar=int(raw["bar"]), t=int(raw["bar"]), qty=1.0,
            entry=parent_price, pnl=float(raw["netFraction"]) * parent_price,
            pnl_pct=gross,
            position_cost_pct=(cost / parent_price * 100.0) if parent_price else cost_pct,
            symbol=candidate["symbol"], side=candidate["direction"],
        )))

    def lane_ratio(tape, requested=15):
        tail = tape[-max(1, int(requested)) :]
        if not tail:
            return 1.0, 0
        rs = []
        for row in tail:
            actual = float(row.get("position_cost_pct") or cost_pct)
            gross_pct = float(row.get("pnl_pct") or 0.0) * 100.0
            rs.append((gross_pct - actual) / max(1e-9, actual))
        return 1.0 + (sum(rs) / len(rs)) * .05, len(tail)

    def entry_filter(bar):
        nonlocal cursor, blocks
        # A close from the current bar is still pending and therefore cannot
        # affect an entry on that same candle.
        while cursor < len(pending) and pending[cursor][0] < bar:
            _, lane, row = pending[cursor]
            histories[lane].append(row)
            cursor += 1
        allowed = np.ones(len(combinations), dtype=bool)
        for lane, (window, minimum) in enumerate(combinations):
            tape = histories[lane]
            current_pf, current_n = lane_ratio(tape, 15)
            previous = tape[-(window * 2):-window] if len(tape) >= window * 2 else []
            previous_pf, previous_n = lane_ratio(previous, window)
            if (current_n >= min(8, 15) and previous_n >= minimum
                    and previous_pf + 1e-9 < POLICY["min_pf"] * .85
                    and current_pf + 1e-9 < POLICY["min_pf"] * .85):
                allowed[lane] = False
        blocks += (~allowed).astype(int)
        return allowed

    results = replay(bars, signals, side, configs, cost_pct=cost_pct,
                     min_pf=POLICY["min_pf"], max_dd_s=POLICY["max_dd_s"],
                     entry_filter=entry_filter, on_close=on_close)
    rows = []
    for result, (window, minimum), block_count in zip(results, combinations, blocks.tolist()):
        rows.append(dict(
            symbol=candidate["symbol"], kind=candidate["kind"], direction=candidate["direction"],
            family=candidate["family"], config=candidate["config"], parameters=cfg,
            parametersText=human_parameters(cfg), prevWindow=window, prevMinCount=minimum,
            gateBlocks=block_count, qualified=bool(result["qualified"]), n=result["n"],
            pf=result["pf"], trainPf=result["trainPf"], holdoutPf=result["holdoutPf"],
            netPct=result["netPct"], holdoutNetPct=result["holdoutNetPct"],
            deltaNetPct=round(result["netPct"] - baseline["netPct"], 6),
            deltaHoldoutNetPct=round(result["holdoutNetPct"] - baseline["holdoutNetPct"], 6),
            maxDrawdownPct=result["maxDrawdownPct"], maxDdS=result["maxDdS"],
            avgDdS=result["avgDdS"], costPct=result["costPct"],
            avgEntryPrice=result.get("avgEntryPrice", 0), avgEntryQty=result.get("avgEntryQty", 0),
            entryExecutions=result.get("entryExecutions", 0), additions=result["additions"],
            baselineNetPct=baseline["netPct"], baselineHoldoutNetPct=baseline["holdoutNetPct"],
            baselinePf=baseline["pf"], baselineQualified=bool(baseline["qualified"]),
        ))
    return dict(candidate=candidate, baseline=baseline, rows=rows)


def score(row):
    return (1 if row["qualified"] else 0, row["holdoutNetPct"], row["netPct"],
            -row["maxDrawdownPct"], number(row["pf"]))


def svg_bars(summary):
    vals = [(s["label"], s["value"]) for s in summary]
    mx = max([abs(v) for _, v in vals] or [1])
    width, row_h = 720, 28
    out = [f'<svg viewBox="0 0 {width} {max(40, len(vals)*row_h)}" role="img" aria-label="Best holdout improvement">']
    for i, (label, value) in enumerate(vals):
        y = i * row_h + 5
        w = int(abs(value) / mx * 260)
        x = 270 if value >= 0 else 270 - w
        color = "#54d19a" if value >= 0 else "#ed807b"
        out.append(f'<text x="4" y="{y+16}" fill="#dbe8f6" font-size="12">{html.escape(label)}</text>')
        out.append(f'<rect x="{x}" y="{y}" width="{max(1,w)}" height="18" rx="3" fill="{color}"/>')
        out.append(f'<text x="{min(width-5,x+w+6)}" y="{y+14}" fill="#dbe8f6" font-size="12">{value:.3f} pp</text>')
    out.append('</svg>')
    return ''.join(out)


def render_html(payload, output):
    rows = payload["rows"]
    by_type = {}
    for row in rows:
        by_type.setdefault((row["symbol"], row["kind"]), []).append(row)
    best_type = []
    for key, group in sorted(by_type.items()):
        best = max(group, key=score)
        best_type.append(dict(best))
    best_chart = [dict(label=f"{r['symbol']} · {r['kind']}", value=r["deltaHoldoutNetPct"]) for r in best_type]
    q = sum(bool(r["qualified"]) for r in rows)
    positives = sum(float(r["netPct"]) > 0 for r in rows)
    # Each lane carries the same parent baseline flag.  Count it once per
    # symbol/type candidate rather than looking for a field on the metadata
    # records (the metadata intentionally contains only the source selection).
    baseline_q = sum(
        bool(group[0]["baselineQualified"])
        for group in by_type.values()
        if group
    )
    heat = {}
    for row in rows:
        cell = heat.setdefault((row["prevWindow"], row["prevMinCount"]), [])
        cell.append(row["deltaHoldoutNetPct"])
    heat_rows = []
    for window in WINDOWS:
        cells = []
        for minimum in WINDOWS:
            values = heat[(window, minimum)]
            avg = statistics.fmean(values) if values else 0.0
            qualified = sum(1 for r in rows if r["prevWindow"] == window and r["prevMinCount"] == minimum and r["qualified"])
            cells.append(f'<td class="{"pos" if avg >= 0 else "neg"}" title="{qualified} qualified">{avg:.2f}<small>/{qualified}</small></td>')
        heat_rows.append(f'<tr><th>{window}</th>{"".join(cells)}</tr>')
    display_rows = []
    for row in rows:
        display_rows.append({k: row[k] for k in (
            "symbol", "kind", "direction", "parametersText", "prevWindow", "prevMinCount",
            "gateBlocks", "n", "pf", "trainPf", "holdoutPf", "netPct", "holdoutNetPct",
            "deltaNetPct", "deltaHoldoutNetPct", "maxDrawdownPct", "maxDdS", "avgDdS",
            "costPct", "avgEntryPrice", "avgEntryQty", "entryExecutions", "additions", "qualified"
        )})
    chart = svg_bars(best_chart)
    type_rows = []
    for r in best_type:
        type_rows.append('<tr>'+''.join(f'<td>{html.escape(str(v))}</td>' for v in (
            r["symbol"], r["kind"], r["direction"], r["prevWindow"], r["prevMinCount"],
            r["qualified"], r["pf"], r["trainPf"], r["holdoutPf"], r["netPct"],
            r["holdoutNetPct"], r["deltaHoldoutNetPct"], r["maxDrawdownPct"],
            r["avgEntryPrice"], r["avgEntryQty"], r["parametersText"]
        ))+'</tr>')
    html_text = f'''<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CTS-G · Prev-Fenster/Counts 14-Tage-Test</title>
<style>body{{margin:0;background:#091421;color:#e7eef8;font:14px/1.5 system-ui}}main{{max-width:1700px;margin:auto;padding:24px}}section{{background:#142337;border-radius:10px;padding:18px;margin:16px 0;overflow:auto}}h1{{font-size:30px}}h2{{margin-top:0}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}}.card{{background:#1e344e;border-radius:8px;padding:12px}}.value{{font-size:24px;font-weight:700}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{padding:7px;border-bottom:1px solid #344b64;text-align:left;white-space:nowrap}}th{{color:#b8d1ea;position:sticky;top:0;background:#142337}}.heat th,.heat td{{text-align:center;min-width:46px}}.heat td{{font-variant-numeric:tabular-nums}}.heat .pos{{background:#1e634b}}.heat .neg{{background:#653940}}.heat small{{display:block;color:#c7d4e4;font-size:9px}}.note{{border-left:4px solid #e7bd68;padding:12px;background:#363526}}select,button{{background:#203c5d;color:#fff;border:1px solid #6080a1;border-radius:5px;padding:7px;margin:3px}}.muted{{color:#abc0d7}}.scroll{{max-height:640px;overflow:auto}}svg{{max-width:100%;height:auto}}code{{color:#a4d9ff}}</style><main>
<h1>CTS-G · Prev Position Window/Count Matrix · 14 Tage</h1>
<p class="muted">Vollständiger menschlicher Bericht mit PF, Kostenabzug, Previous-Segment-Gates, Durchschnitten, TP/SL/Trailing-Parametern und Ergebnisvergleichen.</p>
<section><h2>Messumfang und Status</h2><div class="grid"><div class="card">Eltern-Kandidaten<div class="value">{len(payload['candidates'])}</div></div><div class="card">Prev-Kombinationen<div class="value">{len(WINDOWS)*len(WINDOWS)}</div></div><div class="card">Ausgewertete Lanes<div class="value">{len(rows):,}</div></div><div class="card">Strikt qualifiziert<div class="value">{q:,}</div></div><div class="card">Netto positiv<div class="value">{positives:,}</div></div><div class="card">Baseline qualifiziert<div class="value">{baseline_q}</div></div></div><p class="note">Policy: PF mindestens 1,05 in Training und Holdout, mindestens 8 Abschlüsse je Abschnitt, maximal 16 Stunden Drawdown-Zeit, dynamische PositionCost mit 0,10&nbsp;% Fallback. Jede Lane nutzt nur vorher abgeschlossene eigene Positionen; ein Abschluss der aktuellen Kerze wird erst ab der nächsten Kerze für Prev sichtbar.</p></section>
<section><h2>Beste Lane je Symbol und Typ</h2><table><tr><th>Symbol</th><th>Typ/Pack</th><th>Seite</th><th>Prev Window</th><th>Min Count</th><th>Qualifiziert</th><th>PF</th><th>Train PF</th><th>Holdout PF</th><th>Netto pp</th><th>Holdout pp</th><th>Δ Holdout pp</th><th>DD pp</th><th>Ø Entry</th><th>Ø Qty</th><th>Parameter</th></tr>{''.join(type_rows)}</table></section>
<section><h2>Holdout-Verbesserung der besten Lane je Typ</h2>{chart}<p class="muted">Positive Werte sind eine Verbesserung gegenüber derselben Baseline ohne Prev-Gate. Ein negatives Ergebnis bleibt sichtbar; kein Wert wird als Live-Erfolg behauptet.</p></section>
<section><h2>Heatmap: durchschnittliche Holdout-Änderung pp / qualifizierte Lanes</h2><p class="muted">Zeilen = Prev Window, Spalten = Prev Min Count; Zelle = Mittelwert Δ Holdout pp / Anzahl strikter Qualifikationen.</p><table class="heat"><tr><th>Window ↓ / Count →</th>{''.join(f'<th>{x}</th>' for x in WINDOWS)}</tr>{''.join(heat_rows)}</table></section>
<section><h2>Alle {len(rows):,} Prev-Lanes</h2><label>Typ <select id="kind"><option value="">Alle</option></select></label><label>Status <select id="status"><option value="">Alle</option><option value="qualified">Qualifiziert</option><option value="positive">Netto positiv</option><option value="negative">Netto ≤ 0</option></select></label><label>Sortierung <select id="sort"><option value="deltaHoldoutNetPct">Δ Holdout</option><option value="holdoutNetPct">Holdout</option><option value="maxDrawdownPct">DD niedrig</option><option value="avgEntryPrice">Ø Entry</option></select></label><button id="prev">Zurück</button><button id="next">Weiter</button><span id="count"></span><div class="scroll"><table><thead><tr><th>Symbol</th><th>Typ</th><th>Seite</th><th>Parameter</th><th>Window</th><th>Min Count</th><th>Gate Blocks</th><th>N</th><th>PF</th><th>Train PF</th><th>Holdout PF</th><th>Netto</th><th>Holdout</th><th>Δ Holdout</th><th>DD</th><th>DDT h</th><th>ØDDT h</th><th>Cost %</th><th>Ø Entry</th><th>Ø Qty</th><th>Exec</th><th>Adds</th><th>Q</th></tr></thead><tbody id="body"></tbody></table></div></section>
<section><h2>Berechnung und Kontrollpunkte</h2><p>Prev = die abgeschlossene Positionengruppe unmittelbar vor der aktuellen Gruppe; bei Window <code>w</code> werden die Positionen <code>[-2w:-w]</code> geprüft. Min Count wird unabhängig als Mindestanzahl validierter Previous-Positionen getestet. Prev-Window und Min-Count decken jeweils 5, 10, …, 55 ab.</p><p>TP/SL lösen zuerst aus; bei einer selben Kerze gewinnt der Stop. Gaps füllen am schlechteren Open. Trailing wird nach dem abgeschlossenen Close aktualisiert und erst ab der Folgekerze aktiv. Additionen verwenden den gewichteten Average Entry; jede Entry/Add/Exit-Ausführung trägt ihren eigenen Cost-Anteil. PF basiert auf Nettoabschlüssen nach einmaligem Kostenabzug. Durchschnitts-Entry und Durchschnittsmenge sind aus den tatsächlich gewichteten Fills berechnet.</p><p>Die historische Matrix ist eine unabhängige Lane-Auswertung für Forschung. Sie ist keine summierbare Kontorendite und aktiviert keine Live-Sets automatisch. Längere Matrixfenster können trotz guter kurzer Teilfenster keine Qualifikation erreichen; das wird hier explizit angezeigt.</p></section>
<script id="prev-data"></script></main></html>'''
    data_json = json.dumps(display_rows, separators=(',', ':')).replace('<', '\\u003c')
    script = """<script>const D=__DATA__;const el=x=>document.getElementById(x);let rows=D.slice(),page=0;const size=100;[...new Set(D.map(x=>x.kind))].sort().forEach(k=>{let o=document.createElement('option');o.value=k;o.textContent=k;el('kind').append(o)});function render(){rows=D.filter(r=>(!el('kind').value||r.kind===el('kind').value)&&(!el('status').value||(el('status').value==='qualified'?r.qualified:el('status').value==='positive'?r.netPct>0:r.netPct<=0)));const key=el('sort').value;rows.sort((a,b)=>key==='maxDrawdownPct'?a[key]-b[key]:b[key]-a[key]);page=Math.max(0,Math.min(page,Math.max(0,Math.ceil(rows.length/size)-1)));el('body').innerHTML=rows.slice(page*size,(page+1)*size).map(r=>'<tr><td>'+r.symbol+'</td><td>'+r.kind+'</td><td>'+r.direction+'</td><td>'+r.parametersText+'</td><td>'+r.prevWindow+'</td><td>'+r.prevMinCount+'</td><td>'+r.gateBlocks+'</td><td>'+r.n+'</td><td>'+r.pf+'</td><td>'+r.trainPf+'</td><td>'+r.holdoutPf+'</td><td>'+r.netPct+'</td><td>'+r.holdoutNetPct+'</td><td>'+r.deltaHoldoutNetPct+'</td><td>'+r.maxDrawdownPct+'</td><td>'+(r.maxDdS/3600).toFixed(2)+'</td><td>'+(r.avgDdS/3600).toFixed(2)+'</td><td>'+r.costPct+'</td><td>'+r.avgEntryPrice+'</td><td>'+r.avgEntryQty+'</td><td>'+r.entryExecutions+'</td><td>'+r.additions+'</td><td>'+(r.qualified?'✓':'')+'</td></tr>').join('');el('count').textContent=' '+rows.length.toLocaleString()+' Zeilen · Seite '+(page+1)+'/'+Math.max(1,Math.ceil(rows.length/size))};for(const id of ['kind','status','sort'])el(id).onchange=()=>{page=0;render()};el('prev').onclick=()=>{page--;render()};el('next').onclick=()=>{page++;render()};render();</script>"""
    html_text = html_text.replace('<script id="prev-data"></script>', script.replace('__DATA__', data_json))
    output.write_text(html_text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    source = pathlib.Path(args.source)
    settings = json.loads(pathlib.Path(args.settings).read_text())
    candidates = load_candidates(source)
    payload = dict(candidates=candidates, rows=[])
    with ProcessPoolExecutor(max_workers=max(1, min(args.workers, 4))) as pool:
        futures = [pool.submit(evaluate_candidate, (candidate, str(source), settings.get("overlay", {}))) for candidate in candidates]
        for future in as_completed(futures):
            result = future.result()
            payload["rows"].extend(result["rows"])
            print(json.dumps({"symbol": result["candidate"]["symbol"], "kind": result["candidate"]["kind"], "rows": len(result["rows"])}, separators=(",", ":")), flush=True)
    payload["rows"].sort(key=lambda r: (r["symbol"], r["kind"], r["prevWindow"], r["prevMinCount"]))
    render_html(payload, pathlib.Path(args.output))
    print(json.dumps({"candidates": len(candidates), "rows": len(payload["rows"]), "output": args.output}, separators=(",", ":")))


if __name__ == "__main__":
    main()
