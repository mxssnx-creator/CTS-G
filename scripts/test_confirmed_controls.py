"""No-network TP/SL fill lineage, cumulative price, and failed-response tests."""
import pathlib
import sys
import unittest
import threading
from types import SimpleNamespace
from unittest.mock import patch

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


class MissingPositionControls(unittest.TestCase):
    def setUp(self):
        self.p = p = pt.Pulse.__new__(pt.Pulse)
        p.ctrl_skip = {}; p.open = {}; p.contracts = {}; p._oo_cache = {}
        p.px = {'X-USDT':100., 'Y-USDT':100.}; p.last_px = {}
        p.position_key = lambda pos: pos.set_id
        p.per_config_controls = lambda pos: True
        p.clamp_ctrl_price = lambda pos, kind, price: price
        p.desired_sl_tp = lambda pos: (99.,102.,99.,102.)
        p.cid = lambda kind, pos: f'own-{kind}-{pos.set_id}'
        p.record_event = lambda *args, **kwargs: None
        self.posts = []; self.batches = []
        self.response = {'code':109420, 'msg':'position not exist'}
        self.batch_response = {'code':109420, 'msg':'position not exist'}
        p.api = SimpleNamespace(post=self.post, batch_place=self.batch, path_cd={})

    def post(self, path, body):
        self.posts.append(body)
        return self.response

    def batch(self, bodies):
        self.batches.append(bodies)
        return self.batch_response

    def position(self, name, side='LONG', symbol='X-USDT'):
        pos = pt.Position(symbol,side,3.,100.,90.,99.,102.,100.,set_id=name)
        self.p.open[name] = pos
        return pos

    def test_one_rejection_defers_250_sibling_sets_and_recovers_after_cooldown(self):
        with patch.object(pt.time, 'time', return_value=100.):
            positions = [self.position(str(i)) for i in range(250)]
            for pos in positions:
                self.p.place_ctrl(pos,'sl',99.)
                self.p.place_ctrl(pos,'tp',102.)
                self.p.place_ctrl_pair(pos)
                self.p.ensure_controls(pos)
            self.assertEqual(len(self.posts),1)
            self.assertEqual(self.batches,[])
            self.assertEqual(len(self.p.open),250)
            self.assertTrue(self.p.recon_pending)
            # Other sides and symbols retain independent protection admission.
            self.p.place_ctrl(self.position('short','SHORT'),'sl',101.)
            self.p.place_ctrl(self.position('other',symbol='Y-USDT'),'sl',99.)
            self.assertEqual(len(self.posts),3)
        self.response={'code':0,'data':{'orderId':'restored-sl'}}
        with patch.object(pt.time, 'time', return_value=161.):
            self.assertEqual(self.p.place_ctrl(positions[0],'sl',99.),'restored-sl')
            self.assertEqual(len(self.posts),4)

    def test_batch_missing_position_does_not_fall_back_or_cancel_existing_controls(self):
        pos=self.position('base')
        self.p.place_ctrl_pair(pos)
        self.p.place_ctrl_pair(self.position('sibling'))
        self.assertEqual(len(self.batches),1)
        self.assertEqual(self.posts,[])
        pos.sl_oid='existing-sl'
        self.assertEqual(self.p.place_ctrl(pos,'sl',99.),'existing-sl')
        self.assertEqual(pos.sl_oid,'existing-sl')

    def test_partial_batch_preserves_success_and_stops_missing_position_fallback(self):
        self.batch_response={'code':0,'data':{'orders':[
            {'code':0,'type':'STOP_MARKET','orderId':'accepted-sl'},
            {'code':109420,'msg':'position not exist'}]}}
        pos=self.position('partial')
        self.p.place_ctrl_pair(pos)
        self.assertEqual(pos.sl_oid,'accepted-sl')
        self.assertFalse(pos.tp_oid)
        self.assertFalse(pos.controls_ok)
        self.assertEqual(self.posts,[])

    def test_error_envelope_cannot_confirm_nested_order_ids(self):
        self.batch_response={'code':109420,'data':{'orders':[
            {'code':0,'type':'STOP_MARKET','orderId':'untrusted-sl'}]}}
        pos=self.position('error')
        self.p.place_ctrl_pair(pos)
        self.assertFalse(pos.sl_oid)
        self.assertEqual(self.posts,[])

    def test_minimum_rejection_stops_forms_and_equal_size_siblings_then_retries(self):
        self.response = {'code':110422, 'msg':'The minimum size per order is 2.27 USDT.'}
        with patch.object(pt.time, 'time', return_value=100.):
            positions = [self.position(str(i)) for i in range(250)]
            for pos in positions:
                self.p.place_ctrl(pos, 'sl', 99.)
                self.p.place_ctrl(pos, 'tp', 102.)
                self.p.place_ctrl_pair(pos)
                self.p.ensure_controls(pos)
            self.assertEqual(len(self.posts), 1)
            self.assertEqual(self.batches, [])
            self.assertEqual(len(self.p.open), 250)
            self.assertTrue(all(not p.controls_ok for p in positions))
            self.response = {'code':0, 'data':{'orderId':'resized-sl'}}
            positions[1].qty = 4.
            self.assertEqual(self.p.place_ctrl(positions[1], 'sl', 99.), 'resized-sl')
        with patch.object(pt.time, 'time', return_value=161.):
            self.assertEqual(self.p.place_ctrl(positions[0], 'sl', 99.), 'resized-sl')
            self.assertEqual(len(self.posts), 3)

    def test_minimum_partial_batch_keeps_accepted_control_without_fallback(self):
        self.batch_response = {'code':0, 'data':{'orders':[
            {'code':0, 'type':'STOP_MARKET', 'orderId':'accepted-sl'},
            {'code':110422, 'msg':'The minimum size per order is 2.27 USDT.'}]}}
        pos = self.position('partial-minimum')
        self.p.place_ctrl_pair(pos)
        self.p.place_ctrl_pair(self.position('same-size'))
        self.assertEqual(pos.sl_oid, 'accepted-sl')
        self.assertFalse(pos.tp_oid)
        self.assertFalse(pos.controls_ok)
        self.assertEqual(len(self.batches), 1)
        self.assertEqual(self.posts, [])

    def test_minimum_batch_envelope_never_trusts_nested_ids(self):
        self.batch_response = {'code':110422, 'msg':'The minimum size per order is 2.27 USDT.',
                               'data':{'orders':[{'code':0,'type':'STOP_MARKET','orderId':'untrusted'}]}}
        pos = self.position('minimum-envelope')
        self.p.place_ctrl_pair(pos)
        self.p.place_ctrl_pair(pos)
        self.assertFalse(pos.sl_oid)
        self.assertEqual(len(self.batches), 1)
        self.assertEqual(self.posts, [])


