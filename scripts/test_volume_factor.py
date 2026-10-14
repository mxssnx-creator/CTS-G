"""volumeFactor end to end: overlay load, entry, Block/DCA adds and controls, offline."""
import pathlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server/pulse'))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pulse_trader as pt
import storage_paths
from block_engine import BlockBook, parse_block_count
from dca_engine import DcaBook
from set_engine import SetBook, SetState
import test_all_valid_entries as fixtures


class _StopAfterVolume(Exception):
    """Raised by the first setting read after targetNotional/volumeFactor."""


class _LevMap(dict):
    def __bool__(self):
        raise _StopAfterVolume


class _Api:
    def __init__(self, px):
        self.posts, self.path_cd, self.px = [], {}, px

    def post(self, path, body):
        self.posts.append(dict(body))
        return {'code': 0, 'data': {'order': {'orderId': f'o{len(self.posts)}', 'avgPrice': str(self.px),
                                              'executedQty': str(body.get('quantity'))}}}

    def get(self, path, params=None):
        return {'code': 0, 'data': []}


class VolumeFactorOverlayLoad(unittest.TestCase):
    def load(self, overlay):
        p = pt.Pulse.__new__(pt.Pulse)
        p.api = NS()
        p.lev_map = _LevMap()
        with patch.object(pt, 'dump_cts_settings', return_value={}), \
                patch.object(pt, 'load_json_file', return_value=dict(overlay)), \
                patch.object(pt.redis_config, 'configure'), \
                patch.object(storage_paths, 'configure_retention'), \
                patch.object(pt, 'TARGET_NOTIONAL', 2.15):
            with self.assertRaises(_StopAfterVolume):
                p.apply_live_config(initial=True)
            return pt.TARGET_NOTIONAL, p.volume_factor

    def test_saved_factor_and_target_are_applied_within_engine_bounds(self):
        self.assertEqual(self.load({'volumeFactor': 2.5, 'targetNotional': 3}), (3.0, 2.5))
        self.assertEqual(self.load({'volumeFactor': 99, 'targetNotional': 9999}), (500.0, 10.0))
        self.assertEqual(self.load({'volumeFactor': 0.01, 'targetNotional': 0.01}), (0.2, 0.05))
        # Missing or zero factor means the shared default 0.1, as on the desk.
        self.assertEqual(self.load({}), (2.15, 0.1))
        self.assertEqual(self.load({'volumeFactor': 0}), (2.15, 0.1))

    def test_nonfinite_or_corrupt_values_never_saturate_to_the_maximum(self):
        # write_overlay rejects float NaN, but a JSON string still round-trips
        # and float("nan") passes min(): it must not become 10x / 500 USDT.
        for bad in ('nan', 'NaN', 'inf', '-Infinity', 'abc', None):
            target, factor = self.load({'volumeFactor': bad, 'targetNotional': bad})
            self.assertEqual(factor, 0.1, bad)
            self.assertEqual(target, 2.15, bad)


