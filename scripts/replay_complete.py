"""Complete finite catalog audit, exact public candles, restartable bounded shards.

Uses the conservative, execution-costed research model; exports every tested
parameter/result. Does not call trading APIs or apply selected configurations.
Native pack catalog and per-indication adjustment grid are distinct experiments.
"""
import argparse, base64, datetime as dt, gzip, hashlib, html, json, os
import pathlib, subprocess, sys, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from replay_five_days import replay
from fetch_historic_window import validate
from set_engine import SetBook, IND_KINDS, IND_TAG_KIND, indication_kind_votes, general_signal, votes_to_signal

ROOT=pathlib.Path(__file__).resolve().parents[1]
WINDOWS={'20h':1200,'5d':7200,'14d':20160,'20d':28800}

def grids():
    book=SetBook();book.load({'setMinStep':1,'setStepMax':22})
    native=[]
    for st in book.by_idx:
        if st.pack!='general':continue
        for honor in (True,False):
            native.append(dict(strategy=st.kind,levels=0,incrementPct=0,volumeRatio=0,
                catalogId=st.id.replace('general:','PACK:',1),step=st.step,slRatio=st.sl_ratio,
                tpPct=max(.002,st.tp_pct)*100,
                slPct=max(.0015,st.tp_pct*max(.3,st.sl_ratio))*100,
                trailArmPct=st.trail_arm,trailGivePct=st.trail_give,
                honorTp=honor,maxHoldBars=book.hist_time_bars,
                scratchBars=max(8,int(book.scratch_s/60)),scratchMinPct=book.scratch_min*100))
    variants=[('base',0,0.,0.)]
    variants += [('block',n,.2,r) for n in range(1,7) for r in (.25,.5,1.)]
    variants += [('dca',n,step,r) for n in range(1,5) for step in (.05,.1,.2) for r in (.25,.5,1.)]
    adjust=[dict(strategy=s,levels=n,incrementPct=inc,volumeRatio=r,tpPct=tp/100,slPct=sl/100)
            for s,n,inc,r in variants for tp in range(40,81,5) for sl in range(10,51,5)]
    return {'catalog':native,'adjustments':adjust}

def prepare(path,out):
    blob=json.loads(pathlib.Path(path).read_text());validate(blob['rows'],blob['start']-3600000,blob['end'])
    bars=[b for _,b in blob['rows']]
    signals={k:[(0,0.)]*len(bars) for k in (*IND_KINDS,'pack:general','pack:indications')}
    settings={f'type{k.title()}':True for k in IND_KINDS}
    for i in range(60,len(bars)):
        window=bars[i-59:i+1]
        votes=indication_kind_votes(window,settings,blob['rows'][i][0]/1000)
        for direction,confidence,tag in votes:
            kind=IND_TAG_KIND.get(tag)
            if kind:signals[kind][i]=(direction,confidence)
        signals['pack:general'][i]=general_signal(window)[:2]
        signals['pack:indications'][i]=votes_to_signal(votes)[:2]
    dest=out/(blob['symbol']+'.prepared.json')
    dest.write_text(json.dumps(dict(bars=bars,signals=signals,end=blob['end'],
        source={k:v for k,v in blob.items() if k!='rows'},
        candleSha256=hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()),separators=(',',':')))
    return str(dest)

