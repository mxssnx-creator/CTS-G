#!/usr/bin/env python3
"""Self-contained, source-qualified HTML for the six-hour market audit."""
from __future__ import annotations

import argparse
import base64
from collections import defaultdict
import csv
from datetime import datetime
from html import escape
import json
import io
import math
from pathlib import Path
from zoneinfo import ZoneInfo

STAGES = (("preBase", "Pre-Base"), ("Base", "Base"), ("Main", "Main"),
          ("Real", "Real"), ("admitted", "Qualified after safeguards"))
STYLE = """:root{font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#15223c;background:#f2f5fa;font-size:15px;line-height:1.55}
*{box-sizing:border-box}body{margin:0}main{max-width:1440px;margin:auto;padding:30px 24px 56px}h1{font-size:32px;line-height:1.2;margin:10px 0 18px}h2{font-size:21px;margin:30px 0 12px}h3{font-size:16px}p{margin:10px 0}.eyebrow{font-size:12px;letter-spacing:.13em;text-transform:uppercase;color:#2658ba;font-weight:700}.muted{color:#69758c}.note{padding:14px 18px;border-left:4px solid #2464e6;background:#e8effc;border-radius:0 8px 8px 0}.warn{background:#fff4dc;border-color:#ad6c10}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin:22px 0}.card{background:white;padding:18px;border:1px solid #dce3ee;border-radius:12px}.card strong{display:block;font-size:26px;line-height:1.3;font-variant-numeric:tabular-nums}.label{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#667389}.scroll{overflow:auto;border:1px solid #dce3ee;border-radius:10px;background:white}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{text-align:right;padding:10px 12px;border-bottom:1px solid #e7ecf4;white-space:nowrap}th{background:#f7f9fd;font-size:12px;color:#485978}th:first-child,td:first-child{text-align:left}tr:last-child td{border:0}.positive{color:#087f5b}.negative{color:#c13b45}figure{background:white;border:1px solid #dce3ee;border-radius:10px;margin:20px 0;padding:18px}figcaption{font-size:12px;color:#67758f}svg{width:100%;height:auto;display:block}details{background:white;padding:14px 18px;margin:16px 0;border:1px solid #dce3ee;border-radius:10px}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;color:#526078}.tag{display:inline-block;background:#e8effc;color:#285aaf;border-radius:100px;padding:4px 10px;font-size:12px;font-weight:600}.comparison{display:grid;grid-template-columns:1fr 1fr;gap:18px}.foot{margin-top:30px;font-size:12px;color:#69758c}@media(max-width:700px){main{padding:20px 14px 40px}h1{font-size:26px}.comparison{grid-template-columns:1fr}th,td{padding:8px 10px}.cards{grid-template-columns:1fr 1fr}.card{padding:14px}.card strong{font-size:22px}}@media print{body{background:white}main{padding:0}.scroll{overflow:visible}th,td{font-size:10px;padding:5px}.cards,.card,figure,.scroll{break-inside:avoid}details{display:block}}
"""


def number(value, digits=2):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{value:,.{digits}f}" if math.isfinite(value) else "—"


def text(value):
    return escape(str(value))


def table(headers, rows):
    return "<div class='scroll'><table><thead><tr>" + "".join("<th>"+text(h)+"</th>" for h in headers) + "</tr></thead><tbody>" + "".join("<tr>"+"".join("<td>"+text(cell)+"</td>" for cell in row)+"</tr>" for row in rows) + "</tbody></table></div>"


def download(value, filename, label, *, csv_rows=False):
    if csv_rows:
        output = io.StringIO(newline="")
        keys = list(dict.fromkeys(key for row in value for key in row))
        writer = csv.DictWriter(output, fieldnames=keys)
        writer.writeheader()
        for row in value:
            writer.writerow({key: json.dumps(cell, ensure_ascii=False) if isinstance(cell, (dict, list)) else cell
                             for key, cell in row.items()})
        payload, mime = output.getvalue(), "text/csv"
    else:
        payload, mime = json.dumps(value, ensure_ascii=False, indent=2), "application/json"
    encoded = base64.b64encode(payload.encode()).decode()
    return f"<a class='tag' download='{text(filename)}' href='data:{mime};base64,{encoded}'>{text(label)}</a>"


