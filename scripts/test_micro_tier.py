"""Micro tier: positive Sets below the shared floor trade at minimum size.

Ladder: Real > Main > Base > Micro > Unqualified. A Set qualifies as Micro
when it has the full Base sample, DDT within the cap and PF at or above the
Micro floor (default 1.05) but below the shared floor. Strict lanes also need
Main/Real at the Micro floor. Micro entries are minimum size; the Block/DCA
add gates keep re-checking the parent at the shared floor.
"""
import os
import pathlib
import sys
import tempfile
import time
import unittest

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-micro-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from set_engine import SetBook, SetState  # noqa: E402

COST = 0.1


def gross(pf):
    return (1.0 + (pf - 1.0) / 0.1) * COST / 100.0


class MicroTierTests(unittest.TestCase):
    def book(self, strict=False, **extra):
        ov = dict(setStrictGate=strict, setUseHistoricGate=False, entryPolicy="permissive-bounded",
                  minPf=1.15, baseEvalPosCount=30, setPfWindow=30, setDeactN=25,
                  positionCostPct=COST, setMaxDdTimeS=57600, setLiveNegativeDeact=False)
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

    def feed(self, b, st, pf, count=30, side="LONG"):
        t0 = time.time() - 500000
        base = getattr(self, "_i", 0)
        for i in range(base + 1, base + count + 1):
            b.on_live_close({"t": t0 + i * 300, "symbol": "X-USDT", "side": side, "pnl": 1.0,
                             "pnl_pct": gross(pf), "hold_s": 60, "reason": "tp", "set_id": st.id,
                             "strategy": "core", "client_id": f"c{i}", "close_fill_id": f"f{i}"})
        self._i = base + count

    def entry_ids(self, b, side="LONG"):
        return {s.id for s in b.entry_sets("general", side)}

    # --- ladder ---
    def test_pf_in_micro_band_is_micro_not_base(self):
        b, st = self.book()
        self.feed(b, st, 1.10)
        self.assertFalse(st.stage_ledger.get("base"))
        self.assertTrue(st.micro)
        self.assertEqual(st.stage, "Micro")
        self.assertIn("micro PF", st.stage_ledger.get("reason") or "")

    def test_below_micro_floor_is_unqualified(self):
        b, st = self.book()
        self.feed(b, st, 1.02)
        self.assertFalse(st.micro)
        self.assertEqual(st.stage, "Unqualified")
        self.assertNotIn(st.id, self.entry_ids(b))

    def test_regular_pf_stays_base_not_micro(self):
        b, st = self.book()
        self.feed(b, st, 1.30)
        self.assertTrue(st.stage_ledger.get("base"))
        self.assertFalse(st.micro)
        self.assertFalse(b.micro_tier(st, "LONG"))

    # --- entry boundary ---
    def test_nonstrict_micro_enters_and_is_flagged_minimum_size(self):
        b, st = self.book(strict=False)
        self.feed(b, st, 1.10)
        self.assertIn(st.id, self.entry_ids(b))
        self.assertTrue(b.execution_allowed(st, "general", "LONG"))
        self.assertTrue(b.micro_tier(st, "LONG"))

    def test_strict_micro_needs_main_and_real_at_micro_floor(self):
        b, st = self.book(strict=True)
        self.feed(b, st, 1.10)
        self.assertTrue(st.micro, st.stage_ledger.get("reason"))
        self.assertIn(st.id, self.entry_ids(b))
        self.assertTrue(b.execution_allowed(st, "general", "LONG"))
        self.assertTrue(b.micro_tier(st, "LONG"))

    def test_micro_never_reports_main_or_real(self):
        b, st = self.book(strict=True)
        self.feed(b, st, 1.10)
        self.assertFalse(st.stage_ledger.get("main"))
        self.assertFalse(st.stage_ledger.get("real"))
        self.assertEqual(b.qualified_stage_ids("base"), [])

    def test_micro_disabled_restores_base_only_contract(self):
        b, st = self.book(microEnabled=False)
        self.feed(b, st, 1.10)
        self.assertFalse(st.micro)
        self.assertNotIn(st.id, self.entry_ids(b))
        self.assertFalse(b.execution_allowed(st, "general", "LONG"))

    def test_micro_floor_is_tunable_and_never_above_base(self):
        b, _ = self.book(microMinPf=1.08)
        self.assertAlmostEqual(b.micro_min_pf, 1.08)
        b2, _ = self.book(microMinPf=1.35)  # above the 1.15 floor -> clamped to Base
        self.assertLessEqual(b2.micro_min_pf, b2.stage_min_pf["base"])

    def test_hard_deactivation_blocks_micro(self):
        b, st = self.book()
        self.feed(b, st, 1.10)
        st.locked = True
        self.assertNotIn(st.id, self.entry_ids(b))
        self.assertFalse(b.execution_allowed(st, "general", "LONG"))

    def test_coverage_counts_micro(self):
        b, st = self.book()
        self.feed(b, st, 1.10)
        cov = b.coverage()
        self.assertEqual(cov.get("microCount"), 1)
        self.assertTrue(cov.get("microEnabled"))

    def test_micro_recovers_to_base_on_new_evidence(self):
        b, st = self.book()
        self.feed(b, st, 1.10)
        self.assertTrue(st.micro)
        self.feed(b, st, 1.40)
        self.assertTrue(st.stage_ledger.get("base"))
        self.assertFalse(st.micro)
        self.assertFalse(b.micro_tier(st, "LONG"))


if __name__ == "__main__":
    unittest.main()
