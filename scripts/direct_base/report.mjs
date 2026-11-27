import fs from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {gunzipSync,gzipSync} from 'node:zlib'
import {comparePeriods,confirmationChoices} from './compare.mjs'
import {AXES, BLOCKS, axisPlan, better, getMetric, measure, pf, prepare, rank, streamId, supported} from './metrics.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const [manifest, output, lockFlag, lockPath] = process.argv.slice(2)
const meta = JSON.parse(fs.readFileSync(manifest))
const categories = ['plain', 'axis', 'block', 'both']
const locked = new Map()
const coverage = {}
const endComparisons = new Map()
const baseSigns = {}
const cap = (v) => Number.isFinite(v) ? v.toFixed(3) : (v === Infinity ? '∞' : '—')
const totalPF = m => cap(pf(m.gain, m.loss))
const percent = (x, n) => n ? (100 * x / n).toFixed(1) + '%' : '—'
const esc = s => String(s).replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]))
const keyOf = (family, bf, ef, kind) => [family,bf,ef,kind].join('/')
const codeOf = c => [c.set,c.base,c.end,c.axis,c.block]
const compareCode = (a,b) => { for (let i=0;i<a.length;i++) if(a[i]!==b[i]) return a[i] < b[i]; return false }
const scope = b => meta.base[b].split(' ').length === 3 ? 'native' : 'single'

function select(key, conf, metrics, ranking) {
  const old = locked.get(key)
  if (!old || better(ranking, old.rank) ||
      (!better(old.rank, ranking) && compareCode(codeOf(conf), codeOf(old.conf)))) {
    locked.set(key, {conf, dev:metrics, rank:ranking})
  }
}

function references(data, ef) {
  // Count differently named experiments separately, even if a given sample
  // afforded identical entries. Immutable equal streams need scoring once.
  const ref = new Map()
  for (let bf = 0; bf < meta.baseFloors.length; bf++) {
    for (let b = 0; b < meta.base.length; b++) {
      const family = scope(b)
      const children = data.ends[data.base[bf][b]][ef]
      const group = bf * 2 + (family === 'single' ? 1 : 0)
      for (let e = 0; e < children.length; e++) {
        let groups = ref.get(children[e])
        if (!groups) { groups = []; ref.set(children[e], groups) }
        if (!groups[group]) groups[group] = {bf,base:b,end:e,count:0,family}
        groups[group].count++
      }
    }
  }
  return ref
}

function evaluateWindow(panelIndex) {
  const period = meta.periods[panelIndex]
  for (const [set, part] of period.parts.entries()) {
    const data = JSON.parse(gunzipSync(fs.readFileSync(part)))
    const parentOpens = new Map()
    for (const [bf, parents] of data.base.entries()) for (const [b, parent] of parents.entries()) {
      const blocks = meta.base[b].split(' ')
      if (blocks.length !== 3) continue
      if (!parentOpens.has(parent)) {
        parentOpens.set(parent,prepare(data,data.streams[parent],period).length)
      }
      const signs = blocks[1].slice(-1) + blocks[2].slice(-1)
      const counts = baseSigns[[panelIndex,bf,signs].join('/')] ??= {eligible:0,total:0}
      counts.total++
      if (parentOpens.get(parent)) counts.eligible++
    }
    for (const [ef, floor] of meta.endFloors.entries()) {
      const refs = references(data, ef)
      for (const [id, groups] of refs) {
        const stream = data.streams[id]
        const entries = prepare(data, stream, period)
        for (let axis = 0; axis < AXES.length; axis++) {
          const plan = axisPlan(data, stream, entries, axis, floor)
          for (let block = 0; block < BLOCKS.length; block++) {
            const m = measure(data, plan, period, block)
            const enough = supported(m), ranking = enough && !panelIndex ? rank(m) : null
            const kind = categories[(axis > 0 ? 1 : 0) + (block > 0 ? 2 : 0)]
            for (const g of groups) {
              if (!g) continue
              const {bf, family, base, end, count} = g
              const key = keyOf(family, bf, ef, kind)
              const counts = coverage[panelIndex+'/'+key] ??= {total:0,filled:0,supported:0,profitable:0,bothHalves:0}
              counts.total += count
              if (m.n) counts.filled += count
              if (!enough) continue
              counts.supported += count
              if (m.gain > m.loss) counts.profitable += count
              if (m.gainA > m.lossA && m.gainB > m.lossB) counts.bothHalves += count
              if (!panelIndex) {
                const conf = {set,bf,ef,base,end,axis,block,floor}
                select(key, conf, m, ranking)
                select(keyOf(family, bf, ef, 'all'), conf, m, ranking)
              }
            }
          }
        }
      }
    }
    if (panelIndex) {
      for (const [key,w] of locked) {
        if (w.conf.set !== set) continue
        if (key.startsWith('native/') && key.endsWith('/all')) {
          endComparisons.set(key,meta.end.map((_,end) =>
            getMetric(data,period,{...w.conf,end,axis:0,block:0})))
        }
        w.test = getMetric(data, period, w.conf)
        w.plain = getMetric(data, period, {...w.conf, axis:0, block:0})
        w.baseOnly = getMetric(data, period, {...w.conf, end:-1, axis:0, block:0})
      }
    }
    console.log(panelIndex ? 'holdout' : 'development', data.name, data.streams.length, 'streams scored')
  }
}

