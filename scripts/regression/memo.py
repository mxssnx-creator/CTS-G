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

from set_engine import SetBook

_ORIG = SetBook._replay_symbol
_CACHE: dict = OrderedDict()
MAX_CACHED_SYMBOLS = 2  # bound regression memory even when many distinct full-grid fixtures run
_ACTIVE = False
MODE = os.environ.get("CTSG_MEMO", "on").lower()
STATS = {"hit": 0, "miss": 0, "verified": 0}


def _key(b: SetBook, symbol: str, now: float):
    bars = b.bars[symbol]
    digest = hashlib.blake2b(repr(bars).encode(), digest_size=16).hexdigest()
    settings = json.dumps(b.ind_settings, sort_keys=True, default=str)
    sets = tuple((st.id, st.pack, st.kind, st.trail_arm, st.trail_give, st.tp_pct, st.sl_ratio,
                  st.hold_bars, b.auto_holds(st)) for st in b.by_idx)
    return (symbol, digest, float(now), b.bar_t.get(symbol), b.warmup, tuple(b.packs), settings, b.entry_conf, b.hist_time_bars,
            b.scratch_s, b.scratch_min, b.cooldown_bars, b.cost_pct, b.sl_min_steps,
            bool(getattr(b, "hist_honor_tp", True)), sets)


def _rows_of(hist: dict) -> dict:
    return {sid: [dict(r) for r in rows] for sid, rows in hist.items() if rows}


def _replay(self: SetBook, symbol, hist, now, on_step=None):
    if not _ACTIVE or MODE == "off":
        return _ORIG(self, symbol, hist, now, on_step=on_step)
    key = _key(self, symbol, now)
    cached = _CACHE.get(key)
    if cached is None:
        fresh = {sid: [] for sid in hist}
        inputs = _ORIG(self, symbol, fresh, now, on_step=on_step)
        _CACHE[key] = (_rows_of(fresh), inputs)
        while len(_CACHE) > MAX_CACHED_SYMBOLS:
            _CACHE.popitem(last=False)
        STATS["miss"] += 1
        for sid, rows in fresh.items():
            hist.setdefault(sid, []).extend(dict(r) for r in rows)
        return inputs
    cached_rows, cached_inputs = cached
    if MODE == "verify":
        check = {sid: [] for sid in hist}
        check_inputs = _ORIG(self, symbol, check, now, on_step=on_step)
        if _rows_of(check) != cached_rows or any(check_inputs[k] != cached_inputs[k] for k in ("bars", "base_ts", "warmup", "cfg", "dirs", "confs", "cand")):
            raise AssertionError(f"memo mismatch for {symbol} at {now}: cached rows differ from a fresh replay")
        STATS["verified"] += 1
    STATS["hit"] += 1
    _CACHE.move_to_end(key)
    for sid, rows in cached_rows.items():
        hist.setdefault(sid, []).extend(dict(r) for r in rows)
    if self.by_idx:
        last = [st for st in self.by_idx if st.pack in self.packs]
        if last:
            self.progress.set_id = last[-1].id
    # Replay inputs are read-only in the engine. Use this book's equal bars,
    # while retaining the candidate arrays and index paired with cached rows.
    return {**cached_inputs, "bars": self.bars[symbol]}


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
