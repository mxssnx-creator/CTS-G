"""Causal own-parent adjustment evidence, identity and bounded retention."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/"server/pulse"))
from dca_engine import DcaBook
from block_engine import cost_pf_from_net_fracs
from indication_adjustment_grid import (
    AdjustmentTape, adjustment_set_id, calculate_adjustments, capture_parent_events)
from set_engine import SetBook, hist_fill, recent_direction_rows


def book():
    b = SetBook()
    b.load(dict(histLookbackBars=360, histMinBars=60, histWarmup=2,
        setMinStep=7, setStepMax=7, slToTpRatios=[3.], stratGeneral=False,
        stratTrailing=False, baseEvalPosCount=5, mainEvalPosCount=5,
        realEvalPosCount=5, microEnabled=False, positionCostPct=.1,
        slMinPct=.15, slMaxPct=3., histSimulateBlock=True,
        histSimulateDca=True, dcaEnabled=True, dcaMaxSteps=2,
        dcaStepDistancesPct=[1.2, 1.6], dcaStepVolumeMultipliers=[1.5, 2.],
        dcaCooldownSeconds=30, blockCounts=[1, 2], blockMaxStack=2))
    b.cooldown_bars = 0
    b.scratch_s = 100000
    b.hist_time_bars = 100
    b.hist_honor_tp = True
    return b


def parent(b, name="own", kind="base"):
    p = b.by_idx[0].catalog_copy()
    p.id, p.parent_set_id = name, name
    p.kind, p.indication_kind, p.indication_config, p.indication_symbol = kind, "signals", "direct_tf:1m", "X"
    p.template_set_id = b.by_idx[0].id
    p.tp_pct, p.sl_ratio = .01, 3.
    p.hist = [hist_fill(1000+i, "X", 1, .01, 60, "tp") for i in range(8)]
    b._score_pair((p, None))
    return p


def bars(prices):
    return [[p, p+.01, p-.01, p, 1000] for p in prices]


def one_entry(n, i=2, side=1):
    return [(side, .9, "direct_tf:1m") if j >= i else (0, 0., "") for j in range(n)]


class AdjustmentGridTests(unittest.TestCase):
    def replay(self, b, p, rows, *, capture=True, assume=False):
        return calculate_adjustments(b, "X", rows, p if isinstance(p, list) else [p],
            one_entry(len(rows)), 2, 1_800_000_000, capture=capture,
            assume_qualified=assume)

    def test_exact_parent_identity_and_missing_direction_do_not_borrow(self):
        b = book(); p = parent(b)
        rows = bars([100]*3+[100.3, 101.5, 101.5, 101.5, 101.5, 101.5, 101.5, 101.5])
        states = self.replay(b, p, rows)
        self.assertEqual({s.id for s in states},
            {adjustment_set_id(p.id, strategy, n) for strategy in ("block", "block-add", "dca") for n in (1, 2)})
        self.assertTrue(all(s.parent_set_id == "own" and s.indication_config == p.indication_config for s in states))
        self.assertTrue(all(all(r["side"] == "LONG" for r in s.hist) for s in states))
        for s in states:
            self.assertEqual(s.by_side["SHORT"]["last15_n"], 0)
        empty = parent(b, "empty"); empty.hist = []; empty.by_side = {}
        self.assertEqual(self.replay(b, empty, rows), [])
        self.assertTrue(self.replay(b, empty, rows, assume=True))

    def test_dca_fill_rebases_average_once_and_only_actual_adds_are_evidence(self):
        b = book(); p = parent(b)
        rows = bars([100,100,100,98.7,100.5,100.5,100.5,100.5,100.5,100.5,100.5])
        states = self.replay(b, p, rows)
        dca = next(s for s in states if s.kind == "dca" and s.relative_count == 1)
        self.assertEqual(len(dca.hist), 1)
        row = dca.hist[0]
        self.assertEqual(row["_entry"], 3)
        self.assertEqual(row["_exit"], 4)
        self.assertEqual(row["_log"], ((3, 98.7, 1.5),))
        self.assertAlmostEqual(row["_avg"], 98.7)
        self.assertAlmostEqual(row["_qty"], 1.5)
        self.assertAlmostEqual(row["_bundle_avg"], (100+98.7*1.5)/2.5)
        self.assertAlmostEqual(row["_bundle_qty"], 2.5)
        self.assertAlmostEqual(row["pnl_pct"], (row["_bundle_avg"]*1.01-98.7)/98.7)
        self.assertEqual(row["dca_n"], 1)
        # Untriggered future ordinary parents do not become DCA samples.
        self.assertEqual(dca.n, 1)
        self.assertFalse(dca.stage_ledger["base"])
        second = next(s for s in states if s.kind == "dca" and s.relative_count == 2)
        self.assertEqual(second.hist, [])
        self.assertEqual(second.n, 0)

    def test_add_at_completed_close_cannot_rescue_an_earlier_stop(self):
        b = book(); p = parent(b)
        rows = bars([100]*3+[98.7, 100.5]+[100.5]*7)
        rows[3] = [100, 100.2, 96.5, 98.7, 1000]
        states = self.replay(b, p, rows)
        self.assertTrue(all(not s.hist for s in states if s.kind == "dca"))
        events = capture_parent_events(b, "X", rows, p, one_entry(len(rows)), 2, 1_800_000_000)
        self.assertEqual(events[0]["reason"], "sl")
        self.assertAlmostEqual(events[0]["pnl_pct"], -.03)

    def test_block_standalone_uses_full_selected_exit_ranges(self):
        b = book(); p = parent(b)
        rows = bars([100]*3+[100.3, 99.8, 101.4]+[101.4]*6)
        states = self.replay(b, p, rows)
        block = next(s for s in states if s.kind == "block" and s.relative_count == 1)
        self.assertTrue(block.hist)
        row = block.hist[0]
        self.assertEqual(row["_entry"], 3)
        # The historical 0.15% lock would stop on bar 4; the actual
        # standalone selected 3% SL remains protected until its 1% TP.
        self.assertEqual(row["_exit"], 5)
        self.assertAlmostEqual(row["_exit_px"], 100.3*1.01)
        self.assertAlmostEqual(row["pnl_pct"], .01)
        self.assertAlmostEqual(row["_qty"], .25)
        self.assertEqual(row["_parent_entry"], 2)

    def test_block_add_average_rungs_have_own_complete_fill_paths(self):
        b = book(); p = parent(b)
        rows = bars([100]*3+[100.3,100.8,101.5]+[101.5]*7)
        states = self.replay(b, p, rows)
        first = next(s for s in states if s.id == adjustment_set_id(p.id,"block-add",1))
        second = next(s for s in states if s.id == adjustment_set_id(p.id,"block-add",2))
        self.assertEqual(len(first.hist),1)
        self.assertEqual(len(second.hist),1)
        row = second.hist[0]
        self.assertEqual(row["_log"], ((4,100.8,.25),))
        self.assertEqual(row["_bundle_log"], ((2,100.,1.),(3,100.3,.25),(4,100.8,.25)))
        self.assertAlmostEqual(row["_bundle_avg"], (100+100.3*.25+100.8*.25)/1.5)
        self.assertAlmostEqual(row["_avg"],100.8)
        self.assertAlmostEqual(row["_qty"],.25)
        self.assertAlmostEqual(row["_bundle_qty"],1.5)
        self.assertEqual(row["block_count"],2)
        self.assertEqual(row["axis_key"],"block-add:2")
        self.assertNotEqual(first.hist[0]["_avg"],row["_avg"])

    def test_regular_block_uses_shared_ratio_and_sparse_enabled_rungs(self):
        b=book(); p=parent(b)
        b.block_active=False; b.block_counts=[1,3]; b.block_stack=3
        b.block_vr=.1; b.hist_dca=False
        rows=bars([100]*3+[100.3,100.35,101.5]+[101.5]*7)
        states=self.replay(b,p,rows)
        self.assertEqual({s.axis_key for s in states},{"block-add:1","block-add:3"})
        third=next(s for s in states if s.relative_count==3)
        self.assertEqual(len(third.hist),1)
        self.assertAlmostEqual(third.hist[0]["_qty"],.2)
        self.assertEqual(third.hist[0]["_entry"],4)
        log=third.hist[0]["_bundle_log"]
        self.assertEqual([(i,px) for i,px,q in log],[(2,100.),(3,100.3),(4,100.35)])
        for fill,expected in zip(log,[1.,.1,.2]):
            self.assertAlmostEqual(fill[2],expected)

    def test_aliased_effective_parameters_share_only_immutable_numeric_evidence(self):
        b = book(); a = parent(b, "a"); c = parent(b, "c")
        rows = bars([100]*3+[100.3, 101.5]+[101.5]*7)
        states = self.replay(b, [a, c], rows, capture=False)
        aa = next(s for s in states if s.id == adjustment_set_id("a", "block", 1))
        cc = next(s for s in states if s.id == adjustment_set_id("c", "block", 2))
        self.assertIsInstance(aa.hist, AdjustmentTape)
        self.assertIs(aa.hist.columns, cc.hist.columns)
        self.assertNotEqual(aa.hist[0]["set_id"], cc.hist[0]["set_id"])
        self.assertEqual(aa.hist[0]["ind_config"], cc.hist[0]["ind_config"])
        aa.hist[0]["pnl_pct"] = -99
        self.assertAlmostEqual(aa.hist[0]["pnl_pct"], .01)
        with self.assertRaises(ValueError):
            aa.hist.columns[2][0] = -99
        self.assertEqual(states.diagnostics["effectiveParameters"], 1)
        self.assertIsNot(aa.live, cc.live)

    def test_mixed_signal_configurations_or_symbols_are_rejected(self):
        b=book(); a=parent(b,"a"); c=parent(b,"c")
        c.indication_config="direct_tf:5m"
        with self.assertRaisesRegex(ValueError,"one indication"):
            self.replay(b,[a,c],bars([100]*12))
        c.indication_config=a.indication_config
        c.indication_symbol="Y"
        with self.assertRaisesRegex(ValueError,"replayed symbol"):
            self.replay(b,[a,c],bars([100]*12))

    def test_general_pack_identity_and_higher_confidence_floor_are_preserved(self):
        b=book(); p=parent(b)
        p.pack="general"; p.indication_kind="general"; p.indication_config="general"
        rows=bars([100]*3+[100.3,101.5]+[101.5]*7)
        states=self.replay(b,p,rows,capture=False)
        own=next(s for s in states if s.id==adjustment_set_id(p.id,"block",1))
        self.assertEqual(own.pack,"general")
        self.assertTrue(own.hist)
        self.assertEqual(own.hist[0]["pack"],"general")
        weak=[(1,.55,"") for row in rows]
        cold=calculate_adjustments(b,"X",rows,[p],weak,2,1_800_000_000)
        self.assertTrue(all(len(s.hist)==0 for s in cold))

    def test_capture_has_terminal_open_equity_without_a_fake_close(self):
        b = book(); p = parent(b)
        rows = bars([100]*3+[100.3]+[100.3]*8)
        states = self.replay(b, p, rows)
        block = next(s for s in states if s.kind == "block" and s.relative_count == 1)
        self.assertEqual(block.hist, [])
        self.assertEqual(block.n, 0)
        self.assertTrue(any(r["set_id"] == block.id for r in states.open))
        self.assertTrue(all(r["reason"] == "open" for r in states.open))

    def test_unqualified_parent_cannot_become_a_positive_child_by_accounting(self):
        b = book(); p = parent(b)
        p.by_side["LONG"]["base_pf"] = .5
        p.by_side["LONG"]["last15_ratio"] = .5
        self.assertEqual(self.replay(b, p, bars([100]*12)), [])

    def test_zero_measured_cost_survives_dca_load(self):
        d = DcaBook(); d.load({"positionCostPct": 0})
        self.assertEqual(d.cost_pct, 0)

    def test_dca_deactivation_respects_each_rows_measured_zero_cost(self):
        d = DcaBook(); d.load({"positionCostPct": .1, "minPf": 1.02})
        rows = [dict(t=1000+i, symbol="X", side="LONG", pnl_pct=.0005,
                     position_cost_pct=0., reason="dca:tp") for i in range(25)]
        score = d.score(rows)
        self.assertTrue(score["active"])
        self.assertGreater(score["last25AvgR"], 0.)
        self.assertGreater(score["ratio"], 1.)
        self.assertGreater(cost_pf_from_net_fracs([.001], 0.), 1.)

    def test_full_parent_event_capture_exceeds_runtime_retention_honestly(self):
        b = book(); p = parent(b)
        prices = [100]*3
        for i in range(120):
            prices.extend([101.1, 100])
        lane = [(1,.9,"") if close == 100 else (0,0.,"") for close in prices]
        tape = capture_parent_events(b, "X", bars(prices), p,
            lane, 2, 1_800_000_000)
        self.assertGreater(len(tape), 96)
        self.assertTrue(all(r["_entry"] < r["_exit"] for r in tape))
        self.assertTrue(all(tape[i]["t"] <= tape[i+1]["t"] for i in range(len(tape)-1)))

    def test_parent_capture_matches_native_normal_and_trailing_replay(self):
        import math
        b=book(); normal=parent(b,"normal"); trail=parent(b,"trail",kind="trail")
        trail.trail_arm=.3; trail.trail_give=.2
        prices=[100+2*math.sin(i/8) for i in range(240)]
        rows=[[p,p+.15,p-.15,p,1000] for p in prices]
        lane=[(1 if math.cos(i/8)>0 else -1,.9,"") for i in range(len(rows))]
        native={}; count={}
        b._replay_core_vectorized("X",rows,[normal,trail],lane,1_800_000_000,2,
            100,1666,True,native,count,confidence_floor=.52)
        captured=capture_parent_events(b,"X",rows,[normal,trail],lane,2,1_800_000_000)
        for p in (normal,trail):
            actual=recent_direction_rows(captured[p.id],96)
            expected=native[p.id]
            self.assertEqual(len(captured[p.id]),count[p.id])
            self.assertEqual([(r["t"],r["side"],r["hold_s"],r["reason"]) for r in actual],
                             [(r["t"],r["side"],r["hold_s"],r["reason"]) for r in expected])
            for a,e in zip(actual,expected):
                self.assertAlmostEqual(a["pnl_pct"],e["pnl_pct"])


if __name__ == "__main__":
    unittest.main()
