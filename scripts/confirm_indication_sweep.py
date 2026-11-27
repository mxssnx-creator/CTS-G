#!/usr/bin/env python3
"""Independent native CTS-G replays of an indication study's frozen shortlist."""
from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
import json
import os
from pathlib import Path
import resource
from statistics import median
from time import perf_counter
from unittest.mock import patch

import numpy as np

from indication_sweep import (
    COST_PCT, DEVELOPMENT_MINUTES, PRE_MINUTES, STRESS_BP, ROOT,
    catalogue, ix, market_from_feed, _iso,
)
import stage_eval as sg
import set_engine as se
from stage_grid import GridResult, StageCount, STAGES, window_blocks

DATA = None
NAMES = ()
STUDY_WINDOWS = {"short": [4], "last": [1, 2, 3, 4, 5]}
FOCUSED_GRID = {
    "base": {**STUDY_WINDOWS, "long": [10]},
    **{stage: {**STUDY_WINDOWS, "longModes": ["none"]} for stage in STAGES[1:]},
}


class CandleView:
    """Run entries directly on the packed source instead of retaining Python rows."""

    def __init__(self, columns):
        self.columns = columns

    def __len__(self):
        return len(self.columns)

    def __getitem__(self, key):
        rows = self.columns[key]
        return CandleView(rows) if isinstance(key, slice) else rows.tolist()


def profile(case):
    return {
        "label": f"{case['name']} · TP {case['step'] / 10:g}%",
        "enabled": True,
        "names": [case["name"]],
        "stepMin": case["step"],
        "stepMax": case["step"],
        "slRatios": [case["ratio"]],
        "types": ["base"],
        "pause": 0,
        "signalIntervalMinutes": case["clock"],
        "holdMinutes": case["hold"],
    }


def register_research_sources(cases):
    """Use the candidate formula in the real engine before choosing what ships."""
    by_name = {probe.name: probe for probe in catalogue()}
    for case in cases:
        name = case["name"]
        if ix.known(name):
            continue
        probe = by_name[name]
        if not probe.aggregate:
            raise ValueError(f"Unregistered research source {name}")
        rule = ix._ThinIndicator(probe.clock, probe.window, probe.calculate)
        ix.THIN_INDICATOR_SETTINGS[name] = probe.window * probe.clock, rule
        ix.INDICATORS[name] = rule
        ix.WINDOWS[name] = probe.window * probe.clock


def settings(cases, cost, stage_policy=None):
    return {
        "stratIndications": False, "stratGeneral": False, "stratShortRange": False,
        "stratTrend": False, "stratBreakout": False, "stratReversion": False,
        "stratFlow": False, "stratMulti": False, "stratSignals": False,
        "stratBlock": False, "stratDca": False, "extraIndicators": [],
        "indicationConfigs": [profile(case) for case in cases],
        "directionMode": "split", "stageLastN": stage_policy or {"enabled": True},
        "holdEnabled": True, "holdAuto": False, "setStepAdapt": False,
        "positionCostPct": cost, "setEntryConf": .58, "setCooldownBars": 2,
        "pauseMode": "off", "setScratchEnabled": False,
        "histLookbackBars": PRE_MINUTES, "histLookbackAuto": False,
    }


def init_replay(clocks, market, names, window, cost, stage_policy):
    global DATA, NAMES
    NAMES = names
    DATA = clocks, {s: CandleView(market[s]) for s in names}, market, window, cost, stage_policy
    sg.OVERLAY = str(ROOT / "server/pulse/overlay-bingx-x02.json")


