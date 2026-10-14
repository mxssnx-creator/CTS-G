"""Base stage: PF floor and DDT cap are both Base-level gates, reported correctly.

A Set qualifies Base only with enough samples, Base PF >= floor, and drawdown
time within the cap. A DDT breach is a Base failure and must never be reported
as a downstream "main sample" shortfall.
"""
import os
import pathlib
import sys
import tempfile
import time
import unittest

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-base-gate-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from set_engine import SetBook, SetState  # noqa: E402

COST = 0.1


def gross(pf):
    return (1.0 + (pf - 1.0) / 0.1) * COST / 100.0


class BaseStageGateTests(unittest.TestCase):
    def book(self, cap):
        ov = dict(setStrictGate=False, setUseHistoricGate=False, entryPolicy="permissive-bounded",
                  minPf=1.15, baseEvalPosCount=30, setPfWindow=30, setDeactN=25,
                  positionCostPct=COST, setMaxDdTimeS=cap)
        b = SetBook()
        b.load(ov, rebuild=False)
        b.progress.ready = True
        st = SetState(id="general:1m:sl0.6:st3", pack="general", tf="1m", sl_ratio=0.6,
                      trail_key="", trail_arm=0, trail_give=0, step=3, tp_pct=0.0045, idx=0)
        b.sets = {st.id: st}
        b.by_idx = [st]
        b._reindex()
        return b, st

    def feed(self, b, st, seq):
        t0 = time.time() - 500000
        for i, (pp, side) in enumerate(seq, 1):
            b.on_live_close({"exchange_confirmed": True, "t": t0 + i * 300, "symbol": "X-USDT", "side": side, "pnl": 1.0,
                             "pnl_pct": pp, "hold_s": 60, "reason": "tp", "set_id": st.id,
                             "strategy": "core", "client_id": f"c{i}", "close_fill_id": f"f{i}"})

    def drawdown_tape(self):
        # 15 losses (75 min underwater) then 15 big wins -> overall PF ~1.9
        return [(-gross(1.30), "LONG")] * 15 + [(gross(1.30) * 6, "LONG")] * 15

    def test_high_pf_but_long_drawdown_is_blocked_by_ddt_at_base(self):
        b, st = self.book(600)  # 10 min cap
        self.feed(b, st, self.drawdown_tape())
        self.assertGreater(st.base_pf, 1.15)
        self.assertEqual(st.stage, "Unqualified")
        self.assertFalse(st.stage_ledger.get("base"))
        reason = st.stage_ledger.get("reason") or ""
        self.assertIn("DDt", reason)
        self.assertNotIn("main sample", reason)  # the real blocker is Base DDT, not Main
        self.assertNotIn("real sample", reason)

    def test_same_tape_qualifies_with_a_generous_cap(self):
        b, st = self.book(57600)
        self.feed(b, st, self.drawdown_tape())
        self.assertTrue(st.stage_ledger.get("base"))

    def test_below_pf_floor_reports_base_pf_not_main(self):
        b, st = self.book(57600)
        self.feed(b, st, [(gross(1.08), "LONG")] * 30)  # PF 1.08 < 1.15
        reason = st.stage_ledger.get("reason") or ""
        self.assertIn("base PF", reason)
        self.assertNotIn("main sample", reason)

    def test_insufficient_samples_report_sample_and_do_not_block_on_ddt(self):
        b, st = self.book(600)
        self.feed(b, st, [(gross(1.30), "LONG")] * 10)  # only 10 < 30 required
        reason = st.stage_ledger.get("reason") or ""
        self.assertIn("sample", reason)
        self.assertNotIn("DDt", reason)  # too few positions: DDT is valid-pending, never the blocker


if __name__ == "__main__":
    unittest.main()
