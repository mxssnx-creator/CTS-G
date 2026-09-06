"""Policy summary and complete per-type/per-strategy training rankings."""
import gzip
import json
import pathlib
from collections import defaultdict
import matplotlib.pyplot as plt
from replay_complete import grids, WINDOWS


def rank(row):
    pf=row['trainPf']
    return (-(float('inf') if pf=='∞' else float(pf or 0)),row['trainDdPct'],-row['trainN'],row['group'],row['config'],row['direction'])


def collect(source):
    catalog=grids();groups={};best={};counts=defaultdict(lambda:dict(rows=0,qualified=0,positive=0))
    for path in sorted(pathlib.Path(source).glob('*.summary.json')):
        meta=json.loads(path.read_text());groups[meta['key']]=meta
        payload=json.loads(gzip.decompress(path.with_name(meta['key']+'.json.gz').read_bytes()))
        for values in payload['rows']:
            row=dict(zip(payload['columns'],values));cfg=catalog[meta['family']][row['config']]
            row.update(group=meta['key'],symbol=meta['symbol'],kind=meta['kind'],parameters=cfg)
            for key in [(meta['window'],meta['kind'],'all'),(meta['window'],meta['kind'],cfg['strategy'])]:
                count=counts[key];count['rows']+=1;count['qualified']+=row['qualified'];count['positive']+=row['positive']
                if row['trainN']>=8 and (key not in best or rank(row)<rank(best[key])):best[key]=row
    return best,counts


