"""Shared exchange controls never merge per-Set books or duplicate partial fills."""
import pathlib
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server/pulse'))
import overall_controls as overall
import test_all_valid_entries as harness


class OverallTests(unittest.TestCase):
    def pulse(self,n=2):
        h=harness.AllValidEntries();h.setUp();self.addCleanup(h.doCleanups)
        p=h.pulse(h.book(n));p.control_orders_overall=True
        post=p.api.post
        def call(path,body):
            if path.endswith('/cancelReplace'):
                old=str(body['cancelOrderId'])
                if old not in p.api.orders:return {'code':1,'msg':'missing old order'}
                p.api.orders.pop(old);p.api.n+=1;oid=str(p.api.n)
                p.api.orders[oid]=dict(body,orderId=oid,clientOrderID=body['clientOrderId'])
                return {'code':0,'data':{'cancelResult':'SUCCESS','newOrderResult':'SUCCESS','newOrderId':oid}}
            return post(path,body)
        p.api.post=call
        return p

    def test_multiple_sets_share_pair_but_keep_own_lots_and_targets(self):
        p=self.pulse()
        for st in p.sets.by_idx:p.place('X-USDT',1,'trend',.9,selected_set=st)
        rows=list(p.open.values())
        self.assertEqual(len(rows),2)
        self.assertEqual(len({r.set_id for r in rows}),2)
        self.assertEqual(len({r.sl_oid for r in rows}),1)
        self.assertEqual(len({r.tp_oid for r in rows}),1)
        p._overall_cleanup_next = 0
        overall.drain_retired(p,rows)
        self.assertEqual(len(p.api.orders),2)
        for body in p.api.orders.values():
            self.assertAlmostEqual(float(body['quantity']),sum(r.qty for r in rows))
            self.assertNotIn('closePosition',body)
        before=len(p.api.batches)
        for r in rows:p.ensure_controls(r)
        self.assertEqual(len(p.api.batches),before)
        self.assertEqual(len({p.position_key(r) for r in rows}),2)

    def test_own_targets_and_directions_remain_separate(self):
        p=self.pulse()
        for direction in (1,-1):
            for st in p.sets.by_idx:p.place('X-USDT',direction,'trend',.9,selected_set=st)
        self.assertEqual(len(p.open),4)
        self.assertEqual(len({r.sl_oid for r in p.open.values()}),2)
        long=next(r for r in p.open.values() if r.side=='LONG')
        other=next(r for r in p.open.values() if r.side=='LONG' and r is not long)
        old=other.sl
        p.replace_sl(long,long.sl+.05)
        self.assertEqual(other.sl,old)

    def test_real_accounting_retains_each_set_profit_and_confirmed_quantity(self):
        p=self.pulse()
        for st in p.sets.by_idx:p.place('X-USDT',1,'trend',.9,selected_set=st)
        rows=list(p.open.values());total=sum(r.qty for r in rows);oid=rows[0].tp_oid
        body=p.api.orders[oid];cid=body['clientOrderID']
        order=dict(body,orderId=oid,executedQty=total,avgPrice=101)
        self.assertTrue(p._sync_control_fill(order,cid,{'kind':'v'}))
        self.assertFalse(p.open)
        self.assertEqual(len(p.closed),2)
        self.assertEqual(len({r.set_id for r in p.closed}),2)
        self.assertAlmostEqual(sum(r.qty for r in p.closed),total)
        self.assertAlmostEqual(sum(r.pnl for r in p.closed),total*100*(.01-.0015))
        self.assertFalse(p._sync_control_fill(order,cid,{'kind':'v'}))
        self.assertEqual(len(p.closed),2)

    def test_switch_back_keeps_siblings_protected_until_new_pair_is_confirmed(self):
        p=self.pulse()
        for st in p.sets.by_idx:p.place('X-USDT',1,'trend',.9,selected_set=st)
        rows=list(p.open.values());shared=(rows[0].sl_oid,rows[0].tp_oid)
        p.control_orders_overall=False
        p.ensure_controls(rows[0])
        self.assertNotEqual(rows[0].sl_oid,shared[0])
        self.assertEqual(rows[1].sl_oid,shared[0])
        self.assertIn(shared[0],p.api.orders)
        p.ensure_controls(rows[1]);p._overall_cleanup_next=0
        overall.drain_retired(p,rows)
        self.assertEqual(len({r.sl_oid for r in rows}),2)
        self.assertNotIn(shared[0],p.api.orders)

    def test_replacement_failure_preserves_confirmed_old_pairs(self):
        p=self.pulse()
        st=p.sets.by_idx[0];p.place('X-USDT',1,'trend',.9,selected_set=st)
        pos=next(iter(p.open.values()));old=(pos.sl_oid,pos.tp_oid)
        pos.qty*=2
        with patch.object(p.api,'post',return_value={'code':1}):overall.ensure(p,pos)
        self.assertEqual((pos.sl_oid,pos.tp_oid),old)

    def test_second_leg_failure_keeps_first_confirmed_and_retries_only_missing_leg(self):
        p=self.pulse(1);p.place('X-USDT',1,'trend',.9,selected_set=p.sets.by_idx[0])
        pos=next(iter(p.open.values()));old=(pos.sl_oid,pos.tp_oid);pos.qty*=2
        post=p.api.post
        def fail_tp(path,body):
            return {'code':1} if body.get('type')=='TAKE_PROFIT_MARKET' else post(path,body)
        with patch.object(p.api,'post',side_effect=fail_tp):overall.ensure(p,pos)
        first=pos.sl_oid
        self.assertNotEqual(first,old[0]);self.assertEqual(pos.tp_oid,old[1])
        self.assertIn(first,p.api.orders);self.assertIn(old[1],p.api.orders)
        p._overall_replace_next={};overall.ensure(p,pos)
        self.assertEqual(pos.sl_oid,first)
        self.assertNotEqual(pos.tp_oid,old[1])
        self.assertEqual(len(p.api.orders),2)

    def test_accepted_replace_with_lost_response_is_recovered_without_duplicate(self):
        p=self.pulse(1);p.place('X-USDT',1,'trend',.9,selected_set=p.sets.by_idx[0])
        pos=next(iter(p.open.values()));pos.qty*=2
        post=p.api.post
        def lose_response(path,body):
            post(path,body)
            return {'code':-1,'msg':'timeout'}
        with patch.object(p.api,'post',side_effect=lose_response):overall.ensure(p,pos)
        self.assertIn('sl',pos.overall_replace_intents)
        accepted=next(o['orderId'] for o in p.api.orders.values() if o['type']=='STOP_MARKET')
        def get(path,params=None):
            cid=(params or {}).get('clientOrderId')
            row=next((o for o in p.api.orders.values() if o.get('clientOrderID')==cid),{})
            return {'code':0,'data':row}
        p._overall_replace_next={}
        with patch.object(p.api,'get',side_effect=get):overall.ensure(p,pos)
        self.assertEqual(pos.sl_oid,accepted)
        self.assertFalse(pos.overall_replace_intents)
        self.assertEqual(len(p.api.orders),2)

    def test_late_retired_fill_does_not_close_a_new_member(self):
        p=self.pulse()
        p.place('X-USDT',1,'trend',.9,selected_set=p.sets.by_idx[0])
        first=next(iter(p.open.values()));old=p.api.orders[first.tp_oid].copy()
        original=first.qty
        p.place('X-USDT',1,'trend',.9,selected_set=p.sets.by_idx[1])
        second=next(r for r in p.open.values() if r is not first)
        second_qty=second.qty
        self.assertTrue(p._sync_control_fill(dict(old,executedQty=original,avgPrice=101),old['clientOrderID'],{'kind':'v'}))
        self.assertEqual(second.qty,second_qty)
        self.assertEqual(len(p.closed),1)
        self.assertEqual(p.closed[0].client_id,first.client_id)

    def test_restart_replaces_changed_boundary_and_honors_controls_off(self):
        p=self.pulse(1);p.place('X-USDT',1,'trend',.9,selected_set=p.sets.by_idx[0])
        pos=next(iter(p.open.values()));oid=pos.sl_oid
        p._overall_pairs={};pos.sl+=.05
        p.ensure_controls(pos)
        self.assertNotEqual(pos.sl_oid,oid)
        count=len(p.api.batches);p.control_orders=False;pos.qty*=2
        p.ensure_controls(pos)
        self.assertEqual(len(p.api.batches),count)

    def test_last_close_cleanup_retries_after_restart_and_preserves_foreign(self):
        p=self.pulse(1);p.place('X-USDT',1,'trend',.9,selected_set=p.sets.by_idx[0])
        pos=next(iter(p.open.values()));body=p.api.orders[pos.tp_oid].copy()
        p.api.orders['foreign']={'orderId':'foreign','symbol':'X-USDT','clientOrderID':'OTHER'}
        with patch.object(p,'cancel_order',return_value=False):
            self.assertTrue(p._sync_control_fill(dict(body,executedQty=pos.qty,avgPrice=101),body['clientOrderID'],{'kind':'v'}))
        self.assertFalse(p.open)
        self.assertTrue(overall.cleanup_state(p))
        del p._overall_cleanup
        p._overall_final_cleanup_next=0
        overall.drain_cleanup(p)
        self.assertFalse(overall.cleanup_state(p))
        self.assertEqual(set(p.api.orders),{'foreign'})

    def test_interrupted_partial_retry_does_not_reapply_completed_member(self):
        p=self.pulse()
        for st in p.sets.by_idx:p.place('X-USDT',1,'trend',.9,selected_set=st)
        rows=list(p.open.values());total=sum(r.qty for r in rows)
        body=p.api.orders[rows[0].tp_oid].copy()
        update=dict(body,executedQty=total/2,avgPrice=101)
        original=p._record_close_fill
        count=0
        def apply(*args,**kwargs):
            nonlocal count
            count+=1
            return False if count==2 else original(*args,**kwargs)
        with patch.object(p,'_record_close_fill',side_effect=apply):
            self.assertFalse(p._sync_control_fill(update,body['clientOrderID'],{'kind':'v'}))
        self.assertTrue(p._sync_control_fill(update,body['clientOrderID'],{'kind':'v'}))
        self.assertEqual(len(p.closed),2)
        self.assertAlmostEqual(sum(r.qty for r in p.open.values()),total/2)
        self.assertFalse(p._sync_control_fill(update,body['clientOrderID'],{'kind':'v'}))

    def test_confirmed_shared_partial_and_final_are_allocated_once(self):
        p=self.pulse()
        for st in p.sets.by_idx:p.place('X-USDT',1,'trend',.9,selected_set=st)
        rows=list(p.open.values());total=sum(r.qty for r in rows);oid=rows[0].tp_oid
        order=next(v for k,v in p.api.orders.items() if k==oid);cid=order['clientOrderID']
        initial={r.client_id:r.qty for r in rows};fills=[]
        def apply(pos,qty,px,*args,**kw):
            fills.append((pos.client_id,qty,px));pos.qty-=qty
            if pos.qty<1e-9:p.open.pop(p.position_key(pos),None)
            return True
        p._record_close_fill=apply
        def remember(**kw):p.pending_orders[kw['cid']]=dict(kw)
        p._remember_pending=remember
        p._clear_pending=lambda cid:p.pending_orders.pop(cid,None)
        update=dict(order,orderId=oid,executedQty=total/2,avgPrice=101)
        with patch.object(overall,'ensure'):
            self.assertTrue(p._sync_control_fill(update,cid,{'kind':'v'}))
            self.assertFalse(p._sync_control_fill(update,cid,{'kind':'v'}))
            self.assertTrue(p._sync_control_fill(dict(update,executedQty=total,avgPrice=102),cid,{'kind':'v'}))
            self.assertFalse(p._sync_control_fill(dict(update,executedQty=total,avgPrice=102),cid,{'kind':'v'}))
        self.assertEqual(len(fills),4)
        self.assertEqual([round(f[2],6) for f in fills],[101,101,103,103])
        self.assertAlmostEqual(sum(f[1] for f in fills),total)
        self.assertFalse(p.open)

if __name__=='__main__':unittest.main()
