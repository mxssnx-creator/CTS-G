"""Configured windows, rather than an additional eight-close gate, govern evidence."""
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import test_all_valid_entries as harness_module
from test_all_valid_entries import pt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server/pulse'))
from position_cost import evaluation_windows, cost_aware_metrics
from hist_calc import rank_tuple
from set_engine import SetBook

class WindowFloorTests(unittest.TestCase):
    def test_partial_window_is_measured_without_an_extra_floor(self):
        rows = [dict(t=i, pnl_pct=.004) for i in range(5)]
        window = evaluation_windows(rows, .15, windows=(30,))['last30']
        self.assertEqual(window['n'], 5)
        self.assertFalse(window['available'])
        self.assertTrue(window['validated'])
        self.assertFalse(cost_aware_metrics(rows, .15)['insufficientSample'])
        self.assertFalse(evaluation_windows([], .15, windows=(30,))['last30']['validated'])

    def test_rank_does_not_reject_a_positive_five_position_window(self):
        self.assertEqual(rank_tuple(dict(last15Ratio=1.2, last15N=5))[0], 0)
        self.assertEqual(rank_tuple(dict(last15Ratio=1.2, last15N=0))[0], 1)

    def test_base_window_setting_remains_authoritative(self):
        book = SetBook()
        book.load({'baseEvalPosCount':5, 'setMinSamples':5})
        self.assertEqual(book.eval_need(),5)
        book.load({'baseEvalPosCount':30, 'setMinSamples':30})
        self.assertEqual(book.eval_need(),30)

if __name__ == '__main__': unittest.main()

class ForcedPendingTests(unittest.TestCase):
    def test_pending_other_set_does_not_block_forced_lane(self):
        harness = harness_module.AllValidEntries()
        p = harness.pulse(harness.book(1))
        p.pending_orders = {'other': dict(kind='entry', symbol='X-USDT', side='LONG',
            requested_qty=1, filled_qty=0, metadata={'execution_lane':'normal:another-set'})}
        checked = []
        p._forced_entry_allowed = lambda *a: checked.append(a[0]['id']) or False
        with patch.object(pt.os.path, 'exists', return_value=False):
            p.place('X-USDT', 1, 'ind:signals:forced-baseline', .9,
                    forced_row={'id':'forced:independent'}, execution_strategy='normal')
        self.assertEqual(checked, ['forced:independent'])
