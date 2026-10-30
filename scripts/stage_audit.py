#!/usr/bin/env python3
"""Stage audit: where the PF goes between the Base Sets and the Main and Real Sets, by category (pack), type (base / exit / trailing) and step range.

On stored 1m history (fresh test window, pre-history before it), the shipped engine configuration, shadow tapes per Set:
  1. Lanes: every Set's own tape through the engine's chain (set_engine.stage_chain): the orders opened while Base passed (the Main Set), while
     Base and Main passed (the Real Set), in a Main dip (the Real Negative Set). PF, orders and wins of each lane in the test window against the
     Set's whole tape (the Base Set, unaffected by any stage). Nothing here is selected on the orders it is measured on.
  2. Selection gap, hour by hour: the Sets valid after Base alone and after every gate, their in-sample window PF at selection against the PF of
     their orders opened in the next hour (the executed side of the same Sets).
  3. Exits and hold times: orders and result by exit reason for the Base tape and each lane; mean hold per reason.
Independence: each Set's chain reads only its own tape; the figures per group sum over their own Sets and no other.

usage: stage_audit.py --data DIR --out FILE.json [--txt FILE.txt] [--hours 24] [--lookback 1440] [--jobs 4]"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import set_engine as se  # noqa: E402
import stage_eval as sg  # noqa: E402
from position_cost import cost_as_frac  # noqa: E402

RANGES = [("2-6", 2, 6), ("7-12", 7, 12), ("13-20", 13, 20), ("21-30", 21, 30)]
KIND_LABEL = {"base": "base (TP)", "exit": "exit (no TP)", "trail": "trailing"}
G: Dict[str, Any] = {}


def range_of(step: float) -> str:
    for rid, lo, hi in RANGES:
        if lo <= step <= hi:
            return rid
    return "other"


def _job(args):
    s, bars, t0 = args
    b = sg.make_book()
    warm = min(b.warmup, max(16, len(bars) // 5))
    sig = se.pack_signals(bars, b.packs, b.ind_settings, float(t0) + 60.0, warm)
    dirs = {p: [x[0] for x in sig[p]] for p in b.packs}
    confs = {p: [float(x[1]) for x in sig[p]] for p in b.packs}
    cfg = sg.lane_cfg(b)
    acc = se.BarIndex(bars)
    out = {}
    for st in b.by_idx:
        cand = [i for i in range(warm, len(bars)) if dirs[st.pack][i] != 0 and confs[st.pack][i] >= cfg["entry_conf"]]
        trades = se.SetLane(st, s).scan(0, len(bars), bars, dirs[st.pack], confs[st.pack], True, cfg, cand, acc)
        out[st.idx] = [(t["entry_i"], t["exit_i"], t["pnl_pct"], t["reason"]) for t in trades]
    return s, out


def pf_of(g: float, l_: float):
    return round(g / l_, 3) if l_ > 0 else (None if g <= 0 else 99.0)


class Acc:
    """Orders, wins, gross profit and loss (net fractions after PositionCost) of a group of orders."""
    __slots__ = ("n", "w", "gp", "gl", "hold")

    def __init__(self):
        self.n = self.w = 0
        self.gp = self.gl = self.hold = 0.0

    def add(self, net: float, hold: float = 0.0):
        self.n += 1
        self.hold += hold
        if net > 0:
            self.w += 1
            self.gp += net
        elif net < 0:
            self.gl -= net

    def out(self) -> Dict[str, Any]:
        return {"orders": self.n, "wins": self.w, "pf": pf_of(self.gp, self.gl), "net_pct": round(100 * (self.gp - self.gl), 2),
                "avg_hold_min": round(self.hold / self.n / 60.0, 1) if self.n else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--txt", default="")
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--lookback", type=int, default=1440)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--patch", default="", help="extra overlay keys as JSON")
    a = ap.parse_args()
    t_start = time.time()
    patch = {"holdEnabled": False, "histLookbackBars": a.lookback, "histLookbackAuto": False, **(json.loads(a.patch) if a.patch else {})}
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(patch)
    names, bars, times = sg.load_bars(a.data, a.symbols)
    with ProcessPoolExecutor(max_workers=a.jobs) as pool:
        res = dict(pool.map(_job, [(s, bars[s], times[s][0]) for s in names]))
    book = sg.make_book()
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps({**patch, "stagesEnabled": False})
    book_off = sg.make_book()
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(patch)
    cf = cost_as_frac(book.cost_pct)
    # Tapes (stage_eval) over the same trades, and the reason / hold of every order per Set
    from array import array
    shadow = {}
    for s in names:
        shadow[s] = (0, {}, {}, {})
        o = {}
        for idx, tr in res[s].items():
            o[idx] = (array("i", (t[0] for t in tr)), array("i", (t[1] for t in tr)), array("d", (t[2] for t in tr)))
        shadow[s] = (0, {}, {}, o)
    tapes = sg.Tapes(book, names, times, shadow)
    rows: Dict[int, List[Tuple[float, float, float, str]]] = {}
    for st in book.by_idx:
        r = []
        for s in names:
            ts = times[s]
            for (ei, xi, pp, why) in res[s][st.idx]:
                r.append((ts[xi] + 60.0, ts[ei] + 60.0, pp, why))
        r.sort()
        rows[st.idx] = r
    start = max(times[s][0] for s in names) + book.lookback * 60
    end = min(times[s][-1] for s in names)
    all_hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
    hours = all_hours[-a.hours:]
    t0 = hours[0]
    print(f"symbols={len(names)} test hours={len(hours)} ({time.strftime('%m-%d %H:%M', time.gmtime(t0))} - {time.strftime('%m-%d %H:%M', time.gmtime(hours[-1] + 3600))} UTC) "
          f"built in {time.time() - t_start:.0f}s", flush=True)

    def group_of(st) -> Tuple[str, str, str]:
        return (st.pack, st.kind, range_of(book.step_of(st)))

    sets_in_group: Dict[Tuple, int] = defaultdict(int)
    for st in book.by_idx:
        sets_in_group[group_of(st)] += 1
    # ---- 1. lanes over the test window ----
    lanes = ("base", "main", "real", "realneg")
    lane_acc: Dict[Tuple, Dict[str, Acc]] = defaultdict(lambda: {k: Acc() for k in lanes})
    reason_acc: Dict[Tuple[str, str, str], Acc] = defaultdict(Acc)           # (lane, kind, reason)
    for st in book.by_idx:
        r = rows[st.idx]
        if not r:
            continue
        ts = [x[0] for x in r]
        ents = [x[1] for x in r]
        nets = [x[2] - cf for x in r]
        ch = se.stage_chain(ts, ents, nets, book.stage_params(st.pack))
        member = {"base": range(len(r)), "main": ch["main"], "real": ch["real"], "realneg": ch["realneg"]}
        g = group_of(st)
        for lane, idxs in member.items():
            for k in idxs:
                if ents[k] < t0:
                    continue
                hold = ts[k] - ents[k]
                lane_acc[g][lane].add(nets[k], hold)
                lane_acc[("all", "all", "all")][lane].add(nets[k], hold)
                lane_acc[(g[0], "all", "all")][lane].add(nets[k], hold)
                lane_acc[("all", g[1], "all")][lane].add(nets[k], hold)
                lane_acc[("all", "all", g[2])][lane].add(nets[k], hold)
                reason_acc[(lane, g[1], str(r[k][3]).split(":")[0])].add(nets[k], hold)
    print(f"lanes done {time.time() - t_start:.0f}s", flush=True)
    # ---- 2. selection gap, hour by hour ----
    lb = book.lookback * 60.0
    byentry: Dict[int, Tuple[List[float], List[float]]] = {}
    for st in book.by_idx:
        r = sorted(rows[st.idx], key=lambda x: x[1])
        byentry[st.idx] = ([x[1] for x in r], [x[2] - cf for x in r])
    sel: Dict[Tuple, Dict[str, Dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: {"sets_h": 0, "gp_in": 0.0, "gl_in": 0.0, "n_in": 0, "next": 0.0}))
    nxt: Dict[Tuple, Dict[str, Acc]] = defaultdict(lambda: {"base_only": Acc(), "all_gates": Acc()})
    for hi, h in enumerate(hours):
        for st in book.by_idx:
            g = group_of(st)
            ok_base = sg.gate_metrics(book_off, tapes, st, h)[0]
            ok_all = sg.gate_metrics(book, tapes, st, h)[0] if ok_base else False
            if not ok_base:
                continue
            tx, te, pp = tapes.window(st.idx, h, lb, book.gate_window)
            w = [x - cf for x in pp]
            gp, gl = sum(x for x in w if x > 0), -sum(x for x in w if x < 0)
            en, nn = byentry[st.idx]
            lo, hi_ = bisect.bisect_left(en, h), bisect.bisect_left(en, h + 3600.0)
            for tag, ok in (("base_only", True), ("all_gates", bool(ok_all))):
                if not ok:
                    continue
                for key in (g, ("all", "all", "all"), (g[0], "all", "all"), ("all", g[1], "all"), ("all", "all", g[2])):
                    e = sel[key][tag]
                    e["sets_h"] += 1
                    e["gp_in"] += gp
                    e["gl_in"] += gl
                    e["n_in"] += len(w)
                    for x in nn[lo:hi_]:
                        nxt[key][tag].add(x)
    print(f"selection gap done {time.time() - t_start:.0f}s", flush=True)

    def lane_row(key):
        la = lane_acc[key]
        return {k: la[k].out() for k in lanes}
    groups = sorted(k for k in lane_acc if k[0] != "all" and k[1] != "all" and k[2] != "all")
    result: Dict[str, Any] = {
        "meta": {"symbols": names, "hours": [int(h) for h in hours], "lookback_bars": a.lookback, "created": int(time.time()),
                 "overlay": {"setGateWindow": book.gate_window, "setGateShortN": book.base_short, "mainGateN": book.main_n, "realGateN": book.realst_n,
                             "stageFullWindow": book.stage_full, "minPf": book.min_pf, "minOrders": book.gate_min, "maxDdtH": book.max_ddt_h},
                 "sets": len(book.by_idx)},
        "totals": {"all": lane_row(("all", "all", "all"))},
        "by_pack": {k[0]: lane_row(k) for k in sorted(lane_acc) if k[1] == "all" and k[0] != "all"},
        "by_kind": {k[1]: lane_row(k) for k in sorted(lane_acc) if k[0] == "all" and k[1] != "all" and k[2] == "all"},
        "by_range": {k[2]: lane_row(k) for k in sorted(lane_acc) if k[0] == "all" and k[1] == "all" and k[2] != "all"},
        "by_group": [{"pack": k[0], "kind": k[1], "range": k[2], "sets": sets_in_group[k], **lane_row(k)} for k in groups],
        "reasons": [{"lane": k[0], "kind": k[1], "reason": k[2], **v.out()} for k, v in sorted(reason_acc.items())],
        "selection": [],
    }
    for key in sorted(sel):
        row: Dict[str, Any] = {"pack": key[0], "kind": key[1], "range": key[2], "sets": sets_in_group.get(key) or sum(v for kk, v in sets_in_group.items() if all(key[i] in ("all", kk[i]) for i in range(3)))}
        for tag in ("base_only", "all_gates"):
            e = sel[key][tag]
            row[tag] = {"valid_sets_per_hour": round(e["sets_h"] / len(hours), 1), "in_sample_pf": pf_of(e["gp_in"], e["gl_in"]), "window_orders": e["n_in"],
                        "next_hour": nxt[key][tag].out()}
        result["selection"].append(row)
    json.dump(result, open(a.out, "w"), indent=1)
    if a.txt:
        open(a.txt, "w", encoding="utf-8").write(render_text(result))
    print(render_text(result))
    print(f"done in {time.time() - t_start:.0f}s")
    return 0


def render_text(r: Dict[str, Any]) -> str:
    m = r["meta"]
    out: List[str] = []
    ov = m["overlay"]
    out.append(f"Stage audit · {len(m['symbols'])} symbols · test {time.strftime('%d %b %H:%M', time.gmtime(m['hours'][0]))} - {time.strftime('%d %b %H:%M', time.gmtime(m['hours'][-1] + 3600))} UTC · "
               f"pre-history {m['lookback_bars'] / 60:g} h · {m['sets']} Sets")
    out.append(f"config: Base long {ov['setGateWindow']} / short {ov['setGateShortN']}, Main static {ov['mainGateN']} + Auto, Real {ov['realGateN']}, min PF {ov['minPf']}, "
               f"minimum orders {ov['minOrders']} (1 = off), full windows {ov['stageFullWindow']}")

    def cell(d):
        return f"{d['pf'] if d['pf'] is not None else '–':>6} {d['orders']:>6}"
    out.append("\n1. LANES in the test window (PF after PositionCost / orders). Base = the Set's whole tape (no stage applied); Main = opened while Base passed;")
    out.append("   Real = opened while Base and Main passed; Real Neg = opened in a Main dip (own lane).")
    out.append(f"{'group':<34s}{'Base':>13s}{'Main':>13s}{'Real':>13s}{'Real Neg':>13s}   Main-Base  Real-Base")

    def line(label, d):
        mb = (d["main"]["pf"] - d["base"]["pf"]) if d["main"]["pf"] is not None and d["base"]["pf"] is not None else None
        rb = (d["real"]["pf"] - d["base"]["pf"]) if d["real"]["pf"] is not None and d["base"]["pf"] is not None else None
        return (f"{label:<34s}{cell(d['base'])} {cell(d['main'])} {cell(d['real'])} {cell(d['realneg'])}   "
                f"{'' if mb is None else format(mb, '+.2f'):>8s}  {'' if rb is None else format(rb, '+.2f'):>8s}")
    out.append(line("ALL Sets", r["totals"]["all"]))
    for k, v in r["by_pack"].items():
        out.append(line(f"category {k}", v))
    for k, v in r["by_kind"].items():
        out.append(line(f"type {KIND_LABEL.get(k, k)}", v))
    for k, v in r["by_range"].items():
        out.append(line(f"range {k}", v))
    out.append("\n   by category x type x range:")
    for g in r["by_group"]:
        if g["base"]["orders"] == 0:
            continue
        out.append(line(f"  {g['pack']} {KIND_LABEL.get(g['kind'], g['kind'])} {g['range']} ({g['sets']} Sets)", g))
    out.append("\n2. SELECTION GAP hour by hour: the Sets valid at the hour start - their in-sample window PF at selection against the PF of their orders opened in the next hour.")
    out.append(f"{'group':<34s}{'after Base alone':>40s}{'after all gates':>42s}")
    out.append(f"{'':<34s}{'valid/h':>8s}{'in-sample':>10s}{'next h PF':>10s}{'orders':>8s}{'':>4s}{'valid/h':>8s}{'in-sample':>10s}{'next h PF':>10s}{'orders':>8s}")
    for s in r["selection"]:
        if s["kind"] != "all" and s["range"] != "all" and s["base_only"]["next_hour"]["orders"] < 30:
            continue
        label = "ALL" if (s["pack"], s["kind"], s["range"]) == ("all", "all", "all") else " ".join(x for x in (s["pack"], s["kind"], s["range"]) if x != "all")
        b, a_ = s["base_only"], s["all_gates"]
        out.append(f"{label:<34s}{b['valid_sets_per_hour']:>8}{str(b['in_sample_pf']):>10s}{str(b['next_hour']['pf']):>10s}{b['next_hour']['orders']:>8}{'':>4s}"
                   f"{a_['valid_sets_per_hour']:>8}{str(a_['in_sample_pf']):>10s}{str(a_['next_hour']['pf']):>10s}{a_['next_hour']['orders']:>8}")
    out.append("\n3. EXITS AND HOLD TIMES by lane and type: orders, PF of the orders that left that way, average hold (minutes)")
    out.append(f"{'lane':<8s}{'type':<14s}{'reason':<10s}{'orders':>8s}{'PF':>8s}{'wins %':>8s}{'net %':>9s}{'hold min':>10s}")
    for x in r["reasons"]:
        if x["orders"] < 20:
            continue
        out.append(f"{x['lane']:<8s}{KIND_LABEL.get(x['kind'], x['kind']):<14s}{x['reason']:<10s}{x['orders']:>8}{str(x['pf']):>8s}{100.0 * x['wins'] / x['orders']:>8.0f}{x['net_pct']:>9}{str(x['avg_hold_min']):>10s}")
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
