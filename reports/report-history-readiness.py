from pathlib import Path
import base64,html,json,math,sys
from datetime import datetime,timezone
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
root=Path(__file__).resolve().parents[1]
p=root/'evidence/history-readiness-20261110-final/results'
summary=json.loads((p/'summary.json').read_text())
protocol=json.loads((p/'protocol.json').read_text())
complete=json.loads((p/'completion.json').read_text())
assert complete['cases']==72 and complete['sourceUnchanged']
rows=[json.loads((p/(r['id']+'.json')).read_text()) for r in summary]
by={(r['range'],r['prehistoryHours'],r['baseN'],r['mainN'],r['realN'],r['mode']):r for r in rows}
variants=[tuple(v) for v in protocol['variants']]
colors=['#347aa7','#d27c22']
fig,axes=plt.subplots(3,2,figsize=(15,13),layout='constrained')
fig.suptitle('CTS-G · 24h vs 48h prehistory · same next 24h',fontsize=20,fontweight='bold')
for ri,ran in enumerate(['micro','minimal','short']):
    ax=axes[ri,0]
    for i,pre in enumerate([24,48]):
        r=by[(ran,pre,30,15,12,'positive')]
        vals=[r['stages'][0]['sampleCoverage'][s]['all']['pct'] or 0 for s in ['base','main','real']]
        ax.bar(np.arange(3)+(i-.5)*.32,vals,width=.32,label=f'{pre}h prehistory',color=colors[i])
        for j,v in enumerate(vals):ax.text(j+(i-.5)*.32,v+1,f'{v:.1f}%',ha='center',fontsize=8)
    ax.set_xticks(np.arange(3),['Base N30','Main N15','Real N12']);ax.set_ylim(0,110)
    ax.set_ylabel('Full samples / all exact sets (%)')
    ax.set_title(f'{ran.title()} · readiness at trading start',loc='left',fontweight='bold')
    ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
    if ri==0:ax.legend(loc='upper right',fontsize=8)
    ax=axes[ri,1]
    labels=[]
    for vi,(bn,mn,rn,mode) in enumerate(variants):
        labels.append(f'{bn}/{mn}/{rn}'+(' V' if mode=='veto-1' else ''))
        for i,pre in enumerate([24,48]):
            r=by[(ran,pre,bn,mn,rn,mode)]
            if r['closed']['n']:
                pf=r['closed']['pf']
                if pf is not None:ax.scatter(vi+(i-.5)*.2,pf,s=24+min(50,r['closed']['n']),c=colors[i],marker='o' if i==0 else 's')
                else:ax.text(vi+(i-.5)*.2,1.08,'NL',color=colors[i],ha='center',fontsize=8)
            else:ax.text(vi+(i-.5)*.2,.03,'–',color=colors[i],ha='center',fontsize=8)
    ax.axhline(1,color='#68737a',linestyle='--',linewidth=1)
    ax.set_xticks(range(len(labels)),labels,rotation=50,ha='right',fontsize=8)
    ax.set_ylim(bottom=0);ax.set_ylabel('Settled modeled PF (net costs)')
    ax.set_title(f'{ran.title()} · executions; marker size reflects count',loc='left',fontweight='bold')
    ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
fig.text(.01,.001,'N = Base/Main/Real; V = recent-loss veto Last-1. NL = gains with no loss; – = no settlements. Fixed quoted fees, funding excluded. Retrospective evidence; no preset promoted.',fontsize=9)
image=p.parent/'CTS-G-history-readiness-20261110.png';fig.savefig(image,dpi=150);plt.close(fig)
def dt(t):return datetime.fromtimestamp(t,timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
def fmt(x,n=2):return '—' if x is None else f'{x:.{n}f}'
def pf(m):return 'No loss' if m.get('noLoss') else fmt(m.get('pf'),4)
def sample(s,pop='all'):
    a=s[pop];return f"{a['full']}/{a['population']} ({fmt(a['pct'])}%)" if a['population'] else '— (no population)'
def tab(headers,data):
    return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+html.escape(str(h))+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+html.escape(str(c))+'</td>' for c in row)+'</tr>' for row in data)+'</tbody></table></div>'
