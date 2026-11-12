"""Exact position lineage recovers external exits without adopting their orders."""
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server/pulse'))
from position_settlement import verify_position_settlement
import pulse_trader as pt


def sample():
    pos=SimpleNamespace(ours=True,order_id='101',client_id='Gown',position_id='',symbol='CT-USDT',
        side='SHORT',qty=18.9,entry=.10597,opened_at=1000,block_fills=[],dca_fills=[],close_filled_qty=0)
    entry=dict(orderId='101',clientOrderId='gown',positionID='202',symbol='CT-USDT',
               positionSide='SHORT',side='SELL',executedQty='18.90')
    row=dict(positionId='202',symbol='CT-USDT',positionSide='SHORT',closeAllPositions=True,
             positionAmt='18.90',closePositionAmt='18.90',avgPrice='.10597',avgClosePrice='.10662',
             positionCommission='-.0020089755',totalFunding='0',openTime=999000,
             updateTime=1100000,realisedProfit='-.0123',netProfit='-.0143')
    return pos,entry,row


class Settlement(unittest.TestCase):
    def test_precise_cash_uses_price_and_total_fee_instead_of_rounded_net_display(self):
        pos,entry,row=sample();r=verify_position_settlement(pos,entry,[row],1200)
        self.assertAlmostEqual(r['gross'],-.012285,14)
        self.assertAlmostEqual(r['net'],-.0142939755,14)
        self.assertEqual(r['closedAt'],1100)
        self.assertNotEqual(r['net'],float(row['netProfit']))

    def test_identity_mismatches_never_fall_back_to_geometry(self):
        for target,key,value in [('entry','clientOrderId','foreign'),('entry','positionSide','LONG'),
                                 ('entry','orderId','999'),('row','positionId','999'),('row','symbol','OTHER-USDT')]:
            pos,entry,row=sample();(entry if target=='entry'else row)[key]=value
            self.assertIsNone(verify_position_settlement(pos,entry,[row],1200))

    def test_mixed_or_partial_position_history_cannot_settle_the_owned_parent(self):
        for key,value in [('positionAmt','20'),('closePositionAmt','10'),('avgPrice','.11'),('closeAllPositions',False)]:
            pos,entry,row=sample();row[key]=value
            self.assertIsNone(verify_position_settlement(pos,entry,[row],1200))
        pos,entry,row=sample();pos.block_fills=[[.1,1]]
        self.assertIsNone(verify_position_settlement(pos,entry,[row],1200))

    def test_nonfinite_or_future_receipts_never_establish_a_settlement(self):
        for key,value in [('avgClosePrice','NaN'),('positionCommission','Infinity'),('updateTime',1300000)]:
            pos,entry,row=sample();row[key]=value
            self.assertIsNone(verify_position_settlement(pos,entry,[row],1200))

    def test_native_repeated_reconciliation_assigns_total_fees_once_and_only_reads(self):
        raw,entry,row=sample();entry['clientOrderId']=pt.TAG+'oFixture'
        pos=pt.Position(raw.symbol,raw.side,raw.qty,raw.entry,raw.opened_at,.12,.10,raw.entry,
                        order_id=raw.order_id,client_id=entry['clientOrderId'],entry_fee_usdt=.0010014165)
        p=pt.Pulse.__new__(pt.Pulse);paths=[];p.note_manual_close=lambda *args:None
        def get(path,params):
            paths.append(path)
            return {'code':0,'data':{'order':entry}if path.endswith('/order')else{'positionHistory':[deepcopy(row)]}}
        p.api=SimpleNamespace(get=get)
        for _ in range(2):
            result=p.verified_position_settlement(pos)
            self.assertIsNotNone(result)
            self.assertAlmostEqual(pos.entry_fee_usdt+pos.exit_fee_usdt,.0020089755,14)
            self.assertEqual(pos.exit_fee_qty,pos.qty)
            self.assertFalse(pos.exit_price_estimated)
        self.assertEqual(paths.count('/openApi/swap/v1/trade/positionHistory'),2)
        self.assertEqual(pos.exit_origin,'external')
        self.assertEqual(pos.exit_confirmed_at,1100)

    def test_final_native_partial_close_keeps_its_origin_and_uses_precise_total_fees(self):
        raw,entry,row=sample();entry['clientOrderId']=pt.TAG+'oFixture'
        pos=pt.Position(raw.symbol,raw.side,raw.qty,raw.entry,raw.opened_at,.12,.10,raw.entry,
                        order_id=raw.order_id,client_id=entry['clientOrderId'],entry_fee_usdt=.001001,
                        close_filled_qty=raw.qty/2)
        p=pt.Pulse.__new__(pt.Pulse)
        p.note_manual_close=lambda *args:self.fail('a native final settlement became an external manual close')
        p.api=SimpleNamespace(get=lambda path,params:{'code':0,'data':{'order':entry}if path.endswith('/order')else{'positionHistory':[row]}})
        self.assertIsNone(p.verified_position_settlement(pos))
        result=p.verified_position_settlement(pos,external=False)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(pos.entry_fee_usdt+pos.exit_fee_usdt,.0020089755,14)
        self.assertNotEqual(pos.exit_origin,'external')
        self.assertEqual(pos.exit_confirmed_at,1100)


if __name__=='__main__':unittest.main(verbosity=2)
