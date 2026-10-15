"""Exact indication-parent Block/DCA evidence in bounded numeric tiles.

An adjustment is an alternative strategy under one *exact* indication,
configuration, symbol, direction and exit Set. It cannot inherit the pack's
representative or another parent's profit factor. Effective exit parameters
that are identical after venue clamps share immutable numeric replay arrays;
their public Set identities and live evidence remain independent.

OHLC chronology is conservative: existing protection is tested first, then
trails and adds observed at the completed candle become effective next bar.
No open position or untriggered DCA parent is counted as a profitable close.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from block_engine import clamp_stack, normalize_block_counts
from dca_engine import DCA_STEPS_MAX

ADJUSTMENT_TILE = 128


def adjustment_set_id(parent_id: str, strategy: str, count: int) -> str:
    strategy = str(strategy).lower()
    if strategy not in ("block", "block-add", "dca") or int(count) < 1:
        raise ValueError("adjustment requires block/dca and a positive count")
    return f"{parent_id}:adjust:{strategy}:{int(count)}"


def parent_base_allowed(book, parent, side: str) -> bool:
    """Use the exact parent's own directional Base verdict, never overall PF."""
    if str(parent.kind) not in ("base", "trail"):
        return False
    view = (parent.by_side or {}).get(side) or {}
    return bool(view and book._base_metrics_ok(view))


class AdjustmentStates(list):
    """Results remain list-compatible; capture mode also exposes open equity."""
    def __init__(self):
        super().__init__()
        self.open = []
        self.diagnostics = {}


@dataclass(frozen=True)
class _Identity:
    set_id: str
    parent_id: str
    kind: str
    config: str
    strategy: str
    count: int
    symbol: str
    pack: str = "indications"

    @property
    def axis(self):
        return f"block-active:{self.count}" if self.strategy == "block" else f"{self.strategy}:{self.count}"

    @property
    def execution_strategy(self):
        return "block" if self.strategy == "block-add" else self.strategy


class AdjustmentTape(Sequence):
    """Small immutable identity view over shared double precision columns."""
    __slots__ = ("columns", "identity")

    def __init__(self, columns, identity):
        self.columns, self.identity = columns, identity

    def __len__(self):
        return len(self.columns[0])

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        from set_engine import hist_fill
        cols, ident = self.columns, self.identity
        ts, side, pnl, held, reason, qty, adds = (c[index] for c in cols)
        row = hist_fill(float(ts), ident.symbol, int(side), float(pnl), float(held),
                        str(reason), ind_kind=ident.kind)
        row.update(ind_config=ident.config, strategy=ident.execution_strategy,
                   set_id=ident.set_id, parent_set_id=ident.parent_id,
                   volume_ratio=float(qty), pack=ident.pack)
        if ident.strategy in ("block", "block-add"):
            row.update(block_count=ident.count, axis_key=ident.axis)
        else:
            row.update(dca_n=int(adds), dca_cap=ident.count, axis_key=f"dca:{ident.count}")
        return row

    @property
    def bytes(self):
        return sum(getattr(c, "nbytes", 0) for c in self.columns)


