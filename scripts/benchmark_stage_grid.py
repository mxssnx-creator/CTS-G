#!/usr/bin/env python3
"""Compare bounded and full stage queries while requiring identical admissions."""

import argparse
import json
import math
from pathlib import Path
import random
from statistics import median
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server/pulse"))
from stage_grid import StageGrid, StageHistory


class FullHistory(StageHistory):
    def _decision_tape(self, now):
        return self.root


def fixture(count):
    rng = random.Random(18)
    closes = [i * 6 for i in range(count)]
    entries = [t - (1_200 if i % 240 == 0 else rng.randrange(0, 36)) for i, t in enumerate(closes)]
    nets = [rng.choice((.004, .006, .008, -.003, -.005)) for _ in closes]
    grid = StageGrid.from_settings({
        "base": {"long": [10, 20], "short": [4, 8], "last": [1, 2]},
        "main": {"short": [4, 8], "last": [1, 2]},
        "real": {"short": [4, 8], "last": [1, 2]},
        "exec": {"short": [4, 8, 20, 30], "last": [1, 2]},
    })
    return (grid, closes, entries, nets, 1_200, lambda _n: 1.10), closes[-25]


def evaluate(kind, data, now):
    book = kind(*data)
    result = book.evaluate(now, streams=True, limit=24)
    return result, len(book._decision_tape(now).ids)


def same(left, right):
    assert (left.path, left.streams, left.row_ids, left.validated_base_ids) == (
        right.path, right.streams, right.row_ids, right.validated_base_ids
    )
    for a, b in zip(left.counts, right.counts):
        assert (a.evaluated, a.sample_full, a.qualified, a.orders) == (
            b.evaluated, b.sample_full, b.qualified, b.orders
        )
        assert math.isclose(a.gain, b.gain, abs_tol=1e-9)
        assert math.isclose(a.loss, b.loss, abs_tol=1e-9)


def measure(kind, data, now, trials):
    durations = []
    for _ in range(trials):
        started = time.perf_counter()
        evaluate(kind, data, now)
        durations.append(time.perf_counter() - started)
    tracemalloc.start()
    result, retained = evaluate(kind, data, now)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return result, {"median_ms": round(1000 * median(durations), 3), "peak_python_bytes": peak,
                    "decision_rows": retained, "qualified_exec_lanes": result.counts[-1].qualified}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=12_000)
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args()
    if args.rows < 1_800 or args.trials < 1:
        parser.error("at least 1,800 rows and one trial are required")
    data, now = fixture(args.rows)
    full, baseline = measure(FullHistory, data, now, args.trials)
    limited, bounded = measure(StageHistory, data, now, args.trials)
    same(full, limited)
    print(json.dumps({"identical": True, "input_rows": args.rows, "full": baseline, "bounded": bounded}, indent=2))


if __name__ == "__main__":
    main()