class VolumeFactorEntries(unittest.TestCase):
    def place(self, vf, contract, px, block=False, dca=False):
        t = fixtures.AllValidEntries()
        p = t.pulse(t.book(1))
        for name in ('size_qty', 'max_book_notional', 'avail_notional'):
            p.__dict__.pop(name, None)  # the real sizing chain, not fixture stubs
        p.volume_factor = vf
        p.order_sizing = 'factor'  # these tests pin the volume-factor path
        p.vol1h = {}
        p.coord.size_mult = lambda n: 1.0
        p.block = NS(enabled=block, max_stack=6, volume_ratio=.25, max_volume_multiplier=2.0,
                     register_parent=Mock(), on_parent_close=Mock())
        p.dca = NS(enabled=dca, max_steps=4, _mult_at=lambda i: [1.5, 2, 2.3, 2.5][i],
                   attach=Mock(), on_close=Mock(), drop=Mock())
        p.contracts = {'X-USDT': contract}
        p.px = {'X-USDT': px}
        p.place('X-USDT', 1, 'gen:trend', .9, selected_set=p.sets.by_idx[0])
        entries = [float(b['quantity']) for b in p.api.posts if b.get('type') == 'MARKET']
        controls = {float(b['quantity']) for batch in p.api.batches for b in batch}
        pos = next(iter(p.open.values()), None)
        return entries, controls, pos

    def test_entry_and_control_quantity_follow_the_factor_once(self):
        lot = pt.Contract('X-USDT', .001, .001, 3, 2, .1, 100)
        for vf, want in ((.5, .011), (1., .022), (2., .043), (4., .086)):
            entries, controls, pos = self.place(vf, lot, 100., block=True, dca=True)
            self.assertEqual(entries, [want], vf)
            self.assertEqual(controls, {want}, vf)
            self.assertAlmostEqual(pos.qty, want)

    def test_step_rounding_is_not_dropped_when_no_add_on_room_is_configured(self):
        # Block and DCA off: book room equals the target. The venue step lifts
        # 2.15 USDT to 2.20; that rounding must not cancel the whole entry.
        lot = pt.Contract('X-USDT', .001, .001, 3, 2, .1, 100)
        for vf, want in ((1., .022), (1.5, .033), (2., .043)):
            entries, controls, pos = self.place(vf, lot, 100.)
            self.assertEqual(entries, [want], vf)
            self.assertEqual(controls, {want}, vf)

    def test_min_lot_parent_above_the_book_room_is_raised_to_venue_min(self):
        # Volume is always raised to the exchange minimum lot: an 8 USDT
        # venue minimum above a small book room still trades one min lot.
        btc = pt.Contract('X-USDT', .0001, .0001, 4, 1, 2., 100)
        self.assertEqual(self.place(1., btc, 80000.)[0], [.0001])
        self.assertEqual(self.place(1., btc, 80000., block=True)[0], [.0001])
        self.assertEqual(self.place(1., btc, 80000., block=True, dca=True)[0], [.0001])