class _Ring:
    """One reusable bounded ring per effective numerical lane and direction."""
    def __init__(self, np, lanes, cap, capture):
        self.np, self.cap, self.capture = np, cap, capture
        self.count = np.zeros(lanes, dtype=np.int64)
        self.closed = [[] for _ in range(lanes)] if capture else None
        self.cols = [np.empty((lanes, cap), dtype=float) for _ in range(4)]
        self.bar = np.empty((lanes, cap), dtype=np.int32)
        self.reason = np.empty((lanes, cap), dtype=np.uint8)
        self.reasons = ["sl", "tp", "time", "scratch+"]
        self.reason_ids = {r: i for i, r in enumerate(self.reasons)}

    def record(self, mask, state, px, reasons, i, side, logs=None, *, lot=False):
        np = self.np
        idx = np.flatnonzero(mask)
        if not idx.size:
            return
        slots = self.count[idx] % self.cap
        basis = state.last_leg_px if lot else state.entry
        qty = state.last_leg_qty if lot else state.qty
        opened = state.last_add if lot else state.opened
        raw = (px[idx] - basis[idx]) / basis[idx] * side
        self.bar[idx, slots] = i
        self.cols[0][idx, slots] = raw
        self.cols[1][idx, slots] = i - opened[idx]
        self.cols[2][idx, slots] = qty[idx]
        self.cols[3][idx, slots] = state.adds[idx]
        for j, slot in zip(idx.tolist(), slots.tolist()):
            why = reasons[j]
            if why not in self.reason_ids:
                self.reason_ids[why] = len(self.reasons)
                self.reasons.append(why)
            self.reason[j, slot] = self.reason_ids[why]
            if self.capture:
                self.closed[j].append(dict(
                    _entry=int(opened[j]), _parent_entry=int(state.parent_opened[j]),
                    _exit=i, _qty=float(qty[j]), _parent=1.,
                    _avg=float(basis[j]), _entry_px=float(basis[j] if lot else state.initial_entry[j]),
                    _exit_px=float(px[j]), _log=(tuple(logs[j][-1:]) if lot else tuple(logs[j])),
                    _bundle_avg=float(state.entry[j]), _bundle_qty=float(state.qty[j]),
                    _bundle_log=tuple(logs[j]),
                    pnl_pct=float(raw[np.searchsorted(idx, j)]),
                    hold_s=float((i-opened[j])*60), reason=why,
                    _adds=int(state.adds[j]), side="LONG" if side > 0 else "SHORT"))
        self.count[idx] += 1

    def columns(self, lane, base_ts, side):
        np = self.np
        count = int(self.count[lane])
        serial = np.arange(max(0, count-self.cap), count) % self.cap
        t = base_ts + self.bar[lane, serial]*60
        cols = (t, np.full(len(serial), side, dtype=np.int8),
                self.cols[0][lane, serial], self.cols[1][lane, serial]*60,
                np.asarray([self.reasons[int(x)] for x in self.reason[lane, serial]], dtype=object),
                self.cols[2][lane, serial], self.cols[3][lane, serial])
        for c in cols:
            c.flags.writeable = False
        return cols


