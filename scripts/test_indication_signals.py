"""Signals kind: volatility-normalized fade with turn confirmation on full 1m frames."""
import math
import pathlib
import random
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from indication_engine import (  # noqa: E402
    DEFAULT_SETTINGS,
    SIGNALS_REVERT_DEFAULTS,
    build_indication_frame,
    bars_to_candles,
    evaluate_signal_candles,
    signal_revert_features,
)
from set_engine import indication_kind_votes_frame  # noqa: E402


def quiet_then_move(step_pct, turn_pct, n_quiet=40, n_move=19, seed=7, noise=0.0008):
    """Quiet random walk, a steady move of step_pct per bar, then one last bar of turn_pct."""
    rng = random.Random(seed)
    px, bars = 100.0, []

    def push(ret):
        nonlocal px
        o = px
        px = px * (1 + ret)
        hi = max(o, px) * 1.0008
        lo = min(o, px) * 0.9992
        bars.append([o, hi, lo, px, 1000.0 + rng.random() * 100])

    for _ in range(n_quiet):
        push(rng.choice((-1, 1)) * noise * (0.5 + rng.random()))
    for _ in range(n_move):
        push(step_pct)
    push(turn_pct)
    return bars


def settings(**over):
    st = dict(DEFAULT_SETTINGS)
    st.update(over)
    return st


class SignalRevertTests(unittest.TestCase):
    def test_shared_defaults_untouched(self):
        for key in SIGNALS_REVERT_DEFAULTS:
            self.assertNotIn(key, DEFAULT_SETTINGS)

    def test_features_match_manual_formula(self):
        bars = quiet_then_move(0.0015, -0.001)
        closes = [b[3] for b in bars]
        feat = signal_revert_features(closes, 20)
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        before = rets[:39]
        mu = sum(before) / 39
        sd = math.sqrt(sum((r - mu) ** 2 for r in before) / 39)
        z = math.log(closes[-1] / closes[-21]) / (sd * math.sqrt(20))
        self.assertAlmostEqual(feat["z"], z, places=9)
        self.assertAlmostEqual(feat["last"], rets[-1], places=12)
        self.assertIsNone(signal_revert_features(closes[:40], 20))

    def test_stretched_rally_that_turns_is_faded_short(self):
        ev = evaluate_signal_candles("hist-1m", "Historic 1m", bars_to_candles(quiet_then_move(0.0015, -0.001)), settings())
        self.assertIsNotNone(ev)
        self.assertEqual(ev.direction, "short")
        self.assertGreaterEqual(ev.confidence, 0.6)
        self.assertGreaterEqual(ev.confidence, 0.52)  # kind-tape entry floor

    def test_stretched_drop_that_turns_is_faded_long(self):
        ev = evaluate_signal_candles("hist-1m", "Historic 1m", bars_to_candles(quiet_then_move(-0.0015, 0.001)), settings())
        self.assertIsNotNone(ev)
        self.assertEqual(ev.direction, "long")

    def test_no_turn_no_signal(self):
        bars = quiet_then_move(0.0015, 0.0015)
        self.assertIsNone(evaluate_signal_candles("hist-1m", "H", bars_to_candles(bars), settings()))
        ev = evaluate_signal_candles("hist-1m", "H", bars_to_candles(bars), settings(signalsTurnConfirm=False))
        self.assertIsNotNone(ev)
        self.assertEqual(ev.direction, "short")

    def test_small_z_no_signal(self):
        bars = quiet_then_move(0.0015, -0.001)
        self.assertIsNone(evaluate_signal_candles("hist-1m", "H", bars_to_candles(bars), settings(signalsZMin=50.0)))

    def test_atr_band_rejects_dead_and_violent_frames(self):
        dead = quiet_then_move(0.00015, -0.0001, noise=0.00005)
        for b in dead:
            b[1] = max(b[0], b[3]) * 1.00002
            b[2] = min(b[0], b[3]) * 0.99998
        self.assertIsNone(evaluate_signal_candles("hist-1m", "H", bars_to_candles(dead), settings()))
        violent = quiet_then_move(0.012, -0.008, noise=0.006)
        self.assertIsNone(evaluate_signal_candles("hist-1m", "H", bars_to_candles(violent), settings()))

    def test_frame_limited_to_last_60_bars(self):
        bars = quiet_then_move(0.0015, -0.001)
        junk = [[50.0, 80.0, 40.0, 60.0 + i, 5.0] for i in range(40)]
        a = evaluate_signal_candles("hist-1m", "H", bars_to_candles(bars), settings())
        b = evaluate_signal_candles("hist-1m", "H", bars_to_candles(junk + bars), settings())
        self.assertEqual((a.direction, a.confidence, a.strength), (b.direction, b.confidence, b.strength))

    def test_short_frames_keep_legacy_composite_and_ts_is_ignored(self):
        up = [[100 * (1 + max(0, i - 1) * 0.0012), 100 * (1 + i * 0.0012) * 1.0006,
               100 * (1 + max(0, i - 1) * 0.0012) * 0.9994, 100 * (1 + i * 0.0012), 1000 + i] for i in range(80)]
        st = settings(minimumConfidence=0.5, minimumStrength=0.05)
        short = evaluate_signal_candles("bingx-1m", "B", bars_to_candles(up[:40]), st)
        self.assertEqual(short.direction, "long")
        # Timestamps never change the result (prepared frame == public wrapper).
        one = evaluate_signal_candles("bingx-1m", "B", bars_to_candles(up), st)
        five = evaluate_signal_candles("bingx-5m", "B", bars_to_candles(up, period_s=300.0), st)
        self.assertEqual(one, five)
        legacy = evaluate_signal_candles("bingx-1m", "B", bars_to_candles(up), dict(st, signalsModel="trend"))
        self.assertEqual(legacy.direction, "long")

    def test_replay_vote_uses_same_evaluation(self):
        bars = quiet_then_move(0.0015, -0.001)
        frame = build_indication_frame(bars, now=1_800_000_000.0, period_s=60.0)
        st = settings(typeState=False, typeDirection=False, typeMove=False, typeActive=False,
                      typeCommon=False, typeTrend=False, typeBreak=False)
        votes = indication_kind_votes_frame(frame, st)
        ev = evaluate_signal_candles("hist-1m", "Historic 1m", frame.candles, st, weight=0.85, frame=frame)
        self.assertEqual(votes, [(-1, ev.confidence, "sig")])


if __name__ == "__main__":
    unittest.main()
