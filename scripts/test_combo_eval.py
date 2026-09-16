#!/usr/bin/env python3
"""Independent combo scoring, in-memory SQLite, and a bounded trade simulation."""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server", "pulse"))

import combo_eval as ce  # noqa: E402
from set_engine import SetBook, synth_trend, IND_KINDS, hist_fill  # noqa: E402


class ComboEvalTests(unittest.TestCase):
    def test_self_test_passes(self):
        rows = ce.self_test()
        failed = [name for name, ok, _ in rows if not ok]
        self.assertFalse(failed, failed)
        self.assertGreaterEqual(len(rows), 14)

    def test_family_and_matrix_include_ddt(self):
        blob = ce.evaluate_fills(
            [{"t": i, "pnl_pct": 0.02, "ind_kind": "signals", "strategy": "normal", "set_id": "a", "ind_config": "c", "sl_ratio": 0.6, "step": 8} for i in range(16)],
            min_pf=1.1,
            pf_n=12,
        )
        self.assertIn("maxDdS", blob["pfStats"]["overall"])
        cell = next(c for c in blob["matrix"] if c["indication"] == "signals" and c["strategy"] == "normal")
        self.assertIn("maxDdS", cell)
        self.assertTrue(blob["successful"])
        self.assertIn("maxDdS", blob["successful"][0])

    def test_independent_configs_do_not_share_pf(self):
        a = [{"t": i, "pnl_pct": 0.02, "ind_kind": "signals", "strategy": "normal", "set_id": "cfg-a", "ind_config": "wide", "sl_ratio": 0.6, "step": 8} for i in range(16)]
        b = [{"t": 100 + i, "pnl_pct": -0.015, "ind_kind": "signals", "strategy": "normal", "set_id": "cfg-b", "ind_config": "tight", "sl_ratio": 2.4, "step": 8} for i in range(16)]
        blob = ce.evaluate_fills(a + b, min_pf=1.1, pf_n=12)
        by_id = {row["setId"]: row for row in blob["combos"]}
        self.assertIn("cfg-a", by_id)
        self.assertGreater(by_id["cfg-a"]["pf"], 1.1)
        self.assertTrue(any(row["setId"] == "cfg-a" for row in blob["successful"]))
        self.assertFalse(any(row["setId"] == "cfg-b" for row in blob["successful"]))
        split_a = ce.evaluate_fills(a, min_pf=1.1, pf_n=12)
        split_b = ce.evaluate_fills(b, min_pf=1.1, pf_n=12)
        self.assertGreater(split_a["pfStats"]["normal"]["pf"], 1.1)
        self.assertLess(split_b["pfStats"]["normal"]["pf"], 1.0)

    def test_with_without_block_and_dca_are_separate_books(self):
        core = [{"t": i, "pnl_pct": 0.01, "ind_kind": "active", "strategy": "normal", "set_id": "core", "sl_ratio": 0.6, "step": 4} for i in range(10)]
        block = [{"t": 50 + i, "pnl_pct": 0.03, "ind_kind": "active", "strategy": "block", "set_id": "core", "reason": "block:active:tp"} for i in range(5)]
        dca = [{"t": 80 + i, "pnl_pct": -0.02, "ind_kind": "active", "strategy": "dca", "set_id": "core", "reason": "dca:add"} for i in range(5)]
        blob = ce.evaluate_fills(core + block + dca, min_pf=1.1, pf_n=10)
        ww = blob["withWithout"]
        self.assertEqual(ww["block"]["with"]["n"], 20)
        self.assertEqual(ww["block"]["without"]["n"], 15)
        self.assertEqual(ww["dca"]["with"]["n"], 20)
        self.assertEqual(ww["dca"]["without"]["n"], 15)
        self.assertNotEqual(ww["block"]["with"]["pf"], ww["block"]["without"]["pf"])
        self.assertEqual(blob["pfStats"]["block"]["n"], 5)
        self.assertEqual(blob["pfStats"]["dca"]["n"], 5)
        self.assertEqual(blob["pfStats"]["overall"]["n"], 20)

    def test_memory_db_never_touches_disk(self):
        db = ce.open_combo_db()
        info = ce.db_pragmas(db)
        self.assertEqual(info["path"], "")
        self.assertIn(info["journal"], ("memory", "off"))
        self.assertEqual(info["tempStore"], "memory")
        self.assertLessEqual(int(info["cachePages"]), -8192)
        db.execute("INSERT INTO combos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("signals", "c", "normal", "s", 0.6, 8, "", 1, 1, 1.4, 60, 0.01, 1))
        db.commit()
        n = db.execute("SELECT COUNT(*) FROM combos").fetchone()[0]
        db.close()
        self.assertEqual(n, 1)

    def test_trade_simulation_book_scores_every_lane(self):
        book = SetBook()
        overlay = {
            "histEnabled": True,
            "histLookbackBars": 180,
            "histMinBars": 60,
            "histWarmup": 40,
            "setMinStep": 8,
            "setStepMax": 8,
            "slToTpRatios": [0.6],
            "stratTrailing": True,
            "stratIndications": True,
            "stratGeneral": True,
            "stratBlock": True,
            "histSimulateBlock": True,
            "histSimulateDca": True,
            "dcaEnabled": False,
            "blockEnabled": True,
            "positionCostPct": 0.10,
            "setMinPf": 1.1,
            "indTypeState": True,
            "indTypeSignals": True,
            "indTypeDirection": True,
            "indTypeMove": True,
            "indTypeActive": True,
            "indTypeCommon": True,
            "indTypeTrend": True,
            "indTypeBreak": True,
        }
        book.load(overlay)
        bars = synth_trend(180, start=80.0, step=0.18, noise=0.03)
        book.ingest_bars("SIM-USDT", bars)
        book.replay_all(symbols=["SIM-USDT"], workers=1, merge=True, score=True)
        blob = ce.evaluate_book(book, min_pf=1.1)
        self.assertTrue(blob["ok"])
        self.assertEqual(blob["meta"]["engine"], "sqlite-memory")
        self.assertEqual(len(blob["matrix"]), len(ce.INDICATIONS) * len(ce.STRATEGIES))
        self.assertGreater(blob["pfStats"]["overall"]["n"], 0)
        self.assertGreaterEqual(blob["withWithout"]["block"]["with"]["n"], blob["withWithout"]["block"]["without"]["n"])
        self.assertTrue(all(row["validated"] and row["pf"] >= 1.1 - 1e-9 for row in blob["successful"]))
        # Independent kinds exist as matrix rows even when a kind is quiet.
        kinds = {cell["indication"] for cell in blob["matrix"]}
        self.assertTrue(set(IND_KINDS).issubset(kinds))
        strategies = {cell["strategy"] for cell in blob["matrix"]}
        self.assertEqual(strategies, set(ce.STRATEGIES))

    def test_compact_hist_row_scores_without_dict_copy(self):
        rows = []
        for i in range(16):
            row = hist_fill(1000 + i, "XRP-USDT", 1, 0.018, 60, "ind:signals:tp", ind_kind="signals")
            row["strategy"] = "normal"
            row["set_id"] = "cfg-compact"
            row["ind_config"] = "wide"
            row["sl_ratio"] = 0.6
            row["step"] = 8
            rows.append(row)
        blob = ce.evaluate_fills(rows, min_pf=1.1, pf_n=10)
        self.assertGreater(blob["pfStats"]["overall"]["n"], 0)
        self.assertTrue(any(r["setId"] == "cfg-compact" for r in blob["combos"]))
        self.assertTrue(all(r.get("indication") and r.get("strategy") and r.get("config") for r in blob["successful"]))

    def test_overlay_meta_does_not_require_row_mutation(self):
        core = [{"t": i, "pnl_pct": 0.02} for i in range(10)]
        meta = {"ind_kind": "trend", "strategy": "trailing", "set_id": "trail-a", "ind_config": "sl0.6:st8:tr0.3", "sl_ratio": 0.6, "step": 8, "trail_key": "0.3:0.1"}
        blob = ce.evaluate_fills([(row, meta) for row in core], min_pf=1.1, pf_n=8)
        cell = next((c for c in blob["matrix"] if c["indication"] == "trend" and c["strategy"] == "trailing"), None)
        self.assertIsNotNone(cell)
        self.assertEqual(cell["n"], 10)
        self.assertGreater(cell["pf"], 1.1)

    def test_high_volume_fills_score_in_memory(self):
        rows = []
        kinds = list(ce.INDICATIONS)
        strats = list(ce.STRATEGIES)
        for i in range(8000):
            rows.append({
                "t": i,
                "pnl_pct": 0.012 if i % 3 else -0.008,
                "ind_kind": kinds[i % len(kinds)],
                "strategy": strats[i % len(strats)],
                "set_id": f"set-{i % 40}",
                "ind_config": f"sl{(i % 5) * 0.3:.1f}:st{(i % 10) + 3}",
                "sl_ratio": ((i % 5) + 1) * 0.3,
                "step": (i % 10) + 3,
            })
        blob = ce.evaluate_fills(rows, min_pf=1.1, pf_n=30)
        self.assertEqual(blob["pfStats"]["overall"]["n"], 8000)
        self.assertEqual(len(blob["matrix"]), len(ce.INDICATIONS) * len(ce.STRATEGIES))
        self.assertGreaterEqual(blob["meta"]["cells"], 40)
        self.assertTrue(all(row["validated"] for row in blob["successful"]))
        self.assertLessEqual(len(blob["combos"]), ce.SUCCESSFUL_CAP)


if __name__ == "__main__":
    unittest.main()