evaluateWindow(0)
const developmentLock = JSON.stringify({
  setIds: meta.sets, baseFloors: meta.baseFloors, endFloors: meta.endFloors, base: meta.base, end: meta.end,
  axes: AXES, blocks: BLOCKS, costPct: meta.costPct, lookbackHours: meta.lookbackHours,
  development: meta.periods[0].manifest, warmMinutes: meta.periods[0].warm,
  minimumCloses: 60, minimumClosesPerHalf: 20, minimumActiveHoursFraction: 0.5,
  rankBy: ['weaker chronological half PF', 'positive hour fraction', 'smaller realized drawdown', 'more closes'],
  selections: Object.fromEntries(locked)
})
if (lockFlag === '--freeze-development') {
  fs.writeFileSync(output,developmentLock+'\n')
  console.log('Frozen', locked.size, 'development choices')
  process.exit(0)
}
if (lockFlag === '--verify-lock' && fs.readFileSync(lockPath,'utf8').trimEnd() !== developmentLock) {
  throw Error('Development selection differs from the frozen manifest')
}
evaluateWindow(1)
const overlaps=comparePeriods(meta)
const choices=confirmationChoices(overlaps,meta)
const flagIndex=process.argv.indexOf('--confirmation')
const confirmation=flagIndex<0 ? null : JSON.parse(fs.readFileSync(process.argv[flagIndex+1]))
if (confirmation) {
  const p=confirmation.period
  if (p.manifest.start_ms+p.warm*60000<meta.periods[1].manifest.end_ms ||
      JSON.stringify(p.symbols)!==JSON.stringify(meta.periods[1].symbols) || confirmation.costPct!==meta.costPct ||
      Object.keys(confirmation.results).length!==Object.keys(choices).length)
    throw Error('Confirmation must follow holdout with the same symbols, cost and choices')
  for (const [k,c] of Object.entries(choices)) {
    if (JSON.stringify(c)!==JSON.stringify(confirmation.results[k]?.settings)) throw Error('Confirmation differs from frozen choice '+k)
  }
}
let followRows=''
for(const category of categories){
  const candidates=Object.entries(overlaps).filter(([k])=>k.endsWith('/'+category))
  if(!candidates.length) continue
  let selected=candidates[0][1].best
  for(const [,group] of candidates) if(better(group.best.rank,selected.rank)) selected=group.best
  const c=selected.conf, f=confirmation?.results[category]?.execution
  followRows+='<tr><td>'+esc(category)+'</td><td>'+esc(meta.sets[c.set])+'<small>'+esc(meta.base[c.base])+' → '+esc(meta.end[c.end])+
    ' · PF '+meta.baseFloors[c.bf].toFixed(1)+' / '+c.floor.toFixed(1)+' · Axis '+esc(AXES[c.axis])+' · Block '+esc(BLOCKS[c.block].label)+'</small></td><td>'+
    totalPF(selected.dev)+'</td><td>'+totalPF(selected.test)+'</td><td>'+cap(selected.rank[0])+'</td><td>'+(f?totalPF(f):'—')+'</td><td>'+
    (f?f.n+' / '+f.open:'—')+'</td><td>'+(f?f.positive+'/'+f.hours+' · '+percent(f.positive,f.hours):'—')+'</td><td>'+(f?(100*f.drawdown).toFixed(2):'—')+'</td></tr>'
}
const totals=Object.values(overlaps).reduce((a,g)=>[a[0]+g.positive,a[1]+g.four],[0,0])
let followText=totals[0].toLocaleString('en-US')+' supported configurations had positive net in both 48-hour periods. '+totals[1]+
  ' were profitable in all four daily halves. These counts include different parameter settings that admitted the same orders. The table selects each treatment’s best result across both periods. Those first two PF columns are retrospective.'
