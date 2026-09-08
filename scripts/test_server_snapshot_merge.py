"""Offline regressions for integration of the production source snapshot."""
import pathlib
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server' / 'pulse'))
from pulse_trader import Pulse, ctrl_err_kind
from set_engine import SetBook, synth_trend


class ServerSnapshotMergeTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
