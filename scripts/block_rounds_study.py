#!/usr/bin/env python3
"""Block rounds (owner definition, 2026-11-11): Block as volume control over rounds of N positions.

  A group's positions form consecutive blocks of N (by the order they open). When a block is complete its result is the net of its N positions.
  - A losing block raises the NEXT blocks: every position of them opens at 1 + ratio x level of the original base volume (level 1 after the first loss).
  - The raise is kept while the group is losing: until the volume-weighted net since the losing block that started it is back at or above zero.
  - Step: while still losing, every further losing block raises the level by one more (up to maxLevel), each time by the ratio from the base volume.
  - Pause: after a positive block at base volume, and after a recovered raise, the next `pause` blocks run at base volume and start nothing.
  Every group runs its own procedure; a group is a relation key (overall, direction, symbol, indication, type, Set, pack + TP step).

Sizing changes no entry, so one stored execution per window (block_coordination_study.py parents) carries every arm. A position's volume is
decided at its open from the blocks of its group completed (all N closed) before that instant: nothing closed later is read. Reported: the unit PF
(every position at 1: "without") next to the volume-weighted PF and net ("with"), the largest multiple, and the net per unit of volume (an edge,
not just more size). Chosen on development windows; --confirm reads windows no arm was chosen on. The state is carried from window to window in
time order, as a live book would carry it.

usage: block_rounds_study.py --parents a.pkl b.pkl ... [--confirm c.pkl ...] [--top 20] [--out FILE]
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import pickle
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "pulse"))
import block_rounds  # noqa: E402  (the engine's own rule: the study drives the same book)

RELATIONS = block_rounds.RELATIONS


def pf_of(g: float, l_: float) -> Optional[float]:
    return round(g / l_, 4) if l_ > 0 else (99.0 if g > 0 else None)


def rounds_cfg(cfg: Dict[str, Any]) -> block_rounds.RoundsCfg:
    """A study setting as the engine's configuration: {"relation", "n", "ratio", "step", "max_level", "pause", "cap"} (one count) or
    {"relations": [...], "counts": {n: {...}}, "cap"} (several procedures at once)."""
    if "counts" in cfg:
        return block_rounds.config_of({"enabled": True, "relations": list(cfg["relations"]), "maxMult": cfg["cap"], "counts": cfg["counts"]})
    return block_rounds.config_of({"enabled": True, "relations": [cfg["relation"]], "maxMult": cfg["cap"],
                                   "counts": {str(cfg["n"]): {"on": True, "ratio": cfg["ratio"], "step": cfg["step"], "maxLevel": cfg["max_level"], "pause": cfg["pause"]}}})


def simulate(window: Dict[str, Any], cfg: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    ps = window["parents"]
    events: List[Tuple[float, int, int]] = []
    for i, r in enumerate(ps):
        # Entries at a clock cannot learn an OHLC close at that same clock.
        events.append((r["t_in"], 0, i))
        events.append((r["t"], 1, i))
    events.sort()
    book = state.get("book")
    if book is None:
        book = state["book"] = block_rounds.RoundsBook(cfg=rounds_cfg(cfg))
    name = window.get("name", "")
    mult: Dict[int, float] = {}
    for _t, what, i in events:
        r = ps[i]
        if what == 0:
            keys = block_rounds.keys_of(r["symbol"], r["side"], r["pack"], r["kind"], r["step"], r["lane"], r["set"])
            mult[i] = book.ask(keys)[0]
            book.opened(f"{name}:{i}", keys, mult[i])
        else:
            book.closed(f"{name}:{i}", r["net"])
    book.forget(())                                      # a window's last open positions never close: their blocks stay without them
    ug = sum(r["net"] for r in ps if r["net"] > 0)
    ul = -sum(r["net"] for r in ps if r["net"] < 0)
    wg = sum(r["net"] * mult[i] for i, r in enumerate(ps) if r["net"] > 0)
    wl = -sum(r["net"] * mult[i] for i, r in enumerate(ps) if r["net"] < 0)
    vol = sum(mult.values())
    eq = peak = dd = 0.0
    for i, r in sorted(enumerate(ps), key=lambda x: x[1]["t"]):
        eq += r["net"] * mult[i]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": len(ps), "raised": sum(1 for m in mult.values() if m > 1.0 + 1e-9), "maxMult": max(mult.values(), default=1.0), "volume": vol,
            "g": (ug, ul, wg, wl), "pfWithout": pf_of(ug, ul), "pfWith": pf_of(wg, wl), "netWithout": 100 * (ug - ul), "netWith": 100 * (wg - wl), "ddWith": 100 * dd}


def run(windows: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    state: Dict[str, Any] = {}
    per = [simulate(w, cfg, state) for w in windows]
    g = [sum(x["g"][j] for x in per) for j in range(4)]
    n, vol = sum(x["n"] for x in per), sum(x["volume"] for x in per)
    return {"per": per, "pfWithout": pf_of(g[0], g[1]), "pfWith": pf_of(g[2], g[3]), "netWithout": 100 * (g[0] - g[1]), "netWith": 100 * (g[2] - g[3]),
            "raised": sum(x["raised"] for x in per), "maxMult": max(x["maxMult"] for x in per), "volume": vol,
            "perVolWithout": 100 * (g[0] - g[1]) / n if n else 0.0, "perVolWith": 100 * (g[2] - g[3]) / vol if vol else 0.0,
            "betterOn": sum(1 for x in per if (x["pfWith"] or 0) + 1e-9 >= (x["pfWithout"] or 0)), "windows": len(per)}


def label(c: Dict[str, Any]) -> str:
    return f"{c['relation']:16s} N{c['n']} ratio {c['ratio']:g} {'step to ' + str(c['max_level']) if c['step'] else 'one level '} pause {c['pause']}"


def show(c, r) -> None:
    cells = "  ".join(f"{(x['pfWith'] or 0):.3f}/{(x['pfWithout'] or 0):.3f}" for x in r["per"])
    print(f"  {label(c)} | {cells} | PF {r['pfWith']}/{r['pfWithout']} net {r['netWith']:+.1f}/{r['netWithout']:+.1f} raised {r['raised']} max x{r['maxMult']:.2f} "
          f"net per volume {r['perVolWith']:+.4f}/{r['perVolWithout']:+.4f} % better on {r['betterOn']}/{r['windows']}")


def load(paths):
    out = []
    for p in paths:
        with open(p, "rb") as f:
            d = pickle.load(f)
        d["name"] = os.path.basename(p).rsplit(".", 1)[0]
        out.append(d)
    return sorted(out, key=lambda w: w["meta"]["from"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parents", nargs="+", required=True)
    ap.add_argument("--confirm", nargs="*", default=[])
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    dev, conf = load(a.parents), load(a.confirm)
    grid = [{"relation": rel, "n": n, "ratio": ratio, "step": step, "max_level": ml, "pause": pause, "cap": 5.0}
            for rel, n, ratio, (step, ml), pause in itertools.product(RELATIONS, (1, 2, 3, 4, 5, 6), (0.25, 0.5, 1.0, 2.0), ((False, 1), (True, 3), (True, 6)), (0, 1, 2, 3))]
    print(f"{len(grid)} block-round settings on {len(dev)} development windows in time order: " + ", ".join(f"{w['name']} ({len(w['parents'])})" for w in dev))
    rows = [(c, run(dev, c)) for c in grid]
    raised = [(c, r) for c, r in rows if r["raised"]]
    ok = [(c, r) for c, r in raised if r["betterOn"] == r["windows"] and r["netWith"] >= r["netWithout"]]
    print(f"settings that raise at least one position: {len(raised)}; weighted PF at or above the unit PF on every window AND the weighted net not below the unit net: {len(ok)}; "
          f"pooled weighted PF above unit: {sum(1 for c, r in raised if (r['pfWith'] or 0) > (r['pfWithout'] or 0))}; net per volume better: {sum(1 for c, r in raised if r['perVolWith'] > r['perVolWithout'])}")
    print("columns per window: weighted PF / unit PF")
    finalists = sorted(ok, key=lambda x: -(x[1]["netWith"] - x[1]["netWithout"]))[:a.top]
    if not finalists:
        print("no setting passes; the best by pooled PF gain regardless:")
        finalists_show = sorted(raised, key=lambda x: -((x[1]["pfWith"] or 0) - (x[1]["pfWithout"] or 0)))[:a.top]
        for c, r in finalists_show:
            show(c, r)
        finalists = finalists_show
    else:
        for c, r in finalists:
            show(c, r)
    held = 0
    if conf:
        print(f"\nconfirmation, a fresh book, on {len(conf)} windows no setting was chosen on: " + ", ".join(f"{w['name']} ({len(w['parents'])})" for w in conf))
        for c, _r in finalists:
            r = run(conf, c)
            good = r["betterOn"] == r["windows"] and r["netWith"] >= r["netWithout"] and r["raised"] > 0
            held += good
            print(("  HOLDS" if good else "  fails"), end="")
            show(c, r)
        print(f"\n{held} of {len(finalists)} hold on the confirmation windows")
    if a.out:
        json.dump({"settings": len(grid), "raising": len(raised), "passDevelopment": len(ok), "held": held,
                   "finalists": [{"cfg": c, **{k: v for k, v in r.items() if k != "per"}} for c, r in finalists]}, open(a.out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