css='''body{margin:0;background:#edf2f6;color:#15273b;font:15px system-ui,sans-serif}main{max-width:1400px;margin:auto;padding:28px 18px}header{background:#162d43;color:white;border-radius:14px;padding:26px}h1{font-size:34px;margin:8px 0}h2{margin:24px 0 12px}p{line-height:1.6}.card,details{background:white;padding:20px;border-radius:12px;margin-top:18px;border:1px solid #d1dce6}.note{border-left:5px solid #b87912}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}td,th{padding:8px;text-align:right;border-bottom:1px solid #dfe5eb}th{background:#e9eff5;color:#42596b}td:first-child,th:first-child{text-align:left}summary{cursor:pointer;font-weight:700}img{width:100%;height:auto}.small{font-size:12px;color:#536b7e}code{font-size:12px}li{margin:7px 0}details details{background:#f7fafc}@media(max-width:600px){main{padding:10px}h1{font-size:27px}header,.card,details{padding:14px}}'''
parts=[f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CTS-G prehistory readiness comparison</title><style>{css}</style><main>',
'<header><div>ENGINEERING / FROZEN MARKET COMPARISON</div><h1>More history, measured sample coverage</h1>',f"<p>12 symbols · {dt(protocol['trade'][0])} to {dt(protocol['trade'][1])} · 72 independent configuration trials</p></header>",
'<section class="card note"><h2>What this test establishes</h2><p>Every comparison trades the same final 24 hours. The baseline receives 24 hours of preceding candles; the extended run receives 48 hours. Each run starts fresh and retains its own Base → Main → Real evidence. Full sample counts include losing sets and empty configured sets. PF qualification and later executed results are reported separately.</p>',
f"<p><strong>{complete['passesScreen']} of 72 cases passed the predeclared screen</strong> (PF above 1 and at least ten settled parents in each chronological 12-hour half). No economic preset is promoted. {complete['parityChecks']} native/replay verdict checks and {complete['sampleCountChecks']} native/direct sample-count checks had zero mismatches. Every modeled trade's cost was independently checked against entry and exit prices.</p>",
'<p>These are model executions from previously used frozen market candles. They do not prove future PF, live exchange execution, all strategies, or account-specific fees. Study auto-PF is off to isolate prehistory; automatic PF correctness is tested separately. The rolling history cannot provide upstream context older than its available input. CTS-G’s longer existing 72-hour default is preserved.</p></section>',
'<section class="card"><h2>Readiness and resulting PF</h2><img alt="Six charts compare sample readiness and executed PF across Micro Minimal and Short" src="data:image/png;base64,'+base64.b64encode(image.read_bytes()).decode()+'"></section>']
records=[]
for ran in ['micro','minimal','short']:
    for bn,mn,rn,mode in variants:
        for pre in [24,48]:
            r=by[(ran,pre,bn,mn,rn,mode)];s=r['stages'][0]
            records.append([ran,f'{bn}/{mn}/{rn}',mode,pre,r['sets'],sample(s['sampleCoverage']['base']),sample(s['sampleCoverage']['main']),sample(s['sampleCoverage']['real']),fmt(s['selectedBasePf'],4),fmt(s['validatedPct']),r['closed']['n'],pf(r['closed']),fmt(r['unitDrawdownPct']),r['passesScreen']])
parts+=['<section class="card"><h2>All cases · readiness at trading start</h2><p>The three readiness columns use all exact sets as their denominator. Detailed hourly tables below additionally show only the predecessor-qualified population. Drawdown is in percent of one unit of notional, not account drawdown.</p>',tab(['Range','Base/Main/Real N','Mode','Pre h','Sets','Base full','Main full','Real full','Selected Base PF','Base valid %','Settled','Executed PF','Unit DD %','Screen pass'],records),'</section>']
for r in rows:
    parts.append('<details><summary>'+html.escape(r['id'])+f" · {r['closed']['n']} settlements · PF {pf(r['closed'])}</summary>")
    parts.append('<p>All-set readiness measures own history before PF acceptance; eligible readiness uses the prior stage’s current qualifying sets. Neither percentage asserts profitability.</p>')
    data=[]
    for s in r['stages']:
        c=s['sampleCoverage'];data.append([dt(s['t']),sample(c['base']),sample(c['main']),sample(c['main'],'eligible'),sample(c['real']),sample(c['real'],'eligible'),s['passed']['base'],s['passed']['main'],s['passed']['real'],fmt(s['selectedBasePf'],4),s['settled']['n'],pf(s['settled'])])
    parts.append(tab(['UTC decision','Base full/all','Main full/all','Main full/eligible','Real full/all','Real full/eligible','Base pass','Main pass','Real pass','Selected Base PF','Hour settled','Hour PF'],data))
    parts.append('<details><summary>Individual modeled settlements</summary>')
    parts.append(tab(['Exit UTC','Entry UTC','Symbol','Side','Step','SL ratio','Lane','Reason','Entry price','Exit price','Net %','Set'],[[dt(x['t']),dt(x['t_in']),x['symbol'],'long' if x['side']>0 else 'short',x['step'],x['slRatio'],x['lane'],x['reason'],f"{x['entry']:.8g}",f"{x['exit']:.8g}",fmt(100*x['net'],5),x['set']] for x in r['trades']]))
    parts.append('</details></details>')
parts+=['<section class="card"><h2>Protocol and provenance</h2><p>Symbols: '+html.escape(', '.join(protocol['symbols']))+'</p><p>Costs: '+html.escape(protocol['fees'])+'</p><p>Source hashes and all trial evidence are included with the results. All stages use strict closed-before-decision timestamps. No-loss PF is a separate state; empty windows are unmeasured, and losses are retained.</p></section></main></html>']
report=p.parent/'CTS-G-history-readiness-20261110.html';report.write_text(''.join(parts))
print(report);print(image)
