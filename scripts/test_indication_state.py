"""State (TA pack) indication: stretch-fade contract and replay lane."""
import pathlib
import random
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from indication_engine import (  # noqa: E402
    DEFAULT_SETTINGS,
    IndicationBook,
    build_indication_frame,
    clamp,
    evaluate_ta_pack,
    evaluate_ta_pack_follow,
)
from set_engine import IND_TAG_KIND, core_pack_votes, indication_kind_votes_frame, indication_signal, votes_to_signal  # noqa: E402


def tape(up_bars=55, down_bars=5, step=0.004, wick=0.0015, base=100.0, sign=1.0):
    """Volatile trend of ``up_bars`` then a counter move of ``down_bars``."""
    rows = []
    for i in range(up_bars + down_bars):
        move = min(i, up_bars - 1) * step - max(0, i - (up_bars - 1)) * step
        c = base * (1 + sign * move)
        rows.append([c, c * (1 + wick), c * (1 - wick), c, 1000.0])
    return rows


def ev(rows, **overrides):
    settings = dict(DEFAULT_SETTINGS)
    settings.update(overrides)
    frame = build_indication_frame(rows, now=1_800_000_000.0)
    return evaluate_ta_pack(frame.candles, settings, frame=frame)


def legacy_follow(rows, settings):
    """Previous State pack formula (reference copy for the follow mode)."""
    frame = build_indication_frame(rows, now=1_800_000_000.0)
    close = frame.candles[-1].close
    rsi_score = clamp((frame.rsi(14) - 50.0) / 30.0, -1, 1)
    macd_score = clamp((frame.ema(12) - frame.ema(26)) / max(close * 0.0008, 1e-9), -1, 1)
    ema_score = clamp((frame.ema(20) - frame.ema(50)) / max(close * 0.001, 1e-9), -1, 1)
    raw = rsi_score * 0.4 + macd_score * 0.3 + ema_score * 0.3
    return ("long" if raw >= 0 else "short"), clamp(0.5 + abs(raw) * 0.45, 0.5, 0.99)


class StateStretchFadeTests(unittest.TestCase):
    def test_stretched_uptrend_that_turns_fades_short(self):
        out = ev(tape())
        self.assertIsNotNone(out)
        self.assertEqual(out.direction, "short")
        self.assertEqual(out.source_id, "ta-rsi-macd-ema")

    def test_stretched_downtrend_that_turns_fades_long(self):
        out = ev(tape(sign=-1.0))
        self.assertIsNotNone(out)
        self.assertEqual(out.direction, "long")

    def test_trend_still_extending_is_not_faded(self):
        # RSI(7) has not turned: no knife catching.
        self.assertIsNone(ev(tape(up_bars=60, down_bars=0)))

    def test_quiet_symbol_is_gated_by_own_volatility(self):
        quiet = tape(step=0.0004, wick=0.0002)
        self.assertIsNone(ev(quiet))
        self.assertIsNotNone(ev(quiet, stateMinAtrPct=0.0))

    def test_stretch_threshold_is_configurable(self):
        self.assertIsNone(ev(tape(), stateStretchAtr=1e6))
        self.assertIsNone(ev(tape(), stateTurnRsi=99.0))

    def test_confidence_calibration(self):
        base = ev(tape())
        self.assertGreaterEqual(base.confidence, 0.52)  # replay tape entry floor
        self.assertLessEqual(base.confidence, 0.99)
        # A larger threshold scales the same stretch to a lower strength.
        wide = ev(tape(), stateStretchAtr=3.0, minimumStrength=0.0, minimumConfidence=0.0)
        self.assertIsNotNone(wide)
        self.assertLessEqual(wide.strength, base.strength)
        self.assertLessEqual(wide.confidence, base.confidence)

    def test_frame_is_bounded_to_last_60_bars(self):
        rows = tape()
        rng = random.Random(7)
        noise = [[p, p * 1.01, p * 0.99, p, 5000.0] for p in (rng.uniform(50, 150) for _ in range(120))]
        a, b = ev(rows), ev(noise + rows)
        self.assertEqual((a.direction, round(a.confidence, 12)), (b.direction, round(b.confidence, 12)))

    def test_follow_mode_keeps_previous_pack(self):
        rows = tape(up_bars=40, down_bars=0, step=0.0012, wick=0.0006)
        settings = dict(DEFAULT_SETTINGS, stateMode="follow", minimumConfidence=0.5, minimumStrength=0.05)
        out = ev(rows, **{k: settings[k] for k in ("stateMode", "minimumConfidence", "minimumStrength")})
        direction, conf = legacy_follow(rows, settings)
        self.assertIsNotNone(out)
        self.assertEqual(out.direction, direction)
        self.assertAlmostEqual(out.confidence, conf, places=12)
        self.assertEqual(out.source_name, "RSI/MACD/EMA pack")

    def test_replay_votes_carry_state_fade(self):
        frame = build_indication_frame(tape(), now=1_800_000_000.0)
        votes = indication_kind_votes_frame(frame, dict(DEFAULT_SETTINGS))
        state = [(d, c) for d, c, tag in votes if IND_TAG_KIND.get(tag) == "state"]
        self.assertEqual(len(state), 1)
        self.assertEqual(state[0][0], -1)
        self.assertGreaterEqual(state[0][1], 0.52)

    def test_core_pack_keeps_previous_follow_vote(self):
        settings = dict(DEFAULT_SETTINGS)
        frame = build_indication_frame(tape(), now=1_800_000_000.0)
        votes = indication_kind_votes_frame(frame, settings)
        pack = core_pack_votes(votes, frame, settings)
        legacy = evaluate_ta_pack_follow(frame, settings)
        ta = [v for v in pack if v[2] == "ta"]
        self.assertEqual(len(ta), 1)
        self.assertEqual(ta[0][:2], (1 if legacy.direction == "long" else -1, float(legacy.confidence)))
        self.assertEqual([v for v in pack if v[2] != "ta"], [v for v in votes if v[2] != "ta"])
        self.assertEqual(indication_signal(tape(), settings, 1_800_000_000.0), votes_to_signal(pack))
        follow = dict(settings, stateMode="follow")
        fvotes = indication_kind_votes_frame(frame, follow)
        self.assertEqual(core_pack_votes(fvotes, frame, follow), fvotes)

    def test_live_book_emits_state_ta_row(self):
        book = IndicationBook()
        rows = book.process("FADE-USDT", tape(), bars_by_tf={"1m": tape()})
        ta = [r for r in rows if r.mode == "ta_pack"]
        self.assertEqual(len(ta), 1)
        self.assertEqual((ta[0].kind, ta[0].direction), ("state", "short"))


if __name__ == "__main__":
    unittest.main()
