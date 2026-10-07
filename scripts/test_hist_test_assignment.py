"""Test Historic: symbols and configs are assigned correctly.

Each direction is seeded only from its own replayed Base/Main/Real windows and
re-checked against the live book's gates; a job for another catalog, or a
synthetic job, never gates a live book; the stage windows follow the desk;
positives are never padded with symbols the run did not evaluate.
"""
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-ht-assign-"))
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/pulse"))

import combo_eval as ce  # noqa: E402
import hist_calc  # noqa: E402
import hist_test as ht  # noqa: E402
from set_engine import SetBook  # noqa: E402

PATH_KEYS = ("OUT_DIR", "PUBLIC_JSON", "SUMMARY_PATH", "PUBLIC_SWEEP", "LAST_READY_PATH", "VALIDATED_IDS_PATH")


def live_book(**ov):
    book = SetBook()
    base = {"slToTpRatios": [0.6], "stratTrailing": False, "setMinStep": 8, "setStepMax": 8,
            "baseEvalPosCount": 40, "setPfWindow": 40, "setMinSamples": 30,
            "mainEvalPosCount": 12, "realEvalPosCount": 3, "minPf": 1.2, "setMaxDdTimeS": 64800}
    base.update(ov)
    book.load(base)
    book.progress.ready = True
    book.use_historic_gate = True
    book.strict_gate = True
    return book


def side_ev(pf, n=40, main_n=12, real_n=3, dd=3600.0):
    return {"last15_n": n, "last15_ratio": pf, "n": n * 3, "base_n": n, "base_pf": pf,
            "main_n": main_n, "main_pf": pf, "real_n": real_n, "real_pf": pf, "max_dd_s": dd, "ddOk": True}


