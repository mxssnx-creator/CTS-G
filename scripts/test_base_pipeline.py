"""Base-stage pipeline contract, offline.

User instruction 2026-09-12 (AGENTS.project.md): every Set is evaluated from
its own evidence at Base; only Base-qualified Sets enter downstream
calculations (Main/Real, coordination, entries, Block/DCA) and system result
statistics. Base checks continue for rejected Sets when evidence changes.

The book here uses the production lane shape of both overlays
(setStrictGate=false, setUseHistoricGate=false, permissive-bounded entries,
shared PF floor 1.15, Base last-30).
"""
import pathlib
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from set_engine import SetBook, SetState  # noqa: E402

COST_PCT = 0.1  # PositionCost 0.10% -> PF = 1 + 0.1 * (gross/cost - 1)
PROD_OVERLAY = {
    "setStrictGate": False,
    "setUseHistoricGate": False,
    "entryPolicy": "permissive-bounded",
    "entryPolicyMaxCandidates": 0,
    "entryPolicyMinLiveSamples": 0,
    "minPf": 1.15,
    "baseEvalPosCount": 30,
    "setPfWindow": 30,
    "setDeactN": 25,
    "setLiveNegativeDeact": True,
    "positionCostPct": COST_PCT,
}


def gross_for_pf(pf):
    """Gross fraction whose cost-adjusted Base PF equals ``pf``."""
    return (1.0 + (pf - 1.0) / 0.1) * COST_PCT / 100.0


