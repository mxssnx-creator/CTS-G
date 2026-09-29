"""The engine touches only what this system placed.

Operator rule: orders, positions and settings that belong to other sources
(manual orders, other bots) are never cancelled, closed or altered. Every
scenario drives the real Pulse methods against the stateful fake venue and
checks the venue's truth, not the engine's bookkeeping.

Covered: single-order cancels, close fallbacks that would flatten the whole
exchange position (closePosition / positionId), legacy aggregate control
orders, and the per-symbol leverage / margin-mode settings.
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

SYM = e2e.SYM
LEVERAGE = '/openApi/swap/v2/trade/leverage'
MARGIN = '/openApi/swap/v2/trade/marginType'
MARKET_ORDER = e2e.ORDER


class Venue(e2e.FakeBingX):
    """Fake venue where whole-position closes also take another source's lot."""

    def _post(self, path, body):
        if path.endswith('/trade/leverage') or path.endswith('/trade/marginType'):
            self.requests.append(('POST', path, dict(body)))
            return {'code': 0}
        return super()._post(path, body)

    def _market(self, body):
        key = (body['symbol'], body['positionSide'])
        opening = (body['side'] == 'BUY') == (body['positionSide'] == 'LONG')
        if not opening and (str(body.get('closePosition')).lower() == 'true' or body.get('positionId')):
            self.foreign[key] = 0.0
        return super()._market(body)

    def sent(self, path_suffix):
        return [b for m, p, b in self.requests if m == 'POST' and p.endswith(path_suffix)]

    def close_requests(self):
        return [b for m, p, b in self.requests
                if m == 'POST' and p == MARKET_ORDER and b.get('type') == 'MARKET' and b.get('side') == 'SELL']


