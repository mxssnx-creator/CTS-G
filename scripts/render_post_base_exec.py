#!/usr/bin/env python3
"""Render a self-contained comparison of two completed Base → Exec studies."""
import argparse
import base64
import gzip
import hashlib
from itertools import product
import json
import math
from pathlib import Path


def case_key(row):
    return tuple(row[k] for k in ("basePf", "execPf", "window", "axis", "block"))


def admissible(candidate, control):
    """Development ranking uses settled PF, meaningful activity and drawdown."""
    return (candidate["closed"] >= 30 and candidate["pf"] is not None
            and control["pf"] is not None and candidate["pf"] > control["pf"] + .005
            and candidate["settledDrawdown"] <= 1.10 * control["settledDrawdown"] + 1e-12
            and (candidate["axis"] == "off" or candidate["axisResized"] >= 10)
            and (candidate["block"] == "off" or candidate["adds"] >= 3))


def paired(development, validation):
    controls, holdout = ({case_key(row): row for row in period["cases"]}
                         for period in (development, validation))
    if controls.keys() != holdout.keys():
        raise ValueError("replay matrices differ")
    if any(len(rows) != len(period["cases"]) for rows, period in
           ((controls, development), (holdout, validation))):
        raise ValueError("duplicate case keys")
    if development["end"] > validation["start"]:
        raise ValueError("validation decisions overlap the development period")
    for key in ("symbols", "sets", "costPct", "lookbackMinutes", "baseWindows", "execWindows",
                "overlayHash", "engineHashes"):
        if development[key] != validation[key]:
            raise ValueError(f"replay comparability check failed for {key}")
    for period in (development, validation):
        if [r["basePf"] for r in period["floors"]] != [1.5, 2.0, 2.5] or len(period["execWindows"]) != 60:
            raise ValueError("incomplete PF matrix")
        for row in period["cases"]:
            hours = row["hourly"]
            if len(hours) != period["hours"] or sum(r["closed"] for r in hours) != row["closed"]:
                raise ValueError("hourly settlement counts do not reconcile")
            if not math.isclose(math.fsum(r["net"] for r in hours), row["net"], abs_tol=1e-12):
                raise ValueError("hourly settled net does not reconcile")

    selected = []
    for base_floor, exec_floor in product((1.5, 2.0, 2.5), (2.0, 2.5)):
        root = (base_floor, exec_floor, None, "off", "off")
        for w in range(60):
            if (base_floor, exec_floor, w, "off", "off") not in controls:
                raise ValueError("missing isolated final-window case")
        control, check = controls[root], holdout[root]
        options = [r for k, r in controls.items() if k[:2] == root[:2] and k[2] is None and k != root]
        for label, axis, block in (("Axis only", True, False), ("Block only", False, True),
                                   ("Axis + Block", True, True)):
            pool = [r for r in options if (r["axis"] != "off", r["block"] != "off") == (axis, block)
                    and admissible(r, control)]
            best = max(pool, key=lambda r: (r["pf"], -r["settledDrawdown"], r["net"]), default=None)
            verified = holdout[case_key(best)] if best else None
            passed = (verified is not None and admissible(verified, check) and
                      verified["pf"] > 1 and best["pf"] > 1)
            selected.append({"basePf": base_floor, "execPf": exec_floor, "family": label,
                             "case": case_key(best) if best else None, "confirmed": bool(passed)})
    return selected


