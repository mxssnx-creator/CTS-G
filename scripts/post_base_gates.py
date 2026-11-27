"""Last-N admission for the offline Base → Exec experiment."""
from collections import defaultdict
import math
from typing import NamedTuple

import numpy as np
import stage_eval as sg
from stage_grid import NEG_EPS, StageGrid


BASE_FLOORS = (1.5, 2.0, 2.5)
EXEC_FLOORS = (2.0, 2.5)
BASE_WINDOWS = StageGrid.from_settings({"enabled": True}).windows(0, 0)
MAX_BASE_CLOSED = max(w.need for w in BASE_WINDOWS)


class ExecWindow(NamedTuple):
    short: int
    short_mode: str
    last: int
    last_mode: str


EXEC_WINDOWS = tuple(
    ExecWindow(short, short_mode, last, last_mode)
    for short in (8, 12, 16, 20)
    for short_mode in (("positive", "negative") if short in (8, 12) else ("positive",))
    for last in range(1, 6)
    for last_mode in ("positive", "negative")
)
PORTFOLIO = len(EXEC_WINDOWS)
MIN_EXEC_CLOSED = min(w.short + w.last for w in EXEC_WINDOWS)
MAX_EXEC_CLOSED = max(w.short + w.last for w in EXEC_WINDOWS)


def window_name(window):
    return f"{window.short}{window.short_mode[0]}-{window.last}{window.last_mode[0]}"


def path_name(set_id, base, execution):
    window = BASE_WINDOWS[base]
    return f"{set_id}/base:{window.long}-{window_name(window)}/exec:0-{window_name(EXEC_WINDOWS[execution])}"


