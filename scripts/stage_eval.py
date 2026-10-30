#!/usr/bin/env python3
"""Stage evaluation on stored 1m history, walked forward like the live engine: PF after Base, after the coordinator
axes, and with Block legs.

1. Shadow tapes. Every Set runs its own lane over every symbol (SetLane.scan, the replay's code), always on. These tapes
   are what Base validation reads.
2. Base, hour by hour. At the start of each hour every Set is scored from its shadow trades that closed in the lookback
   before that hour (gate window, minimum orders, PF over the window, drawdown time: the engine's rule, checked against
   SetBook._score_one / gate_reason on samples). No look-ahead.
3. Execution, like live. One position per symbol. On a signal bar the pack with the higher confidence decides; the
   position opens under the best valid Set of that pack (SetBook.valid_for order, base before trail) and closes by that
   Set's own lane from that bar (a fresh SetLane). The symbol is busy until the exit.
4. Axis (optional). Before an entry, the coordinator's gate on the executed closes so far: PF of the last N closes at or
   above a minimum, and a pause after K consecutive losses. A Set-level variant asks the chosen Set's own last N shadow
   closes for a minimum PF instead.
5. Block (optional). On an executed position, leg k (k = 1..max stack) is added at entry + k x spacing steps in its
   favour, the first bar that reaches it before the exit bar, one base size each; every leg leaves at the position's exit.

Numbers: PF is net of PositionCost (the sum of net fractions of equal-size positions; Block legs add their size). Max
drawdown is in percent of one position's notional. The development window is the first two thirds of the hours after
the burn-in, the sealed window the last third: choose on dev, read sealed once.

usage: stage_eval.py --data DIR [--symbols 20] [--out stage.json] [--cache cache.pkl]
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import pickle
import sys
import time
from array import array
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PULSE = os.path.join(HERE, "..", "server", "pulse")
sys.path.insert(0, PULSE)
import set_engine as se  # noqa: E402
from position_cost import cost_as_frac, signed_result_r  # noqa: E402

OVERLAY = os.path.join(PULSE, "overlay-bingx-x01.json")


def load_bars(data_dir: str, n_symbols: int) -> Tuple[List[str], Dict[str, List[List[float]]], Dict[str, List[int]]]:
    names = sorted(f[:-5] for f in os.listdir(data_dir) if f.endswith("-USDT.json"))[:n_symbols]
    bars, times = {}, {}
    for s in names:
        rows = json.load(open(os.path.join(data_dir, s + ".json")))
        rows.sort(key=lambda r: r[0])
        times[s] = [int(r[0]) // 1000 for r in rows]
        bars[s] = [[float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in rows]
    return names, bars, times


def make_book() -> se.SetBook:
    b = se.SetBook()
    ov = json.load(open(OVERLAY))
    patch = os.environ.get("STAGE_OVERLAY_PATCH")      # e.g. {"extraIndicators": ["trend_c10"]}: a variant under test
    if patch:
        ov.update(json.loads(patch))
    b.load(ov)
    return b


def lane_cfg(b: se.SetBook) -> Dict[str, Any]:
    return {"entry_conf": b.entry_conf, "time_bars": b.hist_time_bars, "scratch_bars": b.scratch_bars,
            "scratch_min": b.scratch_min, "cooldown": b.cooldown_bars, "cost_pct": b.cost_pct,
            "honor_tp": bool(getattr(b, "hist_honor_tp", True)), "sl_floor_steps": b.sl_min_steps}


# ---- phase 1: shadow tapes and signals, one worker per symbol ---------------------------------------------------------

def _symbol_job(args):
    s, bars, t0 = args
    b = make_book()
    warm = min(b.warmup, max(16, len(bars) // 5))
    sig = se.pack_signals(bars, b.packs, b.ind_settings, float(t0) + 60.0, warm)
    dirs = {p: [x[0] for x in sig[p]] for p in b.packs}
    confs = {p: [float(x[1]) for x in sig[p]] for p in b.packs}
    cfg = lane_cfg(b)
    acc = se.BarIndex(bars)
    out = {}
    for st in b.by_idx:
        cand = [i for i in range(warm, len(bars)) if dirs[st.pack][i] != 0 and confs[st.pack][i] >= cfg["entry_conf"]]
        lane = se.SetLane(st, s)
        trades = lane.scan(0, len(bars), bars, dirs[st.pack], confs[st.pack], True, cfg, cand, acc)
        ent = array("i", (t["entry_i"] for t in trades))
        ex = array("i", (t["exit_i"] for t in trades))
        pp = array("d", (t["pnl_pct"] for t in trades))
        out[st.idx] = (ent, ex, pp)
    return s, warm, dirs, confs, out


def build(names, bars, times, jobs: int):
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        res = list(pool.map(_symbol_job, [(s, bars[s], times[s][0]) for s in names]))
    return {s: (w, d, c, o) for s, w, d, c, o in res}


# ---- phase 2: the engine's gate, computed directly ----------------------------------------------------------------------

class Tapes:
    """Per Set: every shadow trade over all symbols, ordered by exit time (seconds)."""

    def __init__(self, book, names, times, shadow):
        self.cf = cost_as_frac(book.cost_pct)
        self.by_set: Dict[int, Tuple[array, array, array]] = {}
        for st in book.by_idx:
            rows = []
            for s in names:
                ent, ex, pp = shadow[s][3][st.idx]
                ts = times[s]
                rows.extend((ts[ex[k]] + 60, ts[ent[k]] + 60, pp[k]) for k in range(len(ex)))
            rows.sort()
            self.by_set[st.idx] = (array("d", (r[0] for r in rows)), array("d", (r[1] for r in rows)),
                                   array("d", (r[2] for r in rows)))

    def prefix(self, idx: int):
        """(net results, gain prefix sums, loss prefix sums) of a Set's tape, computed once."""
        cache = self.__dict__.setdefault("_prefix", {})
        if idx not in cache:
            nets = [x - self.cf for x in self.by_set[idx][2]]
            cache[idx] = (nets,) + se.prefix_sums(nets)
        return cache[idx]

    def window(self, idx: int, now: float, lookback_s: float, n: int):
        tx, te, pp = self.by_set[idx]
        end = bisect.bisect_left(tx, now)
        lo = bisect.bisect_left(tx, now - lookback_s)
        start = max(lo, end - n)
        return tx[start:end], te[start:end], pp[start:end]


