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
               positionSide='SHORT',side='SELL',executedQty='18.90',avgPrice='.10597')
    row=dict(positionId='202',symbol='CT-USDT',positionSide='SHORT',closeAllPositions=True,
             positionAmt='18.90',closePositionAmt='18.90',avgPrice='.10597',avgClosePrice='.10662',
             positionCommission='-.0020089755',totalFunding='0',openTime=999000,
             updateTime=1100000,realisedProfit='-.0123',netProfit='-.0143')
    return pos,entry,row


class Settlement(unittest.TestCase):
    def test_position_snapshot_cannot_round_an_owned_fill_entry(self):
        sys.path.insert(0,str(Path(__file__).resolve().parent/'regression'))
        from trader import _isolated,_StubExchange,_enter
        def run():
            p,sym=_enter(_StubExchange(executed='1'))
            pos=p.open[sym];pos.entry=pos.base_entry=.003617
            original_get=p.api.get;p.our_orders=lambda symbol:[]
            def get(path,params=None):
                if path.endswith('/user/positions'):
                    return {'code':0,'data':[{'symbol':sym,'positionSide':pos.side,
                        'positionAmt':str(pos.qty),'avgPrice':'.003616'}]}
                return original_get(path,params)
            p.api.get=get;p.adopt_exchange_positions()
            self.assertEqual(pos.entry,.003617)
            self.assertEqual(pos.base_entry,.003617)
            self.assertEqual(pos.notional,pos.qty*.003617)
        _isolated(run)

    def test_pending_block_partial_receipts_are_incremental_and_never_use_snapshot_price(self):
        cid=pt.TAG+'bCase'
        pos=pt.Position('A-USDT','LONG',1,100,1000,99,103,100,
            client_id=pt.TAG+'oCase',entry_fee_usdt=.05,
            block_pending={'group':0,'qty':1,'client_id':cid,'t':1001})
        order=dict(symbol=pos.symbol,positionSide='LONG',side='BUY',clientOrderId=cid.lower(),
                   orderId='900',status='PARTIALLY_FILLED',executedQty='.4',avgPrice='110')
        p=pt.Pulse.__new__(pt.Pulse);p.last_error='';p.save_open_book=lambda:True
        p.api=SimpleNamespace(get=lambda path,params:{'code':0,'data':{'order':deepcopy(order)}})
        p.fill_cost=lambda data,qty,price,*args:(qty*price*.0005,'taker-rate')
        self.assertTrue(p.reconcile_block_pending(pos,1.4))
        self.assertAlmostEqual(pos.entry,144/1.4,12)
        self.assertAlmostEqual(pos.entry_fee_usdt,.072,12)
        self.assertTrue(p.reconcile_block_pending(pos,1.4))
        self.assertEqual(len(pos.block_fills),1)
        self.assertAlmostEqual(pos.entry_fee_usdt,.072,12)
        order.update(status='FILLED',executedQty='1',avgPrice='109')
        self.assertTrue(p.reconcile_block_pending(pos,2))
        self.assertEqual(pos.qty,2);self.assertEqual(pos.entry,104.5)
        self.assertAlmostEqual(pos.entry_fee_usdt,.1045,12)
        self.assertAlmostEqual(sum(r['qty']*r['price']for r in pos.block_fills),109,12)
        self.assertFalse(pos.block_pending)

    def test_pending_block_cannot_adopt_a_foreign_or_oversized_fill(self):
        for change in({'clientOrderId':'foreign'},{'executedQty':'2'},{'avgPrice':'NaN'}):
            cid=pt.TAG+'bCase';pos=pt.Position('A-USDT','LONG',1,100,1000,99,103,100,
                block_pending={'group':0,'qty':1,'client_id':cid,'t':1001})
            order=dict(symbol=pos.symbol,positionSide='LONG',side='BUY',clientOrderId=cid.lower(),
                orderId='900',status='FILLED',executedQty='1',avgPrice='110',**{})
            order.update(change)
            p=pt.Pulse.__new__(pt.Pulse);p.last_error=''
            p.api=SimpleNamespace(get=lambda *args:{'code':0,'data':{'order':order}})
            self.assertFalse(p.reconcile_block_pending(pos,2))
            self.assertEqual((pos.qty,pos.entry),(1,100));self.assertFalse(pos.block_fills)

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

    def test_old_rounded_book_can_recover_its_confirmed_original_base_entry(self):
        raw,entry,row=sample();entry['clientOrderId']=pt.TAG+'oFixture'
        pos=pt.Position(raw.symbol,raw.side,raw.qty,.10596,raw.opened_at,.12,.10,raw.entry,
            order_id=raw.order_id,client_id=entry['clientOrderId'],entry_fee_usdt=.0010014165,
            base_entry=raw.entry,base_qty=raw.qty)
        p=pt.Pulse.__new__(pt.Pulse)
        p.api=SimpleNamespace(get=lambda path,params:{'code':0,'data':{'order':entry}if path.endswith('/order')else{'positionHistory':[row]}})
        result=p.verified_position_settlement(pos,external=False)
        self.assertIsNotNone(result)
        self.assertEqual(pos.entry,raw.entry)
        self.assertAlmostEqual(result['net'],-.0142939755,14)

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
