"""marginCapPct bounds used margin to a share of equity for every new order."""
import os
import pathlib
import sys
import tempfile
import unittest

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-margin-test-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from types import SimpleNamespace as NS  # noqa: E402

import pulse_trader as pt  # noqa: E402


def lite(available, equity, cap):
    return NS(available=available, equity=equity, margin_cap_pct=cap)


class MarginCapTests(unittest.TestCase):
    def headroom(self, **kw):
        return pt.Pulse.margin_headroom(lite(**kw))

    def test_no_used_margin_leaves_cap_share_of_equity(self):
        self.assertAlmostEqual(self.headroom(available=10.0, equity=10.0, cap=0.5), 5.0)

    def test_used_margin_counts_against_the_cap(self):
        # 3 USDT already used: 7 free, but only 5 of equity may be margin -> 2 left
        self.assertAlmostEqual(self.headroom(available=7.0, equity=10.0, cap=0.5), 2.0)

    def test_headroom_never_goes_negative_beyond_the_cap(self):
        self.assertEqual(self.headroom(available=4.0, equity=10.0, cap=0.5), 0.0)

    def test_cap_zero_or_one_disables_it(self):
        self.assertAlmostEqual(self.headroom(available=7.0, equity=10.0, cap=0.0), 7.0)
        self.assertAlmostEqual(self.headroom(available=7.0, equity=10.0, cap=1.0), 7.0)

    def test_unknown_equity_does_not_block_trading(self):
        self.assertAlmostEqual(self.headroom(available=7.0, equity=0.0, cap=0.5), 7.0)


if __name__ == "__main__":
    unittest.main()
