#!/usr/bin/env python3
"""Replay CTS-G's Base → Exec PF matrix on a contiguous archive of 1m candles."""
from array import array
import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
import sys
import time
from typing import NamedTuple

import numpy as np
import replay_cache
import stage_eval as sg
from post_base_gates import (
    BASE_FLOORS, BASE_WINDOWS, EXEC_FLOORS, EXEC_WINDOWS, PORTFOLIO,
    SetDecisions, path_name,
)


BAR = 60
HOUR = 3600
ARMS = PORTFOLIO + 1


class Trade(NamedTuple):
    symbol: str
    side: int
    set_idx: int
    base: int
    execution: int
    first: int
    last: int
    entry: float
    exit: float
    net: float
    reason: str

    @property
    def held(self):
        return self.reason == "held"


def encode(set_idx, base, execution):
    return (set_idx * len(BASE_WINDOWS) + base) * PORTFOLIO + execution + 1


def decode(code):
    owner, execution = divmod(int(code) - 1, PORTFOLIO)
    set_idx, base = divmod(owner, len(BASE_WINDOWS))
    return set_idx, base, execution


def set_columns(st, names, shadow, first):
    # Match the engine's time, symbol tie-break and one cost deduction per row.
    rows = []
    for symbol in names:
        entries, exits, gross = shadow[symbol][3][st.idx]
        rows.extend((exit, symbol, entry, pnl) for entry, exit, pnl in zip(entries, exits, gross))
    rows.sort()
    return (
        array("d", (first + BAR * (exit + 1) for exit, _, _, _ in rows)),
        array("d", (first + BAR * (entry + 1) for _, _, entry, _ in rows)),
        array("d", (pnl for _, _, _, pnl in rows)),
    )


def candidate_index(book, groups, columns, clocks, floor):
    picks = {}
    base_pass = np.zeros(len(BASE_WINDOWS), dtype=np.int64)
    base_full = np.zeros_like(base_pass)
    full = np.zeros((ARMS, len(clocks)), dtype=np.int32)
    eligible = np.zeros(len(clocks), dtype=np.int32)
    passed = np.zeros((len(EXEC_FLOORS), ARMS, len(clocks)), dtype=np.int32)
    base_sets = []
    for pack, states in groups.items():
        sides = {side: (np.zeros(passed.shape, dtype=np.uint32), np.zeros(passed.shape))
                 for side in (1, -1)}
        kind_priority = {kind: 10 * (len(book.kind_order_for(pack)) - i)
                         for i, kinds in enumerate(book.kind_order_for(pack)) for kind in kinds}
        for st in states:
            ds = SetDecisions(book, st, columns[st.idx], clocks, floor)
            base_pass += ds.base_pass
            base_full += ds.base_full
            eligible += ds.base_valid
            if ds.base_valid.any():
                base_sets.append([st.idx, int(ds.base_valid.sum())])
            full[:, :] += np.vstack([ds.full, ds.full.any(axis=0)])
            qual = ds.parents >= 0
            passed += np.concatenate((qual, qual.any(axis=1, keepdims=True)), axis=1)
            if not qual.any():
                continue

            # Native kind priority first. Within it, compare sample-discounted
            # evidence, then its size, then stable Set order.
            sample_bias = np.array([(w.short + w.last) * 1e-14 for w in EXEC_WINDOWS])
            individual = ds.scores + sample_bias[None, :, None]
            strongest = individual.argmax(axis=1, keepdims=True)
            end_choices = np.broadcast_to(np.arange(PORTFOLIO)[None, :, None], qual.shape)
            selected = np.concatenate((end_choices, strongest), axis=1)
            parents = np.concatenate((ds.parents, np.take_along_axis(ds.parents, strongest, axis=1)), axis=1)
            ranks = np.concatenate((individual, np.take_along_axis(individual, strongest, axis=1)), axis=1)
            ranks += kind_priority.get(st.kind, 0)
            for side in (1, -1) if st.side == 0 else (st.side,):
                ids, best = sides[side]
                update = (parents >= 0) & (ranks > best)
                ids[update] = encode(st.idx, parents[update].astype(np.int32), selected[update])
                best[update] = ranks[update]
        for side, (ids, _) in sides.items():
            if ids.any():
                picks[pack, side] = ids

    def hourly(rows):
        return rows.reshape(*rows.shape[:-1], -1, 60).sum(axis=-1).tolist()

    report = {"baseFull": base_full.tolist(), "basePassed": base_pass.tolist(), "baseSets": base_sets,
              "setMinutes": len(book.by_idx) * len(clocks), "eligible": hourly(eligible),
              "execFull": hourly(full), "execPassed": hourly(passed)}
    return picks, report


