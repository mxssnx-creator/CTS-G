import json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
p=root/'evidence/history-readiness-20261110-final/results'
a=json.loads((p/'summary.json').read_text())
by={}
for row in a:
    key=(row['range'],row['baseN'],row['mainN'],row['realN'],row['mode'])
    by.setdefault(key,{})[row['prehistoryHours']]=json.loads((p/(row['id']+'.json')).read_text())
pairs=[]
for key,variants in by.items():
    if set(variants)!={24,48}:continue
    row={'range':key[0],'baseN':key[1],'mainN':key[2],'realN':key[3],'mode':key[4]}
    for pre,r in variants.items():
        row[str(pre)]={'closed':r['closed'],'halves':r['halves'],'unitDrawdownPct':r['unitDrawdownPct'],'passesScreen':r['passesScreen'],
            'first':r['stages'][0], 'lastScored':r['stages'][-1]}
        for at in ['first','lastScored']:row[str(pre)][at]={k:v for k,v in row[str(pre)][at].items() if k!='settled'}
        row[str(pre)]['setHourCoverage']={}
        for name in ['base','main','real']:
            row[str(pre)]['setHourCoverage'][name]={}
            for pop in ['all','eligible']:
                population=sum(s['sampleCoverage'][name][pop]['population'] for s in r['stages'])
                full=sum(s['sampleCoverage'][name][pop]['full'] for s in r['stages'])
                row[str(pre)]['setHourCoverage'][name][pop]={'population':population,'full':full,'pct':100*full/population if population else None}
    pairs.append(row)
result={'scope':'Paired exact configurations,24h vs48h prehistory; sample percentages separate from PF. setHourCoverage counts repeated hourly observations, not unique physical positions.',
        'cases':len(a),'pairs':len(pairs),'passedScreen':sum(x['passesScreen'] for x in a),'closedOccurrences':sum(x['closed']['n'] for x in a),'rows':pairs}
(p/'paired-comparisons.json').write_text(json.dumps(result,indent=2,allow_nan=False))
print(json.dumps({k:v for k,v in result.items() if k!='rows'}))
for row in pairs:
    if row['baseN'] in [10,30,80] and row['mode']=='positive':
        print(row['range'],row['baseN'],row['mainN'],row['realN'], 'ready0', [(pre,{stage:row[str(pre)]['first']['sampleCoverage'][stage]['all']['pct'] for stage in ['base','main','real']}) for pre in [24,48]], 'closed', [(pre,row[str(pre)]['closed']['n'],row[str(pre)]['closed']['pf']) for pre in [24,48]])
