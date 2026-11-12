"""No-network tests for the bounded native demo wrapper (never starts Pulse)."""
import json
import contextlib
import io
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import x02_native_session as session
sys.path.insert(0, str(session.ROOT / 'server/pulse'))

# Every test must also work in the direct-file CLI used by CI.
sys.path.insert(0, str(session.ROOT / "server" / "pulse"))


SYMBOLS = [f'S{i}-USDT' for i in range(50)]


class NativeSessionTests(unittest.TestCase):
    def test_watchdog_does_not_count_a_stale_cycle_as_completed_observation(self):
        self.assertEqual(session.end_reason('active',200,50,150,100,60),'main cycle heartbeat deadline')
        self.assertEqual(session.end_reason('active',200,50,150,199,60),'observation complete')
        self.assertEqual(session.end_reason('warming',200,150,None,0,60),'readiness deadline')
        self.assertIsNone(session.end_reason('warming',100,150,None,0,60))
        self.assertIsNone(session.end_reason('cleanup',200,50,150,0,60,True))

    def test_native_cycle_cannot_hide_a_stall_when_watcher_thread_does_not_run(self):
        clock = [0.0]
        def get(path, *args):
            if path.endswith('/balance'): return {'code':0,'data':[{'asset':'VST'}]}
            if path.endswith('/positions'): return {'code':0,'data':[]}
            if path.endswith('/openOrders'): return {'code':0,'data':{'orders':[]}}
            raise AssertionError(path)
        api = SimpleNamespace(base=session.HOST,get=get,_req=lambda *a:None)
        class Pulse:
            def __init__(self, api, contracts):
                self.load=SimpleNamespace(partial=True, configure=lambda ov:None)
                self.ignored_foreign=False
                self.fee_schedule=SimpleNamespace(account={},snapshot=lambda *a:{})
                self.active_fee_contracts=lambda:{}
                self.position_cost_pct=.1
                self.slippage_allowance_pct=10
                self.pool=SimpleNamespace(shutdown=lambda **kw:None)
            def _one_cycle(self): clock[0] += 20
            def run(self): self._one_cycle()
        with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(session.os.environ,{}))
            prior_umask=session.os.umask(0o077)
            stack.callback(session.os.umask,prior_umask)
            root=Path(td)
            credentials=root/'credentials.json'
            credentials.write_text('{"api_key":"fixture","api_secret":"fixture","is_testnet":true}')
            credentials.chmod(0o600)
            overlay=root/'overlay.json';overlay.write_text('{}')
            symbols=root/'symbols.json';symbols.write_text(json.dumps(SYMBOLS))
            native=SimpleNamespace(DIR=str(root/'native'),PAUSE_PATH=str(root/'native/PAUSE'),
                STOP_PATH=str(root/'native/STOP'),CONN_SHORT=session.CONN,IS_BYBIT=False,BASE=session.HOST,
                make_api=lambda *a:api,load_contracts=lambda ss:{s:None for s in ss},Pulse=Pulse)
            stack.enter_context(patch.dict(sys.modules,{'pulse_trader':native}))
            for name,replacement in {
                'context_requirement':lambda ov:60,'shared_account_protection_verified':lambda:True,
                'freeze_account_settings':lambda p:None,'install_history_cancellation':lambda p:None,
                'collect_account_fees':lambda *a:None,'prepare_history_warmup':lambda *a,**kw:None,
                'native_admission_ready':lambda p:True,'capture':lambda *a,**kw:None,
                'cleanup':lambda *a:{'confirmedFlat':True},
            }.items():stack.enter_context(patch.object(session,name,replacement))
            stack.enter_context(patch.object(session.time,'monotonic',lambda:clock[0]))
            stack.enter_context(patch.object(session.time,'time',lambda:1_700_000_000+clock[0]))
            stack.enter_context(patch.object(session.signal,'signal'))
            stack.enter_context(patch.object(session.threading,'Thread',return_value=SimpleNamespace(start=lambda:None)))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            result=session.main(['--overlay',str(overlay),'--symbols',str(symbols),'--out',str(root/'run'),
                '--credentials',str(credentials),'--execute','--admission','native','--replay-workers','1','--minutes','30','--heartbeat-seconds','15'])
            report=json.loads((root/'run/session.json').read_text())
            self.assertEqual(result,1)
            self.assertEqual(report['stopReason'],'main cycle heartbeat deadline',str(report))
            self.assertFalse(report['complete'])
            self.assertTrue(report['cleanup']['confirmedFlat'])

    def test_periodic_capture_skips_history_and_full_capture_releases_lock_before_io(self):
        import threading
        class Pulse:
            def write_stats(self, **kwargs): pass
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'stats.json';path.write_text('{"openCount":0}')
            pulse=Pulse();pulse.sets=None
            with patch.object(sys.modules[Pulse.__module__],'STATS_PATH',str(path),create=True):
                session.capture(pulse,td,'periodic',include_candles=False)
                self.assertFalse((Path(td)/'periodic-candles.json').exists())
                book=SimpleNamespace(_lock=threading.RLock(),bars={'X':[[1,2,1,2,3]]},bar_t={'X':60})
                pulse.sets,pulse.contracts=book,{'X':None}
                native=session.atomic_json
                entered=[]
                def write(target,value):
                    if str(target).endswith('-candles.json'):
                        def reader():
                            with book._lock:entered.append(True)
                        thread=threading.Thread(target=reader);thread.start();thread.join(1)
                        self.assertEqual(entered,[True],'snapshot IO retained the trading lock')
                    native(target,value)
                with patch.object(session,'atomic_json',write):session.capture(pulse,td,'ready')
                self.assertEqual(json.loads((Path(td)/'ready-candles.json').read_text())['symbols']['X']['bars'],book.bars['X'])

    def test_history_shutdown_cancellation_leaves_main_close_scoring_available(self):
        calls=[]
        book=SimpleNamespace(_score_one=lambda st:calls.append(st))
        def replay(**kwargs):book._score_one('history')
        book.replay_all=replay
        pulse=SimpleNamespace(sets=book,_hist_stop=False)
        session.install_history_cancellation(pulse)
        book.replay_all()
        pulse._hist_stop=True
        with self.assertRaises(SystemExit):book.replay_all()
        book._score_one('live-close')
        self.assertEqual(calls,['history','live-close'])

    def test_native_admission_requires_a_native_qualified_set(self):
        book=SimpleNamespace(progress=SimpleNamespace(ready=True),_running=False,_lookback_pending=False,
                             by_idx=[1,2],gate_reason=lambda st:'real')
        pulse=SimpleNamespace(sets=book)
        self.assertFalse(session.native_admission_ready(pulse))
        book.gate_reason=lambda st:'' if st==2 else 'main'
        self.assertTrue(session.native_admission_ready(pulse))
        book._lookback_pending=True
        self.assertFalse(session.native_admission_ready(pulse))
        book._lookback_pending=False;book._running=True
        self.assertFalse(session.native_admission_ready(pulse))

    def test_current_quality_checked_replay_does_not_need_a_retained_snapshot(self):
        book = SimpleNamespace(progress=SimpleNamespace(ready=True), _running=False, _lookback_pending=False,
            _history_snapshot_symbols=set(),bars={s:[0]*2880 for s in SYMBOLS},bar_t={s:940 for s in SYMBOLS},
            usable=lambda s:s!=SYMBOLS[0],lookback=1080,_topo_gen=7,
            _hist_replay_key={s:(1080,7)for s in SYMBOLS[1:]},_hist_replay_at={s:990 for s in SYMBOLS[1:]},
            _snapshot_max_age=lambda:300)
        pulse = SimpleNamespace(sets=book,contracts={},fee_schedule=SimpleNamespace(rates=lambda *args:(.0002,.0005,'account-rate')))
        self.assertTrue(session.ready(pulse,SYMBOLS,2880,1000))
        book._hist_replay_key[SYMBOLS[-1]]=(1080,6)
        self.assertFalse(session.ready(pulse,SYMBOLS,2880,1000))
        book._hist_replay_key[SYMBOLS[-1]]=(1080,7)
        book._hist_replay_at[SYMBOLS[-1]]=100
        self.assertFalse(session.ready(pulse,SYMBOLS,2880,1000))

    def test_full_universe_warmup_survives_the_measured_cost_reload(self):
        sys.path.insert(0, str(session.ROOT / 'server/pulse'))
        from load_engine import LoadGovernor
        governor = LoadGovernor()
        pulse = SimpleNamespace(load=governor,apply_live_config=lambda:governor.configure({'loadPartial':True}))
        session.prepare_history_warmup(pulse)
        self.assertFalse(governor.partial)
        selected, _ = governor.history_window(SYMBOLS, 3, 0)
        self.assertEqual(selected,SYMBOLS)
        governor.configure({'loadPartial':True})
        selected, _ = governor.history_window(SYMBOLS, 3, 0)
        self.assertEqual(len(selected),3)

    def test_batch_ack_binds_exact_clients_before_first_rest_order_poll(self):
        orders = [{'symbol':'S0-USDT','clientOrderID':'G1234sl','orderId':'91'},
                  {'symbol':'S0-USDT','clientOrderID':'G1234tp','orderId':'92'}]
        def request(method, path, body):
            return {'code':0,'data':{'orders':orders}}
        with tempfile.TemporaryDirectory() as td:
            api = SimpleNamespace(_req=request,base=session.HOST)
            journal = session.Journal(api,td,'G1234')
            journal.phase,journal.deadline='active',float('inf')
            bodies=[{**o,'side':'BUY','positionSide':'LONG','type':'MARKET','quantity':'1'} for o in orders]
            for body in bodies:body.pop('orderId')
            api._req('POST','/openApi/swap/v2/trade/batchOrders',{'batchOrders':json.dumps(bodies)})
            self.assertTrue(journal.owns_order('S0-USDT','91'))
            self.assertTrue(journal.owns_order('S0-USDT','92'))
            self.assertFalse(journal.owns_order('S1-USDT','91'))
    def test_startup_fee_collection_publishes_only_measured_symbol_rates(self):
        sys.path.insert(0, str(session.ROOT / 'server/pulse'))
        from execution_cost import FeeSchedule
        calls = []
        def get(path, params):
            calls.append((path, params))
            return {'code':0,'data':{'makerCommissionRate':'0.0002','takerCommissionRate':'0.0005'}}
        pulse = SimpleNamespace(api=SimpleNamespace(get=get,commission_scope='account'), fee_schedule=FeeSchedule())
        progress = []
        session.collect_account_fees(pulse, SYMBOLS, lambda: False, float('inf'), lambda *row: progress.append(row))
        self.assertEqual(calls, [('/openApi/swap/v2/user/commissionRate', {})])
        self.assertEqual(set(pulse.fee_schedule.account), set(SYMBOLS))
        self.assertTrue(all(path == '/openApi/swap/v2/user/commissionRate' for path, _ in calls))
        self.assertEqual(progress[-1][:2], (50, 50))
        for symbol in SYMBOLS:
            self.assertEqual(pulse.fee_schedule.rates(symbol)[:2], (.0002, .0005))
        with self.assertRaises(session.SessionEnd):
            session.collect_account_fees(SimpleNamespace(api=pulse.api, fee_schedule=FeeSchedule()),
                                         SYMBOLS, lambda: True, float('inf'), lambda *row: None)

    def test_commission_scope_keeps_bybit_symbol_rates_separate_and_bingx_reads_once(self):
        from execution_cost import FeeSchedule
        from bingx_fast import FastBingX
        from bybit_fast import FastBybit
        self.assertEqual(FastBingX.commission_scope,'account')
        self.assertEqual(FastBybit.commission_scope,'symbol')
        calls=[]
        def get(path, params):
            calls.append(params)
            return {'code':0,'data':{'commission':{'makerCommissionRate':0,'takerCommissionRate':.0002 if params.get('symbol')=='A' else .0005}}}
        per_symbol=FeeSchedule();api=SimpleNamespace(get=get,commission_scope='symbol')
        per_symbol.refresh(api,['A','B'],10_000)
        per_symbol.refresh(api,['A','B'],10_002)
        self.assertEqual(calls,[{'symbol':'A'},{'symbol':'B'}])
        self.assertEqual(per_symbol.rates('A')[1],.0002)
        self.assertEqual(per_symbol.rates('B')[1],.0005)
        calls.clear();account=FeeSchedule();api.commission_scope='account'
        account.refresh(api,['A','B'],10_000)
        self.assertEqual(calls,[{}])
        self.assertEqual(account.rates('A'),account.rates('B'))
        self.assertEqual(account.rates('A')[0],0)

    def test_twelve_symbol_profile_has_independent_micro_defaults_and_preserves_custom_values(self):
        own=SYMBOLS[:12]
        base=session.frozen_overlay({},own,symbol_count=12)
        self.assertEqual((base['microMinSlDistancePct'],base['microMinTrailDistancePct']),(.1,.05))
        custom=session.frozen_overlay({'microMinSlDistancePct':.07,'microMinTrailDistancePct':.03},own,symbol_count=12)
        self.assertEqual((custom['microMinSlDistancePct'],custom['microMinTrailDistancePct']),(.07,.03))
        for bad in (-1,11,float('nan'),True,'0.1'):
            with self.assertRaises(ValueError):session.frozen_overlay({'microMinSlDistancePct':bad},own,symbol_count=12)

    def test_no_submission_cleanup_preserves_a_new_foreign_selected_position(self):
        calls=[]
        def request(method,path,params=None):
            calls.append((method,path))
            return {'code':0,'data':[{'symbol':'S0-USDT','positionSide':'LONG','positionAmt':'7'}]} if path.endswith('/positions') else {'code':0,'data':{'orders':[{'symbol':'S0-USDT','orderId':'99','clientOrderID':'foreign'}]}}
        with tempfile.TemporaryDirectory() as td:
            api=SimpleNamespace(base=session.HOST,_req=request)
            api.get=lambda path,params=None:api._req('GET',path,params)
            journal=session.Journal(api,td,'G1234',['S0-USDT'])
            result=session.cleanup(SimpleNamespace(api=api,open={}),journal,5)
            self.assertTrue(result['confirmedFlat'])
            self.assertFalse(result['selectedScopeFlat'])
            self.assertEqual(result['foreignPositionsPreserved'],1)
            self.assertEqual(result['foreignOrdersPreserved'],1)
            self.assertEqual(result['ordersSubmitted'],0)
            self.assertTrue(all(method=='GET' for method,path in calls))

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
        book.usable = lambda s: s != SYMBOLS[0]
        book._history_snapshot_symbols = set(SYMBOLS[1:])
        self.assertTrue(session.ready(pulse,SYMBOLS,2880,1000), 'native quality exclusion must not deadlock every healthy symbol')
        book.usable = lambda s: True
        book._history_snapshot_symbols = set(SYMBOLS)
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

    def owned_cleanup_fixture(self, root, closed_qty='1', unknown_exit=False):
        orders={};deletes=[]
        foreign={'symbol':'S0-USDT','orderId':'99','clientOrderID':'G1234foreign',
                 'side':'SELL','positionSide':'LONG','type':'STOP_MARKET','status':'NEW','executedQty':'0'}
        def request(method,path,body=None):
            body=body or{}
            if method=='POST':
                oid=str(len(orders)+1);control=body['type']!='MARKET'
                row={**body,'orderId':oid,'status':'NEW'if control else'FILLED',
                     'executedQty':'0'if control else body['quantity']}
                orders[oid]=row;return {'code':0,'data':{'order':dict(row)}}
            if method=='DELETE':
                deletes.append(dict(body));orders[body['orderId']]['status']='CANCELED'
                return {'code':0}
            if path.endswith('/positions'):
                return {'code':0,'data':[{'symbol':'S0-USDT','positionSide':'LONG','positionAmt':'7'}]}
            if path.endswith('/openOrders'):
                return {'code':0,'data':{'orders':[dict(r)for r in orders.values()if r['status']=='NEW']+[foreign]}}
            if path.endswith('/allOrders'):
                return {'code':0,'data':{'orders':[dict(r)for r in orders.values()
                    if not(unknown_exit and r['clientOrderID']=='G1234close')]}}
            if path.endswith('/order'):
                if unknown_exit and str(body.get('orderId'))=='3':return {'code':-1,'error':True}
                return {'code':0,'data':{'order':dict(orders[str(body['orderId'])])}}
            raise AssertionError((method,path))
        api=SimpleNamespace(base=session.HOST,_req=request)
        journal=session.Journal(api,root,'G1234');journal.phase='active';journal.deadline=session.time.monotonic()+10
        api.get=lambda path,body=None:api._req('GET',path,body)
        api.delete=lambda path,body:api._req('DELETE',path,body)
        entry=dict(symbol='S0-USDT',clientOrderID='G1234entry',side='BUY',positionSide='LONG',type='MARKET',quantity='1')
        api._req('POST','/openApi/swap/v2/trade/order',entry)
        api._req('POST','/openApi/swap/v2/trade/order',{**entry,'side':'SELL','type':'STOP_MARKET','clientOrderID':'G1234sl'})
        journal.phase='cleanup'
        api._req('POST','/openApi/swap/v2/trade/order',{**entry,'side':'SELL','clientOrderID':'G1234close','quantity':closed_qty})
        pulse=SimpleNamespace(api=api,open={},sync_own_fills=lambda:None,adopt_exchange_positions=lambda:None)
        return pulse,journal,orders,deletes

    def test_cleanup_after_exclusive_abort_preserves_same_symbol_foreign_inventory(self):
        with tempfile.TemporaryDirectory()as root:
            pulse,journal,orders,deletes=self.owned_cleanup_fixture(root)
            result=session.cleanup(pulse,journal,2)
        self.assertTrue(result['confirmedFlat']);self.assertEqual(result['scope'],'owned session')
        self.assertFalse(result['selectedScopeFlat'])
        self.assertEqual(result['foreignPositionsPreserved'],1);self.assertEqual(result['foreignOrdersPreserved'],1)
        self.assertEqual(deletes,[{'symbol':'S0-USDT','orderId':'2'}])
        self.assertEqual(journal.inventory[('S0-USDT','LONG')],0)

    def test_owned_cleanup_still_blocks_unknown_submissions_and_nonzero_fill_inventory(self):
        for quantity,unknown in [('1',True),('0.5',False)]:
            with self.subTest(quantity=quantity,unknown=unknown),tempfile.TemporaryDirectory()as root:
                pulse,journal,orders,deletes=self.owned_cleanup_fixture(root,quantity,unknown)
                result=session.cleanup(pulse,journal,.02)
                self.assertFalse(result['confirmedFlat']);self.assertEqual(deletes,[])

    def test_cleanup_reserves_archive_budget_for_exact_bound_receipts(self):
        import native_cleanup_evidence as evidence
        seen=[];bound=[]
        with tempfile.TemporaryDirectory()as root:
            pulse,journal,orders,deletes=self.owned_cleanup_fixture(root)
            original=evidence.terminal_intents;original_bound=evidence.resolve_bound_intents
            def archive(api,intents,deadline,*args):
                seen.append(deadline);return original(api,intents,deadline,*args)
            def resolve(api,intents,pending,deadline,*args):
                bound.append(deadline);return original_bound(api,intents,pending,deadline,*args)
            with patch.object(evidence,'terminal_intents',archive),patch.object(evidence,'resolve_bound_intents',resolve):
                result=session.cleanup(pulse,journal,30)
        self.assertTrue(result['confirmedFlat']);self.assertGreaterEqual(len(seen),2)
        self.assertEqual(len(seen),len(bound))
        self.assertTrue(all(archive+10<exact for archive,exact in zip(seen,bound)))

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
            for body in (
                {'symbol':'S0-USDT','clientOrderID':'G1234close','side':'SELL','positionSide':'LONG','type':'STOP_MARKET','closePosition':'true'},
                {'symbol':'S0-USDT','clientOrderID':'G1234close2','side':'SELL','positionSide':'LONG','type':'MARKET','positionId':'55'},
            ):
                rejected=api._req('POST','/openApi/swap/v2/trade/order',body)
                self.assertEqual(rejected['code'],101400)
                self.assertTrue(rejected['localPolicyRejection'])
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
        self.assertTrue(session.shared_account_protection_verified())

    def test_unbounded_native_protection_blocks_execution_before_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); symbols=root/'symbols.json'; symbols.write_text(json.dumps(SYMBOLS))
            out=root/'session'
            with contextlib.redirect_stdout(io.StringIO()), patch.object(session, 'shared_account_protection_verified', return_value=False):
                result=session.main(['--overlay',str(session.ROOT/'server/pulse/overlay-bingx-x02.json'),
                                     '--symbols',str(symbols),'--out',str(out),'--execute'])
            report=json.loads((out/'session.json').read_text())
            self.assertEqual(result,1)
            self.assertFalse(report['executed'])
            self.assertFalse(report['complete'])
            self.assertIn('aggregate-closing',report['stopReason'])
            self.assertFalse((out/'mutation-journal.jsonl').exists())

    def test_another_session_cannot_join_between_empty_preflight_and_start(self):
        def get(path):
            return {'code':0,'data':[{'symbol':'S0-USDT','positionSide':'LONG','positionAmt':'1'}]}
        with self.assertRaises(session.SessionEnd): session.require_empty_account(SimpleNamespace(get=get))
        def orders(path):
            return {'code':0,'data':[] if path.endswith('positions') else {'orders':[{'clientOrderID':'foreign'}]}}
        with self.assertRaises(session.SessionEnd): session.require_empty_account(SimpleNamespace(get=orders))

    def test_shared_account_transport_never_submits_outside_selected_symbols(self):
        calls=[]
        with tempfile.TemporaryDirectory() as td:
            api=SimpleNamespace(_req=lambda *args:calls.append(args),base=session.HOST)
            journal=session.Journal(api,td,'G1234',{'S0-USDT'})
            journal.phase,journal.deadline='active',float('inf')
            with self.assertRaisesRegex(RuntimeError,'selected universe'):
                api._req('POST','/openApi/swap/v2/trade/order',dict(symbol='OTHER-USDT',clientOrderID='G1234entry',side='BUY',positionSide='LONG',type='MARKET',quantity='1'))
            self.assertEqual(calls,[])
    def test_selected_symbol_preflight_ignores_other_symbols_and_blocks_overlap(self):
        def get(path):
            rows=[{'symbol':'OTHER-USDT','positionSide':'LONG','positionAmt':'3'}]
            return {'code':0,'data':rows if path.endswith('positions') else {'orders':[{'symbol':'OTHER-USDT','orderId':'9'}]}}
        api=SimpleNamespace(get=get)
        result=session.require_empty_account(api,{'S0-USDT'})
        self.assertEqual(result['ignoredForeignPositions'],1)
        self.assertEqual(result['ignoredForeignOrders'],1)
        with self.assertRaises(session.SessionEnd):session.require_empty_account(api,{'OTHER-USDT'})
        with self.assertRaises(session.SessionEnd):session.require_empty_account(api)
    def test_owned_cleanup_leaves_other_symbol_positions_and_orders_untouched(self):
        deletes=[]
        def get(path):
            return {'code':0,'data':[{'symbol':'OTHER-USDT','positionSide':'LONG','positionAmt':'3'}] if path.endswith('positions') else {'orders':[{'symbol':'OTHER-USDT','orderId':'9'}]}}
        api=SimpleNamespace(get=get,delete=lambda *args:deletes.append(args))
        pulse=SimpleNamespace(api=api,open={},sync_own_fills=lambda:None,adopt_exchange_positions=lambda:None)
        journal=SimpleNamespace(phase='active',symbols={'S0-USDT'},inventory={},intents=[],lock=__import__('threading').RLock(),owns_order=lambda *args:False)
        result=session.cleanup(pulse,journal,1)
        self.assertTrue(result['confirmedFlat'])
        self.assertEqual(result['scope'],'selected symbols')
        self.assertEqual(result['ignoredForeignPositions'],1)
        self.assertEqual(deletes,[])
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
    def test_explicit_twelve_symbol_profile_has_exact_coverage_without_changing_gates(self):
        raw={"mainGateN":15,"realGateN":8,"microMinSlDistancePct":.07,"microMinTrailDistancePct":.05}
        out=session.frozen_overlay(raw,SYMBOLS[:12],18,12)
        self.assertEqual((out["symbolCap"],out["maxOpen"]),(12,12))
        for key,value in raw.items():self.assertEqual(out[key],value)
        with self.assertRaises(ValueError):session.frozen_overlay(raw,SYMBOLS[:11],18,12)

    def test_integrated_peer_readiness_requires_each_symbol_fee_and_current_history(self):
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

    def test_integrated_peer_mutations_require_exact_journaled_order_ids_and_never_change_account_settings(self):
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

    def test_integrated_peer_test_session_never_changes_leverage_and_uses_conservative_margin(self):
        pulse=SimpleNamespace()
        session.freeze_account_settings(pulse)
        self.assertIsNone(pulse.set_leverage())
        self.assertEqual(pulse.ensure_max_leverage('S0-USDT',force=True),1)
        self.assertEqual(pulse.leverage_for(object()),1)

    def test_integrated_peer_batch_ack_binds_exact_clients_before_first_rest_order_poll(self):
        orders = [{'symbol':'S0-USDT','clientOrderID':'G1234sl','orderId':'91'},
                  {'symbol':'S0-USDT','clientOrderID':'G1234tp','orderId':'92'}]
        def request(method, path, body):
            return {'code':0,'data':{'orders':orders}}
        with tempfile.TemporaryDirectory() as td:
            api = SimpleNamespace(_req=request,base=session.HOST)
            journal = session.Journal(api,td,'G1234')
            journal.phase,journal.deadline='active',float('inf')
            entry={'symbol':'S0-USDT','clientOrderID':'G1234entry','side':'BUY','positionSide':'LONG','type':'MARKET','quantity':'1'}
            journal.owned_clients.add(('S0-USDT','g1234entry'))
            journal.submitted[('S0-USDT','g1234entry')]=entry
            journal.remember_order({**entry,'orderId':'90','executedQty':'1'})
            bodies=[{**o,'side':'SELL','positionSide':'LONG','type':'STOP_MARKET','quantity':'1'} for o in orders]
            for body in bodies:body.pop('orderId')
            api._req('POST','/openApi/swap/v2/trade/batchOrders',{'batchOrders':json.dumps(bodies)})
            self.assertTrue(journal.owns_order('S0-USDT','91'))
            self.assertTrue(journal.owns_order('S0-USDT','92'))
            self.assertFalse(journal.owns_order('S1-USDT','91'))

    def test_integrated_peer_startup_fee_collection_publishes_only_measured_symbol_rates(self):
        sys.path.insert(0, str(session.ROOT / 'server/pulse'))
        from execution_cost import FeeSchedule
        calls = []
        def get(path, params):
            calls.append((path, params['symbol']))
            return {'code':0,'data':{'makerCommissionRate':'0.0002','takerCommissionRate':'0.0005'}}
        pulse = SimpleNamespace(api=SimpleNamespace(get=get), fee_schedule=FeeSchedule())
        pulse.fee_schedule.account['OTHER-USDT']={'maker':.0002,'taker':.0005,'t':session.time.time()}
        progress = []
        clock = [session.time.time()]
        with patch.object(session.time, 'time', side_effect=lambda: clock[0]), patch.object(session.time, 'sleep', side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)):
            session.collect_account_fees(pulse, SYMBOLS, lambda: False, float('inf'), lambda *row: progress.append(row))
        self.assertEqual(sorted(s for _, s in calls), sorted(SYMBOLS))
        self.assertEqual(set(pulse.fee_schedule.account), set(SYMBOLS)|{'OTHER-USDT'})
        self.assertTrue(all(path == '/openApi/swap/v2/user/commissionRate' for path, _ in calls))
        self.assertEqual(progress[-1][:2], (50, 50))
        self.assertTrue(all(measured<=required for measured,required,_error in progress))
        for symbol in SYMBOLS:
            self.assertEqual(pulse.fee_schedule.rates(symbol)[:2], (.0002, .0005))
        with self.assertRaises(session.SessionEnd):
            session.collect_account_fees(SimpleNamespace(api=pulse.api, fee_schedule=FeeSchedule()),
                                         SYMBOLS, lambda: True, float('inf'), lambda *row: None)

    def test_bingx_account_fee_scope_uses_one_measured_response_for_fifty_symbols(self):
        sys.path.insert(0,str(session.ROOT/'server/pulse'))
        from execution_cost import FeeSchedule
        calls=[];progress=[]
        def get(path,params):
            calls.append((path,params))
            return {'code':0,'data':{'commission':{'makerCommissionRate':'.0002','takerCommissionRate':'.0005'}}}
        pulse=SimpleNamespace(api=SimpleNamespace(get=get,commission_scope='account'),fee_schedule=FeeSchedule())
        session.collect_account_fees(pulse,SYMBOLS,lambda:False,float('inf'),lambda *row:progress.append(row))
        self.assertEqual(calls,[('/openApi/swap/v2/user/commissionRate',{})])
        self.assertEqual(progress[-1][:2],(50,50))
        self.assertEqual({r['scope']for r in pulse.fee_schedule.account.values()},{'account'})
        self.assertEqual(len({r['t']for r in pulse.fee_schedule.account.values()}),1)
        session.collect_account_fees(pulse,SYMBOLS,lambda:False,float('inf'),lambda *row:progress.append(row))
        self.assertEqual(len(calls),1)

    def test_invalid_account_fee_response_cannot_populate_any_symbol(self):
        sys.path.insert(0,str(session.ROOT/'server/pulse'))
        from execution_cost import FeeSchedule
        schedule=FeeSchedule()
        api=SimpleNamespace(commission_scope='account',get=lambda *args:{'code':0,'data':{'commission':{
            'makerCommissionRate':'.0002','takerCommissionRate':'NaN'}}})
        self.assertFalse(schedule.refresh(api,SYMBOLS,10000))
        self.assertEqual(schedule.account,{})

    def test_integrated_peer_native_cycle_cannot_hide_a_stall_when_watcher_thread_does_not_run(self):
        clock = [0.0]
        def get(path, *args):
            if path.endswith('/balance'): return {'code':0,'data':[{'asset':'VST'}]}
            if path.endswith('/positions'): return {'code':0,'data':[]}
            if path.endswith('/openOrders'): return {'code':0,'data':{'orders':[]}}
            raise AssertionError(path)
        api = SimpleNamespace(base=session.HOST,get=get,_req=lambda *a:None)
        class Pulse:
            def __init__(self, api, contracts):
                self.load=SimpleNamespace(partial=True,configure=lambda ov:None)
                self.sets=SimpleNamespace()
                self.fee_schedule=SimpleNamespace(account={},snapshot=lambda *a:{})
                self.position_cost_pct=.11;self.slippage_allowance_pct=.01
                self.active_fee_contracts=lambda:{}
                self.ignored_foreign=False
                self.pool=SimpleNamespace(shutdown=lambda **kw:None)
            def _one_cycle(self): clock[0] += 20
            def run(self): self._one_cycle()
        with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(session.os.environ,{}))
            prior_umask=session.os.umask(0o077)
            stack.callback(session.os.umask,prior_umask)
            root=Path(td)
            credentials=root/'credentials.json'
            credentials.write_text('{"api_key":"fixture","api_secret":"fixture","is_testnet":true}')
            credentials.chmod(0o600)
            overlay=root/'overlay.json';overlay.write_text('{}')
            symbols=root/'symbols.json';symbols.write_text(json.dumps(SYMBOLS))
            native=SimpleNamespace(DIR=str(root/'native'),PAUSE_PATH=str(root/'native/PAUSE'),
                STOP_PATH=str(root/'native/STOP'),CONN_SHORT=session.CONN,IS_BYBIT=False,BASE=session.HOST,
                make_api=lambda *a:api,load_contracts=lambda ss:{s:None for s in ss},Pulse=Pulse)
            stack.enter_context(patch.dict(sys.modules,{'pulse_trader':native}))
            for name,replacement in {
                'context_requirement':lambda ov:60,'shared_account_protection_verified':lambda:True,
                'freeze_account_settings':lambda p:None,'install_history_cancellation':lambda p:None,
                'collect_account_fees':lambda *a:None,'prepare_history_warmup':lambda *a,**kw:None,
                'native_admission_ready':lambda p:True,'capture':lambda *a,**kw:None,
                'cleanup':lambda *a:{'confirmedFlat':True},
            }.items():stack.enter_context(patch.object(session,name,replacement))
            stack.enter_context(patch.object(session.time,'monotonic',lambda:clock[0]))
            stack.enter_context(patch.object(session.time,'time',lambda:1_700_000_000+clock[0]))
            stack.enter_context(patch.object(session.signal,'signal'))
            stack.enter_context(patch.object(session.threading,'Thread',return_value=SimpleNamespace(start=lambda:None)))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            result=session.main(['--overlay',str(overlay),'--symbols',str(symbols),'--out',str(root/'run'),
                '--credentials',str(credentials),'--execute','--minutes','30','--heartbeat-seconds','15','--replay-workers','1'])
            report=json.loads((root/'run/session.json').read_text())
            self.assertEqual(result,1)
            self.assertEqual(report['stopReason'],'main cycle heartbeat deadline',report.get('errorFrames'))
            self.assertFalse(report['complete'])
            self.assertTrue(report['cleanup']['confirmedFlat'])




if __name__ == '__main__': unittest.main()
