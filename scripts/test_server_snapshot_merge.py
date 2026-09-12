"""Offline regressions for integration of the production source snapshot."""
import pathlib
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server' / 'pulse'))
from pulse_trader import Pulse, ctrl_err_kind
from set_engine import SetBook, synth_trend


class ServerSnapshotMergeTests(unittest.TestCase):
    def test_first_scored_batch_admits_only_qualified_rows_and_reports_remaining(self):
        for workers in (1, 2):
            with self.subTest(workers=workers):
                book = SetBook()
                book.load({'stratGeneral':True, 'stratIndications':False, 'stratTrailing':False,
                           'slToTpRatios':[.2,.4,.6], 'setMinStep':1, 'setStepMax':30,
                           'baseEvalPosCount':30})
                for st in book.by_idx:
                    st.hist = [dict(t=1000+i*60, symbol='X-USDT', side='LONG',
                                    pnl_pct=.004, hold_s=60) for i in range(30)]
                p = Pulse.__new__(Pulse)
                p.sets = book; p._sets_generation = 1; p._state_lock = threading.RLock()
                p.system_settings = {'systemWorkers':workers}
                observed = []
                def observe(_book, **kwargs):
                    progress = book.progress
                    admitted = book.entry_sets('general', 'LONG')
                    observed.append((progress.phase, progress.sets_done, progress.sets_total,
                                     progress.ready, len(admitted), progress.detail))
                    self.assertTrue(all(s.stage_ledger.get('real') for s in admitted))
                    self.assertFalse(book.entry_sets('general', 'SHORT'))
                p._hist_write_status = observe
                p._score_committed(book, 1, [s.id for s in book.by_idx])
                self.assertEqual(observed[0][:5], ('score',32,90,True,32))
                self.assertIn('remaining 58',observed[0][-1])
                self.assertEqual(observed[-1][:5], ('score',90,90,True,90))
                self.assertIn('remaining 0',observed[-1][-1])

    def test_scoring_status_keeps_live_progress_without_rebuilding_catalog(self):
        book = SetBook(); book.progress.phase = 'score'; book.progress.ready = True
        book.progress.sets_done = 32; book.progress.sets_total = 90
        book.snapshot = Mock(side_effect=AssertionError('progress rebuilt catalog'))
        p = Pulse.__new__(Pulse); p.sets = book; p._state_lock = threading.RLock()
        with patch('pulse_trader.write_hist_job') as write:
            p._hist_write_status(book, progress_only=True)
        book.snapshot.assert_not_called()
        self.assertEqual(write.call_args.args[0]['progress']['setsDone'], 32)
        self.assertEqual(write.call_args.args[0]['progress']['setsTotal'], 90)
        self.assertTrue(write.call_args.args[0]['progress']['ready'])

    def test_incremental_progress_counts_current_universe_and_missing_watermarks(self):
        book = SetBook()
        book.progress.ready = True
        book.progress.valid_symbols = ["A-USDT", "B-USDT"]
        book.progress.watermark = {"A-USDT": 100, "B-USDT": 0, "OLD-USDT": 99}
        book._hist_seen = {f"OLD-{i}" for i in range(65)}
        p = Pulse.__new__(Pulse)
        p.sets = book; p._sets_generation = 1; p._state_lock = threading.RLock()
        p._hist_incremental_symbols = {"A-USDT"}
        p._capped_scan_names = lambda values: list(values)
        p.history_store = SimpleNamespace(window=Mock(return_value=synth_trend(180)), watermark=Mock(return_value=101))
        p._hist_replay_chunked = Mock(return_value=True)
        p._hist_write_status = Mock()
        self.assertTrue(p._hist_incremental_replay())
        self.assertEqual((book.progress.symbols_done, book.progress.symbols_total), (1, 2))
        self.assertEqual(book.progress.missing_symbols, ["B-USDT"])
        self.assertFalse(book.progress.coordination_complete)
        self.assertEqual(book.progress.phase, "partial")
        self.assertTrue(book.progress.ready)  # prior gate survives an incremental update

    def test_all_flat_exchange_messages_are_classified(self):
        for message in ('position not exist', 'position does not exist', 'No position to close'):
            self.assertEqual(ctrl_err_kind(message), 'flat')

    def test_empty_replay_replaces_old_evidence_and_rescores_its_set(self):
        book = SetBook()
        book.load({'stratGeneral': True, 'stratIndications': False, 'stratTrailing': False,
                   'slToTpRatios': [.6], 'setMinStep': 3, 'setStepMax': 3})
        state = book.by_idx[0]
        # The replaced symbol is outside the newest twelve rows.
        state.hist = [dict(t=i, symbol='X-USDT' if i == 0 else 'Y-USDT',
                           side='LONG', pnl_pct=.01) for i in range(15)]
        replay = book.replay_clone(['X-USDT'])
        for row in replay.by_idx:
            row.hist = []
        replay.replay_all = Mock()
        replay.progress.phase = 'ready'
        replay.progress.ready = True
        book.replay_clone = Mock(return_value=replay)
        p = Pulse.__new__(Pulse)
        p.sets = book
        p._sets_generation = 1
        p._state_lock = threading.RLock()
        p.load = SimpleNamespace(last_budget=None)
        p._hist_write_status = Mock()
        p._hist_request_changed = lambda: False
        p._score_committed = Mock()
        self.assertTrue(p._replay_sets_isolated(['X-USDT'], True, 2))
        self.assertEqual(len(state.hist), 14)
        self.assertTrue(all(row['symbol'] == 'Y-USDT' for row in state.hist))
        p._score_committed.assert_called_once_with(book, 1, [state.id])
        self.assertFalse(book._running)

    def test_slice_progress_retains_run_identity_and_counts_only_this_run(self):
        book = SetBook()
        book.load({'stratGeneral': True, 'stratIndications': False, 'stratTrailing': False,
                   'slToTpRatios': [.6], 'setMinStep': 3, 'setStepMax': 3})
        book._hist_seen = {'X-USDT', 'Y-USDT', 'REMOVED-USDT'}
        book.progress.run_id = 'vst-current-run'
        book.progress.generation = 7
        book.progress.mode = 'hourly'
        book.progress.requested_start = 100
        book.progress.requested_end = 819
        book.progress.valid_symbols = ['X-USDT', 'Y-USDT', 'Z-USDT']
        book.progress.last_published_watermark = {'X-USDT': 99}
        replay = book.replay_clone(['Y-USDT'])
        observed = []
        def replay_all(**kwargs):
            from set_engine import Progress
            # The low-level replay resets its metadata and emits progress.
            replay.progress = Progress(phase='replay', pct=1)
            kwargs['on_step']()
            observed.append((book.progress.symbols_done, book.progress.pct, book.progress.run_id))
            replay._hist_seen.add('Y-USDT')
            replay.progress.phase = 'partial'
            kwargs['on_step']()
            observed.append((book.progress.symbols_done, book.progress.pct, book.progress.run_id))
        replay.replay_all = replay_all
        book.replay_clone = Mock(return_value=replay)
        p = Pulse.__new__(Pulse)
        p.sets = book; p._sets_generation = 1; p._state_lock = threading.RLock()
        p.load = SimpleNamespace(last_budget=None)
        p._hist_write_status = Mock(); p.write_stats = Mock()
        p._hist_request_changed = lambda: False
        p._score_committed = Mock()
        self.assertTrue(p._replay_sets_isolated(['Y-USDT'], True, 3, completed_symbols={'X-USDT'}))
        self.assertEqual(observed, [(1, 55., 'vst-current-run'), (2, 75., 'vst-current-run')])
        self.assertEqual((book.progress.symbols_done, book.progress.symbols_total), (2, 3))
        self.assertEqual((book.progress.run_id, book.progress.generation, book.progress.mode), ('vst-current-run', 7, 'hourly'))
        self.assertEqual((book.progress.requested_start, book.progress.requested_end), (100, 819))
        self.assertEqual(book.progress.last_published_watermark, {'X-USDT': 99})
        self.assertEqual(book.progress.valid_symbols, ['X-USDT', 'Y-USDT', 'Z-USDT'])
        self.assertFalse(book.progress.coordination_complete)
        self.assertTrue(book.progress.ready)

    def test_new_generation_during_replay_is_not_overwritten(self):
        book = SetBook()
        book.load({'stratGeneral': True, 'stratIndications': False, 'stratTrailing': False,
                   'slToTpRatios': [.6], 'setMinStep': 3, 'setStepMax': 3})
        p = Pulse.__new__(Pulse)
        p.sets = book; p._sets_generation = 1; p._state_lock = threading.RLock()
        p.load = SimpleNamespace(last_budget=None)
        p._hist_write_status = Mock(); p.write_stats = Mock()
        p._hist_request_changed = lambda: False; p._score_committed = Mock()
        replay = book.replay_clone(['X-USDT'])
        replacement = SetBook(); replacement.progress.run_id = 'new-generation'
        def change(**kwargs):
            p.sets = replacement; p._sets_generation = 2
            kwargs['on_step']()
        replay.replay_all = change
        book.replay_clone = Mock(return_value=replay)
        self.assertFalse(p._replay_sets_isolated(['X-USDT'], False, 1, completed_symbols=set()))
        self.assertEqual(p.sets.progress.run_id, 'new-generation')
        p._score_committed.assert_not_called()


if __name__ == '__main__':
    unittest.main()
