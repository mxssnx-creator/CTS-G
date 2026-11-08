#!/usr/bin/env python3
"""Detailed trade simulation, walked forward hour by hour on stored 1m history (stage_eval's tapes and execution, the
engine's own gate and ranking, checked against SetBook on samples).

Everything is counted in orders, never in hours: a Set is validated on its last N orders (the Base gate window), and what a
validation is worth is read on the orders that follow it. Executed positions are validated per order: at each entry every
Set's verdict is its last N orders at that minute (--refresh 0, the default; stage_eval.gate_until re-reads a Set only when
its window can change). The listing samples the validations every --eval-every seconds.

For every strategy type (pack x kind: base = TP step, exit = no TP, trail = trailing pair) and every range (TP step, SL
ratio, trailing pair) it reports, on the development window (the first two thirds; --sealed reads the last third once):

  sets          Sets of that group
  valid %       mean share of its Sets that pass Base at each evaluation
  eff PF        PF after Base: the last-N gate windows of the validated Sets, pooled (in-sample: what they were chosen on)
  next8 PF      the PF of each validated Set's next 8 entries after validation (MIN_VALIDATION_N), pooled
  next50 PF     the same over its next 50 entries (the gate window): does a passed last-N carry into the next N?
  exec PF       the positions the walk-forward opened under that group's Sets (one per symbol, best valid Set)
  orders, net   executed orders and their net (percent of one position's notional, after PositionCost)
  blk50 >= 1    consecutive blocks of 50 executed orders whose PF is at least 1 (stability, in orders)
  min n         the fewest gate-window orders behind any validation of the group (the last-N count; must be >= 8)

Next-entry observations can overlap between validation samples, and cohorts
near the end may contain fewer than N completed trades. They are shadow-tape
statistics, not the PF of executed portfolio positions.

usage: trade_sim.py --data DIR --cache shadow.pkl [--symbols 20] [--out FILE] [--sealed | --last-hours 12] [--patch JSON]
                    [--refresh 0] [--eval-every 600] [--trades]
"""
from __future__ import annotations

import argparse
import bisect
import collections
from array import array
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))


def pf(gp, gl):
    if gl > 0:
        return round(gp / gl, 3)
    return 99.0 if gp > 0 else None


def acc_pf(vals):
    if vals and not any(vals):
        return 1.0                      # every result nets exactly zero: neutral
    return pf(sum(v for v in vals if v > 0), -sum(v for v in vals if v < 0))


def future_entries(txa, tea, ppa, now, count, cost, symbols=None):
    """Reference next-entry cohort from an exit-ordered tape.

    Inherited positions are excluded. When supplied, stable symbol IDs break
    ties at the same entry time, without using a future close or its result.
    The production walk uses FutureEntryTapes for repeated queries.
    """
    if count <= 0:
        return []
    start = bisect.bisect_left(txa, now)
    indices = (i for i in range(start, len(ppa)) if tea[i] >= now)
    selected = sorted(indices, key=lambda i: (tea[i], symbols[i] if symbols is not None else i))[:count]
    return [ppa[i] - cost for i in selected]


class FutureEntryTapes:
    """Lazily index each Set by entry time and stable symbol, not exit time."""

    def __init__(self, names, times, shadow, cost):
        self.names, self.times, self.shadow, self.cost = sorted(names), times, shadow, cost
        self.cache = {}

    def window(self, idx, now, count):
        if count <= 0:
            return []
        if idx not in self.cache:
            rows = []
            for symbol_order, name in enumerate(self.names):
                ent, ex, pp = self.shadow[name][3][idx]
                ts = self.times[name]
                rows.extend((ts[ent[i]] + 60, symbol_order, i, pp[i]) for i in range(len(ent)))
            rows.sort(key=lambda row: (row[0], row[1], row[2]))
            self.cache[idx] = (array("d", (row[0] for row in rows)), array("d", (row[3] for row in rows)))
        starts, returns = self.cache[idx]
        start = bisect.bisect_left(starts, now)
        return [value - self.cost for value in returns[start:start + count]]


