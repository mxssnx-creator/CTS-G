"""No-network Tactic tests: closure, causal regimes and independent identities."""
import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/pulse"))
from indication_engine import DEFAULT_SETTINGS, IndicationBook, KIND_FLAGS, tactic_should_close
from position_cost import net_return_pf
from tactic_indications import aggregate_closed_1m, closed_rows, evaluate_tactic_configs, prepare_tactic_signals

NOW = 1_800_000_000.0  # UTC 30m boundary


def bars(count=1200, minutes=1, sign=1, end=NOW, closes=None):
    px = closes or [100.0 + sign * i * 0.015 for i in range(count)]
    return [[end - len(px) * minutes * 60 + i * minutes * 60,
             p - sign * 0.01, p + 0.1, p - 0.11, p, 100.0]
            for i, p in enumerate(px)]


def signature(rows):
    return [(r.mode, r.timeframe, r.direction, r.entry_key, r.t, r.last_price, r.sources) for r in rows]


class ClosedAggregation(unittest.TestCase):
    def test_utc_buckets_require_all_minutes_and_reject_forming_bars(self):
        raw = bars(count=11, end=NOW + 60)
        result = aggregate_closed_1m(raw, 5, NOW)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(r[0] % 300 == 0 for r in result))
        self.assertTrue(all(r[0] + 300 <= NOW for r in result))
        self.assertEqual(result[-1][5], 500.0)
        self.assertEqual(result[-1][4], raw[-2][4])

    def test_gaps_and_partial_buckets_do_not_create_higher_evidence(self):
        raw = bars(count=12)
        self.assertEqual(len(aggregate_closed_1m(raw, 5, NOW)), 2)
        raw.pop(-3)
        self.assertEqual(len(aggregate_closed_1m(raw, 5, NOW)), 1)

    def test_seconds_milliseconds_and_closed_five_column_tapes_agree(self):
        raw = bars(count=20)
        millis = [[r[0] * 1000, *r[1:]] for r in raw]
        plain = [r[1:] for r in raw]
        expected = aggregate_closed_1m(raw, 5, NOW)
        self.assertEqual(aggregate_closed_1m(millis, 5, NOW), expected)
        self.assertEqual(aggregate_closed_1m(plain, 5, NOW), expected)

    def test_invalid_price_and_timestamp_rows_are_rejected(self):
        valid = bars(count=3)
        broken = [NOW - 60, 100.0, 99.0, 101.0, 100.0, 10.0]
        invalid = [NOW - 60, 100.0, 102.0, 98.0, float("nan"), 10.0]
        self.assertEqual(closed_rows([*valid, broken, invalid], 1, NOW), valid)


