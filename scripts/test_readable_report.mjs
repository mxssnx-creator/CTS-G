import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {gunzipSync} from 'node:zlib';
const html=fs.readFileSync(process.argv[2],'utf8'),source=process.argv[3],prior=process.argv[4];
class Element{children=[];textContent='';innerHTML='';value='';append(child){this.children.push(child);if(!this.value&&child.value)this.value=child.value}click(){return this.onclick?.()}}
const ids=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element()]));
for(const m of html.matchAll(/<script type="[^"]+" id="([^"]+)">([\s\S]*?)<\/script>/g))ids.get(m[1]).textContent=m[2];
const summaries=JSON.parse(ids.get('summaries').textContent),top=JSON.parse(ids.get('top-data').textContent);
assert.equal(summaries.length,120);assert.equal(top.length,25);
assert.equal(new Set(top.map(p=>p.candidate.identity)).size,25);
let total=0,additional=0;
for(const s of summaries){
 const d=JSON.parse(gunzipSync(Buffer.from(ids.get('data-'+s.key).textContent,'base64')));
 const metrics=d.metrics.map(a=>Object.fromEntries(d.metricColumns.map((k,i)=>[k,a[i]])));
 const rows=d.rows.map(([config,mid])=>{const r={config,...metrics[mid]};return d.columns.map(k=>r[k])});
 const original=JSON.parse(gunzipSync(fs.readFileSync(path.join(source,s.key+'.json.gz'))));
 assert.deepEqual(rows,original.rows);assert.deepEqual(d.columns,original.columns);
 if(prior){const previous=JSON.parse(gunzipSync(fs.readFileSync(path.join(prior,s.key+'.json.gz'))));assert.deepEqual(original,previous,'Full recalculation changed '+s.key)}
 assert.equal(rows.length,s.expectedRows);total+=rows.length;
}
assert.equal(total,2502720);
for(const p of top){
 const full=JSON.parse(gunzipSync(Buffer.from(ids.get('top-full-'+p.candidate.candidateId).textContent,'base64')));
 assert.equal(full.variants.length,988);assert.equal(new Set(full.variants.map(r=>r.variantId)).size,988);
 for(const r of full.variants){
  assert.equal(r.n,r.dailyN.reduce((a,b)=>a+b,0));
  assert.ok(Math.abs(r.netPct-r.dailyNetPct.reduce((a,b)=>a+b,0))<.0001);
  assert.ok(Math.abs(r.combinedNetPct-r.portfolioDailyNetPct.reduce((a,b)=>a+b,0))<.0001);
  assert.ok(Math.abs(r.combinedNetPct-r.combinedTrainNetPct-r.combinedHoldoutNetPct)<.0001);
  assert.equal(r.liveEligible,false);
  assert.ok(r.maxVolume<=r.parameters.entryVolumeRatio*(r.parameters.levels?2:1)+1e-9);
  if(r.parameters.axis&&r.parameters.admission==='strict')assert.equal(r.n,0,'Strict Base gate must stay closed on this data');
 }
 assert.ok(full.gateEvents.every(e=>e.closedThroughBar<e.effectiveBar));additional+=full.variants.length;
}
assert.equal(additional,24700);
const visible=html.replace(/<script[\s\S]*?<\/script>/g,'').replace(/<style[\s\S]*?<\/style>/g,'');
assert.ok(!/<pre\b/i.test(visible));assert.ok(!visible.includes('{"'));
assert.ok((visible.match(/<svg\b/g)||[]).length>=4);
let download;
const context=vm.createContext({document:{getElementById:id=>ids.get(id),createElement:()=>new Element()},
 Blob,Response,DecompressionStream,atob,Uint8Array,
 URL:{createObjectURL:b=>{download=b;return 'blob:test'},revokeObjectURL(){}},setTimeout:fn=>fn()});
ids.get('sort').value='netPct';
const script=html.match(/<script>\s*(const el=[\s\S]*?)<\/script>/)[1];
await vm.runInContext(script,context,{timeout:10000});
assert.match(ids.get('add-count').textContent,/494 Varianten/);
assert.match(ids.get('curve').innerHTML,/<polyline/);assert.equal((ids.get('curve').innerHTML.match(/<polyline/g)||[]).length,4);
ids.get('admission').value='isolated-mechanics';ids.get('mode').value='Axis';await ids.get('mode').onchange();
assert.match(ids.get('add-count').textContent,/25 Varianten/);
ids.get('add-rows').onclick({target:{closest:()=>({dataset:{add:'0'}})}});
assert.match(ids.get('add-details').innerHTML,/<table/);assert.ok(!ids.get('add-details').innerHTML.includes('{"'));
ids.get('add-csv').click();assert.equal((await download.text()).split('\r\n').length,26);
ids.get('runtime-section').value='Axis Entscheidungen der gewählten Konfiguration';await ids.get('runtime-load').click();assert.match(ids.get('runtime-details').innerHTML,/<details>/);
ids.get('group').value=summaries.find(s=>s.family==='adjustments').key;await ids.get('load').click();assert.equal((ids.get('rows').innerHTML.match(/<tr>/g)||[]).length,50);
ids.get('next').click();assert.match(ids.get('count').textContent,/Seite 2/);
ids.get('strategy').value='base';ids.get('strategy').onchange();assert.match(ids.get('count').textContent,/162 \/ 8.910/);
ids.get('rows').onclick({target:{closest:()=>({dataset:{detail:'0'}})}});assert.match(ids.get('details').innerHTML,/Tagesergebnisse/);assert.ok(!ids.get('details').innerHTML.includes('{"'));
ids.get('csv').click();const text=await download.text();assert.equal(text.split('\r\n').length,163);assert.deepEqual([...new Uint8Array(await download.arrayBuffer()).slice(0,3)],[239,187,191]);assert.ok(text.includes('dailyNetPct.0'));
ids.get('status').value='qualified';ids.get('status').onchange();assert.equal(ids.get('rows').innerHTML,'');
console.log(JSON.stringify({pass:true,matrixRows:total,additionalRows:additional,selected:25,fullRecalculationCompared:!!prior,visibleRawJson:false,staticCharts:4,interactiveCurves:4,checks:['all original values','full rerun equality','unique top25 identities','all additional daily totals','causal Axis evidence','scaled Block caps','strict gate preserved','matrix paging/filter/details/CSV','additional filters/details/CSV','runtime tables','diagram updates'],browserRenderVerified:false}));