def tape_source(tapes, idx: int, now: float, lookback_s: float):
    """Resolve a time-dependent tape adapter once per Set and evaluation time."""
    resolve = getattr(tapes, "tape_source", None)
    return resolve(idx, now, lookback_s) if resolve else tapes


def gate_metrics(book, tapes: Tapes, st, now: float):
    """(valid, rank, gate_n, gate_pf): the gate and the ranking key exactly as SetBook._score_one / gate_reason /
    rank_key compute them (self_check compares both with the engine). The Base windows first (gate_base), then the stages after Base."""
    valid, rank, n, pf = gate_base(book, tapes, st, now)
    if valid and (getattr(book, "main_on", False) or getattr(book, "neg_on", False) or getattr(book, "realst_on", False)):
        valid = stage_lane(book, tapes, st, now)          # "pos" / "neg": the lane the Set passes Real on; "" when none
    return valid, rank, n, pf


def base_eval(book, tapes: Tapes, st, now: float) -> Dict[str, Any]:
    """The Base long window's numbers for (Set, now), as SetBook._score_one reads them: the newest gate_window orders closed in the lookback (gross values
    pp, net values w), their count and PF (rounded as last_n_cost_pf does), the drawdown time over them, both halves, and the rank inputs."""
    lb = book.lookback * 60.0
    tx, te, pp = tapes.window(st.idx, now, lb, max(book.gate_window, book.deact_n))
    txa = tape_source(tapes, st.idx, now, lb).by_set[st.idx][0]
    n_all = bisect.bisect_left(txa, now) - bisect.bisect_left(txa, now - lb)     # SetState.n: every order in the lookback
    cf = tapes.cf
    w = pp[-book.gate_window:]
    wt = tx[-book.gate_window:]
    gp = sum(x - cf for x in w if x - cf > 0)
    gl = -sum(x - cf for x in w if x - cf < 0)
    n = len(w)
    pf = round(gp / gl, 4) if gl > 0 else (99.0 if gp > 0 else 0.0)     # rounded as last_n_cost_pf does
    # drawdown time over the last gate_window closes, open drawdown measured to now (se.drawdown_time)
    eq = peak = 0.0
    peak_t = wt[0] if n else now
    in_dd = False
    max_s = 0.0
    for t, x in zip(wt, w):
        eq += x - cf
        if eq >= peak - 1e-12:
            if in_dd:
                max_s = max(max_s, t - peak_t)
                in_dd = False
            peak = max(peak, eq)
            peak_t = t
        else:
            in_dd = True
    if in_dd:
        max_s = max(max_s, now - peak_t)
    last25 = pp[-book.deact_n:]
    r25 = sum(signed_result_r(x, book.cost_pct) for x in last25) / len(last25) if len(last25) else 0.0

    def _pf(v):
        g = sum(x - cf for x in v if x - cf > 0)
        l_ = -sum(x - cf for x in v if x - cf < 0)
        return g / l_ if l_ > 0 else (99.0 if g > 0 else 0.0)
    h = n // 2
    old, new = (round(_pf(w[:h]), 4), round(_pf(w[h:]), 4)) if h >= 1 else (0.0, 0.0)
    return {"w": w, "n": n, "pf": pf, "gp": gp, "gl": gl, "max_s": max_s, "old": old, "new": new, "n_all": n_all, "r25": r25,
            "floor": book.floor_for(book.gate_window, st.pack), "floor_short": book.floor_for(book.base_short, st.pack), "cf": cf}


