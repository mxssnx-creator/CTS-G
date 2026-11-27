import fs from 'node:fs'
import {pathToFileURL} from 'node:url'
import {axisPlan, better, measure, prepare} from './metrics.mjs'

const categories = ['plain','axis','block','both']
const category = (a,b) => categories[Number(a>0) + 2*Number(b>0)]
export const keyOf = c => [c.portfolio,c.cap,c.axis,c.block,...(c.slots ? [c.slots,Number(c.guard)] : [])].join('/')
const floorsOf = p => p.baseFloors.flatMap(b => p.endFloors.map(e => b+'/'+e))

export function limitMinute(data, entries, cap) {
  if (!cap) return entries
  // All entries at this minute are available at the same closed bar. Ties use the symbol, never the outcome.
  const ranked = [...entries].sort((i,j) => data.orders[i][0]-data.orders[j][0]
    || data.orders[j][5]-data.orders[i][5] || data.orders[i][2]-data.orders[j][2])
  let last = -1, count = 0
  const keep = new Set(ranked.filter(i => {
    const t = data.orders[i][0]
    if (last !== t) { last = t; count = 0 }
    return ++count <= cap
  }))
  return entries.filter(i => keep.has(i))
}


export function inventoryPlan(data, stream, entries, slots, guard) {
  if (!slots) return entries
  const rows=data.orders, admitted=[], open=[], buckets=new Map()
  let closed=0
  // The entire cohort is known at this bar close. Give priority to the largest prior pullback.
  const ranked=[...entries].sort((i,j)=>rows[i][0]-rows[j][0] || rows[j][5]-rows[i][5] || rows[i][2]-rows[j][2])
  for(const id of ranked) {
    const at=rows[id][0]
    while(closed<stream.length) {
      const row=rows[stream[closed]]
      if(row[1]===null || row[1]>=at) break
      if(guard) {
        const bucket=Math.ceil(row[1]/15)-1, group=buckets.get(bucket)??[0,0]
        group[0]+=row[3]; group[1]++; buckets.set(bucket,group)
      }
      closed++
    }
    for(let i=open.length-1;i>=0;i--) if(open[i]<at) open.splice(i,1)
    if(open.length>=slots) continue
    if(guard) {
      // Each completed quarter-hour contributes one mean. A basket-wide loss has one vote.
      const through=Math.floor((at-1)/15)-1
      let gains=0, losses=0, count=0
      for(let bucket=through;bucket>through-32 && count<8;bucket--) {
        const group=buckets.get(bucket)
        if(!group) continue
        const mean=group[0]/group[1]
        gains+=Math.max(0,mean); losses+=Math.max(0,-mean); count++
      }
      if(count<3 || gains<=losses) continue
    }
    admitted.push(id)
    open.push(rows[id][1]??Infinity)
  }
  // Block assigns rounds in the engine's chronological symbol order after admission.
  const keep=new Set(admitted)
  return entries.filter(id=>keep.has(id))
}

export function summarize(trades, period) {
  const hours = (period.minutes-period.warm)/60, net = Array(hours).fill(0), counts = Array(hours).fill(0)
  const ticks = new Map(), sides = {}, symbols = new Set(), entries = new Set()
  let gain=0, loss=0, wins=0, open=0, peak=0, equity=0, dd=0, markGain=0, markLoss=0, marked=0
  for (const [entry,exit,sym,unit,weight,side,,mark] of trades) {
    entries.add(entry)
    if (exit === null) {
      open++
      if(Number.isFinite(mark)) { marked++; markGain+=Math.max(0,mark*weight); markLoss+=Math.max(0,-mark*weight) }
      continue
    }
    const pnl = unit*weight, h = Math.ceil((exit-period.warm)/60)-1
    if (h<0 || h>=hours || exit<=entry) throw Error('Settlement outside scoring clock')
    symbols.add(sym)
    const s = sides[side] ??= [0,0,0]
    s[0]++; s[1] += Math.max(0,pnl); s[2] += Math.max(0,-pnl)
    gain += Math.max(0,pnl); loss += Math.max(0,-pnl); wins += pnl>0
    net[h] += pnl; counts[h]++
    ticks.set(exit,(ticks.get(exit)??0)+pnl)
  }
  for (const t of [...ticks.keys()].sort((a,b)=>a-b)) {
    equity+=ticks.get(t); peak=Math.max(peak,equity); dd=Math.max(dd,peak-equity)
  }
  return {n:trades.length-open,open,gain,loss,wins,positive:net.filter(x=>x>0).length,
          active:counts.filter(Boolean).length,hours,dd,symbols:symbols.size,entryMinutes:entries.size,sides,net,counts,marked,markGain,markLoss}
}