def drawdowns(trades, still_open, market, clock, start, stop, cost):
    """Per-minute equal-notional cash and mark-to-market paths."""
    cash = np.zeros(stop - start + 1)
    marks = np.zeros_like(cash)
    for trade in [*trades, *still_open]:
        entry = round((trade["t_in"] - clock) / 60)
        if entry < start or entry >= stop:
            raise ValueError("Invalid replay admission time")
        closed = not trade.get("open_at_end")
        exit_ = round((trade["t"] - clock) / 60) if closed else stop
        net = trade["net"] if closed else trade["mark_net"]
        price = trade["tr"]["entry_px"] if closed else trade["entry"]
        direction = int(trade["tr"]["dir"]) if closed else (1 if trade["side"] == "LONG" else -1)
        series = market[trade["symbol"]][entry:exit_ - 1, 3]
        path = ((series - price) / price * direction) - cost
        marks[entry - start] -= cost
        marks[entry - start + 1:exit_ - start + 1] += np.diff(np.r_[-cost, path, net])
        if closed:
            cash[exit_ - start] += net
    paths = {}
    for name, changes in (("realized", cash), ("marked", marks)):
        equity = 100 + np.cumsum(changes)
        peaks = np.maximum.accumulate(np.r_[100., equity])[1:]
        paths[name + "_dd_pct"] = float(np.max((peaks - equity) / peaks) * 100)
    return paths


