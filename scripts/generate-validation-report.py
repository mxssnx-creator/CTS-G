#!/usr/bin/env python3
"""Self-contained, read-only HTML evidence report; never starts an engine."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path
import re


def load(path: Path):
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Report input exceeds 32 MiB")
    return json.loads(path.read_text())


def display(value, places=3):
    if value is None:
        return "—"
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return f"{value:,.{places}f}"
    return escape(str(value))


def render(data: dict, job: dict, engine_log: str, source_sha: str) -> str:
    matrix = data.get("matrix", [])
    rows = [row for symbol in matrix for row in symbol.get("rows", [])]
    if len(rows) > 50000 or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Oversized or duplicate configuration matrix")
    if len(rows) != data["completed"]:
        raise ValueError("Coverage count differs from actual matrix")
    engine = re.findall(r"(\d+)/(\d+) passed\s+fail=(\d+)", engine_log)
    engine_status = (f"{engine[-1][0]}/{engine[-1][1]}, Fehler {engine[-1][2]}"
                     if engine else "Nicht vollständig abgeschlossen")
    timestamp = datetime.fromtimestamp(data["updatedAt"], timezone.utc).isoformat(timespec="seconds")
    reasons = Counter(row["status"] for row in rows)
    symbol_rows = []
    for item in sorted(matrix, key=lambda item: item["symbol"]):
        best = max(item["rows"], key=lambda row: row["pf"])
        positive = sum(row["pf"] > data["minPf"] for row in item["rows"])
        symbol_rows.append("<tr>" + "".join(f"<td>{display(value)}</td>" for value in
            (item["symbol"], item["bars"], item["completed"], positive,
             item["eligibleCount"], best["pf"], best["indication"], best["direction"],
             best["tpPct"], best["slPct"], best["trainPf"], best["holdoutPf"],
             best["trainN"], best["holdoutN"], best["status"])) + "</tr>")
    columns = [
        ("symbol", "Symbol"), ("indication", "Indikation"), ("direction", "Richtung"),
        ("tpPct", "TP %"), ("slPct", "SL %"), ("slRatio", "SL/TP"),
        ("n", "Trades"), ("trainN", "Train N"), ("holdoutN", "Holdout N"),
        ("pf", "PF gesamt"), ("trainPf", "PF Train"), ("holdoutPf", "PF Holdout"),
        ("last8", "PF letzte 8"), ("last25", "PF letzte 25"), ("last75", "PF letzte 75"),
        ("tradesPerHour", "Trades/h"), ("maxDrawdownR", "Max DD R"),
        ("avgDdS", "Ø DDT s"), ("maxDdS", "Max DDT s"),
        ("netAvgPct", "Netto/Trade %"), ("openUnresolved", "Offen am Ende"),
        ("splitCensored", "Am Split zensiert"), ("status", "Gate"), ("id", "Config-ID"),
    ]

    def cell(row, key):
        if key.startswith("last"):
            window = row.get("recentPf", {}).get(key)
            if not window:
                return "nicht erhoben"
            if not window["available"]:
                return f"unvollständig ({window['n']}/{window['requestedN']})"
            return display(window["classicPf"])
        return display(row.get(key))

    initial = "".join("<tr>" + "".join(f"<td>{cell(row, key)}</td>" for key, _ in columns)
                      + "</tr>" for row in rows[:100])
    data_json = json.dumps({"rows": rows, "columns": columns}, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    parameters = [{key: item.get(key) for key in
                   ("symbol", "bars", "settingsKey", "settings", "costPct", "minPf", "requiredSamples", "maxDrawdownR")}
                  for item in matrix]
    reason_html = "".join(f"<li>{escape(reason)}: <strong>{count}</strong></li>" for reason, count in sorted(reasons.items()))
    evidence = escape(json.dumps({"sourceBySymbol": data["sourceBySymbol"],
        "sourceSha256": source_sha, "requested": data["requested"], "completed": data["completed"],
        "phase": job.get("phase"), "hours": job.get("hours"), "lookback": job.get("lookback"),
        "elapsedMs": job.get("elapsedMs"), "parameters": parameters}, indent=2, ensure_ascii=False))
    head = "".join(f"<th scope=col>{escape(label)}</th>" for _, label in columns)
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light"><title>CTS-G · Validierungsbericht</title>
<style>
:root{{font:15px/1.55 system-ui,sans-serif;color:#e5eaf4;background:#101620;color-scheme:dark}}
*{{box-sizing:border-box}}body{{margin:0}}main{{max-width:1500px;padding:28px 24px;margin:auto}}
h1{{font-size:clamp(26px,4vw,42px);line-height:1.15;margin:8px 0 18px}}h2{{font-size:21px;margin:0 0 12px}}
h3{{font-size:16px}}p{{max-width:100ch}}.muted{{color:#a7b4c9}}.label{{letter-spacing:.12em;text-transform:uppercase;font-size:12px;color:#84b8ed}}
.panel{{background:#171f2d;border:1px solid #2d3a4c;border-radius:12px;padding:22px;margin:18px 0}}
.warning{{border-left:4px solid #e5ab57}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
.card{{border:1px solid #344155;padding:16px;border-radius:8px}}.value{{font-size:30px;font-weight:650}}
.scroll{{max-width:100%;overflow:auto;border:1px solid #344155;border-radius:6px}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid #2a384d;white-space:nowrap}}th{{background:#202d40;font-size:12px}}
td{{font-size:13px}}tr:nth-child(even){{background:#1c2636}}.wrap td{{white-space:normal}}code,pre{{font:12px/1.5 ui-monospace,monospace}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#101620;padding:16px;border-radius:8px}}a{{color:#94c5fa}}
.tools{{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:14px 0}}input,select,button{{font:inherit;color:inherit;background:#111b29;border:1px solid #52627c;border-radius:6px;padding:9px}}
input{{width:min(100%,380px)}}button{{cursor:pointer}}button:disabled{{opacity:.4;cursor:default}}summary{{cursor:pointer}}
@media(max-width:600px){{main{{padding:18px 12px}}.panel{{padding:15px}}}}
</style></head><body><main>
<div class="label">CTS-G / Engineering &amp; Research Evidence</div><h1>Validierung und Konfigurationsabdeckung</h1>
<p class="muted">Kalkulationsstand (UTC): {timestamp} · Eigenständige HTML-Datei ohne externe Bibliotheken oder Tracking.</p>
<section class="panel warning"><h2>Keine Mainnet-Freigabe</h2>
<p>Historische Kandidaten: <strong>{data['selectedCount']}</strong>. Dieser Bericht dokumentiert Backtests auf öffentlichen Marktdaten,
nicht tatsächlich ausgeführte VST- oder Mainnet-Trades. Live-Exchange-Profitabilität wurde in diesem Lauf nicht bestätigt.
Keine neuen Mainnet-Orders, keine automatische Übernahme von Kandidaten und kein Remote-Rollout durch diesen Testlauf.</p>
<p>Die Remote-Prüfung am 2026-09-04, 20:37 UTC meldete beide CTS-G-Engine-Dienste als <strong>failed</strong>.
Der dortige Commit 7f51a6f importiert fehlende Hilfsfunktionen. Lokale Reparaturen sind vorhanden;
ein stabiler Remote-Betrieb ist damit noch nicht nachgewiesen.</p></section>
<div class="cards">
<div class="card">Geprüfte Konfigurationen<div class="value">{len(rows):,}</div>{data['coveragePct']}% des definierten Rasters</div>
<div class="card">Baseline-Kandidaten<div class="value">{data['selectedCount']}</div>maximal 5 pro Symbol/Indikation</div>
<div class="card">Baseline-PF-Schwelle<div class="value">&gt; {data['minPf']}</div>Train + Holdout + Gesamt, netto</div>
<div class="card">Kalkulationsdauer<div class="value">{display((job.get('elapsedMs') or 0)/1000,1)} s</div>begrenzte Pipeline, bis zu 2 Symbol-Lanes</div>
</div>
<section class="panel"><h2>Was vollständig abgedeckt ist</h2>
<p>3 Symbole × 8 Indikationsarten × 2 Richtungen × 9 TP-Werte × 9 SL-Werte = 3.888 Kombinationen.
TP 0,40–0,80%, SL 0,10–0,50%, jeweils Schrittweite 0,05 Prozentpunkte. Ein konkretes Indikator-Settings-Set pro Symbol;
die vollständigen Parameter und Hashes stehen unten. Dies ist <strong>keine</strong> vollständige Suche über alle denkbaren
Indikatorfenster, DCA-/Block-Varianten oder sämtliche Strategiekombinationen.</p>
<p>Baseline ohne Block, DCA, Trailing oder Early Exit. Kostenannahme 0,15% je Roundtrip; keine gemessenen Kontogebühren.
70/30 chronologischer Split, mindestens 8 abgeschlossene Trades in jedem Teil. Einstieg erst am Signal-Kerzenschluss,
Exit ab der Folgekerze; SL gewinnt bei uneindeutiger gleichzeitiger TP-/SL-Berührung; Stop-Gaps werden ungünstiger gefüllt.
Zusätzliche Gates: positive vorhandene Bewertungsfenster und maximal 6R Backtest-Drawdown.
Trades pro Stunde werden erst nach den Profitabilitäts-/Risiko-Gates priorisiert.</p>
<p>PF ist Netto-Bruttogewinn / Netto-Bruttoverlust, nicht der CTS-Kostenratio-Score. Der interne Wert 99 steht bei reinen
Gewinntapes für einen begrenzten PF-Platzhalter, nicht für statistisch abgesicherte Profitabilität.
Netto-% sind normalisierte Einzeltrade-Ergebnisse, kein Kontoertrag. DDT bezieht sich auf die realisierte Backtest-Kurve,
nicht auf Intratrade-Equity oder einen gemessenen Live-Konto-Drawdown.</p></section>
<section class="panel"><h2>Symbolübersicht und jeweils höchster Gesamt-PF</h2>
<p class="muted">Ein hoher Gesamt-PF allein bedeutet keine Freigabe. Besonders kleine Holdouts können scheinbar gute Ergebnisse entkräften.</p>
<div class="scroll"><table><thead><tr><th>Symbol</th><th>1m-Bars</th><th>Configs</th><th>PF &gt;1,02</th><th>Eligible</th><th>Max PF</th><th>Indikation</th><th>Richtung</th><th>TP %</th><th>SL %</th><th>Train PF</th><th>Holdout PF</th><th>Train N</th><th>Holdout N</th><th>Gate</th></tr></thead><tbody>{''.join(symbol_rows)}</tbody></table></div>
<h3>Ablehnungsgründe</h3><ul>{reason_html}</ul></section>
<section class="panel"><h2>Jede Konfiguration · {len(rows):,} Zeilen</h2>
<p>100 Zeilen pro Seite begrenzen die DOM-Last. Suche, Filter und Sortierung arbeiten auf der vollständig eingebetteten Matrix.
„Unvollständig (n/N)“ ist kein validierter Last-N-PF. Details bleiben pro Config-ID getrennt.</p>
<div class="tools"><label>Suche <input id="query" type="search" placeholder="Symbol, Indikation, Richtung oder Config-ID"></label>
<label>Filter <select id="filter"><option value="all">Alle</option><option value="positive">Gesamt-PF &gt; 1,02</option><option value="eligible">Alle Gates bestanden</option></select></label>
<label>Sortierung <select id="sort"><option value="id">Config-ID</option><option value="pf">PF absteigend</option><option value="tph">Trades/h absteigend</option></select></label></div>
<div class="tools"><button id="prev" disabled>Zurück</button><output id="count" aria-live="polite">1–100 / {len(rows)}</output><button id="next">Weiter</button></div>
<div class="scroll"><table id="configs"><thead><tr>{head}</tr></thead><tbody>{initial}</tbody></table></div>
<noscript>JavaScript ist deaktiviert: sichtbar sind die ersten 100 Konfigurationen. Die gesamte Matrix ist in dieser Datei eingebettet.</noscript></section>
<section class="panel"><h2>Tests und bekannte Grenzen</h2>
<div class="scroll"><table class="wrap"><thead><tr><th>Prüfung</th><th>Ergebnis</th><th>Einordnung</th></tr></thead><tbody>
<tr><td>Release-Engine-Gesamttest</td><td>{escape(engine_status)}</td><td>Offline, keine authentifizierten Exchange-Orders.</td></tr>
<tr><td>Forced-Grid-Tests</td><td>16/16 bestanden</td><td>Lookahead, Stop-Gaps, PF-Grenzen, Attribution, Holdout, Fenster.</td></tr>
<tr><td>Release-/Import-Verträge</td><td>6/6 bestanden</td><td>Trader, HTTP, Kalkulator, Instanznamen, Replay-Lock und Mengenaggregation.</td></tr>
<tr><td>JS/TS-Tests</td><td>191 + 34 bestanden; 4 übersprungen</td><td>Die 4 ausgelassenen Tests betreffen nicht vorhandene Template-Skill-Dateien.</td></tr>
<tr><td>Typecheck / Lint / Produktionsbuild</td><td>Bestanden</td><td>Keine erfolgreiche Browserabnahme daraus ableiten.</td></tr>
<tr><td>Session-Equity-Schutz (separater Branch)</td><td>16/16 gezielte Tests; Engine 283/283</td><td>100.000 Beobachtungen ca. 0,103s, 1 Initialschreibvorgang, Status &lt;1 KiB. Noch nicht ausgerollt.</td></tr>
<tr><td>Browser Desktop/Mobil</td><td>BLOCKIERT</td><td>Vorschau: uv_interface_addresses / errno 1; Browser-Daemon startet nicht. Keine visuelle Abnahme.</td></tr>
<tr><td>Installation / Remove / Reinstall / Remote-Soak</td><td>OFFEN</td><td>Kein vollständiger Lebenszyklustest oder abgeschlossener Rollout in diesem Lauf.</td></tr>
<tr><td>VST → Mainnet Promotion</td><td>NICHT FREIGEGEBEN</td><td>Kein unabhängiger Nachweis profitabler, bestätigter VST-Roundtrips je Config.</td></tr>
</tbody></table></div>
<p>Die separate Equity-Schutzlogik verwendet standardmäßig <strong>verbleibende 30% der Session-Start-Equity</strong>:
bei exakt 30% noch kein Trigger, darunter ein persistenter Entry-Stopp und Close-Management für eigene Positionen.
Das entspricht einem Verlust von mehr als 70% und ist kein moderates Drawdown-Limit. Exchange-Liquidation,
Netzwerkfehler oder Slippage können früher eingreifen; verlustfreie Ergebnisse sind nicht garantierbar.</p></section>
<section class="panel"><h2>Noch offene Projektarbeiten</h2><ul>
<li>Reparatur-Release vollständig abnehmen, danach kontrollierter Remote-Rollout mit Backup und Positionsabgleich.</li>
<li>VST-Evidenz pro Config aus bestätigten Roundtrips inklusive Gebühren, Volumen, PF 8/25/75 und DDT sammeln.</li>
<li>Multiinstanz-Installer einschließlich sicherem Remove/Reinstall und konsistenten Redis-/Port-Namensräumen fertigstellen.</li>
<li>Serverübersicht: dynamische Projektliste, geschützte Service-Aktionen, Volume/PF-Regler und Connection-Statistiken.</li>
<li>Session-Equity-Schutz inklusive UI und CTS-K-N integrieren; parallele Änderungen nicht überschreiben.</li>
<li>Langzeitstabilität, Log-Retention und Lastgrenzen unter realistischer Parallelität nachweisen.</li>
</ul></section>
<section class="panel"><details><summary>Vollständige Parameter und Herkunft</summary><pre>{evidence}</pre></details></section>
<p class="muted">Technischer Prüfbericht, keine Zusage zukünftiger Gewinne. Das Erzwingen positiver PF-Werte durch Lockerung von Gates ist keine Validierung.</p>
<script id="matrix-data" type="application/json">{data_json}</script>
<script>
(()=>{{"use strict";const payload=JSON.parse(document.getElementById("matrix-data").textContent);
const q=document.getElementById("query"),filter=document.getElementById("filter"),sort=document.getElementById("sort");
const body=document.querySelector("#configs tbody"),prev=document.getElementById("prev"),next=document.getElementById("next"),count=document.getElementById("count");
let page=0,visible=payload.rows,task;const size=100;
function value(row,key){{if(key.startsWith("last")){{const m=row.recentPf?.[key];return !m?"nicht erhoben":!m.available?`unvollständig (${{m.n}}/${{m.requestedN}})`:m.classicPf.toFixed(3)}}const v=row[key];return v==null?"—":typeof v==="number"?(Number.isInteger(v)?String(v):v.toFixed(3)):String(v)}}
function render(){{const start=page*size,frag=document.createDocumentFragment();for(const row of visible.slice(start,start+size)){{const tr=document.createElement("tr");for(const [key] of payload.columns){{const td=document.createElement("td");td.textContent=value(row,key);tr.appendChild(td)}}frag.appendChild(tr)}}body.replaceChildren(frag);count.textContent=visible.length?`${{start+1}}–${{Math.min(start+size,visible.length)}} / ${{visible.length}}`:"0 Ergebnisse";prev.disabled=page===0;next.disabled=start+size>=visible.length}}
function update(){{const text=q.value.toLowerCase().trim();visible=payload.rows.filter(r=>(!text||r.id.toLowerCase().includes(text))&&(filter.value==="all"||(filter.value==="eligible"?r.eligible:r.pf>1.02)));visible.sort(sort.value==="pf"?(a,b)=>b.pf-a.pf||a.id.localeCompare(b.id):sort.value==="tph"?(a,b)=>b.tradesPerHour-a.tradesPerHour||a.id.localeCompare(b.id):(a,b)=>a.id.localeCompare(b.id));page=0;render()}}
q.addEventListener("input",()=>{{clearTimeout(task);task=setTimeout(update,150)}});filter.addEventListener("change",update);sort.addEventListener("change",update);prev.addEventListener("click",()=>{{if(page>0)page--;render()}});next.addEventListener("click",()=>{{if((page+1)*size<visible.length)page++;render()}});update();
}})();
</script></main></body></html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--engine-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.data_dir / "forced-configs.json"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    output = render(load(source), load(args.data_dir / "hist-calc.json"), args.engine_log.read_text(), digest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output)
    print(json.dumps({"output": str(args.output.resolve()), "bytes": args.output.stat().st_size,
                      "sourceSha256": digest, "visualVerification": "blocked"}))


if __name__ == "__main__":
    main()
