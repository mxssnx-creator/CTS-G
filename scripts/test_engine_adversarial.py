"""Adversarial engine scenarios against the repo's stateful fake venue.

Each test asserts an invariant an operator expects while the exchange misbehaves
or the engine restarts: a lost order response must not double an entry or stack
a second control, a refused close must back off, an external partial or full
close must leave the controls exact, dust write-off must touch only the dust
lot, recovery must never absorb another source's lot, and adopt must keep each
lot's own entry price.

Not covered on purpose: the Block/DCA add margin gate (available * 0.38) is an
open operator decision, see the pull request text.
"""
import pathlib
import sys
import types
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'server/pulse'))
import pulse_trader as pt
import test_controls_e2e as e2e
import test_foreign_ownership as fo
import test_volume_factor as vf

SYM = e2e.SYM
ORDER = e2e.ORDER
TIMEOUT = {'code': -1, 'msg': 'ReadTimeout', 'error': True}   # exactly what bingx_fast._http returns


class AdvVenue(fo.Venue):
    """Stateful venue plus: lost responses, optional duplicate-cid rejection, leverage endpoints."""

    def __init__(self, clock, mark=100.0):
        super().__init__(clock, mark)
        self.lose_rules = []       # [predicate, remaining]  -> request applied, response replaced by TIMEOUT
        self.reject_dup_cid = False
        self.seen_cids = set()
        self.closed_log = []

    def lose(self, predicate, times=1):
        self.lose_rules.append([predicate, times])

    def _should_lose(self, method, path, body):
        for rule in self.lose_rules:
            if rule[1] and rule[0](method, path, body):
                rule[1] -= 1
                return True
        return False

    def _post(self, path, body):
        body = dict(body)
        cid = body.get('clientOrderID') or body.get('clientOrderId')
        if (self.reject_dup_cid and cid and path.endswith('/trade/order')
                and str(cid).lower() in self.seen_cids):
            self.requests.append(('POST', path, body))
            return {'code': 80016, 'msg': 'clientOrderID already exists'}
        lose = self._should_lose('POST', path, body)
        resp = super()._post(path, body)
        if cid and isinstance(resp, dict) and resp.get('code') == 0 and path.endswith('/trade/order'):
            self.seen_cids.add(str(cid).lower())
        return dict(TIMEOUT) if lose else resp

    def _batch(self, bodies):
        lose = any(self._should_lose('POST', '/openApi/swap/v2/trade/batchOrders', dict(b)) for b in bodies)
        resp = super()._batch(bodies)
        return dict(TIMEOUT) if lose else resp

    def _cancel_replace(self, body):
        lose = self._should_lose('POST', '/openApi/swap/v1/trade/cancelReplace', dict(body))
        resp = super()._cancel_replace(body)
        return dict(TIMEOUT) if lose else resp

    def _delete(self, path, params=None):
        lose = self._should_lose('DELETE', path, dict(params or {}))
        resp = super()._delete(path, params)
        return dict(TIMEOUT) if lose else resp

    # helpers -----------------------------------------------------------------
    def own_orders(self):
        tag = pt.TAG.lower()
        return [o for o in self.orders.values() if str(o.get('clientOrderID', '')).lower().startswith(tag)]

    def live_own_controls(self, side=None):
        return self.own_controls(side)

    def ctrl_cover(self, side):
        sl = sum(float(o['origQty']) - float(o['executedQty']) for o in self.own_controls(side) if o['type'] in e2e.SL_TYPES)
        tp = sum(float(o['origQty']) - float(o['executedQty']) for o in self.own_controls(side) if o['type'] not in e2e.SL_TYPES)
        return sl, tp

    def posts(self, kind=None):
        rows = [(m, p, b) for m, p, b in self.requests if m == 'POST']
        if kind == 'market':
            rows = [r for r in rows if str(r[2].get('type')) == 'MARKET']
        elif kind == 'ctrl':
            rows = [r for r in rows if str(r[2].get('type')) in e2e.CTRL_TYPES]
        return rows


