"""Forward settlement and fixed Axis/Block comparisons for the post-Base study."""
from collections import defaultdict
from functools import lru_cache
from itertools import product
import math

import stage_eval as sg
import axis_sets
import block_rules
from pulse_trader import Position
from set_block import SetBlockBook
from post_base_gates import BASE_WINDOWS, EXEC_WINDOWS, fixed_history, path_name, terminal_pass


AXES = {
    "off": None,
    "base-1.25": axis_sets.config_of({"enabled": True, "n": 25, "minN": 8, "margin": 1.05,
                                     "vol": "base", "vmin": 1, "vmax": 1.25}),
    "paired-1.5": axis_sets.config_of({"enabled": True, "n": 25, "minN": 8, "margin": 1.05,
                                       "vol": "base", "vmin": 1, "vmax": 1.5, "combos": True}),
}


def block_profiles(overlay):
    cases = {"off": None, "overlay": block_rules.clean_cfg(overlay)}
    for step, ratio, relations in product((0.5, 1.0), (0.10, 0.20), (("set",), ("symbol", "step"))):
        for stack in ("additive", "shared"):
            key = f"{stack}-{step:g}-{ratio:g}-{'-'.join(relations)}"
            cases[key] = block_rules.clean_cfg({
                "blockStackType": stack, "blockSharedTotal": 2 * ratio,
                "blockCounts": [{"step": step, "ratio": ratio, "on": True}] * 2,
                "blockRelations": relations, "blockMinObservations": 2, "blockSkipFirst": 0,
                "blockGuard": True, "blockTargetShare": 0.5, "blockKinds": ["base", "trail"],
            })
    if any(cfg.carry for cfg in cases.values() if cfg):
        raise ValueError("this comparison requires fixed parent size before Axis")
    return cases


class ParentProof:
    def __init__(self, market, columns, base_floor, exec_floor):
        self.market, self.columns = market, columns
        self.base_floor, self.exec_floor = base_floor, exec_floor
        # Instance-local caches keep neither earlier PF arms nor their tapes alive.
        self.history = lru_cache(maxsize=96)(self.history)
        self.child = lru_cache(maxsize=192)(self.child)
        self.active = lru_cache(maxsize=16384)(self.active)
        self.axis = lru_cache(maxsize=4096)(self.axis)

    def history(self, set_idx):
        book = self.market.book
        st = book.by_idx[set_idx]
        return fixed_history(book, st, self.columns(set_idx), self.base_floor)

    def child(self, set_idx, base):
        history = self.history(set_idx)
        return history, history.child(history.root, history.root.child_mask(BASE_WINDOWS[base], 0))

    def active(self, set_idx, base, exec_idx, now):
        history, tape = self.child(set_idx, base)
        return history.root.passes(BASE_WINDOWS[base], 0, now) and terminal_pass(
            tape, EXEC_WINDOWS[exec_idx], now, self.exec_floor
        )

    def axis(self, set_idx, base, now, profile):
        _, tape = self.child(set_idx, base)
        low, high = tape.indices(now)
        low = max(low, high - sg.se.PROC_KEEP)
        ids = tape.ids[low:high]
        verdict = axis_sets.choose(
            tape.times[low:high], [tape.owner.entries[i] for i in ids], tape.values[low:high],
            self.exec_floor, AXES[profile],
        )
        return verdict["vol"] if verdict["allow"] else 1.0

    def clear(self):
        for cached in (self.axis, self.active, self.child, self.history):
            cached.cache_clear()


class MemoryBlock(SetBlockBook):
    """Use the production Block rules with a fresh ledger per comparison."""
    def __init__(self, cfg):
        super().__init__("")
        self.cfg = cfg

    def save(self):
        pass


def possible_adds(market, trades, minimum_level):
    events = []
    for i, tr in enumerate(trades):
        for bar in range(tr.first + 1, tr.last):
            px = market.bars[tr.symbol][bar][3]
            if tr.side * (px / tr.entry - 1) + 1e-12 >= minimum_level * market.cost:
                events.append((bar, False, i, px))
        if not tr.held:
            events.append((tr.last, True, i, tr.exit))
    return sorted(events)


def block_outcomes(market, trades, proof, cfg, events):
    """Record independent hypothetical observations and fully simulated adds."""
    block = MemoryBlock(cfg)
    parents, honors = [], []
    minimum_level = cfg.levels()[0]
    for i, tr in enumerate(trades):
        st = market.book.by_idx[tr.set_idx]
        sl, tp = market.book.lane_fracs(st)
        pos = Position(tr.symbol, "LONG" if tr.side == 1 else "SHORT", 1 / tr.entry, tr.entry,
                       market.clock(tr.first), tr.entry * (1 - tr.side * sl), tr.entry * (1 + tr.side * tp),
                       tr.entry, set_id=st.id, lane=EXEC_WINDOWS[tr.execution].short_mode[:3],
                       exec_lane_id=path_name(st.id, tr.base, tr.execution),
                       client_id=f"replay-{i}", base_qty=1 / tr.entry, base_entry=tr.entry, pack=st.pack)
        parents.append(pos)
        honor = sg.se.lane_honors_tp(st.kind)
        honors.append(market.cfg["honor_tp"] if honor is None else honor)
    lookback = market.book.lookback * 60
    for bar, settled, i, price in events:
        tr, pos, now = trades[i], parents[i], market.clock(bar)
        if settled:
            block.close(pos, price, now, None, market.book.cost_pct)
            continue
        if tr.side * (price / tr.entry - 1) + 1e-12 < minimum_level * market.cost:
            continue
        if not proof.active(tr.set_idx, tr.base, tr.execution, now):
            block.observe(pos, price, now, None, market.book.cost_pct, lookback)
            continue
        candidate = block.candidate(pos, price, now, None, market.book.cost_pct, lookback,
                                    floor=proof.exec_floor, honor_tp=honors[i])
        if candidate:
            qty = candidate["qty"]
            pos.block_fills.append({"group": candidate["group"], "qty": qty, "price": price, "t": now})
            pos.entry = (pos.entry * pos.qty + price * qty) / (pos.qty + qty)
            pos.qty += qty

    values = []
    for tr, pos in zip(trades, parents):
        total = math.fsum((tr.side * (tr.exit - f["price"]) - market.cost * f["price"]) * f["qty"]
                          for f in pos.block_fills)
        values.append((total, len(pos.block_fills)))
    return values


