"""Many independent orders must reconcile against exchange position groups."""
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from event_ledger import EventLedger
from entry_dispatch import EntryMatrix
from pulse_http import merge_activity_summaries, merge_axis_enablement, overall_report_state
import pulse_http as ph
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

    def test_overall_report_preserves_aggregate_control_mode(self):
        def lane(mode):
            return {
                "trackingScope": "x01",
                "systemId": "cts-g",
                "connection": "x01",
                "open": [],
                "closed": [],
                "logicalPositionCount": 0,
                "exchangePositionGroupCount": 0,
                "coverage": {"controls": {"mode": mode}},
            }

        aggregate = lane("aggregate")
        self.assertEqual(overall_report_state(aggregate, aggregate)["coverage"]["controls"]["mode"], "aggregate")
        self.assertEqual(overall_report_state(aggregate, lane("per-config"))["coverage"]["controls"]["mode"], "mixed")

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

    def test_working_order_count_is_unique_oids_not_position_lanes(self):
        p = Pulse.__new__(Pulse)
        p.position_is_ours = lambda pos: getattr(pos, "ours", True) is not False
        shared_sl, shared_tp = "SL1", "TP1"
        p.open = {
            "a": NS(symbol="X-USDT", side="LONG", qty=0.01, ours=True,
                    sl_oid=shared_sl, tp_oid=shared_tp, sec_sl_oid=shared_sl, sec_tp_oid=shared_tp,
                    order_id="FILLED-A"),
            "b": NS(symbol="X-USDT", side="LONG", qty=0.01, ours=True,
                    sl_oid=shared_sl, tp_oid=shared_tp, sec_sl_oid=shared_sl, sec_tp_oid=shared_tp,
                    order_id="FILLED-B"),
            "c": NS(symbol="Y-USDT", side="SHORT", qty=0.02, ours=True,
                    sl_oid="SL2", tp_oid="TP2", sec_sl_oid="", sec_tp_oid="", order_id=""),
            "foreign": NS(symbol="Z-USDT", side="LONG", qty=1, ours=False,
                          sl_oid="SLX", tp_oid="TPX", sec_sl_oid="", sec_tp_oid="", order_id=""),
        }
        p.pending_orders = {
            "Gx02e-1": {"order_id": "111", "kind": "entry"},
            "Gx02e-2": {"kind": "entry"},
            "Gx02c-3": {"order_id": shared_sl, "kind": "sec-sl"},
        }
        self.assertEqual(len(p.open), 4)
        # Unique working: SL1, TP1, SL2, TP2, pending 111, pending without oid.
        # Shared overall SL/TP and filled entry ids are not extra orders.
        self.assertEqual(p.internal_working_order_count(), 6)
        # Confirmed Live snapshot stays Live. Real keeps the internal book.
        p.exchange_order_snapshot_pending = False
        p.exchange_order_own_count = 178
        self.assertEqual(p.internal_working_order_count(), 6)
        p.exchange_order_snapshot_pending = True
        self.assertEqual(p.internal_working_order_count(), 6)
        # Real Positions collapse intern/config lanes onto symbol+direction.
        self.assertEqual(p.internal_position_group_count(), 2)
        self.assertNotEqual(p.internal_position_group_count(), p.internal_working_order_count())
        self.assertNotEqual(p.internal_position_group_count(), len(p.open))

    def test_real_positions_combine_symbol_and_direction(self):
        p = Pulse.__new__(Pulse)
        p.position_is_ours = lambda pos: getattr(pos, "ours", True) is not False
        p.open = {
            str(i): NS(symbol="X-USDT", side="LONG", qty=0.01, ours=True)
            for i in range(250)
        }
        p.open["short"] = NS(symbol="X-USDT", side="SHORT", qty=0.02, ours=True)
        p.open["y"] = NS(symbol="Y-USDT", side="LONG", qty=0.03, ours=True)
        p.open["dust"] = NS(symbol="Y-USDT", side="SHORT", qty=0, ours=True)
        p.open["foreign"] = NS(symbol="Z-USDT", side="LONG", qty=1, ours=False)
        p.open["proxy"] = NS(symbol="X-USDT", side="LONG", qty=0.5, ours=True, _overall_proxy=True)
        self.assertEqual(p.internal_position_group_count(), 3)
        self.assertEqual(len(p.open), 255)

    def test_compact_stats_prefer_symbol_direction_groups(self):
        live = {
            "trackingScope": "x01", "systemId": "cts-g", "connection": "x01",
            "open": [], "closed": [], "logicalPositionCount": 700, "openCount": 700,
            "realPositionCount": 700, "realPositionGroupCount": 12, "realOrderCount": 24,
            "livePositionCount": 12, "liveOrderCount": 24, "coverage": {"controls": {"mode": "overall"}},
        }
        vst = {
            "trackingScope": "x02", "systemId": "cts-g", "connection": "x02",
            "open": [], "closed": [], "logicalPositionCount": 300, "openCount": 300,
            "realPositionCount": 300, "realPositionGroupCount": 5, "realOrderCount": 10,
            "livePositionCount": 5, "liveOrderCount": 10, "coverage": {"controls": {"mode": "overall"}},
        }
        report = overall_report_state(live, vst)
        self.assertEqual(report["openCount"], 1000)
        self.assertEqual(report["realPositionCount"], 17)
        self.assertEqual(report["realPositionGroupCount"], 17)
        self.assertEqual(report["realOrderCount"], 34)
        self.assertEqual(report["livePositionCount"], 17)
        self.assertEqual(report["liveOrderCount"], 34)
        self.assertNotEqual(report["realPositionCount"], report["openCount"])
        self.assertNotEqual(report["realPositionCount"], report["realOrderCount"])
        self.assertEqual(ph._position_group_count(live), 12)
        self.assertEqual(ph._real_order_count(live), 24)
        compact = ph.lane_summary(
            {"type": "live", "id": "bingx-x01", "label": "Live", "unit": "USDT", "exchange": "BingX"},
            {**live, "running": False, "halted": True},
        )
        self.assertEqual(compact["realPositionCount"], 12)
        self.assertEqual(compact["realOrderCount"], 24)
        self.assertEqual(compact["openCount"], 700)
        self.assertNotEqual(compact["realPositionCount"], compact["openCount"])

        stale = {
            "openCount": 263, "logicalPositionCount": 263,
            "realPositionCount": 263, "realPositionGroupCount": 5, "realOrderCount": 263,
            "open": [
                {"symbol": "SOL-USDT", "side": "LONG", "qty": 0.03, "ours": True,
                 "slOid": "SL1", "tpOid": "TP1", "secSlOid": "SL1", "secTpOid": "TP1"},
                {"symbol": "SOL-USDT", "side": "LONG", "qty": 0.01, "ours": True,
                 "slOid": "SL1", "tpOid": "TP1"},
                {"symbol": "XRP-USDT", "side": "SHORT", "qty": 0.5, "ours": True,
                 "slOid": "SL2", "tpOid": "TP2"},
            ],
        }
        self.assertEqual(ph._position_group_count(stale), 5)
        self.assertEqual(ph._real_order_count(stale), 4)
        self.assertNotEqual(ph._real_order_count(stale), stale["openCount"])
        self.assertNotEqual(ph._position_group_count(stale), ph._real_order_count(stale))
        slim = ph.slim_for_ui(stale)
        self.assertEqual(slim["realPositionCount"], 5)
        self.assertEqual(slim["realPositionGroupCount"], 5)
        self.assertEqual(slim["realOrderCount"], 4)
        self.assertEqual(slim["openCount"], 263)
        self.assertNotEqual(slim["realPositionCount"], slim["realOrderCount"])

        complete = {
            "openCount": 8, "logicalPositionCount": 8,
            "realPositionCount": 3, "realPositionGroupCount": 3, "realOrderCount": 30,
            "livePositionCount": 3, "liveOrderCount": 178,
            "open": [
                {"symbol": "SOL-USDT", "side": "LONG", "qty": 0.03, "ours": True,
                 "slOid": "SL1", "tpOid": "TP1", "exchangeQty": 0.03},
                {"symbol": "SOL-USDT", "side": "LONG", "qty": 0.01, "ours": True,
                 "slOid": "SL1", "tpOid": "TP1", "exchangeQty": 0.03},
                {"symbol": "SOL-USDT", "side": "SHORT", "qty": 0.02, "ours": True,
                 "slOid": "SL2", "tpOid": "TP2", "exchangeQty": 0.02},
                {"symbol": "XRP-USDT", "side": "LONG", "qty": 2, "ours": True,
                 "slOid": "SL3", "tpOid": "TP3", "exchangeQty": 2},
                {"symbol": "ADA-USDT", "side": "SHORT", "qty": 11, "ours": True,
                 "slOid": "SL4", "tpOid": "TP4", "exchangeQty": 11},
                {"symbol": "ADA-USDT", "side": "LONG", "qty": 11, "ours": True,
                 "slOid": "SL5", "tpOid": "TP5", "exchangeQty": 0},
            ],
        }
        # 3 symbols: SOL both, ADA both, XRP long only = 5 Real. ADA long is sim so Live=4.
        self.assertEqual(ph._symbol_direction_count_from_snapshot(complete), 5)
        self.assertEqual(ph._symbol_direction_count_from_snapshot(complete, exchange_only=True), 4)
        self.assertEqual(ph._position_group_count(complete), 5)
        self.assertEqual(ph._live_position_count(complete), 4)
        self.assertEqual(ph._real_order_count(complete), 30)
        slim_complete = ph.slim_for_ui(complete)
        self.assertEqual(slim_complete["realPositionCount"], 5)
        self.assertEqual(slim_complete["livePositionCount"], 4)
        self.assertEqual(slim_complete["realOrderCount"], 30)
        self.assertEqual(slim_complete["liveOrderCount"], 178)
        self.assertNotEqual(slim_complete["realPositionCount"], slim_complete["livePositionCount"])
        self.assertNotEqual(slim_complete["realOrderCount"], slim_complete["liveOrderCount"])

    def test_strategy_attribution_uses_execution_and_not_parent_pack(self):
        base = dict(pack="general", strategy="core", axis_key="", trail_key="")
        for changes, expected in [({}, "general"), ({"strategy": "block", "axis_key": "block-active:5"}, "block"),
                                  ({"strategy": "dca"}, "dca"), ({"axis_key": "prev:5"}, "axis"),
                                  ({"trail_key": "0.3:0.1"}, "trailing")]:
            self.assertEqual(Pulse.event_strategy(NS(**{**base, **changes})), expected)


if __name__ == "__main__":
    unittest.main()
