#!/usr/bin/env python3
"""Entries go out five per batchOrders call and stay exactly as safe as single orders.

The fake venue fills batched MARKET entries like the single endpoint, can lose the
batch response (accepted or unknown) and can reject single rows. Every run keeps its
own client id, pending intent and reserved margin, and never re-sends an order that
may already exist.
"""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server/pulse'))
import pulse_trader as pt
import test_all_valid_entries as base

X = 'X-USDT'


class BatchExchange(base.Exchange):
    def __init__(self):
        super().__init__()
        self.entry_batches = []   # sizes of batched MARKET entry calls
        self.filled = {}          # clientOrderID -> venue order
        self.reject = {}          # clientOrderID -> (code, msg) for a batched row
        self.lost_batch = None    # None | 'accepted' | 'unknown'
        self.refuse_batches = None  # (code, msg): the venue refuses the batch form as a whole
        self.lookups = []

    def _fill(self, body):
        self.n += 1
        row = dict(body, orderId=str(self.n), origQty=body.get('quantity'), status='FILLED',
                   avgPrice='100', executedQty=body.get('quantity'))
        self.filled[body.get('clientOrderID')] = row
        return row

    def post(self, path, body):
        if body.get('type') == 'MARKET':
            self.posts.append(dict(body))
            return {'code': 0, 'data': {'order': self._fill(body)}}
        return super().post(path, body)

    def batch_place(self, bodies):
        if not (bodies and all(b.get('type') == 'MARKET' for b in bodies)):
            return super().batch_place(bodies)
        self.entry_batches.append(len(bodies))
        if self.refuse_batches:
            return {'code': self.refuse_batches[0], 'msg': self.refuse_batches[1]}
        rows = []
        for body in bodies:
            code_msg = self.reject.get(body.get('clientOrderID'))
            if code_msg:
                rows.append({'code': code_msg[0], 'msg': code_msg[1]})
            else:
                rows.append(dict(self._fill(body), code=0))
        if self.lost_batch:
            return {'code': -1, 'error': True, 'msg': 'timeout'}
        return {'code': 0, 'data': {'orders': rows}, 'complete': True, 'pendingIndexes': []}

    def get(self, path, params=None):
        if path.endswith('/trade/order') and params and 'clientOrderId' in params:
            cid = params['clientOrderId']
            self.lookups.append(cid)
            if self.lost_batch == 'unknown':
                return {'code': -1, 'error': True, 'msg': 'timeout'}
            if cid in self.filled:
                return {'code': 0, 'data': {'order': dict(self.filled[cid], clientOrderID=cid)}}
            return {'code': 109400, 'msg': 'order not exist'}
        return super().get(path, params)


