"""Many independent orders must reconcile against exchange position groups."""
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from event_ledger import EventLedger
from pulse_http import merge_activity_summaries
from pulse_trader import Pulse


class ActivityGroupTests(unittest.TestCase):
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
            p.open = {str(i): NS(symbol="X-USDT", side="LONG", qty=.01) for i in range(250)}
            p.closed = []; p.pending_orders = {}; p.exchange_open_count = 1
            result = p.event_summary()
            self.assertEqual(result["internalOpen"], 250)
            self.assertEqual(result["internalPositionGroups"], 1)
            self.assertEqual(result["parity"], "match")
            merged = merge_activity_summaries([result, result])
            self.assertEqual(merged["internalOpen"], 500)
            self.assertEqual(merged["internalPositionGroups"], 2)
            self.assertEqual(merged["parity"], "match")
            p.open["short"] = NS(symbol="X-USDT", side="SHORT", qty=.01)
            self.assertEqual(p.event_summary()["parity"], "discrepant")

    def test_strategy_attribution_uses_execution_and_not_parent_pack(self):
        base = dict(pack="general", strategy="core", axis_key="", trail_key="")
        for changes, expected in [({}, "general"), ({"strategy": "block", "axis_key": "block-active:5"}, "block"),
                                  ({"strategy": "dca"}, "dca"), ({"axis_key": "prev:5"}, "axis"),
                                  ({"trail_key": "0.3:0.1"}, "trailing")]:
            self.assertEqual(Pulse.event_strategy(NS(**{**base, **changes})), expected)


if __name__ == "__main__":
    unittest.main()
