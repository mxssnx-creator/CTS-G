#!/usr/bin/env python3
"""Write a self-contained German research report from the two replay phases."""
from __future__ import annotations

import argparse
from datetime import datetime
from html import escape
import json
from pathlib import Path


def key(row):
    return row["name"], row["step"], row["ratio"]


def check_phases(first, second, description):
    for pair in (first, second):
        if len(pair["symbols"]) != 50 or pair.get("cases_complete") != pair.get("cases_total"):
            raise ValueError(f"Incomplete {description} phase")
    if first["symbols"] != second["symbols"] or first["until"] != second["from"]:
        raise ValueError(f"The {description} phases must share a universe and meet at the frozen split")


def promotion(dev, holdout):
    return all(
        row["trades"] >= minimum and row["pf"] is not None and row["pf"] >= 1.15
        and row["stress_pf"] is not None and row["stress_pf"] > 1.02
        and row["positive_hours"] >= .5 * row["hours"]
        and row["net_pct"] + row["open_mark_pct"] > row["marked_dd_pct"]
        and row["marked_dd_pct"] <= 1.0
        and row["positive_symbols"] >= 12 and row["largest_symbol_gain_share"] <= .22
        for row, minimum in ((dev, 60), (holdout, 50))
    )


def num(value, digits=2):
    return "–" if value is None else f"{value:.{digits}f}".replace(".", ",")


def pct(success, total):
    return "–" if not total else f"{num(success * 100 / total, 1)} %"


def compact(record):
    return [record["pf"], record["stress_pf"], record["hour_dd_pct"], record["positive_hours"],
            record["active_hours"], record["hours"], record["trades"], record["net_pct"]]