def encode_details(periods):
    """Keep exact path and symbol tables lazy, sharing identical strategy outcomes."""
    encoded, ids = [], {}
    for period in periods:
        for row in period["cases"]:
            detail = json.dumps({k: row.pop(k) for k in ("paths", "bySymbol")},
                                separators=(",", ":"), allow_nan=False).encode("utf-8")
            digest = hashlib.sha256(detail).digest()
            if digest not in ids:
                ids[digest] = len(encoded)
                encoded.append(base64.b64encode(gzip.compress(detail, compresslevel=4, mtime=0)).decode("ascii"))
            row["detail"] = ids[digest]
    return encoded


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CTS-G · Post-Base execution PF study</title>
<style>
:root{color-scheme:light;--ink:#152638;--muted:#435b6c;--line:#d7e1e8;--card:white;--accent:#086863;--rose:#a02c40}
*{box-sizing:border-box}body{margin:0;background:#ecf3f5;color:var(--ink);font:15px/1.52 system-ui,sans-serif}
main{max-width:1450px;margin:auto;padding:25px 24px 64px}header{background:#102c40;color:#f7fcfd;padding:36px 30px;border-radius:15px}
h1,h2,h3{line-height:1.2;margin:.4em 0}h1{font-size:clamp(29px,5vw,47px)}h2{font-size:25px}h3{font-size:17px}
p{margin:.5em 0 .85em;max-width:94ch}header p{color:#d2e5ea}.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:21px;margin-top:19px}
.scroll{overflow:auto;max-height:36rem;border:1px solid var(--line);border-radius:8px;margin:11px 0 4px}
table{border-collapse:collapse;width:100%;min-width:630px;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{position:sticky;top:0;background:#e7eff4;z-index:1;color:#213a4f}
tbody tr:nth-child(even){background:#f7fafb}.good{color:#08625a;font-weight:650}.bad{color:var(--rose);font-weight:650}
.sub{color:var(--muted)}small{font-size:13px}code{overflow-wrap:anywhere}a{color:#075d6b}
.filters{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:end;margin:15px 0;color:var(--muted)}
label{display:grid;gap:4px}select,input{color:var(--ink);background:#fff;padding:8px;border:1px solid #9bb1bf;border-radius:6px;font:inherit}
button.case{font:inherit;text-align:left;background:none;border:0;color:#075c68;text-decoration:underline;cursor:pointer;padding:0}
button.case[aria-pressed=true]{font-weight:800;color:#17374b}summary{cursor:pointer;font-weight:650;margin:10px 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere}
.chart{display:grid;grid-template-columns:repeat(var(--hours),minmax(12px,1fr));height:108px;margin:13px 0 0;min-width:400px;gap:3px;
background:linear-gradient(to bottom,transparent 49.5%,#9db0bd 50%,transparent 50.7%);overflow:hidden}
.chart span{position:relative}.chart i{display:block;position:absolute;width:100%;background:#168577;border-radius:2px 2px 0 0;bottom:50%}
.chart i.loss{bottom:auto;top:50%;background:#c04c5f;border-radius:0 0 2px 2px}.chart-label{display:flex;justify-content:space-between}
@media(max-width:600px){main{padding:12px}header,.panel{padding:16px}.filters label{max-width:100%}}
</style></head><body><main>
<header><p>CTS-G historical simulation</p><h1>Direct execution after Base</h1>
<p id="intro"></p></header>
<section class="panel" aria-labelledby="comparison"><h2 id="comparison">All six PF threshold pairs</h2>
<p class="sub">Each row admits the strongest qualifying final window across the full grid. Axis and Block are off for this comparison.</p>
<div class="scroll" id="headline"></div></section>
<section class="panel"><h2>Axis and Block selection</h2>
<p class="sub">The development rule requires at least 30 settled parents, a PF improvement greater than 0.005 and settled drawdown within 110% of the matching baseline. It also requires 10 Axis resizes or 3 settled Block adds for each enabled strategy. Within a family, the highest development PF wins, using lower drawdown and higher net to break ties. Confirmation applies the same comparison and requires PF above 1 in both periods.</p>
<div id="selection" class="scroll"></div></section>
<section class="panel"><h2>Final window and strategy matrix</h2>
<p class="sub">Coverage uses Base-qualified Set-minutes as its denominator. Final support requires a complete Short block immediately before a complete Last block from the same qualified Base path. Click a configuration to inspect its realized hours and exact paths.</p>
<div class="filters">
<label>Period<select id="period"><option value="0">Development</option><option value="1">Validation</option></select></label>
<label>Base min PF<select id="base"><option>1.5</option><option>2.0</option><option>2.5</option></select></label>
<label>Final min PF<select id="exec"><option>2.0</option><option>2.5</option></select></label>
<label>Comparison<select id="scope"><option value="windows">60 isolated signed windows</option><option value="portfolio">Portfolio with Axis and Block</option></select></label>
<label>Sort by<select id="sort"><option value="order">Configuration</option><option value="pf">Execute PF</option><option value="hours">Profitable hours</option><option value="orders">Closed positions</option></select></label>
</div>
<div class="scroll" id="matrix"></div>
<h3 id="detail-heading">Configuration detail</h3><p class="sub" id="detail-summary" aria-live="polite"></p>
<div class="scroll"><div class="chart" id="chart" role="img" aria-label="Hourly settled P&L"></div></div>
<small class="chart-label sub" id="chart-label"></small>
<div class="scroll" id="hourly"></div>
<details><summary>Exact traded Set and Base → Exec paths</summary><div class="scroll" id="paths"></div></details>
<details><summary>Settlement by symbol</summary><div class="scroll" id="symbols"></div></details>
<details id="parameter-details"><summary>Axis and Block parameters</summary><pre id="params"></pre></details>
</section>
<section class="panel"><h2>Base Last-N coverage</h2>
<p class="sub" id="base-intro"></p><details><summary>All 400 Base windows</summary><div class="scroll" id="base-grid"></div></details>
<details id="base-set-details"><summary>All native strategy Sets</summary><div class="filters"><label>Filter by Set or indication<input id="set-search" type="search"></label></div>
<p class="sub" id="set-total"></p><div class="scroll" id="base-sets"></div></details></section>
<section class="panel"><h2>Data and accounting</h2><div id="provenance"></div>
<p>Every decision reads only earlier closes. A Base lane uses disjoint Long, Short and Last blocks plus the native positive combined PF test. Its derived history contains positions admitted by that same Base lane when they entered. The final gate applies the two signed PF tests independently and uses the trade’s original direction. All outcomes retain their original sign.</p>
<p>Settled Execute PF divides gains by absolute losses. A profitable hour has positive net settled P&amp;L after costs. Quiet hours count in the denominator. A close at an exact hour boundary settles in the hour of its closing candle. Held positions and their valuation are reported separately.</p>
<p>The model uses completed 1-minute candles and native Set exits, with one open position per symbol and side. The portfolio uses equal initial notional and applies Axis weights and Block adds where selected. Percentages for net and drawdown are in units of one base position's notional. Drawdown uses settled equity. Block observes completed prices before the parent’s exit candle, starts with a fresh ledger per period and checks the parent’s exact current path before adding. The simulation omits funding, order book liquidity, tick order, venue lot rounding, actual fills and account margin constraints.</p>
<details><summary>Input hashes and strategy universe</summary><div class="scroll" id="hashes"></div><pre id="patch"></pre></details></section>
</main>
<script type="application/json" id="payload">__PAYLOAD__</script>
<script>
"use strict";
const R=JSON.parse(document.getElementById("payload").textContent), D=R.periods,
      $=id=>document.getElementById(id), sum=values=>values.reduce((a,v)=>a+v,0),
      nf=new Intl.NumberFormat("en-GB",{maximumFractionDigits:2}),
      df=new Intl.DateTimeFormat("en-GB",{timeZone:"Europe/Vienna",month:"short",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}),
      date=t=>df.format(new Date(t*1000)), num=n=>nf.format(n), pct=n=>num(n)+"%",
      punit=n=>pct(100*n), f=n=>Number(n).toFixed(1),
      esc=s=>String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]),
      pf=r=>r.pf!==null?r.pf.toFixed(3):r.gain>0?"∞":r.closed?"1.000":"—",
      phaseKey=r=>JSON.stringify([r.basePf,r.execPf,r.window,r.axis,r.block]),
      maps=D.map(period=>new Map(period.cases.map(r=>[phaseKey(r),r]))),
      sty=(text,c)=>'<span class="'+c+'">'+text+'</span>',
      nfmt=x=>sty(punit(x),x>1e-12?"good":x< -1e-12?"bad":""),
      pfmt=r=>sty(pf(r),r.pf>1||r.gain>0&&r.loss===0?"good":r.pf!==null&&r.pf<1?"bad":""),
      all=(b,e)=>JSON.stringify([b,e,null,"off","off"]),
      floorPairs=[1.5,2,2.5].flatMap(b=>[2,2.5].map(e=>[b,e]));
const execWindow=w=>w[0]+" "+w[1]+" / "+w[2]+" "+w[3];
const baseWindow=w=>w[0]+" / "+w[1]+" "+w[4]+" / "+w[2]+" "+w[3];
function strategyLabel(r){
  const a=r.axisSettings, b=r.blockSettings;
  const axis=a?"Axis "+(a.combos?"singles and pairs":"singles")+", "+num(a.vmax)+"× cap":"Axis off";
  if(!b)return axis+" / Block off";
  if(r.block==="overlay")return axis+" / Overlay Block";
  const count=b.blockCounts[0], shared=b.blockStackType==="shared";
  const sizing=shared?pct(100*b.blockSharedTotal)+" total":pct(100*count.ratio)+" each";
  const relation=b.blockRelations.map(x=>x[0].toUpperCase()+x.slice(1)).join(" + ");
  return axis+" / "+(shared?"Shared":"Additive")+" Block, "+num(count.step)+" costs, "+sizing+", "+relation;
}
const sign=s=>s[0];
const path=(d,x)=>{const [st,b,e]=x, bw=d.baseWindows[b], ew=d.execWindows[e];
  return esc(d.sets[st][0])+"/base:"+bw[0]+"-"+bw[1]+sign(bw[4])+"-"+bw[2]+sign(bw[3])+
         "/exec:0-"+ew[0]+sign(ew[1])+"-"+ew[2]+sign(ew[3]);};
function table(id,head,rows){
  $(id).innerHTML="<table><thead><tr>"+head.map(h=>'<th scope="col">'+h+"</th>").join("")+
    "</tr></thead><tbody>"+rows.map(r=>"<tr>"+r.map(v=>"<td>"+v+"</td>").join("")+"</tr>").join("")+"</tbody></table>";
}
const cells=r=>[pfmt(r),num(r.closed),pct(r.hourSuccessPct),punit(r.settledDrawdown)];
$("intro").textContent=D[0].symbols.length+" BingX perpetuals · "+D[0].sets.length.toLocaleString("en-GB")+
  " native Sets · "+D[0].hours+" decision hours in each period · "+D[0].costPct.toFixed(3)+"% modeled round-trip cost";
table("headline",["Base min PF","Final min PF","Dev Execute PF","Dev closes","Dev good hours","Dev drawdown",
 "Validation Execute PF","Validation closes","Validation good hours","Validation drawdown"],
 floorPairs.map(([b,e])=>[f(b),f(e),...cells(maps[0].get(all(b,e))),...cells(maps[1].get(all(b,e)))]));
table("selection",["Base / Final PF","Family","Development pick","Dev Execute PF","Dev adds / resizes",
 "Validation Execute PF","Validation adds / resizes","Result"],R.selection.map(s=>{
  const picked=s.case?maps.map(m=>m.get(JSON.stringify(s.case))):[];
  const activity=r=>num(r.adds)+" / "+num(r.axisResized);
  return [f(s.basePf)+" / "+f(s.execPf),s.family,
    picked.length?esc(strategyLabel(picked[0])):"No qualifying pick",
    picked.length?pfmt(picked[0]):"—",picked.length?activity(picked[0]):"—",
    picked.length?pfmt(picked[1]):"—",picked.length?activity(picked[1]):"—",
    s.confirmed?sty("Confirmed","good"):picked.length?"Below confirmation rule":"—"];}));
$("provenance").innerHTML=D.map(d=>"<p><strong>"+esc(d.label)+"</strong> · "+date(d.start)+" to "+date(d.end)+
  " Vienna time. Warmup starts "+date(d.prehistoryStart)+". Lookup window "+num(d.lookbackMinutes)+" minutes."+
  (d.source?' <a href="'+esc(d.source)+'">BingX candle endpoint</a>':"")+"</p>").join("");
table("hashes",["Symbol",...D.map(d=>esc(d.label)+" SHA-256")],D[0].symbols.map(s=>[esc(s),...D.map(d=>esc(d.inputHashes[s]))]));
$("patch").textContent=JSON.stringify(D[0].overlayPatch,null,2);
let detailRequest=0;
async function detail(d,r,b){
  const request=++detailRequest;
  document.querySelectorAll("button.case").forEach(el=>el.setAttribute("aria-pressed",String(el.dataset.key===phaseKey(r))));
  const label=r.window===null?"Portfolio · "+strategyLabel(r):execWindow(d.execWindows[r.window]);
  $("detail-heading").textContent=label;
  const targetHours=r.hourly.filter(h=>h.gain>0&&(h.loss===0||h.pf>=r.execPf)).length;
  $("detail-summary").textContent="Execute PF "+pf(r)+" · "+num(r.opened)+" opens · "+num(r.closed)+" closes · "+
    num(r.held)+" held · net "+punit(r.net)+" · held mark "+punit(r.heldMarkNet)+" · "+num(r.heldAdds)+" held adds · "+
    num(r.positiveHours)+"/"+d.hours+" profitable hours · "+num(r.activeHours)+" hours with settlements · "+
    num(targetHours)+"/"+d.hours+" hours at the final PF floor"+(r.closed?" · "+pct(100*r.wins/r.closed)+" winning closes":"");
  const max=Math.max(...r.hourly.map(h=>Math.abs(h.net)),1e-9);
  $("chart").style.setProperty("--hours",d.hours);
  $("chart").innerHTML=r.hourly.map((h,i)=>'<span title="'+esc(date(d.start+3600*i)+" "+punit(h.net))+
    '"><i class="'+(h.net<0?"loss":"")+'" style="height:'+(50*Math.abs(h.net)/max)+'%"></i></span>').join("");
  $("chart-label").innerHTML="<span>"+date(d.start)+"</span><span>"+date(d.end)+" Vienna time</span>";
  const j=Number(r.execPf===2.5), k=r.window??d.execWindows.length;
  table("hourly",["Vienna hour","Opens","Closes","Exec PF","Net","Block adds","Base-qualified Set-minutes","Final full","Final passes"],
    r.hourly.map((h,i)=>[date(d.start+i*3600),num(h.opened),num(h.closed),pfmt(h),nfmt(h.net),num(h.adds),
      num(b.eligible[i]),num(b.execFull[k][i]),num(b.execPassed[j][k][i])]));
  const vals=r=>[num(r.closed),num(r.held),num(r.long),num(r.short),pfmt(r),nfmt(r.net),num(r.adds)];
  const tail=["Closes","Held","Long closes","Short closes","Exec PF","Net","Settled adds"];
  $("parameter-details").hidden=!(r.axisSettings||r.blockSettings);
  $("params").textContent=JSON.stringify({axis:r.axisSettings??null,block:r.blockSettings??null},null,2);
  for(const id of ["paths","symbols"])$(id).textContent="Loading settlement details…";
  try{
    const compressed=Uint8Array.from(atob(R.details[r.detail]),c=>c.charCodeAt(0));
    const stream=new Blob([compressed]).stream().pipeThrough(new DecompressionStream("gzip"));
    const unpacked=await new Response(stream).json();
    if(request!==detailRequest)return;
    table("paths",["Set path",...tail],unpacked.paths.map(x=>[path(d,x),...vals(x[3])]));
    table("symbols",["Symbol",...tail],unpacked.bySymbol.map(x=>[esc(x[0]),...vals(x[1])]));
  }catch(error){
    if(request===detailRequest)for(const id of ["paths","symbols"])$(id).textContent="Settlement details could not be opened in this browser.";
  }
}
function sets(d,b){
  const q=$("set-search").value.trim().toLowerCase(), valid=new Map(b.baseSets);
  const found=d.sets.map((s,i)=>[s,valid.get(i)||0]).filter(([s])=>s.join(" ").toLowerCase().includes(q));
  $("set-total").textContent=num(found.length)+" Sets";
  table("base-sets",["Set","Indication","Type","Side","TP range","Base-qualified minutes"],
    found.map(([s,n])=>[esc(s[0]),esc(s[1]),esc(s[2]),s[3]>0?"Long":s[3]<0?"Short":"Both",esc(s[4]),num(n)]));
}
function refresh(){
  const d=D[Number($("period").value)], floor=Number($("base").value), exec=Number($("exec").value);
  const b=d.floors.find(x=>x.basePf===floor), wide=$("scope").value==="portfolio", j=Number(exec===2.5),
        eligible=sum(b.eligible);
  const cases=d.cases.filter(r=>r.basePf===floor&&r.execPf===exec&&(r.window===null)===wide);
  const order=$("sort").value, pfRank=r=>r.pf??(r.gain>0?Infinity:r.closed?1:-1);
  if(order==="pf")cases.sort((a,b)=>pfRank(b)-pfRank(a)||b.closed-a.closed);
  if(order==="hours")cases.sort((a,b)=>b.positiveHours-a.positiveHours||b.closed-a.closed);
  if(order==="orders")cases.sort((a,b)=>b.closed-a.closed);
  table("matrix",[wide?"Axis / Block":"Short / Last","Set-minutes with full samples","Coverage","Qualified","Opens","Closes",
    "Execute PF","Net","Good hours","Drawdown","Adds","Axis resizes"],cases.map(r=>{
    const k=r.window??d.execWindows.length, n=sum(b.execFull[k]);
    const label=wide?strategyLabel(r):execWindow(d.execWindows[k]);
    return ['<button class="case" type="button" aria-pressed="false" data-key="'+esc(phaseKey(r))+'">'+esc(label)+"</button>",
      num(n),eligible?pct(100*n/eligible):"—",num(sum(b.execPassed[j][k])),num(r.opened),num(r.closed),pfmt(r),nfmt(r.net),
      pct(r.hourSuccessPct),punit(r.settledDrawdown),num(r.adds),num(r.axisResized)];}));
  $("matrix").querySelectorAll("button.case").forEach((el,i)=>el.onclick=()=>detail(d,cases[i],b));
  detail(d,cases.find(r=>r.axis==="off"&&r.block==="off"&&r.closed)||cases[0],b);
  $("base-intro").textContent=d.label+" · Base min PF "+f(floor)+" · "+num(eligible)+" Base-qualified Set-minutes of "+
    num(b.setMinutes)+" possible Set-minutes. A Set-minute is counted once in this headline even if multiple Base windows passed.";
  table("base-grid",["Long / Short / Last","Complete samples","Coverage","Passed"],d.baseWindows.map((w,i)=>
    [baseWindow(w),num(b.baseFull[i]),pct(100*b.baseFull[i]/b.setMinutes),num(b.basePassed[i])]));
  if($("base-set-details").open)sets(d,b);
  $("base-set-details").ontoggle=()=>{if($("base-set-details").open)sets(d,b);};
  $("set-search").oninput=()=>sets(d,b);
}
for(const id of ["period","base","exec","scope","sort"])$(id).onchange=refresh;
refresh();
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    runs = [json.loads(p.read_text()) for p in (args.development, args.validation)]
    selection = paired(*runs)
    details = encode_details(runs)
    payload = json.dumps({"periods": runs, "selection": selection, "details": details},
                         separators=(",", ":"), allow_nan=False)
    args.out.write_text(TEMPLATE.replace("__PAYLOAD__", payload.replace("<", r"\u003c")), encoding="utf-8")
    print(f"{len(runs[0]['cases'])} cases per period · {args.out.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