class BasePipeline(unittest.TestCase):
    def book(self, count=3):
        book = SetBook()
        book.load(dict(PROD_OVERLAY), rebuild=False)
        book.progress.ready = True
        book.sets, book.by_idx = {}, []
        for index in range(count):
            state = SetState(
                id=f"general:1m:sl0.6:st{3 + index}", pack="general", tf="1m",
                sl_ratio=0.6, trail_key="", trail_arm=0, trail_give=0,
                step=3 + index, tp_pct=0.0045, idx=index,
            )
            book.sets[state.id] = state
            book.by_idx.append(state)
        book._reindex()
        self.t0 = time.time() - 200000
        self.i = 0
        return book

    def closes(self, book, state, pf, count=30, side="LONG"):
        for _ in range(count):
            self.i += 1
            book.on_live_close({
                "t": self.t0 + self.i * 300, "symbol": "X-USDT", "side": side,
                "pnl": 1.0, "pnl_pct": gross_for_pf(pf), "hold_s": 60,
                "reason": "tp", "set_id": state.id, "strategy": "core",
                "client_id": f"c{self.i}", "close_fill_id": f"f{self.i}",
            })

    def entry_ids(self, book, side="LONG"):
        return {st.id for st in book.entry_sets("general", side)}

    # 1. Rejected Sets stay under system-intern Base evaluation.
    def test_rejected_set_keeps_base_evaluation_and_can_qualify_later(self):
        book = self.book()
        st = book.by_idx[0]
        self.closes(book, st, 1.08)
        self.assertEqual(st.last15_n, 30)
        self.assertAlmostEqual(st.last15_ratio, 1.08, places=3)
        self.assertFalse(st.stage_ledger.get("base"))
        self.assertEqual(st.stage, "Unqualified")
        self.closes(book, st, 1.25)
        self.assertTrue(st.stage_ledger.get("base"), st.stage_ledger.get("reason"))
        self.assertIn(st.id, self.entry_ids(book))
        self.assertTrue(book.execution_allowed(st, "general", "LONG"))

    # 2. No downstream stage evaluation for a Base-rejected Set.
    def test_rejected_set_has_no_main_or_real_evaluation(self):
        book = self.book()
        st = book.by_idx[0]
        self.closes(book, st, 1.08)
        records = st.stage_ledger["records"]
        self.assertTrue(records["Base"]["evaluated"])
        self.assertFalse(records["Main"]["evaluated"])
        self.assertFalse(records["Real"]["evaluated"])
        self.assertEqual((st.stage_ledger["mainN"], st.stage_ledger["realN"]), (0, 0))
        side = st.by_side["LONG"]
        self.assertEqual((side["main_n"], side["real_n"]), (0, 0))

    # 2. Non-strict lanes (both overlays) must not enter a Base-rejected Set.
    def test_nonstrict_lane_does_not_select_or_execute_base_rejected_set(self):
        book = self.book()
        st = book.by_idx[0]
        self.closes(book, st, 1.08)  # PF >= 1.00 but below the 1.15 Base floor
        self.assertFalse(st.stage_ledger.get("base"))
        self.assertFalse(st.active, st.deact_reason)
        self.assertFalse(st.by_side["LONG"]["active"])
        self.assertNotIn(st.id, self.entry_ids(book))
        self.assertFalse(book.execution_allowed(st, "general", "LONG"))

    # 3. A validated Set that later fails Base leaves downstream processing.
    def test_validated_set_that_fails_base_is_removed_from_entries(self):
        book = self.book()
        st = book.by_idx[0]
        self.closes(book, st, 1.25)
        self.assertTrue(st.stage_ledger.get("base"))
        self.assertIn(st.id, self.entry_ids(book))
        self.closes(book, st, 1.05)
        self.assertFalse(st.stage_ledger.get("base"))
        self.assertFalse(st.active)
        self.assertNotIn(st.id, self.entry_ids(book))
        self.assertFalse(book.execution_allowed(st, "general", "LONG"))
        # Evidence keeps being tracked: Base can recover on its own evidence.
        self.closes(book, st, 1.30)
        self.assertTrue(st.stage_ledger.get("base"))
        self.assertIn(st.id, self.entry_ids(book))

    # Wasted work: Sets with no Base evidence are not entry-matrix rows.
    def test_unscored_catalog_sets_are_not_entry_candidates(self):
        book = self.book(count=50)
        winner = book.by_idx[7]
        self.closes(book, winner, 1.25)
        self.assertEqual(self.entry_ids(book), {winner.id})
        self.assertEqual(self.entry_ids(book, "SHORT"), set())

    # Directions are independent Base lanes.
    def test_direction_lanes_qualify_independently(self):
        book = self.book()
        st = book.by_idx[0]
        self.closes(book, st, 1.25, side="LONG")
        self.closes(book, st, 1.05, side="SHORT")
        self.assertIn(st.id, self.entry_ids(book, "LONG"))
        self.assertNotIn(st.id, self.entry_ids(book, "SHORT"))
        self.assertTrue(book.execution_allowed(st, "general", "LONG"))
        self.assertFalse(book.execution_allowed(st, "general", "SHORT"))

    # 3 (Test Historic lane): an allow-listed ID is not a Base bypass.
    def test_hist_test_allow_list_does_not_reactivate_base_rejected_set(self):
        book = self.book()
        good, bad = book.by_idx[0], book.by_idx[1]
        self.closes(book, good, 1.25)
        self.closes(book, bad, 1.25)
        self.closes(book, bad, 1.05)
        book.apply_hist_test_gate([good.id, bad.id])
        self.assertEqual(self.entry_ids(book), {good.id})
        self.assertTrue(good.active)
        self.assertFalse(bad.active)
        self.assertFalse(book.execution_allowed(bad, "general", "LONG"))

    # 3 (Test Historic lane, engine): the 20 s hist-test rescoring must not
    # force Base on allow-listed Sets that fail Base on their own evidence.
    def test_hist_test_rescoring_keeps_own_base_failure(self):
        from unittest.mock import patch
        import pulse_trader as pt
        import hist_test as ht
        book = self.book(count=4)
        good, one_side, both, stale = book.by_idx
        self.closes(book, good, 1.25)
        self.closes(book, one_side, 1.05, side="LONG")
        self.closes(book, both, 1.05, side="LONG")
        self.closes(book, both, 1.05, side="SHORT")
        self.closes(book, stale, 1.05)
        replay = {"validated": True, "pf": 1.4, "evalN": 40, "n": 40}
        job = {"successfulConfigs": [
            dict(replay, setId=good.id), dict(replay, setId=one_side.id), dict(replay, setId=both.id),
            {"setId": stale.id, "validated": True, "pf": 0.9, "evalN": 40, "n": 40},
        ]}
        ids = [good.id, one_side.id, both.id, stale.id]
        p = pt.Pulse.__new__(pt.Pulse)
        p.sets = book
        with patch.object(pt.hist_test_mod, "read_job", return_value=job), \
                patch.object(pt.hist_test_mod, "collect_validated_ids", return_value=ids), \
                patch.object(ht, "read_persisted_validated_ids", return_value=[]), \
                patch.object(ht, "read_last_ready", return_value={}), \
                patch.object(pt.Pulse, "_hist_write_status", lambda *a, **k: None, create=True):
            p._score_hist_test_validated()
        self.assertTrue(good.active)
        self.assertTrue(good.stage_ledger.get("base"))
        # Replay evidence still qualifies the untraded SHORT lane; own LONG fails.
        self.assertNotIn(one_side.id, self.entry_ids(book, "LONG"))
        self.assertFalse(book.execution_allowed(one_side, "general", "LONG"))
        self.assertTrue(book.execution_allowed(one_side, "general", "SHORT"))
        for st in (both, stale):
            self.assertFalse(st.stage_ledger.get("base"), st.id)
            self.assertFalse(st.active, st.id)
            for side in ("LONG", "SHORT"):
                self.assertNotIn(st.id, self.entry_ids(book, side))
                self.assertFalse(book.execution_allowed(st, "general", side))
        self.assertEqual(book.coverage()["validatedCount"], 2)

    # 4. Historic and live evidence merge into one Base window per Set.
    def test_historic_and_live_tapes_merge_into_one_base_window(self):
        from set_engine import hist_fill
        book = self.book()
        st = book.by_idx[0]
        st.hist = [hist_fill(self.t0 - 90000 + i * 60, "X-USDT", "LONG", gross_for_pf(1.25), 60, "tp")
                   for i in range(20)]
        book._score_one(st)
        self.assertEqual(st.last15_n, 20)
        self.assertFalse(st.stage_ledger.get("base"))  # 20/30 samples
        self.closes(book, st, 1.25, count=10)
        self.assertEqual(st.last15_n, 30)
        self.assertEqual(st.by_side["LONG"]["source"], "mixed")
        self.assertTrue(st.stage_ledger.get("base"))
        # Once 30 own live closes exist, they alone are the Base window.
        self.closes(book, st, 1.05, count=30)
        self.assertEqual(st.by_side["LONG"]["source"], "mixed")
        self.assertAlmostEqual(st.by_side["LONG"]["last15_ratio"], 1.05, places=3)
        self.assertFalse(st.stage_ledger.get("base"))

    # 4. Indication-kind lanes keep independent evidence.
    def test_indication_kind_lanes_are_independent(self):
        book = self.book()
        st = book.by_idx[0]
        for i in range(30):
            for kind, pf in (("trend", 1.3), ("break", 1.02)):
                book.on_live_close({"t": self.t0 + i * 60, "symbol": "X-USDT", "side": "LONG",
                                    "pnl_pct": gross_for_pf(pf), "hold_s": 60, "reason": f"ind:{kind}",
                                    "set_id": st.id, "strategy": "core", "ind_kind": kind,
                                    "client_id": f"{kind}{i}", "close_fill_id": f"{kind}{i}"})
        trend, brk = book.ind_stats("trend", "LONG"), book.ind_stats("break", "LONG")
        self.assertEqual((trend["n"], brk["n"]), (30, 30))
        self.assertTrue(trend["validated"])
        self.assertFalse(brk["validated"])
        self.assertEqual(book.ind_stats("state", "LONG")["n"], 0)

    # System result statistics count only Base-qualified Sets.
    def test_statistics_and_coordination_count_only_base_qualified_sets(self):
        from coord_engine import Coordinator
        book = self.book(count=4)
        a, b, c, _cold = book.by_idx
        self.closes(book, a, 1.25)
        self.closes(book, b, 1.08)
        self.closes(book, c, 1.25)
        self.closes(book, c, 1.05)
        cov = book.coverage()
        self.assertEqual(cov["validatedCount"], 1)
        self.assertEqual(cov["activeCount"], 1)
        self.assertEqual(cov["qualifiedParentIds"]["base"], [a.id])
        flow = book.stage_flow()["stages"]
        self.assertEqual(flow["Base"]["qualified"], 1)
        self.assertEqual(flow["Main"]["evaluated"], 1)
        self.assertEqual(flow["Base"]["selected"], 1)
        rel = book.relative_listings()
        self.assertEqual(rel["validatedIds"], [a.id])
        self.assertEqual(rel["relative"]["active"], 1)
        coord = Coordinator()
        coord.load({}, dict(PROD_OVERLAY))
        self.assertFalse(coord.axes_active())
        snap = book.axis_variants(coord)
        self.assertEqual(snap["qualifiedChildren"], 1)
        self.assertEqual(snap["parentSetIds"], [a.id])
        self.assertEqual(snap["parentCount"], 1)



