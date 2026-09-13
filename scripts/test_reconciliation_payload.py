"""Invalid position truth must never clear a tracked, protected book."""
import sys
import pathlib
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'server/pulse'))
from pulse_trader import Pulse


class SnapshotValidation(unittest.TestCase):
    def test_invalid_success_payload_keeps_every_last_confirmed_value(self):
        for rows in (None, {}, '', [None], [{}], [{'positionAmt':'NaN'}],
                     [{'positionAmt':'inf'}], [{'positionAmt':1}],
                     [{'positionAmt':1,'symbol':'XRP-USDT','positionSide':'WRONG'}],
                     [{'positionAmt':0}, {'positionAmt':'broken'}]):
            with self.subTest(rows=rows):
                p=Pulse.__new__(Pulse)
                p.api=SimpleNamespace(get=lambda *a:{'code':0,'data':rows})
                p.record_event=lambda *a,**k:None
                p.open={'own':object()}; before=p.open.copy()
                p.live_pos_keys={'XRP-USDT:LONG'}
                p.exchange_open_count=1;p.exchange_total_open_count=1
                p._empty_rest_streak=1
                p.adopt_exchange_positions()
                self.assertEqual(p.open,before)
                self.assertEqual(p.live_pos_keys,{'XRP-USDT:LONG'})
                self.assertEqual(p.exchange_open_count,1)
                self.assertEqual(p.exchange_total_open_count,1)
                self.assertEqual(p._empty_rest_streak,0)
                self.assertFalse(p.recon_ok)
                self.assertEqual(p.recon_detail,'positions payload malformed')
                self.assertFalse(p._exchange_flat(SimpleNamespace(symbol='XRP-USDT',side='LONG')))

    def test_flatness_requires_a_valid_snapshot_for_the_exact_direction(self):
        p=Pulse.__new__(Pulse)
        pos=SimpleNamespace(symbol='XRP-USDT',side='LONG')
        for rows,expected in (([],True),
                ([{'symbol':'XRP-USDT','positionSide':'LONG','positionAmt':'1'}],False),
                ([{'symbol':'XRP-USDT','positionSide':'SHORT','positionAmt':'-1'}],True)):
            p.api=SimpleNamespace(get=lambda *args, data=rows:{'code':0,'data':data})
            self.assertEqual(p._exchange_flat(pos),expected)

    def test_external_close_delta_is_idempotent_and_pending_intents_win(self):
        from pulse_trader import confirmed_external_close_delta
        self.assertEqual(confirmed_external_close_delta(10, 7), 3)
        self.assertEqual(confirmed_external_close_delta(7, 7), 0)
        self.assertEqual(confirmed_external_close_delta(7, 8), 0)
        self.assertEqual(confirmed_external_close_delta(10, 7, 1), 0)

    def test_empty_open_order_snapshot_is_pending_before_confirming_zero(self):
        p = Pulse.__new__(Pulse)
        responses = iter([
            {"code": 0, "data": {"orders": [{"symbol": "XRP-USDT", "clientOrderId": "Gx02o-test"}]}},
            {"code": 0, "data": {"orders": []}},
            {"code": 0, "data": {"orders": []}},
        ])
        p.api = SimpleNamespace(path_cd={}, get=lambda _path: next(responses))
        p.open = {"own": object()}
        p._oo_cache = {}
        p._empty_order_streak = 0
        p._order_est = 0
        p._order_est_known = False
        p._note_foreign_activity = lambda: None
        p.ok = lambda row: row.get("code") == 0
        p.order_is_ours = lambda row: True

        p.list_orders()
        self.assertEqual(p.exchange_order_own_count, 1)
        p._oo_cache["*"] = (0.0, p._oo_cache["*"][1])
        p.list_orders()
        self.assertEqual(p.exchange_order_own_count, -1)
        self.assertTrue(p.exchange_order_snapshot_pending)
        p._oo_cache["*"] = (0.0, p._oo_cache["*"][1])
        p.list_orders()
        self.assertEqual(p.exchange_order_own_count, 0)
        self.assertEqual(p.exchange_order_total_count, 0)
        self.assertFalse(p.exchange_order_snapshot_pending)

    def test_pending_entry_is_ownership_proof_during_position_propagation(self):
        p = Pulse.__new__(Pulse)
        cid = "Gx02o-pending"
        p.pending_orders = {
            cid: {"kind": "entry", "symbol": "SOL-USDT", "side": "LONG",
                  "requested_qty": 2.0, "filled_qty": 0.0, "updated_at": 10.0}
        }
        self.assertTrue(p.pending_entry_owns("SOL-USDT", "LONG"))
        row = p.pending_entry_for("SOL-USDT", "LONG")
        self.assertIsNotNone(row)
        self.assertEqual(row["symbol"], "SOL-USDT")
        self.assertFalse(p.pending_entry_owns("SOL-USDT", "SHORT"))

if __name__=='__main__':unittest.main()
