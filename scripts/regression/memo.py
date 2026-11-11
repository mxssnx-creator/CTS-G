"""Replay memo for the regression harness. The engine never imports this module.

SetBook._replay_symbol is a pure function of its inputs: the symbol's bars, `now`, the replay settings, the packs,
the indication settings, and each Set's lane parameters (id, pack, kind, trail arm and give, TP, SL ratio). The
processing checks replay the same symbols with the same inputs many times, so the first call stores the Set rows
and an identical later call copies them instead of recomputing.

Only checks wrapped with `memoized` use the memo. Checks that must observe a real replay (determinism, concurrency,
the golden fingerprint, the engine self-tests) run unwrapped.

CTSG_MEMO=verify  every hit is also replayed and compared row by row. Any difference fails the check.
CTSG_MEMO=off     the memo is never used, even inside a memoized check.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections import OrderedDict

from set_engine import SetBook, RowCols, _ROW_KEYS, _ported_pack_sources
import position_cost

_ORIG = SetBook._replay_symbol
_CACHE: dict = OrderedDict()
MAX_CACHED_SYMBOLS = 4  # the repeated full-grid fixture spans four symbols
MAX_CACHED_ROWS = 2_000_000  # compact columns bound total retained observations too
_ACTIVE = False
MODE = os.environ.get("CTSG_MEMO", "on").lower()
STATS = {"hit": 0, "miss": 0, "verified": 0, "bypass": 0}


def _key(b: SetBook, symbol: str, now: float):
    market, dated = b._replay_market()
    bars = market[symbol]
    digest = hashlib.blake2b(repr(bars).encode(), digest_size=16).hexdigest()
    settings = json.dumps(b.ind_settings, sort_keys=True, default=str)
    sets = tuple((st.id, st.pack, st.kind, st.trail_arm, st.trail_give, st.tp_pct, st.sl_ratio,
                  st.hold_bars, b.auto_holds(st)) for st in b.by_idx)
    return (symbol, digest, float(now), dated.get(symbol), b.warmup, tuple(b.packs), settings, b.entry_conf, b.hist_time_bars,
            b.scratch_s, b.scratch_min, b.cooldown_bars, b.cost_pct, b.min_sl_distance_pct,
            tuple(sorted(position_cost.configure_min_steps().items())),
            bool(getattr(b, "hist_honor_tp", True)), sets)


def _rows_of(hist: dict) -> dict:
    return {sid: [dict(r) for r in rows] for sid, rows in hist.items() if rows}


def _plain_rows(columns):
    return {sid: [dict(zip(_ROW_KEYS, row)) for row in rows] for sid, rows in columns.items()}


def _replay(self: SetBook, symbol, hist, now, on_step=None):
    if not _ACTIVE or MODE == "off":
        return _ORIG(self, symbol, hist, now, on_step=on_step)
    # Imported signals depend on peers and adaptive pause publishes per-book
    # choices. Neither is a pure symbol fixture: use the native replay.
    if _ported_pack_sources(self.packs) or any(self.pause_active(p) for p in self.packs):
        STATS["bypass"] += 1
        return _ORIG(self, symbol, hist, now, on_step=on_step)
    key = _key(self, symbol, now)
    cached = _CACHE.get(key)
    if cached is None:
        fresh = {sid: [] for sid in hist}
        inputs = _ORIG(self, symbol, fresh, now, on_step=on_step)
        count = sum(len(rows) for rows in fresh.values())
        if count <= MAX_CACHED_ROWS:
            columns = {sid: RowCols(rows) for sid, rows in fresh.items() if rows}
            _CACHE[key] = (columns, inputs, count)
            while len(_CACHE) > MAX_CACHED_SYMBOLS or sum(item[2] for item in _CACHE.values()) > MAX_CACHED_ROWS:
                _CACHE.popitem(last=False)
        STATS["miss"] += 1
        for sid, rows in fresh.items():
            hist.setdefault(sid, []).extend(dict(r) for r in rows)
        return inputs
    cached_rows, cached_inputs, _count = cached
    if MODE == "verify":
        check = {sid: [] for sid in hist}
        check_inputs = _ORIG(self, symbol, check, now, on_step=on_step)
        if _rows_of(check) != _plain_rows(cached_rows) or any(check_inputs[k] != cached_inputs[k] for k in ("bars", "base_ts", "warmup", "cfg", "dirs", "confs", "cand")):
            raise AssertionError(f"memo mismatch for {symbol} at {now}: cached rows differ from a fresh replay")
        STATS["verified"] += 1
    STATS["hit"] += 1
    _CACHE.move_to_end(key)
    for sid, rows in cached_rows.items():
        hist.setdefault(sid, []).extend(dict(zip(_ROW_KEYS, r)) for r in rows)
    if self.by_idx:
        last = [st for st in self.by_idx if st.pack in self.packs]
        if last:
            self.progress.set_id = last[-1].id
    # Replay inputs are read-only in the engine. Use this book's equal bars,
    # while retaining the candidate arrays and index paired with cached rows.
    return {**cached_inputs, "bars": self._replay_market()[0][symbol]}


SetBook._replay_symbol = _replay


@contextlib.contextmanager
def using(active: bool = True):
    global _ACTIVE
    prev = _ACTIVE
    _ACTIVE = active
    try:
        yield
    finally:
        _ACTIVE = prev


def memoized(fn):
    """Run a check with the replay memo on. The check's own logic is unchanged."""
    def run():
        with using(True):
            return fn()
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    run.__memoized__ = True          # run.py groups memoized checks into one ordered worker
    return run


def clear() -> None:
    _CACHE.clear()
    for k in STATS:
        STATS[k] = 0