class PerConfigAddGate(unittest.TestCase):
    """Per-config Block needs the parent Set's Real, DCA its Base."""

    def pulse(self, pf):
        from types import SimpleNamespace as NS
        import pulse_trader as pt
        book = SetBook()
        st = SetState(id="parent-config", pack="general", tf="1m", sl_ratio=.6, trail_key="",
                      trail_arm=0, trail_give=0)
        n = book.eval_need()
        st.by_side = {"LONG": dict(active=True, last15_n=n, last15_ratio=pf, base_n=n, base_pf=pf,
                                   main_n=n, main_pf=pf, real_n=n, real_pf=pf, ddOk=True, max_dd_s=0)}
        book.sets, book.by_idx = {st.id: st}, [st]
        p = pt.Pulse.__new__(pt.Pulse)
        p.sets = book
        p.block = NS(max_stack=3, count_tape={})
        p.coord = NS(last={}, add_gate=lambda *a, **k: (True, [], {"lastPf": 1.5}),
                     add_stack_cap=lambda stack, pf: stack)
        p.config_strategy_closes = lambda *a, **k: []
        return p

    def test_block_and_dca_adds_need_the_parent_set_stage(self):
        for strategy in ("block", "dca"):
            ok, *_ = self.pulse(1.5)._coord_add_state(set_id="parent-config", side="LONG", strategy=strategy)
            self.assertTrue(ok, strategy)
            ok, _, _, why = self.pulse(1.05)._coord_add_state(set_id="parent-config", side="LONG", strategy=strategy)
            self.assertFalse(ok, strategy)
            self.assertTrue(why and "qualification" in why[0], why)

if __name__ == "__main__":
    unittest.main()
