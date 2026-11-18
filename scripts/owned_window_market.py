#!/usr/bin/env python3
"""Prepare and verify the frozen October 2026 12-symbol independent-window replay."""
import argparse
import csv
import hashlib
import io
import json
import math
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

SYMBOLS = ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "TRX", "LINK", "AVAX", "SUI", "LTC")
SOURCES = ("momchg_n5_t1", "ao_lr12_breakout_trend_t5")
HOUR_MS = 3_600_000
WARM_HOURS = 72
REPORT_HOURS = 168
BASE = "https://data.binance.vision/data/futures/um/monthly/klines"


def read_url(url):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read()
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def prepare_symbol(root, symbol, start, end):
    contract = symbol + "USDT"
    name = f"{contract}-1m-2026-10"
    url = f"{BASE}/{contract}/1m/{name}.zip"
    expected = read_url(url + ".CHECKSUM").decode("ascii").split()[0]
    raw = read_url(url)
    checksum = hashlib.sha256(raw).hexdigest()
    if checksum != expected:
        raise ValueError(f"{symbol} archive checksum mismatch")
    rows = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive, archive.open(name + ".csv") as stream:
        for record in csv.reader(io.TextIOWrapper(stream, encoding="utf-8-sig")):
            if not record or record[0] == "open_time":
                continue
            timestamp = int(record[0])
            if timestamp >= 10**15:
                timestamp //= 1000
            if not start <= timestamp < end:
                continue
            op, hi, lo, cl, vol = map(float, record[1:6])
            if timestamp != start + 60000 * len(rows) or not all(map(math.isfinite, (op, hi, lo, cl, vol))):
                raise ValueError(f"{symbol} minute sequence or numeric data invalid at {timestamp}")
            if lo <= 0 or lo > min(op, cl) or hi < max(op, cl) or vol < 0:
                raise ValueError(f"{symbol} invalid OHLCV at {timestamp}")
            rows.append([timestamp, op, hi, lo, cl, vol])
    if len(rows) != (end - start) // 60000:
        raise ValueError(f"{symbol} has {len(rows)} candles")
    (root / "candles" / f"{symbol}-USDT.json").write_text(json.dumps(rows, separators=(",", ":")))
    return {"symbol": symbol, "source": url, "sha256": checksum, "minutes": len(rows)}


def prepare(root, start, end):
    (root / "candles").mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        provenance = list(pool.map(lambda symbol: prepare_symbol(root, symbol, start, end), SYMBOLS))
    profile = {"id": "window-profile", "scope": "config",
               "lastN": {"baseShort": 0, "mainLong": 6, "mainShort": [1], "realNegN": 5},
               "windowGrid": {"baseLong": [8, 12], "realN": [3, 5]}}
    entry = {"names": list(SOURCES), "symbols": [s + "-USDT" for s in SYMBOLS], "stepMin": 6, "stepMax": 6,
             "trailStepMin": 6, "trailStepMax": 6, "types": ["base", "trail"], "lastNLanes": [profile]}
    settings = {key: False for key in ("stratGeneral", "stratIndications", "stratShortRange", "stratTrend",
                                        "stratBreakout", "stratReversion", "stratFlow", "stratMulti", "stratSignals")}
    settings.update({"extraIndicators": [], "indicationConfigs": [entry], "directionMode": "split",
                     "minPfByStep": True, "minPfStepAuto": True, "minPfStepAutoScope": "step",
                     "holdAuto": False, "histLookbackAuto": False, "histLookbackBars": 1440,
                     "setPriceDistanceGrid": True, "setPriceSlDistances": [.6], "setPriceTrailGaps": [.4],
                     "setTrailingDistance": "both", "exitIgnoreTp": True, "trailMinStep": 3, "trailStepMax": 6,
                     "trailStepMin": 6, "trailMinLockPct": 0, "trailRanges": {"minimal": True, "short": True}})
    (root / "overlay.json").write_text(json.dumps(settings, indent=2))
    (root / "provenance.json").write_text(json.dumps(provenance, indent=2))


def protection(row):
    if row["kind"] != "trail":
        return row["kind"]
    return "fixed" if ":fixed" in row["trail"] else "dynamic"


