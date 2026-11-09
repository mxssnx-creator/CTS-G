#!/usr/bin/env python3
"""Independent arithmetic audit and portable report for the frozen Last-N sweep."""
import argparse, base64, collections, datetime as dt, hashlib, html, json, math
from pathlib import Path


def metric(values):
    gain=math.fsum(max(0,x) for x in values);loss=math.fsum(max(0,-x) for x in values)
    return dict(n=len(values),pf=gain/loss if loss else None,noLoss=bool(gain and not loss),gain=gain,loss=loss,netPct=100*math.fsum(values),wins=sum(x>0 for x in values))

def dd(values):
    equity=peak=worst=0.
    for v in values:
        equity+=v;peak=max(peak,equity);worst=max(worst,peak-equity)
    return worst*100

def same(a,b):
    return a==b if a is None or b is None or isinstance(a,bool) or isinstance(b,bool) else math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-12)

def stamp(t):return dt.datetime.fromtimestamp(t,dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
def esc(x):return html.escape(str(x))
def pf(row):return 'No loss' if row.get('noLoss') else 'Unavailable' if row.get('pf') is None else f'{row["pf"]:.3f}'
def table(headers,rows):
    return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+esc(v)+'</th>' for v in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--evidence',required=True);ap.add_argument('--out',required=True)
    args=ap.parse_args();root=Path(args.evidence);out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    protocol=json.loads((root/'protocol.json').read_text());summary=json.loads((root/'summary.json').read_text())
    trials=[json.loads((root/(r['id']+'.json')).read_text()) for r in summary];errors=[];count=0;hour_checks=0
    for trial in trials:
        trades=trial['trades'];nets=[t['net'] for t in trades]
        for t in trades:
            count+=1
            if t['kind']!='base' or not protocol['trade'][0]<t['t_in']<=t['t']<=protocol['trade'][1]:errors.append([trial['id'],'trade bounds/type'])
            # Independent plain price arithmetic, no production PF/cost helper imported.
            expected=(t['exit']/t['entry']-1)*t['side']-.0011
            if not same(expected,t['net']):errors.append([trial['id'],'cost-once price mismatch'])
        calculated=metric(nets)
        for key,value in calculated.items():
            if not same(value,trial['closed'][key]):errors.append([trial['id'],'closed '+key])
        if not same(dd(nets),trial['unitDrawdownPercent']):errors.append([trial['id'],'drawdown'])
        if len(trial['stages'])!=24:errors.append([trial['id'],'hour count'])
        for h in trial['stages']:
            hour_checks+=1
            m=metric([t['net'] for t in trades if h['t']<t['t']<=h['t']+3600])
            if not 0<=h['executable']<=h['basePassed']<=h['evaluated'] or not 0<=h['mainPassed']<=h['basePassed']:errors.append([trial['id'],'stage counts'])
            if not same(h['validatedPercent'],100*h['basePassed']/h['evaluated']):errors.append([trial['id'],'validation percent'])
            for k,v in m.items():
                if not same(v,h['settled'][k]):errors.append([trial['id'],'hour '+k])
        half_at=protocol['trade'][0]+43200
        halves=[metric([t['net'] for t in trades if (t['t']<=half_at)==first]) for first in (True,False)]
        for actual,expected in zip(trial['halves'],halves):
            for k,v in expected.items():
                if not same(v,actual[k]):errors.append([trial['id'],'half '+k])
        passes=all(h['n']>=10 and (h['noLoss'] or (h['pf'] is not None and h['pf']>1)) for h in halves)
        if passes!=trial['passesScreen']:errors.append([trial['id'],'screen'])
    audit={'trials':len(trials),'tradeOccurrences':count,'hourWindows':hour_checks,'errors':errors,
           'nativeParityChecks':sum(t['parity']['checks'] for t in trials),'nativeParityMismatches':sum(t['parity']['mismatches'] for t in trials),
           'modeledRoundTripFraction':.0011,'scope':'Price/cost-once, all settled PF, drawdown, hourly counts, stage count bounds and chronological halves. Repeated configurations are not independent market samples.'}
    (out/'arithmetic-audit.json').write_text(json.dumps(audit,indent=2));assert not errors,errors[:10]
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'font.size':9,'axes.spines.top':False,'axes.spines.right':False})
    ranges=['micro','minimal','short'];modes=[x[0] for x in protocol['negativeCases']];triples=protocol['triples']
    figure,axes=plt.subplots(3,3,figsize=(18,14),constrained_layout=True)
    for i,name in enumerate(ranges):
        selected=[t for t in trials if t['range']==name];data=np.full((5,10),np.nan);counts=np.zeros((5,10))
        for t in selected:
            y=modes.index(t['mode']);x=triples.index([t['baseN'],t['mainN'],t['realN']]);counts[y,x]=t['closed']['n']
            if t['closed']['pf'] is not None:data[y,x]=t['closed']['pf']
        ax=axes[i,0];im=ax.imshow(data,vmin=0,vmax=1.5,cmap='RdYlGn',aspect='auto')
        ax.set_title(name.title()+' · settled PF (all sample sizes)');ax.set_yticks(range(5),modes);ax.set_xticks(range(10),['/'.join(map(str,x)) for x in triples],rotation=45,ha='right');ax.set_xlabel('Base / Main / Real N')
        for y in range(5):
            for x in range(10):ax.text(x,y,'—' if math.isnan(data[y,x]) else f'{data[y,x]:.2f}',ha='center',va='center',fontsize=8)
        ax=axes[i,1];ax.imshow(counts,cmap='Blues',aspect='auto');ax.set_title(name.title()+' · settled parent counts');ax.set_yticks(range(5),modes);ax.set_xticks(range(10),['/'.join(map(str,x)) for x in triples],rotation=45,ha='right');ax.set_xlabel('Base / Main / Real N')
        for y in range(5):
            for x in range(10):ax.text(x,y,str(int(counts[y,x])),ha='center',va='center',fontsize=8)
        ax=axes[i,2]
        for mode in modes:
            rr=sorted([t for t in selected if t['mode']==mode],key=lambda t:triples.index([t['baseN'],t['mainN'],t['realN']]))
            ax.plot(range(10),[t['unitDrawdownPercent'] for t in rr],marker='.',label=mode)
        ax.set_title(name.title()+' · closed unit-equity drawdown');ax.set_ylabel('Percentage of one position unit');ax.set_xticks(range(10),['/'.join(map(str,x)) for x in triples],rotation=45,ha='right');ax.legend(fontsize=7)
    figure.suptitle('CTS-G · 150 Last-N experiments · 12 symbols · 24h trading\nNo case passed both chronological halves. Drawdown is not account drawdown.',fontsize=16)
    chart=out/'CTS-Last-N-multichart-20261109.png';figure.savefig(chart,dpi=145);plt.close(figure)
    best={name:max((t for t in trials if t['range']==name and t['closed']['n']>=10),key=lambda t:t['closed']['pf'] or 0) for name in ranges}
    comparisons=[]
    for name,t in best.items():
        baseline=next(x for x in trials if x['range']==name and [x['baseN'],x['mainN'],x['realN']]==[t['baseN'],t['mainN'],t['realN']] and x['mode']=='positive')
        comparisons.append([name,t['mode'],f'{t["baseN"]}/{t["mainN"]}/{t["realN"]}',pf(t['closed']),t['closed']['n'],f'{t["unitDrawdownPercent"]:.3f}%',f'{pf(t["halves"][0])} / {t["halves"][0]["n"]}',f'{pf(t["halves"][1])} / {t["halves"][1]["n"]}',f'{pf(baseline["closed"])} / {baseline["closed"]["n"]}'])
    sections=[]
    for t in trials:
        groups=collections.defaultdict(list)
        for trade in t['trades']:groups[(trade['pack'].split('~')[0],trade['kind'],trade['lane'],trade['step'],trade['slRatio'],'Long' if trade['side']>0 else 'Short')].append(trade['net'])
        hourly=table(['Hour UTC','Base pass / evaluated','Validated','Historical selected PF','Main pass','Executable','Settled parents','Settled PF','Settled unit net %'],[
            [stamp(h['t']),f'{h["basePassed"]}/{h["evaluated"]}',f'{h["validatedPercent"]:.2f}%',pf({'pf':h['selectedWindowPf'],'noLoss':h['noLoss']}),h['mainPassed'],h['executable'],h['settled']['n'],pf(h['settled']),f'{h["settled"]["netPct"]:.4f}'] for h in t['stages']])
        breakdown=table(['Indication config','Strategy type','Lane','TP step','SL/TP','Direction','Parents','PF','Unit net %'],[list(k)+[len(vals),pf(metric(vals)),f'{metric(vals)["netPct"]:.4f}'] for k,vals in sorted(groups.items())])
        trade_table=table(['Entry UTC','Close UTC','Symbol','Set ID','Lane','Side','Entry','Exit','Reason','Net %'],[[stamp(r['t_in']),stamp(r['t']),r['symbol'],r['set'],r['lane'],'Long' if r['side']>0 else 'Short',r['entry'],r['exit'],r['reason'],f'{100*r["net"]:.6f}'] for r in t['trades']])
        sections.append(f'<details class="trial" data-name="{esc(t["id"])}"><summary>{esc(t["id"])} · PF {pf(t["closed"])} · {t["closed"]["n"]} settled / {t["held"]} held</summary><p>Negative recovery windows: {esc(t["negativeWindows"])}; recent-loss filter: {esc(t["negativeFilter"])}. Strict full windows. No result promoted.</p><h3>Hourly stages and subsequent closes</h3>{hourly}<h3>Executed indication, type, lane and protection</h3>{breakdown}<details><summary>Every settled parent</summary>{trade_table}</details></details>')
    image=base64.b64encode(chart.read_bytes()).decode()
    body=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CTS Last-N study · 9 November 2026</title>
