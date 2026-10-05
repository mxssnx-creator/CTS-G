"""Exchange minimums systemwide: lots raised to the venue minimum, SL never
tighter than the venue accepts, and the intern Set replay uses the same
per-symbol SL floor as the live entry path."""
import os
import pathlib
import sys
import tempfile
import unittest

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-venue-min-test-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from types import SimpleNamespace as NS  # noqa: E402

import pulse_trader as pt  # noqa: E402
import set_engine as se  # noqa: E402
from position_cost import venue_sl_floor  # noqa: E402


def contract(sym="X-USDT", pprec=4, min_qty=1.0, step=1.0, min_usdt=2.0, max_lev=50):
    return pt.Contract(symbol=sym, min_qty=min_qty, step=step, qprec=0, pprec=pprec, min_usdt=min_usdt, max_lev=max_lev)


def lite(**kw):
    ns = NS(sl_min=0.004, sl_max=0.03, venue_sl_ticks=3.0, sl_learned={}, lev_max={}, **kw)
    for name in ("venue_sl_min", "learn_sl_floor", "leverage_for", "round_qty_up", "min_order_qty", "raise_to_min_qty"):
        setattr(ns, name, getattr(pt.Pulse, name).__get__(ns))
    return ns


class VenueSlFloorTests(unittest.TestCase):
    def test_desk_floor_holds_for_fine_ticks(self):
        self.assertAlmostEqual(venue_sl_floor(100.0, 0.01, 0.004), 0.004)

    def test_tick_floor_lifts_coarse_low_priced_symbols(self):
        # 3 ticks of 0.001 on a 0.5 price is 0.6%, above the 0.4% desk floor
        self.assertAlmostEqual(venue_sl_floor(0.5, 0.001, 0.004, leverage=50), 0.006)

    def test_floor_stays_inside_liquidation(self):
        # 3 ticks would be 3%; at 100x the stop must fire before ~0.9%
        self.assertAlmostEqual(venue_sl_floor(0.1, 0.001, 0.004, leverage=100), 0.009)

    def test_desk_floor_is_never_lowered_by_leverage(self):
        self.assertAlmostEqual(venue_sl_floor(100.0, 0.01, 0.004, leverage=1000), 0.004)

    def test_learned_floor_applies(self):
        self.assertAlmostEqual(venue_sl_floor(100.0, 0.01, 0.004, learned=0.005, leverage=50), 0.005)


class TraderVenueMinTests(unittest.TestCase):
    def test_trader_floor_uses_contract_tick(self):
        p = lite()
        c = contract(pprec=3, max_lev=50)
        self.assertAlmostEqual(p.venue_sl_min(c, 0.5), 0.006)
        self.assertAlmostEqual(p.venue_sl_min(None, 0.5), 0.004)

    def test_rejection_widens_the_learned_floor(self):
        p = lite()
        c = contract(pprec=4, max_lev=50)
        before = p.venue_sl_min(c, 100.0)
        p.learn_sl_floor(c, before)
        self.assertGreater(p.venue_sl_min(c, 100.0), before)
        for _ in range(20):
            p.learn_sl_floor(c, 1.0)
        self.assertLessEqual(p.sl_learned[c.symbol], p.sl_max)

    def test_qty_raised_to_exchange_minimum(self):
        p = lite()
        c = contract(min_qty=1.0, step=1.0, min_usdt=5.0)
        # a 0.1 volume-factor target of 0.3 lots is raised to the 5 USDT floor
        self.assertEqual(p.raise_to_min_qty(c, 1.0, 0.3), 5.0)
        self.assertEqual(p.raise_to_min_qty(c, 1.0, 7.0), 7.0)


class SetReplayFloorTests(unittest.TestCase):
    def test_pair_sl_tp_uses_symbol_floor_only_in_scope(self):
        book = se.SetBook()
        book.sl_min, book.sl_max = 0.004, 0.03
        book.set_symbol_sl_floors({"X-USDT": 0.008})
        free_sl, _ = book.pair_sl_tp(0.005, 0.6)
        with book.sl_scope("X-USDT"):
            sl, tp = book.pair_sl_tp(0.005, 0.6)
        self.assertAlmostEqual(free_sl, 0.004)
        self.assertAlmostEqual(sl, 0.008)
        self.assertGreaterEqual(tp * 0.6 + 1e-12, sl)
        with book.sl_scope("Y-USDT"):
            self.assertAlmostEqual(book.pair_sl_tp(0.005, 0.6)[0], 0.004)

    def test_floors_merge_and_survive_replay_clone(self):
        book = se.SetBook()
        book.set_symbol_sl_floors({"A": 0.005})
        book.set_symbol_sl_floors({"B": 0.006})
        self.assertEqual(book.sym_sl_floor, {"A": 0.005, "B": 0.006})
        self.assertEqual(book.replay_clone([]).sym_sl_floor, {"A": 0.005, "B": 0.006})


if __name__ == "__main__":
    unittest.main()