def _long_ok(book, e: Dict[str, Any]) -> bool:
    """The Base long window on its own: orders, PF, drawdown time and (when asked) the consistency of both halves."""
    ok = e["n"] >= book.gate_min and e["pf"] + 1e-9 >= e["floor"] and e["max_s"] < book.max_dd_s
    if ok and book.consistency_pf > 0 and e["n"] >= 2:
        ok = min(e["old"], e["new"]) + 1e-9 >= book.consistency_pf
    return ok


def _short_ok(book, e: Dict[str, Any]) -> bool:
    sw = [x - e["cf"] for x in e["w"][-book.base_short:]]
    return len(sw) >= (book.base_short if getattr(book, "stage_full", False) else 1) and round(se.pf_of(sum(x for x in sw if x > 0), -sum(x for x in sw if x < 0)), 4) + 1e-9 >= e["floor_short"]


def _min_ok(book, e: Dict[str, Any]) -> bool:
    mw = [x - e["cf"] for x in e["w"][-book.base_min_n:]]
    return len(mw) >= (book.base_min_n if getattr(book, "stage_full", False) else 1) and se.sign_ok(mw, book.base_min_positive, getattr(book, "base_min_both", False), getattr(book, "base_min_avg", False))


def gate_base(book, tapes: Tapes, st, now: float, coordinate: bool = True):
    """The Base part of gate_metrics: the long, short and minimal windows, the drawdown time and the rank (no stage after Base)."""
    e = base_eval(book, tapes, st, now)
    valid = _long_ok(book, e)
    if valid and getattr(book, "base_short", 0):       # Base: the short window too (both must pass)
        valid = _short_ok(book, e)
    if valid and coordinate and getattr(book, "base_min_n", 0):
        valid = _min_ok(book, e)
    old, new, pf, n, floor = e["old"], e["new"], e["pf"], e["n"], e["floor"]
    halves = min(old, new) if (old or new) else pf
    shrunk = 1.0 + (min(float(pf), se.RANK_PF_CAP) - 1.0) * e["n_all"] / (e["n_all"] + se.RANK_SHRINK_N)
    rank = (halves + 1e-9 >= floor, round(shrunk, 4), e["n_all"], e["r25"], -e["max_s"])      # SetBook.rank_key
    return valid, rank, n, pf


def validation_lane(book, tapes: Tapes, st, now: float) -> str:
    """PF-validated lane, independent of the minimal entry coordination."""
    if not gate_base(book, tapes, st, now, coordinate=False)[0]:
        return ""
    if not (book.main_on or book.neg_on or book.realst_on):
        return "pos"
    ch, nets = chain_at(book, tapes, st, now)
    v = se.stage_verdicts([nets[k] for k in ch["main"]], [nets[k] for k in ch["real"]],
                          [nets[k] for k in ch["realneg"]], ch["short_n"], book.stage_params(st.pack))
    main_pf = not book.main_on or (v["main"] and (v["short"] or not book.short_on))
    if main_pf and (v["real"] if book.realst_on else True):
        return "pos"
    return "neg" if v["negl"] else ""