def render(first, second, native_first, native_second, out):
    check_phases(first, second, "screen")
    check_phases(native_first, native_second, "native")
    if first["feed_sha256"] != second["feed_sha256"]:
        raise ValueError("The two screen phases use different source data")
    if first["symbols"] != native_first["symbols"] or first["from"] != native_first["from"]:
        raise ValueError("Native and screening universes differ")
    if native_first["stage_policy"] != native_second["stage_policy"]:
        raise ValueError("The confirmation stages changed after the split")
    held = {key(row): row for row in second["results"]}
    matrix = [[row["name"], row["family"], row["clock"], row["band"], row["step"], row["ratio"],
               row["hold"], *compact(row), *compact(held[key(row)])] for row in first["results"]]
    if len(held) != len(matrix):
        raise ValueError("Screening matrices differ")

    tested = {key(row["cases"][0]): row for row in native_second["results"]}
    native = [(row["cases"][0], row, tested[key(row["cases"][0])])
              for row in native_first["results"]]
    native.sort(key=lambda pair: (promotion(*pair[1:]), pair[2]["pf"] or 0, pair[2]["trades"]), reverse=True)
    stage_rows = []
    hourly = []
    for case, dev, oos in native:
        label = f"{case['name']} · TP {num(case['step'] / 10, 1)} % · SL ×{num(case['ratio'])}"
        hourly.append(dict(label=label, series=[*dev["hourly"], *oos["hourly"]]))
        cells = [
            escape(label), str(case["hold"]),
            num(dev["pf"], 3), str(dev["trades"]), pct(dev["positive_hours"], dev["hours"]),
            num(oos["pf"], 3), num(oos["stress_pf"], 3), str(oos["trades"]),
            pct(oos["positive_hours"], oos["hours"]), pct(oos["positive_hours"], oos["active_hours"]),
            num(oos["marked_dd_pct"], 3) + " %", num(oos["net_pct"], 3) + " %",
            str(oos["open"]),
            " / ".join(str(oos["stage_support"][s]) for s in ("base", "main", "real", "exec")),
            " / ".join(num(oos["median_post_stage_pf"][s]) for s in ("base", "main", "real", "exec")),
            "Aufgenommen" if promotion(dev, oos) else "Studie",
        ]
        stage_rows.append("<tr>" + "".join("<td>" + cell + "</td>" for cell in cells) + "</tr>")

    data = json.dumps(dict(matrix=matrix, hourly=hourly), ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    def date(value):
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")

    stage_policy = native_first["stage_policy"]
    policy = ("Im Prüfprofil nutzt Base Long 10. Alle vier Stufen prüfen Short 4 und Last 1–5 mit beiden Vorzeichen. "
              "Main, Real und Exec laufen ohne Long-Fenster."
              if "base" in stage_policy else "Die Stufen verwenden den vollständigen CTS-G-Last-N-Grid.")
    replacements = {
        "__PROFILE_COUNT__": str(len(first["probes"])),
        "__TRIAL_COUNT__": f"{len(matrix):,}".replace(",", "."),
        "__NATIVE_COUNT__": str(len(native)),
        "__PROMOTED__": str(sum(promotion(dev, oos) for _, dev, oos in native)),
        "__STAGE_ROWS__": "".join(stage_rows), "__DATA__": data,
        "__SYMBOLS__": escape(", ".join(first["symbols"])),
        "__INPUT_HASH__": escape(first["feed_sha256"]),
        "__SCREEN_TIME__": num(first["elapsed_s"] - first.get("signal_seconds", 0), 1),
        "__SOURCE_BASE__": escape(first["engine_commit"][:12]),
        "__PREHISTORY__": date(first["prehistory_from"]), "__START__": date(first["from"]),
        "__SPLIT__": date(first["until"]), "__END__": date(second["until"]),
        "__STAGE_POLICY__": policy,
        "__NATIVE_SL_FLOOR__": num(native_first["results"][0]["min_sl_distance_pct"]),
    }
    report = TEMPLATE
    for marker, value in replacements.items():
        report = report.replace(marker, value)
    out.write_text(report, encoding="utf-8")


TEMPLATE = """<!doctype html>
<html lang="de">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CTS-G Indikatorstudie · 50 BingX-Märkte</title>
<style>
:root{color-scheme:dark;--bg:#0b1221;--panel:#111d30;--line:#283956;--text:#eaf1ff;--muted:#a5b8d5;--blue:#7cceff;--up:#55cba1;--down:#fa877f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 system-ui,sans-serif}
main{max-width:1440px;margin:auto;padding:1.7rem clamp(.9rem,3.4vw,3.2rem) 4rem}section{margin-top:2rem;padding:1.25rem;background:var(--panel);border:1px solid var(--line);border-radius:.65rem}
h1{line-height:1.08;font-size:clamp(2rem,5vw,3.6rem);margin:.45rem 0 .85rem}h2{line-height:1.17;margin:0 0 .7rem}p{max-width:84ch;color:var(--muted)}a{color:var(--blue)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:.7rem;margin-top:1.4rem}.card{padding:.85rem 1rem;background:var(--panel);border:1px solid var(--line);border-radius:.55rem}.card strong{display:block;font-size:1.45rem}.card span{font-size:.9rem;color:var(--muted)}
.scroll{overflow:auto;border:1px solid var(--line);border-radius:.4rem}table{border-collapse:collapse;width:100%;min-width:980px;font-variant-numeric:tabular-nums}
th,td{text-align:right;white-space:nowrap;padding:.5rem .65rem;border-bottom:1px solid var(--line)} th{background:#182742;color:#d5e3f8;font-size:.84rem} th:first-child,td:first-child{text-align:left}
fieldset{padding:0;margin:0 0 .8rem;border:0;display:flex;flex-wrap:wrap;gap:.5rem 1rem}label{display:flex;flex-direction:column;color:var(--muted)}
input,button,select{background:#172641;border:1px solid #3b547a;border-radius:.35rem;padding:.45rem .65rem;color:var(--text);font:inherit}
button{cursor:pointer}button:disabled{opacity:.42;cursor:default}.pages{display:flex;align-items:center;gap:.7rem;margin-top:.7rem}
.hour-bars{display:grid;grid-template-columns:repeat(auto-fit,minmax(10px,1fr));gap:2px;margin:.8rem 0}.hour{height:16px;border-radius:2px;background:#30415b}.hour.up{background:var(--up)}.hour.down{background:var(--down)}
.positive{color:var(--up)}.negative{color:var(--down)}.mono{font-family:ui-monospace,monospace;overflow-wrap:anywhere}button:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--blue)}
</style></head>
<body><main>
<header><h1>CTS-G Indikatorstudie</h1><p>50 BingX-USDT-Perpetuals. Drei Tage Vorlauf ab __PREHISTORY__ und sieben Tage Replay von __START__ bis __END__ UTC. Der Holdout begann __SPLIT__ UTC nach der festen Vorauswahl.</p>
<div class="cards"><div class="card"><strong>720.000</strong><span>1-Minuten-Kerzen</span></div>
<div class="card"><strong>__PROFILE_COUNT__</strong><span>Signalprofile</span></div><div class="card"><strong>__TRIAL_COUNT__</strong><span>unabhängige Geometrien</span></div>
<div class="card"><strong>__PROMOTED__ von __NATIVE_COUNT__</strong><span>native Abnahme bestanden</span></div></div></header>
<section><h2>Base → Main → Real → Exec</h2>
<p>Jede Vorauswahl bekam ihre eigene Konfiguration und ihre eigenen Long- und Short-Sets. CTS-G prüfte alle vier Stufen mit kausalem Auto PF und drei Tagen Lookback. __STAGE_POLICY__ Stage-Zahlen zeigen tägliche Stichproben mit mindestens einem qualifizierten Pfad je Stufe.</p>
<div class="scroll"><table><thead><tr><th>Profil</th><th>Hold min</th><th>Dev-PF</th><th>Dev Trades</th><th>Dev +Std.</th><th>Exec-PF 72h</th><th>PF +4 bp</th><th>Trades</th><th>+Std. gesamt</th><th>+Std. aktiv</th><th>Mark-DD</th><th>Netto</th><th>Offen</th><th>Base / Main / Real / Exec</th><th>Median Post-Stage-PF</th><th>Status</th></tr></thead><tbody>__STAGE_ROWS__</tbody></table></div>
<p>Abnahme je Phase mit mindestens 60 beziehungsweise 50 geschlossenen Positionen, Exec-PF ≥ 1,15, PF mit vier zusätzlichen Basispunkten &gt; 1,02 und mindestens 50 % profitablen Stunden. Marktwert-Drawdown höchstens 1 % und kleiner als der Nettozuwachs einschließlich offener Marks. Mindestens zwölf netto positive Symbole und höchstens 22 % der positiven Symbolerträge aus einem Markt.</p>
<label for="hours">Stundenverlauf eines nativen Profils</label><select id="hours"></select><div id="heat" class="hour-bars"></div><p>Grün steht für positives Netto, rot für negatives Netto. Grau zeigt Stunden ohne Nettoertrag. Die Einzelwerte sind an jeder Stunde abrufbar.</p>
</section>
<section><h2>Alle unabhängigen Versuche</h2>
<p>Der Rohvergleich bewertet für jedes Signalprofil und jede einzelne Stop-/Zielgeometrie einen eigenständigen Lauf. Die 36 Geometrien decken TP 0,2–5,0 %, SL-Faktoren 0,5 / 0,75 / 1,0 und Zeitstopps von 15–360 Minuten ab. Die Stage-Kette gehört zur nativen Prüfung oben.</p>
<fieldset><label>Indikator oder Familie <input id="query" type="search" placeholder="RSI, PSAR, ADX, Donchian …"></label><label>TP-Band <select id="band"><option value="">Alle</option></select></label>
<label>Signalintervall <select id="interval"><option value="">Alle</option></select></label>
<label>Sortierung <select id="sort"><option value="stage">Stabilität in der Entwicklung</option><option value="pf">PF im Holdout</option><option value="hour">Profitable Holdout-Stunden</option><option value="dd">Holdout-Drawdown</option></select></label></fieldset>
<div class="scroll"><table><thead><tr><th>Indikator</th><th>Min.</th><th>TP-Band</th><th>TP</th><th>SL ×</th><th>Hold</th><th>Dev PF</th><th>Dev +Std.</th><th>Holdout PF</th><th>PF +4 bp</th><th>Holdout +Std.</th><th>Stunden-DD</th><th>Trades</th></tr></thead><tbody id="trials"></tbody></table></div>
<div class="pages"><button id="prev">Zurück</button><span id="page" aria-live="polite"></span><button id="next">Weiter</button></div>
</section>
<section><h2>Preis-, Kosten- und Messregeln</h2>
<p>Jeder Signalentscheid nutzt geschlossene Kerzen. Der modellierte Einstieg nutzt den Schlusskurs der Signalkerze. Gleichzeitige Stop- und Zieltreffer werden zum Stop abgerechnet. Beim Überspringen eines aktiven Stops zählt der ungünstigere Eröffnungskurs. Pro Abschluss werden 0,11 % modellierte Roundtrip-Kosten abgezogen. Der Kostentest belastet dieselben Fills zusätzlich mit 0,04 %. Der Stop-Abstand enthält mindestens die Kosten und 0,10 % im Rohvergleich beziehungsweise __NATIVE_SL_FLOOR__ % in der nativen Prüfung. Finanzierung, Venue-Latenz und Mindestlosgrößen liegen außerhalb der Preis-Simulation.</p>
<p>Netto und Drawdown beziehen sich auf gleich große fiktive Positionen und 100 Einheiten Ausgangskapital. Der Rohvergleich misst Drawdown aus stündlich gebuchten Schlüssen, die native Prüfung bewertet den offenen und geschlossenen Positionsbestand je Minute. Profitable Stunden zählen nach Schlusszeit. Der Anteil über alle Stunden schließt Leerlauf ein.</p>
<p>Die Wilder-Profile prüfen RSI-Rückkreuzungen sowie ADX- und DI-Wechsel. PSAR variiert Beschleunigung und Limit. Keltner-Kanäle testen ATR-basierte Ausbrüche, Donchian-Varianten die vorherige Handelsspanne. Der Kandidatenkatalog enthält auch Trend, Reversion, Breakout, Volumen, Kerzenmuster und portierte CTS-A-O- und CTS-C-Technik. <a href="https://ta-lib.org/functions/rsi.html">RSI</a> · <a href="https://ta-lib.org/functions/adx.html">ADX</a> · <a href="https://ta-lib.org/functions/sar.html">PSAR</a> · <a href="https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/keltner-channels">Keltner</a> · <a href="https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/price-channels">Donchian</a></p>
<p>Der zeitlich getrennte Holdout begrenzt Nachselektion. Die Zahl getesteter Varianten und die gemeinsame Marktbewegung der Symbole bleiben für die Aussagekraft wesentlich. <a href="https://doi.org/10.21314/JCF.2016.322">Bailey et al.</a></p>
<p>Der Rohvergleich nach vorhandenen Signalen dauerte __SCREEN_TIME__ Sekunden. Datenquelle und Berichtsbasis <strong>__SOURCE_BASE__</strong>. <a href="https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-market/api-reference.md">BingX Market Data</a></p>
<details><summary>Symbole und Prüfsumme</summary><p>__SYMBOLS__</p><p class="mono">SHA-256 __INPUT_HASH__</p></details>
</section>
</main>
<script type="application/json" id="study-data">__DATA__</script>
<script>
const data=JSON.parse(document.querySelector('#study-data').textContent);
const $=id=>document.getElementById(id), pageSize=75;
const fmt=new Intl.NumberFormat('de-AT',{minimumFractionDigits:2,maximumFractionDigits:3});
const number=v=>v===null?'–':fmt.format(v);
const percentage=(n,d)=>d?number(100*n/d)+' %':'–';
let page=0;
function option(select,value,label){const n=document.createElement('option');n.value=value;n.textContent=label;select.append(n)}
for(const band of ['micro','minimal','short','normal','long','wide']) option($('band'),band,band);
for(const interval of [...new Set(data.matrix.map(row=>row[2]))].sort((a,b)=>a-b)) option($('interval'),interval,interval+' min');
data.hourly.forEach((r,i)=>option($('hours'),i,r.label));
function heat(){
 const series=data.hourly[+$('hours').value]?.series??[];
 $('heat').replaceChildren(...series.map(row=>{
  const c=document.createElement('span');
  const pnl=row.net_pct;
  c.className='hour '+(pnl>0?'up':pnl<0?'down':'');
  c.title=new Date(row.t*1000).toISOString().slice(0,13).replace('T',' ')+' UTC · '+number(pnl)+' % · '+row.orders+' Abschlüsse';
  c.setAttribute('aria-label',c.title);
  return c;
 }));
}
function draw(){
 const query=$('query').value.trim().toLowerCase(), band=$('band').value, interval=$('interval').value;
 const rows=data.matrix.filter(r=>(!band||r[3]===band)&&(!interval||r[2]===+interval)&&
    (!query||(r[0]+' '+r[1]).toLowerCase().includes(query)));
 const sort=$('sort').value;
 const score=sort==='pf'?r=>r[15]??-1:sort==='hour'?r=>r[18]/r[20]:
   sort==='dd'?r=>r[21]>0?-r[17]:-Infinity:r=>r[13]>0?Math.min(r[7]??-1,r[8]??-1):-Infinity;
 rows.sort((a,b)=>score(b)-score(a));
 const pages=Math.max(1,Math.ceil(rows.length/pageSize));
 page=Math.min(page,pages-1);
 $('trials').replaceChildren(...rows.slice(page*pageSize,(page+1)*pageSize).map(r=>{
    const tr=document.createElement('tr');
    const fields=[r[0],r[2],r[3],number(r[4]/10)+' %',number(r[5]),r[6],number(r[7]),percentage(r[10],r[12]),
       number(r[15]),number(r[16]),percentage(r[18],r[20]),number(r[17])+' %',r[21]];
    for(const [i,value] of fields.entries()){
      const td=document.createElement('td');td.textContent=value;
      if(i===8&&r[15]!==null) td.className=r[15]>=1?'positive':'negative';
      tr.append(td);
    }
    return tr;
 }));
 $('page').textContent=(page+1)+' / '+pages+' · '+rows.length.toLocaleString('de-AT')+' Ergebnisse';
 $('prev').disabled=page===0;$('next').disabled=page+1>=pages;
}
for(const name of ['query','band','interval','sort']) $(name).addEventListener('input',()=>{page=0;draw()});
$('prev').addEventListener('click',()=>{page--;draw()});$('next').addEventListener('click',()=>{page++;draw()});
$('hours').addEventListener('change',heat);draw();heat();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ("development", "holdout", "native_development", "native_holdout", "out"):
        ap.add_argument(name, type=Path)
    args = ap.parse_args()
    inputs = (args.development, args.holdout, args.native_development, args.native_holdout)
    render(*(json.loads(p.read_text()) for p in inputs), args.out)


if __name__ == "__main__":
    main()
