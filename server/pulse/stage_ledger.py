"""Causal Base → Main → Real stages and forward PF reporting.

Shared by the hourly lane at run time and by the walk-forward research
(scripts/hourly_walkforward.py), so both take exactly the same decisions.
Every decision at time t reads only closes whose exit time is before t:

- Base(t): the Set's own closes in the rolling window [t - long_h, t):
  at least min_trades, net > 0, return PF >= min_pf and a positive lower
  confidence bound LCB = sum(r) - sd(r)·sqrt(n).
- Main(t): Base passes and the pre-window [t - pre_h, t) is not negative.
- Real(t): one seat per pair, the best Main cell by LCB; the top
  `portfolio` pairs are seated.
- Live (at entry): the Set's last-N closes on that side, if it has N, hold
  PF >= last_n_min_pf and net >= 0.

Unlike the 1m SetBook, Main and Real are never the tail of the Base tape:
they are separate decisions, and the reported PF of every stage is the PF
of trades entered *after* that stage admitted the Set (forward_by_stage).
All arrays are vectorized over decision times so a year of hourly steps
over every cell costs seconds.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
from position_cost import RATIO_SCALE, POSITION_COST_PCT_DEFAULT, PF_MIN

H_MS = 3_600_000
HOURLY_POSITION_COST_PCT = 0.20  # AO simulator round-trip cost, in percent


@dataclass
class StageConfig:
    long_h: int = 336
    pre_h: int = 20
    min_trades: int = 12
    min_pf: float = 1.10
    last_n: int = 12
    last_n_min_pf: float = 1.0
    portfolio: int = 16

    @classmethod
    def from_overlay(cls, ov: Optional[Dict]) -> "StageConfig":
        ov = ov or {}
        keys = {"long_h": "hourlyLongH", "pre_h": "hourlyPreH", "min_trades": "hourlyMinTrades", "min_pf": "hourlyMinPf",
                "last_n": "hourlyLastN", "last_n_min_pf": "hourlyLastNMinPf", "portfolio": "hourlyPortfolio"}
        cfg = cls()
        for f in fields(cls):
            raw = ov.get(keys[f.name])
            if raw is None:
                continue
            try:
                setattr(cfg, f.name, type(getattr(cfg, f.name))(raw))
            except (TypeError, ValueError):
                pass
        return cfg


class Tape:
    """One Set's closed trades ordered by exit time, with prefix sums."""

    def __init__(self, entry_t: Sequence[float], exit_t: Sequence[float], r: Sequence[float], side: Sequence[int],
                 cost_pct=HOURLY_POSITION_COST_PCT) -> None:
        entry, exits, returns = (np.asarray(x, dtype=np.float64) for x in (entry_t, exit_t, r))
        costs = np.broadcast_to(np.asarray(cost_pct, dtype=np.float64), returns.shape)
        valid = np.isfinite(entry) & np.isfinite(exits) & np.isfinite(returns)
        order = np.flatnonzero(valid)[np.argsort(exits[valid], kind="stable")]
        self.entry_t, self.exit_t, self.r = entry[order], exits[order], returns[order]
        self.side = np.asarray(side, dtype=np.int8)[order]
        self.cost_pct = costs[order]
        references = _cost_references(self.cost_pct)
        self._cs = self._prefix(self.r)
        self._cs["returnR"] = np.concatenate([np.zeros(1), np.cumsum(self.r / references)])

    @staticmethod
    def _prefix(r: np.ndarray) -> Dict[str, np.ndarray]:
        z = np.zeros(1)
        return {"s": np.concatenate([z, np.cumsum(r)]), "s2": np.concatenate([z, np.cumsum(r * r)]),
                "gp": np.concatenate([z, np.cumsum(np.where(r > 0, r, 0.0))]),
                "gl": np.concatenate([z, np.cumsum(np.where(r <= 0, -r, 0.0))])}

    def __len__(self) -> int:
        return len(self.r)

    def side_tape(self, side: int) -> "Tape":
        m = self.side == side
        return Tape(self.entry_t[m], self.exit_t[m], self.r[m], self.side[m], self.cost_pct[m])

    def idx_before(self, t: np.ndarray) -> np.ndarray:
        """Number of closes with exit_t < t (strictly before the decision)."""
        return np.searchsorted(self.exit_t, t, side="left")

    def range_stats(self, lo: np.ndarray, hi: np.ndarray) -> Dict[str, np.ndarray]:
        cs = self._cs
        n = (hi - lo).astype(np.float64)
        s = cs["s"][hi] - cs["s"][lo]
        s2 = cs["s2"][hi] - cs["s2"][lo]
        gp = cs["gp"][hi] - cs["gp"][lo]
        gl = cs["gl"][hi] - cs["gl"][lo]
        return {"n": n, "net": s, "s2": s2, "gp": gp, "gl": gl,
                "returnR": cs["returnR"][hi] - cs["returnR"][lo]}

    def window(self, t: np.ndarray, hours: float) -> Dict[str, np.ndarray]:
        hi = self.idx_before(t)
        lo = self.idx_before(t - hours * H_MS)
        return self.range_stats(lo, hi)

    def last_n(self, t: np.ndarray, n: int) -> Dict[str, np.ndarray]:
        hi = self.idx_before(t)
        lo = np.maximum(0, hi - n)
        return self.range_stats(lo, hi)


