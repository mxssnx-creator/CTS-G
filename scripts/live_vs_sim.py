#!/usr/bin/env python3
"""Compare a running engine (live stats) with the simulator on the same bars, and test the minimum distances.

1. Set scores: the engine publishes, for every Set, its gate PF (`sets.index`). The same 1m bars are fetched from the
   exchange's public kline endpoint (the venue the engine uses), the Sets are replayed offline with the engine's own
   SetBook, and the two score tables are compared: how many Sets agree on valid/invalid, the PF difference, and the
   Sets that differ most. A difference here is a processing difference between the live path and the simulator.
2. Orders: when the engine has closed orders, each is matched to the simulator's order for the same Set and symbol
   (entry bar within +-2 minutes) and compared on entry price, exit reason, exit price and net result.
3. Lower performers: live Sets, symbols and exit reasons ranked by net result with their order counts.
4. Minimum distances: each live order's price path (bars after its entry) is replayed with the stop floor at 0..4 steps
   over PositionCost and, for trailing Sets, the trailing distance floor at 1..5 steps, to show which minimum would
   have done best on the orders that actually lost. The replay uses SL, TP and trailing only (no exit coordinator), so it
   is a guide to the direction of the change, not a forecast.

usage: live_vs_sim.py --stats /opt/grok-x01-pulse/stats-bingx-x02.json --overlay overlay.json --out report.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import set_engine as se  # noqa: E402
from set_engine import BAR_S, SetBook, hit_exit  # noqa: E402
from position_cost import configure_min_steps, net_pnl_pct  # noqa: E402

KLINES = "https://open-api-vst.bingx.com/openApi/swap/v3/quote/klines"


def fetch_bars(symbol: str, n: int = 1920, tries: int = 4) -> List[List[float]]:
    """The newest n 1m bars as [t_ms, o, h, l, c, v], oldest first (two pages of up to 1440)."""
    rows: Dict[int, List[float]] = {}
    end = None
    while len(rows) < n:
        url = f"{KLINES}?symbol={symbol}&interval=1m&limit=1000" + (f"&endTime={end}" if end else "")
        data = None
        for k in range(tries):
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    body = json.loads(r.read().decode())
                if body.get("code") == 0:
                    data = body.get("data") or []
                    break
            except Exception:
                pass
            time.sleep(1.5 * (k + 1))
        if not data:
            break
        for d in data:
            rows[int(d["time"])] = [int(d["time"]), float(d["open"]), float(d["high"]), float(d["low"]), float(d["close"]), float(d["volume"])]
        first = min(int(d["time"]) for d in data)
        if end is not None and first >= end:
            break
        end = first - 1
        time.sleep(0.3)
    return [rows[t] for t in sorted(rows)][-n:]


def pf_of(vals: List[float]) -> float:
    gp = sum(v for v in vals if v > 0)
    gl = -sum(v for v in vals if v < 0)
    return round(gp / gl, 4) if gl > 0 else (99.0 if gp > 0 else 0.0)


def compare_scores(book: SetBook, live_index: List[Dict[str, Any]]) -> Dict[str, Any]:
    live = {r["id"]: r for r in live_index}
    rows = []
    for st in book.by_idx:
        r = live.get(st.id)
        if r is None:
            continue
        rows.append({"id": st.id, "live_pf": float(r["pf"]), "sim_pf": round(st.gate_pf, 4), "live_valid": bool(r.get("on")),
                     "sim_valid": bool(book.is_eligible(st)), "sim_n": st.gate_n})
    agree = sum(1 for x in rows if x["live_valid"] == x["sim_valid"])
    diffs = sorted(rows, key=lambda x: -abs(x["live_pf"] - x["sim_pf"]))
    return {"sets": len(rows), "state_agreement_pct": round(100.0 * agree / len(rows), 2) if rows else None,
            "live_valid": sum(1 for x in rows if x["live_valid"]), "sim_valid": sum(1 for x in rows if x["sim_valid"]),
            "mean_abs_pf_diff": round(sum(abs(x["live_pf"] - x["sim_pf"]) for x in rows) / len(rows), 4) if rows else None,
            "largest_diffs": diffs[:15]}


def replay_path(side: int, entry_i: int, bars: List[List[float]], sl_frac: float, tp_frac: float, trail: Optional[Tuple[float, float]],
                time_bars: int, honor_tp: bool) -> Tuple[float, str]:
    """Gross result of one entry replayed on the bars after it: SL/TP/trailing/time, pessimistic same-bar rule."""
    entry = bars[entry_i][4]
    sl = entry * (1 - side * sl_frac)
    tp = entry * (1 + side * tp_frac)
    peak = entry
    tr: Optional[float] = None
    last = min(len(bars) - 1, entry_i + time_bars)
    for j in range(entry_i + 1, last + 1):
        b = [bars[j][1], bars[j][2], bars[j][3], bars[j][4], bars[j][5]]
        if trail:
            arm, give = trail
            peak = max(peak, b[1]) if side > 0 else min(peak, b[2])
            if side * (peak - entry) / entry >= arm:
                cand = peak * (1 - side * give)
                tr = cand if tr is None else (max(tr, cand) if side > 0 else min(tr, cand))
        why, px = hit_exit(side, entry, sl, tp, tr, b, ignore_tp=not honor_tp)
        if why:
            return side * (px - entry) / entry, why
    return side * (bars[last][4] - entry) / entry, "time"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", required=True)
    ap.add_argument("--overlay", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--trades", default="", help="optional JSONL of closed orders (default: stats.closed)")
    ap.add_argument("--max-symbols", type=int, default=40)
    ap.add_argument("--cache", default="", help="JSON cache of fetched bars")
    a = ap.parse_args()
    stats = json.load(open(a.stats))
    ov = json.load(open(a.overlay))
    cost = float(ov.get("positionCostPct") or stats.get("pfCost", {}).get("costPct") or 0.12)
    syms = [s for s in (stats.get("symbols") or [])][: a.max_symbols]
    cache: Dict[str, List[List[float]]] = {}
    if a.cache and os.path.exists(a.cache):
        cache = json.load(open(a.cache))
    for s in syms:
        if s not in cache or len(cache[s]) < 600:
            cache[s] = fetch_bars(s)
    if a.cache:
        json.dump(cache, open(a.cache, "w"))
    book = SetBook()
    book.load(ov)
    se.HIST_CAP = 10 ** 9
    for s in syms:
        if len(cache.get(s) or []) >= 100:
            book.bars[s] = [[r[1], r[2], r[3], r[4], r[5]] for r in cache[s]]
    now = max((cache[s][-1][0] for s in syms if cache.get(s)), default=int(time.time() * 1000)) / 1000.0 + 60.0
    book.progress.ready = True
    book.replay_all(now=now)
    out: Dict[str, Any] = {"symbols_with_bars": sum(1 for s in syms if s in book.bars), "cost_pct": cost,
                           "scores": compare_scores(book, (stats.get("sets") or {}).get("index") or [])}
    closed: List[Dict[str, Any]] = []
    if a.trades and os.path.exists(a.trades):
        closed = [json.loads(x) for x in open(a.trades) if x.strip()]
    else:
        closed = list(stats.get("closed") or [])
    closed = [c for c in closed if c.get("set_id") and c.get("symbol") in cache and c.get("pnl_pct") is not None]
    orders: List[Dict[str, Any]] = []
    cf_sl = {k: [] for k in range(0, 5)}
    cf_tr = {k: [] for k in range(1, 6)}
    cf_n = 0
    for c in closed:
        bars = cache[c["symbol"]]
        t_exit = float(c["t"])
        t_in = t_exit - float(c.get("hold_s") or 0)
        idx = next((i for i in range(len(bars) - 1, -1, -1) if bars[i][0] / 1000.0 <= t_in + 1), None)
        st = book.sets.get(str(c["set_id"]))
        if idx is None or st is None:
            continue
        side = 1 if str(c.get("side")).upper() == "LONG" else -1
        live_net = float(c["pnl_pct"]) * 100.0 - cost
        entry_bar_px = bars[idx][4]
        slip = side * (float(c["entry"]) - entry_bar_px) / entry_bar_px * 100.0 * -1.0
        orders.append({"set_id": c["set_id"], "symbol": c["symbol"], "side": c["side"], "reason": c.get("reason"),
                       "live_net_pct": round(live_net, 4), "entry_slip_pct": round(slip, 4), "hold_s": c.get("hold_s")})
        base_sl = st.tp_pct * (st.sl_ratio if st.kind == "base" else se.TRAIL_SL_RATIO)
        trail = (st.trail_arm / 100.0, st.trail_give / 100.0) if st.kind == "trail" else None
        cf_n += 1
        for k in cf_sl:
            configure_min_steps(k, None)
            sl_frac = max((1 + k) * cost / 100.0, base_sl)
            g, _ = replay_path(side, idx, bars, sl_frac, max(2 * cost / 100.0, st.tp_pct), trail, 360, bool(book.hist_honor_tp))
            cf_sl[k].append(g * 100.0 - cost)
        configure_min_steps(1, 2)
        if trail:
            for k in cf_tr:
                give = max(trail[1], (1 + k) * cost / 100.0)
                arm = max(trail[0], give)
                sl_frac = max((1 + 1) * cost / 100.0, base_sl)
                g, _ = replay_path(side, idx, bars, sl_frac, max(2 * cost / 100.0, st.tp_pct), (arm, give), 360, bool(book.hist_honor_tp))
                cf_tr[k].append(g * 100.0 - cost)
    configure_min_steps(1, 2)
    out["orders"] = {"matched": len(orders), "live_pf": pf_of([o["live_net_pct"] for o in orders]),
                     "mean_entry_slip_pct": round(sum(o["entry_slip_pct"] for o in orders) / len(orders), 5) if orders else None}

    def group(key: str) -> List[Dict[str, Any]]:
        g: Dict[str, List[float]] = {}
        for o in orders:
            g.setdefault(str(o[key]), []).append(o["live_net_pct"])
        rows = [{key: k, "orders": len(v), "pf": pf_of(v), "net_pct": round(sum(v), 3)} for k, v in g.items()]
        return sorted(rows, key=lambda r: r["net_pct"])

    out["lower_performers"] = {"sets": group("set_id")[:15], "symbols": group("symbol")[:15], "exit_reasons": group("reason")}
    out["min_distance_replay"] = {
        "orders_replayed": cf_n,
        "stop_floor_steps": {str(k): {"pf": pf_of(v), "net_pct": round(sum(v), 3), "n": len(v)} for k, v in cf_sl.items() if v},
        "trail_floor_steps": {str(k): {"pf": pf_of(v), "net_pct": round(sum(v), 3), "n": len(v)} for k, v in cf_tr.items() if v},
    }
    json.dump(out, open(a.out, "w"), indent=1)
    sc = out["scores"]
    print(f"bars for {out['symbols_with_bars']} symbols; Sets compared {sc['sets']}, state agreement {sc['state_agreement_pct']}%, "
          f"valid live {sc['live_valid']} / sim {sc['sim_valid']}, mean |PF diff| {sc['mean_abs_pf_diff']}; "
          f"live orders matched {out['orders']['matched']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
