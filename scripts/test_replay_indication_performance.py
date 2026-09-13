#!/usr/bin/env python3
"""Focused no-network regressions for replay and indication hot paths."""
from __future__ import annotations

import os
import pathlib
import sys
import time
import unittest
from unittest.mock import patch, Mock
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PULSE = ROOT / "server" / "pulse"
sys.path.insert(0, str(PULSE))
os.chdir(PULSE)

import indication_engine as indication  # noqa: E402
import pulse_trader as trader  # noqa: E402
from indication_engine import (  # noqa: E402
    Candle,
    DEFAULT_SETTINGS,
    ExtraBook,
    IndicationBook,
    build_indication_frame,
)
from position_cost import row_position_cost_pct  # noqa: E402
from set_engine import (  # noqa: E402
    CompactHistRow,
    IND_TAG_KIND,
    SetBook,
    hist_fill,
    indication_kind_votes,
    indication_kind_votes_frame,
    synth_trend,
)
from pulse_trader import effective_indication_timeframes  # noqa: E402


class _RecordingExecutor:
    def __init__(self) -> None:
        self.submitted = []

    def submit(self, fn, *args):
        self.submitted.append((fn, args))
        return None


def _bar(close: float, previous: float, volume: float = 1000.0) -> list[float]:
    opening = previous
    high = max(opening, close) * 1.0015
    low = min(opening, close) * 0.9985
    return [opening, high, low, close, volume]


def _fixture(name: str, count: int = 96) -> list[list[float]]:
    closes: list[float] = []
    for i in range(count):
        if name == "rising":
            close = 100.0 + i * 0.24
        elif name == "falling":
            close = 130.0 - i * 0.24
        elif name == "reversal":
            close = 124.0 - i * 0.42 if i < count // 2 else 104.0 + (i - count // 2) * 0.7
        elif name == "breakout":
            close = 100.0 + (i % 2) * 0.01 if i < count - 24 else 100.0 + (i - count + 24) * 0.9
        elif name == "flat":
            close = 100.0 + (0.01 if i % 2 else -0.01)
        elif name == "invalid":
            close = 100.0 + i * 0.24
        else:
            raise ValueError(name)
        closes.append(close)

    bars = [_bar(close, closes[i - 1] if i else close) for i, close in enumerate(closes)]
    if name == "invalid":
        bars[7] = [0.0, 0.0, 0.0, 0.0, 0.0]
        bars[19] = ["not-a-bar"]  # type: ignore[list-item]
        bars[41] = None  # type: ignore[assignment]
    return bars


