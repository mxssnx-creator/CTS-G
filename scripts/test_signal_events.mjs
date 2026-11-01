import test from 'node:test';
import assert from 'node:assert/strict';
import { catalogue,aggregate,sourceEvents } from '../server/signals/source-events.mjs';
import { stdev,bollinger,cci } from '../server/signals/ao/math/indicators.ts';

const t0=1791460800000;
const bars=Array.from({length:1200},(_,i)=>{
  const c=100+5*Math.sin(i/13)+2*Math.cos(i/7);
  return [t0+i*60000,c,c+.8,c-.8,c,1+i%7];
});

test('all 103 actual catalogue sources expose both source ranges and every timeframe',()=>{
  const out=sourceEvents({A:bars});
  assert.equal(catalogue.length,103);
  assert.equal(new Set(catalogue.map(s=>s.name)).size,103);
  assert.equal(out.series.length,(103*2+15)*4);
  assert.deepEqual(out.strategyRanges,['signals','exit']);
  for(const s of out.series) {
    assert.ok(['short','medium','combined'].includes(s.identity.sourceRange));
    assert.ok(s.events.every(([t,v])=>t%(s.identity.tf*60000)===0 && [-1,1].includes(v)));
  }
});

test('full source catalogue and coordinations are causal under a truncated or altered future',()=>{
  const full=sourceEvents({A:bars}),past=sourceEvents({A:bars.slice(0,600)});
  assert.equal(full.series.length,past.series.length);
  for(let i=0;i<full.series.length;i++) {
    assert.deepEqual(past.series[i].identity,full.series[i].identity);
    assert.deepEqual(past.series[i].events,full.series[i].events.filter(([t])=>t<=t0+600*60000));
  }
});

test('no incomplete candle, duplicate minute, gap or unrecognized source is accepted',()=>{
  assert.equal(aggregate(bars.slice(1,10),5).length,1);
  assert.equal(aggregate(bars.slice(0,4),5).length,0);
  assert.throws(()=>aggregate([bars[0],bars[2]],1));
  assert.throws(()=>aggregate([bars[0],bars[0]],1));
  assert.throws(()=>sourceEvents({A:bars},{sources:['invented-source']}));
  assert.throws(()=>sourceEvents({A:bars},{sources:['ema-cross','ema-cross']}));
});

test('source histories cannot be changed by another symbol',()=>{
  const first=sourceEvents({A:bars},{sources:['ema-cross','macd-cross','rsi-reversal']});
  const both=sourceEvents({A:bars,B:bars.map(r=>[r[0],...r.slice(1,5).map(x=>x*2),r[5]])},{sources:['ema-cross','macd-cross','rsi-reversal']});
  assert.deepEqual(first.series,both.series.filter(s=>s.identity.symbol==='A'));
});

test('near-flat catalogue variance and CCI retain small movements without phantom flat signals',()=>{
  const step=2**-30;
  const values=Float64Array.from([100,100+step,100+2*step]);
  assert.ok(Math.abs(stdev(values,3)[2]/(step*Math.sqrt(2/3))-1)<1e-12);
  const flat=new Float64Array(1200).fill(100.1);
  const bands=bollinger(flat,20),channel=cci(flat,flat,flat,20);
  for(let i=19;i<flat.length;i++) {
    assert.equal(bands.mid[i],100.1);assert.equal(bands.up[i],100.1);assert.equal(bands.lo[i],100.1);
    assert.equal(channel[i],0);
  }
  const basic=Float64Array.from([1,2,3]);
  assert.ok(Math.abs(cci(basic,basic,basic,3)[2]-100)<1e-10);
});
