"""Last-N admission for the offline Base → Exec experiment."""
from collections import defaultdict
import math
from typing import NamedTuple

import numpy as np
import stage_eval as sg
from stage_grid import NEG_EPS, SIGN_MODES, StageGrid, window_blocks


BASE_FLOORS = (1.5, 2.0, 2.5)
EXEC_FLOORS = (2.0, 2.5)
BASE_WINDOWS = StageGrid.from_settings({"enabled": True}).windows(0, 0)
MAX_BASE_CLOSED = max(w.need for w in BASE_WINDOWS)


class ExecWindow(NamedTuple):
    short: int
    short_mode: str
    last: int
    last_mode: str


class ForwardProof(NamedTuple):
    count: int
    floor: float

    def rank_at(self, tape, end):
        _, gain, loss = tape.evidence(self.count, end)
        if loss and math.isclose(gain, self.floor * loss, rel_tol=1e-8):
            values = tape.values[end - self.count:end]
            gain = math.fsum(v for v in values if v > 0)
            loss = math.fsum(-v for v in values if v < 0)
        if gain <= loss or gain < self.floor * loss:
            return 0.
        profit_factor = min(5., gain / loss) if loss else 5.
        return (profit_factor - 1.) * self.count / (20 + self.count)

    def allows(self, tape, now):
        start, end = tape.indices(now)
        return end - start >= self.count and self.rank_at(tape, end) > 0


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
SHORT_BLOCKS = tuple(dict.fromkeys((w.short, w.last) for w in EXEC_WINDOWS))
LAST_BLOCKS = tuple(dict.fromkeys(w.last for w in EXEC_WINDOWS))
SHORT_BLOCK_INDEX = np.array([SHORT_BLOCKS.index((w.short, w.last)) for w in EXEC_WINDOWS])
LAST_BLOCK_INDEX = np.array([LAST_BLOCKS.index(w.last) for w in EXEC_WINDOWS])
SHORT_NEGATIVE = np.array([w.short_mode == "negative" for w in EXEC_WINDOWS])[:, None]
LAST_NEGATIVE = np.array([w.last_mode == "negative" for w in EXEC_WINDOWS])[:, None]
NEEDED = np.array([w.short + w.last for w in EXEC_WINDOWS])[:, None]
BLOCK_SIZES = np.array([*[size for size, _ in SHORT_BLOCKS], *LAST_BLOCKS])[:, None]
BLOCK_OFFSETS = np.array([*[offset for _, offset in SHORT_BLOCKS], *([0] * len(LAST_BLOCKS))])[:, None]


def window_name(window):
    return f"{window.short}{window.short_mode[0]}-{window.last}{window.last_mode[0]}"


def path_name(set_id, base, execution):
    window = BASE_WINDOWS[base]
    return f"{set_id}/base:{window.long}-{window_name(window)}/exec:0-{window_name(EXEC_WINDOWS[execution])}"


