"""Extended indication kinds (msi / vwap / retest / squeeze) and exit tactics."""
import math
import os
import pathlib
import sys
import tempfile
import unittest

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-ind-ext-test-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))

import indication_engine as ie  # noqa: E402
import set_engine as se  # noqa: E402
from contracts import INDICATION_KINDS  # noqa: E402

S = dict(ie.DEFAULT_SETTINGS)


def frame_from(closes, vols=None, spread=0.0005):
    vols = vols or [100.0] * len(closes)
    cs = []
    prev = closes[0]
    for i, (c, v) in enumerate(zip(closes, vols)):
        hi = max(prev, c) * (1 + spread)
        lo = min(prev, c) * (1 - spread)
        cs.append(ie.Candle(ts=60.0 * i, open=prev, high=hi, low=lo, close=c, volume=v))
        prev = c
    return ie.IndicationFrame(cs, list(closes))


class KindListTests(unittest.TestCase):
    def test_new_kinds_appended_after_break(self):
        self.assertEqual(INDICATION_KINDS[:8], ("state", "signals", "active", "direction", "move", "common", "trend", "break"))
        self.assertEqual(INDICATION_KINDS[8:], ("msi", "vwap", "retest", "squeeze", "sweep", "rsi2", "keltner", "impulse"))
        self.assertEqual(se.IND_KINDS, INDICATION_KINDS)
        self.assertEqual(set(ie.KIND_FLAGS), set(INDICATION_KINDS))

    def test_live_book_reports_every_kind(self):
        book = ie.IndicationBook()
        self.assertEqual(set(book.kind_stats()), set(INDICATION_KINDS))
        self.assertEqual(set(book.snapshot()["types"]), set(INDICATION_KINDS))

    def test_overlay_toggles_and_ranges_load(self):
        book = ie.IndicationBook()
        book.load({"indTypeMsi": False, "indVwapRanges": [12, 24], "indSqueezePctl": 0.3})
        self.assertFalse(book.settings["typeMsi"])
        self.assertEqual(book.settings["vwapRanges"], [12, 24])
        self.assertAlmostEqual(book.settings["squeezePctl"], 0.3)


class EvaluatorTests(unittest.TestCase):
    def test_msi_bearish_divergence(self):
        # Strong first push (RSI high), weaker higher high, then a turn down.
        up1 = [100 + i * 0.30 for i in range(15)]
        dip = [up1[-1] - i * 0.20 for i in range(1, 8)]
        up2 = [dip[-1] + i * 0.12 for i in range(1, 16)]
        closes = [100.0] * 20 + up1 + dip + up2
        closes.append(closes[-1] - 0.05)
        ind = ie.evaluate_msi("T", closes, {**S, "msiRange": 34, "msiMinGap": 3.0})
        self.assertIsNotNone(ind)
        self.assertEqual((ind.kind, ind.direction, ind.exit_tactic), ("msi", "short", "msi-swing-fail"))
        self.assertGreater(ind.exit_level, closes[-1])

    def test_vwap_reversion_on_volume_surge(self):
        closes = [100.0 + 0.02 * math.sin(i) for i in range(40)] + [101.2, 101.0]
        vols = [100.0] * 40 + [800.0, 600.0]
        ind = ie.evaluate_vwap("T", closes, {**S, "vwapRange": 30}, frame_from(closes, vols))
        self.assertIsNotNone(ind)
        self.assertEqual((ind.kind, ind.direction, ind.exit_tactic), ("vwap", "short", "vwap-touch"))
        self.assertLess(ind.exit_level, closes[-1])

    def test_retest_long_holds_broken_level(self):
        base = [100.0 + 0.05 * math.sin(i) for i in range(30)]
        hi = max(base)
        closes = base + [hi * 1.003, hi * 1.004, hi * 1.0006, hi * 1.0004, hi * 1.0012]
        ind = ie.evaluate_retest("T", closes, {**S, "retestRange": 16})
        self.assertIsNotNone(ind)
        self.assertEqual((ind.kind, ind.direction, ind.exit_tactic), ("retest", "long", "retest-fail"))
        self.assertLess(ind.exit_level, hi)

    def test_squeeze_release_breakout(self):
        noisy = [100.0 + (0.4 if i % 2 else -0.4) for i in range(30)]
        tight = [100.0 + (0.02 if i % 2 else -0.02) for i in range(25)]
        closes = noisy + tight + [100.3]
        ind = ie.evaluate_squeeze("T", closes, {**S, "squeezePeriod": 20})
        self.assertIsNotNone(ind)
        self.assertEqual((ind.kind, ind.direction, ind.exit_tactic), ("squeeze", "long", "squeeze-fail"))

    def test_range_configs_carry_one_identity_per_range(self):
        noisy = [100.0 + (0.4 if i % 2 else -0.4) for i in range(30)]
        tight = [100.0 + (0.02 if i % 2 else -0.02) for i in range(25)]
        closes = noisy + tight + [100.3]
        rows = [r for r in ie.evaluate_range_configs("T", closes, S) if r.kind == "squeeze"]
        self.assertTrue(rows)
        self.assertEqual(len({r.entry_key for r in rows}), len(rows))
        off = [r for r in ie.evaluate_range_configs("T", closes, {**S, "typeSqueeze": False}) if r.kind == "squeeze"]
        self.assertEqual(off, [])


