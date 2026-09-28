"""Historic replays keep an explicit operator symbol selection."""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server/pulse'))
import pulse_trader as pt


class HistSelection(unittest.TestCase):
    def pulse(self, cap=50, owns=True):
        p = object.__new__(pt.Pulse)
        p.symbol_cap = cap
        p.open = {}
        p.universe = []
        p._hist_test_owns_catalog = lambda: owns
        p._intern_tradable = lambda: None
        return p

    def test_explicit_picks_survive_when_test_historic_owns_catalog(self):
        p = self.pulse()
        scan = [f'S{i}-USDT' for i in range(60)]
        with patch.object(pt, 'SYMBOLS', scan), \
                patch.object(pt.hist_test_mod, 'intern_liquid_pool', return_value=scan[:50]):
            picks = p._capped_scan_names(['HYPE-USDT', 'kasusdt', 'ENA-USDT'], explicit=True)
            self.assertEqual(picks, ['HYPE-USDT', 'KAS-USDT', 'ENA-USDT'])
            # Without an explicit selection the intern pool still fills the book.
            self.assertEqual(p._capped_scan_names(['*']), scan[:50])

    def test_explicit_picks_are_capped_but_not_replaced(self):
        p = self.pulse(cap=2)
        with patch.object(pt, 'SYMBOLS', ['BTC-USDT']), \
                patch.object(pt.hist_test_mod, 'intern_liquid_pool', return_value=['BTC-USDT']):
            self.assertEqual(p._capped_scan_names(['AAA-USDT', 'BBB-USDT', 'CCC-USDT'], explicit=True),
                             ['AAA-USDT', 'BBB-USDT'])

    def test_cap_zero_is_unlimited(self):
        p = self.pulse(cap=0)
        names = [f'N{i}-USDT' for i in range(80)]
        with patch.object(pt, 'SYMBOLS', names):
            self.assertEqual(len(p._capped_scan_names(names, explicit=True)), 80)


if __name__ == '__main__':
    unittest.main()