def indices(bits, length):
    packed = np.frombuffer(bits.to_bytes((length + 7) // 8, "little"), dtype=np.uint8)
    return np.flatnonzero(np.unpackbits(packed, bitorder="little", count=length))


def fixed_history(book, st, columns, floor, probes=(), *, returns_are_net=False):
    # Native masks answer a historical entry and a decision query identically.
    # Decision probes have a close later than every real close and decision.
    # They can never enter a window, and are stripped from child membership.
    close, entry, gross = columns
    cf = 0 if returns_are_net else sg.cost_as_frac(book.cost_pct)
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


def block_totals(tape, end):
    # Compute all disjoint blocks on each distinct close prefix in one pass.
    right = np.maximum(end[None, :] - BLOCK_OFFSETS, 0)
    left = np.maximum(right - BLOCK_SIZES, 0)
    gp, lp = np.asarray(tape.gains), np.asarray(tape.losses)
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
    repair &= right >= BLOCK_SIZES
    for row, col in zip(*np.nonzero(repair)):
        values = tape.values[int(left[row, col]):int(right[row, col])]
        gain[row, col] = math.fsum(v for v in values if v > 0)
        loss[row, col] = math.fsum(-v for v in values if v < 0)
    return gain, loss


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


class BaseMasks:
    """Share native fixed-floor block verdicts across equal close prefixes."""

    def __init__(self, history, params, floor):
        self.tape, self.params, self.floor = history.root, params, floor
        self.clocks = np.asarray(history.entries)
        self.times = np.asarray(self.tape.times)
        right = np.searchsorted(self.times, self.clocks)
        self.available = right - np.searchsorted(self.times, self.clocks - history.lookback)
        self.right = right
        self.gains, self.losses = np.asarray(self.tape.gains), np.asarray(self.tape.losses)
        values = np.asarray(self.tape.values)
        self.wins = np.r_[0, np.cumsum(values > 0)]
        self.defeats = np.r_[0, np.cumsum(values < 0)]
        self.cache = {}

    def mask(self, size, offset, kind):
        key = size, offset, kind
        if key not in self.cache:
            self.cache[key] = self._mask(size, offset, kind)
        return self.cache[key]

    def _mask(self, size, offset, kind):
        ready = self.available >= size + offset
        if not ready.any():
            return 0
        rows = np.flatnonzero(ready)
        right, inverse = np.unique(self.right[rows] - offset, return_inverse=True)
        left = right - size
        gain = self.gains[right] - self.gains[left]
        loss = self.losses[right] - self.losses[left]
        # An absent sign is exactly zero. Repair cancellation only when that
        # sign actually occurs or could affect a floor or sign comparison.
        have_gain = self.wins[right] != self.wins[left]
        have_loss = self.defeats[right] != self.defeats[left]
        gain[~have_gain] = 0
        loss[~have_loss] = 0
        numerator, denominator = oriented(gain, loss, kind)
        threshold = denominator * self.floor
        pass_at = (numerator > 0) & (numerator >= threshold)
        repair = ((have_loss & (loss <= 1e-10 * self.losses[right]))
                  | (have_gain & (gain <= 1e-10 * self.gains[right]))
                  | ((numerator > 0) & (denominator > 0)
                     & (np.abs(numerator - threshold) <= 1e-8 * np.maximum(numerator, threshold))))
        if kind in SIGN_MODES:
            margin = numerator - denominator
            pass_at &= margin > NEG_EPS
            repair |= np.abs(np.abs(margin) - NEG_EPS) <= 1e-8 * np.maximum(gain, loss)
        pf_kind = "pf" if kind == "risk" else kind
        for i in np.flatnonzero(repair):
            pass_at[i] = self.tape.test(size, int(right[i]), pf_kind, 0, offset) is not None
        valid = pass_at[inverse]
        risk = self.tape.owner.base_risk
        if kind == "risk" and risk:
            start = left[inverse]
            affected = valid & ((self.clocks[rows] - self.times[start] >= self.params.max_dd_s)
                                | (self.params.consistency > 0))
            for i in np.flatnonzero(affected):
                valid[i] = risk(self.tape.times, self.tape.values, self.tape.gains, self.tape.losses,
                                self.clocks[rows[i]], int(right[inverse[i]]), int(start[i]))
        ready[rows] = valid
        return int.from_bytes(np.packbits(ready, bitorder="little").tobytes(), "little")

    def members(self, window):
        allowed = (1 << len(self.clocks)) - 1
        for size, offset, kind in window_blocks(window, 0):
            allowed &= self.mask(size, offset, kind)
            if not allowed:
                break
        return allowed & self.mask(window.need, 0, "pf") if allowed else 0


class SetDecisions:
    """Fixed-floor Base and Exec results for a single independent strategy Set."""

    def __init__(self, book, st, columns, clocks, floor, proof=None, *, returns_are_net=False):
        self.scores = np.zeros((len(EXEC_FLOORS), PORTFOLIO, len(clocks)))
        self.parents = np.full(self.scores.shape, -1, dtype=np.int16)
        self.full = np.zeros((PORTFOLIO, len(clocks)), dtype=bool)
        self.base_full = np.zeros(len(BASE_WINDOWS), dtype=np.int64)
        self.base_pass = np.zeros(len(BASE_WINDOWS), dtype=np.int64)
        self.base_valid = np.zeros(len(clocks), dtype=bool)
        close = np.asarray(columns[0])
        params = book.stage_params(st.pack, st)
        available = np.searchsorted(close, clocks) - np.searchsorted(close, clocks - params.lookback_s)
        sizes = np.bincount(available, minlength=MAX_BASE_CLOSED + 1)
        full_counts = sizes[::-1].cumsum()[::-1]
        self.base_full[:] = [full_counts[w.need] for w in BASE_WINDOWS]
        if not self.base_full.any() or book.stop_cap_exceeded(st):
            return

        n = len(close)
        history = fixed_history(book, st, columns, floor, clocks, returns_are_net=returns_are_net)
        root = history.root
        masks = BaseMasks(history, params, floor)
        real = (1 << n) - 1
        earlier = (1 << int(np.searchsorted(close, clocks[0]))) - 1
        groups = defaultdict(list)
        valid = 0
        keep = max(MAX_EXEC_CLOSED, proof.count if proof else 0)
        for i, window in enumerate(BASE_WINDOWS):
            # Avoid constructing historical masks for Base choices with no
            # possible first decision in this replay period.
            if not self.base_full[i]:
                continue
            hits = masks.members(window)
            now = hits >> n
            self.base_pass[i] = now.bit_count()
            valid |= now
            if now and (members := hits & real).bit_count() >= MIN_EXEC_CLOSED:
                older = members & earlier
                if older.bit_count() > keep:
                    for _ in range(keep):
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
            self._confirm(child, at, clocks[at], owner[at], proof)

    def _confirm(self, tape, at, when, parent, proof):
        times = np.asarray(tape.times)
        end = np.searchsorted(times, when)
        available = end - np.searchsorted(times, when - tape.owner.lookback)
        supported = available >= MIN_EXEC_CLOSED
        if not supported.any():
            return
        at, parent, available = at[supported], parent[supported], available[supported]
        full = available[None, :] >= NEEDED
        self.full[:, at] |= full
        distinct, inverse = np.unique(end[supported], return_inverse=True)
        if proof:
            supported_proof = available >= proof.count
            proof_ranks = np.zeros(len(distinct))
            for i in np.unique(inverse[supported_proof]):
                proof_ranks[i] = proof.rank_at(tape, int(distinct[i]))
            full &= supported_proof[None, :] & (proof_ranks[inverse] > 0)
            if not full.any():
                return
        gain, loss = block_totals(tape, distinct)
        short_gain, short_loss = gain[SHORT_BLOCK_INDEX], loss[SHORT_BLOCK_INDEX]
        last_rows = len(SHORT_BLOCKS) + LAST_BLOCK_INDEX
        last_gain, last_loss = gain[last_rows], loss[last_rows]
        sgain = np.where(SHORT_NEGATIVE, short_loss, short_gain)
        sloss = np.where(SHORT_NEGATIVE, short_gain, short_loss)
        lgain = np.where(LAST_NEGATIVE, last_loss, last_gain)
        lloss = np.where(LAST_NEGATIVE, last_gain, last_loss)
        gain, loss = sgain + lgain, sloss + lloss
        pf = np.full(gain.shape, 5.0)
        np.divide(gain, loss, out=pf, where=loss > 0)
        # Use the native sample-size discount with sign-aligned PF for
        # selection only. Execution PnL and reported PF use actual signs.
        score = np.round((np.minimum(5.0, pf) - 1.0) * NEEDED / (20 + NEEDED), 12)[:, inverse]
        if proof:
            # Rank the expected direction using its own after-cost history.
            # The reciprocal PF of a loss window remains qualification evidence.
            score = proof_ranks[None, inverse] + score * 1e-8
        for j, floor in enumerate(EXEC_FLOORS):
            ok = signed_pass(sgain, sloss, floor) & signed_pass(lgain, lloss, floor)
            seen = self.scores[j][:, at]
            seen_parent = self.parents[j][:, at]
            wins = full & ok[:, inverse] & ((score > seen) | ((score == seen) & (parent < seen_parent)))
            self.scores[j][:, at] = np.where(wins, score, seen)
            self.parents[j][:, at] = np.where(wins, parent, seen_parent)
