"""End-to-end control-order (SL/TP protection) matrix against a stateful fake BingX.

The fake venue keeps real order state: open orders, trigger-on-mark, partial
and complete fills, cancel and cancelReplace, well-known rejection codes,
rate-limit cooling, minimum size and absent positions. Every scenario drives
the real Pulse methods (place, priority_controls, ensure_controls,
sync_own_fills, close_pos, replace_sl, the persisted book) and checks the
exchange truth, not the engine's own bookkeeping.

Modes: ``overall`` (one shared pair per symbol x direction at the widest
member range) and ``per-config`` (one quantity-matched pair per Set group).
"""
import json
import math
import pathlib
import sys
import time as _time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server/pulse'))
import overall_controls as overall
import pulse_trader as pt
import test_all_valid_entries as harness

SYM = 'X-USDT'
ORDER = '/openApi/swap/v2/trade/order'
LIVE = ('NEW', 'PARTIALLY_FILLED')
CTRL_TYPES = {'STOP_MARKET', 'TAKE_PROFIT_MARKET', 'STOP', 'TAKE_PROFIT'}
SL_TYPES = {'STOP_MARKET', 'STOP'}
MODES = ('overall', 'per-config')
SIDES = ('LONG', 'SHORT')


class Clock:
    """Shared wall/monotonic offset so cooldowns can elapse without sleeping."""

    def __init__(self):
        self.offset = 0.0

    def time(self):
        return _time.time() + self.offset

    def monotonic(self):
        return _time.monotonic() + self.offset

    def __getattr__(self, name):
        return getattr(_time, name)


