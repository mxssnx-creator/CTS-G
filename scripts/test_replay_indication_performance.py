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
        self.assertIn("replay_workers = max(1, min(8, cpu, len(names) or 1))", pulse_source)
        self.assertIn("max_workers=w", set_source)
        self.assertIn("generation", pulse_source)
        self.assertIn("def should_abort", pulse_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
