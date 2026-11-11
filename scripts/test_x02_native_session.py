"""No-network tests for the bounded native demo wrapper (never starts Pulse)."""
import json
import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import x02_native_session as session


SYMBOLS = [f'S{i}-USDT' for i in range(50)]


class NativeSessionTests(unittest.TestCase):
    def test_bulk_history_pass_is_limited_to_paused_warmup(self):
        calls=[]
        load=SimpleNamespace(history_window=lambda names,chunk,cursor:calls.append(chunk) or (names[:chunk],cursor+chunk),
                             last_budget=SimpleNamespace(hist_run=False))
        pulse=SimpleNamespace(load=load);journal=SimpleNamespace(phase='warming')
        session.bulk_warmup(pulse,journal)
        self.assertEqual(load.history_window(SYMBOLS,2,0),(SYMBOLS,0))
        self.assertFalse(load.last_budget.hist_run,'the native memory/critical guard is preserved')
        journal.phase='active'
        self.assertEqual(load.history_window(SYMBOLS,2,0),(SYMBOLS[:2],2))
        self.assertEqual(calls,[2])

    def test_history_seed_resumes_candles_and_rejects_foreign_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path=root/'capture.json'
            origin={'host':session.HOST,'connection':session.CONN}
            (root/'session.json').write_text(json.dumps(origin))
            data={'capturedAt':1000,'barSeconds':60,'symbols':{'S0-USDT':{'lastClosedBarAt':900,'bars':[[1,2,.5,1.5,1]]}}}
            path.write_text(json.dumps(data));calls=[]
            book=SimpleNamespace(ingest_bars=lambda *args,**kwargs:calls.append((args,kwargs)))
            self.assertEqual(session.seed_history(book,path,SYMBOLS,1100),1)
            self.assertEqual(calls[0][1],{'last_t':900})
            (root/'session.json').write_text(json.dumps({**origin,'host':'https://foreign.example'}))
            with self.assertRaises(ValueError):session.seed_history(book,path,SYMBOLS,1100)
            self.assertEqual(len(calls),1)

    def test_fee_cache_is_account_bound_fresh_and_rejects_invalid_rates(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'fees.json';credentials={'api_key':'fixture-key'}
            values={'ok':{'t':900,'maker':.0002,'taker':.0005},
                    'old':{'t':1,'maker':.0002,'taker':.0005},
                    'future':{'t':5001,'maker':.0002,'taker':.0005},
                    'bad':{'t':900,'maker':-.1,'taker':.0005}}
            session.save_fee_cache(path,credentials,values)
            self.assertEqual(session.load_fee_cache(path,credentials,4000),{'ok':values['ok']})
            self.assertEqual(session.load_fee_cache(path,{'api_key':'another-account'},4000),{})

    def test_read_retries_never_treat_timeouts_as_an_empty_account(self):
        replies=iter([{'code':-1,'error':True},{'code':-1,'error':True},{'code':0,'data':[]}])
        reads=[]
        api=SimpleNamespace(get=lambda path:reads.append(path) or next(replies))
        with patch.object(session.time,'sleep'):
            self.assertEqual(session.rows(api,'/positions'),[])
        self.assertEqual(reads,['/positions']*3)
        with patch.object(session.time,'sleep'),self.assertRaisesRegex(RuntimeError,'unavailable'):
            session.rows(SimpleNamespace(get=lambda path:{'code':-1,'error':True}),'/positions')

    def test_overlay_preserves_admission_and_requires_explicit_universe(self):
        raw = {'setMinPf': 1.37, 'stageFullWindow': True, 'lastNByRange': {'micro': {'baseLong': 30}}, 'blockEnabled': False}
        ov = session.frozen_overlay(raw, SYMBOLS)
        for k in raw:
            self.assertEqual(raw[k], ov[k])
        self.assertEqual((ov['volumeFactor'], ov['histLookbackBars'], ov['symbolCap']), (.05, 1080, 50))
        self.assertFalse(ov['symbolsDynamic'])
        self.assertFalse(ov['indExtraSources'])
        self.assertEqual(ov['minSlDistancePct'], .1)
        for bad in (SYMBOLS[:49], SYMBOLS[:-1]+[SYMBOLS[0]]):
            with self.assertRaises(ValueError): session.frozen_overlay(raw, bad)

    def test_total_context_is_bounded_before_any_connection(self):
        raw = json.loads((session.ROOT / 'server/pulse/overlay-bingx-x02.json').read_text())
        self.assertLessEqual(session.context_requirement(session.frozen_overlay(raw, SYMBOLS)), 4320)
        with self.assertRaises(ValueError):
            session.context_requirement(session.frozen_overlay(raw, SYMBOLS, 48))

    def test_relocation_is_complete_and_does_not_move_unrelated_values(self):
        mod = SimpleNamespace(DIR='/opt/native', STATS_PATH='/opt/native/stats.json', OTHER='/opt/other')
        session.relocate_runtime(mod, Path('/private/run'), 'G1234')
        self.assertEqual((mod.DIR, mod.STATS_PATH, mod.OTHER), ('/private/run', '/private/run/stats.json', '/opt/other'))
        self.assertEqual(mod.dump_cts_settings(), {})
        self.assertEqual(mod.redis_hget('api_key'), '')

    def test_journal_no_entry_during_warmup_and_no_retry_unknown_write(self):
        calls = []
        def request(*args):
            calls.append(args)
            raise TimeoutError('fixture')
        with tempfile.TemporaryDirectory() as td:
            api = SimpleNamespace(_req=request,base=session.HOST)
            journal = session.Journal(api, td, 'G1234')
            body = {'clientOrderID':'g1234entry','symbol':'S0-USDT','side':'BUY','positionSide':'LONG','type':'MARKET'}
            reply = api._req('POST','/openApi/swap/v2/trade/order',body)
            self.assertTrue(reply['error']); self.assertEqual(calls, [])
            journal.phase, journal.deadline = 'active', float('inf')
            with self.assertRaises(TimeoutError): api._req('POST','/openApi/swap/v2/trade/order',body)
            self.assertEqual(len(calls), 1)
            api.base = 'https://api.bybit.com'
            with self.assertRaises(RuntimeError): api._req('POST','/openApi/swap/v2/trade/order',body)
            self.assertEqual(len(calls), 1)
            api.base = session.HOST
            rows = [json.loads(x) for x in Path(td,'mutation-journal.jsonl').read_text().splitlines()]
            self.assertEqual([x['result'] for x in rows], ['pending','unknown'])
            journal.phase = 'cleanup'
            self.assertTrue(api._req('POST','/openApi/swap/v2/trade/order',body)['error'])
            with self.assertRaises(RuntimeError):
                api._req('POST','/openApi/swap/v2/trade/order',{**body,'clientOrderID':'foreign'})
            self.assertEqual(len(calls), 1)

    def test_readiness_requires_each_symbol_fee_and_current_history(self):
        book = SimpleNamespace(progress=SimpleNamespace(ready=True), _running=False, _lookback_pending=False,
            _history_snapshot_symbols=set(SYMBOLS), bars={s:[0]*2880 for s in SYMBOLS}, bar_t={s:940 for s in SYMBOLS})
        pulse = SimpleNamespace(sets=book,contracts={},fee_schedule=SimpleNamespace(rates=lambda *args:(.0002,.0005,'account-rate')))
        self.assertTrue(session.ready(pulse,SYMBOLS,2880,1000))
        book.bars[SYMBOLS[-1]] = [0]*100
        self.assertFalse(session.ready(pulse,SYMBOLS,2880,1000))
        book.bars[SYMBOLS[-1]] = [0]*2880
        book.bar_t[SYMBOLS[0]] = 800
        self.assertFalse(session.ready(pulse,SYMBOLS,2880,1000))
        book.bar_t[SYMBOLS[0]] = 940
        pulse.fee_schedule.rates = lambda *args:(.0002,.0005,'configured-estimate')
        self.assertFalse(session.ready(pulse,SYMBOLS,2880,1000))

    def test_cleanup_cancels_only_own_controls_after_confirmed_flat(self):
        controls = [{'symbol':'S0-USDT','orderId':'9','clientOrderID':'G1234sl'}]
        calls=[]
        def get(path, *_args):
            calls.append(path)
            return {'code':0,'data':{'orders':list(controls)} if path.endswith('openOrders') else []}
        def delete(path, data):
            self.assertEqual(data['orderId'],'9'); controls.clear()
        pulse=SimpleNamespace(api=SimpleNamespace(get=get,delete=delete),open={},sync_own_fills=lambda:None,adopt_exchange_positions=lambda:None)
        journal=SimpleNamespace(tag='G1234',intents=[],lock=__import__('threading').RLock(), owns_order=lambda symbol, oid: symbol == 'S0-USDT' and oid == '9')
        result=session.cleanup(pulse,journal,1)
        self.assertTrue(result['confirmedFlat'])
        self.assertGreaterEqual(calls.count('/openApi/swap/v2/user/positions'),2)

    def test_mutations_require_exact_journaled_order_ids_and_never_change_account_settings(self):
        calls = []
        def request(method, path, body):
            calls.append((method, path, body))
            if method == 'POST':
                return {'code': 0, 'data': {'order': {'orderId': '123', 'symbol': 'S0-USDT', 'clientOrderID': 'G1234entry'}}}
            if method == 'GET':
                return {'code': 0, 'data': {'orders': [{'orderId':'999','symbol':'S0-USDT','clientOrderID':'G1234foreign'}]}}
            return {'code': 0, 'data': {}}
        with tempfile.TemporaryDirectory() as td:
            api = SimpleNamespace(_req=request, base=session.HOST)
            journal = session.Journal(api, td, 'G1234')
            journal.phase, journal.deadline = 'active', float('inf')
            order = {'clientOrderID':'G1234entry','symbol':'S0-USDT','side':'BUY','positionSide':'LONG','type':'MARKET'}
            api._req('POST','/openApi/swap/v2/trade/order',order)
            api._req('GET','/openApi/swap/v2/trade/openOrders',{})
            self.assertTrue(journal.owns_client('S0-USDT','G1234entry'))
            self.assertFalse(journal.owns_client('S0-USDT','G1234foreign'))
            self.assertTrue(journal.owns_order('S0-USDT','123'))
            self.assertFalse(journal.owns_order('S0-USDT','999'))
            for method, path, body in (
                ('DELETE','/openApi/swap/v2/trade/order',{'symbol':'S0-USDT','orderId':'999'}),
                ('DELETE','/openApi/swap/v2/trade/allOpenOrders',{'symbol':'S0-USDT'}),
                ('POST','/openApi/swap/v2/trade/leverage',{'symbol':'S0-USDT','leverage':5}),
                ('POST','/openApi/swap/v2/trade/marginType',{'symbol':'S0-USDT','marginType':'CROSSED'}),
            ):
                with self.assertRaises(RuntimeError): api._req(method,path,body)
            for extra in ({'closePosition':'true'}, {'positionId':'55'}):
                reply=api._req('POST','/openApi/swap/v2/trade/order',
                    {'symbol':'S0-USDT','clientOrderID':'G1234close','side':'SELL','positionSide':'LONG','type':'MARKET',**extra})
                self.assertTrue(reply['localPolicyRejection'])
            api._req('DELETE','/openApi/swap/v2/trade/order',{'symbol':'S0-USDT','orderId':'123'})
            self.assertEqual([x[0] for x in calls], ['POST','GET','DELETE'])

    def test_unknown_submission_can_bind_only_its_exact_reconciled_client_identity(self):
        calls = []
        def request(method, path, body):
            calls.append((method, path))
            if method == 'POST': raise TimeoutError('fixture')
            if method == 'GET': return {'code':0,'data':{'order':{'orderId':'456','symbol':'S0-USDT'}}}
            return {'code':0,'data':{}}
        with tempfile.TemporaryDirectory() as td:
            api=SimpleNamespace(_req=request,base=session.HOST)
            journal=session.Journal(api,td,'G1234')
            journal.phase,journal.deadline='active',float('inf')
            body={'clientOrderID':'G1234entry','symbol':'S0-USDT','side':'BUY','positionSide':'LONG','type':'MARKET'}
            with self.assertRaises(TimeoutError): api._req('POST','/openApi/swap/v2/trade/order',body)
            with self.assertRaisesRegex(RuntimeError, 'already journaled'): api._req('POST','/openApi/swap/v2/trade/order',body)
            api._req('GET','/openApi/swap/v2/trade/order',{'symbol':'S0-USDT','clientOrderID':'G1234foreign'})
            self.assertFalse(journal.owns_order('S0-USDT','456'))
            api._req('GET','/openApi/swap/v2/trade/order',{'symbol':'S0-USDT','clientOrderID':'G1234entry'})
            self.assertTrue(journal.owns_order('S0-USDT','456'))
            api._req('DELETE','/openApi/swap/v2/trade/order',{'symbol':'S0-USDT','orderId':'456'})
            self.assertEqual(sum(method == 'POST' for method, _ in calls),1)

    def test_test_session_never_changes_leverage_and_uses_conservative_margin(self):
        pulse=SimpleNamespace()
        session.freeze_account_settings(pulse)
        self.assertIsNone(pulse.set_leverage())
        self.assertEqual(pulse.ensure_max_leverage('S0-USDT',force=True),1)
        self.assertEqual(pulse.leverage_for(object()),1)

    def test_execution_requires_private_credentials_before_any_connection(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); symbols=root/'symbols.json'; symbols.write_text(json.dumps(SYMBOLS))
            out=root/'session'
            with self.assertRaisesRegex(ValueError,'credential'),contextlib.redirect_stdout(io.StringIO()):
                session.main(['--overlay',str(session.ROOT/'server/pulse/overlay-bingx-x02.json'),
                              '--symbols',str(symbols),'--out',str(out),'--execute'])
            report=json.loads((out/'session.json').read_text())
            self.assertFalse(report['executed'])
            self.assertFalse(report['complete'])
            self.assertFalse((out/'mutation-journal.jsonl').exists())

    def test_inventory_counts_own_partial_fills_once_and_caps_every_close_and_control(self):
        calls=[]
        def request(method,path,body):
            calls.append(body)
            q='2' if body['side']=='BUY' else '0.5'
            return {'code':0,'data':{'order':{**body,'orderId':str(len(calls)),
                    'executedQty':q if body['type']=='MARKET' else '0'}}}
        with tempfile.TemporaryDirectory() as td:
            api=SimpleNamespace(_req=request,base=session.HOST)
            journal=session.Journal(api,td,'G1234')
            journal.phase,journal.deadline='active',float('inf')
            entry=dict(symbol='S0-USDT',clientOrderID='G1234entry',side='BUY',positionSide='LONG',type='MARKET',quantity='2')
            ack=api._req('POST','/openApi/swap/v2/trade/order',entry)
            journal.remember_order(ack['data']['order'])
            self.assertEqual(journal.inventory[('S0-USDT','LONG')],2)
            for kind in ['MARKET','STOP_MARKET','TAKE_PROFIT_MARKET']:
                reply=api._req('POST','/openApi/swap/v2/trade/order',
                    {**entry,'side':'SELL','type':kind,'quantity':'3','clientOrderID':'G1234tooMuch'+kind})
                self.assertTrue(reply['localPolicyRejection'])
            close={**entry,'side':'SELL','quantity':'.5','clientOrderID':'G1234partial'}
            ack=api._req('POST','/openApi/swap/v2/trade/order',close)
            journal.remember_order(ack['data']['order'])
            self.assertEqual(journal.inventory[('S0-USDT','LONG')],session.Decimal('1.5'))
            # A foreign fill and a lagging original receipt cannot increase capacity.
            journal.remember_order({**entry,'clientOrderID':'foreign','orderId':'999','executedQty':'100'})
            journal.remember_order({**entry,'orderId':'1','executedQty':'1'})
            self.assertEqual(journal.inventory[('S0-USDT','LONG')],session.Decimal('1.5'))
            self.assertEqual(len(calls),2)

    def test_native_filled_order_poll_reduces_control_inventory_once(self):
        orders=[];filled=[0]
        def request(method,path,body):
            if method=='GET':return {'code':0,'data':{'orders':[{**orders[-1],'executedQty':str(filled[0])}]}}
            order={**body,'orderId':str(len(orders)+1),'executedQty':'2'if body['side']=='BUY'else'0'}
            orders.append(order);return {'code':0,'data':{'order':order}}
        with tempfile.TemporaryDirectory() as td:
            api=SimpleNamespace(_req=request,base=session.HOST)
            journal=session.Journal(api,td,'G1234');journal.phase,journal.deadline='active',float('inf')
            entry=dict(symbol='S0-USDT',clientOrderID='G1234entry',side='BUY',positionSide='LONG',type='MARKET',quantity='2')
            api._req('POST','/openApi/swap/v2/trade/order',entry)
            api._req('POST','/openApi/swap/v2/trade/order',{**entry,'clientOrderID':'G1234stop','type':'STOP_MARKET','side':'SELL'})
            filled[0]=1
            for _ in range(2):api._req('GET','/openApi/swap/v2/trade/allOrders',{})
            self.assertEqual(journal.inventory[('S0-USDT','LONG')],1)
            filled[0]=2;api._req('GET','/openApi/swap/v2/trade/allOrders',{})
            self.assertEqual(journal.inventory[('S0-USDT','LONG')],0)

    def test_native_protection_and_partial_close_use_only_quantity_fallbacks(self):
        sys_path=str(session.ROOT/'server/pulse')
        import sys
        if sys_path not in sys.path:sys.path.insert(0,sys_path)
        import pulse_trader as pt
        dispatched=[];closes=[];counter=iter(range(1,100))
        def request(method,path,body):
            self.assertNotIn('closePosition',body)
            self.assertNotIn('positionId',body)
            self.assertNotIn('cancelReplace',path)
            dispatched.append(body)
            quantity='0'
            if body['type']=='MARKET':
                closes.append(body)
                quantity='.5' if len(closes)==1 else body['quantity']
            return {'code':0,'data':{'order':{**body,'orderId':str(next(counter)),
                    'avgPrice':'101','status':'FILLED' if quantity!='0' else 'NEW','executedQty':quantity}}}
        with tempfile.TemporaryDirectory() as td,patch.object(pt,'LOG_PATH',str(Path(td)/'pulse.log')):
            api=SimpleNamespace(_req=request,base=session.HOST,path_cd={})
            api.post=lambda path,body:api._req('POST',path,body)
            api.batch_place=lambda bodies:api.post('/openApi/swap/v2/trade/batchOrders',{'batchOrders':json.dumps(bodies)})
            journal=session.Journal(api,td,'G1234')
            journal.phase,journal.deadline='active',float('inf')
            # An independently observed own entry supplies the transport's capacity.
            entry=dict(symbol='S0-USDT',clientOrderID='G1234entry',side='BUY',positionSide='LONG',type='MARKET',quantity='2')
            journal.owned_clients.add(('S0-USDT','g1234entry'))
            journal.submitted[('S0-USDT','g1234entry')]=entry
            journal.remember_order({**entry,'orderId':'999','executedQty':'2'})
            p=pt.Pulse.__new__(pt.Pulse);p.api=api;p.ctrl_skip={};p._oo_cache={}
            p.px={'S0-USDT':100};p.last_px={};p.errors=0;p.last_error=''
            p.contracts={'S0-USDT':pt.Contract(symbol='S0-USDT',min_qty=.1,step=.1,qprec=1,pprec=2,min_usdt=1,max_lev=1)}
            seq=iter(range(100));p.cid=lambda kind,**kw:'G1234'+kind+str(next(seq))
            p.clamp_ctrl_price=lambda pos,kind,price:price
            p.desired_sl_tp=lambda pos:(None,None,99,103)
            p.save_open_book=lambda:True;p.seen_fill_cids=set()
            p.confirmed_order=lambda data,*args:data
            p.record_exit_fee=lambda *args:None
            pos=pt.Position(symbol='S0-USDT',side='LONG',qty=2,entry=100,opened_at=1,sl=99,tp=103,peak=100)
            p.place_ctrl_pair(pos)
            self.assertTrue(pos.controls_ok)
            self.assertFalse(pos.close_position)
            self.assertEqual(pos.ctrl_qty,2)
            self.assertEqual([row['type'] for row in dispatched],['STOP_MARKET','TAKE_PROFIT_MARKET'])
            pos.qty=3
            self.assertTrue(p.missing_controls(pos),'growth must invalidate fixed-quantity controls')
            pos.qty=2
            complete,_=p.market_close(pos)
            self.assertFalse(complete)
            self.assertEqual(pos.qty,1.5)
            self.assertEqual(pos.close_filled_qty,.5)
            complete,price=p.market_close(pos)
            self.assertTrue(complete)
            self.assertEqual(price,101)
            self.assertEqual([float(row['quantity']) for row in closes],[2,1.5])
            self.assertEqual(journal.inventory[('S0-USDT','LONG')],0)
            self.assertFalse(api.cancel_replace_ok)

    def test_another_session_cannot_join_between_empty_preflight_and_start(self):
        def get(path):
            return {'code':0,'data':[{'symbol':'S0-USDT','positionSide':'LONG','positionAmt':'1'}]}
        with self.assertRaises(session.SessionEnd): session.require_empty_account(SimpleNamespace(get=get))
        def orders(path):
            return {'code':0,'data':[] if path.endswith('positions') else {'orders':[{'clientOrderID':'foreign'}]}}
        with self.assertRaises(session.SessionEnd): session.require_empty_account(SimpleNamespace(get=orders))


if __name__ == '__main__': unittest.main()
