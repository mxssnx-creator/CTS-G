"""HTML for scripts/sim_report.py: one self-contained page (no network), charts and heatmaps drawn as SVG by a small
inline script from the embedded result JSON, and every simulated order listed line by line."""
from __future__ import annotations

import json
from typing import Any, Dict

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>24 h trade simulation</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b2030;--mut:#6a7388;--bd:#dfe3ea;--good:#1f9d5c;--bad:#d1434b;--acc:#3b6fe0;--warn:#c98a12}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#10131a;--card:#171b25;--fg:#e8ebf2;--mut:#98a1b5;--bd:#272d3b;--good:#3ccf86;--bad:#ef6a72;--acc:#6b95ff;--warn:#e3a928}}
:root[data-theme=dark]{--bg:#10131a;--card:#171b25;--fg:#e8ebf2;--mut:#98a1b5;--bd:#272d3b;--good:#3ccf86;--bad:#ef6a72;--acc:#6b95ff;--warn:#e3a928}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1280px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px}
.sub{color:var(--mut);font-size:12.5px}.mono{font-family:ui-monospace,Menlo,Consolas,monospace}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:14px 0}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px 14px}
.card .k{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}.card .v{font-size:24px;font-weight:650;margin-top:2px}
.card .s{font-size:12px;color:var(--mut)}
.note{background:var(--card);border:1px solid var(--bd);border-left:4px solid var(--acc);border-radius:8px;padding:10px 14px;margin:12px 0;font-size:13px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(460px,1fr));gap:12px}
.panel{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px 14px;overflow:auto}
table{border-collapse:collapse;width:100%;font-size:12.5px}th,td{padding:5px 8px;border-bottom:1px solid var(--bd);text-align:right;white-space:nowrap}
th{color:var(--mut);font-weight:600;position:sticky;top:0;background:var(--card)}td:first-child,th:first-child,td.l,th.l{text-align:left}
.good{color:var(--good)}.bad{color:var(--bad)}svg text{fill:var(--mut);font-size:10.5px}svg .t{fill:var(--fg)}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}select,input{background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:5px 8px;font:inherit}
.tw{max-height:560px;overflow:auto;border:1px solid var(--bd);border-radius:10px}
.legend{display:flex;gap:12px;font-size:12px;color:var(--mut);margin:4px 0}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
</style></head><body><main>
<h1>24 h trade simulation · Short Range, Indications and General, TP steps 2–6</h1>
<div class="sub" id="sub"></div>
<div class="note"><b>Two views.</b> <i>Set level</i>: every validated Set trades every signal (the matrix method; the order count is large because the Sets overlap).
<i>Picked</i>: in each pack and TP step the best valid Set of the hour takes the signal, one position per symbol, as live trading does. The order list below is the picked view.</div>
<div class="note"><b>Standard:</b> the result is the <b>PF after Base validation</b> (orders that only validated Sets could open) together with the
<b>share of Sets validated</b> and the order count. The unfiltered PF of all Sets is shown for reference only and is under 1 by design.</div>
<div class="cards" id="cards"></div>
<h2>Per pack and TP step (post-Base)</h2><div class="panel"><table id="steptbl"></table></div>
<h2>Cumulative net per TP step</h2><div class="panel"><div class="legend" id="leg1"></div><div id="cum"></div></div>
<h2>Heatmaps</h2><div class="grid2" id="heat"></div>
<h2>Distributions</h2><div class="grid2" id="dist"></div>
<h2>Every order, line by line</h2>
<div class="filters">
<select id="fpack"><option value="">all packs</option></select><select id="fstep"><option value="">all steps</option></select>
<select id="fsym"><option value="">all symbols</option></select><select id="fside"><option value="">both sides</option><option>LONG</option><option>SHORT</option></select>
<select id="freason"><option value="">all exits</option></select><span class="sub" id="fcount"></span></div>
<div class="tw"><table id="trades"></table></div>
</main>
<script>
const R=__DATA__;
const T=R.trades,M=R.meta,S=R.summary;
const $=id=>document.getElementById(id);
const fmt=(x,d=2)=>x===null||x===undefined?"–":Number(x).toFixed(d);
const cls=v=>v>=1.1?"good":v<1?"bad":"";
const PACKS={indications:"Indications",general:"General",shortrange:"Short Range"};
const COL=["#3b6fe0","#1f9d5c","#c98a12","#d1434b","#8a5cf5","#14a3b8"];
const hh=ms=>new Date(ms).toISOString().slice(11,16);
$("sub").textContent=`${M.symbols.length} symbols · ${M.hours} h window ${new Date(M.window_start).toISOString().slice(0,16)}Z → ${new Date(M.window_end).toISOString().slice(0,16)}Z · PositionCost ${M.cost_pct}% · Base: n ≥ ${M.gate_min}, PF(last ${M.gate_window}) ≥ ${M.min_pf}, DDT < ${M.max_ddt_h} h · min stop ${(1+M.sl_floor_steps)}× cost · ${M.sets} Sets · ${M.runtime_s}s`;
// cards
const ov=S.overall;const rows=S.rows;
const valAll=rows.reduce((a,r)=>a+r.validated_pct_hours*r.sets,0)/rows.reduce((a,r)=>a+r.sets,0);
const hrs={};T.forEach(t=>{(hrs[t.hour]=hrs[t.hour]||[]).push(t.net)});
const pfOf=a=>{let gp=0,gl=0;a.forEach(v=>v>0?gp+=v:gl-=v);return gl>0?gp/gl:(gp>0?99:0)};
const hp=Object.values(hrs).filter(a=>pfOf(a)>=1).length;
[["Post-Base PF",fmt(ov.pf),`${ov.orders} orders`,cls(ov.pf)],["Net after cost",fmt(ov.net_pct,1)+"%","sum over all orders",ov.net_pct>0?"good":"bad"],
 ["Sets validated",fmt(valAll,1)+"%","average share of Set-hours validated",""],
 ["Hours at PF ≥ 1",`${hp} / ${Object.keys(hrs).length}`,"hours with orders",""],
 ["Win rate",fmt(100*T.filter(t=>t.net>0).length/Math.max(1,T.length),1)+"%",`avg net ${fmt(T.reduce((a,t)=>a+t.net,0)/Math.max(1,T.length),3)}% / order`,""]]
 .forEach(([k,v,s,c])=>$("cards").insertAdjacentHTML("beforeend",`<div class="card"><div class="k">${k}</div><div class="v ${c}">${v}</div><div class="s">${s}</div></div>`));
