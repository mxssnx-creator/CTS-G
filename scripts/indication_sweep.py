#!/usr/bin/env python3
"""Run an independent, costed indication screen on 50 contiguous 1m histories.

The candidate universe is fixed before opening the scored slices. This is a
price/geometry screen, followed by the native StageGrid replay for finalists.
The native stage gate is deliberately absent from screening order selection.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import ctypes as ct
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from time import perf_counter
from typing import Callable

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/pulse"))
import indication_extra as ix
import indication_lib as lib
from indication_ao import AO
from indication_ao_lowrange import LOW_RANGE
from position_cost import sl_floor_frac, tp_range_of

DAYS = 10
PRE_MINUTES = 3 * 1440
DEVELOPMENT_MINUTES = 4 * 1440
ENTRY_CONFIDENCE = .58
COST_PCT = .11
STRESS_BP = 4
COOLDOWN = 2


@dataclass(frozen=True)
class Probe:
    name: str
    family: str
    clock: int
    window: int
    calculate: Callable
    aggregate: bool = False


@dataclass(frozen=True)
class Geometry:
    step: int
    ratio: float
    hold: int


GEOMETRIES = tuple(
    Geometry(step, ratio, hold)
    for step, hold in ((2, 15), (3, 15), (4, 25), (6, 25), (8, 45), (12, 45),
                       (14, 90), (20, 90), (24, 180), (32, 180), (40, 360), (50, 360))
    for ratio in (.5, .75, 1.0)
)


def wilder_adx(bars, period):
    """Previous and current Wilder ADX and (+DI - -DI) of a bounded frame."""
    if len(bars) < 2 * period + 1:
        return None
    tr = plus = minus = adx = 0.0
    previous = None
    for i, (a, b) in enumerate(zip(bars, bars[1:]), 1):
        up, down = b[1] - a[1], a[2] - b[2]
        delta_plus = up if up > max(0.0, down) else 0.0
        delta_minus = down if down > max(0.0, up) else 0.0
        delta_tr = max(b[1] - b[2], abs(b[1] - a[3]), abs(b[2] - a[3]))
        if i <= period:
            tr += delta_tr
            plus += delta_plus
            minus += delta_minus
        else:
            tr += delta_tr - tr / period
            plus += delta_plus - plus / period
            minus += delta_minus - minus / period
        if i >= period:
            dx = 100 * abs(plus - minus) / (plus + minus) if plus + minus else 0.0
            if i < 2 * period:
                adx += dx / period
            else:
                adx += (dx - adx) / period
            current = (adx, 100 * (plus - minus) / tr if tr else 0.0)
            if i == len(bars) - 1:
                return previous, current
            previous = current
    return None


def adx_onset(state, floor):
    if state is None or state[0] is None:
        return None
    (prior_adx, prior_di), (adx, di) = state
    if adx < floor or abs(di) < 5:
        return None
    if prior_adx < floor or prior_di * di <= 0:
        return (1 if di > 0 else -1), .72
    return None


def catalogue():
    probes = {}

    def add(name, family, clock, window=None, calculate=None, aggregate=False):
        if name in probes:
            return
        if window is None:
            window = ix.window_of(name)
        if calculate is None:
            calculate = ix.resolve(name)
            if calculate is None:
                raise ValueError(f"Unknown probe {name}")
        probes[name] = Probe(name, family, clock, window, calculate, aggregate)

    for family, (_, rule) in lib.FAMILIES.items():
        periods = {1: (5, 8), 5: (8, 14), 15: (14, 34), 30: (21, 55)}
        if family.startswith("trrev"):
            periods = {1: (6, 10), 5: (6, 10), 15: (10, 16), 30: (10, 16)}
        for tf, values in periods.items():
            for period in values:
                add(f"{family}_n{period}_t{tf}", family, tf, lib.rows_for(tf) // tf,
                    lambda bars, f=rule, n=period: f(bars, n), True)

    for name, (_, profile) in ix.THIN_INDICATOR_SETTINGS.items():
        family = name.split("_")[0]
        add(name, family, profile.timeframe, profile.bars, profile.calculate, True)

    for tf in (1, 3, 5, 15, 30):
        for period in (4, 7, 14, 21):
            for floor in (18, 25):
                name = f"adx_onset_n{period}_a{floor}_t{tf}"
                add(name, "adx", tf, period * 6,
                    lambda b, p=period, a=floor: adx_onset(wilder_adx(b, p), a), True)
        for period in (2, 3) if tf <= 5 else (14, 21):
            for band in (20, 30):
                name = f"rsi_reclaim_n{period}_b{band}_t{tf}"
                add(name, "rsi", tf, 8 * period,
                    lambda b, p=period, r=band: ix.rsi_band_reclaim(b, p, r), True)
        if tf in (15, 30):
            add(f"psar_slow_t{tf}", "psar", tf, 120, lambda b: ix.sar_signal(b, .01, .01, .10, False), True)
            add(f"psar_classic_t{tf}", "psar", tf, 120, lambda b: ix.sar_signal(b, .02, .02, .20, False), True)

    for name in list(ix.INDICATORS)[:32]:
        add(name, name, 5)
    for name in ix.CTS_C_TECHNICAL:
        family, tf = name.rsplit("_n", 1)[0], int(name.rsplit("_t", 1)[1])
        add(name, family, tf)
    for name in AO:
        match = re.fullmatch(r"(.+)_n(8|21|55)_t(5|15|30)", name)
        if match and (match[2], match[3]) in (("8", "5"), ("21", "15"), ("55", "30")):
            add(name, match[1], int(match[3]))
    for name in LOW_RANGE:
        match = re.fullmatch(r"ao_lr[12]_(.+)_standalone_t(1|5|15)", name)
        if match:
            add(name, "ao_lr_" + match[1], int(match[2]))
    for name in ("trend_c6", "trend_c10", "trend_c20", "brk_c6", "brk_c10", "brk_c20",
                 "g_revert_min", "g_revert_med", "g_trend_min", "g_break_min", "g_volume_min", "g_all_min"):
        add(name, name.rsplit("_", 1)[0], 15)
    return tuple(probes.values())


def market_from_feed(path, symbols=None):
    with gzip.open(path, "rt") as source:
        data = json.load(source)
    names = tuple(x["sym"] for x in data["tickers"])
    if len(names) != 50 or len(set(names)) != 50:
        raise ValueError("Expected a fixed 50-symbol universe")
    if symbols:
        names = tuple(s for s in names if s in symbols)
        if set(names) != set(symbols):
            raise ValueError("Unknown requested symbol")
    length = DAYS * 1440
    end = min(data["candles"][name + "|1"][-1]["t"] for name in names) + 60000
    start = end - length * 60000
    if start % 3600000:
        raise ValueError("Expected complete UTC hours")
    clocks = np.arange(start, end, 60000, dtype=np.int64)
    tape = {}
    for name in names:
        rows = [r for r in data["candles"][name + "|1"] if start <= r["t"] < end]
        if len(rows) != length or not np.array_equal(clocks, np.fromiter((r["t"] for r in rows), np.int64)):
            raise ValueError(f"Missing or duplicate candle in {name}")
        columns = np.array([[r[k] for k in "ohlcv"] for r in rows], dtype=np.float64)
        o, h, l, c, v = columns.T
        if (not np.all(np.isfinite(columns)) or np.any(l <= 0) or np.any(v < 0)
                or np.any(h < np.maximum(o, c)) or np.any(l > np.minimum(o, c))):
            raise ValueError(f"Invalid OHLCV in {name}")
        columns.flags.writeable = False
        tape[name] = columns
    return clocks, tape


def frame_rows(candles, tf):
    if tf == 1:
        return candles.tolist()
    groups = candles.reshape(-1, tf, 5)
    return np.column_stack((groups[:, 0, 0], groups[:, :, 1].max(axis=1),
                            groups[:, :, 2].min(axis=1), groups[:, -1, 3],
                            groups[:, :, 4].sum(axis=1))).tolist()


def signals_for(candles, probes):
    """Share OHLCV windows at their actual UTC close. Skip every intervening clock."""
    signals = np.zeros((len(probes), len(candles)), np.int8)
    rows = candles.tolist()
    for tf in sorted({probe.clock for probe in probes}):
        clocked = [(i, p) for i, p in enumerate(probes) if p.clock == tf]
        frames = frame_rows(candles, tf)
        widths = sorted({p.window for _, p in clocked if p.aggregate})
        raw_widths = sorted({p.window for _, p in clocked if not p.aggregate})
        for close in range(max(tf - 1, ((60 + tf - 1) // tf) * tf - 1), len(candles), tf):
            end = (close + 1) // tf
            shared = {w: lib._AggregatedBars(frames[max(0, end - w):end]) for w in widths}
            raw = {w: rows[max(0, close + 1 - w):close + 1] for w in raw_widths}
            adx = {}
            for i, p in clocked:
                if p.name.startswith("adx_onset_"):
                    period = p.window // 6
                    if period not in adx:
                        adx[period] = wilder_adx(shared[p.window], period)
                    floor = int(p.name.rsplit("_a", 1)[1].split("_")[0])
                    signal = adx_onset(adx[period], floor)
                else:
                    signal = p.calculate((shared if p.aggregate else raw)[p.window])
                if signal and signal[1] >= ENTRY_CONFIDENCE:
                    signals[i, close] = signal[0]
    return signals


def compile_kernel(root):
    output = root / "indication_sweep.so"
    src = Path(__file__).with_name("indication_sweep_kernel.cpp")
    subprocess.run(["c++", "-std=c++17", "-O3", "-shared", "-fPIC", str(src), "-o", str(output)], check=True)
    return output


def kernel(path):
    lib_ = ct.CDLL(str(path))
    f64 = np.ctypeslib.ndpointer(dtype=np.float64, flags="C_CONTIGUOUS")
    i32 = np.ctypeslib.ndpointer(dtype=np.int32, flags="C_CONTIGUOUS")
    i8 = np.ctypeslib.ndpointer(dtype=np.int8, flags="C_CONTIGUOUS")
    lib_.exits.argtypes = [f64, ct.c_int, ct.c_double, ct.c_double, ct.c_int, ct.c_int, i32, f64]
    lib_.exits.restype = None
    lib_.trial.argtypes = [i32, i32, f64, f64, i32, i32, i8, ct.c_int, ct.c_int, ct.c_int, ct.c_int,
                           ct.c_double, ct.c_double, f64, i32]
    lib_.trial.restype = None
    return lib_


def events(signals):
    probe, index = np.nonzero(signals)
    counts = np.bincount(probe, minlength=len(signals))
    offsets = np.empty(len(counts) + 1, np.int32)
    offsets[0] = 0
    np.cumsum(counts, out=offsets[1:])
    return offsets, index.astype(np.int32), signals[probe, index]


def screen(calc, candles, signals, begin, end, result, unclosed):
    offsets, index, dirs = events(signals)
    after = end - begin
    for k, g in enumerate(GEOMETRIES):
        tp = g.step / 1000
        sl = max(tp * g.ratio, sl_floor_frac(COST_PCT))
        close = np.empty((2, end), np.int32)
        gross = np.empty((2, end), np.float64)
        for i, side in enumerate((1, -1)):
            calc.exits(candles, end, tp, sl, g.hold, side, close[i], gross[i])
        counts = np.zeros(len(signals), np.int32)
        hourly = np.zeros((len(signals), after // 60, 5), np.float64)
        calc.trial(*close, *gross, offsets, index, dirs, len(signals), begin, end,
                   COOLDOWN, COST_PCT / 100, STRESS_BP / 10000, hourly, counts)
        result[k] += hourly
        unclosed[k] += counts
        yield hourly[..., 1].sum(axis=1) - hourly[..., 2].sum(axis=1)


def _fresh_shape(count, hours):
    return (len(GEOMETRIES), count, hours, 5)


MARKET = {}
PROBES = ()


def prepare_symbol(job):
    symbol, work = job
    started = perf_counter()
    npz = work / (symbol + ".npz")
    signals = signals_for(MARKET[symbol], PROBES)
    with npz.with_suffix(".part").open("wb") as dest:
        np.savez_compressed(dest, signals=signals)
    npz.with_suffix(".part").replace(npz)
    print(f"{symbol} signals in {(perf_counter() - started):.1f}s", flush=True)


def describe(hourly, unclosed, symbol_net):
    trades, gain, loss, stress_gain, stress_loss = hourly.sum(axis=0)
    hours = hourly[:, 1] - hourly[:, 2]
    equity = 100 + np.r_[0, np.cumsum(hours)]
    peaks = np.maximum.accumulate(equity)
    pos_symbols = symbol_net[symbol_net > 0]
    return {
        "trades": int(trades), "open": int(unclosed), "gain": float(gain), "loss": float(loss),
        "pf": round(float(gain / loss), 7) if loss else None,
        "stress_pf": round(float(stress_gain / stress_loss), 7) if stress_loss else None,
        "net_pct": round(float(gain - loss), 7),
        "hour_dd_pct": round(float(np.max((peaks - equity) / peaks) * 100), 7),
        "positive_hours": int(np.count_nonzero(hours > 1e-12)),
        "active_hours": int(np.count_nonzero(hourly[:, 0])), "hours": len(hourly),
        "net_positive_symbols": len(pos_symbols),
        "largest_symbol_gain_share": round(float(pos_symbols.max() / pos_symbols.sum()), 6) if len(pos_symbols) else None,
    }


def _iso(epoch_ms):
    return datetime.fromtimestamp(epoch_ms / 1000, timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--phase", choices=("development", "holdout"), required=True)
    parser.add_argument("--symbol", action="append", help="Small smoke test only")
    parser.add_argument("--only", help="Comma-separated source names for a smoke run or a frozen holdout menu")
    args = parser.parse_args()
    global MARKET, PROBES
    clocks, MARKET = market_from_feed(args.feed, args.symbol)
    PROBES = catalogue()
    selected = [i for i, p in enumerate(PROBES) if not args.only or p.name in args.only.split(",")]
    if not selected or not 1 <= args.jobs <= 9:
        parser.error("No selected probes or unsupported worker count")
    with args.feed.open("rb") as feed:
        feed_hash = hashlib.file_digest(feed, "sha256").hexdigest()
    manifest = {
        "feed_sha256": feed_hash,
        "probes": [{k: v for k, v in asdict(p).items() if k != "calculate"} for p in PROBES],
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "engine_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
    }
    args.work.mkdir(exist_ok=True, parents=True)
    proof = args.work / "manifest.json"
    if proof.exists() and json.loads(proof.read_text()) != manifest:
        raise ValueError("Signal cache manifest differs from this replay")
    proof.write_text(json.dumps(manifest), encoding="utf-8")
    cut = PRE_MINUTES + DEVELOPMENT_MINUTES
    begin, end = (PRE_MINUTES, cut) if args.phase == "development" else (cut, len(clocks))
    names = tuple(MARKET)
    t0 = perf_counter()
    missing = [(s, args.work) for s in names if not (args.work / (s + ".npz")).is_file()]
    if missing:
        with ProcessPoolExecutor(max_workers=min(args.jobs, len(missing))) as pool:
            list(pool.map(prepare_symbol, missing))
    signal_seconds = round(perf_counter() - t0, 2)
    calc = kernel(compile_kernel(args.work))
    result = np.zeros(_fresh_shape(len(selected), (end - begin) // 60))
    unclosed = np.zeros((len(GEOMETRIES), len(selected)), np.int32)
    nets = []
    for symbol in names:
        with np.load(args.work / (symbol + ".npz"), allow_pickle=False) as saved:
            signals = saved["signals"]
        if signals.shape != (len(PROBES), len(clocks)) or signals.dtype != np.int8:
            raise ValueError(f"Malformed signal cache for {symbol}")
        nets.append(np.stack(list(screen(calc, MARKET[symbol], signals[selected], begin, end, result, unclosed))))
    nets = np.stack(nets)
    rows = []
    for j, p in enumerate(PROBES[i] for i in selected):
        for k, g in enumerate(GEOMETRIES):
            series = result[k, j]
            stats = describe(series, unclosed[k, j], nets[:, k, j])
            record = dict(name=p.name, family=p.family, clock=p.clock, band=tp_range_of(g.step),
                          **asdict(g), **stats)
            if args.phase == "development":
                for label, sl in (("first", slice(0, 48)), ("second", slice(48, 96))):
                    totals = series[sl].sum(axis=0)
                    record[label + "_pf"] = round(float(totals[1]/totals[2]), 7) if totals[2] else None
            rows.append(record)
    payload = {**manifest, "phase": args.phase, "symbols": names, "prehistory_from": _iso(clocks[0]),
               "from": _iso(clocks[begin]), "until": _iso(clocks[end - 1] + 60000),
               "cost_pct": COST_PCT, "stress_bp": STRESS_BP, "signal_seconds": signal_seconds,
               "elapsed_s": round(perf_counter() - t0, 2),
               "results": rows}
    args.out.write_text(json.dumps(payload, allow_nan=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(names)} symbols, {len(selected)} probes, {len(rows)} trials, {payload['elapsed_s']} s", flush=True)


if __name__ == "__main__":
    main()
