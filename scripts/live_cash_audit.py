#!/usr/bin/env python3
"""Read-only: the venue's realized PnL next to what the engine recorded, per symbol, over a window.

The venue's income feed (REALIZED_PNL, TRADING_FEE) is the cash truth of the account. The engine's trades file holds the closes it scored. Their difference is cash the
engine's PF never saw: closes it could not verify (reason "...-unverified-exit"), closes made while it was down ("...-downtime"), positions opened or closed by anything
else on the account (no client id), and fills it missed. The report shows both PFs (per close and in USDT) so the effect on the displayed PF is visible.

usage: live_cash_audit.py [--conn bingx-x01] [--hours 6] [--cred /var/lib/cts-g/cred-bingx-x01.json] [--trades PATH]
Only GET /openApi/swap/v2/user/income is called. Nothing is written, no order endpoint is touched. The key is read from --cred (mode 600), never printed."""
from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

HOSTS = {"bingx-x01": "https://open-api.bingx.com", "bingx-x02": "https://open-api-vst.bingx.com"}


def income(host: str, key: str, secret: str, start_ms: int):
    rows, end = [], None
    for _ in range(10):
        p = {"startTime": start_ms, "limit": 1000, "timestamp": int(time.time() * 1000)}
        if end:
            p["endTime"] = end
        q = urllib.parse.urlencode(p)
        sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        req = urllib.request.Request(f"{host}/openApi/swap/v2/user/income?{q}&signature={sig}", headers={"X-BX-APIKEY": key})
        data = (json.load(urllib.request.urlopen(req, timeout=20)).get("data") or [])
        rows += data
        if len(data) < 1000:
            break
        end = min(int(r["time"]) for r in data) - 1
    return rows


def pf(g: float, l_: float):
    return round(g / l_, 3) if l_ > 0 else (None if g <= 0 else 99.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conn", default="bingx-x01")
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--cred", default="/var/lib/cts-g/cred-bingx-x01.json")
    ap.add_argument("--trades")
    a = ap.parse_args()
    cred = json.load(open(a.cred))
    now = time.time()
    start = now - a.hours * 3600
    inc = income(HOSTS.get(a.conn, cred.get("base_url")), cred["api_key"], cred["api_secret"], int(start * 1000))
    venue_pnl, venue_fee = collections.Counter(), collections.Counter()
    gains = losses = 0.0
    for r in inc:
        v = float(r["income"])
        if r["incomeType"] == "REALIZED_PNL":
            venue_pnl[r["symbol"]] += v
            gains += max(v, 0.0)
            losses += max(-v, 0.0)
        elif r["incomeType"] == "TRADING_FEE":
            venue_fee[r["symbol"]] += v
    trades = a.trades or f"/opt/grok-x01-pulse/trades-{a.conn}.jsonl"
    rec_net, rec_n, rec_unscored = collections.Counter(), collections.Counter(), collections.Counter()
    sg = sl = 0.0
    for line in open(trades, encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not (start < float(r.get("t") or 0) <= now) or r.get("ours") is False:
            continue
        reason = str(r.get("reason") or "")
        pnl = float(r.get("pnl") or 0.0)
        rec_n[r["symbol"]] += 1
        if "unverified-exit" in reason or "-downtime" in reason:
            rec_unscored[r["symbol"]] += 1
            continue
        rec_net[r["symbol"]] += pnl
        sg += max(pnl, 0.0)
        sl += max(-pnl, 0.0)
    v_net, r_net = sum(venue_pnl.values()), sum(rec_net.values())
    print(f"window {a.hours:g} h on {a.conn}: venue realized PnL {v_net:+.4f} USDT (fees {sum(venue_fee.values()):+.4f}), engine-scored closes {r_net:+.4f} USDT")
    print(f"  unrecorded or unscored cash: {v_net - r_net:+.4f} USDT ({100 * (v_net - r_net) / abs(v_net):.0f} % of the venue's result)" if v_net else "  no venue PnL in the window")
    print(f"  PF in USDT: venue {pf(gains, losses)}  engine-scored {pf(sg, sl)}")
    print("  symbols with a gap (venue - engine-scored, USDT; engine closes, of them unscored):")
    for s in sorted(set(venue_pnl) | set(rec_net), key=lambda s: -abs(venue_pnl[s] - rec_net[s]))[:12]:
        d = venue_pnl[s] - rec_net[s]
        if abs(d) >= 0.0005:
            print(f"    {s:16s} {d:+.4f}  venue {venue_pnl[s]:+.4f}  engine {rec_net[s]:+.4f}  closes {rec_n[s]} (unscored {rec_unscored[s]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
