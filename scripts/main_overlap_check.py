#!/usr/bin/env python3
"""Engine in-process: for the Sets that pass Base now, their Base window orders split into Main orders (opened while Base passed) and the
rest, next to the Main Sets last 25 orders. usage: main_overlap_check.py DATA_DIR/ (at most 3 days of 1m bars per symbol)."""
import sys, json, os
sys.path.insert(0, '/home/user/CTS-G/server/pulse')
import set_engine as se
from position_cost import cost_as_frac
SP = sys.argv[1] if len(sys.argv) > 1 else 'data/'
ov = json.load(open('/home/user/CTS-G/server/pulse/overlay-bingx-x02.json')); ov.update({"histLookbackBars": 1920, "histLookbackAuto": False})
b = se.SetBook(); b.load(ov)
for f in sorted(os.listdir(SP))[:8]:
    raw = json.load(open(SP + f))[-2401:-1]
    b.bars[f[:-5]] = [[float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in raw]
    b.bar_t[f[:-5]] = raw[-1][0] / 1000
b.replay_all(now=raw[-1][0] / 1000 + 60)
cf = cost_as_frac(b.cost_pct)
def pf(v):
    g = sum(x for x in v if x > 0); l = -sum(x for x in v if x < 0); return round(g / l, 3) if l else None
acc = {"win_all": [], "win_main": [], "win_notmain": [], "main_last25": [], "main_all": []}
for st in b.by_idx:
    if b.gate_reason(st) not in ("",) + se.STAGE_REASONS:
        continue
    mt = {round(t, 3) for t, _x in (st.proc or [])}
    for r in st.hist[-b.gate_window:]:
        net = r["pnl_pct"] - cf
        acc["win_all"].append(net)
        (acc["win_main"] if round(r["t"], 3) in mt else acc["win_notmain"]).append(net)
    acc["main_last25"] += [x for _t, x in (st.proc or [])[-25:]]
    acc["main_all"] += [x for _t, x in (st.proc or [])]
for k, v in acc.items():
    print(f"{k:12s} orders {len(v):6d} PF {pf(v)}")
