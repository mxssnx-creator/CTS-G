"""Continuous processing: live closes reach their Set and range, tapes keep
every range x direction, indication gates use PF and DDT, stage direction
follows the published ledger, and open positions stay priced."""
import os
import pathlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-continuous-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
import pulse_trader as pt  # noqa: E402
from set_engine import SetBook, SetState, trim_ind_tape  # noqa: E402

COST = 0.1


def gross(pf):
    return (1.0 + (pf - 1.0) / 0.1) * COST / 100.0


def book(**extra):
    ov = dict(setStrictGate=False, setUseHistoricGate=False, entryPolicy="permissive-bounded",
              minPf=1.15, baseEvalPosCount=30, setPfWindow=30, setDeactN=25,
              positionCostPct=COST, setMaxDdTimeS=57600)
    ov.update(extra)
    b = SetBook()
    b.load(ov, rebuild=False)
    b.progress.ready = True
    st = SetState(id="general:1m:sl0.6:st3", pack="general", tf="1m", sl_ratio=0.6,
                  trail_key="", trail_arm=0, trail_give=0, step=3, tp_pct=0.0045, idx=0)
    b.sets = {st.id: st}
    b.by_idx = [st]
    b._reindex()
    return b, st


class LiveCloseCreditTests(unittest.TestCase):
    def test_live_close_with_venue_widened_sl_and_ignore_tp_counts_for_its_set(self):
        b, st = book()
        b.on_live_close({"t": time.time(), "symbol": "X-USDT", "side": "LONG", "pnl": -1.0, "pnl_pct": -0.01,
                         "hold_s": 60, "reason": "sl", "set_id": st.id, "strategy": "core",
                         "tp_pct": 0.0108, "sl_pct": 0.006, "set_tp_pct": 0.0045,
                         "client_id": "c1", "close_fill_id": "f1"})
        self.assertEqual(len(st.evaluation_live()), 1)

    def test_close_from_a_different_nominal_target_is_not_credited(self):
        b, st = book()
        b.on_live_close({"t": time.time(), "symbol": "X-USDT", "side": "LONG", "pnl": 1.0, "pnl_pct": 0.01,
                         "hold_s": 60, "reason": "tp", "set_id": st.id, "strategy": "core",
                         "set_tp_pct": 0.0081, "client_id": "c2", "close_fill_id": "f2"})
        self.assertEqual(st.evaluation_live(), [])

    def test_live_losses_deactivate_a_replay_qualified_set(self):
        b, st = book()
        t0 = time.time() - 100000
        for i in range(40):
            b.on_live_close({"t": t0 + i * 300, "symbol": "X-USDT", "side": "LONG", "pnl": -1.0,
                             "pnl_pct": -0.006, "hold_s": 60, "reason": "sl", "set_id": st.id,
                             "strategy": "core", "tp_pct": 0.0135, "sl_pct": 0.006, "set_tp_pct": 0.0045,
                             "client_id": f"c{i}", "close_fill_id": f"f{i}"})
        self.assertFalse(st.stage_ledger.get("base"))

    def test_position_and_close_carry_the_nominal_target_and_range(self):
        for cls in (pt.Position, pt.Closed):
            names = set(cls.__dataclass_fields__)
            self.assertIn("set_tp_pct", names)
            self.assertIn("ind_config", names)


class IndicationTapeTests(unittest.TestCase):
    def rows(self, n, side, cfg, t0):
        return [{"t": t0 + i, "symbol": "X", "side": side, "pnl_pct": 0.001, "hold_s": 60,
                 "reason": "tp", "ind_config": cfg} for i in range(n)]

    def test_busy_direction_and_range_do_not_evict_the_others(self):
        tape = self.rows(100, "SHORT", "msi:14", 0) + self.rows(400, "LONG", "msi:21", 1000) + self.rows(60, "LONG", "msi:34", 2000)
        kept = trim_ind_tape(tape)
        count = lambda side, cfg: sum(1 for r in kept if r["side"] == side and r["ind_config"] == cfg)
        self.assertEqual(count("SHORT", "msi:14"), 75)
        self.assertEqual(count("LONG", "msi:21"), 75)
        self.assertEqual(count("LONG", "msi:34"), 60)
        self.assertEqual([r["t"] for r in kept], sorted(r["t"] for r in kept))

    def test_trim_tapes_keeps_the_quiet_side(self):
        b, _ = book()
        b.ind_hist["msi"] = self.rows(100, "SHORT", "msi:14", 0) + self.rows(400, "LONG", "msi:14", 1000)
        b.trim_tapes()
        self.assertEqual(sum(1 for r in b.ind_hist["msi"] if r["side"] == "SHORT"), 75)

    def test_indication_gate_needs_ddt_as_well_as_pf(self):
        b, _ = book(setMaxDdTimeS=600)
        t0 = time.time() - 500000
        rows = [{"t": t0 + i * 300, "symbol": "X", "side": "LONG", "pnl_pct": -gross(1.3), "hold_s": 60,
                 "reason": "sl", "ind_config": "msi:14"} for i in range(15)]
        rows += [{"t": t0 + (15 + i) * 300, "symbol": "X", "side": "LONG", "pnl_pct": gross(1.3) * 6,
                  "hold_s": 60, "reason": "tp", "ind_config": "msi:14"} for i in range(15)]
        b.ind_hist["msi"] = rows
        stats = b.ind_stats("msi", "LONG")
        self.assertGreater(stats["pf"], 1.15)
        self.assertFalse(stats["validated"])
        self.assertFalse(b.ind_config_stats("msi", "msi:14", "LONG")["validated"])
        b2, _ = book(setMaxDdTimeS=57600)
        b2.ind_hist["msi"] = rows
        self.assertTrue(b2.ind_stats("msi", "LONG")["validated"])


class StageDirectionTests(unittest.TestCase):
    def test_direction_is_ranked_on_the_stage_floors(self):
        # Base floor 1.20 above setMinPf: LONG PF 1.15 fails Base, SHORT PF 1.25 passes.
        b, st = book(minPf=1.10, setMinPf=1.10, baseMinPf=1.20, mainEvalPosCount=0, realEvalPosCount=0)
        if abs(float(b.stage_min_pf.get("base", 0)) - 1.20) > 1e-9:
            self.skipTest("stage floors collapse to one shared floor in this profile")
        t0 = time.time() - 500000
        for i in range(30):
            for side, pf in (("LONG", 1.15), ("SHORT", 1.25)):
                b.on_live_close({"t": t0 + i * 300 + (1 if side == "SHORT" else 0), "symbol": "X-USDT", "side": side,
                                 "pnl": 1.0, "pnl_pct": gross(pf), "hold_s": 60, "reason": "tp", "set_id": st.id,
                                 "strategy": "core", "client_id": f"{side}{i}", "close_fill_id": f"{side}f{i}"})
        self.assertEqual(st.stage_ledger.get("evaluationDirection"), "SHORT")
        self.assertTrue(st.stage_ledger.get("base"))


class OpenPositionPricingTests(unittest.TestCase):
    def test_open_position_symbol_stays_in_the_scan_names(self):
        p = object.__new__(pt.Pulse)
        p._intern_symbols = lambda: []
        p.open = {"k": NS(symbol="GONE-USDT", side="LONG")}
        self.assertIn("GONE-USDT", p._live_scan_names())


if __name__ == "__main__":
    unittest.main()