class MarketReplay:
    def __init__(self, book, names, bars, times, shadow, test_hours):
        first = times[names[0]][0]
        count = len(times[names[0]])
        expected = list(range(first, first + BAR * count, BAR))
        if any(times[s] != expected for s in names):
            raise ValueError("all symbols must share a complete minute clock")
        self.end = first + BAR * count
        self.start = self.end - HOUR * test_hours
        self.first = first
        if self.start % HOUR or self.start - first < max(book.lookback, book.warmup) * BAR:
            raise ValueError("use whole UTC hours with sufficient prehistory for the native replay")
        self.clocks = np.arange(self.start, self.end, BAR)
        self.book, self.names, self.bars, self.shadow = book, names, bars, shadow
        self.cost = sg.cost_as_frac(book.cost_pct)
        self.cfg = sg.lane_cfg(book)
        self.accs = {s: sg.se.BarIndex(bars[s]) for s in names}
        self.choices = self._choices()
        self.max_open = int(book.processing_overlay.get("maxOpen") or 0)

    def _choices(self):
        """Stable indication strength order at the completed candle's clock."""
        result = []
        for symbol in self.names:
            warm, dirs, confs, _ = self.shadow[symbol]
            packs = [p for p in self.book.packs if self.book.symbol_allows(p, symbol)]
            signal_rows = []
            for now in self.clocks:
                bar = int((now - self.first) // BAR) - 1
                side = {1: [], -1: []}
                if bar >= warm:
                    for pack in packs:
                        direction = dirs[pack][bar]
                        confidence = confs[pack][bar]
                        if direction and confidence >= self.cfg["entry_conf"]:
                            side[direction].append((confidence, pack))
                signal_rows.append(tuple(tuple(p for _, p in sorted(side[d], key=lambda x: -x[0]))
                                         for d in (1, -1)))
            result.append(signal_rows)
        return result

    def entry(self, symbol, side, set_idx, bar):
        """Run the native Set exit and cooldown rules on one selected entry."""
        st = self.book.by_idx[set_idx]
        _, dirs, confs, shadow_rows = self.shadow[symbol]
        candles = self.bars[symbol]
        lane = sg.se.SetLane(st, symbol)
        orders = lane.scan(bar, len(candles), candles, dirs[st.pack], confs[st.pack], True,
                           self.cfg, (bar,), self.accs[symbol])
        if orders:
            order = orders[0]
            end = order["exit_i"]
            net = order["pnl_pct"] - self.cost
            rest = int(self.cfg["cooldown"])
            plan = (shadow_rows.get(-1) or {}).get((st.pack, st.side))
            if net > 0 and plan is not None:
                rest = max(rest, int(plan[end]))
            return end + rest, end, order["entry_px"], order["exit_px"], net, order["reason"]
        if lane.open is None:
            raise ValueError(f"qualified entry failed to open {symbol} {st.id} bar {bar}")
        entry = lane.open["entry"]
        end = len(candles) - 1
        mark = candles[end][3]
        return end, end, entry, mark, side * (mark / entry - 1) - self.cost, "held"

    def execute(self, index):
        """Walk every arm independently, sharing only deterministic fill calculations."""
        runs = [[[] for _ in range(ARMS)] for _ in EXEC_FLOORS]
        busy = np.full((len(self.names), 2, len(EXEC_FLOORS), ARMS), -1, dtype=np.int32)
        active = [[] for _ in range(len(EXEC_FLOORS) * ARMS)]
        for minute, now in enumerate(self.clocks):
            bar = int((now - self.first) // BAR) - 1
            capacity = None
            if self.max_open > 0:
                capacity = np.empty(len(active), dtype=bool)
                for i, ends in enumerate(active):
                    ends[:] = [end for end in ends if end > bar]
                    capacity[i] = len(ends) < self.max_open
            for s, symbol in enumerate(self.names):
                for d, side in enumerate((1, -1)):
                    used_until = busy[s, d].reshape(-1)
                    available = used_until < bar
                    if capacity is not None:
                        available &= capacity
                    arms = np.flatnonzero(available)
                    fills = {}
                    for pack in self.choices[s][minute][d]:
                        if not len(arms):
                            break
                        grid = index.get((pack, side))
                        if grid is None:
                            continue
                        codes = grid[:, :, minute].reshape(-1)[arms]
                        accepted = codes != 0
                        for arm, code in zip(arms[accepted], codes[accepted]):
                            st_idx, base, exec_idx = decode(code)
                            if st_idx not in fills:
                                fills[st_idx] = self.entry(symbol, side, st_idx, bar)
                            release, last, entry, exit, net, reason = fills[st_idx]
                            j, k = divmod(int(arm), ARMS)
                            runs[j][k].append(Trade(symbol, side, st_idx, base, exec_idx, bar, last, entry, exit, net, reason))
                            used_until[arm] = release
                            if capacity is not None:
                                active[arm].append(last if reason != "held" else float("inf"))
                                capacity[arm] = len(active[arm]) < self.max_open
                        arms = arms[~accepted]
        return runs

    def clock(self, bar):
        return self.first + BAR * (bar + 1)


def offline(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise RuntimeError("historical replay cannot open network connections")


def run_floor(market, groups, columns, overlay, label, floor):
    from post_base_results import summarize, strategy_cases

    t = time.monotonic()
    index, coverage = candidate_index(market.book, groups, columns, market.clocks, floor)
    print(f"{label}: Base PF {floor:g} decisions complete in {time.monotonic()-t:.1f}s", flush=True)
    runs = market.execute(index)
    del index
    cases = []
    for j, exec_floor in enumerate(EXEC_FLOORS):
        portfolio = runs[j][PORTFOLIO]
        for k, trades in enumerate(runs[j][:-1]):
            cases.append({"basePf": floor, "execPf": exec_floor, "window": k,
                          "axis": "off", "block": "off", **summarize(market, trades)})
        cases.extend(strategy_cases(market, columns, overlay, portfolio, floor, exec_floor))
        print(f"{label}: Base {floor:g} Exec {exec_floor:g}, {len(portfolio)} portfolio opens", flush=True)
    return {"basePf": floor, **coverage}, cases


_FLOOR_CONTEXT = None


def init_floors(market, groups, columns, overlay, label):
    global _FLOOR_CONTEXT
    _FLOOR_CONTEXT = market, groups, columns, overlay, label


def run_worker(floor):
    return run_floor(*_FLOOR_CONTEXT, floor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--patch", type=Path, help="JSON overlay patch for the replay universe")
    parser.add_argument("--symbols", type=int, default=30)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--jobs", type=int, default=3)
    args = parser.parse_args()
    if not 0 < args.hours <= 48 or args.symbols < 1 or args.jobs < 1:
        parser.error("use 1–48 test hours and positive symbol and worker counts")
    if args.patch:
        config = json.loads(args.patch.read_text())
        if not isinstance(config, dict):
            parser.error("the overlay patch must be an object")
        os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(config)

    sys.addaudithook(offline)
    began = time.monotonic()
    overlay = sg.replay_overlay()
    book = sg.make_book()
    if not book.stage_grid or book.stage_grid.windows(0, 0) != BASE_WINDOWS or book.hold_auto:
        raise ValueError("use the complete native Base grid with fixed hold policies")
    names, bars, times = sg.load_bars(str(args.data), args.symbols)
    if not names:
        raise ValueError("no historical candles found")
    manifest = replay_cache.fingerprint(str(args.data), names, overlay, sg.PULSE, sg.__file__)
    meta_path = args.data / "_meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    if any(meta.get("sha256", {}).get(name) != manifest["markets"][name] for name in names) and "sha256" in meta:
        raise ValueError("historical data fails its archive checksum")
    cache = str(args.cache)
    if args.cache.is_file():
        shadow = replay_cache.load(cache, manifest)
    else:
        shadow = sg.build(names, bars, times, jobs=args.jobs)
        replay_cache.save(cache, shadow, manifest)
    market = MarketReplay(book, names, bars, times, shadow, args.hours)
    groups = defaultdict(list)
    columns = {}
    for st in book.by_idx:
        groups[st.pack].append(st)
        columns[st.idx] = set_columns(st, names, shadow, market.first)
    result = {"label": args.label, "start": market.start, "end": market.end, "prehistoryStart": market.first,
              "costPct": book.cost_pct, "lookbackMinutes": book.lookback, "hours": args.hours,
              "symbols": names, "source": meta.get("source"), "inputHashes": manifest["markets"],
              "overlayPatch": json.loads(os.environ.get("STAGE_OVERLAY_PATCH", "{}")),
              "engineHashes": manifest["engine"], "overlayHash": manifest["settings"],
              "sets": [[st.id, st.pack, st.kind, st.side, sg.se.tp_range_of(book.step_bucket(st))]
                       for st in book.by_idx],
              "baseWindows": BASE_WINDOWS, "execWindows": EXEC_WINDOWS,
              "floors": [], "cases": []}
    print(f"{args.label}: {len(names)} symbols, {len(columns)} Sets, {args.hours} decision hours", flush=True)
    args.out.parent.mkdir(exist_ok=True, parents=True)
    with ProcessPoolExecutor(max_workers=min(args.jobs, len(BASE_FLOORS)), initializer=init_floors,
                             initargs=(market, groups, columns, overlay, args.label)) as pool:
        for coverage, cases in pool.map(run_worker, BASE_FLOORS):
            result["floors"].append(coverage)
            result["cases"].extend(cases)
            temp = args.out.with_suffix(".pending")
            temp.write_text(json.dumps(result, separators=(",", ":"), allow_nan=False))
            temp.replace(args.out)
    print(f"{args.label}: finished in {time.monotonic() - began:.1f}s", flush=True)


if __name__ == "__main__":
    main()
