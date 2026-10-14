"""Per-kind Block lanes: every kind with any firing lane gets one, built once.

- combined_kind_signals keeps the strongest lane per bar across a kind's
  bare lane and all its ``kind|config`` range lanes;
- range-only kinds (bare lane silent) still get a ``block:<kind>`` tape;
- a replay of only general-pack Sets (a Historic Calc tile) produces none,
  so tiled callers no longer duplicate them once per pack.
"""
import os
import pathlib
import sys
import tempfile
import time
import unittest

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-kind-block-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))

from set_engine import KindSignals, SetBook, combined_kind_signals, synth_trend  # noqa: E402


class CombinedKindSignals(unittest.TestCase):
    def test_strongest_lane_per_bar_and_range_only_kinds(self):
        lanes = {
            "msi": [(0, 0.0)] * 4,
            "msi|msi:14": [(1, 0.6), (0, 0.0), (-1, 0.55), (0, 0.0)],
            "msi|msi:21": [(-1, 0.7), (1, 0.58), (0, 0.0), (0, 0.0)],
            "trend": [(1, 0.6)] * 4,
            "vwap|vwap:30": [(0, 0.0)] * 4,
        }
        out = combined_kind_signals(lanes)
        self.assertEqual(set(out), {"msi", "trend"})
        self.assertEqual(out["msi"][0], (-1, 0.7, "msi:21"))
        self.assertEqual(out["msi"][1], (1, 0.58, "msi:21"))
        self.assertEqual(out["msi"][2], (-1, 0.55, "msi:14"))
        self.assertEqual(out["msi"][3], (0, 0.0, ""))
        self.assertEqual(out["trend"][0], (1, 0.6, "trend"))


class KindBlockLanesInReplay(unittest.TestCase):
    def book(self):
        b = SetBook()
        b.load({"histEnabled": True, "slToTpRatios": [0.6], "setMinStep": 8, "setStepMax": 8, "stratTrailing": False,
                "stratIndications": True, "stratGeneral": True, "histSimulateBlock": True})
        bars = synth_trend(240, start=100.0, step=0.12, noise=0.05)
        b.lookback = len(bars)
        b.ingest_bars("T-USDT", bars)
        return b, len(b.bars["T-USDT"])

    def prepared(self, n):
        sig = [(0, 0.0, "")] * n
        kinds = KindSignals({"msi": [(0, 0.0)] * n})
        lane = [(0, 0.0)] * n
        for i in range(40, n, 9):
            lane[i] = (1 if (i // 9) % 2 else -1, 0.8)
        kinds["msi|msi:14"] = lane
        return {"indications": sig, "general": sig}, kinds, 30

    def test_range_only_kind_gets_a_block_tape(self):
        b, n = self.book()
        strat = {}
        b._replay_symbol("T-USDT", {}, time.time(), strat_hist=strat, prepared=self.prepared(n))
        self.assertIn("block:msi", strat)

    def test_general_only_tile_produces_no_kind_block_lanes(self):
        b, n = self.book()
        strat = {}
        general = [st.id for st in b.by_idx if st.pack == "general"]
        b._replay_symbol("T-USDT", {}, time.time(), strat_hist=strat, set_ids=general, prepared=self.prepared(n))
        self.assertFalse([k for k in strat if k.startswith("block:")], sorted(strat))


if __name__ == "__main__":
    unittest.main()
