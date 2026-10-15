import os
import sys
import unittest
from copy import deepcopy
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from six_hour_account import execute_account, own_gate, axis_gate, additional_strategy_gate
from set_engine import SetBook
from pulse_trader import Contract


def fixture(aliases=None):
    overlay = dict(setMinSamples=5, setPfWindow=5, mainEvalPosCount=5, realEvalPosCount=5,
                   setMinPf=1.1, baseMinPf=1.1, mainMinPf=1.1, realMinPf=1.1,
                   positionCostPct=.18, marginCapPct=.1, orderSizing="minQty",
                   entryBatchOrders=True, entryBatchSize=1, systemBatchRps=1/60,
                   systemOrderRps=4, slAutoLeverage=False, setHistTimeBars=30,
                   controlOrdersOverall=False,
                   scratchMinPct=20, scratchS=7200)
    book = SetBook(); book.load(overlay)
    bars = [[100., 100., 100., 100., 1.] for _ in range(400)]
    aliases = aliases or ["one"]
    catalog = [dict(kind="base", lane="pack:general", pack="general", set_ids=aliases,
                    sl=.01, tp=.5, trail="", mult=len(aliases))]
    result = dict(symbol="TEST-USDT", bars=bars, funding=[], startS=0, catalog=catalog,
                  ownEvidence={0: {1: [[-1, b, .01] for b in range(30, 35)]}})
    candidates = {"uid": np.array([0]), "sym": np.array([0]), "side": np.array([1]),
                  "entry": np.array([40]), "exit": np.array([-1]), "raw": np.array([.1]),
                  "admitted": np.array([True]), "r30": np.array([1.2]), "countBeforeEntry": np.array([5])}
    contracts = {"TEST-USDT": Contract("TEST-USDT", 1., 1., 0, 2, 0, 100)}
    return [result], catalog, candidates, book, overlay, contracts


