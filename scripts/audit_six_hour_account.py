#!/usr/bin/env python3
"""Independent cash, fee, funding, return-PF and order audit of a report."""
from __future__ import annotations
import argparse
from datetime import datetime
import json
import math
from pathlib import Path


def audit_report(report, data_dir=None):
    account = report["account"]
    totals = account["totals"]
    start = float(report["startEquity"])
    cost = float(report["costPct"]) / 100
    reference = cost if cost > 0 else .001
    checks = []

    def close(name, actual, expected, tolerance=1e-7):
        try:
            actual, expected = float(actual), float(expected)
            valid = math.isfinite(actual) and math.isfinite(expected) and abs(actual-expected) <= tolerance
        except (TypeError, ValueError):
            valid = False
        checks.append(dict(check=name, passed=valid, actual=actual, expected=expected))

    def condition(name, valid):
        checks.append(dict(check=name, passed=bool(valid)))

    closed, opened, live = (account.get(key) or [] for key in ("closedPositions", "openedPositions", "openPositions"))
    close("Entry inventory count", len(opened), len(closed)+len(live), 0)
    close("Unique position IDs", len({row["id"] for row in closed+live}), len(closed)+len(live), 0)
    for row in closed:
        name = f"Lot {row['id']}"
        close(name+" gross cash", row["gross"], row["qty"]*(row["exit"]-row["entry"])*row["side"])
        close(name+" costs once", row["net"], row["gross"]-row["fee_in"]-row["fee_out"]+row.get("fundingUsdt", 0))
        close(name+" configured roundtrip fee", row["fee_in"]+row["fee_out"], row["notional"]*cost)
    if data_dir:
        cached = {}
        for row in closed+live:
            symbol = row["symbol"]
            if symbol not in cached:
                cached[symbol] = json.loads((Path(data_dir)/"funding"/(symbol+".json")).read_text())
            entered = datetime.fromisoformat(row["entryUtc"].replace("Z", "+00:00")).timestamp()*1000
            terminal = datetime.fromisoformat(row.get("exitUtc", report["execution"]["endUtc"]).replace("Z", "+00:00")).timestamp()*1000
            # Report fills occur at completed-candle close. Funding at that
            # same instant precedes new fills; a close at that instant follows
            # the previous candle and no longer owns the next funding tick.
            payment = sum(-row["side"]*row["qty"]*float(tick["markPrice"])*float(tick["fundingRate"])
                          for tick in cached[symbol] if entered < float(tick["fundingTime"]) < terminal)
            close(f"Lot {row['id']} funding during actual ownership", row.get("fundingUsdt", 0), payment)
    close("Shared balance", totals["balanceEnd"], start+sum(row["net"] for row in closed)-sum(row["fee_in"] for row in live)+sum(row.get("fundingUsdt", 0) for row in live))
    close("Shared equity", totals["equityEnd"], totals["balanceEnd"]+sum(row.get("unrealizedUsdt", 0) for row in live))
    close("Fee conservation", totals["fees"], sum(row["fee_in"] for row in opened)+sum(row["fee_out"] for row in closed))
    close("Funding conservation", totals.get("fundingUsdt", 0), sum(row.get("fundingUsdt", 0) for row in closed+live))
    close("Closed sample count", totals["closed"]["n"], len(closed), 0)
    if closed:
        close("Closed return PF", totals["closed"]["pf"], 1+.1*sum(row["net"]/row["notional"]/reference for row in closed)/len(closed), 1e-6)
    else:
        condition("Empty closed return PF unavailable", totals["closed"]["pf"] is None)
    for hour in account.get("hourly") or []:
        name = f"Hour {hour['hour']}"
        strategies = hour.get("byStrategy") or {}
        close(name+" strategy cumulative equity sum", sum(row["contributionUsdt"] for row in strategies.values()), hour["equityEnd"]-start)
        close(name+" strategy hourly equity sum", sum(row["hourContributionUsdt"] for row in strategies.values()), hour["pnl"])
        for strategy, stats in strategies.items():
            close(name+f" {strategy} contribution components", stats["contributionUsdt"],
                  stats["closedNetUsdtToDate"]+stats["openMarkUsdt"]-stats["openEntryFeesUsdt"]+stats["openFundingUsdt"])
            if not stats["n"]:
                condition(name+f" {strategy} empty PF unavailable", stats["pf"] is None)
    hours = account.get("hourly") or []
    for field, total_field in (("feesUsdt", "fees"), ("fundingUsdt", "fundingUsdt")):
        if hours and all(field in hour for hour in hours):
            close("Hourly "+field+" conservation", sum(hour[field] for hour in hours), totals[total_field])
    for minute in account.get("orderRateMinutes") or []:
        condition(f"Bar {minute['bar']} modeled request capacity", minute["ordinaryRequests"] <= minute["ordinaryLimit"] and minute["batchRequests"] <= minute["batchLimit"])
    for key in ("ordinaryRequests", "batchRequests"):
        if hours and all(key in (hour.get("orders") or {}) for hour in hours):
            close("Hourly "+key+" totals", sum(hour["orders"][key] for hour in hours), sum(row[key] for row in account["orderRateMinutes"]), 0)
    for hour in report.get("stageHourly") or []:
        for stage, stats in (hour.get("stages") or {}).items():
            if not stats["n"]:
                condition(f"Hypothetical hour {hour['hour']} {stage} empty PF unavailable", stats["pf"] is None)
            else:
                close(f"Hypothetical hour {hour['hour']} {stage} return PF", stats["pf"], 1+.1*stats["netAvgPct"]/100/reference, 1e-6)
    return dict(generatedAt=report.get("generatedAt"), accountModelSha256=report.get("accountModelSha256"),
                profile=report.get("profile"), passed=all(row["passed"] for row in checks), checks=checks,
                summary=dict(symbols=len(report["symbols"]), closed=len(closed), returnPf=totals["closed"]["pf"],
                             netAccountPnl=totals["pnl"], cashPnl=totals["balanceEnd"]-start,
                             fees=totals["fees"], funding=totals.get("fundingUsdt"), maxDdPct=totals["maxDdPct"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    result = audit_report(json.loads(args.report.read_text()), args.data_dir)
    result["source"] = str(args.report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps(dict(passed=result["passed"], checkCount=len(result["checks"]), summary=result["summary"],
                          failures=[row for row in result["checks"] if not row["passed"]])))
    raise SystemExit(0 if result["passed"] else 1)
