"""No-network tests for the bounded native demo wrapper (never starts Pulse)."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import x02_native_session as session


SYMBOLS = [f'S{i}-USDT' for i in range(50)]


class NativeSessionTests(unittest.TestCase):
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
        journal=SimpleNamespace(tag='G1234',intents=[],lock=__import__('threading').RLock())
        result=session.cleanup(pulse,journal,1)
        self.assertTrue(result['confirmedFlat'])
        self.assertGreaterEqual(calls.count('/openApi/swap/v2/user/positions'),2)

    def test_another_session_cannot_join_between_empty_preflight_and_start(self):
        def get(path):
            return {'code':0,'data':[{'symbol':'S0-USDT','positionSide':'LONG','positionAmt':'1'}]}
        with self.assertRaises(session.SessionEnd): session.require_empty_account(SimpleNamespace(get=get))
        def orders(path):
            return {'code':0,'data':[] if path.endswith('positions') else {'orders':[{'clientOrderID':'foreign'}]}}
        with self.assertRaises(session.SessionEnd): session.require_empty_account(SimpleNamespace(get=orders))


if __name__ == '__main__': unittest.main()