def range_of(st):
    if st.kind == "trail":
        return f"trail {st.trail_key}" + (f" ts{st.trail_sl:.2f}" if getattr(st, "trail_sl", 0) else "")
    return f"step {st.step:02d}"


def tp_range_name(st):
    """The named TP range of a base / Exit Set (Micro .. Wide), or the trailing family of a trailing Set (shipped, or ts<r> of trailSlRatios)."""
    from position_cost import tp_range_of
    if st.kind == "trail":
        return f"trail ts{st.trail_sl:.2f}" if getattr(st, "trail_sl", 0) else "trail shipped"
    return tp_range_of(st.step)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--out", default="")
    ap.add_argument("--sealed", action="store_true", help="read the sealed window (finalists only, once)")
    ap.add_argument("--patch", default="", help="overlay patch (JSON) applied to the x01 overlay")
    ap.add_argument("--refresh", type=int, default=0, help="seconds between gate evaluations of the execution; 0 = per order")
    ap.add_argument("--eval-every", type=int, default=600, help="seconds between the listing's validation samples")
    ap.add_argument("--last-hours", type=int, default=0, help="simulate only the last N hours of the data (a report window)")
    ap.add_argument("--trades", action="store_true", help="print every executed position")
    ap.add_argument("--stage-details", action="store_true", help="export each hourly last-N stage by type and TP range")
    ap.add_argument("--axis-current", action="store_true", help="apply the overlay's Axis rules to every entry, as live")
    ap.add_argument("--next", default="", help="extra next-N order windows after a validation, e.g. 15,25 (next_pf in the JSON)")
    a = ap.parse_args()
    args_refresh = max(60, min(3600, int(a.eval_every)))
    if a.patch:
        os.environ["STAGE_OVERLAY_PATCH"] = a.patch
    import stage_eval as sg  # noqa: E402  (reads STAGE_OVERLAY_PATCH)
    import replay_cache  # noqa: E402
    from position_cost import MIN_VALIDATION_N, cost_as_frac  # noqa: E402
    t0 = time.time()
    book = sg.make_book()
    # hold Auto is modelled: stage_eval.build_hold_auto takes each Set's tape from the engine's own lane with the hold chosen from earlier orders
    names = sorted(f[:-5] for f in os.listdir(a.data) if f.endswith("-USDT.json"))[:a.symbols]
    if not names:
        print("No market data found")
        return 2
    overlay = json.load(open(sg.OVERLAY, encoding="utf-8"))
    if a.patch:
        overlay.update(json.loads(a.patch))
    manifest = replay_cache.fingerprint(a.data, names, overlay, sg.PULSE, sg.__file__)
    loaded_names, bars, times = sg.load_bars(a.data, a.symbols)
    if loaded_names != names or replay_cache.fingerprint(a.data, names, overlay, sg.PULSE, sg.__file__) != manifest:
        raise ValueError("replay inputs changed while reading market data")
    if os.path.exists(a.cache):
        shadow = replay_cache.load(a.cache, manifest)
    else:
        shadow = sg.build(names, bars, times, a.jobs)
        if replay_cache.fingerprint(a.data, names, overlay, sg.PULSE, sg.__file__) != manifest:
            raise ValueError("replay inputs changed during cache generation")
        replay_cache.save(a.cache, shadow, manifest)
    shadow = sg.build_hold_auto(names, bars, times, shadow)      # hold Auto on: each Set's tape is the engine's own lane with the causally chosen hold
    held = sum(1 for k in shadow[names[0]][3] if isinstance(k, int) and k >= 0)       # Set entries only: key -1 holds the per-bar pause arrays, not a Set
    if held != len(book.by_idx):
        print(f"cache holds {held} Sets, the book {len(book.by_idx)}: rebuild the cache for this grid")
        return 2
    tapes = sg.Tapes(book, names, times, shadow)
    cf = cost_as_frac(book.cost_pct)
    future_tapes = FutureEntryTapes(names, times, shadow, cf)
    start = max(times[s][0] for s in names) + book.lookback * 60
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
    if not hours:
        raise ValueError("No complete report hour remains after prehistory")
    split = hours[len(hours) * 2 // 3]
    lo, hi = (split, hours[-1] + 3600) if a.sealed else (hours[0], split)
    if a.last_hours and not 0 < a.last_hours <= len(hours):
        raise ValueError(f"requested {a.last_hours} hours but only {len(hours)} complete report hours are available")
    if a.last_hours:
        lo, hi = hours[-a.last_hours], hours[-1] + 3600
    run_hours = [h for h in hours if lo <= h < hi]
    n_chk, bad = sg.self_check(book, tapes, run_hours, samples=60)
    print(f"symbols={len(names)} sets={len(book.by_idx)} hours={len(run_hours)} ({'last %d h' % a.last_hours if a.last_hours else 'sealed' if a.sealed else 'development'}) "
          f"{time.strftime('%m-%d %H:%M', time.gmtime(lo))}..{time.strftime('%m-%d %H:%M', time.gmtime(hi))} UTC "
          f"self-check {n_chk - bad}/{n_chk} gate+rank match the engine  [{time.time() - t0:.0f}s]", flush=True)
    if bad:
        print("the simulated gate or ranking differs from the engine: numbers below would not be the engine's")
        return 1

    groups = {}            # key -> stats
    by_set_group = {}
    for st in book.by_idx:
        keys = [("type", f"{st.pack}/{st.kind}"), ("range", f"{st.pack}/{st.kind}/{range_of(st)}"), ("all", "all"),
                ("tprange", f"{st.pack}/{st.kind}/{tp_range_name(st)}"), ("tprange_all", f"{st.kind}/{tp_range_name(st)}"),
                ("side", f"{st.pack}/{'long' if st.side > 0 else 'short' if st.side < 0 else 'both'}")]
        if st.kind != "trail":
            keys.append(("sl", f"{st.pack}/{st.kind}/sl {st.sl_ratio:.2f}"))
        by_set_group[st.idx] = keys
        for k in keys:
            g = groups.setdefault(k, collections.defaultdict(float))
            g["sets"] += 1
            g["min_n"] = 1e9
    extra_n = tuple(int(x) for x in a.next.split(",") if x.strip()) if a.next else ()
    nxt_ns = (MIN_VALIDATION_N, book.gate_window) + tuple(m for m in extra_n if m not in (MIN_VALIDATION_N, book.gate_window))
    slots = [h + k * args_refresh for h in run_hours for k in range(max(1, 3600 // args_refresh))]
    evals = []
    stage_evals = []
    auto = sg.AutoFloors(book, tapes)            # Auto floors per TP step, re-learned every hour from the orders closed before it (as the engine)
    for now in slots:
        auto.apply(book, now)
        hv = collections.defaultdict(float)
        stage_rows = collections.defaultdict(lambda: collections.defaultdict(float))
        for st in book.by_idx:
            # Effective Base PF must describe Base-passing Sets, independently
            # of whether their later Main/Real windows already pass.
            ok, key, n, gpf = sg.gate_base(book, tapes, st, now)
            keys = by_set_group[st.idx]
            for k in keys:
                groups[k]["set_evals"] += 1
            if a.stage_details:
                figures = sg.window_figures(book, tapes, st, now)
                for stage, value in figures.items():
                    if value is None:
                        continue
                    passed, gain, loss, count = value
                    for dimension, group in keys:
                        if dimension not in ("all", "type", "tprange", "side"):
                            continue
                        sr = stage_rows[(dimension, group, stage)]
                        sr["evaluated"] += 1
                        sr["passed"] += bool(passed)
                        if passed:
                            sr["gp"] += gain
                            sr["gl"] += loss
                            sr["orders"] += count
            if not ok:
                continue
            executable = bool(sg.gate_metrics(book, tapes, st, now)[0])
            hv["executable"] += executable
            tx, te, pp = tapes.window(st.idx, now, book.lookback * 60.0, book.win(st).base_long)
            gp = sum(x - cf for x in pp if x - cf > 0)
            gl = -sum(x - cf for x in pp if x - cf < 0)
            # A trade already open at validation was not admitted by that
            # decision. Select the earliest later entries rather than the
            # first closes, with stable symbol order when entries share a minute.
            future = future_tapes.window(st.idx, now, max(nxt_ns))
            nxt = {m: future[:m] for m in nxt_ns}
            for k in keys:
                g = groups[k]
                g["valid_evals"] += 1
                g["executable_evals"] += executable
                g["in_gp"] += gp
                g["in_gl"] += gl
                g["in_n"] += len(pp)
                for m, v in nxt.items():
                    g[f"n{m}_gp"] += sum(x for x in v if x > 0)
                    g[f"n{m}_gl"] += -sum(x for x in v if x < 0)
                    g[f"n{m}_n"] += len(v)
                g["min_n"] = min(g["min_n"], n)
            hv["valid"] += 1
            for m, v in nxt.items():
                hv[f"n{m}_gp"] += sum(x for x in v if x > 0)
                hv[f"n{m}_gl"] += -sum(x for x in v if x < 0)
            hv["in_gp"] += gp
            hv["in_gl"] += gl
            hv["min_n"] = n if not hv.get("min_n") else min(hv["min_n"], n)
        evals.append({"t": now, "valid": int(hv["valid"]), "valid_pct": round(100.0 * hv["valid"] / len(book.by_idx), 2),
                      "executable": int(hv["executable"]), "executable_pct": round(100.0 * hv["executable"] / len(book.by_idx), 2),
                      "eff_pf": pf(hv["in_gp"], hv["in_gl"]), "min_n": int(hv.get("min_n") or 0),
                      "next": {str(m): pf(hv[f"n{m}_gp"], hv[f"n{m}_gl"]) for m in nxt_ns}})
        for (dimension, group, stage), sr in sorted(stage_rows.items()):
            stage_evals.append({"t": now, "dimension": dimension, "group": group, "stage": stage,
                                "evaluated": int(sr["evaluated"]), "passed": int(sr["passed"]),
                                "passed_pct": round(100 * sr["passed"] / sr["evaluated"], 3),
                                "selection_pf": pf(sr["gp"], sr["gl"]), "orders": int(sr["orders"]),
                                "gain": sr["gp"], "loss": sr["gl"]})
    print(f"gate pass done [{time.time() - t0:.0f}s]", flush=True)

    coordinator = None
    if a.axis_current:
        from coord_engine import Coordinator
        coordinator = Coordinator()
        coordinator.load({}, overlay)
    rows = sg.execute(book, names, bars, times, shadow, tapes, run_hours, refresh_s=max(0, int(a.refresh)), include_open=True, axis_coord=coordinator)
    trades = [r for r in rows if "net" in r and lo <= r["t_in"] < hi]
    open_at_end = [r for r in rows if r.get("open_at_end") and lo <= r["t_in"] < hi]
    by_id = {st.id: st for st in book.by_idx}
    trades.sort(key=lambda r: r["t_in"])
    for r in trades:
        st = by_id[r["set"]]
        for k in by_set_group[st.idx]:
            g = groups[k]
            g["ex_n"] += 1
            g["ex_net"] += r["net"]
            g["ex_gp"] += max(0.0, r["net"])
            g["ex_gl"] += max(0.0, -r["net"])
            g["ex_win"] += 1 if r["net"] > 0 else 0
            g.setdefault("ex_list", []).append(r["net"])

    def blocks(vals, size=50):
        """PF of consecutive blocks of `size` executed orders (the last, shorter block is left out)."""
        return [acc_pf(vals[i:i + size]) for i in range(0, len(vals) - size + 1, size)]
    def summary(k):
        g = groups[k]
        bl = blocks(g.get("ex_list") or [])
        return {"group": k[1], "sets": int(g["sets"]),
                "valid_pct": round(100.0 * g["valid_evals"] / g["set_evals"], 2) if g["set_evals"] else 0.0,
                "executable_pct": round(100.0 * g["executable_evals"] / g["set_evals"], 2) if g["set_evals"] else 0.0,
                "eff_pf": pf(g["in_gp"], g["in_gl"]), "eff_orders": int(g["in_n"]),
                "next8_pf": pf(g[f"n{nxt_ns[0]}_gp"], g[f"n{nxt_ns[0]}_gl"]), "next8_orders": int(g[f"n{nxt_ns[0]}_n"]),
                "next50_pf": pf(g[f"n{nxt_ns[1]}_gp"], g[f"n{nxt_ns[1]}_gl"]), "next50_orders": int(g[f"n{nxt_ns[1]}_n"]),
                "exec_pf": pf(g["ex_gp"], g["ex_gl"]), "orders": int(g["ex_n"]), "net_pct": round(100 * g["ex_net"], 2),
                "win_pct": round(100.0 * g["ex_win"] / g["ex_n"], 1) if g["ex_n"] else None,
                "blocks50_pf1": f"{sum(1 for x in bl if (x or 0) >= 1.0)}/{len(bl)}",
                "blocks50_min_pf": min((x for x in bl if x is not None), default=None),
                "min_n": int(g["min_n"]) if g["min_n"] < 1e9 else None,
                "next": {str(m): {"pf": pf(g[f"n{m}_gp"], g[f"n{m}_gl"]), "orders": int(g[f"n{m}_n"])} for m in nxt_ns}}

    def hourly():
        """Per simulated hour: the validation samples of that hour (valid share, effective PF, next-N PF) and the orders the
        execution opened in it (count, net, PF, wins)."""
        out_h = []
        for h in run_hours:
            ev = [e for e in evals if h <= e["t"] < h + 3600]
            tr = [r["net"] for r in trades if h <= r["t_in"] < h + 3600]
            out_h.append({"t": h, "valid_pct": round(sum(e["valid_pct"] for e in ev) / len(ev), 2) if ev else 0.0,
                          "executable_pct": round(sum(e["executable_pct"] for e in ev) / len(ev), 2) if ev else 0.0,
                          "eff_pf": ev[0]["eff_pf"] if ev else None, "next": ev[0]["next"] if ev else {},
                          "orders": len(tr), "net_pct": round(100 * sum(tr), 3), "exec_pf": acc_pf(tr),
                          "wins": sum(1 for x in tr if x > 0)})
        return out_h

    out = {"window": f"last {a.last_hours} h" if a.last_hours else "sealed" if a.sealed else "development", "symbols": names, "sets": len(book.by_idx),
           "validation_scope": "Base only; executable_pct is the separate full Base/Main/Real gate",
           "forward_cohort_scope": "earliest entries at or after each validation, ties by symbol; completed shadow trades only; overlapping and potentially incomplete cohorts",
           "execution_axis": "overlay settings" if a.axis_current else "off (parent control)",
           "start_utc": lo, "end_utc": hi, "replay_fingerprint": manifest,
           "hours": len(run_hours), "gate": {"window": book.gate_window, "short": book.base_short, "main_n": book.main_n, "min": book.gate_min, "min_pf": book.min_pf,
                                              "max_ddt_h": book.max_ddt_h, "lookback_bars": book.lookback},
           "config": {"honor_tp_trail": book.hist_honor_tp, "exit_sets": book.exit_sets, "time_bars": book.hist_time_bars,
                      "scratch_bars": book.scratch_bars, "scratch_min_pct": round(book.scratch_min * 100, 3),
                      "cost_pct": book.cost_pct, "extra_indicators": list(book.ind_settings.get("extraIndicators") or [])},
           "all": summary(("all", "all")),
           "types": [summary(k) for k in sorted(groups) if k[0] == "type"],
           "ranges": [summary(k) for k in sorted(groups) if k[0] == "range"],
           "sl": [summary(k) for k in sorted(groups) if k[0] == "sl"],
           "tpranges": [summary(k) for k in sorted(groups) if k[0] == "tprange"],
           "tpranges_all": [summary(k) for k in sorted(groups) if k[0] == "tprange_all"],
           "sides": [summary(k) for k in sorted(groups) if k[0] == "side"],
           "evaluations": evals,
           "stage_evaluations": stage_evals,
           "hourly": hourly(),
           "open_at_end": [{**{k: r[k] for k in ("t_in", "t_mark", "symbol", "set", "pack", "kind", "lane", "side", "entry", "mark")},
                            "mark_net_pct": round(100 * r["mark_net"], 4)} for r in open_at_end],
           "closed_and_marked_pf": acc_pf([r["net"] for r in trades] + [r["mark_net"] for r in open_at_end]),
           "exits": dict(collections.Counter(r["reason"] for r in trades)),
           "trades": [{"t_in": r["t_in"], "t_out": r["t"], "symbol": r["symbol"], "set": r["set"], "pack": r["pack"],
                       "kind": r["kind"], "reason": r["reason"], "net_pct": round(100 * r["net"], 4), "lane": r.get("lane", ""),
                       "step": by_id[r["set"]].step, "sl_ratio": by_id[r["set"]].sl_ratio, "trail": by_id[r["set"]].trail_key,
                       "tp_range": tp_range_name(by_id[r["set"]]), "side": by_id[r["set"]].side} for r in trades]}
    hdr = (f"{'group':40s} {'sets':>5s} {'valid%':>7s} {'effPF':>6s} {'next8PF':>7s} {'next50PF':>8s} {'execPF':>6s} {'orders':>6s} "
           f"{'net%':>8s} {'win%':>5s} {'blk50>=1':>8s} {'minN':>4s}")

    def line(r):
        return (f"{r['group'][:40]:40s} {r['sets']:>5d} {r['valid_pct']:>7.2f} {r['eff_pf']!s:>6s} {r['next8_pf']!s:>7s} {r['next50_pf']!s:>8s} "
                f"{r['exec_pf']!s:>6s} {r['orders']:>6d} {r['net_pct']:>8.2f} {r['win_pct']!s:>5s} {r['blocks50_pf1']:>8s} {r['min_n']!s:>4s}")
    print(hdr)
    print(line(out["all"]))
    for r in out["types"]:
        print(line(r))
    print("-- ranges")
    for r in out["ranges"]:
        if r["orders"] or r["valid_pct"]:
            print(line(r))
    print("-- TP ranges (all packs)")
    for r in out["tpranges_all"]:
        print(line(r))
    print("-- exits", out["exits"])
    if a.trades:
        print("-- trades (entry UTC, symbol, Set, kind, exit reason, net %)")
        for r in out["trades"]:
            print(f"{time.strftime('%m-%d %H:%M', time.gmtime(r['t_in']))}  {r['symbol']:14s} {r['set']:38s} {r['kind']:5s} "
                  f"{r['reason']:8s} {r['net_pct']:+.4f}")
    vmin = [x["min_n"] for x in evals if x["valid"]]
    print(f"-- last-N: fewest gate-window orders behind a validation {min(vmin) if vmin else None} (gate minimum {book.gate_min})")
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        json.dump(out, open(a.out + ".json", "w"), indent=1)
    print(f"done [{time.time() - t0:.0f}s]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