def job(args):
    prepared,window,kind,out,signature=args
    key=f'{window}_{pathlib.Path(prepared).name.split(".")[0]}_{kind.replace(":","-")}'
    output=pathlib.Path(out)/(key+'.json.gz');summary_path=pathlib.Path(out)/(key+'.summary.json')
    if output.exists() and summary_path.exists():
        old=json.loads(summary_path.read_text())
        if old.get('signature')==signature and old.get('sha256')==hashlib.sha256(output.read_bytes()).hexdigest():return old
    started=time.monotonic();blob=json.loads(pathlib.Path(prepared).read_text())
    n=WINDOWS[window];bars=blob['bars'][-n-60:];signals=blob['signals'][kind][-n-60:]
    if len(bars)!=n+60:raise ValueError('Exact requested history unavailable')
    family='catalog' if kind.startswith('pack:') else 'adjustments';cfg=grids()[family]
    results=[]
    for side in (1,-1):
        results.extend(dict(direction='LONG' if side==1 else 'SHORT',**r) for r in replay(bars,signals,side,cfg))
    for row in results:
        if row['n']!=sum(row['dailyN']) or abs(row['netPct']-sum(row['dailyNetPct']))>3e-5:
            raise AssertionError('Daily totals do not reconcile')
        if row['wins']+row['losses']>row['n']:raise AssertionError('Invalid close counts')
    columns=list(results[0]);packed={'columns':columns,'rows':[[r[k] for k in columns] for r in results]}
    raw=gzip.compress(json.dumps(packed,separators=(',',':'),allow_nan=False).encode(),compresslevel=6,mtime=0)
    output.write_bytes(raw)
    eligible=[r for r in results if r['trainN']>=8 and r['holdoutN']>=8]
    best=sorted(eligible,key=lambda r: (-(1e99 if r['pf']=='∞' else r['pf'] or 0),r['maxDrawdownPct']))[:5]
    summary=dict(key=key,window=window,symbol=blob['source']['symbol'],kind=kind,family=family,
        start=blob['end']-n*60000,end=blob['end'],rows=len(results),expectedRows=2*len(cfg),
        positive=sum(r['positive'] for r in results),qualified=sum(r['qualified'] for r in results),
        noTrades=sum(r['n']==0 for r in results),best=best,elapsedS=round(time.monotonic()-started,2),
        candleSha256=blob['candleSha256'],sha256=hashlib.sha256(raw).hexdigest(),signature=signature)
    summary_path.write_text(json.dumps(summary,separators=(',',':'),allow_nan=False))
    print(json.dumps({k:summary[k] for k in ('key','rows','positive','qualified','elapsedS')}),flush=True)
    return summary