class AggregateLaneControls(unittest.TestCase):
    def setUp(self):
        p = self.p = pt.Pulse.__new__(pt.Pulse)
        p.control_orders_per_config = False
        p.control_orders = True
        p.system_id = pt.SYSTEM_ID
        p.connection = pt.CONN_SHORT
        p.tracking_scope = pt.TRACKING_SCOPE
        p.open = {}
        p.px = {'XRP-USDT': 100.0}
        p.last_px = {}
        p.contracts = {'XRP-USDT': pt.Contract('XRP-USDT', 0.001, 0.001, 3, 2, 2.0, 150)}
        p.sl_min = 0.0015
        p.sl_max = 0.02
        p.tp_min = 0.003
        p.tp_max = 0.02
        p.exits = SimpleNamespace(enabled=False, opt_sl_min=0.001, opt_sl_max=0.009)
        p.ctrl_skip = {}
        p.pending_orders = {}
        p.sets = SimpleNamespace(sets={})

    def position(self, side='LONG', sl=.004, tp=.006, lane='lane-a'):
        return pt.Position(
            'XRP-USDT', side, 1.0, 100.0, 1.0, 99.6 if side == 'LONG' else 100.4,
            100.6 if side == 'LONG' else 99.4, 100.0,
            sl_pct=sl, tp_pct=tp, execution_lane=lane, set_id=lane,
            pack='general', client_id=f'own-{lane}', ours=True, set_idx=1,
            system_id=pt.SYSTEM_ID, connection=pt.CONN_SHORT, tracking_scope=pt.TRACKING_SCOPE,
        )

    def test_aggregate_lanes_only_deduplicate_exact_lane(self):
        first = self.position(lane='lane-a')
        self.p.open[self.p.position_key(first)] = first
        self.assertTrue(self.p.occupying('XRP-USDT', 'LONG', 'general', execution_lane='lane-a'))
        self.assertFalse(self.p.occupying('XRP-USDT', 'LONG', 'general', execution_lane='lane-b'))
        self.assertFalse(self.p.occupying('XRP-USDT', 'SHORT', 'general', execution_lane='lane-a'))

    def test_aggregate_range_widens_and_payload_is_quantity_free(self):
        first = self.position(sl=.004, tp=.006, lane='lane-a')
        second = self.position(sl=.009, tp=.012, lane='lane-b')
        self.p.prepare_position_group(first)
        self.p.prepare_position_group(second)
        self.p.merge_position(first, second)
        self.assertAlmostEqual(first.aggregate_sl_pct, .009)
        self.assertAlmostEqual(first.aggregate_tp_pct, .012)
        sl, tp, _, _ = self.p.desired_sl_tp(first)
        self.assertAlmostEqual(sl, 99.1, places=2)
        self.assertAlmostEqual(tp, 101.2, places=2)
        body = self.p._ctrl_body(first, 'sl', sl)
        self.assertEqual(body.get('closePosition'), 'true')
        self.assertNotIn('quantity', body)
        self.assertTrue(self.p.cid('u', pos=first).startswith(pt.TAG + 'ua'))

    def test_strategy_prefixes_keep_general_trailing_and_block_lanes_independent(self):
        selected = SimpleNamespace(id='set-1')
        normal = self.p.execution_lane_key('general', 'signal', selected, 'normal')
        trailing = self.p.execution_lane_key('general', 'signal', selected, 'trailing')
        block = self.p.execution_lane_key('general', 'signal', selected, 'block-active')
        self.assertNotEqual(normal, trailing)
        self.assertNotEqual(normal, block)
        self.assertTrue(normal.startswith('normal:'))
        self.assertTrue(block.startswith('block-active:'))
        legacy = self.position(lane=normal.removeprefix('normal:'))
        self.p.open['legacy'] = legacy
        self.assertTrue(self.p.occupying('XRP-USDT', 'LONG', 'general', execution_lane=normal))
        self.assertFalse(self.p.occupying('XRP-USDT', 'LONG', 'general', execution_lane=trailing))

    def test_per_config_opt_in_keeps_quantity_matched_controls(self):
        self.p.control_orders_per_config = True
        pos = self.position(lane='lane-a')
        self.p.prepare_position_group(pos)
        body = self.p._ctrl_body(pos, 'sl', 99.6)
        self.assertNotIn('closePosition', body)
        self.assertIn('quantity', body)
        self.assertEqual(pos.control_range_key, 'sl0040-tp0060')


if __name__=='__main__':unittest.main()