class ReplayIndicationTests(unittest.TestCase):
    def test_columnar_close_rings_match_full_scalar_tapes_and_counts(self):
        import set_engine as engine
        if engine._np is None:
            self.skipTest("NumPy replay requires NumPy")
        overlay = dict(histEnabled=True, histLookbackBars=1000, histMinBars=60,
                       histWarmup=0, stratIndications=False, stratGeneral=True,
                       stratTrailing=True, stratBlock=True, histSimulateBlock=True,
                       histSimulateDca=True, setMinStep=1, setStepMax=2, slToTpRatios=[.2,.6])
        # Both directions overflow their retained windows; include same-bar
        # TP/SL/trailing decisions and independent Block/DCA representative lanes.
        bars = [[100.,102.,98.,100.,1.] for _ in range(1000)]
        prepared = ({'general':[(1 if i % 400 < 200 else -1,.9,'test') for i in range(1000)]}, {}, 0)
        for honor in (True, False):
            with self.subTest(honor_tp=honor):
                reference = SetBook(); reference.load(overlay); reference.cooldown_bars = 0
                reference.hist_honor_tp = honor; reference.ingest_bars('RING-USDT', bars)
                actual = reference.replay_clone(['RING-USDT'])
                expected_hist = {}; expected_counts = {}; expected_strategy = {}
                with patch.object(engine, '_np', None):
                    reference._replay_symbol('RING-USDT', expected_hist, 1_700_000_000,
                        prepared=prepared, hist_counts=expected_counts, strat_hist=expected_strategy)
                actual_hist = {}; actual_counts = {}; actual_strategy = {}; progress = []
                actual._replay_symbol('RING-USDT', actual_hist, 1_700_000_000,
                    prepared=prepared, hist_counts=actual_counts, strat_hist=actual_strategy,
                    on_step=lambda: progress.append(actual.progress.detail))
                self.assertEqual(actual_counts, expected_counts)
                self.assertGreater(min(actual_counts.values()), 320)
                for sid, rows in expected_hist.items():
                    self.assertEqual([dict(r) for r in actual_hist[sid]],
                                     [{k:v for k,v in dict(r).items() if k != 'strategy'}
                                      for r in engine.recent_direction_rows(rows, engine.HIST_CAP)], sid)
                    for side in ('LONG','SHORT'):
                        self.assertGreaterEqual(sum(r['side']==side for r in actual_hist[sid]), 75)
                self.assertEqual(actual_strategy, expected_strategy)
                self.assertTrue(any('LONG' in p and 'bar ' in p for p in progress))
                self.assertTrue(any('SHORT' in p and 'bar ' in p for p in progress))

    def test_indication_metrics_reuse_content_and_invalidate_same_length_corrections(self):
        import set_engine as engine
        book = SetBook(); book.load({'baseEvalPosCount':30, 'minPf':1.05})
        book.ind_hist['signals'] = [dict(hist_fill(i*60, 'X-USDT', 1, .004, 60, 'tp')) for i in range(40)]
        with patch.object(engine, 'evaluation_windows', wraps=engine.evaluation_windows) as evaluate:
            first = book.ind_stats('signals', 'LONG')
            n = evaluate.call_count
            for _ in range(10): self.assertEqual(book.ind_stats('signals', 'LONG'), first)
            self.assertEqual(evaluate.call_count, n)
            # A correction can keep the same list, length and timestamps.
            book.ind_hist['signals'][-1]['pnl_pct'] = -.5
            corrected = book.ind_stats('signals', 'LONG')
            self.assertLess(corrected['pf'], first['pf'])
            self.assertGreater(evaluate.call_count, n)
        book.ind_hist['signals'][-1]['pnl_pct'] = .004
        self.assertTrue(book.ind_stats('signals', 'LONG')['profitable'])
        book.min_pf = 2.0
        self.assertFalse(book.ind_stats('signals', 'LONG')['profitable'])
        book.pf_n = 35
        self.assertEqual(book.ind_stats('signals', 'LONG')['n'], 35)
        response = book.ind_stats('signals')
        response['bySide']['LONG']['pf'] = -999
        self.assertNotEqual(book.ind_stats('signals')['bySide']['LONG']['pf'], -999)

    def test_unchanged_processing_lineages_reuse_snapshot(self):
        book = SetBook(); book.load({'setMinStep':1, 'setStepMax':1, 'slToTpRatios':[.6]})
        sid = book.by_idx[0].id
        book.sync_processing_sets([sid])
        with patch.object(book, 'coverage', wraps=book.coverage) as coverage:
            first = book.snapshot()
            book.sync_processing_sets([sid])
            self.assertIs(book.snapshot(), first)
            self.assertEqual(coverage.call_count, 1)
            book.sync_processing_sets([])
            self.assertEqual(book.snapshot()['processingCount'], 0)
            self.assertEqual(coverage.call_count, 2)

    def test_full_replay_counts_survive_tape_retention_and_symbol_replacement(self):
        overlay = dict(histEnabled=True, histLookbackBars=1000, histMinBars=60,
                       histWarmup=0, stratIndications=False, stratGeneral=True,
                       stratTrailing=False, stratBlock=False, histSimulateBlock=False,
                       histSimulateDca=False, setMinStep=1, setStepMax=1, slToTpRatios=[.6])
        book = SetBook(); book.load(overlay); book.cooldown_bars = 0
        bars = [[100.,101.,99.,100.,1.] for _ in range(1000)]
        for symbol in ('A-USDT','B-USDT'):
            book.ingest_bars(symbol, bars)
        signals = ({'general':[(1,.9,'test')]*1000}, {}, 0)
        with patch.object(book, 'prepare_replay_signals', return_value=signals):
            book.replay_all(symbols=['A-USDT','B-USDT'], workers=2, score=False)
        st = next(s for s in book.by_idx if s.n)
        self.assertEqual(st.n, 1998)
        self.assertLessEqual(len(st.hist), 160)
        self.assertEqual(book._hist_counts[st.id], {'A-USDT':999,'B-USDT':999})

        target = book.replay_clone(['A-USDT','B-USDT'])
        target._commit_hist({st.id:st.hist}, merge=True,
                            replayed_symbols=['A-USDT','B-USDT'],
                            hist_symbol_counts=book._hist_counts, score=False)
        self.assertEqual(target.sets[st.id].n, 1998)
        target._score_pair((target.sets[st.id], None))
        self.assertEqual(target.sets[st.id].n, 1998)
        target._commit_hist({}, merge=True, replayed_symbols=['A-USDT'],
                            hist_symbol_counts={}, score=False)
        self.assertEqual(target.sets[st.id].n, 999)
        target._score_pair((target.sets[st.id], None))
        self.assertEqual(target.sets[st.id].n, 999)
        self.assertEqual(target._hist_counts[st.id], {'B-USDT':999})
        target._commit_hist({}, merge=True, replayed_symbols=['B-USDT'],
                            hist_symbol_counts={}, score=False)
        self.assertEqual(target.sets[st.id].n, 0)
        self.assertNotIn(st.id, target._hist_counts)

    def test_periodic_export_reuses_the_published_snapshot(self):
        from types import SimpleNamespace
        pulse = trader.Pulse.__new__(trader.Pulse)
        pulse.load = SimpleNamespace(last_budget=SimpleNamespace(stats_full=False))
        pulse.system_settings = {"systemStatsIntervalS":2, "systemReportIntervalS":0}
        pulse._stats_force = True; pulse._stats_ts = pulse._report_ts = 0
        snapshot = {"mode":"QA_FIXTURE", "setCount":37440}
        pulse.stats = Mock(return_value=snapshot)
        pulse.position_cost_pct = .1
        with tempfile.TemporaryDirectory() as root, patch.object(trader, 'DIR', root), \
                patch.object(trader, 'atomic_write') as publish, patch('stats_report.write') as export:
            pulse._write_stats_locked(force=True)
        pulse.stats.assert_called_once_with()
        self.assertIs(publish.call_args.args[1], snapshot)
        self.assertIs(export.call_args.args[0], snapshot)

    def test_disabled_combined_types_do_not_run_unused_tf_or_consensus_work(self):
        bars = _fixture("rising")
        book = IndicationBook()
        book.settings.update(
            typeState=False,
            typeSignals=False,
            tfCombined=True,
            tf1m=True,
            tf5m=True,
            tf15m=True,
        )
        with patch.object(indication, "evaluate_signal_candles", side_effect=AssertionError("unused TF work")), \
             patch.object(indication, "low_stop_consensus", side_effect=AssertionError("unused consensus work")):
            rows = book.process(
                "NO-COMBINED-OVERLOAD-USDT",
                bars,
                bars_by_tf={"1m": bars, "5m": bars, "15m": bars},
            )
        self.assertFalse(any(row.mode in ("tf_combined", "multi_source_consensus") for row in rows))
        self.assertFalse(any(row.kind == "state" for row in rows))

    def test_load_budget_sheds_cached_higher_timeframes_for_combined_lane(self):
        from types import SimpleNamespace

        configured = {"1m": True, "5m": True, "15m": True}
        normal = SimpleNamespace(tf_5m=True, tf_15m=True, level="normal")
        overloaded = SimpleNamespace(
            tf_5m=False,
            tf_15m=False,
            level="overload",
            extra_sources=False,
            extra_n=0,
            scan_chunk=1,
        )
        self.assertEqual(effective_indication_timeframes(configured, normal), ("1m", "5m", "15m"))
        self.assertEqual(effective_indication_timeframes(configured, overloaded), ("1m",))

        bars = _fixture("rising")
        book = IndicationBook()
        book.settings.update(minimumConfidence=0.4, minimumStrength=0.05, tfCombined=True, tfMinAgree=2)
        rows = book.process(
            "SHED-USDT",
            bars,
            bars_by_tf={"1m": bars},
        )
        self.assertFalse(any(row.mode == "tf_combined" for row in rows))
        self.assertEqual({row.timeframe for row in rows if row.mode == "direct_tf"}, {"1m"})

        pulse = trader.Pulse.__new__(trader.Pulse)
        pulse.indications = IndicationBook()
        pulse.indications.settings["extraSources"] = False
        pulse.tf_on = configured
        pulse.open = {}
        pulse.universe = []
        pulse.px = {"SHED-USDT": 100.0}
        pulse.klines_tf = {tf: {"SHED-USDT": bars} for tf in ("1m", "5m", "15m")}
        pulse.klines = pulse.klines_tf["1m"]
        pulse._ind_fp = {}
        pulse.load = SimpleNamespace(
            cursor_ind=0,
            scan_chunk=1,
            scan_window=lambda names, open_symbols, chunk, cursor, ranked: (["SHED-USDT"], 0),
        )
        pulse._budget = lambda: overloaded
        pulse.score = lambda symbol: (1, 0.0, 0.8)
        seen = []
        pulse.indications.process = lambda *args, **kwargs: seen.append(kwargs["bars_by_tf"])
        with patch.object(trader, "SYMBOLS", ["SHED-USDT"]):
            trader.Pulse.process_indications(pulse)
        self.assertEqual([set(item) for item in seen], [{"1m"}])

    def test_compact_historic_rows_preserve_mapping_and_score_contract(self):
        rows = [hist_fill(1_700_000_000 + i * 60, "X-USDT", 1, .004, 60, "tp") for i in range(16)]
        self.assertIsInstance(rows[0], CompactHistRow)
        self.assertEqual(dict(rows[0])["pnl_pct"], .004)
        self.assertEqual({**rows[0]}["side"], "LONG")
        self.assertTrue(all(row.get("reason") == "tp" for row in rows))
        measured = hist_fill(1_700_000_000, "X-USDT", 1, .004, 60, "tp", cost=.12)
        self.assertAlmostEqual(measured.get("costPct"), .12)
        measured["cost_source"] = "exchange-measured"
        self.assertAlmostEqual(row_position_cost_pct(measured), .12)

        book = SetBook()
        compact_metrics = book._fast_historic_metrics(rows, ordered=True)
        dict_metrics = book._fast_historic_metrics([dict(row) for row in rows], ordered=True)
        self.assertEqual(compact_metrics["last15_n"], dict_metrics["last15_n"])
        self.assertAlmostEqual(compact_metrics["last15_ratio"], dict_metrics["last15_ratio"], places=6)
        self.assertEqual(book._fast_historic_metrics(rows, ordered=True)["evaluation_windows"], {})
        book.pf_n = book.min_samples = 15
        self.assertEqual(len(book._fast_historic_metrics(rows, ordered=True)["evaluation_windows"]), 6)

    def test_prepared_frame_matches_public_vote_wrapper(self):
        settings = dict(DEFAULT_SETTINGS)
        tags = {"sig", "ta", "dir", "move", "act", "common", "trend", "brk", "break"}
        self.assertEqual(set(IND_TAG_KIND), tags)

        for fixture_name in ("rising", "falling", "reversal", "breakout", "flat", "invalid"):
            with self.subTest(fixture=fixture_name):
                window = _fixture(fixture_name)[-60:]
                expected = indication_kind_votes(window, settings, now=1_700_000_000.0)
                for period_s in (60.0, 300.0, 900.0):
                    with self.subTest(period_s=period_s):
                        frame = build_indication_frame(window, now=1_700_000_000.0, period_s=period_s)
                        actual = indication_kind_votes_frame(frame, settings)
                        self.assertEqual(actual, expected)
                        self.assertEqual(len(frame.candles), len(frame.closes))

    def test_mixed_timeframes_keep_independent_lanes(self):
        bars = _fixture("rising")
        book = IndicationBook()
        book.settings.update(minimumConfidence=0.4, minimumStrength=0.05)
        rows = book.process(
            "MIX-USDT",
            bars,
            bars_by_tf={"1m": bars, "5m": bars, "15m": bars},
        )
        direct = {row.timeframe for row in rows if row.mode == "direct_tf"}
        self.assertEqual(direct, {"1m", "5m", "15m"})
        self.assertTrue(all(row.timeframe in {"1m", "5m", "15m"} for row in rows if row.mode == "direct_tf"))

    def test_extra_prefetch_is_deduplicated_and_hot_path_never_gets(self):
        extra = ExtraBook()
        executor = _RecordingExecutor()
        extra.pool = executor
        pair = ("binance-usdm", "XRP-USDT")

        extra.prefetch([pair, pair, pair])
        extra.prefetch([pair])
        self.assertEqual(len(executor.submitted), 1)
        self.assertEqual(extra._inflight, {"binance-usdm:XRP-USDT"})

        fn, args = executor.submitted[0]
        with patch.object(extra, "get", return_value=[]):
            fn(*args)
        self.assertFalse(extra._inflight)

        with patch.object(extra, "get", side_effect=AssertionError("peek performed network I/O")):
            self.assertEqual(extra.peek(*pair), [])

        ready = [Candle(time.time(), 100.0, 101.0, 99.0, 100.5, 1_000.0)]
        extra.cache["binance-usdm:XRP-USDT"] = (time.time(), ready)
        with patch.object(extra, "get", side_effect=AssertionError("process performed network I/O")):
            with patch.object(indication, "EXTRA", extra):
                book = IndicationBook()
                book.settings.update(minimumConfidence=0.4, minimumStrength=0.05)
                book.process("XRP-USDT", _fixture("rising"), want_extra=True)
        self.assertIs(extra.peek(*pair), ready)

    def test_one_hour_window_and_tile_parity(self):
        overlay = {
            "histEnabled": True,
            "histLookbackBars": 60,
            "histMinBars": 60,
            "histWarmup": 30,
            "histExactWindow": True,
            "stratIndications": False,
            "stratGeneral": True,
            "stratTrailing": False,
            "stratBlock": False,
            "histSimulateBlock": False,
            "histSimulateDca": False,
            "setMinStep": 1,
            "setStepMax": 2,
            "slToTpRatios": [0.6],
        }
        bars = synth_trend(90, 100.0, 0.18, 0.03)

        reference = SetBook()
        reference.load(overlay)
        reference.ingest_bars("PARITY-USDT", bars)
        prepared = reference.prepare_replay_signals("PARITY-USDT", now=1_700_000_000.0)
        self.assertEqual(prepared[2], 30)
        reference_hist = {}
        reference_counts = {}
        self.assertEqual(
            reference.replay_symbol_partial(
                "PARITY-USDT",
                reference_hist,
                now=1_700_000_000.0,
                drop_bars=False,
                prepared=prepared,
                hist_counts=reference_counts,
            ),
            90,
        )

        tiled = SetBook()
        tiled.load(overlay)
        tiled.ingest_bars("PARITY-USDT", bars)
        tiled_prepared = tiled.prepare_replay_signals("PARITY-USDT", now=1_700_000_000.0)
        tiled_hist = {}
        tiled_counts = {}
        ids = [state.id for state in tiled.by_idx]
        for start in range(0, len(ids), 1):
            local = {}
            local_counts = {}
            tiled.replay_symbol_partial(
                "PARITY-USDT",
                local,
                now=1_700_000_000.0,
                drop_bars=False,
                set_ids=ids[start : start + 1],
                prepared=tiled_prepared,
                hist_counts=local_counts,
            )
            tiled_hist.update(local)
            tiled_counts.update(local_counts)

        def signature(rows):
            return [
                (row.get("t"), row.get("side"), round(float(row.get("pnl_pct") or 0), 10), row.get("reason"))
                for row in rows
            ]

        self.assertEqual(set(reference_hist), set(tiled_hist))
        for set_id in reference_hist:
            self.assertEqual(signature(reference_hist[set_id]), signature(tiled_hist[set_id]), set_id)
            self.assertEqual(reference_counts.get(set_id), tiled_counts.get(set_id), set_id)

    def test_scheduler_contracts_are_bounded_and_nonblocking(self):
        pulse_source = (PULSE / "pulse_trader.py").read_text()
        hist_source = (PULSE / "hist_calc.py").read_text()
        set_source = (PULSE / "set_engine.py").read_text()

        self.assertNotRegex(pulse_source, r"sleep\(\s*2\.4")
        self.assertNotRegex(pulse_source, r"sleep\(\s*0\.12")
        self.assertNotRegex(hist_source, r"sleep\(\s*0\.12")
        self.assertIn("_hist_wake", pulse_source)
        self.assertIn("FIRST_COMPLETED", hist_source)
        self.assertIn("REPLAY_TILE_SIZE", hist_source)
        # Scheduling now follows available CPUs and reduces work under load.
        # Verify the contract rather than an obsolete literal worker cap.
        import pulse_trader as trader
        from types import SimpleNamespace
        worker = trader.Pulse.__new__(trader.Pulse)
        for cpu in (1, 2, 4, 16):
            for count in (0, 1, 4, 50):
                for level in ("normal", "overload", "critical"):
                    with self.subTest(cpu=cpu, count=count, level=level), patch.object(trader.os, "cpu_count", return_value=cpu):
                        got = worker._replay_worker_count(count, SimpleNamespace(level=level))
                        self.assertGreaterEqual(got, 1)
                        self.assertLessEqual(got, min(cpu, max(1, count)))
                        self.assertLessEqual(got, 2)  # configured default worker ceiling
                        if level == "critical" or cpu <= 1:
                            self.assertEqual(got, 1)
                        if level == "overload":
                            self.assertLessEqual(got, 2)
        self.assertIn("max_workers=w", set_source)
        self.assertIn("generation", pulse_source)
        self.assertIn("def should_abort", pulse_source)


