#!/usr/bin/env python3
"""Causal hourly walk-forward of the A-O pairs on cached BingX 1h data.

For each research period:
1. the universe is chosen before the period (scripts/research_periods.py);
2. every pair × exit cell × symbol is simulated over the whole window (ao_sim)
   — the shadow trades; shared per (period, universe, cells, tactics) in a cache;
3. hour by hour, stage_ledger decides Base / Main / Real from closes that
   exited before that hour only, and a shadow trade executes when its Set is
   seated at its entry hour and its last-N side gate passes;
4. executed trades (with funding) are judged: PF at 0.2 % and 0.4 % cost,
   both sides, events, orders/day, weekly blocks, and the PF of trades
   entered after each stage admitted their Set.

    python scripts/hourly_walkforward.py --periods B,C,T --select A --symbols 40 --out reports/hourly-wf
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server", "pulse"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import ao_indications as AI  # noqa: E402
import ao_sim as AS  # noqa: E402
import research_periods as RP  # noqa: E402
import stage_ledger as SL  # noqa: E402

H_MS = SL.H_MS
D_MS = 24 * H_MS
DEFAULT_GRID = {"tp": [0.03, 0.05, 0.08], "slOfTp": [1, 2], "trailOfTp": [0, 0.5], "minTrail": 0.006, "minSl": 0.01, "holdH": [16, 24]}
ROBUST = [
    "follow|rsi-mom-14-15", "follow|rsi-mom-21-20", "follow|rsi-mom-21-25", "follow|bb-walk@x4", "follow|break-vol-2@x4",
    "follow|break-vol@x4", "follow|break-atr-2@x4", "follow|act-burst-2.5@x4", "revert|act-chop@x4", "revert|cci-14-200@x4",
    "revert|cci-40-200@x4", "revert|z-50-2.5@x4",
]
WARMUP_H = 400  # indicator and volatility-rank warm-up before the first decision


# ── shadow trades ──────────────────────────────────────────
def _sim_symbol(job) -> Dict:
    sym, arr, pairs, grid, tactics, cost = job
    b = AI.bars_from_arrays(sym, arr)
    k = AI.SeriesCache(b)
    cells = AS.grid_cells(grid)
    rows = []
    for pi, pair in enumerate(pairs):
        sig = AI.pair_signal(pair, k, tactics)
        if not sig.any():
            continue
        for ci, cell in enumerate(cells):
            trades, _ = AS.simulate_both(pair, b, sig, cell, cost)
            for t in trades:
                rows.append((pi, ci, t.side, t.entry_t, t.exit_t, t.r, t.entry, t.exit))
    a = np.array(rows, dtype=np.float64).reshape(-1, 8)
    return {"sym": sym, "rows": a}


def shadow_trades(bars: Dict[str, Dict[str, np.ndarray]], syms: List[str], t0: int, t1: int, pairs: Sequence[str],
                  grid: Dict, tactics: Optional[Dict], cost: float, cache_dir: str, workers: int) -> Dict[str, np.ndarray]:
    key = hashlib.sha256(json.dumps([syms, t0, t1, list(pairs), grid, tactics or {}, cost], sort_keys=True).encode()).hexdigest()[:16]
    path = os.path.join(cache_dir, f"shadow-{key}.npz")
    if os.path.exists(path):
        with np.load(path) as z:
            # The cache key includes the simulated cost; legacy cache files
            # predate its explicit PF denominator metadata.
            return {**{k: z[k] for k in z.files}, "cost_pct": np.array(cost * 100.0)}
    jobs = [(s, RP.slice_bars(bars[s], t0, t1), list(pairs), grid, tactics, cost) for s in syms]
    parts = []
    with ProcessPoolExecutor(max_workers=max(1, workers)) as ex:
        for res in ex.map(_sim_symbol, jobs):
            sym_i = syms.index(res["sym"])
            if len(res["rows"]):
                parts.append(np.column_stack([np.full(len(res["rows"]), sym_i), res["rows"]]))
    a = np.concatenate(parts) if parts else np.zeros((0, 9))
    out = {"sym": a[:, 0].astype(np.int32), "pair": a[:, 1].astype(np.int32), "cell": a[:, 2].astype(np.int32),
           "side": a[:, 3].astype(np.int8), "entry_t": a[:, 4], "exit_t": a[:, 5], "r": a[:, 6],
           "cost_pct": np.array(cost * 100.0)}
    os.makedirs(cache_dir, exist_ok=True)
    np.savez_compressed(path, **out)
    return out


def load_funding(data_dir: str, syms: List[str]) -> Dict[str, Tuple[np.ndarray, np.ndarray]]:
    out = {}
    for s in syms:
        p = os.path.join(data_dir, "funding", f"{s}.npz")
        if os.path.exists(p):
            with np.load(p) as z:
                out[s] = (z["t"].astype(np.float64), z["rate"].astype(np.float64))
    return out


def funding_for(tr: Dict[str, np.ndarray], syms: List[str], fund: Dict) -> np.ndarray:
    f = np.zeros(len(tr["r"]))
    for si, s in enumerate(syms):
        if s not in fund:
            continue
        ft, fr = fund[s]
        cs = np.concatenate([[0.0], np.cumsum(fr)])
        m = np.flatnonzero(tr["sym"] == si)
        lo = np.searchsorted(ft, tr["entry_t"][m], side="right")
        hi = np.searchsorted(ft, tr["exit_t"][m], side="right")
        f[m] = -tr["side"][m] * (cs[hi] - cs[lo])
    return f


# ── walk-forward decisions ─────────────────────────────────
def walk(tr: Dict[str, np.ndarray], n_pairs: int, cells: List[AS.Cell], start: int, end: int, cfg: SL.StageConfig,
         execute: str = "trailing", allowed_pairs: Optional[Sequence[int]] = None) -> Dict[str, np.ndarray]:
    """Stage stamp (0 none, 1 Base, 2 Main, 3 Real, 4 executed) for every shadow trade entered in [start, end)."""
    hours = np.arange(start, end, H_MS, dtype=np.float64)
    n_cells = len(cells)
    score = np.full((n_pairs, n_cells, len(hours)), -np.inf)
    base_ok = np.zeros((n_pairs, n_cells, len(hours)), dtype=bool)
    main_ok = np.zeros_like(base_ok)
    exec_cell = np.array([execute == "both" or (execute == "trailing") == (c.trail > 0) for c in cells])
    tapes: Dict[Tuple[int, int], SL.Tape] = {}
    allowed = set(range(n_pairs)) if allowed_pairs is None else set(allowed_pairs)
    for p in range(n_pairs):
        if p not in allowed:
            continue
        mp = tr["pair"] == p
        for c in range(n_cells):
            m = mp & (tr["cell"] == c)
            if not m.any():
                continue
            tape = SL.Tape(tr["entry_t"][m], tr["exit_t"][m], tr["r"][m], tr["side"][m],
                           tr.get("cost_pct", AS.RT_COST * 100.0))
            tapes[(p, c)] = tape
            b = SL.base_eval(tape, hours, cfg)
            mo = SL.main_eval(tape, hours, cfg, b["ok"])
            base_ok[p, c] = b["ok"]
            main_ok[p, c] = mo
            if exec_cell[c]:
                score[p, c] = np.where(mo, b["lcb"], -np.inf)
    seated = SL.seat(score, cfg.portfolio)
    inside = (tr["entry_t"] >= start) & (tr["entry_t"] < end)
    stage = np.zeros(len(tr["r"]), dtype=np.int8)
    idx = np.flatnonzero(inside)
    h = ((tr["entry_t"][idx] - start) // H_MS).astype(np.int64)
    p, c = tr["pair"][idx], tr["cell"][idx]
    ok_pair = np.isin(p, list(allowed))
    stage[idx] = np.where(ok_pair & base_ok[p, c, h], 1, 0)
    stage[idx] = np.where(ok_pair & main_ok[p, c, h], 2, stage[idx])
    is_seat = ok_pair & (seated[p, h] == c)
    stage[idx] = np.where(is_seat, 3, stage[idx])
    # Last-N side gate on the seated trades.
    seat_idx = idx[is_seat]
    live = np.zeros(len(seat_idx), dtype=bool)
    groups: Dict[Tuple[int, int, int], List[int]] = {}
    for k, j in enumerate(seat_idx):
        groups.setdefault((int(tr["pair"][j]), int(tr["cell"][j]), int(tr["side"][j])), []).append(k)
    side_tapes: Dict[Tuple[int, int, int], SL.Tape] = {}
    for (pp, cc, sd), ks in groups.items():
        st = side_tapes.get((pp, cc, sd))
        if st is None:
            st = side_tapes[(pp, cc, sd)] = tapes[(pp, cc)].side_tape(sd)
        live[ks] = SL.live_ok(st, tr["entry_t"][seat_idx[ks]], cfg)
    stage[seat_idx[live]] = 4
    return {"stage": stage, "inside": inside, "seated": seated, "hours": hours}


# ── judge ───────────────────────────────────────────────────
JUDGE = {"min_trades": 150, "min_events": 30, "event_pf": 1.10, "pf": 1.15, "stress_pf": 1.0, "side_pf": 1.0,
         "side_trades": 30, "block_share": 2 / 3, "orders_day": 10.0}


def judge(entry_t, side, r, days: float, j: Dict = JUDGE, cost_pct: float = AS.RT_COST * 100.0) -> Dict:
    r = np.asarray(r, dtype=np.float64)
    side = np.asarray(side)
    entry_t = np.asarray(entry_t, dtype=np.float64)
    valid = np.isfinite(r) & np.isfinite(entry_t)
    r, side, entry_t = r[valid], side[valid], entry_t[valid]
    s = SL.summarize(r, cost_pct)
    stress = SL.summarize(r - 0.002, cost_pct)
    lg, sh = SL.summarize(r[side > 0], cost_pct), SL.summarize(r[side < 0], cost_pct)
    ev = SL.events(entry_t, side, 4.0)
    if len(r):
        keys = np.stack([side.astype(np.int64), np.floor(entry_t / (4 * H_MS)).astype(np.int64)], axis=1)
        _, inv = np.unique(keys, axis=0, return_inverse=True)
        ev_r = np.bincount(inv.ravel(), weights=r)
        # Each event represents multiple paid round trips. Its cost unit is
        # their sum, not one trade's cost, so clustering cannot inflate PF.
        ev_cost = np.bincount(inv.ravel()) * (cost_pct if cost_pct > 0 else 0.10)
        ev_pf = SL.summarize(ev_r, ev_cost)["pf"]
        wk = np.floor(entry_t / (7 * D_MS)).astype(np.int64)
        blocks = [SL.summarize(r[wk == w], cost_pct) for w in np.unique(wk)]
        blocks = [b for b in blocks if b["n"] >= 5]
        share = sum(b["pf"] >= 1 for b in blocks) / len(blocks) if blocks else 0.0
    else:
        ev_pf, share = 0.0, 0.0
    per_day = s["n"] / max(days, 1e-9)
    checks = [
        ("trades", s["n"], j["min_trades"], s["n"] >= j["min_trades"]),
        ("events", ev, j["min_events"], ev >= j["min_events"]),
        ("eventPf", ev_pf, j["event_pf"], ev_pf >= j["event_pf"]),
        ("pf", s["pf"], j["pf"], s["pf"] >= j["pf"]),
        ("stressPf", stress["pf"], j["stress_pf"], stress["pf"] >= j["stress_pf"]),
        ("pfLong", lg["pf"], j["side_pf"], lg["n"] >= j["side_trades"] and lg["pf"] >= j["side_pf"]),
        ("pfShort", sh["pf"], j["side_pf"], sh["n"] >= j["side_trades"] and sh["pf"] >= j["side_pf"]),
        ("weeklyShare", share, j["block_share"], share >= j["block_share"]),
        ("ordersPerDay", per_day, j["orders_day"], per_day >= j["orders_day"]),
    ]
    return {"pass": all(c[3] for c in checks), "checks": [{"name": a, "value": round(float(v), 4), "threshold": t, "ok": bool(o)} for a, v, t, o in checks],
            "metrics": {**s, "perDay": per_day, "stressPf": stress["pf"], "long": lg, "short": sh, "events": ev, "eventPf": ev_pf, "weeklyShare": share}}


# ── one variant over one period ────────────────────────────
def run_period(name: str, bars, data_dir: str, cfg: SL.StageConfig, pairs: Sequence[str], grid: Dict, tactics: Optional[Dict],
               n_symbols: int, execute: str, cache_dir: str, workers: int, cost: float = AS.RT_COST, use_funding: bool = True) -> Dict:
    start, end = RP.period_range(name, bars)
    look = int((cfg.long_h + WARMUP_H) * H_MS)
    syms = RP.rank_symbols(bars, start, end, n_symbols, lookback_ms=look)
    hold_max = max(grid["holdH"]) * H_MS
    tr = shadow_trades(bars, syms, start - look, end + hold_max, pairs, grid, tactics, cost, cache_dir, workers)
    cells = AS.grid_cells(grid)
    if use_funding:
        tr = dict(tr)
        tr["r"] = tr["r"] + funding_for(tr, syms, load_funding(data_dir, syms))
    # Entry decisions must see the same cost/funding-net closes as reporting.
    w = walk(tr, len(pairs), cells, start, end, cfg, execute)
    ex = w["stage"] == 4
    days = (end - start) / D_MS
    stage_names = np.array(["", "Base", "Main", "Real", "Live"])
    ins = w["inside"]
    forward = SL.forward_by_stage(stage_names[w["stage"][ins]], tr["r"][ins], cost * 100.0)
    res = judge(tr["entry_t"][ex], tr["side"][ex], tr["r"][ex], days, cost_pct=cost * 100.0)
    by_pair = {}
    for pi, pair in enumerate(pairs):
        m = ex & (tr["pair"] == pi)
        if m.any():
            by_pair[pair] = SL.summarize(tr["r"][m], cost * 100.0)
    res.update(period=name, start=RP.day_of(start), end=RP.day_of(end), symbols=len(syms), days=round(days, 2),
               forward=forward, byPair=by_pair, shadow=SL.summarize(tr["r"][ins], cost * 100.0))
    return res


def period_trades(name: str, bars, data_dir: str, pairs: Sequence[str], grid: Dict, tactics: Optional[Dict], n_symbols: int,
                  cache_dir: str, workers: int, cost: float = AS.RT_COST, use_funding: bool = True):
    start, end = RP.period_range(name, bars)
    look = int((336 + WARMUP_H) * H_MS)
    syms = RP.rank_symbols(bars, start, end, n_symbols, lookback_ms=look)
    tr = shadow_trades(bars, syms, start - look, end + max(grid["holdH"]) * H_MS, pairs, grid, tactics, cost, cache_dir, workers)
    if use_funding:
        tr = dict(tr)
        tr["r"] = tr["r"] + funding_for(tr, syms, load_funding(data_dir, syms))
    return tr, start, end, syms


def static_select(tr, start: int, end: int, n_pairs: int, n_cells: int, pf_min: float, per_pair: int,
                  rate_min: float = 0.3) -> List[Tuple[int, int]]:
    """Each pair's best cells on the selection period (PF >= pf_min, >= rate_min trades/day)."""
    ins = (tr["entry_t"] >= start) & (tr["entry_t"] < end)
    days = (end - start) / D_MS
    out = []
    for p in range(n_pairs):
        cand = []
        for c in range(n_cells):
            s = SL.summarize(tr["r"][ins & (tr["pair"] == p) & (tr["cell"] == c)], tr.get("cost_pct", AS.RT_COST * 100.0))
            if s["n"] / days >= rate_min and s["pf"] >= pf_min:
                cand.append((s["pf"], c))
        cand.sort(reverse=True)
        out += [(p, c) for _pf, c in cand[:per_pair]]
    return out


