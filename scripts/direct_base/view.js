const meta = JSON.parse(document.getElementById('model').textContent)
const el = id => document.getElementById(id)
const controls = ['panel','set','bf','base','ef','end','axis','block']
const fmt = n => n === null ? '—' : n === Infinity ? '∞' : n.toFixed(3)
const pct = (n,d) => d ? (100*n/d).toFixed(1)+'%' : '—'
const points = x => (100*x).toFixed(3)
const escapeText = s => String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))
const grid = (heads,rows) => '<table><thead><tr>'+heads.map(x=>'<th>'+escapeText(x)+'</th>').join('')+
  '</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(x=>'<td>'+escapeText(x)+'</td>').join('')+'</tr>').join('')+'</tbody></table>'
function opts(id, values) {
  values.forEach((v,i)=>el(id).add(new Option(v,i)))
}
opts('set',meta.sets); opts('bf',meta.baseFloors.map(x=>x.toFixed(1)));opts('base',meta.base)
opts('ef',meta.endFloors.map(x=>x.toFixed(1)))
el('end').add(new Option('Base only (control)',-1));opts('end',meta.end)
opts('axis',AXES);opts('block',BLOCKS.map(b=>b.label))
let seq=0, cacheKey=null, cache=null, current=null
const initial=Object.entries(meta.winners).find(([k])=>k==='native/0/0/all')?.[1]
function selectConfig(conf) {
  for (const id of controls.filter(id=>id!=='panel')) el(id).value=conf[id]
}
if(initial) selectConfig(initial.conf)
else el('end').value=0

