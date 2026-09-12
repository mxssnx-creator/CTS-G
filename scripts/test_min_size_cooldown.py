"""Regression: unlearnable venue min-size rejections must cool down, not storm.

BingX VST reports an unlearnable floor ("The minimum size per order is 0 USDT"),
so the engine cannot adopt a real minimum. Retrying the same size every cycle
burned the request budget and tripped the 20-errors/480s endpoint lockout. These
tests pin the cooldown-and-stop behaviour for the close path.
"""
import pathlib
import sys
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server' / 'pulse'))
import pulse_trader as pt


class MinSizeCooldownTests(unittest.TestCase):
    def _pulse(self, post):
        p = pt.Pulse.__new__(pt.Pulse)
        p.cooldown = {}
        p.px = {'X-USDT': 100.0}
        p.did_io = False
        p.errors = 0
        p.last_error = ''
        p.position_is_ours = lambda pos: True
        p.per_config_controls = lambda pos: False
        p.cid = lambda *a, **k: 'cid1'
        p.api = NS(post=post)
        p.ok = lambda r: False
        return p

    def test_min_size_close_cools_down_without_trying_fallback_forms(self):
        calls = []

        def post(path, body):
            calls.append(body)
            return {'code': 101485, 'msg': 'The minimum size for closing an order is 0 USDT'}

        p = self._pulse(post)
        pos = NS(symbol='X-USDT', side='LONG', qty=0.5, entry=100.0, position_id='')
        ok, _ = p.market_close(pos)
        self.assertFalse(ok)
        # Only the first form is attempted; the fallback forms would fail the
        # same way and each one counts against the endpoint error budget.
        self.assertEqual(len(calls), 1)
        self.assertGreater(p.cooldown.get('X-USDT', 0.0), 0.0)

    def test_min_size_messages_classify_as_qty(self):
        self.assertEqual(pt.ctrl_err_kind('The minimum size per order is 0 USDT.'), 'qty')
        self.assertEqual(pt.ctrl_err_kind('The minimum size for closing an order is 0 USDT'), 'qty')


if __name__ == '__main__':
    unittest.main()