def indices(bits, length):
    packed = np.frombuffer(bits.to_bytes((length + 7) // 8, "little"), dtype=np.uint8)
    return np.flatnonzero(np.unpackbits(packed, bitorder="little", count=length))


def fixed_history(book, st, columns, floor, probes=()):
    # Native masks answer a historical entry and a decision query identically.
    # Decision probes have a close later than every real close and decision.
    # They can never enter a window, and are stripped from child membership.
    close, entry, gross = columns
    cf = sg.cost_as_frac(book.cost_pct)
    net = [value - cf for value in gross]
    if len(probes):
        sentinel = max(close[-1] if len(close) else 0, probes[-1]) + 60
        close = (*close, *([sentinel] * len(probes)))
        entry = (*entry, *probes)
        net.extend([0.0] * len(probes))
    history = book.grid_history(st, closes=close, entries=entry, nets=net)
    history.floors = dict.fromkeys(history.floors, floor)
    history.window_auto = None
    params = book.stage_params(st.pack, st)
    if history.base_risk and not params.consistency and not book.stop_cap_exceeded(st):
        times, entries = np.asarray(close), np.asarray(entry)
        right = np.searchsorted(times, entries)
        low = np.searchsorted(times, entries - params.lookback_s)
        visible = right > low
        oldest = np.maximum(low, right - MAX_BASE_CLOSED)
        # The longest possible drawdown is bounded by the entire window span.
        # Retain the native check whenever any decision can reach that limit.
        if not np.any(entries[visible] - times[oldest[visible]] >= params.max_dd_s):
            history.base_risk = None
    return history


def block_totals(tape, end, count, offset, prefixes):
    right, inverse = np.unique(end - offset, return_inverse=True)
    left = right - count
    gp, lp = prefixes
    gain = gp[right] - gp[left]
    loss = lp[right] - lp[left]
    # Match the native reader near zero and at a PF boundary. Prefix arithmetic
    # must not decide which lane a borderline sample can enter.
    repair = (
        ((gp[right] > 0) & (gain <= 1e-10 * gp[right]))
        | ((lp[right] > 0) & (loss <= 1e-10 * lp[right]))
        | (np.abs(np.abs(gain - loss) - NEG_EPS) <= 1e-8 * np.maximum(gain, loss))
    )
    for floor in EXEC_FLOORS:
        for numerator, denominator in ((gain, loss), (loss, gain)):
            threshold = denominator * floor
            repair |= np.abs(numerator - threshold) <= 1e-8 * np.maximum(numerator, threshold)
    for i in np.flatnonzero(repair):
        values = tape.values[int(left[i]):int(right[i])]
        gain[i] = math.fsum(v for v in values if v > 0)
        loss[i] = math.fsum(-v for v in values if v < 0)
    return gain[inverse], loss[inverse]


def oriented(gain, loss, mode):
    return (loss, gain) if mode == "negative" else (gain, loss)


def signed_pass(gain, loss, floor):
    return (gain - loss > NEG_EPS) & (gain >= floor * loss)


def terminal_pass(tape, window, now, floor):
    low, high = tape.indices(now)
    if high - low < window.short + window.last:
        return False
    for size, end, mode in ((window.last, high, window.last_mode),
                            (window.short, high - window.last, window.short_mode)):
        values = tape.values[end - size:end]
        gain = math.fsum(v for v in values if v > 0)
        loss = math.fsum(-v for v in values if v < 0)
        if not signed_pass(*oriented(gain, loss, mode), floor):
            return False
    return True


class SetDecisions:
    """Fixed-floor Base and Exec results for a single independent strategy Set."""

    def __init__(self, book, st, columns, clocks, floor):
        self.scores = np.zeros((len(EXEC_FLOORS), PORTFOLIO, len(clocks)))
        self.parents = np.full(self.scores.shape, -1, dtype=np.int16)
        self.full = np.zeros((PORTFOLIO, len(clocks)), dtype=bool)
        self.base_full = np.zeros(len(BASE_WINDOWS), dtype=np.int64)
        self.base_pass = np.zeros(len(BASE_WINDOWS), dtype=np.int64)
        self.base_valid = np.zeros(len(clocks), dtype=bool)
        close = np.asarray(columns[0])
        lookback = book.stage_params(st.pack, st).lookback_s
        available = np.searchsorted(close, clocks) - np.searchsorted(close, clocks - lookback)
        sizes = np.bincount(available, minlength=MAX_BASE_CLOSED + 1)
        full_counts = sizes[::-1].cumsum()[::-1]
        self.base_full[:] = [full_counts[w.need] for w in BASE_WINDOWS]
        if not self.base_full.any() or book.stop_cap_exceeded(st):
            return

        n = len(close)
        history = fixed_history(book, st, columns, floor, clocks)
        root = history.root
        real = (1 << n) - 1
        earlier = (1 << int(np.searchsorted(close, clocks[0]))) - 1
        groups = defaultdict(list)
        valid = 0
        for i, window in enumerate(BASE_WINDOWS):
            # Avoid constructing historical masks for Base choices with no
            # possible first decision in this replay period.
            if not self.base_full[i]:
                continue
            hits = root.child_mask(window, 0)
            now = hits >> n
            self.base_pass[i] = now.bit_count()
            valid |= now
            if now and (members := hits & real).bit_count() >= MIN_EXEC_CLOSED:
                older = members & earlier
                if older.bit_count() > MAX_EXEC_CLOSED:
                    for _ in range(MAX_EXEC_CLOSED):
                        older ^= 1 << (older.bit_length() - 1)
                    members ^= older
                groups[members].append((i, now))
        self.base_valid[indices(valid, len(clocks))] = True

        for members, paths in groups.items():
            # Each Base path keeps its own history. Identical membership shares
            # calculations, but only currently qualifying parents can win.
            owner = np.full(len(clocks), -1, dtype=np.int16)
            for base, valid_at in reversed(paths):
                owner[indices(valid_at, len(clocks))] = base
            at = np.flatnonzero(owner >= 0)
            child = history.child(root, members)
            self._confirm(child, at, clocks[at], owner[at])

    def _confirm(self, tape, at, when, parent):
        times = np.asarray(tape.times)
        prefixes = np.asarray(tape.gains), np.asarray(tape.losses)
        end = np.searchsorted(times, when)
        available = end - np.searchsorted(times, when - tape.owner.lookback)
        calculations = {}
        for i, window in enumerate(EXEC_WINDOWS):
            need = window.short + window.last
            enough = available >= need
            if not enough.any():
                continue
            clocks = at[enough]
            chosen_parent = parent[enough]
            self.full[i, clocks] = True
            # The calculation keys include the disjoint block boundaries.
            short_key = (window.short, window.last)
            if short_key not in calculations:
                tail = end[enough]
                calculations[short_key] = (
                    block_totals(tape, tail, window.short, window.last, prefixes),
                    block_totals(tape, tail, window.last, 0, prefixes),
                )
            short, last = calculations[short_key]
            sgain, sloss = oriented(*short, window.short_mode)
            lgain, lloss = oriented(*last, window.last_mode)
            gain, loss = sgain + lgain, sloss + lloss
            pf = np.full(len(gain), 5.0)
            np.divide(gain, loss, out=pf, where=loss > 0)
            # Use the native sample-size discount with sign-aligned PF for
            # selection only. Execution PnL and reported PF use actual signs.
            score = np.round((np.minimum(5.0, pf) - 1.0) * need / (20 + need), 12)
            for j, floor in enumerate(EXEC_FLOORS):
                ok = signed_pass(sgain, sloss, floor) & signed_pass(lgain, lloss, floor)
                seen = self.scores[j, i, clocks]
                seen_parent = self.parents[j, i, clocks]
                wins = ok & ((score > seen) | ((score == seen) & (chosen_parent < seen_parent)))
                self.scores[j, i, clocks[wins]] = score[wins]
                self.parents[j, i, clocks[wins]] = chosen_parent[wins]
