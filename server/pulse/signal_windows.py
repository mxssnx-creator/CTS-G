"""Independent Signals window policies. All inputs are already net of costs.

The most recent minimal window is a condition, not an inverse strategy. The
long and short evaluations end before it in BOTH polarities. Negative windows
test loss/gain, without changing the sign of any recorded result. No loss is
removed from cumulative accounting and no equal-clock close qualifies an entry.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from itertools import product
import math

SIGNAL_MIN_PF = 2.2
STRATEGY_RANGES = ("signals", "exit")
DEFAULT_WINDOWS = {"long": [5, 10, 15, 20], "short": [4, 6, 8, 10],
                   "minimal": [1, 2, 3], "polarity": ["positive", "negative"], "shortRatio": .5}


@dataclass(frozen=True, slots=True)
class Rule:
    long: int
    short: int
    minimal: int
    polarity: str
    short_polarity: str = "positive"

    @property
    def id(self):
        suffix = "" if self.short_polarity == "positive" else "-short-negative"
        return f"l{self.long}-s{self.short}-m{self.minimal}-{self.polarity}{suffix}"


def rules(raw=None):
    raw = DEFAULT_WINDOWS if raw is None else raw
    if set(raw) - {"shortPolarity"} != set(DEFAULT_WINDOWS):
        raise ValueError("Signals windows require long, short, minimal, polarity and shortRatio")
    for key in ("long", "short", "minimal"):
        values = raw[key]
        if not isinstance(values, list) or not values or len(set(values)) != len(values):
            raise ValueError(f"empty/duplicate Signals {key} windows")
        if any(type(n) is not int or not 1 <= n <= (3 if key == "minimal" else 200) for n in values):
            raise ValueError(f"invalid Signals {key} window")
    if (not isinstance(raw["polarity"], list) or not raw["polarity"] or
            len(set(raw["polarity"])) != len(raw["polarity"]) or
            any(x not in ("positive", "negative") for x in raw["polarity"])):
        raise ValueError("unsupported Signals minimal polarity")
    short_polarity = raw.get("shortPolarity", ["positive"])
    if (not isinstance(short_polarity, list) or not short_polarity or
            len(set(short_polarity)) != len(short_polarity) or
            any(x not in ("positive", "negative") for x in short_polarity)):
        raise ValueError("unsupported Signals short polarity")
    if type(raw["shortRatio"]) not in (int, float) or not math.isfinite(raw["shortRatio"]) or not 0 < raw["shortRatio"] <= .5:
        raise ValueError("Signals short/long ratio must be positive and at most 0.5")
    return tuple(Rule(a, b, n, mode, short_mode) for a, b, n, mode, short_mode in product(
        sorted(raw["long"]), sorted(raw["short"]), sorted(raw["minimal"]), raw["polarity"], short_polarity)
        if b <= a * raw["shortRatio"])


def policy_listing(raw=None):
    raw = DEFAULT_WINDOWS if raw is None else raw
    allowed = rules(raw)
    return {"rules": [{"id": r.id, "long": r.long, "short": r.short,
            "minimal": r.minimal, "polarity": r.polarity, "shortPolarity": r.short_polarity} for r in allowed],
            "ruleCount": len(allowed), "excludedPairs": [{"long": a, "short": b, "reason": "short exceeds 50% of long"}
                for a, b in product(sorted(raw["long"]), sorted(raw["short"])) if b > a * raw["shortRatio"]],
            "windowBoundary": "long and short end before minimal; the full long-plus-minimal window must also meet the positive PF floor"}


def metrics(values):
    gain = math.fsum(v for v in values if v > 0)
    loss = -math.fsum(v for v in values if v < 0)
    total = peak = dd = 0.0
    for value in values:
        total += value
        peak = max(peak, total)
        dd = max(dd, peak - total)
    state = "empty" if not values else "finite" if loss else "no-loss" if gain else "flat"
    return {"n": len(values), "gain": gain, "loss": loss, "net": gain-loss,
            "pf": gain/loss if loss else 1.0 if state == "flat" else None,
            "pfState": state, "maxReturnDrawdown": dd}


def positive(m, n, floor, max_dd):
    return (m["n"] == n and m["gain"] > 0 and
            (m["loss"] == 0 or m["pf"] >= floor) and m["maxReturnDrawdown"] <= max_dd)


def negative(m, n, floor):
    """Apply the same floor to a negative condition using loss/gain.

    The returns remain signed in every history and execution statistic.
    Drawdown is not used as a second sign test on a negative condition.
    """
    return (m["n"] == n and m["loss"] > 0 and
            (m["gain"] == 0 or m["loss"] >= floor * m["gain"]))


class History:
    """Bounded ordered tail with indexed clocks and per-version window memo.

    Retain one extra close for strict-clock exclusion. A stage holds only one
    position; duplicate close timestamps are rejected. Memory does not grow
    with the run length. Cumulative sums include every recorded loss.
    """
    __slots__ = ("times", "values", "capacity", "count", "gain", "loss", "cache", "version")

    def __init__(self, capacity):
        self.times, self.values = [], []
        self.capacity = capacity + 1
        self.count, self.gain, self.loss, self.version = 0, 0.0, 0.0, 0
        self.cache = {}

    def record(self, t, value):
        if not math.isfinite(t) or not math.isfinite(value) or (self.times and t <= self.times[-1]):
            raise ValueError("Signals closes must be finite and strictly chronological per lane")
        self.times.append(t); self.values.append(value)
        if len(self.times) > self.capacity:
            del self.times[0]; del self.values[0]
        self.count += 1
        self.gain += max(value, 0); self.loss += max(-value, 0)
        self.version += 1
        self.cache.clear()

    def window(self, now, history_ms, n, skip=0):
        lo = bisect_left(self.times, now-history_ms)
        end = max(lo, bisect_left(self.times, now)-skip)
        begin = max(lo, end-n)
        key = (begin, end)
        if key not in self.cache:
            self.cache[key] = metrics(self.values[begin:end])
        return self.cache[key]

    def evaluate(self, now, history_ms, policy, floor=SIGNAL_MIN_PF, max_dd=1):
        if not math.isfinite(floor) or floor <= 1:
            raise ValueError("Signals evaluations require a PF floor above 1")
        lo, hi = bisect_left(self.times, now-history_ms), bisect_left(self.times, now)
        if not policy or hi-lo < min(r.long+r.minimal for r in policy):
            return {}
        # A mutable policy or recycled object id must never reuse another
        # configuration's qualification. Frozen Rule values define identity.
        memo = ("rules", tuple(policy), lo, hi, floor, max_dd)
        if memo in self.cache:
            return self.cache[memo]
        passed = {}
        for rule in policy:
            minimal = self.window(now, history_ms, rule.minimal)
            if minimal["n"] != rule.minimal:
                continue
            if rule.polarity == "positive":
                if not positive(minimal, rule.minimal, floor, max_dd):
                    continue
            elif not negative(minimal, rule.minimal, floor):
                continue
            long = self.window(now, history_ms, rule.long, rule.minimal)
            short = self.window(now, history_ms, rule.short, rule.minimal)
            overall = self.window(now, history_ms, rule.long + rule.minimal)
            short_ok = (positive(short, rule.short, floor, max_dd) if rule.short_polarity == "positive"
                        else negative(short, rule.short, floor))
            if (positive(long, rule.long, floor, max_dd) and short_ok and
                    positive(overall, rule.long + rule.minimal, floor, max_dd)):
                passed[rule.id] = {"long": long, "short": short, "minimal": minimal,
                                   "overall": overall, "minimalPolarity": rule.polarity,
                                   "shortPolarity": rule.short_polarity, "floor": floor}
        self.cache[memo] = passed
        return passed

    def cumulative(self):
        state = "empty" if not self.count else "finite" if self.loss else "no-loss" if self.gain else "flat"
        return {"n": self.count, "gain": self.gain, "loss": self.loss, "net": self.gain-self.loss,
                "pf": self.gain/self.loss if self.loss else 1.0 if state == "flat" else None, "pfState": state}
