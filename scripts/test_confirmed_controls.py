"""No-network TP/SL fill lineage, cumulative price, and failed-response tests."""
import pathlib
import sys
import unittest
import threading
from types import SimpleNamespace

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'/'pulse'))
import pulse_trader as pt


class ControlFills(unittest.TestCase):
    def setUp(self):
        self.p=p=pt.Pulse.__new__(pt.Pulse)
        self.pos=SimpleNamespace(symbol='XRP-USDT',side='LONG',qty=3.,ours=True,client_id='own-parent',
                                 sl_oid='101',tp_oid='102',sec_sl_oid='',sec_tp_oid='',control_group_key='group',pending_close_qty=0)
        p.open={'group':self.pos}; p.px={'XRP-USDT':999.}; p.pending_orders={}; p.seen_fill_cids=set()
        p.cid_ours=lambda cid:cid.startswith('own-'); p._save_pending_orders=lambda:None
        p._position_for_client=lambda cid:next((pos for pos in p.open.values() if pos.client_id==cid),None)
        p.control_event_fields=lambda pos:{}; p.record_event=lambda *args,**kwargs:None
        p.position_for_group=lambda key:(_ for _ in ()).throw(AssertionError('control must use parent'))
        p.variants=SimpleNamespace(current_sl=lambda:1);p.sl_min=.001;p.sl_max=.02;p.tp_min=.002;p.tp_max=.03
        p.position_cost_pct=.15;p.tp_cost_ratio=5
        self.fills=[]
        def close(pos,quantity,price,*args,**kwargs):
            self.fills.append((quantity,price));pos.qty-=quantity
            if pos.qty<=0:p.open.clear()
            return quantity
        p._record_close_fill=close
        self.order=dict(symbol='XRP-USDT',positionSide='LONG',orderId='102',executedQty=1,avgPrice=100,status='PARTIALLY_FILLED')

    def sync(self,order=None):return self.p._sync_control_fill(order or self.order,'own-tp',{'kind':'t'})

    def test_partial_repeated_and_final_use_delta_quantity_and_price(self):
        self.assertTrue(self.sync());self.assertFalse(self.sync())
        self.assertTrue(self.sync(dict(self.order,executedQty=3,avgPrice=102,status='FILLED')))
        self.assertEqual(self.fills,[(1,100),(2,103)])
        self.assertFalse(self.sync(dict(self.order,executedQty=3,avgPrice=102,status='FILLED')))
        self.assertFalse(self.p.open);self.assertFalse(self.p.pending_orders)

    def test_missing_execution_zero_price_foreign_ambiguous_never_applied(self):
        for patch in ({'executedQty':0},{'executedQty':None,'quantity':3},{'avgPrice':0,'price':100},
                      {'avgPrice':'NaN'},{'orderId':'999'},{'positionSide':'SHORT'},{'symbol':'SOL-USDT'}):
            self.assertFalse(self.sync(dict(self.order,**patch)))
        self.pos.ours=False;self.assertFalse(self.sync());self.pos.ours=True
        self.p.open['other']=SimpleNamespace(**vars(self.pos));self.assertFalse(self.sync())
        self.assertEqual(self.fills,[]);self.assertEqual(self.p.pending_orders,{})

    def test_persisted_partial_survives_replaced_control_but_not_new_parent(self):
        self.assertTrue(self.sync());self.pos.tp_oid='new-control'
        self.assertTrue(self.sync(dict(self.order,executedQty=2,avgPrice=101)))
        self.pos.client_id='own-new-parent'
        self.assertFalse(self.sync(dict(self.order,executedQty=3,avgPrice=102)))
        self.assertEqual(self.fills,[(1,100),(1,102)])

    def test_failed_response_cannot_change_costs_or_fallback_poll(self):
        calls=[];self.p.api=SimpleNamespace(get=lambda *args:calls.append(args) or {'code':109429,'data':[self.order]})
        self.p._update_live_position_costs=lambda orders:self.fail('error payload cannot update costs')
        self.p.sync_own_fills();self.assertEqual(len(calls),1);self.assertEqual(self.fills,[])

    def test_fallback_accepts_list_payload(self):
        responses=iter([{'code':0,'data':[]},{'code':0,'data':[]}])
        self.p.api=SimpleNamespace(get=lambda *args:next(responses))
        costs=[];self.p._update_live_position_costs=lambda rows:costs.append(rows)
        self.p.sync_own_fills();self.assertEqual(costs,[[]])

    def test_warm_network_wait_does_not_lock_stats(self):
        entered=threading.Event();release=threading.Event();completed=[]
        self.p._state_lock=threading.RLock();self.p.last_bal=0
        self.p.refresh_balance=lambda: (entered.set(), release.wait(timeout=2))
        for name in ('refresh_klines','refresh_vol1h','process_indications','update_regime'):
            setattr(self.p,name,lambda:completed.append(True))
        worker=threading.Thread(target=self.p._warm_pass)
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=1))
            acquired=self.p._state_lock.acquire(timeout=.2)
            if acquired:self.p._state_lock.release()
            self.assertTrue(acquired,'warm REST blocked the stats lock')
        finally:
            release.set();worker.join(timeout=2)
        self.assertFalse(worker.is_alive());self.assertEqual(len(completed),4)

    def test_rollup_survives_new_symbol_during_iteration(self):
        entered=threading.Event();release=threading.Event();errors=[]
        class WaitingBars(list):
            def __len__(self):
                entered.set();release.wait(timeout=2)
                return super().__len__()
        self.p.klines_tf={'1m':{'first':WaitingBars([[1,2,.5,1,1]]*75)}}
        def run():
            try:self.p.rollup_tf()
            except Exception as e:errors.append(e)
        worker=threading.Thread(target=run);worker.start()
        try:
            self.assertTrue(entered.wait(timeout=1))
            self.p.klines_tf['1m']['new']=[[1,2,.5,1,1]]*75
        finally:
            release.set();worker.join(timeout=2)
        self.assertEqual(errors,[]);self.assertFalse(worker.is_alive())
        self.assertIn('first',self.p.klines_tf['5m'])


if __name__=='__main__':unittest.main()
