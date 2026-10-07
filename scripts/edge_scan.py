#!/usr/bin/env python3
"""Signal edge scan: rolling walk-forward, out-of-sample, after costs.

Loads cached 1m candles (``<data-dir>/*/SYMBOL.json``, the fetch_sim_window
format), merges them into one continuous tape per symbol and splits it into
UTC days. Every signal family x parameter x timeframe x exit configuration is
traded on every symbol; per configuration the scan keeps a daily (n, net)
aggregate. Each test day k then picks the top configurations of a family on the
training days before k (rolling ``--train-window`` days, 0 = all prior days),
and trades exactly those on day k (out-of-sample).

Trade model
  * entry on the next 1m open after the signal bar closes (5m/15m signals fire
    on that bar's close);
  * exits: TP / SL / time stop; SL first when both are touched in one bar;
  * one position at a time per symbol x configuration (``--one-position``):
    a new signal is taken only after the previous hold window has passed;
  * every trade pays ``--cost-pct`` round trip.

Robustness on the picked configurations (second pass, trade level):
  * thirds: out-of-sample net positive in each third of the test period;
  * clusters: same-direction trades of a family entering in the same 15
    minutes on several symbols count once (their mean);
  * leave-one-symbol-out: positive with any single symbol removed;
  * hour-of-day net and positive-hour share.

Usage
  python3 scripts/edge_scan.py --data-dir /tmp/claude-0/wf60 \
      --out reports/edge-scan-x/edge.json --html reports/edge-scan-x/index.html
"""
from __future__ import annotations

import argparse
import glob
import html
import itertools
import json
import math
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

DAY_MS = 86_400_000
LEGACY_FAMILIES = ("momentum", "reversion", "breakout", "fade-breakout", "squeeze-breakout",
                   "trend-pullback", "volume-follow", "volume-fade")


# --------------------------------------------------------------------- data
def load_tapes(data_dir: str, symbols: Optional[List[str]]) -> Dict[str, np.ndarray]:
    merged: Dict[str, Dict[int, List[float]]] = {}
    for path in sorted(glob.glob(os.path.join(data_dir, "*", "*.json"))):
        sym = os.path.basename(path)[:-5]
        if symbols and sym not in symbols:
            continue
        try:
            rows = json.load(open(path)).get("rows") or []
        except Exception:
            continue
        book = merged.setdefault(sym, {})
        for ts, ohlcv in rows:
            book[int(ts)] = [float(x) for x in ohlcv[:5]]
    out = {}
    for sym, book in merged.items():
        ts = np.array(sorted(book), dtype=np.int64)
        if len(ts) < 2000:
            continue
        arr = np.array([book[t] for t in ts], dtype=float)
        out[sym] = np.column_stack([ts.astype(float), arr])  # t, o, h, l, c, v
    return out


# ------------------------------------------------------------------ helpers
def ema(x: np.ndarray, n: int) -> np.ndarray:
    a = 2.0 / (n + 1.0)
    out = np.empty_like(x)
    acc = x[0]
    for i, v in enumerate(x):
        acc = a * v + (1 - a) * acc
        out[i] = acc
    return out


def rolling(x: np.ndarray, n: int, fn) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n or n < 1:
        return out
    w = np.lib.stride_tricks.sliding_window_view(x, n)
    out[n - 1:] = fn(w, axis=1)
    return out


def edge(sig: np.ndarray) -> np.ndarray:
    """Trigger only when a signal turns on (no re-entry every bar)."""
    prev = np.concatenate([[0], sig[:-1]])
    return np.where((sig != 0) & (sig != prev), sig, 0).astype(np.int8)


