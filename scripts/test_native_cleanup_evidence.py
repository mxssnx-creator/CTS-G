"""Exact archived batch/market evidence, deadline and entry scheduling regressions."""
import copy
from pathlib import Path
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server/pulse'))
import pulse_trader as pt
from native_cleanup_evidence import terminal_intents, resolve_bound_intents
from x02_native_session import order_bodies


class CleanupEvidence(unittest.TestCase):
    def fixture(self):
        body = dict(symbol='X-USDT', clientOrderID='GabcEntry', side='BUY', positionSide='LONG', type='MARKET')
        row = {**body, 'clientOrderID': 'gabcEntry'.lower(), 'orderId': '9007199254740993123',
               'status': 'FILLED', 'executedQty': '1'}
        intent = dict(method='POST', path='/openApi/swap/v2/trade/order', at=100,
                      body=body, response={'code': 0, 'data': {'order': row}})
        return body, row, intent

    def test_archived_market_and_batch_controls_resolve_in_one_history_read(self):
        body, entry, intent = self.fixture()
        controls = [{**body, 'clientOrderID': cid, 'type': typ, 'side': 'SELL'}
                    for cid, typ in [('GabcSl', 'STOP_MARKET'), ('GabcTp', 'TAKE_PROFIT_MARKET')]]
        rows = [entry] + [{**b, 'clientOrderID': b['clientOrderID'].lower(), 'orderId': str(i+2),
                           'status': 'CANCELLED', 'executedQty': '0'} for i, b in enumerate(controls)]
        batch = dict(method='POST', path='/openApi/swap/v2/trade/batchOrders', at=101,
                     body={'batchOrders': __import__('json').dumps(controls)}, response={'code': 0, 'data': {'orders': rows[1:]}})
        calls = []; remembered = []
        api = SimpleNamespace(get=lambda *args: calls.append(args) or {'code': 0, 'data': {'orders': rows}})
        self.assertEqual(terminal_intents(api, [intent, batch], time.monotonic()+2, order_bodies,
                                         lambda r,b: remembered.append((r,b))), [])
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].endswith('/allOrders'))
        self.assertEqual(len(remembered), 3)

    def test_wrong_id_symbol_side_type_or_unknown_status_stays_unresolved(self):
        body, row, intent = self.fixture()
        for key, value in [('orderId','88'), ('symbol','OTHER-USDT'), ('positionSide','SHORT'),
                           ('side','SELL'), ('type','LIMIT'), ('status','NEW'), ('clientOrderID','foreign')]:
            with self.subTest(key=key):
                api=SimpleNamespace(get=lambda *a: {'code':0,'data':{'orders':[{**row,key:value}]}})
                self.assertEqual(terminal_intents(api,[intent],time.monotonic()+2,order_bodies),[body['clientOrderID']])
        for reply in [{'code':0,'data':{'orders':[]}}, {'code':-1}, {'code':0,'data':{'orders':[row,row]}}]:
            api=SimpleNamespace(get=lambda *a:reply)
            self.assertEqual(terminal_intents(api,[intent],time.monotonic()+2,order_bodies),[body['clientOrderID']])

    def test_expired_deadline_dispatches_no_history_reads(self):
        body, _, intent = self.fixture(); calls=[]
        api=SimpleNamespace(get=lambda *a:calls.append(a))
        self.assertEqual(terminal_intents(api,[intent],time.monotonic()-1,order_bodies),[body['clientOrderID']])
        self.assertEqual(calls,[])

    def test_exact_bound_control_resolves_when_archive_omits_it(self):
        body, row, intent = self.fixture()
        body.update(type='STOP_MARKET', side='SELL')
        row.update(type='STOP_MARKET', side='SELL', status='CANCELLED')
        calls=[];remembered=[]
        api=SimpleNamespace(get=lambda *args:calls.append(args) or {'code':0,'data':{'order':row}})
        binding={(body['symbol'],row['orderId']):body}
        self.assertEqual(resolve_bound_intents(api,[intent],[body['clientOrderID']],time.monotonic()+2,
            order_bodies,binding,lambda r,b:remembered.append((r,b))),[])
        self.assertEqual(calls[0][1],{'symbol':body['symbol'],'orderId':row['orderId']})
        self.assertEqual(len(remembered),1)

    def test_bound_lookup_rejects_wrong_identity_and_unfinished_market(self):
        body,row,intent=self.fixture();binding={(body['symbol'],row['orderId']):body}
        for key,value in [('orderId','88'),('symbol','OTHER-USDT'),('positionSide','SHORT'),
                          ('side','SELL'),('type','LIMIT'),('status','NEW'),('clientOrderID','foreign')]:
            with self.subTest(key=key):
                api=SimpleNamespace(get=lambda *args:{'code':0,'data':{'order':{**row,key:value}}})
                self.assertEqual(resolve_bound_intents(api,[intent],[body['clientOrderID']],time.monotonic()+2,
                    order_bodies,binding),[body['clientOrderID']])

    def test_unbound_or_expired_submissions_remain_unresolved_without_lookup(self):
        body,row,intent=self.fixture();calls=[]
        api=SimpleNamespace(get=lambda *args:calls.append(args))
        for response,deadline in [({'code':-1,'error':True},time.monotonic()+2),
                                  (intent['response'],time.monotonic()-1)]:
            pending={**intent,'response':response}
            self.assertEqual(resolve_bound_intents(api,[pending],[body['clientOrderID']],deadline,
                order_bodies,{}),[body['clientOrderID']])
        self.assertEqual(calls,[])

    def test_definite_refusal_needs_no_fill_lookup_but_unknown_dispatch_does(self):
        body,row,intent=self.fixture();calls=[]
        api=SimpleNamespace(get=lambda *args:calls.append(args)or {'code':0,'data':{'orders':[]}})
        refused={**intent,'response':{'code':109420,'msg':'position not exist'}}
        self.assertEqual(terminal_intents(api,[refused],time.monotonic()+2,order_bodies),[])
        self.assertEqual(calls,[])
        unknown={**intent,'response':{'code':-1,'error':True}}
        self.assertEqual(terminal_intents(api,[unknown],time.monotonic()+2,order_bodies),[body['clientOrderID']])
        self.assertEqual(len(calls),1)
        # An arbitrary nonzero response is not evidence that no order was sent.
        uncertain={**intent,'response':{'code':109421,'msg':'order unavailable'}}
        self.assertEqual(terminal_intents(api,[uncertain],time.monotonic()+2,order_bodies),[body['clientOrderID']])
        self.assertEqual(len(calls),2)

    def test_slow_placements_yield_and_all_eligible_entries_still_progress(self):
        p=pt.Pulse.__new__(pt.Pulse);p.halted=False;p.real_stage_paper=False
        p.sets=SimpleNamespace(enabled=True,packs={},use_historic_gate=False)
        p.strategy_closes=lambda:[];p.entries_blocked=lambda:False
        p.coord=SimpleNamespace(net_streak=lambda rows:0,gate=lambda *a,**k:(True,[],{}),
                                pick_rearrange=lambda *a:None)
        p.rearrange_opens=lambda:[];p.open={};p.available=100;p.cooldown={};p.skip_log={}
        p.strat_general=False;p.avail_notional=lambda:100
        symbols=['A-USDT','B-USDT','C-USDT'];clock=[100.0];calls=[]
        p.set_entry_candidates=lambda:{s:(.8,s,1,'set:general:test') for s in symbols if s not in p.open}
        def place(s,*args):
            calls.append(s);p.open[s]=object();clock[0]+=2
        p.place=place;p.maybe_block_adds=lambda:None;p.maybe_dca_adds=lambda:None
        with patch.object(pt,'MAX_OPEN',50),patch.object(pt.time,'monotonic',lambda:clock[0]):
            for count in [1,2,3]:
                p.maybe_entries();self.assertEqual(len(calls),count)
        self.assertEqual(set(calls),set(symbols))
        self.assertEqual(len(calls),len(set(calls)))


if __name__ == '__main__':
    unittest.main()