def static_eval(tr, start: int, end: int, sets: Sequence[Tuple[int, int]], pairs: Sequence[str], cells: List[AS.Cell]) -> Dict:
    keep = np.zeros(len(tr["r"]), dtype=bool)
    for p, c in sets:
        keep |= (tr["pair"] == p) & (tr["cell"] == c)
    ex = keep & (tr["entry_t"] >= start) & (tr["entry_t"] < end)
    cost_pct = float(tr.get("cost_pct", AS.RT_COST * 100.0))
    res = judge(tr["entry_t"][ex], tr["side"][ex], tr["r"][ex], (end - start) / D_MS, cost_pct=cost_pct)
    res["bySet"] = {f"{pairs[p]}|{cells[c].id}": SL.summarize(tr["r"][ex & (tr["pair"] == p) & (tr["cell"] == c)], cost_pct) for p, c in sets}
    return res


def run_static(args, bars, pairs, grid, tactics) -> Dict:
    """Cells frozen on the selection period, then every signal of those Sets is taken in the judged periods."""
    cells = AS.grid_cells(grid)
    data = {n: period_trades(n, bars, args.data, pairs, grid, tactics, args.symbols, args.cache, args.workers,
                             use_funding=not args.no_funding) for n in [args.select] + args.periods.split(",")}
    tr, s0, e0, _ = data[args.select]
    sets = static_select(tr, s0, e0, len(pairs), len(cells), args.static_pf, args.static_per_pair, args.static_rate)
    out = {"mode": "static", "select": args.select, "pfMin": args.static_pf, "perPair": args.static_per_pair,
           "rateMin": args.static_rate, "sets": [f"{pairs[p]}|{cells[c].id}" for p, c in sets], "periods": {}}
    for name in [args.select] + args.periods.split(","):
        tr, s, e, syms = data[name]
        r = static_eval(tr, s, e, sets, pairs, cells)
        r.update(period=name, start=RP.day_of(s), end=RP.day_of(e), symbols=len(syms))
        out["periods"][name] = r
        m = r["metrics"]
        print(f"{args.label or 'static'} {name} sets={len(sets)} n={m['n']} /day={m['perDay']:.1f} PF={m['pf']:.3f} "
              f"stress={m['stressPf']:.3f} L={m['long']['pf']:.2f} S={m['short']['pf']:.2f} wk={m['weeklyShare']:.2f} pass={r['pass']}", flush=True)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=RP.DATA_DIR)
    ap.add_argument("--periods", default="A,B,C,T")
    ap.add_argument("--symbols", type=int, default=40)
    ap.add_argument("--pairs", default="robust", help="robust | validated | all | comma list")
    ap.add_argument("--grid", default="", help="JSON grid override")
    ap.add_argument("--tactics", default="", help="JSON tactics, e.g. {\"volRegime\":true}")
    ap.add_argument("--execute", default="trailing", choices=["trailing", "normal", "both"])
    ap.add_argument("--long-h", type=int, default=336)
    ap.add_argument("--pre-h", type=int, default=20)
    ap.add_argument("--min-trades", type=int, default=12)
    ap.add_argument("--min-pf", type=float, default=1.10)
    ap.add_argument("--last-n", type=int, default=12)
    ap.add_argument("--last-n-min-pf", type=float, default=1.0)
    ap.add_argument("--portfolio", type=int, default=16)
    ap.add_argument("--no-funding", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--cache", default=os.path.join(ROOT, ".research-data", "wf-cache"))
    ap.add_argument("--mode", default="walk", choices=["walk", "static"], help="walk: hourly Base/Main/Real; static: cells frozen on --select")
    ap.add_argument("--select", default="A")
    ap.add_argument("--static-pf", type=float, default=1.2)
    ap.add_argument("--static-per-pair", type=int, default=2)
    ap.add_argument("--static-rate", type=float, default=0.3)
    ap.add_argument("--label", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    pairs = {"robust": ROBUST, "validated": list(AI.PAIRS_VALIDATED), "all": list(AI.PAIRS_ALL)}.get(args.pairs) or args.pairs.split(",")
    grid = json.loads(args.grid) if args.grid else DEFAULT_GRID
    tactics = json.loads(args.tactics) if args.tactics else None
    cfg = SL.StageConfig(long_h=args.long_h, pre_h=args.pre_h, min_trades=args.min_trades, min_pf=args.min_pf,
                         last_n=args.last_n, last_n_min_pf=args.last_n_min_pf, portfolio=args.portfolio)
    bars = RP.load_bars(args.data)
    if args.mode == "static":
        out = run_static(args, bars, pairs, grid, tactics)
        out.update(pairs=pairs, grid=grid, tactics=tactics, symbols=args.symbols, funding=not args.no_funding)
        if args.out:
            os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
            with open(args.out, "w") as f:
                json.dump(out, f, indent=1, default=float)
        return 0
    t0 = time.time()
    out = {"label": args.label, "config": vars(cfg), "pairs": pairs, "grid": grid, "tactics": tactics, "execute": args.execute,
           "symbols": args.symbols, "funding": not args.no_funding, "periods": {}}
    for name in args.periods.split(","):
        r = run_period(name, bars, args.data, cfg, pairs, grid, tactics, args.symbols, args.execute, args.cache, args.workers,
                       use_funding=not args.no_funding)
        out["periods"][name] = r
        m = r["metrics"]
        print(f"{args.label or 'run'} {name} {r['start'][:10]}→{r['end'][:10]} sym={r['symbols']} n={m['n']} /day={m['perDay']:.1f} "
              f"PF={m['pf']:.3f} stress={m['stressPf']:.3f} L={m['long']['pf']:.2f}/{m['long']['n']} S={m['short']['pf']:.2f}/{m['short']['n']} "
              f"evPF={m['eventPf']:.2f} wk={m['weeklyShare']:.2f} shadowPF={r['shadow']['pf']:.3f} "
              f"fwd B/M/R={r['forward']['Base']['pf']:.2f}/{r['forward']['Main']['pf']:.2f}/{r['forward']['Real']['pf']:.2f} pass={r['pass']} "
              f"({time.time() - t0:.0f}s)", flush=True)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(out, f, indent=1, default=float)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
