"""Offline policy and exchange-boundary tests; no real orders."""
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server' / 'pulse'))
import pulse_trader as pt
from block_active import adjusted_quantity, observe_continuation
from block_engine import BlockBook
from set_engine import SetBook, SetState


class BlockActiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = self.p = pt.Pulse.__new__(pt.Pulse)
        p.block = BlockBook(self.tmp.name + '/block.json', {})
        p.block_active, p.strat_block, p.control_orders, p.recon_ok = True, True, True, True
        p.normal_execution_enabled = False
        self.view = dict(last15_n=12, last15_ratio=1.8, net_avg=.1, max_dd_s=10)
        self.st = NS(id='general:sl0.6:st3', active=True, sl_ratio=.6, tp_pct=.0045,
                     idx=0, kind='base', step=3, parent_set_id='', volume_ratio=1)
        p.sets = NS(enabled=True, progress=NS(ready=True), eval_need=lambda: 8,
                    real_min_pf=1.15, max_dd_s=27000, _side_view=lambda *_: self.view,
                    pick_any=lambda *a, **k: self.st)
        p.coord = NS(min_pf=1.05, gate=lambda *a, **k: (True, [], {}))
        p.strategy_closes = lambda: []
        p.live_recent_pf = lambda *a, **k: None
        p._coord_add_state = lambda **k: (True, 6, 1.8, [])
        p.positions_for = lambda *a: []
        p.pending_orders = {}
        p._block_reference_anchors = {('X-USDT','LONG',self.st.id):
                                     dict(at=0, seen=50, price=100, direction=1)}

    def plan(self):
        with patch.object(pt.time, 'time', return_value=60):
            return self.p.block_active_plan('X-USDT', 'LONG', self.st, 8, 100.3)

    def test_virtual_parent_emits_only_increment(self):
        r = self.plan()
        self.assertEqual(r['requestedQty'], 2)
        self.assertEqual(r['referenceQty'], 8)
        self.assertEqual(r['normalQtyExecuted'], 0)
        self.assertEqual(r['blockCount'], 1)

    def test_overall_owned_and_pending_deducted(self):
        self.p.positions_for = lambda *a: [NS(qty=.5)]
        self.p.pending_orders = {'a': dict(symbol='X-USDT', side='LONG', kind='entry', requested_qty=1, filled_qty=.25)}
        self.assertEqual(self.plan()['requestedQty'], .75)

    def test_no_order_when_every_target_satisfied(self):
        self.p.positions_for = lambda *a: [NS(qty=8)]
        self.assertIsNone(self.plan())

    def test_all_counts_and_ratios_remain_additive(self):
        for count in range(1, 7):
            for ratio in (.05, .25, .5, 1, 2):
                self.p.block.counts = [count]
                self.p.block.volume_ratio = ratio
                r = self.plan()
                self.assertAlmostEqual(r['requestedQty'], 8 * min(1, count * ratio))

    def test_each_qualification_and_control_gate_is_enforced(self):
        for key, value in [('last15_n',7), ('last15_ratio',1.14), ('last15_ratio',float('nan')),
                           ('net_avg',0), ('net_avg',float('nan')), ('max_dd_s',27001), ('max_dd_s',float('nan'))]:
            before = self.view[key]; self.view[key] = value
            self.assertIsNone(self.plan(), key); self.view[key] = before
        for obj, key in [(self.p,'control_orders'), (self.p,'recon_ok'), (self.p,'block_active'),
                         (self.p.block,'enabled'), (self.p.block,'active_live'), (self.p.block,'active_real'), (self.st,'active'),
                         (self.p.sets.progress,'ready')]:
            before = getattr(obj,key); setattr(obj,key,False)
            self.assertIsNone(self.plan(),key); setattr(obj,key,before)

    def test_overall_coordination_is_hard_gate(self):
        self.p.coord.gate = lambda *a, **k: (False,['pause'],{})
        self.assertIsNone(self.plan())

    def test_negative_live_book_blocks(self):
        self.p.live_recent_pf = lambda *a, **k: 1.04
        self.assertIsNone(self.plan())

    def test_configured_live_pf_floor_is_inclusive(self):
        self.p.live_recent_pf = lambda *a, **k: 1.05
        self.assertIsNotNone(self.plan())
        self.p.coord.min_pf = 1.20
        self.assertIsNone(self.plan())

    def test_loss_blocks_only_its_own_count(self):
        self.p.strategy_closes = lambda: [NS(parent_set_id=self.st.id, axis_key='block-active:1',
                                              symbol='X-USDT', side='LONG', pnl=-1)]
        self.assertEqual(self.plan()['blockCount'], 2)

    def test_restart_stale_reverse_and_reversal_restart_observation(self):
        a={}
        self.assertFalse(observe_continuation(a,'a',100,1,0))
        self.assertFalse(observe_continuation(a,'a',101,1,44))
        self.assertTrue(observe_continuation(a,'a',101,1,45))
        self.assertFalse(observe_continuation(a,'a',99,1,46))
        self.assertFalse(observe_continuation(a,'a',99,-1,47))
        self.assertTrue(observe_continuation(a,'a',98,-1,92))
        self.assertFalse(observe_continuation(a,'a',97,-1,300))
        self.assertFalse(observe_continuation({},'a',97,-1,400))

    def test_invalid_and_excess_quantities_fail_closed(self):
        for v in [float('nan'),float('inf'),-1]:
            self.assertEqual(adjusted_quantity(v,.25),0)
            self.assertEqual(adjusted_quantity(8,v),0)
        self.assertEqual(adjusted_quantity(8,2),8)
        self.assertEqual(adjusted_quantity(8,.25,3),0)

    def entry_fixture(self):
        p=self.p
        p.entries_blocked=lambda:False; p.halted=False; p.cooldown={}; p.available=1000
        p.last_entry_ts=0; p.open={}; p.control_orders_per_config=True
        p.ignore_syms={}; p.group_of=lambda s:'g'; p.group_count=lambda g:0
        p.entry_sense=lambda *a:None
        p.contracts={'X-USDT':pt.Contract('X-USDT',.01,.01,2,2,1,100)}
        p.px={'X-USDT':100.3}; p.size_qty=lambda *a,**k:8
        p.max_book_notional=lambda **k:10000; p.ensure_max_leverage=Mock()
        p.leverage_for=lambda c:100; p.cid=lambda *a,**k:'test-order'
        p.record_event=Mock(); p._remember_pending=Mock()
        p.sl_min=.001; p.sl_max=.03; p.tp_min=.002; p.tp_max=.03
        p.position_cost_pct=.15; p.tp_cost_ratio=3
        p.api=NS(post=Mock(side_effect=RuntimeError('exchange boundary')))
        return p

    def test_actual_order_boundary_is_adjusted_and_attributed(self):
        p=self.entry_fixture()
        with patch.object(pt.os.path,'exists',return_value=False), patch.object(pt.time,'time',return_value=60):
            with self.assertRaisesRegex(RuntimeError,'exchange boundary'):
                p.place('X-USDT',1,'gen:test',.9)
        request=p.api.post.call_args.args[1]
        self.assertEqual(request['quantity'],2)
        meta=p._remember_pending.call_args.kwargs['metadata']
        self.assertEqual(meta['strategy'],'block')
        self.assertEqual(meta['axis_key'],'block-active:1')
        self.assertEqual(meta['execution']['normalQtyExecuted'],0)

    def test_normal_default_and_forced_path_cannot_bypass(self):
        p=self.entry_fixture(); p.block_active=False
        p.place('X-USDT',1,'gen:test',.9)
        p.block_active=True
        p.place('X-USDT',1,'gen:test',.9,forced_row={'id':'forced:x'})
        p.api.post.assert_not_called()

    def test_pending_recovery_retains_adjusted_identity(self):
        p=self.entry_fixture()
        p.variants=NS(current_sl=lambda:.6,current_trail=lambda:('0.3:0.1',.3,.1))
        meta=dict(strategy='block',axis_key='block-active:2',relative_count=2,
                  volume_ratio=.5,parent_set_id=self.st.id,set_id=self.st.id,
                  sl_ratio=.6,sl_pct=.003,tp_pct=.005,trail_key='0.3:0.1')
        pos=p._pending_position(dict(symbol='X-USDT',side='LONG',metadata=meta,
                                     requested_qty=2,client_id='recover'),1,100)
        self.assertEqual(pos.strategy,'block')
        self.assertEqual(pos.axis_key,'block-active:2')
        self.assertEqual(pos.volume_ratio,.5)
        self.assertEqual(pos.pending_qty,1)

    def test_normal_explicit_enabled_restores_reference_order(self):
        p=self.entry_fixture(); p.block_active=False; p.normal_execution_enabled=True
        with patch.object(pt.os.path,'exists',return_value=False), patch.object(pt.time,'time',return_value=60):
            with self.assertRaisesRegex(RuntimeError,'exchange boundary'):
                p.place('X-USDT',1,'gen:test',.9)
        self.assertEqual(p.api.post.call_args.args[1]['quantity'],8)

    def test_exchange_minimum_does_not_upsize_adjustment(self):
        p=self.entry_fixture(); p.contracts['X-USDT'].min_qty=3
        with patch.object(pt.os.path,'exists',return_value=False), patch.object(pt.time,'time',return_value=60):
            p.place('X-USDT',1,'gen:test',.9)
        p.api.post.assert_not_called()
        p.ensure_max_leverage.assert_not_called()

    def test_eighty_selection_recovery_and_explicit_unlimited(self):
        b=SetBook(); self.assertEqual(b.max_active,80)
        b.by_idx=[SetState(id=str(i),pack='general',tf='1m',sl_ratio=.6,trail_key='',trail_arm=0,
                          trail_give=0,last15_ratio=1+i/100,active=True) for i in range(100)]
        b._cap_active()
        self.assertEqual(sum(s.active for s in b.by_idx),80)
        self.assertFalse(b.by_idx[0].active)
        b.by_idx[0].last15_ratio=100
        b._cap_active();self.assertTrue(b.by_idx[0].active)
        b.by_idx[-1].active=False;b.by_idx[-1].deact_reason='live negative'
        b.max_active=0;b._cap_active()
        self.assertEqual(sum(s.active for s in b.by_idx),99)
        self.assertFalse(b.by_idx[-1].active)


if __name__=='__main__':unittest.main()
