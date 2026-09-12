"""Restartable seven-day, all-policy matrix. Price paths are computed once.

The independent shadow Set continues collecting closes even while admission
is false. Live-admission policies use only closes before an entry. Results are
research lanes, not an additive portfolio or an exchange execution claim.
"""
import argparse
import ctypes
import gzip
import hashlib
import html
import itertools
import json
import pathlib
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
from replay_five_days import replay
from fetch_historic_window import validate
from set_engine import IND_KINDS, IND_TAG_KIND, indication_kind_votes

ROOT=pathlib.Path(__file__).resolve().parents[1]
POLICIES=np.array(list(itertools.product(range(5,76,5),range(5,26,5),(1,3,5,7,9),(1,5,10),(0,1,2))),dtype=np.int32)
STAGE_WINDOWS=((5,3),(10,5),(15,10))
POLICIES=np.array([(*p[:4],*STAGE_WINDOWS[p[4]]) for p in POLICIES],dtype=np.int32)
POLICY_KEYS=('lastN','deactivationN','maxDdtHours','recalcAfterCloses','mainN','realN')
METRICS=('n','wins','net','cost','gain','loss','maxDd','maxDdtBars','trainN','trainNet','testN','testNet','disabled','costRSum')


def configs():
    out=[]
    for step,sl in itertools.product(range(1,31),(.2,.4,.6,.8)):
        base=dict(strategy='base',step=step,tpPct=max(.3,step*.1),slPct=sl,minSlPct=sl,
                  levels=0,incrementPct=0.,volumeRatio=0.,honorTp=True)
        out.append(base)
        for arm,give,honor in itertools.product((.3,.6,.9,1.2,1.5),(.1,.2,.3,.4,.5),(True,False)):
            out.append(dict(base,strategy='trail',trailArmPct=arm,trailGivePct=give,honorTp=honor))
        for level in range(1,7):
            out.append(dict(base,strategy='block',levels=level,incrementPct=.2,volumeRatio=.25))
        for level,inc in itertools.product((1,2,3),(.05,.1,.2)):
            out.append(dict(base,strategy='dca',levels=level,incrementPct=inc,volumeRatio=.25))
    return out


def kernel(path):
    lib=ctypes.CDLL(str(path))
    arr=np.ctypeslib.ndpointer(flags='C_CONTIGUOUS')
    lib.sweep.argtypes=[ctypes.c_int,arr,arr,arr,arr,ctypes.c_int,arr,ctypes.c_int,ctypes.c_int,arr]
    lib.sweep.restype=None
    return lib.sweep


def prepare(path,out):
    raw=path.read_bytes();blob=json.loads(raw)
    validate(blob['rows'],blob['start']-3600000,blob['end'])
    if blob['end']-blob['start']!=7*86400000:raise ValueError('Exact seven-day window required')
    bars=[b for _,b in blob['rows']];signals={k:np.zeros((len(bars),2)) for k in IND_KINDS}
    settings={f'type{k.title()}':True for k in IND_KINDS}
    for i in range(60,len(bars)):
        for d,c,tag in indication_kind_votes(bars[i-59:i+1],settings,blob['rows'][i][0]/1000):
            kind=IND_TAG_KIND.get(tag)
            if kind:signals[kind][i]=(d,c)
    dest=out/(blob['symbol']+'.npz')
    np.savez_compressed(dest,bars=np.array(bars),**signals)
    return dict(path=str(dest),symbol=blob['symbol'],start=blob['start'],end=blob['end'],
                bars=len(bars),sha256=hashlib.sha256(raw).hexdigest(),source=blob['source'])


