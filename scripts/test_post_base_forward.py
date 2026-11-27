"""Regressions for the forward fill model, independent evidence and release rule."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import numpy as np

import stage_eval as sg
from check_post_base_forward import assess
from post_base_exec import MarketReplay, Trade, encode
from post_base_forward import ReplayFills, fill_entry, load_archive
from post_base_gates import ExecWindow, ForwardProof, terminal_pass
from post_base_results import possible_adds, summarize
from stage_grid import StageGrid, StageHistory


class ForwardReplayTests(unittest.TestCase):
    def test_both_sides_fill_next_open_and_use_the_active_stop_first(self):
        cfg = {"entry_conf": 0., "cooldown": 2, "cost_pct": .11, "time_bars": 10,
               "scratch_bars": 10, "scratch_min": 0., "honor_tp": True}
        bars = [(100., 101., 99., 100., 1.), (110., 115., 105., 110., 1.)]
        for side in (1, -1):
            with self.subTest(side=side):
                st = sg.se.SetState("fixture", "fixture", "1m", 1., "", 0., 0., side=side, tp_pct=.01)
                cost = ReplayFills(.0005, .00005)
                release, last, entry, exit, pnl, reason = fill_entry(
                    st, "X-USDT", side, 0, bars, sg.se.BarIndex(bars), cfg, cost
                )
                expected_entry = 110 * (1 + side * cost.slip)
                expected_exit = expected_entry * (1 - side * .01) * (1 - side * cost.slip)
                expected_pnl = side * (expected_exit / expected_entry - 1) - cost.taker * (1 + expected_exit / expected_entry)
                self.assertEqual((release, last, reason), (3, 1, "sl"))
                self.assertAlmostEqual(entry, expected_entry)
                self.assertAlmostEqual(exit, expected_exit)
                self.assertAlmostEqual(pnl, expected_pnl)

    def test_signed_lanes_use_their_own_strict_prefix_and_do_not_mirror_execution_proof(self):
        count = 25
        closes = tuple(range(1, count + 1))
        values = (.01,) * 15 + (-.005,) * 9 + (-1.,)
        history = StageHistory(StageGrid.from_settings({"enabled": True}), closes, closes, values,
                               100, lambda n: 1.5)
        tape = history.root
        negative = ExecWindow(8, "negative", 1, "negative")
        proof = ForwardProof(24, 1.05)
        self.assertFalse(proof.allows(tape, 16))
        self.assertTrue(proof.allows(tape, 25))
        self.assertTrue(terminal_pass(tape, negative, 25, 2.5))
        self.assertTrue(terminal_pass(tape, negative, 26, 2.5))
        self.assertFalse(proof.allows(tape, 26))
        peer = history.child(tape, ((1 << count) - 1) ^ ((1 << 15) - 1))
        self.assertFalse(proof.allows(peer, 25))

    def test_arm_mask_does_not_block_other_arms_or_change_fills(self):
        market = SimpleNamespace(names=["X"], clocks=np.array([120, 180]), max_open=0, first=0,
                                 choices=[[(('p',), ()), (('p',), ())]],
                                 entry=lambda _, side, set_idx, bar: (bar, bar + 1, 100., 101., .01, "tp"))
        picks = np.full((2, 2, 2), encode(0, 0, 0), dtype=np.uint32)
        mask = np.zeros((1, 2, 2, 2), dtype=bool)
        mask[0, 0] = [[True, False], [False, True]]
        runs = MarketReplay.execute(market, {("p", 1): picks}, admit=mask)
        self.assertEqual([[trades[0].first for trades in floor] for floor in runs], [[1, 2], [1, 2]])
        with self.assertRaises(ValueError):
            MarketReplay.execute(market, {("p", 1): picks}, admit=np.empty((2,), dtype=bool))

    def test_block_uses_next_open_and_skips_parent_exit_bar(self):
        market = SimpleNamespace(cost=.001, bars={"X": [(p, p, p, p, 0.) for p in (100, 102, 101, 103)]})
        tr = Trade("X", 1, 0, 0, 0, 0, 3, 100., 101., .01, "tp")
        self.assertEqual(possible_adds(market, [tr], 1, next_open=True), [(1, False, 0, 101), (3, True, 0, 101.)])

    def test_drawdown_does_not_impute_symbol_order_to_the_same_minute(self):
        market = SimpleNamespace(start=0, end=3600, clock=lambda i: i * 60)
        trades = [Trade("A", 1, 0, 0, 0, 0, 1, 100., 99., -.01, "sl"),
                  Trade("B", 1, 0, 0, 0, 0, 1, 100., 102., .02, "tp")]
        self.assertEqual(summarize(market, trades)["settledDrawdown"], 0.)

    def test_archive_rejects_gaps_subminute_stamps_and_impossible_prices(self):
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            file = directory / "X.json"
            first = [0, 100., 101., 99., 100., 0.]
            for stamp, high in ((60_001, 101), (120_000, 101), (60_000, 99)):
                file.write_text(json.dumps([first, [stamp, 100., high, 99., 100., 1.]]))
                with self.assertRaises(ValueError):
                    load_archive(directory, ["X"], 2)
            file.write_text(json.dumps([first, [60_000, 100., 101., 99., 100., 1.]]))
            self.assertEqual(load_archive(directory, ["X"], 2)[1], {"X": [0, 60]})


class ForwardReleaseTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.selection = Path(folder.name) / "selection.json"
        self.case = {"base": 1.5, "final": 2.0, "proof": [24, 1.05], "scope": "fixture", "alignment": "all"}
        data = dict(cases=[self.case], decision={"minimumEvaluationWindows": 2, "minimumPf": 1.0,
                    "minimumClosesPer8Hours": 4, "minimumTotalIndependentCloses": 8,
                    "stressAdditionalPerSideBps": 1})
        self.selection.write_text(json.dumps(data))

    def run_at(self, hour, *, stress=False, empty=False):
        start, end = hour * 3600, (hour + 8) * 3600
        market = SimpleNamespace(start=start, end=end, clock=lambda i: start + i * 60)
        trades = [Trade("X", 1, 0, 0, 0, i * 60, i * 60 + 1, 100., 101., .02 if i < 3 else -.01, "tp")
                  for i in range(4)] if not empty else []
        return {"start": start, "end": end, "prehistory": start - 48 * 3600,
                "label": str(hour), "symbols": ["X"], "inputHashes": {"X": "same-input"},
                "overlayHash": "same-settings", "taker": .0005, "slippage": .00015 if stress else .00005,
                "selectionHash": sha256(self.selection.read_bytes()).hexdigest(), "sourceArchiveChecksumsVerified": True,
                "cases": [{**self.case, **summarize(market, trades)}]}

    def test_release_requires_positive_nonempty_fresh_and_stressed_evidence(self):
        research = [self.run_at(100)]
        evaluation = [self.run_at(80), self.run_at(108)]
        self.assertTrue(assess(self.selection, research, evaluation, self.run_at(108, stress=True))["eligible"])
        no_trades = self.run_at(108, stress=True, empty=True)
        self.assertFalse(assess(self.selection, research, evaluation, no_trades)["eligible"])
        wrong_source = self.run_at(108, stress=True)
        wrong_source["inputHashes"] = {"X": "different-input"}
        with self.assertRaisesRegex(ValueError, "inputHashes"):
            assess(self.selection, research, evaluation, wrong_source)
        with self.assertRaisesRegex(ValueError, "different slippage"):
            assess(self.selection, research, evaluation, self.run_at(108))
        wrong_case = deepcopy(evaluation)
        wrong_case[-1]["cases"][0]["hourly"][0]["closed"] = 0
        with self.assertRaisesRegex(ValueError, "hourly"):
            assess(self.selection, research, wrong_case, self.run_at(108, stress=True))
        with self.assertRaisesRegex(ValueError, "overlap"):
            assess(self.selection, research, [self.run_at(80), self.run_at(85)], self.run_at(85, stress=True))


if __name__ == "__main__":
    unittest.main()