class ForeignOwnership(unittest.TestCase):
    def make(self, per_config=False):
        helper = e2e.ControlsE2E('make')
        self.addCleanup(helper.doCleanups)
        p, _ = helper.make('per-config')
        venue = Venue(helper.clock)
        p.api = venue
        p.px = venue.mark
        p.control_orders_per_config = per_config
        p.control_orders_overall = False
        # State the harness desk does not initialise but the engine keeps.
        p.exchange_foreign_qty = {}
        p.exchange_foreign_order_symbols = set()
        p.lev_map, p.lev_max, p._lev_retry = {}, {}, {}
        p._offline_symbols = set()
        p._persist_lev = lambda: None
        p._load_lev_file = lambda: None
        p._drain_offline_hits = lambda: None
        # The harness desk stubs the leverage call; these tests need the real one.
        p.ensure_max_leverage = types.MethodType(pt.Pulse.ensure_max_leverage, p)
        guard = patch.object(pt, 'SYMBOLS', [SYM])
        guard.start()
        self.addCleanup(guard.stop)
        self.helper, self.p, self.venue = helper, p, venue
        helper.p, helper.ex = p, venue
        return p, venue

    def hold_foreign_lot(self, row, qty=0.4):
        """Another source holds ``qty`` more on the same symbol and hedge side."""
        self.venue.foreign[(row.symbol, row.side)] = qty
        self.p.exchange_foreign_qty[f'{row.symbol}:{row.side}'] = qty
        row.foreign_qty = qty

    # -- ownership detection ------------------------------------------------
    def test_foreign_exposure_is_seen_from_positions_and_from_orders(self):
        p, _ = self.make()
        self.assertFalse(p.symbol_has_foreign_exposure(SYM))
        p.exchange_foreign_qty = {f'{SYM}:SHORT': 0.7}
        self.assertTrue(p.symbol_has_foreign_exposure(SYM))
        self.assertTrue(p.symbol_has_foreign_exposure(SYM.lower()))
        self.assertFalse(p.symbol_has_foreign_exposure('OTHER-USDT'))
        p.exchange_foreign_qty = {f'{SYM}:SHORT': 0.0}
        self.assertFalse(p.symbol_has_foreign_exposure(SYM))
        p.exchange_foreign_order_symbols = {'ABC-USDT'}
        self.assertTrue(p.symbol_has_foreign_exposure('ABC-USDT'))
        self.assertFalse(p.symbol_has_foreign_exposure(''))

    def test_open_order_snapshot_records_the_symbols_of_foreign_orders(self):
        p, venue = self.make()
        base = dict(status='NEW', type='LIMIT', positionSide='LONG', side='BUY', origQty='1', executedQty='0')
        venue.orders['9001'] = dict(base, orderId='9001', symbol='MAN-USDT', clientOrderID='manual-1')
        venue.orders['9002'] = dict(base, orderId='9002', symbol='MANUAL2-USDT', clientOrderID='')
        venue.orders['9003'] = dict(base, orderId='9003', symbol='OWN-USDT', clientOrderID=pt.TAG + 'o00000000000001')
        p._oo_cache = {}
        p.list_orders()
        self.assertEqual(p.exchange_foreign_order_symbols, {'MAN-USDT', 'MANUAL2-USDT'})
        self.assertTrue(p.symbol_has_foreign_exposure('MAN-USDT'))
        self.assertFalse(p.symbol_has_foreign_exposure('OWN-USDT'))

    def test_cancel_never_reaches_a_foreign_order(self):
        p, venue = self.make()
        venue.orders['9001'] = dict(orderId='9001', symbol=SYM, status='NEW', type='STOP_MARKET', positionSide='LONG',
                                    side='SELL', origQty='1', executedQty='0', stopPrice='90', clientOrderID='manual-stop')
        venue.orders['9002'] = dict(orderId='9002', symbol=SYM, status='NEW', type='STOP_MARKET', positionSide='LONG',
                                    side='SELL', origQty='1', executedQty='0', stopPrice='90', clientOrderID='')
        p._oo_cache = {}
        self.assertFalse(p.cancel_order(SYM, '9001'))
        self.assertFalse(p.cancel_order(SYM, '9002'))
        p.cancel_controls(SYM)
        self.assertEqual(venue.orders['9001']['status'], 'NEW')
        self.assertEqual(venue.orders['9002']['status'], 'NEW')
        self.assertEqual([r for r in venue.requests if r[0] == 'DELETE'], [])

    # -- closes -------------------------------------------------------------
    def close_with_first_form_refused(self, foreign):
        p, venue = self.make(per_config=False)
        row = self.helper.enter('LONG', 0)
        venue.positions[(SYM, 'LONG')] = row.qty
        if foreign:
            self.hold_foreign_lot(row)
        venue.inject(lambda m, path, b: b.get('type') == 'MARKET' and b.get('side') == 'SELL' and b.get('quantity')
                     and not b.get('closePosition') and not b.get('positionId'),
                     {'code': 100400, 'msg': 'rejected'}, times=3)
        ok, _ = p.market_close(row)
        return p, venue, row, ok

    def test_a_refused_close_never_falls_back_to_closing_the_whole_position(self):
        _, venue, row, ok = self.close_with_first_form_refused(foreign=True)
        self.assertFalse(ok)
        for body in venue.close_requests():
            self.assertNotIn('closePosition', body)
            self.assertNotIn('positionId', body)
        self.assertAlmostEqual(venue.foreign[(SYM, 'LONG')], 0.4)

    def test_without_foreign_exposure_the_whole_position_fallback_still_works(self):
        _, venue, row, ok = self.close_with_first_form_refused(foreign=False)
        self.assertTrue(ok)
        self.assertTrue(any(str(b.get('closePosition')).lower() == 'true' for b in venue.close_requests()))

    def test_a_quantity_close_leaves_the_foreign_lot_alone(self):
        p, venue = self.make(per_config=False)
        row = self.helper.enter('LONG', 0)
        venue.positions[(SYM, 'LONG')] = row.qty
        self.hold_foreign_lot(row)
        ok, _ = p.market_close(row)
        self.assertTrue(ok)
        self.assertAlmostEqual(venue.positions[(SYM, 'LONG')], 0.0)
        self.assertAlmostEqual(venue.foreign[(SYM, 'LONG')], 0.4)

    # -- legacy aggregate controls ------------------------------------------
    def replace_controls(self, foreign):
        p, venue = self.make(per_config=False)
        row = self.helper.enter('LONG', 0)
        venue.positions[(SYM, 'LONG')] = row.qty
        if foreign:
            self.hold_foreign_lot(row)
        p.cancel_controls(SYM, pos=row)
        p.clear_position_controls(row)
        before = {o['orderId'] for o in venue.open_orders()}
        self.helper.tick(passes=3)
        fresh = [o for o in venue.own_controls('LONG') if o['orderId'] not in before]
        return p, venue, row, fresh

    def test_aggregate_controls_never_use_close_position_next_to_a_foreign_lot(self):
        _, venue, row, fresh = self.replace_controls(foreign=True)
        self.assertTrue(fresh, 'protection was not re-placed')
        for order in fresh:
            self.assertNotEqual(str(order.get('closePosition')).lower(), 'true')
            self.assertGreater(float(order['origQty']), 0.0)
            self.assertLessEqual(float(order['origQty']), row.qty + 1e-9)
        self.assertAlmostEqual(venue.foreign[(SYM, 'LONG')], 0.4)

    def test_aggregate_controls_still_close_the_position_when_all_of_it_is_ours(self):
        _, _, _, fresh = self.replace_controls(foreign=False)
        self.assertTrue(fresh)
        self.assertTrue(any(str(o.get('closePosition')).lower() == 'true' for o in fresh))

    # -- leverage and margin mode ---------------------------------------------
    def test_leverage_and_margin_mode_stay_untouched_next_to_foreign_exposure(self):
        for label, arrange in (
            ('position', lambda p: p.exchange_foreign_qty.update({f'{SYM}:SHORT': 0.7})),
            ('order', lambda p: setattr(p, 'exchange_foreign_order_symbols', {SYM})),
        ):
            with self.subTest(label):
                p, venue = self.make()
                p.lev_map.pop(SYM, None)
                arrange(p)
                p.ensure_max_leverage(SYM, force=True)
                p.set_leverage()
                self.assertEqual(venue.sent('/trade/leverage'), [])
                self.assertEqual(venue.sent('/trade/marginType'), [])

    def test_leverage_is_still_set_to_max_when_nobody_else_is_on_the_symbol(self):
        p, venue = self.make()
        p.lev_map.pop(SYM, None)
        p.ensure_max_leverage(SYM, force=True)
        self.assertEqual({b['side'] for b in venue.sent('/trade/leverage')}, {'LONG', 'SHORT'})
        self.assertTrue(venue.sent('/trade/marginType'))

    def test_the_max_leverage_sweep_skips_a_symbol_another_source_holds(self):
        p, venue = self.make()
        p.lev_map.pop(SYM, None)
        p.lev_max.pop(SYM, None)
        p.exchange_foreign_qty[f'{SYM}:LONG'] = 1.0
        for _ in range(3):
            p.set_leverage()
        self.assertEqual(venue.sent('/trade/leverage'), [])


if __name__ == '__main__':
    unittest.main()