class VolumeFactorAdds(unittest.TestCase):
    PX = 100.3

    def pulse(self, vf):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = pt.Pulse.__new__(pt.Pulse)
        p.api = _Api(self.PX)
        p.halted = False
        p.volume_factor = vf
        p.order_sizing = 'factor'  # these tests pin the volume-factor path
        p.vol1h = {}
        p.available = 1000.
        p.coord = NS(min_pf=1.1, real_eval=3, last={}, size_mult=lambda n: 1.,
                     add_gate=lambda *a, **k: (True, [], {'lastPf': 1.2}), add_stack_cap=lambda s, pf: s)
        p.block = BlockBook(tmp.name + '/block.json', {
            'variantBlockEnabled': True, 'blockMaxStack': 6, 'blockVolumeRatio': .25,
            'blockProfitFactorRatio': 1.1, 'defaultMinPF': 1.2,
            'blockActiveRealEnabled': True, 'blockActiveLiveEnabled': True})
        p.dca = DcaBook()
        p.dca.load({'dcaEnabled': True, 'dcaMaxSteps': 4, 'dcaStepDistancesPct': [.5, 1, 1.5, 2],
                    'dcaStepVolumeMultipliers': [1.5, 2, 2.3, 2.5], 'dcaCooldownSeconds': 0})
        p.contracts = {'TST-USDT': pt.Contract('TST-USDT', .0001, .0001, 4, 2, .1, 100)}
        p.px = {'TST-USDT': self.PX}
        p.last_px = {}
        p.lev_map = p.lev_max = {'TST-USDT': 100}
        p.leverage_for = lambda c: 100
        p.sl_min, p.sl_max, p.tp_min, p.tp_max = .001, .02, .002, .05
        p.position_cost_pct, p.tp_cost_ratio = .15, 1.5
        p.exits = NS(enabled=False, opt_sl_min=.001, opt_sl_max=.009)
        p.pending_orders = {}
        p._save_pending_orders = lambda: None
        p.seen_fill_cids = set()
        p.skip_log = {}
        p.cooldown = {}
        p.errors = 0
        p.last_error = ''
        p.did_io = False
        p.entries_blocked = lambda: False
        p.missing_controls = lambda pos: False
        p.ensure_controls = lambda pos: None
        p.save_open_book = lambda: None
        p._coord_add_state = lambda **k: (True, 6, 1.8, [])
        p.live_recent_pf = lambda *a, **k: None
        p.cid = lambda kind='o', pos=None, **kw: f'GTEST{kind}{len(p.api.posts)}'
        p.ok = lambda r: r.get('code') == 0
        p.closed = []
        p.open = {}
        return p

    def parent(self, p, set_id='parent-config', px=None):
        qty = p.size_qty(p.contracts['TST-USDT'], self.PX)
        pos = pt.Position(symbol='TST-USDT', side='LONG', qty=qty, entry=100., opened_at=time.time() - 600,
                          sl=98., tp=101., peak=100., set_id=set_id, pack='general', sl_pct=.02)
        p.open = {'TST-USDT': pos}
        if px:
            p.px = {'TST-USDT': px}
        return pos

    def block_add(self, vf):
        p = self.pulse(vf)
        p.strat_block = True
        p.block_active = True
        p.block_overall = True
        p.block_last_emit = 0.
        p.control_orders = False
        st = SetState(id='parent-config', pack='general', tf='1m', sl_ratio=.6, trail_key='',
                      trail_arm=0, trail_give=0, last15_ratio=1.5, last15_n=12)
        p.sets = SetBook()
        p.sets.sets = {st.id: st}
        p.score = lambda sym: (1, 't', .9)
        p.indications = NS(best=lambda s: None, primary=lambda s: None)
        p.block_overall_real_pf = lambda *a, **k: 1.5
        pos = self.parent(p)
        parent = pos.qty
        p.block.register_parent('TST-USDT', 'LONG', parent, 100.)
        p.maybe_block_adds()
        return p, pos, parent

    def test_block_active_add_fill_is_recorded_on_the_position(self):
        p, pos, parent = self.block_add(1.)
        self.assertEqual(len(p.api.posts), 1, p.api.posts)
        added = float(p.api.posts[0]['quantity'])
        lane = p.block.lanes['TST-USDT:LONG']
        self.assertAlmostEqual(pos.qty, parent + added)
        self.assertAlmostEqual(lane.confirmed_add, added)
        self.assertEqual(len(lane.legs), 1)
        self.assertEqual(parse_block_count(lane.legs[0].set_key), 1)
        self.assertEqual(p.pending_orders, {})

    def test_block_and_dca_adds_scale_with_the_parent_not_again(self):
        blocks, dcas = {}, {}
        for vf in (1., 2., 4.):
            p, pos, parent = self.block_add(vf)
            blocks[vf] = (parent, float(p.api.posts[0]['quantity']))
            p = self.pulse(vf)
            p.strat_dca = True
            p.dca_overall = True
            p.dca_last_emit = 0.
            p.dca_fail_cd = {}
            p.overall_side_closes = lambda *a: []
            p.per_config_controls = lambda pos: True
            p.position_key = lambda pos: 'k'
            p._pending_add_open = lambda pos, kind: False
            p._apply_position_fill = lambda pos, filled, avg, **k: setattr(pos, 'qty', pos.qty + filled)
            pos = self.parent(p, set_id='s', px=98.7)  # 1.3% adverse: first DCA step
            parent = pos.qty
            p.maybe_dca_adds()
            dcas[vf] = (parent, float(p.api.posts[0]['quantity']))
        for vf in (2., 4.):
            self.assertAlmostEqual(blocks[vf][0] / blocks[1.][0], vf, delta=.02 * vf)
            self.assertAlmostEqual(dcas[vf][0] / dcas[1.][0], vf, delta=.02 * vf)
        for parent, add in blocks.values():
            self.assertAlmostEqual(add / parent, .25, delta=.01)
        for parent, add in dcas.values():
            self.assertAlmostEqual(add / parent, 1.5, delta=.01)


if __name__ == '__main__':
    unittest.main()


