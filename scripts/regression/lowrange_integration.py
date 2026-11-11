"""Directional entry arbitration and imported Low-Range source parity."""
import io
import os
import sys
import types
import unittest
from unittest.mock import patch
from common import ROOT

sys.path.insert(0, os.path.join(ROOT, "scripts"))


def ao_source_parity():
    from test_ao_lowrange import AOPortTest
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output).run(unittest.defaultTestLoader.loadTestsFromTestCase(AOPortTest))
    return result.wasSuccessful(), output.getvalue().strip()


def live_selection_respects_signal_side():
    import pulse_trader as pt
    from trader import _bare_pulse
    from coord_engine import Coordinator
    sym, minute = "X-USDT", 1_790_002_800.0
    sides = {"strong": -1, "valid": 1}
    class Sets:
        packs = ["strong", "valid"]
        bar_t = {sym: minute - 60}
        def entry_signals(self, *_):
            return [("strong", 1, .95, "long"), ("valid", 1, .8, "long")]
        def pick_any(self, pack, allowed_lanes=None, side=0, **_):
            return types.SimpleNamespace(lane="pos") if not side or sides[pack] == side else None
        def lane_of(self, row):
            return row.lane
    p = _bare_pulse()
    p.sets, p.open, p.coord = Sets(), {}, Coordinator()
    with patch.object(pt, "SYMBOLS", [sym]):
        first = p.set_entry_candidates(now=minute + 5)[sym]
        sides["strong"] = 1
        second = p.set_entry_candidates(now=minute + 6)[sym]
    return first[3].startswith("set:valid:") and second[3].startswith("set:strong:"), f"side-qualified candidates {first} then {second}"


def replay_falls_through_ineligible_pack():
    import stage_eval as sg
    import set_engine as se
    b = se.SetBook()
    b.load({"minPfStepAuto": False, "holdAuto": False, "pauseMode": "off", "positionCostPct": .11, "groupGate": False})
    b.packs = ["strong", "valid"]
    states = [se.SetState(id=p, pack=p, tf="1", sl_ratio=1, trail_key="", trail_arm=0, trail_give=0,
                          side=sd, idx=i, step=2, tp_pct=.002) for i, (p, sd) in enumerate(zip(b.packs, [-1, 1]))]
    b.by_idx = states
    b.sets = {s.id: s for s in states}
    t = 1_790_002_800
    bars = {"X": [[100, 100.4, 99.95, 100, 1] for _ in range(60)]}
    times = {"X": [t + i * 60 for i in range(60)]}
    ds = {p: [0, 1] + [0] * 58 for p in b.packs}
    cs = {p: [0, c] + [0] * 58 for p, c in zip(b.packs, [.95, .8])}
    shadow = {"X": (0, ds, cs, {})}
    with patch.object(sg, "gate_metrics", return_value=("pos", (1,), 8, 2)):
        rows = sg.execute(b, ["X"], bars, times, shadow, None, [t], refresh_s=3600)
    trades = [r for r in rows if "net" in r]
    return len(trades) == 1 and trades[0]["pack"] == "valid" and trades[0]["net"] > 0, f"real lane scan selected {[r['pack'] for r in trades]}"


def future_entries_exclude_inherited_positions():
    from trade_sim import future_entries
    got = future_entries([10, 20, 30, 40], [1, 5, 21, 35], [.1, -.2, .3, -.4], 15, 1, .01)
    return got == [.29], f"future entry, cost once: {got}"


def future_entries_follow_entry_order_without_close_or_pnl_tiebreaks():
    from trade_sim import future_entries
    # B closes first but was entered after A and C. A and C share an entry
    # minute; symbol order, not close time or profit, resolves the boundary.
    exits, entries, pnl, symbols = [180, 240, 360, 420], [0, 120, 60, 60], [-.8, -.2, .3, .1], ['D', 'B', 'C', 'A']
    got = future_entries(exits, entries, pnl, 60, 2, .01, symbols)
    return len(got) == 2 and all(abs(a-b) < 1e-12 for a,b in zip(got,[.09,.29])), f"next entries={got} (A then C; inherited D excluded)"


