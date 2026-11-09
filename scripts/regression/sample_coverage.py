"""Sample sufficiency is independent of profitability and uses each Set's own scored clock."""
from common import overlay, ROOT
from set_engine import SetBook
import sys
sys.path.insert(0, ROOT + "/scripts")
import stage_eval as sg


def own_full_windows():
    b = SetBook()
    b.load(overlay(setGateWindow=8, setGateShortN=0, mainGateEnabled=False,
                   mainNegEnabled=False, realGateEnabled=False, stagesEnabled=False))
    b.by_idx = b.by_idx[:3]
    for st, count, gross in zip(b.by_idx[:2], (8, 3), (-.01, .02)):
        st.hist = [{"t": 1000 + 60*i, "pnl_pct": gross, "pnl": gross - b.cost_pct/100,
                    "symbol": "A", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(count)]
        b._score_one(st, now=2000)
    s = b.snapshot()["pfBase"]["windows"]["baseLong"]["samples"]
    assert s == {"population": 3, "full": 1, "insufficient": 1, "unavailable": 1,
                 "disabled": 0, "pct": 33.33, "basis": "full-requested-window", "at": 2000.0, "oldestAt": 2000.0}, s
    assert not b.by_idx[0].base_long_ok, "a full losing sample is still full"
    # Same-clock and future closes cannot complete the sample.
    st = b.by_idx[1]
    st.hist += [{"t": 2000 + i, "pnl_pct": .02, "pnl": .02 - b.cost_pct/100,
                 "symbol": "A", "side": "LONG", "hold_s": 1, "reason": "tp"} for i in range(5)]
    b._score_one(st, now=2000)
    assert b.snapshot()["pfBase"]["windows"]["baseLong"]["samples"]["full"] == 1
    b._score_one(st, now=2005)
    assert b.snapshot()["pfBase"]["windows"]["baseLong"]["samples"]["full"] == 2
    b.load(overlay(setGateWindow=20))
    assert all(st.sample_counts is None for st in b.by_idx), "policy changes invalidate old readiness"
    return True, "losing/full, short, unmeasured, causal boundary and economic reset"


def independent_base_windows():
    b = SetBook()
    b.load(overlay(setGateWindow=1, setGateShortN=0, setGateMinN=3,
                   stageFullWindow=True, stagesEnabled=True))
    b.by_idx = b.by_idx[:1]
    st = b.by_idx[0]
    st.hist = [{"t": 1000 + 60*i, "pnl_pct": .02, "pnl": .02 - b.cost_pct/100,
                "symbol": "A", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(3)]
    b._score_one(st, now=2000)
    assert st.base_min_n == 3 and st.base_min_ok, "Base Last-1 must not truncate independent minimal Last-3"
    assert b.snapshot()["pfBase"]["windows"]["baseMinimal"]["samples"]["full"] == 1
    tapes = sg.Tapes.__new__(sg.Tapes)
    tapes.cf = b.cost_pct / 100
    tapes.by_set = {st.idx: ([r["t"] for r in st.hist], [r["t"]-60 for r in st.hist], [.02]*3)}
    e = sg.base_eval(b, tapes, st, 2000)
    assert e["n"] == 1 and sg._min_ok(b, e)
    assert sg.window_figures(b, tapes, st, 2000)["baseMinimal"][3] == 3
    st.hist[0]["pnl_pct"] = -.02
    b._score_one(st, now=2000)
    tapes.by_set[st.idx][2][0] = -.02
    assert not st.base_min_ok and not sg._min_ok(b, sg.base_eval(b, tapes, st, 2000)), "an older loss inside Last-3 cannot disappear"
    # A per-range short window can be on when the global one is off.
    b.gate_window = 8
    b.last_n_by_range = {sg.se.tp_range_of(b.step_bucket(st)): {"baseShort": 2}}
    b._score_one(st, now=2000)
    window = b.snapshot()["pfBase"]["windows"]["baseShort"]
    assert window["on"] and window["sets"] == 1 and window["orders"] == 2, window
    return True, "independent minimal Last-3 and per-range short window retain their own counts"


CHECKS = [("samples.own-full-window-coverage", own_full_windows),
          ("samples.independent-base-windows", independent_base_windows)]
