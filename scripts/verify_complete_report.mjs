// Execute the embedded report against a minimal DOM contract. This is a
// structural/script check, explicitly not a browser-render or CSS screenshot.
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const html=readFileSync(process.argv[2], 'utf8');
const dataText=html.match(/<script id="data" type="application\/json">([\s\S]*?)<\/script>/)?.[1];
assert.ok(dataText,'embedded evidence exists');
const evidence=JSON.parse(dataText),elements=new Map();
class Element {
  constructor(id){this.id=id;this.value=['stageGroup','stageHour','health'].includes(id)?'all':'';this.textContent='';this.markup='';this.dataset={};this.open=false;this.details=[];}
  set innerHTML(value){this.markup=value;this.details=[...value.matchAll(/<details\b/g)].map((_,i)=>new Element(this.id+'-detail-'+i));}
  get innerHTML(){return this.markup;}
  querySelectorAll(selector){return selector==='details'?this.details:[];}
  insertAdjacentHTML(_where,value){this.markup+=value;}
  click(){}
}
const element=id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id)};
element('data').textContent=dataText;
const document={getElementById:element,createElement:()=>new Element('created')};
const scripts=[...html.matchAll(/<script(?: [^>]*)?>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
const script=scripts.at(-1);
const context=vm.createContext({document,console,Blob,URL,setTimeout:()=>0});
new vm.Script(script,{filename:'embedded-report.js'}).runInContext(context,{timeout:30000});
const chartIds=['equity','drawdown','hourbars','orderschart','coverage','progress','symbolchart','axiscompare','blockcompare','roundscompare','dcacompare'];
for(const id of chartIds){assert.ok(element(id).innerHTML.includes('<svg'),`${id} chart generated`);assert.ok(!element(id).innerHTML.includes('NaN'),`${id} values finite`);}
assert.ok(element('hourtable').innerHTML.includes('<tbody>'));
assert.equal((element('hourtable').innerHTML.match(/<tr>/g)??[]).length,25,'all 24 hourly rows');
assert.equal((element('symboltable').innerHTML.match(/<tr>/g)??[]).length,51,'all 50 distinct symbols');
assert.ok(element('cards').innerHTML.includes('Enough Base samples'));
for(const panel of element('indicationlist').querySelectorAll('details')){panel.open=true;panel.ontoggle();}
if(evidence.breadth.indications.length){
  const count=(evidence.breadth.configurations??[]).filter(c=>c.pack===evidence.breadth.indications[0].group).length;
  assert.equal((element('ind0c').innerHTML.match(/<tr>/g)??[]).length,count+1,'every independent configuration in expanded first pack');
}
element('strategyHourGroup').value='0';element('strategyHourGroup').onchange();
element('search').value='unlikely-no-match';element('search').oninput();assert.equal(element('indicationlist').innerHTML,'');
element('search').value='';element('search').oninput();
assert.ok(element('count').textContent.startsWith(String(evidence.breadth.indications.length)));
element('stageHour').value=String(evidence.breadth.hourly[0].t);element('stageHour').onchange();
element('ledgerFilter').value='unlikely-no-match';element('ledgerFilter').oninput();
assert.equal((element('ledgertable').innerHTML.match(/<tr>/g)??[]).length,1);
assert.equal((html.match(/<script[^>]+src=/g)??[]).length,0,'no external script dependencies');
console.log(JSON.stringify({status:'PASS',scope:'Embedded script and DOM contract; browser rendering not verified',charts:chartIds.length,hours:24,symbols:50,expandedIndications:evidence.breadth.indications.length,filterChecks:4}));