class AdvBase(e2e.ControlsE2E):
    """ControlsE2E.make() wired to AdvVenue with the state the foreign tests add."""

    def adv(self, mode='per-config', n=3):
        p, ex = self.make(mode, n=n)
        venue = AdvVenue(self.clock)
        p.api = venue
        p.px = venue.mark
        p.exchange_foreign_qty = {}
        p.exchange_foreign_order_symbols = set()
        p.lev_map, p.lev_max, p._lev_retry = {}, {}, {}
        p._offline_symbols = set()
        p._persist_lev = lambda: None
        p._load_lev_file = lambda: None
        p._drain_offline_hits = lambda: None
        p.dust_retired = set()
        p.boot_ts = 0
        p.strat_trail = False
        p.ensure_max_leverage = types.MethodType(pt.Pulse.ensure_max_leverage, p)
        guard = patch.object(pt, 'SYMBOLS', [SYM])
        guard.start()
        self.addCleanup(guard.stop)
        self.ex = venue
        self.p = p
        self.mode = mode
        return p, venue

    def restart(self, keep_book=True, keep_pending=True):
        """New engine process on the same data dir + same venue (crash/restart)."""
        q = self.desk.pulse(self.desk.book(3))
        ex = self.ex
        q.api, q.px, q.last_px = ex, ex.mark, {}
        q.control_orders = True
        q.control_orders_per_config = True
        q.control_orders_overall = self.mode == 'overall'
        q.recon_pending = False
        q.bump = lambda *a, **k: None
        q.exchange_foreign_qty = {}
        q.exchange_foreign_order_symbols = set()
        q.lev_map, q.lev_max, q._lev_retry = {}, {}, {}
        q._offline_symbols = set()
        q._persist_lev = lambda: None
        q._load_lev_file = lambda: None
        q._drain_offline_hits = lambda: None
        q.dust_retired = set()
        q.boot_ts = 0
        q.ensure_max_leverage = types.MethodType(pt.Pulse.ensure_max_leverage, q)
        if keep_book:
            q._load_open_book()
        if keep_pending:
            q._load_pending_orders()
        self.p = q
        return q

    def venue_qty(self, side):
        return self.ex.positions.get((SYM, side), 0.0)

    def book_qty(self, side):
        return sum(r.qty for r in self.p.open.values() if r.side == side and self.p.position_is_ours(r))


def w(ex, start=0):
    """Compact list of venue writes since request index ``start``."""
    out = []
    for m, path, b in ex.requests[start:]:
        if m == 'GET':
            continue
        kind = path.split('/')[-1]
        out.append((m, kind, {k: b.get(k) for k in ('type', 'quantity', 'orderId', 'cancelOrderId', 'n', 'clientOrderID')
                              if b.get(k) is not None}))
    return out


class AvgVenue(AdvVenue):
    """Reports the true weighted-average entry of own + foreign quantity, like BingX does."""

    def __init__(self, clock, mark=100.0):
        super().__init__(clock, mark)
        self.cost = {}        # (symbol, side) -> [qty, notional]  (own opening fills only)
        self.foreign_px = {}

    def _market(self, body):
        key = (body['symbol'], body['positionSide'])
        opening = (body['side'] == 'BUY') == (body['positionSide'] == 'LONG')
        resp = super()._market(body)
        if opening and resp.get('code') == 0:
            q = float(resp['data']['order']['executedQty'])
            c = self.cost.setdefault(key, [0.0, 0.0])
            c[0] += q
            c[1] += q * self.mark[body['symbol']]
        return resp

    def get(self, path, params=None):
        resp = super().get(path, params)
        if path.endswith('/user/positions'):
            for row in resp['data']:
                key = (row['symbol'], row['positionSide'])
                own = self.positions.get(key, 0.0)
                foreign = self.foreign.get(key, 0.0)
                c = self.cost.get(key, [0.0, 0.0])
                own_avg = (c[1] / c[0]) if c[0] else self.mark[key[0]]
                notional = own * own_avg + foreign * self.foreign_px.get(key, self.mark[key[0]])
                row['avgPrice'] = str(notional / max(own + foreign, 1e-12))
        return resp


class Crash(Exception):
    pass


def cover(ex, side='LONG'):
    return ex.ctrl_cover(side)


