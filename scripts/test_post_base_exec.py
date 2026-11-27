"""Causal scalar oracle for the offline Base → Exec matrix."""
import math
import random
from types import SimpleNamespace
import unittest

import numpy as np
import stage_eval as sg
from post_base_exec import Trade, decode, encode
from post_base_gates import (
    BASE_FLOORS, BASE_WINDOWS, EXEC_FLOORS, EXEC_WINDOWS, BaseMasks, SetDecisions,
    fixed_history, terminal_pass,
)
from post_base_results import summarize
from stage_grid import StageGrid, StageHistory


class FixtureBook:
    stage_grid = StageGrid.from_settings({"enabled": True})
    cost_pct = 0.1

    def stage_params(self, *_):
        return SimpleNamespace(lookback_s=120 * 60)

    def stop_cap_exceeded(self, _):
        return False

    def grid_history(self, _, *, closes, entries, nets):
        return StageHistory(self.stage_grid, closes, entries, nets,
                            self.stage_params().lookback_s, lambda n: 1.25)


def signed(values, floor, negative=False, orientation_required=True):
    gain = math.fsum(v for v in values if v > 0)
    loss = math.fsum(-v for v in values if v < 0)
    if negative:
        gain, loss = loss, gain
    return gain > 0 and gain >= floor * loss and (not orientation_required or gain - loss > 1e-12)


def regular_base(rows, now, win, floor, lookback):
    values = [v for t, _, v in rows if now - lookback <= t < now]
    if len(values) < win.need:
        return False
    tail = values[-win.need:]
    split = win.long + win.short
    return all((signed(tail[:win.long], floor, orientation_required=False),
                signed(tail[win.long:split], floor, win.short_mode == "negative",
                       win.short_mode == "negative"),
                signed(tail[split:], floor, win.last_mode == "negative"),
                signed(tail, floor, orientation_required=False)))