def section(source,evidence,out,table,fmt,esc,svg):
    best,counts=collect(source)
    status_path=pathlib.Path(evidence)/'policy-status.json'
    status=json.loads(status_path.read_text()) if status_path.exists() else {}
    records=[dict(window=w,kind=k,strategy=s,**counts[(w,k,s)],best=best.get((w,k,s)))
             for w,k,s in sorted(counts,key=lambda k:(list(WINDOWS).index(k[0]),k[1],k[2]))]
    (pathlib.Path(evidence)/'best-by-type.json').write_text(json.dumps(records,allow_nan=False))
    parts=['<section id="policy"><h2>Aktuelle Regeln · PF 1,05 · DD-Zeit höchstens 16 Stunden</h2>',
        table(['Regel','Sollwert / Modell','Status'],[
            ['Mindest-PF','1,05 einschließlich Grenzwert','Neue lokale Defaults für Base/Main/Real, Sets, DCA und Exits; konfigurierte Overall-PF-Grenze gilt auch für Block Active.'],
            ['Maximale Drawdown-Zeit','57.600 Sekunden = 960 Minuten = 16 Stunden','Default und obere Einstellgrenze; Research akzeptiert nur Ergebnisse innerhalb dieser Grenze.'],
            ['PositionCost dynamisch','Enabled','Gemessene eigene Exchange-Gebühren werden nach Notional gewichtet; kumulative Order-Meldungen ersetzen ihre vorherige Probe.'],
            ['Fallback PositionCost','0,10 % Roundtrip','Historischer Lauf: keine zeitbezogenen Exchange-Gebührenproben vorhanden; 0,05 % je ausgeführtem Entry/Add/Exit-Notional.'],
            ['Anzeige in Settings','Effektive Kosten, Fallback, Quelle, Proben, Messzeit','Gemessener Wert und editierbarer Fallback sind getrennt.'],
            ['Live-Ausführung','Block → Active; Normal (General) disabled; bis zu 50 qualifizierte Sets','Neue Source-Installation weiterhin durch automatische Freigabeprüfung blockiert. Keine erzwungene Auffüllung.']]),
        '<p><strong>PF-Skalen:</strong> Die Ranglisten zeigen klassischen Netto-PF (Summe positiver Nettoabschlüsse / Betrag negativer Nettoabschlüsse). Die Runtime verwendet CTS-Kosten-PF: 1 + durchschnittliches Netto-Ergebnis in Kosten-Einheiten × 0,10. Der Sollwert 1,05 wird in der jeweils ausdrücklich genannten Skala geprüft; die Skalen sind nicht austauschbar.</p>',
        '<p><strong>Research-Qualifikation:</strong> Mindestens acht Abschlüsse und klassischer Netto-PF ≥ 1,05 im Training sowie im Kontrollabschnitt; maximale Drawdown-Zeit über das ganze Fenster ≤ 16 Stunden. Die Drawdown-Zeit misst Zeit unter dem bisherigen Equity-Hoch. Ein Positions-Zeitstop garantiert keine maximale Portfolio-Erholungszeit.</p>',
        '<h3>Bereinigung und aktueller Prüfstand</h3>',table(['Prüfung','Ergebnis'],[[esc(k),esc(v)] for k,v in status.items()]),'</section>',
        '<section id="best-types"><h2>Beste Ergebnisse für jeden Typ und jede Strategie</h2><p>Je Zeitfenster und Typ bzw. Typ × Strategie wird über alle drei Symbole und beide Richtungen nach Trainings-PF, Trainings-DD und Trainings-N gerankt. Mindestens acht Trainingsabschlüsse; bei fehlender Evidenz steht „Keine belastbare Rangfolge“. Der Kontrollabschnitt beeinflusst diese Auswahl nicht. „Qualifiziert gesamt“ zählt alle bestandenen Alternativen der jeweiligen Gruppe, nicht nur den Trainingssieger. Die Daten waren bereits bekannt; dies bleibt eine retrospektive Prüfung.</p>']
    kinds=sorted({k for w,k,s in counts if s=='all'})
    fig,ax=plt.subplots(figsize=(11,4.5))
    vals=[best.get(('20d',k,'all'),{}).get('trainPf') for k in kinds]
    numbers=[float(v) if isinstance(v,(int,float)) else 0 for v in vals]
    ax.barh(kinds,numbers,color=['#0d9488' if v>=1.05 else '#b45309' for v in numbers])
    ax.axvline(1.05,color='#dc2626',ls='--',label='Mindest-PF 1,05')
    ax.set_xlabel('Bester Trainings-Netto-PF je Typ · 20-Tage-Fenster')
    ax.legend();ax.grid(axis='x',alpha=.3)
    parts.append(svg(fig,pathlib.Path(out).parent/'charts'/'best-by-type.svg'))
    headers=['Typ','Strategie','Zeilen / qualifiziert gesamt','Symbol / Richtung','Train PF / N','Kontroll-PF / N','Gesamt PF / N','Netto pp','Kosten pp','DD pp / DDT h','Sieger qualifiziert','Parameter']
    def cells(w,k,s):
        c=counts[(w,k,s)];r=best.get((w,k,s))
        prefix=[esc(k),esc(s),fmt(c['rows'],0)+' / '+fmt(c['qualified'],0)]
        if not r:return prefix+['Keine belastbare Rangfolge']+['—']*8
        p=r['parameters']
        params=' · '.join(f'{key}={value}' for key,value in p.items())
        return prefix+[esc(r['symbol']+' / '+r['direction']),fmt(r['trainPf'],6)+' / '+fmt(r['trainN'],0),
            fmt(r['holdoutPf'],6)+' / '+fmt(r['holdoutN'],0),fmt(r['pf'],6)+' / '+fmt(r['n'],0),
            fmt(r['netPct'],6),fmt(r['costPct'],6),fmt(r['maxDrawdownPct'],6)+' / '+fmt(r['maxDdS']/3600,3),
            'Ja' if r['qualified'] else 'Nein',esc(params)]
    for w in WINDOWS:
        parts.extend(['<h3>'+esc(w)+' · Typenübersicht</h3>',table(headers,[cells(w,k,'all') for k in kinds]),
            '<details><summary>'+esc(w)+' · vollständige Rangfolge je Typ × Strategie</summary>',
            table(headers,[cells(w,k,s) for ww,k,s in sorted(counts) if ww==w and s!='all']),'</details>'])
    parts.append('<p>Sämtliche Einzelwerte einschließlich Tagesergebnissen und PF8/25/75 stehen unter „Alle Konfigurationen“. Die ausgewählten Block-/Axis-Varianten und deren kombinierte Equity stehen im Zusatzstrategie-Abschnitt.</p></section>')
    return ''.join(parts)