def execute(task):
    source,kind,side,out,lib,signature=task;out=pathlib.Path(out)
    key=f"{source['symbol']}_{kind}_{'LONG' if side==1 else 'SHORT'}"
    dest=out/(key+'.json.gz')
    if dest.exists():
        old=json.loads(gzip.decompress(dest.read_bytes()))
        if old.get('signature')==signature:return old
    start=time.monotonic();blob=np.load(source['path']);bars=blob['bars'];signals=blob[kind]
    all_cfg=configs();unique=[];aliases=[];seen={}
    for i,c in enumerate(all_cfg):
        ck=json.dumps({k:v for k,v in c.items() if k not in ('step','minSlPct')},sort_keys=True)
        if ck not in seen:seen[ck]=len(unique);unique.append(c);aliases.append([])
        aliases[seen[ck]].append(i)
    n=len(unique);tapes=[[] for _ in unique];event_chunks=[]
    def closes(ids,entries,bar,net,cost):
        event_chunks.append(np.column_stack((ids,entries,np.full(len(ids),bar),net,cost)))
    replay(bars,signals,side,unique,on_closes=closes,return_rows=False,cost_pct=.1)
    events=np.concatenate(event_chunks) if event_chunks else np.zeros((0,5));del event_chunks
    if len(events):
        events=events[np.lexsort((events[:,1],events[:,0]))]
    offsets=np.searchsorted(events[:,0],np.arange(n+1)) if len(events) else np.zeros(n+1,dtype=int)
    fn=kernel(lib);metrics=np.zeros((len(POLICIES),14));summary=np.zeros((len(POLICIES),7))
    top=[];best_train=None;positive=0;traded=0;qualified=0;ever_positive=0;unique_results=hashlib.sha256()
    by_strategy={};split=60+int((len(bars)-60)*.7)
    for i,cfg in enumerate(unique):
        rows=events[offsets[i]:offsets[i+1]]
        ids=aliases[i];multiplicity=len(ids)
        metrics.fill(0)
        if len(rows):
            fn(len(rows),np.ascontiguousarray(rows[:,1],dtype=np.int32),np.ascontiguousarray(rows[:,2],dtype=np.int32),
               np.ascontiguousarray(rows[:,3]),np.ascontiguousarray(rows[:,4]),len(POLICIES),POLICIES,len(bars)-1,split,metrics)
        unique_results.update(metrics.tobytes())
        pos=metrics[:,2]>1e-12;has=metrics[:,0]>0
        costpf=np.divide(metrics[:,13],metrics[:,0],out=np.zeros(len(POLICIES)),where=has)*.1+1
        q=(metrics[:,8]>=8)&(metrics[:,10]>=8)&(metrics[:,9]>0)&(metrics[:,11]>0)&(costpf>1.05+1e-9)
        positive+=int(pos.sum())*multiplicity;traded+=int(has.sum())*multiplicity;qualified+=int(q.sum())*multiplicity
        ever_positive+=int(pos.any())*multiplicity
        st=by_strategy.setdefault(cfg['strategy'],dict(tested=0,positive=0,qualified=0))
        st['tested']+=len(POLICIES)*multiplicity;st['positive']+=int(pos.sum())*multiplicity;st['qualified']+=int(q.sum())*multiplicity
        summary[:,0]+=pos*multiplicity;summary[:,1]+=has*multiplicity;summary[:,2]+=q*multiplicity
        summary[:,3]+=metrics[:,9]*multiplicity;summary[:,4]+=metrics[:,11]*multiplicity
        summary[:,5]+=metrics[:,8]*multiplicity;summary[:,6]+=metrics[:,10]*multiplicity
        # Keep a bounded, deterministic review table; every result still
        # contributes to exact counters and the full-matrix digest above.
        candidate_ids=np.flatnonzero(has)
        if candidate_ids.size:
            ranking=sorted(candidate_ids,key=lambda j:(bool(q[j]),metrics[j,2],-metrics[j,6]),reverse=True)[:3]
            for j in ranking:
                m=metrics[j]
                item=dict(config=ids[0],aliases=ids,policy=int(j),metrics=dict(zip(METRICS,m.tolist())),
                          costPf=float(costpf[j]),classicPf=float(m[4]/m[5]) if m[5]>1e-12 else None,qualified=bool(q[j]))
                top.append(item)
            train_ids=np.flatnonzero(metrics[:,8]>=8)
            if len(train_ids):
                j=max(train_ids,key=lambda j:(metrics[j,9],-int(j)))
                # Selection score uses training net only; holdout is disclosed
                # after selection and never used to change this choice.
                if best_train is None or metrics[j,9]>best_train['metrics']['trainNet']:
                    m=metrics[j];best_train=dict(config=ids[0],policy=int(j),metrics=dict(zip(METRICS,m.tolist())),costPf=float(costpf[j]))
        if len(top)>300:
            top=sorted(top,key=lambda r:(r['qualified'],r['metrics']['net'],-r['metrics']['maxDd']),reverse=True)[:100]
    result=dict(key=key,signature=signature,symbol=source['symbol'],kind=kind,direction='LONG' if side==1 else 'SHORT',
                configs=len(all_cfg),uniquePricePaths=n,policies=len(POLICIES),tested=len(all_cfg)*len(POLICIES),positive=positive,
                traded=traded,qualified=qualified,positiveConfigs=ever_positive,byStrategy=by_strategy,
                summary=summary.tolist(),top=sorted(top,key=lambda r:(r['qualified'],r['metrics']['net'],-r['metrics']['maxDd']),reverse=True)[:100],
                trainingChoice=best_train,elapsedS=time.monotonic()-start,resultSha256=unique_results.hexdigest(),source=source)
    dest.write_bytes(gzip.compress(json.dumps(result,separators=(',',':'),allow_nan=False).encode(),mtime=0))
    print(json.dumps({k:result[k] for k in ('key','tested','positive','qualified','elapsedS')}),flush=True)
    return result