class Isolated(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ht-assign-")
        self._saved = {k: getattr(ht, k) for k in PATH_KEYS}
        ht.isolate_outputs(self._tmp)
        ht.clear_stop()
        ht.clear_pause()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(ht, k, v)
        ht.invalidate_job_cache()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def seed(self, sid, by_side, pf=1.31):
        ht.persist_validated_ids([sid], replace=True, evidence={
            sid: {"n": 200, "evalN": 40, "pf": pf, "maxDdS": 3600.0, "bySide": by_side}})
        return {"phase": "ready", "ready": True,
                "successfulConfigs": [{"setId": sid, "validated": True, "pf": pf, "evalN": 40, "n": 200}]}

    @staticmethod
    def admitted(book, st, side):
        return st.id in {row.id for row in book._validated_entry_rows(st.pack, side=side)}


class PerDirectionEvidence(Isolated):
    def test_losing_short_never_inherits_the_long_pass(self):
        book = live_book()
        st = book.by_idx[0]
        job = self.seed(st.id, {"LONG": side_ev(1.6), "SHORT": side_ev(0.94)})
        self.assertEqual(ht.apply_scores_to_book(book, job), [st.id])
        self.assertTrue(st.by_side["LONG"]["active"])
        self.assertFalse(st.by_side["SHORT"]["active"])
        self.assertEqual(st.by_side["SHORT"]["base_pf"], 0.94)
        self.assertTrue(self.admitted(book, st, "LONG"))
        self.assertFalse(self.admitted(book, st, "SHORT"))
        self.assertTrue(st.active)

    def test_direction_without_replay_evidence_stays_closed(self):
        book = live_book()
        st = book.by_idx[0]
        ht.apply_scores_to_book(book, self.seed(st.id, {"LONG": side_ev(1.6)}))
        self.assertTrue(self.admitted(book, st, "LONG"))
        self.assertFalse(self.admitted(book, st, "SHORT"))
        self.assertEqual(st.by_side["SHORT"]["deact_reason"], "hist-test no side evidence")

    def test_evidence_is_rechecked_on_the_live_windows_and_floor(self):
        # Replay Real window 3 cannot satisfy a live Real window of 8.
        book = live_book(realEvalPosCount=8, microEnabled=False)
        st = book.by_idx[0]
        ht.apply_scores_to_book(book, self.seed(st.id, {"LONG": side_ev(1.6, real_n=3), "SHORT": side_ev(1.6, real_n=8)}))
        self.assertFalse(self.admitted(book, st, "LONG"))
        self.assertTrue(self.admitted(book, st, "SHORT"))
        # A live floor above the replayed PF closes the direction (Micro off;
        # with Micro on it would trade at venue-minimum size, tested below).
        book = live_book(minPf=1.35, microEnabled=False)
        st = book.by_idx[0]
        ht.apply_scores_to_book(book, self.seed(st.id, {"LONG": side_ev(1.3), "SHORT": side_ev(1.6)}))
        self.assertFalse(self.admitted(book, st, "LONG"))
        self.assertTrue(self.admitted(book, st, "SHORT"))

    def test_ddt_over_the_live_cap_is_not_seeded(self):
        book = live_book(setMaxDdTimeS=3600)
        st = book.by_idx[0]
        ht.apply_scores_to_book(book, self.seed(st.id, {"LONG": side_ev(1.6, dd=7200.0), "SHORT": side_ev(1.6, dd=600.0)}))
        self.assertFalse(self.admitted(book, st, "LONG"))
        self.assertTrue(self.admitted(book, st, "SHORT"))

    def test_micro_direction_keeps_the_soft_reason_for_the_micro_gate(self):
        book = live_book(microEnabled=True, microMinPf=1.05)
        st = book.by_idx[0]
        ht.apply_scores_to_book(book, self.seed(st.id, {"LONG": side_ev(1.1), "SHORT": side_ev(0.8)}))
        self.assertEqual(st.by_side["LONG"]["deact_reason"], "unproven")
        self.assertEqual(st.by_side["SHORT"]["deact_reason"], "hist-test intern")
        self.assertTrue(self.admitted(book, st, "LONG"))   # Micro tier, venue-minimum size
        self.assertFalse(self.admitted(book, st, "SHORT"))

    def test_run_evidence_carries_each_direction(self):
        st = live_book().by_idx[0]
        st.by_side = {"LONG": {**side_ev(1.6), "live": {"n": 3}, "noise": "x"}, "SHORT": side_ev(0.9), "X": {}}
        ev = ht.side_evidence(st)
        self.assertEqual(set(ev), {"LONG", "SHORT"})
        self.assertEqual(set(ev["LONG"]), set(ht.SIDE_EVIDENCE_KEYS))
        self.assertEqual(ev["SHORT"]["base_pf"], 0.9)


class GateScope(Isolated):
    def test_job_for_another_catalog_does_not_close_the_book(self):
        book = live_book()
        ids = ht.apply_scores_to_book(book, {"phase": "ready", "ready": True,
                                             "validatedIds": ["general:1m:sl9.9:st99", "indications:1m:sl9.9:st98"]})
        self.assertEqual(ids, [])
        self.assertIsNone(book.hist_test_set_ids)

    def test_synthetic_job_never_gates_a_live_book(self):
        book = live_book()
        sid = book.by_idx[0].id
        self.assertIsNone(ht.gate_ids_for_book(book, [sid], {"source": "synth"}))
        ht.apply_scores_to_book(book, {"phase": "ready", "ready": True, "source": "synth", "validatedIds": [sid]})
        self.assertIsNone(book.hist_test_set_ids)

    def test_partial_overlap_keeps_the_allow_list(self):
        book = live_book()
        sid = book.by_idx[0].id
        self.assertEqual(ht.gate_ids_for_book(book, [sid, "general:1m:sl9.9:st99"], {}), [sid, "general:1m:sl9.9:st99"])
        self.assertEqual(ht.gate_ids_for_book(book, [], {}), [])

    def test_synthetic_runs_never_write_the_live_files(self):
        for k, v in self._saved.items():
            setattr(ht, k, v)
        try:
            self.assertTrue(ht.shares_live_outputs())
            with self.assertRaises(RuntimeError):
                ht.run_test({"synth": True, "hours": 4})
            with patch.object(ht, "publish") as pub:
                job = ht.start_test({"synth": True})
            self.assertFalse(job["ok"])
            pub.assert_not_called()
        finally:
            ht.isolate_outputs(self._tmp)
        self.assertFalse(ht.shares_live_outputs())


class ReplayInputs(unittest.TestCase):
    def test_replay_shaping_desk_keys_are_forwarded(self):
        for key in ("setHistTimeBars", "setHonorTp", "exitTacticOn", "exitTacticMinGainPct", "indMoveRanges",
                    "indTypeMsi", "indTypeImpulse", "indKeltnerRanges", "indRsi2Low", "actSweepMin"):
            self.assertIn(key, ht.DESK_OVERLAY_KEYS)
        self.assertEqual(set(ht.DESK_STAGE_KEYS), {"baseEvalPosCount", "setPfWindow", "setMinSamples",
                                                   "mainEvalPosCount", "realEvalPosCount", "setDdtWindow"})
        # PF floors and the DDT cap stay owned by the run.
        for key in ("minPf", "baseMinPf", "setMaxDdTimeS"):
            self.assertNotIn(key, ht.DESK_OVERLAY_KEYS + ht.DESK_STAGE_KEYS)

    def test_combo_validated_needs_the_base_sample(self):
        fills = [{"t": 1000 + i, "pnl_pct": 0.01, "ind_kind": "signals", "strategy": "normal", "set_id": "general:1m:sl0.6:st8",
                  "ind_config": "c1", "sl_ratio": 0.6, "step": 8} for i in range(20)]
        loose = ce.evaluate_fills(fills, min_pf=1.1, cost_pct=0.1, pf_n=30, min_n=8)
        strict = ce.evaluate_fills(fills, min_pf=1.1, cost_pct=0.1, pf_n=30, min_n=30)
        self.assertTrue(loose["successful"])
        self.assertEqual(strict["successful"], [])
        self.assertEqual(strict["minN"], 30)

    def test_calc_panel_toggles_win_over_the_desk_overlay(self):
        desk = {"stratBlock": True, "stratDca": True, "indTypeMsi": True, "indTypeState": True}
        ov = hist_calc.overlay_from_options({"stratBlock": False, "stratDca": False, "indTypeMsi": False}, desk)
        self.assertFalse(ov["stratBlock"])
        self.assertFalse(ov["blockEnabled"])
        self.assertFalse(ov["stratDca"])
        self.assertFalse(ov["indTypeMsi"])
        self.assertTrue(ov["indTypeState"])  # the panel did not send it


class SymbolAssignment(Isolated):
    def test_positives_are_never_padded(self):
        job = {"phase": "ready", "ready": True, "positive": ["PEPE2-USDT"], "symbols": ["PEPE2-USDT"],
               "bySymbol": [{"symbol": "PEPE2-USDT", "n": 40, "pf": 1.5, "validated": True}]}
        out = ht.compact_job(job, [], [], 20, 1.1)
        self.assertEqual(out["positive"], ["PEPE2-USDT"])
        empty = ht.compact_job({"phase": "ready", "ready": True, "positive": [], "symbols": []}, [], [], 20, 1.1)
        self.assertEqual(empty["positive"], [])
        self.assertEqual(len(empty["internSymbols"]), ht.SYMBOL_CAP)  # the intern pool is separate
        majors = ht.compact_job({"phase": "ready", "ready": True, "positive": ["XRP-USDT", "PEPE2-USDT"]}, [], [], 20, 1.1)
        self.assertEqual(majors["positive"], ["XRP-USDT"])

    def test_probe_ids_are_not_persisted(self):
        bars = [[0, 1, 1, 1, 1, 1]] * 200
        rows = [(None, type("St", (), {"id": "general:1m:sl0.6:st8"})(), "LONG", True, False)]
        with patch.object(SetBook, "ingest_bars"), patch.object(SetBook, "replay_all"), \
                patch.object(ht, "symbol_rollup", return_value=[{"symbol": "XRP-USDT", "pf": 1.5, "n": 40, "evalN": 40, "validated": True}]), \
                patch.object(ht, "_rank_set_rows", return_value=rows), \
                patch.object(ht, "persist_validated_ids") as persist:
            fill = ht.fill_positive([{"symbol": "XRP-USDT"}], 1, 1.1, ht.test_overlay(4, 1.1, 8, 8), lambda s, n: bars)
        self.assertEqual([r["symbol"] for r in fill["selected"]], ["XRP-USDT"])
        persist.assert_not_called()


class IndicationGateWhileNotReady(unittest.TestCase):
    def test_existing_evidence_decides_during_a_run(self):
        book = live_book()
        book.progress.ready = False
        self.assertTrue(book.indication_ok("msi", "LONG"))  # no tape at all: defer
        book.ind_hist = {"msi": [{"t": 100 + i, "pnl_pct": -0.004, "side": "LONG", "symbol": "T", "ind_config": "msi|r21"}
                                 for i in range(40)]}
        self.assertFalse(book.indication_ok("msi", "LONG"))
        self.assertFalse(book.indication_ok("msi", "LONG", "msi|r21"))


if __name__ == "__main__":
    unittest.main()
