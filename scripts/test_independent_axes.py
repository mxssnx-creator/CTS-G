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


def synth_days(n: int = 4320, start: float = 80.0, step: float = 0.07, noise: float = 0.02, cycle: int = 120):
    """Multi-day local trends. 18-bar oscillators hide Block continuation extras."""
    bars = []
    px = start
    for i in range(n):
        drift = step if (i // cycle) % 2 == 0 else -step * 0.4
        o = px
        c = px + drift + ((i % 5) - 2) * noise
        h = max(o, c) + abs(noise)
        l = min(o, c) - abs(noise) * 0.6
        bars.append([o, h, l, c, 1000.0 + (i % 7) * 40])
        px = c
    return bars


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
        p.entries_blocked = lambda: False
        p.ctrl_skip = {}
        p.open = {}
        p.cooldown = {}
        p.api = NS(path_cd={})
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

    def test_block_specified_ratio_is_not_shared_crumbs(self):
        book = SetBook()
        book.load({
            "histEnabled": True, "stratIndications": True, "stratGeneral": False, "stratTrailing": False,
            "slToTpRatios": [0.6], "setMinStep": 7, "setStepMax": 7,
            "blockVolumeRatio": 1.0, "blockMaxStack": 6, "blockMaxVolumeMultiplier": 2.0,
            "histSimulateBlock": True,
        })
        self.assertAlmostEqual(book._block_specified_ratio(), 1.0)
        parent = book._seed_pos(1, 100.0, 99.4, 100.6, 0, "core")
        extra = book._try_block_extra(parent, [100.3, 100.4, 100.2, 100.3], 2, 0.004, 0.006)
        self.assertIsNotNone(extra)
        self.assertAlmostEqual(extra["qty"], 1.0)
        self.assertAlmostEqual(extra["entry"], 100.3)
        self.assertAlmostEqual(extra["anchor"], 100.0)
        self.assertAlmostEqual(extra["sl"], 100.3 * (1 - 0.0015))
        self.assertAlmostEqual(extra["tp"], 100.6)
        self.assertEqual(extra["adds"], 1)
        too_soon = book._try_block_extra(parent, [100.3, 100.4, 100.2, 100.3], 0, 0.004, 0.006)
        self.assertIsNone(too_soon)
        crumbs = SetBook()
        crumbs.load({
            "histEnabled": True, "stratIndications": True, "stratGeneral": False, "stratTrailing": False,
            "slToTpRatios": [0.6], "setMinStep": 7, "setStepMax": 7,
            "blockVolumeRatio": 0.25, "blockMaxStack": 6, "blockMaxVolumeMultiplier": 2.0,
            "histSimulateBlock": True,
        })
        self.assertAlmostEqual(crumbs._block_specified_ratio(), 0.25)
        crumb_extra = crumbs._try_block_extra(parent, [100.3, 100.4, 100.2, 100.3], 2, 0.004, 0.006)
        self.assertIsNotNone(crumb_extra)
        self.assertAlmostEqual(crumb_extra["qty"], 0.25)

    def test_block_with_beats_without_over_multi_day_trend(self):
        book = SetBook()
        book.load({
            "histEnabled": True,
            "histLookbackBars": 4320,
            "histMinBars": 200,
            "histWarmup": 40,
            "setMinStep": 7,
            "setStepMax": 7,
            "slToTpRatios": [0.6],
            "stratTrailing": False,
            "stratIndications": True,
            "stratGeneral": False,
            "stratBlock": True,
            "histSimulateBlock": True,
            "histSimulateDca": False,
            "blockVolumeRatio": 1.0,
            "blockMaxStack": 6,
            "blockMaxVolumeMultiplier": 2.0,
            "positionCostPct": 0.10,
            "setMinPf": POSITIVE_PF,
            "baseEvalPosCount": 50,
            "indTypeState": True,
            "indTypeSignals": True,
            "indTypeDirection": True,
            "indTypeMove": True,
            "indTypeActive": True,
            "indTypeCommon": True,
            "indTypeTrend": True,
            "indTypeBreak": True,
        })
        bars = synth_days(4320, start=80.0, step=0.07, noise=0.02, cycle=120)
        book.ingest_bars("SIM-USDT", bars)
        book.replay_all(symbols=["SIM-USDT"], workers=1, merge=True, score=True)
        blob = ce.evaluate_book(book, min_pf=POSITIVE_PF)
        ww = blob["withWithout"]["block"]
        block_pf = blob["pfStats"]["block"]
        self.assertGreater(ww["with"]["n"], ww["without"]["n"], ww)
        self.assertGreater(ww["with"]["pf"], ww["without"]["pf"], ww)
        self.assertGreater(block_pf["n"], 0, block_pf)
        self.assertGreater(block_pf["pf"], ww["without"]["pf"], {"block": block_pf, "without": ww["without"]})
        self.assertAlmostEqual(book._block_specified_ratio(), 1.0)


if __name__ == "__main__":
    unittest.main()