def pf_of(gp: np.ndarray, gl: np.ndarray) -> np.ndarray:
    """Classic PF; loss-free windows with profit read as 4 (A-O PF_NO_LOSS)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        pf = np.where(gl > 0, gp / np.where(gl > 0, gl, 1.0), np.where(gp > 0, 4.0, 0.0))
    return pf


def return_pf_of(st: Dict[str, np.ndarray]) -> np.ndarray:
    """Return PF from each trade's already cost-net result, never GP/GL."""
    return 1.0 + RATIO_SCALE * st["returnR"] / np.maximum(st["n"], 1)


def _cost_references(costs: np.ndarray) -> np.ndarray:
    """Vector equivalent of pf_reference_cost_pct for the hot hourly tape."""
    return np.where(np.isfinite(costs) & (costs > 0), costs, POSITION_COST_PCT_DEFAULT) / 100.0


def lcb_of(st: Dict[str, np.ndarray]) -> np.ndarray:
    n = st["n"]
    with np.errstate(divide="ignore", invalid="ignore"):
        mean = np.where(n > 0, st["net"] / np.maximum(n, 1), 0.0)
        var = np.where(n > 1, (st["s2"] - n * mean * mean) / np.maximum(n - 1, 1), 0.0)
    return st["net"] - np.sqrt(np.maximum(var, 0.0)) * np.sqrt(n)


def base_eval(tape: Tape, t: np.ndarray, cfg: StageConfig) -> Dict[str, np.ndarray]:
    st = tape.window(t, cfg.long_h)
    pf = return_pf_of(st)
    lcb = lcb_of(st)
    ok = ((st["n"] >= max(3, cfg.min_trades)) & (st["net"] > 0)
          & (pf > PF_MIN + 1e-9) & (pf + 1e-9 >= cfg.min_pf) & (lcb > 0))
    return {"ok": ok, "n": st["n"], "pf": pf, "classicPf": pf_of(st["gp"], st["gl"]), "net": st["net"], "lcb": lcb}


def main_eval(tape: Tape, t: np.ndarray, cfg: StageConfig, base_ok: np.ndarray) -> np.ndarray:
    if cfg.pre_h <= 0:
        return base_ok.copy()
    st = tape.window(t, cfg.pre_h)
    pf = return_pf_of(st)
    # Fewer than 3 closes in the pre-window: no evidence against, Main follows Base.
    pre_ok = (st["n"] < 3) | ((st["net"] >= 0) & (pf > PF_MIN + 1e-9) & (pf + 1e-9 >= cfg.min_pf))
    return base_ok & pre_ok


def live_ok(side_tape: Tape, t: np.ndarray, cfg: StageConfig) -> np.ndarray:
    """Last-N gate on the Set's own closes of one side (fewer than N → open)."""
    if cfg.last_n <= 0:
        return np.ones(len(t), dtype=bool)
    st = side_tape.last_n(t, cfg.last_n)
    pf = return_pf_of(st)
    full = st["n"] >= cfg.last_n
    return ~full | ((pf >= cfg.last_n_min_pf) & (st["net"] >= 0))