class _Positions:
    def __init__(self, np, sl, tp, arms, gives, trailing):
        self.np, self.sl_frac, self.tp_frac = np, sl, tp
        self.arm, self.give, self.trailing = arms, gives, trailing
        n = len(sl)
        self.active = np.zeros(n, dtype=bool)
        self.cooldown = np.zeros(n, dtype=np.int32)
        self.entry = np.zeros(n)
        self.initial_entry = np.zeros(n)
        self.sl = np.zeros(n)
        self.tp = np.zeros(n)
        self.peak = np.zeros(n)
        self.trail = np.zeros(n)
        self.opened = np.zeros(n, dtype=np.int32)
        self.parent_opened = np.zeros(n, dtype=np.int32)
        self.last_add = np.zeros(n, dtype=np.int32)
        self.last_leg_px = np.zeros(n)
        self.last_leg_qty = np.zeros(n)
        self.qty = np.ones(n)
        self.adds = np.zeros(n, dtype=np.int32)
        self.tactic = np.full(n, "", dtype=object)
        self.level = np.zeros(n)

    def seed(self, mask, close, i, side, tactic=("", 0.), qty=1.):
        self.entry[mask] = close
        self.initial_entry[mask] = close
        self.peak[mask] = close
        self.trail[mask] = 0.
        self.opened[mask] = i
        self.parent_opened[mask] = i
        self.last_add[mask] = i
        self.last_leg_px[mask] = 0.
        self.last_leg_qty[mask] = 0.
        self.qty[mask] = qty
        self.adds[mask] = 0
        self.tactic[mask], self.level[mask] = tactic
        self.rearm(mask, side)
        self.active[mask] = True

    def rearm(self, mask, side):
        self.sl[mask] = self.entry[mask] * (1-side*self.sl_frac[mask])
        self.tp[mask] = self.entry[mask] * (1+side*self.tp_frac[mask])

    def advance(self, high, low, close, i, side, book, time_bars, scratch_bars,
                honor_tp, *, scratch=True):
        """Protect old quantity/stops before any completed-bar sizing change."""
        np = self.np
        live = self.active.copy()
        safe_entry = np.maximum(self.entry, 1e-12)
        if side > 0:
            effective = np.where(self.trail > 0, np.maximum(self.sl, self.trail), self.sl)
            sl_hit = live & (low <= effective)
            tp_hit = live & (high >= self.tp) if honor_tp else np.zeros(len(live), dtype=bool)
        else:
            effective = np.where(self.trail > 0, np.minimum(self.sl, self.trail), self.sl)
            sl_hit = live & (high >= effective)
            tp_hit = live & (low <= self.tp) if honor_tp else np.zeros(len(live), dtype=bool)
        exited = sl_hit | tp_hit
        px = np.where(sl_hit, effective, self.tp)
        reasons = np.where(sl_hit, "sl", "tp").astype(object)
        held = i-self.opened
        settings = book.ind_settings or {}
        if settings.get("exitTacticOn", True):
            from indication_engine import tactic_should_close
            for j in np.flatnonzero(live & ~exited & (held >= 1) & (self.tactic != "")):
                if self.tactic[j] and tactic_should_close(self.tactic[j], side, close, self.level[j],
                        self.entry[j], float(settings.get("exitTacticBufferPct", .1)),
                        float(settings.get("exitTacticMinGainPct", .15))):
                    exited[j], px[j], reasons[j] = True, close, "tactic-"+self.tactic[j]
        timed = live & ~exited & (held >= time_bars)
        exited[timed], px[timed], reasons[timed] = True, close, "time"
        if scratch:
            scratched = live & ~exited & (held >= scratch_bars) & (((close-self.entry)/safe_entry*side) >= book.scratch_min)
            exited[scratched], px[scratched], reasons[scratched] = True, close, "scratch+"
        remain = live & ~exited
        if side > 0:
            self.peak[remain] = np.maximum(self.peak[remain], high)
            arm = remain & self.trailing & ((self.peak-self.entry)/safe_entry >= self.arm)
            self.trail[arm] = np.maximum(self.trail[arm], self.peak[arm]*(1-self.give[arm]))
        else:
            self.peak[remain] = np.minimum(self.peak[remain], low)
            arm = remain & self.trailing & ((self.entry-self.peak)/safe_entry >= self.arm)
            next_trail = self.peak*(1+self.give)
            fresh = arm & (self.trail <= 0)
            self.trail[fresh] = next_trail[fresh]
            self.trail[arm & ~fresh] = np.minimum(self.trail[arm & ~fresh], next_trail[arm & ~fresh])
        self.active[exited] = False
        self.cooldown[exited] = max(0, int(book.cooldown_bars))
        return exited, px, reasons


def _metadata(row, ident, base_ts):
    out = dict(row)
    out.update(t=base_ts+out["_exit"]*60, symbol=ident.symbol,
        set_id=ident.set_id, parent_set_id=ident.parent_id, ind_kind=ident.kind,
        ind_config=ident.config, strategy=ident.execution_strategy, pack=ident.pack,
        volume_ratio=out["_qty"], axis_key=ident.axis)
    if ident.strategy in ("block", "block-add"):
        out["block_count"] = ident.count
    else:
        out.update(dca_n=out["_adds"], dca_cap=ident.count)
    return out


