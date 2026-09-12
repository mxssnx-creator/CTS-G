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

if __name__=='__main__':unittest.main()
