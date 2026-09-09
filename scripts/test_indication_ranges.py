"""Configured ranges retain independent signal identities and replay tapes."""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from indication_engine import DEFAULT_SETTINGS, IndicationBook, evaluate_range_configs, indication_ranges
from set_engine import SetBook, slim_hist_row
import pulse_trader as trader


class RangeTests(unittest.TestCase):
    @staticmethod
    def rising(n):
        return [[100+i, 101.6+i, 99.9+i, 101+i, 1000.] for i in range(n)]

    def test_all_six_ranges_and_exact_enabled_subset(self):
        bars = self.rising(100)
        rows = evaluate_range_configs("X-USDT", [b[3] for b in bars], DEFAULT_SETTINGS)
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({row.entry_key for row in rows}), 6)
        book = IndicationBook()
        book.load({"indTrendRanges": [21, 34], "indBreakRanges": [16], "indTypeBreak": False})
        rows = book.process("X-USDT", bars)
        ranges = [row for row in rows if row.kind in ("trend", "break")]
        self.assertEqual({row.mode for row in ranges}, {"trend:ema8/21", "trend:ema13/34"})
        self.assertEqual(indication_ranges([8, 8, "bad", 100, 32], [16]), [8, 32])

    def test_replay_keeps_each_config_and_configuration_change_invalidates_history(self):
        book = SetBook()
        settings = {"histLookbackBars": 120, "histMinBars": 60, "histWarmup": 30,
                    "setMinStep": 3, "setStepMax": 3, "slToTpRatios": [.6], "stratTrailing": False}
        book.load(settings)
        book.ingest_bars("X-USDT", self.rising(120))
        prepared = book.prepare_replay_signals("X-USDT", now=1800000000)
        configs = [key for key in prepared[1] if "|" in key]
        self.assertEqual(len(configs), 6)
        hist, kinds = {}, {}
        book.replay_symbol_partial("X-USDT", hist, now=1800000000, ind_hist=kinds, prepared=prepared)
        self.assertEqual({row.get("ind_config") for row in kinds["trend"] if row.get("ind_config")},
                         {"trend:ema5/13", "trend:ema8/21", "trend:ema13/34"})
        row = next(row for row in kinds["trend"] if row.get("ind_config"))
        self.assertEqual(slim_hist_row(row)["ind_config"], row["ind_config"])
        before = book._hist_set_signature
        book.load({**settings, "indTrendRanges": [21]})
        self.assertNotEqual(before, book._hist_set_signature)

    def test_vst_only_launch_rejects_real_endpoint_before_api_creation(self):
        with patch.dict(trader.os.environ, {"CTS_VST_ONLY": "1"}), \
             patch.object(trader, "BASE", trader.BASE), \
             patch.object(trader, "seed_overlay"), patch.object(trader.os, "makedirs"), \
             patch.object(trader, "redis_hget", side_effect=lambda key: {"base_url": "https://open-api.bingx.com", "api_key": "test", "api_secret": "test"}.get(key, "")), \
             patch.object(trader, "FastBingX") as api:
            with self.assertRaises(SystemExit):
                trader.main()
            api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