def resample(t: np.ndarray, tf: int) -> Tuple[np.ndarray, np.ndarray]:
    """tf-minute bars from 1m; returns (bars[o,h,l,c,v], 1m index of each
    bar's last minute). Incomplete buckets are dropped."""
    if tf == 1:
        return t[:, 1:6], np.arange(len(t))
    bucket = (t[:, 0] // (tf * 60_000)).astype(np.int64)
    starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
    ends = np.r_[starts[1:], len(t)] - 1
    # reduceat segments are exactly [start_k, start_{k+1}) = one bucket each
    h = np.maximum.reduceat(t[:, 2], starts)
    l = np.minimum.reduceat(t[:, 3], starts)
    v = np.add.reduceat(t[:, 5], starts)
    o, c = t[starts, 1], t[ends, 4]
    full = (ends - starts + 1) == tf
    return np.column_stack([o, h, l, c, v])[full], ends[full]


# ------------------------------------------------------------------ signals
def signal_families(b: np.ndarray, tf: int, btc_calm: Optional[np.ndarray],
                    families: Optional[set]) -> Dict[str, Dict[str, np.ndarray]]:
    """Signals on tf bars; parameters are in minutes, scaled to tf bars."""
    o, h, l, c, v = b[:, 0], b[:, 1], b[:, 2], b[:, 3], b[:, 4]
    lr = np.diff(np.log(c), prepend=np.log(c[0]))
    fam: Dict[str, Dict[str, np.ndarray]] = {}

    def want(name):
        return families is None or name in families

    def bars(minutes):
        return max(1, int(round(minutes / tf)))

    sd = rolling(lr, bars(240) if tf == 1 else max(48, bars(240)), np.std)
    e_fast, e_slow = ema(c, bars(60)), ema(c, bars(240))
    trend = np.sign(e_fast - e_slow)
    for L in (15, 60, 240, 1440):
        n = bars(L)
        if n < 2 or n >= len(c) // 4:
            continue
        ret = np.log(c) - np.log(np.roll(c, n))
        ret[:n] = 0
        z = ret / (sd * math.sqrt(n) + 1e-12)
        for thr in (1.5, 2.5, 3.5) if L != 1440 else ():
            s = np.where(z > thr, 1, np.where(z < -thr, -1, 0))
            if want("momentum"):
                fam.setdefault("momentum", {})[f"L{L}z{thr}"] = edge(s)
            if want("reversion"):
                fam.setdefault("reversion", {})[f"L{L}z{thr}"] = edge(-s)
        for thr in (2.0, 3.0, 4.0, 5.0):
            s = np.where(z > thr, 1, np.where(z < -thr, -1, 0))
            fade = -s
            if want("reversion-confirm"):
                # wait for a reversal candle in the fade direction
                rev = np.where((fade > 0) & (c > o), 1, np.where((fade < 0) & (c < o), -1, 0))
                held = np.maximum.accumulate(np.where(fade != 0, np.arange(len(c)), -10**9))
                recent = (np.arange(len(c)) - held) <= 3
                last = fade[np.clip(held, 0, None)]
                fam.setdefault("reversion-confirm", {})[f"L{L}z{thr}"] = edge(np.where(recent & (rev == last) & (rev != 0), rev, 0))
            if want("reversion-with-trend"):
                # fade only moves against the 1h/4h trend (buy dips in uptrends)
                fam.setdefault("reversion-with-trend", {})[f"L{L}z{thr}"] = edge(np.where(fade == trend, fade, 0))
            if want("reversion-btc-calm") and btc_calm is not None:
                fam.setdefault("reversion-btc-calm", {})[f"L{L}z{thr}"] = edge(np.where(btc_calm, fade, 0))
    for L in (60, 240, 720):
        n = bars(L)
        if n < 3:
            continue
        hi = np.roll(rolling(h, n, np.max), 1)
        lo = np.roll(rolling(l, n, np.min), 1)
        s = np.where(c > hi, 1, np.where(c < lo, -1, 0))
        s[: n + 1] = 0
        if want("breakout"):
            fam.setdefault("breakout", {})[f"L{L}"] = edge(s)
        if want("fade-breakout"):
            fam.setdefault("fade-breakout", {})[f"L{L}"] = edge(-s)
    if want("squeeze-breakout"):
        tr = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)), abs(l - np.roll(c, 1))))
        for L, k in itertools.product((30, 120), (0.5, 0.7)):
            n = bars(L)
            if n < 3:
                continue
            short = rolling(tr, n, np.mean)
            long_ = rolling(tr, n * 5, np.mean)
            squeeze = np.roll((short / (long_ + 1e-12)) < k, 1)
            hi = np.roll(rolling(h, n, np.max), 1)
            lo = np.roll(rolling(l, n, np.min), 1)
            s = np.where(squeeze & (c > hi), 1, np.where(squeeze & (c < lo), -1, 0))
            s[: n * 5 + 1] = 0
            fam.setdefault("squeeze-breakout", {})[f"L{L}k{k}"] = edge(s)
    if want("trend-pullback"):
        for f, sl in ((60, 240), (240, 960)):
            ef, es = ema(c, bars(f)), ema(c, bars(sl))
            tr_ = np.sign(ef - es)
            e20 = ema(c, max(2, bars(20)))
            pull = np.where((tr_ > 0) & (l <= e20) & (c > e20), 1,
                            np.where((tr_ < 0) & (h >= e20) & (c < e20), -1, 0))
            pull[: bars(sl)] = 0
            fam.setdefault("trend-pullback", {})[f"E{f}/{sl}"] = edge(pull)
    if want("volume-follow") or want("volume-fade"):
        vavg = rolling(v, bars(240), np.mean)
        for m, zr in itertools.product((4.0, 8.0), (2.0, 3.0)):
            spike = (v > m * (vavg + 1e-12)) & (abs(lr) > zr * (sd + 1e-12))
            s = np.where(spike, np.sign(lr), 0).astype(int)
            s[: bars(240)] = 0
            if want("volume-follow"):
                fam.setdefault("volume-follow", {})[f"m{m}z{zr}"] = edge(s)
            if want("volume-fade"):
                fam.setdefault("volume-fade", {})[f"m{m}z{zr}"] = edge(-s)
    return fam


