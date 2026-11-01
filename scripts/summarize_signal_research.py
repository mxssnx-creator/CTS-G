"""Readable scenario, hourly and per-grid stage evidence from frozen results."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path


def load(path):return json.loads(gzip.decompress(path.read_bytes()))
def pf(gain,loss):return f"{gain/loss:.6f}" if loss else "no-loss" if gain else "empty/flat"
def metric(row):return f"{row['pf']:.6f}" if row.get("pf") is not None else row.get("pfState","unknown")
def stamp(t):return datetime.fromtimestamp(t/1000,timezone.utc).strftime("%m-%d %H:%M")


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenario-dir",type=Path,required=True)
    ap.add_argument("--summary",type=Path,required=True)
    ap.add_argument("--grid",type=Path,required=True)
    ap.add_argument("--focus",default="expanded")
    args=ap.parse_args()
    if args.summary.exists() or args.grid.exists():ap.error("preserve previous summaries")
    results={p.name.removesuffix(".json.gz"):load(p) for p in sorted(args.scenario_dir.glob("*.json.gz"))}
    if args.focus not in results:ap.error("focus scenario is absent")
    floors=sorted({r["config"]["minPf"] for r in results.values()})
    lines=["# Signals retrospective diagnostic — 1 November 2026", "",
      "Offline experiment only. No x02/demo orders, production integration or live acceptance. No configuration is promoted.","",
      "Exactly eight archived symbols, 12 hours prehistory and 12 hours modeled trading. No hidden earlier warmup. Window 8 October 2026 12:00–9 October 2026 12:00 UTC (14:00–14:00 Europe/Vienna); the second half is trading. The archive has been studied before: this is **not an untouched holdout**.","",
      f"Per-leg fees use each leg's own notional, with adverse entry/exit slippage. Configured PF floors: {floors}. A maker scenario is an assumed fill, not proof of a maker execution. Fractions and source identities are frozen in adjacent `.config.json` files. A `-frozen` case admits only exact contexts qualifying before trading; dynamic cases can qualify later. All ongoing gates remain active.","",
      "## Modeled execution, including losses", "",
      "| Scenario | Contexts | Settled / still open | Closed-return PF | Equity change | Mark drawdown % | Positive / negative / flat hours | Seconds |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name,r in results.items():
        h=r["hourly"]["counts"];c=r["modelCounts"]
        lines.append(f"| {name} | {len(r['contexts'])} | {c['ordersSettled']} / {c['ordersOpen']} | {metric(r['modeledExecution'])} | {r['finalEquity']-r['initialEquity']:.6f} | {r['maxEquityDrawdown']*100:.6f} | {h.get('positive',0)} / {h.get('negative',0)} / {h.get('flat',0)} | {r['performance']['wallSeconds']:.3f} |")
    lines += ["", "Initial modeled equity is 1,000 quote units. Marks include estimated close fees/slippage on open positions; no forced end-of-test close hides the remaining exposure. No funding, liquidation, intraminute liquidity or venue netting is modeled. Low drawdown here reflects small modeled exposure and this short window, not a reliable future bound.","",
      f"Net-positive ending equity: {sum(r['finalEquity']>r['initialEquity'] for r in results.values())}/{len(results)} scenarios. Positive every hour: {sum(r['hourly']['everyHourNetPositive'] for r in results.values())}/{len(results)}. Empty/flat hours are not wins. Passing historical windows are not a portfolio profit guarantee.","",
      f"## {args.focus}: final stage evidence", "", "Each exact source/range/TF/symbol/direction/protection/Last-N branch owns separate Base, Main and Real positions. Main and Real re-simulate their admitted entries; they are not filtered copies of Base outcomes. Historical current-window qualification is not future execution profitability.","",
      "| Stage | Own passing / population | % passing | Passing through this stage | Insufficient own Last-N | Passing-window PF | All modeled closes | All-close PF |",
      "|---|---:|---:|---:|---:|---:|---:|---:|"]
    expanded=results[args.focus]
    for name,row in expanded["stageProgress"][-1]["stages"].items():
        allrows=[c["stages"][name]["cumulativeReturns"] for c in expanded["contexts"]]
        window_pf=f"{row['pooledWindowPf']:.6f}" if row['pooledWindowPf'] is not None else row['pooledWindowState']
        lines.append(f"| {name} | {row['passed']} / {row['evaluated']} | {row['pct']:.6f} | {row['chainPassed']} | {row['insufficient']} | {window_pf} | {sum(x['n'] for x in allrows)} | {pf(sum(x['gain'] for x in allrows),sum(x['loss'] for x in allrows))} |")
    cold=sum(not c["sourceWarmAtTradeStart"] for c in expanded["sourceCoverage"])
    endcold=sum(not c["sourceWarmAtEnd"] for c in expanded["sourceCoverage"])
    lines += ["", "Stage totals pool hypothetical overlapping branches and shared price events for diagnosis only; they are not a portfolio PF or independent statistical observations.","",
      f"Source coverage: {cold}/{len(expanded['sourceCoverage'])} source/symbol/range/TF combinations lack minimum indicator warmup at trading start; {endcold} still lack it at the end. Warm indicators can still lack enough own stage closes. The engine reports this insufficiency instead of fabricating prehistory, pooling another lane, or lowering the PF floor.","",
      f"Detailed grid lines are in `{args.grid.name}`; the compressed scenario files retain every exact symbol/direction context, hourly stage progress, every modeled execution and all 721 close equity marks plus opening/admission marks.","",
      "## Every modeled trading hour (UTC)","",
      "Equity changes include open-position marks. Settled cash alone is not hourly equity profit. Exact boundary convention: candle closes settle in `(from, to]`.","",
      "| Scenario | From | To | Equity change | State | Settled orders | Settled cash |", "|---|---|---|---:|---|---:|---:|"]
    for name,r in results.items():
        for h in r["hourly"]["hours"]:
            lines.append(f"| {name} | {stamp(h['from'])} | {stamp(h['to'])} | {h['netEquity']:.6f} | {h['state']} | {h['settledOrders']} | {h['settledNetCash']:.6f} |")
    lines += ["", "## Limits and release gate", "",
      "Nine families are implemented: EMA cross/trend, MACD cross, RSI reversal/momentum, Donchian, Bollinger bounce, CCI reversal and OBV trend. This is not the full CTS-A-O catalog, MFE fitting, timeframe-distance scaling, DCA/-Active Signals or the ordinary runtime's coordinator adapter. Model orders/positions are 1:1, not exchange stacking or partial execution. Separate trader fixtures cover existing production paths; venue acceptance remains untested.","",
      "Independent reconstruction covers normal, trailing and trailing-only paths; consult the separate audit for each scenario's exact comparison count. Do not merge into a live release until frontend build/render checks, complete Signal integration, forward/out-of-sample evidence and x02 order/fill/protection reconciliation pass. Positive PF and hourly profits cannot be enforced by changing reported results."]
    args.summary.write_text("\n".join(lines)+"\n")
    groups=defaultdict(list)
    for c in expanded["contexts"]:
        key=(c["source"],c["range"],c["tf"],c["protection"]["id"],tuple(c["lastN"][s] for s in ("base","main","real")))
        groups[key].append(c)
    grid=["# Expanded Signals: every configuration/stage line", "",
      "Each row aggregates 8 symbols × 2 directions for one exact source/range/TF/protection/Last-N branch. Gates are evaluated per exact context before aggregation. These overlapping simulated streams must not be treated as independent portfolio trades. Last-N is Base/Main/Real. All-close PF includes losers; passing-window PF is selection evidence only.","",
      "| Source | Range | TF min | Protection | Last-N | Stage | Passing / contexts | Passing % | Window samples (all) | All closes | All-close PF | Passing-window PF |", "|---|---|---:|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for key,rows in sorted(groups.items()):
        source,rng,tf,protect,lane=key
        for stage in ("base","main","real"):
            vals=[c["stages"][stage] for c in rows];passed=[v for v in vals if v["qualified"]]
            totals=[v["cumulativeReturns"] for v in vals]
            grid.append(f"| {source} | {rng} | {tf} | {protect} | {'/'.join(map(str,lane))} | {stage} | {len(passed)} / {len(vals)} | {100*len(passed)/len(vals):.2f} | {sum(v['n'] for v in vals)} | {sum(v['n'] for v in totals)} | {pf(sum(v['gain'] for v in totals),sum(v['loss'] for v in totals))} | {pf(sum(v['gain'] for v in passed),sum(v['loss'] for v in passed))} |")
    args.grid.write_text("\n".join(grid)+"\n")
    print(json.dumps({"scenarios":len(results),"hourRows":sum(len(r["hourly"]["hours"]) for r in results.values()),"configurationStageLines":len(groups)*3}))


if __name__=="__main__":main()