class AccountTest(unittest.TestCase):
    def test_actual_live_results_override_positive_replay(self):
        _, _, _, book, _, _ = fixture()
        history = [[-1, i, .02] for i in range(5)]
        live = [(10+i, -.02) for i in range(5)]
        ok, _, metrics = own_gate(history, live, 20, book)
        self.assertFalse(ok)
        self.assertEqual(metrics["base"]["source"], "live")

    def test_only_latest_deactivation_window_is_used(self):
        _, _, _, book, _, _ = fixture()
        book.deact_n = 5
        # Earlier losses do not poison the last five positive actual closes.
        live = [(i, -.2 if i < 5 else .01) for i in range(10)]
        ok, _, _ = own_gate([], live, 20, book)
        self.assertTrue(ok)

    def test_future_close_cannot_qualify_entry(self):
        _, _, _, book, _, _ = fixture()
        ok, _, _ = own_gate([[-1, i, .02] for i in range(40, 45)], [], 40, book)
        self.assertFalse(ok)

    def test_actual_roundtrip_replaces_its_matching_replay_sample(self):
        _, _, _, book, _, _ = fixture()
        history = [[i, 10+i, .02] for i in range(4)]
        ok, _, metrics = own_gate(history, [(15, .02, 0)], 20, book)
        self.assertFalse(ok)
        self.assertEqual(metrics["base"]["n"], 4)

    def test_old_drawdown_cannot_veto_a_recovered_validated_window(self):
        _,_,_,book,_,_ = fixture()
        book.max_dd_s = 600
        history = [[i,i+1,-1.0 if i == 0 else .03] for i in range(21)]
        self.assertTrue(own_gate(history,[],40,book)[0])
        book.ddt_n = 21
        self.assertFalse(own_gate(history,[],40,book)[0])

    def test_independent_block_dca_windows_veto_only_when_full(self):
        _,_,_,book,overlay,_ = fixture()
        for kind,count in (("block",50),("dca",15)):
            with self.subTest(kind=kind):
                self.assertTrue(additional_strategy_gate([[i,i+1,-.01] for i in range(4)],[],100,book,overlay,kind)[0])
                history = [[i,i+1,-.01 if i < count-5 else .01] for i in range(count)]
                self.assertTrue(own_gate(history,[],100,book)[0])
                self.assertFalse(additional_strategy_gate(history,[],100,book,overlay,kind)[0])

    def test_new_close_fill_does_not_encounter_completed_candle_wick(self):
        args = fixture()
        args[0][0]["bars"][40] = [100., 150., 50., 100., 1.]
        report = execute_account(*args, 1000)
        self.assertLess(report["equityCurve"][0]["worstDdPct"], .01)

    def test_existing_stop_caps_its_adverse_mark_and_fills_without_close_rest(self):
        args = fixture()
        args[0][0]["bars"][41] = [100., 100., 50., 100., 1.]
        report = execute_account(*args, 1000)
        self.assertEqual(report["closedPositions"][0]["exit"], 99)
        self.assertLess(report["totals"]["maxIntrabarDdPct"], .2)
        self.assertEqual(report["totals"]["apiRequests"].get("closeRequests",0), 0)
        self.assertEqual(report["totals"]["orders"]["protectiveCloseFills"], 1)

    def test_internal_stop_inside_outer_group_consumes_close_rest_capacity(self):
        args = fixture([f"narrow-{i}" for i in range(300)])
        wide = deepcopy(args[1][0]); wide.update(set_ids=["wide"], mult=1, sl=.03)
        args[1].append(wide)
        args[0][0]["ownEvidence"][1] = {1: [[-1,b,.01] for b in range(30,35)]}
        for name in args[2]:
            args[2][name] = np.append(args[2][name], args[2][name][0])
        args[2]["uid"][1] = 1
        args[4].update(entryBatchSize=500, controlOrdersPerConfig=False)
        args[0][0]["bars"][41] = [100.,100.,98.,100.,1.]
        report = execute_account(*args, 10000)
        # The outer installed97 stop is not touched by low98. Internal99
        # stops require REST and excess exits wait for a fresh next-bar price.
        at_bar = [row for row in report["closedPositions"] if row["exitBar"] == 41]
        self.assertGreater(len(at_bar), 0)
        self.assertLess(len(at_bar), 300)
        self.assertGreater(report["totals"]["rejected"].get("closeDeferredRate",0), 0)
        self.assertGreater(report["totals"]["apiRequests"].get("closeRequests",0), 0)
        self.assertLessEqual(report["orderRateMinutes"][1]["ordinaryRequests"],240)

    def test_per_config_controls_preserve_each_alias_scope_and_bound_entries(self):
        args = fixture([f"own-{i}" for i in range(150)])
        args[4].update(entryBatchSize=500, controlOrdersPerConfig=True)
        report = execute_account(*args,10000)
        first_bar = [row for row in report["openedPositions"] if row["entryBar"] == 40]
        self.assertEqual(len(first_bar),120)
        self.assertEqual(report["equityCurve"][0]["openControlGroups"],120)
        self.assertEqual(report["equityCurve"][0]["openGroups"],1)
        self.assertEqual(len(set(row["controlSetId"] for row in report["openedPositions"])),150)
        self.assertTrue(all(row["ordinaryRequests"] <= 240 for row in report["orderRateMinutes"]))

    def test_unbatched_entries_include_entry_and_two_protection_requests(self):
        args = fixture([f"own-{i}" for i in range(100)])
        args[4].update(entryBatchOrders=False,controlOrdersPerConfig=True)
        report = execute_account(*args,10000)
        first = report["orderRateMinutes"][0]
        self.assertEqual(first["entries"],80)
        self.assertEqual(first["controlRequests"],160)
        self.assertEqual(first["ordinaryRequests"],240)

    def test_overall_physical_protection_precedes_per_config_internal_identity(self):
        args = fixture([f"own-{i}" for i in range(150)])
        args[4].update(entryBatchSize=500,controlOrdersPerConfig=True,controlOrdersOverall=True)
        report = execute_account(*args,10000)
        self.assertEqual(report["orderRateMinutes"][0]["entries"],150)
        self.assertEqual(report["orderRateMinutes"][0]["controlRequests"],2)
        self.assertEqual(report["equityCurve"][0]["openControlGroups"],1)
        self.assertEqual(len(set(row["setId"] for row in report["openedPositions"])),150)
        self.assertEqual(report["totals"]["controlMode"],"overall")

    def test_trailing_outer_pair_updates_when_its_limiting_member_sl_tightens(self):
        args = fixture()
        args[1][0].update(kind="trail",trail="0.6:0.2")
        args[4].update(controlOrdersOverall=True,controlOrdersPerConfig=True)
        args[0][0]["bars"][41] = [100.,101.,100.,101.,1.]
        args[0][0]["bars"][42] = [101.,101.,100.5,100.5,1.]
        report = execute_account(*args,1000)
        self.assertEqual(report["orderRateMinutes"][1]["controlRequests"],4)
        self.assertAlmostEqual(report["closedPositions"][0]["exit"],100.798)
        self.assertEqual(report["totals"]["orders"]["protectiveCloseFills"],1)
        self.assertEqual(report["totals"]["apiRequests"].get("closeRequests",0),0)

    def test_nonlimiting_member_trail_does_not_replace_overall_outer_pair(self):
        args = fixture()
        args[1][0].update(kind="trail",trail="0.6:0.2")
        wide = deepcopy(args[1][0]); wide.update(kind="base",trail="",set_ids=["wide"],sl=.03)
        args[1].append(wide)
        args[0][0]["ownEvidence"][1] = {1:[[-1,b,.01] for b in range(30,35)]}
        for name in args[2]:
            args[2][name] = np.append(args[2][name],args[2][name][0])
        args[2]["uid"][1] = 1
        args[4].update(controlOrdersOverall=True,controlOrdersPerConfig=True,entryBatchSize=5)
        args[0][0]["bars"][41] = [100.,101.,100.,101.,1.]
        report = execute_account(*args,1000)
        self.assertEqual(report["orderRateMinutes"][1]["controlRequests"],0)

    def test_negative_actual_alias_does_not_veto_its_positive_sibling(self):
        args = fixture(["negative", "positive"])
        args[1][0].update(sl=.001,tp=.03)
        args[0][0]["ownEvidence"][0] = {1:[[i,30+i,.03] for i in range(5)]}
        entries = [40,42,44,46,48,50]
        for name,value in list(args[2].items()):
            args[2][name] = np.repeat(value,len(entries))
        args[2]["entry"] = np.array(entries)
        for minute in (41,43,45,47,49):
            args[0][0]["bars"][minute] = [100.,101. if minute == 49 else 100.,98.,98.,1.]
        report = execute_account(*args,1000)
        prior_negative = [row for row in report["closedPositions"] if row["templateId"] == "negative" and row["exitBar"] < 50]
        self.assertEqual(len(prior_negative),5)
        self.assertTrue(all(row["net"] < 0 for row in prior_negative))
        # At bar50 the negative alias has five own losing actual results;
        # the other alias has a positive actual close and must still execute.
        at_bar = [row for row in report["openedPositions"] if row["entryBar"] == 50]
        self.assertEqual([row["templateId"] for row in at_bar],["positive"])
        self.assertGreater(report["totals"]["rejected"].get("baseOwnEvidence",0),0)

    def test_refreshing_signal_preserves_unfinished_tail_aliases(self):
        args = fixture([f"own-{i}" for i in range(150)])
        args[1][0]["tp"] = .01
        args[4].update(entryBatchSize=500,controlOrdersPerConfig=True)
        for name,value in list(args[2].items()):
            args[2][name] = np.repeat(value,5)
        args[2]["entry"] = np.arange(40,45)
        for minute in range(41,46):
            args[0][0]["bars"][minute] = [100.,102.,100.,100.,1.]
        report = execute_account(*args,10000)
        tail = next(row for row in report["openedPositions"] if row["templateId"] == "own-149")
        self.assertEqual(tail["entryBar"],42)
        self.assertEqual(tail["signalBar"],42)
        self.assertTrue(all(row["ordinaryRequests"] <= 240 for row in report["orderRateMinutes"]))

    def test_deferred_axis_requires_its_current_parent_window(self):
        _, _, _, book, _, _ = fixture()
        positive = [[i, i+10, .02] for i in range(5)]
        self.assertTrue(axis_gate(positive, [], 20, book, "last", 2)[0])
        actual = [(15+i, -.02, i) for i in range(5)]
        self.assertFalse(axis_gate(positive, actual, 30, book, "last", 2)[0])

    def test_deferred_fill_uses_fresh_price_and_native_exit(self):
        args = fixture(["one", "two"])
        for i in range(41, 400):
            args[0][0]["bars"][i] = [105., 105., 105., 105., 1.]
        report = execute_account(*args, 1000)
        self.assertEqual(report["openedPositions"][0]["entry"], 100)
        self.assertEqual(report["openedPositions"][1]["entry"], 105)
        self.assertEqual(report["openedPositions"][1]["deferredBars"], 1)
        self.assertTrue(all(r["batchRequests"] <= r["batchLimit"] for r in report["orderRateMinutes"]))
        self.assertTrue(all(r["ordinaryRequests"] <= r["ordinaryLimit"] for r in report["orderRateMinutes"]))
        # Cached raw .1 is deliberately ignored: native time exit is flat105.
        second = next(r for r in report["closedPositions"] if r["templateId"] == "two")
        self.assertEqual(second["exit"], 105)

    def test_funding_and_strategy_contributions_reconcile(self):
        args = fixture()
        args[0][0]["funding"] = [dict(fundingTime=42*60000, fundingRate=".001", markPrice="100")]
        report = execute_account(*args, 1000)
        self.assertAlmostEqual(report["totals"]["fundingUsdt"], -.1)
        total = report["totals"]
        self.assertAlmostEqual(total["balanceEnd"], 1000 + total["grossRealizedUsdt"] - total["fees"] + total["fundingUsdt"])
        self.assertAlmostEqual(sum(hour["feesUsdt"] for hour in report["hourly"]), total["fees"])
        self.assertAlmostEqual(sum(hour["fundingUsdt"] for hour in report["hourly"]), total["fundingUsdt"])
        for point in report["equityCurve"]:
            self.assertAlmostEqual(sum(point["strategyContributions"].values()), point["equity"]-1000)

    def test_strategy_wick_drawdown_uses_peak_before_the_completed_candle(self):
        args = fixture()
        args[1][0]["sl"] = .03
        args[0][0]["bars"][41] = [100.,105.,98.,105.,1.]
        report = execute_account(*args,1000)
        # Peak after this candle +4.91 cannot precede its wick -2.09.
        # The later flat close loses5.09 from that peak, not an invented7.
        self.assertAlmostEqual(report["hourly"][0]["byStrategy"]["normal"]["cumulativeDdUsdt"],5.09)

    def test_dca_is_one_real_add_leg_with_actual_average_bundle_protection(self):
        args = fixture()
        args[1][0]["sl"] = .03
        args[1].append(dict(kind="dca", lane="pack:general", pack="general", set_ids=["dca:one:1"],
                            parentSetIds=["TEST-USDT|pack:general|one"], parentTemplateId="one", relativeCount=1,
                            volumeRatio=1.5, axisKey="dca:1", sl=.03, tp=.5, trail="", mult=1))
        args[0][0]["ownEvidence"][1] = {1: [[-1, b, .01] for b in range(30, 35)]}
        args[0][0]["bars"][41] = [98.5, 98.5, 98.5, 98.5, 1.]
        for i in range(42, 400):
            args[0][0]["bars"][i] = [96., 96., 96., 96., 1.]
        for name in args[2]:
            args[2][name] = np.append(args[2][name], args[2][name][0])
        args[2]["uid"][1], args[2]["entry"][1] = 1, 41
        args[2]["parentEntry"] = np.array([40, 40])
        report = execute_account(*args, 1000)
        self.assertEqual(len(report["openedPositions"]), 2)
        self.assertEqual(report["openedPositions"][1]["qty"], 1)
        # The requested1.5 venue lots are capped then rounded DOWN to1;
        # actual average=(100+98.5)/2=99.25, therefore shared SL=96.2725.
        self.assertEqual(len(report["closedPositions"]), 2)
        self.assertTrue(all(abs(row["exit"]-96.2725) < 1e-9 for row in report["closedPositions"]))
        self.assertAlmostEqual(report["totals"]["grossRealizedUsdt"], -5.955)
        self.assertEqual(report["closedPositions"][1]["strategy"], "dca")

    def test_dca_does_not_add_to_an_unrelated_or_closed_parent(self):
        args = fixture()
        args[1][0].update(kind="dca", parentSetIds=["absent"], parentTemplateId="absent",
                          relativeCount=1, volumeRatio=1.5, axisKey="dca:1")
        report = execute_account(*args, 1000)
        self.assertEqual(report["openedPositions"], [])
        self.assertGreater(report["totals"]["rejected"].get("actualParentNotOpen",0)+report["totals"]["rejected"].get("descendantParentEvidenceMissing",0), 0)

    def test_dca_venue_quantization_cannot_round_2_3_to_3_parent_units(self):
        args = fixture()
        args[1][0]["sl"] = .03
        args[4].update(dcaStepVolumeMultipliers=[2.3],dcaMaxSteps=1)
        args[3].load(args[4])
        args[1].append(dict(kind="dca",lane="pack:general",pack="general",set_ids=["dca:one:1"],
                            parentSetIds=["TEST-USDT|pack:general|one"],relativeCount=1,
                            volumeRatio=2.3,axisKey="dca:1",sl=.03,tp=.5,trail="",mult=1))
        args[0][0]["ownEvidence"][1] = {1:[[-1,b,.01] for b in range(30,35)]}
        args[0][0]["bars"][41] = [98.5,98.5,98.5,98.5,1.]
        for name in args[2]:
            args[2][name] = np.append(args[2][name],args[2][name][0])
        args[2]["uid"][1],args[2]["entry"][1] = 1,41
        args[2]["parentEntry"] = np.array([40,40])
        report = execute_account(*args,1000)
        leg = next(row for row in report["openedPositions"] if row["strategy"] == "dca")
        self.assertEqual(leg["qty"],2)
        self.assertLessEqual(leg["qty"],report["openedPositions"][0]["qty"]*2.55)


if __name__ == "__main__":
    unittest.main()
