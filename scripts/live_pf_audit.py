#!/usr/bin/env python3
"""Read-only audit of the live executed PF: recompute it from the trades file with plain loops and diff it with the engine's own figures.

The engine reports, per scope (overall, lane:pos|neg, pack:<pack>, set:<id>:<lane>), `stats.execution.overall.scopes[scope].executed` = {orders, wins, pf, cashPf, ...}:
  pf     = sum of the positive net position returns / sum of the negative ones (`net_pnl_pct`, a fraction of the base notional, one value per close),
  cashPf = the same on the cash result `pnl` (USDT, fees included),
over the Set-lane closes of this connection (`pulse_trader.Pulse._exec_note`), skipping the rows `pulse_trader.not_scored_close` skips: a close whose exit price could not be
verified (reason "...unverified-exit") and a close the venue made while the engine was down (reason "...-downtime", or a close time inside an interval of the downtime state file
`downtime-<conn>.json`: the Set's own stop / trailing / time exit did not run, so it is not a result of the Set's rules). A scope whose returns are not all known reports pf None
(a partial sample is never reported as the cohort's PF).

usage: live_pf_audit.py [--trades PATH] [--stats PATH] [--downtime PATH] [--conn bingx-x01] [--tol 0.0011] [--all]
Exit code 1 when a figure differs by more than the tolerance (PF is rounded to 3 decimals by the engine, so 0.0011 is the default). Nothing is written, no API is called."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

DEFAULT_DIR = "/opt/grok-x01-pulse"


def number(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def downtime_intervals(path):
    try:
        raw = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for item in raw if isinstance(raw, list) else []:
        try:
            a, b = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(a) and math.isfinite(b) and b > a:
            out.append((a, b))
    return out


def not_scored(rec, intervals=()):
    """`pulse_trader.not_scored_close`, restated: unverified exit, a downtime close (reason suffix) or a close time inside an outage interval."""
    reason = str(rec.get("reason") or "").lower()
    if "unverified-exit" in reason or "-downtime" in reason:
        return True
    t = number(rec.get("t"))
    return t is not None and any(a < t <= b for a, b in intervals)


def scored(rec, conn, stats_t=None, intervals=()):
    """The rows the engine counts: ours, a Set-lane close of this connection, a sane time, not an unverified exit or a downtime close (pulse_trader loader, `_exec_note`)."""
    if rec.get("ours", True) is False:
        return False
    if not (str(rec.get("set_id") or rec.get("setId") or "")):
        return False
    c = str(rec.get("conn") or "")
    if c and conn and c != conn:
        return False
    t = number(rec.get("t"))
    if t is None or not (1.6e9 < t):
        return False
    if stats_t is not None and t > stats_t:
        return False                                   # closed after the snapshot being compared
    return not not_scored(rec, intervals)


def recompute(rows, conn="bingx-x01", stats_t=None, intervals=()):
    """{scope: {orders, wins, return_n, return_gain, return_loss, cash_n, cash_gain, cash_loss}} by plain loops over the rows."""
    out = {}
    for rec in rows:
        if not scored(rec, conn, stats_t, intervals):
            continue
        lane = "neg" if str(rec.get("lane") or "") == "neg" else "pos"
        cash, ret = number(rec.get("pnl")), number(rec.get("net_pnl_pct"))
        for key in ("overall", f"lane:{lane}", f"pack:{rec.get('pack') or '-'}", f"set:{rec.get('set_id') or rec.get('setId')}:{lane}"):
            r = out.setdefault(key, {"orders": 0, "wins": 0, "return_n": 0, "return_gain": 0.0, "return_loss": 0.0, "cash_n": 0, "cash_gain": 0.0, "cash_loss": 0.0})
            r["orders"] += 1
            if cash is not None:
                r["cash_n"] += 1
                r["wins"] += int(cash > 0)
                r["cash_gain"] += max(0.0, cash)
                r["cash_loss"] += max(0.0, -cash)
            if ret is not None:
                r["return_n"] += 1
                r["return_gain"] += max(0.0, ret)
                r["return_loss"] += max(0.0, -ret)
    return out


def figures(r):
    """The engine's published figures for one scope (same rounding and the same None rules)."""
    full_ret = r["orders"] > 0 and r["return_n"] == r["orders"]
    full_cash = r["orders"] > 0 and r["cash_n"] == r["orders"]
    return {"orders": r["orders"], "wins": r["wins"],
            "pf": round(r["return_gain"] / r["return_loss"], 3) if full_ret and r["return_loss"] > 0 else None,
            "cashPf": round(r["cash_gain"] / r["cash_loss"], 3) if full_cash and r["cash_loss"] > 0 else None}