if(confirmation){
  const p=confirmation.period
  const fmtDate=t=>new Date(t).toISOString().slice(0,16).replace('T',' ')
  followText+=' The four choices were frozen before the additional '+((p.minutes-p.warm)/60)+'-hour check from '+
    fmtDate(p.manifest.start_ms+p.warm*60000)+' to '+fmtDate(p.manifest.end_ms)+' UTC. Unsettled entries are shown separately.'
}
const winners = Object.fromEntries(locked)
const parts = meta.periods.map(p => p.parts)
for (const p of meta.periods) delete p.parts
const metaJson = JSON.stringify({...meta, winners, coverage, baseSigns, confirmation}).replaceAll('<','\\u003c')
const tables = new Map()
let printable = ''
for (const family of ['native','single']) {
  let rows = ''
  for (const [bf, bv] of meta.baseFloors.entries()) for (const [ef, ev] of meta.endFloors.entries()) {
    const w = locked.get(keyOf(family,bf,ef,'all'))
    if (!w) {
      rows += `<tr><td>${bv.toFixed(1)}</td><td>${ev.toFixed(1)}</td><td colspan="8">No development configuration met the sample requirement.</td></tr>`
      continue
    }
    const c = w.conf, m = w.test
    const identity = [meta.sets[c.set],meta.base[c.base]+' → '+meta.end[c.end],AXES[c.axis]+' · Block '+BLOCKS[c.block].label]
    rows += `<tr><td>${bv.toFixed(1)}</td><td>${ev.toFixed(1)}</td><td class="ident"><button class="load" data-key="${keyOf(family,bf,ef,'all')}">${esc(identity[0])}</button><small>${esc(identity.slice(1).join(' / '))}</small></td><td>${totalPF(w.dev)}</td><td><b>${totalPF(m)}</b></td><td>${m.n}</td><td>${m.positive}/${m.hours}<small>${percent(m.positive,m.hours)}</small></td><td>${(100*m.drawdown).toFixed(2)}</td><td>${totalPF(w.plain)}</td><td>${totalPF(w.baseOnly)}</td></tr>`
    printable += [family,bv,ev,...identity,'dev',totalPF(w.dev),w.dev.n,'test',totalPF(m),m.n,m.positive,m.active,m.hours,'DD',(100*m.drawdown).toFixed(3),'net',(100*(m.gain-m.loss)).toFixed(3),'plain',totalPF(w.plain),'baseOnly',totalPF(w.baseOnly),'halves',cap(pf(m.gainA,m.lossA)),cap(pf(m.gainB,m.lossB))].join(' | ')+'\n'
  }
  tables.set(family,rows)
}
const endColumns = []
for (const [bf,bv] of meta.baseFloors.entries()) for (const [ef,ev] of meta.endFloors.entries()) {
  endColumns.push([bv.toFixed(1)+' / '+ev.toFixed(1),endComparisons.get(keyOf('native',bf,ef,'all'))])
}
function endTable(paired) {
  const rows = meta.end.flatMap((label,i) => {
    if (label.includes(' ') !== paired) return []
    const cells = endColumns.map(([,col]) => {
      if (!col) return '<td>—</td>'
      const m=col[i]
      return '<td>'+totalPF(m)+'<small>'+m.n+' closes · '+percent(m.positive,m.hours)+'</small></td>'
    }).join('')
    return ['<tr><td>'+esc(label)+'</td>'+cells+'</tr>']
  })
  return '<div class="scroll"><table><thead><tr><th>End gate</th>'+endColumns.map(([title])=>'<th>'+title+'</th>').join('')+
         '</tr></thead><tbody>'+rows.join('')+'</tbody></table></div>'
}
let signRows = ''
for (const p of [0,1]) for (const [bf,value] of meta.baseFloors.entries()) {
  const cells=['++','+-','-+','--'].map(sign => {
    const c=baseSigns[[p,bf,sign].join('/')]
    return '<td>'+c.eligible+'/'+c.total+'<small>'+percent(c.eligible,c.total)+'</small></td>'
  })
  signRows += '<tr><td>'+(p?'Holdout':'Development')+'</td><td>'+value.toFixed(1)+'</td>'+cells.join('')+'</tr>'
}
const controls = []
for (const [key,w] of locked) {
  if (key.endsWith('/all')) continue
  const {conf:c,dev:d,test:t}=w
  controls.push(`<tr><td>${key.startsWith('native')?'Compound':'Standalone'}</td><td>${meta.baseFloors[c.bf].toFixed(1)} / ${c.floor.toFixed(1)}</td><td>${esc(AXES[c.axis])}</td><td>${esc(BLOCKS[c.block].label)}</td><td><button class="load" data-key="${key}">${esc(meta.sets[c.set])}</button><small>${esc(meta.base[c.base])} → ${esc(meta.end[c.end])}</small></td><td>${totalPF(d)}</td><td>${totalPF(t)}</td><td>${t.n}</td><td>${percent(t.positive,t.hours)}</td><td>${(100*t.drawdown).toFixed(2)}</td></tr>`)
}
const count = meta.base.length*meta.end.length*meta.baseFloors.length*meta.endFloors.length*AXES.length*BLOCKS.length*meta.sets.length
for (const index of [0,1]) {
  const total = Object.entries(coverage).filter(([k])=>k.startsWith(index+'/')).reduce((n,[,v])=>n+v.total,0)
  if (total !== count) throw Error('Incomplete grid '+total+'/'+count)
}
const dates = meta.periods.map(p=>[p.manifest.start_ms+p.warm*60000,p.manifest.end_ms].map(t=>new Date(t).toISOString().slice(0,16).replace('T',' ')).join(' – '))
const staticTable = rows => '<div class="scroll"><table><thead><tr><th>Base PF</th><th>End PF</th><th>Development selection</th><th>Dev PF</th><th>Holdout PF</th><th>Closes</th><th>Positive hours</th><th>DD¹</th><th>Same gates, plain PF</th><th>Base alone PF</th></tr></thead><tbody>'+rows+'</tbody></table></div>'
let html = fs.readFileSync(path.join(here,'report.html'),'utf8')
const values = {
  METADATA: metaJson, FOLLOW_TEXT:esc(followText), FOLLOW:followRows, NATIVE:staticTable(tables.get('native')), SINGLE:staticTable(tables.get('single')),
  CONTROLS:controls.sort().join(''), BASE_SIGNS:signRows, END_SINGLE:endTable(false), END_PAIRED:endTable(true), COUNT:count.toLocaleString('en-US'), SET_LABEL:meta.sets.length+' directional Set'+(meta.sets.length===1?'':'s'),
  SYMBOL_COUNT:meta.periods[0].symbols.length, DATES_DEV:dates[0], DATES_OOS:dates[1],
  FEE:meta.costPct.toFixed(3), LOOKBACK:meta.lookbackHours, WARMUP:meta.periods[0].warm/60,
  METRICS:fs.readFileSync(path.join(here,'metrics.mjs'),'utf8').replace(/^export /gm,''),
  VIEW:fs.readFileSync(path.join(here,'view.js'),'utf8')
}
for (const [key,value] of Object.entries(values)) html = html.replaceAll('<!--'+key+'-->', () => String(value))
function browserChunk(file) {
  const data=JSON.parse(gunzipSync(fs.readFileSync(file)))
  const offsets=Buffer.alloc(4*(data.streams.length+1))
  const bytes=Buffer.allocUnsafe(5*data.streams.reduce((n,s)=>n+s.length,0))
  let j=0
  for(const [index,stream] of data.streams.entries()) {
    offsets.writeUInt32LE(j,index*4)
    let last=0
    for(const id of stream) {
      let gap=id-last
      last=id
      while(gap>=128) { bytes[j++]=128+(gap%128); gap=Math.floor(gap/128) }
      bytes[j++]=gap
    }
  }
  offsets.writeUInt32LE(j,data.streams.length*4)
  delete data.streams
  data.index64=offsets.toString('base64')
  data.delta64=bytes.subarray(0,j).toString('base64')
  return gzipSync(JSON.stringify(data),{level:6}).toString('base64')
}
const [lead, tail] = html.split('<!--PARTS-->')
const fd = fs.openSync(output,'w')
try {
  fs.writeSync(fd,lead)
  for (let p=0;p<parts.length;p++) for(let s=0;s<parts[p].length;s++) {
    fs.writeSync(fd,'<script type="application/octet-stream" id="tape-'+p+'-'+s+'">')
    fs.writeSync(fd,browserChunk(parts[p][s]))
    fs.writeSync(fd,'</script>\n')
  }
  fs.writeSync(fd,tail)
} finally { fs.closeSync(fd) }
console.log(printable)
console.log('Exhaustively evaluated',count.toLocaleString('en-US'),'configurations per period.')