class MicroCapEntries(unittest.TestCase):
    """A Micro entry trades one venue-minimum lot, capped to microMaxShare x maxOpen."""

    def place_micro(self, open_micro, share=0.25, max_open=8):
        t = fixtures.AllValidEntries()
        p = t.pulse(t.book(1))
        for name in ('size_qty', 'max_book_notional', 'avail_notional'):
            p.__dict__.pop(name, None)
        p.volume_factor = 1.0
        p.order_sizing = 'factor'  # these tests pin the volume-factor path
        p.vol1h = {}
        p.coord.size_mult = lambda n: 1.0
        p.block = NS(enabled=False, max_stack=0, volume_ratio=.25, max_volume_multiplier=2.0,
                     register_parent=Mock(), on_parent_close=Mock())
        p.dca = NS(enabled=False, max_steps=0, _mult_at=lambda i: 1.0, attach=Mock(), on_close=Mock(), drop=Mock())
        p.contracts = {'X-USDT': pt.Contract('X-USDT', .001, .001, 3, 2, .1, 100)}
        p.px = {'X-USDT': 100.}
        p.sets.micro_tier = lambda st, side: True
        p.sets.micro_max_share = share
        for k in range(open_micro):
            p.open[f'm{k}'] = NS(micro=True, symbol=f'M{k}-USDT', side='LONG')
        with patch.object(pt, 'MAX_OPEN', max_open):
            p.place('X-USDT', 1, 'gen:trend', .9, selected_set=p.sets.by_idx[0])
        return [float(b['quantity']) for b in p.api.posts if b.get('type') == 'MARKET'], p

    def test_micro_below_cap_trades_one_minimum_lot(self):
        entries, p = self.place_micro(open_micro=1)
        self.assertEqual(entries, [.001])
        pos = next(v for k, v in p.open.items() if not str(k).startswith('m'))
        self.assertTrue(pos.micro)

    def test_micro_at_cap_is_skipped(self):
        entries, p = self.place_micro(open_micro=2)
        self.assertEqual(entries, [])
        self.assertIn('micro cap 2/2', str(p._execution_decision.get('reason')))

    def test_micro_share_zero_blocks_micro(self):
        entries, _ = self.place_micro(open_micro=0, share=0.0)
        self.assertEqual(entries, [])


class LotsPerSymbolSideCap(unittest.TestCase):
    def place_with(self, open_same, cap):
        t = fixtures.AllValidEntries()
        p = t.pulse(t.book(1))
        for name in ('size_qty', 'max_book_notional', 'avail_notional'):
            p.__dict__.pop(name, None)
        p.volume_factor = 1.0
        p.order_sizing = 'factor'  # these tests pin the volume-factor path
        p.vol1h = {}
        p.coord.size_mult = lambda n: 1.0
        p.block = NS(enabled=False, max_stack=0, volume_ratio=.25, max_volume_multiplier=2.0,
                     register_parent=Mock(), on_parent_close=Mock())
        p.dca = NS(enabled=False, max_steps=0, _mult_at=lambda i: 1.0, attach=Mock(), on_close=Mock(), drop=Mock())
        p.contracts = {'X-USDT': pt.Contract('X-USDT', .001, .001, 3, 2, .1, 100)}
        p.px = {'X-USDT': 100.}
        for k in range(open_same):
            p.open[f's{k}'] = NS(symbol='X-USDT', side='LONG', ours=True, micro=False)
        p.open['other'] = NS(symbol='X-USDT', side='SHORT', ours=True, micro=False)
        with patch.object(pt, 'MAX_LOTS_PER_SIDE', cap):
            p.place('X-USDT', 1, 'gen:trend', .9, selected_set=p.sets.by_idx[0])
        return [b for b in p.api.posts if b.get('type') == 'MARKET']

    def test_cap_blocks_another_lot_on_the_same_signal_only(self):
        self.assertEqual(self.place_with(open_same=2, cap=2), [])
        self.assertEqual(len(self.place_with(open_same=1, cap=2)), 1)
        self.assertEqual(len(self.place_with(open_same=5, cap=0)), 1)  # 0 = unlimited