def diff(plain, scopes, tol=0.0011, only_with_orders=True):
    """Differences between the plain reading and the engine's `executed` blocks: [(scope, field, plain, engine)]."""
    bad = []
    for key in sorted(set(plain) | set(scopes)):
        e = (scopes.get(key) or {}).get("executed") or {}
        if only_with_orders and not e.get("orders") and key not in plain:
            continue
        p = figures(plain[key]) if key in plain else {"orders": 0, "wins": 0, "pf": None, "cashPf": None}
        for f in ("orders", "wins"):
            if int(e.get(f) or 0) != p[f]:
                bad.append((key, f, p[f], e.get(f)))
        for f in ("pf", "cashPf"):
            a, b = p[f], e.get(f)
            if (a is None) != (b is None) or (a is not None and abs(a - b) > tol):
                bad.append((key, f, a, b))
    return bad


def pf_of(vals):
    g = sum(v for v in vals if v > 0)
    l = -sum(v for v in vals if v < 0)
    return g / l if l > 0 else float("inf")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--trades", default=os.path.join(DEFAULT_DIR, "trades-bingx-x01.jsonl"))
    ap.add_argument("--stats", default=os.path.join(DEFAULT_DIR, "stats-bingx-x01.json"))
    ap.add_argument("--downtime", default=os.path.join(DEFAULT_DIR, "downtime-bingx-x01.json"))
    ap.add_argument("--conn", default="bingx-x01")
    ap.add_argument("--tol", type=float, default=0.0011)
    ap.add_argument("--all", action="store_true", help="also list the matching scopes")
    a = ap.parse_args(argv)
    rows = []
    for line in open(a.trades, encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    stats = json.load(open(a.stats, encoding="utf-8"))
    stats_t = number(stats.get("now"))
    scopes = ((stats.get("execution") or {}).get("overall") or {}).get("scopes") or {}
    intervals = downtime_intervals(a.downtime)
    plain = recompute(rows, a.conn, stats_t, intervals)
    bad = diff(plain, scopes, a.tol)
    skipped = {}
    for r in rows:
        if r.get("set_id") and not scored(r, a.conn, stats_t, intervals) and (number(r.get("t")) or 0) <= (stats_t or 1e18):
            why = "downtime (engine down)" if (not_scored(r, intervals) and "unverified-exit" not in str(r.get("reason")).lower()) else str(r.get("reason"))
            skipped[why] = skipped.get(why, 0) + 1
    ov = plain.get("overall")
    print(f"trades file {len(rows)} rows, stats at {time.strftime('%F %T', time.gmtime(stats_t or 0))} UTC; rows not scored by the engine (by reason): {skipped}")
    if ov:
        f = figures(ov)
        print(f"overall plain reading: {f['orders']} closes, PF per close {f['pf']}, PF in USDT {f['cashPf']}, wins {f['wins']}")
    for key in sorted(plain):
        if a.all or key in ("overall", "lane:pos", "lane:neg") or key.startswith("pack:"):
            f = figures(plain[key])
            e = (scopes.get(key) or {}).get("executed") or {}
            print(f"  {key:28s} plain {f['orders']:4d} orders pf {f['pf']} cash {f['cashPf']} | engine {e.get('orders')} orders pf {e.get('pf')} cash {e.get('cashPf')}")
    if bad:
        print(f"{len(bad)} DIFFERENCES (scope, field, plain reading, engine):")
        for b in bad[:40]:
            print("  ", b)
        return 1
    print("no difference between the plain reading and the engine's executed figures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
