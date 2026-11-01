import { readFileSync,writeFileSync,existsSync } from 'node:fs';
import { gunzipSync,gzipSync } from 'node:zlib';
import { createHash } from 'node:crypto';
import { sourceEvents } from '../server/signals/source-events.mjs';

const [feedPath,outPath,startText,endText]=process.argv.slice(2);
const start=Number(startText),end=Number(endText);
if (!feedPath || !outPath || !(end>start) || start%3600000 || end%3600000 || existsSync(outPath)) throw new Error('feed out start_ms end_ms required; preserve prior output');
const bytes=readFileSync(feedPath),feed=JSON.parse(feedPath.endsWith('.gz')?gunzipSync(bytes):bytes);
const symbols=Object.keys(feed.candles).filter(k=>k.endsWith('|1')).map(k=>k.slice(0,-2)).sort().slice(0,8);
if (symbols.length!==8) throw new Error('Exactly eight symbols required');
const data=Object.fromEntries(symbols.map(s=>[s,feed.candles[s+'|1'].filter(r=>r.t>=start&&r.t<end).map(r=>[r.t,r.o,r.h,r.l,r.c,r.v])]));
if (Object.values(data).some(rows=>rows.length!==(end-start)/60000 || rows[0][0]!==start || rows.at(-1)[0]+60000!==end)) throw new Error('Incomplete requested feed');
const began=performance.now(),result=sourceEvents(data);
const manifest=JSON.parse(readFileSync(new URL('../server/signals/ao-source-provenance.json',import.meta.url)));
for (const [name,sha] of Object.entries(manifest.files)) {
  const actual=createHash('sha256').update(readFileSync(new URL('../server/signals/ao/'+name,import.meta.url))).digest('hex');
  if (actual!==sha) throw new Error(`Source manifest does not match ${name}`);
}
result.provenance={feedSha256:createHash('sha256').update(bytes).digest('hex'),start,end,symbols,
  source:manifest,
  adapterSha256:createHash('sha256').update(readFileSync(new URL('../server/signals/source-events.mjs',import.meta.url))).digest('hex')};
result.performance={seconds:(performance.now()-began)/1000};
const payload=Buffer.from(JSON.stringify(result));
writeFileSync(outPath,outPath.endsWith('.gz')?gzipSync(payload):payload);
console.log(JSON.stringify({sources:result.catalogue.length,series:result.series.length,events:result.series.reduce((s,x)=>s+x.events.length,0),...result.performance}));