def calculate_adjustments(book, symbol, bars, parent_states, lane, warmup, now,
                          *, lane_exits=None, capture=False, assume_qualified=False):
    """Return own Block-count and DCA-step States for exact parent parameters.

    By default only directions whose exact parent passes Base are calculated.
    Offline causal simulators may set ``assume_qualified=True`` to produce
    hypothetical tapes, then apply each parent's past-only gate at entry.
    ``capture=True`` keeps all close paths and current open positions for
    balance/equity accounting; runtime retains at most 96 closes per side.
    The caller publishes/scopes children in the parent's generation token.
    """
    from set_engine import BAR_S, REPLAY_HIST_CAP, _np
    if _np is None:
        raise RuntimeError("independent adjustments require NumPy replay")
    np = _np
    result = AdjustmentStates()
    parents = [p for p in parent_states if p.kind in ("base", "trail")]
    identities = {(p.indication_kind, p.indication_config) for p in parents}
    if len(identities) > 1:
        raise ValueError("one signal lane can only replay one indication kind/configuration")
    if any(p.indication_symbol and p.indication_symbol != str(symbol) for p in parents):
        raise ValueError("an adjustment parent must belong to the replayed symbol")
    if len(parents) > ADJUSTMENT_TILE:
        effective = 0
        for start in range(0, len(parents), ADJUSTMENT_TILE):
            tile = calculate_adjustments(book, symbol, bars, parents[start:start+ADJUSTMENT_TILE],
                lane, warmup, now, lane_exits=lane_exits, capture=capture,
                assume_qualified=assume_qualified)
            result.extend(tile)
            result.open.extend(tile.open)
            effective += int(tile.diagnostics.get("effectiveParameters", 0))
        result.diagnostics = dict(parents=len(parents), effectiveParameters=effective,
            children=len(result), capture=capture, tile=ADJUSTMENT_TILE,
            ringRowsPerDirection=REPLAY_HIST_CAP, independent=True, causal=True)
        return result
    block_counts = [c for c in normalize_block_counts(getattr(book, "block_counts", None))
                    if c <= clamp_stack(getattr(book, "block_stack", 6))]
    do_block = bool(getattr(book, "hist_block", True)) and bool(block_counts)
    block_active = bool(getattr(book, "block_active", True))
    do_standalone = do_block and block_active
    do_dca = bool(getattr(book, "hist_dca", True)) and bool(getattr(book, "dca_enabled", True))
    distances = list(getattr(book, "dca_dist", (.012, .016, .020, .024)))[:DCA_STEPS_MAX]
    mults = list(getattr(book, "dca_mult", (1.5, 2., 2.3, 2.5)))
    step_n = min(DCA_STEPS_MAX, int(getattr(book, "dca_max_steps", len(distances)) or len(distances)))
    if not distances:
        do_dca = False
    while distances and len(distances) < step_n:
        distances.append(min(.08, distances[-1]+.005))
    while len(mults) < step_n:
        mults.append(mults[-1] if mults else 1.5)
    distances = np.asarray(distances[:step_n], dtype=float)
    mults = np.asarray(mults[:step_n], dtype=float).clip(.25, 2.5)
    if not parents or not (do_block or do_dca) or len(bars) <= warmup:
        return result
    confidence_floor = .58 if parents[0].indication_kind in ("general", "combined") or parents[0].pack == "general" else .52
    with book.sl_scope(symbol):
        keys = []
        owners = []
        index = {}
        for parent in parents:
            allowed = tuple(assume_qualified or parent_base_allowed(book, parent, side)
                            for side in ("LONG", "SHORT"))
            if not any(allowed):
                continue
            sl, tp = book.pair_sl_tp(parent.tp_pct, float(parent.sl_ratio or .6))
            arm = parent.trail_arm/100 if parent.trail_arm > .05 else parent.trail_arm
            give = parent.trail_give/100 if parent.trail_give > .05 else parent.trail_give
            key = (sl, tp, arm, give, parent.kind == "trail", allowed)
            if key not in index:
                index[key] = len(keys)
                keys.append(key)
            owners.append((parent, index[key]))
    if not keys:
        return result
    m = len(keys)
    # Cached aliases are bounded by this tile, never by the full live catalogue.
    sl, tp, arms, gives = (np.asarray([k[i] for k in keys]) for i in range(4))
    trailing = np.asarray([k[4] for k in keys], dtype=bool)
    allowed = np.asarray([k[5] for k in keys], dtype=bool)
    final_ts = float((getattr(book, "_bar_end_ts", {}) or {}).get(symbol) or now)
    base_ts = final_ts-(len(bars)-1)*BAR_S
    time_bars = max(8, min(book.hist_time_bars, max(8, len(bars)-warmup-1)))
    scratch_bars = max(8, int(book.scratch_s/BAR_S))
    honor_tp = bool(getattr(book, "hist_honor_tp", True))
    increment = min(max(0., float(book.block_max_multiplier)-1),
                    max(.05, float(getattr(book, "block_vr_specified", book.block_vr))))
    add_increment = increment if block_active else float(book.block_vr)
    dca_cd = max(0, float(getattr(book, "dca_cooldown_s", 30.)))
    outputs = {}
    opens = {}
    for side_idx, side in enumerate((1, -1)):
        parent = _Positions(np, sl, tp, arms, gives, trailing)
        block = _Positions(np, sl, tp, arms, gives, trailing)
        block.used_parent = np.full(m, -1, dtype=np.int32)
        block_ring = _Ring(np, m, REPLAY_HIST_CAP, capture) if do_standalone else None
        dca = _Positions(np, np.repeat(sl, step_n), np.repeat(tp, step_n),
                         np.repeat(arms, step_n), np.repeat(gives, step_n), np.repeat(trailing, step_n)) if do_dca else None
        caps = np.tile(np.arange(1, step_n+1), m) if do_dca else None
        dca_ring = _Ring(np, m*step_n, REPLAY_HIST_CAP, capture) if do_dca else None
        block_logs = [[] for _ in range(m)] if capture else None
        dca_logs = [[] for _ in range(m*step_n)] if capture and do_dca else None
        block_width = len(block_counts)
        block_add = _Positions(np, np.repeat(sl, block_width), np.repeat(tp, block_width),
            np.repeat(arms, block_width), np.repeat(gives, block_width), np.repeat(trailing, block_width)) if do_block else None
        block_caps = np.tile(block_counts, m) if do_block else None
        block_add_ring = _Ring(np, m*block_width, REPLAY_HIST_CAP, capture) if do_block else None
        block_add_logs = [[] for _ in range(m*block_width)] if capture and do_block else None
        for i in range(int(warmup), len(bars)):
            high, low, close = (float(bars[i][j]) for j in (1, 2, 3))
            if min(high, low, close) <= 0:
                continue
            parent.cooldown[(~parent.active) & (parent.cooldown > 0)] -= 1
            parent.advance(high, low, close, i, side, book, time_bars, scratch_bars, honor_tp)
            if do_standalone:
                dead, px, reasons = block.advance(high, low, close, i, side, book,
                    time_bars, scratch_bars, honor_tp)
                block_ring.record(dead, block, px, reasons, i, side, block_logs)
                # An extra opens only while its exact parent remains open.
                # Each count is an alternative admission rung of the active
                # delta, not six copies of the same executable order.
                ready = parent.active & ~block.active & ((i-parent.opened) >= 1) & \
                        (block.used_parent != parent.opened) & \
                        (((close-parent.entry)/np.maximum(parent.entry, 1e-12)*side) >= .002)
                signal_direction, signal_confidence = lane[i][:2] if i < len(lane) else (0, 0.)
                ready &= signal_direction == side and signal_confidence >= confidence_floor
                if ready.any() and increment > 0:
                    block.seed(ready, close, i, side, (lane_exits or {}).get(i, ("", 0.)), qty=increment)
                    block.parent_opened[ready] = parent.opened[ready]
                    block.used_parent[ready] = parent.opened[ready]
                    block.adds[ready] = 1
                    if capture:
                        for j in np.flatnonzero(ready):
                            block_logs[j] = [(i, close, increment)]
            if do_dca:
                dca.cooldown[(~dca.active) & (dca.cooldown > 0)] -= 1
                dead, px, reasons = dca.advance(high, low, close, i, side, book,
                    time_bars, scratch_bars, honor_tp)
                dca_ring.record(dead & (dca.adds == caps), dca, px, reasons, i, side, dca_logs, lot=True)
                adv = (dca.entry-close)/np.maximum(dca.entry, 1e-12)*side
                nxt = np.minimum(dca.adds, max(0, step_n-1))
                # Match the live adverse guard and one confirmed step at a
                # time. A close-based add never rescues an earlier candle SL.
                add = dca.active & (dca.adds < caps) & ((i-dca.opened) >= 1) & \
                      (((i-dca.last_add)*BAR_S) >= dca_cd) & (adv >= distances[nxt]) & \
                      (adv <= np.maximum(dca.sl_frac*1.5, .012)+1e-12)
                if add.any():
                    qty = mults[nxt[add]]
                    total = dca.qty[add]+qty
                    dca.entry[add] = (dca.entry[add]*dca.qty[add]+close*qty)/total
                    dca.qty[add] = total
                    dca.adds[add] += 1
                    dca.last_add[add] = i
                    dca.last_leg_px[add] = close
                    dca.last_leg_qty[add] = qty
                    dca.rearm(add, side)
                    if capture:
                        for j, q in zip(np.flatnonzero(add), qty):
                            dca_logs[j].append((i, close, float(q)))
            if do_block:
                block_add.cooldown[(~block_add.active) & (block_add.cooldown > 0)] -= 1
                dead, px, reasons = block_add.advance(high, low, close, i, side, book,
                    time_bars, scratch_bars, honor_tp)
                block_add_ring.record(dead & (block_add.adds == block_caps), block_add,
                    px, reasons, i, side, block_add_logs, lot=True)
                favorable = (close-block_add.entry)/np.maximum(block_add.entry, 1e-12)*side
                next_count = np.zeros(len(block_caps), dtype=np.int32)
                for rung in reversed(block_counts):
                    next_count = np.where(block_add.adds < rung, rung, next_count)
                target = np.minimum(max(0., float(book.block_max_multiplier)-1), add_increment*next_count)
                quantity = np.maximum(0., target-(block_add.qty-1.))
                threshold = .002*next_count if block_active else .002
                add = block_add.active & (block_add.adds < block_caps) & ((i-block_add.opened) >= 1) & \
                    (favorable >= threshold) & (quantity > 1e-12) & (next_count <= block_caps)
                if add.any():
                    q = quantity[add]
                    total = block_add.qty[add]+q
                    block_add.entry[add] = (block_add.entry[add]*block_add.qty[add]+close*q)/total
                    block_add.qty[add] = total
                    block_add.adds[add] = next_count[add]
                    block_add.last_add[add] = i
                    block_add.last_leg_px[add] = close
                    block_add.last_leg_qty[add] = q
                    block_add.rearm(add, side)
                    if capture:
                        for j, qty in zip(np.flatnonzero(add), q):
                            block_add_logs[j].append((i, close, float(qty)))
            if i >= len(lane):
                continue
            d, conf = lane[i][:2]
            if d != side or conf < confidence_floor:
                continue
            seed = (~parent.active) & (parent.cooldown <= 0) & allowed[:, side_idx]
            if not seed.any():
                continue
            tactic = (lane_exits or {}).get(i, ("", 0.))
            parent.seed(seed, close, i, side, tactic)
            if do_dca:
                dca_seed = np.repeat(seed, step_n) & ~dca.active & (dca.cooldown <= 0)
                dca.seed(dca_seed, close, i, side, tactic)
                if capture:
                    for j in np.flatnonzero(dca_seed):
                        dca_logs[j] = [(i, close, 1.)]
            if do_block:
                block_seed = np.repeat(seed, block_width) & ~block_add.active & (block_add.cooldown <= 0)
                block_add.seed(block_seed, close, i, side, tactic)
                if capture:
                    for j in np.flatnonzero(block_seed):
                        block_add_logs[j] = [(i, close, 1.)]
        for strategy, obj, ring, logs in (("block", block, block_ring, block_logs),
                                           ("dca", dca, dca_ring, dca_logs),
                                           ("block-add", block_add, block_add_ring, block_add_logs)):
            if ring is None:
                continue
            width = step_n if strategy == "dca" else block_width if strategy == "block-add" else 1
            for j in range(m*width):
                slot = (strategy, j, side)
                outputs[slot] = (ring.columns(j, base_ts, side), int(ring.count[j]),
                                 ring.closed[j] if capture else None)
                reached = strategy == "block" or obj.adds[j] == (caps[j] if strategy == "dca" else block_caps[j])
                if capture and obj.active[j] and obj.adds[j] > 0 and reached:
                    lot = strategy != "block"
                    basis = float(obj.last_leg_px[j] if lot else obj.entry[j])
                    qty = float(obj.last_leg_qty[j] if lot else obj.qty[j])
                    opened = int(obj.last_add[j] if lot else obj.opened[j])
                    opens[slot] = dict(_entry=opened,
                        _parent_entry=int(obj.parent_opened[j]), _exit=len(bars)-1,
                        _qty=qty, _parent=1., _avg=basis,
                        _entry_px=basis if lot else float(obj.initial_entry[j]), _exit_px=float(bars[-1][3]),
                        _log=tuple(logs[j][-1:]) if lot else tuple(logs[j]),
                        _bundle_avg=float(obj.entry[j]), _bundle_qty=float(obj.qty[j]),
                        _bundle_log=tuple(logs[j]), _adds=int(obj.adds[j]),
                        pnl_pct=(float(bars[-1][3])-basis)/basis*side,
                        hold_s=(len(bars)-1-opened)*BAR_S, reason="open",
                        side="LONG" if side > 0 else "SHORT")
    shared = {}
    shared_scores = {}
    for parent, parent_j in owners:
        variants = ([("block", c, parent_j) for c in block_counts] if do_standalone else [])
        if do_block:
            variants += [("block-add", c, parent_j*block_width+k) for k,c in enumerate(block_counts)]
        if do_dca:
            variants += [("dca", c, parent_j*step_n+c-1) for c in range(1, step_n+1)]
        for strategy, count, j in variants:
            st = parent.catalog_copy()
            st.id = adjustment_set_id(parent.id, strategy, count)
            st.parent_set_id = parent.id
            st.kind, st.relative_count = "block" if strategy == "block-add" else strategy, count
            st.template_set_id = parent.template_set_id or parent.id
            st.axis_key = f"block-active:{count}" if strategy == "block" else f"{strategy}:{count}"
            extra = max(0., float(book.block_max_multiplier)-1)
            previous_count = max((n for n in block_counts if n<count), default=0)
            st.volume_ratio = increment if strategy == "block" else min(count*add_increment,extra)-min(previous_count*add_increment,extra) if strategy == "block-add" else float(mults[count-1])
            ident = _Identity(st.id, parent.id, parent.indication_kind, parent.indication_config,
                              strategy, count, symbol, parent.pack)
            tapes, total, captured = [], 0, []
            for side in (1, -1):
                cols, n, allrows = outputs[(strategy, j, side)]
                total += n
                if capture:
                    captured.extend(_metadata(row, ident, base_ts) for row in allrows)
                    if (strategy, j, side) in opens:
                        result.open.append(_metadata(opens[(strategy, j, side)], ident, base_ts))
                else:
                    tapes.append(cols)
            if capture:
                st.hist = sorted(captured, key=lambda row: row["t"])
            else:
                cache_key = (strategy, j)
                cols = shared.get(cache_key)
                if cols is None:
                    cols = tuple(np.concatenate([x[k] for x in tapes]) for k in range(7))
                    order = np.argsort(cols[0], kind="stable")
                    cols = tuple(c[order] for c in cols)
                    for c in cols:
                        c.flags.writeable = False
                    shared[cache_key] = cols
                st.hist = AdjustmentTape(cols, ident)
            st.n = total
            score_key = (strategy, j)
            bundle = shared_scores.get(score_key)
            if bundle is None:
                bundle = book._fast_historic_bundle(st.hist, ordered=True)
                shared_scores[score_key] = bundle
            book._score_pair((st, bundle))
            st.n = total
            st.strategy_adjustments["adjustment"] = dict(strategy=strategy, count=count,
                parentSetId=parent.id, ownEvidence=True, aliasEffectiveExit=True,
                calculatedDirections=[s for s in ("LONG", "SHORT") if assume_qualified or parent_base_allowed(book, parent, s)])
            result.append(st)
    result.diagnostics = dict(parents=len(owners), effectiveParameters=m, children=len(result),
        blockCounts=block_counts if do_block else [], dcaCaps=list(range(1, step_n+1)) if do_dca else [],
        blockActive=block_active,
        capture=capture, ringRowsPerDirection=REPLAY_HIST_CAP, independent=True,
        strategySemantics="block-active-delta/own-add-lot-under-capped-average-controls", causal=True)
    return result