# -------------------------------------------------------------------- exits
class Exits:
    """First-touch offsets via a sparse max/min table (binary lifting)."""

    def __init__(self, t: np.ndarray, max_hold: int):
        self.o, self.h, self.l, self.c = t[:, 1], t[:, 2], t[:, 3], t[:, 4]
        self.n = len(t)
        self.H = max_hold
        self.entry = np.r_[self.o[1:], self.o[-1]]
        self.levels = max_hold.bit_length()
        self.hmax = [self.h]
        self.lmin = [self.l]
        for k in range(1, self.levels + 1):
            step = 1 << (k - 1)
            ph, pl = self.hmax[-1], self.lmin[-1]
            self.hmax.append(np.maximum(ph, np.r_[ph[step:], np.full(step, -np.inf)]))
            self.lmin.append(np.minimum(pl, np.r_[pl[step:], np.full(step, np.inf)]))

    def first_touch(self, pct: float, up: bool) -> np.ndarray:
        """Offset j (0-based from the entry bar i+1) of the first bar whose
        high (up) / low (down) touches entry*(1 +/- pct%); H+1 if none within H."""
        level = self.entry * (1 + pct / 100.0) if up else self.entry * (1 - pct / 100.0)
        pos = np.arange(self.n) + 1  # entry bar
        start = pos.copy()
        for k in range(self.levels, -1, -1):
            span = 1 << k
            p = np.minimum(pos, self.n - 1)
            if up:
                block_ok = self.hmax[k][p] < level
            else:
                block_ok = self.lmin[k][p] > level
            room = (pos - start + span) <= self.H + 1
            move = block_ok & room & (pos < self.n)
            pos = np.where(move, pos + span, pos)
        off = pos - start
        hit_in = (pos < self.n)
        p = np.minimum(pos, self.n - 1)
        touched = (self.h[p] >= level) if up else (self.l[p] <= level)
        return np.where(hit_in & touched & (off <= self.H), off, self.H + 1).astype(np.int32)

    def net(self, tp: float, sl: float, hold: int, side: int, cost: float, cache: Dict) -> np.ndarray:
        up_tp = side > 0
        ktp, ksl = ("tp", tp, side), ("sl", sl, side)
        if ktp not in cache:
            cache[ktp] = self.first_touch(tp, up_tp)
        if ksl not in cache:
            cache[ksl] = self.first_touch(sl, not up_tp)
        ht, hs = cache[ktp], cache[ksl]
        idx_close = np.minimum(np.arange(self.n) + hold, self.n - 1)
        time_move = side * (self.c[idx_close] / self.entry - 1) * 100
        hit_sl = (hs < hold) & (hs <= ht)
        hit_tp = (ht < hold) & ~hit_sl
        return (np.where(hit_sl, -sl, np.where(hit_tp, tp, time_move)) - cost).astype(np.float32)


def space(idx: np.ndarray, gap: int) -> np.ndarray:
    """One position at a time: drop signals inside the previous hold window."""
    if len(idx) == 0 or gap <= 0:
        return idx
    keep = []
    nxt = -1
    for i in idx.tolist():
        if i >= nxt:
            keep.append(i)
            nxt = i + gap
    return np.array(keep, dtype=np.int64)


# ------------------------------------------------------------------ scoring
def pf_of(total: float, n: float, cost: float) -> float:
    """Cost-coordinate PF used by the engine: 1 + 0.1 * mean(net / cost)."""
    return 1.0 + 0.1 * (total / n) / cost if n else 1.0


