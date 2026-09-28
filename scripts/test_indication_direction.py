"""Direction kind: qualified reversal (strong first window, early partial turn, active frame)."""
import math
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from indication_engine import DEFAULT_SETTINGS, evaluate_direction  # noqa: E402

LEGACY = {"dirMaxRetrace": 1e9, "dirMinFirstZ": 0.0, "dirMinSigma": 0.0}


def path(*legs, start=100.0):
    """Closes from (bars, per-bar return) legs."""
    out = [start]
    for n, r in legs:
        for _ in range(n):
            out.append(out[-1] * (1.0 + r))
    return out


def wiggle(closes, amp, seed=1):
    """Deterministic zero-mean noise so the frame has a realistic sigma."""
    out = []
    for i, c in enumerate(closes):
        out.append(c * (1.0 + amp * math.sin(i * 1.7 + seed)))
    return out


class DirectionTests(unittest.TestCase):
    def settings(self, **kw):
        st = dict(DEFAULT_SETTINGS)
        st["dirRange"] = 10
        st["dirMinChange"] = 0.001
        st.update(kw)
        return st

    def test_partial_turn_after_strong_drop_goes_long(self):
        closes = path((40, 0.0), (10, -0.004), (10, 0.0015))
        closes = wiggle(closes, 0.0012)
        row = evaluate_direction("X-USDT", closes, self.settings())
        self.assertIsNotNone(row)
        self.assertEqual(row.direction, "long")
        self.assertEqual(row.kind, "direction")
        self.assertEqual(row.mode, "direction")
        self.assertGreaterEqual(row.confidence, 0.52)

    def test_partial_turn_after_strong_rally_goes_short(self):
        closes = wiggle(path((40, 0.0), (10, 0.004), (10, -0.0015)), 0.0012)
        row = evaluate_direction("X-USDT", closes, self.settings())
        self.assertIsNotNone(row)
        self.assertEqual(row.direction, "short")

    def test_completed_v_is_rejected_but_legacy_settings_accept_it(self):
        closes = wiggle(path((40, 0.0), (10, -0.004), (10, 0.004)), 0.0012)
        self.assertIsNone(evaluate_direction("X-USDT", closes, self.settings()))
        legacy = evaluate_direction("X-USDT", closes, self.settings(**LEGACY))
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy.direction, "long")

    def test_weak_first_move_relative_to_own_volatility_is_rejected(self):
        # First window moves 0.2% while the frame is noisy (sigma ~0.4%/bar): z1 << 0.8.
        closes = wiggle(path((40, 0.0), (10, -0.0002), (10, 0.00005)), 0.004)
        st = self.settings(dirMaxRetrace=1e9)
        self.assertIsNone(evaluate_direction("X-USDT", closes, st))

    def test_quiet_frame_below_sigma_floor_is_rejected(self):
        # Clean drop and partial turn, but per-bar sigma far below 0.12%.
        closes = path((40, 0.0), (10, -0.0006), (10, 0.0002))
        self.assertIsNone(evaluate_direction("X-USDT", closes, self.settings()))
        row = evaluate_direction("X-USDT", closes, self.settings(dirMinSigma=0.0))
        self.assertIsNotNone(row)

    def test_same_sign_windows_never_fire(self):
        closes = wiggle(path((40, 0.0), (20, -0.003)), 0.0012)
        self.assertIsNone(evaluate_direction("X-USDT", closes, self.settings(**LEGACY)))

    def test_confidence_rises_with_first_move_strength(self):
        weak = wiggle(path((40, 0.0), (10, -0.0025), (10, 0.001)), 0.0015)
        strong = wiggle(path((40, 0.0), (10, -0.006), (10, 0.002)), 0.0015)
        a = evaluate_direction("X-USDT", weak, self.settings())
        b = evaluate_direction("X-USDT", strong, self.settings())
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertLess(a.confidence, b.confidence)
        self.assertLessEqual(b.confidence, 0.82)

    def test_uses_only_the_60_bar_frame(self):
        tail = wiggle(path((40, 0.0), (10, -0.004), (10, 0.0015)), 0.0012)
        long_hist = wiggle(path((200, 0.01)), 0.02, seed=5)[:-1] + [c * 1.0 for c in tail]
        a = evaluate_direction("X-USDT", tail[-60:], self.settings())
        b = evaluate_direction("X-USDT", long_hist, self.settings())
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual((a.direction, round(a.confidence, 9)), (b.direction, round(b.confidence, 9)))

    def test_short_history_returns_none(self):
        self.assertIsNone(evaluate_direction("X-USDT", [100.0] * 5, self.settings()))


if __name__ == "__main__":
    unittest.main()
