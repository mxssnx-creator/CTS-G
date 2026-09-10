"""Many independent orders must reconcile against exchange position groups."""
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from event_ledger import EventLedger
from entry_dispatch import EntryMatrix
from pulse_http import merge_activity_summaries, merge_axis_enablement
import pulse_trader as pt
from pulse_trader import Pulse


class ActivityGroupTests(unittest.TestCase):
    def test_overall_axis_visibility_includes_every_lane(self):
        off = {"coord": {"axes": {"last": {"enabled": False}}}}
        on = {"coverage": {"coord": {"axes": {"last": {"enabled": True}, "prev": {"enabled": False}}}}}
        self.assertTrue(merge_axis_enablement([off, on])["last"]["enabled"])
        self.assertFalse(merge_axis_enablement([off, on])["prev"]["enabled"])
        self.assertFalse(merge_axis_enablement([off, off])["last"]["enabled"])

    def test_unlimited_general_matrix_keeps_every_validated_configuration(self):
        states = [NS(id=f"cfg-{i:03d}") for i in range(250)]
        signals = [(0.9, "X-USDT", 1, "general"), (0.8, "Y-USDT", 1, "general")]
        matrix = EntryMatrix(signals, {("general", "LONG"): states})
        self.assertEqual(len(matrix), 500)
        self.assertEqual({matrix[i][-1].id for i in range(len(matrix))}, {state.id for state in states})
        self.assertEqual(matrix[0][-1].id, "cfg-000")
        self.assertEqual(matrix[1][-1].id, "cfg-001")
        self.assertEqual(matrix[2][-1].id, "cfg-001")

    def test_response_and_rejection_are_one_order_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = EventLedger(str(pathlib.Path(tmp) / "events.json"), "test")
            for kind in ("exchange_response", "rejected"):
                ledger.record(kind, kind, client_id="order-1", indication_kind="trend", strategy="block", status="rejected")
            ledger.record("rejected", "another-order", client_id="order-2", indication_kind="trend", strategy="block", status="rejected")
            result = ledger.summary()
            self.assertEqual(result["byIndication"]["trend"]["rejected"], 2)
            self.assertEqual(result["byStrategy"]["block"]["rejected"], 2)
            self.assertEqual(result["eventCount"], 3)

    def test_answered_requests_are_not_pending_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = EventLedger(str(pathlib.Path(tmp) / "events.json"), "test", max_events=512)
            for i in range(100):
                ledger.record("exchange_request", f"req{i}", status="pending")
                ledger.record("exchange_response", f"res{i}", status="confirmed")
            result = ledger.summary(pending_count=2)
            self.assertEqual(result["pendingCount"], 2)
            self.assertEqual(result["requestCount"], result["responseCount"])

    def test_250_config_orders_match_one_exchange_symbol_direction(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Pulse.__new__(Pulse)
            p.event_ledger = EventLedger(str(pathlib.Path(tmp) / "events.json"), "test")
            p.open = {
                str(i): NS(
                    symbol="X-USDT",
                    side="LONG",
                    qty=.01,
                    ours=True,
                    system_id=pt.SYSTEM_ID,
                    connection=pt.CONN_SHORT,
                    tracking_scope=pt.TRACKING_SCOPE,
                )
                for i in range(250)
            }
            p.closed = []; p.pending_orders = {}; p.exchange_open_count = 1
            result = p.event_summary()
            self.assertEqual(result["internalOpen"], 250)
            self.assertEqual(result["internalPositionGroups"], 1)
            self.assertEqual(result["parity"], "match")
            merged = merge_activity_summaries([result, result])
            self.assertEqual(merged["internalOpen"], 500)
            self.assertEqual(merged["internalPositionGroups"], 2)
            self.assertEqual(merged["parity"], "match")
            p.open["short"] = NS(
                symbol="X-USDT",
                side="SHORT",
                qty=.01,
                ours=True,
                system_id=pt.SYSTEM_ID,
                connection=pt.CONN_SHORT,
                tracking_scope=pt.TRACKING_SCOPE,
            )
            self.assertEqual(p.event_summary()["parity"], "discrepant")
            p.recon_pending = True
            waiting = p.event_summary()
            self.assertEqual(waiting["parity"], "pending")
            self.assertEqual(waiting["pendingCount"], 0)
            self.assertEqual(merge_activity_summaries([result, waiting])["parity"], "pending")
            confirmed_error={**result,"parity":"discrepant"}
            self.assertEqual(merge_activity_summaries([confirmed_error, waiting])["parity"], "discrepant")

    def test_strategy_attribution_uses_execution_and_not_parent_pack(self):
        base = dict(pack="general", strategy="core", axis_key="", trail_key="")
        for changes, expected in [({}, "general"), ({"strategy": "block", "axis_key": "block-active:5"}, "block"),
                                  ({"strategy": "dca"}, "dca"), ({"axis_key": "prev:5"}, "axis"),
                                  ({"trail_key": "0.3:0.1"}, "trailing")]:
            self.assertEqual(Pulse.event_strategy(NS(**{**base, **changes})), expected)


if __name__ == "__main__":
    unittest.main()
