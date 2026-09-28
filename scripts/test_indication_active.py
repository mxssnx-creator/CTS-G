"""Active indication: volatility-normalized fade / outbreak lanes as independent configs."""
import math
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from indication_engine import (  # noqa: E402
    ACTIVE_FADE_RANGES,
    ACTIVE_OUTBREAK_RANGES,
    DEFAULT_SETTINGS,
    IndicationBook,
    _active_z_ranges,
    active_model,
    active_z_features,
    evaluate_active,
    evaluate_active_all,
)
from set_engine import SetBook  # noqa: E402


def noisy(n, amp=0.0015, start=100.0):
    """Alternating +/-amp steps: prior sigma ~= amp * 100 %."""
    px = [start]
    for i in range(n - 1):
        px.append(px[-1] * (1 + amp if i % 2 == 0 else 1 - amp))
    return px


def run(px, step, n):
    px = list(px)
    for _ in range(n):
        px.append(px[-1] * (1 + step))
    return px


SETTINGS = {**DEFAULT_SETTINGS, "minimumConfidence": 0.3}


class ActiveFeatureTests(unittest.TestCase):
    def test_sigma_uses_only_prior_steps_inside_the_60_bar_frame(self):
        px = run(noisy(48), 0.004, 12)
        feat = active_z_features(px, 10)
        prior = px[-60:][:-10]
        steps = [(prior[k] - prior[k - 1]) / prior[k - 1] * 100 for k in range(1, len(prior))]
        mean = sum(steps) / len(steps)
        sigma = math.sqrt(sum((s - mean) ** 2 for s in steps) / len(steps))
        self.assertAlmostEqual(feat["sigma"], sigma, places=9)
        signed = (px[-1] - px[-11]) / px[-11] * 100
        self.assertAlmostEqual(feat["signed"], signed, places=9)
        self.assertAlmostEqual(feat["z"], signed / (sigma * math.sqrt(10)), places=9)
        # Bars older than the 60-bar frame never change the evaluation.
        older = [50.0, 500.0, 5.0] * 20 + px
        self.assertEqual(active_z_features(older, 10), feat)

    def test_short_frame_has_no_features(self):
        self.assertIsNone(active_z_features(noisy(25), 10))
        self.assertIsNotNone(active_z_features(noisy(31), 10))


