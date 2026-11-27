#!/usr/bin/env python3
"""Walk a compact direct post-Base study with next-open fills and directional proof."""
import argparse
from array import array
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

import numpy as np
import replay_cache
import stage_eval as sg
from execution_cost import execution_result
from fee_policy import cost_policy
from post_base_exec import MarketReplay, candidate_index, offline, BAR
from post_base_gates import BASE_FLOORS, BASE_WINDOWS, EXEC_FLOORS, EXEC_WINDOWS, PORTFOLIO, ForwardProof
from post_base_results import strategy_cases, summarize


PROOFS = (None, ForwardProof(24, 1.05), ForwardProof(36, 1.15))


@dataclass(frozen=True)
class ReplayFills:
    taker: float
    slip: float

    def open_price(self, opening, side):
        return opening * (1 + side * self.slip)

    def close(self, price, entry, side):
        exit_fill = price * (1 - side * self.slip)
        cash = execution_result(entry, exit_fill, 1.0 / entry, "LONG" if side > 0 else "SHORT",
                                self.taker, self.taker * exit_fill / entry)
        return exit_fill, cash["net_pct"]


def fill_entry(st, symbol, side, signal_bar, candles, acc, cfg, fills):
    """Admit at the next candle's opening, before testing its range for exits."""
    first = signal_bar + 1
    if first >= len(candles):
        raise ValueError("a filled entry needs a next-bar opening quote")
    entry = fills.open_price(candles[first][0], side)
    lane = sg.se.SetLane(st, symbol)
    lane.step(signal_bar, (entry, entry, entry, entry, 0), side, 1., True, cfg)
    rows = lane.scan(first, len(candles), candles, (), (), False, cfg, (), acc)
    if rows:
        done = rows[0]
        last = done["exit_i"]
        exit_fill, net = fills.close(done["exit_px"], entry, side)
        rest = sg.se.rest_after(int(cfg["cooldown"]), st.pause_bars, net > 0)
        return last + rest, last, entry, exit_fill, net, done["reason"]
    last = len(candles) - 1
    mark, net = fills.close(candles[last][3], entry, side)
    return last, last, entry, mark, net, "held"


