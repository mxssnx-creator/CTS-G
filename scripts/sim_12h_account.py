#!/usr/bin/env python3
"""12-hour account-level trade simulation for the CTS-G pulse engine.

Layers (see ``--help`` and the JSON ``assumptions`` block):

1. ENGINE (replay) -- per symbol, the deployed SetBook (overlay-bingx-x02.json
   + connection_profile.processing_profile()) is replayed over the engine's own
   2-day lookback (histLookbackBars) before the simulation window and through
   it.  Set trades come from ``SetBook._replay_core_vectorized`` (every
   general/indications Set: SL:TP ratio x TP step x Normal/trailing), Block and
   DCA trades from the ``histSimulateBlock`` / ``histSimulateDca`` lanes of
   ``SetBook._replay_symbol`` and indication-kind evidence from
   ``SetBook._replay_kind_tapes``.  The replay functions are called unchanged;
   only their row constructor is swapped for a compact tuple and the bounded
   ring (REPLAY_HIST_CAP) is widened so that every close is kept.

2. STAGE CHAIN (walk-forward) -- a Set trade may only be executed when, at its
   entry bar, the Set x direction evidence that CLOSED BEFORE the entry passes
   the engine's strict entry gate: Base last-N cost-PF ratio >= floor,
   Main and Real last-N >= floor (the book's stage windows), DD-time <= setMaxDdTimeS, indication
   kind gate (SetBook.indication_ok) for the indications pack and the live
   negative deactivation (last-25 own closes).  Rejected Sets keep producing
   evidence.

3. ACCOUNT -- start equity, venue-minimum sizing through pulse_trader's own
   ``size_qty`` / ``raise_to_min_qty`` / ``leverage_for``, cross margin,
   mark-to-market every minute, order/position accounting with shared
   (controlOrdersOverall) SL/TP control pairs.

Usage::

    CTS_DATA_DIR=/path/data-sim python3 scripts/sim_12h_account.py \
        --data-dir /path/data --start-equity 10 --hours 12 \
        --out reports/sim-12h-account.json --html reports/sim-12h-account.html
"""
from __future__ import annotations

import argparse
import bisect
import glob
import html
import io
import json
import math
import os
import pickle
import sys
import time
from collections import defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PULSE = os.path.join(ROOT, "server", "pulse")
BAR = 60
COST_REASONS = ("sl", "tp", "time", "scratch+")
IND_KINDS = ("state", "signals", "active", "direction", "move", "common", "trend", "break", "msi", "vwap", "retest", "squeeze", "sweep", "rsi2", "keltner", "impulse")
AXIS_NAMES = ("prev", "last", "cont", "pause")
AXIS_WINDOWS = AXIS_NAMES  # legacy name: iterated as the axis list


def axis_children(ov: Optional[Dict[str, Any]] = None) -> List[Tuple[str, int]]:
    """(axis, count) children the engine evaluates: coord_engine.AXIS_SPECS
    counts from min up to the overlay's clamped max window, enabled axes only."""
    _engine_path()
    from coord_engine import AXIS_SPECS, clamp_window
    ov = ov or {}
    out: List[Tuple[str, int]] = []
    for axis in AXIS_NAMES:
        cap = axis.capitalize()
        if ov and ov.get(f"axis{cap}Enabled") is False:
            continue
        spec = AXIS_SPECS[axis]
        top = clamp_window(axis, ov.get(f"axis{cap}MaxWindow", spec["default"]))
        out.extend((axis, c) for c in range(int(spec["min"]), int(top) + 1, int(spec["step"])))
    return out
VST_CONTRACTS_URL = "https://open-api-vst.bingx.com/openApi/swap/v2/quote/contracts"


def _engine_path() -> None:
    os.environ.setdefault("CTS_DATA_DIR", os.path.join(ROOT, ".sim-data"))
    if PULSE not in sys.path:
        sys.path.insert(0, PULSE)


# --------------------------------------------------------------------------
# Pure account math (unit tested in scripts/test_sim_12h_account.py)
# --------------------------------------------------------------------------

def classic_pf(values: Sequence[float]) -> float:
    gp = sum(v for v in values if v > 0)
    gl = -sum(v for v in values if v < 0)
    if gl <= 0:
        return 99.0 if gp > 0 else 0.0
    return gp / gl


# Round-trip fee the simulated account pays (None = the PositionCost).
FEE_PCT: Optional[float] = None


def cost_pf_ratio(moves: Sequence[float], cost_pct: float) -> float:
    """Engine cost-PF ratio (position_cost.last_n_cost_pf over the whole list)."""
    if not moves:
        return 1.0
    avg_r = sum((m * 100.0 - cost_pct) / cost_pct for m in moves) / len(moves)
    return round(1.0 + 0.1 * avg_r, 4)


def clears(value: float, floor: float) -> bool:
    """position_cost.clears_pf."""
    return value + 1e-9 >= 1.0 and value + 1e-9 >= floor


class Account:
    """Cross-margin USDT-M account with independent lots.

    ``cost_pct`` is the engine round-trip PositionCost in percent of entry
    notional; half is charged on the entry fill and half on the close fill.
    """

    def __init__(self, equity: float, cost_pct: float) -> None:
        self.start = float(equity)
        self.cash = float(equity)
        self.cost_frac = float(cost_pct) / 100.0
        self.lots: Dict[int, Dict[str, Any]] = {}
        self.used_margin = 0.0
        self.fees = 0.0
        self.gross_realized = 0.0
        # per (symbol, side) aggregates for O(symbols) marking
        self.agg: Dict[Tuple[str, int], List[float]] = defaultdict(lambda: [0.0, 0.0, 0])  # qty, qty*entry, lots

    def open_lot(self, lot_id: int, symbol: str, side: int, qty: float, px: float, lev: float, **meta: Any) -> Dict[str, Any]:
        notional = qty * px
        fee = notional * self.cost_frac / 2.0
        margin = notional / max(1.0, float(lev))
        lot = dict(id=lot_id, symbol=symbol, side=int(side), qty=float(qty), entry=float(px),
                   notional=notional, margin=margin, fee_in=fee, **meta)
        self.lots[lot_id] = lot
        self.cash -= fee
        self.fees += fee
        self.used_margin += margin
        a = self.agg[(symbol, int(side))]
        a[0] += qty
        a[1] += qty * px
        a[2] += 1
        return lot

    def add_fill(self, lot_id: int, qty: float, px: float, lev: float) -> float:
        """Add quantity to an open lot (DCA add). Returns the added margin."""
        lot = self.lots[lot_id]
        notional = qty * px
        fee = notional * self.cost_frac / 2.0
        margin = notional / max(1.0, float(lev))
        lot["entry"] = (lot["entry"] * lot["qty"] + px * qty) / (lot["qty"] + qty)
        lot["qty"] += qty
        lot["notional"] += notional
        lot["margin"] += margin
        lot["fee_in"] += fee
        self.cash -= fee
        self.fees += fee
        self.used_margin += margin
        a = self.agg[(lot["symbol"], lot["side"])]
        a[0] += qty
        a[1] += qty * px
        return margin

    def close_lot(self, lot_id: int, px: float) -> Dict[str, Any]:
        lot = self.lots.pop(lot_id)
        gross = lot["qty"] * (px - lot["entry"]) * lot["side"]
        fee = lot["notional"] * self.cost_frac / 2.0
        self.cash += gross - fee
        self.fees += fee
        self.gross_realized += gross
        self.used_margin -= lot["margin"]
        if abs(self.used_margin) < 1e-12:
            self.used_margin = 0.0
        a = self.agg[(lot["symbol"], lot["side"])]
        a[0] -= lot["qty"]
        a[1] -= lot["qty"] * lot["entry"]
        a[2] -= 1
        if a[2] <= 0:
            self.agg.pop((lot["symbol"], lot["side"]), None)
        lot.update(exit=px, gross=gross, fee_out=fee, net=gross - lot["fee_in"] - fee)
        return lot

    def maintenance(self, mmr_of: Dict[str, float]) -> float:
        return sum(lot["notional"] * mmr_of[lot["symbol"]] for lot in self.lots.values())

    def liquidate(self, high: Dict[str, float], low: Dict[str, float], mmr_of: Dict[str, float]) -> List[Dict[str, Any]]:
        """Cross-margin liquidation: every lot closes at the bar's adverse
        extreme and the maintenance margin is forfeited (balance floored at 0)."""
        mm = self.maintenance(mmr_of)
        out = []
        for lot_id in list(self.lots):
            lot = self.lots[lot_id]
            px = low[lot["symbol"]] if lot["side"] > 0 else high[lot["symbol"]]
            out.append(self.close_lot(lot_id, px))
        before = self.cash
        self.cash = max(0.0, self.cash - mm)
        self.liquidation_loss = getattr(self, "liquidation_loss", 0.0) + (before - self.cash)
        return out

    def unrealized(self, px: Dict[str, float]) -> float:
        total = 0.0
        for (sym, side), (qty, basis, _n) in self.agg.items():
            total += (qty * px[sym] - basis) * side
        return total

    def worst_unrealized(self, high: Dict[str, float], low: Dict[str, float]) -> float:
        total = 0.0
        for (sym, side), (qty, basis, _n) in self.agg.items():
            total += (qty * low[sym] - basis) if side > 0 else (basis - qty * high[sym])
        return total

    def equity(self, px: Dict[str, float]) -> float:
        return self.cash + self.unrealized(px)

    def groups(self) -> Dict[Tuple[str, int], int]:
        return {k: int(v[2]) for k, v in self.agg.items() if v[2] > 0}


class ControlOrders:
    """controlOrdersOverall: one SL+TP pair per symbol x direction group.

    Per processing minute a group that becomes occupied places both legs, a
    group whose membership/quantity changed while staying occupied
    cancel-replaces both legs, and a group that empties cancels both legs.
    """

    def __init__(self) -> None:
        self.place = 0
        self.cancel_replace = 0
        self.cancel = 0

    def step(self, before: Dict[Any, int], after: Dict[Any, int], changed: set) -> None:
        for g in changed:
            b = before.get(g, 0)
            a = after.get(g, 0)
            if b == 0 and a > 0:
                self.place += 2
            elif b > 0 and a == 0:
                self.cancel += 2
            elif b > 0 and a > 0:
                self.cancel_replace += 2

    def as_dict(self) -> Dict[str, int]:
        return {"controlPlace": self.place, "controlCancelReplace": self.cancel_replace, "controlCancel": self.cancel}


class DrawdownTracker:
    """Running-peak equity drawdown in percent plus underwater time."""

    def __init__(self, start: float) -> None:
        self.peak = float(start)
        self.max_dd_pct = 0.0
        self.max_dd_intrabar_pct = 0.0
        self.under_since: Optional[int] = None
        self.max_under_min = 0

    def update(self, t: int, equity_close: float, equity_worst: Optional[float] = None) -> Tuple[float, float]:
        worst = equity_close if equity_worst is None else min(equity_worst, equity_close)
        dd_worst = max(0.0, (self.peak - worst) / self.peak * 100.0) if self.peak > 0 else 0.0
        self.max_dd_intrabar_pct = max(self.max_dd_intrabar_pct, dd_worst)
        if equity_close >= self.peak - 1e-12:
            self.peak = max(self.peak, equity_close)
            if self.under_since is not None:
                self.max_under_min = max(self.max_under_min, t - self.under_since)
                self.under_since = None
            dd = 0.0
        else:
            dd = (self.peak - equity_close) / self.peak * 100.0
            if self.under_since is None:
                self.under_since = t
            self.max_under_min = max(self.max_under_min, t + 1 - self.under_since)
        self.max_dd_pct = max(self.max_dd_pct, dd)
        return dd, dd_worst


# --------------------------------------------------------------------------
# Engine setup
# --------------------------------------------------------------------------

# The live engine reads the desk overlay; connection_profile.processing_profile
# is only a prepared patch (scripts/prepare_connection_profile.py). So the
# overlay wins and the profile fills keys the overlay does not set. Sweeps that
# inject their values through processing_profile set PROFILE_WINS.
PROFILE_WINS = False


def deployed_settings(overlay_path: str) -> Dict[str, Any]:
    _engine_path()
    from connection_profile import processing_profile
    overlay = json.load(open(overlay_path))
    if PROFILE_WINS:
        return {**overlay, **processing_profile()}
    return {**processing_profile(), **overlay}


def make_book(overlay_path: str):
    _engine_path()
    from set_engine import SetBook
    book = SetBook()
    book.load(deployed_settings(overlay_path))
    return book


def build_catalog(book) -> List[Dict[str, Any]]:
    """Unique replay behaviours of the full catalog, with Set multiplicity.

    Sets whose (pack, bound SL, bound TP, trailing pair) coincide produce the
    identical tape; each is still an independent Set/lot in the engine, so the
    multiplicity is carried into the account layer.
    """
    index: Dict[Tuple, int] = {}
    out: List[Dict[str, Any]] = []
    for st in book.by_idx:
        sl, tp = book.pair_sl_tp(st.tp_pct, float(st.sl_ratio or 0.6))
        key = (st.pack, round(sl, 10), round(tp, 10), st.trail_key)
        if key not in index:
            index[key] = len(out)
            out.append(dict(uid=len(out), set_id=st.id, pack=st.pack, kind=st.kind, sl=sl, tp=tp,
                            trail=st.trail_key, mult=0, set_ids=[]))
        row = out[index[key]]
        row["mult"] += 1
        row["set_ids"].append(st.id)
    return out


def load_symbol(data_dir: str, symbol: str) -> Tuple[List[List[float]], int]:
    with open(os.path.join(data_dir, symbol + ".json")) as fh:
        d = json.load(fh)
    # The first row is the time base. Fetched files carry warmup bars before
    # "start", so "start" would shift every timestamp (and report label) late.
    return [r[1] for r in d["rows"]], int(d["rows"][0][0]) // 1000


# --------------------------------------------------------------------------
# Stage A: engine replay per symbol (worker process)
# --------------------------------------------------------------------------

def _tuple_fill(ts, symbol, side, pnl_pct, hold_s, reason, **_kw):
    return (float(ts), int(side), float(pnl_pct), float(hold_s), str(reason))


