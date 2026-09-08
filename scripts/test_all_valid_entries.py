"""All-valid scheduling through the real order/accounting boundary, offline."""
import pathlib
import sys
import tempfile
import unittest
from dataclasses import replace
from collections import deque
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server/pulse'))
import pulse_trader as pt
from entry_dispatch import EntryMatrix
from indication_engine import Indication, IndicationBook
from set_engine import SetBook, SetState


class Exchange:
    def __init__(self):
        self.posts, self.orders, self.batches = [], {}, []
        self.n = 0
        self.path_cd = {}
        self.fill_fraction = 1

    def post(self, path, body):
        self.posts.append(dict(body))
        self.n += 1
        return {'code': 0, 'data': {'order': {'orderId': str(self.n), 'avgPrice': '100',
                'executedQty': float(body.get('quantity', 0)) * self.fill_fraction}}}

    def batch_place(self, bodies):
        self.batches.append(list(bodies))
        rows = []
        for body in bodies:
            self.n += 1
            row = dict(body, orderId=str(self.n), origQty=body.get('quantity'))
            self.orders[str(self.n)] = row
            rows.append(dict(row, code=0))
        return {'code': 0, 'data': {'orders': rows}}

    def get(self, path, params=None):
        return {'code': 0, 'data': list(self.orders.values()) if 'openOrders' in path else []}

    def delete(self, path, params=None):
        self.orders.pop(str((params or {}).get('orderId')), None)
        return {'code': 0}