// step table
$("steptbl").innerHTML="<tr><th class=l>Pack</th><th>Step</th><th>Sets</th><th>Validated % (Set-hours)</th><th>Validated % (ever)</th><th>Set-level orders</th><th>Set-level PF</th><th>Picked orders</th><th>Picked PF post-Base</th><th>Net %</th><th>Avg net %</th><th>Win %</th><th>Avg hold min</th></tr>"+
 rows.map(r=>`<tr><td class=l>${PACKS[r.pack]||r.pack}</td><td>${r.step}</td><td>${r.sets}</td><td>${fmt(r.validated_pct_hours,1)}</td><td>${fmt(r.validated_pct_ever,1)}</td><td>${r.set_orders}</td><td class="${cls(r.set_pf)}">${r.set_orders?fmt(r.set_pf):"–"}</td><td>${r.orders}</td><td class="${cls(r.pf)}">${r.orders?fmt(r.pf):"–"}</td><td class="${r.net_pct>0?"good":"bad"}">${fmt(r.net_pct,2)}</td><td>${fmt(r.avg_net_pct,3)}</td><td>${fmt(r.win_rate,1)}</td><td>${fmt(r.avg_hold_min,1)}</td></tr>`).join("");
// svg helpers
const svg=(w,h,body)=>`<svg viewBox="0 0 ${w} ${h}" width="100%" role="img">${body}</svg>`;
function heat(title,rowsL,colsL,get,opt){opt=opt||{};const cw=opt.cw||44,ch=opt.ch||22,lw=opt.lw||74,top=22,W=lw+colsL.length*cw+8,H=top+rowsL.length*ch+8;let b="";
 colsL.forEach((c,j)=>b+=`<text x="${lw+j*cw+cw/2}" y="14" text-anchor="middle">${c}</text>`);
 rowsL.forEach((r,i)=>{b+=`<text x="${lw-6}" y="${top+i*ch+ch/2+4}" text-anchor="end">${r}</text>`;
  colsL.forEach((c,j)=>{const g=get(i,j);const bg=g.bg||"transparent";b+=`<g><title>${g.tip||""}</title><rect x="${lw+j*cw}" y="${top+i*ch}" width="${cw-2}" height="${ch-2}" rx="3" fill="${bg}"/>${g.txt?`<text class="t" x="${lw+j*cw+cw/2-1}" y="${top+i*ch+ch/2+3}" text-anchor="middle" style="font-size:9.5px">${g.txt}</text>`:""}</g>`})});
 return `<div class="panel"><div class="sub" style="margin-bottom:6px"><b style="color:var(--fg)">${title}</b>${opt.sub?" · "+opt.sub:""}</div>${svg(W,H,b)}</div>`}
