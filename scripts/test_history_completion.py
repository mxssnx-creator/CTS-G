"""Actual replay completion, fair retries and calculated input watermarks."""
import pathlib
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server' / 'pulse'))
from pulse_trader import Pulse
from set_engine import SetBook


class HistoryCompletionTests(unittest.TestCase):
    def test_consumed_request_cannot_cancel_an_automatic_run_through_fast_cache(self):
        p = Pulse.__new__(Pulse)
        p._hist_active_run_id = 'automatic-current'
        p._hist_request_seen = 'manual-already-consumed'
        p._hist_latest_request_id = 'manual-already-consumed'
        p._hist_request_check_ts = 0
        with patch('pulse_trader.time.monotonic', side_effect=[100., 100.1]), \
                patch('pulse_trader.read_hist_request', return_value={'runId':'manual-already-consumed'}) as read:
            self.assertFalse(p._hist_request_changed())
            self.assertFalse(p._hist_request_changed())
            read.assert_called_once()
        with patch('pulse_trader.time.monotonic', side_effect=[101., 101.1]), \
                patch('pulse_trader.read_hist_request', return_value={'runId':'manual-new'}) as read:
            self.assertTrue(p._hist_request_changed())
            self.assertTrue(p._hist_request_changed())
            read.assert_called_once()

    def harness(self, missing=()):
        p = Pulse.__new__(Pulse)
        p.sets = SetBook()
        p.sets.enabled = True
        p._state_lock = threading.RLock()
        p._sets_generation = 1
        p._hist_stop = threading.Event()
        p._hist_wake = Mock()
        p._catalog_ready = threading.Event(); p._catalog_ready.set()
        p._hist_next_hourly_at = 0
        p._hist_fetch_failures = 0
        p._hist_last_published_watermark = {}
        p._hist_incremental_symbols = set()
        p._hist_new_request = Mock(return_value={})
        p._hist_request_changed = Mock(return_value=False)
        p._hist_peer_busy = Mock(return_value='')
        p._hist_peer_claim = Mock(return_value=True)
        p._hist_peer_touch = Mock(); p._hist_peer_release = Mock()
        p._budget = Mock(return_value=SimpleNamespace(hist_run=True, level='normal'))
        p._hist_replay_chunk_size = Mock(return_value=1)
        p._hist_write_status = Mock(); p.write_stats = Mock(); p.trim_caches = Mock()
        p.symbol_cap = 20
        p.names = [f'S{i}-USDT' for i in range(20)]
        p._hist_selected_snapshot = Mock(return_value=(p.names, []))
        p._history_bounds = Mock(return_value=(10, 129))
        p.history_store = SimpleNamespace(watermark=Mock(side_effect=AssertionError('read a future watermark')))
        p.coverage = {name: dict(enough=name not in missing, barsHeld=0 if name in missing else 120,
                                present=0 if name in missing else 120, watermark=0 if name in missing else 129,
                                gaps=[{'start':10,'end':129}] if name in missing else []) for name in p.names}
        p._hist_fetch_durable = Mock(side_effect=lambda *a: (p.coverage, {}))
        p.api = SimpleNamespace(err=Mock())
        def checkpoint(book, reason):
            if reason in ('superseded-or-deferred', 'published', 'skip-unchanged', 'error'):
                p._hist_stop.set()
        p._hist_checkpoint = Mock(side_effect=checkpoint)
        return p

    def run_pass(self, p):
        p._hist_stop.clear()
        p._hist_next_hourly_at = 0
        p._hist_loop_durable()
        p.api.err.write.assert_not_called()

    def test_failed_slice_is_not_complete_and_does_not_starve_other_symbols(self):
        p = self.harness()
        p.sets.progress.last_complete_run = 42
        p._replay_sets_isolated = Mock(side_effect=lambda names, *a, **kw: names != ['S1-USDT'])
        self.run_pass(p)
        self.assertEqual(p._replay_sets_isolated.call_count, 20)
        self.assertEqual(set(p._hist_last_published_watermark), set(p.names) - {'S1-USDT'})
        self.assertEqual(p._hist_replay_retry, {'S1-USDT'})
        progress = p.sets.progress
        self.assertEqual((progress.symbols_done, progress.symbols_total), (19, 20))
        self.assertFalse(progress.coordination_complete)
        self.assertEqual(progress.last_complete_run, 42)
        self.assertTrue(progress.ready)  # completed independent slices remain available

        # New prices on the completed prefix must not force it to be redone
        # ahead of the missing slice on every retry.
        for item in p.coverage.values(): item['watermark'] = 130
        p._replay_sets_isolated = Mock(return_value=True)
        self.run_pass(p)
        p._replay_sets_isolated.assert_called_once()
        self.assertEqual(p._replay_sets_isolated.call_args.args[0], ['S1-USDT'])
        self.assertEqual(p._hist_last_published_watermark['S0-USDT'], 129)
        self.assertEqual(p._hist_last_published_watermark['S1-USDT'], 130)
        self.assertFalse(p._hist_replay_retry)
        self.assertEqual((progress.symbols_done, progress.symbols_total), (20, 20))
        self.assertTrue(progress.coordination_complete)

        # Refresh only the previously completed prefix whose input advanced.
        p._replay_sets_isolated.reset_mock()
        self.run_pass(p)
        actual = [c.args[0][0] for c in p._replay_sets_isolated.call_args_list]
        self.assertEqual(set(actual), set(p.names) - {'S1-USDT'})
        self.assertEqual(set(p._hist_last_published_watermark.values()), {130})
        p._replay_sets_isolated.reset_mock()
        self.run_pass(p)
        p._replay_sets_isolated.assert_not_called()
        self.assertIn('skip replay', progress.detail)

    def test_partial_data_never_becomes_complete_calculation(self):
        p = self.harness(missing={'S19-USDT'})
        p.sets.progress.last_complete_run = 42
        p._replay_sets_isolated = Mock(return_value=True)
        self.run_pass(p)
        self.assertEqual(p._replay_sets_isolated.call_count, 19)
        progress = p.sets.progress
        self.assertEqual((progress.symbols_done, progress.symbols_total), (19, 20))
        self.assertFalse(progress.coordination_complete)
        self.assertEqual(progress.phase, 'partial')
        self.assertEqual(progress.last_complete_run, 42)
        self.assertNotIn('S19-USDT', progress.last_published_watermark)


if __name__ == '__main__':
    unittest.main()