class Adversarial(AdvBase):
    # ---------------------------------------------------------------- F1
    def test_F1_lost_entry_response_keeps_ownership_and_never_doubles_the_lane(self):
        for mode in ('overall', 'per-config'):
            with self.subTest(mode=mode):
                p, ex = self.adv(mode)
                ex.lose(lambda m, path, b: b.get('type') == 'MARKET' and b.get('side') == 'BUY', times=1)
                p.place(SYM, 1, 'trend', .9, selected_set=p.sets.by_idx[0])
                # engine cycle: reconcile then controls
                p.adopt_exchange_positions()
                self.tick(seconds=1.0)
                self.assertEqual(p.exchange_foreign_qty.get(SYM + ':LONG', 0.0), 0.0,
                                 'own accepted-but-lost entry was classified as another source')
                sl, tp = cover(ex)
                self.assertAlmostEqual(sl, self.venue_qty('LONG'), msg='naked / under-covered lot after lost entry response')
                self.assertAlmostEqual(tp, self.venue_qty('LONG'))
                # the 12 s symbol cooldown passes long before the 30 s fill poll: same Set retries the same lane
                self.clock.offset += 13
                p.place(SYM, 1, 'trend', .9, selected_set=p.sets.by_idx[0])
                self.assertAlmostEqual(self.venue_qty('LONG'), 0.05, msg='same lane entered twice after an ambiguous response')

    # ---------------------------------------------------------------- F2
    def test_F2_lost_control_response_neither_duplicates_nor_orphans(self):
        for mode in ('overall', 'per-config'):
            for leg in ('batch', 'STOP_MARKET', 'TAKE_PROFIT_MARKET'):
                with self.subTest(mode=mode, leg=leg):
                    p, ex = self.adv(mode)
                    if leg == 'batch':
                        ex.lose(lambda m, path, b: path.endswith('batchOrders') or b.get('type') == 'STOP_MARKET', times=1)
                    else:
                        ex.lose(lambda m, path, b, leg=leg: b.get('type') == leg, times=1)
                    pos = self.enter('LONG', 0)
                    for _ in range(4):
                        self.tick(seconds=25.0)
                    sl, tp = cover(ex)
                    self.assertAlmostEqual(sl, pos.qty, msg=f'SL cover {sl} != lot {pos.qty} (duplicate)')
                    self.assertAlmostEqual(tp, pos.qty, msg=f'TP cover {tp} != lot {pos.qty} (duplicate)')
                    p.close_pos(pos, ex.mark[SYM], 'test-exit')
                    for _ in range(4):
                        self.tick(seconds=25.0)
                    self.assertEqual([(o['orderId'], o['origQty']) for o in ex.own_controls('LONG')], [],
                                     'orphan own control left on the venue after the lot closed')

    # ---------------------------------------------------------------- F3
    def test_F3_persistently_rejected_close_is_backed_off(self):
        p, ex = self.adv('overall')
        pos = self.enter('LONG', 0)
        self.tick()
        p.px = dict(ex.mark)
        p.px[SYM] = pos.sl * 0.995                       # price is through the stop, venue stop did not fire
        ex.inject(lambda m, path, b: b.get('type') == 'MARKET' and b.get('side') == 'SELL',
                  {'code': 80014, 'msg': 'Invalid parameters'}, times=10 ** 6)
        for _ in range(30):                              # 30 cycles, one simulated second apart
            self.clock.offset += 1.0
            p._oo_cache = {}
            p.manage()
        sent = sum(1 for r in ex.requests if r[0] == 'POST' and r[2].get('type') == 'MARKET' and r[2].get('side') == 'SELL')
        self.assertLessEqual(sent, 6, f'{sent} close orders in 30 cycles against a persistent hard rejection')

    # ---------------------------------------------------------------- F4
    def test_F4_external_partial_close_keeps_controls_exact_without_gaps(self):
        for mode in ('overall', 'per-config'):
            with self.subTest(mode=mode):
                p, ex = self.adv(mode)
                for i in range(3):
                    self.enter('LONG', i)
                self.tick()
                p.adopt_exchange_positions()
                gaps = []
                own = lambda: {'LONG': self.book_qty('LONG'), 'SHORT': 0.0}
                ex.watch = lambda: gaps.extend(ex.coverage_gaps(own()))
                ex.positions[(SYM, 'LONG')] -= 0.06      # someone closes 0.06 of the aggregate by hand
                p.adopt_exchange_positions()
                self.tick(passes=3)
                ex.watch = None
                self.assertEqual(gaps, [], 'a leg was cancelled and its replacement rejected (naked window)')
                sl, tp = cover(ex)
                self.assertAlmostEqual(sl, self.book_qty('LONG'), msg=f'SL cover {sl}')
                self.assertAlmostEqual(tp, self.book_qty('LONG'), msg=f'TP cover {tp}')
                self.assertEqual(len(ex.own_controls('LONG')), 2 if mode == 'overall' else 6,
                                 'duplicate or missing control orders')

    # ---------------------------------------------------------------- F5
    def test_F5_external_flat_leaves_no_orphan_controls(self):
        for mode in ('overall', 'per-config'):
            with self.subTest(mode=mode):
                p, ex = self.adv(mode)
                self.enter('LONG', 0)
                self.enter('LONG', 1)
                self.tick()
                ex.positions[(SYM, 'LONG')] = 0.0        # position closed by hand on the venue
                for _ in range(12):
                    p.adopt_exchange_positions()
                    self.tick(seconds=6.0)
                self.assertFalse(p.open, 'book still holds the lot')
                self.assertEqual([(o['orderId'], o['origQty']) for o in ex.own_controls('LONG')], [],
                                 'own SL/TP survive on the venue after the position vanished')

    # ---------------------------------------------------------------- F6
    def test_F6_dust_writeoff_touches_only_the_dust_lot(self):
        for mode in ('overall', 'per-config'):
            with self.subTest(mode=mode):
                p, ex = self.adv(mode)
                dust = self.enter('LONG', 0)
                healthy = self.enter('LONG', 1)
                short = self.enter('SHORT', 2)
                self.tick()
                p.adopt_exchange_positions()
                short_before = {o['orderId'] for o in ex.own_controls('SHORT')}
                dust.qty = dust.exchange_qty = 0.0001
                ex.positions[(SYM, 'LONG')] = 0.0001 + healthy.qty
                ex.inject(lambda m, path, b: b.get('type') == 'MARKET' and b.get('side') == 'SELL' and b.get('positionSide') == 'LONG',
                          {'code': 101400, 'msg': 'The minimum size for closing an order is 0 USDT'}, times=1)
                p.close_pos(dust, ex.mark[SYM], 'test-exit')
                self.assertEqual({o['orderId'] for o in ex.own_controls('SHORT')}, short_before,
                                 'dust write-off cancelled the opposite hedge side controls')
                self.clock.offset += 60
                for _ in range(3):
                    p.adopt_exchange_positions()
                    self.tick(seconds=5.0)
                self.assertGreater(p.exchange_own_qty.get(SYM + ':LONG', 0.0), 0.0,
                                   'healthy own lot on the same symbol+side was reclassified as foreign')
                sl, tp = cover(ex)
                self.assertAlmostEqual(sl, healthy.qty, msg='healthy lot lost its stop after another lot was written off')
                before = sum(1 for r in ex.requests if r[0] == 'POST' and r[2].get('type') == 'MARKET')
                p.close_pos(healthy, ex.mark[SYM], 'max-hold-6h')
                after = sum(1 for r in ex.requests if r[0] == 'POST' and r[2].get('type') == 'MARKET')
                self.assertEqual(after, before + 1, 'closing the healthy lot only removed it locally; the venue lot stays open')
                self.assertAlmostEqual(ex.positions[(SYM, 'LONG')], 0.0001, places=6)

    # ---------------------------------------------------------------- F7
    def test_F7_recovery_never_absorbs_a_foreign_lot(self):
        for mode in ('overall', 'per-config'):
            with self.subTest(mode=mode):
                p, ex = self.adv(mode)
                ex.foreign[(SYM, 'LONG')] = 0.04
                orig = p.record_event

                def boom(kind, key, **kw):
                    if kind == 'exchange_response' and kw.get('detail') == 'entry market order' and kw.get('status') == 'confirmed':
                        raise Crash()
                    return orig(kind, key, **kw)
                p.record_event = boom
                with self.assertRaises(Crash):
                    p.place(SYM, 1, 'trend', .9, selected_set=p.sets.by_idx[0])
                q = self.restart()
                q.reconcile_startup_positions()
                self.tick(passes=2)
                self.assertAlmostEqual(self.book_qty('LONG'), 0.05, msg='recovered book quantity includes the foreign lot')
                sl, tp = cover(ex)
                self.assertAlmostEqual(sl, 0.05, msg='controls are sized over the foreign lot')
                self.assertEqual([o for o in ex.own_controls('LONG') if str(o.get('closePosition')).lower() == 'true'], [],
                                 'closePosition control next to a foreign lot')

    def test_F7b_recovery_from_a_lost_book_adopts_the_live_pair_instead_of_stacking_another(self):
        for mode in ('overall', 'per-config'):
            with self.subTest(mode=mode):
                p, ex = self.adv(mode)
                self.enter('LONG', 0)
                self.enter('LONG', 1)
                self.tick()
                q = self.restart(keep_book=False)
                q.reconcile_startup_positions()
                self.tick(passes=3)
                sl, tp = cover(ex)
                self.assertAlmostEqual(sl, self.venue_qty('LONG'), msg=f'SL cover {sl} vs lot {self.venue_qty("LONG")}')
                self.assertAlmostEqual(tp, self.venue_qty('LONG'), msg=f'TP cover {tp}')


