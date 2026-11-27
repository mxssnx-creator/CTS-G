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
            original_get=p.api.get
            order_reads=[]
            p.our_orders=lambda symbol:order_reads.append(symbol) or []
            pos.set_id='general:L:1m:sl0.75:st9'
            def get(path,params=None):
                if path.endswith('/user/positions'):
                    return {'code':0,'data':[{'symbol':sym,'positionSide':pos.side,
                        'positionAmt':str(pos.qty),'avgPrice':'.003616'}]}
                return original_get(path,params)
            p.api.get=get;p.adopt_exchange_positions()
            self.assertEqual(pos.entry,.003617)
            self.assertEqual(pos.base_entry,.003617)
            self.assertEqual(pos.notional,pos.qty*.003617)
            self.assertEqual(order_reads, [])
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

    def test_dca_ack_partial_repeat_restart_and_final(self):
        cid = pt.TAG+"dCase"
        pos = pt.Position("A-USDT","LONG",1,100,1000,99,102,100,base_entry=100,base_qty=1,sl_pct=.01,
            entry_fee_usdt=.05,block_pending={"strategy":"dca","rung":1,"qty":.1,"t":1001,"client_id":cid})
        pulse = pt.Pulse.__new__(pt.Pulse)
        pulse.dca = pt.DcaBook()
        pulse.dca.load({"fixedSlBands":{}, "dcaEnabled":True, "dcaMaxExposureX":1.1})
        pulse.save_open_book = lambda: True
        pulse.fill_cost = lambda data,q,px,*a: (q*px*.0005,"taker-rate")
        row = dict(symbol=pos.symbol,positionSide=pos.side,side="BUY",clientOrderId=cid,orderId="901",
                   status="NEW",origQty=".1",avgPrice="99.4")
        pulse.api = SimpleNamespace(get=lambda path,params: {"code":0,"data":{"order":row.copy()}})
        self.assertFalse(pulse.reconcile_block_pending(pos,1.1,row=row))
        self.assertEqual(pos.qty,1)
        row.update(status="PARTIALLY_FILLED",executedQty=".04")
        self.assertTrue(pulse.reconcile_block_pending(pos,1.04,row=row))
        self.assertTrue(pulse.reconcile_block_pending(pos,1.04,row=row))
        self.assertEqual(len(pos.dca_fills),1)
        self.assertEqual(pos.dca_fills[0],[99.4,.04])
        pos = pt.Position(**pt.asdict(pos))
        pulse.dca = pt.DcaBook()
        pulse.dca.load({"fixedSlBands":{}, "dcaEnabled":True})
        row.update(status="FILLED",executedQty=".1",avgPrice="99.2")
        self.assertTrue(pulse.reconcile_block_pending(pos,1.1,row=row))
        self.assertFalse(pos.block_pending)
        self.assertEqual(pos.block_fills,[])
        self.assertEqual(pos.dca_fills,[[99.2,.1]])
        self.assertAlmostEqual(pos.qty,1.1)
        self.assertAlmostEqual(pos.entry,(100+9.92)/1.1)
        self.assertAlmostEqual(pos.entry_fee_usdt,.05+9.92*.0005)
        restored = pulse.dca.restore_position(pos,.01)
        self.assertTrue(restored.steps[0].filled)
        self.assertEqual(restored.steps[0].qty,.1)
        self.assertIsNone(pulse.dca.due(pos.symbol,pos.side,1,100,98,now=1100,sl_frac=.01))

    def test_dca_evidence_is_owned_by_exact_path(self):
        book = pt.DcaBook()
        book.load({"dcaEnabled":True, "fixedSlBands":{}, "dcaMinPf":1.2, "minPfLinked":False})
        for i in range(10):
            receipt = {"scope":"A","pnl_pct":-.005,"dca_n":1,"client_id":str(i)}
            book.on_close(receipt)
            book.on_close(receipt)
            book.on_close({**receipt,"scope":"B","pnl_pct":.01})
        self.assertEqual(book.score("A")["count"],10)
        self.assertFalse(book.score("A")["active"])
        self.assertTrue(book.score("B")["active"])
        self.assertEqual(book.score("cold")["count"],0)

    def test_dca_add_requires_this_symbols_exact_qualified_exec_owner(self):
        pulse = pt.Pulse.__new__(pt.Pulse)
        pulse.dca = pt.DcaBook()
        pulse.dca.on_sets = True
        owners = {"qualified": []}
        pulse.sets = SimpleNamespace(stage_grid=True, sets={"set":object()},
            held_exec_stream=lambda state, identity: owners.get(identity))
        pos = pt.Position("X-USDT", "SHORT", 1, 100, 1000, 101, 99, 100, set_id="set")
        self.assertFalse(pulse.dca_eligible(pos))
        pos.exec_lane_id = "qualified"
        self.assertTrue(pulse.dca_eligible(pos))
        owner = pulse.dca_scope(pos)
        pos.symbol = "Y-USDT"
        self.assertNotEqual(pulse.dca_scope(pos), owner)
        pos.exec_lane_id = "sibling"
        self.assertFalse(pulse.dca_eligible(pos))
        pos.exec_lane_id = "qualified"
        pos.close_pending = {"t":1001}
        self.assertFalse(pulse.dca_eligible(pos))

    def test_direct_block_partial_fill_recovers_across_two_books(self):
        import tempfile
        from block_engine import BlockBook
        with tempfile.TemporaryDirectory() as tmp:
            cid = pt.TAG + "bPaper"
            pulse = pt.Pulse.__new__(pt.Pulse)
            pulse.block = BlockBook(str(Path(tmp) / "block.json"), {})
            pulse.save_open_book = lambda: True
            pulse.fill_cost = lambda *a: (0., "taker-rate")
            rule = dict(blockCount=1, targetAddQty=.4, targetBlockQty=1.4, setKey="ind", requestedAddQty=.4)
            pos = pt.Position("X-USDT","SHORT",1,100,1000,101,99,100,
                block_pending=dict(strategy="direct-block",qty=.4,client_id=cid,t=1001,group=1,baseQty=1,baseEntry=100,rule=rule))
            before = deepcopy(pos)
            row = dict(symbol=pos.symbol, positionSide="SHORT", side="SELL", clientOrderId=cid, orderId="92",
                       status="PARTIALLY_FILLED", executedQty=".15", avgPrice="99")
            self.assertTrue(pulse.reconcile_block_pending(pos,1.15,row=row))
            self.assertTrue(pulse.reconcile_block_pending(before,1.15,row=row))
            row.update(status="FILLED",executedQty=".4",avgPrice="98.90")
            self.assertTrue(pulse.reconcile_block_pending(before,1.4,row=row))
            lane = pulse.block.lanes[pulse.block.key(before.symbol,before.side)]
            self.assertAlmostEqual(lane.confirmed_add,.4)
            self.assertAlmostEqual(before.qty,1.4)
            self.assertAlmostEqual(sum(f["qty"] for f in before.block_fills),.4)
            self.assertAlmostEqual(before.entry,(100+.4*98.9)/1.4)
            self.assertFalse(before.block_pending)

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
