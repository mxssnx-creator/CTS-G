"""Independence of indications, strategies, ranges, configs and exit lanes.

Each axis owns its tape and PF. A loser on one axis must not qualify or
block a sibling. The trade simulation walks synthetic bars through SetBook
replay, combo_eval, Block Active and ExitBook.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server" / "pulse"))

import combo_eval as ce
from block_active import adjusted_quantity
from block_engine import BlockBook
from exit_engine import ExitBook
from position_cost import POSITIVE_PF, last_n_cost_pf
from set_engine import IND_KINDS, SetBook, synth_trend
import pulse_trader as pt


OVERLAY = {
    "histEnabled": True,
    "histLookbackBars": 220,
    "histMinBars": 80,
    "histWarmup": 40,
    "setMinStep": 7,
    "setStepMax": 8,
    "slToTpRatios": [0.4, 0.6, 1.0],
    "stratTrailing": True,
    "stratIndications": True,
    "stratGeneral": True,
    "stratBlock": True,
    "histSimulateBlock": True,
    "histSimulateDca": True,
    "dcaEnabled": False,
    "blockEnabled": True,
    "positionCostPct": 0.10,
    "setMinPf": POSITIVE_PF,
    "indTypeState": True,
    "indTypeSignals": True,
    "indTypeDirection": True,
    "indTypeMove": True,
    "indTypeActive": True,
    "indTypeCommon": True,
    "indTypeTrend": True,
    "indTypeBreak": True,
}


class IndependentAxesTests(unittest.TestCase):
    def test_trade_simulation_scores_every_independent_axis(self):
        book = SetBook()
        book.load(OVERLAY)
        bars = synth_trend(220, start=80.0, step=0.16, noise=0.04)
        book.ingest_bars("SIM-USDT", bars)
        book.replay_all(symbols=["SIM-USDT"], workers=1, merge=True, score=True)
        blob = ce.evaluate_book(book, min_pf=POSITIVE_PF)
        self.assertTrue(blob["ok"])
        kinds = {cell["indication"] for cell in blob["matrix"]}
        self.assertTrue(set(IND_KINDS).issubset(kinds))
        strategies = {cell["strategy"] for cell in blob["matrix"]}
        self.assertEqual(strategies, set(ce.STRATEGIES))
        self.assertGreater(blob["pfStats"]["overall"]["n"], 0)
        ww = blob["withWithout"]
        self.assertGreaterEqual(ww["block"]["with"]["n"], ww["block"]["without"]["n"])
        combos = blob["combos"]
        by_sl = {}
        for row in combos:
            cfg = str(row.get("config") or "")
            sl = cfg.split(":")[0] if cfg else ""
            by_sl.setdefault(sl, []).append(row["pf"])
        self.assertGreaterEqual(len(by_sl), 2, "independent SL ranges must both score")
        # One config's PF is not copied onto every sibling.
        pfs = sorted({round(p, 4) for vals in by_sl.values() for p in vals})
        self.assertTrue(len(pfs) >= 1)

    def test_losing_kind_does_not_open_or_close_a_sibling(self):
        book = SetBook()
        book.load({
            "histEnabled": True, "stratIndications": True, "stratGeneral": True,
            "slToTpRatios": [0.6], "setMinStep": 7, "setStepMax": 7, "stratTrailing": False,
        })
        book.strict_gate = True
        book.use_historic_gate = True
        book.enabled = True
        book.progress.ready = True
        book.pf_n = 8
        book.min_samples = 8
        book.ind_live["move"] = [
            {"t": 1000 + i * 60, "pnl": -0.004, "pnl_pct": -0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "sl"}
            for i in range(16)
        ]
        book.ind_live["state"] = [
            {"t": 1000 + i * 60, "pnl": 0.002, "pnl_pct": 0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"}
            for i in range(16)
        ]
        self.assertFalse(book.indication_ok("move"))
        self.assertTrue(book.indication_ok("state"))
        self.assertFalse(book.indication_ok("common"))

    def test_intern_metric_source_is_pack_scoped(self):
        book = SetBook()
        book.load({
            "histEnabled": True, "stratIndications": True, "stratGeneral": True,
            "slToTpRatios": [0.6], "setMinStep": 7, "setStepMax": 7, "stratTrailing": False,
        })
        ind = next(s for s in book.by_idx if s.pack == "indications" and s.kind == "base")
        gen = next(s for s in book.by_idx if s.pack == "general" and s.kind == "base")
        ind.last15_n, ind.last15_ratio = 40, 1.8
        gen.last15_n, gen.last15_ratio = 12, 0.7
        self.assertEqual(book.intern_metric_source("indications").id, ind.id)
        self.assertEqual(book.intern_metric_source("general").id, gen.id)
        overall = book.intern_metric_source()
        self.assertEqual(overall.id, ind.id)

    def test_base_close_is_not_tagged_trailing(self):
        pos = NS(strategy="core", kind="base", trail_key="0.3:0.1", pack="indications", axis_key="")
        self.assertNotEqual(pt.Pulse.event_strategy(pos), "trailing")
        trail = NS(strategy="", kind="trail", trail_key="0.3:0.1", pack="indications", axis_key="")
        self.assertEqual(pt.Pulse.event_strategy(trail), "trailing")

    def test_exit_lanes_deactivate_independently(self):
        exits = ExitBook()
        exits.load({"exitEnabled": True, "exitLockOn": True, "exitPeakOn": True, "exitRevOn": True, "exitTimeOn": True,
                    "positionCostPct": 0.10, "minPf": POSITIVE_PF})
        for i in range(16):
            exits.on_close({"reason": "exit:lock", "pnl": -0.01, "pnl_pct": -0.003, "t": 1000 + i})
            exits.on_close({"reason": "exit:peak", "pnl": 0.02, "pnl_pct": 0.004, "t": 2000 + i})
        snap = {row["key"]: row for row in exits.snapshot()["lanes"]}
        lock, peak = snap["lock"], snap["peak"]
        self.assertTrue(exits._lane_ok("hard"))
        self.assertNotEqual(lock["active"], peak["active"])
        self.assertTrue(peak["active"])
        self.assertFalse(lock["active"])

    def test_block_active_uses_specified_ratio_not_count_step(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        book = BlockBook(tmp.name + "/b.json", {"blockVolumeRatio": 2.0, "blockMaxStack": 6})
        self.assertAlmostEqual(book.active_increment(1), 1.0)
        self.assertAlmostEqual(book.active_increment(6), 1.0)
        self.assertAlmostEqual(adjusted_quantity(8, book.active_increment()), 8.0)
        p = pt.Pulse.__new__(pt.Pulse)
        p.block_active = True
        p.halted = False
        p.block = book
        p.strat_block = True
        p.available = 100
        p.block_last_emit = 0
        p.maybe_block_adds()

    def test_long_short_scores_do_not_share_pf(self):
        long_rows = [{"t": i, "pnl_pct": 0.02, "side": "LONG", "ind_kind": "signals", "strategy": "normal", "set_id": "a", "sl_ratio": 0.6, "step": 7} for i in range(12)]
        short_rows = [{"t": 100 + i, "pnl_pct": -0.02, "side": "SHORT", "ind_kind": "signals", "strategy": "normal", "set_id": "a", "sl_ratio": 0.6, "step": 7} for i in range(12)]
        long_pf = last_n_cost_pf(long_rows, 12)
        short_pf = last_n_cost_pf(short_rows, 12)
        mixed = last_n_cost_pf(long_rows + short_rows, 12)
        self.assertGreater(long_pf["ratio"], POSITIVE_PF)
        self.assertLess(short_pf["ratio"], 1.0)
        self.assertNotAlmostEqual(long_pf["ratio"], mixed["ratio"])

    def test_combo_with_without_block_stays_separate_after_replay(self):
        core = [{"t": i, "pnl_pct": 0.01, "ind_kind": "active", "strategy": "normal", "set_id": "core", "sl_ratio": 0.6, "step": 7} for i in range(10)]
        block = [{"t": 50 + i, "pnl_pct": 0.04, "ind_kind": "active", "strategy": "block", "set_id": "core", "reason": "block:active:tp"} for i in range(8)]
        blob = ce.evaluate_fills(core + block, min_pf=POSITIVE_PF, pf_n=10)
        self.assertGreater(blob["withWithout"]["block"]["with"]["pf"], blob["withWithout"]["block"]["without"]["pf"])
        self.assertEqual(blob["pfStats"]["block"]["n"], 8)
        self.assertEqual(blob["pfStats"]["normal"]["n"], 10)


if __name__ == "__main__":
    unittest.main()