def indexed_future_entries_match_reference_and_handle_short_cohorts():
    from trade_sim import FutureEntryTapes, future_entries
    times = {s: [i * 60 for i in range(7)] for s in ('A', 'B', 'C')}
    shadow = {'A': (None, None, None, {0: ([1], [6], [.1])}),
              'B': (None, None, None, {0: ([3], [4], [-.2])}),
              'C': (None, None, None, {0: ([1], [2], [.3])})}
    index = FutureEntryTapes(['C', 'B', 'A'], times, shadow, .01)
    # This is the same data as stage_eval.Tapes: sorted by exit time.
    expected = future_entries([180, 300, 420], [120, 240, 120], [.3, -.2, .1], 120, 2, .01, ['C', 'B', 'A'])
    a = index.window(0, 120, 2)
    b = index.window(0, 121, 5)
    return (a == expected and len(a) == 2 and all(abs(x-y) < 1e-12 for x,y in zip(a,[.09,.29]))
            and len(b) == 1 and abs(b[0] - (-.21)) < 1e-12
            and index.window(0, 120, 0) == [] and len(index.cache) == 1), f"first={a}, later={b}"


def indexed_future_entries_match_independent_entry_sort():
    import random
    from trade_sim import FutureEntryTapes
    rng = random.Random(1108)
    names = ['A', 'B', 'C', 'D']
    times = {name: [i * 60 for i in range(80)] for name in names}
    shadow, expected = {}, []
    for name in names:
        entries = sorted(rng.sample(range(70), 20))
        rows = sorted((rng.randrange(e, 80), e, rng.uniform(-.02,.02)) for e in entries)
        shadow[name] = (None, None, None, {0: ([r[1] for r in rows], [r[0] for r in rows], [r[2] for r in rows])})
        expected.extend((times[name][entry] + 60, name, i, value - .001)
                        for i,(_,entry,value) in enumerate(rows))
    expected.sort(key=lambda r: (r[0], r[1], r[2]))
    index = FutureEntryTapes(list(reversed(names)), times, shadow, .001)
    for now in (0, 660, 2400, 4800, 5400):
        for count in (1, 8, 50, 100):
            actual = index.window(0, now, count)
            want = [r[3] for r in expected if r[0] >= now][:count]
            if actual != want:
                return False, f"mismatch at {now=} {count=}"
    return True, '20 entry-time/count combinations match an independent symbol-stable sort'


def dca_unlimited_cap_and_weighted_stats():
    from stack_eval import dca_rungs, summarize
    cfg = {"n": 2, "f0": .2, "fs": .1, "m0": .5, "g": 2, "max_expo": 0}
    r = dca_rungs(1, 100, 101, .02, 0, 5, cfg, .001, lambda a, *_: a)
    limited = dca_rungs(1, 100, 101, .02, 0, 5, {**cfg, "max_expo": 1.5}, .001, lambda a, *_: a)
    stats = summarize([(1, .01, .2), (3600, -.01, .5)], 0, 2, [0])
    return (len(r) == 2 and len(limited) == 1 and stats["pf"] == .4 and stats["net_pct"] == -.3
            and stats["hourly"][0]["fills"] == 2 and stats["hourly"][0]["net_pct"] == -.3), f"unlimited={len(r)}, capped={len(limited)}, weighted PF={stats['pf']}"


CHECKS = [("stage-audit." + fn.__name__.replace("_", "-"), fn) for fn in (
    ao_source_parity, live_selection_respects_signal_side, replay_falls_through_ineligible_pack,
    future_entries_exclude_inherited_positions, future_entries_follow_entry_order_without_close_or_pnl_tiebreaks,
    indexed_future_entries_match_reference_and_handle_short_cohorts, indexed_future_entries_match_independent_entry_sort,
    dca_unlimited_cap_and_weighted_stats)]
