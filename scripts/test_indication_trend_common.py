"""Trend / Common fade lanes: direction, identities, knobs and an independent reference."""
import math
import pathlib
import random
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from indication_engine import (  # noqa: E402
    DEFAULT_SETTINGS,
    Candle,
    evaluate_common,
    evaluate_range_configs,
    evaluate_trend,
)
from set_engine import SetBook  # noqa: E402


def stretched(direction=1, turn=3, n=60):
    """Quiet base, a sharp run, then `turn` closes stepping back against it."""
    rnd = random.Random(3)
    px, out = 100.0, []
    for i in range(n - 17 - turn):
        px *= 1 + rnd.uniform(-0.0006, 0.0006)
        out.append(px)
    for _ in range(17):
        px *= 1 + direction * 0.003
        out.append(px)
    for _ in range(turn):
        px *= 1 - direction * 0.0008
        out.append(px)
    return out


def candles_of(closes):
    return [Candle(i * 60.0, c, c * 1.0005, c * 0.9995, c, 1000.0) for i, c in enumerate(closes)]


def ref_ema_last(values, period):
    a = 2.0 / (period + 1)
    cur = values[0]
    for v in values[1:]:
        cur = v * a + cur * (1 - a)
    return cur


def ref_sigma(values):
    r = [math.log(values[i] / values[i - 1]) for i in range(1, len(values))]
    m = sum(r) / len(r)
    return math.sqrt(max(0.0, sum(x * x for x in r) / len(r) - m * m))


def ref_turned(values, sign, k):
    return all((values[-i] - values[-i - 1]) * sign > 0 for i in range(1, k + 1))


def ref_trend(values, slow, fast, z_min=1.5, turn=3):
    last = values[-1]
    spread = (ref_ema_last(values, fast) - ref_ema_last(values, slow)) / last
    sig = ref_sigma(values)
    if sig <= 0 or spread == 0 or abs(spread) / sig < z_min:
        return 0
    want = -1 if spread > 0 else 1
    return want if ref_turned(values, want, turn) else 0


def ref_common(values, ez=2.0, cz=4.0, turn=2):
    last = values[-1]
    sig = ref_sigma(values)
    e20, e50 = ref_ema_last(values, 20), ref_ema_last(values, 50)
    a, b = (e20 - e50) / last / sig, (last - e50) / last / sig
    buy = int(a <= -ez) + int(b <= -cz)
    sell = int(a >= ez) + int(b >= cz)
    if buy == sell:
        return 0
    d = 1 if buy > sell else -1
    return d if ref_turned(values, d, turn) else 0


def walk(n=900, seed=11):
    """Random walk with volatility bursts and drifts so both lanes fire both ways."""
    rnd = random.Random(seed)
    px, out, drift, vol = 50.0, [], 0.0, 0.001
    for i in range(n):
        if i % 45 == 0:
            drift = rnd.choice((-1, 1)) * rnd.uniform(0.0, 0.0012)
            vol = rnd.uniform(0.0005, 0.0025)
        px *= 1 + drift + rnd.gauss(0, vol)
        out.append(px)
    return out


