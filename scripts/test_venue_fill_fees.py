"""Exact venue commissions, cumulative controls, rebates and restart evidence."""
from dataclasses import asdict
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server/pulse'))
from pulse_trader import Position, Pulse
from venue_fill_fees import verified_bingx_fee, remember_fee_leg, reconcile_position_fees


def position():
    return Position('X-USDT','LONG',2,100,1_700_000_000,90,110,100,
        order_id='11',entry_fee_usdt=.1,base_entry_fee_usdt=.1,
        exit_fee_usdt=.1001,fee_source='exchange-rate-estimate')


def row(oid,side,qty,price,commission):
    return dict(orderId=str(oid),symbol='X-USDT',positionSide='LONG',side=side,
                volume=str(qty),price=str(price),commission=str(commission),currency='USDT')


class FeeEvidence(unittest.TestCase):
    def test_actual_rounded_commissions_replace_estimates_once_without_entry_io(self):
        pos=position();p=Pulse.__new__(Pulse)
        p.fee_schedule=SimpleNamespace(rates=lambda *a:(.0002,.0005,'account-rate'))
        p.contracts={};p.overlay={}
        p.api=SimpleNamespace(reported_fill_fee=lambda *a:(_ for _ in()).throw(AssertionError('entry queried fees')))
        entry=dict(orderId='11',symbol=pos.symbol,positionSide=pos.side,side='BUY')
        exit=dict(orderId='12',symbol=pos.symbol,positionSide=pos.side,side='SELL')
        fee,source=p.fill_cost(entry,2,100,pos.symbol,pos)
        self.assertEqual((fee,source),(.1,'taker-rate'))
        p.fill_cost(exit,2,100.1,pos.symbol,pos)
        calls=[]
        def exact(leg,opened):
            calls.append(leg['orderId']);return {'11':.099999,'12':.100099}[leg['orderId']]
        self.assertTrue(reconcile_position_fees(pos,exact))
        self.assertAlmostEqual(pos.entry_fee_usdt+pos.exit_fee_usdt,.200098,places=12)
        self.assertEqual(pos.base_entry_fee_usdt,.099999)
        self.assertTrue(reconcile_position_fees(pos,lambda *a:self.fail('reported fees queried again')))
        self.assertEqual(calls,['11','12'])
        self.assertEqual(pos.fee_source,'reported-fee')

    def test_complete_fill_scope_validates_partial_rows_and_signed_rebate(self):
        leg=dict(orderId='12',symbol='X-USDT',positionSide='LONG',side='SELL',qty=2,price=101)
        reply={'code':0,'data':{'fill_orders':[row(12,'SELL',.5,101,-.025),row(12,'SELL',1.5,101,.003)]}}
        self.assertEqual(verified_bingx_fee(reply,leg),.022)
        reply['data']['fill_orders'][0]['commission']='0'
        self.assertEqual(verified_bingx_fee(reply,leg),-.003)

    def test_foreign_or_incomplete_fill_evidence_never_becomes_actual_fees(self):
        leg=dict(orderId='12',symbol='X-USDT',positionSide='LONG',side='SELL',qty=2,price=101)
        original=row(12,'SELL',2,101,-.101)
        for key,value in [('orderId','99'),('symbol','Y-USDT'),('side','BUY'),('positionSide','SHORT'),
                          ('volume','1'),('price','102'),('commission','nan'),('currency','BNB')]:
            with self.subTest(key=key):
                self.assertIsNone(verified_bingx_fee({'code':0,'data':{'fill_orders':[{**original,key:value}]}},leg))
        self.assertIsNone(verified_bingx_fee({'code':0,'data':{'fill_orders':[]}},leg))

    def test_cumulative_protection_poll_keeps_one_leg_through_restart(self):
        pos=position()
        entry=dict(orderId='11',symbol=pos.symbol,positionSide=pos.side,side='BUY')
        close=dict(orderId='12',symbol=pos.symbol,positionSide=pos.side,side='SELL')
        remember_fee_leg(pos,entry,2,100,.1,'taker-rate')
        for qty in (1,1,2,1):remember_fee_leg(pos,close,qty,101,qty*.05,'taker-rate')
        self.assertEqual(len(pos.fee_legs),2)
        self.assertEqual(pos.fee_legs['12']['qty'],2)
        pos.control_fill_state={'12':{'qty':2,'value':202,'fee':.1}}
        restored=Position(**json.loads(json.dumps(asdict(pos))))
        self.assertTrue(reconcile_position_fees(restored,lambda leg,t:.099 if leg['role']=='entry' else .100999))
        self.assertEqual(restored.control_fill_state['12']['fee'],.100999)
        self.assertEqual(restored.fee_source,'reported-fee')

    def test_failed_read_cannot_partially_promote_fee_totals(self):
        pos=position()
        for oid,side,price in [('11','BUY',100),('12','SELL',100.1)]:
            remember_fee_leg(pos,dict(orderId=oid,symbol=pos.symbol,positionSide=pos.side,side=side),2,price,.1,'taker-rate')
        before=asdict(pos)
        self.assertFalse(reconcile_position_fees(pos,lambda leg,t:.02 if leg['role']=='entry' else None))
        self.assertEqual(asdict(pos),before)
        self.assertFalse(reconcile_position_fees(pos,lambda *a:(_ for _ in()).throw(TimeoutError())))
        self.assertEqual(asdict(pos),before)

    def test_missing_add_coverage_stays_an_estimate(self):
        pos=position();pos.qty=3
        for oid,side in [('11','BUY'),('12','SELL')]:
            remember_fee_leg(pos,dict(orderId=oid,symbol=pos.symbol,positionSide=pos.side,side=side),2,100,.1,'taker-rate')
        self.assertFalse(reconcile_position_fees(pos,lambda *a:self.fail('incomplete parent queried fees')))
        self.assertEqual(pos.fee_source,'exchange-rate-estimate')

    def test_base_and_add_commissions_are_not_counted_twice(self):
        pos=position();pos.qty=3
        for oid,side,qty in [('11','BUY',2),('13','BUY',1),('12','SELL',3)]:
            remember_fee_leg(pos,dict(orderId=oid,symbol=pos.symbol,positionSide=pos.side,side=side),qty,100,.1,'taker-rate')
        self.assertTrue(reconcile_position_fees(pos,lambda leg,t:{'11':.1,'13':.05,'12':.15}[leg['orderId']]))
        self.assertAlmostEqual(pos.entry_fee_usdt,.15)
        self.assertEqual(pos.base_entry_fee_usdt,.1)
        self.assertEqual(pos.exit_fee_usdt,.15)


if __name__=='__main__':unittest.main()
