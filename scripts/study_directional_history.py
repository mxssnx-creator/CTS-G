#!/usr/bin/env python3
"""Offline study: rebuild separate Set/symbol/direction shadow lanes.

The strongest-signal-per-symbol selection and one-position-per-symbol rule are
those of trade_sim, not a complete model of live Signal-source coordination.
Input cache must be locally generated and trusted. Automatic holds must be off.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import pickle
import sys
from array import array
from collections import defaultdict


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True, help="trusted trade_sim cache made by this version")
    ap.add_argument("--result", required=True, help="trade_sim JSON; automatic holds must be off")
    ap.add_argument("--time-stop-min", type=int, help="exploratory global time exit, 8–360 minutes")
    ap.add_argument("--scratch-after-min", type=int, help="exploratory scratch delay in minutes (at least 8)")
    ap.add_argument("--scratch-min-pct", type=float, help="exploratory gross scratch threshold in percent")
    args = ap.parse_args()
    if args.time_stop_min is not None and not 8 <= args.time_stop_min <= 360:
        ap.error("--time-stop-min must be 8–360")
    if args.scratch_after_min is not None and not 8 <= args.scratch_after_min <= 360:
        ap.error("--scratch-after-min must be 8–360")
    if args.scratch_min_pct is not None and not 0 <= args.scratch_min_pct <= 1:
        ap.error("--scratch-min-pct must be 0–1 percent")
    with open(args.result, encoding="utf-8") as fh:
        sim = json.load(fh)
    patch = {"histLookbackBars": sim["gate"]["lookback_bars"], "histLookbackAuto": False, "holdEnabled": False}
    if args.time_stop_min is not None:
        patch["timeStopS"] = args.time_stop_min * 60
    if args.scratch_after_min is not None:
        patch["scratchS"] = args.scratch_after_min * 60
    if args.scratch_min_pct is not None:
        patch["scratchMinPct"] = args.scratch_min_pct
    variant = any(x is not None for x in (args.time_stop_min, args.scratch_after_min, args.scratch_min_pct))
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(patch)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import stage_eval as sg

    names, bars, times = sg.load_bars(args.data, len(sim["symbols"]))
    if names != sim["symbols"]:
        raise ValueError("data symbols differ from the simulation")
    # A cache is a pickle: accept only locally generated, trusted input.
    with open(args.cache, "rb") as fh:
        shadow = pickle.load(fh)
    book = sg.make_book()
    if (len(book.by_idx) != sim["sets"] or book.cost_pct != sim["config"]["cost_pct"]
            or book.gate_window != sim["gate"]["window"] or book.lookback != sim["gate"]["lookback_bars"]):
        raise ValueError("Set grid, gate window, lookback or cost differs from the simulation")
    pooled = sg.Tapes(book, names, times, shadow)
    cfg = sg.lane_cfg(book)
    hours = [int(row["t"]) for row in sim["evaluations"]]
    if not hours:
        raise ValueError("reference run has no evaluation window")
    hi = hours[-1] + 3600
    accs = {s: sg.se.BarIndex(bars[s]) for s in names}
    idxs = {s: {t: i for i, t in enumerate(times[s])} for s in names}
    sets_by_pack = {p: [st for st in book.by_idx if st.pack == p] for p in book.packs}
    checked_st = {next(st.idx for st in book.by_idx if st.kind == kind) for kind in ("base", "exit", "trail")}
    contexts = {}
    checked = 0

    for symbol in names:
        warm, directions, confidence, _ = shadow[symbol]
        for side in (-1, 1):
            source = sg.Tapes.__new__(sg.Tapes)
            source.cf, source.by_set = pooled.cf, {}
            dirs = {p: [d if d == side else 0 for d in directions[p]] for p in book.packs}
            cand = {p: [i for i in range(warm, len(bars[symbol]))
                        if dirs[p][i] and confidence[p][i] >= cfg["entry_conf"]] for p in book.packs}
            for st in book.by_idx:
                lane = sg.se.SetLane(st, symbol)
                trades = lane.scan(0, len(bars[symbol]), bars[symbol], dirs[st.pack], confidence[st.pack],
                                   True, cfg, cand[st.pack], accs[symbol])
                if st.idx in checked_st:
                    # Independent bar-by-bar walk checks the indexed history path.
                    scalar, plain = sg.se.SetLane(st, symbol), []
                    for i, bar in enumerate(bars[symbol]):
                        hit = scalar.step(i, bar, dirs[st.pack][i] if i >= warm else 0,
                                          confidence[st.pack][i], True, cfg)
                        if hit:
                            plain.append(hit)
                    keys = ("entry_i", "exit_i", "side", "reason", "exit_px", "pnl_pct")
                    if [[row[k] for k in keys] for row in trades] != [[row[k] for k in keys] for row in plain]:
                        raise ValueError(f"directional replay differs from scalar walk: {symbol}, {side}, {st.id}")
                    checked += 1
                ts = times[symbol]
                source.by_set[st.idx] = (array("d", (ts[r["exit_i"]] + 60 for r in trades)),
                                         array("d", (ts[r["entry_i"]] + 60 for r in trades)),
                                         array("d", (r["pnl_pct"] for r in trades)))
            contexts[(symbol, side)] = source

    def score_contexts():
        hourly = []
        for now in hours:
            base_n = all_n = 0
            gain = loss = 0.0
            for source in contexts.values():
                for st in book.by_idx:
                    if not sg.gate_base(book, source, st, now)[0]:
                        continue
                    base_n += 1
                    e = sg.base_eval(book, source, st, now)
                    gain += e["gp"]
                    loss += e["gl"]
                    if not (book.main_on or book.neg_on or book.realst_on) or sg.stage_lane(book, source, st, now):
                        all_n += 1
            hourly.append({"t": now, "base": base_n, "all_stages": all_n, "base_gain": gain, "base_loss": loss})
        den = len(contexts) * len(book.by_idx) * len(hourly)
        gain, loss = sum(x["base_gain"] for x in hourly), sum(x["base_loss"] for x in hourly)
        return {"hourly": hourly, "base_validated_pct": 100 * sum(x["base"] for x in hourly) / den,
                "all_stage_validated_pct": 100 * sum(x["all_stages"] for x in hourly) / den,
                "base_pf": gain / loss if loss else None, "denominator": den}

    def walk(scoped, allowed):
        cache, busy = {}, {s: -1 for s in names}
        closed, opened = [], []
        counts = defaultdict(int)
        for h in hours:
            for minute in range(60):
                t, now = h + minute * 60, float(h + minute * 60 + 60)
                if now >= hi:
                    continue
                for s in names:
                    i = idxs[s].get(t)
                    if i is None or i <= busy[s]:
                        continue
                    warm, dirs, confs, _ = shadow[s]
                    if i < warm:
                        continue
                    best = None
                    for pack in book.packs:
                        d, c = dirs[pack][i], confs[pack][i]
                        if d and c >= cfg["entry_conf"] and (best is None or c > best[1]):
                            best = (pack, c, d)
                    if best is None:
                        continue
                    pack, conf, side = best
                    counts["signal_minutes"] += 1
                    source = contexts[(s, side)] if scoped else pooled
                    ranked = {}
                    for st in sets_by_pack[pack]:
                        if allowed is not None and st.kind not in allowed:
                            continue
                        key = (s, side, st.idx) if scoped else (st.idx,)
                        cached = cache.get(key)
                        if cached is None or now >= cached[0]:
                            valid, rank, _, _ = sg.gate_metrics(book, source, st, now)
                            cached = cache[key] = (sg.gate_until(book, source, st, now), valid, rank)
                            counts["gate_evaluations"] += 1
                        valid, rank = cached[1:]
                        if valid:
                            lane = valid if isinstance(valid, str) else "pos"
                            ranked.setdefault((st.kind, lane), []).append((rank, st))
                    selected = []
                    for lane in ("pos", "neg"):
                        fixed = sorted(ranked.get(("base", lane), []) + ranked.get(("exit", lane), []),
                                       key=lambda r: r[0], reverse=True)
                        selected = fixed or sorted(ranked.get(("trail", lane), []), key=lambda r: r[0], reverse=True)
                        if selected:
                            break
                    if not selected:
                        continue
                    st = selected[0][1]
                    counts["eligible_entries"] += 1
                    d, c = [0] * len(bars[s]), [0.0] * len(bars[s])
                    d[i], c[i] = side, conf
                    end = bisect.bisect_left(times[s], hi)
                    trade = sg.se.SetLane(st, s).scan(i, end, bars[s], d, c, True, cfg, [i], accs[s])
                    record = {"t_in": now, "symbol": s, "set": st.id, "pack": pack, "kind": st.kind,
                              "side": "LONG" if side > 0 else "SHORT"}
                    if not trade:
                        busy[s] = end
                        entry, mark = bars[s][i][3], bars[s][end - 1][3]
                        record["mark_net_pct"] = 100 * ((mark - entry) / entry * side - pooled.cf)
                        opened.append(record)
                        continue
                    trade = trade[0]
                    busy[s] = trade["exit_i"]
                    record.update({"t_out": float(times[s][trade["exit_i"]] + 60), "reason": trade["reason"],
                                   "net_pct": 100 * (trade["pnl_pct"] - pooled.cf)})
                    closed.append(record)
        vals = [r["net_pct"] for r in closed]
        marks = [r["mark_net_pct"] for r in opened]
        gain = sum(x for x in vals if x > 0)
        loss = -sum(x for x in vals if x < 0)
        marked_gain = gain + sum(x for x in marks if x > 0)
        marked_loss = loss - sum(x for x in marks if x < 0)
        for key in ("signal_minutes", "gate_evaluations", "eligible_entries"):
            counts.setdefault(key, 0)
        return {"closed": closed, "open_at_end": opened, "closed_pf": gain / loss if loss else None,
                "marked_pf": marked_gain / marked_loss if marked_loss else None,
                "closed_net_pct_sum": sum(vals), "marked_net_pct_sum": sum(vals + marks), "counts": dict(counts)}

    baseline = None
    if not variant:
        baseline = walk(False, None)
        identity = lambda row: (row["symbol"], row["set"], row["t_in"], row["t_out"], row["reason"], round(row["net_pct"], 4))
        if sorted(map(identity, baseline["closed"])) != sorted(map(identity, sim["trades"])):
            raise ValueError("pooled baseline closes differ from the reference simulation")
    scores = score_contexts()
    trials = {name: walk(True, allowed) for name, allowed in
              (("all", None), ("base_only", {"base"}), ("exit_only", {"exit"}), ("trail_only", {"trail"}))}
    print(json.dumps({"config": {"time_bars": book.hist_time_bars, "scratch_bars": book.scratch_bars,
                                 "scratch_min_pct": 100 * book.scratch_min, "cost_pct": book.cost_pct},
                      "scalar_lane_checks": checked, "hourly_context_scores": scores,
                      "pooled_baseline": baseline, "directional_trials": trials,
                      "note": "offline strongest-pack selector, one position per symbol; equal notional, full cost applied to closing marks; not live Signal-source coordination"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