function evaluator(panel, policy) {
  const memo = new Map()
  function lane(component,side,floors,cfg) {
    const cacheKey=[component,side,floors,cfg.cap,cfg.axis,cfg.block,cfg.slots,cfg.guard].join('/')
    if(memo.has(cacheKey)) return memo.get(cacheKey)
    const [shape]=policy.components[component], data=panel.sets[(policy.source??'market-catchup')+':'+side+':1m:'+shape]
    const evidence=data.rules[component][floors]
    const stream=evidence.end.filter(i=>data.orders[i][4])
    const entries=prepare(data,stream,panel.period)
    const selection=axisPlan(data,stream,entries,cfg.axis,Number(floors.split('/')[1]))
    const minute=limitMinute(data,selection.admitted,cfg.cap)
    const admitted=inventoryPlan(data,stream,minute,cfg.slots,cfg.guard)
    const settled=admitted.map((_,i)=>i).filter(i=>data.orders[admitted[i]][1]!==null)
                         .sort((a,b)=>admitted[a]-admitted[b])
    const book=measure(data,{admitted,settled},panel.period,cfg.block,true)
    const rows=book.trades.map(([i,w]) => [...data.orders[i].slice(0,4),w,side,component,data.orders[i][6]??null])
    memo.set(cacheKey,rows)
    return rows
  }
  return (cfg,floors) => {
    const components=policy.portfolios[cfg.portfolio], trades=[]
    for (const component of components) for(const side of ['L','S']) {
      for(const [en,ex,sym,unit,w,dir,id,mark] of lane(component,side,floors,cfg)) {
        trades.push([en,ex,sym,unit,w/components.length,dir,id,mark])
      }
    }
    trades.sort((a,b)=>a[0]-b[0]||a[2]-b[2]||a[6].localeCompare(b[6])||a[5].localeCompare(b[5]))
    return {stats:summarize(trades,panel.period),trades}
  }
}

function configurations(p) {
  const cases=[]
  for(const portfolio of Object.keys(p.portfolios)) for(const cap of p.caps)
    for(const axis of p.axis) for(const block of p.block) {
      if(p.inventory) for(const slots of p.inventory) for(const guard of p.guard) cases.push({portfolio,cap,axis,block,slots,guard})
      else cases.push({portfolio,cap,axis,block})
    }
  return cases
}

function reportPanel(panel,p,selected) {
  const run=evaluator(panel,p), ledgers={}, summary=[]
  for(const cfg of configurations(p)) for(const floors of floorsOf(p)) {
    const m=run(cfg,floors).stats
    summary.push([cfg.portfolio,cfg.cap,cfg.axis,cfg.block,floors,m.n,m.open,m.gain,m.loss,m.positive,m.active,m.dd,m.symbols,m.entryMinutes,cfg.slots??0,Number(!!cfg.guard),m.markGain,m.markLoss])
  }
  for(const [label,c] of Object.entries(selected)) {
    const id=keyOf(c)
    if(ledgers[id]) continue
    ledgers[id]=Object.fromEntries(floorsOf(p).map(f=>[f,run(c,f)]))
  }
  return {period:panel.period,costPct:panel.costPct,summary,ledgers}
}

function fit(input) {
  const {policy:p,panels,protocol}=input, runs=panels.map(v=>evaluator(v,p))
  const selected={}, ranks={}
  for(const c of configurations(p)) {
    const cells=runs.flatMap(run=>floorsOf(p).map(f=>run(c,f).stats))
    // Require distributed trading at every floor in every fold. Equalize the PF shrinkage
    // for a single book and a portfolio whose component books each use a fraction of one unit.
    if(cells.some(m=>m.n<30 || m.active<8 || m.entryMinutes<15 || m.symbols<5)) continue
    const ratios=cells.map(m=>(m.gain+.08)/(m.loss+.08)).sort((a,b)=>a-b)
    const rank=[ratios[0],ratios[Math.floor(ratios.length/2)],Math.min(...cells.map(m=>m.positive/m.hours)),
                -Math.max(...cells.map(m=>m.dd)),Math.min(...cells.map(m=>m.n))]
    for(const label of ['primary',category(c.axis,c.block)]) {
      if(!ranks[label] || better(rank,ranks[label])) { ranks[label]=rank; selected[label]=c }
    }
  }
  if(!selected.primary) throw Error('No development policy has sufficient support on every floor and fold')
  const symbols=panels[0].period.symbols
  return {protocol,policy:p,symbols,trainingEnd:panels.at(-1).period.end,
          training:panels.map(v=>v.period),selected,ranks,
          development:panels.map(v=>reportPanel(v,p,selected))}
}

function main() {
  const [mode,path,out]=process.argv.slice(2), input=JSON.parse(fs.readFileSync(path))
  if(!['fit','replay'].includes(mode)) throw Error('Expected fit or replay')
  const result=mode==='fit'?fit(input):{protocol:input.protocol,selection:input.selection.sha256,selected:input.selection.selected,
    policy:input.policy,...reportPanel(input.panels[0],input.policy,input.selection.selected)}
  fs.writeFileSync(out,JSON.stringify(result)+'\n')
}

if(import.meta.url===pathToFileURL(process.argv[1]??'').href) main()
