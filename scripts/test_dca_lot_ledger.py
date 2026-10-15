"""Confirmed DCA lots conserve quantity, independent returns, fees and cash."""
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
import dca_lot_ledger as module
from dca_lot_ledger import DcaLotLedger
from position_cost import accumulate_close, completed_roundtrips, row_net_pnl
from dca_engine import DcaBook


class DcaLotTests(unittest.TestCase):
    def test_entry_lots_returns_only_active_original_cid_and_independent_copies(self):
        ledger = DcaLotLedger("x02")
        ledger.register_fill("p", "b", "b", "A", "LONG", 1, 100, .05, "one", client_id="cid")
        ledger.register_fill("other", "b", "b", "A", "LONG", 1, 100, .05, "two", client_id="other-cid")
        lots = ledger.entry_lots("cid")
        self.assertEqual(len(lots), 1)
        lots[0]["entry"] = 1
        self.assertEqual(ledger.entry_lots("cid")[0]["entry"], 100)
        closed = ledger.allocate_close("p", 1, 101, .05, "close")
        ledger.ack_close(closed["outbox_id"])
        self.assertEqual(ledger.entry_lots("cid"), [])
        ledger.register_fill("p", "b", "b", "A", "LONG", 1, 99, .05, "three", client_id="cid", reactivate=True)
        self.assertEqual(ledger.entry_lots("cid")[0]["entry_client_id"], "cid")

    def test_delta_order_fee_and_price_refine_without_inventing_fill_quantity(self):
        ledger = DcaLotLedger("x02")
        for seq in (1, 2):
            ledger.register_fill("p", "b", "c", "A", "LONG", 1, 100, 0, f"f{seq}",
                                 order_id="order", client_id="cid", metadata=dict(cost_source="manual-fallback", position_cost_pct=.18))
        result = ledger.refine_order("p", 2, 99, .099, order_id="order", client_id="cid",
                                     metadata=dict(cost_source="live-exchange"))
        self.assertFalse(result["duplicate"])
        self.assertEqual(ledger.parent_qty("p"), 2)
        self.assertAlmostEqual(result["feeDelta"], .099)
        self.assertAlmostEqual(result["notionalDelta"], -2)
        self.assertTrue(ledger.refine_order("p", 2, 99, .099, order_id="order", client_id="cid",
                                           metadata=dict(cost_source="live-exchange"))["duplicate"])
        row = ledger.allocate_close("p", 2, 100, .1, "done", metadata=dict(cost_source="live-exchange"))["rows"][0]
        self.assertAlmostEqual(row["pnl"], 2-.099-.1)
        self.assertEqual(row["cost_source"], "live-exchange")

    def test_refinement_after_partial_close_is_rejected_without_mutating_history(self):
        ledger = self.seed()
        ledger.allocate_close("parent", .5, 100, .025, "partial", t=3)
        before = ledger._state()
        with self.assertRaises(ValueError):
            ledger.refine_order("parent", 1, 101, .06, client_id=before["lots"][next(iter(before["lots"]))]["entry_client_id"])
        self.assertEqual(ledger._state(), before)

    def test_measured_free_fee_refinement_keeps_configured_pf_reference(self):
        ledger = DcaLotLedger("x02")
        ledger.register_fill("p", "b", "b", "A", "LONG", 1, 100, 0, "fill", client_id="cid",
                             metadata=dict(position_cost_pct=.2, cost_source="manual-fallback"))
        ledger.refine_order("p", 1, 100, 0, client_id="cid", metadata=dict(cost_source="live-exchange"))
        result = ledger.allocate_close("p", 1, 100.2, 0, "close", metadata=dict(cost_source="live-exchange"))
        self.assertEqual(result["rows"][0]["position_cost_pct"], 0)
        self.assertAlmostEqual(result["netPnl"], .2)

    def test_partial_before_first_add_remains_one_complete_baseline_roundtrip(self):
        prior_leg = dict(client_id="parent", set_id="base", symbol="BTC-USDT", side="LONG", conn="x02",
                         qty=.4, entry=100., exit=105., pnl=1.96, pnl_pct=.05, partial=True,
                         exchange_confirmed=True, entry_fee=.02, exit_fee=.02, fee_total=.04,
                         position_cost_pct=.1, cost_source="live-exchange")
        prior = accumulate_close({}, prior_leg)
        ledger = DcaLotLedger("x02")
        baseline = ledger.register_fill("parent", "base", "base", "BTC-USDT", "LONG", .6, 100., .03,
                                        "baseline", strategy="core", client_id="parent", t=1,
                                        prior_roundtrip_result=prior)["lot"]
        self.assertEqual(ledger.parent_qty("parent"), .6)
        self.assertEqual(baseline["remaining_entry_fee"], .03)
        self.assertAlmostEqual(baseline["opened_qty"], 1.)
        ledger.register_fill("parent", "base", "child", "BTC-USDT", "LONG", .6, 90., .027,
                             "child", strategy="dca", client_id="child", t=2)
        closing = ledger.allocate_close("parent", 1.2, 110., .066, "done", t=3)
        base = closing["baseRows"][0]
        self.assertAlmostEqual(base["qty"], .6)
        self.assertAlmostEqual(base["roundtrip_result"]["qty"], 1.)
        self.assertAlmostEqual(base["roundtrip_result"]["pnl"], 1.96+6-.03-.033)
        self.assertAlmostEqual(base["roundtrip_result"]["entry_fee"], .05)
        self.assertAlmostEqual(base["roundtrip_result"]["fee_total"], .04+.03+.033)
        self.assertEqual(len(completed_roundtrips(closing["rows"])), 2)
        wrong = dict(prior, client_id="foreign")
        with self.assertRaises(ValueError):
            DcaLotLedger("x02").register_fill("parent", "base", "base", "BTC-USDT", "LONG", .6, 100., .03,
                                             "baseline", client_id="parent", prior_roundtrip_result=wrong)

    def test_invalid_reactivation_preserves_retired_inventory_truth(self):
        ledger = self.seed()
        closed = ledger.allocate_close("parent", 2, 100, .1, "full", t=3)
        ledger.ack_close(closed["outbox_id"])
        before = ledger._state()
        with self.assertRaises(ValueError):
            ledger.register_fill("parent", "base", "base", "BTC-USDT", "LONG", 0, 100, .01,
                                 "invalid-new-fill", strategy="core", reactivate=True, t=4)
        self.assertEqual(ledger._state(), before)
        self.assertTrue(ledger.tracked_parent_state("parent")["retired"])

    def seed(self, ledger=None):
        ledger = ledger or DcaLotLedger("x02")
        ledger.register_fill("parent", "base", "base", "BTC-USDT", "LONG", 1, 100, .05,
                             "base-fill", strategy="core", t=1)
        ledger.register_fill("parent", "base", "child", "BTC-USDT", "LONG", 1, 90, .045,
                             "child-fill", strategy="dca", t=2)
        return ledger

    def test_partial_closes_conserve_every_lot_and_fee(self):
        ledger = self.seed()
        first = ledger.allocate_close("parent", 1, 95, .0475, "close-1", t=10)
        self.assertEqual(len(first["baseRows"]), 1)
        self.assertEqual(len(first["childRows"]), 1)
        self.assertAlmostEqual(first["qty"], 1)
        self.assertAlmostEqual(first["remainingQty"], 1)
        self.assertAlmostEqual(first["grossPnl"], 0)
        self.assertAlmostEqual(first["netPnl"], -.095)
        for row in first["rows"]:
            self.assertAlmostEqual(row["qty"], .5)
            self.assertTrue(row["partial"])
            self.assertAlmostEqual(row_net_pnl(row)*row["notional"], row["pnl"])
        self.assertLess(first["baseRows"][0]["pnl"], 0)
        self.assertGreater(first["childRows"][0]["pnl"], 0)
        last = ledger.allocate_close("parent", 1, 110, .055, "close-2", t=20)
        self.assertEqual(ledger.parent_qty("parent"), 0)
        self.assertTrue(ledger.has_parent("parent"), "unpublished child closes remain recoverable")
        ledger.ack_close(first["outbox_id"])
        ledger.ack_close(last["outbox_id"])
        self.assertFalse(ledger.has_parent("parent"))
        self.assertAlmostEqual(first["netPnl"]+last["netPnl"], 15-.095-.0475-.055)
        self.assertAlmostEqual(first["feeTotal"]+last["feeTotal"], .095+.0475+.055)
        rows = completed_roundtrips(first["rows"]+last["rows"])
        self.assertEqual(len(rows), 2, "partial legs are not extra positions")
        self.assertEqual({row["set_id"] for row in rows}, {"base", "child"})
        self.assertAlmostEqual(sum(row["pnl"] for row in rows), first["netPnl"]+last["netPnl"])
        retained_final_only = completed_roundtrips(last["rows"])
        self.assertEqual(len(retained_final_only), 2)
        self.assertAlmostEqual(sum(row["pnl"] for row in retained_final_only), first["netPnl"]+last["netPnl"])

    def test_duplicate_partial_close_and_entry_do_not_change_inventory(self):
        ledger = self.seed()
        before = ledger.snapshot()
        duplicate = ledger.register_fill("parent", "base", "child", "BTC-USDT", "LONG", 1, 90, .045, "child-fill")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(ledger.snapshot(), before)
        ledger.allocate_close("parent", .5, 100, .025, "close", t=10)
        after = ledger.snapshot()
        receipt = ledger.allocate_close("parent", .5, 100, .025, "close", t=10)
        self.assertTrue(receipt["duplicate"])
        self.assertTrue(receipt["replay"])
        self.assertEqual(len(receipt["rows"]), 2)
        self.assertEqual(ledger.snapshot(), after)
        self.assertTrue(ledger.ack_close(receipt["outbox_id"]))
        self.assertEqual(ledger.allocate_close("parent", .5, 100, .025, "close", t=10)["rows"], [])

    def test_cumulative_average_uses_incremental_notional_not_latest_average(self):
        ledger = DcaLotLedger("x02")
        kwargs = dict(order_id="order", cumulative=True, t=1)
        ledger.register_fill("p", "b", "c", "A", "LONG", 1, 100, .05, **kwargs)
        update = ledger.register_fill("p", "b", "c", "A", "LONG", 2, 90, .09, **kwargs)
        self.assertAlmostEqual(update["qty"], 1)
        self.assertAlmostEqual(update["lot"]["entry"], 90)
        self.assertAlmostEqual(update["lot"]["entry_fee"], .09)
        self.assertTrue(ledger.register_fill("p", "b", "c", "A", "LONG", 2, 90, .09, **kwargs)["duplicate"])
        close = ledger.allocate_close("p", 2, 100, .1, "done", t=5)
        self.assertAlmostEqual(close["grossPnl"], 20)
        self.assertAlmostEqual(close["netPnl"], 19.81)

    def test_cumulative_fill_after_partial_close_preserves_remaining_cost_basis(self):
        ledger = DcaLotLedger("x02")
        ledger.register_fill("p", "b", "c", "A", "LONG", 1, 100, .05, order_id="o", cumulative=True, t=1)
        first = ledger.allocate_close("p", .5, 100, .025, "first", t=2)
        ledger.register_fill("p", "b", "c", "A", "LONG", 2, 90, .09, order_id="o", cumulative=True, t=3)
        final = ledger.allocate_close("p", 1.5, 100, .075, "final", t=4)
        self.assertAlmostEqual(final["rows"][0]["entry"], 130/1.5)
        self.assertAlmostEqual(first["grossPnl"]+final["grossPnl"], 20)
        self.assertAlmostEqual(first["netPnl"]+final["netPnl"], 20-.09-.1)

    def test_cumulative_final_price_and_fee_can_refine_before_close(self):
        ledger = DcaLotLedger("x02")
        ledger.register_fill("p", "b", "c", "A", "LONG", 2, 100, .1, order_id="o", cumulative=True, t=1)
        refined = ledger.register_fill("p", "b", "c", "A", "LONG", 2, 99, .101, order_id="o", cumulative=True, t=2)
        self.assertAlmostEqual(refined["lot"]["entry"], 99)
        self.assertAlmostEqual(refined["lot"]["entry_fee"], .101)
        # Fees cannot regress silently; an exchange rebate/correction needs
        # an explicit reconciliation instead of being mistaken for a fill.
        with self.assertRaises(ValueError):
            ledger.register_fill("p", "b", "c", "A", "LONG", 2, 99, .099, order_id="o", cumulative=True, t=3)
        close = ledger.allocate_close("p", 2, 100, .1, "close", t=4)
        self.assertAlmostEqual(close["netPnl"], 2-.101-.1)

    def test_same_symbol_and_side_do_not_mix_independent_parents(self):
        ledger = self.seed()
        ledger.register_fill("other", "other-base", "other-child", "BTC-USDT", "LONG", 3, 50, .075, "other")
        ledger.allocate_close("parent", 2, 100, .1, "close", t=10)
        self.assertEqual(ledger.parent_qty("other"), 3)
        self.assertEqual(ledger.snapshot("other")["lots"][0]["entry"], 50)

    def test_emitted_client_ids_keep_venue_ownership_and_independent_lot_ids(self):
        ledger = DcaLotLedger("bingx-x02")
        ledger.register_fill("Gx02o1", "base", "base", "A", "LONG", 1, 100, .05, "base",
                             client_id="Gx02o1", order_id="1", strategy="core", t=1)
        ledger.register_fill("Gx02o1", "base", "child", "A", "LONG", 1, 90, .045, "child",
                             client_id="Gx02d1", order_id="2", t=2)
        rows = ledger.allocate_close("Gx02o1", 2, 100, .1, "close", t=3)["rows"]
        self.assertEqual({row["client_id"] for row in rows}, {"Gx02o1", "Gx02d1"})
        self.assertEqual(len({row["lot_id"] for row in rows}), 2)
        self.assertEqual(len(completed_roundtrips(rows)), 2)

    def test_short_lots_use_their_own_price_and_direction(self):
        ledger = DcaLotLedger("x02")
        ledger.register_fill("p", "b", "c", "A", "SHORT", 2, 100, .1, "entry", t=1)
        close = ledger.allocate_close("p", 2, 90, .09, "close", t=2)
        self.assertAlmostEqual(close["grossPnl"], 20)
        self.assertAlmostEqual(close["netPnl"], 19.81)

    def test_reported_exchange_net_reconciles_without_double_fees(self):
        ledger = self.seed()
        close = ledger.allocate_close("parent", 2, 105, .105, "actual", reported_net_pnl=19.75, t=10)
        self.assertAlmostEqual(close["netPnl"], 19.75)
        self.assertAlmostEqual(close["feeTotal"], .2)
        self.assertAlmostEqual(sum(row_net_pnl(row)*row["notional"] for row in close["rows"]), 19.75)
        self.assertAlmostEqual(sum(row["pnl_reconciliation"] for row in close["rows"]), -.05)

    def test_deactivation_r_uses_the_same_measured_cost_as_pf(self):
        book = DcaBook()
        book.load(dict(positionCostPct=.18, dcaPfWindow=5, dcaDeactN=5))
        rows = [dict(t=i, qty=1, entry=100, pnl_pct=.001, position_cost_pct=.01,
                     cost_source="live-exchange") for i in range(5)]
        result = book.score(rows)
        self.assertEqual(result["last25AvgR"], 9)
        self.assertTrue(result["active"])
        book.load(dict(positionCostPct=0))
        self.assertEqual(book.cost_pct, 0)

    def test_dca_book_learns_completed_tagged_child_once_and_preserves_partial_lane(self):
        book = DcaBook()
        lane = book.attach("A", "LONG", 1, 100)
        row = dict(symbol="A", side="LONG", client_id="v1:child", strategy="dca", pack="dca",
                   pnl_pct=.001, pnl=1, t=1, exchange_confirmed=True, partial=True)
        book.on_close(row)
        self.assertIn(book.key("A", "LONG"), book.lanes)
        self.assertEqual(book.closes, [])
        full = dict(row, partial=False)
        book.on_close(full)
        book.on_close(full)
        self.assertEqual(len(book.closes), 1)
        self.assertNotIn(book.key("A", "LONG"), book.lanes)
        book.on_close(dict(full, client_id="v1:unconfirmed", exchange_confirmed=False))
        self.assertEqual(len(book.closes), 1)

    def test_unknown_fee_uses_declared_fallback_once_not_free_exchange_cost(self):
        ledger = DcaLotLedger("x02")
        meta = dict(position_cost_pct=.18, cost_source="manual-fallback")
        ledger.register_fill("p", "base", "child", "A", "LONG", 1, 100, 0, "open", metadata=meta, t=1)
        close = ledger.allocate_close("p", 1, 101, 0, "close", metadata=meta, reported_net_pnl=.82, t=2)
        row = close["rows"][0]
        self.assertAlmostEqual(row["pnl_pct"], .01)
        self.assertEqual(row["position_cost_pct"], .18)
        self.assertEqual(row["cost_source"], "manual-fallback")
        self.assertAlmostEqual(row["estimated_cost_usdt"], .18)
        self.assertAlmostEqual(row_net_pnl(row)*100, .82)
        self.assertEqual(row["fee_total"], 0)

    def test_final_cumulative_fee_measurement_replaces_fallback_source(self):
        ledger = DcaLotLedger("x02")
        ledger.register_fill("p", "b", "c", "A", "LONG", 1, 100, 0, order_id="o", cumulative=True,
                             metadata=dict(position_cost_pct=.18, cost_source="manual-fallback"), t=1)
        ledger.register_fill("p", "b", "c", "A", "LONG", 1, 100, .05, order_id="o", cumulative=True,
                             metadata=dict(cost_source="live-exchange"), t=2)
        row = ledger.allocate_close("p", 1, 101, .05, "close", t=3)["rows"][0]
        self.assertEqual(row["cost_source"], "live-exchange")
        self.assertEqual(row["position_cost_pct"], .1)
        self.assertAlmostEqual(row["pnl"], .9)

    def test_one_measured_fee_leg_cannot_claim_complete_measured_cost(self):
        ledger = DcaLotLedger("x02")
        ledger.register_fill("p", "b", "c", "A", "LONG", 1, 100, 0, "open", t=1,
                             metadata=dict(position_cost_pct=.18, cost_source="manual-fallback"))
        row = ledger.allocate_close("p", 1, 101, .05, "close", t=2,
                                    metadata=dict(cost_source="live-exchange"))["rows"][0]
        self.assertEqual(row["cost_source"], "manual-fallback")
        self.assertEqual(row["position_cost_pct"], .18)
        self.assertAlmostEqual(row["pnl"], .82)

    def test_measured_zero_fee_is_zero_and_not_fallback(self):
        ledger = DcaLotLedger("x02")
        ledger.register_fill("p", "base", "child", "A", "LONG", 1, 100, 0, "open", t=1)
        row = ledger.allocate_close("p", 1, 101, 0, "close", t=2)["rows"][0]
        self.assertEqual(row["position_cost_pct"], 0)
        self.assertEqual(row["estimated_cost_usdt"], 0)
        self.assertAlmostEqual(row_net_pnl(row)*100, 1)

    def test_persistence_restores_lots_and_close_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(pathlib.Path(directory)/"x02.json")
            first = self.seed(DcaLotLedger("x02", path))
            first.allocate_close("parent", 1, 95, .0475, "partial", t=3)
            second = DcaLotLedger("x02", path)
            self.assertAlmostEqual(second.parent_qty("parent"), 1)
            self.assertTrue(second.allocate_close("parent", 1, 95, .0475, "partial", t=3)["duplicate"])
            final = second.allocate_close("parent", 1, 110, .055, "final", t=4)
            complete = completed_roundtrips(final["rows"])
            self.assertEqual(len(complete), 2)
            self.assertAlmostEqual(sum(row["pnl"] for row in complete), 15-.095-.0475-.055)
            with self.assertRaisesRegex(ValueError, "connection"):
                DcaLotLedger("x01", path)

    def test_oversell_invalid_fill_and_failed_checkpoint_leave_original_inventory(self):
        ledger = self.seed()
        before = ledger.snapshot()
        with self.assertRaises(ValueError):
            ledger.allocate_close("parent", 3, 100, .1, "too-much")
        with self.assertRaises(ValueError):
            ledger.register_fill("p", "b", "c", "A", "LONG", float("nan"), 100, 0, "bad")
        self.assertEqual(ledger.snapshot(), before)
        ledger.path = "/unused"
        with patch.object(module, "atomic_write", side_effect=OSError("storage failure")):
            with self.assertRaises(OSError):
                ledger.allocate_close("parent", 1, 100, .05, "retry", t=10)
        self.assertEqual(ledger.snapshot()["lots"], before["lots"])
        ledger.path = None
        self.assertFalse(ledger.allocate_close("parent", 1, 100, .05, "retry", t=10)["duplicate"])

    def test_full_close_outbox_survives_restart_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(pathlib.Path(directory)/"lots.json")
            ledger = self.seed(DcaLotLedger("x02", path))
            emitted = ledger.allocate_close("parent", 2, 105, .105, "physical-close", t=5)
            restored = DcaLotLedger("x02", path)
            self.assertEqual(restored.parent_qty("parent"), 0)
            self.assertTrue(restored.has_parent("parent"))
            pending = restored.pending_closes()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["rows"], emitted["rows"])
            replayed = restored.allocate_close("parent", 2, 105, .105, "physical-close", t=5)
            self.assertTrue(replayed["replay"])
            self.assertEqual(replayed["rows"], emitted["rows"])
            self.assertTrue(restored.ack_close(pending[0]["outbox_id"]))
            complete = DcaLotLedger("x02", path)
            self.assertFalse(complete.has_parent("parent"))
            state = complete.tracked_parent_state("parent")
            self.assertTrue(state["tracked"])
            self.assertTrue(state["retired"])
            self.assertEqual(state["remainingQty"], 0)
            self.assertFalse(complete.tracked_parent_state("unknown")["tracked"])

    def test_acknowledgement_cannot_remove_another_parents_same_fill_id(self):
        ledger = self.seed()
        ledger.register_fill("other", "b", "c", "A", "LONG", 1, 100, .05, "other")
        ledger.allocate_close("parent", 2, 100, .1, "venue-close")
        ledger.allocate_close("other", 1, 100, .05, "venue-close")
        with self.assertRaises(ValueError):
            ledger.ack_close("venue-close")
        self.assertTrue(ledger.ack_close("venue-close", parent_key="parent"))
        self.assertEqual(len(ledger.pending_closes()), 1)
        self.assertTrue(ledger.has_parent("other"))

    def test_proven_late_core_fill_reactivates_without_reseeding_old_position(self):
        ledger = DcaLotLedger("bingx-x02")
        ledger.register_fill("Gx02o1", "base", "base", "A", "LONG", 1, 100, .05, "cum1",
                             client_id="Gx02o1", order_id="order", strategy="core", t=1)
        first = ledger.allocate_close("Gx02o1", 1, 101, .05, "first", t=2)
        ledger.ack_close(first["outbox_id"])
        self.assertTrue(ledger.register_fill("Gx02o1", "base", "base", "A", "LONG", 1, 100, .05, "cum1",
                             client_id="Gx02o1", order_id="order", strategy="core", t=3, reactivate=True)["duplicate"])
        accepted = ledger.register_fill("Gx02o1", "base", "base", "A", "LONG", 1, 99, .0495, "cum2",
                             client_id="Gx02o1", order_id="order", strategy="core", t=3, reactivate=True)
        self.assertFalse(accepted["duplicate"])
        self.assertEqual(ledger.parent_qty("Gx02o1"), 1)
        final = ledger.allocate_close("Gx02o1", 1, 100, .05, "final", t=4)
        self.assertNotEqual(first["rows"][0]["client_id"], final["rows"][0]["client_id"])
        self.assertEqual(final["rows"][0]["entry_client_id"], "Gx02o1")
        self.assertEqual(len(completed_roundtrips(first["rows"]+final["rows"])), 2)


if __name__ == "__main__":
    unittest.main()
