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


SYMBOLS = [f'S{i}-USDT' for i in range(50)]


class NativeSessionTests(unittest.TestCase):
    def test_watchdog_does_not_count_a_stale_cycle_as_completed_observation(self):
        self.assertEqual(session.end_reason('active',200,50,150,100,60),'main cycle heartbeat deadline')
        self.assertEqual(session.end_reason('active',200,50,150,199,60),'observation complete')
        self.assertEqual(session.end_reason('warming',200,150,None,0,60),'readiness deadline')
        self.assertIsNone(session.end_reason('warming',100,150,None,0,60))
        self.assertIsNone(session.end_reason('cleanup',200,50,150,0,60,True))

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
            bodies=[{**o,'side':'SELL','positionSide':'LONG','type':'STOP_MARKET','quantity':'1'} for o in orders]
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
            calls.append((path, params['symbol']))
            return {'code':0,'data':{'makerCommissionRate':'0.0002','takerCommissionRate':'0.0005'}}
        pulse = SimpleNamespace(api=SimpleNamespace(get=get), fee_schedule=FeeSchedule())
        progress = []
        session.collect_account_fees(pulse, SYMBOLS, lambda: False, float('inf'), lambda *row: progress.append(row))
        self.assertEqual(sorted(s for _, s in calls), sorted(SYMBOLS))
        self.assertEqual(set(pulse.fee_schedule.account), set(SYMBOLS))
        self.assertTrue(all(path == '/openApi/swap/v2/user/commissionRate' for path, _ in calls))
        self.assertEqual(progress[-1][:2], (50, 50))
        for symbol in SYMBOLS:
            self.assertEqual(pulse.fee_schedule.rates(symbol)[:2], (.0002, .0005))
        with self.assertRaises(session.SessionEnd):
            session.collect_account_fees(SimpleNamespace(api=pulse.api, fee_schedule=FeeSchedule()),
                                         SYMBOLS, lambda: True, float('inf'), lambda *row: None)

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
                ('POST','/openApi/swap/v2/trade/order',{'symbol':'S0-USDT','clientOrderID':'G1234close','side':'SELL','positionSide':'LONG','type':'STOP_MARKET','closePosition':'true'}),
                ('POST','/openApi/swap/v2/trade/order',{'symbol':'S0-USDT','clientOrderID':'G1234close2','side':'SELL','positionSide':'LONG','type':'MARKET','positionId':'55'}),
            ):
                with self.assertRaises(RuntimeError): api._req(method,path,body)
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


if __name__ == '__main__': unittest.main()