<style>body{{font:16px/1.55 system-ui,sans-serif;color:#17243a;background:#f5f7fa;margin:0}}main{{max-width:1450px;margin:auto;padding:32px}}h1{{font-size:36px}}h2{{margin-top:40px}}.lead{{font-size:20px;max-width:1000px}}.note{{background:#fff1d5;border-left:5px solid #ae6900;padding:20px}}img{{width:100%;height:auto;background:white}}table{{border-collapse:collapse;width:100%;background:white;font-size:13px}}th,td{{padding:9px 12px;border-bottom:1px solid #dde3ec;text-align:left;white-space:nowrap}}th{{background:#e7edf5}}.scroll{{overflow:auto;margin:14px 0}}details{{background:#fff;border:1px solid #d7dfeb;padding:14px;margin:12px 0}}summary{{cursor:pointer;font-weight:650}}input{{font:inherit;padding:12px;width:min(90%,600px)}}code{{overflow-wrap:anywhere}}@media(max-width:700px){{main{{padding:16px}}h1{{font-size:28px}}}}</style><main>
<p>CTS-G / CTS-A / CTS-A-O · engineering and retrospective research · 9 November 2026</p><h1>Expanded Last-N windows and negative filters</h1>
<p class="lead">150 native CTS-G experiments completed. Some loss filters improve selected comparisons; none meets the predeclared consistency and sample screen. No economic default is promoted.</p>
<p class="note">A high qualification PF describes a past selected window. It cannot guarantee subsequent execution PF. All later losses remain in this report. These are modeled parent positions, not exchange orders or verified account returns.</p>
<h2>Strongest case per range with at least 10 total closes</h2>{table(['Range','Mode','Base/Main/Real','PF','Closes','Unit DD','First half PF / n','Second half PF / n','Matching positive baseline PF / n'],comparisons)}
<p>Micro fails profitability. Minimal has only 1 and 9 closes in its halves. Short has 7 and 10, with first-half PF below 1. Selection requires at least 10 closes and PF above 1 in <em>each</em> 12-hour half, not merely a profitable aggregate. No-loss samples are shown explicitly; empty samples have no PF.</p>
<h2>Protocol and scope</h2><p>Trading: {stamp(protocol['trade'][0])}–{stamp(protocol['trade'][1])}. Evaluated prehistory: {stamp(protocol['prehistory'][0])}–{stamp(protocol['prehistory'][1])}. Completed one-minute bars; 12 frozen symbols: {esc(', '.join(protocol['symbols']))}. Data overlap earlier research, so this is retrospective sensitivity, not a pristine holdout.</p>
<p>Ten Base/Main/Real combinations × five modes × three ranges. Strict full windows; Base secondary and Main-short selection disabled to isolate the tested windows. The existing Axis gate remains active. The sweep contains normal TP/SL base strategies only, four indication configurations per range, independent symbol/config identities and SL/TP ratios 0.25 and 0.5. Block, DCA and Relate are not enabled in this market comparison. Concurrent qualification does not add venue slots: the native one-physical-position-per-symbol constraint remains.</p>
<p>Frozen public fee inputs: maker 0.02%, taker 0.05%, slippage 10% of fees. Market entry/exit uses a 0.11% modeled round trip; funding is excluded. Actual account fee tiers, partial fills, order-book impact and account drawdown are not established by this replay. Unit drawdown is the peak-to-trough sum of equal-notional settled returns, not leveraged account equity.</p>
<p>Negative recovery means CTS-G's existing same-direction recovery branch, followed by its own positive Real-negative qualification. Recent-loss vetoes instead block later admission when an exact stage's completed own window is loss-heavy. They do not reverse PnL, invert a direction, rewrite a close or close a held position. CTS-A-O's requested opposite-direction processed Main remains a separate unfinished integration.</p>
<h2>All 150 experiments</h2><img alt="Three range rows showing settled PF heatmaps, trade-count heatmaps and drawdown comparisons" src="data:image/png;base64,{image}">
<h2>Integrity checks</h2><p>{count:,} trade occurrences independently reconcile entry/exit prices and one cost deduction. All {hour_checks:,} hourly windows reconcile counts and PF. {audit['nativeParityChecks']} sampled native-engine/replay verdict comparisons have {audit['nativeParityMismatches']} mismatches. Negative-filter arithmetic matched Python, CTS-A and CTS-A-O in 2,880 comparisons. Repeated configurations reuse market data; these counts are not independent observations of profitability.</p>
<h2>What changed in each project</h2>{table(['Project','Implemented','Remaining scope'],[
['CTS-G','1–200 Last-N values; up to 8 exact policy profiles; recent-loss any/all filters; strict Base full-window fix; native replay parity; Relate ownership/persistence guards','No profitable preset established; no new x01 start'],
['CTS-A','Expanded positive/negative grids; per-policy filter; cache/retention fixes; configured low-N statistics replaces hidden Last-8 minimum','Persistent separately funded stages and complete live close reconciliation remain open'],
['CTS-A-O','Expanded independent processed policies; causal own-parent loss veto; restart validation; step/SL PF; demo network separation','Processed book remains standalone; persistent runtime stage adapter is not integrated']])}
<p>UI typechecks and builds pass. Broad browser rendering is not certified in this environment. CTS-A-O's historical October 8–9 sample is unchanged: Base 143/1,398 (10.23%); post-Base 405/1,276 (31.74%), finite historical mean PF 17.3995 plus six no-loss contexts; later funded-model PF 0.27305. Current-policy CTS-A-O post-Base and venue PF are unavailable.</p>
<h2>Earlier x02 demo interruption</h2><p>The separate CTS-G VST run lasted approximately 20m25s and ended when network access was blocked. Cleanup verified zero positions and zero orders. There were 22 entries and 22 exits: one natural close and 21 interruption closes. Across all 22 owned closes, return PF was 0.2302; cash diagnostic PF was 0.2266 and net was −0.127680 VST. This predates the new Last-N filters and is not a completed 30-minute acceptance run. CTS-A and CTS-A-O 30-minute demo runs were not completed. No live/sim parity is claimed without matching final candles.</p>
<h2>Detailed hourly, indication and trade results</h2><p>Historical selected PF pools only currently Base-qualified windows and can overlap between configurations. Main/executable counts are same-clock readiness, not future fills. Settled hourly PF includes every admitted later loss. Search by range, window or mode.</p><label>Find experiment <input id="find" type="search" placeholder="Example: minimal-b10-m5-r3"></label>{''.join(sections)}
<p>Reproduction: scripts/lastn_sensitivity.py and scripts/report_lastn_sensitivity.py. Source/cache hashes, full trial JSON and the independent arithmetic audit accompany the evidence archive.</p></main><script>document.getElementById('find').addEventListener('input',e=>{{const q=e.target.value.trim().toLowerCase();document.querySelectorAll('.trial').forEach(x=>x.hidden=!x.dataset.name.includes(q));}});</script></html>'''
    report=out/'CTS-Last-N-negative-windows-20261109.html';report.write_text(body)
    (out/'best-comparisons.json').write_text(json.dumps({name:{k:v for k,v in t.items() if k not in ('trades','stages')} for name,t in best.items()},indent=2))
    print(json.dumps({'report':str(report),'chart':str(chart),'audit':audit},indent=2))

if __name__=='__main__':main()
