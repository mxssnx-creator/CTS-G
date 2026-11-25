#!/usr/bin/env python3
"""Relate on stored executed parents (owner 2026-11-25): the engine's own RelateBook driven in time order.

For every stored parent the book decides at its entry (only closes before that instant are read) and learns at its close. A parent's volume is the receipt's
total (1 = base); "without" is every parent at 1. Reported per window: unit PF (without), volume-weighted PF (with), net, parents raised, largest multiple.
Parents come from `trade_sim.py --trades` JSON files (sim windows) and from the live trades file; stop and target fractions come from the engine's
`lane_fracs` on a SetBook built from the x01 overlay with the full grid, so eligibility (stop <= maxSlRatio x TP) is the engine's.

usage: relate_x01_study.py --dev a.json b.json --confirm c.json d.json [--live trades.jsonl] [--overlay overlay.json] [--samples 4000] [--seed 7]
Choose on development; read confirmation once. Ship rule: weighted PF above without on every development AND confirmation window, >= 20 raised on development.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import relate_engine  # noqa: E402
import set_engine  # noqa: E402
from position_cost import tp_range_of  # noqa: E402


def full_book(overlay_path: str):
    ov = json.load(open(overlay_path))
    ov.update(stratGeneral=True, tpStepsOff=[], packSidesOff=[], setSlRatios=[0.5, 0.75, 1.0, 1.25], histEnabled=True, replayWorkers=0)
    ov["tpRanges"] = {k: True for k in ("micro", "minimal", "short", "normal", "long", "wide")}
    ov["slRatiosByRange"] = {"long": [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8], "wide": [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]}
    ov["setMinStep"] = 3
    ov["tpStepMin"] = 3
    b = set_engine.SetBook()
    b.load(ov)
    return b


def parents_from_sim(path: str, book) -> List[Dict[str, Any]]:
    d = json.load(open(path))
    out = []
    for t in d["trades"]:
        st = book.sets.get(t["set"])
        if st is None or t.get("net_pct") is None:
            continue
        sl, tp = book.lane_fracs(st)
        out.append({"id": f"{os.path.basename(path)}:{len(out)}", "set": t["set"], "symbol": t["symbol"], "side": "LONG" if ":L:" in t["set"] else "SHORT",
                    "pack": t["pack"], "tf": "1m", "kind": st.kind, "range": tp_range_of(book.step_bucket(st)), "step": int(book.step_of(st)), "lane": t.get("lane") or "pos",
                    "t_in": float(t["t_in"]), "t": float(t["t_out"]), "net": float(t["net_pct"]) / 100.0, "sl": float(sl), "tp": float(tp)})
    return sorted(out, key=lambda r: r["t_in"])


def parents_from_live(path: str, book) -> List[Dict[str, Any]]:
    out = []
    for line in open(path):
        r = json.loads(line)
        if not (r.get("ours") and r.get("set_id") and r.get("sl_pct") and r.get("tp_pct")) or "downtime" in r["reason"] or "unverified" in r["reason"] or r["reason"] == "external-position-close":
            continue
        st = book.sets.get(r["set_id"])
        kind = st.kind if st is not None else ("trail" if r.get("trail_set_id") else "base")
        rng = tp_range_of(book.step_bucket(st)) if st is not None else "short"
        out.append({"id": f"live:{len(out)}", "set": r["set_id"], "symbol": r["symbol"], "side": r["side"], "pack": r["pack"], "tf": "1m", "kind": kind, "range": rng,
                    "step": int(book.step_of(st)) if st is not None else 0, "lane": r.get("lane") or "pos", "t_in": float(r["t"]) - float(r.get("hold_s") or 0), "t": float(r["t"]), "net": float(r["net_pnl_pct"]),
                    "sl": float(r["sl_pct"]), "tp": float(r["tp_pct"])})
    return sorted(out, key=lambda r: r["t_in"])


def simulate(ps: List[Dict[str, Any]], book: "relate_engine.RelateBook") -> Dict[str, Any]:
    ev: List[Tuple[float, int, int]] = []
    for i, r in enumerate(ps):
        ev.append((r["t_in"], 0, i))
        ev.append((r["t"], 1, i))
    ev.sort()
    receipts: Dict[int, Dict[str, Any]] = {}
    mult = [1.0] * len(ps)
    for _t, what, i in ev:
        r = ps[i]
        if what == 0:
            ident = {"config": r["set"], "symbol": r["symbol"], "side": r["side"], "indication": f"{r['pack']}:{r['tf']}", "strategy": r["kind"],
                     "coordination": "Main-Real", "range": r["range"], "step": r["step"], "lane": r["lane"]}
            rc = book.decide(ident, r["t_in"], r["sl"], r["tp"], qualified=True, base=1.0)
            receipts[i] = rc
            mult[i] = rc["total"]
        else:
            rc = receipts.get(i)
            if rc is not None:
                book.close(rc, r["id"], max(r["t"], rc["at"]), r["net"], accepted=True, final=True)
    ug = sum(r["net"] for r in ps if r["net"] > 0)
    ul = -sum(r["net"] for r in ps if r["net"] < 0)
    wg = sum(r["net"] * mult[i] for i, r in enumerate(ps) if r["net"] > 0)
    wl = -sum(r["net"] * mult[i] for i, r in enumerate(ps) if r["net"] < 0)
    pf = lambda g, l_: round(g / l_, 4) if l_ > 0 else (99.0 if g > 0 else None)
    return {"n": len(ps), "raised": sum(1 for m in mult if m > 1 + 1e-9), "maxMult": max(mult, default=1.0), "pfWithout": pf(ug, ul), "pfWith": pf(wg, wl),
            "netWithout": 100 * (ug - ul), "netWith": 100 * (wg - wl), "g": (ug, ul, wg, wl)}


def run(windows: List[List[Dict[str, Any]]], cfg: Dict[str, Any], carry: bool) -> Dict[str, Any]:
    per, book = [], None
    for ps in windows:
        if book is None or not carry:
            book = relate_engine.RelateBook([cfg])
        per.append(simulate(ps, book))
    g = [sum(x["g"][j] for x in per) for j in range(4)]
    pf = lambda a, b: round(a / b, 4) if b > 0 else (99.0 if a > 0 else None)
    return {"per": per, "pfWith": pf(g[2], g[3]), "pfWithout": pf(g[0], g[1]), "raised": sum(x["raised"] for x in per), "maxMult": max(x["maxMult"] for x in per),
            "netWith": 100 * (g[2] - g[3]), "netWithout": 100 * (g[0] - g[1]), "better": sum(1 for x in per if (x["pfWith"] or 0) + 1e-9 >= (x["pfWithout"] or 0)), "windows": len(per)}


def draw(rng: random.Random) -> Dict[str, Any]:
    min_trades = rng.choice([5, 12, 12, 20])
    last_n = rng.choice([12, 12, 24, 40, 60, 120, 250])
    last_n = max(last_n, min_trades)
    return {"id": "study", "enabled": True, "steps": rng.choice([2, 4, 8, 12]), "ratio": rng.choice([0.2, 0.4, 0.6, 1.0]), "mode": rng.choice(["shared", "additive"]),
            "sources": rng.choice([["configAll"], ["configAll", "config"], ["configAll", "symbol"], ["configAll", "range"], ["configAll", "strategy"], ["configAll", "direction"], ["coordination"], ["coordination", "configAll"], ["coordination", "range"], ["configAll", "range", "coordination"], ["range"], ["step"], ["step", "configAll"], ["step", "direction"], ["indication"], ["indication", "direction"], ["overall"], ["symbol"], ["direction", "step", "range"]]),
            "lastN": last_n, "minTrades": min_trades, "minPf": rng.choice([1.05, 1.1, 1.2, 1.3, 1.5]), "maxStack": rng.choice([1.5, 2.0, 3.0]),
            "maxSlRatio": rng.choice([0.5, 0.8, 1.25]), "maxLossRun": rng.choice([0, 2, 3, 4, 6]), "trigger": rng.choice(["win", "every"])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", nargs="+", required=True)
    ap.add_argument("--confirm", nargs="*", default=[])
    ap.add_argument("--live")
    ap.add_argument("--overlay", default="/opt/grok-x01-pulse/overlay-bingx-x01.json")
    ap.add_argument("--samples", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--min-raised", type=int, default=20)
    ap.add_argument("--ranges", help="comma list of TP ranges kept in every window, e.g. micro,minimal")
    ap.add_argument("--packs", help="comma list of packs kept, e.g. shortrange,reversion")
    ap.add_argument("--out")
    a = ap.parse_args()
    book = full_book(a.overlay)
    dev = [parents_from_sim(p, book) for p in a.dev]
    conf = [parents_from_sim(p, book) for p in a.confirm]
    live = parents_from_live(a.live, book) if a.live else []
    keep = lambda ps: [r for r in ps if (not a.ranges or r["range"] in a.ranges.split(",")) and (not a.packs or r["pack"] in a.packs.split(","))]
    dev, conf, live = [keep(x) for x in dev], [keep(x) for x in conf], keep(live)
    print("development windows (parents):", [len(x) for x in dev], "confirmation:", [len(x) for x in conf], "live:", len(live))
    rng = random.Random(a.seed)
    rows = []
    for _ in range(a.samples):
        cfg = draw(rng)
        try:
            relate_engine.clean_configs([cfg])
            r = run(dev, cfg, True)
        except ValueError:
            continue
        rows.append((cfg, r))
    ok = [(c, r) for c, r in rows if r["raised"] >= a.min_raised and r["better"] == r["windows"] and r["netWith"] >= r["netWithout"]]
    print(f"{len(rows)} settings; raising >= {a.min_raised} on development: {sum(1 for _, r in rows if r['raised'] >= a.min_raised)}; weighted PF above without on every development window and net not lower: {len(ok)}")
    ok.sort(key=lambda x: (-x[1]["raised"] if False else -(x[1]["pfWith"] or 0)))
    held = []
    for c, r in ok[:15]:
        cc = [run([w], c, False) for w in conf]
        lv = run([live], c, False) if live else None
        passes = all(x["better"] == x["windows"] and x["netWith"] >= x["netWithout"] for x in cc) if cc else False
        held.append(passes)
        print(f"dev PF {r['pfWith']}/{r['pfWithout']} net {r['netWith']:.1f}/{r['netWithout']:.1f} raised {r['raised']} max x{r['maxMult']:.2f} | confirm "
              + " ".join(f"{x['pfWith']}/{x['pfWithout']} r{x['raised']}" for x in cc) + (f" | live {lv['pfWith']}/{lv['pfWithout']} r{lv['raised']}" if lv else "")
              + f" {'HOLDS' if passes else 'fails'} | {json.dumps({k: c[k] for k in ('steps', 'ratio', 'mode', 'sources', 'lastN', 'minTrades', 'minPf', 'maxStack', 'maxSlRatio', 'maxLossRun', 'trigger')})}")
    print("held on confirmation:", sum(held), "of", len(held))
    if a.out:
        json.dump([{"cfg": c, "dev": r} for c, r in ok[:50]], open(a.out, "w"), default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