class FakeBingX:
    """Stateful hedge-mode swap venue with exactly the endpoints Pulse uses."""

    def __init__(self, clock, mark=100.0):
        self.clock = clock
        self.mark = {SYM: float(mark)}
        self.orders = {}           # every order ever accepted, by id
        self.positions = {}        # (symbol, side) -> own qty on the venue
        self.foreign = {}          # (symbol, side) -> foreign qty on the venue
        self.requests = []         # (method, path, body) actually sent
        self.cooled = 0            # requests refused locally while cooling
        self.path_cd = {}
        self.cooldown_until = 0.0
        self.rules = []            # [predicate, response, remaining]
        self.min_qty = 0.0
        self.n = 1000
        self.cancel_filled_msg = ('109421', 'order not exist')
        self.market_fraction = 1.0
        self.watch = None          # invariant callback after every venue write

    # -- helpers -----------------------------------------------------------
    def order_retry_after(self):
        return max(0.0, max(self.cooldown_until, self.path_cd.get(ORDER, 0.0)) - self.clock.time())

    def inject(self, predicate, response, times=1):
        self.rules.append([predicate, response, times])

    def _rule(self, method, path, body):
        for rule in self.rules:
            predicate, response, remaining = rule
            if remaining and predicate(method, path, body):
                rule[2] = remaining - 1
                return dict(response)
        return None

    def _order_lane(self, path):
        return '/trade/order' in path or '/batchOrders' in path or '/cancelReplace' in path

    def _gate(self, method, path):
        if self._order_lane(path) and method != 'GET' and self.order_retry_after() > 0:
            self.cooled += 1
            return {'code': 101209, 'msg': 'cooling', 'error': True, 'cooled': True}
        return None

    def _trip(self, response):
        if str(response.get('code')) in ('100410', '109429'):
            self.cooldown_until = self.clock.time() + 30.0
        return response

    def open_orders(self, symbol=SYM):
        return [o for o in self.orders.values() if o['status'] in LIVE and o['symbol'] == symbol]

    def own_controls(self, side=None, cid_prefix=None):
        tag = (cid_prefix or pt.TAG).lower()
        return [o for o in self.open_orders() if o['type'] in CTRL_TYPES
                and str(o.get('clientOrderID', '')).lower().startswith(tag)
                and (side is None or o['positionSide'] == side)]

    def count(self, kind=None):
        rows = self.requests
        if kind is None:
            return len(rows)
        return sum(1 for m, p, b in rows if _kind(m, p, b) == kind)

    def counts(self):
        out = {}
        for m, p, b in self.requests:
            k = _kind(m, p, b)
            out[k] = out.get(k, 0) + 1
        return out

    def _new_id(self):
        self.n += 1
        return str(self.n)

    # -- validation shared by single, batch and cancelReplace --------------
    def _validate(self, body):
        typ = str(body.get('type') or '')
        side = str(body.get('positionSide') or '')
        key = (body.get('symbol'), side)
        if typ not in CTRL_TYPES:
            return None
        held = self.positions.get(key, 0.0) + self.foreign.get(key, 0.0)
        if held <= 1e-12:
            return {'code': 101205, 'msg': 'No position to close'}
        close_all = str(body.get('closePosition')).lower() == 'true'
        if not close_all and not body.get('quantity'):
            return {'code': 110422, 'msg': 'parameter quantity is required'}
        qty = float(body.get('quantity') or 0) if body.get('quantity') else held
        if self.min_qty and qty + 1e-12 < self.min_qty:
            return {'code': 101400, 'msg': f'The minimum order amount is {self.min_qty} X.'}
        if qty > held + 1e-12:
            return {'code': 101204, 'msg': f'The order size must be less than the available amount of {held} X'}
        stop = float(body.get('stopPrice') or 0)
        mark = self.mark[body['symbol']]
        is_sl = typ in SL_TYPES
        protective_below = (side == 'LONG') == is_sl
        if protective_below and not stop < mark:
            word = 'Stop loss' if is_sl else 'Take profit'
            return {'code': 110400, 'msg': f'{word} price should be less than {mark}'}
        if not protective_below and not stop > mark:
            word = 'Stop loss' if is_sl else 'Take profit'
            return {'code': 110400, 'msg': f'{word} price should be greater than {mark}'}
        return None

    def _accept(self, body, cid_key='clientOrderID'):
        oid = self._new_id()
        cid = body.get('clientOrderID') or body.get('clientOrderId') or ''
        row = dict(body)
        row.pop('clientOrderId', None)
        row.pop('cancelOrderId', None)
        row.pop('cancelReplaceMode', None)
        row.update(orderId=oid, clientOrderID=cid, status='NEW', executedQty='0', avgPrice='0',
                   origQty=str(body.get('quantity') or '0'), time=int(self.clock.time() * 1000))
        self.orders[oid] = row
        return row

    # -- endpoints ----------------------------------------------------------
    def post(self, path, body):
        try:
            return self._post(path, body)
        finally:
            self._watch()

    def _watch(self):
        if self.watch:
            self.watch()

    def _post(self, path, body):
        body = dict(body)
        cooled = self._gate('POST', path)
        if cooled:
            return cooled
        self.requests.append(('POST', path, body))
        injected = self._rule('POST', path, body)
        if injected is not None:
            return self._trip(injected)
        if path.endswith('/cancelReplace'):
            return self._cancel_replace(body)
        typ = str(body.get('type') or '')
        if typ == 'MARKET':
            return self._market(body)
        bad = self._validate(body)
        if bad:
            return bad
        row = self._accept(body)
        return {'code': 0, 'data': {'order': dict(row)}}

    def batch_place(self, bodies):
        try:
            return self._batch(bodies)
        finally:
            self._watch()

    def _batch(self, bodies):
        cooled = self._gate('POST', '/openApi/swap/v2/trade/batchOrders')
        if cooled:
            return cooled
        self.requests.append(('POST', '/openApi/swap/v2/trade/batchOrders', {'n': len(bodies)}))
        injected = self._rule('POST', '/openApi/swap/v2/trade/batchOrders', {'orders': bodies})
        if injected is not None:
            return self._trip(injected)
        rows = []
        for body in bodies:
            bad = self._validate(body)
            if bad:
                rows.append(dict(bad, clientOrderID=body.get('clientOrderID')))
                continue
            rows.append(dict(self._accept(body), code=0))
        return {'code': 0, 'data': {'orders': rows}}

    def _market(self, body):
        key = (body['symbol'], body['positionSide'])
        qty = float(body.get('quantity') or 0)
        opening = (body['side'] == 'BUY') == (body['positionSide'] == 'LONG')
        if not opening:
            if str(body.get('closePosition')).lower() == 'true':
                qty = self.positions.get(key, 0.0)
            if self.positions.get(key, 0.0) <= 1e-12:
                return {'code': 101205, 'msg': 'No position to close'}
            qty = min(qty, self.positions.get(key, 0.0))
        row = self._accept(body)
        px = self.mark[body['symbol']]
        done = qty if opening else round(qty * self.market_fraction, 12)
        row.update(status='FILLED' if done + 1e-12 >= qty else 'PARTIALLY_FILLED',
                   executedQty=str(done), avgPrice=str(px), origQty=str(qty))
        self.positions[key] = self.positions.get(key, 0.0) + (done if opening else -done)
        return {'code': 0, 'data': {'order': dict(row)}}

    def complete(self, oid):
        """Finish a partially executed market close at the current mark."""
        row = self.orders[oid]
        key = (row['symbol'], row['positionSide'])
        left = min(float(row['origQty']) - float(row['executedQty']), self.positions.get(key, 0.0))
        px = self.mark[row['symbol']]
        done = float(row['executedQty'])
        row['avgPrice'] = str((done * float(row['avgPrice']) + left * px) / (done + left))
        row['executedQty'] = str(round(done + left, 12))
        row['status'] = 'FILLED'
        self.positions[key] -= left

    def _cancel_replace(self, body):
        old = self.orders.get(str(body.get('cancelOrderId')))
        if old is None or old['status'] not in LIVE:
            return {'code': 109421, 'msg': 'order not exist'}
        new = dict(body)
        bad = self._validate(new)
        old['status'] = 'CANCELED'
        if bad:
            return {'code': 0, 'data': {'cancelResult': 'SUCCESS', 'newOrderResult': 'FAILED',
                                        'newOrderMsg': bad['msg']}}
        row = self._accept(new)
        return {'code': 0, 'data': {'cancelResult': 'SUCCESS', 'newOrderResult': 'SUCCESS',
                                    'newOrderId': row['orderId']}}

    def delete(self, path, params=None):
        try:
            return self._delete(path, params)
        finally:
            self._watch()

    def _delete(self, path, params=None):
        params = dict(params or {})
        cooled = self._gate('DELETE', path)
        if cooled:
            return cooled
        self.requests.append(('DELETE', path, params))
        injected = self._rule('DELETE', path, params)
        if injected is not None:
            return self._trip(injected)
        row = self.orders.get(str(params.get('orderId')))
        if row is None or row['status'] not in LIVE:
            code, msg = ('109421', 'order not exist') if row is None or row['status'] == 'CANCELED' else self.cancel_filled_msg
            return {'code': int(code), 'msg': msg}
        row['status'] = 'CANCELED'
        return {'code': 0, 'data': {'order': dict(row)}}

    def get(self, path, params=None):
        params = dict(params or {})
        self.requests.append(('GET', path, params))
        injected = self._rule('GET', path, params)
        if injected is not None:
            return injected
        if path.endswith('/openOrders'):
            return {'code': 0, 'data': {'orders': [dict(o) for o in self.orders.values() if o['status'] in LIVE]}}
        if path.endswith('/trade/order'):
            oid = str(params.get('orderId') or '')
            cid = str(params.get('clientOrderId') or '').lower()
            row = self.orders.get(oid) if oid else next(
                (o for o in self.orders.values() if str(o.get('clientOrderID', '')).lower() == cid), None)
            if row is None:
                return {'code': 109421, 'msg': 'order not exist'}
            return {'code': 0, 'data': {'order': dict(row)}}
        if path.endswith('/allOrders'):
            rows = sorted(self.orders.values(), key=lambda o: int(o['orderId']))[-50:]
            return {'code': 0, 'data': {'orders': [dict(o) for o in rows]}}
        if path.endswith('/user/positions'):
            out = []
            keys = set(self.positions) | set(self.foreign)
            for symbol, side in keys:
                total = self.positions.get((symbol, side), 0.0) + self.foreign.get((symbol, side), 0.0)
                if total > 1e-12:
                    out.append({'symbol': symbol, 'positionSide': side, 'positionAmt': str(total),
                                'avgPrice': str(self.mark[symbol]), 'markPrice': str(self.mark[symbol])})
            return {'code': 0, 'data': out}
        return {'code': 0, 'data': []}

    def public(self, path, params=None):
        symbol = (params or {}).get('symbol', SYM)
        return {'code': 0, 'data': {'lastPrice': str(self.mark[symbol]), 'markPrice': str(self.mark[symbol])}}

    # -- market simulation ----------------------------------------------------
    def fill(self, oid, qty, px=None):
        """Execute ``qty`` more of an open control order (partial or final)."""
        row = self.orders[oid]
        key = (row['symbol'], row['positionSide'])
        qty = min(qty, float(row['origQty']) - float(row['executedQty']), self.positions.get(key, 0.0))
        px = float(px or row['stopPrice'])
        done = float(row['executedQty'])
        avg = (done * float(row['avgPrice']) + qty * px) / (done + qty)
        row['executedQty'] = str(round(done + qty, 12))
        row['avgPrice'] = str(avg)
        row['status'] = 'FILLED' if float(row['executedQty']) + 1e-12 >= float(row['origQty']) else 'PARTIALLY_FILLED'
        self.positions[key] = self.positions.get(key, 0.0) - qty
        return qty

    def coverage_gaps(self, own_qty):
        """Sides whose own venue position is not fully covered by own SL and TP."""
        gaps = []
        for side, qty in own_qty.items():
            if qty <= 1e-12:
                continue
            for want_sl in (True, False):
                live = sum(float(o['origQty']) - float(o['executedQty']) for o in self.own_controls(side)
                           if (o['type'] in SL_TYPES) == want_sl)
                if live + 1e-9 < qty:
                    gaps.append((side, 'SL' if want_sl else 'TP', live, qty))
        return gaps

    def set_mark(self, px, symbol=SYM, fraction=1.0):
        """Move mark and trigger every stop/take-profit it crosses."""
        self.mark[symbol] = float(px)
        hit = []
        for row in list(self.orders.values()):
            if row['symbol'] != symbol or row['status'] not in ('NEW', 'PARTIALLY_FILLED') or row['type'] not in CTRL_TYPES:
                continue
            stop = float(row['stopPrice'])
            below = (row['positionSide'] == 'LONG') == (row['type'] in SL_TYPES)
            if (below and px <= stop) or (not below and px >= stop):
                left = float(row['origQty']) - float(row['executedQty'])
                if self.fill(row['orderId'], left * fraction):
                    hit.append(row['orderId'])
        return hit