class TacticRegimes(unittest.TestCase):
    def settings(self, **changes):
        return {**DEFAULT_SETTINGS, **changes}

    def test_timeframes_ranges_and_directions_have_independent_identity(self):
        settings = self.settings(tacticMethods=["trend"])
        rows = evaluate_tactic_configs("X-USDT", bars(), settings, now_s=NOW)
        independent = [r for r in rows if r.timeframe != "combined"]
        self.assertEqual({r.timeframe for r in independent}, {"1m", "5m", "15m", "30m"})
        self.assertEqual({r.mode for r in independent}, {"tactic:trend:12", "tactic:trend:20", "tactic:trend:32"})
        self.assertEqual(len({r.entry_key for r in independent}), len(independent))
        short = evaluate_tactic_configs("X-USDT", bars(sign=-1), settings, now_s=NOW)
        self.assertTrue(all(r.direction == "short" for r in short))
        self.assertTrue(all(r.direction == "long" for r in rows))
        self.assertFalse({r.entry_key for r in rows} & {r.entry_key for r in short})

    def test_future_input_does_not_change_closed_decision(self):
        raw = bars()
        settings = self.settings()
        expected = evaluate_tactic_configs("X", raw, settings, now_s=NOW)
        future = [NOW, 120.0, 1000.0, 1.0, 2.0, 1_000_000.0]
        got = evaluate_tactic_configs("X", [*raw, future], settings, now_s=NOW)
        self.assertEqual(signature(got), signature(expected))

    def test_actual_higher_bars_keep_their_own_direction(self):
        raw = bars()
        actual5 = bars(count=50, minutes=5, sign=-1)
        result = evaluate_tactic_configs("X", raw,
            self.settings(tacticMethods=["trend"]), {"5m": actual5}, now_s=NOW)
        self.assertTrue(all(r.direction == "short" for r in result if r.timeframe == "5m"))
        self.assertTrue(any(r.direction == "short" and r.timeframe == "5m" for r in result))
        self.assertTrue(all(r.direction == "long" for r in result if r.timeframe == "1m"))

    def test_combined_requires_two_real_timeframes(self):
        settings = self.settings(tacticMethods=["trend"], tacticTf5m=False,
                                  tacticTf15m=False, tacticTf30m=False)
        rows = evaluate_tactic_configs("X", bars(count=40), settings, now_s=NOW)
        self.assertTrue(rows)
        self.assertFalse(any(r.timeframe == "combined" for r in rows))
        combined = evaluate_tactic_configs("X", bars(), self.settings(), now_s=NOW)
        self.assertTrue(any(r.timeframe == "combined" for r in combined))
        for row in (r for r in combined if r.timeframe == "combined"):
            self.assertGreaterEqual(len([s for s in row.sources if s.startswith("tf:")]), 2)

    def test_flat_market_has_no_positive_candidate(self):
        self.assertEqual(evaluate_tactic_configs("X", bars(closes=[100.0] * 40),
            self.settings(tacticMethods=["trend", "mean_reversion"]), now_s=NOW), [])

    def test_mean_reversion_requires_ranging_regime_and_confirmed_turn(self):
        px = [100.0 + (-1 if i % 2 else 1) for i in range(38)] + [104.0, 103.6]
        settings = self.settings(tacticMethods=["mean_reversion"], tacticRanges=[20])
        result = evaluate_tactic_configs("X", bars(closes=px), settings, now_s=NOW)
        self.assertTrue(result)
        self.assertTrue(all(r.direction == "short" and r.exit_tactic == "mean_reversion" for r in result))
        px[-1] = 104.4  # still moving away: fade must not enter
        self.assertEqual(evaluate_tactic_configs("X", bars(closes=px), settings, now_s=NOW), [])

    def test_move_requires_volume_and_pullback_requires_reclaim(self):
        raw = bars(count=40)
        raw[-1][4] += 0.8
        raw[-1][2] = raw[-1][4] + 0.1
        settings = self.settings(tacticMethods=["move"], tacticRanges=[20])
        self.assertEqual(evaluate_tactic_configs("X", raw, settings, now_s=NOW), [])
        raw[-1][5] = 300.0
        self.assertTrue(evaluate_tactic_configs("X", raw, settings, now_s=NOW))
        px = [100.0 + i * 0.1 for i in range(40)]
        px[-2], px[-1] = 103.2, 103.9
        settings = self.settings(tacticMethods=["pullback"], tacticRanges=[20])
        self.assertTrue(evaluate_tactic_configs("X", bars(closes=px), settings, now_s=NOW))

    def test_target_return_pf_is_projected_and_cost_net_once(self):
        rows = evaluate_tactic_configs("X", bars(count=40), self.settings(), now_s=NOW)
        self.assertTrue(rows)
        for row in rows:
            if row.timeframe == "combined":
                continue
            source = next(s for s in row.sources if s.startswith("targetReturnPf="))
            expected = net_return_pf((row.take_profit_pct - 0.1) / 100.0, 0.1)
            self.assertAlmostEqual(float(source.split("=")[1]), expected, places=4)
        expensive = self.settings(positionCostPct=1.0)
        self.assertEqual(evaluate_tactic_configs("X", bars(count=40), expensive, now_s=NOW), [])

    def test_type_toggle_and_timeframe_toggles_stop_only_requested_lane(self):
        self.assertEqual(evaluate_tactic_configs("X", bars(), self.settings(typeTactic=False), now_s=NOW), [])
        rows = evaluate_tactic_configs("X", bars(), self.settings(tacticTf15m=False), now_s=NOW)
        self.assertFalse(any(r.timeframe == "15m" for r in rows))
        self.assertTrue(any(r.timeframe == "1m" for r in rows))


