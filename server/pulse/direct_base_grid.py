"""Native Base orders through signed end gates. All indices belong to one exact Set."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from stage_grid import BASE_LONG, BYTE_BITS, LAST, SHORT, SIGN_MODES, StageHistory, Window

BASE_FLOORS = (1.5, 2.0, 2.5)
END_FLOORS = (2.0, 2.5)


@dataclass(frozen=True)
class Rule:
    name: str
    blocks: tuple[tuple[int, int, str], ...] = ()
    native: Window | None = None

    def mask(self, tape) -> int:
        if self.native:
            return tape.child_mask(self.native, 0)
        result = (1 << len(tape.ids)) - 1
        for count, offset, mode in self.blocks:
            result &= tape.mask(count, offset, mode)
            if not result:
                break
        return result


def signed(prefix, count, mode):
    return f"{prefix}{count}{'+' if mode == 'positive' else '-'}"


def base_rules(grid):
    """Native disjoint Base paths plus standalone Long, Short and Last controls."""
    rules = [
        Rule(f"L{w.long}+ {signed('S', w.short, w.short_mode)} {signed('T', w.last, w.last_mode)}", native=w)
        for w in grid.windows(0, 0)
    ]
    rules += [Rule(f"L{n}+", ((n, 0, "risk"),)) for n in BASE_LONG]
    for label, counts in (("S", SHORT), ("T", LAST)):
        rules += [Rule(signed(label, n, mode), ((n, 0, mode),)) for n, mode in product(counts, SIGN_MODES)]
    return rules


def end_rules():
    shorts = [(s, m) for s in (8, 12, 16, 20) for m in (SIGN_MODES if s in (8, 12) else ("positive",))]
    lasts = list(product(LAST, SIGN_MODES))
    rules = [Rule(signed("S", s, m), ((s, 0, m),)) for s, m in shorts]
    rules += [Rule(signed("T", n, m), ((n, 0, m),)) for n, m in lasts]
    for (s, sm), (n, nm) in product(shorts, lasts):
        rules.append(Rule(f"{signed('S', s, sm)} {signed('T', n, nm)}", ((n, 0, nm), (s, n, sm))))
    return rules


def members(mask, parent_ids):
    return tuple(parent_ids[8 * byte + bit] for byte, value in enumerate(
        mask.to_bytes((mask.bit_length() + 7) // 8, 'little')) for bit in BYTE_BITS[value])


def fixed_history(native, floor, ids=None):
    """Keep native risk and lookback for Base; the end reads strictly its own parent."""
    if ids is None:
        return StageHistory(native.grid, native.closes, native.entries, native.nets,
                            native.lookback, lambda _: floor, base_risk=native.base_risk)
    return StageHistory(native.grid, [native.closes[i] for i in ids], [native.entries[i] for i in ids],
                        [native.nets[i] for i in ids], native.lookback, lambda _: floor)


def memberships(native):
    """Intern equivalent immutable memberships without pooling mutable lane states or metrics."""
    base, end = base_rules(native.grid), end_rules()
    streams, stream_ids = [()], {(): 0}

    def intern(ids):
        index = stream_ids.get(ids)
        if index is None:
            index = len(streams)
            streams.append(ids)
            stream_ids[ids] = index
        return index

    parents, lookup = {}, []
    for base_floor in BASE_FLOORS:
        history = fixed_history(native, base_floor)
        row = []
        for rule in base:
            root = history.root
            parent = members(rule.mask(root), root.ids)
            parent_id = intern(parent)
            row.append(parent_id)
            if parent_id in parents:
                continue
            ends = []
            for end_floor in END_FLOORS:
                child = fixed_history(native, end_floor, parent)
                masks, result = {}, []
                for gate in end:
                    mask = gate.mask(child.root)
                    if mask not in masks:
                        masks[mask] = intern(members(mask, parent))
                    result.append(masks[mask])
                ends.append(result)
            parents[parent_id] = ends
        lookup.append(row)
    return streams, lookup, parents