class ResearchedKindTests(unittest.TestCase):
    def noisy(self, n=50, base=100.0, amp=0.05):
        return [base + amp * math.sin(i * 1.3) for i in range(n)]

    def test_sweep_short_on_wick_through_high_closing_inside(self):
        closes = self.noisy()
        f = frame_from(closes)
        hi = max(c.high for c in f.candles[-21:])
        prev = closes[-1]
        f.candles.append(ie.Candle(ts=60.0 * 51, open=prev, high=hi * 1.004, low=prev * 0.9995, close=prev * 0.9998, volume=300))
        f.closes.append(prev * 0.9998)
        ind = ie.evaluate_sweep("T", f.closes, {**S, "sweepRange": 20, "actSweepMin": 0.0}, f)
        self.assertIsNotNone(ind)
        self.assertEqual((ind.kind, ind.direction, ind.exit_tactic), ("sweep", "short", "sweep-fail"))
        self.assertGreater(ind.exit_level, hi)

    def test_rsi2_long_pullback_in_uptrend(self):
        closes = [100 + i * 0.05 for i in range(50)] + [102.40, 102.33, 102.27]
        ind = ie.evaluate_rsi2("T", closes, {**S, "rsi2Trend": 34, "actRsi2Min": 0.0}, frame_from(closes))
        self.assertIsNotNone(ind)
        self.assertEqual((ind.kind, ind.direction, ind.exit_tactic), ("rsi2", "long", "ema5-cross"))
        self.assertGreater(ind.exit_level, closes[-1])

    def test_impulse_fade_needs_volume_surge(self):
        closes = self.noisy(45) + [101.5]
        vols = [100.0] * 45 + [600.0]
        ind = ie.evaluate_impulse("T", closes, {**S, "impulseRange": 30, "actImpulseMin": 0.0}, frame_from(closes, vols))
        self.assertIsNotNone(ind)
        self.assertEqual((ind.kind, ind.direction, ind.exit_tactic), ("impulse", "short", "impulse-fail"))
        quiet = ie.evaluate_impulse("T", closes, {**S, "impulseRange": 30, "actImpulseMin": 0.0}, frame_from(closes))
        self.assertIsNone(quiet)

    def test_activity_specialization_blocks_quiet_markets(self):
        closes = self.noisy(45) + [101.5]
        vols = [100.0] * 45 + [600.0]
        self.assertIsNone(ie.evaluate_impulse("T", closes, {**S, "impulseRange": 30, "actImpulseMin": 50.0}, frame_from(closes, vols)))

    def test_keltner_returns_none_or_keltner_mid(self):
        closes = self.noisy(60)
        ind = ie.evaluate_keltner("T", closes, {**S, "keltnerPeriod": 20, "actKeltnerMin": 0.0}, frame_from(closes))
        self.assertTrue(ind is None or ind.exit_tactic == "keltner-mid")

    def test_target_tactics(self):
        self.assertTrue(ie.exit_tactic_hit("ema5-cross", "LONG", 101.0, 100.5))
        self.assertTrue(ie.exit_tactic_hit("keltner-mid", "SHORT", 99.0, 99.5))
        self.assertTrue(ie.exit_tactic_hit("sweep-fail", "SHORT", 100.3, 100.0, 0.1))
        self.assertFalse(ie.exit_tactic_hit("impulse-fail", "LONG", 99.95, 100.0, 0.1))


class ExitTacticTests(unittest.TestCase):
    def test_target_and_invalidations(self):
        hit = ie.exit_tactic_hit
        self.assertTrue(hit("vwap-touch", "SHORT", 99.9, 100.0))
        self.assertFalse(hit("vwap-touch", "SHORT", 100.1, 100.0))
        self.assertTrue(hit("retest-fail", "LONG", 99.8, 100.0, 0.1))
        self.assertFalse(hit("retest-fail", "LONG", 99.95, 100.0, 0.1))
        self.assertTrue(hit("msi-swing-fail", "SHORT", 100.2, 100.0, 0.1))
        self.assertFalse(hit("", "LONG", 1.0, 2.0))
        self.assertFalse(hit("squeeze-fail", "LONG", 99.0, 0.0))

    def test_kind_tape_closes_on_tactic(self):
        book = se.SetBook()
        book.load({"histEnabled": True}, rebuild=False)
        n = 80
        bars = [[100.0, 100.05, 99.95, 100.0, 1.0] for _ in range(n)]
        for i in range(31, n):
            bars[i] = [99.7, 99.75, 99.65, 99.7, 1.0]  # back through the level, inside SL
        sigs = se.KindSignals({"retest|retest:16": [(0, 0.0)] * n})
        sigs["retest|retest:16"][30] = (1, 0.8)
        sigs.exits["retest|retest:16"] = {30: ("retest-fail", 99.9)}
        out = {}
        book.sl_min, book.sl_max = 0.004, 0.03
        book._replay_kind_tapes("T", bars, sigs, out, 10_000.0, 20, 60, 30, True)
        rows = out.get("retest") or []
        self.assertTrue(rows)
        self.assertIn("tactic-retest-fail", rows[0]["reason"])
        book.ind_settings["exitTacticOn"] = False
        out2 = {}
        book._replay_kind_tapes("T", bars, sigs, out2, 10_000.0, 20, 60, 30, True)
        self.assertNotIn("tactic", (out2.get("retest") or [{"reason": ""}])[0]["reason"])

    def test_kind_signals_pickle_keeps_exits(self):
        import pickle
        sigs = se.KindSignals({"a": [(1, 0.5)]})
        sigs.exits["a"] = {0: ("vwap-touch", 1.0)}
        back = pickle.loads(pickle.dumps(sigs))
        self.assertEqual(back.exits, sigs.exits)
        self.assertEqual(dict(back), dict(sigs))


if __name__ == "__main__":
    unittest.main()