function decode(encoded) {
  if(typeof Uint8Array.fromBase64==='function') return Uint8Array.fromBase64(encoded)
  const text=atob(encoded), bytes=new Uint8Array(text.length)
  for(let i=0;i<text.length;i++)bytes[i]=text.charCodeAt(i)
  return bytes
}
async function openTape(p,s) {
  const k=p+'-'+s
  if(k!==cacheKey) {
    const raw=decode(el('tape-'+k).textContent.trim())
    cacheKey=k
    cache=new Response(new Blob([raw]).stream().pipeThrough(new DecompressionStream('gzip'))).json().then(data=>{
      data.offsets=new DataView(decode(data.index64).buffer)
      data.delta=decode(data.delta64)
      delete data.index64
      delete data.delta64
      return data
    })
  }
  return cache
}
const utc=(p,minute)=>new Date(p.manifest.start_ms+minute*60000).toISOString().slice(0,16).replace('T',' ')
function matrix() {
  if (!current || !el('matrixDetails').open) return
  const {d,p,c}=current
  el('matrix').innerHTML=grid(['End rule','Closes','Open','Exec PF','Net pp','Positive / all hours','Active hours','DD pp'],
    meta.end.map((label,i)=>{
      const r=getMetric(d,p,{...c,end:i})
      return [label,r.n,r.open,fmt(pf(r.gain,r.loss)),points(r.gain-r.loss),pct(r.positive,r.hours),r.active,points(r.drawdown)]
    }))
}
function chart(m) {
  let running=0
  const equity=[0,...m.hourNet.map(n=>(running+=n)*100)]
  const lo=Math.min(...equity,0), hi=Math.max(...equity,0), range=hi-lo||1
  const y=n=>136-(n-lo)/range*112
  const x=i=>52+820*i/(equity.length-1)
  const line=equity.map((v,i)=>x(i).toFixed(2)+','+y(v).toFixed(2)).join(' ')
  return '<svg viewBox="0 0 920 164" role="img" aria-label="Cumulative realized net return by settlement hour"><path d="M52 '+y(0)+'H872" stroke="#9dafa9" stroke-dasharray="5 3"/><polyline points="'+line+'" fill="none" stroke="#106459" stroke-width="2.8"/><g fill="#334e4a" font-size="12"><text x="7" y="27">'+hi.toFixed(1)+'</text><text x="7" y="139">'+lo.toFixed(1)+'</text><text x="52" y="157">0 h</text><text x="841" y="157">'+m.hours+' h</text></g></svg>'
}
function showLedger() {
  if(!current) return
  const {m,p,d}=current
  const rows=m.trades.map(([id,mult])=>{
    const [entry,exit,symbol,net]=d.orders[id]
    return [p.symbols[symbol],utc(p,entry),exit===null?'Open':utc(p,exit),mult.toFixed(2),net===null?'—':points(net),net===null?'—':points(net*mult)]
  })
  el('ledger').innerHTML=grid(['Symbol','Entry UTC','Exit UTC','Size ×','Unit net pp','Weighted net pp'],rows)
}
function fullWindows(d, ids, candidateEntries, need, lookback) {
  let lo=0, hi=0, count=0
  for(const id of candidateEntries) {
    const when=d.orders[id][0]
    while(hi<ids.length && d.orders[ids[hi]][1]!==null && d.orders[ids[hi]][1]<when) hi++
    while(lo<hi && d.orders[ids[lo]][1]<when-lookback) lo++
    if(hi-lo>=need) count++
  }
  return count
}
const needed=rule=>(rule.match(/\d+/g)||[]).reduce((n,x)=>n+Number(x),0)
async function refresh(){
  const ticket=++seq
  try {
    const c=Object.fromEntries(controls.map(id=>[id,Number(el(id).value)]))
    c.floor=meta.endFloors[c.ef]
    el('message').textContent='Reading the selected Set…'
    const p=meta.periods[c.panel], d=await openTape(c.panel,c.set)
    if(ticket!==seq)return
    const m=getMetric(d,p,c,true)
    const parent=orderIds(d,d.base[c.bf][c.base]), qualified=orderIds(d,streamId(d,c.bf,c.base,c.ef,c.end))
    const scored=ids=>prepare(d,ids,p).length
    const raw=d.orders.map((_,id)=>id)
    const rawEntries=prepare(d,raw,p),baseEntries=prepare(d,parent,p),nRaw=rawEntries.length
    const withBaseN=fullWindows(d,raw,rawEntries,needed(meta.base[c.base]),meta.lookbackHours*60)
    const withEndN=fullWindows(d,parent,baseEntries,c.end<0?0:needed(meta.end[c.end]),meta.lookbackHours*60)
    const facts=[['Execution PF',fmt(pf(m.gain,m.loss))],['Closes / open',m.n+' / '+m.open],['Positive / all hours',m.positive+'/'+m.hours+' · '+pct(m.positive,m.hours)],['Positive / active hours',m.positive+'/'+m.active+' · '+pct(m.positive,m.active)],['Realized net',points(m.gain-m.loss)+' pp'],['Max drawdown',points(m.drawdown)+' pp'],['Win rate',pct(m.wins,m.n)],['Block-raised entries',m.raised]]
    el('stats').innerHTML=facts.map(([a,b])=>'<span>'+a+'<strong>'+b+'</strong></span>').join('')
    const nBase=baseEntries.length, nEnd=scored(qualified)
    el('funnel').textContent=nRaw+' candidate entries → '+nBase+' Base-qualified ('+pct(nBase,nRaw)+') → '+nEnd+' end-qualified ('+pct(nEnd,nBase)+' of Base) → '+(m.n+m.open)+' simulated opens. First-half PF '+fmt(pf(m.gainA,m.lossA))+' on '+m.nA+' closes. Second-half PF '+fmt(pf(m.gainB,m.lossB))+' on '+(m.n-m.nA)+' closes.'
    const stages=[['Native candidates',raw,rawEntries],['Base-qualified',parent,baseEntries],['End-qualified',qualified,prepare(d,qualified,p)]]
    const stageRows=stages.map(([name,stream,entries])=>[name,measure(d,axisPlan(d,stream,entries,0,c.floor),p,0)])
    stageRows.push(['Executed with selected sizing',m])
    el('stages').innerHTML=grid(['Stage outcomes','Closes','Open','Settled PF','Positive / all hours','DD pp'],
      stageRows.map(([name,r])=>[name,r.n,r.open,fmt(pf(r.gain,r.loss)),pct(r.positive,r.hours),points(r.drawdown)]))
    el('samples').textContent='A full Base window was available at '+withBaseN+' of '+nRaw+' candidate entries ('+pct(withBaseN,nRaw)+'). A full end window was available at '+withEndN+' of '+nBase+' Base-qualified entries ('+pct(withEndN,nBase)+').'
    current={d,p,c,m}
    el('chart').innerHTML=chart(m)
    let accum=0
    el('hourly').innerHTML=grid(['Hour starting UTC','Closes','Net pp','Cumulative pp'],m.hourNet.map((v,i)=>[utc(p,p.warm+60*i),m.hourN[i],points(v),points(accum+=v)]))
    el('ledger').innerHTML=''
    if(el('ledger').closest('details').open)showLedger()
    matrix()
    const cov=[]
    for(const family of ['native','single'])for(const [bf,bv] of meta.baseFloors.entries())for(const [ef,ev] of meta.endFloors.entries()){
      const counts={total:0,filled:0,supported:0,profitable:0,bothHalves:0}
      for(const cat of ['plain','axis','block','both']){
        const a=meta.coverage[[c.panel,family,bf,ef,cat].join('/')]
        for(const k of Object.keys(counts))counts[k]+=a[k]
      }
      cov.push([family,bv.toFixed(1),ev.toFixed(1),...Object.values(counts).map(v=>v.toLocaleString('en-US'))])
    }
    el('coverage').innerHTML=grid(['Base family','Base PF','End PF','Configurations','With closes','Supported','Supported and profitable','Supported, both halves positive'],cov)
    el('message').textContent=c.end<0?'Base-only control. End minimum PF applies only if Prev 4 Axis is selected.':''
  }catch(err){if(ticket===seq)el('message').textContent='The detailed tape could not be opened. Download the HTML and open it in a current browser. '+err.message}
}
document.querySelectorAll('.load').forEach(b=>b.addEventListener('click',()=>{
  selectConfig(meta.winners[b.dataset.key].conf);el('panel').value=1;refresh();el('explore').scrollIntoView()
}))
controls.forEach(id=>el(id).addEventListener('change',refresh))
el('ledger').closest('details').addEventListener('toggle',e=>{if(e.target.open)showLedger()})
el('matrixDetails').addEventListener('toggle',matrix)
el('csv').addEventListener('click',()=>{
  if(!current)return
  const {m,d,p,c}=current
  const rows=[['set','base_pf','base','end_pf','end','axis','block','symbol','entry_utc','exit_utc','size_multiple','unit_net_fraction','weighted_net_fraction']]
  for(const [id,mult] of m.trades){
    const [en,ex,sym,net]=d.orders[id]
    rows.push([d.name,meta.baseFloors[c.bf],meta.base[c.base],c.floor,meta.end[c.end]??'Base only',AXES[c.axis],BLOCKS[c.block].label,p.symbols[sym],utc(p,en),ex===null?'':utc(p,ex),mult,net??'',net===null?'':net*mult])
  }
  const csv=rows.map(r=>r.map(x=>'"'+String(x).replaceAll('"','""')+'"').join(',')).join('\r\n')
  const url=URL.createObjectURL(new Blob([csv],{type:'text/csv'})), a=document.createElement('a')
  a.href=url;a.download='cts-g-direct-base-executions.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)
})
if(meta.confirmation) {
  const f=meta.confirmation, p=f.period
  el('forwardHourly').innerHTML=grid(['Treatment','Hour starting UTC','Closes','Weighted net pp'],
    Object.entries(f.results).flatMap(([k,w])=>w.execution.hourNet.map((v,i)=>[k,utc(p,p.warm+60*i),w.execution.hourN[i],points(v)])))
} else { el('forwardHours').hidden=true }
el('source').innerHTML=grid(['Period','Symbol','Input SHA-256'],meta.periods.flatMap((p,i)=>p.symbols.map(s=>[i?'Holdout':'Development',s,p.manifest.sha256[s]])))
refresh()