def summarize(trades, open_, market, clock, begin, end, cost, hours):
    vals = np.fromiter((r["net"] for r in trades), np.float64)
    gain, loss = vals[vals > 0].sum(), -vals[vals < 0].sum()
    stressed = vals - STRESS_BP / 10000
    stress_gain, stress_loss = stressed[stressed > 0].sum(), -stressed[stressed < 0].sum()
    hourly = [{"t": h, "orders": 0, "opened": 0, "net_pct": 0.0} for h in hours]
    symbol_net = defaultdict(float)
    for row in trades:
        bucket = int((row["t"] - hours[0] - 1) // 3600)
        hourly[bucket]["orders"] += 1
        hourly[bucket]["net_pct"] += row["net"] * 100
        symbol_net[row["symbol"]] += row["net"]
    for row in [*trades, *open_]:
        hourly[int((row["t_in"] - hours[0]) // 3600)]["opened"] += 1
    positive_symbols = [pnl for pnl in symbol_net.values() if pnl > 0]
    result = {
        "trades": len(trades), "open": len(open_), "pf": float(gain / loss) if loss > 0 else None,
        "stress_pf": float(stress_gain / stress_loss) if stress_loss > 0 else None,
        "net_pct": float(gain - loss), "open_mark_pct": sum(row["mark_net"] for row in open_),
        "positive_hours": sum(h["net_pct"] > 1e-10 for h in hourly),
        "active_hours": sum(h["orders"] > 0 for h in hourly), "hours": len(hourly),
        "positive_symbols": len(positive_symbols),
        "largest_symbol_gain_share": max(positive_symbols) / sum(positive_symbols) if positive_symbols else None,
        "hourly": hourly,
    }
    result.update(drawdowns(trades, open_, market, clock, begin, end, cost))
    return result


def native_shadow(market, times):
    """Use exactly the live signal and shadow-tape functions for each symbol."""
    from replay_storage import compact_shadow
    sg._init_replay_market({}, {})
    out = {}
    for symbol in NAMES:
        _, warm, dirs, confs, sets = sg._symbol_job((symbol, market[symbol].tolist(), times[symbol][0]))
        part = {symbol: (warm, dirs, confs, sets)}
        compact_shadow(part)
        out[symbol] = part[symbol]
    return out


def first_exec_path(history, now, *, whole_tape=False):
    """Evaluate entry eligibility without ranking other passing paths.

    Each portfolio in this study has one Set per signal and direction.
    The simulator can admit that Set as soon as one complete path passes.
    Native ranking and related-set evidence are checked separately.
    """
    root = history.root if whole_tape else history._decision_tape(now)
    options, bounds = history.grid.rules, history.grid.sample_bounds
    stages = len(STAGES)

    @lru_cache(maxsize=128)
    def walk(tape, depth, base_long):
        start, end = tape.indices(now)
        if end - start < bounds[depth, base_long][0]:
            return (), None
        if depth == 1 and history.main_filter and not history.main_filter(
            tape.values[max(start, end - history.filter_need):end]
        ):
            return (), None
        tested = {}

        def evidence(n, offset, kind):
            key = n, offset, kind
            if key not in tested:
                tested[key] = tape.test(n, end - offset, kind, now, offset)
            return tested[key]

        for window in options[depth, base_long]:
            if window.need > end - start:
                continue
            if any(n and evidence(n, offset, kind) is None for n, offset, kind in window_blocks(window, depth)):
                continue
            total = evidence(window.need, 0, "pf")
            if total is None:
                continue
            if depth == stages - 1:
                return (window,), total
            long = window.long if depth == 0 else base_long
            mask = tape.child_mask(window, depth)
            if (mask >> start & ((1 << (end - start)) - 1)).bit_count() < bounds[depth + 1, long][0]:
                continue
            path, result = walk(history.child(tape, mask), depth + 1, long)
            if path:
                return (window, *path), result
        return (), None

    try:
        path, found = walk(root, 0, 0)
    finally:
        walk.cache_clear()
    return GridResult(tuple(StageCount() for _ in STAGES), path, found) if path else history.grid.insufficient()


class PreparedEligibility:
    """Reuse immutable related-set membership across decision clocks.

    With a full three-day lookback, the four-stage context covers this entire
    replay. Masks are causal at each historical entry; a current decision
    always bisects each parent at its own strictly closed prefix.
    """

    def __init__(self):
        self.histories = {}
        self.verdicts = {}

    def __call__(self, book, tapes, st, now, *, streams=False):
        close, entry, gross = tapes.by_set[st.idx]
        params = book.stage_params(st.pack, st)
        cutoff = bisect_left(close, now - book.history_bars() * 60)
        end = bisect_left(close, now)
        visible = bisect_left(close, now - params.lookback_s, cutoff, end)
        if end - visible < book.stage_grid.sample_bounds[0, 0][0]:
            return book.stage_grid.insufficient()
        bounds = cutoff, end, visible, book.grid_clock_key(params, now, close[visible])
        existing = self.verdicts.get(st.idx)
        if existing and existing[0] == bounds:
            return existing[1]
        prepared = self.histories.get(st.idx)
        if prepared is None or prepared[0] != cutoff:
            history = book.grid_history(st, closes=close[cutoff:], entries=entry[cutoff:],
                                        nets=[p - tapes.cf for p in gross[cutoff:]], params=params)
            prepared = cutoff, history
            self.histories[st.idx] = prepared
        result = first_exec_path(prepared[1], now, whole_tape=True)
        self.verdicts[st.idx] = bounds, result
        return result


def run_case(cases):
    clocks, bars, market, window, cost, stage_policy = DATA
    clock = int(clocks[0] // 1000)
    begin, end = window
    t0 = perf_counter()
    register_research_sources(cases)
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(settings(cases, cost, stage_policy))
    book = sg.make_book()
    if len(book.by_idx) != 2 * len(cases) or not book.grid_auto_enabled():
        raise ValueError("Each case must have two direction Sets and independent Auto PF enabled")
    cadence = range(clock, clock + len(bars[NAMES[0]]) * 60, 60)
    times = dict.fromkeys(NAMES, cadence)
    shadow = native_shadow(market, times)
    print(f"{cases[0]['name']} native tapes ready ({perf_counter() - t0:.1f}s)", flush=True)
    tapes = sg.Tapes(book, NAMES, times, shadow)
    hours = tuple(range(clock + begin * 60, clock + end * 60, 3600))
    checked, failed = sg.self_check(book, tapes, hours, samples=2)
    if failed:
        raise ValueError(f"Native/replay stage verdict mismatch: {failed}/{checked}")
    print(f"{cases[0]['name']} native rank and gate checks {checked}/{checked}", flush=True)
    tapes.__dict__.pop("_exec_grid_query", None)
    eligibility = PreparedEligibility()
    with patch.object(sg, "grid_at", eligibility):
        rows = sg.execute(book, NAMES, bars, times, shadow, tapes, hours,
                          refresh_s=0, include_open=True, indexed_exits=False)
    tapes.__dict__.pop("_exec_grid_query", None)
    trades = [r for r in rows if "net" in r]
    still_open = [r for r in rows if r.get("open_at_end") and r["t_in"] < clock + end * 60]
    report = summarize(trades, still_open, market, clock, begin, end, cost / 100, hours)
    report["stage_checks"] = checked
    report["stage_support"] = {name: 0 for name in STAGES}
    stage_pf = {name: [] for name in STAGES}
    report["stage_samples"] = 0
    for now in hours[::24]:
        for st in book.by_idx:
            report["stage_samples"] += 1
            result = sg.grid_at(book, tapes, st, now)
            eligible = eligibility(book, tapes, st, now).ready
            if result.ready != eligible:
                raise ValueError("Early exit eligibility differed from native ranking")
            for name, count in zip(STAGES, result.counts):
                if count.qualified:
                    report["stage_support"][name] += 1
            for name, evidence in zip(STAGES, result.post_evidence):
                if evidence.loss:
                    stage_pf[name].append(evidence.gain / evidence.loss)
    report["median_post_stage_pf"] = {name: median(values) if values else None for name, values in stage_pf.items()}
    report["min_sl_distance_pct"] = book.min_sl_distance_pct
    report.update(cases=cases, seconds=round(perf_counter() - t0, 2),
                  peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    print(f"{','.join(row['name'] for row in cases)}: PF {report['pf']} / {len(trades)} trades / "
          f"{report['positive_hours']}/{len(hours)} positive hours / {report['seconds']}s",
          flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", type=Path, required=True)
    parser.add_argument("--roster", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--phase", choices=("development", "holdout"), required=True)
    parser.add_argument("--cost", type=float, default=COST_PCT)
    parser.add_argument("--focused", action="store_true", help="Exact native replay using the frozen smaller signed study grid")
    parser.add_argument("--portfolio", action="store_true")
    parser.add_argument("--symbol", action="append", help="Small smoke test only")
    args = parser.parse_args()
    roster = json.loads(args.roster.read_text())
    if not roster or args.jobs < 1:
        parser.error("A confirmation needs at least one case and one worker")
    cases = [roster] if args.portfolio else [[c] for c in roster]
    clocks, market = market_from_feed(args.feed, args.symbol)
    names = tuple(market)
    split = PRE_MINUTES + DEVELOPMENT_MINUTES
    window = (PRE_MINUTES, split) if args.phase == "development" else (split, len(clocks))
    end = window[1]
    stage_policy = FOCUSED_GRID if args.focused else {"enabled": True}
    market = {s: v[:end] for s, v in market.items()}
    t0 = perf_counter()
    report = []
    def record(results):
        for result in results:
            report.append(result)
            path = args.out.with_suffix(".part")
            path.write_text(json.dumps({
                "phase": args.phase, "symbols": names, "from": _iso(clocks[window[0]]),
                "until": _iso(clocks[end - 1] + 60000), "cost_pct": args.cost, "stage_policy": stage_policy,
                "seconds": round(perf_counter() - t0, 2), "results": report,
                "cases_complete": len(report), "cases_total": len(cases),
            }, allow_nan=False, separators=(",", ":")), encoding="utf-8")
            path.replace(args.out)

    initargs = (clocks, market, names, window, args.cost, stage_policy)
    if args.jobs == 1:
        init_replay(*initargs)
        record(map(run_case, cases))
    else:
        with ProcessPoolExecutor(max_workers=args.jobs, initializer=init_replay, initargs=initargs) as pool:
            record(pool.map(run_case, cases))


if __name__ == "__main__":
    main()
