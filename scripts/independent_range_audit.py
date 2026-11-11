"""Independent stage accounting over supplied candidate closes. Standard library only.

No production-engine imports or PF helpers. Signal generation, entry opportunities
and hold/exit selection are INPUTS, not independently reconstructed here. The
fixed range branches are shadow experiments, not funded executable portfolios.
"""
from __future__ import annotations

import bisect
import math


def figures(values):
    gain = math.fsum(v for v in values if v > 0)
    loss = -math.fsum(v for v in values if v < 0)
    return {"n": len(values), "gain": gain, "loss": loss,
            "pf": gain / loss if loss else None,
            "pfState": "finite" if loss else "no-loss" if gain else "flat" if values else "empty"}


def gate_pf(values):
    f = figures(values)
    return f["pf"] if f["pf"] is not None else 99.0 if f["gain"] else 0.0


def floor(p, n):
    if p.get("size_floors"):
        return next((value for bound, value in p["size_floors"] if n <= bound), p["long_floor"])
    return p["floor"]


def sign(values, positive, both=False, average=False):
    if average:
        total = math.fsum(values)
        return abs(total) > 1e-12 if both else total > 1e-12 if positive else total < -1e-12
    return (all(v > 0 for v in values) or all(v < 0 for v in values)) if both else all(v > 0 if positive else v < 0 for v in values)


class Tape:
    def __init__(self, times, values):
        self.times, self.values = times, values

    def window(self, now, seconds, n):
        end = bisect.bisect_left(self.times, now)
        start = max(bisect.bisect_left(self.times, now - seconds), end - n)
        return self.values[start:end], self.times[start:end]


def passes(tape, now, n, p, minimum=None):
    values, _ = tape.window(now, p["lookback_s"], n)
    need = minimum if minimum is not None else n if p["full_window"] else 1
    return len(values) >= need and round(gate_pf(values), 4) + 1e-9 >= floor(p, n)


def minimal(tape, now, p, prefix):
    n = p.get(prefix + "_min_n", 0)
    if not n:
        return True
    values, _ = tape.window(now, p["lookback_s"], n)
    return len(values) >= (n if p["full_window"] else 1) and sign(
        values, p.get(prefix + "_min_positive", True), p.get(prefix + "_min_both", False), p.get(prefix + "_min_avg", False))


def base_pass(tape, now, p):
    values, times = tape.window(now, p["lookback_s"], p["base_long"])
    if not passes(tape, now, p["base_long"], p, p["gate_min"]):
        return False
    if p["consistency"] > 0:
        half = len(values) // 2
        if min(gate_pf(values[:half]), gate_pf(values[half:])) + 1e-9 < p["consistency"]:
            return False
    eq = peak = longest = 0.0
    in_drawdown = False
    peak_at = times[0] if times else now
    for t, value in zip(times, values):
        eq += value
        if eq >= peak - 1e-12:
            if in_drawdown:
                longest = max(longest, t - peak_at)
                in_drawdown = False
            peak = max(peak, eq)
            peak_at = t
        else:
            in_drawdown = True
    if eq < peak - 1e-12:
        longest = max(longest, now - peak_at)
    if longest >= p["max_dd_s"]:
        return False
    if p["base_short"] and not passes(tape, now, p["base_short"], p):
        return False
    return minimal(tape, now, p, "base")


