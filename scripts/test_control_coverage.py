"""Protective-order safety contracts against a local fake venue; no client init."""
import sys
import inspect
from pathlib import Path
from types import SimpleNamespace
from dataclasses import asdict
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"server/pulse"))
import pulse_trader as pt


def fixture(side="LONG"):
    p = pt.Pulse.__new__(pt.Pulse)
    p.ctrl_skip, p._oo_cache, p.px, p.last_px = {}, {}, {"A":100.0}, {"A":100.0}
    p.contracts = {}
    p.control_orders = True
    p.opt_fracs = lambda pos: (.01,.02,.005,.05)
    p.tp_max, p.tp_min, p.sl_max, p.sl_min = .08, .005, .05, .005
    p.exits = SimpleNamespace(enabled=False)
    p.api = SimpleNamespace(path_cd={})
    p.cid = lambda ch, **kw: pt.TAG+ch+"fixture"
    p.fmt_px = lambda c,v: str(v)
    p.fmt_qty = lambda c,v: str(v)
    p.cancelled, p.placed = [], []
    p.cancel_order = lambda symbol,oid,cid="": p.cancelled.append(oid) or True
    p.open = {}
    p.save_open_book = lambda: True
    pos = pt.Position("A",side,10,100,1,99 if side=="LONG" else 101,102 if side=="LONG" else 98,100)
    p.open["A"] = pos
    return p,pos


def row(side,kind,oid,price,qty=10,close_all=True):
    return {"symbol":"A","positionSide":side,"type":"STOP_MARKET" if kind=="u" else "TAKE_PROFIT_MARKET",
            "orderId":oid,"stopPrice":price,"quantity":qty,"closePosition":str(close_all).lower(),
            "clientOrderID":pt.TAG+kind+"fixture"}