def window_figures(book, tapes: Tapes, st, now: float) -> Dict[str, Optional[Tuple[bool, float, float, int]]]:
    """Every last-N window on its own for (Set, now), as pfBase.windows counts them: {window: (passes, gains, losses, orders)} of the orders the window reads
    (net after PositionCost), None for a window the Set is not judged by (the Main windows, the Negative and the Real windows judge only the Sets that passed
    Base; Real judges the ones that passed Main; Real Negative the ones in a dip). The engine's SetState fields are the check (self_check)."""
    e = base_eval(book, tapes, st, now)
    cf = e["cf"]

    def sums(v):
        return sum(x for x in v if x > 0), -sum(x for x in v if x < 0)
    out: Dict[str, Optional[Tuple[bool, float, float, int]]] = {k: None for k in ("baseLong", "baseShort", "baseMinimal", "mainLong", "mainShort", "mainMinimal", "negative", "real", "realNeg")}
    nets_w = [x - cf for x in e["w"]]
    out["baseLong"] = (_long_ok(book, e),) + sums(nets_w) + (len(nets_w),)
    if getattr(book, "base_short", 0):
        sw = nets_w[-book.base_short:]
        out["baseShort"] = (_short_ok(book, e),) + sums(sw) + (len(sw),)
    if getattr(book, "base_min_n", 0):
        mw = nets_w[-book.base_min_n:]
        out["baseMinimal"] = (_min_ok(book, e),) + sums(mw) + (len(mw),)
    base_pass = out["baseLong"][0] and (out["baseShort"] is None or out["baseShort"][0])
    if not base_pass or not (getattr(book, "main_on", False) or getattr(book, "neg_on", False) or getattr(book, "realst_on", False)):
        return out
    ch, nets = chain_at(book, tapes, st, now)
    P = book.stage_params(st.pack)
    mn, rn, qn = [nets[k] for k in ch["main"]], [nets[k] for k in ch["real"]], [nets[k] for k in ch["realneg"]]
    v = se.stage_verdicts(mn, rn, qn, ch["short_n"], P)
    if book.main_on:
        w_ = mn[-book.main_n:]
        out["mainLong"] = (bool(v["main"]),) + sums(w_) + (len(w_),)
        if book.short_on:
            w_ = mn[-ch["short_n"]:]
            out["mainShort"] = (bool(v["short"]),) + sums(w_) + (len(w_),)
        if book.main_min_n:
            w_ = mn[-book.main_min_n:]
            out["mainMinimal"] = (bool(v["minimal"]),) + sums(w_) + (len(w_),)
    if book.neg_on:
        nw = next((w for w in sorted(book.neg_windows) if len(mn) >= w and sum(mn[-w:]) < -se.NEG_EPS), 0)
        out["negative"] = (bool(v["neg"]),) + (sums(mn[-nw:]) if nw else (0.0, 0.0)) + (nw,)
    main_pass = (not book.main_on) or bool(v["main"] and (v["short"] or not book.short_on))
    if book.realst_on and main_pass:
        w_ = rn[-book.realst_n:]
        out["real"] = (bool(v["real"]),) + sums(w_) + (len(w_),)
    if book.realst_on and book.neg_on and v["neg"]:
        w_ = qn[-book.realst_n:]
        out["realNeg"] = (bool(v["realneg"]),) + sums(w_) + (len(w_),)
    return out


def chain_at(book, tapes: Tapes, st, now: float):
    """(chain, nets): the Set's chain over its orders in the lookback before now (set_engine.stage_chain, the replay's rule). The chain reads the
    Base and Main windows and the Negative windows, never the Real last N: variants that differ only in Real N share it."""
    lb = book.lookback * 60.0
    tx, te, pp = tape_source(tapes, st.idx, now, lb).by_set[st.idx]
    e = bisect.bisect_left(tx, now)
    lo = bisect.bisect_left(tx, now - lb)
    ts, ents = list(tx[lo:e]), list(te[lo:e])
    nets = [x - tapes.cf for x in pp[lo:e]]
    return se.stage_chain(ts, ents, nets, book.stage_params(st.pack)), nets


def stage_lane(book, tapes: Tapes, st, now: float) -> str:
    """The Set's stage verdicts (set_engine.stage_verdicts) on its chain: "pos" when Main and Real pass, "neg" when the Negative lane and Real
    Negative pass, else ""."""
    ch, nets = chain_at(book, tapes, st, now)
    v = se.stage_verdicts([nets[k] for k in ch["main"]], [nets[k] for k in ch["real"]], [nets[k] for k in ch["realneg"]], ch["short_n"], book.stage_params(st.pack))
    return "pos" if v["pos"] else ("neg" if v["negl"] else "")


def processed_tail(book, tapes: Tapes, st, now: float, n: int) -> List[float]:
    """The net results of the Set's newest n processed orders closed before now: orders it opened while it passed its Base gate,
    judged on its own closes before each entry within the lookback (set_engine.processed_flag, SetBook.set_processed's rule)."""
    lb = book.lookback * 60.0
    source = tape_source(tapes, st.idx, now, lb)
    tx, te, pp = source.by_set[st.idx]
    nets, gpre, lpre = source.prefix(st.idx)
    e = bisect.bisect_left(tx, now)
    lo = bisect.bisect_left(tx, now - lb)
    floor = book.floor_for(book.gate_window, st.pack)
    out: List[float] = []
    for k in range(e - 1, lo - 1, -1):
        if se.processed_flag(tx, nets, gpre, lpre, te[k], book.gate_window, book.gate_min, floor, book.max_dd_s, lb, lo,
                             book.consistency_pf):
            out.append(nets[k])
            if len(out) >= n:
                break
    out.reverse()
    return out