def render(results,out,signature):
    cfg=configs();pol=[dict(zip(POLICY_KEYS,map(int,p))) for p in POLICIES]
    best=[];train=[]
    for r in results:
        for row in r['top']:
            best.append(dict(symbol=r['symbol'],kind=r['kind'],direction=r['direction'],**row))
        if r['trainingChoice']:train.append(dict(symbol=r['symbol'],kind=r['kind'],direction=r['direction'],**r['trainingChoice']))
    best=sorted(best,key=lambda r:(r['qualified'],r['metrics']['net'],-r['metrics']['maxDd']),reverse=True)[:500]
    summary=[]
    for sym in ('BCH-USDT','SOL-USDT','XRP-USDT'):
        group=[r for r in results if r['symbol']==sym]
        summary.append(dict(symbol=sym,groups=len(group),**{key:sum(r[key] for r in group) for key in ('tested','positive','traded','qualified','positiveConfigs')}))
    accepted_defaults=[r for r in train if r['metrics']['trainNet']>0 and r['metrics']['testN']>=8 and r['metrics']['testNet']>0 and r['costPf']>1.05]
    default_decision=dict(selection="training-only with independent holdout check",accepted=accepted_defaults,
                          applied=False,reason="No training-selected winner clears the independent holdout; preserve requested Base last30 and two-day defaults.")
    payload=dict(defaultDecision=default_decision,signature=signature,configs=cfg,policies=pol,best=best,trainingChoices=train,summary=summary,
                 groups=[{k:v for k,v in r.items() if k not in ('top','summary')} for r in results])
    (out/'summary.json').write_text(json.dumps(payload,separators=(',',':')))
    def table(headers,rows):
        return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+html.escape(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'
    totals=table(['Symbol','Gruppen','Getestete Varianten','Mit Trades','Netto positiv','Beide Abschnitte positiv / PF > 1,05','Positive Risikokonfigurationen'],
                 [[s[k] for k in ('symbol','groups','tested','traded','positive','qualified','positiveConfigs')] for s in summary])
    def best_table(rows):
        body=[]
        for r in rows:
            c=cfg[r['config']];p=pol[r['policy']];m=r['metrics'];classic=m['gain']/m['loss'] if m['loss']>1e-12 else None
            body.append([r['symbol'],r['kind'],r['direction'],c['strategy'],c['step'],f"{c['tpPct']:.2f} / {c['slPct']:.2f}",
              f"{c.get('trailArmPct',0):.1f} / {c.get('trailGivePct',0):.1f} · TP {'an' if c['honorTp'] else 'aus'}",
              f"{c['levels']} / {c['incrementPct']} / {c['volumeRatio']}",p['lastN'],p['deactivationN'],p['maxDdtHours'],p['recalcAfterCloses'],f"{p['mainN']}/{p['realN']}",
              int(m['n']),f"{r['costPf']:.4f}",f"{classic:.3f}" if classic is not None else '∞' if m['gain'] else '—',
              f"{m['net']*100:.3f}",f"{m['cost']*100:.3f}",f"{m['maxDd']*100:.3f}",f"{m['maxDdtBars']/60:.2f}",f"{m['trainNet']*100:.3f}",f"{m['testNet']*100:.3f}"])
        return table(['Symbol','Indikation','Seite','Strategie','TP-Schritt','TP / SL %','Trailing Arm / Give %','Stufen / Abstand % / Add-Ratio','Last N','Deact N','DDT h','Recalc N','Main/Real N','Trades','CTS Cost-PF','Klassischer PF','Netto pp','Kosten pp','DD pp','DDT max h','Training pp','Kontrolle pp'],body)
    content=f'''<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CTS-G · 7 Tage · kontinuierliche Set-Auswertung</title>
<style>body{{background:#10151c;color:#e6edf4;margin:0;font:14px/1.5 system-ui}}main{{max-width:1600px;margin:auto;padding:24px}}h1{{font-size:30px}}section{{background:#19232e;border:1px solid #344758;border-radius:12px;margin:20px 0;padding:20px}}table{{border-collapse:collapse;white-space:nowrap;width:100%}}th,td{{padding:9px 12px;text-align:right;border-bottom:1px solid #344758}}th{{color:#96caeb}}th:first-child,td:first-child{{text-align:left}}.scroll{{overflow:auto;max-height:720px}}p{{max-width:1150px}}code{{color:#a3d8f7}}</style><main>
<h1>CTS-G · 7 Tage · Base → Main → Real</h1><p>04.09.2026 00:00 bis 11.09.2026 00:00 UTC · BCH, SOL, XRP · Axis aus.</p>
<section><h2>Vollständigkeit und Resultate</h2>{totals}<p>„Varianten“ sind alternative, unabhängige Set-/Policy-/Richtungsversuche. Ihre Gewinne ergeben keine gemeinsame Kontorendite. Jede getestete Variante zählt, auch ohne Trade. Gleichwertige Preisparameter werden berechnet und auf alle angeforderten IDs abgebildet.</p></section>
<section><h2>Exakte Testmatrix</h2><p>Je Symbol acht Projektindikationen × LONG/SHORT × {len(cfg):,} Risikokonfigurationen × {len(pol):,} Policies. Last-N: 5–75 in Schritten von 5; Deactivation: 5–25 in Schritten von 5; DDT: 1/3/5/7/9 Stunden; neue Bewertung nach 1/5/10 zusätzlichen Abschlüssen; Main/Real-Fenster 5/3, 10/5, 15/10. Alle kartesischen Kombinationen dieser Werte.</p>
<p>TP-Schritte 1–30 × 0,10 % mit effektivem TP-Floor 0,30 %; SL 0,20/0,40/0,60/0,80 %. Normal; 25 Trailing-Paare (Arm 0,3–1,5 Schritt 0,3, Give 0,1–0,5 Schritt 0,1), jeweils TP an/aus; Block 1–6 mit Add-Ratio 0,25 und Auslösung +0,20 %; DCA 1–3 Stufen × Abstand 0,05/0,10/0,20 %, Add-Ratio 0,25. Block/DCA verwenden feste TP/SL; Kombinationen mit Trailing sind ein gesondertes, hier nicht geprüftes Modell.</p></section>
<section><h2>Beste 50 beobachtete Varianten</h2><p>Explorative Rangliste nach dem Gesamtlauf. Beide Teilfenster positiv zuerst, danach Netto. Alle Parameter stehen je Zeile.</p>{best_table(best[:50])}</section>
<section><h2>Auswahl ausschließlich aus Training, danach Kontrolle</h2><p>Je Symbol/Indikation/Seite: höchstes Trainings-Netto mit mindestens acht Trainingsabschlüssen. Die Kontrollperiode verändert diese Auswahl nicht. Ein negativer Kontrollwert wird vollständig ausgewiesen.</p>{best_table(train)}</section>
<section><h2>Entscheidung zu Defaults</h2><p>{len(accepted_defaults)} nur anhand des Trainings ausgewählte Gewinner erfüllen den unabhängigen Kontrolltest (mindestens acht Abschlüsse, netto positiv, Gesamt-Cost-PF über 1,05). Deshalb wird kein explorativer Gesamtlauf-Gewinner als bewährter Live-Default ausgegeben. Die angeforderten Defaults bleiben: Base letzte 30 Positionen, Historie zwei Tage, gemeinsame PF-Schwelle 1,05, Achsen aus und keine logischen Mengenlimits.</p></section>
<section><h2>Beste Variante je Symbol und Indikation</h2>{best_table([dict(symbol=r['symbol'],kind=r['kind'],direction=r['direction'],**r['top'][0]) for r in results if r['top']])}</section>
<section><h2>Rechenmodell und Aussagegrenzen</h2><p>Öffentliche, auf lückenlose Zeitstempel und OHLCV geprüfte BingX-Kerzen, 60 Minuten Vorlauf. Signale nach Kerzenschluss; vorhandene SL vor TP, Stop-Gaps zum schlechteren Open, kein Wiedereinstieg in der Exit-Kerze. Trailing-Änderung gilt ab der nächsten Kerze. Additionen ändern Menge, Durchschnittseinstand und Ausführungskosten. Jede Ausführung zahlt 0,05 % des ausgeführten Notionals, einfacher Roundtrip ungefähr 0,10 %. Keine Funding-/Orderbuch-/Latenzrekonstruktion.</p>
<p>Basis-Shadow-Positionen werden kontinuierlich berechnet. Eine Eröffnung ist nur erlaubt, wenn frühere Abschlüsse in Base, Main und Real jeweils CTS Cost-PF > 1,05 ergeben. CTS Cost-PF = 1 + 0,10 × Durchschnitt(Netto / individuelle Ausführungskosten); klassischer PF = positive Nettoergebnisse / Betrag negativer Nettoergebnisse. Ein Abschluss der aktuellen Kerze kann deren Eröffnung nicht freigeben. Nach einem negativen Mittel der letzten Deactivation-N zugelassenen Abschlüsse bleibt die Policy für diese Sitzung deaktiviert; ihr Shadow-Set sammelt weiter.</p>
<p>DD/DDT der Policy beruhen auf abgeschlossenen Ergebnissen; offene Verluste innerhalb einer Position sind darin nicht enthalten. Die DDT-Schwelle am Eingang nutzt den bis dahin bekannten Shadow-Verlauf. Am 70/30-Split und Ende werden verbleibende Positionen mit Kosten geschlossen. Netto/Kosten/DD in Prozentpunkten des ursprünglichen Parent-Notionals, keine Kontorendite. Die Suche prüft die hier angegebene endliche Matrix und belegt keine reale Exchange-Ausführung.</p>
<p>Signatur: <code>{signature}</code>. Alle Gruppen besitzen einen SHA256 über sämtliche vollständigen Ergebnisvektoren; Rohereignisse und Quellkerzen sind reproduzierbar. Der HTML-Bericht zeigt die besten Varianten, Summen und die Auswahl aus Training.</p></section></main></html>'''
    (out/'cts-g-seven-days.html').write_text(content)
    return summary


def main():
    p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--output',required=True);p.add_argument('--workers',type=int,default=2);p.add_argument('--report-only',action='store_true');a=p.parse_args()
    out=pathlib.Path(a.output).resolve();out.mkdir(parents=True,exist_ok=True)
    files=['scripts/policy_sweep.cpp','scripts/sweep_seven_days.py','scripts/replay_five_days.py','server/pulse/indication_engine.py','server/pulse/set_engine.py']
    signature=hashlib.sha256(b''.join((ROOT/f).read_bytes() for f in files)).hexdigest()
    if a.report_only:results=[json.loads(gzip.decompress(x.read_bytes())) for x in out.glob('*.json.gz')]
    else:
        lib=out/'policy_sweep.so';subprocess.run(['g++','-O3','-std=c++17','-shared','-fPIC',str(ROOT/'scripts/policy_sweep.cpp'),'-o',str(lib)],check=True)
        sources=[prepare(pathlib.Path(a.data)/(s+'-USDT.json'),out) for s in ('BCH','SOL','XRP')]
        if len({(s['start'],s['end']) for s in sources})!=1:raise ValueError('Different symbol periods')
        tasks=[(s,k,side,str(out),str(lib),signature) for s in sources for k in IND_KINDS for side in (1,-1)]
        results=[]
        with ProcessPoolExecutor(max_workers=max(1,a.workers)) as pool:
            fs=[pool.submit(execute,t) for t in tasks]
            for f in as_completed(fs):results.append(f.result())
    if len(results)!=48 or any(r['signature']!=signature for r in results):raise ValueError('Missing/stale result groups')
    print(json.dumps(dict(summary=render(results,out,signature),groups=len(results))),flush=True)


if __name__=='__main__':main()
