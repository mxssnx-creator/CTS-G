"""Readable Block Active supplement, including all 900 result rows."""
import json
import pathlib
import matplotlib.pyplot as plt


def section(evidence, out, table, fmt, esc, svg):
    path=pathlib.Path(evidence)/'block-active-results.json'
    if not path.exists():return ''
    data=json.loads(path.read_text())
    status=json.loads((path.parent/'block-active-verification.json').read_text())
    parents=data['results'];rows=[r for p in parents for r in p['results']]
    fig,ax=plt.subplots(figsize=(10,3.6))
    ax.plot([p['candidate']['rank'] for p in parents],
            [p['baseline']['trainPf'] for p in parents],color='#b91c1c',label='Trainings-PF der Referenz')
    ax.axhline(1.02,color='#0f766e',linestyle='--',label='Ergebnisprüfung: PF > 1,02')
    ax.set_xlabel('Trainingsrang der 50 Kandidaten');ax.set_ylabel('Klassischer Netto-PF')
    ax.grid(alpha=.3);ax.legend();plot=svg(fig,pathlib.Path(out).parent/'charts'/'block-active-training.svg')
    parts=['<section id="block-active"><h2>Block → Active · angepasste Exchange-Ausführung</h2>',
           '<div class="notice">Lokal implementiert und geprüft; Remote-Installation und vollständige Browser-/Schreibabnahme bleiben offen. X02 bleibt gestoppt. Die 50 Kandidaten sind Research-Kandidaten: '+fmt(data['qualified'],0)+' von 900 Varianten bestehen die Ergebnisprüfung. '+fmt(sum(r['n'] for r in rows),0)+' modellierte Abschlüsse. Ein Ergebnis von null bei gesperrten Einstiegen ist kein positiver Handelsnachweis.</div>',
           table(['Setting','Neuer Default','Wirkung'],[
               ['Block → Active','Enabled','Virtuelle Referenz, mindestens 45 Sekunden und 0,2 % Fortsetzung; qualifizierter zusätzlicher Mengenanteil'],
               ['Normal (General)','Disabled','Keine unadjustierte Entry-Order, einschließlich Forced-Baseline. General-/Indications-Berechnung läuft weiter.'],
               ['Max active Sets','50','Auswahl begrenzt qualifizierte aktive Kandidaten; keine Auffüllung mit Verlust-Sets. Explizit 0 bleibt unbegrenzt.'],
               ['Block Active Live / Real','Enabled','Beide Freigaben sind für Active-Einstiege erforderlich.'],
               ['Gesamte gleiche Symbolseite','Mengenabzug','Vorhandene und noch ausstehende Entry-/Add-Mengen werden vom Block-Ziel abgezogen. Kein Netting gegen andere Symbole oder fremde Orders.'],
               ['Schutz und Wiederanlauf','Fail closed','Reconciliation, Controls, eigene PF-/Samples-/Netto-/DDT-Prüfung und Overall-Koordination müssen bestehen. Virtuelle Beobachtung beginnt nach Neustart neu; bestätigte/pending Orders behalten ihre Identität.']]),
           '<p><strong>Menge:</strong> max(0; Referenzmenge × min(1; Count × Ratio) − vorhandene Menge − ausstehende Menge). Die normale Referenzmenge wird nicht ausgeführt. Exchange-Mindestmengen dürfen den berechneten Anteil nicht vergrößern. Auf Active-Positionen wird kein zweites Block-/DCA-Pyramidisieren gestartet.</p>',
           '<p><strong>Beispiel:</strong> Referenz 8 × Count 1 × Ratio 0,25 = Ziel 2. Bereits vorhanden 0,5 und ausstehend 0,75 → neue Order höchstens 0,75. Das ist ein Rechenbeispiel, kein Live-Fill.</p>',plot,
           '<h3>Prüfstatus dieses Änderungsstands</h3>',table(['Prüfung','Status','Grenze'],[[esc(x['name']),esc(x['result']),esc(x.get('detail',''))] for x in status['checks']]),
           '<h3>Alle 50 Kandidaten und 900 angepassten Varianten</h3><p>Je Kandidat Counts 1–6 × Ratios 0,25 / 0,5 / 1,0. Die normale Menge bleibt null; jede Variante ist eine unabhängige Alternative. Auswahl im 14-Tage-Training, Kontrolle in den anschließenden sechs Tagen. Die Daten waren bereits bekannt. Historische Shadow-Evidenz ersetzt im Experiment die Account-Koordination; Exchange-Mindestmengen, Funding, Latenz und ein gemeinsames Portfolio werden nicht simuliert. Die Minutenkerze prüft die Fortsetzung gröber als die Laufzeitregel.</p>']
    for parent in parents:
        c=parent['candidate'];b=parent['baseline']
        parts.append('<details class="active-parent"><summary>#'+str(c['rank'])+' · '+esc(c['symbol']+' · '+c['kind']+' · '+c['direction'])+' · 18 Varianten</summary>')
        parts.append(table(['Referenzparameter','Wert'],[[esc(k),fmt(v,6)] for k,v in c['parameters'].items()]))
        parts.append(table(['Training PF / N','Kontroll-PF / N','Referenz Netto pp','Real-freigegebene Minuten','Fortsetzungsminuten'],[[fmt(b['trainPf'],6)+' / '+fmt(b['trainN'],0),fmt(b['holdoutPf'],6)+' / '+fmt(b['holdoutN'],0),fmt(b['netPct'],6),fmt(parent['realQualifiedBars'],0),fmt(parent['continuationBars'],0)]]))
        parts.append(table(['Count','Ratio','Anteil','N','Netto pp','DD pp','Train PF / N','Kontroll-PF / N','Bestanden'],[[fmt(r['blockCount'],0),fmt(r['blockRatio']),fmt(r['adjustedVolume']),fmt(r['n'],0),fmt(r['netPct'],6),fmt(r['maxDrawdownPct'],6),fmt(r['trainPf'],6)+' / '+fmt(r['trainN'],0),fmt(r['holdoutPf'],6)+' / '+fmt(r['holdoutN'],0),fmt(r['qualified'])] for r in parent['results']]))
        for row in parent['results']:
            parts.append('<details class="active-result"><summary>Count '+str(row['blockCount'])+' · Ratio '+str(row['blockRatio'])+' · sämtliche Kennzahlen und Tage</summary>')
            parts.append(table(['Kennzahl','Wert'],[[esc(k),fmt(v,6)] for k,v in row.items() if not isinstance(v,(list,dict))]))
            parts.append(table(['Tag','Netto pp','Abschlüsse'],[[i+1,fmt(v,6),fmt(row['dailyN'][i],0)] for i,v in enumerate(row['dailyNetPct'])]))
            parts.append('</details>')
        parts.append('</details>')
    parts.append('<details><summary>Einzelne Funktionstests dieses Änderungsstands</summary>'+table(['Suite','Test','Status'],[[esc(v) for v in r] for r in status['tests']])+'</details></section>')
    return ''.join(parts)
