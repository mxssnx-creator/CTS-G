"""Closed-candle replay and live timeframe identities must share the same evidence."""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
import set_engine as se
from indication_engine import DEFAULT_SETTINGS, IndicationBook, KIND_FLAGS


SYMBOL = "TF-IDENTITY-USDT"
START_MINUTE = 30_000_013  # deliberately not a 5m or 15m boundary


def bars(n=720):
    rows, price = [], 100.0
    for i in range(n):
        move = (0.12 if (i // 160) % 2 == 0 else -0.09) + (i % 7 - 3) * 0.012
        end = price + move
        rows.append([price, max(price, end) + 0.04, min(price, end) - 0.03,
                     end, 1000.0 + (i % 9) * 100])
        price = end
    return rows


def closed_rollup(rows, width):
    """Independent reference: include only complete UTC-aligned buckets."""
    groups = {}
    for index, row in enumerate(rows):
        minute = START_MINUTE + index
        groups.setdefault(minute // width, []).append((minute, row))
    out = []
    for bucket, points in sorted(groups.items()):
        if len(points) != width or points[0][0] != bucket * width:
            continue
        chunk = [p[1] for p in points]
        out.append([chunk[0][0], max(r[1] for r in chunk), min(r[2] for r in chunk),
                    chunk[-1][3], sum(r[4] for r in chunk)])
    return out


def prepared(rows, **overrides):
    book = se.SetBook()
    book.load({"histEnabled": True, "histLookbackBars": len(rows), "histMinBars": 60,
               "histWarmup": 30, "setMinStep": 3, "setStepMax": 3,
               "slToTpRatios": [.6], "stratTrailing": False,
               "stratGeneral": False, "stratIndications": True})
    settings = {**DEFAULT_SETTINGS, **book.ind_settings,
                **{flag: False for flag in KIND_FLAGS.values()},
                "typeSignals": True, "typeState": True, "signalsModel": "trend",
                "minimumConfidence": 0.4, "minimumStrength": 0.05,
                "extraSources": False, **overrides}
    book.ind_settings = settings
    book.ingest_bars(SYMBOL, rows)
    end = (START_MINUTE + len(rows) - 1) * 60.0
    book._bar_end_ts = {SYMBOL: end}
    result = book.prepare_replay_signals(SYMBOL, now=end)
    return book, settings, result


class ReplayTimeframeIdentityTests(unittest.TestCase):
    def test_per_timeframe_signals_and_combined_state_match_live_closed_bars(self):
        rows = bars()
        _, settings, (_, signals, _) = prepared(rows)
        live = IndicationBook()
        live.settings = settings
        for index in (59, 99, 157, 299, 301, 419, 550, 719):
            prefix = rows[:index + 1]
            emitted = live.process(SYMBOL, prefix[-60:], bars_by_tf={
                "1m": prefix[-60:], "5m": closed_rollup(prefix, 5)[-60:],
                "15m": closed_rollup(prefix, 15)[-60:],
            })
            expected = {}
            for row in emitted:
                if row.kind == "signals" and row.mode == "direct_tf":
                    key = f"signals|direct_tf:{row.timeframe}"
                elif row.kind == "state" and row.mode in ("ta_pack", "tf_combined"):
                    key = f"state|{row.mode}"
                else:
                    continue
                expected[key] = (1 if row.direction == "long" else -1, row.confidence)
            for key in ("signals|direct_tf:1m", "signals|direct_tf:5m", "signals|direct_tf:15m",
                        "state|ta_pack", "state|tf_combined"):
                with self.subTest(index=index, key=key):
                    self.assertEqual(signals[key][index], expected.get(key, (0, 0.0)))
        self.assertTrue(all(value == (0, 0.0) for value in signals["signals"]))
        self.assertTrue(all(value == (0, 0.0) for value in signals["state"]))

    def test_future_candles_cannot_change_an_earlier_signal(self):
        rows = bars()
        _, _, (_, full, _) = prepared(rows)
        _, _, (_, prefix, _) = prepared(rows[:420])
        for key in (k for k in prefix if k.startswith(("signals|", "state|"))):
            with self.subTest(key=key):
                self.assertEqual(full[key][100:420], prefix[key][100:420])

    def test_partial_buckets_never_count_toward_the_twenty_candle_minimum(self):
        _, _, (_, signals, _) = prepared(bars())
        for tf, width in (("5m", 5), ("15m", 15)):
            first_complete = (-START_MINUTE) % width
            first_allowed = first_complete + 20 * width - 1
            self.assertTrue(all(value == (0, 0.0)
                                for value in signals[f"signals|direct_tf:{tf}"][:first_allowed]))

    def test_disabled_timeframes_have_no_replay_identity(self):
        _, _, (_, signals, _) = prepared(bars(), tf5m=False, tf15m=False, tfCombined=False)
        self.assertIn("signals|direct_tf:1m", signals)
        self.assertNotIn("signals|direct_tf:5m", signals)
        self.assertNotIn("signals|direct_tf:15m", signals)
        self.assertNotIn("state|tf_combined", signals)

    def test_direction_and_common_use_their_actual_live_modes(self):
        rows = bars(420)
        for mode in ("fade", "follow"):
            _, settings, (_, signals, _) = prepared(
                rows, typeSignals=False, typeState=False, typeDirection=True,
                typeCommon=True, commonMode=mode,
            )
            live = IndicationBook()
            live.settings = settings
            common_key = "common|rsi-macd-ema-bb" if mode == "follow" else "common|ema-stretch-fade"
            self.assertIn("direction|direction", signals)
            self.assertIn(common_key, signals)
            for index in (59, 100, 157, 299, 330, 419):
                emitted = live.process(SYMBOL, rows[max(0, index - 59):index + 1])
                expected = {
                    f"{row.kind}|{row.mode}": (1 if row.direction == "long" else -1, row.confidence)
                    for row in emitted if row.kind in ("direction", "common")
                }
                for key in ("direction|direction", common_key):
                    with self.subTest(mode=mode, index=index, key=key):
                        self.assertEqual(signals[key][index], expected.get(key, (0, 0.0)))
            self.assertTrue(all(value == (0, 0.0) for value in signals["direction"]))
            self.assertTrue(all(value == (0, 0.0) for value in signals["common"]))

    def test_higher_timeframe_evaluators_reuse_the_last_closed_candle(self):
        with patch.object(se, "evaluate_signal_candles", wraps=se.evaluate_signal_candles) as evaluate:
            prepared(bars(), typeState=False)
        sources = [call.args[0] for call in evaluate.call_args_list]
        self.assertGreater(sources.count("bingx-15m"), 0)
        self.assertLessEqual(sources.count("bingx-5m"), 720 // 5)
        self.assertLessEqual(sources.count("bingx-15m"), 720 // 15)

    def test_disabled_consumers_do_not_run_timeframe_evaluators(self):
        with patch.object(se, "evaluate_signal_candles", wraps=se.evaluate_signal_candles) as evaluate:
            prepared(bars(180), typeSignals=False, typeState=False, tfCombined=False)
        evaluate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