class Controls(unittest.TestCase):
    def test_unknown_single_control_is_persisted_and_recovered_without_resubmission(self):
        p,pos=fixture();p.errors=0;calls=[]
        def post(path,body):
            calls.append(body);return {'code':-1,'error':True,'msg':'reply lost'}
        p.api.post=post
        self.assertEqual(p.place_ctrl(pos,'sec-sl',95),'')
        self.assertIn('sl',pos.control_pending)
        restored=pt.Position(**asdict(pos));body=calls[0]
        p.api.get=lambda *args:{'code':0,'data':{'orders':[{**body,'orderId':'100','status':'NEW','executedQty':'0'}]}}
        self.assertEqual(p.place_ctrl(restored,'sec-sl',95),'100')
        self.assertEqual(len(calls),1)
        self.assertFalse(restored.control_pending)

    def test_missing_unknown_control_cannot_be_replaced_by_a_different_identity(self):
        p,pos=fixture();calls=[]
        p.api.post=lambda path,body:calls.append(body)or{'code':-1,'error':True}
        p.api.get=lambda *args:{'code':0,'data':{'orders':[]}}
        p.place_ctrl(pos,'sec-sl',95)
        pos.control_pending['sl']['at']-=3600
        for _ in range(4):
            pos.control_pending['sl']['nextRead']=0
            p.place_ctrl(pos,'sec-sl',95)
        self.assertEqual(len(calls),1)
        self.assertTrue(p.missing_controls(pos))

    def test_lost_batch_reply_never_falls_back_to_two_new_single_orders(self):
        p,pos=fixture();batches=[]
        p.api.batch_place=lambda bodies:batches.append(bodies)or{'code':-1,'error':True}
        p.api.post=lambda *args:self.fail('unknown batch was resent as singles')
        p.place_ctrl_pair(pos)
        self.assertEqual(len(pos.control_pending),2)
        rows=[{**body,'orderId':str(100+i),'status':'NEW','executedQty':'0'}for i,body in enumerate(batches[0])]
        p.api.get=lambda *args:{'code':0,'data':{'orders':rows}}
        p.place_ctrl_pair(pos)
        self.assertEqual(len(batches),1)
        self.assertEqual((pos.sl_oid,pos.tp_oid),('100','101'))
        self.assertFalse(pos.control_pending)

    def test_missing_batch_ids_remain_unknown_instead_of_becoming_refusals(self):
        p,pos=fixture();p.api.batch_place=lambda bodies:{'code':0,'data':{'orders':[]}}
        p.api.post=lambda *args:self.fail('unconfirmed acknowledgement resent')
        p.place_ctrl_pair(pos)
        self.assertEqual(len(pos.control_pending),2)
        self.assertFalse(pos.controls_ok)

    def test_failed_intent_persistence_prevents_protective_dispatch(self):
        p,pos=fixture();p.save_open_book=lambda:False
        p.api.post=lambda *args:self.fail('unpersisted protective intent dispatched')
        self.assertEqual(p.place_ctrl(pos,'sec-sl',95),'')
        self.assertFalse(pos.control_pending)

    def test_recovered_cancelled_partial_control_uses_native_cumulative_accounting_once(self):
        p,pos=fixture();posts=[]
        p.api.post=lambda path,body:posts.append(body)or{'code':-1,'error':True}
        p.place_ctrl(pos,'sec-sl',95)
        receipt={**posts[0],'orderId':'100','status':'CANCELLED','executedQty':'4','avgPrice':'101'}
        p.api.get=lambda *args:{'code':0,'data':{'orders':[receipt]}}
        p.fill_cost=lambda data,qty,price,*args:(qty*price*.0005,'reported-fee')
        p.note_fee_source=lambda *args:None;pos.entry_fee_usdt=0
        p.place_ctrl(pos,'sec-sl',95)
        self.assertIsNone(p.venue_exit(pos))
        self.assertEqual(pos.qty,6)
        self.assertAlmostEqual(pos.exit_fee_usdt,.202)
        self.assertEqual(pos.close_filled_qty,4)
        self.assertIsNone(p.venue_exit(pos))
        self.assertEqual(pos.qty,6)
        self.assertEqual(pos.close_filled_qty,4)
        self.assertFalse(pos.control_pending)

    def test_flat_venue_refusal_stops_control_variants_and_reconciles_once(self):
        for side in ('LONG','SHORT'):
            for kind in ('sec-sl','sec-tp'):
                p,pos=fixture(side);p.overlay={'ownedQuantityControls':True};p.errors=0
                calls=[];confirmed=[];dropped=[]
                p.api.post=lambda path,body:calls.append(body)or{'code':109420,'msg':'position not exist'}
                p._exchange_flat=lambda candidate:confirmed.append(candidate)or False
                p.drop_ghost=lambda *args:dropped.append(args)
                self.assertEqual(p.place_ctrl(pos,kind,95 if side=='LONG'else 105),'')
                self.assertEqual(len(calls),1,'an already-flat response cannot be fixed by another order form or price')
                self.assertEqual(confirmed,[pos])
                self.assertEqual(dropped,[],'a refusal alone cannot erase a position')
                self.assertEqual(p.errors,0)

    def test_quantity_only_pair_covers_owned_size_without_aggregate_close(self):
        for side in ("LONG", "SHORT"):
            p, pos = fixture(side)
            p.overlay = {"ownedQuantityControls": True}
            def batch(bodies):
                p.placed.extend(bodies)
                return {"code": 0, "data": {"orders": [{**b, "orderId": str(i+100)} for i, b in enumerate(bodies)]}}
            p.api.batch_place = batch
            p.place_ctrl_pair(pos)
            self.assertTrue(pos.controls_ok)
            self.assertFalse(pos.close_position)
            self.assertEqual(pos.ctrl_qty, pos.qty)
            for body in p.placed:
                self.assertNotIn("closePosition", body)
                self.assertEqual(float(body["quantity"]), pos.qty)
                self.assertEqual(body["positionSide"], side)
            pos.qty += 1
            self.assertTrue(p.missing_controls(pos))

    def test_quantity_only_single_control_never_tries_aggregate_fallback(self):
        p, pos = fixture()
        p.overlay = {"ownedQuantityControls": True}
        p.errors = 0
        calls = []
        def post(path, body):
            calls.append(body)
            return {"code": 101400, "msg": "quantity invalid"}
        p.api.post = post
        p.place_ctrl(pos, "sec-sl", 95)
        self.assertTrue(calls)
        self.assertTrue(all("closePosition" not in b and float(b["quantity"]) == pos.qty for b in calls))

    def test_quantity_only_market_close_never_falls_back_to_position_id(self):
        p, pos = fixture()
        p.overlay = {"ownedQuantityControls": True}
        pos.position_id = "1234"
        p.seen_fill_cids = set()
        p.save_open_book = lambda: True
        p.errors = 0
        calls = []
        p.api.post = lambda path, body: calls.append(body) or {"code": 101400, "msg": "quantity is must"}
        ok, _ = p.market_close(pos)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("closePosition", calls[0])
        self.assertNotIn("positionId", calls[0])
        self.assertEqual(calls[0]["quantity"], pos.qty)

    def test_refused_close_never_removes_existing_controls(self):
        p,pos=fixture()
        pos.sl_oid=pos.sec_sl_oid="100";pos.tp_oid=pos.sec_tp_oid="101"
        pos.close_position=pos.controls_ok=pos.ctrl_verified=True
        p.seen_fill_oids=set();p.ignore_syms={};p.last_error="refused"
        def close(_pos):
            self.assertEqual(p.cancelled,[])
            self.assertEqual((_pos.sl_oid,_pos.tp_oid),("100","101"))
            return False,100
        p.market_close=close
        p.close_pos(pos,100,"test-close")
        self.assertEqual(p.cancelled,[])
        self.assertEqual((pos.sl_oid,pos.tp_oid),("100","101"))
        self.assertFalse(p.missing_controls(pos))
        self.assertEqual(p.seen_fill_oids,set(),"a failed manager close must not suppress a subsequent protective fill")

    def test_cleanup_of_long_cannot_cancel_short_or_unknown_direction(self):
        p,pos=fixture()
        rows=[row("LONG","u","100",95),row("SHORT","u","200",105),row("BOTH","u","300",95)]
        p.list_orders=lambda symbol:rows
        p.order_is_ours=lambda order:True
        p.cancel_controls("A",side="LONG")
        self.assertEqual(p.cancelled,["100"])

    def test_9995_percent_close_is_still_partial(self):
        p,pos=fixture()
        pos.close_pending={"order_id":"100","client_id":"fixture"}
        p.seen_fill_cids=set();p.save_open_book=lambda:True
        p.confirmed_order=lambda *args:{"executedQty":"9.995","avgPrice":"101","status":"FILLED"}
        p.record_exit_fee=lambda *args:None
        ok,_=p.market_close(pos)
        self.assertFalse(ok)
        self.assertAlmostEqual(pos.qty,.005)
        self.assertAlmostEqual(pos.close_filled_qty,9.995)

    def test_cumulative_protective_partials_are_booked_once_and_survive_restart(self):
        import json
        from dataclasses import asdict
        for side in ("LONG","SHORT"):
            p,pos=fixture(side)
            pos.sl_oid=pos.sec_sl_oid="100";pos.tp_oid=pos.sec_tp_oid="101"
            pos.entry_fee_usdt=1
            p.save_open_book=lambda:True
            p.fill_cost=lambda order,*args:(float(order['commission']),'reported-fee')
            first={"orderId":"100","symbol":"A","positionSide":side,"executedQty":3,"avgPrice":101,"commission":.3,"status":"PARTIALLY_FILLED"}
            self.assertFalse(p.apply_control_fill(pos,first,'sl'))
            self.assertEqual(pos.qty,7);self.assertEqual(pos.exit_fee_usdt,.3)
            self.assertFalse(p.apply_control_fill(pos,first,'sl'))
            self.assertEqual(pos.qty,7);self.assertEqual(pos.exit_fee_usdt,.3)
            pos=pt.Position(**json.loads(json.dumps(asdict(pos))))
            second={**first,'executedQty':6,'avgPrice':102,'commission':.6,'status':'FILLED'}
            self.assertFalse(p.apply_control_fill(pos,second,'sl'))
            self.assertEqual(pos.qty,4);self.assertEqual(pos.close_filled_qty,6)
            self.assertEqual(pos.close_filled_value,612);self.assertEqual(pos.exit_fee_usdt,.6)
            self.assertEqual(pos.sl_oid,'');self.assertEqual(pos.tp_oid,'101')
            final={**first,'orderId':'101','executedQty':4,'avgPrice':103,'commission':.4,'status':'FILLED'}
            self.assertTrue(p.apply_control_fill(pos,final,'tp'))
            self.assertEqual(pos.qty,0);self.assertEqual(pos.close_filled_qty,10)
            self.assertEqual(pos.close_filled_value,1024);self.assertEqual(pos.exit_fee_usdt,1)
            self.assertEqual(pos.exit_fee_qty,10)

    def test_control_fill_missing_quantity_foreign_id_or_opposite_side_cannot_close(self):
        p,pos=fixture();pos.sl_oid='100';p.save_open_book=lambda:True;p.fill_cost=lambda *args:(0,'reported-fee')
        base={'orderId':'100','symbol':'A','positionSide':'LONG','avgPrice':100,'status':'FILLED'}
        for row in (base,{**base,'executedQty':10,'positionSide':'SHORT'},{**base,'executedQty':10,'orderId':'999'}):
            self.assertFalse(p.apply_control_fill(pos,row,'sl'))
            self.assertEqual(pos.qty,10)

    def test_same_quantity_terminal_update_clears_control_without_double_fee(self):
        p,pos=fixture();pos.sl_oid=pos.sec_sl_oid='100';pos.entry_fee_usdt=1
        p.save_open_book=lambda:True
        p.fill_cost=lambda order,*args:(float(order['commission']),'reported-fee')
        partial={'orderId':'100','symbol':'A','positionSide':'LONG','executedQty':3,'avgPrice':101,
                 'commission':.3,'status':'PARTIALLY_FILLED'}
        self.assertFalse(p.apply_control_fill(pos,partial,'sl'))
        self.assertFalse(p.apply_control_fill(pos,{**partial,'commission':.27,'status':'FILLED'},'sl'))
        self.assertEqual(pos.qty,7);self.assertEqual(pos.close_filled_qty,3)
        self.assertAlmostEqual(pos.exit_fee_usdt,.27);self.assertEqual(pos.exit_fee_qty,3)
        self.assertEqual(pos.sl_oid,'');self.assertEqual(pos.sec_sl_oid,'')

    def test_overall_pair_uses_outer_long_and_short_ranges(self):
        for side in ("LONG","SHORT"):
            p,pos=fixture(side)
            def batch(bodies):
                p.placed.extend(bodies)
                return {"code":0,"data":{"orders":[{**b,"orderId":str(i+100)} for i,b in enumerate(bodies)]}}
            p.api.batch_place=batch
            p.place_ctrl_pair(pos)
            want = (95,108) if side=="LONG" else (105,92)
            self.assertEqual(tuple(float(b["stopPrice"]) for b in p.placed),want)
            self.assertTrue(all(b["closePosition"]=="true" and float(b["quantity"])==pos.qty for b in p.placed))
            self.assertEqual((pos.sl,pos.tp,pos.sec_sl,pos.sec_tp),(*want,*want))

    def test_failed_refresh_preserves_existing_protection(self):
        p,pos=fixture()
        pos.sl_oid=pos.sec_sl_oid="100";pos.tp_oid=pos.sec_tp_oid="101"
        pos.sl=pos.sec_sl=94;pos.tp=pos.sec_tp=108
        p.list_orders=lambda s:[row("LONG","u","100",94),row("LONG","v","101",108)]
        p.place_ctrl=lambda *args:""
        p.ensure_controls(pos)
        self.assertEqual(p.cancelled,[])
        self.assertEqual((pos.sl_oid,pos.sl),("100",94))

    def test_batch_values_are_actual_clamped_prices(self):
        p,pos=fixture()
        p.clamp_ctrl_price=lambda pos,kind,price:price+(.01 if kind=='sl' else -.01)
        p.api.batch_place=lambda bodies:{'code':0,'data':{'orders':[{**b,'orderId':str(i+100)} for i,b in enumerate(bodies)]}}
        p.place_ctrl_pair(pos)
        for value in (pos.sl,pos.sec_sl):self.assertAlmostEqual(value,95.02)
        for value in (pos.tp,pos.sec_tp):self.assertAlmostEqual(value,107.98)

    def test_illegal_control_price_does_not_certify_coverage_after_failed_replace(self):
        p,pos=fixture();pos.sl_oid=pos.sec_sl_oid='100';pos.tp_oid=pos.sec_tp_oid='101'
        p.list_orders=lambda symbol:[row('LONG','u','100',105),row('LONG','v','101',108)]
        p.place_ctrl=lambda *args:''
        p.ensure_controls(pos)
        self.assertTrue(p.missing_controls(pos));self.assertFalse(pos.ctrl_verified)
        self.assertEqual(p.cancelled,[])

    def test_failed_trailing_replacement_preserves_stop_and_returns_false(self):
        p,pos=fixture()
        pos.sl_oid=pos.sec_sl_oid="100";pos.tp_oid=pos.sec_tp_oid="101"
        p.place_ctrl=lambda *args:""
        self.assertFalse(p.replace_sl(pos,99.5))
        self.assertEqual(p.cancelled,[])
        self.assertEqual((pos.sl_oid,pos.sec_sl_oid,pos.sl),("100","100",99))

    def test_full_position_control_survives_stack_quantity_growth(self):
        p,pos=fixture()
        pos.sl_oid=pos.sec_sl_oid="100";pos.tp_oid=pos.sec_tp_oid="101"
        pos.sl=pos.sec_sl=95;pos.tp=pos.sec_tp=108
        p.list_orders=lambda s:[row("LONG","u","100",95,qty=1),row("LONG","v","101",108,qty=1)]
        p.place_ctrl=lambda *args:self.fail("close-all coverage does not shrink with reported request qty")
        p.ensure_controls(pos)
        self.assertEqual(p.cancelled,[])
        self.assertFalse(p.missing_controls(pos))

    def test_quantity_shortfall_cannot_report_full_coverage(self):
        p,pos=fixture()
        pos.sl_oid=pos.sec_sl_oid="100";pos.tp_oid=pos.sec_tp_oid="101"
        pos.sl=pos.sec_sl=95;pos.tp=pos.sec_tp=108
        p.list_orders=lambda s:[row("LONG","u","100",95,qty=9.6,close_all=False),row("LONG","v","101",108,qty=10,close_all=False)]
        p.place_ctrl=lambda *args:""
        p.ensure_controls(pos)
        self.assertTrue(p.missing_controls(pos),"96% covered must not certify full protection")
        self.assertEqual(p.cancelled,[],"keep the partial protection until its replacement is confirmed")

    def test_overlay_outer_stop_is_not_clipped_to_inner_optimizer(self):
        p,pos=fixture()
        p.opt_fracs=lambda pos:(.01,.02,.005,.015)
        self.assertEqual(p.max_range_prices(pos),(95,108))

    def test_confirmed_replacement_then_cleanup_is_side_scoped(self):
        for side,opposite in (("LONG","SHORT"),("SHORT","LONG")):
            p,pos=fixture(side)
            pos.sl_oid=pos.sec_sl_oid="100";pos.tp_oid=pos.sec_tp_oid="101"
            pos.sl=pos.sec_sl=95 if side=="LONG" else 105
            pos.tp=pos.sec_tp=108 if side=="LONG" else 92
            rows=[row(side,"u","100",pos.sl,9,False), row(side,"v","101",pos.tp),
                  row(opposite,"u","200",105),row(side,"u","300",95)]
            rows[-1]["clientOrderID"]="foreign-bot-u"
            p.list_orders=lambda s:rows
            events=[]
            p.cancel_order=lambda symbol,oid,cid="":events.append(("cancel",oid)) or True
            def place(_pos,kind,price):
                events.append(("place",kind));_pos.sec_sl=price;_pos.close_position=True
                return "102"
            p.place_ctrl=place
            p.ensure_controls(pos)
            self.assertEqual(events,[("place","sec-sl"),("cancel","100")])
            self.assertFalse(p.missing_controls(pos))
            self.assertEqual(pos.sl_oid,"102")

    def test_duplicate_cleanup_retries_without_forgetting_current_order(self):
        p,pos=fixture()
        pos.sl_oid=pos.sec_sl_oid="102";pos.tp_oid=pos.sec_tp_oid="101"
        pos.sl=pos.sec_sl=95;pos.tp=pos.sec_tp=108
        p.list_orders=lambda s:[row("LONG","u","100",95),row("LONG","u","102",95),row("LONG","v","101",108)]
        p.cancel_order=lambda symbol,oid,cid="":p.cancelled.append(oid) or False
        p.place_ctrl=lambda *args:self.fail("already covered")
        p.ensure_controls(pos);p.ensure_controls(pos)
        self.assertEqual(p.cancelled,["100","100"])
        self.assertEqual(pos.sl_oid,"102")

    def test_quantity_coverage_blocks_growth_before_next_rest_poll(self):
        p,pos=fixture()
        pos.sl_oid="100";pos.tp_oid="101";pos.close_position=False;pos.ctrl_qty=9.9
        self.assertTrue(p.missing_controls(pos))
        pos.ctrl_qty=10
        self.assertFalse(p.missing_controls(pos))

    def test_replacement_success_commits_actual_price_before_cancel(self):
        p,pos=fixture()
        pos.sl_oid=pos.sec_sl_oid="100";pos.tp_oid=pos.sec_tp_oid="101"
        def place(_pos,kind,price):
            self.assertEqual(p.cancelled,[])
            _pos.sec_sl=price;return "102"
        p.place_ctrl=place
        self.assertTrue(p.replace_sl(pos,99.5))
        self.assertEqual(p.cancelled,["100"])
        self.assertEqual((pos.sl_oid,pos.sl),("102",99.5))

    def test_replacement_cancels_the_previously_recorded_id(self):
        p,pos=fixture()
        pos.sl_oid=pos.sec_sl_oid='100'; pos.tp_oid=pos.sec_tp_oid='101'
        p.place_ctrl=lambda _pos,_kind,price: setattr(_pos,'sec_sl',price) or '102'
        p.list_orders=lambda _symbol: []
        calls=[]
        p.api.delete=lambda path,body: calls.append((path,body['orderId'])) or {'code':0}
        p.cancel_order=pt.Pulse.cancel_order.__get__(p,pt.Pulse)
        self.assertTrue(p.replace_sl(pos,99.5))
        self.assertEqual([oid for _,oid in calls],['100'])
        self.assertEqual((pos.sl_oid,pos.sec_sl_oid),('102','102'))

    def test_atomic_replace_without_an_id_is_unconfirmed_not_old_id_success(self):
        p,pos=fixture()
        pos.sl_oid=pos.sec_sl_oid="100";pos.tp_oid=pos.sec_tp_oid="101"
        p.api.cancel_replace=lambda body:{"code":0,"data":{}}
        p.place_ctrl=lambda *args:self.fail("do not duplicate an unconfirmed atomic replace")
        self.assertFalse(p.replace_sl(pos,99.5))
        self.assertTrue(p.missing_controls(pos))
        self.assertFalse(pos.controls_ok)
        self.assertFalse(pos.ctrl_verified)

    def test_add_paths_keep_existing_ids_until_control_refresh(self):
        for method in (pt.Pulse.maybe_block_adds,pt.Pulse.maybe_dca_adds):
            source=inspect.getsource(method)
            self.assertNotIn("self.cancel_controls(pos.symbol)",source)
            self.assertNotIn('pos.sl_oid = pos.tp_oid = pos.sec_sl_oid = pos.sec_tp_oid = ""',source)
            self.assertIn("self.ensure_controls(pos)",source)

    def test_exists_recovery_uses_full_quantity_and_actual_reported_price(self):
        p,pos=fixture()
        pos.sl_oid='100'; pos.sec_sl_oid='102'
        p.api.post=lambda *args:{"code":100,"msg":"order already exists"}
        p.our_orders=lambda sym:[row("LONG","u","100",95,9.6,False),row("LONG","u","102",96,10,False)]
        self.assertEqual(p.place_ctrl(pos,"sec-sl",95),"102")
        self.assertEqual(pos.sec_sl,96)
        self.assertFalse(pos.close_position)
        self.assertEqual(pos.ctrl_qty,10)

    def test_trigger_partial_is_not_full_remaining_quantity(self):
        p,pos=fixture()
        control=row("LONG","u","100",95,10,False)
        control["executedQty"]=1
        self.assertFalse(p.control_covers_quantity(pos,control))
        control["closePosition"]="true"
        self.assertTrue(p.control_covers_quantity(pos,control))


if __name__=="__main__":unittest.main()
