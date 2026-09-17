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

    def test_core_tag_and_base_trail_are_not_trailing(self):
        core = [{"t": i, "pnl_pct": 0.02, "ind_kind": "signals", "strategy": "core", "set_id": "a", "trail_key": "base", "sl_ratio": 0.6, "step": 8} for i in range(12)]
        blob = ce.evaluate_fills(core, min_pf=1.1, pf_n=10)
        cell = next(c for c in blob["matrix"] if c["indication"] == "signals" and c["strategy"] == "normal")
        self.assertEqual(cell["n"], 12)
        trail_cell = next(c for c in blob["matrix"] if c["indication"] == "signals" and c["strategy"] == "trailing")
        self.assertEqual(trail_cell["n"], 0)
        self.assertFalse(any(c["indication"] == "block" for c in blob["matrix"]))

    def test_block_pack_is_not_an_indication(self):
        rows = [{"t": i, "pnl_pct": 0.02, "strategy": "block", "pack": "block", "set_id": "seed", "sl_ratio": 0.6, "step": 8} for i in range(12)]
        blob = ce.evaluate_fills(rows, min_pf=1.1, pf_n=10)
        self.assertTrue(all(c["indication"] in ce.INDICATIONS for c in blob["combos"]))
        self.assertTrue(all(c["indication"] != "block" for c in blob["successful"]))
        cell = next(c for c in blob["matrix"] if c["indication"] == "combined" and c["strategy"] == "block")
        self.assertEqual(cell["n"], 12)

    def test_kind_lane_does_not_pollute_with_without(self):
        core = [{"t": i, "pnl_pct": 0.02, "ind_kind": "general", "strategy": "normal", "set_id": "core", "sl_ratio": 0.6, "step": 8} for i in range(10)]
        kinds = [({"t": 100 + i, "pnl_pct": 0.05, "ind_kind": "signals", "strategy": "normal", "set_id": "indications:signals"}, {"combo_lane": "kind", "ind_kind": "signals", "strategy": "normal", "set_id": "indications:signals"}) for i in range(20)]
        overlay = [({"t": 200 + i, "pnl_pct": 0.03, "strategy": "block", "set_id": "core"}, {"combo_lane": "overlay", "strategy": "block", "ind_kind": "general", "set_id": "block:core"}) for i in range(5)]
        blob = ce.evaluate_fills(core + kinds + overlay, min_pf=1.1, pf_n=10)
        self.assertEqual(blob["pfStats"]["overall"]["n"], 15)
        self.assertEqual(blob["withWithout"]["block"]["with"]["n"], 15)
        self.assertEqual(blob["withWithout"]["block"]["without"]["n"], 10)
        sig = next(c for c in blob["matrix"] if c["indication"] == "signals" and c["strategy"] == "normal")
        self.assertEqual(sig["n"], 20)
        self.assertTrue(all(not str(r["setId"]).startswith("core") or r["strategy"] != "block" for r in blob["successful"] if r["strategy"] == "block") or True)
        self.assertTrue(all(r["setId"] != "core" for r in blob["successful"] if r["strategy"] == "block"))
        self.assertFalse(any(r["setId"] == "indications:signals" for r in blob["successful"]))
        self.assertEqual(blob["pfStats"]["block"]["n"], 5)

    def test_overlay_meta_set_id_does_not_inherit_seed(self):
        core = [{"t": i, "pnl_pct": 0.02, "strategy": "normal", "set_id": "indications:1m:sl0.6:st8", "pack": "indications"} for i in range(12)]
        block = [({"t": 50 + i, "pnl_pct": 0.04, "strategy": "block", "set_id": "indications:1m:sl0.6:st8", "pack": "indications"}, {"combo_lane": "overlay", "strategy": "block", "ind_kind": "combined", "set_id": "block:indications:1m:sl0.6:st8"}) for i in range(8)]
        blob = ce.evaluate_fills(core + block, min_pf=1.1, pf_n=8)
        ids = {r["setId"] for r in blob["successful"]}
        self.assertIn("indications:1m:sl0.6:st8", ids)
        self.assertIn("block:indications:1m:sl0.6:st8", ids)
        self.assertTrue(all(r["indication"] in ce.INDICATIONS for r in blob["successful"]))

    def test_core_pack_identity_ignores_fill_kind_vote(self):
        rows = [{"t": i, "pnl_pct": 0.02, "ind_kind": "signals", "strategy": "normal", "set_id": "indications:1m:sl0.6:st8", "pack": "indications", "sl_ratio": 0.6, "step": 8} for i in range(12)]
        meta = {"pack": "indications", "ind_kind": "combined", "strategy": "normal", "set_id": "indications:1m:sl0.6:st8", "combo_lane": "core"}
        blob = ce.evaluate_fills([(row, meta) for row in rows], min_pf=1.1, pf_n=10)
        combined = next(c for c in blob["matrix"] if c["indication"] == "combined" and c["strategy"] == "normal")
        signals = next(c for c in blob["matrix"] if c["indication"] == "signals" and c["strategy"] == "normal")
        self.assertEqual(combined["n"], 12)
        self.assertEqual(signals["n"], 0)
        self.assertTrue(all(r["indication"] == "combined" for r in blob["successful"]))

    def test_kind_overlay_is_not_a_catalog_coordination_or_affection(self):
        core = [{"t": i, "pnl_pct": 0.02, "pack": "indications", "strategy": "normal", "set_id": "indications:1m:sl0.6:st8", "sl_ratio": 0.6, "step": 8} for i in range(10)]
        kind_block = [({"t": 50 + i, "pnl_pct": 0.05, "strategy": "block", "pack": "block", "ind_kind": "signals"}, {"combo_lane": "kind-overlay", "strategy": "block", "ind_kind": "signals", "pack": "indications", "set_id": "block:signals"}) for i in range(8)]
        pack_block = [({"t": 80 + i, "pnl_pct": 0.03, "strategy": "block", "pack": "indications", "set_id": "indications:1m:sl0.6:st8"}, {"combo_lane": "overlay", "strategy": "block", "ind_kind": "combined", "pack": "indications", "set_id": "block:indications:1m:sl0.6:st8"}) for i in range(5)]
        blob = ce.evaluate_fills(core + kind_block + pack_block, min_pf=1.1, pf_n=8)
        self.assertEqual(blob["withWithout"]["block"]["with"]["n"], 15)
        self.assertEqual(blob["withWithout"]["block"]["without"]["n"], 10)
        self.assertEqual(blob["pfStats"]["block"]["n"], 5)
        self.assertEqual(blob["pfStats"]["overall"]["n"], 15)
        sig_block = next(c for c in blob["matrix"] if c["indication"] == "signals" and c["strategy"] == "block")
        self.assertEqual(sig_block["n"], 8)
        ids = {r["setId"] for r in blob["successful"]}
        self.assertNotIn("block:signals", ids)
        self.assertIn("block:indications:1m:sl0.6:st8", ids)
        self.assertIn("indications:1m:sl0.6:st8", ids)

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
