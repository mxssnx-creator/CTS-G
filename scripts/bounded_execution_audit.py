#!/usr/bin/env python3
"""One-symbol-slot execution audit on <=3 days; no credentials or exchange calls.

Uses completed-bar signals, the production SetLane exits, the production stage
chain and Coordinator, separate executed windows, and the Set Block book. Prices
between candles, venue slippage and order latency are not modeled. Open positions
at the boundary remain open and never enter PF. Hold is fixed for this audit;
automatic hold selection is covered by minimal_coordination_snapshot.py.
"""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import resource
import sys
import tempfile
import time

import stage_eval as sg
from coord_engine import Coordinator
from set_block import SetBlockBook
from pulse_trader import Position


def measure(values):
    gp, gl = sum(x for x in values if x > 0), -sum(x for x in values if x < 0)
    return {"n": len(values), "pf": round(sg.se.pf_of(gp, gl), 4), "net": sum(values)}


def walk(names, bars, times, shadow, overlay, axis_on, block_on):
    from block_engine import BlockBook
    book = sg.make_book()
    tapes = sg.Tapes(book, names, times, shadow)
    cfg = sg.lane_cfg(book)
    co = Coordinator()
    co.load({}, {**overlay, **{f"axis{x}Enabled": axis_on for x in ("Last", "Prev", "Pause", "Cont")}})
    accs = {s: sg.se.BarIndex(bars[s]) for s in names}
    indexes = {s: {t + 60: i for i, t in enumerate(times[s])} for s in names}
    start = max(t[0] for t in times.values()) + book.lookback * 60
    end = min(t[-1] for t in times.values()) + 60
    active, cooldown, done, selected = {}, {}, [], []
    pools, next_refresh = {}, 0
    cf = book.cost_pct / 100
    with tempfile.TemporaryDirectory() as tmp:
        block = BlockBook(os.path.join(tmp, "block.json"), {"blockMaxStack": 6, "blockMinSamples": 8,
                                                          "blockWindow": 25, "defaultMinPF": book.min_pf})
        addons = SetBlockBook(os.path.join(tmp, "sets.json"))
        for now in range(int(start), int(end) + 1, 60):
            for sym, item in list(active.items()):
                tr, pos = item["trade"], item["position"]
                if tr is None or item["exit_t"] > now:
                    continue
                st = book.sets[pos.set_id]
                actual = {"t": item["exit_t"], "pnl_pct": tr["pnl_pct"], "lane": pos.lane}
                st.live.append(actual)
                st.live = st.live[-400:]
                addons.close(pos, tr["exit_px"], item["exit_t"], block, book.cost_pct)
                add_net = sum(f["qty"] * (tr["exit_px"] - f["price"]) * (1 if pos.side == "LONG" else -1)
                              - f["qty"] * f["price"] * cf for f in pos.block_fills) / pos.base_entry
                done.append({"set": st.id, "pack": st.pack, "type": st.kind, "step": book.step_of(st), "lane": pos.lane,
                             "symbol": sym, "entry_t": pos.opened_at, "exit_t": item["exit_t"], "reason": tr["reason"],
                             "net": tr["pnl"], "block_net": add_net, "add_fills": len(pos.block_fills)})
                cooldown[sym] = item["exit_t"] + book.cooldown_bars * 60
                del active[sym]
            if now >= next_refresh:
                pools = {}
                for st in book.by_idx:
                    lane, rank, n, pf = sg.gate_metrics(book, tapes, st, now)
                    if lane:
                        pools.setdefault(st.pack, []).append((lane if isinstance(lane, str) else "pos", st, rank))
                for pack in pools:
                    pools[pack].sort(key=lambda x: (x[0] == "pos", x[1].kind != "trail", x[2]), reverse=True)
                selected.append({"t": now, "valid": sum(map(len, pools.values())), "total": len(book.by_idx)})
                next_refresh = now + 120
            def allows(st, lane):
                if not book.execution_stats(st, lane, now)["allow"]:
                    return False
                if not axis_on:
                    return True
                ch, nets = sg.chain_at(book, tapes, st, now)
                rows = [{"pnl_pct": nets[k] + cf} for k in ch["realneg" if lane == "neg" else "real"]][-200:]
                judged = co.judge(rows, co.net_streak(rows))
                occupied = sum(x["position"].set_id == st.id and x["position"].lane == lane for x in active.values())
                return judged["allow"] and occupied < co.slot_cap(0, judged["metrics"]["last15Pf"])
            for sym in names:
                i = indexes[sym].get(now)
                if i is None:
                    continue
                px = bars[sym][i][3]
                if sym in active:
                    pos = active[sym]["position"]
                    if block_on:
                        row = addons.candidate(pos, px, now, block, book.cost_pct, book.lookback * 60)
                        st = book.sets[pos.set_id]
                        if row and allows(st, pos.lane):
                            pos.block_fills.append({"group": row["group"], "qty": row["qty"], "price": px, "t": now})
                    continue
                if now <= cooldown.get(sym, 0):
                    continue
                warm, dirs, confs, _ = shadow[sym]
                if i < warm:
                    continue
                candidates = sorted((p for p in book.packs if dirs[p][i] and confs[p][i] >= book.entry_conf),
                                    key=lambda p: confs[p][i], reverse=True)
                chosen = None
                for pack in candidates:
                    chosen = next(((lane, st) for lane, st, rank in pools.get(pack, []) if allows(st, lane)), None)
                    if chosen:
                        break
                if not chosen:
                    continue
                lane_name, st = chosen
                lane = sg.se.SetLane(st, sym)
                d = dirs[st.pack][i]
                # A single candidate: the returned close is withheld until its event time.
                traces = lane.scan(i, len(bars[sym]), bars[sym], dirs[st.pack], confs[st.pack], True, cfg, [i], accs[sym])
                tr = traces[0] if traces else None
                pos = Position(sym, "LONG" if d > 0 else "SHORT", 1, px, now, 0, 0, px,
                               set_id=st.id, lane=lane_name, client_id=f"{sym}:{now}", base_entry=px, base_qty=1)
                active[sym] = {"position": pos, "trade": tr, "exit_t": times[sym][tr["exit_i"]] + 60 if tr else None}
        groups = {}
        for row in done:
            key = f"{row['pack']}|{row['type']}|{row['step']}|{row['lane']}"
            groups.setdefault(key, []).append(row["net"])
        return {"axis": axis_on, "block": block_on, "source": "historical execution model", "hold_auto": False,
                "start": start, "end": end, "sets": len(book.by_idx), "closed": measure([x["net"] for x in done]),
                "with_block": measure([x["net"] + x["block_net"] for x in done]),
                "add_fills": sum(x["add_fills"] for x in done), "open_at_end": len(active),
                "validated_pct_mean": sum(x["valid"] / x["total"] * 100 for x in selected) / len(selected),
                "groups": {key: measure(values) for key, values in groups.items()}, "trades": done,
                "block_evidence": addons.snapshot(end, block, book.cost_pct, book.lookback * 60)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    started = time.monotonic()
    names, bars, times = sg.load_bars(args.data, 50)
    if not names or any(len(v) > 4320 or v[-1] - v[0] > 3 * 86400 for v in times.values()):
        raise ValueError("audit requires 1..50 symbols with at most three days each")
    if any(len(v) < 420 or any(b-a != 60 for a,b in zip(v,v[1:])) for v in times.values()):
        raise ValueError("consecutive one-minute candles and at least seven hours required")
    patch = {"holdEnabled": False, "histLookbackBars": 360, "histLookbackAuto": False, "setStepAdapt": False,
             "setSlRatios": [1.0], "trailVariants": ["0.60:0.36", "1.20:0.60", "2.40:1.20", "3.60:1.20"],
             **{k: True for k in ("stratShortRange", "stratTrend", "stratBreakout", "stratReversion", "stratFlow", "stratMulti")}}
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(patch)
    overlay = {**json.load(open(sg.OVERLAY)), **patch}
    shadow = sg.build(names, bars, times, 1)
    runs = [walk(names, bars, times, shadow, overlay, axis, block) for axis,block in ((False,False),(True,False),(True,True))]
    result = {"symbols": names, "input_bars": {s: len(bars[s]) for s in names}, "cost_pct": .12, "runs": runs,
              "seconds": time.monotonic()-started, "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    for run in runs:
        print({k: v for k,v in run.items() if k not in ("groups","trades","block_evidence")}, flush=True)
    print({k:result[k] for k in ("seconds","peak_rss_kb")}, flush=True)


if __name__ == "__main__":
    main()