def symbol_entries(t, sym, tapes, args, families):
    """Yield (family, key-prefix, tf, hold, side array, 1m entry indices)."""
    btc_calm_1m = None
    if sym != "BTC-USDT" and "BTC-USDT" in tapes and (families is None or "reversion-btc-calm" in families):
        bt = tapes["BTC-USDT"]
        lr = np.log(bt[:, 4])
        ret = lr - np.roll(lr, 60)
        ret[:60] = 0
        sd = rolling(np.diff(lr, prepend=lr[0]), 240, np.std) * math.sqrt(60)
        calm = np.abs(ret) < 1.5 * (sd + 1e-12)
        btc_calm_1m = dict(zip(bt[:, 0].astype(np.int64).tolist(), calm.tolist()))
    valid_upto = len(t) - max(args.holds) - 2
    for tf in args.tfs:
        bars, last_min = resample(t, tf)
        calm = None
        if btc_calm_1m is not None:
            calm = np.array([btc_calm_1m.get(int(t[i, 0]), False) for i in last_min])
        fams = signal_families(bars, tf, calm, families)
        for fname, configs in fams.items():
            for cname, sig in configs.items():
                at = np.flatnonzero(sig)
                if len(at) == 0:
                    continue
                idx = last_min[at]
                ok = idx < valid_upto
                idx, sides = idx[ok], sig[at][ok]
                for hold in args.holds:
                    keep = space(idx, hold) if args.one_position else idx
                    sel = np.searchsorted(idx, keep)
                    yield fname, f"tf{tf}|{cname}", hold, sides[sel], keep


def exit_grid(args):
    for tp in args.tps:
        for r in args.sl_ratios:
            for hold in args.holds:
                yield tp, r, hold