def metrics(values):
    gain = math.fsum(max(v, 0.0) for v in values)
    loss = math.fsum(max(-v, 0.0) for v in values)
    return {"closed": len(values), "gain": gain, "loss": loss, "net": gain - loss,
            "pf": gain / loss if loss > 0 else None}


def summarize(market, trades, axis=None, block=None):
    if axis is None:
        axis = [1.0] * len(trades)
    if block is None:
        block = [(0.0, 0)] * len(trades)
    if not len(trades) == len(axis) == len(block):
        raise ValueError("every trade must have exactly one Axis weight and Block outcome")
    hours = (market.end - market.start) // 3600
    opens = [0] * hours
    settlements = [[] for _ in range(hours)]
    hourly_adds = [0] * hours
    rows, held = [], []
    paths, symbols = defaultdict(list), defaultdict(list)
    for tr, weight, (added, fills) in zip(trades, axis, block):
        opened, closed = market.clock(tr.first), market.clock(tr.last)
        value = weight * (tr.net + added)
        opens[(opened - market.start) // 3600] += 1
        if tr.held:
            held.append((value, fills))
        else:
            hour = (closed - market.start - 1) // 3600
            settlements[hour].append(value)
            hourly_adds[hour] += fills
            rows.append((closed, tr.symbol, tr.side, value))
        detail = (value, tr.held, fills, tr.side)
        paths[(tr.set_idx, tr.base, tr.execution)].append(detail)
        symbols[tr.symbol].append(detail)

    outcomes = [r[-1] for r in sorted(rows)]
    equity = peak = drawdown = 0.0
    for value in outcomes:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    hourly = [{**metrics(values), "opened": opened, "adds": adds}
              for values, opened, adds in zip(settlements, opens, hourly_adds)]
    good = sum(row["net"] > 1e-12 for row in hourly)
    active = sum(row["closed"] > 0 for row in hourly)
    result = {**metrics(outcomes), "opened": len(trades), "held": len(held),
              "adds": sum(hourly_adds), "heldAdds": sum(f for _, f in held),
              "heldMarkNet": math.fsum(v for v, _ in held), "wins": sum(v > 0 for v in outcomes),
              "positiveHours": good, "activeHours": active, "hourSuccessPct": 100 * good / hours,
              "settledDrawdown": drawdown, "axisResized": sum(w != 1 for w in axis),
              "maxAxisWeight": max(axis, default=1.0), "hourly": hourly}

    def group(values):
        return {**metrics([v for v, is_held, _, _ in values if not is_held]),
                "held": sum(is_held for _, is_held, _, _ in values),
                "adds": sum(f for _, is_held, f, _ in values if not is_held),
                "long": sum(side > 0 and not is_held for _, is_held, _, side in values),
                "short": sum(side < 0 and not is_held for _, is_held, _, side in values)}

    result["paths"] = [[*key, group(values)] for key, values in sorted(paths.items())]
    result["bySymbol"] = [[symbol, group(values)] for symbol, values in sorted(symbols.items())]
    assert result["opened"] == result["closed"] + result["held"] == sum(opens)
    assert result["closed"] == sum(row["closed"] for row in hourly)
    return result


def strategy_cases(market, columns, overlay, trades, base_floor, exec_floor):
    proof = ParentProof(market, columns, base_floor, exec_floor)
    blocks = block_profiles(overlay)
    levels = [cfg.levels()[0] for cfg in blocks.values() if cfg]
    events = possible_adds(market, trades, min(levels))
    axes = {"off": None}
    for profile in AXES:
        if profile != "off":
            axes[profile] = [proof.axis(tr.set_idx, tr.base, market.clock(tr.first), profile) for tr in trades]
    out = []
    for name, cfg in blocks.items():
        # Without carry the quantity ratio and add eligibility are invariant to
        # the parent's Axis multiplier. Scale its weighted settlement once.
        block = block_outcomes(market, trades, proof, cfg, events) if cfg else None
        for profile, weights in axes.items():
            out.append({"basePf": base_floor, "execPf": exec_floor, "window": None,
                        "axis": profile, "axisSettings": AXES[profile].to_json() if AXES[profile] else None,
                        "block": name, "blockSettings": cfg.settings() if cfg else None,
                        **summarize(market, trades, weights, block)})
    proof.clear()
    return out
