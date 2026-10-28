#!/usr/bin/env python3
"""Causal live general entries use the same closed-bar signal as Set evaluation."""
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server/pulse'))
import pulse_trader as pt
from history_store import HistoryStore
from set_engine import SetBook, general_signal


class GeneralEntryParity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = HistoryStore('test', path=str(pathlib.Path(self.tmp.name) / 'history.json'))
        self.pulse = pt.Pulse.__new__(pt.Pulse)
        self.pulse.history_store = self.store
        self.end = 30_000_000
        self.now = (self.end + 1) * 60 + 10
        self.rows = []
        for i in range(70):
            p = 100 + i * 0.09 + (0.02 if i % 2 else 0)
            self.rows.append({'minute': self.end - 69 + i,
                              'bar': [p, p + .04, p - .04, p, 5 + i % 9], 'closed': True})
        self.store.merge('A-USDT', self.rows, persist=False)

    def test_matches_the_actual_set_signal_and_ignores_the_forming_candle(self):
        book = SetBook()
        book.packs = ['general']
        book.bars['A-USDT'] = [row['bar'] for row in self.rows]
        replay, _, _ = book.prepare_replay_signals('A-USDT', now=self.now)
        expected_direction, expected_conf, expected_reason = replay['general'][-1]
        self.store.merge('A-USDT', [{'minute': self.end + 1, 'bar': [120, 121, 80, 81, 999], 'closed': True}], persist=False)
        with patch.object(pt.time, 'time', return_value=self.now):
            self.assertEqual(self.pulse.general_entry_score('A-USDT'), (expected_direction, expected_reason, expected_conf))
            # Mutable score and regime deliberately disagree; neither may alter the validated entry timing.
            self.pulse.score = lambda _symbol: (-expected_direction, 'modified-live-score', 1.0)
            self.assertEqual(self.pulse.general_entry_score('A-USDT'), (expected_direction, expected_reason, expected_conf))

    def test_missing_stale_or_discontinuous_history_fails_closed(self):
        self.store = HistoryStore('missing', path=str(pathlib.Path(self.tmp.name) / 'missing.json'))
        self.pulse.history_store = self.store
        with patch.object(pt.time, 'time', return_value=self.now):
            self.assertEqual(self.pulse.general_entry_score('A-USDT')[0], 0)
            self.store.merge('A-USDT', self.rows[:-1], persist=False)
            self.assertEqual(self.pulse.general_entry_score('A-USDT')[1], 'stale-closed-history')
            self.store.merge('A-USDT', self.rows[-1:], persist=False)
            d, conf, reason = general_signal([row['bar'] for row in self.rows[-60:]])
            self.assertEqual(self.pulse.general_entry_score('A-USDT'), (d, reason, conf))
        broken = HistoryStore('gap', path=str(pathlib.Path(self.tmp.name) / 'gap.json'))
        broken.merge('A-USDT', self.rows[:-38] + self.rows[-30:], persist=False)
        self.pulse.history_store = broken
        with patch.object(pt.time, 'time', return_value=self.now):
            self.assertEqual(self.pulse.general_entry_score('A-USDT')[1], 'incomplete-closed-history')

    def test_market_refresh_publishes_only_completed_exchange_candles(self):
        p = self.pulse
        p.history_store = HistoryStore('refresh', path=str(pathlib.Path(self.tmp.name) / 'refresh.json'))
        p.klines_tf = {'1m': {}, '5m': {}, '15m': {}}
        p.kline_ts_tf = {'1m': {}, '5m': {}, '15m': {}}
        p.tf_on = {'1m': True, '5m': False, '15m': False}
        p.kline_ban = 0
        p.seed_px_bars = lambda: None
        p.rollup_tf = lambda: None
        p._live_scan_names = lambda: ['A-USDT']
        p._budget = lambda: SimpleNamespace(kline_rest=True, tf_5m=True, tf_15m=True, kline_batch=4)
        p._hist_wake = Mock()
        raw = [[r['minute'] * 60_000, *r['bar']] for r in self.rows[-61:]]
        raw.append([(self.end + 1) * 60_000, 120, 121, 80, 81, 999])
        p.api = SimpleNamespace(gather_public=lambda reqs, timeout: [(path, extra, {'data': raw}) for path, extra in reqs])
        with patch.object(pt.time, 'time', return_value=self.now):
            p.refresh_klines()
            records = p.history_store.window_records('A-USDT', bars=62, end=self.end + 1, source='exchange')
            self.assertEqual(records[-1]['minute'], self.end)
            self.assertEqual(len(records), 61)
            expected = general_signal([r['bar'] for r in self.rows[-60:]])
            self.assertEqual(p.general_entry_score('A-USDT'), (expected[0], expected[2], expected[1]))

    def test_bounded_window_matches_full_record_scan_for_sparse_sources_and_open_bars(self):
        store = HistoryStore('windows', path=str(pathlib.Path(self.tmp.name) / 'windows.json'))
        for i, row in enumerate(self.rows):
            if i % 7:
                store.merge('A-USDT', [dict(row, closed=i % 4 != 0)],
                            source='mark' if i % 2 else 'exchange', persist=False)
        for size in (10, 60, 1000):
            for source in (None, 'mark', 'exchange'):
                for closed in (True, False):
                    self.assertEqual(store.window_records('A-USDT', bars=size, end=self.end, source=source, closed_only=closed),
                                     store.records('A-USDT', start=self.end-size+1, end=self.end,
                                                   source=source, closed_only=closed))


if __name__ == '__main__':
    unittest.main()
