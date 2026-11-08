#!/usr/bin/env python3
"""Axis, Block and DCA on the walk-forward executed positions, each with the engine's own rules, against the same execution without them.

The parents are trade_sim's executed positions (per-order validation, one position per symbol, the Auto floors re-learned every hour, the rest after a
close as live). On top of them, each with its own switch, exactly as the live trader applies it:

  Axis   Pulse._scope_choice: for each signal the positive lane first, then Real Negative; per lane base and Exit Sets by rank, then trailing; a Set is
         taken only when the coordinator's verdict on THAT Set's own lane stream (its Real Set or Real Negative Set, closed orders inside the lookback) at
         that Set's floor allows (axes last / prev / pause) and its open positions are under the continuation cap (cont). Changes which parents exist.
  Block  Pulse.maybe_set_block_adds + SetBlockBook: group n observes the first price n PositionCosts in the parent's favour and measures an add there to
         the parent's exit. An add of group n needs blockMinSamples paper observations of that group (newest blockWindow, inside the lookback) at a PF of
         at least the stack threshold, executed adds of the group (when there are enough) at it too, the newest blockPauseCountRatio observations not
         negative, the parent's Set still valid on its lane and its Axis allowing. One partial per group, volume blockVolumeRatio x the base volume, the
         parent's stop, target and hold unchanged. Evidence scope: "engine" (the parent's Set, lane, symbol and side; observations from executed parents,
         as SetBlockBook keys them), "set" (Set, lane, side over every symbol), "tape" / "tape-sym" (every paper order of the Set's own tape, all symbols /
         the parent's symbol: evidence that does not wait for executions).
  DCA    DcaBook auto plan: one rung at frac x the parent's stop distance (never nearer than one PositionCost, never at the stop), mult x the base volume
         (whole position at most dcaMaxExposureX), the Set keeping its own stop, target and hold; dcaOnSets adds to every Set position, dcaOnAxis only
         while the parent's Set is valid on its lane and its Axis allows; the DCA lane switches itself off on its own adds (last 15 PF under the minimum
         PF with 8 or more, or the last 25 averaging under zero R).

Every figure is volume weighted (a parent counts 1, a Block add blockVolumeRatio, a DCA add its multiple), net of one PositionCost per fill. Max drawdown
is the deepest fall of the cumulative net (in % of one base position) over exit times. The development window (the first two thirds) unless --sealed.

usage: stack_eval.py --data DIR --cache FILE --out FILE [--symbols 20] [--jobs 2] [--patch JSON] [--part axis,block,dca] [--sealed]"""
from __future__ import annotations

import argparse
import bisect
import heapq
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))


def pf(vals: Sequence[float]) -> Optional[float]:
    gp = sum(v for v in vals if v > 0)
    gl = -sum(v for v in vals if v < 0)
    return round(gp / gl, 3) if gl > 0 else (99.0 if gp > 0 else None)