def run(args) -> Dict:
    tapes = load_tapes(args.data_dir, args.symbols.split(",") if args.symbols else None)
    if not tapes:
        sys.exit("no tapes found")
    families = set(args.families.split(",")) if args.families else None
    t0 = min(int(t[0, 0]) for t in tapes.values())
    day0 = ((t0 // DAY_MS) + 1) * DAY_MS
    t_end = max(int(t[-1, 0]) for t in tapes.values())
    n_days = int((t_end - day0) // DAY_MS)
    syms = sorted(tapes)
    # agg[family][config] = array (n_days, 2): trades, net
    agg: Dict[str, Dict[str, np.ndarray]] = {}
    started = time.time()
    for sym in syms:
        t = tapes[sym]
        ex = Exits(t, max(args.holds))
        day = ((t[:, 0] - day0) // DAY_MS).astype(np.int64)
        cache: Dict = {}
        nets: Dict[Tuple, Dict[int, np.ndarray]] = {}
        for tp, r, hold in exit_grid(args):
            nets[(tp, r, hold)] = {s: ex.net(tp, round(tp * r, 4), hold, s, args.cost_pct, cache) for s in (1, -1)}
        for fname, prefix, hold, sides, idx in symbol_entries(t, sym, tapes, args, families):
            d = day[idx]
            okd = (d >= 0) & (d < n_days)
            if not okd.any():
                continue
            d, sides_, idx_ = d[okd], sides[okd], idx[okd]
            cnt = np.bincount(d, minlength=n_days)
            for tp in args.tps:
                for r in args.sl_ratios:
                    by = nets[(tp, r, hold)]
                    net = np.where(sides_ > 0, by[1][idx_], by[-1][idx_])
                    key = f"{prefix}|tp{tp}|r{r}|h{hold}"
                    arr = agg.setdefault(fname, {}).setdefault(key, np.zeros((n_days, 2)))
                    arr[:, 0] += cnt
                    arr[:, 1] += np.bincount(d, weights=net, minlength=n_days)
        print(f"  {sym}: {len(t)} bars, configs={sum(len(v) for v in agg.values())}, {time.time() - started:.0f}s", flush=True)

    # walk-forward picks per family and day
    picks: Dict[str, Dict[int, List[Tuple[str, float]]]] = {}
    first_test = max(args.train_days, 1)
    for fname, configs in agg.items():
        keys = list(configs)
        mat = np.stack([configs[k] for k in keys])  # (configs, days, 2)
        for k in range(first_test, n_days):
            lo = 0 if args.train_window <= 0 else max(0, k - args.train_window)
            n = mat[:, lo:k, 0].sum(axis=1)
            tot = mat[:, lo:k, 1].sum(axis=1)
            pf = np.where(n >= args.min_train_trades, 1 + 0.1 * (tot / np.maximum(n, 1)) / args.cost_pct, -np.inf)
            order = np.argsort(-pf)[: args.top]
            picks.setdefault(fname, {})[k] = [(keys[i], float(pf[i])) for i in order if pf[i] > 1.0]

    # second pass: trade-level rows for the picked (config, day) pairs
    want: Dict[str, Dict[str, set]] = {}
    for fname, by_day in picks.items():
        for k, lst in by_day.items():
            for key, _ in lst:
                want.setdefault(fname, {}).setdefault(key, set()).add(k)
    rows: Dict[str, List[Tuple]] = {f: [] for f in agg}
    for sym in syms:
        t = tapes[sym]
        ex = Exits(t, max(args.holds))
        day = ((t[:, 0] - day0) // DAY_MS).astype(np.int64)
        cache = {}
        for fname, prefix, hold, sides, idx in symbol_entries(t, sym, tapes, args, families):
            wanted = want.get(fname, {})
            if not wanted:
                continue
            for tp in args.tps:
                for r in args.sl_ratios:
                    key = f"{prefix}|tp{tp}|r{r}|h{hold}"
                    days = wanted.get(key)
                    if not days:
                        continue
                    d = day[idx]
                    m = np.isin(d, list(days))
                    if not m.any():
                        continue
                    net_s = {s: ex.net(tp, round(tp * r, 4), hold, s, args.cost_pct, cache) for s in (1, -1)}
                    for i, side, dd in zip(idx[m], sides[m], d[m]):
                        rows[fname].append((int(dd), key, sym, int(side), float(t[i, 0]),
                                            float(net_s[int(side)][i])))
    return summarize(args, agg, picks, rows, syms, n_days, first_test, day0)


def summarize(args, agg, picks, rows, syms, n_days, first_test, day0) -> Dict:
    families = {}
    test_range = list(range(first_test, n_days))
    thirds = np.array_split(np.array(test_range), 3) if len(test_range) >= 3 else [np.array(test_range)]
    for fname in agg:
        # one row per (day, picked config, trade); dedupe identical trades picked twice
        seen = set()
        trades = []
        for dd, key, sym, side, ts, net in rows.get(fname, []):
            if (dd, key, sym, ts) in seen:
                continue
            if key not in dict(picks[fname].get(dd, [])):
                continue
            seen.add((dd, key, sym, ts))
            trades.append((dd, key, sym, side, ts, net))
        test_days = []
        hours = np.zeros(24)
        hours_traded = np.zeros(24)
        for k in test_range:
            day_tr = [x for x in trades if x[0] == k]
            nets = np.array([x[5] for x in day_tr])
            hr: Dict[int, float] = {}
            for x in day_tr:
                h_ = int((x[4] % DAY_MS) // 3_600_000)
                hr[h_] = hr.get(h_, 0.0) + x[5]
            for h_, val in hr.items():
                hours[h_] += val
                hours_traded[h_] += 1
            test_days.append({
                "day": k, "date": time.strftime("%Y-%m-%d", time.gmtime((day0 + k * DAY_MS) / 1000)),
                "picks": [p for p, _ in picks[fname].get(k, [])],
                "trainPf": [round(v, 4) for _, v in picks[fname].get(k, [])],
                "n": int(len(nets)), "netPct": round(float(nets.sum()), 4) if len(nets) else 0.0,
                "pf": round(pf_of(float(nets.sum()), len(nets), args.cost_pct), 4) if len(nets) else None,
                "winRate": round(float((nets > 0).mean() * 100), 1) if len(nets) else None,
                "hoursPositive": sum(1 for v in hr.values() if v > 0), "hoursTraded": len(hr),
            })
        all_net = np.array([x[5] for x in trades])
        n_all = len(all_net)
        tot = float(all_net.sum()) if n_all else 0.0
        oos_pf = pf_of(tot, n_all, args.cost_pct) if n_all else 1.0
        days_pos = sum(1 for d in test_days if d["netPct"] > 0)
        third_net = [round(float(sum(d["netPct"] for d in test_days if d["day"] in set(th.tolist()))), 3) for th in thirds]
        # clusters: same side, same 15 minutes, any symbol -> one sample (mean)
        cl: Dict[Tuple, List[float]] = {}
        for dd, key, sym, side, ts, net in trades:
            cl.setdefault((side, int(ts // 900_000)), []).append(net)
        cl_net = np.array([np.mean(v) for v in cl.values()]) if cl else np.zeros(0)
        cluster_pf = pf_of(float(cl_net.sum()), len(cl_net), args.cost_pct) if len(cl_net) else 1.0
        loo = {}
        for s in syms:
            sub = np.array([x[5] for x in trades if x[2] != s])
            loo[s] = round(pf_of(float(sub.sum()), len(sub), args.cost_pct), 4) if len(sub) else None
        loo_min = min([v for v in loo.values() if v is not None], default=None)
        hp = sum(d["hoursPositive"] for d in test_days)
        ht = sum(d["hoursTraded"] for d in test_days)
        per_day = n_all / max(1, len(test_days))
        checks = {
            "pf": oos_pf >= args.min_pf,
            "days": len(test_days) > 0 and days_pos / len(test_days) >= args.min_days_positive,
            "thirds": all(x > 0 for x in third_net),
            "tradesPerDay": per_day >= args.min_trades_day,
            "hours": bool(ht) and hp / ht >= args.min_hours_positive,
            "clusters": len(cl_net) > 0 and cluster_pf > 1.0,
            "leaveOneOut": loo_min is not None and loo_min > 1.0,
        }
        families[fname] = {
            "configs": len(agg[fname]), "testDays": test_days, "oosTrades": n_all,
            "tradesPerDay": round(per_day, 1), "oosNetPct": round(tot, 3), "oosPf": round(oos_pf, 4),
            "positiveDays": days_pos, "thirdsNetPct": third_net,
            "clusters": len(cl_net), "clusterPf": round(cluster_pf, 4),
            "leaveOneOutPf": loo, "leaveOneOutMin": loo_min,
            "hoursPositivePct": round(100 * hp / ht, 1) if ht else None,
            "hourNet": [round(float(x), 3) for x in hours], "hourTraded": [int(x) for x in hours_traded],
            "checks": checks, "survives": all(checks.values()),
        }
    return {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "symbols": syms,
        "days": n_days, "testDays": len(test_range),
        "firstDay": time.strftime("%Y-%m-%d", time.gmtime(day0 / 1000)),
        "costPct": args.cost_pct, "trainWindow": args.train_window, "top": args.top,
        "minTrainTrades": args.min_train_trades, "onePosition": args.one_position,
        "grid": {"tfs": args.tfs, "tp": args.tps, "slRatio": args.sl_ratios, "holdBars": args.holds},
        "rule": {"minPf": args.min_pf, "minDaysPositive": args.min_days_positive, "eachThirdPositive": True,
                 "minTradesPerDay": args.min_trades_day, "minHoursPositive": args.min_hours_positive,
                 "clusterPfAbove": 1.0, "leaveOneSymbolOutPfAbove": 1.0},
        "families": families,
    }


# ------------------------------------------------------------------- report
def render(rep: Dict) -> str:
    fams = sorted(rep["families"].items(), key=lambda kv: -kv[1]["oosNetPct"])
    w, hgt, pad = 760, 280, 40
    lo = hi = 0.0
    series = []
    for name, f in fams:
        acc, pts = 0.0, []
        for d in f["testDays"]:
            acc += d["netPct"]
            pts.append(acc)
        if pts:
            lo, hi = min(lo, *pts), max(hi, *pts)
        series.append((name, pts, f["survives"]))
    span = (hi - lo) or 1.0
    palette = ["#2a6fdb", "#d9822b", "#2e9e6a", "#c23b3b", "#7a5bc7", "#8a6d3b", "#1f9bb5", "#b5487a", "#5f6b7a", "#9aa53a", "#e0a800", "#6b8e23"]
    lines = []
    for i, (name, pts, ok) in enumerate(series):
        if not pts:
            continue
        xy = " ".join(f"{pad + j * (w - 2 * pad) / max(1, len(pts) - 1):.1f},{hgt - pad - (p - lo) / span * (hgt - 2 * pad):.1f}"
                      for j, p in enumerate(pts))
        lines.append(f'<polyline fill="none" stroke="{palette[i % len(palette)]}" stroke-width="{3 if ok else 1.6}" points="{xy}"><title>{html.escape(name)}</title></polyline>')
    zero_y = hgt - pad - (0 - lo) / span * (hgt - 2 * pad)
    legend = "".join(f'<span class="lg"><i style="background:{palette[i % len(palette)]}"></i>{html.escape(n)}</span>' for i, (n, _, _) in enumerate(series))

    def yn(b):
        return "<b class='g'>✓</b>" if b else "<b class='r'>✗</b>"
    rows = "".join(
        f"<tr class='{'ok' if f['survives'] else ''}'><td>{html.escape(n)}</td><td>{f['configs']}</td><td>{f['oosTrades']}</td>"
        f"<td>{f['tradesPerDay']}</td><td>{f['oosPf']:.3f} {yn(f['checks']['pf'])}</td><td>{f['oosNetPct']:+.1f}</td>"
        f"<td>{f['positiveDays']}/{len(f['testDays'])} {yn(f['checks']['days'])}</td>"
        f"<td>{' / '.join(f'{x:+.0f}' for x in f['thirdsNetPct'])} {yn(f['checks']['thirds'])}</td>"
        f"<td>{'' if f['hoursPositivePct'] is None else f['hoursPositivePct']} {yn(f['checks']['hours'])}</td>"
        f"<td>{f['clusterPf']:.3f} ({f['clusters']}) {yn(f['checks']['clusters'])}</td>"
        f"<td>{'' if f['leaveOneOutMin'] is None else f['leaveOneOutMin']} {yn(f['checks']['leaveOneOut'])}</td>"
        f"<td>{'yes' if f['survives'] else 'no'}</td></tr>"
        for n, f in fams)
    best = fams[0][1] if fams else None
    heat = ""
    if best:
        mx = max(1e-9, max(abs(x) for x in best["hourNet"]))
        cells = "".join(
            f"<td style='background:{'rgba(46,158,106,' if x >= 0 else 'rgba(194,59,59,'}{abs(x) / mx:.2f})' title='{h:02d}:00 UTC {x:+.2f}%'>{h:02d}</td>"
            for h, x in enumerate(best["hourNet"]))
        heat = f"<h2>Hour of day (UTC), out-of-sample net · {html.escape(fams[0][0])}</h2><div class='wrap'><table class='heat'><tr>{cells}</tr></table></div>"
    detail = ""
    for n, f in fams:
        detail += (f"<details><summary>{html.escape(n)} · {f['oosTrades']} trades · PF {f['oosPf']:.3f}</summary>"
                   "<table><tr><th>date</th><th>trades</th><th>PF</th><th>net %</th><th>win %</th><th>hours +/traded</th><th>picked (in-sample PF)</th></tr>")
        for d in f["testDays"]:
            picks = ", ".join(f"{html.escape(k)} ({p})" for k, p in zip(d["picks"], d["trainPf"]))
            detail += (f"<tr><td>{d['date']}</td><td>{d['n']}</td><td>{'' if d['pf'] is None else d['pf']}</td><td>{d['netPct']:+.2f}</td>"
                       f"<td>{'' if d['winRate'] is None else d['winRate']}</td><td>{d['hoursPositive']}/{d['hoursTraded']}</td><td class='pk'>{picks or '—'}</td></tr>")
        detail += "</table></details>"
    survivors = [n for n, f in fams if f["survives"]]
    verdict = (f"<p class='good'>Survivors: {', '.join(map(html.escape, survivors))}</p>" if survivors
               else "<p class='bad'>No family survives out-of-sample after costs.</p>")
    r = rep["rule"]
    rule = (f"PF ≥ {r['minPf']}, ≥ {int(r['minDaysPositive'] * 100)}% of test days positive and every third positive, "
            f"≥ {r['minTradesPerDay']:.0f} trades/day, ≥ {int(r['minHoursPositive'] * 100)}% of traded hours positive, "
            f"cluster PF &gt; 1, PF &gt; 1 with any single symbol removed")
    g = rep["grid"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signal Edge Scan</title><style>
:root{{--bg:#fff;--fg:#1d2330;--mut:#5f6b7a;--line:#e3e7ee;--ok:#e8f6ee;--good:#1d7a4a;--bad:#b23a3a}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151b;--fg:#e6e9ef;--mut:#9aa3b2;--line:#2a303b;--ok:#17301f;--good:#5bc58a;--bad:#e07a7a}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1180px;padding:16px}}
table{{border-collapse:collapse;width:100%;margin:8px 0 20px;font-variant-numeric:tabular-nums}}td,th{{border-bottom:1px solid var(--line);padding:4px 6px;text-align:right;white-space:nowrap}}
td:first-child,th:first-child,.pk{{text-align:left}}.pk{{font-size:12px;color:var(--mut);white-space:normal}}tr.ok{{background:var(--ok)}}
.good{{color:var(--good);font-weight:600}}.bad{{color:var(--bad);font-weight:600}}.mut{{color:var(--mut)}}.g{{color:var(--good)}}.r{{color:var(--bad)}}
.lg{{display:inline-flex;align-items:center;margin-right:12px;font-size:12px}}.lg i{{width:10px;height:10px;margin-right:4px;display:inline-block}}
.heat td{{text-align:center;font-size:11px;padding:6px 0}}details{{margin:6px 0}}summary{{cursor:pointer}}
.wrap{{overflow-x:auto}}svg{{max-width:100%;height:auto}}</style></head><body>
<h1>Signal edge scan</h1>
<p class="mut">{len(rep['symbols'])} symbols · {rep['days']} UTC days from {rep['firstDay']} · {rep['testDays']} out-of-sample test days ·
cost {rep['costPct']}% per trade · each test day trades the top {rep['top']} configs of the previous {rep['trainWindow'] or 'all'} days ·
timeframes {', '.join(f'{x}m' for x in g['tfs'])} · TP {g['tp']} % · SL:TP {g['slRatio']} · hold {', '.join(f'{x // 60}h' if x >= 60 else f'{x}m' for x in g['holdBars'])} ·
{'one position per symbol × config' if rep['onePosition'] else 'overlapping trades'} · generated {rep['generatedAt']}</p>
<p class="mut">Survival rule (fixed before the run): {rule}.</p>{verdict}
<h2>Cumulative out-of-sample net % (sum of trade %)</h2>
<div class="wrap"><svg viewBox="0 0 {w} {hgt}" role="img" aria-label="cumulative out-of-sample net per family"><line x1="{pad}" x2="{w - pad}" y1="{zero_y:.1f}" y2="{zero_y:.1f}" stroke="currentColor" stroke-opacity=".3"/>{''.join(lines)}
<text x="{pad}" y="14" font-size="11" fill="currentColor">{hi:+.0f}%</text><text x="{pad}" y="{hgt - 8}" font-size="11" fill="currentColor">{lo:+.0f}%</text></svg></div>
<div>{legend}</div>
<h2>Families (out-of-sample)</h2><div class="wrap"><table><tr><th>family</th><th>configs</th><th>trades</th><th>/day</th><th>PF</th><th>net %</th><th>days +</th><th>thirds net %</th><th>hours + %</th><th>cluster PF (n)</th><th>LOO min PF</th><th>survives</th></tr>{rows}</table></div>
{heat}
<h2>Per day</h2><div class="wrap">{detail}</div></body></html>"""


def floats(s):
    return [float(x) for x in s.split(",") if x]


def ints(s):
    return [int(x) for x in s.split(",") if x]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--families", default="", help="comma list; default all")
    ap.add_argument("--cost-pct", type=float, default=0.18)
    ap.add_argument("--tfs", type=ints, default=[1, 5, 15])
    ap.add_argument("--tps", type=floats, default=[0.3, 0.6, 1.0, 1.5, 2.0, 3.0])
    ap.add_argument("--sl-ratios", type=floats, default=[0.6, 1.0, 1.5, 2.0])
    ap.add_argument("--holds", type=ints, default=[60, 240, 720, 1440])
    ap.add_argument("--one-position", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--train-days", type=int, default=14, help="first test day index")
    ap.add_argument("--train-window", type=int, default=14, help="rolling training days (0 = all prior)")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--min-train-trades", type=int, default=60)
    ap.add_argument("--min-pf", type=float, default=1.05)
    ap.add_argument("--min-days-positive", type=float, default=0.60)
    ap.add_argument("--min-trades-day", type=float, default=30)
    ap.add_argument("--min-hours-positive", type=float, default=0.55)
    ap.add_argument("--out", required=True)
    ap.add_argument("--html", default="")
    args = ap.parse_args()
    rep = run(args)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump(rep, open(args.out, "w"), indent=1)
    if args.html:
        os.makedirs(os.path.dirname(os.path.abspath(args.html)), exist_ok=True)
        open(args.html, "w").write(render(rep))
    for name, f in sorted(rep["families"].items(), key=lambda kv: -kv[1]["oosNetPct"]):
        fails = ",".join(k for k, v in f["checks"].items() if not v)
        print(f"{name:22s} n={f['oosTrades']:6d} /day={f['tradesPerDay']:6.1f} pf={f['oosPf']:.3f} net={f['oosNetPct']:+9.1f}% "
              f"days+={f['positiveDays']}/{len(f['testDays'])} thirds={f['thirdsNetPct']} cl={f['clusterPf']:.3f} "
              f"loo={f['leaveOneOutMin']} hrs+={f['hoursPositivePct']} {'SURVIVES' if f['survives'] else 'fails:' + fails}")


if __name__ == "__main__":
    main()
