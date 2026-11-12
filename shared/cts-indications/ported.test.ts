import {test} from "node:test";
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {readFileSync} from "node:fs";
import {portedStates,PORTED_INDICATIONS,portedBars,StreamMath} from "./index.ts";
import {SeriesCache} from "./origin/indications/cache.ts";
import {dcaTacticConfigs,dcaTacticId,dcaTacticState} from "./origin/indications/dca-tactic.ts";
import {aggregateClosedBars,alignedSourceStates} from "./aligned.ts";
import {waveBars,declineBars} from "./fixtures.ts";

test('all 2918 imported source identities match the CTS-A-O golden states',()=>{
 const bars=waveBars(),peers=[{symbol:'BTC-USDT',bars:waveBars().map(b=>({...b,close:b.close*.99,open:b.open*.99,high:b.high*.99,low:b.low*.99}))},{symbol:'ETH-USDT',bars:waveBars()}];
 const result=portedStates('SOL-USDT',1,bars,PORTED_INDICATIONS.map(s=>s.id),peers);
 const golden=JSON.parse(readFileSync(new URL('./golden.json',import.meta.url),'utf8'));
 assert.equal(PORTED_INDICATIONS.filter(s=>s.kind==='inverse').length,604);assert.equal(dcaTacticConfigs().length,2304);
 assert.equal(new Set(PORTED_INDICATIONS.map(s=>s.id)).size,2918);
 for(const s of PORTED_INDICATIONS)assert.equal(createHash('sha256').update(result.states[s.id]).digest('hex'),golden.states[s.id],s.id);
});
test('decline levels and recovery ratios are independent, mirrored before entry, and preserve all existing Tactics',()=>{
 for(const method of ['bounce','trend','vwap','sweep'] as const){
  const c={lookback:8,basis:'percent' as const,spacing:.5,level:2,recovery:.5,method};
  assert.equal(dcaTacticState(new SeriesCache(portedBars('SOL-USDT',1,declineBars())),c).at(-1),1,method);
  assert.equal(dcaTacticState(new SeriesCache(portedBars('SOL-USDT',1,declineBars(true))),c).at(-1),-1,method+' mirror');
  assert.equal(dcaTacticState(new SeriesCache(portedBars('SOL-USDT',1,declineBars())),{...c,level:1}).at(-1),0);
 }
 assert.equal(PORTED_INDICATIONS.filter(s=>s.kind==='tactic'&&!s.id.startsWith('tactic-dca-')).length,10);
 assert.throws(()=>dcaTacticConfigs({lookbacks:[8],bases:['atr'],spacings:[0],levels:[1],recoveries:[.5],methods:['bounce']}));
});
test('source calculations are causal and append-stream states equal the full oracle',()=>{
 const rows=waveBars(240),ids=[...dcaTacticConfigs().map(dcaTacticId),'inverse-known-ema-9-21','inverse-zscore-20'];
 const full=portedStates('SOL-USDT',1,rows,ids),stream=new StreamMath();
 for(const n of [80,96,160,201,240]){
  const prefix=portedStates('SOL-USDT',1,rows.slice(0,n),ids,[],stream);
  for(const id of ids)assert.deepEqual(prefix.states[id],full.states[id].slice(0,n),id+' prefix '+n);
 }
 const known=portedStates('SOL-USDT',1,Array.from({length:120},(_,i)=>({timestamp:rows[0].timestamp+i*60000,open:100+i,high:100.5+i,low:99.5+i,close:100+i,volume:10})),['inverse-known-ema-9-21']);
 assert.equal(known.states['inverse-known-ema-9-21'].at(-1),-1,'rising EMA is inverted before entry, never a return sign flip');
});
test('gaps reset warmup; incomplete higher candles, missing references, and unsupported lanes never invent signals',()=>{
 const rows=waveBars(240),ids=['inverse-known-ema-9-21','inverse-pair-btc-spread-20'];
 const missing=portedStates('SOL-USDT',1,rows,[ids[1]]);assert.equal(missing.available[ids[1]],false);assert.ok(missing.states[ids[1]].every(x=>x===0));
 const gap=rows.map((b,i)=>({...b,timestamp:b.timestamp+(i>=120?60000:0)}));
 const g=portedStates('SOL-USDT',1,gap,[ids[0]]);assert.ok(g.states[ids[0]].slice(120,200).every(x=>x===0));
 const higher=aggregateClosedBars(rows.slice(0,239),5);assert.ok(higher.bars.length<239/5);assert.equal(higher.at.at(-1)!<238,true);
 const aligned=alignedSourceStates('SOL-USDT',rows,[{name:'test',id:ids[0],tf:5}]);assert.ok(aligned.states.test.every((v,i)=>!v||rows[i].timestamp%300000===240000));
 assert.equal(portedStates('SOL-USDT',30,aggregateClosedBars(rows,30).bars,[ids[0]]).available[ids[0]],false);
 assert.throws(()=>portedStates('SOL-USDT',1,rows,['invented-source']));
 assert.throws(()=>portedBars('SOL-USDT',1,[rows[1],rows[0]]));
});