def _sf_(v):
    try:
        return float(v or 0)
    except Exception:
        return 0.0


class PendingAdds(AdvBase):
    # ---------------------------------------------------------------- F10
    def test_F10_pending_dca_row_is_seen_by_the_guard_in_overall_dca_mode(self):
        for mode in ('overall', 'per-config'):
            with self.subTest(mode=mode):
                p, ex = self.adv(mode)
                pos = self.enter('LONG', 0)
                p.dca_overall = True                     # shipped default; the row carries group_key == ""
                p._remember_pending(kind='dca', cid=pt.TAG + 'd-dca-0001', symbol=SYM, side='LONG',
                                    requested_qty=0.03, group_key='', metadata={})
                self.assertTrue(p._pending_add_open(pos, 'dca'),
                                'an unresolved DCA add is invisible to _pending_add_open')


class EntryPrice(AdvBase):
    # ---------------------------------------------------------------- F9
    def test_F9_adopt_keeps_each_lots_own_entry_price(self):
        for mode in ('overall', 'per-config'):
            with self.subTest(mode=mode):
                p, ex = self.adv(mode)
                av = AvgVenue(self.clock)
                p.api, p.px, self.ex = av, av.mark, av
                a = self.enter('LONG', 0)                 # filled at 100
                av.mark[SYM] = 102.0
                b = self.enter('LONG', 1)                 # filled at 102
                av.foreign[(SYM, 'LONG')] = 0.10          # another source holds 0.10 at 90
                av.foreign_px[(SYM, 'LONG')] = 90.0
                p.adopt_exchange_positions()
                self.assertAlmostEqual(a.entry, 100.0, places=6, msg='lot A entry replaced by the aggregate venue average')
                self.assertAlmostEqual(b.entry, 102.0, places=6, msg='lot B entry replaced by the aggregate venue average')