def first_value(row, *keys):
    return next((row[key] for key in keys if row.get(key) is not None), None)


def pf(stats):
    return number(stats.get("pf"), 4) if int(stats.get("n") or 0) > 0 else "—"


def interval(period):
    return str(period.get("startUtc") or "unavailable") + " → " + str(period.get("endUtc") or "unavailable")


def local_interval(period):
    try:
        return " → ".join(datetime.fromisoformat(period[key].replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Vienna")).strftime("%d %b %H:%M") for key in ("startUtc", "endUtc")) + " · Vienna"
    except (KeyError, ValueError, TypeError):
        return ""


def account_plot(account):
    curve = account.get("equityCurve") or []
    valid = [p for p in curve if isinstance(p.get("equity"), (int, float)) and math.isfinite(p["equity"])
             and isinstance(p.get("balance"), (int, float)) and math.isfinite(p["balance"])]
    if not valid:
        return ""
    low = min(min(p["equity"], p["balance"]) for p in valid)
    high = max(max(p["equity"], p["balance"]) for p in valid)
    span = high-low
    padding = span*.08 if span else max(1., abs(low)*.001)
    low, high = low-padding, high+padding
    left, top, width, height = 75, 20, 945, 220
    def y(v): return top+height-(v-low)/(high-low)*height
    lines = []
    for i in range(5):
        v = low+(high-low)*i/4
        lines.append(f"<line x1='{left}' y1='{y(v):.2f}' x2='{left+width}' y2='{y(v):.2f}' stroke='#e6ebf3'/><text x='{left-10}' y='{y(v)+4:.2f}' text-anchor='end' fill='#6b7890' font-size='12'>{number(v)}</text>")
    for key, color in (("balance", "#8b97aa"), ("equity", "#2362dd")):
        points = " ".join(f"{left+i*width/max(1,len(valid)-1):.2f},{y(p[key]):.2f}" for i,p in enumerate(valid))
        lines.append(f"<polyline points='{points}' fill='none' stroke='{color}' stroke-width='2.4'><title>{key.title()} from the supplied minute curve</title></polyline>")
    lines += [f"<text x='{left}' y='265' fill='#6b7890' font-size='12'>{text(valid[0].get('t',''))}</text>",
              f"<text x='{left+width}' y='265' text-anchor='end' fill='#6b7890' font-size='12'>{text(valid[-1].get('t',''))}</text>"]
    return "<figure><svg viewBox='0 0 1045 280' role='img' aria-label='Simulated balance and equity over the six-hour period'>"+"".join(lines)+"</svg><figcaption>Blue: equity including open mark PnL. Gray: balance. Values come from the supplied simulated account curve.</figcaption></figure>"


def coverage_section(report):
    coverage = report.get("coverage") or {}
    kinds = list(coverage.get("kinds") or [])
    groups = defaultdict(list)
    for lane in coverage.get("lanes") or []:
        key = str(lane.get("lane") or "").split("|")[0]
        if key in kinds:
            groups[key].append(lane)
    rows = []
    for kind in kinds:
        lanes = groups.get(kind, [])
        requested = sum(int(r.get("requestedConfigs") or 0) for r in lanes)
        evaluated = sum(int(r.get("evaluatedConfigs") or 0) for r in lanes)
        signals = sum(int(r.get("signalBars") or 0) for r in lanes)
        status = "Signals observed" if signals else "No signals" if lanes else "No lane evidence supplied"
        rows.append([kind.title(), len({r.get("symbol") for r in lanes}), len(lanes),
                     number(requested, 0), number(evaluated, 0), number(signals, 0), status])
    note = "Descendant calculations cover the eligible Base-qualified parents. Empty parent eligibility produces no descendant calculation or execution evidence. Coverage does not establish actual child orders." if coverage.get("adjustmentsComplete") else "Descendant strategy coverage is not marked complete in this report. Normal/Trailing lane counts below do not establish Block, DCA or axis completion."
    content = "<h2>Indication coverage</h2><p class='muted'>Declared types: "+str(len(kinds))+". Counts are supplied lane/configuration identities, separate from completed execution samples.</p>"+table(["Type", "Symbols", "Lanes", "Requested configs", "Evaluated configs", "Signal bars", "Evidence"], rows)+"<p class='note'>"+text(note)+"</p>"
    descendants = coverage.get("descendantCoverage")
    if descendants:
        records = descendants if isinstance(descendants, list) else [dict(strategy=key, **value) if isinstance(value, dict) else dict(strategy=key, status=value) for key, value in descendants.items()]
        fields = list(dict.fromkeys(key for row in records for key in row))
        content += "<h3>Descendant calculation coverage</h3>"+table(fields, [[json.dumps(row[key]) if isinstance(row.get(key), (list, dict)) else row.get(key, "—") for key in fields] for row in records])
    return content


def strategy_stages(report):
    rows = []
    for symbol in report.get("byStrategyStage") or []:
        for name, stages in (symbol.get("strategies") or {}).items():
            for key, title in STAGES:
                stats = stages.get(key)
                if stats is not None:
                    rows.append([symbol.get("symbol"), name, title, int(stats.get("n") or 0), pf(stats), number(stats.get("netAvgPct"), 4)])
    if not rows:
        return ""
    return "<details><summary>Independent strategy stages by symbol</summary>"+table(["Symbol", "Strategy", "Stage", "Closes", "Return PF", "Mean net %"], rows)+"</details>"


def supplemental_calculations(report):
    content = ""
    preparation = report.get("preparationHourly") or []
    if preparation:
        rows, endpoints = [], []
        for hour in preparation:
            for key, title in STAGES:
                stats = (hour.get("stages") or {}).get(key) or {}
                rows.append([hour.get("hour"),str(hour.get("startUtc") or "")[11:16]+"–"+str(hour.get("endUtc") or "")[11:16],
                             title,int(stats.get("n") or 0),pf(stats),number(stats.get("netAvgPct"),4),
                             number(stats.get("entryPositions"),0)])
            end = hour.get("endpoint") or {}
            qualifications = end.get("qualifiedDirectionConfigs") or {}
            endpoints.append([hour.get("hour"),end.get("t"),number(end.get("observedDirectionConfigs"),0),
                              number(end.get("insufficientBaseSamples"),0),
                              *[number(qualifications.get(key),0) for key in ("Base","Main","Real","admitted")],
                              number(end.get("ddBlockedDirectionConfigs"),0),number(end.get("maxOwnObservedDdtS"),0)])
        content += "<h2>Six-hour preparation calculations</h2><p class='muted'>Preparation starts flat and uses only its six-hour trades for evidence. Earlier candles warm indicators. These calculations do not add wallet PnL or simulated execution orders.</p>"+table(["Hour","UTC","Stage","Hypothetical closes","Return PF","Mean net %","Entry cohort"],rows)
        content += "<h3>Own qualification progress and drawdown time</h3><p class='muted'>Direction/configuration counts remain independently scoped. Drawdown time is the maximum observed elapsed time on one own tape, in seconds.</p>"+table(["Hour","Endpoint UTC","Observed directions","Insufficient Base","Base","Main","Real","Qualified after DDT","DDT blocked","Maximum own DDT s"],endpoints)
    for field, group_key, heading in (("byIndicationHourly","types","Hourly calculations for every indication"),
                                      ("byStrategyHourly","strategies","Hourly independent strategy calculations")):
        hours = report.get(field) or []
        if not hours:
            continue
        groups = list(dict.fromkeys(name for hour in hours for name in (hour.get(group_key) or {})))
        content += "<h2>"+heading+"</h2><p class='muted'>Completed hypothetical execution-period entry cohorts. Aggregate return PF describes their returns; qualification continues to use each own symbol/configuration/direction tape. Empty cohorts retain unavailable PF.</p>"
        for name in groups:
            rows = []
            for hour in hours:
                stages = ((hour.get(group_key) or {}).get(name) or {}).get("stages") or {}
                for key,title in STAGES:
                    stats = stages.get(key) or {}
                    rows.append([hour.get("hour"),title,int(stats.get("n") or 0),pf(stats),number(stats.get("netAvgPct"),4),number(stats.get("entryPositions"),0)])
            content += "<details><summary>"+text(name)+" · six hourly stage tables</summary>"+table(["Hour","Stage","Hypothetical closes","Return PF","Mean net %","Entry cohort"],rows)+"</details>"
    snapshots = report.get("supplementalStageSnapshots") or []
    if snapshots:
        rows = []
        for symbol in snapshots:
            for snapshot in symbol.get("snapshots") or []:
                qualifications = snapshot.get("qualifiedDirectionConfigs") or {}
                rows.append([symbol.get("symbol"),snapshot.get("phase"),snapshot.get("t"),
                             *[number(qualifications.get(key),0) for key in ("Base","Main","Real","admitted")],
                             number(snapshot.get("ddBlockedDirectionConfigs"),0),number(snapshot.get("maxOwnObservedDdtS"),0)])
        content += "<details><summary>Own stage and drawdown-time endpoints by symbol</summary>"+table(["Symbol","Phase","UTC","Base directions","Main directions","Real directions","Qualified after DDT","DDT blocked","Maximum own DDT s"],rows)+"</details>"
    if report.get("augmentation"):
        content += "<details><summary>Supplemental calculation provenance</summary><pre>"+text(json.dumps(report["augmentation"],indent=2))+"</pre></details>"
    return content


def qualification_snapshots(report):
    rows = []
    for symbol in report.get("stageSnapshots") or []:
        for snapshot in symbol.get("snapshots") or []:
            stages = snapshot.get("qualifiedDirectionConfigs") or {}
            sets = snapshot.get("qualifiedSets") or {}
            rows.append([symbol.get("symbol"), snapshot.get("t"),
                         number(snapshot.get("requestedDirectionConfigs"), 0),
                         number(snapshot.get("observedDirectionConfigs"), 0),
                         number(snapshot.get("unobservedDirectionConfigs"), 0),
                         *[number(stages.get(key), 0) for key in ("Base", "Main", "Real")],
                         *[number(sets.get(key), 0) for key in ("Base", "Main", "Real")]])
    if not rows:
        return ""
    return "<details><summary>Stage progress at each recorded hour</summary>"+table(["Symbol", "UTC", "Requested directions", "Observed directions", "Unobserved directions", "Base directions", "Main directions", "Real directions", "Base Sets", "Main Sets", "Real Sets"], rows)+"</details>"


def execution_details(report):
    account = report.get("account") or {}
    content = "<h2>Trades, orders and positions</h2><p>"+download(report, "six-hour-complete-report.json", "Download full source report")+"</p>"
    for key, label in (("closedPositions", "Closed trades"), ("openedPositions", "Entry positions"), ("openPositions", "Open positions")):
        if key not in account:
            continue
        records = account.get(key) or []
        rows = [[row.get("id", row.get("positionId", "—")), row.get("symbol"), row.get("strategy"), row.get("setId"),
                 row.get("entryUtc"), row.get("exitUtc", "Open"), number(row.get("qty"), 6),
                 number(row.get("entry"), 6), number(row.get("exit"), 6), number(row.get("net")),
                 number(first_value(row, "fee_in", "entryFee"), 4), number(first_value(row, "fee_out", "exitFee"), 4),
                 number(row.get("fundingUsdt"), 4)] for row in records[:250]]
        content += "<details><summary>"+text(label)+" · "+str(len(records))+"</summary><p>"+download(records, "six-hour-"+key+".csv", "Download complete CSV", csv_rows=True)+"</p>"
        content += table(["ID", "Symbol", "Strategy", "Own Set", "Entry UTC", "Exit UTC", "Quantity", "Entry price", "Exit price", "Net cash", "Entry fee", "Exit fee", "Funding"], rows)
        if len(records) > 250:
            content += "<p class='muted'>First 250 rows shown; the download contains every row.</p>"
        content += "</details>"
    order_rows = first_value(account, "orderEvents", "orderJournal", "orderDetails")
    if order_rows is not None:
        content += "<details><summary>Order journal · "+str(len(order_rows))+"</summary><p>"+download(order_rows, "six-hour-order-journal.csv", "Download complete order CSV", csv_rows=True)+"</p>"
        fields = list(dict.fromkeys(key for row in order_rows for key in row))
        content += table(fields, [[json.dumps(row[key]) if isinstance(row.get(key), (dict, list)) else row.get(key, "—") for key in fields] for row in order_rows[:250]])+"</details>"
    else:
        content += "<p class='muted'>Only order totals are supplied; individual physical order details are unavailable.</p>"
    minutes = account.get("orderRateMinutes")
    if minutes is not None:
        content += "<details><summary>Minute request capacity · "+str(len(minutes))+" snapshots</summary><p>"+download(minutes, "six-hour-minute-order-requests.csv", "Download minute request CSV", csv_rows=True)+"</p>"
        content += table(["Bar", "Ordinary requests", "Batch requests", "Ordinary limit", "Batch limit", "Pending prototypes"], [[row.get("bar"), row.get("ordinaryRequests"),row.get("batchRequests"),row.get("ordinaryLimit"),row.get("batchLimit"),row.get("pendingPrototypes")] for row in minutes])+"</details>"
    content += "<p>"+download(account.get("hourly") or [], "six-hour-hourly-account.csv", "Download hourly account CSV", csv_rows=True)+"</p>"
    return content


def htf_note(report):
    htf = report.get("htf") or (report.get("coverage") or {}).get("htf")
    if not htf:
        return "<h2>Hourly lane</h2><p class='note'>HTF evidence is unavailable in the supplied report. Hourly-lane admission and execution are not established by the 1m configuration tables.</p>"
    return "<h2>Hourly lane</h2><p class='note'>"+text(htf.get("status") or htf.get("reason") or "See recorded hourly evidence")+"</p><pre>"+text(json.dumps(htf, indent=2))+"</pre>"


def section(report, label="Baseline"):
    account = report.get("account") or {}
    totals = account.get("totals") or {}
    closed = totals.get("closed") or {}
    engine = report.get("engine") or {}
    hours = account.get("hourly") or []
    cost = report.get("costPct")
    order_totals = totals.get("orders") or {}
    evidence = "No closed execution evidence" if not closed.get("n") else "Sparse closed execution evidence" if int(closed.get("n") or 0) < 30 else "Closed execution sample available"
    experimental = "short-history" in str(report.get("profile") or "") or (engine.get("requiredSamples") is not None and int(engine["requiredSamples"]) <= 5)
    content = "<span class='tag'>"+text(label)+"</span><h1>CTS-G · six-hour market audit</h1>"
    content += "<p><strong>Execution:</strong> "+text(interval(report.get("execution") or {}))+" · UTC<br><span class='muted'>"+text(local_interval(report.get("execution") or {}))+"</span></p>"
    content += "<p><strong>Preparation:</strong> "+text(interval(report.get("preparation") or {}))+" · UTC</p>"
    content += "<p class='note'>Public market candles; simulated orders, fees and account balance. Return PF is 1 + 0.10 × mean cost/funding-net return ÷ the declared PositionCost reference. Empty windows show —.</p>"
    if experimental:
        content += "<p class='note warn'>Experimental five-position admission uses a small per-configuration sample. This scenario is exploratory and does not replace the baseline or establish a reliable future edge.</p>"
    content += "<div class='cards'>"+"".join("<div class='card'><span class='label'>"+text(name)+"</span><strong>"+text(value)+"</strong><span class='muted'>"+text(note)+"</span></div>" for name,value,note in (
        ("Ending equity", number(totals.get("equityEnd")), "Start "+number(report.get("startEquity"))),
        ("Net account PnL", number(totals.get("pnl"), 6), "Closed + open mark results"),
        ("Closed return PF", pf(closed), str(int(closed.get("n") or 0))+" closed samples"),
        ("Maximum drawdown", number(totals.get("maxDdPct"), 6)+"%", "Intrabar "+number(totals.get("maxIntrabarDdPct"), 6)+"%")))+"</div>"
    content += "<p class='muted'>"+text(evidence)+". Cost "+number(cost, 4)+"% · "+text(report.get("costSource") or "cost source unavailable")+". Base/Main/Real windows: "+" / ".join(number(engine.get(k),0) for k in ("baseWindow","mainWindow","realWindow"))+" · PF floor "+number(engine.get("minPf"),2)+".</p>"
    content += account_plot(account)
    rows = []
    for hour in hours:
        stats = hour.get("closed") or {}
        orders = hour.get("orders") or {}
        entries, exits = int(hour.get("positionsOpened") or 0), int(hour.get("positionsClosed") or 0)
        rows.append([hour.get("hour"), str(hour.get("startUtc") or "")[11:16]+"–"+str(hour.get("endUtc") or "")[11:16],
                     number(hour.get("balanceEnd"), 6), number(hour.get("equityEnd"), 6), number(hour.get("pnl"), 6), pf(stats),
                     int(stats.get("n") or 0), number(hour.get("ddMaxPct"), 6), number(hour.get("intrabarDdMaxPct"), 6),
                     entries, exits, int(hour.get("openPositions") or 0),
                     number(orders.get("ordinaryRequests"), 0), number(orders.get("batchRequests"), 0)])
    content += "<h2>Hourly shared account</h2>"+table(["Hour","UTC","Balance","Equity","Equity PnL","Closed return PF","Closed n","DD %","Intrabar DD %","Entry lots","Closed lots","Open end","Ordinary requests","Batch requests"],rows)
    if any("feesUsdt" in hour or "fundingUsdt" in hour for hour in hours):
        content += "<h3>Hourly account costs and funding</h3>"+table(["Hour","Fees charged","Funding paid/received","Gross realized cash","Close requests","Control requests"],
            [[hour.get("hour"),number(hour.get("feesUsdt"),6),number(hour.get("fundingUsdt"),6),number(hour.get("grossRealizedUsdt"),6),
              number((hour.get("orders") or {}).get("closeRequests"),0),number((hour.get("orders") or {}).get("controlRequests"),0)] for hour in hours])
    content += "<p class='muted'>Entry/closed lots describe internal execution positions. Total configured fees "+number(totals.get("fees"), 6)+"; funding "+number(totals.get("fundingUsdt", totals.get("funding")), 6)+"; max drawdown time "+number(totals.get("maxDdtMinutes"),0)+" min. Cancel-replace counts affected control legs; the action total counts their cancellations and replacement placements. Protective-close fills and bundle-close legs, when present, are subsets of the close count; they do not add REST requests.</p>"
    content += table(["Physical order action", "Count"], [[name,number(value,0)] for name,value in order_totals.items()])
    if totals.get("apiRequests"):
        content += "<h3>Modeled API requests</h3>"+table(["Request type", "Count"], [[name,number(value,0)] for name,value in totals["apiRequests"].items()])
    strategy_rows = []
    for hour in hours:
        for name, stats in (hour.get("byStrategy") or {}).items():
            strategy_rows.append([hour.get("hour"), str(name).title(), int(stats.get("n") or 0), pf(stats),
                                  number(stats.get("netUsdt"), 6), number(stats.get("hourContributionUsdt"), 6),
                                  number(stats.get("hourDdMaxUsdt"),6),number(stats.get("hourSharedCapitalDdPct"),6),number(stats.get("contributionUsdt"), 6),
                                  number(stats.get("cumulativeDdUsdt"), 6), number(stats.get("sharedCapitalDdPct"), 6),
                                  number(stats.get("positionsOpened"),0), number(stats.get("positionsClosed"),0), number(stats.get("openPositions"),0),
                                  number((stats.get("orders") or {}).get("entry"),0), number((stats.get("orders") or {}).get("close"),0)])
    content += "<h2>Hourly strategy contributions</h2><p class='muted'>Hour contribution includes closed cash, open mark PnL, entry fees and funding. Cumulative contributions reconcile to one shared account's equity change. Return PF uses completed positions only. Strategy DD uses its equity contribution; its percent denominator is shared starting capital. Strategy order columns count cumulative entry/close actions; shared controls remain in the account totals.</p>"+table(["Hour","Strategy","Closed n","Closed return PF","Closed net cash","Hour equity contribution","Hour DD cash","Hour DD / shared start %","Cumulative contribution","Cumulative DD cash","Cumulative DD / shared start %","Entries","Closes","Open end","Cumulative entry orders","Cumulative close orders"], strategy_rows)
    if any("feesUsdt" in stats or "fundingUsdt" in stats for hour in hours for stats in (hour.get("byStrategy") or {}).values()):
        content += "<h3>Actual hourly strategy costs and funding</h3><p class='muted'>Funding is attributed at its settlement hour. These charges/payments are already included in equity contributions.</p>"+table(["Hour","Strategy","Fees charged","Funding paid/received"],
            [[hour.get("hour"),str(name).title(),number(stats.get("feesUsdt"),6),number(stats.get("fundingUsdt"),6)] for hour in hours for name,stats in (hour.get("byStrategy") or {}).items()])
    decomposition = []
    for hour in hours:
        for name, stats in (hour.get("byStrategy") or {}).items():
            fields = ("closedNetUsdtToDate", "openMarkUsdt", "openEntryFeesUsdt", "openFundingUsdt", "feesUsdtToDate", "fundingUsdtToDate")
            if any(key in stats for key in fields):
                decomposition.append([hour.get("hour"), str(name).title(), *[number(stats.get(key), 6) for key in fields]])
    if decomposition:
        content += "<h3>Cumulative contribution components at each hour end</h3><p class='muted'>Contribution = closed net cash + open mark PnL − open entry fees + open funding. Closed net includes funding from completed positions. Fees and funding to date describe total charges/payments and are already included in the contribution.</p>"+table(["Hour","Strategy","Closed net to date","Open mark PnL","Open entry fees","Open funding","Fees to date","Funding to date"],decomposition)
    stage_rows = []
    for hour in report.get("stageHourly") or []:
        for key, name in STAGES:
            stats = (hour.get("stages") or {}).get(key) or {}
            stage_rows.append([hour.get("hour"),name,int(stats.get("n") or 0),pf(stats),number(stats.get("netAvgPct"),4),
                               number(stats.get("entryPositions"),0),number(stats.get("distinctExitBehaviours"),0)])
    content += "<h2>Independent stage calculations</h2><p class='muted'>These are hypothetical cohorts admitted at entry, separate from sized account executions. Pre-Base, Base, Main, Real and final safeguards retain distinct evidence and counts.</p>"+table(["Hour","Stage","Hypothetical closes","Return PF","Mean net %","Entry cohort","Distinct behaviours"],stage_rows)
    content += supplemental_calculations(report)+strategy_stages(report)+qualification_snapshots(report)+coverage_section(report)+htf_note(report)+execution_details(report)
    content += "<details><summary>Source, exclusions and calculation assumptions</summary><pre>"+text(json.dumps({"symbols":report.get("symbols"),"skippedSymbols":report.get("skippedSymbols"),"modelSignature":report.get("modelSignature"),"generatedAt":report.get("generatedAt"),"assumptions":report.get("assumptions"),"accountAssumptions":totals.get("assumptions"),"profileChanges":report.get("profileChanges"),"profileProvenance":report.get("profileProvenance"),"coverage":{k:v for k,v in (report.get("coverage") or {}).items() if k!="lanes"}},indent=2))+"</pre></details>"
    return content


def write(path, title, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>"+text(title)+"</title><style>"+STYLE+"</style></head><body><main>"+body+"<p class='foot'>Return PF is a cost-relative return coordinate; account cash, costs and sample counts remain separate. This report records simulated outcomes from the supplied frozen data.</p></main></body></html>")


def render_report(report, path):
    write(path, "CTS-G six-hour market audit", section(report, str(report.get("profile") or "Baseline")))


def render_comparison(baseline, experimental, path):
    rows = []
    for label, report in (("Baseline",baseline),("Experimental five-position",experimental)):
        totals = (report.get("account") or {}).get("totals") or {}
        stats = totals.get("closed") or {}
        rows.append([label,number((report.get("engine") or {}).get("requiredSamples"),0),int(stats.get("n") or 0),pf(stats),
                     number(totals.get("pnl")),number(totals.get("equityEnd")),number(totals.get("maxDdPct")),number(totals.get("fees"))])
    body = "<div class='eyebrow'>CTS-G · frozen period comparison</div><h1>Baseline and short-history experiment</h1><p class='note warn'>The five-position scenario is an exploratory alternative with limited per-configuration evidence. Results from one six-hour window do not establish optimized defaults or a future profit guarantee.</p>"+table(["Scenario","Required samples","Closed n","Return PF","Net account PnL","Ending equity","DD %","Fees"],rows)
    body += "<h2>Baseline detail</h2>"+section(baseline,"Baseline")+"<h2>Experimental detail</h2>"+section(experimental,"Experimental five-position")
    write(path,"CTS-G six-hour baseline comparison",body)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report",type=Path)
    parser.add_argument("output",type=Path)
    parser.add_argument("--experimental",type=Path)
    args = parser.parse_args()
    baseline = json.loads(args.report.read_text())
    if args.experimental:
        render_comparison(baseline,json.loads(args.experimental.read_text()),args.output)
    else:
        render_report(baseline,args.output)