def shadow_symbol(args):
    symbol, candles, first, fills = args
    book = sg.make_book()
    warm = min(book.warmup, max(16, len(candles) // 5))
    cfg = sg.lane_cfg(book)
    signals = sg.se.pack_signals(candles, book.packs, book.ind_settings, first + BAR, warm)
    directions = {p: array("i", (r[0] for r in rows)) for p, rows in signals.items()}
    confidence = {p: array("d", (r[1] for r in rows)) for p, rows in signals.items()}
    candidates = {
        p: [i for i in range(warm, len(candles) - 1) if sides[i] and confidence[p][i] >= cfg["entry_conf"]]
        for p, sides in directions.items()
    }
    acc = sg.se.BarIndex(candles)
    tape = {}
    for st in book.by_idx:
        entries, exits, nets = array("i"), array("i"), array("d")
        tape[st.idx] = entries, exits, nets
        if not book.symbol_allows(st.pack, symbol):
            continue
        busy = -1
        for i in candidates[st.pack]:
            side = directions[st.pack][i]
            if i <= busy or st.side not in (0, side):
                continue
            busy, last, _, _, net, reason = fill_entry(st, symbol, side, i, candles, acc, cfg, fills)
            if reason == "held":
                break
            entries.append(i)
            exits.append(last)
            nets.append(net)
    return symbol, (warm, directions, confidence, tape)


class ForwardReplay(MarketReplay):
    def __init__(self, book, names, bars, times, shadow, test_hours, fills):
        self.fills = fills
        super().__init__(book, names, bars, times, shadow, test_hours)
        self.cost = 2 * (fills.taker + fills.slip)

    def entry(self, symbol, side, set_idx, bar):
        return fill_entry(self.book.by_idx[set_idx], symbol, side, bar, self.bars[symbol],
                          self.accs[symbol], self.cfg, self.fills)


def market_filters(market):
    """At a decision, every feature ends on the already completed bar."""
    if "BTC-USDT" not in market.names:
        raise ValueError("market confirmation requires BTC-USDT candles")
    source = np.array([market.bars[s] for s in market.names])
    close = source[:, :, 3]
    at = (market.clocks - market.first) // BAR - 1
    hour = close[:, at] / close[:, at - 60] - 1.
    market_move = hour[market.names.index("BTC-USDT")]
    sides = np.array([1, -1])[None, :, None]
    local = sides * hour[:, None, :] > 0
    benchmark = np.broadcast_to(sides * market_move[None, None, :] > 0, local.shape)
    hi = np.lib.stride_tricks.sliding_window_view(source[:, :, 1], 60, axis=1).max(axis=-1)
    lo = np.lib.stride_tricks.sliding_window_view(source[:, :, 2], 60, axis=1).min(axis=-1)
    travel = hi[:, at - 59] / lo[:, at - 59] - 1.
    active = np.broadcast_to((travel >= .008)[:, None, :], local.shape)
    return {
        "all": np.ones_like(local),
        "symbol-hour": local,
        "btc-hour": benchmark,
        "breadth-and-symbol": local & benchmark,
        "active-symbol-hour": local & active,
        "hourly-range": active,
        "btc-pullback": ~local & benchmark,
    }


def frozen_cases(path):
    document = json.loads(path.read_text())
    cases = set()
    for row in document["cases"]:
        proof = None if row["proof"] is None else ForwardProof(*row["proof"])
        case = row["base"], row["final"], proof, row["scope"], row["alignment"]
        if (case in cases or row["base"] not in BASE_FLOORS
                or row["final"] not in EXEC_FLOORS or proof not in PROOFS):
            raise ValueError(f"duplicate or unsupported selection: {case}")
        cases.add(case)
    if not cases:
        raise ValueError("the frozen selection must contain at least one case")
    return cases


def study(market, label, floors=BASE_FLOORS, *, align=False, exact=False, selected=None, modifiers=None):
    groups = defaultdict(list)
    for st in market.book.by_idx:
        groups[st.pack].append(st)
    scopes = ("combined", *groups)
    filters = market_filters(market) if align else {
        "all": np.ones((len(market.names), 2, len(market.clocks)), dtype=bool)
    }
    arms = [(scope, alignment) for scope in scopes for alignment in filters]
    exact_filter = ("all", "btc-hour", "hourly-range", "btc-pullback") if align else ("all",)
    if exact:
        arms.extend((st.id, mode) for st in market.book.by_idx for mode in exact_filter)
    if selected:
        missing = {(scope, mode) for _, _, _, scope, mode in selected} - set(arms)
        if missing:
            raise ValueError(f"frozen scope or filter unavailable: {sorted(missing)}")
    result = []
    for floor in floors:
        for proof in PROOFS:
            active_arms = arms if selected is None else [
                (scope, mode) for scope, mode in arms
                if any((floor, target, proof, scope, mode) in selected for target in EXEC_FLOORS)
            ]
            if not active_arms:
                continue
            active_scopes = {scope for scope, _ in active_arms}
            exact_picks = {} if active_scopes - set(scopes) else None
            active_groups = groups
            if "combined" not in active_scopes:
                active_groups = {pack: [st for st in states if pack in active_scopes or st.id in active_scopes]
                                 for pack, states in groups.items()}
                active_groups = {pack: states for pack, states in active_groups.items() if states}
            index, _ = candidate_index(market.book, active_groups, market.columns, market.clocks, floor, label,
                                       proof, returns_are_net=True, exact_sets=exact_picks)
            if index:
                empty = np.zeros_like(next(iter(index.values()))[:, :1])
                for tag, value in index.items():
                    pack, side = tag
                    eligible = {st.id: exact_picks.get(st.idx, empty)
                                for st in active_groups[pack] if st.side in (0, side)} if exact_picks is not None else {}
                    index[tag] = np.concatenate([
                        value[:, PORTFOLIO:PORTFOLIO + 1] if scope in ("combined", pack)
                        else eligible.get(scope, empty) for scope, _ in active_arms
                    ], axis=1)
            admission = np.stack([filters[mode] for _, mode in active_arms], axis=2)
            runs = market.execute(index, admit=admission)
            for j, target in enumerate(EXEC_FLOORS):
                for i, (scope, alignment) in enumerate(active_arms):
                    if selected is not None and (floor, target, proof, scope, alignment) not in selected:
                        continue
                    case = summarize(market, runs[j][i])
                    if selected is None:
                        case.pop("paths")
                        case.pop("bySymbol")
                    if selected is None and scope not in scopes:
                        case.pop("hourly")
                    if modifiers is not None:
                        extra = strategy_cases(market, market.columns, modifiers, runs[j][i],
                                               floor, target, forward=proof, fills=market.fills)
                        for row in extra:
                            row.pop("paths")
                            row.pop("bySymbol")
                        case["axisBlock"] = extra
                    result.append(dict(base=floor, final=target, proof=proof, scope=scope, alignment=alignment, **case))
                    if selected or scope in scopes:
                        print(label, floor, target, proof, scope, alignment, case["closed"],
                              f"PF {case['pf']:.4f}" if case["pf"] is not None else "PF —", flush=True)
    return result


def load_archive(directory, names, needed):
    bars, times = {}, {}
    for name in names:
        rows = json.loads((directory / (name + ".json")).read_text())[-needed:]
        if len(rows) != needed or any(len(row) != 6 for row in rows):
            raise ValueError(f"{name} needs {needed} complete OHLCV candles")
        stamps = [row[0] for row in rows]
        if any(type(ms) is not int or ms % 60_000 for ms in stamps):
            raise ValueError(f"{name} has a timestamp off the minute boundary")
        times[name] = [ms // 1000 for ms in stamps]
        candles = [[float(x) for x in row[1:]] for row in rows]
        for opening, high, low, close, volume in candles:
            if (not all(map(math.isfinite, (opening, high, low, close, volume))) or volume < 0
                    or not 0 < low <= min(opening, close) <= max(opening, close) <= high):
                raise ValueError(f"{name} contains an invalid OHLCV candle")
        bars[name] = candles
    first = times[names[0]][0]
    complete_clock = list(range(first, first + BAR * needed, BAR))
    if any(times[name] != complete_clock for name in names):
        raise ValueError("all symbols must have a common uninterrupted minute clock")
    return bars, times


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--patch", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--cache", required=True, type=Path)
    ap.add_argument("--label", required=True)
    ap.add_argument("--symbols", type=int, default=30)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--additional-slippage-bps", type=float, default=0.)
    ap.add_argument("--market-filters", action="store_true")
    ap.add_argument("--exact-sets", action="store_true")
    ap.add_argument("--selection", type=Path, help="run only the cases in a frozen JSON selection")
    ap.add_argument("--axis-block", action="store_true", help="compare sizing on the frozen cases")
    args = ap.parse_args()
    if not (0 < args.hours <= 24 and args.jobs > 0 and args.symbols > 0
            and math.isfinite(args.additional_slippage_bps) and 0 <= args.additional_slippage_bps <= 25):
        ap.error("invalid replay dimensions or per-side slippage stress")
    patch = json.loads(args.patch.read_text())
    if not isinstance(patch, dict):
        ap.error("patch must be a JSON object")
    if args.axis_block and not args.selection:
        ap.error("--axis-block needs a frozen --selection")
    selected = frozen_cases(args.selection) if args.selection else None
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(patch)
    sys.addaudithook(offline)
    book = sg.make_book()
    if book.hold_auto or sg.replay_overlay().get("pauseMode") != "off":
        ap.error("use fixed hold and cooldown for this fill comparison")
    if (not book.stage_grid or book.stage_grid.windows(0, 0) != BASE_WINDOWS
            or not book.by_idx or any(st.side not in (1, -1) for st in book.by_idx)):
        ap.error("use the full Base grid with independent long and short Sets")
    overlay = sg.replay_overlay()
    policy = cost_policy(overlay)
    taker = policy["takerFeeRate"]
    fills = ReplayFills(taker, taker * policy["slippageFeeRatio"] + args.additional_slippage_bps / 10000.)
    names = sorted(p.stem for p in args.data.glob("*-USDT.json"))[:args.symbols]
    if len(names) != args.symbols:
        ap.error("archive has fewer symbols than requested")
    # Use at most three days and keep the same cold-start allowance on each panel.
    needed = 60 * (args.hours + 48)
    bars, times = load_archive(args.data, names, needed)
    manifest = replay_cache.fingerprint(str(args.data), names, overlay, sg.PULSE, sg.__file__)
    metadata_path = args.data / "_meta.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    supplied_hashes = metadata.get("sha256", {})
    if "sha256" in metadata and any(supplied_hashes.get(name) != manifest["markets"][name] for name in names):
        raise ValueError("historical data differs from its archive checksums")
    shadow_code = "\n".join(inspect.getsource(fn) for fn in (ReplayFills, fill_entry, shadow_symbol))
    manifest.update(fills=list((fills.taker, fills.slip)), simulation=[args.hours, needed],
                    runner=hashlib.sha256(shadow_code.encode()).hexdigest())
    cache = str(args.cache)
    if args.cache.exists():
        shadow = replay_cache.load(cache, manifest)
    else:
        todo = [(s, bars[s], times[s][0], fills) for s in names]
        shadow = {}
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            for symbol, raw in pool.map(shadow_symbol, todo):
                shadow[symbol] = raw
                if len(shadow) % 5 == 0:
                    print(args.label, len(shadow), "symbol tapes", flush=True)
        replay_cache.save(cache, shadow, manifest)
    market = ForwardReplay(book, names, bars, times, shadow, args.hours, fills)
    print(args.label, len(names), "symbols,", len(book.by_idx), "Sets", flush=True)
    cases = study(market, args.label, align=args.market_filters, exact=args.exact_sets, selected=selected,
                  modifiers=overlay if args.axis_block else None)
    record = dict(label=args.label, start=market.start, end=market.end, prehistory=market.first,
                  symbols=names, sets=len(book.by_idx), setDefinitions=[st.id for st in book.by_idx],
                  baseWindows=[w._asdict() for w in BASE_WINDOWS], execWindows=[w._asdict() for w in EXEC_WINDOWS],
                  taker=fills.taker, slippage=fills.slip,
                  source=metadata.get("source"), inputHashes=manifest["markets"], overlayHash=manifest["settings"],
                  sourceArchiveChecksumsVerified=bool(supplied_hashes), cases=cases,
                  selectionHash=hashlib.sha256(args.selection.read_bytes()).hexdigest() if args.selection else None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.out.parent, delete=False) as output:
        temp = Path(output.name)
        try:
            json.dump(record, output, allow_nan=False, separators=(",", ":"))
            output.close()
            temp.replace(args.out)
        finally:
            temp.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