class TrendFadeTests(unittest.TestCase):
    def test_stretched_rise_that_turns_fades_short_on_every_range(self):
        closes = stretched(+1)
        rows = [r for r in evaluate_range_configs("T-USDT", closes, DEFAULT_SETTINGS) if r.kind == "trend"]
        self.assertEqual({r.mode for r in rows}, {"trend:ema5/13", "trend:ema8/21", "trend:ema13/34"})
        self.assertEqual(len({r.entry_key for r in rows}), 3)
        self.assertTrue(all(r.direction == "short" for r in rows))
        self.assertTrue(all(0.52 <= r.confidence <= 0.99 for r in rows))
        base = evaluate_trend("T-USDT", closes, DEFAULT_SETTINGS)
        self.assertEqual((base.direction, base.mode), ("short", "trend:ema8/21"))

    def test_stretched_drop_that_turns_fades_long(self):
        row = evaluate_trend("T-USDT", stretched(-1), DEFAULT_SETTINGS)
        self.assertIsNotNone(row)
        self.assertEqual(row.direction, "long")

    def test_no_fade_into_a_running_trend_or_a_quiet_market(self):
        self.assertIsNone(evaluate_trend("T-USDT", stretched(+1, turn=0), DEFAULT_SETTINGS))
        self.assertIsNone(evaluate_trend("T-USDT", stretched(+1, turn=2), DEFAULT_SETTINGS))
        rnd = random.Random(5)
        quiet = [100 * (1 + rnd.uniform(-0.0005, 0.0005)) for _ in range(60)]
        self.assertIsNone(evaluate_trend("T-USDT", quiet, DEFAULT_SETTINGS))
        self.assertIsNone(evaluate_trend("T-USDT", [100.0] * 60, DEFAULT_SETTINGS))

    def test_knobs_and_legacy_follow_mode(self):
        closes = stretched(+1, turn=2)
        self.assertIsNotNone(evaluate_trend("T-USDT", closes, {**DEFAULT_SETTINGS, "trendFadeTurn": 2}))
        self.assertIsNone(evaluate_trend("T-USDT", stretched(+1), {**DEFAULT_SETTINGS, "trendFadeZ": 50}))
        rise = [100 * (1.0012 ** i) for i in range(60)]
        legacy = evaluate_trend("T-USDT", rise, {**DEFAULT_SETTINGS, "trendMode": "follow"})
        self.assertEqual(legacy.direction, "long")
        self.assertIsNone(evaluate_trend("T-USDT", rise, DEFAULT_SETTINGS))

    def test_matches_independent_reference_on_random_walk(self):
        px = walk()
        fired = {1: 0, -1: 0}
        for i in range(59, len(px)):
            window = px[i - 59 : i + 1]
            for slow in (13, 21, 34):
                fast = round(slow * 0.38)
                row = evaluate_trend("W", window, {**DEFAULT_SETTINGS, "trendSlow": slow, "trendFast": fast})
                got = 0 if row is None else (1 if row.direction == "long" else -1)
                self.assertEqual(got, ref_trend(window, slow, fast), f"bar {i} slow {slow}")
                if got:
                    fired[got] += 1
        self.assertGreater(fired[1], 0)
        self.assertGreater(fired[-1], 0)

    def test_replay_lanes_keep_range_identities(self):
        book = SetBook()
        book.load({"histLookbackBars": 200, "histMinBars": 60, "histWarmup": 60,
                   "setMinStep": 3, "setStepMax": 3, "slToTpRatios": [.6], "stratTrailing": False})
        px = walk(200, seed=4)
        bars = [[p, p * 1.0008, p * 0.9992, p, 1000.0] for p in px]
        book.ingest_bars("W-USDT", bars)
        _, kinds, _ = book.prepare_replay_signals("W-USDT", now=1_800_000_000)
        keys = {k for k in kinds if k.startswith("trend|")}
        self.assertTrue(keys <= {"trend|trend:ema5/13", "trend|trend:ema8/21", "trend|trend:ema13/34"})
        self.assertTrue(keys)
        for k in keys:
            self.assertTrue(all(c >= 0.52 for d, c in kinds[k] if d))


class CommonFadeTests(unittest.TestCase):
    def test_stretched_drop_that_turns_up_is_long(self):
        row = evaluate_common("C-USDT", candles_of(stretched(-1, turn=2)), DEFAULT_SETTINGS)
        self.assertIsNotNone(row)
        self.assertEqual((row.kind, row.direction, row.mode), ("common", "long", "ema-stretch-fade"))
        self.assertGreaterEqual(row.confidence, 0.54)

    def test_stretched_rise_that_turns_down_is_short(self):
        row = evaluate_common("C-USDT", candles_of(stretched(+1, turn=2)), DEFAULT_SETTINGS)
        self.assertEqual(row.direction, "short")

    def test_no_signal_without_turn_or_stretch(self):
        self.assertIsNone(evaluate_common("C-USDT", candles_of(stretched(-1, turn=0)), DEFAULT_SETTINGS))
        self.assertIsNone(evaluate_common("C-USDT", candles_of(stretched(-1, turn=1)), DEFAULT_SETTINGS))
        rnd = random.Random(8)
        quiet = [100 * (1 + rnd.uniform(-0.0005, 0.0005)) for _ in range(60)]
        self.assertIsNone(evaluate_common("C-USDT", candles_of(quiet), DEFAULT_SETTINGS))
        self.assertIsNone(evaluate_common("C-USDT", candles_of(stretched(-1)[:20]), DEFAULT_SETTINGS))

    def test_knobs_and_legacy_vote_mode(self):
        closes = stretched(-1, turn=2)
        self.assertIsNone(evaluate_common("C-USDT", candles_of(closes),
                                          {**DEFAULT_SETTINGS, "commonFadeEmaZ": 99, "commonFadeCloseZ": 99}))
        self.assertIsNone(evaluate_common("C-USDT", candles_of(closes), {**DEFAULT_SETTINGS, "commonFadeTurn": 3}))
        legacy = evaluate_common("C-USDT", candles_of(closes), {**DEFAULT_SETTINGS, "commonMode": "vote"})
        self.assertEqual(legacy.mode, "rsi-macd-ema-bb")

    def test_matches_independent_reference_on_random_walk(self):
        px = walk(seed=12)
        fired = {1: 0, -1: 0}
        for i in range(59, len(px)):
            window = px[i - 59 : i + 1]
            row = evaluate_common("W", candles_of(window), DEFAULT_SETTINGS)
            got = 0 if row is None else (1 if row.direction == "long" else -1)
            self.assertEqual(got, ref_common(window), f"bar {i}")
            if got:
                fired[got] += 1
        self.assertGreater(fired[1], 0)
        self.assertGreater(fired[-1], 0)


if __name__ == "__main__":
    unittest.main()