def replay_symbol(job: Dict[str, Any]) -> Dict[str, Any]:
    _engine_path()
    import set_engine as se
    sym, cache = job["symbol"], job["cache"]
    out_path = os.path.join(cache, f"{sym}.pkl")
    if os.path.exists(out_path) and not job.get("force"):
        try:
            with open(out_path, "rb") as fh:
                cached_floor = float(pickle.load(fh).get("sl_floor") or 0.0)
        except Exception:
            cached_floor = -1.0
        if abs(cached_floor - float(job.get("sl_floor") or 0.0)) < 1e-12:
            return {"symbol": sym, "cached": True}
    t0 = time.time()
    book = make_book(job["overlay"])
    sl_floor = float(job.get("sl_floor") or 0.0)
    if sl_floor > 0:
        book.set_symbol_sl_floors({sym: sl_floor})
    catalog = build_catalog(book)
    bars_all, start_s = load_symbol(job["data_dir"], sym)
    n_all = len(bars_all)
    sim_start = job["sim_start"]
    lo = max(0, sim_start - int(book.lookback) - 60)
    sub = bars_all[lo:]
    book.exact_replay_window = False
    book.lookback = len(sub)
    book.ingest_bars(sym, sub)
    bars = book.bars[sym]
    assert len(bars) == len(sub)
    n = len(bars)
    now = start_s + (n_all - 1) * BAR
    signals, kind_sigs, warmup = book.prepare_replay_signals(sym, now=now)
    time_bars = max(8, min(book.hist_time_bars, max(8, n - warmup - 1)))
    scratch_bars = max(8, int(book.scratch_s / BAR))
    honor_tp = bool(getattr(book, "hist_honor_tp", True))
    base_ts = now - (n - 1) * BAR

    # ---- core Set tapes: engine vectorized replay, every close retained ----
    orig = (se.hist_fill, se.recent_direction_rows, se.REPLAY_HIST_CAP)
    cols = {k: [] for k in ("uid", "side", "entry", "exit", "raw", "reason")}
    reason_code = {r: i for i, r in enumerate(COST_REASONS)}
    open_end = 0
    try:
        se.hist_fill = _tuple_fill
        se.recent_direction_rows = lambda rows, cap: rows
        # Long lookbacks produce more closes per Set than a fixed ring holds;
        # size it to the replay (bounded) so evidence is not cut short.
        se.REPLAY_HIST_CAP = max(512, min(4096, n // 4))
        for pack in book.packs:
            reps = [r for r in catalog if r["pack"] == pack]
            states = [book.sets[r["set_id"]] for r in reps]
            hist: Dict[str, list] = {}
            counts: Dict[str, int] = {}
            book._replay_core_vectorized(sym, bars, states, signals[pack], now, warmup, time_bars,
                                         scratch_bars, honor_tp, hist, counts)
            sig = signals[pack]
            sig_bars = {d: np.array([i for i in range(warmup, n) if sig[i][0] == d and sig[i][1] >= 0.58], dtype=np.int64)
                        for d in (1, -1)}
            for rep, st in zip(reps, states):
                rows = hist.get(st.id) or []
                if len(rows) != int(counts.get(st.id, 0)):
                    # Only the oldest closes fell out of the ring. That is fine
                    # as long as everything from the sim window on is kept.
                    first_exit = min((int(round((r[0] - base_ts) / BAR)) for r in rows), default=n)
                    if first_exit + lo > sim_start:
                        raise RuntimeError(f"{sym} {st.id}: ring truncated inside the sim window "
                                           f"{len(rows)} != {counts.get(st.id)}")
                last_exit = {1: None, -1: None}
                for ts, side, move, hold, why in rows:
                    ex = int(round((ts - base_ts) / BAR))
                    en = ex - int(round(hold / BAR))
                    cols["uid"].append(rep["uid"]); cols["side"].append(side)
                    cols["entry"].append(en + lo); cols["exit"].append(ex + lo)
                    cols["raw"].append(move); cols["reason"].append(reason_code[why])
                    if last_exit[side] is None or ex > last_exit[side]:
                        last_exit[side] = ex
                # Position still open at the end of data: first eligible signal
                # after the last close (+cooldown) that never produced a close.
                for side in (1, -1):
                    first = warmup if last_exit[side] is None else last_exit[side] + max(0, int(book.cooldown_bars))
                    arr = sig_bars[side]
                    k = int(np.searchsorted(arr, first, side="left"))
                    if k < len(arr):
                        j = int(arr[k])
                        if n - 1 - j >= time_bars:
                            raise RuntimeError(f"{sym} {st.id}: open reconstruction inconsistent at {j}")
                        cols["uid"].append(rep["uid"]); cols["side"].append(side)
                        cols["entry"].append(j + lo); cols["exit"].append(-1)
                        cols["raw"].append(0.0); cols["reason"].append(255)
                        open_end += 1
            del hist, counts
    finally:
        se.hist_fill, se.recent_direction_rows, se.REPLAY_HIST_CAP = orig
    core = dict(uid=np.array(cols["uid"], dtype=np.int32), side=np.array(cols["side"], dtype=np.int8),
                entry=np.array(cols["entry"], dtype=np.int32), exit=np.array(cols["exit"], dtype=np.int32),
                raw=np.array(cols["raw"], dtype=np.float64), reason=np.array(cols["reason"], dtype=np.uint8))
    del cols
    # Retain the evidence the walk-forward gate can reach: every close from
    # ``keep_from`` on (pooled over 48 symbols this is several hundred closes
    # per Set x side before the window, far above the last-30/last-96 needs)
    # plus the last ``keep_tail`` earlier closes per Set x side as a safety.
    n_closes_total = int((core["exit"] >= 0).sum())
    keep_from = sim_start - int(job.get("keep_bars", 240))
    keep = (core["exit"] < 0) | (core["exit"] >= keep_from) | (core["entry"] >= sim_start)
    pre = np.flatnonzero((core["exit"] >= 0) & (core["exit"] < keep_from))
    if len(pre):
        g = core["uid"][pre].astype(np.int64) * 2 + (core["side"][pre] > 0)
        o = np.lexsort((core["exit"][pre], g))
        gs = g[o]
        ends_at = np.r_[np.flatnonzero(gs[1:] != gs[:-1]), len(gs) - 1]
        counts_g = np.diff(np.r_[-1, ends_at])
        rank = np.repeat(ends_at, counts_g) - np.arange(len(gs))
        keep[pre[o[rank < int(job.get("keep_tail", 6))]]] = True
    core = {k: v[keep] for k, v in core.items()}

    # ---- Block / DCA / indication-kind lanes: engine _replay_symbol ----
    orig_adv = book._advance_pos

    def advance(pos, bar, i, **kw):
        strategy = kw.get("strategy")
        before = float(pos.get("qty") or 0.0)
        new_pos, rec = orig_adv(pos, bar, i, **kw)
        if strategy in ("dca", "block"):
            if float(pos.get("qty") or 0.0) > before + 1e-12 and int(pos.get("i", -1)) != i:
                pos.setdefault("_log", []).append((i + lo, float(bar[3]), float(pos["qty"]) - before))
            if rec is not None:
                rec["_entry"] = int(pos["i"]) + lo
                rec["_exit"] = i + lo
                rec["_qty"] = float(pos.get("qty") or 1.0)
                rec["_parent"] = float(pos.get("parent") or 1.0)
                rec["_avg"] = float(pos["entry"])
                rec["_log"] = list(pos.get("_log") or [])
        return new_pos, rec

    book._advance_pos = advance
    seeds = []
    for pack in book.packs:
        seed = next(st for st in book.by_idx if st.pack == pack and st.kind == "base")
        seeds.append(seed.id)
    hist_tmp: Dict[str, list] = {}
    ind_hist: Dict[str, list] = {}
    strat_hist: Dict[str, list] = {}
    book._replay_symbol(sym, hist_tmp, now, ind_hist=ind_hist, strat_hist=strat_hist, set_ids=seeds,
                        prepared=(signals, kind_sigs, warmup))
    strat = []
    for key, rows in strat_hist.items():
        for r in rows:
            d = dict(r.as_dict() if hasattr(r, "as_dict") else r)
            d["lane"] = key
            strat.append(d)
    kinds = []
    for kind, rows in ind_hist.items():
        for r in rows:
            ex = int(round((float(r["t"]) - base_ts) / BAR))
            en = ex - int(round(float(r["hold_s"]) / BAR))
            kinds.append((kind, 1 if str(r["side"])[:1] == "L" else -1, en + lo, ex + lo, float(r["pnl_pct"]),
                          str(r.get("ind_config") or ""), str(r.get("reason") or "")))
    # indications-pack signal: contributing kinds per bar (bitmask, IND_KINDS order)
    mask = np.zeros(n_all, dtype=np.uint16)
    tagmap = se.IND_TAG_KIND
    ind_sig = signals.get("indications") or [(0, 0.0, "")] * n
    for i in range(n):
        d, conf, why = ind_sig[i]
        if d == 0:
            continue
        m = 0
        for tag in str(why or "").split("+"):
            k = tagmap.get(tag.strip())
            if k in IND_KINDS:
                m |= 1 << IND_KINDS.index(k)
        mask[i + lo] = m
    payload = dict(symbol=sym, lo=lo, n_all=n_all, start_s=start_s, warmup=warmup + lo, time_bars=time_bars,
                   core=core, strat=strat, kinds=kinds, ind_mask=mask, open_end=open_end, closes_total=n_closes_total,
                   seconds=round(time.time() - t0, 1), sl_floor=sl_floor)
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, out_path)
    return {"symbol": sym, "cached": False, "seconds": payload["seconds"], "closes": n_closes_total,
            "kept": int(len(core["uid"])),
            "openEnd": open_end, "strat": len(strat), "kinds": len(kinds)}


# --------------------------------------------------------------------------
# Stage B: walk-forward stage chain
# --------------------------------------------------------------------------

def _rolling_ratio(cs: np.ndarray, idx: np.ndarray, gstart: np.ndarray, n: int) -> Tuple[np.ndarray, np.ndarray]:
    """Cost-PF ratio of the last ``n`` closes up to sorted index ``idx``.

    ``cs`` is cumsum(R) with a leading zero; returns (ratio, available).
    """
    cnt = idx - gstart + 1
    ok = (idx >= gstart) & (cnt >= n)
    hi = np.where(ok, idx + 1, 0)
    lo = np.where(ok, idx + 1 - n, 0)
    mean = (cs[hi] - cs[lo]) / n
    return np.round(1.0 + 0.1 * mean, 4), ok


def clears_vec(v: np.ndarray, floor: float) -> np.ndarray:
    return (v + 1e-9 >= 1.0) & (v + 1e-9 >= floor)


def stage_flags(ev, idx, gstart, need: int, windows: Tuple[int, int, int], floors: Dict[str, float]) -> Dict[str, np.ndarray]:
    """SetBook stage chain on walk-forward evidence (one call per candidate chunk).

    Base = PF over the last min(n, Base window) closes with n >= need
    (last_n_cost_pf); Main / Real need their full last-N window. Each later
    stage requires the earlier one (_stage_qualification / _real_metrics_ok).
    """
    base_n, main_n, real_n = windows
    r30, ok30 = ev.partial_ratio(idx, gstart, base_n, need)
    r5, ok5 = ev.ratio(idx, gstart, main_n)
    r3, ok3 = ev.ratio(idx, gstart, real_n)
    base_ok = ok30 & clears_vec(r30, floors["base"])
    main_ok = base_ok & ok5 & clears_vec(r5, floors["main"])
    real_ok = main_ok & ok3 & clears_vec(r3, floors["real"])
    return dict(r30=r30, ok30=ok30, r5=r5, ok5=ok5, r3=r3, ok3=ok3,
                base_ok=base_ok, main_ok=main_ok, real_ok=real_ok)


class Evidence:
    """Sorted closes per group with O(log n) walk-forward lookups."""

    def __init__(self, group: np.ndarray, exit_bar: np.ndarray, moves: np.ndarray, cost_pct: float, tiebreak=None):
        keys = [exit_bar, group] if tiebreak is None else [tiebreak, exit_bar, group]
        order = np.lexsort(keys)
        del keys
        self.group = np.asarray(group)[order].astype(np.int32)
        self.exit = np.asarray(exit_bar)[order].astype(np.int32)
        self.moves = np.asarray(moves)[order].astype(np.float64)
        self.tiebreak = None if tiebreak is None else np.asarray(tiebreak)[order]
        del order
        r = (self.moves * 100.0 - cost_pct) / cost_pct
        self.cs = np.concatenate([[0.0], np.cumsum(r)])
        # cumulative count of cost-net losing closes (axis pause streaks)
        self.cl = np.concatenate([[0], np.cumsum(r < 0)]).astype(np.int64)
        del r
        self.key = self.group.astype(np.int64) * 1_000_000 + self.exit.astype(np.int64)
        self.ug, self.first = np.unique(self.group, return_index=True)
        self.ug = self.ug.astype(np.int64)

    def lookup(self, group: np.ndarray, bar: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Index of the last close with exit < bar in the same group, and group start."""
        group = np.asarray(group, dtype=np.int64)
        q = group * 1_000_000 + np.asarray(bar, dtype=np.int64)
        idx = np.searchsorted(self.key, q, side="left") - 1
        if len(self.ug):
            pos = np.clip(np.searchsorted(self.ug, group), 0, len(self.ug) - 1)
            known = self.ug[pos] == group
            gstart = np.where(known, self.first[pos], 10**15).astype(np.int64)
        else:
            gstart = np.full(len(group), 10**15, dtype=np.int64)
        same = (idx >= 0) & (idx >= gstart)
        idx = np.where(same, idx, -1)
        gstart = np.where(same, gstart, 0)
        return idx, gstart

    def ratio(self, idx, gstart, n):
        return _rolling_ratio(self.cs, idx, gstart, n)

    def window_ratio(self, idx, gstart, skip, n):
        """Ratio of closes (idx-skip-n, idx-skip]; axis 'prev' window."""
        return _rolling_ratio(self.cs, idx - skip, gstart, n)

    def partial_ratio(self, idx, gstart, n, min_n):
        """coord_engine axis tape: the last min(n, available) closes, at least ``min_n``."""
        cnt = np.where(idx >= gstart, idx - gstart + 1, 0)
        take = np.minimum(cnt, n)
        ok = (idx >= 0) & (take >= min_n) & (take > 0)
        hi = np.where(ok, idx + 1, 0)
        lo = np.where(ok, idx + 1 - take, 0)
        mean = (self.cs[hi] - self.cs[lo]) / np.where(ok, take, 1)
        return np.round(1.0 + 0.1 * mean, 4), ok

    def all_losses(self, idx, gstart, n):
        """The last ``n`` closes are all cost-net losses (coord_engine pause)."""
        cnt = np.where(idx >= gstart, idx - gstart + 1, 0)
        full = (idx >= 0) & (cnt >= n)
        hi = np.where(full, idx + 1, 0)
        lo = np.where(full, idx + 1 - n, 0)
        return full & ((self.cl[hi] - self.cl[lo]) >= n)


def load_cache(cache: str, symbols: Sequence[str]) -> Dict[str, Any]:
    out = {}
    for s in symbols:
        with open(os.path.join(cache, f"{s}.pkl"), "rb") as f:
            out[s] = pickle.load(f)
    return out


# --------------------------------------------------------------------------
# Stage C: account simulation
# --------------------------------------------------------------------------

class Sizer:
    """pulse_trader.Pulse sizing methods bound to a light state object."""

    def __init__(self, contracts: Dict[str, Any], overlay: Dict[str, Any], leverage: Optional[int]):
        _engine_path()
        import pulse_trader as pt
        from coord_engine import Coordinator
        self.pt = pt
        self.contracts = contracts
        self.lev_max: Dict[str, int] = {}
        self.lev_map: Dict[str, int] = {}
        if leverage:
            for s in contracts:
                self.lev_max[s] = int(leverage)
        self.volume_factor = max(0.05, min(10.0, float(overlay.get("volumeFactor") or 0.1)))
        self.order_sizing = "factor" if overlay.get("orderSizing") == "factor" else pt.ORDER_SIZING_DEFAULT
        self.sl_auto_leverage = overlay.get("slAutoLeverage", True) is not False
        self.margin_cap_pct = max(0.0, min(1.0, float(overlay.get("marginCapPct", 0.5))))
        pt.TARGET_NOTIONAL = max(0.2, min(500.0, float(overlay.get("targetNotional") or pt.TARGET_NOTIONAL)))
        self.vol1h: Dict[str, float] = {}
        self.open: Dict[int, Any] = {}
        self.available = 0.0
        self.equity = 0.0  # the simulator passes the already-capped free margin as ``available``
        self.coord = Coordinator()
        self.coord.load({}, overlay)
        P = pt.Pulse
        # Exchange-accepted SL floor, same helper as the live entry path.
        self.sl_min = max(pt.SL_MIN_PCT / 100.0, float(overlay.get("slMinPct") or pt.SL_MIN_PCT) / 100.0)
        self.sl_max = max(self.sl_min, float(overlay.get("slMaxPct") or 3.0) / 100.0)
        self.venue_sl_ticks = float(overlay.get("venueSlTicks") or pt.VENUE_SL_TICKS)
        self.sl_learned: Dict[str, float] = {}
        for name in ("round_qty_up", "min_order_qty", "raise_to_min_qty", "leverage_for", "sized_notional",
                     "avail_notional", "margin_headroom", "size_qty", "venue_sl_min"):
            setattr(self, name, getattr(P, name).__get__(self))


def load_contracts(path: str, symbols: Sequence[str], fetch: bool = True) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    _engine_path()
    import pulse_trader as pt
    meta: Dict[str, Any] = {}
    blob = None
    if os.path.exists(path):
        blob = json.load(open(path))
    elif fetch:
        import urllib.request
        with urllib.request.urlopen(VST_CONTRACTS_URL, timeout=20) as r:
            data = json.loads(r.read().decode()).get("data") or []
        blob = {"vst": {"source": VST_CONTRACTS_URL, "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "data": [c for c in data if c.get("symbol") in set(symbols)]}}
        json.dump(blob, open(path, "w"), indent=1)
    rows = (blob.get("vst") or blob.get("live") or {}).get("data") or []
    meta = {k: {"source": v.get("source"), "fetchedAt": v.get("fetchedAt"), "n": len(v.get("data") or [])}
            for k, v in blob.items()}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real = pt.urllib.request.urlopen
    pt.urllib.request.urlopen = lambda *a, **k: _Resp(json.dumps({"data": rows}).encode())
    try:
        contracts = pt.load_contracts(set(symbols))  # engine parser, fed from the cached public response
    finally:
        pt.urllib.request.urlopen = real
    return contracts, meta


def strategy_key(pack: str, kind: str) -> str:
    return f"{pack}/{'trailing' if kind == 'trail' else 'normal'}"


def ddt_max_s_fast(t, sym, moves, cost_frac: float) -> float:
    """``drawdown_time_by_symbol(rows, ordered=True)["maxS"]`` for rows
    ``{t, symbol, pnl_pct}`` without building the row dicts (that call was
    ~75% of the gating stage). Same episode rule: per symbol, an episode opens
    when cost-net equity falls below its peak and closes at the first row that
    recovers it; an open one runs to that symbol's last row. Rows with t <= 0
    are ignored, exactly like the engine."""
    per: Dict[int, List[Tuple[float, float]]] = {}
    for ti, si, mv in zip(t, sym, moves):
        if ti > 0:
            per.setdefault(int(si), []).append((float(ti), float(mv)))
    best = 0.0
    for seq in per.values():
        eq = peak = 0.0
        started = None
        for ti, mv in seq:
            eq += mv - cost_frac
            if eq >= peak - 1e-12:
                if started is not None:
                    best = max(best, ti - started)
                    started = None
                if eq > peak:
                    peak = eq
            elif started is None:
                started = ti
        if started is not None:
            best = max(best, seq[-1][0] - started)
    return round(best, 1)


def build_candidates(caches, catalog, symbols, sim_start, sim_end, book, gated: bool, ddt_cache: Dict,
                     chunk: int = 2_000_000, drop_core: bool = True,
                     children: Optional[List[Tuple[str, int]]] = None) -> Dict[str, Any]:
    """Core Set lots eligible in [sim_start, sim_end), with walk-forward gate results."""
    _engine_path()
    from set_engine import drawdown_time_by_symbol
    cost = float(book.cost_pct)
    floors = {k: float(v) for k, v in book.stage_min_pf.items()}
    need = int(book.eval_need())
    base_n, main_n, real_n = book._stage_window_ns()
    micro_on = bool(getattr(book, "micro_enabled", False))
    micro_floor = float(getattr(book, "micro_min_pf", 0.0) or 0.0)
    strict = bool(getattr(book, "strict_gate", True))
    # candidates (entries inside the window) and closed evidence, per symbol
    cand_parts = defaultdict(list)
    ev_parts = defaultdict(list)
    for si, s in enumerate(symbols):
        c = caches[s]["core"]
        sel = (c["entry"] >= sim_start) & (c["entry"] < sim_end)
        for k in ("uid", "side", "entry", "exit", "raw", "reason"):
            cand_parts[k].append(c[k][sel])
        cand_parts["sym"].append(np.full(int(sel.sum()), si, dtype=np.int16))
        cl = c["exit"] >= 0
        ev_parts["group"].append(c["uid"][cl].astype(np.int32) * 2 + (c["side"][cl] > 0))
        ev_parts["exit"].append(c["exit"][cl])
        ev_parts["raw"].append(c["raw"][cl])
        ev_parts["sym"].append(np.full(int(cl.sum()), si, dtype=np.int16))
        if drop_core:
            caches[s]["core"] = None  # evidence now lives in ev_parts; keeps peak memory down
    cands = {k: np.concatenate(v) for k, v in cand_parts.items()}
    del cand_parts
    cat = {}
    for k in ("group", "exit", "raw", "sym"):
        cat[k] = np.concatenate(ev_parts.pop(k))
    ev = Evidence(cat.pop("group"), cat.pop("exit"), cat.pop("raw"), cost, tiebreak=cat.pop("sym"))
    n_evidence = len(ev.exit)
    m = len(cands["uid"])
    out = {k: np.zeros(m, dtype=bool) for k in ("base_ok", "main_ok", "real_ok", "micro_ok", "dd_ok", "admitted")}
    out["r30"] = np.full(m, np.nan)
    out["cnt"] = np.zeros(m, dtype=np.int32)
    children = list(children if children is not None else axis_children())
    # per-axis summary (any child of that axis qualifies) + one mask per axis:count
    axes = {a: np.zeros(m, dtype=bool) for a in AXIS_NAMES}
    axes.update({f"{a}:{c}": np.zeros(m, dtype=bool) for a, c in children})
    g_all = cands["uid"].astype(np.int64) * 2 + (cands["side"] > 0)
    for a0 in range(0, m, chunk):
        a1 = min(m, a0 + chunk)
        g = g_all[a0:a1]
        idx, gstart = ev.lookup(g, cands["entry"][a0:a1])
        fl = stage_flags(ev, idx, gstart, need, (base_n, main_n, real_n), floors)
        r30, ok30, r5, ok5, r3, ok3 = fl["r30"], fl["ok30"], fl["r5"], fl["ok5"], fl["r3"], fl["ok3"]
        cnt = np.where(idx >= 0, idx - gstart + 1, 0)
        base_ok = fl["base_ok"]
        out["base_ok"][a0:a1] = base_ok
        out["main_ok"][a0:a1] = fl["main_ok"]
        out["real_ok"][a0:a1] = fl["real_ok"]
        if micro_on:
            # Micro tier (set_engine._stage_qualification): positive at the
            # Micro floor but below the shared floor; strict lanes also need
            # Main/Real at that floor. Executes at venue-minimum size.
            micro_ok = (cnt >= need) & ok30 & clears_vec(r30, micro_floor) & ~base_ok
            if strict:
                micro_ok &= ok5 & clears_vec(r5, micro_floor) & ok3 & clears_vec(r3, micro_floor)
            out["micro_ok"][a0:a1] = micro_ok
        out["r30"][a0:a1] = np.where(ok30, r30, np.nan)
        out["cnt"][a0:a1] = cnt
        # coordination axes = coord_engine.axis_variants on the parent's own
        # direction tape: prev = the window before the last c closes (needs 2c),
        # last/cont/pause = the last min(c, available) closes (>= min(3, c)),
        # pause also off while the last c closes are all cost-net losses.
        for axis, c in children:
            if axis == "prev":
                rr, ok = ev.window_ratio(idx, gstart, c, c)
            else:
                rr, ok = ev.partial_ratio(idx, gstart, c, min(3, c))
            q = ok & clears_vec(rr, floors["base"])
            if axis == "pause":
                q &= ~ev.all_losses(idx, gstart, c)
            axes[f"{axis}:{c}"][a0:a1] = q
            axes[axis][a0:a1] |= q
    from position_cost import POSITION_COST_PCT_DEFAULT, cost_as_frac
    dd_cost_frac = cost_as_frac(POSITION_COST_PCT_DEFAULT)
    # DD-time gate: engine drawdown_time_by_symbol on the Set x side tape over
    # the same window the Base PF validates (SetBook.ddt_window), refreshed at
    # each simulated hour.
    ddt_n = int(getattr(book, "ddt_window", lambda: 96)()) if callable(getattr(book, "ddt_window", None)) else 96
    hour = (cands["entry"] - sim_start) // 60
    dd_ok = np.ones(m, dtype=bool)
    need_dd = np.flatnonzero(out["real_ok"] | out["micro_ok"])
    ddt_values = []
    if len(need_dd):
        pairs = np.unique(g_all[need_dd] * 1000 + hour[need_dd])
        sym_names = list(symbols)
        pg, ph = pairs // 1000, pairs % 1000
        i_end, gs = ev.lookup(pg, sim_start + ph * 60)
        for key, ie, g0 in zip(pairs.tolist(), i_end.tolist(), gs.tolist()):
            if key in ddt_cache:
                continue
            if ie < 0:
                ddt_cache[key] = 0.0
                continue
            lo_i = max(g0, ie - (ddt_n - 1))
            if ie - g0 + 1 < book.eval_need():
                # too few prior closes for a DDT: valid until the sample exists
                ddt_cache[key] = 0.0
                continue
            ddt_cache[key] = ddt_max_s_fast(ev.exit[lo_i:ie + 1].astype(np.float64) * BAR, ev.tiebreak[lo_i:ie + 1],
                                           ev.moves[lo_i:ie + 1], dd_cost_frac)
        vals = np.array([ddt_cache[k] for k in (g_all[need_dd] * 1000 + hour[need_dd]).tolist()])
        dd_ok[need_dd] = vals <= float(book.max_dd_s) + 1e-9
        ddt_values = vals
    out["dd_ok"] = dd_ok
    out["admitted"] = (out["real_ok"] | out["micro_ok"]) & dd_ok
    cands.update(out)
    cands["axes"] = axes
    cands["n_evidence"] = n_evidence
    cands["ddt_max_s"] = float(np.max(ddt_values)) if len(ddt_values) else 0.0
    cands["ddt_blocked"] = int((~dd_ok).sum())
    # evidence depth at the window start (pooled closes per Set x side before sim_start)
    ug = ev.ug
    k0 = np.searchsorted(ev.key, ug * 1_000_000 + sim_start, side="left")
    ends = np.r_[ev.first[1:], len(ev.key)]
    depth = np.minimum(k0, ends) - ev.first
    cands["evidence_depth"] = dict(min=int(depth.min()), p5=float(np.percentile(depth, 5)), median=float(np.median(depth)))
    return cands


def apply_factors(cands, catalog, strategies: str, axis_filter: str, no_micro: bool) -> Dict[str, Any]:
    """Restrict the admitted candidates by the factor options (sim-only filters)."""
    want = {s.strip().lower() for s in str(strategies or "").split(",") if s.strip()} or {"normal", "trailing"}
    trail = np.array([bool(r["trail"]) for r in catalog], dtype=bool)[cands["uid"]]
    keep = np.zeros(len(cands["uid"]), dtype=bool)
    if "normal" in want:
        keep |= ~trail
    if "trailing" in want:
        keep |= trail
    axis_filter = str(axis_filter or "none").strip().lower()
    if axis_filter == "any":
        anym = np.zeros(len(keep), dtype=bool)
        for a in AXIS_NAMES:
            anym |= cands["axes"][a]
        keep &= anym
    elif axis_filter != "none":
        if axis_filter not in cands["axes"]:
            raise SystemExit(f"--axis-filter {axis_filter!r}: unknown axis (have {sorted(cands['axes'])[:8]}...)")
        keep &= cands["axes"][axis_filter]
    if no_micro:
        keep &= cands["real_ok"]
    before = int(cands["admitted"].sum())
    cands["admitted"] = cands["admitted"] & keep
    return dict(strategies=sorted(want), axisFilter=axis_filter, noMicro=bool(no_micro),
                admittedBefore=before, admittedAfter=int(cands["admitted"].sum()))


def kind_gate(caches, symbols, book, sim_start, sim_end):
    """SetBook.indication_ok(kind, side) walk-forward: kind tape Base window >= floor."""
    cost = float(book.cost_pct)
    need = int(book.eval_need())
    rows = [r for s in symbols for r in caches[s]["kinds"]]
    if not rows:
        return None
    kind = np.array([IND_KINDS.index(r[0]) for r in rows], dtype=np.int64)
    side = np.array([r[1] for r in rows], dtype=np.int64)
    ex = np.array([r[3] for r in rows], dtype=np.int64)
    mv = np.array([r[4] for r in rows], dtype=np.float64)
    grp = kind * 2 + (side > 0)
    ev = Evidence(grp, ex, mv, cost)
    bars = np.arange(sim_start, sim_end)
    table = np.zeros((len(IND_KINDS), 2, len(bars)), dtype=bool)
    for k in range(len(IND_KINDS)):
        for si, sd in enumerate((-1, 1)):
            gg = np.full(len(bars), k * 2 + (sd > 0))
            idx, gs = ev.lookup(gg, bars)
            # SetBook.ind_stats: last-pf_n PF over the available closes, n >= need
            r, ok = ev.partial_ratio(idx, gs, book.pf_n, need)
            table[k, si] = ok & clears_vec(r, float(book.min_pf))
    return table


def kind_lane_lots(caches, symbols, book, sim_start, sim_end, kind_table, gated: bool):
    """Indication kind lanes traded on their own (pulse_trader pick_entries:
    one lane per kind x range config x side), admitted walk-forward by
    SetBook.indication_ok: the config's own last-N cost-PF once it has enough
    samples, else the pooled kind x side gate."""
    cost = float(book.cost_pct)
    need = int(book.eval_need())
    pf_n = int(book.pf_n)
    floor = float(book.min_pf)
    rows = []
    for si, s in enumerate(symbols):
        for r in caches[s]["kinds"]:
            cfg = r[5] if len(r) > 5 else ""
            why = r[6] if len(r) > 6 else ""
            rows.append(dict(symbol=s, si=si, kind=r[0], side=int(r[1]), _entry=int(r[2]), _exit=int(r[3]),
                             pnl_pct=float(r[4]), config=cfg, reason=why))
    rows.sort(key=lambda r: (r["_exit"], r["symbol"]))
    tapes: Dict[Tuple[str, str, int], List[Tuple[int, float]]] = defaultdict(list)
    for r in rows:
        if r["config"]:
            tapes[(r["kind"], r["config"], r["side"])].append((r["_exit"], r["pnl_pct"]))
    times = {k: [t for t, _ in v] for k, v in tapes.items()}
    out = []
    for r in rows:
        if not (sim_start <= r["_entry"] < sim_end) or r["_exit"] < 0:
            continue
        ok, why = True, ""
        if gated:
            key = (r["kind"], r["config"], r["side"])
            decided = False
            if r["config"] and key in tapes:
                k = bisect.bisect_left(times[key], r["_entry"])
                past = [m for _, m in tapes[key][max(0, k - pf_n):k]]
                if k >= need and past:
                    pf = cost_pf_ratio(past, cost)
                    ok = bool(clears_vec(np.array([pf]), floor)[0])
                    why, decided = f"config last{pf_n} {pf:.3f}", True
            if not decided:
                if kind_table is None or r["kind"] not in IND_KINDS:
                    ok, why = False, "no kind evidence"
                else:
                    ok = bool(kind_table[IND_KINDS.index(r["kind"]), 1 if r["side"] > 0 else 0, r["_entry"] - sim_start])
                    why = "kind pooled gate"
        r["_ok"], r["_why"] = ok, why
        out.append(r)
    return out


def strat_lots(caches, symbols, book, sim_start, sim_end, gated: bool):
    """Block / DCA lane trades in the window with the engine's own add-on gates."""
    _engine_path()
    from position_cost import is_positive_pf, clears_pf
    cost = float(book.cost_pct)
    rows = []
    for s in symbols:
        for r in caches[s]["strat"]:
            d = dict(r)
            d["symbol"] = s
            rows.append(d)
    rows.sort(key=lambda r: (r["_exit"], r["symbol"]))
    # Evidence tapes: Block keyed like score_block_main (count, ind_kind, set_id),
    # DCA keyed by lane/pack/side (DcaBook.score on its own closes).
    evid: Dict[Any, List[Tuple[int, float]]] = defaultdict(list)
    for r in rows:
        if r["lane"].startswith("block"):
            key = ("block", int(r.get("block_count") or 1), str(r.get("ind_kind") or ""), str(r.get("set_id") or ""))
        else:
            key = ("dca", str(r.get("pack") or ""), str(r["side"])[:1])
        r["_key"] = key
        evid[key].append((int(r["_exit"]), float(r["pnl_pct"])))
    bnd = max(5, min(75, int(getattr(book, "block_eval_pos", 50) or 50)))
    floor = float(book.real_min_pf)
    times = {k: [t for t, _ in v] for k, v in evid.items()}
    out = []
    for r in rows:
        if not (sim_start <= r["_entry"] < sim_end):
            continue
        tape = evid[r["_key"]]
        k = bisect.bisect_left(times[r["_key"]], r["_entry"])
        past = [m for _, m in tape[max(0, k - 60):k]]
        ok = True
        why = ""
        if gated:
            if r["_key"][0] == "block":
                if len(past) >= bnd:
                    pf = cost_pf_ratio(past[-bnd:], cost)
                    ok = bool(is_positive_pf(pf) and clears_pf(pf, floor))
                    why = f"real-overall last{bnd} {pf:.3f}"
                else:
                    why = "insufficient-sample (engine: valid)"
            else:
                pf_n, deact_n = 15, 25
                last25 = past[-deact_n:]
                avg_r = sum((m * 100 - cost) / cost for m in last25) / len(last25) if last25 else 0.0
                if len(last25) >= deact_n and avg_r < 0:
                    ok, why = False, f"last25 avgR {avg_r:.2f}<0"
                elif len(past) >= pf_n and not clears_pf(cost_pf_ratio(past[-pf_n:], cost), float(book.min_pf)):
                    ok, why = False, "last15 PF below floor"
        r["_ok"] = ok
        r["_why"] = why
        out.append(r)
    return out


def kind_ok_mask(cands, catalog, kind_table, bars_by_sym, sim_start) -> np.ndarray:
    """SetBook.indication_ok for indications-pack candidates (True for general)."""
    is_ind_uid = np.array([c["pack"] == "indications" for c in catalog], dtype=bool)
    ok = np.ones(len(cands["uid"]), dtype=bool)
    if kind_table is None:
        return ok
    sel = np.flatnonzero(is_ind_uid[cands["uid"]])
    masks = np.array([bars_by_sym["ind_mask"][si][en] for si, en in zip(cands["sym"][sel].tolist(), cands["entry"][sel].tolist())],
                     dtype=np.int64)
    si = (cands["side"][sel] > 0).astype(np.int64)
    tt = (cands["entry"][sel] - sim_start).astype(np.int64)
    okk = np.zeros(len(sel), dtype=bool)
    for k in range(len(IND_KINDS)):
        okk |= (((masks >> k) & 1) == 1) & kind_table[k, si, tt]
    ok[sel] = okk
    return ok


def simulate(run: str, cands: Dict[str, Any], strat: List[Dict[str, Any]], kind_table, catalog, symbols, bars_by_sym,
             sim_start: int, sim_end: int, start_s: int, book, sizer: Sizer, start_equity: float, gated: bool,
             live_neg: bool, mmr_factor: float = 0.5, eq_min: float = 0.20, liq_mode: str = "intrabar",
             kind_lanes: Optional[List[Dict[str, Any]]] = None, max_open: int = 100,
             dd_pause_pct: float = 0.0, side_cap: int = 0) -> Dict[str, Any]:
    _engine_path()
    from set_engine import drawdown_time_by_symbol
    cost_pct = float(book.cost_pct)
    # marginCapPct: used margin may not exceed this share of equity (0 = uncapped)
    margin_cap = float(getattr(sizer, "margin_cap_pct", 0.0) or 0.0)
    margin_cap = margin_cap if 0.0 < margin_cap < 1.0 else 1.0
    acct = Account(start_equity, cost_pct if FEE_PCT is None else float(FEE_PCT))
    ctrl = ControlOrders()
    dd = DrawdownTracker(start_equity)
    T = sim_end - sim_start
    H = T // 60
    nsym = len(symbols)
    # --- candidate lots per entry bar (already expanded by Set multiplicity) ---
    mult = np.array([c["mult"] for c in catalog], dtype=np.int64)
    packs = [c["pack"] for c in catalog]
    kinds = [c["kind"] for c in catalog]
    use = (cands["admitted"] if gated else np.ones(len(cands["uid"]), dtype=bool)).copy()
    is_ind_uid = np.array([p == "indications" for p in packs], dtype=bool)
    ind_block = 0
    if gated and kind_table is not None:
        kok = kind_ok_mask(cands, catalog, kind_table, bars_by_sym, sim_start)
        ind_block = int((use & ~kok).sum())
        use &= kok
    idxs = np.flatnonzero(use)
    idxs = idxs[np.argsort(cands["entry"][idxs], kind="stable")]
    idx_entry = cands["entry"][idxs]
    sig_key_all = (cands["sym"].astype(np.int64) * 4 + (cands["side"] > 0) * 2 + is_ind_uid[cands["uid"]])
    prio_all = -np.nan_to_num(cands["r30"], nan=0.0)
    strat_by_bar: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for r in strat:
        if (not gated) or r["_ok"]:
            strat_by_bar[int(r["_entry"])].append(r)
    # Micro cap (pulse_trader.place): Micro lots <= microMaxShare x maxOpen.
    micro_cap = 0
    if gated and bool(getattr(book, "micro_enabled", False)):
        share = float(getattr(book, "micro_max_share", 0.05) or 0.0)
        micro_cap = max(1, int(max_open * share)) if share > 0 else 0
    micro_ids: set = set()
    is_micro_c = (cands["micro_ok"] & ~cands["real_ok"]) if "micro_ok" in cands else np.zeros(len(cands["uid"]), bool)
    lanes_by_bar: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for r in kind_lanes or []:
        if (not gated) or r["_ok"]:
            lanes_by_bar[int(r["_entry"])].append(r)
    close = bars_by_sym["close"]
    high = bars_by_sym["high"]
    low = bars_by_sym["low"]
    exits: Dict[int, List[Tuple[int, float, str]]] = defaultdict(list)
    adds: Dict[int, List[Tuple[int, float, float]]] = defaultdict(list)
    live_tape: Dict[Tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=25))
    live_seen: set = set()
    liquidations: List[Dict[str, Any]] = []
    halted_minutes = 0
    dd_pause = max(0.0, float(dd_pause_pct or 0.0)) / 100.0
    eq_peak, dd_paused, dd_pause_minutes = float(start_equity), False, 0
    lot_seq = 0
    closed_rows: List[Dict[str, Any]] = []
    hours = [dict(hour=h, opened_lots=0, opened_groups=0, entry_orders=0, block_orders=0, dca_entry_orders=0,
                  dca_add_orders=0, close_fills={}, skipped_margin=0, skipped_min=0, skipped_live_neg=0,
                  skipped_no_parent=0, max_margin=0.0, max_margin_pct=0.0, max_lots=0, max_groups=0,
                  max_dd_pct=0.0, max_dd_intrabar_pct=0.0, ctrl_before=None) for h in range(H)]
    eq_curve = []
    max_lots = max_groups = 0
    sym_names = symbols
    for t in range(sim_start, sim_end):
        ti = t - sim_start
        h = ti // 60
        hr = hours[h]
        if ti % 60 == 0:
            hr["ctrl_before"] = dict(ctrl.as_dict())
            # vol1h = last completed 1h candle range (pulse_trader vol1h refresh)
            for k, s in enumerate(sym_names):
                w0 = max(0, t - 60)
                hi = max(high[k][w0:t]); lo_ = min(low[k][w0:t]); last = close[k][t - 1]
                sizer.vol1h[s] = (hi - lo_) / last * 100.0 if last > 0 else 0.0
        px = {s: close[k][t] for k, s in enumerate(sym_names)}
        hi_px = {s: high[k][t] for k, s in enumerate(sym_names)}
        lo_px = {s: low[k][t] for k, s in enumerate(sym_names)}
        lev_of = {s: sizer.leverage_for(sizer.contracts[s]) for s in sym_names}
        min_margin_of = {s: sizer.min_order_qty(sizer.contracts[s], px[s]) * px[s] / lev_of[s] for s in sym_names}
        global_min_margin = min(min_margin_of.values())
        mmr_of = {s: mmr_factor / lev_of[s] for s in sym_names}
        before = acct.groups()
        worst = acct.cash + acct.worst_unrealized(hi_px, lo_px)
        changed = set()
        # 0) exchange liquidation (cross margin): intrabar worst equity at or
        # below the maintenance margin closes the whole book
        liq_eq = worst if liq_mode == "intrabar" else acct.equity(px)
        if acct.lots and liq_eq <= acct.maintenance(mmr_of):
            liq = acct.liquidate(hi_px, lo_px, mmr_of) if liq_mode == "intrabar" else acct.liquidate(px, px, mmr_of)
            liquidations.append(dict(t=t, utc=time.strftime("%H:%M", time.gmtime(start_s + t * BAR)), lots=len(liq),
                                     notional=round(sum(l["notional"] for l in liq), 4), equityAfter=round(acct.cash, 6)))
            hr["liquidations"] = hr.get("liquidations", 0) + 1
            micro_ids.clear()
            for lot in liq:
                sizer.open.pop(lot["id"], None)
                changed.add((lot["symbol"], lot["side"]))
                hr["close_fills"]["liquidation"] = hr["close_fills"].get("liquidation", 0) + 1
                closed_rows.append(dict(t=float(start_s + t * BAR), symbol=lot["symbol"], side=lot["side"], strategy=lot["strategy"],
                                        pnl_pct=(lot["exit"] - lot["entry"]) / lot["entry"] * lot["side"], gross=lot["gross"],
                                        net=lot["net"], notional=lot["notional"], hour=h, reason="liquidation",
                                        axes=lot.get("axes") or {}, kinds=lot.get("kinds") or [], uid=lot.get("uid", -1)))
            worst = min(worst, acct.cash)
        # 1) DCA adds due this bar (lane fills), then exits
        for lot_id, apx, aqty in adds.pop(t, []):
            lot = acct.lots.get(lot_id)
            if lot is None:
                continue
            c = sizer.contracts[lot["symbol"]]
            q = sizer.raise_to_min_qty(c, apx, aqty * lot["unit_qty"])
            lev = lev_of[lot["symbol"]]
            avail = max(0.0, acct.equity(px) * margin_cap - acct.used_margin)
            if q * apx / lev > avail * 0.95:
                hr["skipped_margin"] += 1
                lot["skipped_adds"] = lot.get("skipped_adds", 0) + 1
                continue
            acct.add_fill(lot_id, q, apx, lev)
            hr["dca_add_orders"] += 1
            changed.add((lot["symbol"], lot["side"]))
        for lot_id, xpx, why in exits.pop(t, []):
            if lot_id not in acct.lots:
                continue
            lot = acct.close_lot(lot_id, xpx)
            sizer.open.pop(lot_id, None)
            micro_ids.discard(lot_id)
            changed.add((lot["symbol"], lot["side"]))
            hr["close_fills"][why] = hr["close_fills"].get(why, 0) + 1
            move = (xpx - lot["entry"]) / lot["entry"] * lot["side"]
            row = dict(t=float(start_s + t * BAR), symbol=lot["symbol"], side=lot["side"], strategy=lot["strategy"],
                       pnl_pct=move, gross=lot["gross"], net=lot["net"], notional=lot["notional"], hour=h,
                       reason=why, axes=lot.get("axes") or {}, kinds=lot.get("kinds") or [], uid=lot.get("uid", -1))
            closed_rows.append(row)
            ci = lot.get("ci", -1)
            if lot.get("uid", -1) >= 0 and ci not in live_seen:
                # one close per Set trade (duplicate-Set lots share the tape)
                live_seen.add(ci)
                live_tape[(lot["uid"], lot["side"])].append(move - cost_pct / 100.0)
        # 2) entries at bar close, EntryMatrix order: round-robin over signals
        # (symbol, side, pack); inside a signal entry_sets() order = highest
        # last-30 cost-PF first. A Set with multiplicity m occupies m ranks.
        avail = max(0.0, acct.equity(px) * margin_cap - acct.used_margin)
        eq_now = acct.equity(px)
        halted = eq_now < eq_min  # pulse_trader EQ_MIN halt: no new entries
        # ddPausePct (pulse_trader drawdown pause): no new entries while equity
        # sits ddPausePct below its running peak; resume at 60% of it.
        if dd_pause > 0:
            eq_peak = max(eq_peak, eq_now)
            dd_now = (eq_peak - eq_now) / eq_peak if eq_peak > 0 else 0.0
            if dd_paused and dd_now < dd_pause * 0.6:
                dd_paused = False
            elif not dd_paused and dd_now >= dd_pause:
                dd_paused = True
            if dd_paused and not halted:
                halted = True
                dd_pause_minutes += 1
        if halted:
            halted_minutes += 1
        a0 = int(np.searchsorted(idx_entry, t, side="left"))
        a1 = int(np.searchsorted(idx_entry, t, side="right"))
        cand = idxs[a0:a1]
        if len(cand):
            sk = sig_key_all[cand]
            o = np.lexsort((cands["uid"][cand], prio_all[cand], sk))
            cand = cand[o]
            sk = sk[o]
            m_c = mult[cands["uid"][cand]]
            csum = np.cumsum(m_c)
            first_of_sig = np.r_[0, np.flatnonzero(sk[1:] != sk[:-1]) + 1]
            base = np.repeat(csum[first_of_sig] - m_c[first_of_sig], np.diff(np.r_[first_of_sig, len(cand)]))
            rank0 = csum - m_c - base
            o2 = np.lexsort((sk, rank0))
            cand = cand[o2]
            m_c = m_c[o2]
            remaining = int(m_c.sum())
        else:
            remaining = 0
        for j, i in enumerate(cand.tolist() if len(cand) else []):
            m_i = int(m_c[j])
            remaining -= m_i
            if halted:
                hr["skipped_halt"] = hr.get("skipped_halt", 0) + m_i + remaining
                break
            if avail * 0.95 < global_min_margin:
                hr["skipped_margin"] += m_i + remaining
                break
            s_i, side = int(cands["sym"][i]), int(cands["side"][i])
            s = sym_names[s_i]
            u = int(cands["uid"][i])
            if gated and live_neg:
                tape = live_tape[(u, side)]
                if len(tape) >= 25 and sum(tape) < 0:
                    hr["skipped_live_neg"] += m_i
                    continue
            c = sizer.contracts[s]
            p = px[s]
            lev = lev_of[s]
            ex = int(cands["exit"][i])
            meta_axes = {a: bool(cands["axes"][a][i]) for a in AXIS_WINDOWS}
            meta_kinds = []
            if is_ind_uid[u]:
                mk = int(bars_by_sym["ind_mask"][s_i][t])
                meta_kinds = [IND_KINDS[k] for k in range(len(IND_KINDS)) if (mk >> k) & 1]
            micro_c = bool(gated and is_micro_c[i])
            for _dup in range(m_i):
                if side_cap and int((acct.agg.get((s, side)) or (0, 0, 0))[2]) >= side_cap:
                    hr["skipped_side_cap"] = hr.get("skipped_side_cap", 0) + 1
                    continue
                if micro_c and len(micro_ids) >= micro_cap:
                    hr["skipped_micro_cap"] = hr.get("skipped_micro_cap", 0) + 1
                    continue
                if min_margin_of[s] > avail * 0.95:
                    hr["skipped_margin"] += 1
                    continue
                sizer.available = avail
                # Live place(): a Micro lot is one venue-minimum lot, not a
                # factor-sized order.
                q = sizer.raise_to_min_qty(c, p, sizer.min_order_qty(c, p)) if micro_c else sizer.size_qty(c, p)
                if q <= 0:
                    hr["skipped_min"] += 1
                    continue
                lot_seq += 1
                g_before = acct.agg.get((s, side))
                acct.open_lot(lot_seq, s, side, q, p, lev, uid=u, ci=int(i), strategy=strategy_key(packs[u], kinds[u]),
                              unit_qty=q, axes=meta_axes, kinds=meta_kinds)
                if micro_c:
                    micro_ids.add(lot_seq)
                sizer.open[lot_seq] = 1
                avail = max(0.0, acct.equity(px) * margin_cap - acct.used_margin)
                hr["opened_lots"] += 1
                hr["entry_orders"] += 1
                if not g_before or g_before[2] <= 0:
                    hr["opened_groups"] += 1
                changed.add((s, side))
                if ex >= 0:
                    xpx = p * (1.0 + float(cands["raw"][i]) * side)
                    exits[ex].append((lot_seq, xpx, COST_REASONS[int(cands["reason"][i])]))
        # Block / DCA add-on lanes (need an open parent group)
        for r in (strat_by_bar.get(t, []) if not halted else []):
            s = r["symbol"]
            side = 1 if str(r["side"])[:1] == "L" else -1
            if acct.groups().get((s, side), 0) <= 0:
                hr["skipped_no_parent"] += 1
                continue
            c = sizer.contracts[s]
            p = px[s]
            lev = lev_of[s]
            sizer.available = avail
            # Live sizes an add from its parent's stored quantity, not from a
            # fresh unit at add time.
            parents = [lot for lot in acct.lots.values()
                       if lot["symbol"] == s and lot["side"] == side and lot.get("strategy") not in ("block", "dca")]
            unit = float(parents[0].get("unit_qty") or 0.0) if parents else sizer.size_qty(c, p)
            if unit <= 0:
                hr["skipped_margin" if avail <= 0 else "skipped_min"] += 1
                continue
            is_block = r["lane"].startswith("block")
            qty0 = unit * (float(r["_qty"]) / float(r["_parent"]) if is_block else 1.0)
            if is_block:
                qty0 = sizer.raise_to_min_qty(c, p, qty0)
            if qty0 * p / lev > avail * 0.95:
                hr["skipped_margin"] += 1
                continue
            lot_seq += 1
            strat_name = "block" if is_block else "dca"
            acct.open_lot(lot_seq, s, side, qty0, p, lev, uid=-1, strategy=strat_name, unit_qty=unit,
                          kinds=[r.get("ind_kind")] if r.get("ind_kind") else [])
            # Add-ons are not positions: live size_mult counts open positions.
            avail = max(0.0, acct.equity(px) * margin_cap - acct.used_margin)
            hr["opened_lots"] += 1
            hr["block_orders" if is_block else "dca_entry_orders"] += 1
            changed.add((s, side))
            # exit price of the lane: move from its final average entry
            qratio = float(r["_qty"]) / float(r["_parent"])
            move_avg = float(r["pnl_pct"]) / (qratio if qratio > 0 else 1.0)
            xpx = float(r["_avg"]) * (1.0 + move_avg * side)
            # the lane's entry price equals this bar's close; scale if data differs
            xpx *= p / float(r.get("_entry_px", p) or p)
            for (ab, apx, aq) in r.get("_log") or []:
                if not is_block:
                    adds[int(ab)].append((lot_seq, float(apx), float(aq)))
            exits[int(r["_exit"])].append((lot_seq, xpx, str(r.get("reason") or "x").split(":")[-1]))
        # Indication kind lanes (own entries, venue-minimum sized like any lot)
        for r in (lanes_by_bar.get(t, []) if not halted else []):
            s, side = r["symbol"], int(r["side"])
            if side_cap and int((acct.agg.get((s, side)) or (0, 0, 0))[2]) >= side_cap:
                hr["skipped_side_cap"] = hr.get("skipped_side_cap", 0) + 1
                continue
            c = sizer.contracts[s]
            p = px[s]
            lev = lev_of[s]
            if min_margin_of[s] > avail * 0.95:
                hr["skipped_margin"] += 1
                continue
            sizer.available = avail
            q = sizer.size_qty(c, p)
            if q <= 0:
                hr["skipped_min"] += 1
                continue
            lot_seq += 1
            g_before = acct.agg.get((s, side))
            acct.open_lot(lot_seq, s, side, q, p, lev, uid=-1, strategy="kind:" + r["kind"], unit_qty=q, kinds=[r["kind"]])
            sizer.open[lot_seq] = 1
            avail = max(0.0, acct.equity(px) * margin_cap - acct.used_margin)
            hr["opened_lots"] += 1
            hr["entry_orders"] += 1
            hr["kind_lane_orders"] = hr.get("kind_lane_orders", 0) + 1
            if not g_before or g_before[2] <= 0:
                hr["opened_groups"] += 1
            changed.add((s, side))
            xpx = p * (1.0 + float(r["pnl_pct"]) * side)
            why = str(r.get("reason") or "x").split(":")[-1]
            exits[int(r["_exit"])].append((lot_seq, xpx, why))
        after = acct.groups()
        ctrl.step(before, after, changed)
        eq = acct.equity(px)
        ddp, ddw = dd.update(ti, eq, min(worst, eq))
        n_lots = len(acct.lots)
        n_groups = len(after)
        max_lots = max(max_lots, n_lots)
        max_groups = max(max_groups, n_groups)
        hr["max_lots"] = max(hr["max_lots"], n_lots)
        hr["max_groups"] = max(hr["max_groups"], n_groups)
        if acct.used_margin > hr["max_margin"]:
            hr["max_margin"] = acct.used_margin
            hr["max_margin_pct"] = acct.used_margin / eq * 100.0 if eq > 0 else float("inf")
        hr["max_dd_pct"] = max(hr["max_dd_pct"], ddp)
        hr["max_dd_intrabar_pct"] = max(hr["max_dd_intrabar_pct"], ddw)
        eq_curve.append((t, round(eq, 6), round(min(worst, eq), 6), round(acct.used_margin, 6), n_lots, n_groups))
        if ti % 60 == 59:
            hr["equity_end"] = eq
            hr["open_lots_end"] = n_lots
            hr["open_groups_end"] = n_groups
            hr["cum_max_dd_pct"] = dd.max_dd_pct
            hr["cum_max_dd_intrabar_pct"] = dd.max_dd_intrabar_pct
            hr["underwater_max_min"] = dd.max_under_min
            hr["ctrl"] = {k: v - hr["ctrl_before"][k] for k, v in ctrl.as_dict().items()}
    # ---- statistics ----
    def stats(rows):
        if not rows:
            return dict(n=0)
        nets = [r["net"] for r in rows]
        gross = [r["gross"] for r in rows]
        wins = sum(1 for x in nets if x > 0)
        losses = sum(1 for x in nets if x < 0)
        ddt = drawdown_time_by_symbol([{"t": r["t"], "symbol": r["symbol"], "pnl_pct": r["pnl_pct"]} for r in sorted(rows, key=lambda r: r["t"])], ordered=True)
        return dict(n=len(rows), wins=wins, losses=losses, winRate=round(wins / len(rows) * 100, 2),
                    pfNormal=round(classic_pf(nets), 4), pfGross=round(classic_pf(gross), 4),
                    costPf=cost_pf_ratio([r["pnl_pct"] for r in rows], cost_pct),
                    netUsdt=round(sum(nets), 6), grossUsdt=round(sum(gross), 6),
                    engineDdtMaxS=float(ddt.get("maxS") or 0.0))

    def by_strategy(rows):
        out = {}
        for key in ("general/normal", "general/trailing", "indications/normal", "indications/trailing", "block", "dca"):
            out[key] = stats([r for r in rows if r["strategy"] == key])
        out["general"] = stats([r for r in rows if r["strategy"].startswith("general")])
        out["indications"] = stats([r for r in rows if r["strategy"].startswith("indications")])
        out["trailing"] = stats([r for r in rows if r["strategy"].endswith("trailing")])
        out["normal"] = stats([r for r in rows if r["strategy"].endswith("normal")])
        for a in AXIS_WINDOWS:
            out[f"axis:{a}"] = stats([r for r in rows if r["axes"].get(a)])
        for k in IND_KINDS:
            out[f"kind:{k}"] = stats([r for r in rows if k in r["kinds"]])
            out[f"lane:{k}"] = stats([r for r in rows if r["strategy"] == "kind:" + k])
        out["kindLanes"] = stats([r for r in rows if r["strategy"].startswith("kind:")])
        return out

    hourly = []
    cum: List[Dict[str, Any]] = []
    prev_eq = start_equity
    for h, hr in enumerate(hours):
        rows_h = [r for r in closed_rows if r["hour"] == h]
        cum.extend(rows_h)
        st_h = stats(rows_h)
        st_c = stats(cum)
        hourly.append(dict(
            hour=h, startUtc=time.strftime("%H:%M", time.gmtime(start_s + (sim_start + h * 60) * BAR)),
            equityEnd=round(hr["equity_end"], 6), pnl=round(hr["equity_end"] - prev_eq, 6),
            ddMaxPct=round(hr["max_dd_pct"], 3), ddIntrabarMaxPct=round(hr["max_dd_intrabar_pct"], 3),
            ddCumMaxPct=round(hr["cum_max_dd_pct"], 3), ddCumIntrabarMaxPct=round(hr["cum_max_dd_intrabar_pct"], 3),
            marginMax=round(hr["max_margin"], 6), marginMaxPct=round(hr["max_margin_pct"], 2),
            closed=st_h, cumulative={k: st_c.get(k) for k in ("n", "pfNormal", "pfGross", "costPf", "winRate", "engineDdtMaxS", "netUsdt")},
            underwaterMaxMin=hr["underwater_max_min"],
            positions=dict(lotsOpened=hr["opened_lots"], groupsOpened=hr["opened_groups"], lotsOpenEnd=hr["open_lots_end"],
                           groupsOpenEnd=hr["open_groups_end"], maxLots=hr["max_lots"], maxGroups=hr["max_groups"]),
            orders=dict(entry=hr["entry_orders"], blockAdd=hr["block_orders"], dcaEntry=hr["dca_entry_orders"],
                        dcaAdd=hr["dca_add_orders"], closeFills=hr["close_fills"], **hr["ctrl"]),
            skipped=dict(noFreeMargin=hr["skipped_margin"], belowMinOrQty=hr["skipped_min"],
                         liveNegativeDeact=hr["skipped_live_neg"], addOnNoParent=hr["skipped_no_parent"],
                         equityHalt=hr.get("skipped_halt", 0)),
            liquidations=hr.get("liquidations", 0),
            byStrategy=by_strategy(rows_h),
        ))
        prev_eq = hr["equity_end"]
    total = stats(closed_rows)
    fills = defaultdict(int)
    for hr in hours:
        for k, v in hr["close_fills"].items():
            fills[k] += v
    totals = dict(
        equityStart=start_equity, equityEnd=round(hours[-1]["equity_end"], 6),
        pnl=round(hours[-1]["equity_end"] - start_equity, 6),
        returnPct=round((hours[-1]["equity_end"] / start_equity - 1) * 100, 3),
        realizedGross=round(acct.gross_realized, 6), fees=round(acct.fees, 6),
        unrealizedEnd=round(hours[-1]["equity_end"] - acct.cash, 6),
        ddMaxPct=round(dd.max_dd_pct, 3), ddIntrabarMaxPct=round(dd.max_dd_intrabar_pct, 3),
        underwaterMaxMin=dd.max_under_min,
        marginMax=round(max(h["max_margin"] for h in hours), 6),
        marginMaxPct=round(max(h["max_margin_pct"] for h in hours), 2),
        closed=total, byStrategy=by_strategy(closed_rows),
        positions=dict(lotsOpened=sum(h["opened_lots"] for h in hours), groupsOpened=sum(h["opened_groups"] for h in hours),
                       lotsOpenEnd=hours[-1]["open_lots_end"], groupsOpenEnd=hours[-1]["open_groups_end"],
                       maxLots=max_lots, maxGroups=max_groups),
        orders=dict(entry=sum(h["entry_orders"] for h in hours), blockAdd=sum(h["block_orders"] for h in hours),
                    dcaEntry=sum(h["dca_entry_orders"] for h in hours), dcaAdd=sum(h["dca_add_orders"] for h in hours),
                    closeFills=dict(fills), **ctrl.as_dict()),
        skipped=dict(noFreeMargin=sum(h["skipped_margin"] for h in hours), belowMinOrQty=sum(h["skipped_min"] for h in hours),
                     liveNegativeDeact=sum(h["skipped_live_neg"] for h in hours),
                     addOnNoParent=sum(h["skipped_no_parent"] for h in hours),
                     equityHalt=sum(h.get("skipped_halt", 0) for h in hours),
                     microCap=sum(h.get("skipped_micro_cap", 0) for h in hours),
                     sideCap=sum(h.get("skipped_side_cap", 0) for h in hours)),
        liquidations=liquidations, liquidationLoss=round(getattr(acct, "liquidation_loss", 0.0), 6),
        haltedMinutes=halted_minutes, ddPauseMinutes=dd_pause_minutes,
        candidates=dict(setTradesInWindow=int(len(cands["uid"])), setLotsInWindow=int(mult[cands["uid"]].sum()),
                        admittedTrades=int(use.sum()), admittedLots=int(mult[cands["uid"][use]].sum()),
                        blockedByKindGate=ind_block,
                        blockDcaLaneTrades=len(strat), blockDcaAdmitted=sum(1 for r in strat if (not gated) or r["_ok"]),
                        kindLaneTrades=len(kind_lanes or []),
                        kindLaneAdmitted=sum(1 for r in (kind_lanes or []) if (not gated) or r["_ok"]),
                        kindLaneOrders=sum(h.get("kind_lane_orders", 0) for h in hours)),
    )
    totals["orders"]["total"] = (totals["orders"]["entry"] + totals["orders"]["blockAdd"] + totals["orders"]["dcaEntry"]
                                 + totals["orders"]["dcaAdd"] + sum(fills.values()) + ctrl.place + ctrl.cancel_replace + ctrl.cancel)
    return dict(run=run, hourly=hourly, totals=totals, equityCurve=eq_curve[::5])


def ddt_max_s(t: np.ndarray, sym: np.ndarray, net: np.ndarray) -> float:
    """Vectorized set_engine.drawdown_time_by_symbol(...)['maxS'] (historic tape, now = last sample)."""
    if len(t) == 0:
        return 0.0
    best = 0.0
    order = np.lexsort((t, sym))
    t, sym, net = t[order], sym[order], net[order]
    cuts = np.r_[0, np.flatnonzero(sym[1:] != sym[:-1]) + 1, len(sym)]
    parts = [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]
    if len(parts) == 1:
        parts = [(0, len(t))]
    for a, b in parts:
        tt = t[a:b]
        eq = np.cumsum(net[a:b])
        peak_before = np.maximum.accumulate(np.r_[0.0, eq[:-1]])
        peak_before = np.maximum(peak_before, 0.0)
        under = eq < peak_before - 1e-12
        if not under.any():
            continue
        d = np.diff(np.r_[0, under.astype(np.int8), 0])
        starts = np.flatnonzero(d == 1)
        ends = np.flatnonzero(d == -1)  # first recovered row index (or len)
        st = tt[starts]
        en = np.where(ends < len(tt), tt[np.minimum(ends, len(tt) - 1)], tt[-1])
        best = max(best, float(np.max(en - st)))
    return round(best, 1)


def _level_stats(mv, w, t, sym, cost_pct):
    if len(mv) == 0:
        return dict(n=0)
    net = mv - cost_pct / 100.0
    wn = net * w
    gp = float(wn[wn > 0].sum())
    gl = float(-wn[wn < 0].sum())
    gg = mv * w
    ggp = float(gg[gg > 0].sum())
    ggl = float(-gg[gg < 0].sum())
    avg_r = float((((mv * 100 - cost_pct) / cost_pct) * w).sum() / w.sum())
    wins = int(w[net > 0].sum())
    losses = int(w[net < 0].sum())
    return dict(n=int(w.sum()), trades=int(len(mv)), wins=wins, losses=losses, winRate=round(wins / float(w.sum()) * 100, 2),
                pfNormal=round(gp / gl if gl > 0 else (99.0 if gp > 0 else 0.0), 4),
                pfGross=round(ggp / ggl if ggl > 0 else (99.0 if ggp > 0 else 0.0), 4),
                costPf=round(1 + 0.1 * avg_r, 4), netSumPctUnits=round(float(wn.sum()) * 100, 3),
                engineDdtMaxS=ddt_max_s(t, sym, net * w))


def trade_level_hourly(cands, catalog, mask, strat, gated, sim_start, H, cost_pct):
    """Stage chain as a continuous process: every admitted Set lot (unit size,
    no margin limit) by close hour, per strategy, plus Block/DCA lanes."""
    mult = np.array([c["mult"] for c in catalog], dtype=np.int64)
    pack_ind = np.array([c["pack"] == "indications" for c in catalog])
    trail = np.array([c["kind"] == "trail" for c in catalog])
    sel = np.flatnonzero(mask & (cands["exit"] >= 0) & (cands["exit"] < sim_start + H * 60))
    mv = cands["raw"][sel]
    u = cands["uid"][sel]
    w = mult[u].astype(np.float64)
    t = cands["exit"][sel].astype(np.float64) * BAR
    sy = cands["sym"][sel].astype(np.int64)
    hour = (cands["exit"][sel] - sim_start) // 60
    groups = {
        "all": np.ones(len(sel), bool),
        "normal": ~trail[u], "trailing": trail[u],
        "general/normal": ~pack_ind[u] & ~trail[u], "general/trailing": ~pack_ind[u] & trail[u],
        "indications/normal": pack_ind[u] & ~trail[u], "indications/trailing": pack_ind[u] & trail[u],
    }
    for a in AXIS_WINDOWS:
        groups[f"axis:{a}"] = cands["axes"][a][sel]
    srows = [r for r in strat if ((not gated) or r["_ok"]) and sim_start <= r["_exit"] < sim_start + H * 60]
    s_mv = np.array([float(r["pnl_pct"]) for r in srows])
    s_t = np.array([float(r["_exit"]) * BAR for r in srows])
    s_sym = np.array([hash(r["symbol"]) % 10007 for r in srows], dtype=np.int64)
    s_hour = np.array([(int(r["_exit"]) - sim_start) // 60 for r in srows], dtype=np.int64)
    s_kind = np.array(["block" if r["lane"].startswith("block") else "dca" for r in srows])
    out = []
    for h in list(range(H)) + ["total"]:
        hm = np.ones(len(sel), bool) if h == "total" else (hour == h)
        row = {"hour": h}
        for name, gm in groups.items():
            k = hm & gm
            row[name] = _level_stats(mv[k], w[k], t[k], sy[k], cost_pct)
        shm = np.ones(len(srows), bool) if h == "total" else (s_hour == h)
        for name in ("block", "dca"):
            k = shm & (s_kind == name) if len(srows) else np.zeros(0, bool)
            row[name] = _level_stats(s_mv[k], np.ones(int(k.sum())), s_t[k], s_sym[k], cost_pct) if len(srows) else dict(n=0)
        out.append(row)
    return out


def trade_level_markdown(rows, start_s, sim_start):
    head = ("| h (UTC) | Lots | PF normal | PF gross | Cost PF | Win% | DDT s | Normal PF | Trailing PF | Gen N | Gen Tr | Ind N | Ind Tr | "
            "Block PF (n) | DCA PF (n) | axis prev | axis last | axis cont | axis pause |")
    lines = [head, "|" + "|".join(["---"] * (head.count("|") - 1)) + "|"]

    def pf(st):
        return "-" if not st or not st.get("n") else f"{st['pfNormal']:.2f}"

    def pfn(st):
        return "-" if not st or not st.get("n") else f"{st['pfNormal']:.2f} ({st['n']})"

    for r in rows:
        a = r["all"]
        label = "**Total**" if r["hour"] == "total" else time.strftime("%H:%M", time.gmtime(start_s + (sim_start + r["hour"] * 60) * BAR))
        if not a.get("n"):
            lines.append(f"| {label} | 0 |" + " - |" * (head.count("|") - 3))
            continue
        lines.append(
            f"| {label} | {a['n']} | {a['pfNormal']:.2f} | {a['pfGross']:.2f} | {a['costPf']:.3f} | {a['winRate']:.1f} | {a['engineDdtMaxS']:.0f} | "
            f"{pf(r['normal'])} | {pf(r['trailing'])} | {pf(r['general/normal'])} | {pf(r['general/trailing'])} | "
            f"{pf(r['indications/normal'])} | {pf(r['indications/trailing'])} | {pfn(r['block'])} | {pfn(r['dca'])} | "
            f"{pf(r['axis:prev'])} | {pf(r['axis:last'])} | {pf(r['axis:cont'])} | {pf(r['axis:pause'])} |")
    return "\n".join(lines)


def per_signal_metrics(cands, mask, cost_pct):
    """One trade per distinct signal (symbol x side x entry bar), the admitted
    Set with the highest in-sample last-N PF first (the account's pick).

    Pooled Set-trade metrics count one signal once per overlapping Set
    configuration (hundreds of near-duplicate Sets enter the same signal),
    so a few winning signals can dominate the pooled PF. This is the edge an
    account can actually trade."""
    sel = np.flatnonzero(mask & (cands["exit"] >= 0))
    if not len(sel):
        return dict(signals=0)
    key = cands["sym"][sel].astype(np.int64) * 10_000_000 + (cands["side"][sel] > 0) * 5_000_000 + cands["entry"][sel]
    r30 = np.nan_to_num(cands["r30"][sel], nan=0.0)
    order = np.lexsort((-r30, key))
    k = key[order]
    first = np.r_[True, k[1:] != k[:-1]]
    mv = cands["raw"][sel][order][first]
    net = mv - cost_pct / 100.0
    gl = float(-net[net < 0].sum())
    return dict(signals=int(first.sum()), setTrades=int(len(sel)), trades=int(first.sum()), lots=int(len(sel)),
                classicPf=round(float(net[net > 0].sum()) / gl, 4) if gl > 0 else 99.0,
                winRate=round(float((net > 0).mean()) * 100, 2), netAvgPct=round(float(net.mean()) * 100, 4))


def tape_metrics(cands, catalog, mask, cost_pct):
    """Trade-level (no account/margin limit) metrics of a candidate selection, per Set unit."""
    mult = np.array([c["mult"] for c in catalog], dtype=np.int64)
    sel = mask & (cands["exit"] >= 0)
    mv = cands["raw"][sel]
    w = mult[cands["uid"][sel]]
    if not len(mv):
        return dict(n=0)
    net = mv - cost_pct / 100.0
    gp = float((net[net > 0] * w[net > 0]).sum())
    gl = float(-(net[net < 0] * w[net < 0]).sum())
    avg_r = float((((mv * 100 - cost_pct) / cost_pct) * w).sum() / w.sum())
    return dict(trades=int(len(mv)), lots=int(w.sum()), classicPf=round(gp / gl if gl > 0 else 99.0, 4),
                costPf=round(1 + 0.1 * avg_r, 4), winRate=round(float(w[net > 0].sum()) / float(w.sum()) * 100, 2),
                netAvgPct=round(float((net * w).sum() / w.sum()) * 100, 4))


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def fmt(v, nd=2):
    if v is None:
        return "-"
    if isinstance(v, float):
        if math.isinf(v):
            return "inf"
        return f"{v:.{nd}f}"
    return str(v)


def markdown_table(res: Dict[str, Any]) -> str:
    head = ("| h (UTC) | Equity | PnL | DD% max (intrabar) | cum DD% | Margin max $ (% eq) | PF normal h / cum | Cost PF h | "
            "DDT eng s / uw min | Gen N | Gen Tr | Ind N | Ind Tr | Block | DCA | Closed W/L (win%) | Lots opened / open / max | Groups max | "
            "Orders E/B/D/close/ctrl |")
    sep = "|" + "---|" * head.count("|")[:0].__len__() if False else "|" + "|".join(["---"] * (head.count("|") - 1)) + "|"
    lines = [head, sep]

    def pf(st):
        return "-" if not st or not st.get("n") else f"{st['pfNormal']:.2f}({st['n']})"

    for h in res["hourly"]:
        c = h["closed"]
        o = h["orders"]
        ctrl = o["controlPlace"] + o["controlCancelReplace"] + o["controlCancel"]
        bs = h["byStrategy"]
        lines.append(
            f"| {h['startUtc']} | {h['equityEnd']:.3f} | {h['pnl']:+.3f} | {h['ddMaxPct']:.2f} ({h['ddIntrabarMaxPct']:.2f}) | "
            f"{h['ddCumMaxPct']:.2f} | {h['marginMax']:.2f} ({h['marginMaxPct']:.0f}%) | "
            f"{fmt(c.get('pfNormal'))} / {fmt(h['cumulative'].get('pfNormal'))} | {fmt(c.get('costPf'), 3)} | "
            f"{fmt(c.get('engineDdtMaxS'), 0)} / {h['underwaterMaxMin']} | {pf(bs['general/normal'])} | {pf(bs['general/trailing'])} | "
            f"{pf(bs['indications/normal'])} | {pf(bs['indications/trailing'])} | {pf(bs['block'])} | {pf(bs['dca'])} | "
            f"{c.get('wins', 0)}/{c.get('losses', 0)} ({fmt(c.get('winRate'), 1)}) | "
            f"{h['positions']['lotsOpened']} / {h['positions']['lotsOpenEnd']} / {h['positions']['maxLots']} | {h['positions']['maxGroups']} | "
            f"{o['entry']}/{o['blockAdd']}/{o['dcaEntry'] + o['dcaAdd']}/{sum(o['closeFills'].values())}/{ctrl} |")
    t = res["totals"]
    c = t["closed"]
    o = t["orders"]
    bs = t["byStrategy"]
    ctrl = o["controlPlace"] + o["controlCancelReplace"] + o["controlCancel"]
    lines.append(
        f"| **Total** | {t['equityEnd']:.3f} | {t['pnl']:+.3f} | {t['ddMaxPct']:.2f} ({t['ddIntrabarMaxPct']:.2f}) | {t['ddMaxPct']:.2f} | "
        f"{t['marginMax']:.2f} ({t['marginMaxPct']:.0f}%) | {fmt(c.get('pfNormal'))} | {fmt(c.get('costPf'), 3)} | "
        f"{fmt(c.get('engineDdtMaxS'), 0)} / {t['underwaterMaxMin']} | {pf(bs['general/normal'])} | {pf(bs['general/trailing'])} | "
        f"{pf(bs['indications/normal'])} | {pf(bs['indications/trailing'])} | {pf(bs['block'])} | {pf(bs['dca'])} | "
        f"{c.get('wins', 0)}/{c.get('losses', 0)} ({fmt(c.get('winRate'), 1)}) | {t['positions']['lotsOpened']} / "
        f"{t['positions']['lotsOpenEnd']} / {t['positions']['maxLots']} | {t['positions']['maxGroups']} | "
        f"{o['entry']}/{o['blockAdd']}/{o['dcaEntry'] + o['dcaAdd']}/{sum(o['closeFills'].values())}/{ctrl} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Inline SVG diagrams (no external assets; colours follow the page theme)
# --------------------------------------------------------------------------

_SVG_W, _SVG_H, _PAD_L, _PAD_R, _PAD_T, _PAD_B = 640, 220, 52, 12, 14, 30
_PALETTE = ["var(--acc)", "var(--pos)", "var(--neg)", "#c98a1b", "#8a5cd1", "#1b9aa6", "#a64d79", "#6b6b66"]


def _nice_ticks(lo: float, hi: float, n: int = 4) -> List[float]:
    if not math.isfinite(lo) or not math.isfinite(hi):
        return [0.0, 1.0]
    if hi - lo < 1e-12:
        hi = lo + 1.0
    step = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(step))
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= step), default=step)
    start = math.floor(lo / step) * step
    out, v = [], start
    while v <= hi + step * 0.5 and len(out) < 12:
        out.append(round(v, 10))
        v += step
    return out


def _svg_frame(title: str, labels: List[str], ymin: float, ymax: float, body, legend: Optional[List[Tuple[str, str]]] = None):
    ticks = _nice_ticks(ymin, ymax)
    ymin, ymax = min(ymin, ticks[0]), max(ymax, ticks[-1])
    span = (ymax - ymin) or 1.0
    iw, ih = _SVG_W - _PAD_L - _PAD_R, _SVG_H - _PAD_T - _PAD_B

    def yp(v):
        return _PAD_T + ih - (v - ymin) / span * ih

    parts = [f"<svg viewBox='0 0 {_SVG_W} {_SVG_H}' role='img' aria-label='{html.escape(title)}' class='chart'>"]
    for tk in ticks:
        y = yp(tk)
        parts.append(f"<line x1='{_PAD_L}' x2='{_SVG_W - _PAD_R}' y1='{y:.1f}' y2='{y:.1f}' class='grid'/>"
                     f"<text x='{_PAD_L - 6}' y='{y + 4:.1f}' class='ax' text-anchor='end'>{fmt(tk, 2 if abs(tk) < 100 else 0)}</text>")
    n = max(1, len(labels))
    every = max(1, n // 12)
    for i, lab in enumerate(labels):
        if i % every == 0:
            x = _PAD_L + (i + 0.5) / n * iw
            parts.append(f"<text x='{x:.1f}' y='{_SVG_H - 10}' class='ax' text-anchor='middle'>{html.escape(str(lab))}</text>")
    parts.append(body(yp, iw, ih, n))
    parts.append("</svg>")
    leg = ""
    if legend:
        leg = "<div class='legend'>" + "".join(f"<span><i style='background:{c}'></i>{html.escape(t)}</span>" for t, c in legend) + "</div>"
    return f"<figure><figcaption>{html.escape(title)}</figcaption>{''.join(parts)}{leg}</figure>"


def svg_bars(title: str, labels: List[str], series: List[Tuple[str, List[float]]], stacked: bool = False, colors=None) -> str:
    colors = colors or _PALETTE
    if stacked:
        pos = [sum(max(0.0, s[1][i]) for s in series) for i in range(len(labels))]
        neg = [sum(min(0.0, s[1][i]) for s in series) for i in range(len(labels))]
        ymax, ymin = max(pos + [0.0]), min(neg + [0.0])
    else:
        vals = [v for s in series for v in s[1]]
        ymax, ymin = max(vals + [0.0]), min(vals + [0.0])

    def body(yp, iw, ih, n):
        out = [f"<line x1='{_PAD_L}' x2='{_SVG_W - _PAD_R}' y1='{yp(0):.1f}' y2='{yp(0):.1f}' class='zero'/>"]
        slot = iw / n
        bw = slot * 0.7 / (1 if stacked else max(1, len(series)))
        for i in range(len(labels)):
            up = dn = 0.0
            for k, (name, vals) in enumerate(series):
                v = vals[i]
                col = colors[k % len(colors)]
                if stacked:
                    base = up if v >= 0 else dn
                    top = base + v
                    y0, y1 = yp(base), yp(top)
                    if v >= 0:
                        up = top
                    else:
                        dn = top
                    x = _PAD_L + i * slot + slot * 0.15
                else:
                    y0, y1 = yp(0), yp(v)
                    x = _PAD_L + i * slot + slot * 0.15 + k * bw
                h = abs(y1 - y0)
                if h > 0.01:
                    out.append(f"<rect x='{x:.1f}' y='{min(y0, y1):.1f}' width='{bw:.1f}' height='{h:.1f}' fill='{col}'>"
                               f"<title>{html.escape(labels[i])} {html.escape(name)}: {fmt(v, 4)}</title></rect>")
        return "".join(out)

    legend = [(n_, colors[k % len(colors)]) for k, (n_, _v) in enumerate(series)] if len(series) > 1 else None
    return _svg_frame(title, labels, ymin, ymax, body, legend)


def svg_lines(title: str, labels: List[str], series: List[Tuple[str, List[Optional[float]]]], ref: Optional[float] = None) -> str:
    vals = [v for s in series for v in s[1] if v is not None and math.isfinite(v)]
    if ref is not None:
        vals.append(ref)
    ymax, ymin = max(vals + [0.0]), min(vals + [0.0])

    def body(yp, iw, ih, n):
        out = []
        if ref is not None:
            out.append(f"<line x1='{_PAD_L}' x2='{_SVG_W - _PAD_R}' y1='{yp(ref):.1f}' y2='{yp(ref):.1f}' class='ref'/>")
        for k, (name, ys) in enumerate(series):
            col = _PALETTE[k % len(_PALETTE)]
            pts = [(_PAD_L + (i + 0.5) / n * iw, yp(v)) for i, v in enumerate(ys) if v is not None and math.isfinite(v)]
            if len(pts) > 1:
                out.append(f"<polyline fill='none' stroke='{col}' stroke-width='1.8' points='{' '.join(f'{x:.1f},{y:.1f}' for x, y in pts)}'/>")
            for (x, y), (i, v) in zip(pts, [(i, v) for i, v in enumerate(ys) if v is not None and math.isfinite(v)]):
                out.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='2.6' fill='{col}'><title>{html.escape(labels[i])} {html.escape(name)}: {fmt(v, 3)}</title></circle>")
        return "".join(out)

    legend = [(n_, _PALETTE[k % len(_PALETTE)]) for k, (n_, _v) in enumerate(series)]
    return _svg_frame(title, labels, ymin, ymax, body, legend)


def run_charts(res: Dict[str, Any], start_s: int, sim_start_bar: int) -> str:
    hrs = res["hourly"]
    labels = [h["startUtc"] for h in hrs]
    pf = lambda st: (st.get("pfNormal") if st and st.get("n") else None)  # noqa: E731
    charts = [
        svg_lines("Equity at end of hour (USDT)", labels, [("equity", [h["equityEnd"] for h in hrs])]),
        svg_bars("PnL per hour (USDT)", labels, [("pnl", [h["pnl"] for h in hrs])], colors=["var(--acc)"]),
        svg_lines("Max drawdown per hour (%)", labels, [("DD close", [h["ddMaxPct"] for h in hrs]),
                                                      ("DD intrabar", [h["ddIntrabarMaxPct"] for h in hrs])]),
        svg_bars("Peak margin used per hour (% of equity)", labels, [("margin %", [h["marginMaxPct"] for h in hrs])], colors=["#c98a1b"]),
        svg_bars("Closed trades per hour (wins / losses)", labels,
                 [("wins", [float(h["closed"].get("wins", 0)) for h in hrs]), ("losses", [float(h["closed"].get("losses", 0)) for h in hrs])],
                 stacked=True, colors=["var(--pos)", "var(--neg)"]),
        svg_bars("Orders per hour", labels,
                 [("entry", [float(h["orders"]["entry"]) for h in hrs]), ("block add", [float(h["orders"]["blockAdd"]) for h in hrs]),
                  ("DCA", [float(h["orders"]["dcaEntry"] + h["orders"]["dcaAdd"]) for h in hrs]),
                  ("close fills", [float(sum(h["orders"]["closeFills"].values())) for h in hrs]),
                  ("control", [float(h["orders"]["controlPlace"] + h["orders"]["controlCancelReplace"] + h["orders"]["controlCancel"]) for h in hrs])],
                 stacked=True),
        svg_bars("Lots / positions opened per hour", labels,
                 [("lots", [float(h["positions"]["lotsOpened"]) for h in hrs]), ("position groups", [float(h["positions"]["groupsOpened"]) for h in hrs])]),
        svg_lines("PF normal per hour by strategy (1.0 = break-even)", labels,
                  [(k, [pf(h["byStrategy"].get(k)) for h in hrs]) for k in
                   ("general/normal", "general/trailing", "indications/normal", "indications/trailing", "block", "dca")], ref=1.0),
        svg_bars("Skipped entries per hour", labels,
                 [("no free margin", [float(h["skipped"]["noFreeMargin"]) for h in hrs]), ("below min", [float(h["skipped"]["belowMinOrQty"]) for h in hrs]),
                  ("live-negative", [float(h["skipped"]["liveNegativeDeact"]) for h in hrs]), ("no parent", [float(h["skipped"]["addOnNoParent"]) for h in hrs])],
                 stacked=True),
    ]
    curve = res.get("equityCurve") or []
    if curve:
        cl = [time.strftime("%H:%M", time.gmtime(start_s + b * BAR)) for b, *_ in curve]
        charts.insert(1, svg_lines("Equity, 5-minute resolution (close / worst intrabar)", cl,
                                   [("equity", [c[1] for c in curve]), ("worst intrabar", [c[2] for c in curve])]))
        charts.append(svg_lines("Open lots and position groups (5-minute)", cl,
                                [("lots", [float(c[4]) for c in curve]), ("groups", [float(c[5]) for c in curve])]))
    return "<div class='charts'>" + "".join(charts) + "</div>"


def assumption_text(text: str, book) -> str:
    """Fill the assumption template with the book's own stage windows and floors."""
    base_w, main_w, real_w = book._stage_window_ns()
    ddt = int(book.ddt_window()) if callable(getattr(book, "ddt_window", None)) else 96
    vals = {
        "{floor}": f"{float(book.stage_min_pf['base']):.2f}",
        "{mainFloor}": f"{float(book.stage_min_pf['main']):.2f}",
        "{realFloor}": f"{float(book.stage_min_pf['real']):.2f}",
        "{base}": str(base_w), "{main}": str(main_w), "{real}": str(real_w),
        "{need}": str(int(book.eval_need())), "{ddt}": str(ddt), "{lookback}": str(int(book.lookback)),
    }
    for key, value in vals.items():
        text = text.replace(key, value)
    return text


def html_report(report: Dict[str, Any]) -> str:
    w = report.get("window") or {}
    hours_n = max(1, int(round((int(w.get("simEndBar", 0)) - int(w.get("simStartBar", 0))) / 60))) if w else 12
    def esc(x):
        return html.escape(str(x))

    def run_section(res):
        rows = []
        for h in res["hourly"] + [dict(res["totals"], startUtc="Total", hour="T")]:
            is_total = h.get("hour") == "T"
            c = h["closed"]
            o = h["orders"]
            bs = h["byStrategy"]
            ctrl = o["controlPlace"] + o["controlCancelReplace"] + o["controlCancel"]

            def pf(st):
                return "&ndash;" if not st or not st.get("n") else f"{st['pfNormal']:.2f}<small> ({st['n']})</small>"
            eq = h["equityEnd"]
            pnl = h["pnl"]
            cls = "pos" if pnl > 0 else ("neg" if pnl < 0 else "")
            cum = h.get("cumulative") or c
            rows.append(
                f"<tr class='{'total' if is_total else ''}'><td>{esc(h['startUtc'])}</td><td>{eq:.3f}</td>"
                f"<td class='{cls}'>{pnl:+.3f}</td>"
                f"<td>{h['ddMaxPct']:.2f}<small> / {h['ddIntrabarMaxPct']:.2f}</small></td>"
                f"<td>{(h.get('ddCumMaxPct', h['ddMaxPct'])):.2f}</td>"
                f"<td>{h['marginMax']:.2f}<small> ({h['marginMaxPct']:.0f}%)</small></td>"
                f"<td>{fmt(c.get('pfNormal'))}</td><td>{fmt(cum.get('pfNormal'))}</td><td>{fmt(c.get('pfGross'))}</td>"
                f"<td>{fmt(c.get('costPf'), 3)}</td>"
                f"<td>{fmt(c.get('engineDdtMaxS'), 0)}<small> / {h['underwaterMaxMin']}m</small></td>"
                f"<td>{pf(bs['general/normal'])}</td><td>{pf(bs['general/trailing'])}</td>"
                f"<td>{pf(bs['indications/normal'])}</td><td>{pf(bs['indications/trailing'])}</td>"
                f"<td>{pf(bs['block'])}</td><td>{pf(bs['dca'])}</td>"
                + "".join(f"<td>{pf(bs['axis:' + a])}</td>" for a in AXIS_WINDOWS)
                + f"<td>{c.get('n', 0)}</td><td>{c.get('wins', 0)}/{c.get('losses', 0)}<small> ({fmt(c.get('winRate'), 1)}%)</small></td>"
                f"<td>{h['positions']['lotsOpened']}/{h['positions']['lotsOpenEnd']}/{h['positions']['maxLots']}</td>"
                f"<td>{h['positions']['groupsOpened']}/{h['positions']['groupsOpenEnd']}/{h['positions']['maxGroups']}</td>"
                f"<td>{o['entry']}</td><td>{o['blockAdd']}</td><td>{o['dcaEntry']}+{o['dcaAdd']}</td>"
                f"<td>{sum(o['closeFills'].values())}<small> {esc(', '.join(f'{k}:{v}' for k, v in sorted(o['closeFills'].items())))}</small></td>"
                f"<td>{ctrl}<small> ({o['controlPlace']}/{o['controlCancelReplace']}/{o['controlCancel']})</small></td>"
                f"<td>{h['skipped']['noFreeMargin']}</td></tr>")
        heads = (["Hour (UTC)", "Equity", "PnL", "DD% max / intrabar", "Cum DD%", "Margin max $ (% eq)", "PF normal", "PF normal cum",
                  "PF gross", "Cost PF", "DDT eng s / underwater", "General", "General trail", "Indic.", "Indic. trail",
                  "Block", "DCA"] + [f"Axis {a}*" for a in AXIS_WINDOWS]
                 + ["Trades", "W/L (win%)", "Lots opened/open/max", "Positions opened/open/max", "Entry ord.",
                    "Block add", "DCA entry+add", "Close fills", "Control (place/cxlRepl/cxl)", "Skipped: no margin"])
        t = res["totals"]
        kinds = "".join(
            f"<tr><td>{k}</td><td>{t['byStrategy']['kind:' + k].get('n', 0)}</td><td>{fmt(t['byStrategy']['kind:' + k].get('pfNormal'))}</td>"
            f"<td>{fmt(t['byStrategy']['kind:' + k].get('costPf'), 3)}</td><td>{fmt(t['byStrategy']['kind:' + k].get('winRate'), 1)}</td></tr>"
            for k in IND_KINDS)
        liq = t.get("liquidations") or []
        liq_txt = ("Liquidations: " + "; ".join(f"{x['utc']} UTC {x['lots']} lots / {x['notional']:.0f} USDT notional, equity after {x['equityAfter']:.3f}" for x in liq)) if liq else "No liquidation."
        return (f"<h2>{esc(res['title'])}</h2>{run_charts(res, report['startS'], report['window']['simStartBar'])}<p class='note'>{esc(liq_txt)} Leverage: {esc(res.get('leverage'))}. "
                f"Skipped entries: {t['skipped']['noFreeMargin']} no free margin, {t['skipped'].get('equityHalt', 0)} equity halt, "
                f"{t['skipped']['liveNegativeDeact']} live-negative deactivation.</p>"
                f"<div class='scroll'><table><thead><tr>{''.join(f'<th>{esc(x)}</th>' for x in heads)}</tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table></div>"
                f"<details><summary>Indication-kind attribution (indications lots; a lot counts for every contributing kind)</summary>"
                f"<table><thead><tr><th>Kind</th><th>Trades</th><th>PF normal</th><th>Cost PF</th><th>Win %</th></tr></thead>"
                f"<tbody>{kinds}</tbody></table></details>"
                f"<details><summary>Totals JSON</summary><pre>{esc(json.dumps({k: v for k, v in t.items() if k != 'byStrategy'}, indent=1))}</pre></details>")

    runs = "".join(run_section(r) for r in report["runs"])

    def tl_section(name, rows):
        cols = ["all", "normal", "trailing", "general/normal", "general/trailing", "indications/normal", "indications/trailing",
                "block", "dca"] + [f"axis:{a}" for a in AXIS_WINDOWS]
        body = []
        for r in rows:
            label = "Total" if r["hour"] == "total" else time.strftime("%H:%M", time.gmtime(report["startS"] + (report["window"]["simStartBar"] + r["hour"] * 60) * BAR))
            a = r["all"]
            cells = [f"<td>{label}</td>", f"<td>{a.get('n', 0)}</td>", f"<td>{fmt(a.get('costPf'), 3)}</td>",
                     f"<td>{fmt(a.get('winRate'), 1)}</td>", f"<td>{fmt(a.get('engineDdtMaxS'), 0)}</td>"]
            for c in cols:
                st = r.get(c) or {}
                cells.append("<td>&ndash;</td>" if not st.get("n") else f"<td>{st['pfNormal']:.2f}<small> ({st['n']})</small></td>")
            body.append(f"<tr class='{'total' if r['hour'] == 'total' else ''}'>{''.join(cells)}</tr>")
        heads = ["Hour (UTC)", "Lots", "Cost PF", "Win %", "DDT s"] + [f"PF {c}" for c in cols]
        return (f"<h3>{esc(name)}</h3><div class='scroll'><table><thead><tr>{''.join(f'<th>{esc(h)}</th>' for h in heads)}</tr></thead>"
                f"<tbody>{''.join(body)}</tbody></table></div>")

    tlh = (tl_section("Post-Base (admitted by the stage chain)", report["tradeLevelHourly"]["post-base"])
           + tl_section("Unfiltered (all Sets)", report["tradeLevelHourly"]["unfiltered"]))
    assumptions = "".join(f"<li>{esc(a)}</li>" for a in report["assumptions"])
    floors = report.get("slFloorPct") or {}
    if floors:
        fv = sorted(floors.values())
        above = [f"{k} {v:.3f}%" for k, v in sorted(floors.items(), key=lambda kv: -kv[1]) if v > fv[0] + 1e-9][:12]
        sl_floor_txt = (f"min {fv[0]:.3f}% · median {fv[len(fv) // 2]:.3f}% · max {fv[-1]:.3f}% over {len(fv)} symbols"
                        + (f" · above desk floor: {', '.join(above)}" if above else " · every symbol at the desk floor"))
    else:
        sl_floor_txt = "not recorded"
    tm = report["tradeLevel"]
    tl = "".join(f"<tr><td>{esc(k)}</td><td>{v.get('trades', 0)}</td><td>{v.get('lots', 0)}</td><td>{fmt(v.get('classicPf'))}</td>"
                 f"<td>{fmt(v.get('costPf'), 3)}</td><td>{fmt(v.get('winRate'), 1)}</td><td>{fmt(v.get('netAvgPct'), 4)}</td></tr>"
                 for k, v in tm.items())
    css = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--mut:#6b6b66;--line:#e3e2dd;--acc:#2d5bd7;--pos:#1f7a3a;--neg:#b3261e;--tot:#f1f0ec}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#161615;--fg:#ecebe6;--mut:#9b9a94;--line:#34332f;--acc:#8fb0ff;--pos:#6fcf8a;--neg:#ff8a80;--tot:#22211f}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1500px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 6px}
.note,.meta{color:var(--mut)}.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:12.5px}th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{position:sticky;top:0;background:var(--bg);font-weight:600;text-align:right}td:first-child,th:first-child{text-align:left}
tr.total td{background:var(--tot);font-weight:600}small{color:var(--mut)}.pos{color:var(--pos)}.neg{color:var(--neg)}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:12px;margin:10px 0 14px}
figure{margin:0;border:1px solid var(--line);border-radius:6px;padding:8px 10px}figcaption{font-weight:600;font-size:12.5px;margin-bottom:4px}
.chart{width:100%;height:auto}.chart .grid{stroke:var(--line);stroke-width:1}.chart .zero{stroke:var(--mut);stroke-width:1}
.chart .ref{stroke:var(--mut);stroke-dasharray:4 3}.chart .ax{fill:var(--mut);font-size:10px}
.legend{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:11.5px;color:var(--mut)}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
details{margin:10px 0}summary{cursor:pointer;color:var(--acc)}pre{white-space:pre-wrap;font-size:12px}ul{padding-left:18px}li{margin:3px 0}
"""
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>CTS-G {hours_n}h account simulation</title><style>{css}</style></head><body><main>"
            f"<h1>CTS-G {hours_n}-hour account simulation</h1>"
            f"<p class='meta'>{esc(report['window']['startUtc'])} &rarr; {esc(report['window']['endUtc'])} UTC &middot; "
            f"{len(report['symbols'])} symbols &middot; start equity {report['startEquity']} USDT &middot; "
            f"leverage {esc(report['leverage'])} &middot; cost {report['costPct']}% round trip &middot; generated {esc(report['generatedAt'])}</p>"
            f"{runs}"
            f"<h2>Stage chain as a continuous process (unit Set lots by close hour, no margin limit)</h2>"
            f"<p class='note'>Every admitted Set lot at unit size, net of the engine PositionCost; PF normal (classic, net). "
            f"Block/DCA lane PF uses the engine's lane pnl_pct (relative to the parent unit).</p>{tlh}"
            f"<h2>Trade level (engine tape, no account limits)</h2><div class='scroll'><table><thead><tr><th>Selection</th><th>Trades</th>"
            f"<th>Set lots</th><th>Classic PF</th><th>Cost PF</th><th>Win %</th><th>Net avg %</th></tr></thead><tbody>{tl}</tbody></table></div>"
            f"<h2>Exchange-minimum SL floor per symbol</h2><p class='note'>{esc(sl_floor_txt)}</p>"
            f"<h2>Assumptions</h2><ul>{assumptions}</ul>"
            f"<p class='note'>* Axis columns are diagnostics: coordination axes are disabled in the deployed profile, so they do not trade; the column "
            f"shows the executed Set lots whose coord_engine axis child would have qualified.</p>"
            f"</main></body></html>")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True, help="directory with <SYMBOL>.json 1m OHLCV files")
    ap.add_argument("--start-equity", type=float, default=10.0)
    ap.add_argument("--hours", type=int, default=12)
    ap.add_argument("--out", default=os.path.join(ROOT, "reports", "sim-12h-account.json"))
    ap.add_argument("--html", default=os.path.join(ROOT, "reports", "sim-12h-account.html"))
    ap.add_argument("--overlay", default=os.path.join(PULSE, "overlay-bingx-x02.json"))
    ap.add_argument("--contracts", default=None, help="cached public contract specs (fetched from BingX VST if missing)")
    ap.add_argument("--cache", default=None, help="replay cache dir (default $CTS_DATA_DIR/sim12h-cache)")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--leverage", type=int, default=0, help="override max leverage (0 = engine: exchange max, fallback 150)")
    ap.add_argument("--sensitivity-leverage", type=int, default=50, help="extra post-Base run at this leverage (0 = off)")
    ap.add_argument("--symbols", default="", help="comma list (default: all files in --data-dir)")
    ap.add_argument("--mmr-factor", type=float, default=0.5,
                    help="maintenance margin rate = factor / leverage (not public; 0.5 = half the max-leverage initial margin)")
    ap.add_argument("--force", action="store_true", help="recompute the replay cache")
    ap.add_argument("--strategies", default="normal,trailing",
                    help="factor: Set strategies admitted, comma list of normal / trailing")
    ap.add_argument("--axis-filter", default="none",
                    help="factor: admit only candidates whose axis child qualifies: none, any, prev|last|cont|pause or axis:count")
    ap.add_argument("--no-micro", action="store_true", help="factor: admit only Base-qualified Sets (no Micro tier)")
    ap.add_argument("--fee-pct", type=float, default=None,
                    help="round-trip fee the account pays in percent (default: the PositionCost). Gates, PF and the "
                         "TP grid keep using the PositionCost hurdle.")
    ap.add_argument("--profile-wins", action="store_true",
                    help="apply connection_profile.processing_profile over the overlay (old behaviour)")
    args = ap.parse_args(argv)
    global FEE_PCT, PROFILE_WINS
    FEE_PCT = args.fee_pct
    PROFILE_WINS = bool(args.profile_wins)
    _engine_path()
    cache = args.cache or os.path.join(os.environ["CTS_DATA_DIR"], "sim12h-cache")
    os.makedirs(cache, exist_ok=True)
    symbols = [s for s in args.symbols.split(",") if s] or sorted(os.path.basename(f)[:-5] for f in glob.glob(os.path.join(args.data_dir, "*.json")))
    n_all = len(load_symbol(args.data_dir, symbols[0])[0])
    sim_end = n_all
    sim_start = n_all - args.hours * 60
    book = make_book(args.overlay)
    catalog = build_catalog(book)
    contracts_path = args.contracts or os.path.join(cache, "contracts.json")
    contracts, cmeta = load_contracts(contracts_path, symbols)
    missing = [s for s in symbols if s not in contracts]
    if missing:
        raise SystemExit(f"missing contract specs: {missing}")
    ov = deployed_settings(args.overlay)
    # Exchange-accepted SL floor per symbol at the window start: the intern
    # replay is computed with the stops the live desk would really place.
    floor_sizer = Sizer(contracts, ov, args.leverage or None)
    sl_floor = {}
    for s in symbols:
        px0 = float(load_symbol(args.data_dir, s)[0][max(0, sim_start - 1)][3])
        sl_floor[s] = round(floor_sizer.venue_sl_min(contracts[s], px0), 8)
    jobs = [dict(symbol=s, cache=cache, overlay=args.overlay, data_dir=args.data_dir, sim_start=sim_start, force=args.force,
                 sl_floor=sl_floor[s])
            for s in symbols]
    t0 = time.time()
    workers = max(1, min(2, int(args.workers)))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for i, r in enumerate(pool.map(replay_symbol, jobs), 1):
            print(f"[replay {i}/{len(jobs)}] {r}", flush=True)
    print(f"replay stage {time.time() - t0:.0f}s", flush=True)
    caches = load_cache(cache, symbols)
    start_s = caches[symbols[0]]["start_s"]
    for s in symbols:
        if caches[s]["n_all"] != n_all:
            raise SystemExit(f"{s}: bar count mismatch")
    # ---- bars for the window ----
    bars_by_sym = {"close": [], "high": [], "low": [], "ind_mask": []}
    for s in symbols:
        bars, _ = load_symbol(args.data_dir, s)
        bars_by_sym["close"].append([float(b[3]) for b in bars])
        bars_by_sym["high"].append([float(b[1]) for b in bars])
        bars_by_sym["low"].append([float(b[2]) for b in bars])
        bars_by_sym["ind_mask"].append(caches[s]["ind_mask"])
    # lane entry prices come from the same bars
    for s in symbols:
        for r in caches[s]["strat"]:
            r["_entry_px"] = bars_by_sym["close"][symbols.index(s)][r["_entry"]]
    # ---- Stage B ----
    t1 = time.time()
    ddt_cache: Dict = {}
    children = axis_children(ov)
    cands = build_candidates(caches, catalog, symbols, sim_start, sim_end, book, True, ddt_cache, children=children)
    factors = apply_factors(cands, catalog, args.strategies, args.axis_filter, args.no_micro)
    ktable = kind_gate(caches, symbols, book, sim_start, sim_end)
    strat_g = strat_lots(caches, symbols, book, sim_start, sim_end, True)
    strat_u = strat_lots(caches, symbols, book, sim_start, sim_end, False)
    lanes_g = kind_lane_lots(caches, symbols, book, sim_start, sim_end, ktable, True)
    lanes_u = kind_lane_lots(caches, symbols, book, sim_start, sim_end, ktable, False)
    print(f"gating stage {time.time() - t1:.0f}s candidates={len(cands['uid'])} admitted={int(cands['admitted'].sum())}", flush=True)
    runs = []
    base_w, main_w, real_w = book._stage_window_ns()
    specs = [("post-base", f"Post-Base (deployed strict gate: Base last-{base_w} / Main last-{main_w} / Real last-{real_w} >= "
                           f"{float(book.stage_min_pf['base']):.2f}/{float(book.stage_min_pf['main']):.2f}/{float(book.stage_min_pf['real']):.2f}, "
                           f"DDT, kind gate, live-negative deact)", True, args.leverage, "intrabar"),
             ("unfiltered", "Unfiltered (all Sets, no stage gates)", False, args.leverage, "intrabar")]
    specs.append(("post-base-closeliq", "Post-Base, liquidation tested on 1m close equity (less pessimistic than simultaneous intrabar extremes)",
                  True, args.leverage, "close"))
    if args.sensitivity_leverage:
        specs.append(("post-base-lev%d" % args.sensitivity_leverage, f"Post-Base, leverage sensitivity {args.sensitivity_leverage}x", True, args.sensitivity_leverage, "intrabar"))
    for name, title, gated, lev, liq_mode in specs:
        t2 = time.time()
        sizer = Sizer(contracts, ov, lev or None)
        res = simulate(name, cands, strat_g if gated else strat_u, ktable if gated else None, catalog, symbols, bars_by_sym,
                       sim_start, sim_end, start_s, book, sizer, args.start_equity, gated, bool(book.live_negative_deact),
                       mmr_factor=args.mmr_factor, eq_min=float(sizer.pt.EQ_MIN), liq_mode=liq_mode,
                       kind_lanes=lanes_g if gated else lanes_u, max_open=int(ov.get("maxOpen") or 100),
                       dd_pause_pct=float(ov.get("ddPausePct") or 0.0),
                       side_cap=int(ov.get("maxLotsPerSymbolSide") or 0))
        res["title"] = title
        res["leverage"] = lev or "exchange max (engine fallback 150)"
        runs.append(res)
        print(f"run {name}: {time.time() - t2:.0f}s equity_end={res['totals']['equityEnd']:.4f}", flush=True)
    cost_pct = float(book.cost_pct)
    trade_level = {
        "unfiltered (all Set trades entering in window)": tape_metrics(cands, catalog, np.ones(len(cands["uid"]), bool), cost_pct),
        f"Base passed (last-{book._stage_window_ns()[0]} >= {float(book.stage_min_pf['base']):.2f})": tape_metrics(cands, catalog, cands["base_ok"], cost_pct),
        "Base+Main+Real passed": tape_metrics(cands, catalog, cands["real_ok"], cost_pct),
        f"Micro passed ({float(getattr(book, 'micro_min_pf', 0.0) or 0.0):.2f} <= PF < floor)": tape_metrics(cands, catalog, cands["micro_ok"], cost_pct),
        "admitted (Base/Main/Real + Micro + DDT)": tape_metrics(cands, catalog, cands["admitted"], cost_pct),
    }
    trade_level["per distinct signal · admitted (top in-sample PF per signal)"] = per_signal_metrics(cands, cands["admitted"], cost_pct)
    trade_level["per distinct signal · Base+Main+Real"] = per_signal_metrics(cands, cands["real_ok"], cost_pct)
    trade_level["per distinct signal · unfiltered"] = per_signal_metrics(cands, np.ones(len(cands["uid"]), bool), cost_pct)
    for a in AXIS_NAMES:
        trade_level[f"admitted & axis {a} child qualifies"] = tape_metrics(cands, catalog, cands["admitted"] & cands["axes"][a], cost_pct)
    # one row per axis child (axis:count), trade level and per distinct signal
    for a, c in children:
        mask = cands["admitted"] & cands["axes"][f"{a}:{c}"]
        trade_level[f"admitted & axis {a}:{c} qualifies"] = tape_metrics(cands, catalog, mask, cost_pct)
        trade_level[f"per distinct signal · admitted & axis {a}:{c}"] = per_signal_metrics(cands, mask, cost_pct)
    def lane_metrics(rows):
        mv = np.array([r["pnl_pct"] for r in rows], dtype=float)
        if not len(mv):
            return dict(n=0)
        net = mv - cost_pct / 100.0
        gl = float(-net[net < 0].sum())
        return dict(trades=int(len(mv)), lots=int(len(mv)), classicPf=round(float(net[net > 0].sum()) / gl if gl > 0 else 99.0, 4),
                    costPf=round(cost_pf_ratio(mv.tolist(), cost_pct), 4), winRate=round(float((net > 0).mean()) * 100, 2),
                    netAvgPct=round(float(net.mean()) * 100, 4))
    trade_level["kind lanes unfiltered"] = lane_metrics(lanes_u)
    trade_level["kind lanes admitted (config/kind PF validated)"] = lane_metrics([r for r in lanes_g if r["_ok"]])
    for k in IND_KINDS:
        unf = [r for r in lanes_u if r["kind"] == k]
        if unf:
            trade_level[f"kind lane {k} unfiltered"] = lane_metrics(unf)
        sel = [r for r in lanes_g if r["_ok"] and r["kind"] == k]
        if sel:
            trade_level[f"kind lane {k} admitted"] = lane_metrics(sel)
    H = args.hours
    tl_post = trade_level_hourly(cands, catalog, cands["admitted"] & kind_ok_mask(cands, catalog, ktable, bars_by_sym, sim_start),
                                 strat_g, True, sim_start, H, cost_pct)
    tl_unf = trade_level_hourly(cands, catalog, np.ones(len(cands["uid"]), bool), strat_u, False, sim_start, H, cost_pct)
    open_end = sum(int(caches[s]["open_end"]) for s in symbols)
    report = dict(
        factors=factors,
        generatedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        window=dict(simStartBar=sim_start, simEndBar=sim_end, startUtc=time.strftime("%Y-%m-%d %H:%M", time.gmtime(start_s + sim_start * BAR)),
                    endUtc=time.strftime("%Y-%m-%d %H:%M", time.gmtime(start_s + sim_end * BAR)),
                    replayFromBar=int(caches[symbols[0]]["lo"])),
        symbols=symbols, startEquity=args.start_equity, costPct=cost_pct, feePct=(cost_pct if FEE_PCT is None else FEE_PCT), startS=start_s,
        leverage=args.leverage or "exchange max; not public -> engine fallback 150 (Contract.max_lev / LEVERAGE)",
        contracts=cmeta,
        slFloorPct={s: round(v * 100, 4) for s, v in sl_floor.items()},
        engine=dict(overlay=os.path.relpath(args.overlay, ROOT), catalogSets=len(book.sets), uniqueBehaviours=len(catalog),
                    baseN=book.pf_n, mainN=book.main_eval, realN=book.real_eval, floors=book.stage_min_pf,
                    maxDdS=book.max_dd_s, strictGate=book.strict_gate, costPct=cost_pct, timeBars=book.hist_time_bars,
                    scratchS=book.scratch_s, lookbackBars=int(ov.get("histLookbackBars")), openAtEndReconstructed=open_end,
                    axes={k: bool(ov.get(k)) for k in ("axisPrevEnabled", "axisLastEnabled", "axisContEnabled", "axisPauseEnabled")},
                    targetNotional=float(ov.get("targetNotional")), volumeFactor=float(ov.get("volumeFactor") or 0.1),
                    posCountsVolumeRatio=float(ov.get("posCountsVolumeRatio") or 0), maxOpen=int(ov.get("maxOpen")),
                    symbolCap=int(ov.get("symbolCap"))),
        gate=dict(candidatesInWindow=int(len(cands["uid"])), evidenceCloses=int(cands["n_evidence"]),
                  evidenceDepthAtWindowStart=cands["evidence_depth"],
                  baseOk=int(cands["base_ok"].sum()), mainOk=int(cands["main_ok"].sum()), realOk=int(cands["real_ok"].sum()),
                  ddtBlocked=cands["ddt_blocked"], ddtMaxS=cands["ddt_max_s"], admitted=int(cands["admitted"].sum()),
                  replayClosesTotal=int(sum(int(caches[s].get("closes_total", 0)) for s in symbols))),
        tradeLevel=trade_level,
        runs=runs,
        tradeLevelHourly={"post-base": tl_post, "unfiltered": tl_unf},
        tradeLevelMarkdown={"post-base": trade_level_markdown(tl_post, start_s, sim_start),
                            "unfiltered": trade_level_markdown(tl_unf, start_s, sim_start)},
        assumptions=[assumption_text(a, book) for a in ASSUMPTIONS_TEMPLATE],
    )
    for r in runs:
        r["markdown"] = markdown_table(r)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(report, f, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    if args.html:
        with open(args.html, "w") as f:
            f.write(html_report(report))
    print(runs[0]["markdown"])
    print(report["tradeLevelMarkdown"]["post-base"])
    print(f"wrote {args.out} {args.html}")
    return 0


ASSUMPTIONS_TEMPLATE = [
    "ENGINE-COMPUTED: every Set trade (2 packs x 30 SL:TP ratios x TP steps 7-22 x Normal + 25 trailing pairs = 24,960 Sets) comes from "
    "SetBook._replay_core_vectorized with the deployed profile (overlay-bingx-x02.json + connection_profile.processing_profile()); "
    "Sets with identical bound SL/TP/trailing share one tape but stay separate lots (multiplicity).",
    "ENGINE-COMPUTED: Block and DCA trades are the histSimulateBlock/histSimulateDca lanes of SetBook._replay_symbol (pack lanes seeded from "
    "the first Normal Set of each pack plus per-kind block:<kind> lanes); indication-kind evidence from SetBook._replay_kind_tapes.",
    "Replay window = engine lookback (histLookbackBars {lookback}) + 60-bar frame before the simulation start, replayed continuously through the "
    "window (the live engine re-replays a rolling 2-day window; continuous replay is used instead).",
    "Positions still open at the end of data are reconstructed from the engine entry rule (first signal >= cooldown after the last close); "
    "Block/DCA lanes open at the end are not reconstructed.",
    "STAGE CHAIN (walk-forward): a Set trade executes only if, from closes strictly before its entry bar, the Set x direction passes "
    "Base last-{base} cost-PF ratio >= {floor} (n >= {need}), Main last-{main} >= {mainFloor}, Real last-{real} >= {realFloor} (strict gate / _real_metrics_ok) and "
    "DD-time <= setMaxDdTimeS (engine drawdown_time_by_symbol on the last {ddt} closes, refreshed hourly). Evidence is pooled over all symbols "
    "as in the SetState tape; rejected Sets keep producing evidence. Live closes are not added back into Base evidence (they duplicate replay trades).",
    "Indications-pack Sets additionally need SetBook.indication_ok(kind, side) for at least one kind contributing to the pack vote (kind tape "
    "last-{base} >= {floor}). Live-negative deactivation (last 25 own executed closes net < 0) blocks a Set x side.",
    "Block lanes use score_block_main Real-overall last-50 (n < 50 = valid, engine rule; n >= 50 needs is_positive_pf and floor); "
    "DCA lanes use DcaBook.score (last-15 PF >= floor, last-25 avgR >= 0). Both need an open executed Set lot on the same symbol x direction.",
    "Coordination axes prev/last/cont/pause are diagnostics (as live: children never place, gate or size orders). Every child "
    "axis:count from coord_engine.AXIS_SPECS up to the overlay max window is evaluated per Set x direction like "
    "coord_engine.axis_variants: prev = the c closes before the last c (needs 2c), last/cont/pause = the last min(c, available) "
    "closes (>= min(3, c)), PF >= the Base floor; pause is off while the last c closes are all cost-net losses. Per-axis columns "
    "mean 'any child of that axis qualifies'.",
    "SIZING: pulse_trader.Pulse.size_qty/sized_notional/raise_to_min_qty/min_order_qty/leverage_for called on a stub: targetNotional 2.15 x "
    "volumeFactor 1 x vol1h factor x Coordinator.size_mult(open lots) raised to the venue minimum (tradeMinQuantity / tradeMinUSDT, step "
    "rounding) from the cached public BingX VST contracts endpoint. Block extra = parent x block ratio raised to the venue minimum.",
    "LEVERAGE: exchange max leverage is not on a public endpoint; the engine fallback (Contract.max_lev = LEVERAGE = 150) is used; a 50x "
    "sensitivity run is included.",
    "MARGIN: cross margin; used margin = sum(notional / leverage); available = MTM equity - used margin; an entry is skipped when the venue-minimum "
    "margin exceeds 95% of available (pulse_trader entry check) or size_qty returns 0 (room = available x leverage x 0.9). Skips are counted.",
    "ORDER SEQUENCE: per minute, exits first, then entries at the bar close in EntryMatrix order (round-robin over symbol/side/pack signals, "
    "Sets by highest Base-window PF). Entry burst limits, rate limits, latency, slippage and funding are not modelled; fills at the replay prices.",
    "COSTS: engine PositionCost 0.1% of entry notional per round trip (overlay positionCostPct), half charged on entry and half on close. "
    "PF normal = classic PF of lot USDT results net of cost; PF gross excludes cost; Cost PF = engine ratio 1 + 0.1 x avgR.",
    "CONTROL ORDERS (controlOrdersOverall): one SL+TP pair per symbol x direction; per minute a newly occupied group places 2, a changed "
    "occupied group cancel-replaces 2, an emptied group cancels 2. Lot exits are counted as close fills by engine reason (sl/tp/time/scratch+).",
    "LIQUIDATION (account layer): BingX maintenance-margin tiers are not public; MMR = 0.5 / leverage (half the max-leverage initial "
    "margin). When the intrabar worst equity is at or below the maintenance margin of all open lots, the whole cross-margin book is "
    "closed at the bar's adverse extremes and the maintenance margin is forfeited (balance floored at 0). pulse_trader's EQ_MIN "
    "(0.20 USDT) halts new entries below that equity; drawdownHaltPct is 0 (off) in the overlay.",
    "LIMITS: maxOpen 100 effective positions (symbol x direction) and 50 symbols never bind with 48 symbols (max 96 groups); lots per group unlimited.",
    "DRAWDOWN: equity marked every minute at closes; intrabar worst uses low (longs) / high (shorts) of every open lot. DDT: engine "
    "drawdown_time_by_symbol on closed lots (seconds) and account underwater time (minutes below running equity peak).",
]


if __name__ == "__main__":
    raise SystemExit(main())