def gate_until(book, tapes: Tapes, st, now: float) -> float:
    """The last moment gate_metrics(st, now) is known to keep its verdict: the Set's next close (it joins the last N), its
    oldest order in the lookback leaving it, and an open drawdown reaching the maximum drawdown time. A walk-forward that
    re-reads a Set only after this validates every order on the Set's last N orders at that order's own minute."""
    lb = book.lookback * 60.0
    tx = tape_source(tapes, st.idx, now, lb).by_set[st.idx][0]
    end = bisect.bisect_left(tx, now)
    lo = bisect.bisect_left(tx, now - lb)
    until = float(tx[end]) if end < len(tx) else float("inf")
    next_change = getattr(tapes, "next_change_time", None)
    if next_change:
        until = min(until, next_change(st.idx, now, lb))
    if lo < end:
        until = min(until, float(tx[lo]) + lb)
    _, _, pp = tapes.window(st.idx, now, lb, book.gate_window)
    wt = tx[max(lo, end - book.gate_window):end]
    eq = peak = 0.0
    peak_t = wt[0] if len(wt) else now
    in_dd = False
    for t, x in zip(wt, pp):
        eq += x - tapes.cf
        if eq >= peak - 1e-12:
            peak, peak_t, in_dd = max(peak, eq), t, False
        else:
            in_dd = True
    if in_dd and now - peak_t < book.max_dd_s:
        until = min(until, peak_t + book.max_dd_s)
    return until


def window_state_equals(book, tapes: Tapes, st, now: float) -> bool:
    """window_figures (the simulator's per-window figures) against the SetState fields the engine's _score_one left on the same Set: the pass flag, the gains and
    losses and the order count of every window the simulator judges."""
    got = window_figures(book, tapes, st, now)
    v = st.stage or {}
    want = {"baseLong": (st.base_long_ok, st.gate_gp, st.gate_gl, st.gate_n), "baseShort": (st.base_short_ok, st.base_short_gp, st.base_short_gl, st.base_short_n),
            "baseMinimal": (st.base_min_ok, st.base_min_gp, st.base_min_gl, st.base_min_n), "mainLong": (bool(v.get("main")), st.main_gp, st.main_gl, st.main_n),
            "mainShort": (bool(v.get("short")), st.main_short_gp, st.main_short_gl, st.main_short_n), "mainMinimal": (bool(v.get("minimal")), st.main_min_gp, st.main_min_gl, st.main_min_have),
            "negative": (bool(v.get("neg")), st.neg_win_gp, st.neg_win_gl, st.neg_win), "real": (bool(v.get("real")), st.exe_gp, st.exe_gl, st.exe_n),
            "realNeg": (bool(v.get("realneg")), st.neg_gp, st.neg_gl, st.neg_n)}
    for k, g in got.items():
        if g is None:
            continue
        w_ = want[k]
        if bool(g[0]) != bool(w_[0]) or abs(g[1] - w_[1]) > 1e-6 or abs(g[2] - w_[2]) > 1e-6 or int(g[3]) != int(w_[3]):
            return False
    return True