class AddPaths(vf.VolumeFactorAdds):
    def test_F1c_lost_dca_response_never_leaves_venue_and_book_apart(self):
        class Lossy(vf._Api):
            def __init__(self, px):
                super().__init__(px)
                self.venue = 0.0
                self.first = True

            def post(self, path, body):
                resp = super().post(path, body)
                self.venue += float(body['quantity'])
                if self.first:
                    self.first = False
                    return dict(TIMEOUT)
                return resp

        p = self.pulse(1.)
        p.api = Lossy(self.PX)
        # The fixture's ids lack the engine tag, which makes _remember_pending a no-op; use real ones.
        p.cid = lambda kind='o', pos=None, **kw: pt.TAG + f'{kind}dca{len(p.api.posts):04d}'
        p.strat_dca, p.dca_overall, p.dca_last_emit, p.dca_fail_cd = True, True, 0., {}
        p.overall_side_closes = lambda *a: []
        p.per_config_controls = lambda pos: True
        p.position_key = lambda pos: 'k'
        p._apply_position_fill = lambda pos, filled, avg, **k: setattr(pos, 'qty', pos.qty + filled)
        pos = self.parent(p, set_id='s', px=98.7)
        p.maybe_dca_adds()
        first = p.api.venue
        # The unconfirmed add must stay on record (so the fill poll can book it) ...
        self.assertEqual(sum(_sf_(r.get('requested_qty')) for r in p.pending_orders.values()), first)
        # ... and, once the failure back-off elapses, must block a second add on the same lane.
        p.dca_fail_cd.clear()
        p.dca_last_emit = 0
        p.maybe_dca_adds()
        self.assertAlmostEqual(p.api.venue, first, places=9, msg='the same DCA step fired again while its first add was unconfirmed')