def capture_parent_events(book, symbol, bars, parent_states, lane, warmup, now,
                          *, lane_exits=None):
    """Full causal parent close paths for offline chronological admission.

    A single SetState returns its chronological list; a sequence returns a
    dictionary by exact Set ID. This bypasses the runtime 96-row retention
    cap without changing runtime storage or borrowing another parent's tape.
    """
    from set_engine import BAR_S, REPLAY_HIST_CAP, _np
    if _np is None:
        raise RuntimeError("parent event capture requires NumPy replay")
    np = _np
    single = hasattr(parent_states, "id")
    parents = [parent_states] if single else list(parent_states)
    if not parents:
        return {}
    if len({(p.indication_kind, p.indication_config) for p in parents}) > 1:
        raise ValueError("one signal lane can only capture one indication kind/configuration")
    confidence_floor = .58 if parents[0].indication_kind in ("general", "combined") or parents[0].pack == "general" else .52
    with book.sl_scope(symbol):
        pairs = [book.pair_sl_tp(p.tp_pct, float(p.sl_ratio or .6)) for p in parents]
    sl, tp = (np.asarray([pair[i] for pair in pairs]) for i in (0, 1))
    arms = np.asarray([p.trail_arm/100 if p.trail_arm > .05 else p.trail_arm for p in parents])
    gives = np.asarray([p.trail_give/100 if p.trail_give > .05 else p.trail_give for p in parents])
    trailing = np.asarray([p.kind == "trail" for p in parents], dtype=bool)
    base_ts = float((getattr(book, "_bar_end_ts", {}) or {}).get(symbol) or now)-(len(bars)-1)*BAR_S
    time_bars = max(8, min(book.hist_time_bars, max(8, len(bars)-warmup-1)))
    scratch_bars = max(8, int(book.scratch_s/BAR_S))
    result = {p.id: [] for p in parents}
    for side in (1, -1):
        state = _Positions(np, sl, tp, arms, gives, trailing)
        ring = _Ring(np, len(parents), REPLAY_HIST_CAP, True)
        logs = [[] for p in parents]
        for i in range(int(warmup), len(bars)):
            high, low, close = (float(bars[i][j]) for j in (1, 2, 3))
            if min(high, low, close) <= 0:
                continue
            state.cooldown[(~state.active) & (state.cooldown > 0)] -= 1
            dead, px, reasons = state.advance(high, low, close, i, side, book,
                time_bars, scratch_bars, bool(getattr(book, "hist_honor_tp", True)))
            ring.record(dead, state, px, reasons, i, side, logs)
            d, conf = lane[i][:2] if i < len(lane) else (0, 0.)
            if d == side and conf >= confidence_floor:
                seed = ~state.active & (state.cooldown <= 0)
                state.seed(seed, close, i, side, (lane_exits or {}).get(i, ("", 0.)))
                for j in np.flatnonzero(seed):
                    logs[j] = [(i, close, 1.)]
        for j, parent in enumerate(parents):
            for row in ring.closed[j]:
                row = dict(row)
                row.update(t=base_ts+row["_exit"]*BAR_S, symbol=symbol,
                    set_id=parent.id, parent_set_id=parent.id,
                    ind_kind=parent.indication_kind, ind_config=parent.indication_config,
                    strategy="trailing" if parent.kind == "trail" else "core",
                    pack=parent.pack, volume_ratio=1.)
                result[parent.id].append(row)
    for rows in result.values():
        rows.sort(key=lambda row: row["t"])
    return result[parents[0].id] if single else result
