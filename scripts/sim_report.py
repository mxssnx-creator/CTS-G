#!/usr/bin/env python3
"""24-hour trade simulation on stored 1m history with the engine's own Set machinery, and a detailed HTML report.

What runs: the shipped x01 overlay with the Short Range pack (Low-Range signals) next to the Indications and General
packs, all limited to TP steps 2-6 (one step = one PositionCost), SL ratios 0.5..3.5 step 0.25, trailing Sets from the
engine's grid. The Base validation is the engine's own (`SetBook._score_one` / `gate_reason`): every Set keeps a shadow
lane that is always on and forms its validation tape, and an executed lane opens positions only in the hours in which
its Set is validated, scored from trades that closed BEFORE the hour (no look-ahead). Open positions run to their own
exit; positions still open at the window end are marked to the last close and listed as such.

The headline numbers are post-Base: the PF of the executed orders (which only validated Sets can open), the share of
Sets validated, and the order count behind them. Unfiltered PF (the shadow tapes of every Set) is shown for
reference only: it is under 1 by design.

usage: sim_report.py --data DIR --out report.html [--json trades.json] [--hours 24] [--symbols 20]
  DIR holds <SYMBOL>.json files of rows [t_ms, o, h, l, c, v].
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import set_engine as se  # noqa: E402
from set_engine import BAR_S, SetBook, SetLane  # noqa: E402

PULSE = os.path.join(HERE, "..", "server", "pulse")
BURN = 1920          # bars of shadow history before the window (the engine's default lookback)


def load_bars(data_dir: str, n_symbols: int) -> Tuple[List[str], Dict[str, List[List[float]]], Dict[str, List[int]]]:
    syms: List[str] = []
    bars: Dict[str, List[List[float]]] = {}
    times: Dict[str, List[int]] = {}
    for f in sorted(glob.glob(os.path.join(data_dir, "*.json"))):
        name = os.path.basename(f)
        if name.startswith("_"):
            continue
        rows = json.load(open(f))
        sym = name[:-5]
        syms.append(sym)
        bars[sym] = [[r[1], r[2], r[3], r[4], r[5]] for r in rows]
        times[sym] = [int(r[0]) for r in rows]
    syms = syms[:n_symbols]
    return syms, bars, times


def build_book(cost: float, sl_min_steps: int = 1, trail_min_steps: int = 2) -> SetBook:
    ov = json.load(open(os.path.join(PULSE, "overlay-bingx-x01.json")))
    ov.update({"stratShortRange": True, "stratIndications": True, "stratGeneral": True,
               "setMinStep": 2, "setStepMax": 6, "shortRangeStepMin": 2, "shortRangeStepMax": 6,
               "setStepAdapt": False, "positionCostPct": cost, "slMinSteps": sl_min_steps,
               "trailMinSteps": trail_min_steps})
    b = SetBook()
    b.load(ov)
    se.HIST_CAP = 10 ** 9
    return b


def pf_of(vals: List[float]) -> float:
    gp = sum(v for v in vals if v > 0)
    gl = -sum(v for v in vals if v < 0)
    return round(gp / gl, 4) if gl > 0 else (99.0 if gp > 0 else 0.0)


def run(a: argparse.Namespace) -> Dict[str, Any]:
    t_start = time.time()
    syms, bars_all, times_all = load_bars(a.data, a.symbols)
    w_bars = a.hours * 60
    L = BURN + w_bars
    sb = build_book(a.cost, a.sl_min_steps, a.trail_min_steps)
    sets = [st for st in sb.by_idx]
    off = int(a.offset_h * 60)       # the window ends `offset_h` hours before the end of the data
    bars = {s: bars_all[s][len(bars_all[s]) - off - L : len(bars_all[s]) - off] for s in syms}
    tms = {s: times_all[s][len(times_all[s]) - off - L : len(times_all[s]) - off] for s in syms}
    base_ts = tms[syms[0]][0] / 1000.0
    cfg = {"entry_conf": sb.entry_conf, "time_bars": max(8, min(sb.hist_time_bars, 360)),
           "scratch_bars": max(8, int(sb.scratch_s / BAR_S)), "scratch_min": sb.scratch_min,
           "cooldown": sb.cooldown_bars, "cost_pct": sb.cost_pct, "honor_tp": bool(sb.hist_honor_tp),
           "sl_floor_steps": sb.sl_min_steps}
    warm = min(sb.warmup, 30)
    sig: Dict[str, Dict[str, Tuple[List[int], List[float]]]] = {}
    for s in syms:
        raw = se.pack_signals(bars[s], sb.packs, sb.ind_settings, base_ts, warm)
        sig[s] = {p: ([x[0] for x in raw[p]], [x[1] for x in raw[p]]) for p in sb.packs}
    cand: Dict[Tuple[str, str], List[int]] = {}
    for s in syms:
        for p in sb.packs:
            dd, cc = sig[s][p]
            cand[(s, p)] = [i for i in range(warm, L) if dd[i] != 0 and cc[i] >= cfg["entry_conf"]]
    shadow = {(st.id, s): SetLane(st, s) for st in sets for s in syms}
    execd = {(st.id, s): SetLane(st, s) for st in sets for s in syms}
    tape: Dict[str, List[Dict[str, Any]]] = {st.id: [] for st in sets}

    def row(rec: Dict[str, Any], sid: str, s: str) -> Dict[str, Any]:
        return {"t": float(rec["exit_i"]) * BAR_S, "symbol": s, "side": rec["side"], "pnl": rec["pnl"],
                "pnl_pct": rec["pnl_pct"], "hold_s": rec["hold_s"], "reason": rec["reason"], "set_id": sid,
                "exit_i": rec["exit_i"]}

    for st in sets:                                    # burn-in: the validation tape before the window
        for s in syms:
            dd, cc = sig[s][st.pack]
            for rec in shadow[(st.id, s)].scan(warm, BURN, bars[s], dd, cc, True, cfg, cand[(s, st.pack)]):
                tape[st.id].append(row(rec, st.id, s))
    set_nets: Dict[str, List[float]] = {st.id: [] for st in sets}      # Set-level orders: every validated Set trades
    picked: List[Dict[str, Any]] = []                                  # one Set per (pack, step, symbol): the engine's pick
    pick_lanes: Dict[Tuple[str, str], SetLane] = {}
    groups = sorted({(st.pack, st.step if st.kind == "base" else 0) for st in sets})
    elig_by_hour: List[Dict[str, bool]] = []
    for h in range(a.hours):
        t_h = BURN + h * 60
        elig: Dict[str, bool] = {}
        for st in sets:
            tp = [r for r in tape[st.id] if t_h - sb.lookback <= r["exit_i"] < t_h]
            tp.sort(key=lambda r: r["exit_i"])
            st.hist = tp[-sb.gate_keep():]
            st.n = len(tp)
            sb._score_one(st, now=t_h * BAR_S)
            elig[st.id] = bool(sb.is_eligible(st))
            tape[st.id] = tp
        elig_by_hour.append(elig)
        # the pick: in each (pack, step) group the best valid Set of the hour, ranked as the engine ranks (PF, last-25
        # R, shorter drawdown time, more orders). One position per (group, symbol): a symbol is entered only when no
        # other Set of the group still holds one there.
        chosen: Dict[Tuple[str, int], str] = {}
        for pack, step in groups:
            cands = [st for st in sets if st.pack == pack and (st.step == step if step else st.kind == "trail")
                     and (st.kind == "base" if step else True) and elig[st.id]]
            if cands:
                cands.sort(key=lambda x: (x.gate_pf, x.last25_avg_r, -x.max_dd_s, x.n), reverse=True)
                chosen[(pack, step)] = cands[0].id
        for (pack, step), sid in chosen.items():
            st = sb.sets[sid]
            for s in syms:
                lane = pick_lanes.get((sid, s))
                if lane is None:
                    lane = pick_lanes[(sid, s)] = SetLane(st, s)
                others_open = any(l.open is not None for (osid, os_), l in pick_lanes.items()
                                  if os_ == s and osid != sid and sb.sets[osid].pack == pack
                                  and (sb.sets[osid].step if sb.sets[osid].kind == "base" else 0) == step)
                dd, cc = sig[s][pack]
                for rec in lane.scan(t_h, t_h + 60, bars[s], dd, cc, not others_open, cfg, cand[(s, pack)]):
                    picked.append(_trade(rec, st, s, h, bars[s], tms[s], cfg, False))
        for (sid, s), lane in pick_lanes.items():      # a Set that is no longer chosen still manages its open position
            if chosen.get((sb.sets[sid].pack, sb.sets[sid].step if sb.sets[sid].kind == "base" else 0)) == sid:
                continue
            dd, cc = sig[s][sb.sets[sid].pack]
            for rec in lane.scan(t_h, t_h + 60, bars[s], dd, cc, False, cfg, cand[(s, sb.sets[sid].pack)]):
                picked.append(_trade(rec, sb.sets[sid], s, h, bars[s], tms[s], cfg, False))
        for st in sets:
            for s in syms:
                dd, cc = sig[s][st.pack]
                cd = cand[(s, st.pack)]
                for rec in shadow[(st.id, s)].scan(t_h, t_h + 60, bars[s], dd, cc, True, cfg, cd):
                    tape[st.id].append(row(rec, st.id, s))
                for rec in execd[(st.id, s)].scan(t_h, t_h + 60, bars[s], dd, cc, bool(elig[st.id]), cfg, cd):
                    set_nets[st.id].append((float(rec["pnl_pct"]) * 100.0) - cfg["cost_pct"])
    for (sid, s), lane in pick_lanes.items():   # still open at the window end: marked to the last close
        o = lane.open
        if o is None:
            continue
        st = sb.sets[sid]
        last = bars[s][L - 1][3]
        sgn = 1.0 if o["side"] > 0 else -1.0
        gross = sgn * (last - o["entry"]) / o["entry"]
        rec = {"side": "LONG" if o["side"] > 0 else "SHORT", "entry_i": int(o["i"]), "exit_i": L - 1,
               "entry_px": o["entry"], "exit_px": last, "reason": "open-at-end", "pnl_pct": gross,
               "pnl": gross - sb.cost_pct / 100.0, "hold_s": (L - 1 - int(o["i"])) * BAR_S}
        picked.append(_trade(rec, st, s, a.hours - 1, bars[s], tms[s], cfg, True))
    trades = picked
    # per-Set unfiltered (shadow) PF over the window, for reference
    sets_info = []
    for st in sets:
        win = [r["pnl"] for r in tape[st.id] if r["exit_i"] >= BURN]
        ex = [v / 100.0 for v in set_nets[st.id]]
        sets_info.append({"id": st.id, "pack": st.pack, "kind": st.kind, "step": st.step, "sl_ratio": st.sl_ratio,
                          "trail": st.trail_key, "validated_hours": sum(1 for e in elig_by_hour if e[st.id]),
                          "shadow_n": len(win), "shadow_pf": pf_of(win), "orders": len(ex), "exec_pf": pf_of(ex),
                          "exec_gp": round(sum(v for v in ex if v > 0), 6), "exec_gl": round(-sum(v for v in ex if v < 0), 6)})
    return {
        "meta": {"symbols": syms, "hours": a.hours, "cost_pct": sb.cost_pct, "min_pf": sb.min_pf, "gate_window": sb.gate_window,
                 "gate_min": sb.gate_min, "max_ddt_h": sb.max_dd_s / 3600.0, "packs": sb.packs,
                 "window_start": tms[syms[0]][BURN], "window_end": tms[syms[0]][L - 1],
                 "sl_floor_steps": sb.sl_min_steps, "runtime_s": round(time.time() - t_start, 1),
                 "sets": len(sets)},
        "trades": trades,
        "sets": sets_info,
        "validated": [{"hour": h, "by_set": e} for h, e in enumerate(elig_by_hour)],
    }


def _trade(rec: Dict[str, Any], st: Any, s: str, h: int, bars: List[List[float]], t_ms: List[int],
           cfg: Dict[str, Any], open_end: bool) -> Dict[str, Any]:
    cost = cfg["cost_pct"]
    gross = float(rec["pnl_pct"]) * 100.0
    return {"symbol": s, "pack": st.pack, "kind": st.kind, "step": int(st.step), "sl_ratio": st.sl_ratio,
            "trail": st.trail_key, "set_id": st.id, "side": rec["side"], "hour": h,
            "t_in": t_ms[int(rec["entry_i"])], "t_out": t_ms[min(int(rec["exit_i"]), len(t_ms) - 1)],
            "entry": float(rec["entry_px"]), "exit": float(rec["exit_px"]), "reason": rec["reason"],
            "hold_s": float(rec["hold_s"]), "gross": round(gross, 4), "net": round(gross - cost, 4),
            "open_end": open_end}


def summarize(res: Dict[str, Any]) -> Dict[str, Any]:
    """Per (pack, step). Set level: every validated Set trades (validated share, orders, pooled PF). Picked: one Set per
    (pack, step, symbol), the best valid one of the hour as the engine ranks them, which is what live trading does."""
    trades = res["trades"]
    sets = res["sets"]
    hours = res["meta"]["hours"]
    out: Dict[str, Any] = {"rows": []}
    for pack in res["meta"]["packs"]:
        for step in [2, 3, 4, 5, 6, 0]:
            ss = [x for x in sets if x["pack"] == pack and x["step"] == step and (x["kind"] == "base" if step else x["kind"] == "trail")]
            if not ss:
                continue
            tr = [t for t in trades if t["pack"] == pack and t["step"] == step and (t["kind"] == "base" if step else t["kind"] == "trail")]
            nets = [t["net"] for t in tr]
            gp, gl = sum(x["exec_gp"] for x in ss), sum(x["exec_gl"] for x in ss)
            set_orders = sum(x["orders"] for x in ss)
            val_hours = sum(x["validated_hours"] for x in ss)
            ever = sum(1 for x in ss if x["validated_hours"] > 0)
            out["rows"].append({
                "pack": pack, "step": step if step else "trail", "sets": len(ss),
                "validated_pct_hours": round(100.0 * val_hours / (len(ss) * hours), 2),
                "validated_pct_ever": round(100.0 * ever / len(ss), 2),
                "set_orders": set_orders, "set_pf": round(gp / gl, 4) if gl > 0 else (99.0 if gp > 0 else 0.0),
                "orders": len(tr), "pf": pf_of(nets), "net_pct": round(sum(nets), 3),
                "avg_net_pct": round(sum(nets) / len(nets), 4) if nets else 0.0,
                "win_rate": round(100.0 * sum(1 for v in nets if v > 0) / len(nets), 1) if nets else 0.0,
                "avg_hold_min": round(sum(t["hold_s"] for t in tr) / len(tr) / 60.0, 1) if tr else 0.0,
            })
    nets_all = [t["net"] for t in trades]
    out["overall"] = {"orders": len(trades), "pf": pf_of(nets_all), "net_pct": round(sum(nets_all), 3),
                      "set_orders": sum(r["set_orders"] for r in out["rows"])}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="", help="HTML report path (omit for JSON only)")
    ap.add_argument("--json", default="")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--cost", type=float, default=0.12)
    ap.add_argument("--sl-min-steps", type=int, default=1)
    ap.add_argument("--trail-min-steps", type=int, default=2)
    ap.add_argument("--offset-h", type=float, default=0.0)
    a = ap.parse_args()
    res = run(a)
    res["summary"] = summarize(res)
    if a.json:
        json.dump(res, open(a.json, "w"))
    if a.out:
        import sim_report_html as H
        open(a.out, "w", encoding="utf-8").write(H.render(res))
    ov = res["summary"]["overall"]
    print(f"{res['meta']['sets']} Sets, {len(res['meta']['symbols'])} symbols, {a.hours} h: {ov['orders']} orders, PF {ov['pf']}, "
          f"net {ov['net_pct']}%  ({res['meta']['runtime_s']} s) -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
