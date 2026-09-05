// Local DOM-contract test, without launching a browser or loading external code.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html = fs.readFileSync(process.argv[2], 'utf8');
const data = html.match(/<script type="application\/json" id="report-data">([\s\S]*?)<\/script>/)[1];
const script = html.match(/<\/script><script>([\s\S]*?)<\/script>/)[1];
class Element {
  children = []; textContent = ''; value = ''; disabled = false;
  append(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  scrollIntoView() {}
  click() { this.onclick?.(); }
}
const ids = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m => [m[1], new Element()]));
ids.get('report-data').textContent = data;
ids.get('sort').value = 'maxDrawdownPct';
let downloaded;
const context = vm.createContext({
  document: { getElementById: id => ids.get(id), createElement: () => new Element() },
  Blob, URL: { createObjectURL: blob => { downloaded = blob; return 'blob:test'; }, revokeObjectURL() {} },
  setTimeout: fn => fn(),
});
vm.runInContext(script, context, { timeout: 10000 });
assert.equal(ids.get('rows').children.length, 100);
assert.match(ids.get('count').textContent, /62208 Zeilen/);
ids.get('next').click();
assert.match(ids.get('count').textContent, /Seite 2/);
ids.get('symbol').value = 'BCH-USDT'; ids.get('symbol').onchange();
ids.get('kind').value = 'move'; ids.get('kind').onchange();
ids.get('strategy').value = 'base'; ids.get('strategy').onchange();
assert.match(ids.get('count').textContent, /162 Zeilen/);
ids.get('rows').children[0].children[0].children[0].click();
const detail = JSON.parse(ids.get('details').textContent);
assert.equal(detail.symbol, 'BCH-USDT'); assert.equal(detail.parameters.strategy, 'base');
ids.get('csv').click();
const csv = await downloaded.text();
assert.equal(csv.split('\n').length, 163);
assert.ok(csv.includes('dailyNetPct')); assert.ok(csv.includes('recentPf'));
ids.get('status').value = 'qualified'; ids.get('status').onchange();
assert.equal(ids.get('rows').children.length, 0);
assert.equal(ids.get('next').disabled, true);
const d = JSON.parse(data);
assert.equal(d.rows.length, 62208);
const index = Object.fromEntries(d.columns.map((k,i)=>[k,i]));
for (const row of d.rows) {
  assert.equal(row[index.dailyN].reduce((a,b)=>a+b,0), row[index.n]);
  assert.ok(Math.abs(row[index.dailyNetPct].reduce((a,b)=>a+b,0)-row[index.netPct]) < .00001);
  assert.ok(row[index.maxVolume] <= 2);
}
console.log('PASS: all 62208 rows, daily reconciliation, filters, pagination, details, CSV, empty results');
