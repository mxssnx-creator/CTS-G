"""Independent cash/mark reconstruction and normal-stage scalar differential.

No signal_replay imports. Source onset inputs are shared with the source port,
which has a separate pinned CTS-A-O registry parity test. All three protection
types are independently stage-replayed; venue execution remains separate.
"""
import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"server/pulse"))
from signal_sources import onsets
from signal_scalar_oracle import scalar_stages


def load(path):
    raw=path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix==".gz" else raw)


def check(result, feed, stages=False):
    cfg=result["config"];fee=cfg["fees"]
    start,end=result["window"]["start"],result["window"]["end"]
    bars={s:[[r["t"],r["o"],r["h"],r["l"],r["c"],r["v"]] for r in feed["candles"][s+"|1"] if start<=r["t"]<end]
          for s in result["provenance"]["symbols"]}
    prices={s:{r[0]+60000:r[4] for r in rows} for s,rows in bars.items()}
    differences=[];mark_error=fee_error=admission_error=0.0
    trades=result["trades"];all_orders=trades+result["openPositions"]
    for r in trades:
        gross=(r["exit"]-r["entry"])*r["qty"]*r["side"]
        expected=gross-r["entry"]*r["qty"]*fee[fee["entryLiquidity"]]-r["exit"]*r["qty"]*fee[fee["exitLiquidity"]]
        fee_error=max(fee_error,abs(expected-r["netCash"]))
    for mark in result["marks"]:
        t=mark["t"];equity=result["initialEquity"]
        for order in all_orders:
            if order["entryT"]>=t:continue
            equity-=order["entryFee"]
            if "exitT" in order and order["exitT"]<=t:
                equity+=order["qty"]*(order["exit"]-order["entry"])*order["side"]-order["exitFee"]
            else:
                fill=prices[order["symbol"]][t]*(1-order["side"]*fee["slippagePerLeg"])
                equity+=order["qty"]*(fill-order["entry"])*order["side"]-order["qty"]*fill*fee[fee["exitLiquidity"]]
        mark_error=max(mark_error,abs(equity-mark["equity"]))
    opens={s:{r[0]:r[1] for r in rows} for s,rows in bars.items()}
    for mark in result["admissionMarks"]:
        t=mark["t"];equity=result["initialEquity"]
        for order in all_orders:
            if order["entryT"]>t or order["entrySequence"]>mark["openedCount"]:continue
            equity-=order["entryFee"]
            if "exitT" in order and order["exitT"]<=t:
                equity+=order["qty"]*(order["exit"]-order["entry"])*order["side"]-order["exitFee"]
            else:
                fill=opens[order["symbol"]][t]*(1-order["side"]*fee["slippagePerLeg"])
                equity+=order["qty"]*(fill-order["entry"])*order["side"]-order["qty"]*fill*fee[fee["exitLiquidity"]]
        admission_error=max(admission_error,abs(equity-mark["equity"]))
    # A close mark at t precedes all decision/open/fee marks at t. Stable sort
    # retains the actual same-clock admission order, including each entry fee.
    timeline=sorted([(m["t"],0,m["equity"]) for m in result["marks"]]+[(m["t"],1,m["equity"]) for m in result["admissionMarks"]],key=lambda x:x[:2])
    peak=result["initialEquity"];drawdown=0
    for _,_,equity in timeline:
        peak=max(peak,equity);drawdown=max(drawdown,(peak-equity)/peak)
    if admission_error>1e-8 or not math.isclose(drawdown,result["maxEquityDrawdown"],abs_tol=1e-10):differences.append({"admissionMarkError":admission_error,"drawdown":drawdown,"reported":result["maxEquityDrawdown"]})
    if fee_error>1e-9 or mark_error>1e-8:differences.append({"cashError":fee_error,"markError":mark_error})
    counts=result["modelCounts"]
    if counts["ordersOpened"]!=len(all_orders) or counts["ordersSettled"]!=len(trades):differences.append({"counts":counts})
    marks={r["t"]:r["equity"] for r in result["marks"]}
    for hour in result["hourly"]["hours"]:
        cash=sum(r["netCash"] for r in trades if hour["from"]<r["exitT"]<=hour["to"])
        if not math.isclose(cash,hour["settledNetCash"],abs_tol=1e-9) or not math.isclose(marks[hour["to"]]-marks[hour["from"]],hour["netEquity"],abs_tol=1e-9):
            differences.append({"hour":hour["from"]})
    compared=0;cached={};historical={s:[] for s in ("base","main","real")}
    if stages:
        for c in result["contexts"]:
            key=(c["symbol"],c["source"],c["range"],c["tf"])
            if key not in cached:cached[key]=onsets(bars[c["symbol"]],c["source"],c["range"],c["tf"])
            closed,opened=scalar_stages(bars[c["symbol"]],cached[key],c["protection"],c["lastN"],fee,cfg["historyMinutes"],cfg["minPf"],cfg["maxContextDrawdown"],c["side"])
            for stage,want in c["stages"].items():
                compared+=1
                boundary=result["window"]["tradeStart"]
                past=[v for t,v in closed[stage] if boundary-cfg["historyMinutes"]*60000<=t<boundary][-c["lastN"][stage]:]
                gain0=sum(v for v in past if v>0);loss0=-sum(v for v in past if v<0)
                balance=peak0=dd0=0
                for v in past:
                    balance+=v;peak0=max(peak0,balance);dd0=max(dd0,peak0-balance)
                if len(past)==c["lastN"][stage] and gain0>0 and (not loss0 or gain0/loss0>=cfg["minPf"]) and dd0<=cfg["maxContextDrawdown"]:
                    historical[stage].append(c["id"])
                vals=[v for t,v in closed[stage] if end-cfg["historyMinutes"]*60000<=t<end][-c["lastN"][stage]:]
                gain=sum(v for v in vals if v>0);loss=-sum(v for v in vals if v<0)
                if (len(vals)!=want["n"] or len(closed[stage])!=want["closes"] or (opened[stage] is not None)!=want["open"] or
                    not math.isclose(gain,want["gain"],abs_tol=1e-10) or not math.isclose(loss,want["loss"],abs_tol=1e-10) or
                    not math.isclose(sum(v for _,v in closed[stage]),want["cumulativeReturns"]["net"],abs_tol=1e-10)):
                    differences.append({"context":c["id"],"stage":stage})
    selected=result.get("prehistorySelection")
    if selected:
        if stages:
            chain=set.intersection(*(set(historical[s]) for s in historical))
            if any(set(historical[s])!=set(selected["stageQualifiedIds"][s]) for s in historical) or chain!=set(selected["chainQualifiedIds"]):
                differences.append({"prehistorySelection":"does not match independent strictly earlier stage reconstruction"})
        if selected["frozenAdmissions"] and any(r["context"] not in selected["chainQualifiedIds"] for r in all_orders):
            differences.append({"frozenSelection":"non-selected order admitted"})
    return {"modeledOrders":len(all_orders),"settled":len(trades),"marks":len(marks),"allTypeStageContextsCompared":compared,
            "maxCashError":fee_error,"maxMarkError":mark_error,"maxAdmissionMarkError":admission_error,
            "drawdown":drawdown,"differenceCount":len(differences),"differences":differences[:50]}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--feed",type=Path,required=True)
    ap.add_argument("--scenario-dir",type=Path,required=True)
    ap.add_argument("--before",type=Path)
    ap.add_argument("--optimization-after",type=Path,help="frozen post-optimization, pre-drawdown-fix comparator")
    ap.add_argument("--stage-cases",default="expanded",help="comma-separated cases for full independent stage reconstruction")
    ap.add_argument("--out",type=Path,required=True)
    args=ap.parse_args()
    if args.out.exists():ap.error("preserve earlier audit")
    begun=time.perf_counter();feed=load(args.feed);audits={}
    for path in sorted(args.scenario_dir.glob("*.json.gz")):
        name=path.name.removesuffix(".json.gz")
        result=load(args.scenario_dir/(name+".json.gz"))
        audits[name]=check(result,feed,stages=name in args.stage_cases.split(","))
    if not audits:ap.error("no scenarios found")
    if bool(args.before)!=bool(args.optimization_after):ap.error("optimization comparison needs both frozen files")
    def semantic(x):
        return hashlib.sha256(json.dumps({k:v for k,v in x.items() if k not in ("performance","provenance")},sort_keys=True,separators=(",",":")).encode()).hexdigest()
    comparison=None
    if args.before:
        before=load(args.before);after=load(args.optimization_after)
        comparison={"beforeSha256":semantic(before),"afterSha256":semantic(after),"equal":semantic(before)==semantic(after),
                    "beforeSeconds":before["performance"]["wallSeconds"],"afterSeconds":after["performance"]["wallSeconds"]}
    out={"schema":"cts-g-signals-independent-audit-v1","scenarios":audits,"activeStageOptimization":comparison,
         "seconds":time.perf_counter()-begun,"limits":["shared source-onset inputs, separately registry-parity-tested",
         "all three protection types independently replayed across every expanded stage context; no DCA/Active/venue oracle",
         "cash/marks reconstruct modeled execution, not venue executions or untouched out-of-sample performance"]}
    args.out.write_text(json.dumps(out,indent=2)+"\n")
    print(json.dumps(out,indent=2))
    if (comparison and not comparison["equal"]) or any(v["differenceCount"] for v in audits.values()):sys.exit(1)


if __name__=="__main__":main()
