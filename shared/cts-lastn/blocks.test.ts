import test from 'node:test';
import assert from 'node:assert/strict';
import {blockPolicies,blockPrefix,evaluateBlocks,blockPolicyKey,DEFAULT_BLOCK_POLICY,LONG_COUNTS,SHORT_COUNTS,MINIMAL_COUNTS} from './blocks.ts';
test('20/10/3 uses 33 disjoint closes and a negative tail cannot contaminate the long PF',()=>{
  const v=[-1,-1,-1,...Array(10).fill(-2),...Array(20).fill(3)];
  const p={...DEFAULT_BLOCK_POLICY,shortPolarity:'negative' as const,minimalPolarity:'negative' as const};
  const r=evaluateBlocks(blockPrefix(v),p,1.1);
  assert.equal(r.required,33);assert.equal(r.state,'qualified');assert.equal(r.long.net,60);assert.equal(r.short.net,-20);assert.equal(r.minimal.net,-3);
  assert.deepEqual([r.minimal.offset,r.short.offset,r.long.offset],[0,3,13]);
  assert.equal(evaluateBlocks(blockPrefix(v.slice(0,32)),p,1.1).state,'insufficient');
  v[4]=NaN;assert.equal(evaluateBlocks(blockPrefix(v),p,1.1).state,'unknown');
});
test('all requested signed combinations are distinct and agree with an independent scalar slicing oracle',()=>{
  assert.deepEqual(LONG_COUNTS,[10,15,20,25,30,35,40,45,50,55,60,65,70]);assert.deepEqual(SHORT_COUNTS,[5,8,11,14,17,20]);assert.deepEqual(MINIMAL_COUNTS,[1,2,3,4,5]);
  const policies=[...blockPolicies()];assert.equal(policies.length,1560);assert.equal(new Set(policies.map(blockPolicyKey)).size,1560);
  let seed=17;const rnd=()=>((seed=(1664525*seed+1013904223)>>>0)/2**32);
  for(let run=0;run<20;run++){
    const values=Array.from({length:95},()=>Math.floor(rnd()*19)-8),pre=blockPrefix(values);
    for(const p of policies){
      const r=evaluateBlocks(pre,p,1.1);
      for(const [name,offset,n] of [['long',p.short+p.minimal,p.long],['short',p.minimal,p.short],['minimal',0,p.minimal]] as const){
        const a=values.slice(offset,offset+n),gp=a.reduce((s,v)=>s+Math.max(v,0),0),gl=a.reduce((s,v)=>s+Math.max(-v,0),0);
        assert.equal(r[name].gp,gp);assert.equal(r[name].gl,gl);assert.equal(r[name].count,n);
      }
      const positive=(a:number[])=>{const gp=a.reduce((s,v)=>s+Math.max(v,0),0),gl=a.reduce((s,v)=>s+Math.max(-v,0),0);return gp>gl&&(gl===0||gp/gl>=1.1)};
      const long=values.slice(p.short+p.minimal,p.short+p.minimal+p.long),short=values.slice(p.minimal,p.minimal+p.short),minimal=values.slice(0,p.minimal);
      const pass=positive(long)&&(p.shortPolarity==='positive'?positive(short):short.reduce((a,b)=>a+b,0)<0)&&(p.minimalPolarity==='positive'?minimal.reduce((a,b)=>a+b,0)>0:minimal.reduce((a,b)=>a+b,0)<0);
      assert.equal(r.state==='qualified',pass);
    }
  }
});
test('zero, unknown, insufficient and signed alternatives remain separate; sizing does not alter unit PF',()=>{
  const p={...DEFAULT_BLOCK_POLICY};
  assert.equal(evaluateBlocks(blockPrefix(Array(33).fill(0)),p,1.1).state,'failed');
  assert.equal(evaluateBlocks(blockPrefix([null,...Array(32).fill(2)]),p,1.1).state,'unknown');
  const values=Array.from({length:33},(_,i)=>i%4===0?-1:2);
  const a=evaluateBlocks(blockPrefix(values),p,1.1),b=evaluateBlocks(blockPrefix(values.map(x=>x*3)),p,1.1);
  assert.equal(a.long.pf,b.long.pf);assert.equal(a.state,b.state);
});
