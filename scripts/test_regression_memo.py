"""The test-only replay memo must preserve the native rows and invalidate inputs."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts/regression'), str(ROOT/'server/pulse')]
from processing import _book
from common import SMALL, T0
import memo
import position_cost


def fixture(symbols=1):
    return _book(n_syms=symbols, bars=220, **{**SMALL, 'holdEnabled': False,
        'holdAuto': False, 'setStepAdapt': False, 'stratIndications': False,
        'stratShortRange': False, 'pauseMode': 'off'})


def rows(book):
    return {s:{sid:list(tape) for sid,tape in tapes.items()} for s,tapes in book._hist_rows.items()}


def replay(book, cached=True):
    with memo.using(cached):
        book.replay_all(now=T0)
    if book.progress.errors:
        raise AssertionError(book.progress.error)
    return rows(book)


class ReplayMemoTests(unittest.TestCase):
    def setUp(self):
        memo.clear()

    def tearDown(self):
        memo.clear()
        position_cost.configure_min_steps(1, 2)

    def test_four_symbol_repeated_fixture_matches_unmemoized_native_rows(self):
        expected = replay(fixture(4), cached=False)
        first = replay(fixture(4))
        with patch.object(memo, 'MODE', 'verify'):
            second = replay(fixture(4))
        self.assertTrue(any(expected.values()))
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        self.assertEqual((memo.STATS['miss'], memo.STATS['hit'], memo.STATS['verified']), (4, 4, 4))
        self.assertEqual(len(memo._CACHE), 4)

    def test_changed_cost_floor_candle_and_global_minimum_are_not_reused(self):
        book = fixture()
        replay(book)
        symbol = next(iter(book.bars))
        for change in [lambda:setattr(book,'cost_pct',.2),
                       lambda:setattr(book,'min_sl_distance_pct',.4),
                       lambda:book.bars[symbol][-1].__setitem__(3,book.bars[symbol][-1][3]*1.01),
                       lambda:position_cost.configure_min_steps(trail=5)]:
            change()
            actual = replay(book)
            self.assertEqual(actual, replay(book, cached=False))
        self.assertEqual(memo.STATS['hit'], 0)
        self.assertEqual(memo.STATS['miss'], 5)

    def test_mutating_copied_history_cannot_poison_retained_columns(self):
        book = fixture()
        expected = replay(book)
        item = next(st for st in book.by_idx if st.hist)
        item.hist[0]['pnl'] = 999
        self.assertEqual(replay(fixture()), expected)
        self.assertEqual(memo.STATS['hit'], 1)

    def test_row_budget_evicts_and_oversized_tapes_are_not_retained(self):
        with patch.object(memo, 'MAX_CACHED_ROWS', 100):
            expected = replay(fixture(4), cached=False)
            self.assertEqual(replay(fixture(4)), expected)
            self.assertLessEqual(sum(item[2] for item in memo._CACHE.values()), 100)
        memo.clear()
        with patch.object(memo, 'MAX_CACHED_ROWS', 1):
            replay(fixture())
            self.assertFalse(memo._CACHE)

    def test_peer_and_pause_dependent_replays_use_the_native_path(self):
        book = fixture()
        expected = replay(book, cached=False)
        with patch.object(memo, '_ported_pack_sources', return_value={'general':'fixture-peer'}):
            self.assertEqual(replay(book), expected)
        book.pause_mode = 'fixed'
        book.pause_fixed = 0
        self.assertEqual(replay(book), replay(book,cached=False))
        self.assertEqual(memo.STATS['bypass'], 2)
        self.assertFalse(memo._CACHE)


if __name__ == '__main__':
    unittest.main()