def reconstruct(times, entries, values, p, now):
    """Exact adaptive chain + separate fixed-N positive and Negative branches."""
    if not (len(times) == len(entries) == len(values)):
        raise ValueError("unaligned evidence")
    if any(not math.isfinite(v) for col in (times, entries, values) for v in col):
        raise ValueError("nonfinite evidence")
    if any(a > b for a, b in zip(times, times[1:])) or any(t < e for t, e in zip(times, entries)):
        raise ValueError("invalid chronology")
    raw = Tape(times, values)
    main = [i for i, e in enumerate(entries) if base_pass(raw, e, p)]
    mt = [times[i] for i in main]
    mv = [values[i] for i in main]
    main_tape = Tape(mt, mv)
    windows = sorted(set([p["main_n"], *p["short_windows"]]))
    votes = {n: [passes(main_tape, entries[i], n, p) for i in main] for n in windows}

    def choose(clock):
        lo, hi = bisect.bisect_left(mt, clock - p["lookback_s"]), bisect.bisect_left(mt, clock)
        reference = gate_pf([mv[j] for j in range(lo, hi) if votes[p["main_n"]][j]])
        best, selected = reference, p["main_n"]
        if p["main_on"] and p["short_on"]:
            for n in p["short_windows"]:
                sample = [mv[j] for j in range(lo, hi) if votes[n][j]]
                pf = gate_pf(sample)
                if len(sample) >= p["short_min"] and pf > best + 1e-12 and pf >= reference * p["short_margin"] - 1e-12:
                    best, selected = pf, n
        return selected

    chosen = [choose(entries[i]) for i in main]
    positive = [i for j, i in enumerate(main) if not p["main_on"] or (
        votes[p["main_n"]][j] and (not p["short_on"] or votes[chosen[j]][j]) and minimal(main_tape, entries[i], p, "main"))]
    negative_by_n = {}
    if p["neg_on"]:
        for n in p["neg_windows"]:
            accepted = []
            for i in main:
                sample, _ = main_tape.window(entries[i], p["lookback_s"], n)
                if len(sample) >= n and math.fsum(sample) < -1e-12:
                    accepted.append(i)
            negative_by_n[n] = accepted
    negative = sorted(set(i for branch in negative_by_n.values() for i in branch))
    cutoff = now - p["lookback_s"]
    visible = lambda indices: [i for i in indices if cutoff <= times[i] < now]
    legacy = {"main": visible(main), "real": visible(positive), "realneg": visible(negative), "short_n": choose(now)}
    branches = {}
    for n in windows if p["main_on"] and p["short_on"] else [p["main_n"]]:
        branches[f"positive:main{p['main_n']}:short{n}:real{p['real_n']}"] = (
            n, [i for j, i in enumerate(main) if not p["main_on"] or
                (votes[p["main_n"]][j] and (not p["short_on"] or votes[n][j]) and minimal(main_tape, entries[i], p, "main"))], False)
    for n, indices in negative_by_n.items():
        branches[f"negative:main{n}:real{p['real_n']}"] = (n, indices, True)
    base_ok = base_pass(raw, now, p)
    base_values, _ = raw.window(now, p["lookback_s"], p["base_long"])
    output = []
    for lane, (n, accepted, negative_lane) in branches.items():
        real_tape = Tape([times[i] for i in accepted], [values[i] for i in accepted])
        if negative_lane:
            sample, _ = main_tape.window(now, p["lookback_s"], n)
            main_ok = len(sample) >= n and math.fsum(sample) < -1e-12
        else:
            main_ok = not p["main_on"] or (passes(main_tape, now, p["main_n"], p)
                and (not p["short_on"] or passes(main_tape, now, n, p)) and minimal(main_tape, now, p, "main"))
        main_values, _ = main_tape.window(now, p["lookback_s"], n if negative_lane else p["main_n"])
        real_values, _ = real_tape.window(now, p["lookback_s"], p["real_n"])
        real_ok = not p["real_on"] or passes(real_tape, now, p["real_n"], p)
        matured = [i for i in accepted if entries[i] >= cutoff and times[i] < now]
        admitted = [i for i in matured if not p["real_on"] or passes(real_tape, entries[i], p["real_n"], p)]
        output.append({"lane": lane, "base": {"qualified": base_ok, **figures(base_values)},
                       "main": {"evaluated": base_ok, "qualified": base_ok and main_ok, **figures(main_values)},
                       "real": {"evaluated": base_ok and main_ok, "qualified": base_ok and main_ok and real_ok, **figures(real_values)},
                       "shadowCloses": {"base": len(visible(range(len(times)))), "main": len(visible(main)), "real": len(visible(accepted))},
                       "subsequentRealAdmittedShadow": figures([values[i] for i in admitted]),
                       "positionCount": None, "venueOrders": 0})
    return legacy, output
