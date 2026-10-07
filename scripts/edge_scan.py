#!/usr/bin/env python3
"""Signal edge scan: walk-forward, out-of-sample, after costs.

Merges the cached 1m windows (``<data-dir>/d*/SYMBOL.json``) into one
continuous tape per symbol, splits it into UTC-aligned 24h days, and tests
signal families x parameter grids x exit grids:

  each day k (k >= --train-days): pick the best configurations of a family on
  days < k (in-sample PF after cost, min trades), then trade exactly those
  configurations on day k (out-of-sample). A family survives only when its
  out-of-sample result is positive on most test days.

Costs: every trade pays ``--cost-pct`` round trip (PositionCost). Entries are
on the next bar open after the signal bar closes; exits are TP / SL / time
stop with intrabar SL-first when both are touched (conservative).

Usage:
  python3 scripts/edge_scan.py --data-dir /tmp/claude-0/wfwin \
      --out reports/edge-scan/edge.json --html reports/edge-scan/index.html
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
from typing import Dict, List, Tuple

import numpy as np

DAY_MS = 86_400_000


# --------------------------------------------------------------------- data
def load_tapes(data_dir: str, symbols: List[str] | None) -> Dict[str, np.ndarray]:
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
    if len(x) < n:
        return out
    w = np.lib.stride_tricks.sliding_window_view(x, n)
    out[n - 1:] = fn(w, axis=1)
    return out


def edge(sig: np.ndarray) -> np.ndarray:
    """Trigger only when a signal turns on (no re-entry every bar)."""
    prev = np.concatenate([[0], sig[:-1]])
    return np.where((sig != 0) & (sig != prev), sig, 0)


# ------------------------------------------------------------------ signals
def signal_families(t: np.ndarray) -> Dict[str, Dict[str, np.ndarray]]:
    o, h, l, c, v = t[:, 1], t[:, 2], t[:, 3], t[:, 4], t[:, 5]
    lr = np.diff(np.log(c), prepend=np.log(c[0]))
    fam: Dict[str, Dict[str, np.ndarray]] = {}
    sd60 = rolling(lr, 240, np.std)
    for L in (15, 60, 240):
        ret = np.log(c) - np.log(np.roll(c, L))
        ret[:L] = 0
        z = ret / (sd60 * math.sqrt(L) + 1e-12)
        for thr in (1.5, 2.5, 3.5):
            s = np.where(z > thr, 1, np.where(z < -thr, -1, 0))
            fam.setdefault("momentum", {})[f"L{L}z{thr}"] = edge(s)
            fam.setdefault("reversion", {})[f"L{L}z{thr}"] = edge(-s)
    for L in (60, 240, 720):
        hi = np.roll(rolling(h, L, np.max), 1)
        lo = np.roll(rolling(l, L, np.min), 1)
        s = np.where(c > hi, 1, np.where(c < lo, -1, 0))
        s[: L + 1] = 0
        fam.setdefault("breakout", {})[f"L{L}"] = edge(s)
        fam.setdefault("fade-breakout", {})[f"L{L}"] = edge(-s)
    tr = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)), abs(l - np.roll(c, 1))))
    for L, k in itertools.product((30, 120), (0.5, 0.7)):
        short = rolling(tr, L, np.mean)
        long_ = rolling(tr, L * 5, np.mean)
        squeeze = np.roll((short / (long_ + 1e-12)) < k, 1)
        hi = np.roll(rolling(h, L, np.max), 1)
        lo = np.roll(rolling(l, L, np.min), 1)
        s = np.where(squeeze & (c > hi), 1, np.where(squeeze & (c < lo), -1, 0))
        s[: L * 5 + 1] = 0
        fam.setdefault("squeeze-breakout", {})[f"L{L}k{k}"] = edge(s)
    for f, sl in ((60, 240), (240, 960)):
        ef, es = ema(c, f), ema(c, sl)
        trend = np.sign(ef - es)
        e20 = ema(c, 20)
        pull = np.where((trend > 0) & (l <= e20) & (c > e20), 1,
                        np.where((trend < 0) & (h >= e20) & (c < e20), -1, 0))
        pull[: sl] = 0
        fam.setdefault("trend-pullback", {})[f"E{f}/{sl}"] = edge(pull)
    vavg = rolling(v, 240, np.mean)
    for m, zr in itertools.product((4.0, 8.0), (2.0, 3.0)):
        spike = (v > m * (vavg + 1e-12)) & (abs(lr) > zr * (sd60 / 1.0 + 1e-12))
        s = np.where(spike, np.sign(lr), 0).astype(int)
        s[:240] = 0
        fam.setdefault("volume-follow", {})[f"m{m}z{zr}"] = edge(s)
        fam.setdefault("volume-fade", {})[f"m{m}z{zr}"] = edge(-s)
    return fam


# -------------------------------------------------------------------- exits
TPS = (0.3, 0.6, 1.0, 1.5)
SL_RATIOS = (0.6, 1.0, 1.5, 2.0)
HOLDS = (60, 240)


def first_hits(t: np.ndarray, pct: float, side: int, kind: str, hold: int) -> np.ndarray:
    """Bars until price first touches entry*(1 +/- pct) after entry at i+1 open.
    Returns hold+1 when never touched."""
    o, h, l = t[:, 1], t[:, 2], t[:, 3]
    n = len(o)
    entry = np.roll(o, -1)
    out = np.full(n, hold + 1, dtype=np.int32)
    up = (side > 0) == (kind == "tp")
    level = entry * (1 + pct / 100.0) if up else entry * (1 - pct / 100.0)
    for j in range(hold):
        idx = np.arange(n) + 1 + j
        ok = idx < n
        hit = np.zeros(n, dtype=bool)
        if up:
            hit[ok] = h[idx[ok]] >= level[ok]
        else:
            hit[ok] = l[idx[ok]] <= level[ok]
        mask = hit & (out == hold + 1)
        out[mask] = j
    return out


def outcomes(t: np.ndarray, cost: float) -> Dict[Tuple, Dict[int, np.ndarray]]:
    """Net % per entry bar for every exit config and side."""
    o, c = t[:, 1], t[:, 4]
    n = len(o)
    entry = np.roll(o, -1)
    res: Dict[Tuple, Dict[int, np.ndarray]] = {}
    for hold in HOLDS:
        cache: Dict[Tuple, np.ndarray] = {}
        for side in (1, -1):
            idx_close = np.minimum(np.arange(n) + hold, n - 1)
            time_move = side * (c[idx_close] / entry - 1) * 100
            for tp in TPS:
                key_tp = ("tp", tp, side)
                if key_tp not in cache:
                    cache[key_tp] = first_hits(t, tp, side, "tp", hold)
                for r in SL_RATIOS:
                    sl = round(tp * r, 4)
                    key_sl = ("sl", sl, side)
                    if key_sl not in cache:
                        cache[key_sl] = first_hits(t, sl, side, "sl", hold)
                    ht, hs = cache[key_tp], cache[key_sl]
                    move = np.where(hs <= ht, np.where(hs <= hold, -sl, time_move),
                                    np.where(ht <= hold, tp, time_move))
                    res.setdefault((tp, r, hold), {})[side] = move - cost
    return res


# ----------------------------------------------------------------- scoring
def pf(net: np.ndarray, cost: float) -> float:
    """Cost-coordinate PF used by the engine: 1 + 0.1 * mean(net / cost)."""
    if len(net) == 0:
        return 1.0
    return 1.0 + 0.1 * float(np.mean(net)) / cost


def run(args) -> Dict:
    tapes = load_tapes(args.data_dir, args.symbols.split(",") if args.symbols else None)
    if not tapes:
        sys.exit("no tapes found")
    t0 = min(int(t[0, 0]) for t in tapes.values())
    day0 = ((t0 // DAY_MS) + 1) * DAY_MS  # first complete UTC day
    # trades[family][config] -> list of (day, hour, net)
    trades: Dict[str, Dict[str, List[Tuple[int, int, float]]]] = {}
    started = time.time()
    for sym, t in sorted(tapes.items()):
        fams = signal_families(t)
        outs = outcomes(t, args.cost_pct)
        day = ((t[:, 0] - day0) // DAY_MS).astype(int)
        hour = ((t[:, 0] % DAY_MS) // 3_600_000).astype(int)
        valid = (day >= 0) & (np.arange(len(t)) < len(t) - max(HOLDS) - 2)
        for fname, configs in fams.items():
            for cname, sig in configs.items():
                idx = np.nonzero((sig != 0) & valid)[0]
                if len(idx) == 0:
                    continue
                sides = sig[idx]
                for ekey, by_side in outs.items():
                    net = np.where(sides > 0, by_side[1][idx], by_side[-1][idx])
                    key = f"{cname}|tp{ekey[0]}|r{ekey[1]}|h{ekey[2]}"
                    bucket = trades.setdefault(fname, {}).setdefault(key, [])
                    bucket.extend(zip(day[idx].tolist(), hour[idx].tolist(), net.tolist()))
        print(f"  {sym}: {len(t)} bars, {time.time() - started:.0f}s", flush=True)
    n_days = int(max(d for fam in trades.values() for b in fam.values() for d, _, _ in b)) + 1
    families = {}
    for fname, configs in trades.items():
        per_cfg = {}
        for key, rows in configs.items():
            arr = np.array(rows, dtype=float)
            per_cfg[key] = arr
        test_days = []
        for k in range(args.train_days, n_days):
            ranked = []
            for key, arr in per_cfg.items():
                tr = arr[arr[:, 0] < k]
                if len(tr) < args.min_train_trades:
                    continue
                ranked.append((pf(tr[:, 2], args.cost_pct), key))
            ranked.sort(reverse=True)
            picks = [key for p, key in ranked[: args.top] if p > 1.0]
            oos = np.concatenate([per_cfg[key][per_cfg[key][:, 0] == k] for key in picks]) if picks else np.zeros((0, 3))
            hours = {}
            for _, hr, net in oos:
                hours[int(hr)] = hours.get(int(hr), 0.0) + net
            test_days.append({
                "day": k, "picks": picks, "trainPf": [round(p, 4) for p, key in ranked[: args.top] if p > 1.0],
                "n": int(len(oos)), "pf": round(pf(oos[:, 2], args.cost_pct), 4) if len(oos) else None,
                "netPct": round(float(oos[:, 2].sum()), 4) if len(oos) else 0.0,
                "winRate": round(float((oos[:, 2] > 0).mean() * 100), 1) if len(oos) else None,
                "hoursPositive": sum(1 for x in hours.values() if x > 0), "hoursTraded": len(hours),
            })
        traded = [d for d in test_days if d["n"] > 0]
        pos_days = sum(1 for d in traded if d["netPct"] > 0)
        last = test_days[-args.last_days:]
        last_pos = sum(1 for d in last if d["n"] > 0 and d["netPct"] > 0)
        all_net = sum(d["netPct"] for d in test_days)
        all_n = sum(d["n"] for d in test_days)
        hp = sum(d["hoursPositive"] for d in test_days)
        ht = sum(d["hoursTraded"] for d in test_days)
        survives = (last_pos >= math.ceil(args.last_days * 2 / 3) and all_net > 0
                    and all_n / max(1, len(test_days)) >= args.min_trades_day
                    and ht and hp / ht >= args.min_hours_positive)
        families[fname] = {
            "configs": len(per_cfg), "testDays": test_days, "oosTrades": all_n,
            "oosNetPct": round(all_net, 3), "oosPf": round(1 + 0.1 * (all_net / max(all_n, 1)) / args.cost_pct, 4),
            "positiveDays": pos_days, "tradedDays": len(traded),
            "lastPositive": last_pos, "lastDays": len(last),
            "hoursPositivePct": round(100 * hp / ht, 1) if ht else None, "survives": bool(survives),
        }
    return {"generatedAt": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "symbols": sorted(tapes),
            "days": n_days, "costPct": args.cost_pct, "trainDays": args.train_days, "top": args.top,
            "minTrainTrades": args.min_train_trades, "exitGrid": {"tp": TPS, "slRatio": SL_RATIOS, "holdBars": HOLDS},
            "rule": (f"positive OOS on >= {math.ceil(args.last_days * 2 / 3)} of the last {args.last_days} days, "
                     f"total OOS net > 0, >= {args.min_trades_day} trades/day, "
                     f">= {int(args.min_hours_positive * 100)}% of traded hours positive"),
            "families": families}


# ------------------------------------------------------------------- report
def render(rep: Dict) -> str:
    fams = sorted(rep["families"].items(), key=lambda kv: -kv[1]["oosNetPct"])
    w, hgt, pad = 720, 260, 36
    days = [d["day"] for d in fams[0][1]["testDays"]] if fams else []
    series = []
    lo = hi = 0.0
    for name, f in fams:
        acc, pts = 0.0, []
        for d in f["testDays"]:
            acc += d["netPct"]
            pts.append(acc)
        lo, hi = min(lo, *pts), max(hi, *pts)
        series.append((name, pts, f["survives"]))
    span = (hi - lo) or 1.0
    palette = ["#2a6fdb", "#d9822b", "#2e9e6a", "#c23b3b", "#7a5bc7", "#8a6d3b", "#1f9bb5", "#b5487a", "#5f6b7a", "#9aa53a"]
    lines = []
    for i, (name, pts, ok) in enumerate(series):
        if not pts:
            continue
        xy = " ".join(f"{pad + j * (w - 2 * pad) / max(1, len(pts) - 1):.1f},{hgt - pad - (p - lo) / span * (hgt - 2 * pad):.1f}"
                      for j, p in enumerate(pts))
        lines.append(f'<polyline fill="none" stroke="{palette[i % len(palette)]}" stroke-width="{2.5 if ok else 1.5}" points="{xy}"><title>{html.escape(name)}</title></polyline>')
    zero_y = hgt - pad - (0 - lo) / span * (hgt - 2 * pad)
    legend = "".join(f'<span class="lg"><i style="background:{palette[i % len(palette)]}"></i>{html.escape(n)}</span>' for i, (n, _, _) in enumerate(series))
    rows = "".join(
        f"<tr class='{'ok' if f['survives'] else ''}'><td>{html.escape(n)}</td><td>{f['configs']}</td><td>{f['oosTrades']}</td>"
        f"<td>{f['oosTrades'] / max(1, len(f['testDays'])):.0f}</td><td>{f['oosPf']:.3f}</td><td>{f['oosNetPct']:+.2f}</td>"
        f"<td>{f['positiveDays']}/{f['tradedDays']}</td><td>{f['lastPositive']}/{f['lastDays']}</td>"
        f"<td>{'' if f['hoursPositivePct'] is None else f['hoursPositivePct']}</td><td>{'yes' if f['survives'] else 'no'}</td></tr>"
        for n, f in fams)
    detail = ""
    for n, f in fams:
        detail += f"<h3>{html.escape(n)}</h3><table><tr><th>day</th><th>trades</th><th>PF</th><th>net %</th><th>win %</th><th>hours +/traded</th><th>picked (in-sample PF)</th></tr>"
        for d in f["testDays"]:
            picks = ", ".join(f"{html.escape(k)} ({p})" for k, p in zip(d["picks"], d["trainPf"]))
            detail += (f"<tr><td>{d['day']}</td><td>{d['n']}</td><td>{'' if d['pf'] is None else d['pf']}</td><td>{d['netPct']:+.2f}</td>"
                       f"<td>{'' if d['winRate'] is None else d['winRate']}</td><td>{d['hoursPositive']}/{d['hoursTraded']}</td><td class='pk'>{picks or '—'}</td></tr>")
        detail += "</table>"
    survivors = [n for n, f in fams if f["survives"]]
    verdict = (f"<p class='good'>Survivors: {', '.join(map(html.escape, survivors))}</p>" if survivors
               else "<p class='bad'>No family survives out-of-sample after costs.</p>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signal Edge Scan</title><style>
:root{{--bg:#fff;--fg:#1d2330;--mut:#5f6b7a;--line:#e3e7ee;--ok:#e8f6ee;--good:#1d7a4a;--bad:#b23a3a}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151b;--fg:#e6e9ef;--mut:#9aa3b2;--line:#2a303b;--ok:#17301f;--good:#5bc58a;--bad:#e07a7a}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px}}
table{{border-collapse:collapse;width:100%;margin:8px 0 20px;font-variant-numeric:tabular-nums}}td,th{{border-bottom:1px solid var(--line);padding:4px 6px;text-align:right}}
td:first-child,th:first-child,.pk{{text-align:left}}.pk{{font-size:12px;color:var(--mut)}}tr.ok{{background:var(--ok)}}
.good{{color:var(--good);font-weight:600}}.bad{{color:var(--bad);font-weight:600}}.mut{{color:var(--mut)}}
.lg{{display:inline-flex;align-items:center;margin-right:12px;font-size:12px}}.lg i{{width:10px;height:10px;margin-right:4px;display:inline-block}}
.wrap{{overflow-x:auto}}svg{{max-width:100%;height:auto}}</style></head><body>
<h1>Signal edge scan</h1>
<p class="mut">{len(rep['symbols'])} symbols · {rep['days']} UTC days · cost {rep['costPct']}% per trade · train on all prior days (min {rep['trainDays']}), trade the top {rep['top']} in-sample configs next day · generated {rep['generatedAt']}</p>
<p class="mut">Survival rule: {html.escape(rep['rule'])}</p>{verdict}
<h2>Cumulative out-of-sample net % (sum of trade %)</h2>
<div class="wrap"><svg viewBox="0 0 {w} {hgt}" role="img"><line x1="{pad}" x2="{w - pad}" y1="{zero_y:.1f}" y2="{zero_y:.1f}" stroke="currentColor" stroke-opacity=".3"/>{''.join(lines)}
<text x="{pad}" y="14" font-size="11" fill="currentColor">{hi:+.1f}%</text><text x="{pad}" y="{hgt - 6}" font-size="11" fill="currentColor">{lo:+.1f}% · test days {days[0] if days else ''}–{days[-1] if days else ''}</text></svg></div>
<div>{legend}</div>
<h2>Families (out-of-sample)</h2><div class="wrap"><table><tr><th>family</th><th>configs</th><th>trades</th><th>trades/day</th><th>PF</th><th>net %</th><th>days +</th><th>last days +</th><th>hours + %</th><th>survives</th></tr>{rows}</table></div>
<h2>Per day</h2><div class="wrap">{detail}</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--cost-pct", type=float, default=0.18)
    ap.add_argument("--train-days", type=int, default=3)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--min-train-trades", type=int, default=60)
    ap.add_argument("--last-days", type=int, default=6)
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
        print(f"{name:18s} trades={f['oosTrades']:6d} pf={f['oosPf']:.3f} net={f['oosNetPct']:+8.2f}% "
              f"days+={f['positiveDays']}/{f['tradedDays']} last+={f['lastPositive']}/{f['lastDays']} "
              f"hours+={f['hoursPositivePct']} survives={f['survives']}")


if __name__ == "__main__":
    main()
