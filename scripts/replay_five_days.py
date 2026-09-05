"""Bounded research replay: real CTS-G indications, isolated hypothetical lanes.

No exchange client, settings writes, synthetic fallback, or live orders.
This is a causal candle model, not a reproduction of live order-book fills.
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import pathlib
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server' / 'pulse'))
from set_engine import IND_KINDS, IND_TAG_KIND, indication_kind_votes
from block_engine import calculate_block_max_additional_ratio
from fetch_historic_window import validate


def configs():
    variants = [('base', 0, 0., 0.)]
    variants += [('block', n, .2, .25) for n in range(1, 7)]
    variants += [('dca', n, step, .25) for n in (1, 2, 3) for step in (.05, .10, .20)]
    return [dict(strategy=s, levels=n, incrementPct=d, volumeRatio=r, tpPct=tp/100, slPct=sl/100)
            for s, n, d, r in variants for tp in range(40, 81, 5) for sl in range(10, 51, 5)]


def pf(gain, loss):
    return round(gain/loss, 6) if loss > 1e-15 else ('∞' if gain > 0 else None)


def replay(bars, signals, side, cfg, warmup=60, cost_pct=.15):
    """Parallel independent configs. Only completed bar information is used.

    Stops/targets are tested before close-price additions. Same-bar ambiguity
    chooses the stop; gaps fill at worse open. No exit-bar re-entry. Boundary
    positions close with costs, so neither split nor terminal losses vanish.
    PnL is normalized to original parent notional, not leveraged account equity.
    """
    count = len(cfg)
    zeros = lambda: np.zeros(count, dtype=float)
    entry, original, qty, added, entered, fees = [zeros() for _ in range(6)]
    gp, gl, net, costs, wins, losses, closes, hold, peak, dd = [zeros() for _ in range(10)]
    dd_age, dd_max, dd_total, dd_episodes = [zeros() for _ in range(4)]
    max_qty, adds_total, forced = [zeros() for _ in range(3)]
    train_gp, train_gl, train_n, train_dd = [zeros() for _ in range(4)]
    test_gp, test_gl, test_n = [zeros() for _ in range(3)]
    daily_net = np.zeros((count, (len(bars)-warmup+1439)//1440))
    daily_n = np.zeros_like(daily_net)
    recent = np.zeros((count, 75))
    tp = np.array([c['tpPct']/100 for c in cfg])
    sl = np.array([c['slPct']/100 for c in cfg])
    levels = np.array([c['levels'] for c in cfg])
    increments = np.array([c['incrementPct']/100 for c in cfg])
    ratios = np.array([c['volumeRatio'] for c in cfg])
    is_dca = np.array([c['strategy'] == 'dca' for c in cfg])
    is_block = np.array([c['strategy'] == 'block' for c in cfg])
    block_qty = np.array([1+calculate_block_max_additional_ratio(c['levels'], c['volumeRatio'], 2.)
                          if c['strategy']=='block' else 1 for c in cfg])
    fee = cost_pct/200
    split = warmup + int((len(bars)-warmup)*.7)
    for i in range(warmup, len(bars)):
        op, hi, lo, price = bars[i][:4]
        active = qty > 0
        stop = entry * (1-side*sl)
        target = entry * (1+side*tp)
        hit_sl = active & ((lo <= stop) if side == 1 else (hi >= stop))
        hit_tp = active & ((hi >= target) if side == 1 else (lo <= target))
        boundary = i in (split-1, len(bars)-1)
        exiting = active & (hit_sl | hit_tp | boundary)
        ids = np.flatnonzero(exiting)
        if ids.size:
            px = np.where(hit_sl, np.minimum(op, stop) if side==1 else np.maximum(op, stop),
                          np.where(hit_tp, target, price))[ids]
            cost = (fees[ids] + qty[ids]*px*fee)/original[ids]
            pnl = side*qty[ids]*(px-entry[ids])/original[ids] - cost
            gains, loss = np.maximum(pnl, 0), np.maximum(-pnl, 0)
            gp[ids] += gains; gl[ids] += loss; net[ids] += pnl; costs[ids] += cost
            recent[ids, closes[ids].astype(int)%75] = pnl
            wins[ids] += pnl > 0; losses[ids] += pnl < 0; closes[ids] += 1
            hold[ids] += (i-entered[ids])*60
            day = (i-warmup)//1440
            daily_net[ids, day] += pnl; daily_n[ids, day] += 1
            forced[ids] += ~(hit_sl[ids] | hit_tp[ids])
            if i < split:
                train_gp[ids] += gains; train_gl[ids] += loss; train_n[ids] += 1
            else:
                test_gp[ids] += gains; test_gl[ids] += loss; test_n[ids] += 1
            qty[ids] = 0; fees[ids] = 0
        # Add only at candle close AFTER surviving its existing protection.
        active = qty > 0
        safe_entry = np.where(active, entry, 1.)
        adverse = side*(entry-price)/safe_entry
        dca = active & is_dca & (added < levels) & (adverse >= increments*(added+1))
        block = active & is_block & (added == 0) & (-adverse >= increments)
        adding = dca | block
        ids = np.flatnonzero(adding)
        if ids.size and not boundary:
            extra = np.where(dca, ratios, np.maximum(0, block_qty-qty))[ids]
            entry[ids] = (entry[ids]*qty[ids]+price*extra)/(qty[ids]+extra)
            qty[ids] += extra; fees[ids] += price*extra*fee
            added[ids] += 1; adds_total[ids] += 1
        direction, confidence = signals[i]
        if not boundary and direction == side and confidence >= .58:
            opening = (qty == 0) & ~exiting
            entry[opening] = price; original[opening] = price; qty[opening] = 1
            fees[opening] = price*fee; added[opening] = 0; entered[opening] = i
        max_qty = np.maximum(max_qty, qty)
        # Close-mark liquidation equity includes accrued + estimated exit costs.
        active = qty > 0
        equity = net.copy()
        equity[active] += (side*qty[active]*(price-entry[active])-fees[active]-qty[active]*price*fee)/original[active]
        peak = np.maximum(peak, equity)
        dd = np.maximum(dd, peak-equity)
        underwater = equity < peak-1e-12
        dd_episodes += underwater & (dd_age == 0)
        dd_age = np.where(underwater, dd_age+60, 0)
        dd_total += underwater*60; dd_max = np.maximum(dd_max, dd_age)
        if i == split-1:
            train_dd = dd.copy()
    hours = (len(bars)-warmup)/60
    rows = []
    for k in range(count):
        n = int(closes[k])
        recent_metrics = []
        for window in (8, 25, 75):
            available = min(n, window)
            tape = [recent[k, (n-1-j)%75] for j in range(available)]
            recent_metrics.append(dict(n=available, requested=window,
                                      pf=pf(sum(max(0,x) for x in tape), sum(max(0,-x) for x in tape))))
        train_pf = pf(train_gp[k], train_gl[k]); test_pf = pf(test_gp[k], test_gl[k])
        passes = lambda p: p == '∞' or isinstance(p, (float, int)) and p > 1.02
        rows.append(dict(config=k, n=n, wins=int(wins[k]), losses=int(losses[k]),
            pf=pf(gp[k], gl[k]), netPct=round(net[k]*100, 6), costPct=round(costs[k]*100, 6),
            grossProfitPct=round(gp[k]*100,6), grossLossPct=round(gl[k]*100,6),
            winRate=round(wins[k]/n*100, 4) if n else 0,
            maxDrawdownPct=round(dd[k]*100,6), maxDdS=int(dd_max[k]),
            avgDdS=round(dd_total[k]/dd_episodes[k],2) if dd_episodes[k] else 0,
            ddEpisodes=int(dd_episodes[k]), tradesPerHour=round(n/hours,6),
            avgHoldS=round(hold[k]/n,2) if n else 0, maxVolume=float(max_qty[k]),
            additions=int(adds_total[k]), boundaryCloses=int(forced[k]),
            trainN=int(train_n[k]), trainPf=train_pf, trainDdPct=round(train_dd[k]*100,6),
            holdoutN=int(test_n[k]), holdoutPf=test_pf,
            holdoutNetPct=round((test_gp[k]-test_gl[k])*100,6), recentPf=recent_metrics,
            positive=bool(net[k]>0), qualified=bool(train_n[k]>=8 and test_n[k]>=8 and passes(train_pf) and passes(test_pf)),
            dailyNetPct=np.round(daily_net[k]*100,6).tolist(), dailyN=daily_n[k].astype(int).tolist()))
    return rows


def one_symbol(path):
    blob = json.loads(pathlib.Path(path).read_text())
    validate(blob['rows'], blob['start']-3600000, blob['end'])
    bars = [r[1] for r in blob['rows']]
    signals = {kind: [(0,0.)]*len(bars) for kind in IND_KINDS}
    settings = {f'type{k.title()}': True for k in IND_KINDS}
    for i in range(60, len(bars)):
        # Project evaluators receive just the last 60 completed 1m candles.
        for direction, confidence, tag in indication_kind_votes(bars[i-59:i+1], settings, blob['rows'][i][0]/1000):
            kind = IND_TAG_KIND.get(tag)
            if kind:
                signals[kind][i] = (direction, confidence)
    rows = []
    cfg = configs()
    for kind in IND_KINDS:
        for side in (1,-1):
            rows.extend(dict(symbol=blob['symbol'], indication=kind, direction='LONG' if side==1 else 'SHORT', **row)
                        for row in replay(bars, signals[kind], side, cfg))
        print(json.dumps(dict(symbol=blob['symbol'], indication=kind, completed=len(rows))), flush=True)
    return dict(symbol=blob['symbol'], rows=rows, source=blob['source'], start=blob['start'], end=blob['end'],
                bars=len(bars), fetchedAt=blob['fetchedAt'],
                signals={kind: {label: sum(d==side and c>=.58 for d,c in signals[kind])
                               for side,label in ((1,'LONG'),(-1,'SHORT'))} for kind in IND_KINDS},
                sha256=hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest())


def report(results, elapsed):
    cfg = configs()
    rows = [r for result in results for r in result['rows']]
    # Exploratory ranking only; the 30% chronological check is disclosed and
    # never presented as independent proof after searching the whole matrix.
    winners = []
    for symbol in ('XRP-USDT','BCH-USDT','SOL-USDT'):
        for kind in IND_KINDS:
            eligible = [r for r in rows if r['symbol']==symbol and r['indication']==kind and r['qualified']]
            winners += sorted(eligible, key=lambda r:(r['maxDrawdownPct'], -r['tradesPerHour'], -r['netPct']))[:5]
    code_files = ['scripts/replay_five_days.py','scripts/fetch_historic_window.py',
                  'server/pulse/set_engine.py','server/pulse/indication_engine.py','server/pulse/block_engine.py']
    payload = dict(configs=cfg, rows=rows, best=winners, elapsedS=elapsed,
                   code={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in code_files},
                   baseCommit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                   sources=[{k:v for k,v in r.items() if k!='rows'} for r in results])
    # Array encoding keeps the complete 62,208-lane file modest in size.
    columns = list(rows[0]) if rows else []
    packed = dict(payload, columns=columns, rows=[[r[k] for k in columns] for r in rows])
    data = json.dumps(packed, separators=(',', ':'), allow_nan=False).replace('<','\\u003c')
    dates = [dt.datetime.fromtimestamp(results[0][k]/1000,dt.timezone.utc).isoformat() for k in ('start','end')]
    summary = []
    for result in results:
        rr = result['rows']; best = min((r for r in rr if r['qualified']), key=lambda r:(r['maxDrawdownPct'],-r['netPct']), default=None)
        measured = [r for r in rr if r['trainN']>=8 and r['holdoutN']>=8]
        observed = max(measured,key=lambda r: (float('inf') if r['pf']=='∞' else r['pf'] or 0, r['netPct']),default=None)
        summary.append(dict(symbol=result['symbol'], configs=len(rr), positive=sum(r['positive'] for r in rr),
                            noTrades=sum(r['n']==0 for r in rr), qualified=sum(r['qualified'] for r in rr),
                            best=best, highestObservedPf=observed, parameters=cfg[observed['config']] if observed else None))
    heads = ''.join(f'<th>{html.escape(x)}</th>' for x in ('Symbol','Geprüft','Ohne Trades','Netto positiv','PF/Samples beide Teilfenster'))
    body = ''.join('<tr>'+''.join(f'<td>{s[k]}</td>' for k in ('symbol','configs','noTrades','positive','qualified'))+'</tr>' for s in summary)
    return r'''<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CTS-G · Fünf Tage historischer Konfigurationstest</title><style>
body{background:#0b1421;color:#e5edf7;font:15px/1.55 system-ui;margin:0}main{max-width:1450px;margin:auto;padding:30px}h1{font-size:34px}h2{margin-top:32px}p{max-width:1080px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:9px;text-align:left;border-bottom:1px solid #34445b;white-space:nowrap}th{color:#9eb9d9}button,select,input{padding:8px;margin:4px;background:#20334d;color:white;border:1px solid #577190;border-radius:5px}button{cursor:pointer}section{overflow:auto;background:#111f31;padding:18px;margin-top:18px;border-radius:10px}.note{border-left:4px solid #eac36c;padding:14px;background:#292b27}pre{white-space:pre-wrap;font-size:13px}a{color:#81d2fa}.muted{color:#a6b6c9}</style>
<main><p class="muted">CTS-G · Research-Replay · keine Exchange-Ausführungen</p>
<h1>Fünf Tage · XRP / BCH / SOL</h1><p>WINDOW</p>
<p class="note">Echte öffentliche BingX-1m-Kerzen; alle Zeitstempel lückenlos geprüft. Jede Konfiguration ist ein unabhängiges hypothetisches Konto. Summen über alternative Konfigurationen sind kein Portfolioergebnis. Positive Backtests sind keine Profitgarantie; keine Live-Defaults wurden übernommen.</p>
<section><h2>Abdeckung und Ergebnisse</h2><table><tr>HEADS</tr>BODY</table><p id="coverage"></p></section>
<section><h2>Stärkster beobachteter PF je Symbol</h2><p>Mindestens acht Abschlüsse in beiden Abschnitten. Auch negative Ergebnisse bleiben sichtbar; dies ist keine Empfehlung.</p><pre>OBSERVED</pre></section>
<section><h2>Methode und genaue Aussagegrenzen</h2><p>Je Symbol: 8 Projekt-Indikationen × LONG/SHORT × 81 TP/SL-Paare × 16 Varianten. TP 0,40–0,80 %, SL 0,10–0,50 %, jeweils Schritt 0,05. Varianten: Base; Block-Zähler 1–6 einzeln, Ratio 0,25, Auslösung bei +0,20 % und additives Gesamtziel höchstens 2×; DCA 1/2/3 Stufen mit Inkrement 0,05/0,10/0,20 %, Additionsmenge jeweils 0,25 der Originalmenge. DCA-Distanzen steigen je Stufe und beziehen sich auf den aktuellen Durchschnittseinstieg.</p>
<p>Signal bei Kerzenschluss; erste Ausstiegsmöglichkeit in der Folgekerze. Bei TP/SL-Berührung in derselben Kerze gilt SL; Gaps durch SL zum schlechteren Open. Vor einer Addition werden vorhandene Stops/TP geprüft. Kein Wiedereinstieg in der Ausstiegskerze. Close-basierte Addition; Stops werden danach vom neuen Durchschnittspreis berechnet. 60 Vorlaufkerzen zählen nicht zur Testperiode. An der chronologischen 70/30-Grenze sowie am Periodenende werden Restpositionen zum Close mit Kosten geschlossen.</p>
<p>Kostenmodell: 0,075 % pro ausgeführtem Entry/Add/Exit-Notional; insgesamt ungefähr 0,15 % je einfacher Roundtrip. Tatsächliche kontospezifische Gebühren, Funding, Orderbuch-Slippage, Latenz und Liquidation sind nicht rekonstruiert. PF = Summe positiver Nettoabschlüsse / Betrag negativer Nettoabschlüsse. ∞ bedeutet Gewinne ohne Verlust, — keine Aussage. PF8/25/75 zeigen die tatsächliche Stichprobengröße.</p>
<p>Netto, Gebühren und Drawdown werden in Prozentpunkten des ursprünglichen Parent-Notionals ausgewiesen, nicht als gehebelte Kontorendite. Drawdown und DDT basieren auf minütlicher Close-Liquidationsequity einschließlich geschätzter Exitkosten; Intraminuten-Equity-Minima werden damit nicht gemessen. DDT-Episoden enthalten die am Periodenende offenen Episoden. Der Volumenwert 1 ist die Originalmenge.</p>
<p>Die Varianten untersuchen Preis-/Volumenmechanik mit CTS-G-Indikationsfunktionen. Sie bilden weder Live-Block-Freigaben/Pausen bis positivem Abschluss noch Exchange-Koordination und vollständige Runtime-Gates nach. Der 30%-Abschnitt ist ein chronologischer Kontrollabschnitt; die gezeigte Bestenauswahl ist explorativ nach dem Gesamtlauf, keine unabhängige Out-of-sample-Freigabe. „Qualifiziert“ heißt lediglich mindestens 8 Abschlüsse und PF &gt; 1,02 in beiden Abschnitten; kein zusätzlicher Drawdown-Grenzwert.</p></section>
<section><h2>Alle Konfigurationen · eine Zeile pro Kombination</h2>
<select id="symbol"><option value="">Alle Symbole</option><option>XRP-USDT</option><option>BCH-USDT</option><option>SOL-USDT</option></select>
<select id="kind"><option value="">Alle Indikationen</option></select><select id="strategy"><option value="">Alle Varianten</option><option>base</option><option>dca</option><option>block</option></select>
<select id="status"><option value="">Alle Ergebnisse</option><option value="positive">Netto positiv</option><option value="qualified">PF/Samples beide Abschnitte</option><option value="negative">Netto ≤ 0</option></select>
<select id="sort"><option value="maxDrawdownPct">Drawdown aufsteigend</option><option value="netPct">Netto absteigend</option><option value="tradesPerHour">Trades/h absteigend</option></select>
<button id="prev">Zurück</button><button id="next">Weiter</button><button id="csv">Alle gefilterten Zeilen als CSV</button><p id="count"></p>
<table><thead><tr><th>Details</th><th>Symbol / Typ / Seite</th><th>Variante / Stufen / Inkrement</th><th>TP / SL %</th><th>N / Win%</th><th>PF</th><th>Netto pp</th><th>DD pp</th><th>DDT Ø / max h</th><th>Trades/h</th><th>Train PF / N</th><th>Kontroll-PF / N</th><th>Vol max</th></tr></thead><tbody id="rows"></tbody></table></section>
<section><h2>Konfigurationsdetails</h2><p>Eine Zeile öffnen: vollständige Metriken, Tagesergebnisse, Kosten, PF8/25/75 und sämtliche Parameter.</p><pre id="details"></pre></section>
<section><h2>Bis zu fünf Kandidaten je Symbol und Indikation</h2><p>Unter den qualifizierten Zeilen: niedriger Drawdown zuerst, dann Trades/h. Keine automatische Aktivierung.</p><pre id="best"></pre></section>
<section><h2>Datenherkunft und Reproduzierbarkeit</h2><pre id="sources"></pre></section>
<noscript>Die Zusammenfassung oben ist ohne JavaScript sichtbar. Vollständige Konfigurationsdaten sind im eingebetteten JSON report-data enthalten.</noscript>
<script type="application/json" id="report-data">DATA</script><script>
const D=JSON.parse(document.getElementById('report-data').textContent), cols=D.columns;
const all=D.rows.map(a=>Object.fromEntries(cols.map((k,i)=>[k,a[i]])));
const el=id=>document.getElementById(id), fmt=x=>x==null?'—':typeof x==='number'?x.toFixed(3):x;
let page=0,filtered=[];const size=100;
for(const k of [...new Set(all.map(r=>r.indication))]){let o=document.createElement('option');o.textContent=k;el('kind').append(o)}
el('sources').textContent=JSON.stringify({sources:D.sources,baseCommit:D.baseCommit,codeSha256:D.code},null,2);
el('coverage').textContent=`${all.length.toLocaleString()} Konfigurationen; ${D.configs.length} Parameterkombinationen je Symbol/Indikation/Seite. Laufzeit ${D.elapsedS.toFixed(1)} s. Jede Minute der 120 h plus 60 Minuten Warmup vorhanden.`;
el('best').textContent=D.best.length?D.best.map(r=>JSON.stringify({...r,parameters:D.configs[r.config]})).join('\n'):'Keine Konfiguration erfüllt beide PF-/Stichprobengrenzen.';
function render(){filtered=all.filter(r=>(!el('symbol').value||r.symbol===el('symbol').value)&&(!el('kind').value||r.indication===el('kind').value)&&(!el('strategy').value||D.configs[r.config].strategy===el('strategy').value)&&(!el('status').value||(el('status').value==='negative'?!r.positive:r[el('status').value])));
let key=el('sort').value;filtered.sort((a,b)=>(key==='maxDrawdownPct'?1:-1)*(a[key]-b[key]));page=Math.min(page,Math.max(0,Math.ceil(filtered.length/size)-1));el('count').textContent=`${filtered.length} Zeilen · Seite ${page+1} / ${Math.max(1,Math.ceil(filtered.length/size))}`;el('rows').replaceChildren();
for(const r of filtered.slice(page*size,(page+1)*size)){const c=D.configs[r.config],tr=document.createElement('tr'),td=document.createElement('td'),btn=document.createElement('button');btn.textContent='#'+r.config;btn.onclick=()=>{el('details').textContent=JSON.stringify({parameters:c,...r},null,2);el('details').scrollIntoView({block:'center'})};td.append(btn);tr.append(td);
for(const v of [`${r.symbol} / ${r.indication} / ${r.direction}`,`${c.strategy} / ${c.levels} / ${c.incrementPct}`,`${c.tpPct} / ${c.slPct}`,`${r.n} / ${fmt(r.winRate)}`,fmt(r.pf),fmt(r.netPct),fmt(r.maxDrawdownPct),`${fmt(r.avgDdS/3600)} / ${fmt(r.maxDdS/3600)}`,fmt(r.tradesPerHour),`${fmt(r.trainPf)} / ${r.trainN}`,`${fmt(r.holdoutPf)} / ${r.holdoutN}`,fmt(r.maxVolume)]){const td=document.createElement('td');td.textContent=v;tr.append(td)}el('rows').append(tr)}el('prev').disabled=page===0;el('next').disabled=(page+1)*size>=filtered.length;}
for(const id of ['symbol','kind','strategy','status','sort'])el(id).onchange=()=>{page=0;render()};el('prev').onclick=()=>{page--;render()};el('next').onclick=()=>{page++;render()};
el('csv').onclick=()=>{const cc=[...Object.keys(D.configs[0]),...cols],q=x=>'"'+String(typeof x==='object'?JSON.stringify(x):x??'').replaceAll('"','""')+'"';const text=[cc.map(q).join(','),...filtered.map(r=>{const v={...D.configs[r.config],...r};return cc.map(k=>q(v[k])).join(',')})].join('\n');const url=URL.createObjectURL(new Blob([text],{type:'text/csv;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='cts-g-five-days-configs.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};render();</script></main></html>'''.replace('WINDOW', html.escape(' bis '.join(dates)+' (Ende exklusiv)')).replace('HEADS',heads).replace('BODY',body).replace('OBSERVED',html.escape(json.dumps(summary,indent=2))).replace('DATA',data), summary


def main():
    p = argparse.ArgumentParser(); p.add_argument('--data',required=True); p.add_argument('--output',required=True)
    args = p.parse_args(); started=time.monotonic()
    paths = [str(pathlib.Path(args.data)/(s+'-USDT.json')) for s in ('XRP','BCH','SOL')]
    blobs = [json.loads(pathlib.Path(path).read_text()) for path in paths]
    if len({(b['start'],b['end']) for b in blobs}) != 1 or any(b['end']-b['start'] != 5*86400000 for b in blobs):
        raise ValueError('Require the same exact five-day period for every symbol')
    with ProcessPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(one_symbol, paths))
    output = pathlib.Path(args.output); output.parent.mkdir(parents=True,exist_ok=True)
    page, summary = report(results, time.monotonic()-started)
    output.write_text(page)
    output.with_suffix('.summary.json').write_text(json.dumps(summary,indent=2,allow_nan=False))
    print(json.dumps(dict(output=str(output),summary=summary,elapsedS=time.monotonic()-started)),flush=True)


if __name__ == '__main__':
    main()
