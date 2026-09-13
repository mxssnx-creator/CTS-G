"""Seven-day observed-candle audit of independent CTS Set qualification.

Reuses stored market inputs. No API client or account access. Last-N results
are end-of-window diagnostics, not a causal trading-policy performance claim.
"""
import hashlib
import html
import json
import pathlib
import sys
import time
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server/pulse'))
from set_engine import SetBook


def main():
    out = ROOT/'reports/config-isolation-20260912'
    out.mkdir(exist_ok=True, parents=True)
    settings = dict(histEnabled=True,histLookbackBars=10140,histMinBars=60,histWarmup=60,
        stratGeneral=True,stratIndications=True,stratTrailing=True,stratBlock=False,
        histSimulateBlock=False,histSimulateDca=False,setMinStep=5,setStepMax=10,
        slToTpRatios=[.2,.4,.6,.8],trailArmMin=.3,trailArmMax=.3,trailGiveMin=.1,trailGiveMax=.1,
        minPf=1.02,baseEvalPosCount=30,mainEvalPosCount=5,realEvalPosCount=3)
    started=time.perf_counter(); results=[]; sources=[]; progress=0; total_closes=0
    for symbol in ('BCH-USDT','SOL-USDT','XRP-USDT'):
        source=ROOT/'reports/7d-simulation-20260911/data'/(symbol+'.json')
        data=json.loads(source.read_text()); bars=[row[1] for row in data['rows']]
        now=data['rows'][-1][0]/1000+60
        b=SetBook();b.load(settings);b.ingest_bars(symbol,bars)
        hist={};counts={}
        def tick():
            nonlocal progress
            progress+=1
        b._replay_symbol(symbol,hist,now,hist_counts=counts,on_step=tick)
        total_closes+=sum(counts.values())
        for st in b.by_idx:st.hist=hist.get(st.id,[])
        sources.append(dict(symbol=symbol,inputSha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            start=data['start'],end=data['end'],bars=len(bars),sets=len(b.by_idx),closes=sum(counts.values())))
        for n in range(5,76,5):
            b.pf_n=b.min_samples=n
            for st in b.by_idx:
                b._score_pair((st,None))
                for side,m in st.by_side.items():
                    results.append(dict(symbol=symbol,setId=st.id,strategy=st.kind,pack=st.pack,side=side,
                        tpPct=round(st.tp_pct*100,4),slPct=round(st.tp_pct*st.sl_ratio*100,4),
                        slRatio=st.sl_ratio,trail=st.trail_key or 'off',lastN=n,
                        sampleCount=m['base_n'],basePf=m['base_pf'],base=b._base_metrics_ok(m),
                        mainN=m['main_n'],mainPf=m['main_pf'],realN=m['real_n'],realPf=m['real_pf'],
                        real=b._real_metrics_ok(m),ddHours=round(m['max_dd_s']/3600,4)))
        print(json.dumps(sources[-1]),flush=True)
    counts=Counter((r['symbol'],r['lastN']) for r in results if r['base'])
    rows=sorted((r for r in results if r['base']),key=lambda r:(-r['basePf'],r['slPct'],r['setId']))
    summary=dict(kind='offline candle replay and end-of-window qualification audit',settings=settings,
        sources=sources,evaluations=len(results),baseQualified=sum(r['base'] for r in results),
        realQualified=sum(r['real'] for r in results),closedSimulations=total_closes,progressCallbacks=progress,
        seconds=round(time.perf_counter()-started,3),threshold=1.02,liveOrdersSubmitted=0,
        defaultPolicyChanged=False,results=results,
        limitations=['Normal/trailing risk catalog only; Block/DCA isolation is covered by regression tests.',
            'Stored seven-day candle data; costs 0.10% round trip, without funding/order-book latency.',
            'Last-N alternatives overlap. Qualified counts are configurations/windows, not live orders.',
            'These descriptive results do not select hindsight-optimized live defaults.'])
    (out/'evidence.json').write_text(json.dumps(summary,indent=2)+'\n')
    table=''.join('<tr>'+''.join(f'<td>{html.escape(str(v))}</td>' for v in
        [symbol,*[counts[symbol,n] for n in range(5,76,5)]])+'</tr>' for symbol in ('BCH-USDT','SOL-USDT','XRP-USDT'))
    best=''.join('<tr>'+''.join(f'<td>{html.escape(str(r[k]))}</td>' for k in
        ('symbol','setId','side','tpPct','slPct','trail','lastN','basePf','mainPf','realPf'))+'</tr>' for r in rows[:100])
    doc=f'''<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CTS-G · Unabhängige Config-Prüfung</title><style>
body{{margin:0;background:#f2f6fb;color:#17283c;font:16px/1.6 system-ui}}main{{max-width:1220px;margin:auto;padding:32px 20px}}
h1{{font-size:clamp(28px,5vw,46px);line-height:1.15}}h2{{font-size:22px;margin-top:36px}}.cards{{display:flex;gap:16px;flex-wrap:wrap}}
.card{{padding:20px;background:white;border:1px solid #d6e0ed;border-radius:12px;flex:1;min-width:160px}}strong{{font-size:30px;display:block;color:#145eb3}}
.scroll{{overflow:auto;background:white;border:1px solid #d6e0ed;border-radius:12px}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:10px;text-align:left;white-space:nowrap;border-bottom:1px solid #e4ebf4}}th{{background:#e6effa}}
a{{color:#145eb3}}.note{{color:#52647a}}li{{margin:6px 0}}</style><main>
<p>CTS-G · Prüfstand 12. September 2026</p><h1>Jedes Config-Set zählt für sich.</h1>
<p>Base erfordert den eigenen PF &gt; 1,02 und die vollständige eigene Stichprobe. Erst danach folgen Main und Real.
System-Ergebnisstatistiken berücksichtigen Base-validierte Sets; bestätigte Exchange-Ergebnisse bleiben vollständig nachvollziehbar.</p>
<div class="cards"><div class="card"><strong>{len(results):,}</strong>Set-/Richtungs-/Fensterprüfungen</div>
<div class="card"><strong>{summary['baseQualified']:,}</strong>Base qualifiziert</div>
<div class="card"><strong>{summary['realQualified']:,}</strong>Real qualifiziert</div>
<div class="card"><strong>{total_closes:,}</strong>Simulierte Abschlüsse</div></div>
<p class="note">BCH, SOL und XRP · 7 Tage plus Aufwärmphase · {summary['seconds']} Sekunden · {progress} Fortschrittsmeldungen.
Diese Zahlen sind Offline-Ergebnisse; sie sind keine geöffneten Live-Orders. VST-Tests bleiben auf 20 Symbole eingestellt.</p>
<h2>Base-validierte Konfigurationen je Last-N-Fenster</h2><div class="scroll"><table><thead><tr><th>Symbol</th>
{''.join(f'<th>{n}</th>' for n in range(5,76,5))}</tr></thead><tbody>{table}</tbody></table></div>
<h2>Höchste beobachtete Set-PF-Werte · bis zu 100 Ergebnisse</h2><p>Jede Zeile gehört zu genau einem Set, einer Richtung und einem Auswertungsfenster. TP und SL in Prozent.
Ein PF von 0 in Main/Real bedeutet, dass die Stufe wegen ihrer Vorstufe nicht berechnet wurde.</p>
<div class="scroll"><table><thead><tr>{''.join(f'<th>{x}</th>' for x in ('Symbol','Set','Richtung','TP %','SL %','Trailing','Last N','Base PF','Main PF','Real PF'))}</tr></thead><tbody>{best}</tbody></table></div>
<h2>Prüfumfang und Einordnung</h2><ul><li>Last N: 5–75 in 5er-Schritten; normal und trailing; getrennte LONG/SHORT-Evidenz; General und Indications.</li>
<li>SL:TP-Verhältnisse 0,2 / 0,4 / 0,6 / 0,8; Steps 5–10; Trailing 0,3:0,1. Die tatsächlichen SL-Prozente stehen je Set in der Tabelle.</li>
<li>PF ist CTS Cost-PF: 1,00 = kostendeckend, 1,10 = ein Positionskostenbetrag Nettogewinn im Mittel.</li>
<li>Dies ist eine Auswertung der letzten Abschlüsse nach dem Replay, kein erneuter vollständiger kausaler Policy-Sweep. Fenster überlappen.</li>
<li>Keine Optimierung von Live-Defaults aus diesen nachträglich ausgewählten Gewinnern. Standard bleibt: zwei Tage Historie und Base Last 30.</li></ul>
<p><a href="evidence.json">Vollständige Messwerte und Eingabe-Hashes</a></p></main></html>'''
    (out/'config-evidence.html').write_text(doc)
    print(json.dumps({k:v for k,v in summary.items() if k not in ('results','sources','settings')}),flush=True)


if __name__=='__main__':main()
