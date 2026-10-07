"""Move fades volatility-normalized extremes with independent range configs."""
import math
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from indication_engine import (  # noqa: E402
    DEFAULT_SETTINGS, IndicationBook, evaluate_move, evaluate_range_configs, move_fade_stats,
)
from set_engine import SetBook  # noqa: E402


def noise(n, amp=0.001, px=100.0):
    """Alternating +/-amp closes: pre-window 1m sigma ~= amp."""
    return [px * (1 + amp if i % 2 else 1.0) for i in range(n)]


def spike(prior, rise, bars=28, last=-0.0005):
    """Prior noise, then a linear `rise` over `bars`, then one bar of `last` return."""
    out = list(prior)
    top = out[-1]
    out += [top * (1 + rise * (i + 1) / bars) for i in range(bars)]
    out.append(out[-1] * (1 + last))
    return out


class MoveFadeTests(unittest.TestCase):
    def setUp(self):
        self.st = dict(DEFAULT_SETTINGS)

    def test_fades_up_spike_short_and_down_spike_long(self):
        up = spike(noise(31), 0.0155)
        row = evaluate_move("M-USDT", up, self.st)
        self.assertIsNotNone(row)
        self.assertEqual((row.kind, row.direction, row.mode), ("move", "short", "move:30"))
        down = spike(noise(31), -0.0155, last=0.0005)
        row = evaluate_move("M-USDT", down, self.st)
        self.assertIsNotNone(row)
        self.assertEqual(row.direction, "long")
        self.assertGreaterEqual(row.confidence, 0.6)
        self.assertLessEqual(row.confidence, 0.7)

    def test_z_uses_prior_sigma_and_band_is_two_to_below_three(self):
        px = spike(noise(31), 0.0155)
        st = move_fade_stats(px, 30)
        prior = px[:31]
        rets = [math.log(prior[i] / prior[i - 1]) for i in range(1, len(prior))]
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        self.assertAlmostEqual(st["sd"], sd, places=12)
        self.assertAlmostEqual(st["z"], abs(px[-1] / px[-30] - 1) / (sd * math.sqrt(29)), places=9)
        self.assertTrue(2.0 <= st["z"] < 3.0)
        # Too small (z < 2) and runaway (z >= 3) moves are both skipped.
        self.assertIsNone(evaluate_move("M-USDT", spike(noise(31), 0.008), self.st))
        self.assertIsNone(evaluate_move("M-USDT", spike(noise(31), 0.04), self.st))
        # The thresholds are settings.
        self.assertIsNotNone(evaluate_move("M-USDT", spike(noise(31), 0.04), {**self.st, "moveZMax": 99}))
        self.assertIsNone(evaluate_move("M-USDT", spike(noise(31), 0.0155), {**self.st, "moveZMin": 2.9}))

    def test_requires_reversal_tick(self):
        still_up = spike(noise(31), 0.0155, last=0.0003)
        self.assertIsNone(evaluate_move("M-USDT", still_up, self.st))
        self.assertIsNotNone(evaluate_move("M-USDT", still_up, {**self.st, "moveConfirm": False}))

    def test_frame_is_bounded_to_last_60_closes(self):
        px = spike(noise(31), 0.0155)
        base = evaluate_move("M-USDT", px, self.st)
        # Wild history older than the 60-bar frame must not change anything.
        older = [50.0, 200.0, 20.0, 400.0] * 25
        row = evaluate_move("M-USDT", older + px, self.st)
        self.assertEqual((row.direction, row.mode, round(row.confidence, 9)),
                         (base.direction, base.mode, round(base.confidence, 9)))
        self.assertEqual(move_fade_stats(older + px, 30), move_fade_stats(px, 30))
        # Not enough pre-window returns inside the frame -> no signal.
        self.assertIsNone(move_fade_stats(px[-35:], 30))

    def test_range_configs_have_stable_ids_and_skip_primary(self):
        px = spike(noise(31), 0.0155)
        rows = [r for r in evaluate_range_configs("M-USDT", px, self.st) if r.kind == "move"]
        self.assertEqual(sorted(r.mode for r in rows), ["move:20", "move:40"])
        self.assertTrue(all(r.direction == "short" for r in rows))
        primary = evaluate_move("M-USDT", px, self.st)
        keys = {r.entry_key for r in rows} | {primary.entry_key}
        self.assertEqual(len(keys), 3)
        off = [r for r in evaluate_range_configs("M-USDT", px, {**self.st, "typeMove": False}) if r.kind == "move"]
        self.assertEqual(off, [])
        book = IndicationBook()
        book.load({"indMoveRanges": [20, 30]})
        self.assertEqual(book.settings["moveRanges"], [20, 30])
        modes = sorted(r.mode for r in book.process("M-USDT", [[p, p, p, p, 1000.0] for p in px]) if r.kind == "move")
        self.assertEqual(modes, ["move:20", "move:30"])

    def test_replay_lanes_present_when_only_move_enabled(self):
        px = spike(noise(31), 0.0155)
        px = px + [px[-1]] * 3
        bars = [[p, p * 1.0002, p * 0.9998, p, 1000.0] for p in px]
        book = SetBook()
        book.load({"histLookbackBars": 120, "histMinBars": 60, "histWarmup": 30, "setMinStep": 3,
                   "setStepMax": 3, "slToTpRatios": [.6], "stratTrailing": False,
                   "indTypeTrend": False, "indTypeBreak": False})
        book.warmup = 30
        book.ingest_bars("M-USDT", bars)
        _, kind_sigs, _ = book.prepare_replay_signals("M-USDT", now=1800000000)
        i = len(px) - 4
        # The primary range (30) is recorded as its own config lane, like live
        # (evaluate_move mode "move:30"); the bare kind lane stays empty.
        self.assertEqual(kind_sigs["move|move:30"][i][0], -1)
        self.assertEqual(kind_sigs["move"][i][0], 0)
        self.assertEqual(kind_sigs["move|move:20"][i][0], -1)
        self.assertEqual(kind_sigs["move|move:40"][i][0], -1)
        self.assertFalse(any(k.startswith(("trend|", "break|")) for k in kind_sigs))


if __name__ == "__main__":
    unittest.main()
