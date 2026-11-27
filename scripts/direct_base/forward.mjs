import fs from 'node:fs'
import {gunzipSync} from 'node:zlib'
import {AXES,BLOCKS,getMetric} from './metrics.mjs'

const [input,output]=process.argv.slice(2)
const meta=JSON.parse(fs.readFileSync(input))
const panel=meta.period
const sets=panel.parts.map(f=>JSON.parse(gunzipSync(fs.readFileSync(f))))
delete panel.parts
const results={}
for(const [name,s] of Object.entries(meta.selections)){
  const data=sets[meta.sets.indexOf(s.set)]
  const c={bf:meta.baseFloors.indexOf(s.baseFloor),base:meta.base.indexOf(s.base),ef:meta.endFloors.indexOf(s.endFloor),
           end:meta.end.indexOf(s.end),axis:s.axis,block:s.block,floor:s.endFloor}
  if(!data || Object.values(c).some(v=>!Number.isFinite(v)||v<0) || !AXES[c.axis] || !BLOCKS[c.block]) throw Error('Unknown frozen setting')
  const m=getMetric(data,panel,c,true)
  m.trades=m.trades.map(([id,weight])=>[...data.orders[id],weight])
  results[name]={settings:s,execution:m,plain:getMetric(data,panel,{...c,axis:0,block:0})}
}
fs.writeFileSync(output,JSON.stringify({period:panel,costPct:meta.costPct,results})+'\n')