const pfColor=(pf,n)=>{if(!n)return "transparent";const x=Math.max(-1,Math.min(1,(pf-1)/0.6));return x>=0?`rgba(31,157,92,${0.15+0.7*x})`:`rgba(209,67,75,${0.15+0.7*-x})`};
const netColor=(v,sc)=>{if(!v)return "transparent";const x=Math.max(-1,Math.min(1,v/sc));return x>=0?`rgba(31,157,92,${0.15+0.7*x})`:`rgba(209,67,75,${0.15+0.7*-x})`};
const packs=M.packs,steps=[2,3,4,5,6],ratios=[...new Set(R.sets.filter(s=>s.kind=="base").map(s=>s.sl_ratio))].sort((a,b)=>a-b);
// heatmaps: step x SL ratio per pack (Set level, post-Base: orders of validated Sets)
packs.forEach(p=>{const cell={};R.sets.filter(x=>x.pack==p&&x.kind=="base").forEach(x=>{cell[x.step+"|"+x.sl_ratio]=x});
 $("heat").insertAdjacentHTML("beforeend",heat(`${PACKS[p]||p}: PF post-Base (Set level)`,steps.map(s=>"step "+s),ratios.map(r=>r.toFixed(2)),(i,j)=>{const x=cell[steps[i]+"|"+ratios[j]];if(!x)return {};return {bg:pfColor(x.exec_pf,x.orders),txt:x.orders?(x.exec_pf>=10?"9+":x.exec_pf.toFixed(1)):"",tip:`step ${steps[i]} · SL ratio ${ratios[j]} · ${x.orders} orders · PF ${fmt(x.exec_pf)} · validated ${x.validated_hours}/${M.hours} h · unfiltered PF ${fmt(x.shadow_pf)}`}},{cw:38,sub:"columns: SL ratio · cell: PF of the validated hours (green ≥ 1, red < 1), blank = never validated"}))});
// heatmap: validated Sets per step x hour (Short Range first)
packs.forEach(p=>{const cells=steps.map(s=>R.validated.map(v=>{const ids=R.sets.filter(x=>x.pack==p&&x.kind=="base"&&x.step==s);const n=ids.filter(x=>v.by_set[x.id]).length;return [n,ids.length]}));
 $("heat").insertAdjacentHTML("beforeend",heat(`${PACKS[p]||p}: validated Sets per hour`,steps.map(s=>"step "+s),R.validated.map(v=>v.hour),(i,j)=>{const [n,t]=cells[i][j];const x=t?n/t:0;return {bg:t&&n?`rgba(59,111,224,${0.12+0.78*x})`:"transparent",txt:t?n:"",tip:`step ${steps[i]} · hour ${j} · ${n}/${t} Sets validated`}},{cw:26,sub:"cell: validated base Sets out of 13 SL ratios"}))});
// heatmap: symbol x hour net
{const cell={};T.forEach(t=>{const k=t.symbol+"|"+t.hour;cell[k]=(cell[k]||0)+t.net});const sc=Math.max(0.5,...Object.values(cell).map(Math.abs))*0.7;
 $("heat").insertAdjacentHTML("beforeend",heat("Net % per symbol and hour",M.symbols.map(s=>s.replace("-USDT","")),Array.from({length:M.hours},(_,h)=>h),(i,j)=>{const v=cell[M.symbols[i]+"|"+j]||0;return {bg:netColor(v,sc),txt:v?v.toFixed(1):"",tip:`${M.symbols[i]} · hour ${j} · net ${fmt(v,2)}%`}},{cw:30,ch:18,lw:60,sub:"sum of net % of the orders closed"}))}
// heatmap: orders per pack step x hour
{const cell={};T.forEach(t=>{const k=t.pack+"|"+t.step+"|"+t.hour;cell[k]=(cell[k]||0)+1});const labels=[];packs.forEach(p=>steps.forEach(s=>labels.push([p,s])));const mx=Math.max(1,...Object.values(cell));
 $("heat").insertAdjacentHTML("beforeend",heat("Orders per pack, step and hour",labels.map(([p,s])=>(PACKS[p]||p).slice(0,6)+" s"+s),Array.from({length:M.hours},(_,h)=>h),(i,j)=>{const v=cell[labels[i][0]+"|"+labels[i][1]+"|"+j]||0;return {bg:v?`rgba(201,138,18,${0.12+0.78*v/mx})`:"transparent",txt:v||"",tip:`${labels[i][0]} step ${labels[i][1]} · hour ${j} · ${v} orders`}},{cw:26,ch:16,lw:92}))}