class BatchEntries(unittest.TestCase):
    def setUp(self):
        self.h = base.AllValidEntries('setUp')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def desk(self, lots, batch=True):
        book = self.h.book(lots)
        book.prefer_minimal_range = False
        book.strict_gate = False
        p = self.h.pulse(book)
        p.api = BatchExchange()
        p.entry_batch_orders = batch
        p.entry_batch_size = 5
        return p

    def rows(self, p, count=None):
        return [(X, 1, 'gen:trend', .9, state) for state in p.sets.by_idx[:count]]

    def entries(self, p):
        return [b for b in p.api.posts if b.get('type') == 'MARKET']

    def test_twelve_lots_go_out_as_batches_of_five_five_two(self):
        p = self.desk(12)
        opened = p.place_batch(self.rows(p))
        self.assertEqual(opened, 12, p.last_error)
        self.assertEqual(p.api.entry_batches, [5, 5, 2])
        self.assertEqual(self.entries(p), [], 'no entry used the single-order endpoint')
        self.assertEqual(len(p.open), 12)
        self.assertEqual(len({pos.set_id for pos in p.open.values()}), 12)
        self.assertEqual(len({pos.client_id for pos in p.open.values()}), 12)
        self.assertEqual(p.pending_orders, {}, 'every fill cleared its pending intent')
        for pos in p.open.values():
            self.assertTrue(pos.sl_oid and pos.tp_oid, 'each lot still gets its own protection')

    def test_maybe_entries_collects_candidates_into_batches(self):
        p = self.desk(10)
        with patch.object(pt, 'SYMBOLS', [X]):
            for _ in range(6):
                p.maybe_entries()
                if len(p.open) == 10:
                    break
        self.assertEqual(p.errors, 0, p.last_error)
        self.assertEqual(len(p.open), 10)
        self.assertTrue(p.api.entry_batches and max(p.api.entry_batches) == 5)
        self.assertEqual(self.entries(p), [])

    def test_batching_is_off_unless_the_overlay_turns_it_on(self):
        p = self.desk(6, batch=False)
        with patch.object(pt, 'SYMBOLS', [X]):
            for _ in range(6):
                p.maybe_entries()
                if len(p.open) == 6:
                    break
        self.assertEqual(len(p.open), 6)
        self.assertEqual(p.api.entry_batches, [])
        self.assertEqual(len(self.entries(p)), 6)

    def reject_row(self, p, index, code, msg):
        real_submit = p._submit_entry_chunk
        seen = {}

        def submit(bodies):
            seen['cid'] = bodies[index]['clientOrderID']
            p.api.reject[seen['cid']] = (code, msg)
            return real_submit(bodies)

        p._submit_entry_chunk = submit
        return seen

    def test_one_rejected_row_does_not_touch_the_other_four(self):
        p = self.desk(5)
        seen = self.reject_row(p, 2, 100400, 'Order rejected by risk control')
        opened = p.place_batch(self.rows(p))
        self.assertEqual(opened, 4)
        self.assertEqual(len(p.open), 4)
        self.assertEqual(p.errors, 1)
        self.assertNotIn(seen['cid'], p.pending_orders, 'the rejected intent was released')
        self.assertEqual(self.entries(p), [], 'a hard rejection is not retried')

    def test_a_margin_rejected_row_is_retried_alone_and_smaller_like_a_single_order(self):
        p = self.desk(5)
        p.api.fill_fraction = 1
        self.reject_row(p, 1, 100202, 'Insufficient margin')
        opened = p.place_batch(self.rows(p))
        self.assertEqual(opened, 5, p.last_error)
        retry = self.entries(p)
        self.assertEqual(len(retry), 1, 'only the rejected row went out again, as a single order')
        self.assertLess(float(retry[0]['quantity']), .05, 'the retry is smaller, never larger')
        self.assertEqual(p.api.entry_batches, [5])

    def test_a_refused_batch_falls_back_to_single_orders_and_batching_switches_itself_off(self):
        p = self.desk(15)
        p.api.refuse_batches = (100400, 'batch form not supported')
        rows = self.rows(p)
        self.assertEqual(p.place_batch(rows[0:5]), 5, p.last_error)
        self.assertEqual(len(self.entries(p)), 5, 'every order was sent on its own after the refusal')
        self.assertTrue(p.entry_batch_orders)
        self.assertEqual(p.place_batch(rows[5:10]), 5)
        self.assertTrue(p.entry_batch_orders)
        self.assertEqual(p.place_batch(rows[10:15]), 5)
        self.assertFalse(p.entry_batch_orders, 'three refused batches in a row turn batching off')
        self.assertEqual(len(self.entries(p)), 15)
        self.assertEqual(len({pos.client_id for pos in p.open.values()}), 15, 'one order per client id')

    def test_a_working_batch_resets_the_refusal_streak(self):
        p = self.desk(10)
        p.api.refuse_batches = (100400, 'nope')
        rows = self.rows(p)
        p.place_batch(rows[0:5])
        p.place_batch(rows[5:10])
        self.assertEqual(p._batch_reject_streak, 2)
        p.api.refuse_batches = None
        q = self.desk(5)
        q._batch_reject_streak = 2
        q.place_batch(self.rows(q))
        self.assertEqual(q._batch_reject_streak, 0)
        self.assertTrue(q.entry_batch_orders)

    def test_lost_batch_response_resolves_every_order_by_client_id_without_resending(self):
        p = self.desk(5)
        p.api.lost_batch = 'accepted'
        opened = p.place_batch(self.rows(p))
        self.assertEqual(opened, 5, p.last_error)
        self.assertEqual(len(p.api.lookups), 5)
        self.assertEqual(self.entries(p), [], 'nothing was re-sent')
        self.assertEqual(p.api.entry_batches, [5], 'the batch was sent once')
        self.assertEqual(len(p.api.filled), 5, 'the venue holds exactly one order per client id')

    def test_lost_batch_response_with_unknown_state_keeps_every_intent_and_sends_nothing_new(self):
        p = self.desk(5)
        p.api.lost_batch = 'unknown'
        opened = p.place_batch(self.rows(p))
        self.assertEqual(opened, 0)
        self.assertEqual(len(p.pending_orders), 5)
        self.assertTrue(all((row.get('metadata') or {}).get('ambiguous_since') for row in p.pending_orders.values()))
        self.assertEqual(self.entries(p), [])
        self.assertEqual(p.api.entry_batches, [5])
        # The lanes stay occupied: a second pass must not enter them again.
        p.api.lost_batch = None
        self.assertEqual(p.place_batch(self.rows(p)), 0)
        self.assertEqual(p.api.entry_batches, [5])

    def test_new_groups_keep_the_stagger_while_occupied_groups_batch_freely(self):
        p = self.desk(5)
        names = ['A-USDT', 'B-USDT', 'C-USDT']
        for name in names:
            p.px[name] = 100.
            p.contracts[name] = pt.Contract(name, .001, .001, 3, 2, 1., 100)
        rows = [(name, 1, 'gen:trend', .9, p.sets.by_idx[i]) for i, name in enumerate(names)]
        with patch.object(pt, 'STAGGER_S', 0.6), patch.object(pt, 'MAX_OPEN', 100):
            opened = p.place_batch(rows)
        self.assertEqual(opened, 1, 'only the first new group opens; the rest wait for the stagger')
        q = self.desk(5)
        with patch.object(pt, 'STAGGER_S', 0.6), patch.object(pt, 'MAX_OPEN', 100):
            self.assertEqual(q.place_batch(self.rows(q)), 5, 'independent lots on one group are not staggered')

    def test_block_active_entries_run_after_the_batch_through_place(self):
        p = self.desk(3)
        p._entry_modes = lambda forced_row, selected: ['normal', 'block-active']
        calls = []
        real_place = p.place

        def spy(sym, direction, reason, conf, forced_row=None, *, selected_set=None, execution_strategy=None):
            calls.append((sym, execution_strategy, len(p.open)))
            return real_place(sym, direction, reason, conf, forced_row, selected_set=selected_set,
                              execution_strategy=execution_strategy)

        p.place = spy
        p.place_batch(self.rows(p))
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(mode == 'block-active' for _, mode, _ in calls))
        self.assertTrue(all(opened == 3 for _, _, opened in calls), 'Normal lots were booked first')

    def test_250_independent_lots_open_through_the_scheduler_in_batches(self):
        p = self.desk(250)
        with patch.object(pt, 'SYMBOLS', [X]):
            for _ in range(60):
                p.maybe_entries()
                if len(p.open) == 250:
                    break
        self.assertEqual(p.errors, 0, p.last_error)
        self.assertEqual(len(p.open), 250)
        singles = self.entries(p)
        self.assertEqual(sum(p.api.entry_batches) + len(singles), 250)
        self.assertEqual(max(p.api.entry_batches), 5)
        self.assertGreaterEqual(sum(p.api.entry_batches), 240, 'almost every lot went out in a batch')
        self.assertLess(len(singles), 5, 'only a cycle-end remainder of one candidate is sent alone')
        self.assertEqual(len({pos.client_id for pos in p.open.values()}), 250)
        self.assertEqual(len({pos.set_id for pos in p.open.values()}), 250)
        self.assertEqual(p.pending_orders, {})
        for pos in p.open.values():
            self.assertEqual(float(p.api.orders[pos.sl_oid]['quantity']), pos.qty)
            self.assertEqual(float(p.api.orders[pos.tp_oid]['quantity']), pos.qty)
        p.save_open_book()
        restored = self.h.pulse(p.sets)
        restored._load_open_book()
        self.assertEqual(set(restored.open), set(p.open))

    def test_a_single_candidate_uses_the_plain_order_endpoint(self):
        p = self.desk(1)
        self.assertEqual(p.place_batch(self.rows(p)), 1)
        self.assertEqual(p.api.entry_batches, [])
        self.assertEqual(len(self.entries(p)), 1)


if __name__ == '__main__':
    unittest.main()
