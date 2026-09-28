"""Break indication: fresh reversal break against the 60-bar frame's move.

Research basis (48 BingX symbols, one week of 1m bars, engine kind-tape exits,
parameters chosen on the first four days only): classic N-bar close breaks have
~zero gross edge, while a fresh break that runs against the 59-bar move by at
least ``breakContextSigma`` (default 6) prior-bar sigmas carries a positive
gross edge that held on the later validation and simulation windows.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from indication_engine import DEFAULT_SETTINGS, IndicationFrame, evaluate_break, evaluate_range_configs  # noqa: E402

CLASSIC = {"breakContextSigma": 0}


def reversal_up(last_jump=0.3):
    """40-bar decline 106 -> 100, 19 bars of basing, then a close above the base."""
    closes = [106.0 - 0.15 * i + (0.03 if i % 2 else -0.03) for i in range(40)]
    base = [100.0, 100.08, 99.96, 100.1, 99.98, 100.12, 100.0, 100.14, 100.02, 100.16,
            100.04, 100.18, 100.06, 100.2, 100.08, 100.22, 100.1, 100.24, 100.12]
    closes += base
    closes.append(max(closes[-16:]) + last_jump)
    assert len(closes) == 60
    return closes


def mirror(closes):
    return [200.0 - c for c in closes]


def rising(n=60, step=0.05):
    return [100.0 + step * i + (0.02 if i % 2 else 0.0) for i in range(n)]


class BreakTests(unittest.TestCase):
    def settings(self, rng=16, **extra):
        return {**DEFAULT_SETTINGS, "breakRange": rng, **extra}

    def test_reversal_break_fires_long_and_short(self):
        row = evaluate_break("R-USDT", reversal_up(), self.settings())
        self.assertIsNotNone(row)
        self.assertEqual((row.kind, row.direction, row.mode), ("break", "long", "break:16"))
        self.assertIn(":rev-", row.sources[0])
        short = evaluate_break("R-USDT", mirror(reversal_up()), self.settings())
        self.assertIsNotNone(short)
        self.assertEqual(short.direction, "short")
        self.assertGreaterEqual(row.confidence, 0.52)  # clears the kind-tape entry floor

    def test_continuation_break_is_classic_only(self):
        closes = rising()
        self.assertIsNone(evaluate_break("T-USDT", closes, self.settings()))
        classic = evaluate_break("T-USDT", closes, self.settings(**CLASSIC))
        self.assertIsNotNone(classic)
        self.assertEqual(classic.direction, "long")
        self.assertNotIn(":rev", classic.sources[0])

    def test_first_break_bar_only(self):
        closes = reversal_up()
        closes = closes[1:] + [closes[-1] + 0.2]  # second consecutive break bar
        self.assertIsNone(evaluate_break("F-USDT", closes, self.settings()))
        self.assertIsNotNone(evaluate_break("F-USDT", closes, self.settings(breakFresh=False)))

    def test_context_threshold_and_frame_bound(self):
        closes = reversal_up()
        self.assertIsNone(evaluate_break("C-USDT", closes, self.settings(breakContextSigma=1e6)))
        # The context needs the full 60-bar frame; shorter frames stay silent.
        self.assertIsNone(evaluate_break("C-USDT", closes[-59:], self.settings()))
        # Only the last 60 bars matter: older history cannot change the signal.
        longer = [150.0 + i for i in range(40)] + closes
        a = evaluate_break("C-USDT", closes, self.settings())
        b = evaluate_break("C-USDT", longer, self.settings())
        self.assertEqual((a.direction, a.sources), (b.direction, b.sources))
        frame = IndicationFrame([], list(closes))
        self.assertEqual(evaluate_break("C-USDT", [], self.settings(), frame).sources, a.sources)

    def test_noise_floor_still_applies(self):
        tiny = reversal_up(last_jump=0.0)
        tiny[-1] = max(tiny[-17:-1]) * (1 + 0.0002)  # 0.02% break is below the 0.04% floor
        self.assertIsNone(evaluate_break("N-USDT", tiny, self.settings()))
        for noise in (0.02, 0.03, 0.05):
            self.assertIsNotNone(evaluate_break("N-USDT", reversal_up(), self.settings(activeNoise=noise)), noise)

    def test_range_configs_keep_stable_mode_ids(self):
        rows = evaluate_range_configs("R-USDT", reversal_up(), {**DEFAULT_SETTINGS, "typeTrend": False})
        modes = {row.mode for row in rows if row.kind == "break"}
        self.assertTrue(modes)
        self.assertTrue(modes <= {"break:8", "break:16", "break:32"})
        classic = evaluate_range_configs("T-USDT", rising(), {**DEFAULT_SETTINGS, **CLASSIC, "typeTrend": False})
        self.assertEqual({row.mode for row in classic}, {"break:8", "break:16", "break:32"})
        self.assertEqual(evaluate_range_configs("T-USDT", rising(), {**DEFAULT_SETTINGS, "typeTrend": False}), [])


if __name__ == "__main__":
    unittest.main()