class PostBaseExecTests(unittest.TestCase):
    def test_matrix_matches_independent_scalar_windows(self):
        rng = random.Random(77132)
        book = FixtureBook()
        st = SimpleNamespace(pack="test")
        cf = sg.cost_as_frac(book.cost_pct)
        rows = []
        for i in range(330):
            close = 60 * (i // 2 + 1)
            entry = close - 60 * rng.randint(1, 4)
            # Dense wins and runs of losses exercise both signs and exact ties.
            gross = rng.choice((.012, .007, .005, -.001, -.002, .001, .002))
            rows.append((close, entry, gross - cf))
        end = rows[-1][0]
        clocks = np.arange(end - 2400, end + 120, 240)
        lookback = book.stage_params().lookback_s
        columns = tuple(zip(*[(t, e, net + cf) for t, e, net in rows]))
        rows = [(t, e, gross - cf) for t, e, gross in zip(*columns)]
        any_pass = any_negative = False
        for base_floor in BASE_FLOORS:
            actual = SetDecisions(book, st, columns, clocks, base_floor)
            expected_base = []
            expected_full = np.zeros_like(actual.full)
            expected_pass = np.zeros_like(actual.parents, dtype=bool)
            for w in BASE_WINDOWS:
                current = [regular_base(rows, now, w, base_floor, lookback) for now in clocks]
                expected_base.append(sum(current))
                if not any(current):
                    continue
                child = [(t, v) for t, entry, v in rows if regular_base(rows, entry, w, base_floor, lookback)]
                for i, (now, allowed) in enumerate(zip(clocks, current)):
                    if not allowed:
                        continue
                    values = [v for t, v in child if now - lookback <= t < now]
                    for j, gate in enumerate(EXEC_WINDOWS):
                        if len(values) < gate.short + gate.last:
                            continue
                        expected_full[j, i] = True
                        short = values[-(gate.short + gate.last):-gate.last]
                        last = values[-gate.last:]
                        for k, floor in enumerate(EXEC_FLOORS):
                            ok = signed(short, floor, gate.short_mode == "negative") and signed(
                                last, floor, gate.last_mode == "negative"
                            )
                            expected_pass[k, j, i] |= ok
                            if actual.parents[k, j, i] == BASE_WINDOWS.index(w):
                                self.assertTrue(ok, (base_floor, w, gate, now))
                            any_pass |= ok
                            any_negative |= ok and gate.last_mode == "negative"
            np.testing.assert_array_equal(actual.base_pass, expected_base)
            np.testing.assert_array_equal(actual.full, expected_full)
            np.testing.assert_array_equal(actual.parents >= 0, expected_pass)
        self.assertTrue(any_pass and any_negative)

    def test_both_negative_blocks_qualify_and_same_clock_loss_is_excluded(self):
        book = FixtureBook()
        st = SimpleNamespace(pack="test")
        cf = sg.cost_as_frac(book.cost_pct)
        closes = tuple(60 * i for i in range(1, 11))
        columns = closes, tuple(t - 60 for t in closes), (-.01,) * 9 + (.1,)
        history = fixed_history(book, st, columns, 2.5)
        gate = next(w for w in EXEC_WINDOWS if w == (8, "negative", 1, "negative"))
        for floor in EXEC_FLOORS:
            self.assertFalse(terminal_pass(history.root, gate, closes[-2], floor))
            self.assertTrue(terminal_pass(history.root, gate, closes[-1], floor))
            self.assertFalse(terminal_pass(history.root, gate, closes[-1] + 60, floor))
        self.assertTrue(all(w.short in (8, 12) for w in EXEC_WINDOWS if w.short_mode == "negative"))
        self.assertEqual((len(BASE_WINDOWS), len(EXEC_WINDOWS)), (400, 60))
        self.assertLess(history.root.values[0] + cf, 0)

    def test_shared_base_prefixes_preserve_native_drawdown_and_consistency(self):
        class RiskBook(FixtureBook):
            def stage_params(self, *_):
                return SimpleNamespace(lookback_s=7200, max_dd_s=1800, consistency=1.15)

            def grid_history(self, st, *, closes, entries, nets):
                params = self.stage_params()

                def risk(times, values, gains, losses, now, end, start):
                    return sg.se._base_tail_ok(times, values, gains, losses, now, end, start,
                                               params.max_dd_s, params.consistency)

                return StageHistory(self.stage_grid, closes, entries, nets, params.lookback_s,
                                    lambda size: 1.25, base_risk=risk)

        book, st = RiskBook(), SimpleNamespace(pack="test")
        closes = np.arange(160) * 60
        entries = closes - 180
        gross = np.resize([.007, .001 - 2e-17, -.004, .007, .011, -.002], len(closes))
        probes = np.arange(3600, 9540, 120)
        history = fixed_history(book, st, (closes, entries, gross), 1.5, probes)
        masks = BaseMasks(history, book.stage_params(), 1.5)
        all_members = []
        for window in BASE_WINDOWS:
            native = history.root.child_mask(window, 0)
            self.assertEqual(masks.members(window), native, window)
            all_members.append(native)
        self.assertTrue(any(all_members))

    def test_hourly_settlement_and_marks_reconcile(self):
        market = SimpleNamespace(start=0, end=7200, clock=lambda i: i * 60)
        trades = [
            Trade("X", 1, 0, 9, 3, 1, 60, 100., 101., .010, "tp"),
            Trade("X", -1, 0, 9, 3, 60, 61, 100., 102., -.020, "sl"),
            Trade("Y", 1, 1, 2, 4, 61, 120, 100., 99., -.010, "held"),
        ]
        result = summarize(market, trades, [1.2, 1., 1.], [(0.005, 1), (0., 0), (-.01, 1)])
        self.assertEqual([r["closed"] for r in result["hourly"]], [1, 1])
        self.assertEqual([r["opened"] for r in result["hourly"]], [1, 2])
        self.assertEqual([result[k] for k in ("closed", "held", "adds", "heldAdds")], [2, 1, 1, 1])
        self.assertAlmostEqual(result["pf"], .018 / .020)
        self.assertAlmostEqual(result["settledDrawdown"], .02)
        self.assertAlmostEqual(result["heldMarkNet"], -.02)
        self.assertEqual(result["hourSuccessPct"], 50)
        self.assertEqual(decode(encode(5229, 399, 59)), (5229, 399, 59))


if __name__ == "__main__":
    unittest.main()
