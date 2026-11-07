#!/usr/bin/env python3
"""Render every completed Signal scenario without pooling separate portfolios."""
import argparse
from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--study', type=Path, required=True)
    ap.add_argument('--oracle', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    plan = json.loads((args.study/'plan.json').read_text())
    summaries = json.loads((args.study/'summary.json').read_text())
    if {x['source'] for x in summaries} != set(plan['sources']):
        raise ValueError('Incomplete source study')
    hourly, groups, trades = [], [], []
    cash_error = 0
    for item in summaries:
        archive = (args.study/item['file']).read_bytes()
        if hashlib.sha256(archive).hexdigest() != item['sha256']:
            raise ValueError('Changed result archive')
        r = json.loads(gzip.decompress(archive))
        assert r['counts']['opened'] == r['counts']['settled'] + r['counts']['open']
        assert r['counts']['settled'] == len(r['trades'])
        assert len(r['hourly']['hours']) == (r['window']['end']-r['window']['tradeStart'])//3600000
        assert sum(h['settledOrders'] for h in r['hourly']['hours']) == r['counts']['settled']
        f = r['config']['progress']['fees']
        for t in r['trades']:
            wanted = t['side']*(t['exit']-t['entry'])*t['qty'] - t['entry']*t['qty']*f[f['entryLiquidity']] - t['exit']*t['qty']*f[f['exitLiquidity']]
            cash_error = max(cash_error, abs(wanted-t['netCash']))
            assert abs(wanted-t['netCash']) < 1e-10
            assert abs(t['netCash']/(t['qty']*t['entry'])-t['unitNet']) < 1e-12
            trades.append({'source':item['source'], **t})
        for h in r['hourly']['hours']:
            hourly.append({'source':item['source'], **h})
        by = defaultdict(lambda: dict(streams=0, opened=0, held=0, closed=0, gain=0., loss=0., passed=0, chain=0))
        for rec in r['final']['records']:
            p, ident = rec['protection'], rec['identity']
            key = (rec['stage'], p['kind'], p['strategyRange'], ident['tf'], ident['side'])
            a = by[key]; c = rec['cumulative']
            a['streams'] += 1; a['opened'] += rec['opened']; a['held'] += rec['open']
            a['closed'] += c['n']; a['gain'] += c['gain']; a['loss'] += c['loss']
            a['passed'] += len(rec['passedRules']); a['chain'] += rec['chainPassed']
        for (stage, kind, lane, tf, side), a in sorted(by.items()):
            groups.append(dict(source=item['source'], stage=stage, kind=kind, lane=lane, tf=tf, side=side, **a))
    oracle = json.loads(args.oracle.read_text())
    assert oracle['passed']
    data = dict(summaries=summaries, hourly=hourly, groups=groups, trades=trades, plan=plan,
                oracle=oracle, cashError=cash_error)
    html = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CTS-G Signal progression · 24-hour audit</title><style>
*{box-sizing:border-box}body{margin:0;font:15px/1.55 system-ui,sans-serif;background:#f2f5f7;color:#18333e}header{background:#18333e;color:white;padding:34px max(22px,calc((100vw - 1250px)/2))}h1{font-size:36px;margin:8px 0}h2{margin-top:0;font-size:23px}main{max-width:1300px;margin:auto;padding:22px}section{background:white;border:1px solid #d9e2e7;border-radius:10px;padding:22px;margin-bottom:20px}.notice{padding:16px;background:#fff3df;border-left:4px solid #bd811f}.muted{color:#5b717b}.controls{display:flex;gap:15px;flex-wrap:wrap}label{display:grid;gap:5px;font-size:13px}select{font:inherit;max-width:100%;padding:8px;border:1px solid #acbdc6;border-radius:5px}.scroll{overflow:auto;max-height:620px;margin-top:14px}table{border-collapse:collapse;width:100%;white-space:nowrap;font-size:12px}th,td{padding:9px 11px;border-bottom:1px solid #dce4e8;text-align:left}th{position:sticky;top:0;background:#eaf1f4}tr:hover{background:#f3f9f8}.bad{color:#a0322c}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:15px}.card{padding:16px;border:1px solid #d5e0e4;border-top:3px solid #287c73}.number{font-size:29px;font-weight:700}code{word-break:break-all}nav{display:flex;gap:16px;flex-wrap:wrap}a{color:#27766c}header a{color:#c9ede5}details{margin-top:15px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}@media(max-width:700px){.cards{grid-template-columns:1fr 1fr}main{padding:10px}section{padding:14px}h1{font-size:28px}}
</style><header><small>CTS-G / Independent Signals / Frozen market evidence</small><h1>Signal progression and execution</h1><p>12 symbols · 24h prehistory + 24h trading · 6–7 November 2026 UTC trading window</p><nav><a href="#sources">Sources</a><a href="#hours">Every hour</a><a href="#types">Types and lanes</a><a href="#trades">Executed orders</a><a href="#method">Method and limits</a></nav></header><main>
<section><div class="cards" id="cards"></div><p class="notice"><b>The Signal catalogue has not demonstrated profitable execution.</b> Most scenarios produced no admitted orders; empty PF is unavailable. Each source has a separate simulated capital book. Counts across scenarios describe coverage and cannot be presented as one funded portfolio. No source was promoted to live trading.</p><p>This companion report is separate from the ordinary CTS-G replay, which closed 61 orders at PF 1.835 with marked PF 1.217. These are different engines, cohorts and configurations.</p></section>
<section id="sources"><h2>Final source and coordination snapshots</h2><p class="muted">Passing-window PF describes historical qualified windows. Forward PF describes subsequent funded-model closes. Main qualification can exist while its current Base predecessor fails; the chain column preserves this distinction. Denominators include every configured policy path, even paths never allocated.</p><div class="controls"><label>Source<select id="source"></select></label></div><div id="sourceTable"></div></section>
<section id="hours"><h2>Detailed hourly results</h2><p class="muted">Every hour is retained, including no trades and losses. Net equity includes held-position marks. Each scenario starts with $1,000, $10/order and a cap of eight open orders.</p><div id="hourTable"></div></section>
<section id="types"><h2>Strategy types, lanes, timeframe and side</h2><p class="muted">Historical stream diagnostics include both prehistory and trading (48h). Base/Main/Real are separate simulated streams; overlapping policy paths reuse observations. These totals are not funded execution or venue PF.</p><div class="controls"><label>Stage<select id="stage"><option value="">All</option><option>base</option><option>main</option><option>real</option></select></label><label>Type<select id="kind"><option value="">All</option><option>normal</option><option>trailing</option><option>trailing-only</option></select></label><label>Strategy lane<select id="lane"><option value="">All</option><option>signals</option><option>exit</option></select></label></div><div id="typeTable"></div></section>
<section id="trades"><h2>All funded-model settled orders</h2><div id="tradeTable"></div><details><summary>Complete admission evidence for each close</summary><pre id="admissions"></pre></details></section>
<section id="method"><h2>Reproduction and constraints</h2><p>103 pinned CTS-A-O source families and 15 dominating coordinations; complete UTC 1/5/15/30-minute bars; short/medium source variants; Normal, Trailing and Trailing-only through separate Signals and Exit lanes. Twelve protection profiles, both directions, all 42 Base × 48 Main policies. Real uses the explicitly configured current Main proof, not a second closed-position gate.</p><p>Historical PF floor 2.2 and maximum context return drawdown 4% are unchanged. Research history is explicitly 24h to match the 48h dataset. Costs use maker 0.02%, taker 0.05%, and slippage 0.005% per leg; both entries and exits are taker estimates. Fees depend on actual modeled fill notionals. No funding, liquidation, liquidity queue or actual exchange fills are modeled.</p><p>The cache now keys qualification by rule values, and restart validation rejects totals that omit retained losses. Regression and sampled scalar checks protect causality and arithmetic. They cannot guarantee future PF. Source generation and these stage replays remain an offline engine; they are not connected to the ordinary Pulse live order adapter.</p><p id="proof"></p><details><summary>Frozen configuration and source hashes</summary><pre id="config"></pre></details></section>
<section><h2>Independent repository repairs</h2><p><a href="https://github.com/mxssnx-creator/CTS-A-O/pull/247">CTS-A-O #247</a>: delayed Signal settlements no longer hide recent losses; 29 bounded tests, build and typecheck pass. No new market profitability claim; the persistent stage adapter remains pending.</p><p><a href="https://github.com/mxssnx-creator/CTS-A/pull/11">CTS-A #11</a>: parent/Block closes retain actual losses; remove combined-window PF bonuses; preserve tiny and unknown evidence. Five settlement regressions fail on original main and pass after repair. Old preset figures require revalidation. No live deployment was performed.</p></section></main>
<script id="data" type="application/json">__DATA__</script><script>
const D=JSON.parse(document.getElementById('data').textContent),$=id=>document.getElementById(id);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(x,n=3)=>Number.isFinite(x)?x.toLocaleString('en-US',{maximumFractionDigits:n}):'unavailable';
const pf=x=>x.pfState==='no-loss'?'no loss':x.pfState==='empty'?'empty':x.pfState==='flat'?'flat (1)':num(x.pf);
const time=t=>new Date(t).toISOString().replace('T',' ').replace('.000Z',' UTC');
const table=(id,headers,rows)=>$(id).innerHTML='<div class="scroll"><table><thead><tr>'+headers.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(c=>'<td>'+esc(c)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';
const total=D.summaries.reduce((a,r)=>a+r.counts.settled,0),active=D.summaries.filter(r=>r.counts.settled).length;
$('cards').innerHTML=[['Source scenarios',D.summaries.length],['With settled orders',active],['Settled across scenarios',total],['Profitable funded scenarios',D.summaries.filter(r=>r.counts.settled&&r.execution.net>0).length]].map(([label,value])=>'<div class="card"><div class="number">'+value+'</div>'+label+'</div>').join('');
$('source').innerHTML='<option value="">All sources</option>'+D.summaries.map(r=>'<option>'+esc(r.source)+'</option>').join('');
function draw(){
const source=$('source').value,keep=r=>!source||r.source===source;
table('sourceTable',['Source','Base pass / all','Base %','Historical passing PF','Main pass / all','Main chain pass','Real chain pass','Opened','Closed','Held','Forward PF','Net equity $','Max DD %','p95 step ms'],D.summaries.filter(keep).map(r=>{const s=r.final.stages;return[r.source,s.base.passed+' / '+s.base.population,num(s.base.validatedPct,5),s.base.passingWindowState==='no-loss'?'no loss':num(s.base.passingWindowPf),s.main.passed+' / '+s.main.population,s.main.chainPassed,s.real.chainPassed,r.counts.opened,r.counts.settled,r.counts.open,pf(r.execution),num(r.netEquity,6),num(r.drawdown*100,6),num(r.stepPerformance.p95Ms)]}));
table('hourTable',['Source','Hour UTC','Closed orders','Return PF','Settled net $','Net equity $','Hour state'],D.hourly.filter(keep).map(h=>[h.source,time(h.from),h.settledOrders,pf(h.settledReturns),num(h.settledNetCash,6),num(h.netEquity,6),h.state]));
const rows=D.groups.filter(keep).filter(r=>(!$('stage').value||r.stage===$('stage').value)&&(!$('kind').value||r.kind===$('kind').value)&&(!$('lane').value||r.lane===$('lane').value));
table('typeTable',['Source','Stage','Type','Lane','TF min','Side','Allocated streams','Passing rules','Chain pass','Opened','Closed','Held','History PF'],rows.map(r=>[r.source,r.stage,r.kind,r.lane,r.tf,r.side===1?'long':'short',r.streams,r.passed,r.chain,r.opened,r.closed,r.held,r.loss?num(r.gain/r.loss):r.gain?'no loss':r.closed?'flat (1)':'empty']));
const trades=D.trades.filter(keep);table('tradeTable',['Source','Symbol','Side','Type / lane','Entry UTC','Exit UTC','Entry','Exit','Qty','Net $','Net return %','Reason','Base / Main'],trades.map(t=>[t.source,t.identity.symbol,t.side===1?'long':'short',t.protection.kind+' / '+t.protection.strategyRange,time(t.entryT),time(t.exitT),num(t.entry,8),num(t.exit,8),num(t.qty,8),num(t.netCash,8),num(t.unitNet*100,6),t.reason,t.baseRule+' / '+t.mainRule]));
$('admissions').textContent=JSON.stringify(trades.map(t=>({context:t.context,admission:t.admission})),null,2);
}
for(const id of ['source','stage','kind','lane'])$(id).addEventListener('change',draw);draw();
$('proof').textContent=`Independent scalar reconstruction passed ${D.oracle.roots} sampled roots and ${D.oracle.paths} policy paths. All scenario/hour/order counts and every settled cash/return calculation reconcile; maximum cash difference ${D.cashError}. These checks do not establish live readiness. Browser rendering could not be inspected in this environment; local HTML/data and JavaScript checks were performed.`;
$('config').textContent=JSON.stringify(D.plan,null,2);
</script></html>'''
    payload = json.dumps(data, separators=(',', ':'), allow_nan=False).replace('<', '\\u003c')
    args.out.write_text(html.replace('__DATA__', payload))
    args.out.with_suffix('.client.js').write_text(html.split('<script>')[-1].split('</script>')[0])
    print(json.dumps({'file':str(args.out),'scenarios':len(summaries),'hours':len(hourly),'groups':len(groups),'trades':len(trades),'maxCashError':cash_error}))


if __name__ == '__main__':
    main()