class HistoricScoreBundleTests(unittest.TestCase):
    def test_bundle_matches_separate_side_scores(self):
        book = SetBook()
        tape = [
            {"t": 10.0, "side": "LONG", "pnl": 0.2, "pnl_pct": 0.004, "hold_s": 60, "symbol": "X-USDT", "reason": "tp"},
            {"t": 20.0, "side": "SHORT", "pnl": -0.1, "pnl_pct": -0.003, "hold_s": 120, "symbol": "X-USDT", "reason": "sl"},
            {"t": 30.0, "side": "LONG", "pnl": 0.15, "pnl_pct": 0.003, "hold_s": 90, "symbol": "Y-USDT", "reason": "tp"},
            {"t": 40.0, "side": "SHORT", "pnl": 0.05, "pnl_pct": 0.002, "hold_s": 60, "symbol": "Y-USDT", "reason": "tp"},
        ]
        overall, split = book._fast_historic_bundle(tape)
        direct = book._fast_historic_metrics(tape)
        self.assertAlmostEqual(overall["last15_ratio"], direct["last15_ratio"], places=6)
        self.assertEqual(overall["n"], direct["n"])
        long_only = book._fast_historic_metrics([row for row in tape if row["side"] == "LONG"])
        short_only = book._fast_historic_metrics([row for row in tape if row["side"] == "SHORT"])
        self.assertAlmostEqual(split["LONG"]["last15_ratio"], long_only["last15_ratio"], places=6)
        self.assertAlmostEqual(split["SHORT"]["last15_ratio"], short_only["last15_ratio"], places=6)
        self.assertEqual(split["LONG"]["n"], 2)
        self.assertEqual(split["SHORT"]["n"], 2)

    def test_ordered_helpers_match_unsorted_path(self):
        from set_engine import last_n_balanced, drawdown_time_by_symbol
        from position_cost import last_n_cost_pf, evaluation_windows
        tape = [
            {"t": 40.0, "side": "SHORT", "pnl": 0.05, "pnl_pct": 0.002, "hold_s": 60, "symbol": "Y-USDT"},
            {"t": 10.0, "side": "LONG", "pnl": 0.2, "pnl_pct": 0.004, "hold_s": 60, "symbol": "X-USDT"},
            {"t": 30.0, "side": "LONG", "pnl": 0.15, "pnl_pct": 0.003, "hold_s": 90, "symbol": "Y-USDT"},
            {"t": 20.0, "side": "SHORT", "pnl": -0.1, "pnl_pct": -0.003, "hold_s": 120, "symbol": "X-USDT"},
        ]
        ordered = sorted(tape, key=lambda row: row["t"])
        bal = last_n_balanced(tape, 3)
        bal_ordered = last_n_balanced(ordered, 3, ordered=True)
        self.assertEqual([row["t"] for row in bal], [row["t"] for row in bal_ordered])
        pf = last_n_cost_pf(tape, 3, 0.10)
        pf_ordered = last_n_cost_pf(ordered, 3, 0.10, ordered=True, simple=True)
        self.assertAlmostEqual(pf["ratio"], pf_ordered["ratio"], places=6)
        self.assertEqual(pf["count"], pf_ordered["count"])
        windows = evaluation_windows(tape, 0.10)
        windows_ordered = evaluation_windows(ordered, 0.10, ordered=True, simple=True)
        self.assertEqual(set(windows), set(windows_ordered))
        for key in windows:
            self.assertAlmostEqual(windows[key]["pf"], windows_ordered[key]["pf"], places=6)
            self.assertEqual(windows[key]["n"], windows_ordered[key]["n"])
        dd = drawdown_time_by_symbol(tape)
        dd_ordered = drawdown_time_by_symbol(ordered, ordered=True)
        self.assertAlmostEqual(dd["maxS"], dd_ordered["maxS"], places=4)

    def test_historic_score_windows_and_winner_cover_full_catalog(self):
        from hist_calc import overlay_from_options, parse_options, expand_rows, pick_winner_row, _rank_set_rows
        from position_cost import EVALUATION_WINDOWS
        book = SetBook()
        book.load(overlay_from_options(parse_options({"hours": 1, "allConfigs": True})))
        self.assertGreaterEqual(len(book.by_idx), 1000)
        tape = []
        t = 1_700_000_000.0
        for i in range(24):
            side = "LONG" if i % 2 == 0 else "SHORT"
            symbol = ("XRP-USDT", "BCH-USDT", "SOL-USDT")[i % 3]
            tape.append({
                "t": t + i * 60,
                "side": side,
                "direction": side,
                "pnl": 0.02 if i % 3 else -0.01,
                "pnl_pct": 0.002 if i % 3 else -0.001,
                "hold_s": 60,
                "symbol": symbol,
                "reason": "tp" if i % 3 else "sl",
            })
        sample = book.by_idx[0]
        sample.hist = list(tape)
        book._score_one(sample)
        self.assertEqual(sample.n, 24)
        # Twelve closes per direction cannot meet the default last-30 Base gate.
        self.assertEqual(sample.evaluation_windows, {})
        self.assertFalse(sample.stage_ledger["base"])
        self.assertIn("LONG", sample.by_side)
        self.assertIn("SHORT", sample.by_side)
        ranked = _rank_set_rows(book)
        self.assertGreaterEqual(len(ranked), len(book.by_idx))
        winner = pick_winner_row(book, ranked)
        self.assertIsNotNone(winner)
        self.assertIn("last15Ratio", winner)
        rows = expand_rows(book, limit=5)
        self.assertLessEqual(len(rows), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
