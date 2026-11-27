#!/usr/bin/env python3
"""Render the frozen forward results and research scans as one offline HTML report."""
import argparse
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path

from check_post_base_forward import assess, case_key, read


TITLE = "CTS-G · Direct post-Base forward validation"


def clock(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %H:%M")


def factor(row):
    if row["pf"] is not None:
        return f'{row["pf"]:.3f}'
    return "∞" if row["gain"] else "—"


def grid(headers, rows):
    head = "".join(f'<th scope="col">{escape(str(cell))}</th>' for cell in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="scroll" tabindex="0"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def write_report(output, result, research, evaluation, stress, scans):
    period_names = {
        "development": "Research · Nov 19–20", "validation": "Research · Nov 26",
        "fresh": "Nov 27 · fresh", "fresh-stress": "Nov 27 · higher slippage",
        "nov10": "Nov 10", "nov11": "Nov 12", "nov16": "Nov 15", "nov25": "Nov 24",
    }
    documents = [*research, *sorted(evaluation, key=lambda run: run["start"]), stress]
    for run in [*documents, *scans]:
        run["title"] = period_names.get(run["label"].removesuffix("-scan"), run["label"])
    keys = [case_key(row) for row in result["cases"]]
    primary, control = keys[0], next(key for key in keys if key[3] == "combined")
    fixed = []
    hourly, hour_index = [], {}
    for run in documents:
        by_case = {case_key(row): row for row in run["cases"]}
        run["cases"] = [by_case[key] for key in keys]
        first, overview = by_case[primary], by_case[control]
        fixed.append([run["title"], f'{clock(run["start"])}–{clock(run["end"])}',
                      factor(first), first["closed"], f'{first["positiveHours"]}/{len(first["hourly"])}',
                      factor(overview), overview["closed"], f'{overview["positiveHours"]}/{len(overview["hourly"])}'])
        for case in run["cases"]:
            for variant in case["axisBlock"]:
                key = tuple(tuple(row.values()) for row in variant["hourly"])
                if key not in hour_index:
                    hour_index[key] = len(hourly)
                    hourly.append(variant["hourly"])
                variant["hourly"] = hour_index[key]
    lookup = [{case_key(row): row for row in panel["cases"]} for panel in scans]
    if not lookup or any(panel.keys() != lookup[0].keys() for panel in lookup):
        raise ValueError("research scans do not cover the same cases")
    scans_combined = []
    for key in lookup[0]:
        rows = [panel[key] for panel in lookup]
        scans_combined.append([*key, [[r["pf"], r["gain"], r["closed"], r["hourSuccessPct"]] for r in rows]])
    supported = [row for row in scans_combined if min(cell[2] for cell in row[-1]) >= 30]
    uniformly_positive = sum(all(cell[0] is not None and cell[0] > 1 for cell in row[-1]) for row in supported)
    for row in scans_combined:
        row[-1] = [[None if pf is None else round(pf, 3), bool(gain), n, round(hours, 1)]
                   for pf, gain, n, hours in row[-1]]
    evaluated = len(result["cases"])
    verified = sum(r["eligible"] for r in result["cases"])
    latest = evaluation[-1]
    settled = round(sum((run["end"] - run["start"]) / 3600 for run in evaluation))
    report = (
        TEMPLATE.replace("%%TITLE%%", TITLE)
        .replace("%%RELEASE%%", "Ready for release review" if verified else "Production promotion blocked")
        .replace("%%SUMMARY%%", f"{verified} of {evaluated} frozen configurations passed the release check. "
                 f"The comparison used {settled} distinct evaluation hours and {len(latest['symbols'])} symbols per panel.")
        .replace("%%PERIODS%%", grid(["Replay period", "Window (UTC)", "Step 10 PF", "Closes", "Profitable hours",
                                    "Combined PF", "Closes", "Profitable hours"], fixed))
        .replace("%%SCAN%%", f"{len(scans_combined):,} configurations across {len(scans)} research and retrospective windows. "
                 f"{len(supported):,} recorded at least 30 closes in every window. "
                 f"{uniformly_positive} of those had PF above 1 in every window.")
        .replace("%%DATA%%", json.dumps(dict(documents=documents, decision=result["cases"], hourly=hourly,
                                              scan=scans_combined, scanTitles=[p["title"] for p in scans]),
                                         separators=(",", ":"), allow_nan=False).replace("<", "\\u003c"))
    )
    output.write_text(report, encoding="utf-8")


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%%TITLE%%</title>
<style>
:root{color-scheme:light;--ink:#182c3a;--muted:#465d6a;--rule:#d8e3e8;--wash:#eef3f5;--link:#006865}
*{box-sizing:border-box}body{margin:0;background:var(--wash);color:var(--ink);font:15px/1.55 system-ui,sans-serif}
main{max-width:1430px;margin:auto;padding:24px 23px 52px}header{background:#123142;color:#f5fafb;padding:32px;border-radius:12px}
header p{color:#d8ecef}h1,h2,h3{line-height:1.2;margin:.4em 0}h1{font-size:clamp(29px,5vw,44px)}h2{font-size:24px}
h3{font-size:17px;margin-top:19px}p{margin:.5em 0 .8em;max-width:100ch}.status{font-weight:700;color:#ffe5aa}
section{background:#fff;border:1px solid var(--rule);border-radius:12px;padding:20px;margin-top:16px}
.scroll{overflow:auto;max-height:31rem;margin:12px 0 7px;border:1px solid var(--rule);border-radius:6px}
table{border-collapse:collapse;width:100%;min-width:625px;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:8px 10px;border-bottom:1px solid var(--rule);white-space:nowrap;text-align:right}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}th{position:sticky;top:0;background:#e8f0f3;color:#254050}
tbody tr:nth-child(even){background:#f7fafb}a{color:var(--link)}.sub{color:var(--muted)}
.controls{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:end;margin:12px 0 5px}
label{display:grid;gap:4px;color:var(--muted)}select,input,button{font:inherit;color:var(--ink);border:1px solid #9eb1bf;
border-radius:5px;padding:7px;background:white;max-width:100%}label,input,select{min-width:0}button{cursor:pointer}button:disabled{opacity:.45;cursor:default}
:focus-visible{outline:3px solid #13887f;outline-offset:2px}summary{cursor:pointer;color:var(--link);font-weight:650;margin-top:10px}
.chart{min-width:410px;display:grid;height:96px;gap:4px;grid-template-columns:repeat(var(--n),1fr);position:relative;
background:linear-gradient(to bottom,transparent 49%,#a5b7c2 50%,transparent 51%);padding:0 4px}.chart span{position:relative}.chart i{position:absolute;left:0;right:0;bottom:50%;background:#137d70;min-height:1px}
.chart i.neg{top:50%;bottom:auto;background:#ad3c54}.chartbox{overflow:auto;margin:.6em 0}
code{overflow-wrap:anywhere}ul{padding-left:1.4em}.notice{padding:.55em .65em;border-left:3px solid #b48120;background:#fffaeb}
@media(max-width:640px){main{padding:10px}header,section{padding:14px}.controls label{max-width:100%}}
</style></head><body><main>
<header><h1>Direct post-Base forward validation</h1><p class="status">%%RELEASE%%</p><p>%%SUMMARY%%</p></header>
<section><h2>Settled execution</h2>
<p>The primary candidate is the 5-minute Bollinger short, step 10, Base PF 1.5 and final PF 2.0. The combined control uses Bollinger and MACD, Base PF 2.0 and final PF 2.5. Profitable hours count positive settled P&amp;L among all scored hours.</p>
%%PERIODS%%
</section>
<section><h2>Frozen configurations</h2>
<div class="controls"><label>Configuration<select id="case"></select></label><label>Period<select id="period"></select></label><label>Sizing<select id="variant"></select></label></div>
<p class="notice" id="verdict" aria-live="polite"></p>
<details><summary>Release checks by period</summary><ul id="failures"></ul></details>
<p id="summary" aria-live="polite"></p>
<h3>Hourly net P&amp;L</h3><div class="chartbox"><div class="chart" id="chart" role="img" aria-label="Net P and L by hour, with negative bars below zero"></div></div>
<div id="hours"></div>
<details><summary>Baseline results by exact path and symbol</summary><div id="paths"></div><div id="symbols"></div></details>
<details><summary>Axis and Block sizing comparisons</summary><p class="sub">Profiles share the same parent entries. The table lists actual simulated resizes and settled adds.</p><div id="mods"></div></details>
</section>
<section><h2>Configuration scan</h2><p>%%SCAN%%</p>
<div class="controls"><label>Set or filter<input id="filter" type="search" placeholder="Search configurations"></label>
<label>Sample support<select id="sample"><option value="0">All configurations</option><option value="30">30 closes per period</option></select></label>
<button type="button" id="prev">Previous</button><button type="button" id="next">Next</button></div>
<p id="page" class="sub" aria-live="polite"></p><div id="scan"></div></section>
<section><h2>Replay methodology</h2>
<p>Each scenario uses 48 hours of prehistory, then a separate scoring window. Signals use completed candles and enter at the next minute’s opening. A stop active at the opening wins a same-candle stop and target tie. Fees apply to the actual simulated entry and exit notionals. The baseline models 0.05% per side with 0.5 basis points of adverse slippage per side. The stress replay adds one basis point per side. <a href="https://bingxservice.zendesk.com/hc/en-001/articles/11263240298255-Perpetual-Futures-Fee-Schedule">BingX fee schedule</a></p>
<p>The native Base grid evaluates 400 disjoint Long, Short and Last combinations. The terminal grid checks 60 combinations. Positive Short uses 8, 12, 16 and 20 closes, negative Short uses 8 and 12, and both Last signs use 1–5. A negative gate measures the magnitude of loss relative to gain. The direction and P&amp;L of the trade retain their original sign. Directional proof uses earlier settled positions admitted by the same Base path. Each configuration’s fills, cooldown and concurrent positions are simulated independently.</p>
<p>All figures use units of starting parent notional. Drawdown groups settlements by minute. The release check requires PF above 1 and positive net including marks in each independent period and the stressed replay. It requires at least 20 closes per 24-hour window, eight in the fresh eight-hour window, and 100 across all distinct evaluation windows. Scored windows are disjoint. Production evidence still requires venue fills, lot-size checks, liquidity, and funding.</p>
<details><summary>Market data checksums</summary><div id="data-source"></div></details>
</section>
</main>
<script type="application/json" id="data">%%DATA%%</script>
<script>
"use strict";
const $ = (id) => document.getElementById(id);
const E = (tag,text) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; return n; };
const data = JSON.parse($("data").textContent);
const pf = (value,gain) => value === null ? (gain ? "∞" : "—") : value.toFixed(3);
const num = (value) => value.toFixed(5);
const hour = (time) => new Date(time*1000).toISOString().slice(11,16);
const pct = (value) => value.toFixed(1)+"%";
const title = (r) => r.scope+" · "+r.base.toFixed(1)+" / "+r.final.toFixed(1)+(r.proof ? " · "+r.proof.join(" @ ") : "");
function table(id, headers, rows) {
  const box = E("div"); box.className = "scroll"; box.tabIndex = 0;
  const tab = E("table"), top = E("thead"), line = E("tr"), body = E("tbody");
  for (const value of headers) { const cell = E("th",value); cell.scope = "col"; line.append(cell); }
  top.append(line);
  for (const row of rows) { const tr = E("tr"); for (const v of row) tr.append(E("td",v)); body.append(tr); }
  tab.append(top,body); box.append(tab); $(id).replaceChildren(box);
}
for (const [i,r] of data.decision.entries()) { const opt=E("option",title(r)); opt.value=i; $("case").append(opt); }
for (const [i,r] of data.documents.entries()) { const opt=E("option",r.title); opt.value=i; $("period").append(opt); }
$("period").value = data.documents.length-2;
function lane(w) { return (w.long === undefined ? "" : w.long+" / ")+w.short+w.short_mode[0]+" / "+w.last+w.last_mode[0]; }
function draw() {
  const i=Number($("case").value), view=data.documents[Number($("period").value)], base=view.cases[i], d=data.decision[i];
  const pick=Number($("variant").value), chosen=base.axisBlock[pick], r=chosen || base;
  const hours=chosen ? data.hourly[chosen.hourly] : base.hourly;
  $("variant").replaceChildren(...base.axisBlock.map((v,j) => {
    const option=E("option",v.axis+" / "+v.block); option.value=j; return option;
  }));
  $("variant").value=pick;
  $("verdict").textContent = d.eligible ? "Cleared the independent replay check." : "Release check blocked this configuration.";
  $("failures").replaceChildren(...d.reasons.map((v) => E("li",v)));
  $("summary").textContent = "Execute PF "+pf(r.pf,r.gain)+" · "+r.opened+" opened · "+r.closed+" settled · "
    +r.held+" held · profitable hours "+r.positiveHours+"/"+hours.length+" ("+pct(r.hourSuccessPct)+") · settled net "
    +num(r.net)+" · held marks "+num(r.heldMarkNet)+" · minute drawdown "+num(r.settledDrawdown)+".";
  table("hours",["Hour (UTC)","Opened","Closed","PF","Net"],hours.map((v,j) =>
    [hour(view.start+j*3600),v.opened,v.closed,pf(v.pf,v.gain),num(v.net)]));
  const most=Math.max(1e-12,...hours.map((v)=>Math.abs(v.net)));
  $("chart").style.setProperty("--n",hours.length);
  $("chart").replaceChildren(...hours.map((v,j) => {
    const x=E("span"); x.title=hour(view.start+j*3600)+" UTC "+num(v.net);
    if(v.net) { const b=E("i"); b.className=v.net<0?"neg":""; b.style.height=46*Math.abs(v.net)/most+"%"; x.append(b); }
    return x;
  }));
  table("paths",["Set","Base Long / Short / Last","Final Short / Last","Closes","PF","Net"],base.paths.map(([s,b,e,v]) =>
    [view.setDefinitions[s],lane(view.baseWindows[b]),lane(view.execWindows[e]),v.closed,pf(v.pf,v.gain),num(v.net)]));
  table("symbols",["Symbol","Closes","PF","Net"],base.bySymbol.map(([s,v]) => [s,v.closed,pf(v.pf,v.gain),num(v.net)]));
  table("mods",["Axis","Block","Closed","Resized","Settled adds","Held adds","PF","Profitable hours","Drawdown"],
    base.axisBlock.map((v) => [v.axis,v.block,v.closed,v.axisResized,v.adds,v.heldAdds,pf(v.pf,v.gain),
      pct(v.hourSuccessPct),num(v.settledDrawdown)]));
  table("data-source",["Symbol","SHA-256"],Object.entries(view.inputHashes));
}
let page=0;
function scan() {
  const text=$("filter").value.trim().toLowerCase(), size=Number($("sample").value);
  const rows=data.scan.filter(r => (r[3]+" "+r[4]).toLowerCase().includes(text) && r[5].every(v => v[2]>=size));
  const limit=Math.max(1,Math.ceil(rows.length/50)); page=Math.min(page,limit-1);
  $("prev").disabled=page===0; $("next").disabled=page+1>=limit;
  $("page").textContent=rows.length.toLocaleString()+" matching configurations · page "+(page+1)+" of "+limit+
    " · each period cell shows PF / closes / profitable hours.";
  table("scan",["Set or pack","Entry filter","Base PF","Final PF","Directional proof",...data.scanTitles],
    rows.slice(page*50,page*50+50).map(([b,f,p,s,m,a]) =>
      [s,m,b.toFixed(1),f.toFixed(1),p.length?p.join(" @ "):"Off",...a.map(v=>pf(v[0],v[1])+" / "+v[2]+" / "+pct(v[3]))]));
}
for (const id of ["case","period","variant"]) $(id).addEventListener("change",draw);
$("filter").addEventListener("input",()=>{page=0;scan();});
$("sample").addEventListener("change",()=>{page=0;scan();});
$("prev").addEventListener("click",()=>{page--;scan();});
$("next").addEventListener("click",()=>{page++;scan();});
draw(); scan();
</script></body></html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--research", type=Path, nargs="+", required=True)
    parser.add_argument("--evaluate", type=Path, nargs="+", required=True)
    parser.add_argument("--stress", type=Path, required=True)
    parser.add_argument("--scan", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    research, evaluation = ([read(path) for path in paths] for paths in (args.research, args.evaluate))
    stress = read(args.stress)
    result = assess(args.selection, research, evaluation, stress)
    write_report(args.out, result, research, evaluation, stress, [read(path) for path in args.scan])


if __name__ == "__main__":
    main()
