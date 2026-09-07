#!/usr/bin/env python3
"""Focused no-network regressions for replay and indication hot paths."""
from __future__ import annotations

import os
import pathlib
import sys
import time
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
PULSE = ROOT / "server" / "pulse"
sys.path.insert(0, str(PULSE))
os.chdir(PULSE)

import indication_engine as indication  # noqa: E402
from indication_engine import (  # noqa: E402
    Candle,
    DEFAULT_SETTINGS,
    ExtraBook,
    IndicationBook,
    build_indication_frame,
)
import set_engine as set_engine_module  # noqa: E402
from set_engine import IND_TAG_KIND, SetBook, indication_kind_votes, indication_kind_votes_frame, synth_trend  # noqa: E402


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

    def test_vectorized_replay_matches_scalar_reference(self):
        if set_engine_module._np is None:
            self.skipTest("numpy is unavailable")
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

        def replay(vector):
            original = set_engine_module._np
            with patch.object(set_engine_module, "_np", original if vector else None):
                book = SetBook()
                book.load(overlay)
                book.ingest_bars("REFERENCE-USDT", bars)
                hist = {}
                counts = {}
                book.replay_symbol_partial(
                    "REFERENCE-USDT",
                    hist,
                    now=1_700_000_000.0,
                    drop_bars=False,
                    hist_counts=counts,
                )
                ids = [state.id for state in book.by_idx]
                signature = lambda rows: sorted(
                    (
                        row.get("t"),
                        row.get("side"),
                        round(float(row.get("pnl_pct") or 0), 12),
                        row.get("reason"),
                        row.get("hold_s"),
                    )
                    for row in rows
                )
                return {sid: signature(hist.get(sid) or []) for sid in ids}, {
                    sid: int(counts.get(sid) or 0) for sid in ids
                }

        vector, vector_counts = replay(True)
        scalar, scalar_counts = replay(False)
        self.assertEqual(vector, scalar)
        self.assertEqual(vector_counts, scalar_counts)

    def test_tile_merge_replaces_replayed_symbol_without_duplicates(self):
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
            "setStepMax": 1,
            "slToTpRatios": [0.6],
        }
        book = SetBook()
        book.load(overlay)
        state = book.by_idx[0]
        symbol = "MERGE-USDT"
        state.hist = [{"t": 1.0, "symbol": symbol, "side": "LONG", "pnl_pct": -0.002, "reason": "sl", "hold_s": 60}]
        book._hist_counts = {state.id: {symbol: 1}}
        replacement = {"t": 2.0, "symbol": symbol, "side": "LONG", "pnl_pct": 0.003, "reason": "tp", "hold_s": 60}
        for _ in range(2):
            book._commit_hist(
                {state.id: [replacement]},
                merge=True,
                replayed_symbols=[symbol],
                hist_counts={state.id: 1},
                replayed_set_ids=[state.id],
                score=False,
            )
        self.assertEqual(len(state.hist), 1)
        self.assertEqual(state.hist[0]["t"], 2.0)
        self.assertEqual(state.n, 1)
        book._commit_hist(
            {},
            merge=True,
            replayed_symbols=[symbol],
            hist_counts={},
            replayed_set_ids=[state.id],
            score=False,
        )
        self.assertEqual(state.hist, [])
        self.assertEqual(state.n, 0)

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
        self.assertIn("replay_pool_workers", hist_source)
        self.assertIn("queue_limit = max(workers, workers * REPLAY_QUEUE_MULTIPLIER)", hist_source)
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
        self.assertEqual(set(sample.evaluation_windows), {f"last{n}" for n in EVALUATION_WINDOWS})
        self.assertEqual(sample.evaluation_windows["last15"]["n"], 15)
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