def make_report(out,summaries,signature):
    summary=sorted(summaries,key=lambda x:(WINDOWS[x['window']],x['symbol'],x['kind']))
    # Many catalog settings have identical outcomes in a finite price path.
    # Keep every config identity, but embed each identical metric vector once.
    # This reduces the standalone HTML's DOM footprint without dropping rows.
    for s in summary:
        original=json.loads(gzip.decompress((out/(s['key']+'.json.gz')).read_bytes()))
        ci=original['columns'].index('config'); metric_columns=[k for k in original['columns'] if k!='config']
        metrics=[];references=[];seen={}
        for row in original['rows']:
            values=row[:ci]+row[ci+1:]
            key=json.dumps(values,separators=(',',':'))
            mid=seen.get(key)
            if mid is None:
                mid=len(metrics);seen[key]=mid;metrics.append(values)
            references.append([row[ci],mid])
        packed={'columns':original['columns'],'metricColumns':metric_columns,'metrics':metrics,'rows':references}
        raw=gzip.compress(json.dumps(packed,separators=(',',':'),allow_nan=False).encode(),compresslevel=9,mtime=0)
        (out/(s['key']+'.embedded.gz')).write_bytes(raw)
        s['embeddedSha256']=hashlib.sha256(raw).hexdigest();s['uniqueMetricVectors']=len(metrics)
    grids_json=json.dumps(grids(),separators=(',',':'),allow_nan=False).replace('<','\\u003c')
    headers=['Window','Symbol','Typ / Pack','Zeilen','Positiv','PF/Samples beide Abschnitte','Ohne Trades','Sekunden']
    table='<tr>'+''.join('<th>'+h+'</th>' for h in headers)+'</tr>'
    for s in summary:
        table+='<tr>'+''.join('<td>'+html.escape(str(s[k]))+'</td>' for k in ('window','symbol','kind','rows','positive','qualified','noTrades','elapsedS'))+'</tr>'
    audit_path=out/'acceptance.json';audit=json.loads(audit_path.read_text()) if audit_path.exists() else {'status':'Remote/UI acceptance pending'}
    with (out/'cts-g-complete-validation-20260906.html').open('w') as f:
        f.write('''<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CTS-G · vollständige Testmatrix</title><style>
body{margin:0;background:#0b1421;color:#e5edf7;font:15px/1.5 system-ui}main{max-width:1500px;margin:auto;padding:24px}section{background:#142337;padding:18px;margin:18px 0;border-radius:10px;overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:8px;border-bottom:1px solid #354860;text-align:left;white-space:nowrap}button,select{background:#253e5c;color:white;border:1px solid #597798;border-radius:5px;padding:8px;margin:4px}button{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere}.note{border-left:4px solid #e9be64;padding:16px;background:#34332a}h1{font-size:32px}a{color:#8cd7ff}</style><main><h1>CTS-G · Konfigurationen, Typen und Ergebnisse</h1>''')
        f.write(f'<p>{sum(s["rows"] for s in summary):,} abgeschlossene unabhängige Konfigurations-/Richtungs-/Symbol-/Fenster-Zeilen. Jede Zeile ist vollständig eingebettet.</p>')
        f.write('''<p class="note">Historische Ergebnisse sind unabhängige hypothetische Lanes, keine summierbare Kontorendite. Börsenbestätigte Remote-Ergebnisse und Funktionstests stehen separat. Kein automatisches Speichern von Gewinnern als Live-Default.</p>
<section><h2>Exakte Abdeckung und Methode</h2><p>Fenster: letzte 20 Stunden sowie 5, 14 und 20 abgeschlossene UTC-Tage bis 06.09.2026 00:00 UTC. XRP-USDT, BCH-USDT, SOL-USDT; jeweils 60 zusätzliche Vorlaufkerzen. Öffentliche BingX-1m-OHLCV; Zeitstempel, Vollständigkeit und SHA256 geprüft.</p>
<p>Native Katalog-Packs General und Indications: Schritte 1–22 × 30 SL:TP-Verhältnisse 0,1–3,0 × Normal plus 25 Trailing-Kombinationen × TP an/aus × LONG/SHORT. Alle tatsächlichen Katalogparameter werden exportiert. Acht einzelne Typen State, Signals, Active, Direction, Move, Common, Trend und Break: 81 TP/SL-Paare × 55 Base/Block/DCA-Varianten × LONG/SHORT. Block 1–6, DCA 1–4, Volumenratio 0,25/0,5/1,0 und DCA-Inkrement 0,05/0,10/0,20 %. Die Katalog-Packs aggregieren Signale; sie sind nicht acht unabhängige Typen. Die beiden Matrizen bleiben deshalb getrennt.</p>
<p>Im nativen Katalog werden die vorhandenen effektiven Floors ausdrücklich abgebildet: TP mindestens 0,20 %, SL mindestens 0,15 %, wirksames SL:TP-Verhältnis mindestens 0,3. Angeforderte 0,1/0,2 können deshalb identische effektive Ausstiege haben. TP aus Schritt × 0,15 % Kosten, bis 3,30 %. Parameterfelder zeigen angeforderte und effektive Werte.</p>
<p>Kausales, konservatives Research-Modell: Entry am Candle-Close, Ausstiege erst ab Folgekerze. Vorhandener Stop vor TP und vor Additionen; Stop-Gaps zum schlechteren Open. Trailing wird aus bereits abgeschlossenen Close-Preisen aktualisiert und gilt erst für die nächste Kerze. Kein Exit-Bar-Reentry. Native Zeit-/Scratch-Ausstiege aus dem Katalog; die separate Typenmatrix untersucht feste TP/SL plus Additionen. Restpositionen an der 70/30-Grenze und am Ende mit Kosten geschlossen. Keine synthetischen Marktdaten, kein Forward-Lookup.</p>
<p>0,075 % modellierte Kosten pro ausgeführtem Entry/Add/Exit-Notional. Alle Netto-/Kosten-/Drawdown-Werte in Prozentpunkten des Original-Parent-Notionals. PF = positive Nettoabschlüsse / Betrag negativer Nettoabschlüsse; nicht die zusätzliche CTS-PositionCost-PF-Skala. DDT und Drawdown enthalten minütliche Close-Liquidationsequity; keine Intraminuten-Equity-Minima. ∞ = Gewinne ohne Verluste; null = keine Aussage. PF8/25/75 enthalten Stichprobengrößen.</p>
<p>Die vollständige hier definierte endliche Matrix ist kein Beweis sämtlicher denkbarer frei eingebbarer Zahlenwerte, Live-Gates oder Exchange-Fills. Live-Block-Freigaben/Pausen, DCA-Koordination, echte Funding-/Liquidations-/Orderbuchausführung sind nicht durch OHLCV rekonstruierbar und werden über Funktionstests separat bewertet. Positive/qualifizierte Ergebnisse sind explorativ; mindestens acht Abschlüsse und PF &gt; 1,02 in beiden chronologischen Abschnitten bedeutet keine unabhängige Handelsfreigabe.</p></section>''')
        f.write('<section><h2>Remote, Settings, Aktivität und Funktionsabnahme</h2><pre>'+html.escape(json.dumps(audit,ensure_ascii=False,indent=2))+'</pre></section>')
        f.write('<section><h2>Vollständigkeitsbilanz</h2><table>'+table+'</table></section>')
        f.write('''<section><h2>Alle Einzelresultate</h2><p>Zur Begrenzung des Speicherbedarfs wird nur die gewählte Gruppe entpackt. CSV exportiert alle gefilterten Zeilen dieser Gruppe; kein Top-N-Limit.</p><select id="group"></select><button id="load">Gruppe öffnen</button><select id="strategy"><option value="">Alle Strategien</option><option>base</option><option>trail</option><option>block</option><option>dca</option></select><select id="status"><option value="">Alle Ergebnisse</option><option value="positive">Netto positiv</option><option value="qualified">PF/Samples beide Abschnitte</option><option value="negative">Netto ≤ 0</option></select><select id="sort"><option value="netPct">Netto absteigend</option><option value="maxDrawdownPct">Drawdown aufsteigend</option><option value="n">Trades absteigend</option></select><button id="prev">Zurück</button><button id="next">Weiter</button><button id="csv">CSV</button><p id="count">Gruppe wählen.</p><table><thead><tr><th>Details</th><th>Seite</th><th>Strategie</th><th>Schritt/Stufen</th><th>TP/SL %</th><th>Trailing</th><th>N</th><th>PF</th><th>Netto pp</th><th>Kosten pp</th><th>DD pp</th><th>DDT max h</th></tr></thead><tbody id="rows"></tbody></table></section><section><h2>Alle Parameter und Kennzahlen / Tagesergebnisse</h2><pre id="details"></pre></section>''')
        f.write('<section><h2>Reproduktion und Datenherkunft</h2><p>Report-Code-Signatur: '+signature+'. Die jeweilige Berechnungs-Signatur steht im Feld signature jeder Gruppe; reine Änderungen der Berichtsdarstellung berechnen die Ergebnisse nicht neu.</p><pre>'+html.escape(json.dumps(summary,ensure_ascii=False,indent=1))+'</pre></section>')
        f.write('<script type="application/json" id="grids">'+grids_json+'</script>')
        f.write('<script type="application/json" id="summaries">'+json.dumps(summary,separators=(',',':')).replace('<','\\u003c')+'</script>')
        for s in summary:
            f.write('<script type="application/octet-stream" id="data-'+s['key']+'">'+base64.b64encode((out/(s['key']+'.embedded.gz')).read_bytes()).decode()+'</script>')
        f.write('''<script>
const el=id=>document.getElementById(id), G=JSON.parse(el('grids').textContent), S=JSON.parse(el('summaries').textContent);let all=[],shown=[],page=0,current=null;
for(const s of S){const o=document.createElement('option');o.value=s.key;o.textContent=`${s.window} · ${s.symbol} · ${s.kind} · ${s.rows} Zeilen`;el('group').append(o)}
function view(){shown=all.filter(r=>(!el('strategy').value||G[current.family][r.config].strategy===el('strategy').value)&&(!el('status').value||(el('status').value==='negative'?r.netPct<=0:r[el('status').value])));const sort=el('sort').value;shown.sort((a,b)=>sort==='maxDrawdownPct'?a[sort]-b[sort]:b[sort]-a[sort]);page=Math.max(0,Math.min(page,Math.ceil(shown.length/50)-1));el('rows').replaceChildren();el('count').textContent=`${shown.length} / ${all.length} Zeilen · Seite ${page+1} / ${Math.max(1,Math.ceil(shown.length/50))}`;for(const r of shown.slice(page*50,page*50+50)){const c=G[current.family][r.config],tr=document.createElement('tr'),td=document.createElement('td'),b=document.createElement('button');b.textContent='Öffnen';b.onclick=()=>el('details').textContent=JSON.stringify({window:current.window,symbol:current.symbol,type:current.kind,parameters:c,metrics:r},null,2);td.append(b);tr.append(td);for(const v of [r.direction,c.strategy,c.step??c.levels,`${c.tpPct}/${c.slPct}`,c.trailArmPct?`${c.trailArmPct}:${c.trailGivePct}`:'—',r.n,r.pf??'—',r.netPct,r.costPct,r.maxDrawdownPct,(r.maxDdS/3600).toFixed(2)]){const d=document.createElement('td');d.textContent=v;tr.append(d)}el('rows').append(tr)}}
el('load').onclick=async()=>{try{el('count').textContent='Entpacken …';current=S.find(s=>s.key===el('group').value);const raw=Uint8Array.from(atob(el('data-'+current.key).textContent.trim()),c=>c.charCodeAt(0));const text=await new Response(new Blob([raw]).stream().pipeThrough(new DecompressionStream('gzip'))).text();const d=JSON.parse(text);const metrics=d.metrics.map(a=>Object.fromEntries(d.metricColumns.map((k,i)=>[k,a[i]])));all=d.rows.map(([config,mid])=>({config,...metrics[mid]}));page=0;view()}catch(e){el('count').textContent='Öffnen fehlgeschlagen: '+e.message}};
for(const id of ['strategy','status','sort'])el(id).onchange=()=>{page=0;if(current)view()};el('prev').onclick=()=>{page--;if(current)view()};el('next').onclick=()=>{page++;if(current)view()};el('csv').onclick=()=>{if(!current)return;const data=shown.map(r=>({window:current.window,symbol:current.symbol,type:current.kind,...G[current.family][r.config],...r}));if(!data.length)return;const cols=Object.keys(data[0]),quote=v=>'"'+String(typeof v==='object'?JSON.stringify(v):v??'').replaceAll('"','""')+'"';const csv=[cols.map(quote).join(','),...data.map(r=>cols.map(k=>quote(r[k])).join(','))].join('\\r\\n');const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=current.key+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
</script></main></html>''')

