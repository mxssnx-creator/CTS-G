"""Offline replay/live lifecycle invariants; no account or network access."""
import os
import sys
import types

from common import ROOT, T0, SMALL, overlay

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import stage_eval as sg
import set_engine as se


def axis_windows_follow_settlement_order():
    # Entry-ordered paths close at 30, 10, 20. The newest loss must
    # pause Axis, and the last two returns have PF 1/2, not PF 2.
    rows = [(30, -2.0), (10, 2.0), (20, 1.0), (99, 100.0)]
    pause = sg.axis_allows(rows, 30, (0, 1.0, 1), 0)
    window = sg.axis_allows(rows, 30, (2, 1.0, 0), 0)
    before = sg.axis_allows(rows, 20, (2, 1.0, 0), 0)
    return not pause and not window and before, f"pause={pause}, last2={window}, earlier={before}"


def held_positions_consume_axis_slots_until_the_end():
    b = se.SetBook()
    b.load(overlay(**{**SMALL, "stagesEnabled": False, "mainGateEnabled": False,
        "setStepMax": 2, "setSlRatios": [1], "groupGate": False,
        "minPfStepAuto": False, "timeStopS": 21600}))
    st = next(x for x in b.by_idx if x.kind == "base" and x.side in (0, 1))
    st.idx = 0
    b.by_idx, b.packs = [st], [st.pack]
    hour = float(int(T0) // 3600 * 3600)
    names = ["A-USDT", "B-USDT", "C-USDT"]
    bars = {s: [[100., 100., 100., 100., 1.] for _ in range(60)] for s in names}
    times = {s: [int(hour) + i * 60 for i in range(60)] for s in names}
    shadow = {s: (0, {st.pack: [1] * 60}, {st.pack: [.9] * 60}, {}) for s in names}
    saved = sg.gate_metrics, sg.gate_until, sg.axis_judge, se.SetLane.scan
    entries = []
    native_scan = se.SetLane.scan

    def scan(lane, *args, **kwargs):
        entries.append(lane)
        return native_scan(lane, *args, **kwargs)

    try:
        sg.gate_metrics = lambda *_: (True, (True, 2., 1, 0, 0), 1, 2.)
        sg.gate_until = lambda *_: float("inf")
        sg.axis_judge = lambda *_: (True, 2., 1.)
        se.SetLane.scan = scan
        coord = types.SimpleNamespace(slot_cap=lambda *a, **kw: 1)
        counts = []
        for include in (False, True):
            entries.clear()
            rows = sg.execute(b, names, bars, times, shadow,
                types.SimpleNamespace(cf=b.cost_pct / 100), [hour],
                refresh_s=0, include_open=include, axis_coord=coord)
            counts.append((len(entries), sum(bool(r.get("open_at_end")) for r in rows)))
    finally:
        sg.gate_metrics, sg.gate_until, sg.axis_judge, se.SetLane.scan = saved
    return counts == [(1, 0), (1, 1)], f"entries/open report rows={counts}; cap=1 across three symbols"


CHECKS = [("replay-runtime." + fn.__name__.replace("_", "-"), fn) for fn in (
    axis_windows_follow_settlement_order, held_positions_consume_axis_slots_until_the_end)]