class AllValidEntries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name in ('TRADES_PATH', 'OPEN_PATH', 'PENDING_PATH'):
            guard = patch.object(pt, name, str(pathlib.Path(self.tmp.name) / name))
            guard.start(); self.addCleanup(guard.stop)
        for name, value in [('STAGGER_S', 0), ('MAX_OPEN', 0), ('MAX_PER_GROUP', 0)]:
            guard = patch.object(pt, name, value)
            guard.start(); self.addCleanup(guard.stop)
        silent = patch.object(pt, 'log')
        silent.start(); self.addCleanup(silent.stop)

    def book(self, count=4):
        book = SetBook()
        book.max_active = 0
        book.progress.ready = True
        book.sets, book.by_idx = {}, []
        for index in range(count):
            state = SetState(id=f'general:1m:sl0.6:st3:config{index}', pack='general',
                tf='1m', sl_ratio=.6, trail_key='', trail_arm=0, trail_give=0,
                step=3, tp_pct=.0045, idx=index, active=True, last15_n=12,
                last15_ratio=1.2, expectancy=.002)
            book.sets[state.id] = state
            book.by_idx.append(state)
        return book

    def pulse(self, book=None):
        p = pt.Pulse.__new__(pt.Pulse)
        p.sets = book or self.book()
        p.api = Exchange()
        p.normal_execution_enabled = True
        p.block_active = False
        p.halted = False
        p.errors = 0; p.last_error = ''; p.did_io = False
        p.cooldown = {}; p.ignore_syms = {}; p.skip_log = {}; p.open = {}
        p.owned_syms = set(); p.seen_fill_cids = set(); p.pending_orders = {}
        p.px = {'X-USDT': 100.}; p.last_px = {}
        p.contracts = {'X-USDT': pt.Contract('X-USDT', .001, .001, 3, 2, 1., 100)}
        p.available = 1000.; p.fees_est = 0; p._order_est = 0; p._order_est_known = True
        p._oo_cache = {}; p.ctrl_skip = {}; p.last_entry_ts = 0
        p.entries_blocked = lambda: False
        p.group_of = lambda s: 'g'; p.group_count = lambda g: 0
        p.indications = IndicationBook(); p.strat_ind = True; p.strat_general = True
        p.variants = NS(on_close=Mock(), current_sl=lambda: .6, current_trail=lambda: ('', 0, 0))
        p.exits = NS(enabled=False, ignore_tp=False, opt_sl_min=.001, opt_sl_max=.02, on_close=Mock())
        p.sl_min = .0015; p.sl_max = .03; p.tp_min = .003; p.tp_max = .03
        p.position_cost_pct = .15; p.tp_cost_ratio = 1.5
        p.size_qty = lambda *a, **k: .05
        p.max_book_notional = lambda **k: 1e9
        p.ensure_max_leverage = lambda *a, **k: 100; p.leverage_for = lambda c: 100
        p.control_orders = True; p.control_orders_per_config = True
        p.record_event = Mock(return_value=True)
        p.signals = []; p.closed = []; p.wins = 0; p.losses = 0; p.consec_loss = 0
        p.block = NS(register_parent=Mock(), on_parent_close=Mock())
        p.dca = NS(attach=Mock(), on_close=Mock(), drop=Mock())
        p.ban_sym = Mock(); p.live_pos_keys = None
        p.strategy_closes = lambda: p.closed
        p.coord = NS(gate=lambda *a, **k: (True, [], {}), slot_cap=lambda *a: 10**9,
                     pick_rearrange=lambda *a: None)
        p.score = lambda s: (1, 'trend', .9)
        p.maybe_forced_entries = Mock(); p.avail_notional = lambda: 1000
        return p

    def indication(self, **changes):
        return Indication(**dict(symbol='X-USDT', direction='long', mode='trend',
            confidence=.8, strength=.8, agreement=1., stop_loss_pct=.3,
            take_profit_pct=.5, reward_risk=1.7, last_price=100., sources=['price'],
            votes_long=1, votes_short=0, primary=False, t=1., timeframe='1m', kind='trend', **changes))

    def test_all_base_trail_and_cold_siblings_pass_without_winner_filter(self):
        book = self.book()
        book.by_idx[1].kind = 'trail'
        book.by_idx[0].live = [dict(t=i, pnl=1, pnl_pct=.01, qty=1, entry=100) for i in range(12)]
        self.assertEqual({s.id for s in book.pick_all('general', 'LONG')}, set(book.sets))
        book.by_idx[2].last15_ratio = .9
        book.by_idx[3].max_dd_s = book.max_dd_s + 1
        self.assertEqual(len(book.pick_all('general', 'LONG')), 2)

    def test_indication_variants_directions_and_exact_match(self):
        b = IndicationBook(); first = self.indication()
        rows = [first, replace(first, mode='trend:8:34'), replace(first, timeframe='5m'),
                replace(first, direction='short'), replace(first, kind='break')]
        b.last['X-USDT'] = rows + [first]
        self.assertEqual(len(b.pick_entries('X-USDT')), 5)
        for row in rows:
            self.assertIs(b.match('X-USDT', f'ind:{row.kind}:{row.mode} cfg={row.entry_key}'), row)
        b.last['X-USDT'] = rows[1:]
        self.assertIsNone(b.match('X-USDT', f'ind:trend:trend cfg={first.entry_key}'))

    def test_lazy_matrix_covers_all_combinations_with_bounded_storage(self):
        signals = [(.9, f'S{i}', 1, 'gen:trend') for i in range(100)]
        states = list(range(34000))
        matrix = EntryMatrix(signals, {('general', 'LONG'): states})
        self.assertEqual(len(matrix), 3_400_000)
        self.assertEqual(len(matrix.signals), 100)
        self.assertEqual(len(matrix.ends), 100)
        self.assertIs(matrix.sets_by_scope[('general', 'LONG')], states)
        self.assertEqual(matrix[33999][-1], 33999)
        self.assertEqual(matrix[34000][-1], 0)

    def test_scheduler_opens_250_independent_orders_on_one_symbol_and_range(self):
        p = self.pulse(self.book(250))
        with patch.object(pt, 'SYMBOLS', ['X-USDT']):
            for _ in range(50):
                p.maybe_entries()
                if len(p.open) == 250:
                    break
        self.assertEqual(p.errors, 0, p.last_error)
        self.assertEqual(len(p.open), 250)
        self.assertEqual(len(p.api.posts), 250)
        self.assertEqual(len(p.api.orders), 500)
        self.assertEqual(len({pos.set_id for pos in p.open.values()}), 250)
        self.assertEqual(len({p.cid('t', pos) for pos in p.open.values()}), 250)
        for pos in p.open.values():
            self.assertEqual(pos.member_count, 1)
            token = pt.control_group_token(pos.control_group_key, pos.control_range_key)
            self.assertIs(p.position_for_group_token(pos.symbol, pos.side, token), pos)
            self.assertEqual(float(p.api.orders[pos.sl_oid]['quantity']), pos.qty)
            self.assertEqual(float(p.api.orders[pos.tp_oid]['quantity']), pos.qty)
        p.save_open_book()
        restored = self.pulse(p.sets)
        restored._load_open_book()
        self.assertEqual(set(restored.open), set(p.open))
        self.assertEqual(len({v.execution_lane for v in restored.open.values()}), 250)

    def test_same_lane_pending_blocks_only_itself_and_reserves_a_slot(self):
        p = self.pulse()
        p.api.fill_fraction = .5
        for state in p.sets.by_idx[:2]:
            p.place('X-USDT', 1, 'gen:trend', .9, selected_set=state)
        self.assertEqual(len(p.open), 2)
        self.assertEqual(len(p.pending_orders), 2)
        self.assertEqual(p.entry_slot_count(), 2)
        self.assertGreater(p.pending_entry_margin(), 0)
        p.place('X-USDT', 1, 'gen:trend', .9, selected_set=p.sets.by_idx[0])
        self.assertEqual(len(p.api.posts), 2)
        with patch.object(pt, 'MAX_OPEN', 2):
            p.place('X-USDT', 1, 'gen:trend', .9, selected_set=p.sets.by_idx[2])
        self.assertEqual(len(p.api.posts), 2)

    def test_all_indication_and_general_candidates_keep_their_own_orders(self):
        b = self.book(2)
        original = b.by_idx[1]
        del b.sets[original.id]
        ind_state = replace(original, id='indications:1m:sl0.6:st3', pack='indications')
        b.by_idx[1] = ind_state; b.sets[ind_state.id] = ind_state
        p = self.pulse(b)
        signal = self.indication()
        p.indications.last['X-USDT'] = [signal, replace(signal, timeframe='5m'), replace(signal, direction='short')]
        with patch.object(pt, 'SYMBOLS', ['X-USDT']):
            for _ in range(5):
                p.maybe_entries()
        self.assertEqual(p.errors, 0, p.last_error)
        self.assertEqual(len(p.open), 4)
        self.assertEqual(sum(pos.pack == 'general' for pos in p.open.values()), 1)
        self.assertEqual(sum(pos.side == 'SHORT' for pos in p.open.values()), 1)
        self.assertEqual(len({pos.execution_lane for pos in p.open.values()}), 4)

    def test_one_failed_candidate_cannot_starve_the_others(self):
        p = self.pulse(); examined = []
        def submit(*args, selected_set=None):
            examined.append(selected_set.id)
            if selected_set.idx == 0:
                raise TimeoutError('simulated venue timeout')
        p.place = submit
        with patch.object(pt, 'SYMBOLS', ['X-USDT']):
            p.maybe_entries()
        self.assertEqual(set(examined), set(p.sets.sets))
        self.assertEqual(p.errors, 1)

    def test_selected_state_is_rechecked_and_never_reselected(self):
        p = self.pulse(); selected = p.sets.by_idx[1]
        p.sets.pick_any = Mock(side_effect=AssertionError('must not pick another Set'))
        p.place('X-USDT', 1, 'gen:trend', .9, selected_set=selected)
        self.assertEqual(next(iter(p.open.values())).set_id, selected.id)
        stale = replace(p.sets.by_idx[2])
        p.place('X-USDT', 1, 'gen:trend', .9, selected_set=stale)
        self.assertEqual(len(p.api.posts), 1)

    def test_partial_exit_keeps_siblings_and_attributes_completed_own_set(self):
        p = self.pulse()
        for state in p.sets.by_idx[:2]:
            p.place('X-USDT', 1, 'gen:trend', .9, selected_set=state)
        first, sibling = list(p.open.values())
        siblings_controls = (sibling.sl_oid, sibling.tp_oid)
        p._record_close_fill(first, .02, 101., 'test-partial', exchange=True)
        self.assertAlmostEqual(first.qty, .03)
        self.assertEqual(sibling.qty, .05)
        self.assertTrue(all(oid in p.api.orders for oid in siblings_controls))
        p._record_close_fill(first, .03, 102., 'test-final', exchange=True)
        self.assertEqual(len(p.open), 1)
        self.assertEqual(len(p.sets.by_idx[0].live), 1)
        self.assertEqual(len(p.sets.by_idx[1].live), 0)
        self.assertTrue(all(row.execution_lane == first.execution_lane for row in p.closed))

    def test_interleaved_partials_beyond_shared_tape_keep_complete_own_pf(self):
        p = self.pulse(self.book(100))
        p.closed = deque(maxlen=80)
        for state in p.sets.by_idx:
            p.place('X-USDT', 1, 'gen:trend', .9, selected_set=state)
        positions = list(p.open.values())
        for pos in positions:
            p._record_close_fill(pos, .02, 101., 'first-part', exchange=True)
        self.assertEqual(len(p.closed), 80)
        p.save_open_book()
        restored = self.pulse(p.sets)
        restored.api = p.api
        restored.closed = p.closed
        restored._load_open_book()
        for pos in list(restored.open.values()):
            restored._record_close_fill(pos, .03, 102., 'last-part', exchange=True)
        self.assertFalse(restored.open)
        for state in p.sets.by_idx:
            self.assertEqual(len(state.live), 1)
            self.assertAlmostEqual(state.live[0]['qty'], .05)
            self.assertAlmostEqual(state.live[0]['pnl'], .0725)
            self.assertAlmostEqual(state.live[0]['pnl_pct'], .016)


if __name__ == '__main__':
    unittest.main()
