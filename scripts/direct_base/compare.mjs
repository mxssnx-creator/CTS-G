import fs from 'node:fs'
import {gunzipSync} from 'node:zlib'
import {AXES, BLOCKS, axisPlan, better, measure, pf, prepare, supported} from './metrics.mjs'

// Descriptive overlap after both replays. Kept out of development selection.
export function comparePeriods(meta) {
  const buckets = {}
  const [dev, test] = meta.periods
  const families = ['plain','axis','block','both']
  for (let set=0; set<meta.sets.length; set++) {
    const data = [dev,test].map(p=>JSON.parse(gunzipSync(fs.readFileSync(p.parts[set]))))
    for (const [ef, floor] of meta.endFloors.entries()) {
      const caches = [new Map(),new Map()]
      function profitable(panel,id) {
        const cache = caches[panel]
        if (cache.has(id)) return cache.get(id)
        const tape = data[panel], period = meta.periods[panel], stream = tape.streams[id]
        const entries = prepare(tape,stream,period), results = new Map()
        if (entries.length >= 60) for (let axis=0;axis<AXES.length;axis++) {
          const plan = axisPlan(tape,stream,entries,axis,floor)
          if (plan.settled.length < 60) continue
          for (let block=0;block<BLOCKS.length;block++) {
            const m = measure(tape,plan,period,block)
            if (supported(m) && m.gain > m.loss) results.set(axis*BLOCKS.length+block,m)
          }
        }
        cache.set(id,results)
        return results
      }
      for (const [bf] of meta.baseFloors.entries()) for(const [base,name] of meta.base.entries()) {
        const family = name.includes(' ') ? 'native' : 'single'
        const children = data.map(d=>d.ends[d.base[bf][base]][ef])
        for(let end=0;end<meta.end.length;end++) {
          const a = profitable(0,children[0][end])
          if (!a.size) continue
          const b = profitable(1,children[1][end])
          for (const [treatment,first] of a) {
            const second = b.get(treatment)
            if (!second) continue
            const axis = Math.floor(treatment/BLOCKS.length), block = treatment%BLOCKS.length
            const cat = families[(axis>0?1:0)+(block>0?2:0)]
            const key = [family,bf,ef,cat].join('/')
            const group = buckets[key] ??= {positive:0,four:0,best:null}
            group.positive++
            const four = [first,second].flatMap(m=>[pf(m.gainA,m.lossA)??0,pf(m.gainB,m.lossB)??0])
            const worst = Math.min(...four)
            if (worst > 1) group.four++
            const score = [worst,Math.min(first.positive/first.hours,second.positive/second.hours),
                           -Math.max(first.drawdown,second.drawdown),Math.min(first.n,second.n)]
            if (!group.best || better(score,group.best.rank)) {
              group.best={conf:{set,bf,ef,base,end,axis,block,floor},dev:first,test:second,rank:score}
            }
          }
        }
      }
    }
  }
  return buckets
}

export function confirmationChoices(groups,meta) {
  const best={}
  for (const [key,group] of Object.entries(groups)) {
    const category=key.split('/').at(-1)
    if (!best[category] || better(group.best.rank,best[category].rank)) best[category]=group.best
  }
  return Object.fromEntries(Object.entries(best).map(([category,w]) => {
    const c=w.conf
    return [category,{set:meta.sets[c.set],base:meta.base[c.base],baseFloor:meta.baseFloors[c.bf],
      end:meta.end[c.end],endFloor:meta.endFloors[c.ef],axis:c.axis,block:c.block}]
  }))
}