def self_check(book, tapes: Tapes, hours: Sequence[float], samples: int = 40) -> Tuple[int, int]:
    """The direct gate against the engine's own scoring on sampled (Set, hour) pairs; half of them are pairs that pass the Base
    gate, so the Main gate on the processed orders is compared too."""
    import random
    rnd = random.Random(5)
    bad = 0
    picks, tries = [], 0
    hours = list(hours)
    while len(picks) < samples and tries < samples * 400:
        tries += 1
        st, now = rnd.choice(book.by_idx), rnd.choice(hours)
        want_base = len(picks) % 2 == 0
        if want_base and tries < samples * 300:
            tx, te, pp = tapes.window(st.idx, now, book.lookback * 60.0, book.gate_window)
            g_ = sum(x - tapes.cf for x in pp if x - tapes.cf > 0)
            l_ = -sum(x - tapes.cf for x in pp if x - tapes.cf < 0)
            if len(pp) < book.gate_min or (g_ / l_ if l_ > 0 else 99.0) < book.min_pf:
                continue
        picks.append((st, now))
    for st, now in picks:
        tx, te, pp = tapes.window(st.idx, now, book.lookback * 60.0, 10 ** 9)
        st.hist = [{"t": float(a), "pnl": float(x) - tapes.cf, "pnl_pct": float(x), "hold_s": float(a - b), "reason": "x"}
                   for a, b, x in zip(tx, te, pp)]
        st.live = []
        book._score_one(st, now=now)
        st.n = len(st.hist)
        eng_valid = book.gate_reason(st) == ""
        eng_lane = book.lane_of(st)
        valid, key, n, pf = gate_metrics(book, tapes, st, now)
        if (valid or "") not in ("", True) and str(valid) != eng_lane:
            bad += 1
        valid = bool(valid)
        ek = book.rank_key(st)
        if valid != eng_valid or n != st.gate_n or abs(pf - st.gate_pf) > 1e-6 or key[:3] != ek[:3] or abs(key[3] - ek[3]) > 1e-9 \
                or abs(key[4] - ek[4]) > 1e-6:
            bad += 1
        elif not window_state_equals(book, tapes, st, now):
            bad += 1
        st.hist = []
    return len(picks), bad


# ---- phase 3: execution --------------------------------------------------------------------------------------------------