def seat(score: np.ndarray, portfolio: int) -> np.ndarray:
    """score[pair, cell, time] (-inf = not Main) → seated cell per pair and time (-1 = none).

    The best cell of each pair competes; the top `portfolio` pairs by score
    are seated (0 = every pair with a Main cell).
    """
    best_cell = np.argmax(score, axis=1)
    best = np.take_along_axis(score, best_cell[:, None, :], axis=1)[:, 0, :]
    seated = np.where(np.isfinite(best), best_cell, -1)
    if portfolio > 0 and score.shape[0] > portfolio:
        rank = np.argsort(np.argsort(-best, axis=0, kind="stable"), axis=0, kind="stable")
        seated = np.where(rank < portfolio, seated, -1)
    return seated


def events(entry_t: np.ndarray, side: np.ndarray, bucket_h: float = 4.0) -> int:
    if not len(entry_t):
        return 0
    keys = np.stack([side.astype(np.int64), np.floor(entry_t / (bucket_h * H_MS)).astype(np.int64)], axis=1)
    return int(len(np.unique(keys, axis=0)))


def summarize(r: np.ndarray, cost_pct=HOURLY_POSITION_COST_PCT) -> Dict[str, float]:
    r = np.asarray(r, dtype=np.float64)
    costs = np.broadcast_to(np.asarray(cost_pct, dtype=np.float64), r.shape)
    valid = np.isfinite(r)
    r, costs = r[valid], costs[valid]
    gp = float(r[r > 0].sum())
    gl = float(-r[r <= 0].sum())
    n = len(r)
    refs = _cost_references(costs)
    pf = 1.0 + RATIO_SCALE * float((r / refs).mean()) if n else 1.0
    return {"n": n, "pf": pf, "classicPf": float(pf_of(np.array(gp), np.array(gl))), "net": float(r.sum() * 100),
            "wr": float((r > 0).mean()) if n else 0.0, "avg": float(r.mean() * 100) if n else 0.0,
            "costSubtracted": True, "sampleAvailable": bool(n)}


def forward_by_stage(stage_at_entry: Sequence[str], r: Sequence[float], cost_pct=HOURLY_POSITION_COST_PCT) -> Dict[str, Dict[str, float]]:
    """PF of trades entered while the Set held each stage (or a higher one)."""
    order = {"": 0, "Base": 1, "Main": 2, "Real": 3, "Live": 4}
    st = np.array([order.get(s, 0) for s in stage_at_entry])
    rr = np.asarray(r, dtype=np.float64)
    costs = np.broadcast_to(np.asarray(cost_pct, dtype=np.float64), rr.shape)
    return {name: summarize(rr[st >= lvl], costs[st >= lvl]) for name, lvl in (("Base", 1), ("Main", 2), ("Real", 3), ("Live", 4))}


def lane_guard(executed_r: Sequence[float], n: int = 100, stop_below: float = 0.95, resume_at: float = 1.10,
               shadow_r: Optional[Sequence[float]] = None, live_orders: bool = True,
               cost_pct: float = HOURLY_POSITION_COST_PCT) -> Dict[str, object]:
    """Stop live orders when the last n executed trades fall below stop_below; resume on shadow evidence."""
    n = max(1, int(n))
    ex = np.asarray(list(executed_r), dtype=np.float64)
    ex = ex[np.isfinite(ex)][-n:]
    pf = summarize(ex, cost_pct)["pf"] if len(ex) >= n else None
    if live_orders and pf is not None and pf < stop_below:
        return {"liveOrders": False, "reason": f"last {n} executed PF {pf:.2f} < {stop_below}", "pf": pf}
    if not live_orders:
        sh = np.asarray(list(shadow_r) if shadow_r is not None else [], dtype=np.float64)
        sh = sh[np.isfinite(sh)][-n:]
        spf = summarize(sh, cost_pct)["pf"] if len(sh) >= n else None
        if spf is not None and spf >= resume_at:
            return {"liveOrders": True, "reason": f"shadow PF {spf:.2f} >= {resume_at}", "pf": spf}
        return {"liveOrders": False, "reason": "waiting for shadow evidence", "pf": spf}
    return {"liveOrders": True, "reason": "ok", "pf": pf}