def _kind(method, path, body):
    if method == 'DELETE':
        return 'cancel'
    if path.endswith('/cancelReplace'):
        return 'replace'
    if path.endswith('/batchOrders'):
        return 'batch'
    if method == 'GET':
        return 'read'
    if str(body.get('type') or '') == 'MARKET':
        return 'market'
    return 'ctrl'


class ControlsE2E(unittest.TestCase):
    def make(self, mode, n=3, tp=(.0045, .006, .009)):
        desk = harness.AllValidEntries('test_unlimited_tp_keeps_selected_range_and_sl_cap')
        desk.setUp()
        self.addCleanup(desk.doCleanups)
        self.desk = desk
        self.clock = Clock()
        for module in (pt, overall):
            guard = patch.object(module, 'time', self.clock)
            guard.start()
            self.addCleanup(guard.stop)
        p = desk.pulse(desk.book(n))
        for state, value in zip(p.sets.by_idx, tp):
            state.tp_pct = value
        ex = FakeBingX(self.clock)
        p.api = ex
        p.px = ex.mark
        p.last_px = {}
        p.control_orders = True
        p.control_orders_per_config = True
        p.control_orders_overall = mode == 'overall'
        p.recon_pending = False
        p.bump = lambda *a, **k: None
        self.p, self.ex, self.mode = p, ex, mode
        return p, ex

    # -- driving helpers -----------------------------------------------------
    def enter(self, side, idx):
        p = self.p
        before = set(id(r) for r in p.open.values())
        p.place(SYM, 1 if side == 'LONG' else -1, 'trend', .9, selected_set=p.sets.by_idx[idx])
        rows = [r for r in p.open.values() if id(r) not in before]
        self.assertTrue(rows, p.last_error)
        return rows[0]

    def tick(self, seconds=20.0, passes=1):
        """One engine control pass after ``seconds`` of simulated time."""
        miss = 0
        for _ in range(passes):
            self.clock.offset += seconds
            self.p._oo_cache = {}
            miss = self.p.priority_controls()
        return miss

    def ours(self, side):
        return [r for r in self.p.open.values() if r.side == side and self.p.position_is_ours(r)]

    def assert_protected(self, side, ranges=True):
        """Exchange truth: protection matches the mode's contract exactly."""
        p, ex = self.p, self.ex
        rows = self.ours(side)
        orders = ex.own_controls(side)
        sls = [o for o in orders if o['type'] in SL_TYPES]
        tps = [o for o in orders if o['type'] not in SL_TYPES]
        mark = ex.mark[SYM]
        if not rows:
            self.assertEqual(orders, [], 'orphan control orders with no position')
            return
        groups = [rows] if self.mode == 'overall' else [[r] for r in rows]
        self.assertEqual(len(sls), len(groups), [(o['type'], o['origQty']) for o in orders])
        self.assertEqual(len(tps), len(groups), [(o['type'], o['origQty']) for o in orders])
        for members in groups:
            qty = sum(r.qty for r in members)
            sl = ex.orders[members[0].sl_oid]
            tp = ex.orders[members[0].tp_oid]
            self.assertEqual({r.sl_oid for r in members}, {sl['orderId']})
            self.assertEqual({r.tp_oid for r in members}, {tp['orderId']})
            for order in (sl, tp):
                self.assertEqual(order['status'], 'NEW')
                self.assertAlmostEqual(float(order['origQty']), qty, places=9)
                self.assertEqual(order['positionSide'], side)
                self.assertEqual(order['side'], 'SELL' if side == 'LONG' else 'BUY')
                self.assertNotEqual(str(order.get('closePosition')).lower(), 'true')
            s, t = float(sl['stopPrice']), float(tp['stopPrice'])
            entry = sum(r.qty * r.entry for r in members) / qty
            if side == 'LONG':
                self.assertLess(s, mark)
                self.assertGreater(t, mark)
                self.assertGreaterEqual((entry - s) / entry, p.sl_min - 1e-4)
            else:
                self.assertGreater(s, mark)
                self.assertLess(t, mark)
                self.assertGreaterEqual((s - entry) / entry, p.sl_min - 1e-4)
            if ranges:
                if side == 'LONG':
                    widest_sl = min(r.sl for r in members)
                    widest_tp = max(r.tp for r in members)
                else:
                    widest_sl = max(r.sl for r in members)
                    widest_tp = min(r.tp for r in members)
                tick = 0.01
                self.assertAlmostEqual(s, widest_sl, delta=tick)
                self.assertAlmostEqual(t, widest_tp, delta=tick)
            for r in members:
                self.assertTrue(r.controls_ok)
        return groups

    def status(self):
        """Desk-reported counts (the coverage ``controls`` block source)."""
        mode, groups, expected, protected, gaps = self.p._control_pair_counts()
        return dict(mode=mode, groups=groups, expected=expected, protected=protected, gaps=gaps)

    def assert_status_matches_exchange(self):
        p, ex = self.p, self.ex
        truth_protected = 0
        groups = {}
        for r in p.open.values():
            if p.position_is_ours(r):
                groups.setdefault((r.side,) if self.mode == 'overall' else (r.side, id(r)), []).append(r)
        for members in groups.values():
            live = {o['orderId'] for o in ex.own_controls(members[0].side)}
            if all(r.sl_oid in live and r.tp_oid in live for r in members):
                truth_protected += 1
        st = self.status()
        # Per-position controlStatus ("protected" iff controls_ok) must match
        # the venue: both of its legs are live own orders.
        for r in p.open.values():
            if p.position_is_ours(r):
                live = {o['orderId'] for o in ex.own_controls(r.side)}
                self.assertEqual(bool(r.controls_ok), r.sl_oid in live and r.tp_oid in live, (r.sl_oid, r.tp_oid, live))
        self.assertEqual(st['expected'], len(groups), st)
        self.assertEqual(st['protected'], truth_protected, st)
        self.assertEqual(st['gaps'], len(groups) - truth_protected, st)