class ActiveLaneTests(unittest.TestCase):
    def test_extreme_run_follows_as_outbreak(self):
        rows = evaluate_active_all("X-USDT", run(noisy(48), 0.004, 12), SETTINGS)
        self.assertTrue(rows)
        self.assertEqual({r.mode for r in rows}, {f"outbreak:{n}" for n in ACTIVE_OUTBREAK_RANGES})
        self.assertTrue(all(r.kind == "active" and r.direction == "long" for r in rows))
        down = evaluate_active_all("X-USDT", run(noisy(48), -0.004, 12), SETTINGS)
        self.assertTrue(down and all(r.direction == "short" for r in down))

    def test_moderate_stretch_is_faded(self):
        rows = evaluate_active_all("X-USDT", run(noisy(50), 0.0016, 10), SETTINGS)
        fades = [r for r in rows if r.mode.startswith("fade:")]
        self.assertTrue(fades)
        self.assertTrue(all(r.direction == "short" for r in fades))
        self.assertFalse([r for r in rows if r.mode.startswith("outbreak:")])
        for r in fades:
            z = abs(active_z_features(run(noisy(50), 0.0016, 10), int(r.mode.split(":")[1]))["z"])
            self.assertTrue(3.0 <= z < 5.0, z)
        up = evaluate_active_all("X-USDT", run(noisy(50), -0.0016, 10), SETTINGS)
        self.assertTrue(up and all(r.direction == "long" for r in up if r.mode.startswith("fade:")))

    def test_quiet_tape_never_fires(self):
        # Huge z relative to a tiny sigma, but the tape is too quiet to pay costs.
        px = run(noisy(48, amp=0.0002), 0.001, 12)
        self.assertGreater(abs(active_z_features(px, 10)["z"]), 5)
        self.assertEqual(evaluate_active_all("Q-USDT", px, SETTINGS), [])
        self.assertEqual(evaluate_active_all("Q-USDT", noisy(60), SETTINGS), [])

    def test_confidence_is_calibrated_and_monotone(self):
        rows = evaluate_active_all("X-USDT", run(noisy(48), 0.004, 12), SETTINGS)
        self.assertTrue(all(0.52 <= r.confidence <= 0.99 for r in rows))
        z = {r.mode: abs(active_z_features(run(noisy(48), 0.004, 12), int(r.mode.split(":")[1]))["z"]) for r in rows}
        ordered = sorted(rows, key=lambda r: z[r.mode])
        self.assertEqual([r.confidence for r in ordered], sorted(r.confidence for r in ordered))
        best = evaluate_active("X-USDT", run(noisy(48), 0.004, 12), SETTINGS)
        self.assertEqual(best.confidence, max(r.confidence for r in rows))

    def test_settings_and_legacy_model(self):
        self.assertEqual(active_model(DEFAULT_SETTINGS), "z")
        self.assertEqual(active_model({"activeModel": "LEGACY"}), "legacy")
        self.assertEqual(_active_z_ranges([12, "x", 12, 1, 60, 4], (8,)), [4, 12])
        self.assertEqual(_active_z_ranges("bad", (8, 10)), [8, 10])
        px = run(noisy(48), 0.004, 12)
        only = evaluate_active_all("X-USDT", px, {**SETTINGS, "activeOutbreakZRanges": [10], "activeFadeRanges": [15]})
        self.assertEqual([r.mode for r in only], ["outbreak:10"])
        self.assertEqual(evaluate_active_all("X-USDT", px, {**SETTINGS, "activeMinSigmaPct": 1.0}), [])
        legacy = [100.0] * 12 + [100.0 + i * 0.02 for i in range(8)] + [100.16 + (i + 1) * 1.2 for i in range(6)]
        rows = evaluate_active_all("L-USDT", legacy, {**SETTINGS, "activeModel": "legacy", "activeMovePct": 0.3})
        self.assertTrue(rows and all(r.mode.startswith("outbreak:") and r.direction == "long" for r in rows))

    def test_each_lane_has_its_own_entry_identity(self):
        book = IndicationBook()
        bars = [[p, p * 1.001, p * 0.999, p, 1000.0] for p in run(noisy(48), 0.004, 12)]
        rows = [r for r in book.process("X-USDT", bars) if r.kind == "active"]
        self.assertEqual(len(rows), len(ACTIVE_OUTBREAK_RANGES))
        self.assertEqual(len({r.entry_key for r in rows}), len(rows))


class ActiveReplayTests(unittest.TestCase):
    def test_replay_trades_the_same_lanes_as_live(self):
        closes = run(noisy(70), 0.004, 10)
        closes = closes + [c * closes[-1] / 100.0 for c in noisy(30)]
        closes = run(closes, 0.0016, 25)
        bars = [[c, c * 1.0005, c * 0.9995, c, 1000.0] for c in closes]
        book = SetBook()
        book.load({"histLookbackBars": len(bars), "histMinBars": 60, "histWarmup": 60,
                   "setMinStep": 3, "setStepMax": 3, "slToTpRatios": [.6], "stratTrailing": False})
        book.ingest_bars("X-USDT", bars)
        _, kind_sigs, warmup = book.prepare_replay_signals("X-USDT", now=1800000000)
        lanes = {k: v for k, v in kind_sigs.items() if k.startswith("active|")}
        self.assertTrue(lanes)
        self.assertFalse(any(d for d, _ in kind_sigs["active"]), "no best-of-ranges tape next to the config lanes")
        for i in range(warmup, len(bars)):
            live = evaluate_active_all("X-USDT", closes[max(0, i - 59): i + 1], book.ind_settings)
            want = {"active|" + r.mode: (1 if r.direction == "long" else -1, r.confidence) for r in live}
            got = {k: v[i] for k, v in lanes.items() if v[i][0]}
            self.assertEqual(got, want, i)


if __name__ == "__main__":
    unittest.main()