def closed_outcomes(configs):
    gains = math.fsum(c["exec_gain"] for c in configs)
    losses = math.fsum(c["exec_loss"] for c in configs)
    closes = sum(c["orders"] for c in configs)
    return {"closes": closes, "gross_gain_pct": 100 * gains, "gross_loss_pct": 100 * losses,
            "net_pct": round(100 * (gains - losses), 4), "pf": round(gains / losses, 3) if losses else None}


def summarize(root, start, end):
    report = json.loads((root / "result.json").read_text())
    if len(report["symbols"]) != len(SYMBOLS) or report["hours"] != REPORT_HOURS:
        raise ValueError("incomplete symbol or time coverage")
    configs = report["configurations"]
    modes = {("fixed" if ":fixed" in row["trail"] else "dynamic") for row in configs if row["kind"] == "trail"}
    sources = {r["pack"].split("~cfg")[0].removeprefix("extra.") for r in configs}
    if modes != {"fixed", "dynamic"} or sources != set(SOURCES):
        raise ValueError(f"missing or unexpected protection modes or sources: {modes}, {sources}")
    executions = report["trades"]
    if len(executions) != sum(row["orders"] for row in report["settled_hourly"]):
        raise ValueError("hourly subledger does not reconcile to executed closes")
    outcome = {"read_from_ms": start, "evaluate_from_ms": end - REPORT_HOURS * HOUR_MS, "end_exclusive_ms": end,
               "symbols": len(report["symbols"]), "sets": report["sets"], "hours": report["hours"],
               "protection_modes": sorted(modes), "indications": sorted(sources), "evaluation_pf": report["all"]["eff_pf"],
               "executed_pf": report["all"]["exec_pf"], "closed": len(executions),
               "market_entries": len({(r["symbol"], r["side"], r["t_in"]) for r in executions}),
               "kinds": report["types"], "engine_cost_pct": report["config"]["cost_pct"], "runtime": report["runtime"]}
    outcome["next_8_pf"] = report["all"]["next8_pf"]
    outcome["next_50_pf"] = report["all"]["next50_pf"]
    kinds = sorted({protection(c) for c in configs})
    def source(row):
        return row["pack"].split("~cfg", 1)[0].removeprefix("extra.")
    # Aggregate the native unrounded per-Set gains and losses. A zero-loss group has no finite PF.
    outcome["by_protection"] = {mode: closed_outcomes([r for r in configs if protection(r) == mode])
                                for mode in kinds}
    outcome["by_indication"] = {name: closed_outcomes([r for r in configs if source(r) == name])
                                for name in SOURCES}
    outcome["by_indication_protection"] = {
        name: {mode: closed_outcomes([r for r in configs if source(r) == name and protection(r) == mode])
               for mode in kinds} for name in SOURCES}
    for groups in (outcome["by_protection"].values(), outcome["by_indication"].values(),
                   [x for modes in outcome["by_indication_protection"].values() for x in modes.values()]):
        if sum(group["closes"] for group in groups) != len(executions):
            raise ValueError("grouped settlement counts do not reconcile")
        if not math.isclose(math.fsum(group["gross_gain_pct"] for group in groups),
                            100 * report["all"]["exec_gain"], rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("grouped gains differ from the native ledger")
        if not math.isclose(math.fsum(group["gross_loss_pct"] for group in groups),
                            100 * report["all"]["exec_loss"], rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("grouped losses differ from the native ledger")
    (root / "summary.json").write_text(json.dumps(outcome, indent=2))
    print("Owned-window replay", json.dumps({k: v for k, v in outcome.items() if k != "kinds"}, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "summarize"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--end-day", type=int, choices=(14, 21, 28), default=14,
                        help="End of the frozen seven-day October 2026 UTC window, exclusive")
    args = parser.parse_args()
    end = int(datetime(2026, 10, args.end_day, tzinfo=timezone.utc).timestamp() * 1000)
    start = end - (REPORT_HOURS + WARM_HOURS) * HOUR_MS
    (prepare if args.action == "prepare" else summarize)(args.directory, start, end)
