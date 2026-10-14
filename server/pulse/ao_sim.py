"""CTS-A-O bar simulator and trade statistics for the hourly lane.

Ported from CTS-A-O src/core/sim/backtest.ts (simulate, splitSides,
mergeSideTrades), the base cells of walkforward.ts protectGrid and
src/core/metrics/stats.ts. The rules that make it honest:

- a signal is decided on the close of bar i and entered at the open of i+1;
- intrabar order is unknown, so the stop is checked before the target;
- after the entry bar, a stop or target crossed by a gap fills at the open;
- the trailing stop tightens from completed bars and applies from the next bar;
- every closed trade pays the round-trip cost on notional.

Long and short run on their own side-filtered copy of a signal (one slot
per config x symbol x side), exactly as A-O does.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np

from ao_indications import js_num

RT_COST = 0.002  # 0.1 % per side, round trip (A-O config.ts RT_COST)
PF_NO_LOSS = 4.0  # PF reported for wins without losses (A-O config.ts)
EVAL_MIN_SL = 0.005  # minimum evaluated stop (A-O minimal-coord.ts)
H_MS = 3_600_000


@dataclass(frozen=True)
class Cell:
    """One protect cell: fractions of price and a hold in bars."""

    tp: float
    sl: float
    trail: float
    hold: int
    trail_step: float = 1.0
    trail_free: bool = False

    @property
    def id(self) -> str:
        return f"tp{pct(self.tp)}|sl{pct(self.sl)}|tr{pct(self.trail)}|h{self.hold}"


def pct(x: float) -> str:
    """A fraction as a short percent string ('0.052' → '5.2')."""
    return js_num(float(f"{x * 100:.4f}"))


def grid_cells(grid: Dict, tf_min: int = 60) -> List[Cell]:
    """The base protect cells of a grid (walkforward.ts protectGrid)."""
    sl_floor = float(grid.get("minSlEval", EVAL_MIN_SL))
    step = float(grid.get("trailStep", 1))
    free = bool(grid.get("trailFree", False))
    out: List[Cell] = []
    seen = set()
    for tp in grid["tp"]:
        for k in grid["slOfTp"]:
            for tr in grid["trailOfTp"]:
                for h in grid["holdH"]:
                    sl = float(f"{max(sl_floor, float(grid.get('minSl', 0)), tp * k):.4f}")
                    trail = float(f"{max(float(grid.get('minTrail', 0)) / step, tp * tr):.4f}") if tr > 0 else 0.0
                    hold = max(2, int(math.floor((h * 60) / tf_min + 0.5)))
                    cell = Cell(float(tp), sl, trail, hold, step if trail > 0 else 1.0, free if trail > 0 else False)
                    key = (cell.tp, cell.sl, cell.trail, cell.hold)
                    if key not in seen:
                        seen.add(key)
                        out.append(cell)
    return out


@dataclass
class Trade:
    cfg: str
    sym: str
    side: int
    entry_t: float
    exit_t: float
    entry: float
    exit: float
    r: float
    reason: str
    bars: int
    mfe: float
    mae: float
    funding: float = 0.0
    meta: Dict = field(default_factory=dict)

    def to_ao_dict(self) -> Dict:
        return {"cfg": self.cfg, "sym": self.sym, "side": self.side, "entryT": self.entry_t, "exitT": self.exit_t,
                "entry": self.entry, "exit": self.exit, "r": self.r, "reason": self.reason, "bars": self.bars,
                "mfe": self.mfe, "mae": self.mae}


@dataclass
class OpenPosition:
    cfg: str
    sym: str
    side: int
    entry_t: float
    entry_i: int
    entry: float
    stop: float
    target: float
    peak: float
    trail_on: bool
    mtm: float


def simulate(cfg: str, bars, sig: np.ndarray, cell: Cell, cost: float = RT_COST, cooldown: int = 0):
    """Single-slot simulation of one signal. Returns (trades, open position or None)."""
    n = len(bars.c)
    t, o, h, l, c = bars.t.tolist(), bars.o.tolist(), bars.h.tolist(), bars.l.tolist(), bars.c.tolist()
    sym = bars.sym
    tf_ms = bars.tf_min * 60_000
    s = sig.tolist()
    trades: List[Trade] = []
    in_pos = False
    side = 1
    entry_i = 0
    entry = stop = target = peak = 0.0
    trail_on = False
    mfe = mae = 0.0
    next_allowed = 0
    dist = cell.trail * cell.trail_step
    entries = np.flatnonzero(sig).tolist()
    ep = 0

    def close(i, ex, reason, exit_t):
        trades.append(Trade(cfg, sym, side, t[entry_i], exit_t, entry, ex, (side * (ex - entry)) / entry - cost,
                            reason, i - entry_i + 1, mfe, mae))

    i = 0
    while i < n:
        if in_pos and i >= entry_i:
            gap = i > entry_i
            bar_end = t[i] + tf_ms
            closed = False
            if side == 1:
                up = (h[i] - entry) / entry
                dn = (entry - l[i]) / entry
                if up > mfe:
                    mfe = up
                if dn > mae:
                    mae = dn
                if l[i] <= stop:
                    close(i, min(o[i], stop) if gap else stop, "trail" if trail_on else "sl", bar_end)
                    closed = True
                elif h[i] >= target and not (trail_on and cell.trail_free):
                    close(i, max(o[i], target) if gap else target, "tp", bar_end)
                    closed = True
            else:
                up = (entry - l[i]) / entry
                dn = (h[i] - entry) / entry
                if up > mfe:
                    mfe = up
                if dn > mae:
                    mae = dn
                if h[i] >= stop:
                    close(i, max(o[i], stop) if gap else stop, "trail" if trail_on else "sl", bar_end)
                    closed = True
                elif l[i] <= target and not (trail_on and cell.trail_free):
                    close(i, min(o[i], target) if gap else target, "tp", bar_end)
                    closed = True
            if closed:
                in_pos = False
                next_allowed = i + cooldown
            else:
                if i - entry_i + 1 >= cell.hold:
                    close(i, c[i], "time", bar_end)
                    in_pos = False
                    next_allowed = i + cooldown
                elif cell.trail > 0:
                    if side == 1:
                        if h[i] > peak:
                            peak = h[i]
                        if (peak - entry) / entry >= cell.trail:
                            trail_on = True
                            lvl = peak * (1 - dist)
                            if lvl > stop:
                                stop = lvl
                    else:
                        if l[i] < peak:
                            peak = l[i]
                        if (entry - peak) / entry >= cell.trail:
                            trail_on = True
                            lvl = peak * (1 + dist)
                            if lvl < stop:
                                stop = lvl
        if not in_pos:
            start = i if i >= next_allowed else min(next_allowed, n)
            while ep < len(entries) and entries[ep] < start:
                ep += 1
            j = entries[ep] if ep < len(entries) else n
            if j + 1 >= n:
                break
            i = j
            in_pos = True
            side = 1 if s[i] > 0 else -1
            entry_i = i + 1
            entry = o[i + 1]
            stop = entry * (1 - cell.sl) if side == 1 else entry * (1 + cell.sl)
            target = entry * (1 + cell.tp) if side == 1 else entry * (1 - cell.tp)
            peak = entry
            trail_on = False
            mfe = mae = 0.0
        i += 1
    open_pos = None
    if in_pos:
        last = c[n - 1]
        open_pos = OpenPosition(cfg, sym, side, t[entry_i], entry_i, entry, stop, target, peak, trail_on,
                                (side * (last - entry)) / entry - cost)
    return trades, open_pos


def split_sides(sig: np.ndarray) -> List[np.ndarray]:
    """[sig] when it holds one side only, else [long only, short only]."""
    has_l = bool(np.any(sig > 0))
    has_s = bool(np.any(sig < 0))
    if has_l and has_s:
        return [np.where(sig > 0, sig, 0).astype(np.int8), np.where(sig < 0, sig, 0).astype(np.int8)]
    return [sig]


def merge_side_trades(lists: Sequence[List[Trade]]) -> List[Trade]:
    if len(lists) == 1:
        return list(lists[0])
    out = [tr for lst in lists for tr in lst]
    out.sort(key=lambda a: (a.exit_t, a.entry_t, -a.side))
    return out


def simulate_both(cfg: str, bars, sig: np.ndarray, cell: Cell, cost: float = RT_COST, cooldown: int = 0):
    """Long and short independently; trades merged by exit, entry, then side."""
    runs = [simulate(cfg, bars, s, cell, cost, cooldown) for s in split_sides(sig)]
    return merge_side_trades([r[0] for r in runs]), [r[1] for r in runs if r[1] is not None]


def apply_funding(trades: Iterable[Trade], funding_t: np.ndarray, funding_rate: np.ndarray) -> List[Trade]:
    """Charge funding settled while a trade was open.

    A settlement at time T is paid by positions open at T: entry_t < T and
    T <= exit_t (exit_t is the exit bar's end). Longs pay a positive rate,
    shorts receive it.
    """
    out = []
    for tr in trades:
        lo = int(np.searchsorted(funding_t, tr.entry_t, side="right"))
        hi = int(np.searchsorted(funding_t, tr.exit_t, side="right"))
        f = float(np.sum(funding_rate[lo:hi])) if hi > lo else 0.0
        cp = Trade(**{**asdict(tr), "meta": dict(tr.meta)})
        cp.funding = -tr.side * f
        cp.r = tr.r + cp.funding
        out.append(cp)
    return out


def profit_factor(gp: float, gl: float) -> float:
    if gl <= 0:
        return PF_NO_LOSS if gp > 0 else 0.0
    return gp / gl


def stats_of(trades: Sequence[Trade]) -> Dict[str, float]:
    """n, PF, net % and win rate, A-O semantics (PF_NO_LOSS for loss-free samples)."""
    n = len(trades)
    if not n:
        return {"n": 0, "pf": 0.0, "net": 0.0, "wr": 0.0, "gp": 0.0, "gl": 0.0}
    gp = sum(t.r for t in trades if t.r > 0)
    gl = -sum(t.r for t in trades if t.r <= 0)
    wins = sum(1 for t in trades if t.r > 0)
    return {"n": n, "pf": profit_factor(gp, gl), "net": sum(t.r for t in trades) * 100, "wr": wins / n, "gp": gp, "gl": gl}
