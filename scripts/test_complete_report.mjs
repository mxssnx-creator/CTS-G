// Data and UI-event contracts only; this does not claim a browser render.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { gunzipSync } from 'node:zlib';
const html=fs.readFileSync(process.argv[2],'utf8');
class Element{children=[];textContent='';value='';append(child){this.children.push(child)}replaceChildren(){this.children=[]}click(){return this.onclick?.()}}
const ids=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element()]));
for(const match of html.matchAll(/<script type="[^"]+" id="([^"]+)">([\s\S]*?)<\/script>/g))ids.get(match[1]).textContent=match[2];
const summaries=JSON.parse(ids.get('summaries').textContent),grids=JSON.parse(ids.get('grids').textContent);
assert.equal(summaries.length,120);assert.equal(new Set(summaries.map(s=>s.key)).size,120);
let count=0;
for(const s of summaries){
 const d=JSON.parse(gunzipSync(Buffer.from(ids.get('data-'+s.key).textContent,'base64')));
 assert.equal(d.rows.length,s.expectedRows);assert.equal(s.rows,2*grids[s.family].length);
 const index=Object.fromEntries(d.columns.map((k,i)=>[k,i]));
 const unique=new Set();
 for(const row of d.rows){
  unique.add(row[index.config]+':'+row[index.direction]);
  assert.equal(row[index.dailyN].reduce((a,b)=>a+b,0),row[index.n]);
  assert.ok(Math.abs(row[index.dailyNetPct].reduce((a,b)=>a+b,0)-row[index.netPct])<.00003);
  assert.ok(row[index.wins]+row[index.losses]<=row[index.n]);
 }
 assert.equal(unique.size,s.expectedRows);count+=d.rows.length;
}
assert.equal(count,2502720);
let downloaded;ids.get('sort').value='netPct';
const context=vm.createContext({document:{getElementById:id=>ids.get(id),createElement:()=>new Element()},
 Blob,Response,DecompressionStream,atob,Uint8Array,
 URL:{createObjectURL:b=>{downloaded=b;return 'blob:test'},revokeObjectURL(){}},setTimeout:fn=>fn()});
const script=html.match(/<script>\s*(const el=[\s\S]*?)<\/script>/)[1];
vm.runInContext(script,context,{timeout:10000});
ids.get('group').value=summaries.find(s=>s.family==='adjustments').key;
await ids.get('load').click();assert.equal(ids.get('rows').children.length,50);
ids.get('next').click();assert.match(ids.get('count').textContent,/Seite 2/);
ids.get('strategy').value='base';ids.get('strategy').onchange();assert.match(ids.get('count').textContent,/162 \/ 8910/);
ids.get('rows').children[0].children[0].children[0].click();
assert.equal(JSON.parse(ids.get('details').textContent).parameters.strategy,'base');
ids.get('csv').click();assert.equal((await downloaded.text()).split('\r\n').length,163);
ids.get('status').value='qualified';ids.get('status').onchange();assert.equal(ids.get('rows').children.length,0);
ids.get('group').value=summaries.find(s=>s.family==='catalog').key;
ids.get('strategy').value='trail';ids.get('status').value='';await ids.get('load').click();
assert.match(ids.get('count').textContent,/66000 \/ 68640/);
console.log(JSON.stringify({pass:true,groups:120,rows:count,checks:['unique Cartesian identities','all daily totals','compressed loading','pagination','filters','details','CSV','empty results','catalog TP-on/off trails'],visualBrowserVerified:false}));
