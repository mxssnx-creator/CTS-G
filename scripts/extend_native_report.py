#!/usr/bin/env python3
"""Add exact native x02 and fresh-replay evidence to the complete HTML report.

No external assets or network calls. Existing frozen results stay unchanged.
"""
import argparse
import collections
import datetime as dt
import html
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo


def esc(value):return html.escape(str(value))
def number(value,digits=3):
    return '—' if value is None else f'{value:,.{digits}f}'
def stamp(value):
    return dt.datetime.fromtimestamp(float(value),ZoneInfo('Europe/Vienna')).strftime('%d %b %H:%M:%S')if value else'—'
def table(headers,rows):
    return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+esc(h)+'</th>'for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+esc(v)+'</td>'for v in row)+'</tr>'for row in rows)+'</tbody></table></div>'
def panel(title,body):return '<div class="panel"><h3>'+esc(title)+'</h3>'+body+'</div>'
def note(text):return '<p class="tiny">'+esc(text)+'</p>'
def card(label,value,detail):return '<div class="card"><div class="label">'+esc(label)+'</div><div class="value">'+esc(value)+'</div><small>'+esc(detail)+'</small></div>'


def pf(values):
    values=list(values)
    if not values:return None
    gain=sum(max(v,0)for v in values);loss=-sum(min(v,0)for v in values)
    return gain/loss if loss else('∞'if gain else 1.0)
def pftext(value):return value if isinstance(value,str)else number(value)
def execution_pf(row):
    if not row.get('orders'):return '—'
    gain,loss=row.get('exec_gain'),row.get('exec_loss')
    if gain is not None and loss is not None:
        return pftext(gain/loss if loss else('∞'if gain else 1.0))
    if row.get('wins')==row['orders']:return '∞'
    return number(row.get('exec_pf'))