def matrix(test):
    """Run ``test(self, mode, side)`` for every mode and direction."""
    def run(self):
        for mode in MODES:
            for side in SIDES:
                with self.subTest(mode=mode, side=side):
                    test(self, mode, side)
    run.__name__ = test.__name__
    run.__doc__ = test.__doc__
    return run


class Scenarios(ControlsE2E):
    @matrix
    def test_01_new_entry_gets_exactly_one_pair(self, mode, side):
        p, ex = self.make(mode)
        pos = self.enter(side, 0)
        self.assert_protected(side)
        self.assertEqual(len(ex.own_controls()), 2)
        self.assertEqual(ex.count('ctrl') + ex.count('batch'), 2 if mode == 'overall' else 1, ex.counts())
        self.assertAlmostEqual(ex.positions[(SYM, side)], pos.qty)
        self.assert_status_matches_exchange()
        # A quiet second pass must not touch the venue order lane.
        before = ex.counts()
        self.assertEqual(self.tick(), 0)
        after = ex.counts()
        self.assertEqual({k: v for k, v in after.items() if k != 'read'},
                         {k: v for k, v in before.items() if k != 'read'})


    @matrix
    def test_02_members_join_resize_to_total_and_widest(self, mode, side):
        p, ex = self.make(mode)
        a = self.enter(side, 0)
        first = {a.sl_oid, a.tp_oid}
        b = self.enter(side, 1)
        second = {b.sl_oid, b.tp_oid}
        self.enter(side, 2)
        self.tick(passes=2)
        self.assert_protected(side)
        own = ex.own_controls()
        self.assertEqual(len(own), 2 if mode == 'overall' else 6)
        live = {o['orderId'] for o in own}
        if mode == 'overall':
            for oid in (first | second) - live:
                self.assertEqual(ex.orders[oid]['status'], 'CANCELED')
            self.assertFalse(any(r.retired_control_ids for r in p.open.values()))
            # Joins resize through cancelReplace; nothing is re-placed from scratch.
            self.assertEqual(ex.count('ctrl'), 2, ex.counts())
            self.assertEqual(ex.count('replace'), 4, ex.counts())
            self.assertLessEqual(ex.count('cancel'), 0, ex.counts())
        else:
            self.assertEqual(ex.count('cancel') + ex.count('replace'), 0, ex.counts())
        self.assert_status_matches_exchange()

    @matrix
    def test_03_member_partial_and_full_close_resize_then_cancel(self, mode, side):
        p, ex = self.make(mode)
        a, b, c = (self.enter(side, i) for i in range(3))
        mark = ex.mark[SYM]
        # Full close of one member.
        p.close_pos(b, mark, 'test-exit')
        self.tick()
        self.assertNotIn(b, list(p.open.values()))
        self.assert_protected(side)
        # Partial close of another member: half now, the rest confirmed later.
        ex.market_fraction = .5
        p.close_pos(a, mark, 'test-exit')
        ex.market_fraction = 1.0
        self.assertAlmostEqual(a.qty, .025)
        self.tick()
        self.assert_protected(side)
        market = [o for o in ex.orders.values() if o['type'] == 'MARKET' and o['status'] == 'PARTIALLY_FILLED']
        self.assertEqual(len(market), 1)
        ex.complete(market[0]['orderId'])
        p.sync_own_fills()
        self.tick()
        self.assertNotIn(a, list(p.open.values()))
        self.assert_protected(side)
        # Last member: both legs must disappear from the venue.
        p.close_pos(c, mark, 'test-exit')
        self.tick(passes=2)
        self.assertFalse(self.ours(side))
        self.assertEqual(ex.own_controls(), [], [(o['type'], o['origQty']) for o in ex.own_controls()])
        self.assertAlmostEqual(sum(r.qty for r in p.closed), .15)
        self.assertAlmostEqual(ex.positions[(SYM, side)], 0.0)
        self.assert_status_matches_exchange()

    @matrix
    def test_03b_last_close_cleanup_survives_restart(self, mode, side):
        p, ex = self.make(mode)
        a = self.enter(side, 0)
        ids = {a.sl_oid, a.tp_oid}
        # Every cancel is refused while the last member closes.
        ex.inject(lambda m, path, b: m == 'DELETE', {'code': 80012, 'msg': 'service unavailable'}, times=50)
        p.close_pos(a, ex.mark[SYM], 'test-exit')
        self.tick(passes=2)
        self.assertFalse(p.open)
        self.assertEqual({o['orderId'] for o in ex.own_controls()}, ids)
        ex.rules.clear()
        # Restart: a fresh engine on the same data directory.
        p2 = self.desk.pulse(self.desk.book(3))
        p2.api, p2.px, p2.last_px = ex, ex.mark, {}
        p2.control_orders_overall = mode == 'overall'
        p2.bump = lambda *a, **k: None
        p2._load_open_book()
        self.p = p2
        self.tick(passes=3)
        self.assertEqual(ex.own_controls(), [], 'orphan controls survived the restart')

    @matrix
    def test_03c_partial_close_never_leaves_the_rest_unprotected(self, mode, side):
        p, ex = self.make(mode)
        a = self.enter(side, 0)
        own = lambda: {s: sum(r.qty for r in self.ours(s)) for s in SIDES}
        gaps = []
        ex.watch = lambda: gaps.extend(ex.coverage_gaps(own()))
        ex.market_fraction = .5
        p.close_pos(a, ex.mark[SYM], 'test-exit')
        ex.market_fraction = 1.0
        self.tick(passes=2)
        ex.watch = None
        self.assertAlmostEqual(a.qty, .025)
        self.assertEqual(gaps, [])
        self.assert_protected(side)
        self.assert_status_matches_exchange()

    @matrix
    def test_03d_rejected_resize_keeps_old_pair_until_replaced(self, mode, side):
        p, ex = self.make(mode)
        a = self.enter(side, 0)
        old = {a.sl_oid, a.tp_oid}
        own = lambda: {s: sum(r.qty for r in self.ours(s)) for s in SIDES}
        gaps = []
        ex.watch = lambda: gaps.extend(ex.coverage_gaps(own()))
        writes = lambda m, path, b: m == 'POST' and (b.get('type') in CTRL_TYPES or 'batch' in path or 'cancelReplace' in path)
        ex.inject(writes, {'code': 80014, 'msg': 'Invalid parameters'}, times=10**6)
        ex.market_fraction = .5
        p.close_pos(a, ex.mark[SYM], 'test-exit')
        ex.market_fraction = 1.0
        self.tick(seconds=2.0, passes=3)
        self.assertEqual(gaps, [])
        self.assertEqual({o['orderId'] for o in ex.own_controls()}, old)
        self.assertEqual(self.status()['gaps'], 1)     # oversized pair is not "protected"
        ex.rules.clear()
        self.tick(seconds=130.0, passes=2)
        ex.watch = None
        self.assert_protected(side)
        self.assert_status_matches_exchange()

    @matrix
    def test_04_trigger_partial_then_full_allocates_once(self, mode, side):
        p, ex = self.make(mode)
        a, b = self.enter(side, 0), self.enter(side, 1)
        entry = {r.client_id: (r.qty, r.entry) for r in (a, b)}
        sl_oid = a.sl_oid
        stop = float(ex.orders[sl_oid]['stopPrice'])
        filled = ex.set_mark(stop, fraction=.5)
        self.assertEqual(filled, [sl_oid])
        p.sync_own_fills()
        p.sync_own_fills()
        self.tick(passes=2)
        first_qty = float(ex.orders[sl_oid]['executedQty'])
        self.assertAlmostEqual(sum(r.qty for r in p.closed), first_qty)
        self.assert_protected(side, ranges=False)
        self.assertAlmostEqual(sum(r.qty for r in self.ours(side)), ex.positions[(SYM, side)])
        # Price continues through every remaining stop.
        far = stop * (0.98 if side == 'LONG' else 1.02)
        ex.set_mark(far)
        p.sync_own_fills()
        p.sync_own_fills()
        self.tick(passes=2)
        closed = sum(r.qty for r in p.closed)
        venue_closed = sum(float(o['executedQty']) for o in ex.orders.values() if o['type'] in CTRL_TYPES)
        self.assertAlmostEqual(closed, venue_closed)
        self.assertAlmostEqual(ex.positions[(SYM, side)], sum(r.qty for r in self.ours(side)))
        if mode == 'overall':
            self.assertFalse(p.open)
            self.assertAlmostEqual(closed, .1)
            per_member = {}
            for r in p.closed:
                per_member[r.client_id] = per_member.get(r.client_id, 0.0) + r.qty
            self.assertEqual({k: round(v, 9) for k, v in per_member.items()},
                             {k: round(v[0], 9) for k, v in entry.items()})
        self.assertEqual(ex.own_controls() if not p.open else [], [])
        n = len(p.closed)
        p.sync_own_fills()
        self.assertEqual(len(p.closed), n)
        self.assert_status_matches_exchange()

    @matrix
    def test_04b_cleanup_of_a_filled_leg_is_bounded(self, mode, side):
        """A venue that answers "already filled" (not "not exist") on cancel."""
        p, ex = self.make(mode)
        ex.cancel_filled_msg = ('109414', 'order has been filled')
        a = self.enter(side, 0)
        tp = float(ex.orders[a.tp_oid]['stopPrice'])
        ex.set_mark(tp * (1.01 if side == 'LONG' else 0.99))
        p.sync_own_fills()
        start = ex.count()
        for _ in range(30):
            self.tick(seconds=2.0)
        self.assertFalse(p.open)
        self.assertEqual(ex.own_controls(), [])
        self.assertFalse(overall.cleanup_state(p))
        self.assertLessEqual(ex.count('cancel'), 3, ex.counts())
        self.assertLessEqual(ex.count() - start, 8, ex.counts())

    @matrix
    def test_05a_persistent_rejection_is_paced_and_reported(self, mode, side):
        p, ex = self.make(mode)
        ex.inject(lambda m, path, b: m == 'POST' and b.get('type') in CTRL_TYPES or path.endswith('batchOrders'),
                  {'code': 80014, 'msg': 'Invalid parameters'}, times=10**6)
        self.enter(side, 0)
        start = ex.count('ctrl') + ex.count('batch')
        misses = [self.tick(seconds=2.0) for _ in range(30)]   # one simulated minute
        sent = ex.count('ctrl') + ex.count('batch') - start
        self.assertTrue(all(m == 1 for m in misses), misses)
        self.assertEqual(self.status()['gaps'], 1)
        self.assertLessEqual(sent, 32, ex.counts())   # was 240-510 per minute
        ex.rules.clear()
        self.tick(seconds=120.0, passes=2)
        self.assert_protected(side)
        self.assert_status_matches_exchange()

    @matrix
    def test_05b_rate_limit_cools_without_requests(self, mode, side):
        p, ex = self.make(mode)
        ex.inject(lambda m, path, b: m == 'POST' and (b.get('type') in CTRL_TYPES or path.endswith('batchOrders')),
                  {'code': 100410, 'msg': 'rate limit'}, times=1)
        self.enter(side, 0)
        start = ex.count()
        misses = [self.tick(seconds=2.0) for _ in range(10)]   # 20s inside the 30s ban
        writes = sum(1 for m, *_ in ex.requests[start:] if m != 'GET')
        self.assertEqual(writes, 0, ex.counts())
        self.assertTrue(all(m == 1 for m in misses), misses)
        self.assertEqual(self.status()['gaps'], 1)
        self.tick(seconds=30.0, passes=2)
        self.assert_protected(side)
        self.assert_status_matches_exchange()

    @matrix
    def test_05c_minimum_size_and_absent_position_back_off(self, mode, side):
        p, ex = self.make(mode)
        # An unlearnable venue floor (VST "minimum size" without a number).
        floor = {'code': 101400, 'msg': 'order amount below the minimum size'}
        is_ctrl = lambda m, path, b: m == 'POST' and (b.get('type') in CTRL_TYPES or path.endswith('batchOrders'))
        ex.inject(is_ctrl, floor, times=10**6)
        self.enter(side, 0)
        start = ex.count('ctrl') + ex.count('batch')
        misses = [self.tick(seconds=2.0) for _ in range(15)]
        self.assertTrue(all(m == 1 for m in misses), misses)
        self.assertLessEqual(ex.count('ctrl') + ex.count('batch') - start, 8, ex.counts())
        ex.rules.clear()
        self.tick(seconds=90.0, passes=2)
        self.assert_protected(side)
        # The venue briefly does not show the position (propagation lag) and
        # the pair was lost with it.
        pos = self.ours(side)[0]
        for oid in (pos.sl_oid, pos.tp_oid):
            ex.orders[oid]['status'] = 'CANCELED'
        held = ex.positions[(SYM, side)]
        ex.positions[(SYM, side)] = 0.0
        start = ex.count('ctrl') + ex.count('batch') + ex.count('replace')
        # An all-empty open-order page needs two confirmed reads (15s apart).
        misses = [self.tick(seconds=2.0) for _ in range(20)]
        self.assertLessEqual(ex.count('ctrl') + ex.count('batch') + ex.count('replace') - start, 8, ex.counts())
        self.assertEqual(misses[-1], 1, misses)
        ex.positions[(SYM, side)] = held
        self.tick(seconds=60.0, passes=3)
        self.assert_protected(side)
        self.assert_status_matches_exchange()

    @matrix
    def test_06_restart_adopts_pair_and_replaces_mismatch(self, mode, side):
        p, ex = self.make(mode)
        a, b = self.enter(side, 0), self.enter(side, 1)
        self.tick()
        ids = {(r.client_id, r.sl_oid, r.tp_oid) for r in p.open.values()}
        p.save_open_book()

        def restart():
            q = self.desk.pulse(self.desk.book(3))
            q.api, q.px, q.last_px = ex, ex.mark, {}
            q.control_orders_overall = mode == 'overall'
            q.bump = lambda *a, **k: None
            q._load_open_book()
            self.p = q
            q.reconcile_startup_positions()
            return q

        q = restart()
        writes = sum(1 for m, *_ in ex.requests if m != 'GET')
        self.tick(passes=3)
        self.assertEqual(sum(1 for m, *_ in ex.requests if m != 'GET'), writes, ex.counts())
        self.assertEqual({(r.client_id, r.sl_oid, r.tp_oid) for r in q.open.values()}, ids)
        self.assert_protected(side)
        # Mismatch while the engine was down: the venue lost one leg and, in
        # overall mode, the widest member range moved.
        row = next(iter(q.open.values()))
        ex.orders[row.tp_oid]['status'] = 'CANCELED'
        if mode == 'overall':
            row.sl -= .3 if side == 'LONG' else -.3
        q.save_open_book()
        q = restart()
        self.tick(passes=3)
        self.assert_protected(side)
        self.assertEqual(len(ex.own_controls()), 2 if mode == 'overall' else 4)
        self.assert_status_matches_exchange()

    @matrix
    def test_07_foreign_orders_and_positions_are_untouched(self, mode, side):
        p, ex = self.make(mode)
        ex.foreign[(SYM, side)] = 1.0
        close = 'SELL' if side == 'LONG' else 'BUY'
        below = side == 'LONG'
        foreign = {}
        for typ, px in (('STOP_MARKET', 95.0 if below else 105.0), ('TAKE_PROFIT_MARKET', 105.0 if below else 95.0)):
            row = ex._accept({'symbol': SYM, 'type': typ, 'side': close, 'positionSide': side,
                              'stopPrice': str(px), 'quantity': '1.0', 'clientOrderID': 'OTHERBOT' + typ[:4]})
            foreign[row['orderId']] = row
        a, b = self.enter(side, 0), self.enter(side, 1)
        p.adopt_exchange_positions()         # reconciliation sees the foreign 1.0 too
        self.tick()
        self.assertEqual(len(p.open), 2)
        self.assert_protected(side)          # sized to our lots, not the foreign 1.0
        self.assertEqual(p.exchange_order_foreign_count, 2)
        self.assertEqual(p.exchange_order_own_count, len(ex.own_controls()))
        self.assert_status_matches_exchange()
        p.close_pos(a, ex.mark[SYM], 'test-exit')
        p.close_pos(b, ex.mark[SYM], 'test-exit')
        self.tick(passes=2)
        for oid, row in foreign.items():
            self.assertEqual(ex.orders[oid]['status'], 'NEW')
            self.assertEqual(ex.orders[oid]['origQty'], '1.0')
        touched = [b.get('orderId') or b.get('cancelOrderId') for m, path, b in ex.requests if m != 'GET']
        self.assertFalse(set(foreign) & set(map(str, touched)))
        self.assertAlmostEqual(ex.foreign[(SYM, side)], 1.0)
        self.assertEqual(ex.own_controls(), [])
        self.assertEqual(self.status()['expected'], 0)

    @matrix
    def test_08_trailing_moves_stop_only_protectively(self, mode, side):
        p, ex = self.make(mode)
        a = self.enter(side, 0)
        sign = 1 if side == 'LONG' else -1
        old = float(ex.orders[a.sl_oid]['stopPrice'])
        tighter = round(old + sign * .05, 2)   # stays outside the 0.2% mark pad
        self.assertTrue(p.replace_sl(a, tighter))
        self.tick()
        now = float(ex.orders[a.sl_oid]['stopPrice'])
        self.assertAlmostEqual(now, tighter, delta=.011)
        # A looser request never widens the installed stop.
        p.replace_sl(a, round(old - sign * .3, 2))
        self.tick()
        self.assertAlmostEqual(float(ex.orders[a.sl_oid]['stopPrice']), now, delta=.011)
        self.assert_protected(side)
        b = self.enter(side, 1)
        self.tick()
        # A second member keeps its own wider stop: the shared stop is the widest.
        self.assert_protected(side)
        self.assert_status_matches_exchange()

    def _toggle(self, mode, side, to_overall):
        p, ex = self.make(mode)
        for i in range(3):
            self.enter(side, i)
        self.tick()
        self.assert_protected(side)
        own = lambda: {s: sum(r.qty for r in self.ours(s)) for s in SIDES}
        gaps = []
        ex.watch = lambda: gaps.extend(ex.coverage_gaps(own()))
        p.control_orders_overall = to_overall
        self.mode = 'overall' if to_overall else 'per-config'
        for _ in range(4):
            self.tick(seconds=5.0)
        ex.watch = None
        self.assertEqual(gaps, [])
        self.assert_protected(side)
        self.assert_status_matches_exchange()

    @matrix
    def test_09a_toggle_overall_to_per_config(self, mode, side):
        if mode == 'overall':
            self._toggle(mode, side, False)

    @matrix
    def test_09b_toggle_per_config_to_overall(self, mode, side):
        if mode == 'per-config':
            self._toggle(mode, side, True)

    @matrix
    def test_10_controls_off_places_nothing_and_keeps_existing(self, mode, side):
        p, ex = self.make(mode)
        a = self.enter(side, 0)
        existing = {a.sl_oid, a.tp_oid}
        p.control_orders = False
        b = self.enter(side, 1)
        self.tick(passes=3)
        self.assertEqual({o['orderId'] for o in ex.own_controls()}, existing)
        self.assertEqual(self.status()['expected'], 0)
        self.assertEqual(self.status()['gaps'], 0)
        p.close_pos(a, ex.mark[SYM], 'test-exit')
        p.close_pos(b, ex.mark[SYM], 'test-exit')
        self.tick(passes=2)
        self.assertEqual(ex.own_controls(), [])


if __name__ == '__main__':
    unittest.main()