// cumulative net per step (all packs, base+trail by step)
{const series=[];packs.forEach(p=>steps.forEach(s=>{const a=T.filter(t=>t.pack==p&&t.step==s&&t.kind=="base").sort((x,y)=>x.t_out-y.t_out);if(a.length)series.push({name:`${PACKS[p]||p} s${s}`,pts:a})}));
 const W=1180,H=300,l=44,b=24,t0=M.window_start,t1=M.window_end;let cum=series.map(s=>{let c=0;return s.pts.map(t=>[t.t_out,c+=t.net])});
 const all=cum.flat();const lo=Math.min(0,...all.map(p=>p[1])),hi=Math.max(1,...all.map(p=>p[1]));const X=t=>l+(t-t0)/(t1-t0)*(W-l-8),Y=v=>H-b-(v-lo)/(hi-lo)*(H-b-10);
 let body=`<line x1="${l}" x2="${W-8}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--bd)"/>`;for(let k=0;k<=4;k++){const v=lo+(hi-lo)*k/4;body+=`<text x="${l-6}" y="${Y(v)+3}" text-anchor="end">${v.toFixed(0)}</text>`}
 for(let h=0;h<=M.hours;h+=3){const t=t0+(t1-t0)*h/M.hours;body+=`<text x="${X(t)}" y="${H-6}" text-anchor="middle">${hh(t)}</text>`}
 cum.forEach((c,i)=>{body+=`<polyline fill="none" stroke="${COL[i%COL.length]}" stroke-width="1.6" opacity=".9" points="${c.map(p=>X(p[0]).toFixed(1)+","+Y(p[1]).toFixed(1)).join(" ")}"/>`});
 $("cum").innerHTML=svg(W,H,body);$("leg1").innerHTML=series.map((s,i)=>`<span><i style="background:${COL[i%COL.length]}"></i>${s.name}</span>`).join("")}
// distributions: exits per step (stacked), hold histogram, PF per hour, per symbol net
function bars(title,labels,vals,color,fmtv){const W=560,H=210,l=40,b=40,mx=Math.max(1e-9,...vals.map(Math.abs)),mn=Math.min(0,...vals);const hi=Math.max(0,...vals);const Y=v=>10+(hi-v)/(hi-mn||1)*(H-b-10);const bw=(W-l-8)/labels.length;let s="";
 labels.forEach((lab,i)=>{const y0=Y(0),y1=Y(vals[i]);s+=`<rect x="${l+i*bw+2}" y="${Math.min(y0,y1)}" width="${bw-4}" height="${Math.max(1,Math.abs(y1-y0))}" fill="${typeof color=="function"?color(vals[i]):color}" rx="2"><title>${lab}: ${fmtv?fmtv(vals[i]):vals[i]}</title></rect><text x="${l+i*bw+bw/2}" y="${H-b+12}" text-anchor="middle" transform="rotate(${labels.length>12?35:0} ${l+i*bw+bw/2} ${H-b+12})">${lab}</text>`});
 s+=`<line x1="${l}" x2="${W-8}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--bd)"/>`;[mn,0,hi].forEach(v=>s+=`<text x="${l-5}" y="${Y(v)+3}" text-anchor="end">${v.toFixed(v%1?1:0)}</text>`);
 return `<div class="panel"><div class="sub" style="margin-bottom:6px"><b style="color:var(--fg)">${title}</b></div>${svg(W,H,s)}</div>`}
{const reasons=[...new Set(T.map(t=>t.reason))].sort();const rc={};T.forEach(t=>{const k=t.reason;rc[k]=(rc[k]||0)+1});
 $("dist").insertAdjacentHTML("beforeend",bars("Orders by exit reason",reasons,reasons.map(r=>rc[r]),"#3b6fe0"));
 const hb=[0,5,10,20,40,80,160,360,1e9],hl=hb.slice(0,-1).map((a,i)=>`${a}-${hb[i+1]>1e8?"∞":hb[i+1]}m`),hv=hl.map((_,i)=>T.filter(t=>t.hold_s/60>=hb[i]&&t.hold_s/60<hb[i+1]).length);
 $("dist").insertAdjacentHTML("beforeend",bars("Hold time (orders)",hl,hv,"#8a5cf5"));
 const ph=Array.from({length:M.hours},(_,h)=>(hrs[h]?Math.min(9.99,pfOf(hrs[h])):0));
 $("dist").insertAdjacentHTML("beforeend",bars("PF per hour (post-Base, all packs)",ph.map((_,h)=>h),ph,v=>v>=1?"#1f9d5c":"#d1434b",v=>v.toFixed(2)));
 const sn=M.symbols.map(s=>T.filter(t=>t.symbol==s).reduce((a,t)=>a+t.net,0));
 $("dist").insertAdjacentHTML("beforeend",bars("Net % per symbol",M.symbols.map(s=>s.replace("-USDT","")),sn,v=>v>=0?"#1f9d5c":"#d1434b",v=>v.toFixed(2)+"%"));
 const sp=steps.map(s=>T.filter(t=>t.step==s&&t.kind=="base").reduce((a,t)=>a+t.net,0));
 $("dist").insertAdjacentHTML("beforeend",bars("Net % per TP step (all packs)",steps.map(s=>"step "+s),sp,v=>v>=0?"#1f9d5c":"#d1434b",v=>v.toFixed(2)+"%"));
 const pp=packs.map(p=>T.filter(t=>t.pack==p).reduce((a,t)=>a+t.net,0));
 $("dist").insertAdjacentHTML("beforeend",bars("Net % per pack",packs.map(p=>PACKS[p]||p),pp,v=>v>=0?"#1f9d5c":"#d1434b",v=>v.toFixed(2)+"%"))}