def execute(book, names, bars, times, shadow, tapes, hours, axis=None, set_axis=None, block=None, axis_evidence=None, only_steps=None, pick=None, control=None,
            refresh_s: int = 3600) -> List[Dict[str, Any]]:
    """pick(pool, key) chooses the Set among the ranked valid ones (default: the top rank); control='all' ranks every Set whether it passed
    the gate or not (a control: does validation carry information out of sample?). refresh_s: the gate and the ranking are recomputed
    this often inside the hour (the live engine refreshes every histRefreshS, 120 s)."""
    cfg = lane_cfg(book)
    accs = {s: se.BarIndex(bars[s]) for s in names}
    idx_of = {s: {t: i for i, t in enumerate(times[s])} for s in names}
    busy = {s: -1 for s in names}
    executed: List[Dict[str, Any]] = []
    closes: List[Tuple[float, float]] = []          # (exit t, net) of executed positions, for the axes
    cf = cost_as_frac(book.cost_pct)
    lb = book.lookback * 60.0
    every = max(1, int(refresh_s) // 60) if refresh_s else 1
    cache: Dict[int, Tuple[float, bool, Any]] = {}       # refresh_s 0: per order, each Set re-read only when its verdict can change

    def verdict(st, now: float):
        if not refresh_s:
            c = cache.get(st.idx)
            if c is None or now >= c[0]:
                ok_, key_, _, _ = gate_metrics(book, tapes, st, now)
                c = cache[st.idx] = (gate_until(book, tapes, st, now), ok_, key_)
            return c[1], c[2]
        ok_, key_, _, _ = gate_metrics(book, tapes, st, now)
        return ok_, key_

    def rank_at(now: float):
        ranked_: Dict[Tuple[str, str], List[Any]] = {}
        valid_ = 0
        for st in book.by_idx:
            if only_steps is not None and (st.kind == "trail" or st.step not in only_steps):
                continue                      # an evaluation of one step's Sets alone
            ok, key = verdict(st, now)
            if ok:
                valid_ += 1
            if ok or control == "all":
                lane_ = ok if isinstance(ok, str) else "pos"
                ranked_.setdefault((st.pack, st.kind, lane_), []).append((key, st))
        for k in ranked_:
            ranked_[k].sort(key=lambda r: r[0], reverse=True)
        return ranked_, valid_

    for h in hours:
        ranked, valid_n = rank_at(float(h) + 60.0)
        executed.append({"hour": h, "valid": valid_n, "total": len(book.by_idx)})
        ranked_t = float(h) + 60.0
        for minute in range(60):
            t = int(h) + minute * 60
            entry_t = float(t + 60)
            if refresh_s and minute and minute % every == 0:
                ranked, _ = rank_at(entry_t)
            for s in names:
                i = idx_of[s].get(t)
                if i is None or i <= busy[s]:
                    continue
                w, dirs, confs, _ = shadow[s]
                if i < w:
                    continue
                best = None
                for p in book.packs:
                    d, c = dirs[p][i], confs[p][i]
                    if d != 0 and c >= cfg["entry_conf"] and (best is None or c > best[1]):
                        best = (p, c, d)
                if best is None:
                    continue
                pack, conf, d = best
                if not refresh_s and ranked_t != entry_t:
                    ranked, ranked_t = rank_at(entry_t)[0], entry_t      # completed signal candle's close is the entry time
                # SetBook.pick_any: the positive lane first, then Real Negative; per lane base and Exit Sets together, else trailing
                pool, lane_pick = [], ""
                for lane_ in ("pos", "neg"):
                    fixed = sorted(ranked.get((pack, "base", lane_), []) + ranked.get((pack, "exit", lane_), []), key=lambda r: r[0], reverse=True)
                    pool = [st for _, st in fixed] or [st for _, st in sorted(ranked.get((pack, "trail", lane_), []), key=lambda r: r[0], reverse=True)]
                    if pool:
                        lane_pick = lane_
                        break
                if set_axis:
                    n_, min_pf = set_axis
                    keep = []
                    for st in pool:
                        tx, te, pp = tapes.window(st.idx, entry_t, lb, n_)
                        g = sum(x - cf for x in pp if x - cf > 0)
                        l_ = -sum(x - cf for x in pp if x - cf < 0)
                        if len(pp) >= n_ and (g / l_ if l_ > 0 else 99.0) >= min_pf:
                            keep.append(st)
                    pool = keep
                if not pool:
                    continue
                if axis and not axis_allows(closes if axis_evidence is None else axis_evidence, entry_t, axis, cf):
                    continue
                st = pick(pool, (s, t)) if pick else pool[0]
                lane = se.SetLane(st, s)
                dd = [0] * len(bars[s])
                cc = [0.0] * len(bars[s])
                dd[i], cc[i] = d, conf
                tr = lane.scan(i, len(bars[s]), bars[s], dd, cc, True, cfg, [i], accs[s])
                if not tr:
                    continue
                tr = tr[0]
                busy[s] = tr["exit_i"]
                legs = block_legs(bars[s], tr, block, book.cost_pct) if block else []
                rec = {"t": float(times[s][tr["exit_i"]] + 60), "t_in": float(t + 60), "symbol": s, "set": st.id,
                       "pack": pack, "kind": st.kind, "lane": lane_pick, "reason": tr["reason"], "net": tr["pnl_pct"] - cf, "legs": legs,
                       "tr": tr, "fracs": se.lane_fracs(st, book.cost_pct, book.sl_min_steps)}
                executed.append(rec)
                closes.append((rec["t"], rec["net"]))
    return executed


def axis_allows(closes, now, axis, cf) -> bool:
    last_n, min_pf, pause_k = axis
    done = [x for t, x in closes if t <= now]
    if pause_k:
        k = 0
        for x in reversed(done):
            if x < 0:
                k += 1
            else:
                break
        if k >= pause_k:
            return False
    if last_n and len(done) >= last_n:
        w = done[-last_n:]
        g = sum(x for x in w if x > 0)
        l_ = -sum(x for x in w if x < 0)
        if (g / l_ if l_ > 0 else 99.0) + 1e-9 < min_pf:
            return False
    return True


def block_legs(bars, tr, block, cost_pct) -> List[float]:
    """Net results of the Block legs of one position: leg k at entry + k x spacing steps in its favour, filled on the
    first bar that reaches it strictly before the exit bar, leaving at the position's exit price."""
    spacing, max_stack = block
    cf = cost_as_frac(cost_pct)
    side = int(tr["dir"])
    e = float(tr["entry_px"])
    x = float(tr["exit_px"])
    out = []
    i0, i1 = int(tr["entry_i"]) + 1, int(tr["exit_i"])
    for k in range(1, max_stack + 1):
        lvl = e * (1 + side * k * spacing * cf)
        hit = None
        for j in range(i0, i1):
            if (side > 0 and bars[j][1] >= lvl) or (side < 0 and bars[j][2] <= lvl):
                hit = j
                break
        if hit is None:
            break
        out.append((x - lvl) / lvl * side - cf)
        i0 = hit
    return out


def summarize(rows: List[Dict[str, Any]], lo: float, hi: float, with_legs: bool) -> Dict[str, Any]:
    hours = [r for r in rows if "hour" in r and lo <= r["hour"] < hi]
    trades = [r for r in rows if "net" in r and lo <= r["t_in"] < hi]
    vals = []
    for r in trades:
        vals.append((r["t"], r["net"]))
        if with_legs:
            vals.extend((r["t"], v) for v in r["legs"])
    gp = sum(v for _, v in vals if v > 0)
    gl = -sum(v for _, v in vals if v < 0)
    eq = peak = mdd = 0.0
    for _, v in sorted(vals):
        eq += v
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    # hours positive: by the hour the position was opened in, like the window itself (orders opened in [lo, hi)); keyed by exit time it counted an extra hour
    # of positions that left after the window end and could not match the order count, the PF or the net of the same orders
    by_hour: Dict[int, float] = {}
    for r in trades:
        k = int(r["t_in"] // 3600)
        by_hour[k] = by_hour.get(k, 0.0) + r["net"] + (sum(r["legs"]) if with_legs else 0.0)
    pos_h = sum(1 for v in by_hour.values() if v > 0)
    vp = [100.0 * h["valid"] / h["total"] for h in hours]
    return {"pf": round(gp / gl, 3) if gl > 0 else None, "orders": len(trades), "legs": len(vals) - len(trades),
            "gp_pct": round(100 * gp, 6), "gl_pct": round(100 * gl, 6),          # the exact sums the PF divides: pooling several cells or windows adds these, never PFs
            "net_pct": round(100 * (gp - gl), 2), "net_per_order_pct": round(100 * (gp - gl) / len(trades), 4) if trades else 0.0, "max_dd_pct": round(100 * mdd, 2),
            "hours_positive": f"{pos_h}/{len(by_hour)}", "validated_pct": round(sum(vp) / len(vp), 2) if vp else 0.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--cache", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--sealed", action="store_true", help="also print the sealed window (finalists only)")
    a = ap.parse_args()
    t0 = time.time()
    names, bars, times = load_bars(a.data, a.symbols)
    if a.cache and os.path.exists(a.cache):
        shadow = pickle.load(open(a.cache, "rb"))
    else:
        shadow = build(names, bars, times, a.jobs)
        if a.cache:
            pickle.dump(shadow, open(a.cache, "wb"))
    book = make_book()
    tapes = Tapes(book, names, times, shadow)
    start = max(times[s][0] for s in names) + book.lookback * 60
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
    split = hours[len(hours) * 2 // 3]
    n_chk, bad = self_check(book, tapes, hours)
    print(f"symbols={len(names)} hours={len(hours)} dev<{time.strftime('%m-%d %H:%M', time.gmtime(split))}<=sealed "
          f"self-check {n_chk - bad}/{n_chk} match the engine's gate  build {time.time() - t0:.0f}s", flush=True)
    if bad:
        print("the direct gate differs from SetBook._score_one / gate_reason: numbers below are not the engine's")
        return 1
    variants = [("base", {}), ]
    for last_n in (4, 8, 16):
        for mpf in (1.0, 1.2, 1.5):
            variants.append((f"axis last{last_n}>={mpf} pause5", {"axis": (last_n, mpf, 5)}))
    variants.append(("axis current last4>=1.2 pause8", {"axis": (4, 1.2, 8)}))
    for n_, mpf in ((5, 1.2), (10, 1.2), (10, 1.5), (20, 1.5)):
        variants.append((f"set-axis last{n_}>={mpf}", {"set_axis": (n_, mpf)}))
    results = {}
    for name, kw in variants:
        rows = execute(book, names, bars, times, shadow, tapes, hours, **kw)
        dev = summarize(rows, hours[0], split, False)
        results[name] = {"dev": dev, "rows": rows}
        print(f"{name:34s} dev  PF {dev['pf']}  orders {dev['orders']}  net {dev['net_pct']}%  maxDD {dev['max_dd_pct']}%  "
              f"hours+ {dev['hours_positive']}  validated {dev['validated_pct']}%", flush=True)
    for spacing in (1, 2, 3):
        rows = execute(book, names, bars, times, shadow, tapes, hours, block=(spacing, 8))
        dev = summarize(rows, hours[0], split, True)
        name = f"base + block 8 legs every {spacing} step(s)"
        results[name] = {"dev": dev, "rows": rows}
        print(f"{name:34s} dev  PF {dev['pf']}  orders {dev['orders']}+{dev['legs']} legs  net {dev['net_pct']}%  "
              f"maxDD {dev['max_dd_pct']}%  hours+ {dev['hours_positive']}", flush=True)
    if a.sealed:
        for name in a.sealed_names if hasattr(a, "sealed_names") else results:
            r = results[name]
            sealed = summarize(r["rows"], split, hours[-1] + 3600, "block" in name)
            r["sealed"] = sealed
            print(f"{name:34s} SEALED PF {sealed['pf']}  orders {sealed['orders']}  net {sealed['net_pct']}%  "
                  f"maxDD {sealed['max_dd_pct']}%  hours+ {sealed['hours_positive']}  validated {sealed['validated_pct']}%")
    if a.out:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "rows"} for k, v in results.items()}, open(a.out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