def summarize(events: List[Tuple[float, float, float]], parents: int, adds: int, hours: Sequence[float]) -> Dict[str, Any]:
    """events: (exit t, net fraction, weight). PF, net and max drawdown of the weighted results; hours with a positive net."""
    vals = [n * w for _, n, w in events]
    eq = peak = mdd = 0.0
    for _t, n, w in sorted(events):
        eq += n * w
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    def bucket(t):
        # The final candle closes exactly at the report end. Keep that settled
        # fill in the last hour so hourly totals reconcile to the portfolio.
        return float(hours[-1]) if hours and t == hours[-1] + 3600 else float(int(t // 3600) * 3600)
    by_h: Dict[float, float] = {}
    for t, n, w in events:
        h = bucket(t)
        by_h[h] = by_h.get(h, 0.0) + n * w
    net = sum(vals)
    hourly = [{"t": h, "net_pct": round(100 * by_h.get(h, 0), 6),
               "fills": sum(1 for t, _, _ in events if bucket(t) == h),
               "pf": pf([n * w for t, n, w in events if bucket(t) == h])} for h in hours]
    return {"pf": pf(vals), "parents": parents, "adds": adds, "net_pct": round(100 * net, 2), "max_dd_pct": round(100 * mdd, 2),
            "hourly": hourly,
            "net_per_dd": round(net / mdd, 3) if mdd > 0 else None, "hours_positive": f"{sum(1 for v in by_h.values() if v > 0)}/{len(hours)}"}


def dca_rungs(side: int, entry: float, exit_px: float, sl_f: float, e_i: int, i_end: int, cfg: Dict[str, Any], cf: float, hit, ind_ok=None, allow=None
              ) -> List[Tuple[int, int, float, float]]:
    """The rungs one parent's DCA ladder fills: [(bar j, rung k, volume m in V0, net of the add)], pure (no state of the lane).
    Rung k sits at d_k = clamp((f0 + k x fs) x sl_f, cf, 0.9 x sl_f) against the position; it adds m_k = m0 x g^k V0, cut so the whole add volume stays under
    max_expo - 1 (a remainder under 0.05 V0 ends the ladder). `hit(i0, i1, level, direction)` is the first bar j in [i0, i1) whose high (direction > 0) / low
    (direction < 0) reaches the level, else -1; a gated rung (`ind_ok(j)` False) waits for the next bar that reaches its level; `allow(j)` False (the lane's
    own off switch at that bar) ends the ladder. Every add exits at the parent's exit price and pays one PositionCost."""
    out: List[Tuple[int, int, float, float]] = []
    used, pos_j = 0.0, e_i + 1
    cap = float(cfg["max_expo"]) - 1.0 if float(cfg["max_expo"]) > 0 else float("inf")
    for k in range(int(cfg["n"])):
        d = min(max((float(cfg["f0"]) + k * float(cfg["fs"])) * sl_f, cf), 0.9 * sl_f)
        m = float(cfg["m0"]) * (float(cfg["g"]) ** k)
        if used + m > cap + 1e-12:
            m = cap - used
            if m < 0.05:
                break
        lvl = entry * (1 - side * d)
        j = hit(pos_j, i_end, lvl, -side)
        while j >= 0 and ind_ok is not None and not ind_ok(j):
            j = hit(j + 1, i_end, lvl, -side) if j + 1 < i_end else -1
        if j < 0:
            break
        if allow is not None and not allow(j):
            break
        out.append((j, k, m, (exit_px - lvl) / lvl * side - cf))
        used += m
        pos_j = j
    return out


def admit_dca_candidates(candidates, min_pf, *, deact=True, check_average=True, allow=None, stop_parent=False):
    """Admit simulated adds using only earlier, chronologically closed adds.

    Candidate fields: now, close, net, parent_key, symbol, rung and parent.
    The parent key and symbol provide deterministic ties without future prices.
    An add closing at the candidate's exact clock is not evidence for it.
    """
    ordered = sorted(candidates, key=lambda c: (c['now'], c['symbol'], c['parent_key'], c['rung']))
    pending, closes, admitted, blocked = [], [], [], set()
    for c in ordered:
        now = c['now']
        while pending and pending[0][0] < now:
            closes.append(heapq.heappop(pending)[-1])
        key = c['parent_key']
        if key in blocked:
            continue
        last15, last25 = closes[-15:], closes[-25:]
        is_off = deact and ((len(last15) >= 8 and (pf(last15) or 0.0) + 1e-9 < min_pf) or
                            (check_average and len(last25) >= 25 and sum(last25) < 0))
        if is_off or (allow is not None and not allow(c)):
            if stop_parent:
                blocked.add(key)
            continue
        admitted.append(c)
        heapq.heappush(pending, (c['close'], c['symbol'], key, c['rung'], c['net']))
    return admitted


def dca_prior_bar_indication(ind_dir, symbol, hit_index, side, names):
    """Use the last completed bar before a simulated intrabar price hit."""
    return hit_index > 0 and ind_dir(symbol, hit_index - 1, names) == side


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--patch", default="")
    ap.add_argument("--part", default="axis,block,dca")
    ap.add_argument("--sealed", action="store_true")
    ap.add_argument("--last-hours", type=int, default=0, help="compare on the same final N full hours as trade_sim")
    ap.add_argument("--axis-arms", default="", help="JSON list of Axis arms to run instead of the built-in grid")
    ap.add_argument("--block-candidates", default="", help="part block2: JSON file, a list of {label, cfg} (cfg = Block v2 overlay keys, block_rules.clean_cfg)")
    ap.add_argument("--block-axis-current", action="store_true", help="compare Block v2 on parents admitted by the overlay's Axis rules")
    a = ap.parse_args()
    parts = {x.strip() for x in a.part.split(",") if x.strip()}      # exact part names ("block" must not match "block2")
    os.environ["STAGE_OVERLAY_PATCH"] = a.patch or "{}"
    import stage_eval as sg  # noqa: E402  (reads STAGE_OVERLAY_PATCH)
    import replay_cache  # noqa: E402
    import set_engine as se  # noqa: E402
    from coord_engine import Coordinator  # noqa: E402
    from block_engine import calculate_block_minimum_profit_factor, BLOCK_PF_RATIO_MIN, BLOCK_PF_RATIO_MAX  # noqa: E402
    from position_cost import cost_as_frac, gate_min_pf  # noqa: E402
    import numpy as np  # noqa: E402

    t0 = time.time()
    overlay = json.load(open(sg.OVERLAY, encoding="utf-8"))
    overlay.update(json.loads(a.patch or "{}"))
    names = sorted(f[:-5] for f in os.listdir(a.data) if f.endswith("-USDT.json"))[:a.symbols]
    manifest = replay_cache.fingerprint(a.data, names, overlay, sg.PULSE, sg.__file__)
    names, bars, times = sg.load_bars(a.data, a.symbols)
    if os.path.exists(a.cache):
        shadow = replay_cache.load(a.cache, manifest)
    else:
        shadow = sg.build(names, bars, times, a.jobs)
        replay_cache.save(a.cache, shadow, manifest)
    shadow = sg.build_hold_auto(names, bars, times, shadow)
    book = sg.make_book()
    tapes = sg.Tapes(book, names, times, shadow)
    cf = cost_as_frac(book.cost_pct)
    lb = book.lookback * 60.0
    start = max(times[s][0] for s in names) + book.lookback * 60
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
    if not hours:
        raise ValueError("No complete report hour remains after prehistory")
    split = hours[len(hours) * 2 // 3]
    lo, hi = (split, hours[-1] + 3600) if a.sealed else (hours[0], split)
    if a.last_hours:
        if not 0 < a.last_hours <= len(hours):
            raise ValueError(f"requested {a.last_hours} hours, available {len(hours)}")
        lo, hi = hours[-a.last_hours], hours[-1] + 3600
    run_hours = [h for h in hours if lo <= h < hi]
    by_id = {st.id: st for st in book.by_idx}
    auto = sg.AutoFloors(book, tapes)
    out: Dict[str, Any] = {"window": f"last {a.last_hours} h" if a.last_hours else "sealed" if a.sealed else "development", "data": a.data, "hours": len(run_hours), "patch": json.loads(a.patch or "{}"),
                           "axis": {}, "block": {}, "block2": {}, "dca": {}, "combined": {}}
    print(f"symbols={len(names)} Sets={len(book.by_idx)} hours={len(run_hours)} [{time.time() - t0:.0f}s]", flush=True)

    def coord_for(axes: Dict[str, Tuple[bool, int]], linked: bool = True):
        ov = dict(overlay)
        for name, (on, w) in axes.items():
            cap = name[0].upper() + name[1:]
            ov[f"axis{cap}Enabled"] = bool(on)
            ov[f"axis{cap}MaxWindow"] = int(w)
        ov["minPfLinked"] = bool(linked)
        c = Coordinator()
        c.load({}, ov)
        return c

    def run(axis_coord=None):
        rows = sg.execute(book, names, bars, times, shadow, tapes, run_hours, refresh_s=0, axis_coord=axis_coord)
        return [r for r in rows if "net" in r and lo <= r["t_in"] < hi]

    def parent_events(ps):
        return [(r["t"], r["net"], 1.0) for r in ps]

    t1 = time.time()
    base = run()
    out["baseline"] = {**summarize(parent_events(base), len(base), 0, run_hours), "by_lane": {ln: len([r for r in base if r["lane"] == ln]) for ln in ("pos", "neg")}}
    print(f"{'no Axis / Block / DCA':44s} {json.dumps(out['baseline'])}  [{time.time() - t1:.0f}s]", flush=True)

    off = {"last": (False, 4), "prev": (False, 12), "cont": (False, 8), "pause": (False, 8)}
    axis_arms: List[Tuple[str, Dict[str, Tuple[bool, int]], bool]] = [("x01 all four (prev 12, last 4, cont 8, pause 8)", {"last": (True, 4), "prev": (True, 12), "cont": (True, 8), "pause": (True, 8)}, True),
                                                                      ("x01 all four, coordinator floor (unlinked)", {"last": (True, 4), "prev": (True, 12), "cont": (True, 8), "pause": (True, 8)}, False)]
    for w in (1, 2, 4):
        axis_arms.append((f"last {w}", {**off, "last": (True, w)}, True))
    for w in (4, 8, 12):
        axis_arms.append((f"prev {w}", {**off, "prev": (True, w)}, True))
    for w in (1, 2, 4, 8):
        axis_arms.append((f"pause {w}", {**off, "pause": (True, w)}, True))
    for w in (1, 2, 8):
        axis_arms.append((f"cont {w}", {**off, "cont": (True, w)}, True))
    if a.axis_arms:
        # explicit arms: [{"label": .., "last": [on, w], "prev": .., "cont": .., "pause": .., "linked": true}, ...]
        axis_arms = [(x["label"], {k: tuple(x.get(k, off[k])) for k in off}, bool(x.get("linked", True))) for x in json.loads(a.axis_arms)]
    parents_by_axis: Dict[str, List[Dict[str, Any]]] = {"none": base}
    coords: Dict[str, Any] = {}
    if "axis" in parts:
        for label, axes, linked in axis_arms:
            t1 = time.time()
            c = coord_for(axes, linked)
            ps = run(c)
            parents_by_axis[label] = ps
            coords[label] = c
            out["axis"][label] = {**summarize(parent_events(ps), len(ps), 0, run_hours),
                                  "by_lane": {ln: len([r for r in ps if r["lane"] == ln]) for ln in ("pos", "neg")}}
            print(f"{'Axis ' + label:44s} {json.dumps(out['axis'][label])}  [{time.time() - t1:.0f}s]", flush=True)
            json.dump(out, open(a.out, "w"), indent=1)

    # ---- Axis Sets: each Axis setting a derived Set of the parent's Real (or Real Negative) Set: the orders that setting admitted, each judged on the
    # orders closed before its entry; per parent the best setting is chosen from closed orders only; an entry goes only where the chosen setting admits it
    AX_CONFIGS = [("last", w) for w in (1, 2, 3, 4)] + [("prev", w) for w in (4, 6, 8, 12)] + [("pause", w) for w in (1, 2, 3, 4, 6, 8)]

    def rule_ok(kind: str, w: int, nets: List[float], floor: float) -> bool:
        n = len(nets)
        if kind == "last":
            if n < min(3, w):
                return True
            return (pf(nets[-w:]) or 0.0) + 1e-9 >= floor
        if kind == "prev":
            pw = min(25, 2 * w)
            if n < 8 or len(nets[-pw:]) < 8:
                return True
            lim = 0.85 * floor
            return not ((pf(nets[-pw:]) or 0.0) + 1e-9 < lim and (pf(nets[-50:]) or 0.0) + 1e-9 < lim)
        k = 0
        for x in reversed(nets):
            if x < 0:
                k += 1
            else:
                break
        return k < w

    ax_cache: Dict[Tuple[int, str, float], Any] = {}

    def axis_set_pick(cfg):
        def pick(st, lane, now):
            key = (st.idx, lane, now)
            if key not in ax_cache:
                auto.apply(book, float(int(now // 3600) * 3600))
                floor = float(book.floor_for(book.win(st).real_n, st.pack, st))
                rows = sg.lane_stream_entries(book, tapes, st, now, lane)
                exits = [r[0] for r in rows]
                nets = [r[2] for r in rows]
                best = (None, pf(nets[-cfg["n"]:]) or 0.0, len(nets))
                base_pf = best[1]
                for kind, w in AX_CONFIGS:
                    adm = [nets[j] for j in range(len(rows)) if rule_ok(kind, w, nets[:bisect.bisect_left(exits, rows[j][1])], floor)]
                    if len(adm) < cfg["min_n"]:
                        continue
                    p_ = pf(adm[-cfg["n"]:]) or 0.0
                    if p_ > best[1] and p_ >= base_pf * cfg["margin"]:
                        best = ((kind, w), p_, len(adm))
                chosen, p_c, _n = best
                allow = True if chosen is None else rule_ok(chosen[0], chosen[1], nets, floor)
                if cfg.get("strict") and p_c + 1e-9 < floor:
                    allow = False
                vol = 1.0
                if cfg.get("vol") == "pf":
                    vol = max(0.5, min(float(cfg.get("vmax", 2.0)), p_c / floor)) if floor > 0 else 1.0
                ax_cache[key] = (allow, vol, chosen)
            a_, v_, _c = ax_cache[key]
            return a_, v_
        return pick

    if "axissets" in parts:
        arms_as = [("choose per Set (14 settings), flat volume", {"n": 25, "min_n": 8, "margin": 1.05}),
                   ("choose per Set, the Axis Set must reach its floor", {"n": 25, "min_n": 8, "margin": 1.05, "strict": True}),
                   ("choose per Set, volume by Axis-Set PF", {"n": 25, "min_n": 8, "margin": 1.05, "vol": "pf"}),
                   ("choose per Set, floor + volume by PF", {"n": 25, "min_n": 8, "margin": 1.05, "strict": True, "vol": "pf"}),
                   ("choose per Set, 15 orders, 5 minimum", {"n": 15, "min_n": 5, "margin": 1.05}),
                   ("choose per Set, floor + volume by PF up to 3x", {"n": 25, "min_n": 8, "margin": 1.05, "strict": True, "vol": "pf", "vmax": 3.0}),
                   ("choose per Set, volume by PF up to 3x", {"n": 25, "min_n": 8, "margin": 1.05, "vol": "pf", "vmax": 3.0})]
        out["axissets"] = {}
        for label, cfg in arms_as:
            t1 = time.time()
            ax_cache.clear()
            ps = sg.execute(book, names, bars, times, shadow, tapes, run_hours, refresh_s=0, axis_pick=axis_set_pick(cfg))
            ps = [r for r in ps if "net" in r and lo <= r["t_in"] < hi]
            ev = [(r["t"], r["net"], float(r.get("vol", 1.0))) for r in ps]
            out["axissets"][label] = {**summarize(ev, len(ps), 0, run_hours), "by_lane": {ln: len([r for r in ps if r["lane"] == ln]) for ln in ("pos", "neg")},
                                      "chosen": {str(k): v for k, v in __import__("collections").Counter(str(c[2]) for c in ax_cache.values()).most_common(8)}}
            print(f"{'Axis Sets ' + label:58s} {json.dumps({k: v for k, v in out['axissets'][label].items() if k != 'chosen'})}  [{time.time() - t1:.0f}s]", flush=True)
            json.dump(out, open(a.out, "w"), indent=1)

    # ---- helpers shared by Block and DCA: the parent's Set still valid on its lane, its Axis allowing, at a moment inside the parent ------------------
    allow_coord = coord_for({k: (True, v[1]) for k, v in {"last": (True, 4), "prev": (True, 12), "cont": (True, 8), "pause": (True, 8)}.items()}, True)
    no_axis_coord = coord_for(off, True)
    valid_cache: Dict[Tuple[int, float], str] = {}

    def set_lane_at(st, now: float) -> str:
        k = (st.idx, now)
        if k not in valid_cache:
            auto.apply(book, float(int(now // 3600) * 3600))
            ok, _key, _n, _pf = sg.gate_metrics(book, tapes, st, now)
            valid_cache[k] = ok if isinstance(ok, str) else ("pos" if ok else "")
        return valid_cache[k]

    def parent_ok(r, now: float, coord) -> bool:
        st = by_id[r["set"]]
        if set_lane_at(st, now) != r["lane"]:
            return False
        if coord is None:
            return True
        return sg.axis_judge(book, tapes, coord, st, now, r["lane"])[0]

    arrays = {s: (np.array([b[1] for b in bars[s]]), np.array([b[2] for b in bars[s]])) for s in names}

    def first_hit(s: str, i0: int, i1: int, level: float, side: int) -> int:
        """First bar index j with i0 <= j < i1 whose high (long) / low (short) reaches level; -1 if none."""
        if i1 <= i0:
            return -1
        hi_, lo_ = arrays[s]
        seg = hi_[i0:i1] >= level if side > 0 else lo_[i0:i1] <= level
        k = int(np.argmax(seg)) if seg.size else 0
        return i0 + k if seg.size and seg[k] else -1

    # ---- Block ---------------------------------------------------------------------------------------------------------------------------------------
    tape_obs: Dict[Tuple[int, str], List[Tuple[float, int, float]]] = {}

    def tape_observations(st, s: str, max_stack: int) -> List[Tuple[float, int, float]]:
        """(exit t, group, gross) of every paper order of the Set on symbol s: group n reached before the exit bar, measured to the order's exit."""
        key = (st.idx, s)
        if key in tape_obs:
            return tape_obs[key]
        ent, ex, pp = shadow[s][3][st.idx]
        side = int(st.side)
        rows: List[Tuple[float, int, float]] = []
        if side:
            hi_, lo_ = arrays[s]
            for e_i, x_i, raw in zip(ent, ex, pp):
                entry = float(bars[s][e_i][3])
                exit_px = entry * (1 + raw * side)
                if x_i - e_i <= 1:
                    continue
                fav = (hi_[e_i + 1:x_i].max() / entry - 1) if side > 0 else (1 - lo_[e_i + 1:x_i].min() / entry)
                for n in range(1, max_stack + 1):
                    if fav + 1e-12 < n * cf:
                        break
                    lvl = entry * (1 + side * n * cf)
                    rows.append((float(times[s][x_i] + 60), n, (exit_px / lvl - 1) * side))
        rows.sort()
        tape_obs[key] = rows
        return rows

    def block_sim(parents, cfg, coord) -> Tuple[List[Tuple[float, float, float]], int, List[float]]:
        """Block adds on top of the parents. Returns (events, adds, the adds' own net results)."""
        max_stack, vol, pf_ratio, min_n, window, pause_n, scope = (cfg["max_stack"], cfg["vol"], cfg["pf_ratio"], cfg["min_n"], cfg["window"], cfg["pause"],
                                                                     cfg["scope"])
        paper: Dict[Tuple, List[Tuple[float, int, float]]] = {}       # scope key -> (t, group, gross) of executed parents' observations
        done: Dict[Tuple, List[Tuple[float, int, float]]] = {}        # scope key -> (t, group, net) of executed adds
        events = parent_events(parents)
        add_nets: List[float] = []
        adds = 0
        for r in sorted(parents, key=lambda x: x["t_in"]):
            st = by_id[r["set"]]
            tr = r["tr"]
            s, side = r["symbol"], int(tr["dir"])
            entry, exit_px = float(tr["entry_px"]), float(tr["exit_px"])
            e_i, x_i = int(tr["entry_i"]), int(tr["exit_i"])
            if scope == "engine":
                key = (st.id, r["lane"], s, side)
            else:
                key = (st.id, r["lane"], side) if scope in ("set", "tape") else (st.id, r["lane"], s, side)
            floor = None
            filled = 0
            obs_here: List[Tuple[float, int, float]] = []
            for n in range(1, max_stack + 1):
                lvl = entry * (1 + side * n * cf)
                j = first_hit(s, e_i + 1, x_i, lvl, side)
                if j < 0:
                    break
                now = float(times[s][j])
                obs_here.append((float(r["t"]), n, (exit_px / lvl - 1) * side))
                if scope in ("tape", "tape-sym"):
                    syms = names if scope == "tape" else [s]
                    src = [o for sy in syms for o in tape_observations(st, sy, max_stack) if o[1] == n and now - lb <= o[0] <= now]
                    src.sort()
                else:
                    src = [o for o in paper.get(key, ()) if o[1] == n and now - lb <= o[0] <= now]
                src = src[-window:]
                vals = [g - cf for _t, _n, g in src]
                if floor is None:
                    auto.apply(book, float(int(now // 3600) * 3600))
                    floor = float(book.floor_for(book.win(st).real_n, st.pack, st))
                thr = max(floor, calculate_block_minimum_profit_factor(floor, max(BLOCK_PF_RATIO_MIN, min(BLOCK_PF_RATIO_MAX, pf_ratio)), (filled + 1) * vol))
                if len(vals) < min_n or (pf(vals) or 0.0) + 1e-9 < thr:
                    continue
                ex_vals = [v for t, g, v in done.get(key, ()) if g == n and now - lb <= t <= now][-window:]
                if len(ex_vals) >= min_n and (pf(ex_vals) or 0.0) + 1e-9 < thr:
                    continue
                if pause_n and any(v < 0 for v in vals[-pause_n:]):
                    continue
                if not parent_ok(r, now, coord):
                    continue
                net = (exit_px - lvl) / lvl * side - cf
                events.append((float(r["t"]), net, vol))
                add_nets.append(net)
                done.setdefault(key, []).append((float(r["t"]), n, net))
                filled += 1
                adds += 1
            paper.setdefault(key, []).extend(obs_here)
        return events, adds, add_nets

    if "block" in parts:
        base_cfg = {"max_stack": 6, "vol": 1.0, "pf_ratio": 0.8, "min_n": 8, "window": 25, "pause": 1, "scope": "engine"}
        arms = [("engine default (6 groups, ratio 1, 8 samples, pause 1)", {})]
        for scope in ("set", "tape-sym", "tape"):
            arms.append((f"scope {scope}", {"scope": scope}))
            for mn in (3, 5, 12):
                arms.append((f"scope {scope}, {mn} samples", {"scope": scope, "min_n": mn}))
            for ms in (1, 2, 3):
                arms.append((f"scope {scope}, {ms} groups", {"scope": scope, "max_stack": ms}))
            arms.append((f"scope {scope}, no pause", {"scope": scope, "pause": 0}))
            arms.append((f"scope {scope}, pause 2", {"scope": scope, "pause": 2}))
            arms.append((f"scope {scope}, volume 0.5", {"scope": scope, "vol": 0.5}))
            arms.append((f"scope {scope}, PF ratio 2", {"scope": scope, "pf_ratio": 2.0}))
            arms.append((f"scope {scope}, window 10", {"scope": scope, "window": 10}))
        for label, ch in arms:
            t1 = time.time()
            cfg = {**base_cfg, **ch}
            ev, n_add, nets = block_sim(base, cfg, no_axis_coord)
            out["block"][label] = {**summarize(ev, len(base), n_add, run_hours), "adds_pf": pf(nets), "adds_net_pct": round(100 * sum(nets) * cfg["vol"], 2), "cfg": cfg}
            print(f"{'Block ' + label:44s} {json.dumps({k: v for k, v in out['block'][label].items() if k != 'cfg'})}  [{time.time() - t1:.0f}s]", flush=True)
            json.dump(out, open(a.out, "w"), indent=1)

    # ---- Block v2 (block_rules): per-count steps and ratios, cumulative evidence, relations, Additive / Shared, lifecycle ------------------------------
    floor_memo: Dict[Tuple[int, float], float] = {}

    def block_sim2(parents, cfg, coordinator):
        """The engine's rules (block_rules, the same module SetBlockBook runs) over the executed parents, in time order: a parent's close (its observations
        enter the ledgers, its lifecycle updates) comes before an open at the same moment, an open before a decision. Returns (events, adds, add nets)."""
        import heapq
        import block_rules as br
        levels = cfg.levels()
        ps = sorted(parents, key=lambda x: x["t_in"])
        ev: List[Tuple[float, int, int, int]] = []          # (t, order 0 close / 1 open / 2 decide, parent index, count)
        meta: List[Dict[str, Any]] = []
        for i, r in enumerate(ps):
            st = by_id[r["set"]]
            tr = r["tr"]
            s_, side = r["symbol"], int(tr["dir"])
            entry, exit_px = float(tr["entry_px"]), float(tr["exit_px"])
            e_i, x_i = int(tr["entry_i"]), int(tr["exit_i"])
            hits = []
            for n, lv in enumerate(levels, start=1):
                lvl = entry * (1 + side * lv * cf)
                j = first_hit(s_, e_i + 1, x_i, lvl, side)
                if j < 0:
                    break
                hits.append((n, float(times[s_][j]), lvl))
            rk = br.relation_keys(cfg, st.id, r["lane"], "LONG" if side > 0 else "SHORT", s_, st.pack, int(st.step))
            meta.append({"r": r, "st": st, "side": side, "entry": entry, "exit": exit_px, "hits": hits, "rk": rk, "fired": {}, "scale": 1.0})
            ev.append((float(r["t"]), 0, i, 0))
            ev.append((float(r["t_in"]), 1, i, 0))
            for n, now_t, _lvl in hits:
                ev.append((now_t, 2, i, n))
        ev.sort()
        ledgers: Dict[str, Dict[str, Any]] = {}

        def led(k):
            return ledgers.setdefault(k, {"paper": {}, "executed": {}, "life": br.new_life()})
        events: List[Tuple[float, float, float]] = []
        add_nets: List[float] = []
        adds = 0
        for t, order, i, n in ev:
            m = meta[i]
            r = m["r"]
            if order == 1:
                if cfg.carry:
                    m["scale"] = max([br.carried_scale(cfg, led(k)["life"]) for k in m["rk"]] or [1.0])
                continue
            if order == 2:
                if not cfg.active(n):
                    continue
                books = [led(k) for k in m["rk"]]
                if any(int(b["life"].get("pause", 0)) > 0 for b in books):
                    continue
                if not parent_ok(r, t, coordinator):
                    continue
                hr = float(int(t // 3600) * 3600)
                fk = (m["st"].idx, hr)
                if fk not in floor_memo:           # the Auto floor of a Set at an hour never changes between candidates: learn it once
                    auto.apply(book, hr)
                    floor_memo[fk] = float(book.floor_for(book.win(m["st"]).real_n, m["st"].pack, m["st"]))
                floor = floor_memo[fk]
                fired_ratio = sum(cfg.target_ratio(k) for k in m["fired"])
                if not br.count_passes(cfg, n, [b["paper"].get(str(n)) for b in books], [b["executed"].get(str(n)) for b in books], floor, fired_ratio):
                    continue
                lvl = m["hits"][n - 1][2]
                net = (m["exit"] - lvl) / lvl * m["side"] - cf
                m["fired"][n] = net
                continue
            # close
            add_part = 0.0
            for n, net in m["fired"].items():
                ratio = cfg.target_ratio(n)
                events.append((float(r["t"]), net, ratio * m["scale"]))
                add_nets.append(net * ratio * m["scale"])
                adds += 1
                add_part += ratio * net
            events.append((float(r["t"]), float(r["net"]), m["scale"]))
            parent_net = float(r["net"]) + add_part
            highest = max(m["fired"]) if m["fired"] else 0
            for k in m["rk"]:
                b = led(k)
                for hn, _now, lvl in m["hits"]:
                    br.add_obs(b["paper"].setdefault(str(hn), br.new_record()), (m["exit"] / lvl - 1) * m["side"] - cf)
                for hn, net in m["fired"].items():
                    br.add_obs(b["executed"].setdefault(str(hn), br.new_record()), net)
                b["life"] = br.life_after_close(cfg, b["life"], parent_net, highest)
        return events, adds, add_nets

    if "block2" in parts:
        import block_rules as br
        cands = json.load(open(a.block_candidates)) if a.block_candidates else [{"label": "default", "cfg": {}}]
        block_coord, block_parents = no_axis_coord, base
        if a.block_axis_current:
            block_coord = Coordinator()
            block_coord.load({}, overlay)
            block_parents = run(block_coord)
        out["block2"] = {"axis": "overlay" if a.block_axis_current else "off",
                         "baseline": summarize(parent_events(block_parents), len(block_parents), 0, run_hours), "results": {}}
        t1 = time.time()
        for k, c in enumerate(cands):
            cfg = br.clean_cfg(c.get("cfg") or {})
            ev, n_add, nets = block_sim2(block_parents, cfg, block_coord)
            res = summarize(ev, len(block_parents), n_add, run_hours)
            res["adds_pf"] = pf(nets)
            res["adds_net_pct"] = round(100 * sum(nets), 2)
            res["cfg"] = c.get("cfg") or {}
            out["block2"]["results"][c["label"]] = res
            if k % 500 == 499:
                print(f"block2 {k + 1}/{len(cands)} [{time.time() - t1:.0f}s]", flush=True)
        json.dump(out, open(a.out, "w"), indent=1)
        print(f"block2 done {len(cands)} candidates [{time.time() - t1:.0f}s]; baseline {json.dumps(out['block2']['baseline'])}", flush=True)

    # ---- DCA -----------------------------------------------------------------------------------------------------------------------------------------
    dca_min_pf = gate_min_pf(overlay, "dcaMinPf", "minPf")

    def dca_sim(parents, cfg, coord) -> Tuple[List[Tuple[float, float, float]], int, List[float]]:
        frac, mult, gate, deact = cfg["frac"], min(cfg["mult"], cfg["max_expo"] - 1.0), cfg["gate"], cfg["deact"]
        events = parent_events(parents)
        candidates = []
        ordered_parents = sorted(parents, key=lambda x: (x['t_in'], x['symbol'], x['set']))
        for parent_no, r in enumerate(ordered_parents):
            tr = r["tr"]
            s, side = r["symbol"], int(tr["dir"])
            entry, exit_px = float(tr["entry_px"]), float(tr["exit_px"])
            e_i, x_i = int(tr["entry_i"]), int(tr["exit_i"])
            sl_f = float(r["fracs"][0])
            d = min(max(frac * sl_f, cf), 0.9 * sl_f)
            lvl = entry * (1 - side * d)
            j = first_hit(s, e_i + 1, x_i + 1, lvl, -side)
            if j < 0:
                continue
            now = float(times[s][j])
            net = (exit_px - lvl) / lvl * side - cf
            candidates.append({'now': now, 'close': float(r['t']), 'net': net, 'rung': 0,
                               'symbol': s, 'parent_key': parent_no, 'parent': r})
        admitted = admit_dca_candidates(candidates, dca_min_pf, deact=deact,
                   allow=(lambda c: parent_ok(c['parent'], c['now'], coord)) if gate == 'axis' else None)
        events.extend((c['close'], c['net'], mult) for c in admitted)
        return events, len(admitted), [c['net'] for c in admitted]

    if "dca" in parts:
        base_dca = {"frac": 0.4, "mult": 0.5, "max_expo": 2.0, "gate": "sets", "deact": True}
        arms = [("auto plan (0.4 x stop, 0.5 x, on Sets, self-off)", {})]
        for fr in (0.25, 0.6, 0.8):
            arms.append((f"rung {fr} x stop", {"frac": fr}))
        for m in (0.25, 1.0):
            arms.append((f"multiple {m}", {"mult": m}))
        arms.append(("no self-off", {"deact": False}))
        arms.append(("on Axis (x01 axes)", {"gate": "axis"}))
        arms.append(("on Axis, rung 0.6", {"gate": "axis", "frac": 0.6}))
        for label, ch in arms:
            t1 = time.time()
            cfg = {**base_dca, **ch}
            ev, n_add, nets = dca_sim(base, cfg, allow_coord)
            out["dca"][label] = {**summarize(ev, len(base), n_add, run_hours), "adds_pf": pf(nets), "cfg": cfg}
            print(f"{'DCA ' + label:44s} {json.dumps({k: v for k, v in out['dca'][label].items() if k != 'cfg'})}  [{time.time() - t1:.0f}s]", flush=True)
            json.dump(out, open(a.out, "w"), indent=1)
    # ---- DCA 12: up to twelve rungs of its own, per rung distance and volume, optionally gated by its own reversal indications ------------------------
    # Owner 2026-11-04 ("12 step DCA ... own indication type ... lower and higher distances, ratios ... moderate drawdowns"). A rung k sits at a fraction
    # d_k of the parent's own stop distance below (above) the entry, adds m_k x V0, and fills at the first bar that reaches it (strictly before the parent's
    # exit bar on a winner, on the exit bar too on a loser: the pessimistic order). The parent keeps its stop, target and hold at the original entry, so every
    # rung exits at the parent's exit price and pays one PositionCost. The whole add volume stays under max_expo - 1. A rung with an indication gate fills
    # only at a bar where one of its reversal indications (library names on the 1m series) points the position's way; a rung that cannot fill there stays
    # open for the next qualifying bar of the parent's life. The lane switches itself off when its own last 15 adds read under the minimum PF (as dca_sim).
    if "dca12" in parts:
        if os.environ.get("IND_LIB_PATH"):          # a newer library than the one the replay cache was built with (the gate reads only its own names)
            import importlib.util as _iu
            _sp = _iu.spec_from_file_location("indication_lib_gate", os.environ["IND_LIB_PATH"])
            IL = _iu.module_from_spec(_sp)
            _sp.loader.exec_module(IL)
        else:
            import indication_lib as IL
        ind_cache: Dict[Tuple[str, int, str], int] = {}

        def ind_dir(s: str, j: int, names_: Sequence[str]) -> int:
            """Net direction of the named indications on the completed bar j of symbol s: +1 / -1 when any fires and none disagrees, else 0."""
            up = dn = 0
            for nm in names_:
                k = (s, j, nm)
                if k not in ind_cache:
                    rows = bars[s][max(0, j - 380): j + 1]
                    sig = IL.LIB[nm]([[float(x) for x in b[:5]] for b in rows])
                    ind_cache[k] = int(sig[0]) if sig else 0
                v = ind_cache[k]
                up += v > 0
                dn += v < 0
            return 1 if up and not dn else (-1 if dn and not up else 0)

        def dca12_sim(parents, cfg):
            gate = cfg.get("ind") or []
            events = parent_events(parents)
            candidates = []
            ordered_parents = sorted(parents, key=lambda x: (x['t_in'], x['symbol'], x['set']))
            for parent_no, r in enumerate(ordered_parents):
                tr = r["tr"]
                s, side = r["symbol"], int(tr["dir"])
                e_i, x_i = int(tr["entry_i"]), int(tr["exit_i"])
                i_end = x_i + 1 if r["net"] < 0 else x_i

                fills = dca_rungs(side, float(tr["entry_px"]), float(tr["exit_px"]), float(r["fracs"][0]), e_i, i_end, cfg, cf,
                                  lambda i0, i1, lvl, d, s=s: first_hit(s, i0, i1, lvl, d),
                                  (lambda j, s=s, side=side: dca_prior_bar_indication(ind_dir, s, j, side, gate)) if gate else None)
                for j, k, m, net in fills:
                    candidates.append({'now': float(times[s][j]), 'close': float(r['t']), 'net': net,
                                       'rung': k, 'weight': m, 'symbol': s, 'parent_key': parent_no, 'parent': r})
            admitted = admit_dca_candidates(candidates, dca_min_pf, check_average=False, stop_parent=True)
            per_rung = [0] * cfg['n']
            for c in admitted:
                events.append((c['close'], c['net'], c['weight']))
                per_rung[c['rung']] += 1
            return events, len(admitted), [c['net'] for c in admitted], per_rung

        IND_REV = ["sweep_n8_t1", "wickrev_n8_t1", "exhaust_n8_t1", "volclimax_n8_t1"]
        IND_RSI = ["rsirev_n8_t1", "stochrsi_n8_t1"]
        IND_ALL = IND_REV + IND_RSI + ["zrev_n21_t1", "ibs_n8_t1"]
        base12 = {"n": 12, "f0": 0.1, "fs": 0.07, "m0": 0.2, "g": 1.0, "max_expo": 3.0, "ind": []}
        arms12: List[Tuple[str, Dict[str, Any]]] = []
        for n_r in (1, 3, 6, 12):
            for (f0, fs) in ((0.1, 0.05), (0.2, 0.1), (0.05, 0.075), (0.3, 0.05)):
                for m0 in (0.1, 0.25, 0.5):
                    if n_r == 1 and fs != 0.05:
                        continue
                    arms12.append((f"n{n_r} f{f0}+{fs} m{m0}", {"n": n_r, "f0": f0, "fs": fs, "m0": m0}))
        for g in (1.15, 1.3):
            arms12.append((f"n12 f0.05+0.075 m0.15 g{g}", {"n": 12, "f0": 0.05, "fs": 0.075, "m0": 0.15, "g": g, "max_expo": 4.0}))
        for nm, lst in (("rev", IND_REV), ("rsi", IND_RSI), ("all", IND_ALL)):
            for n_r in (3, 6, 12):
                for (f0, fs) in ((0.1, 0.05), (0.05, 0.075), (0.2, 0.1)):
                    for m0 in (0.25, 0.5):
                        arms12.append((f"IND-{nm} n{n_r} f{f0}+{fs} m{m0}", {"n": n_r, "f0": f0, "fs": fs, "m0": m0, "ind": lst}))
        out["dca12"] = {}
        for label, ch in arms12:
            t1 = time.time()
            cfg = {**base12, **ch}
            ev, n_add, nets, per_rung = dca12_sim(base, cfg)
            out["dca12"][label] = {**summarize(ev, len(base), n_add, run_hours), "adds_pf": pf(nets), "per_rung": per_rung, "cfg": cfg}
            print(f"{'DCA12 ' + label:48s} {json.dumps({k: v for k, v in out['dca12'][label].items() if k != 'cfg'})}  [{time.time() - t1:.0f}s]", flush=True)
            json.dump(out, open(a.out, "w"), indent=1)
    json.dump(out, open(a.out, "w"), indent=1)
    print(f"done [{time.time() - t0:.0f}s]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
