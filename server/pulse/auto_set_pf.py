"""Causal minimum-PF training for one independent Exec Set."""

from bisect import bisect_left, bisect_right
import math
from typing import Sequence

TRAINING_HOURS = 48
REFIT_SECONDS = 3600
DEFAULT_MIN_ADMITTED = 20
DEFAULT_FALLBACKS = {"minimal": 1.24, "short": 1.42}


def training_window(now: float) -> tuple[float, float]:
    end = math.floor(now / REFIT_SECONDS) * REFIT_SECONDS
    return end - TRAINING_HOURS * 3600, end


def observations(closes: Sequence[float], entries: Sequence[float], nets: Sequence[float],
                 previous: int, lower: float, upper: float) -> list[tuple[float, float]]:
    """A trade has a label only after it closes. Its signal uses only earlier closes."""
    end = bisect_left(closes, upper)
    start = bisect_left(closes, lower, 0, end)
    gains, losses = [0.0], [0.0]
    for i in range(end):
        gains.append(gains[-1] + max(0.0, nets[i]))
        losses.append(losses[-1] + max(0.0, -nets[i]))
    output = []
    for i in range(start, end):
        before = bisect_left(closes, entries[i], 0, i)
        if before < previous:
            continue
        gain = gains[before] - gains[before - previous]
        loss = losses[before] - losses[before - previous]
        if gain > 0:
            output.append((gain / loss if loss > 0 else math.inf, nets[i]))
    return output


def _outcomes(pairs: Sequence[tuple[float, float]], floors: tuple[float, ...]) -> list[tuple[int, float, float]]:
    """Bin each observation once, then accumulate the results at every floor."""
    buckets = [[0, 0.0, 0.0] for _ in floors]
    for entry_pf, net in pairs:
        index = bisect_right(floors, entry_pf + 1e-12) - 1
        if index >= 0:
            bucket = buckets[index]
            bucket[0] += 1
            bucket[1] += max(0.0, net)
            bucket[2] += max(0.0, -net)
    count = 0
    gain = loss = 0.0
    outcomes = []
    for n, g, l in reversed(buckets):
        count += n
        gain += g
        loss += l
        outcomes.append((count, gain, loss))
    return outcomes[::-1]


def fit(pairs: Sequence[tuple[float, float]], min_admitted: int, fallback: float,
        candidates: Sequence[float], tolerance: float, orders_factor: float) -> float | None:
    """Use a Set's own chronological folds to improve on its fallback.

    Give both folds enough admitted trades. Require positive net expectancy in
    both. Shrink short and loss-free samples before comparing PF and coverage.
    """
    minimum = max(3, (min_admitted + 1) // 2)
    if len(pairs) < 2 * minimum:
        return None
    middle = len(pairs) // 2
    floors = tuple(sorted({fallback, *candidates}))
    recent = _outcomes(pairs[middle:], floors)
    earlier = _outcomes(pairs[:middle], floors)
    best = None
    fallback_score = 0.0
    for floor, first, second in zip(floors, earlier, recent):
        if min(first[0], second[0]) < minimum:
            continue
        estimates = []
        for count, gain, loss in (first, second):
            pf = min(5.0, gain / loss) if loss > 0 else (5.0 if gain > 0 else 0.0)
            estimates.append((pf - 1) * count / (count + min_admitted))
        score = min(estimates)
        if score <= 0:
            continue
        count = first[0] + second[0]
        if orders_factor > 0:
            score *= (count / len(pairs)) ** orders_factor
        if floor == fallback:
            fallback_score = score
        candidate = (score, count, -floor)
        if best is None or candidate > best:
            best = candidate
    if best is None or best[0] <= fallback_score * (1 + tolerance) + 1e-12:
        return None
    return -best[2]