// trades table
const opt=(id,vals)=>vals.forEach(v=>$(id).insertAdjacentHTML("beforeend",`<option value="${v}">${v}</option>`));
opt("fpack",packs);opt("fstep",[...steps,"trail"]);opt("fsym",M.symbols);opt("freason",[...new Set(T.map(t=>t.reason))].sort());
const sorted=T.slice().sort((a,b)=>a.t_out-b.t_out);
function draw(){const fp=$("fpack").value,fs=$("fstep").value,fy=$("fsym").value,fd=$("fside").value,fr=$("freason").value;
 const a=sorted.filter(t=>(!fp||t.pack==fp)&&(!fs||(fs=="trail"?t.kind=="trail":t.step==+fs&&t.kind=="base"))&&(!fy||t.symbol==fy)&&(!fd||t.side==fd)&&(!fr||t.reason==fr));
 $("fcount").textContent=`${a.length} of ${T.length} orders · net ${fmt(a.reduce((x,t)=>x+t.net,0),2)}% · PF ${fmt(pfOf(a.map(t=>t.net)))}`;
 $("trades").innerHTML="<tr><th>#</th><th class=l>Out (UTC)</th><th class=l>In (UTC)</th><th class=l>Symbol</th><th class=l>Pack</th><th>Step</th><th>SL ×</th><th class=l>Side</th><th>Entry</th><th>Exit</th><th class=l>Exit</th><th>Hold min</th><th>Gross %</th><th>Net %</th><th class=l>Set</th></tr>"+
  a.map((t,i)=>`<tr><td>${i+1}</td><td class=l>${new Date(t.t_out).toISOString().slice(5,16).replace("T"," ")}</td><td class=l>${new Date(t.t_in).toISOString().slice(5,16).replace("T"," ")}</td><td class=l>${t.symbol.replace("-USDT","")}</td><td class=l>${PACKS[t.pack]||t.pack}</td><td>${t.kind=="trail"?"trail":t.step}</td><td>${t.kind=="trail"?t.trail:t.sl_ratio}</td><td class=l>${t.side}</td><td>${t.entry}</td><td>${t.exit}</td><td class=l>${t.reason}${t.open_end?" *":""}</td><td>${fmt(t.hold_s/60,1)}</td><td>${fmt(t.gross,3)}</td><td class="${t.net>0?"good":"bad"}">${fmt(t.net,3)}</td><td class="l mono" style="font-size:11px">${t.set_id}</td></tr>`).join("")}
["fpack","fstep","fsym","fside","freason"].forEach(id=>$(id).addEventListener("change",draw));draw();
</script></body></html>
"""


def render(res: Dict[str, Any]) -> str:
    slim = {"meta": res["meta"], "trades": res["trades"], "sets": res["sets"], "validated": res["validated"],
            "summary": res["summary"]}
    return PAGE.replace("__DATA__", json.dumps(slim, separators=(",", ":")))
