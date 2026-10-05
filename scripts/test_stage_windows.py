"""Main / Real stage windows: Main last-12 default, Real 0 = gate off, Real N."""
import os
import pathlib
import sys
import tempfile
import unittest

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-stage-win-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))

import connection_profile as cp  # noqa: E402
from coord_engine import Coordinator  # noqa: E402
from set_engine import SetBook  # noqa: E402


def book(**ov):
    b = SetBook()
    b.load(dict(ov), rebuild=False)
    return b


class StageWindowTests(unittest.TestCase):
    def test_main_defaults_to_last_12_real_to_last_3(self):
        b = book()
        self.assertEqual((b.main_eval, b.real_eval), (12, 3))
        self.assertEqual(b._stage_window_ns()[1:], (12, 3))
        c = Coordinator()
        c.load({}, {})
        self.assertEqual((c.main_eval, c.real_eval), (12, 3))

    def test_profile_pins_main_12_real_3(self):
        prof = cp.processing_profile()
        self.assertEqual((prof["mainEvalPosCount"], prof["realEvalPosCount"]), (12, 3))

    def test_real_zero_turns_the_real_gate_off(self):
        b = book(mainEvalPosCount=8, realEvalPosCount=0)
        self.assertEqual(b.real_eval, 0)
        # Real reuses the Main window: Real passes exactly when Main passes.
        self.assertEqual(b._stage_window_ns()[1:], (8, 8))

    def test_real_window_values(self):
        self.assertEqual(book(realEvalPosCount=25)._stage_window_ns()[2], 25)
        self.assertEqual(book(realEvalPosCount=1)._stage_window_ns()[2], 3)
        self.assertEqual(book(realEvalPosCount=None).real_eval, 3)
        self.assertEqual(book(mainEvalPosCount=30)._stage_window_ns()[1], 30)

    def test_real_zero_qualifies_on_main_evidence(self):
        b = book(mainEvalPosCount=5, realEvalPosCount=0, setMinSamples=8, baseEvalPosCount=8,
                 setPfWindow=8, setMinStep=3, setStepMax=3, slToTpRatios=[0.6], minPf=1.15,
                 stratGeneral=True, stratIndications=False, stratTrailing=False, histEnabled=True)
        b._rebuild_sets()
        st = next(x for x in b.by_idx if x.kind == "base")
        st.hist = [{"t": 100 + i, "pnl": 0.002, "pnl_pct": 0.004, "symbol": "T", "side": "LONG",
                    "hold_s": 30, "reason": "tp"} for i in range(8)]
        b._score_one(st)
        self.assertEqual(st.stage, "Real", (st.stage, st.main_pf, st.real_pf))


if __name__ == "__main__":
    unittest.main()
