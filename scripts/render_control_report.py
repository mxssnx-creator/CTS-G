"""Human-readable control audit. Rendering never changes computed evidence."""
from collections import Counter
import datetime as dt
import hashlib
import html
import json
import pathlib

from sweep_seven_days import configs, POLICIES, POLICY_KEYS


def render(results, out, signature, rule):
    results=sorted(results,key=lambda r:r['key'])
    cfg=configs();pol=[dict(zip(POLICY_KEYS,map(int,p))) for p in POLICIES]
    selected=[];best=[];summary=[];sensitivity=Counter();outcomes=Counter()
    for r in results:
        selected.extend(dict(symbol=r['symbol'],kind=r['kind'],direction=r['direction'],**row) for row in r['trainingSelections'])
        best.extend(dict(symbol=r['symbol'],kind=r['kind'],direction=r['direction'],**row) for row in r['top'])
        outcomes.update(r['outcomes'])
        for row in r['sensitivity']:sensitivity[(row['controlN'],row['controlPf'])]+=row['qualified']
    status=Counter();unique_status=Counter()
    for r in selected:
        status[r['status']]+=len(r['aliases']);unique_status[r['status']]+=1
    for symbol in ('BCH-USDT','SOL-USDT','XRP-USDT'):
        groups=[r for r in results if r['symbol']==symbol]
        chosen=[r for r in selected if r['symbol']==symbol]
        summary.append(dict(symbol=symbol,**{k:sum(r[k] for r in groups) for k in ('tested','positive','traded','trainingQualified','strictQualified','qualified')},
            selectedConfigs=sum(len(r['aliases']) for r in chosen),
            selectedQualified=sum(len(r['aliases']) for r in chosen if r['qualified']),
            selectedUniqueQualified=sum(r['qualified'] for r in chosen)))
    qualified=sorted((r for r in selected if r['qualified']),key=lambda r:(-r['metrics']['net'],r['symbol'],r['kind'],r['config'],r['policy']))
    observed=[r for r in qualified if r['metrics']['testN']>=5 and r['metrics']['testNet']>1e-12 and 1+.1*r['metrics']['testCostRSum']/r['metrics']['testN']>1.+1e-9]
    best=sorted(best,key=lambda r:(not r['qualified'],-r['metrics']['net'],r['symbol'],r['config']))[:500]
    windows=[]
    for last_n in range(5,76,5):
        for deact in range(5,26,5):
            rows=[r for r in selected if pol[r['policy']]['lastN']==last_n and pol[r['policy']]['deactivationN']==deact]
            if rows:
                windows.append(dict(lastN=last_n,deactivationN=deact,selected=len(rows),
                    qualified=sum(r['qualified'] for r in rows),controlPositive=sum(r['metrics']['testN']>0 and r['metrics']['testNet']>0 for r in rows),
                    insufficient=sum(r['status']=='control-insufficient' for r in rows)))
    payload=dict(signature=signature,renderHash=hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
        controlRule=rule.settings(),summary=summary,outcomes=dict(outcomes),selectedOutcomes=dict(status),
        uniqueSelectedOutcomes=dict(unique_status),windowComparison=windows,
        sensitivity=[dict(controlN=k[0],controlPf=k[1],qualified=v) for k,v in sorted(sensitivity.items())],
        configs=cfg,policies=pol,best=best,trainingSelectedBest=qualified[:500],
        observedControl5Best=observed[:500],observedControl5Count=len(observed),controlChecked=bool(rule.control_n),
        defaults=dict(controlMinTrades=rule.control_n,controlMinPf=rule.control_pf,baseLastN=30,deactivationN=25,
                      runtimeWindowChange=False,reason='Revised control uses the already inspected period; fresh evidence is needed before claiming an optimized trading default.'),
        note='All independent training-qualified variants are counted. One policy per risk configuration is selected from training only; group winners are not an admission gate. Full selections are retained in the compressed group files.')
    (out/'summary.json').write_text(json.dumps(payload,separators=(',',':'),allow_nan=False))
    def table(headers, rows):
        return '<div class="scroll" tabindex="0"><table><thead><tr>'+''.join('<th>'+html.escape(str(h))+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in r)+'</tr>' for r in rows)+'</tbody></table></div>'
    def f(v,places=4):return f'{v:.{places}f}' if v is not None else '—'
    def pf(m,prefix):
        n=m[prefix+'N'];return 1+.1*m[prefix+'CostRSum']/n if n else None
    def rows_table(rows):
        output=[]
        for r in rows:
            c=cfg[r['config']];p=pol[r['policy']];m=r['metrics']
            output.append([r['symbol'],r['kind'],r['direction'],c['strategy'],
                f"{c['tpPct']:.2f}/{c['slPct']:.2f}",f"{c.get('trailArmPct',0):.1f}/{c.get('trailGivePct',0):.1f}",
                'an' if c['honorTp'] else 'aus',f"{c['levels']}/{c['incrementPct']}/{c['volumeRatio']}",
                p['lastN'],p['deactivationN'],p['maxDdtHours'],p['recalcAfterCloses'],f"{p['mainN']}/{p['realN']}",
                int(m['trainN']),f(m['trainNet']*100),f(pf(m,'train')),int(m['testN']),f(m['testNet']*100),f(pf(m,'test')),
                int(m['n']),f(m['net']*100),f(m['cost']*100),f(r['costPf']),f(r.get('classicPf')),
                f(m['maxDd']*100),f(m['maxDdtBars']/60,2),'ja' if m['disabled'] else 'nein'])
        return table(['Symbol','Indikation','Seite','Strategie','TP/SL %','Trail Arm/Give %','Festes TP','Add: Stufen/Abstand %/Ratio',
          'Last N','Deact N','DDT h','Recalc N','Main/Real','Train N','Train netto pp','Train CTS-PF',
          'Kontrolle N','Kontrolle netto pp','Kontrolle CTS-PF','Trades','Netto pp','Kosten pp','CTS-PF','Klassischer PF','DD pp','DDT max h','Sitzung deaktiviert'],output)
    totals=table(['Symbol','Varianten','Netto positiv','Training qualifiziert','Strikt 8 / >1,02','Zugelassen (Kontrollregel)','Eigene Trainingswahl: zugelassen','Davon eindeutige Preisparameter'],
        [[r[k] for k in ('symbol','tested','positive','trainingQualified','strictQualified','qualified','selectedQualified','selectedUniqueQualified')] for r in summary])
    sensitivity_table=table(['Mindestanzahl Kontrolle','Kontroll-PF strikt größer als','Qualifizierte Varianten'],[[0,'aus – nur Training',sum(r['trainingQualified'] for r in summary)]]+[[n,pf,v] for (n,pf),v in sorted(sensitivity.items())])
    state_table=table(['Status nach eigener Trainingsauswahl','Konfigurations-IDs','Eindeutige Preisparameter'],[[k,status[k],unique_status[k]] for k in sorted(status)])
    window_table=table(['Last N','Deact N','Unabhängig im Training ausgewählt','Zugelassen','Kontrolle netto positiv, beliebige Anzahl','Kontrolle unvollständig'],
        [[r[k] for k in ('lastN','deactivationN','selected','qualified','controlPositive','insufficient')] for r in sorted(windows,key=lambda r:(-r['qualified'],r['lastN'],r['deactivationN']))])
    total=sum(r['tested'] for r in summary);old=sum(r['strictQualified'] for r in summary);new=sum(r['qualified'] for r in summary)
    content=f'''<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CTS-G · Kontrolltest und unabhängige Kandidaten</title>
<style>body{{margin:0;background:#10151c;color:#e6edf4;font:14px/1.55 system-ui}}main{{max-width:1600px;margin:auto;padding:24px}}h1{{font-size:30px}}section{{background:#19232e;border:1px solid #344758;border-radius:12px;margin:20px 0;padding:20px}}p{{max-width:1150px;overflow-wrap:anywhere}}.scroll{{overflow:auto;max-height:680px}}table{{border-collapse:collapse;white-space:nowrap;width:100%}}th,td{{padding:9px 12px;text-align:right;border-bottom:1px solid #344758}}th{{color:#96caeb}}th:first-child,td:first-child{{text-align:left}}code{{overflow-wrap:anywhere}}</style><main>
<h1>CTS-G · Kontrolltest korrigiert</h1><p>04.–11. September 2026 UTC · BCH, SOL, XRP · sieben Tage · Axis aus.</p>
<section><h2>Ergebnisübersicht</h2><p>{total:,} alternative Versuche; strenge Kontrolle: {old:,}; Zulassung mit Kontrolle {rule.control_n}: {new:,}. Nach eigener Auswahl ausschließlich anhand des Trainings sind {sum(r['selectedQualified'] for r in summary):,} Konfigurations-IDs zugelassen. {len(observed):,} eindeutige Trainingsauswahlen haben zusätzlich mindestens fünf Kontrollabschlüsse mit positivem Netto und CTS-PF &gt; 1,00.</p>{totals}
<p>Die Gesamtsuche zählt jede Alternative. Davon getrennt wählt jede Risikokonfiguration ihre Policy ausschließlich anhand positiver Trainingsdaten. Die Kontrollwerte beeinflussen diese Wahl nicht. Gleichwertige TP-Parameter können mehrere angeforderte Schritt-IDs haben; beide Zählweisen stehen ausdrücklich in der Tabelle. Es sind keine gleichzeitig geöffneten Live-Orders.</p></section>
<section><h2>Was am bisherigen Kontrolltest falsch war</h2><p>Nur ein Trainingsgewinner je Symbol/Indikation/Seite wurde für Defaults untersucht. Dabei gingen eigenständig gute Konfigurationen verloren; neun der 37 ausgewählten Gewinner hatten sogar negatives Training. Alle 37 hatten weniger als acht Kontrollabschlüsse, 33 überhaupt keinen. Eine Variante mit sechs Kontroll-Trades, positivem Netto und PF 1,165 scheiterte ausschließlich an der Anzahl.</p>
<p>Jetzt gelten je Konfiguration mindestens {rule.training_n} Trainingsabschlüsse, Trainings-Netto &gt; 0 und Trainings-CTS-PF &gt; {rule.training_pf:.2f}. Die zusätzliche Kontrollfreigabe steht auf <b>{rule.control_n}</b>; <b>0 = aus</b>. Bei 0 beeinflussen weder Anzahl noch PF oder Netto der Kontrolle die Zulassung. Bei einer positiven Mindestanzahl gelten zusätzlich Kontroll-Netto &gt; 0 und CTS-PF &gt; {rule.control_pf:.2f}. Ohne Kontrollabschlüsse gibt es keinen Kontrollnachweis. Eine negative Kontrollbilanz bleibt negativ.</p>
<p>Zusätzlich prüft der Ergebnis-Cache nun Kerzeninhalt und Kontrollregel; geänderte Daten können keine alten Resultate zurückgeben. Im separaten Forced-Baseline-Replay werden offene Positionen an Split und Ende mit Kosten abgerechnet, statt deren Ergebnis zu verlieren.</p></section>
<section><h2>Transparenter Vergleich der Toleranz</h2>{sensitivity_table}<p>Alle Regeln sehen dieselben tatsächlich zugelassenen Replay-Trades. Die Änderung der Kontrolle erzeugt keine zusätzlichen Gewinne und ändert keine Entry-Gates. Der 3-Trade-Vergleich zeigt ausdrücklich die schwächere Stichprobe; der Standard ist 0 = aus. Zulassung ohne Kontrollprüfung ist kein positiver Kontrollnachweis.</p>{state_table}</section>
<section><h2>Beste 50 im Training gewählte, zugelassene Konfigurationen</h2><p>Je Risikokonfiguration ist die Policy vor Betrachtung der Kontrolle festgelegt. Die folgende Reihenfolge nach Gesamtnetto ist eine nachträgliche Übersicht.</p>{rows_table(qualified[:50])}</section>
<section><h2>Positives Kontrollergebnis bei mindestens fünf Abschlüssen</h2><p>Diese {len(observed):,} eindeutigen Trainingsauswahlen haben nach Kosten positive Kontrollbilanz und CTS-PF &gt; 1,00. Das ist eine optionale Auswertung derselben bereits untersuchten Periode.</p>{rows_table(observed[:50])}</section>
<section><h2>Last-N und Deaktivierungsfenster</h2>{window_table}<p>Diese Gruppen enthalten unterschiedliche Trainingsauswahlen. Ihre Summen sind keine gemeinsame Kontorendite und kein kausaler Beweis, dass ein Fenster für jede Strategie besser ist.</p></section>
<section><h2>Beste beobachtete Varianten der Gesamtsuche</h2><p>Explorative Auswahl über die komplette Periode; keine unabhängige Trainingsauswahl.</p>{rows_table(best[:50])}</section>
<section><h2>Matrix, Kosten und Aussagegrenzen</h2><p>Je Symbol acht Indikationen, beide Richtungen, 7.920 Risikokonfigurationen und 3.375 Policies: Last-N 5–75/5, Deaktivierung 5–25/5, DDT 1–9h/2h, Recalc nach 1/5/10 Abschlüssen und Main/Real 5/3, 10/5, 15/10. TP-Schritte 1–30 × 0,1% mit Floor 0,3%; SL 0,2/0,4/0,6/0,8%. Normal, Trailing mit 25 Arm/Give-Paaren und TP an/aus; Block 1–6, DCA 1–3 × Abstand 0,05/0,1/0,2%, Add-Ratio 0,25.</p>
<p>Entry nur anhand älterer Shadow-Abschlüsse mit Base/Main/Real-PF &gt; 1,02. Signal bei Kerzenschluss, Stop vor TP, Stop-Gaps zum schlechteren Open, kein Wiedereinstieg in derselben Exit-Kerze. Trailing gilt ab Folgekerze. Jede Ausführung kostet 0,05% des ausgeführten Notionals. CTS-PF = 1 + 0,1 × Mittel(Netto / individuelle Kosten); klassischer PF = positive Nettoergebnisse / Betrag negativer Nettoergebnisse. Ohne Verlust ist der klassische PF unbeschränkt und als — dargestellt.</p>
<p>Kein Funding-, Orderbuch- oder Latenzmodell. DD/DDT aus abgeschlossenen zugelassenen Ergebnissen; offene Intratrade-Verluste sind darin nicht enthalten. Nach negativer Deactivation-N-Bilanz bleibt diese Sitzung deaktiviert, während Shadow-Berechnungen weiterlaufen. Das kann zu fehlenden Kontroll-Trades führen und wird nicht als positiver Kontrollnachweis gewertet.</p>
<p>Die geänderte Kontrollregel wurde nach Sichtung dieses Zeitraums geändert. Ihre Ergebnisse sind eine erneute Untersuchung desselben Materials; ein neuer, unberührter Zeitraum bleibt für unabhängige Bestätigung nötig. Allgemeine Trading-Fenster bleiben Base 30 / Deaktivierung 25. Neuer Kontrollstandard: 0 = aus. Optional aktivierte Kontrolle: CTS-PF &gt; 1,00. Laufende VST-Tests verwenden weiterhin 20 Symbole.</p>
<p>Rechensignatur: <code>{signature}</code>. Vollständige Auswahl je Risikokonfiguration und alle Zähler sind in den 48 komprimierten Gruppen gespeichert; jeder vollständige Ergebnisvektor geht in den Rechendigest ein.</p></section></main></html>'''
    (out/'cts-g-control-audit.html').write_text(content)
    return summary
