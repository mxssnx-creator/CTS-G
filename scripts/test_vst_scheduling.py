"""Offline regressions from observed VST bans and long entry cycles."""
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server" / "pulse"))
import bingx_fast
import pulse_trader as trader
from vst_readonly_probe import validate_base, snapshot

ORDER = "/openApi/swap/v2/trade/order"


class VstSchedulingTests(unittest.TestCase):
    def test_probe_refuses_non_vst_and_credential_bearing_urls(self):
        self.assertEqual(validate_base("https://open-api-vst.bingx.com/"), "https://open-api-vst.bingx.com")
        for base in ["https://open-api.bingx.com", "http://open-api-vst.bingx.com",
                     "https://name@open-api-vst.bingx.com", "https://open-api-vst.bingx.com/?key=test",
                     "https://open-api-vst.bingx.com/order", "https://open-api-vst.bingx.com:444"]:
            with self.assertRaises(ValueError):
                validate_base(base)

    def test_probe_uses_one_get_and_does_not_infer_ownership(self):
        calls = []
        def get(path):
            calls.append(path)
            return {"code": 0, "data": [{"symbol": "XRP-USDT", "positionAmt": "1"},
                                        {"symbol": "SOL-USDT", "positionAmt": "0"}]}
        result = snapshot(SimpleNamespace(get=get))
        self.assertEqual(calls, ["/openApi/swap/v2/user/positions"])
        self.assertEqual(result["exchangeTotalOpenCount"], 1)
        self.assertEqual(result["ownership"], "not inferred")
        self.assertFalse(snapshot(SimpleNamespace(get=lambda path: {"code": 100410, "data": []}))["ok"])

    def api(self):
        a = bingx_fast.FastBingX.__new__(bingx_fast.FastBingX)
        a.path_cd, a.cooldown_until = {}, 0
        a.stats = {"rl": 0, "wait": 0}
        a.err = SimpleNamespace(write=lambda *args, **kw: None)
        return a

    def test_both_observed_deadline_formats_and_units_are_respected(self):
        now = 1_788_598_800
        for wording in ["unblocked after ", "UNBLOCKED AFTER: ", "retry after time: "]:
            for scale in [1, 1000]:
                for delay in [80, 480, 1800]:
                    a = self.api()
                    with self.subTest(wording=wording, scale=scale, delay=delay), patch.object(bingx_fast.time, "time", return_value=now):
                        a._trip(ORDER, {"code": 109429, "msg": wording + str((now + delay) * scale)})
                        self.assertAlmostEqual(a.order_retry_after(), delay + .4, delta=1e-6)
                        self.assertGreaterEqual(a.order_retry_after(), delay)
                        self.assertEqual(a.stats["rl"], 1)

    def test_shorter_or_unspecified_deadline_cannot_shorten_existing_ban(self):
        a = self.api()
        with patch.object(bingx_fast.time, "time", return_value=1_788_598_800):
            a.path_cd[ORDER] = 1_788_599_280
            a._trip(ORDER, {"code": 100410, "msg": "rate limited"})
            self.assertEqual(a.order_retry_after(), 480)

    def test_generic_rate_limit_uses_eight_second_backoff(self):
        a = self.api()
        with patch.object(bingx_fast.time, "time", return_value=1_788_598_800):
            a._trip(ORDER, {"code": 100410, "msg": "rate limited"})
            self.assertEqual(a.order_retry_after(), 8)

    def test_batch_deadline_blocks_single_orders_for_full_duration(self):
        a = self.api()
        now = 1_788_598_800
        batch = '/openApi/swap/v2/trade/batchOrders'
        a.buckets = {'order': SimpleNamespace(take=lambda: self.fail('must not spend a token'))}
        with patch.object(bingx_fast.time, 'time', return_value=now):
            a._trip(batch, {'code':'109429', 'msg':f'retry after time: {(now+480)*1000}'})
        with patch.object(bingx_fast.time, 'time', return_value=now+100):
            self.assertFalse(a._take('order', ORDER))
            self.assertFalse(a._take('order', batch))
            self.assertAlmostEqual(a.order_retry_after(), 380.4, delta=1e-6)

    def test_http_retry_after_seconds_and_date(self):
        from email.utils import formatdate
        now = 1_788_598_800
        for header in ('120', formatdate(now+120, usegmt=True)):
            a = self.api()
            with patch.object(bingx_fast.time, 'time', return_value=now):
                a._trip(ORDER, {'code':429, 'retryAfter':header})
                self.assertAlmostEqual(a.order_retry_after(), 120.4, delta=1e-6)

    def test_http_429_keeps_retry_after_even_without_json(self):
        import io
        from urllib.error import HTTPError
        a = self.api(); a.key = 'offline'; a.base = 'https://example.invalid'
        error = HTTPError(a.base, 429, 'Too Many Requests', {'Retry-After':'120'}, io.BytesIO(b'busy'))
        def fail(*args, **kwargs): raise error
        a._opener = SimpleNamespace(open=fail)
        result = a._http('POST', ORDER)
        self.assertEqual(result['code'], 429)
        self.assertEqual(result['retryAfter'], '120')

    def test_success_envelope_with_throttled_batch_item_blocks_followup_orders(self):
        a = self.api(); a.stats['rest'] = 0
        a._take = lambda *args: True
        a._next_ts = lambda: 1
        a._sign = lambda params: 'offline'
        a._http = lambda *args: {'code':0, 'data':{'orders':[
            {'code':0, 'orderId':'accepted'}, {'code':109429, 'msg':'rate limit'}]}}
        with patch.object(bingx_fast.time, 'time', return_value=100):
            result = a.post('/openApi/swap/v2/trade/batchOrders')
            self.assertEqual(result['data']['orders'][0]['orderId'], 'accepted')
            self.assertEqual(a.order_retry_after(), 8)

    def test_restart_restores_active_venue_deadline_without_network(self):
        import tempfile, pathlib, json
        a = self.api()
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder)/'errors-x02.jsonl'
            path.write_text(json.dumps(dict(kind='rate-limit', t=1000, wait=480, path='/openApi/swap/v2/trade/batchOrders'))+'\n')
            a.err.path = str(path)
            with patch.object(bingx_fast.time, 'time', return_value=1200):
                a._restore_retry_deadlines()
                self.assertGreaterEqual(a.order_retry_after(),280)
            b = self.api(); b.err.path = str(pathlib.Path(folder)/'errors-x01.jsonl')
            b._restore_retry_deadlines()
            self.assertEqual(b.cooldown_until,0)

    def test_batch_quantity_is_json_number_without_mutating_retry_intent(self):
        a = self.api(); bodies = []
        a.post = lambda path, body: bodies.append(bingx_fast.loads(body['batchOrders'])) or {'code':0}
        orders = [{'clientOrderID':'same-id', 'quantity':'0.010', 'stopPrice':'100.25', 'type':'STOP_MARKET'}]
        a.batch_place(orders)
        self.assertIsInstance(bodies[0][0]['quantity'], float)
        self.assertEqual(bodies[0][0]['quantity'], .01)
        self.assertEqual(bodies[0][0]['clientOrderID'], 'same-id')
        self.assertEqual(orders[0]['quantity'], '0.010')
        for bad in ('NaN','Infinity','-1','0'):
            with self.assertRaises(ValueError): a.batch_place([{'quantity':bad}])
        self.assertEqual(len(bodies),1)

    def test_large_batch_submits_all_inputs_in_chunks_of_five(self):
        a = self.api(); calls = []
        orders = [{'clientOrderID':str(i)} for i in range(12)]
        def post(path, body):
            rows = bingx_fast.loads(body['batchOrders']); calls.append(rows)
            return {'code':0, 'data':{'orders':[dict(r, code=0, orderId=r['clientOrderID']) for r in rows]}}
        a.post = post
        result = a.batch_place(orders)
        self.assertEqual([len(c) for c in calls], [5,5,2])
        self.assertEqual([r['clientOrderID'] for r in result['data']['orders']], [str(i) for i in range(12)])
        self.assertTrue(result['complete'])

    def test_async_public_admission_is_per_request_and_keeps_http_deadline(self):
        import asyncio
        from collections import deque
        bridge = bingx_fast.AsyncBridge.__new__(bingx_fast.AsyncBridge)
        bridge.lat = deque(); admitted = []; sent = []; responses = []
        def admit(path):
            admitted.append(path)
            return path != '/cooled'
        async def get(url):
            self.assertIn(url, admitted)
            sent.append(url)
            return SimpleNamespace(status_code=429, content=b'busy', headers={'Retry-After':'90'})
        bridge.before_request = admit
        bridge.on_response = lambda path, body: responses.append((path, body))
        bridge.client = SimpleNamespace(get=get)
        rows = asyncio.run(bridge._gather([('/one',{}),('/cooled',{}),('/two',{})]))
        self.assertEqual(sorted(sent), ['/one','/two'])
        self.assertTrue(rows[1][2]['cooled'])
        self.assertTrue(all(body['code'] == 429 and body['retryAfter'] == '90' for _,body in responses))

    def test_batch_cooldown_retains_pending_without_resubmitting_accepted(self):
        a = self.api(); calls = []
        orders = [{'clientOrderID':str(i)} for i in range(12)]
        def post(path, body):
            rows = bingx_fast.loads(body['batchOrders']); calls.append(rows)
            if len(calls) == 2: return {'code':101209, 'msg':'cooling', 'cooled':True}
            return {'code':0, 'data':{'orders':[dict(r, code=0, orderId=r['clientOrderID']) for r in rows]}}
        a.post = post
        result = a.batch_place(orders)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(result['data']['orders']), 12)
        self.assertEqual(result['pendingIndexes'], list(range(5,12)))
        self.assertFalse(result['complete'])
        self.assertTrue(all(r['code'] == 0 for r in result['data']['orders'][:5]))
        self.assertTrue(all(not r['submitted'] for r in result['data']['orders'][5:]))

    def test_rate_limit_rechecked_after_token_wait(self):
        a = self.api()
        def token_wait():
            a.path_cd[ORDER] = 200
            return .1
        a.buckets = {"order": SimpleNamespace(take=token_wait)}
        with patch.object(bingx_fast.time, "time", return_value=100):
            self.assertFalse(a._take("order", ORDER))
            self.assertEqual(a.stats["wait"], .1)

    def test_order_ban_does_not_block_private_position_snapshots(self):
        a = self.api()
        a.path_cd[ORDER], a.cooldown_until = 200, 150
        a.buckets = {"private": SimpleNamespace(take=lambda: 0)}
        with patch.object(bingx_fast.time, "time", return_value=100):
            self.assertTrue(a._take("private", "/openApi/swap/v2/user/positions"))

    def test_order_cooling_skips_entries_without_evaluating_or_posting(self):
        p = trader.Pulse.__new__(trader.Pulse)
        p.halted, p.boot_ts, p.ctrl_skip = False, 0, {}
        p.api = SimpleNamespace(order_retry_after=lambda: 400)
        p.strategy_closes = lambda: self.fail("blocked transport must not prepare entries")
        p.maybe_entries()
        p.api.order_retry_after = lambda: 0
        self.assertFalse(p.entries_blocked())

    def test_expired_slices_preserve_full_eventual_candidate_coverage(self):
        p = trader.Pulse.__new__(trader.Pulse)
        rows = list(range(100))
        clock = [0.0]
        seen = []
        with patch.object(trader.time, "monotonic", side_effect=lambda: clock[0]):
            for _ in rows:
                for row in p.entry_candidate_window(rows):
                    seen.append(row)
                    clock[0] += 10  # Simulated slow REST call, no real sleep.
        self.assertEqual(seen, rows)
        self.assertEqual(p._entry_cursor, 0)

    def test_consumer_break_resumes_at_next_candidate(self):
        p = trader.Pulse.__new__(trader.Pulse)
        rows = list(range(6))
        self.assertEqual(next(p.entry_candidate_window(rows)), 0)
        self.assertEqual(next(p.entry_candidate_window(rows)), 1)
        self.assertEqual(list(p.entry_candidate_window([])), [])
        self.assertEqual(list(p.entry_candidate_window(rows)), [2, 3, 4, 5, 0, 1])

    def test_only_pending_empty_startup_snapshot_gets_second_read(self):
        for pending, streak, detail, expected in [
            (True, 1, "pending empty exchange read 1/2", 2),
            (False, 0, "confirmed", 1),
            (False, 1, "adopt failed", 1),
            (True, 0, "pending other", 1),
        ]:
            p = trader.Pulse.__new__(trader.Pulse)
            calls = []
            def adopt():
                calls.append("snapshot")
                p.recon_pending, p._empty_rest_streak, p.recon_detail = pending, streak, detail
            p.adopt_exchange_positions = adopt
            p.reconcile_startup_positions()
            self.assertEqual(len(calls), expected)


if __name__ == "__main__":
    unittest.main()