def plot(title,series,labels):
    """Inline SVG with explicit gaps for unavailable values, including 1 point."""
    colors=['#2556c7','#087f70','#a16cd3','#bb3745','#cf8e32']
    finite=[v for _,values in series for v in values if isinstance(v,(float,int))and math.isfinite(v)]
    lo,hi=min([0,*finite]),max([1,*finite]);span=hi-lo
    width,height,left,right,top,bottom=720,250,64,16,16,45
    x=lambda i:left+i*(width-left-right)/max(1,len(labels)-1)
    y=lambda v:height-bottom-(v-lo)*(height-top-bottom)/span
    parts=[f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{esc(title)}">']
    for i in range(5):
        value=lo+span*i/4
        parts.append(f'<line class="axis" x1="{left}" x2="{width-right}" y1="{y(value):.2f}" y2="{y(value):.2f}"/><text x="{left-7}" y="{y(value)+3:.2f}" text-anchor="end">{number(value,2)}</text>')
    for j,(name,values)in enumerate(series):
        path='';active=False
        for i,value in enumerate(values):
            if not isinstance(value,(int,float))or not math.isfinite(value):active=False;continue
            path+=('L'if active else'M')+f'{x(i):.2f},{y(value):.2f}';active=True
        color=colors[j%len(colors)]
        parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.2"/>')
        for i,value in enumerate(values):
            if isinstance(value,(int,float))and math.isfinite(value):
                parts.append(f'<circle cx="{x(i):.2f}" cy="{y(value):.2f}" r="2.1" fill="{color}"><title>{esc(name)} · {esc(labels[i])}: {number(value,6)}</title></circle>')
    every=max(1,math.ceil(len(labels)/7))
    for i,label in enumerate(labels):
        if i%every==0 or i==len(labels)-1:parts.append(f'<text x="{x(i):.2f}" y="{height-17}" text-anchor="middle">{esc(label)}</text>')
    parts.append('</svg><div class="legend">'+''.join(f'<span><i style="background:{colors[j%len(colors)]}"></i>{esc(name)}</span>'for j,(name,_)in enumerate(series))+'</div>')
    return panel(title,''.join(parts))


def captures(run,report):
    seen={}
    for path in sorted(run.glob('*stats.json')):
        if path.name.startswith('warming-'):continue
        raw=json.loads(path.read_text());clock=float(raw.get('now',0))
        if clock<report.get('activeStartedAt',0)-5:continue
        seen[clock]=(path.name,raw)
    return [value for _,value in sorted(seen.items())]


def native_section(run,section_id='native',heading='Native x02 · actual engine observation'):
    report=json.loads((run/'session.json').read_text());snaps=captures(run,report)
    if not snaps:raise ValueError('native report needs active captures')
    final=snaps[-1][1];book=final['sets'];closed={}
    for _,snapshot in snaps:
        for row in snapshot.get('closed',[]):
            if row.get('ours')is not True:continue
            if row['symbol']not in report['symbols']or not str(row.get('client_id','')).lower().startswith(report['tag'].lower()):
                raise ValueError('foreign close in native report')
            closed[(row.get('client_id'),row.get('t'),row.get('qty'))]=row
    audit_path=run/'native-fill-audit.json';audit=json.loads(audit_path.read_text())if audit_path.exists()else{}
    # The venue ledger also retains external exits missing from the old
    # engine's closed-row list. Never compute the displayed PF on a subset.
    closes=sorted(audit.get('financialCloses',list(closed.values())),key=lambda row:row['t'])
    natural=[r for r in closes if r['reason']!='bounded-session-cleanup']
    cleanup=[r for r in closes if r['reason']=='bounded-session-cleanup']
    labels=[stamp(s['now'])[7:15]for _,s in snaps]
    stages=['baseLong','baseShort','mainLong','mainShort','negative','real','realNeg']
    windows=lambda s:s['sets'].get('pfBase',{}).get('windows',{})
    parts=['<section id="'+esc(section_id)+'"><h2>'+esc(heading)+'</h2>',
           '<p class="sub">'+esc(stamp(report.get('activeStartedAt'))+' → '+stamp(report.get('activeEndedAt'))+' Europe/Vienna · '+str(len(report['symbols']))+' selected crypto contracts · separate universe and population from the frozen replay')+'</p>']
    parts.append('<div class="cards">'+''.join([
        card('Native observation',number(report.get('observationSeconds',0)/60,1)+' min',report.get('stopReason','still running')),
        card('Natural close PF',pftext(pf(r['net_pnl_pct']for r in natural)),str(len(natural))+' natural closes; net position returns'),
        card('All native close PF',pftext(pf(r['net_pnl_pct']for r in closes)),str(len(closes))+' closes; includes '+str(len(cleanup))+' cleanup closes'),
        card('Actual own-position cash',number(audit.get('actualFillNet'),6)+' VST','Full exchange ledger: '+('verified'if audit.get('ledgerPassed')else'unverified')+'; engine reconciliation: '+('PASS'if audit.get('passed')else'incomplete')),
        card('Own cleanup','Flat'if audit.get('verifiedFlat')or report.get('cleanup',{}).get('confirmedFlat')else'Unresolved','Original session verification: '+('PASS'if report.get('complete')else'FAILED / incomplete'))])+'</div>')
    parts.append('<div class="notice">'+esc('This bounded observation tests native progress and exchange handling. Small samples and cleanup settlements do not establish a profitable strategy. Fees in actual fills are charged once; market slippage is already in fill prices. All natural and cleanup outcomes are retained.')+'</div>')
    if audit.get('externalOrUnjournaledCloses'):
        parts.append(note(str(audit['externalOrUnjournaledCloses'])+' positions include an external or unjournaled exit. Exact owned entry IDs and venue position IDs establish the financial result. External closing orders remain outside session ownership. These outcomes are included in the PF and cash above.'))
    realized=[sum(float(r['pnl'])for r in closes if r['t']<=s['now'])for _,s in snaps]
    unrealized=[sum(float(p['qty'])*float(p['entry'])*float(p['uPnlPct'])/100 for p in s.get('open',[])if p.get('ours')is True)for _,s in snaps]
    parts.append('<div class="grid">'+plot('Own marked gross and settled venue cash · VST',[('Settled venue cash',realized),('Open gross mark',unrealized)],labels)+plot('Native positions and confirmed protections',[('Own positions',[s['openCount']for _,s in snaps]),('Verified quantity coverage',[sum(bool(p.get('ctrlVerified'))and not p.get('ctrlUndercovered')for p in s.get('open',[]))for _,s in snaps])],labels)+'</div>')
    parts.append('<div class="grid">'+plot('Full requested Last-N coverage · eligible Sets %',[(stage,[windows(s).get(stage,{}).get('samples',{}).get('pct')for _,s in snaps])for stage in('baseLong','mainLong','real','realNeg')],labels)+plot('Measured processing · milliseconds',[('Entry scan',[s.get('scanMs')for _,s in snaps]),('History replay',[s['sets'].get('progress',{}).get('lastRunMs')for _,s in snaps])],labels)+'</div>')
    parts.append(note('Open marks exclude modeled future exit commissions and are not realized cash. Account equity belongs to the shared account and is shown only in the timestamp table. Scan latency and history replay latency are separate measurements.'))
    rows=[]
    for name,s in snaps:
        w=windows(s);rows.append([stamp(s['now']),s['openCount'],len(s.get('closed',[])),number(s.get('equity'),3),s['sets'].get('setCount'),s['sets'].get('counts',{}).get('valid'),*[number(w.get(stage,{}).get('samples',{}).get('pct'),2)for stage in('baseLong','mainLong','real','realNeg')],number(s.get('scanMs'),1),s.get('errors'),name])
    parts.append(panel('Every captured progress point',table(['Vienna time','Own open','Recent close records','Account equity VST (shared)','Sets','Valid Sets','Base full N %','Main full N %','Real full N %','Real Negative full N %','Scan ms','Native errors','Capture'],rows)))
    parts.append(panel('Final eligible stage populations',table(['Window','Configured N','Eligible Sets','Passing Sets','Passing %','Full requested N','Insufficient','Unavailable','Full N %','Paper selected PF'],[[stage,str(row.get('n')),row.get('pop'),row.get('sets'),number(row.get('pct'),2),row.get('samples',{}).get('full'),row.get('samples',{}).get('insufficient'),row.get('samples',{}).get('unavailable'),number(row.get('samples',{}).get('pct'),2),number(row.get('pf'),4)]for stage,row in windows(final).items()])))
    bypack=book.get('pfBase',{}).get('byPack',{})
    parts.append(panel('Every native indication pack',table(['Indication','Configured Sets','Base validated %','Executable %','Post-Base paper PF','Own closes','Return PF','Net VST'],[[pack,row.get('total'),number(row.get('validatedBasePct'),2),number(row.get('validatedPct'),2),number(row.get('baseOnly',{}).get('pf'),4),sum(r.get('pack')==pack for r in closes),pftext(pf(r['net_pnl_pct']for r in closes if r.get('pack')==pack)),number(sum(r['pnl']for r in closes if r.get('pack')==pack),6)]for pack,row in bypack.items()])))
    index=book.get('index',[]);types={row['id']:row.get('kind','')for row in index}
    groups=collections.defaultdict(list)
    for row in closes:groups[(row.get('pack',''),types.get(row.get('set_id'),''),row.get('set_id',''),row.get('lane',''))].append(row)
    parts.append(panel('Each actually executed Set and lane',table(['Indication','Strategy type','Set','Lane','Closes','Natural','Cleanup','Return PF','Net VST','Block adds','DCA adds'],[[*key,len(rows),sum(r['reason']!='bounded-session-cleanup'for r in rows),sum(r['reason']=='bounded-session-cleanup'for r in rows),pftext(pf(r['net_pnl_pct']for r in rows)),number(sum(r['pnl']for r in rows),6),sum(len(r.get('block_fills',[]))for r in rows),sum(len(r.get('dca_fills',[]))for r in rows)]for key,rows in sorted(groups.items())])))
    parts.append('<details><summary>All '+str(len(index))+' native configurations · latest evaluation</summary>'+table(['Set','Indication','Type','Side','Step','SL/TP ratio','Trail','Base N','Base PF','Main observations','Main PF','Real observations','Real PF','Negative Real observations','Negative Real PF','State'],[[r['id'],r['pack'],r['kind'],r.get('sd'),r.get('st'),r.get('sl'),r.get('tr'),r.get('gn'),number(r.get('pf'),4),r.get('mainN'),number(r.get('mainPf'),4),r.get('realN'),number(r.get('realPf'),4),r.get('realNegN'),number(r.get('realNegPf'),4),r.get('why')or'qualified']for r in index])+'</details>')
    parts.append(panel('Every native close · losses and cleanup retained',table(['Vienna exit','Symbol','Side','Set','Lane','Reason','Qty','Entry','Exit','Gross %','Net %','Net VST','Fees VST','Fee evidence'],[[stamp(r['t']),r['symbol'],r['side'],r.get('set_id'),r.get('lane'),r['reason'],number(r['qty'],6),number(r['entry'],8),number(r['exit'],8),number(100*r['pnl_pct'],4),number(100*r['net_pnl_pct'],4),number(r['pnl'],7),number(r.get('fees_usdt'),7),r.get('fee_source')]for r in closes])))
    if audit:
        parts.append(panel('Independent exact-position cash reconciliation',table(['Symbol','Side','Entry quantity','Entry value VST','Exit value VST','Gross VST','Full position commission VST','Net VST','Engine arithmetic VST'],[[r['symbol'],r['side'],number(r['quantity'],6),number(r['entryValue'],7),number(r['exitValue'],7),number(r['gross'],7),number(r['commission'],7),number(r['net'],7),number(r['engineRecomputedNet'],7)]for r in audit['comparisons']])+note('Stored verified engine cash − all actual position cash = '+number(audit['storedNetDifference'],12)+' VST. On the matched verified engine records the difference is '+number(audit.get('matchedEngineNetDifference'),12)+' VST. Full-precision position commissions are used; the per-order table exposes the exchange’s rounded display commissions. Refusals remain in the audit; account-balance deltas are not used for attribution.')))
        parts.append('<details><summary>Every exact owned exchange order · '+str(len(audit['orders']))+' receipts</summary>'+table(['Symbol','Direction','Order side','Type','Status','Filled qty','Filled value VST','Reported commission VST','Fill count','Order hash'],[[r['symbol'],r['positionSide'],r['side'],r['type'],r['status'],number(r['qty'],6),number(r['value'],7),number(r['commission'],7),r['fills'],r['orderHash']]for r in audit['orders']])+'</details>')
        parts.append(panel('Definite venue refusals',table(['Journal index','Venue code','Venue message'],[[r['index'],r['code'],r['message']]for r in audit.get('definiteRefusals',[])])))
    workers=report.get('replayWorkers',{})
    parts.append(panel('Processing scope and limitations',table(['Item','Observed / configured'],[
        ['Order ownership','Exact session tag + journaled identities + confirmed own fill inventory'],
        ['Account scope',report.get('accountScope')],['Native requested symbols',', '.join(report['symbols'])],
        ['Raw replay workers',workers.get('configured')],['Raw symbols replayed',workers.get('completed')],['Worker native fallbacks',workers.get('fallbacks')],
        ['Paper modeled cost %',number(report.get('accountRates',{}).get('paperCostPct'),5)],['Leverage / margin','Unchanged; conservative 1x test margin estimate'],
        ['Last-N policy',book.get('lastNMode')],['Independent fixed Last-N lanes',book.get('independentLastNLanes')],['Relation lanes',book.get('relations',{}).get('config',{}).get('enabled')],
        ['Axis enabled',str({k:report['overlay'].get('axis'+k+'Enabled')for k in('Prev','Last','Cont','Pause')})],
        ['Block enabled',report['overlay'].get('stratBlock')],['DCA enabled',report['overlay'].get('dcaEnabled')],
        ['Native self-check failures retained','; '.join(t['name']+': '+t.get('detail','')for t in final.get('tests',[])if t.get('pass')is False)],
        ['Native source SHA256',str(report.get('sourceSha256'))],
        *[['Limitation',value]for value in report.get('limitations',[])]])))
    parts.append('</section>')
    return ''.join(parts)


def fresh_section(path):
    run=json.loads(path.read_text());hours=run['settled_hourly'];all_=run['all'];labels=[stamp(h['t'])[7:15]for h in hours]
    cumulative=[];current=0
    for h in hours:current+=h['net_pct'];cumulative.append(current)
    parts=['<section id="fresh"><h2>Fresh 50-symbol replay · unchanged x02 preset</h2><p class="sub">'+esc(stamp(run['start_utc'])+' → '+stamp(run['end_utc'])+' Europe/Vienna · '+str(run['hours'])+' scored hours · measured account fees · separate disjoint market universe')+'</p>']
    parts.append('<div class="cards">'+''.join([card('Configured Sets',number(run['sets'],0),'Each Set owns its own tape and stages'),card('Selected Base PF',number(all_['eff_pf']),'Selection-window statistic'),card('Execution PF',number(all_['exec_pf']),str(all_['orders'])+' closed simulated parents'),card('Settled unit net',number(all_['net_pct'])+'%','Percent of one base notional'),card('Held at cutoff',len(run.get('open_at_end',[])),'Marks remain separate from realized results')])+'</div>')
    parts.append('<div class="grid">'+plot('Fresh replay · settled unit net by exit hour',[('Cumulative unit net %',cumulative)],labels)+plot('Fresh replay · sufficient Last-N samples',[(stage,[next((row.get('enough_samples_pct')for row in run['stage_evaluations']if row['dimension']=='all'and row['stage']==stage and row['t']==h['t']),None)for h in hours])for stage in('baseLong','mainLong','real','realNeg')],labels)+'</div>')
    parts.append(panel('Every fresh scored hour',table(['Vienna exit hour','Opened','Closed','Wins','Settled return PF','Unit net %','Cumulative unit net %'],[[stamp(h['t']),h['opened'],h['orders'],h['wins'],execution_pf(h),number(h['net_pct']),number(cumulative[i])]for i,h in enumerate(hours)])))
    summary_headers=['Group','Sets','Base validated %','Selected Base PF','Full execution gate %','Closed parents','Execution PF','Unit net %','Win %']
    def summary(rows):return [[r['group'],r.get('sets'),number(r.get('valid_pct'),2),number(r.get('eff_pf')),number(r.get('executable_pct'),2),r.get('orders'),execution_pf(r),number(r.get('net_pct')),number(r.get('win_pct'),2)]for r in rows]
    parts.append(panel('Every indication and strategy type',table(summary_headers,summary(run.get('types',[])))))
    for indication in run.get('indications',[]):
        pack=indication['group'];configs=[r for r in run.get('configurations',[])if r['pack']==pack]
        parts.append('<details><summary>'+esc(pack)+' · '+str(len(configs))+' independent configurations</summary>'+table(['Full Base samples %',*summary_headers],[[number(c.get('enough_base_samples_pct'),2),*row]for c,row in zip(configs,summary(configs))])+'</details>')
    parts.append(panel('Every eligible Last-N stage by hour and strategy',table(['Vienna time','Type','Window','Eligible','Passed','Passed %','Full N','Insufficient N','Full N %','Selected PF'],[[stamp(r['t']),r['group'],r['stage'],r['evaluated'],r['passed'],number(r['passed_pct'],2),r.get('samples_full'),r.get('samples_insufficient'),number(r.get('enough_samples_pct'),2),number(r.get('selection_pf'))]for r in run.get('stage_evaluations',[])if r['dimension']in('all','type')])))
    parts.append(panel('Every fresh simulated close',table(['Entry · Vienna','Exit · Vienna','Symbol','Set','Type','Lane','Direction','Exit reason','Net %','Volume ×','Weighted net %'],[[stamp(r['t_in']),stamp(r['t_out']),r['symbol'],r['set'],r['kind'],r.get('lane'),r['side'],r['reason'],number(r['net_pct'],5),number(r.get('volume_multiple',1),4),number(r.get('weighted_net_pct'),5)]for r in run.get('trades',[])])))
    parts.append(note('This is a fixed-preset diagnostic with no PnL-based symbol selection or retuning on the scored interval. It is not tick-identical exchange execution. Natural native fills, cleanup fills and historical simulation remain separate. Full raw JSON, candle hashes and costs are retained with the evidence.'))
    parts.append('</section>');return ''.join(parts)


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--report',type=Path,required=True)
    ap.add_argument('--session',type=Path);ap.add_argument('--session-id',default='native');ap.add_argument('--session-heading',default='Native x02 · actual engine observation')
    ap.add_argument('--fresh-replay',type=Path);ap.add_argument('--out',type=Path,required=True)
    args=ap.parse_args();body=args.report.read_text();sections='';links=''
    if args.session:sections+=native_section(args.session,args.session_id,args.session_heading);links+='<a href="#'+esc(args.session_id)+'">'+esc(args.session_heading)+'</a>'
    if args.fresh_replay:sections+=fresh_section(args.fresh_replay);links+='<a href="#fresh">Fresh 50-symbol replay</a>'
    marker='<section id="verification">'
    if marker not in body:raise ValueError('complete report verification section is missing')
    body=body.replace(marker,sections+marker,1).replace('</nav>',links+'</nav>',1)
    args.out.write_text(body);print(json.dumps({'output':str(args.out),'bytes':args.out.stat().st_size,'native':bool(args.session),'freshReplay':bool(args.fresh_replay)}))


if __name__=='__main__':main()