def main():
    p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--output',required=True)
    p.add_argument('--workers',type=int,default=2);p.add_argument('--report-only',action='store_true')
    p.add_argument('--windows',nargs='+',choices=WINDOWS,default=list(WINDOWS));a=p.parse_args()
    out=pathlib.Path(a.output);out.mkdir(parents=True,exist_ok=True)
    source_files=['scripts/replay_complete.py','scripts/replay_five_days.py','server/pulse/set_engine.py','server/pulse/indication_engine.py']
    signature=hashlib.sha256(b''.join((ROOT/f).read_bytes() for f in source_files)).hexdigest()
    if a.report_only:
        summaries=[json.loads(p.read_text()) for p in out.glob('*.summary.json')]
    else:
        paths=[]
        for symbol in ('XRP-USDT','BCH-USDT','SOL-USDT'):
            paths.append(prepare(pathlib.Path(a.data)/(symbol+'.json'),out))
            print('Prepared '+symbol,flush=True)
        tasks=[(p,w,k,str(out),signature) for w in a.windows for p in paths for k in (*IND_KINDS,'pack:general','pack:indications')]
        summaries=[]
        with ProcessPoolExecutor(max_workers=max(1,min(a.workers,2))) as pool:
            futures=[pool.submit(job,t) for t in tasks]
            for future in as_completed(futures):summaries.append(future.result())
    make_report(out,summaries,signature)
    print(json.dumps({'completedGroups':len(summaries),'rows':sum(s['rows'] for s in summaries),'html':str(out/'cts-g-complete-validation-20260906.html')}))

if __name__=='__main__':main()
