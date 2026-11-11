#!/usr/bin/env python3
"""Independent synthetic oracle for Main short-window selection.

This tests candidate membership and entry-time voting, not market performance,
exchange execution, or whether the configured strategy is profitable.
"""
from __future__ import annotations

import bisect
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "pulse"))
from set_engine import SetBook, _main_short_choice, _main_windows  # noqa: E402


def pf(values):
    gain = sum(v for v in values if v > 0)
    loss = -sum(v for v in values if v < 0)
    return gain / loss if loss else (99.0 if gain > 0 else 0.0)


def oracle(ts, entries, nets, long_n, candidates, lookback_s, minimum, margin, full, floor):
    """Plain, non-prefix implementation using strictly earlier closes."""
    ends = [bisect.bisect_left(ts, t) for t in entries]
    starts = [bisect.bisect_left(ts, t - lookback_s) for t in entries]

    def passed(window, i):
        end = ends[i]
        vals = nets[max(starts[i], end - window):end]
        need = window if full else 1
        return len(vals) >= need and round(pf(vals), 4) + 1e-9 >= floor

    def choose(end, start):
        def outcomes(window):
            return [nets[i] for i in range(start, end) if passed(window, i)]

        ref = pf(outcomes(long_n))
        best, chosen = ref, long_n
        for window in candidates:
            vals = outcomes(window)
            value = pf(vals)
            if len(vals) >= minimum and value > best + 1e-12 and value >= ref * margin - 1e-12:
                best, chosen = value, window
        return chosen

    return choose(len(nets), 0), [choose(e, s) for e, s in zip(ends, starts)]


def main():
    rng = random.Random(20261031)
    cases = 0
    for long_n in (10, 20, 50):
        for ratio in (0.1, 0.25, 0.5, 1.0):
            for configured in ([5, 10, 15], [10, 15], [1, 2, 4], [40]):
                for full in (False, True):
                    book = SetBook()
                    book.load({"mainGateN": long_n, "mainShortMaxRatio": ratio, "mainShortWindows": configured,
                               "mainShortMin": 3, "mainShortMargin": 1.02, "minPfBySize": False,
                               "histLookbackAuto": False, "histLookbackBars": 120, "stageFullWindow": full})
                    params = book.stage_params("general")
                    expected_candidates = tuple(sorted(w for w in set(configured) if w <= int(long_n * ratio + 1e-9)))
                    assert params.short_windows == expected_candidates, (configured, params.short_windows, expected_candidates)
                    for _ in range(6):
                        ts = [1_700_000_000.0 + i * 300 for i in range(60)]
                        entries = [t - 1 for t in ts]
                        nets = [rng.choice([-0.005, -0.003, -0.001, 0.001, 0.004, 0.007]) for _ in ts]
                        ends, starts, passes = _main_windows(ts, nets, entries, params)
                        actual = _main_short_choice(nets, ends, starts, params, passes)
                        expected = oracle(ts, entries, nets, long_n, expected_candidates, params.lookback_s,
                                          3, 1.02, full, params.floor)
                        assert actual == expected, (long_n, ratio, configured, full, actual, expected)
                        cases += 1
    print(f"independent Main short-window oracle: {cases} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
