#!/usr/bin/env python3
"""12-hour trade simulation, hour by hour, on stored 1m history (stage_eval's walk-forward, the engine's own Set code).

Window: PRE hours of pre-history (Base validation reads only these and what follows), then HOURS simulated hours. Each
hour: every Set is scored from its shadow tape (gate window, minimum orders, PF, drawdown time: SetBook._score_one's rule,
checked against the engine on samples), the valid Sets are ranked, and positions open under the best valid Set of the
signalling pack and leave by that Set's own lane. Axis is the coordinator's gate on the executed closes. Block and DCA are
reported as add-on lanes on the executed positions (hypothetical when switched off in the shipped config; each is scored
on its own legs / adds only).

Money: a start balance, one fixed notional per position, net PnL = notional x (return - PositionCost). Equity is built from
the closes in time order; drawdown is peak to trough of that equity.

usage: sim12h.py --data DIR --out DIR [--hours 12] [--pre 12] [--balance 10] [--notional 2.15] [--alt-notional 0.2]
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import stage_eval as sg  # noqa: E402
import dca_eval as dv  # noqa: E402
import set_engine as se  # noqa: E402
from position_cost import cost_as_frac  # noqa: E402

OVERLAY_X02 = os.path.join(HERE, "..", "server", "pulse", "overlay-bingx-x02.json")


def pf_of(vals):
    gp = sum(v for v in vals if v > 0)
    gl = -sum(v for v in vals if v < 0)
    if gl > 0:
        return round(gp / gl, 3)
    return None if gp <= 0 else 99.0          # 99 = no loss (shown as "no loss")


def pool(book, tapes, hour):
    """The engine's pfBase at `hour`: every Set's gate window, pooled before and after Base validation."""
    cf = tapes.cf
    lb = book.lookback * 60.0
    tot = val = 0
    gp_a = gl_a = gp_v = gl_v = 0.0
    n_a = n_v = 0
    ddts = []
    by_pack = {}
    for st in book.by_idx:
        ok, key, n, pf = sg.gate_metrics(book, tapes, st, hour)
        tx, te, pp = tapes.window(st.idx, hour, lb, max(book.gate_window, book.deact_n))
        w = pp[-book.gate_window:]
        gp = sum(x - cf for x in w if x - cf > 0)
        gl = -sum(x - cf for x in w if x - cf < 0)
        tot += 1
        gp_a += gp
        gl_a += gl
        n_a += len(w)
        p = by_pack.setdefault(st.pack, [0, 0, 0.0, 0.0, 0])
        p[0] += 1
        if ok:
            val += 1
            p[1] += 1
            p[2] += gp
            p[3] += gl
            p[4] += len(w)
            gp_v += gp
            gl_v += gl
            n_v += len(w)
            ddts.append(-key[2] / 3600.0)
    return {"total": tot, "valid": val, "validated_pct": round(100.0 * val / tot, 2) if tot else 0.0,
            "pf_pre": round(gp_a / gl_a, 3) if gl_a > 0 else None, "orders_pre": n_a,
            "pf_post": (round(gp_v / gl_v, 3) if gl_v > 0 else (99.0 if gp_v > 0 else None)) if val else None, "orders_post": n_v,
            "ddt_mean_h": round(sum(ddts) / len(ddts), 3) if ddts else None, "ddt_max_h": round(max(ddts), 3) if ddts else None,
            "by_pack": {k: {"total": v[0], "valid": v[1], "pct": round(100.0 * v[1] / v[0], 2) if v[0] else 0.0,
                            "pf_post": round(v[2] / v[3], 3) if v[3] > 0 else None, "orders": v[4]} for k, v in by_pack.items()}}


def run(a):
    t0 = time.time()
    sg.OVERLAY = os.path.abspath(a.overlay)
    names, bars, times = sg.load_bars(a.data, a.symbols)
    meta = json.load(open(os.path.join(a.data, "_meta.json")))
    t_end = meta["t_end"] // 1000
    t_sim0 = t_end - a.hours * 3600
    shadow = sg.build(names, bars, times, a.jobs)
    book = sg.make_book()
    book.lookback = a.pre * 60                                   # Base validation reads the pre-history window
    if getattr(a, "real", None) is not None:                     # --real N: the Real gate's last N (0 = off); default = the engine's shipped value
        book.real_on = a.real > 0
        book.real_n = a.real or book.real_n
    tapes = sg.Tapes(book, names, times, shadow)
    hours = [float(t_sim0 + k * 3600) for k in range(a.hours)]
    n_chk, bad = sg.self_check(book, tapes, hours)
    print(f"symbols={len(names)} sets={len(book.by_idx)} sim {time.strftime('%F %H:%M', time.gmtime(hours[0]))} -> "
          f"{time.strftime('%F %H:%M', time.gmtime(t_end))} UTC, pre-history {a.pre} h, gate check {n_chk - bad}/{n_chk}, build {time.time() - t0:.0f}s", flush=True)
    cfg = sg.lane_cfg(book)
    N = int(getattr(a, "counts", 0) or 0)       # 0 = the counts as shipped before (windows 6, Axis last 6, Block without samples); 8 / 12 = every count at N
    axis_cfg = (N or 6, 1.0, 0)          # the best Axis found on the 30-day development window (axis_eval): last 6 closes at or above PF 1.0
    free = sg.execute(book, names, bars, times, shadow, tapes, hours)
    evidence = sorted((r["t"], r["net"]) for r in free if "net" in r)           # the Axis reads a paper stream, see axis_eval
    runs = {"axis": sg.execute(book, names, bars, times, shadow, tapes, hours, axis=axis_cfg, axis_evidence=evidence), "no axis": free}
    cf = cost_as_frac(book.cost_pct)

    def trades_of(rows):
        return sorted((r for r in rows if "net" in r), key=lambda r: r["t"])

    primary = trades_of(runs["no axis"])          # the shipped config: the Axis gates are off
    plain = trades_of(runs["axis"])               # the alternative shown next to it
    # add-on lanes on the executed positions (own results only)
    block = []
    dca = []
    done_nets = []                                   # nets of the executed closes before each parent opened: Block's own evidence
    for r in primary:
        seen = [x for t_, x in sorted((q["t"], q["net"]) for q in primary) if t_ <= r["t_in"]]
        w = seen[-N:] if N else []
        g, l_ = sum(x for x in w if x > 0), -sum(x for x in w if x < 0)
        if N and not (len(w) >= N and (g / l_ if l_ > 0 else 99.0) >= 1.0):
            continue                                  # Block adds only on N closes of evidence at or above PF 1.0
        for v in sg.block_legs(bars[r["symbol"]], r["tr"], (1, 6), book.cost_pct):
            block.append((r["t"], v))
        base_net, _ = dv.walk(bars[r["symbol"]], r["tr"], r["fracs"], cfg, [])
        with_net, size = dv.walk(bars[r["symbol"]], r["tr"], r["fracs"], cfg, [(0.4, 0.5)])
        if size > 1.0:
            dca.append((r["t"], with_net - base_net))

    evals = []
    for k in range(a.hours * 4 + 1):                              # continuous evaluation every 15 minutes
        t = float(t_sim0 + k * 900)
        e = pool(book, tapes, t)
        e["t"] = t
        evals.append(e)
    hourly = []
    eq = {"main": a.balance, "alt": a.balance}
    peak = dict(eq)
    series = []                                                    # (t, equity main, equity alt, dd main usd, dd main pct)
    cum = []
    ti = 0
    last15 = []
    block_cum, dca_cum = [], []
    ddt_open = 0.0
    for hi, h in enumerate(hours):
        ev = next(e for e in evals if abs(e["t"] - h) < 1)
        in_h = [r for r in primary if h <= r["t"] < h + 3600]
        opened = [r for r in primary if h <= r["t_in"] < h + 3600]
        open_end = sum(1 for r in primary if r["t_in"] < h + 3600 <= r["t"])
        nets = [r["net"] for r in in_h]
        for r in in_h:
            cum.append(r["net"])
            last15.append(r["net"])
            for key, n_ in (("main", a.notional), ("alt", a.alt_notional)):
                eq[key] += n_ * r["net"]
                peak[key] = max(peak[key], eq[key])
            series.append((r["t"], eq["main"], eq["alt"], peak["main"] - eq["main"], 100.0 * (peak["main"] - eq["main"]) / peak["main"]))
        wins = sum(1 for x in nets if x > 0)
        bh = [v for t, v in block if h <= t < h + 3600]
        dh = [v for t, v in dca if h <= t < h + 3600]
        block_cum += bh
        dca_cum += dh
        dd_usd = peak["main"] - eq["main"]
        hourly.append({
            "hour": time.strftime("%m-%d %H:00", time.gmtime(h)), "t": h,
            "sets_total": ev["total"], "sets_valid": ev["valid"], "validated_pct": ev["validated_pct"],
            "pf_pre": ev["pf_pre"], "orders_pre": ev["orders_pre"], "pf_post": ev["pf_post"], "orders_post": ev["orders_post"],
            "ddt_mean_h": ev["ddt_mean_h"], "ddt_max_h": ev["ddt_max_h"], "by_pack": ev["by_pack"],
            "opened": len(opened), "closed": len(in_h), "open_at_end": open_end,
            "orders": 2 * len(in_h) + open_end, "win_rate": round(100.0 * wins / len(in_h), 1) if in_h else None,
            "hour_net_pct": round(100 * sum(nets), 3), "hour_pf": pf_of(nets), "cum_pf": pf_of(cum), "cum_orders": len(cum),
            "last15_pf": pf_of(last15[-15:]),
            "equity": round(eq["main"], 4), "equity_alt": round(eq["alt"], 4), "dd_usd": round(dd_usd, 4),
            "dd_pct": round(100.0 * dd_usd / peak["main"], 3),
            "axis_pf": pf_of([r["net"] for r in plain if r["t"] < h + 3600]), "noaxis_pf_cum": pf_of(cum),
            "block_pf_hour": pf_of(bh), "block_pf_cum": pf_of(block_cum), "block_legs": len(block_cum),
            "dca_pf_hour": pf_of(dh), "dca_pf_cum": pf_of(dca_cum), "dca_adds": len(dca_cum),
        })
    # drawdown time of the account equity: the longest stretch below a prior peak, in hours
    peak_v, peak_t, longest, cur_start = a.balance, hours[0], 0.0, None
    for t, e1, _, _, _ in series:
        if e1 >= peak_v - 1e-12:
            if cur_start is not None:
                longest = max(longest, t - cur_start)
                cur_start = None
            peak_v, peak_t = e1, t
        elif cur_start is None:
            cur_start = peak_t
    if cur_start is not None:
        longest = max(longest, t_end - cur_start)
    total = {
        "trades": len(primary), "pf_exec": pf_of([r["net"] for r in primary]), "pf_exec_axis": pf_of([r["net"] for r in plain]),
        "trades_axis": len(plain), "win_rate": round(100.0 * sum(1 for r in primary if r["net"] > 0) / len(primary), 1) if primary else None,
        "equity_end": round(eq["main"], 4), "equity_alt_end": round(eq["alt"], 4),
        "max_dd_usd": round(max([s[3] for s in series] or [0.0]), 4), "max_dd_pct": round(max([s[4] for s in series] or [0.0]), 3),
        "equity_ddt_h": round(longest / 3600.0, 2),
        "block_pf": pf_of([v for _, v in block]), "block_legs": len(block), "dca_pf": pf_of([v for _, v in dca]), "dca_adds": len(dca),
        "settings": {"overlay": os.path.basename(sg.OVERLAY), "balance": a.balance, "notional": a.notional, "alt_notional": a.alt_notional,
                     "cost_pct": book.cost_pct, "pre_hours": a.pre, "hours": a.hours, "symbols": names, "gate_check": f"{n_chk - bad}/{n_chk}",
                     "axis": list(axis_cfg), "sets": len(book.by_idx), "gate_min_orders": book.gate_min, "gate_window": book.gate_window,
                     "min_pf": book.min_pf, "max_ddt_h": book.max_dd_s / 3600.0},
    }
    trade_rows = [{"t": r["t"], "t_in": r["t_in"], "symbol": r["symbol"], "set": r["set"], "kind": r["kind"], "reason": r["reason"],
                   "net_pct": round(100 * r["net"], 4)} for r in primary]
    os.makedirs(a.out, exist_ok=True)
    out = {"total": total, "hourly": hourly, "evals15": [{k: v for k, v in e.items() if k != "by_pack"} for e in evals],
           "equity": [list(s) for s in series], "trades": trade_rows}
    json.dump(out, open(os.path.join(a.out, "sim12h.json"), "w"), indent=1)
    total["counts"] = N
    print(f"done in {time.time() - t0:.0f}s: trades {len(primary)} PF {total['pf_exec']} equity {a.balance} -> {total['equity_end']} maxDD {total['max_dd_pct']}%")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--overlay", default=OVERLAY_X02)
    ap.add_argument("--symbols", type=int, default=12)
    ap.add_argument("--hours", type=int, default=12)
    ap.add_argument("--pre", type=int, default=12)
    ap.add_argument("--balance", type=float, default=10.0)
    ap.add_argument("--notional", type=float, default=2.15)
    ap.add_argument("--alt-notional", dest="alt_notional", type=float, default=0.2)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--real", type=int, default=None, help="the Real gate's last N (0 = off); omitted = the shipped overlay")
    ap.add_argument("--counts", type=int, default=0, help="0 = as before; N = the Axis last-N and Block's evidence at N")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