class BookIntegration(unittest.TestCase):
    def test_overlay_load_scan_and_stats_are_wired(self):
        book = IndicationBook()
        book.load({"indTypeTactic": True, "indTacticRanges": [12, 20],
                   "indTacticMethods": ["trend"], "positionCostPct": 0.18})
        self.assertEqual(book.settings["tacticRanges"], [12, 20])
        self.assertEqual(book.settings["positionCostPct"], 0.18)
        self.assertEqual(KIND_FLAGS["tactic"], "typeTactic")
        with patch("time.time", return_value=NOW):
            rows = book.process("X", bars(count=120), bars_by_tf={"1m": bars(count=120)})
        self.assertTrue(any(r.kind == "tactic" for r in rows))
        self.assertIn("tactic", book.kind_stats())
        self.assertGreater(book.kind_stats()["tactic"]["qualified"], 0)
        book.load({"indTypeTactic": False})
        with patch("time.time", return_value=NOW):
            rows = book.process("X", bars(count=120))
        self.assertFalse(any(r.kind == "tactic" for r in rows))
        self.assertFalse(book.snapshot()["types"]["tactic"])


class CachedChronologicalReplay(unittest.TestCase):
    def test_cached_replay_matches_live_at_every_prefix(self):
        raw = bars(count=120)
        settings = {**DEFAULT_SETTINGS, "tacticRanges": [6, 12]}
        cached = prepare_tactic_signals("X", raw, settings, NOW)
        self.assertTrue(cached)
        for i, base in enumerate(raw):
            live = evaluate_tactic_configs("X", raw[:i + 1], settings, now_s=base[0] + 60)
            expected = {}
            for row in live:
                key = row.mode if row.timeframe == "combined" else row.mode + ":" + row.timeframe
                expected[key] = (1 if row.direction == "long" else -1, row.confidence)
            for key, lane in cached.items():
                self.assertEqual(lane[i], expected.get(key, (0, 0.0)), (i, key))

    def test_appended_future_candles_cannot_change_prior_replay(self):
        raw = bars(count=120)
        settings = {**DEFAULT_SETTINGS, "tacticRanges": [6, 12]}
        old = prepare_tactic_signals("X", raw, settings, NOW)
        future = bars(count=60, end=NOW + 3600, sign=-1)
        extended = prepare_tactic_signals("X", [*raw, *future], settings, NOW + 3600)
        for key, lane in old.items():
            self.assertEqual(extended[key][:len(raw)], lane, key)

    def test_higher_timeframe_calculation_only_runs_on_bucket_closes(self):
        import tactic_indications as ti
        raw = bars(count=120)
        settings = {**DEFAULT_SETTINGS, "tacticRanges": [6]}
        counts = {}
        real = ti.evaluate_tactic_configs
        def counted(*args, **kwargs):
            tf = next(iter(kwargs["bars_by_tf"]))
            counts[tf] = counts.get(tf, 0) + 1
            return real(*args, **kwargs)
        with patch.object(ti, "evaluate_tactic_configs", side_effect=counted):
            prepare_tactic_signals("X", raw, settings, NOW)
        self.assertEqual(counts, {"1m": 120, "5m": 24, "15m": 8, "30m": 4})

    def test_legacy_open_anchor_and_timestamped_inputs_have_parity(self):
        raw = bars(count=120)
        settings = {**DEFAULT_SETTINGS, "tacticRanges": [6]}
        expected = prepare_tactic_signals("X", raw, settings, NOW)
        plain = [r[1:] for r in raw]
        anchored = prepare_tactic_signals("X", plain, settings, NOW, end_open_ts=raw[-1][0])
        self.assertEqual(anchored, expected)
        self.assertEqual(anchored.exits, expected.exits)

    def test_mean_reversion_exit_metadata_and_actual_price_close(self):
        px = [100.0 + (-1 if i % 2 else 1) for i in range(38)] + [104.0, 103.6]
        settings = {**DEFAULT_SETTINGS, "tacticMethods": ["mean_reversion"], "tacticRanges": [20]}
        prepared = prepare_tactic_signals("X", bars(closes=px), settings, NOW)
        key = "tactic:mean_reversion:20:1m"
        self.assertEqual(prepared[key][-1][0], -1)
        tactic, level = prepared.exits[key][len(px) - 1]
        self.assertTrue(tactic_should_close(tactic, "SHORT", level, level, px[-1]))
        self.assertFalse(tactic_should_close(tactic, "SHORT", level + 0.01, level, px[-1]))
        self.assertFalse(tactic_should_close(tactic, "SHORT", level, level, level + 0.01),
                         "target must also cover the configured minimum gain")
        self.assertTrue(tactic_should_close("mean_reversion", "LONG", 101, 101, 100))


if __name__ == "__main__":
    unittest.main()
