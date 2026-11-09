#!/usr/bin/env python3
"""Compare the three projects' negative-window arithmetic; no strategy-performance claim."""
import argparse,json,random,subprocess,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server/pulse'))
from lane_policy import negative_filter,negative_filter_ok

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--cts-a',required=True);ap.add_argument('--cts-ao',required=True);ap.add_argument('--out',required=True);a=ap.parse_args()
    rng=random.Random(9911);cases=[]
    for n in [1,2,3,5,8,10,12,15,20,25,30,40,50,60,80,100,150,200]:
        for mode in ['any','all']:
            for threshold in [0,.25,.5,.75,1]:
                for count in [0,max(0,n-1),n,n+4]:
                    for unknown in [False,True]:
                        values=[rng.choice([-4,-2,-1,0,1,2,4])/1024 for _ in range(count)]
                        if unknown and values:values[0]=None
                        p={'windows':sorted(set([n,max(1,n//2)])),'mode':mode,'maxPf':threshold}
                        cases.append({'values':values,'policy':p})
    expected=[negative_filter_ok(list(reversed(c['values'])),negative_filter(c['policy'])) for c in cases]
    modules=[Path(a.cts_a)/'src/lib/desk/negative-last-n.ts',Path(a.cts_ao)/'src/core/negative-last-n.ts']
    assert modules[0].read_bytes()==modules[1].read_bytes(),'shared rule implementations diverged'
    js="""import fs from 'node:fs';import {pathToFileURL} from 'node:url';
const m=await import(pathToFileURL(process.argv[1]));const a=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(a.map(x=>m.negativeLastNDecision(x.values,m.negativeLastN(x.policy)).pass)));"""
    results=[]
    for module in modules:
        p=subprocess.run(['node','--experimental-strip-types','--input-type=module','-e',js,str(module.resolve())],input=json.dumps(cases),capture_output=True,text=True,check=True)
        actual=json.loads(p.stdout);bad=[i for i,(x,y) in enumerate(zip(expected,actual)) if x!=y]
        assert len(actual)==len(expected) and not bad,(module,bad[:5])
        results.append({'module':str(module.relative_to(module.parents[3] if 'desk' in module.parts else module.parents[2])),'cases':len(cases),'mismatches':len(bad)})
    result={'basis':'Same already-net own-parent observations; arithmetic parity only, no execution profitability claim','checks':sum(x['cases'] for x in results),'projects':results}
    Path(a.out).write_text(json.dumps(result,indent=2));print(json.dumps(result))
if __name__=='__main__':main()
