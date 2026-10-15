"""Bounded, asynchronous evidence for each indication configuration's exits.

The combined indication pack is a different signal and is never evidence for
an independent indication. A requested lane walks the entire configured
Normal/Trailing grid in bounded tiles. Completed tiles may admit their own
qualified Sets while the remaining grid continues; missing tiles stay cold.
"""
from __future__ import annotations

import copy
from array import array
from collections.abc import Sequence
import hashlib
import json
import os
import threading
import time


def lane_set_id(kind, config, template_id, symbol=""):
    token = hashlib.sha256((str(kind) + "\0" + str(config) + "\0" + str(symbol)).encode()).hexdigest()[:16]
    return "indcfg:" + token + ":" + str(template_id)


class ColumnarTape(Sequence):
    """Exact double-valued, immutable evidence without a dict per retained fill."""
    __slots__ = ("t", "pnl", "hold", "symbols", "symbol", "side", "reasons", "reason", "kind", "config")

    def __init__(self, rows, kind, config):
        self.kind, self.config = kind, config
        self.t, self.pnl, self.hold = array("d"), array("d"), array("d")
        self.symbol, self.side, self.reason = array("H"), array("B"), array("H")
        symbols, reasons = {}, {}
        for row in rows:
            sym, why = str(row.get("symbol") or ""), str(row.get("reason") or "")
            symbols.setdefault(sym, len(symbols)); reasons.setdefault(why, len(reasons))
            self.t.append(row["t"]); self.pnl.append(row["pnl_pct"])
            self.hold.append(row.get("hold_s") or 0)
            self.symbol.append(symbols[sym]); self.reason.append(reasons[why])
            self.side.append(1 if str(row.get("side") or "").upper().startswith("L") else 0)
        self.symbols, self.reasons = tuple(symbols), tuple(reasons)

    def __len__(self):
        return len(self.t)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        from set_engine import hist_fill
        row = hist_fill(self.t[index], self.symbols[self.symbol[index]],
                        1 if self.side[index] else -1, self.pnl[index], self.hold[index],
                        self.reasons[self.reason[index]], ind_kind=self.kind)
        row["ind_config"] = self.config
        return row

    @property
    def bytes(self):
        return sum(a.buffer_info()[1] * a.itemsize for a in
                   (self.t, self.pnl, self.hold, self.symbol, self.side, self.reason))


class IndicationGrid:
    TILE = 128

    def __init__(self, book):
        self.book = book
        self.lock = threading.RLock()
        self.jobs = {}
        self.queue = []
        self.worker = None
        self.workers = []
        self.signal_cache = {}
        self.behavior_cache = {}

    def token(self, symbol=""):
        book = self.book
        # ingest_bars publishes whole arrays; normal live refreshes retain
        # identity. A changed replay/input or cost/window generation retires
        # the old verdict before the new worker can publish.
        return (book._hist_set_signature, book._score_signature(),
                round(book.symbol_sl_min(symbol), 12) if symbol else None,
                json.dumps(getattr(book, "axis_settings", {}), sort_keys=True, separators=(",", ":")),
                tuple(sorted((s, id(rows), len(rows), float(getattr(book, "_bar_end_ts", {}).get(s) or 0))
                             for s, rows in list(book.bars.items())
                             if len(rows) >= book.min_bars and (not symbol or s == symbol))))

    def request(self, kind, config, symbol="", *, background=True):
        kind, config = str(kind).strip().lower(), str(config).strip().lower()
        if not symbol and len(self.book.bars) == 1:
            symbol = next(iter(self.book.bars))
        symbol = str(symbol).upper()
        key = (kind, config, symbol)
        token = self.token(symbol)
        with self.lock:
            job = self.jobs.get(key)
            if job is None or job["token"] != token:
                pack = "general" if kind == "general" else "indications"
                templates = [s for s in self.book.by_idx
                             if s.pack == pack and not s.indication_config]
                job = dict(kind=kind, config=config, symbol=symbol, token=token, ids=[], _ids=set(), completed=0,
                           completedBehaviors=0, requestedBehaviors=0,
                           derived=0, derivedByKind={},
                           qualifiedBase=0, qualifiedReal=0, _qualification={},
                           requested=len(templates), phase="queued", error="", elapsedMs=0)
                self.jobs[key] = job
                if key not in self.queue:
                    self.queue.append(key)
            if background and self.queue:
                self.workers = [worker for worker in self.workers if worker.is_alive()]
                limit = min(2, max(1, int(getattr(self.book, "system_workers", 1))), max(1, os.cpu_count() or 1))
                while self.queue and len(self.workers) < limit:
                    worker = threading.Thread(target=self._run, name="indication-grid", daemon=True)
                    self.workers.append(worker)
                    self.worker = worker
                    worker.start()
        return job

    def _run(self):
        while True:
            with self.lock:
                if not self.queue:
                    self.worker = None
                    return
                key = self.queue.pop(0)
                job = self.jobs[key]
                if job.get("running"):
                    continue
                job["running"] = True
            try:
                self.calculate(*key, job=job, tiles=1)
                with self.lock:
                    job["running"] = False
                    if self.jobs.get(key) is job and job["phase"] == "calculating":
                        self.queue.append(key)  # Fair rotation across independent lanes.
            except Exception as exc:
                with self.lock:
                    job["running"] = False
                    if self.jobs.get(key) is job:
                        job.update(phase="error", error=type(exc).__name__ + ": " + str(exc)[:160])

    def calculate(self, kind, config, symbol="", *, job=None, tiles=None):
        """Synchronous worker entry, also used by deterministic QA/research."""
        from set_engine import BAR_S, Progress, recent_direction_rows, _tape_fingerprint, _np
        if _np is None:
            raise RuntimeError("Indication grid requires the installed NumPy replay engine")
        job = job or self.request(kind, config, symbol, background=False)
        key = (job["kind"], job["config"], job["symbol"])
        token = job["token"]
        started = time.monotonic()
        last_elapsed = 0.0
        source = self.book
        # Only immutable replay parameters and references to published bars
        # cross the worker boundary. Do not clone the full live evidence graph.
        calc = copy.copy(source)
        calc.progress = Progress()
        calc.bars = {s: rows for s, rows in list(source.bars.items())
                     if len(rows) >= source.min_bars and s == key[2]}
        calc.calculation_cache = None
        calc._pick_lock = threading.RLock()
        calc._entry_rows_cache = {}
        calc._indication_grid = None
        pack = "general" if key[0] == "general" else "indications"
        templates = [s for s in list(source.by_idx)
                     if s.pack == pack and not s.indication_config]
        # Resolved protection, not slider identity, determines replay behavior.
        # Keep every identity/evaluation while walking identical immutable
        # inputs once (SL floors and TP caps often collapse adjacent steps).
        behavior_key = (token[:3], bool(calc.hist_honor_tp), pack)
        groups = self.behavior_cache.get(behavior_key)
        if groups is None:
            resolved = {}
            with calc.sl_scope(key[2]):
                for template in sorted(templates, key=lambda st: (st.kind != "base", st.step, st.sl_ratio)):
                    sl, tp = calc.pair_sl_tp(template.tp_pct, template.sl_ratio)
                    behavior = (round(sl, 12), round(tp, 12) if calc.hist_honor_tp else None,
                                template.kind, template.trail_arm, template.trail_give)
                    resolved.setdefault(behavior, []).append(template)
            groups = tuple(tuple(group) for group in resolved.values())
            if len(self.behavior_cache) >= 32:
                self.behavior_cache.clear()
            self.behavior_cache[behavior_key] = groups
        job["requestedBehaviors"] = len(groups)
        prepared = {}
        allowed_symbols = set(source.bars)
        self.signal_cache = {k: v for k, v in self.signal_cache.items() if k[0] in allowed_symbols}
        now = float(source.last_run or time.time())
        for sym, bars in calc.bars.items():
            cache_key = (sym, id(bars), token[:2], float(getattr(source, "_bar_end_ts", {}).get(sym) or 0))
            cached = self.signal_cache.get(cache_key)
            if cached is None:
                cached = calc.prepare_replay_signals(sym, now)
                # Keep one immutable signal frame per currently published symbol.
                self.signal_cache = {k: v for k, v in self.signal_cache.items() if k[0] != sym}
                self.signal_cache[cache_key] = cached
            signals, kind_sigs, warmup = cached
            lane = ([(d, conf) for d, conf, _ in signals.get(pack, ())]
                    if key[0] in ("general", "combined") else kind_sigs.get(key[0] + "|" + key[1]))
            if lane is None and not key[1]:
                lane = kind_sigs.get(key[0])
            if lane is None:
                continue
            prepared[sym] = (bars, [(d, conf, key[1]) for d, conf in lane], warmup,
                             (getattr(kind_sigs, "exits", {}) or {}).get(key[0] + "|" + key[1], {}))
        with self.lock:
            if self.jobs.get(key) is not job or self.token(key[2]) != token:
                return
            job["phase"] = "calculating" if prepared else "no-evidence"
        if not prepared:
            return
        first = int(job["completedBehaviors"])
        end = len(groups) if tiles is None else min(len(groups), first + max(1, tiles) * self.TILE)
        for start in range(first, end, self.TILE):
            if self.token(key[2]) != token:
                return
            tile = []
            tile_groups = groups[start:start + self.TILE]
            for aliases in tile_groups:
                template = aliases[0]
                st = template.catalog_copy()
                st.id = lane_set_id(key[0], key[1], template.id, key[2])
                st.template_set_id = template.id
                st.indication_kind, st.indication_config, st.indication_symbol = key
                st.parent_set_id = st.id if st.kind == "base" else lane_set_id(key[0], key[1], template.parent_set_id, key[2])
                tile.append(st)
            hist = {}
            counts = {}
            for sym, (bars, lane, warmup, exits) in prepared.items():
                time_bars = max(8, min(calc.hist_time_bars, max(8, len(bars) - warmup - 1)))
                calc._replay_core_vectorized(sym, bars, tile, lane, now, warmup, time_bars,
                    max(8, int(calc.scratch_s / BAR_S)), bool(calc.hist_honor_tp), hist, counts,
                    confidence_floor=0.58 if key[0] in ("general", "combined") else 0.52, lane_exits=exits)
            for st in tile:
                st.hist = recent_direction_rows(hist.get(st.id, ()), 96)
                for row in st.hist:
                    row.update(ind_kind=key[0], ind_config=key[1])
                st.n = counts.get(st.id, 0)
            expanded = []
            shared_bundles = {}
            for leader, aliases in zip(tile, tile_groups):
                tape = ColumnarTape(leader.hist, key[0], key[1])
                bundle = calc._fast_historic_bundle(leader.hist, ordered=True)
                for template in aliases:
                    st = leader if template.id == leader.template_set_id else template.catalog_copy()
                    st.id = lane_set_id(key[0], key[1], template.id, key[2])
                    st.template_set_id = template.id
                    st.indication_kind, st.indication_config, st.indication_symbol = key
                    st.parent_set_id = st.id if st.kind == "base" else lane_set_id(key[0], key[1], template.parent_set_id, key[2])
                    st.hist, st.n = tape, leader.n
                    expanded.append(st)
                    shared_bundles[st.id] = bundle
            # Score away from the live selection lock. Each publication holds
            # it for one object swap, never for a complete calculation tile.
            keep_fields = {"id", "pack", "tf", "sl_ratio", "trail_key", "trail_arm", "trail_give",
                "step", "tp_pct", "idx", "pack_i", "sl_i", "tr_i", "step_i", "kind",
                "live", "locked", "processing_active", "processing_reason"}
            for st in expanded:
                with source._pick_lock:
                    old = source.sets.get(st.id)
                    live_signature = _tape_fingerprint(old.live, tail=len(old.live)) if old else (0,)
                    if old is not None:
                        st.live = list(old.live)
                        st.locked = old.locked
                    elif st.id in getattr(source, "_pending_set_live", {}):
                        st.live = list(source._pending_set_live[st.id])
                full_n = st.n
                calc._score_one(st, bundle=None if st.evaluation_live() else shared_bundles[st.id])
                st.n = full_n
                with source._pick_lock:
                    if self.token(key[2]) != token or self.jobs.get(key) is not job:
                        return
                    if old is not None:
                        for field in st.__dataclass_fields__:
                            if field not in keep_fields:
                                setattr(old, field, getattr(st, field))
                        st = old
                        if _tape_fingerprint(st.live, tail=len(st.live)) != live_signature:
                            source._score_pair((st, None))  # A raced confirmed close decides.
                    else:
                        pending_live = getattr(source, "_pending_set_live", {}).get(st.id, ())
                        if _tape_fingerprint(pending_live, tail=len(pending_live)) != _tape_fingerprint(st.live, tail=len(st.live)):
                            st.live = list(pending_live)
                            source._score_pair((st, None))
                        st.idx = len(source.by_idx)
                        source.sets[st.id] = st
                        source.by_idx.append(st)
                        source._by_pack.setdefault(st.pack, []).append(st)
                        source._ids_by_pack.setdefault(st.pack, []).append(st.id)
                        source._ids_by_kind.setdefault(st.kind, []).append(st.id)
                        source._pending_set_live.pop(st.id, None)
                    job["ids"].append(st.id)
                    job["_ids"].add(st.id)
                    self.record_qualification(st, job)
            # Every enabled additional family owns its actual execution tape,
            # after the exact parent's own Base qualification. They never use
            # the combined pack's representative Block/DCA or axis PF.
            from indication_adjustment_grid import calculate_adjustments
            from axis_grid import calculate_axes
            parents = [source.sets[st.id] for st in expanded if st.id in source.sets]
            derived = []
            for sym, (bars, lane, warmup, exits) in prepared.items():
                derived.extend(calculate_adjustments(calc, sym, bars, parents, lane, warmup, now, lane_exits=exits))
                derived.extend(calculate_axes(calc, sym, bars, parents, lane, warmup, now, lane_exits=exits))
            for child in derived:
                with source._pick_lock:
                    old = source.sets.get(child.id)
                    live_signature = _tape_fingerprint(old.live, tail=len(old.live)) if old else (0,)
                    if old is not None:
                        child.live = list(old.live)
                        child.locked = old.locked
                    elif child.id in getattr(source, "_pending_set_live", {}):
                        child.live = list(source._pending_set_live[child.id])
                full_n = child.n
                if child.live:
                    calc._score_pair((child, None))
                child.n = full_n
                with source._pick_lock:
                    if self.token(key[2]) != token or self.jobs.get(key) is not job:
                        return
                    if old is not None:
                        for field in child.__dataclass_fields__:
                            if field not in keep_fields:
                                setattr(old, field, getattr(child, field))
                        child = old
                        if _tape_fingerprint(child.live, tail=len(child.live)) != live_signature:
                            source._score_pair((child, None))
                    else:
                        pending_live = getattr(source, "_pending_set_live", {}).get(child.id, ())
                        if _tape_fingerprint(pending_live, tail=len(pending_live)) != _tape_fingerprint(child.live, tail=len(child.live)):
                            child.live = list(pending_live)
                            source._score_pair((child, None))
                        child.idx = len(source.by_idx)
                        source.sets[child.id] = child
                        source.by_idx.append(child)
                        source._by_pack.setdefault(child.pack, []).append(child)
                        source._ids_by_pack.setdefault(child.pack, []).append(child.id)
                        source._ids_by_kind.setdefault(child.kind, []).append(child.id)
                        source._pending_set_live.pop(child.id, None)
                    job["ids"].append(child.id)
                    job["_ids"].add(child.id)
                    self.record_qualification(child, job)
                    job["derived"] += 1
                    job["derivedByKind"][child.kind] = job["derivedByKind"].get(child.kind, 0) + 1
            with source._pick_lock:
                job["completed"] += len(expanded)
                job["completedBehaviors"] += len(tile_groups)
                elapsed = (time.monotonic() - started) * 1000
                job["elapsedMs"] = round(job["elapsedMs"] + elapsed - last_elapsed, 1)
                last_elapsed = elapsed
                source._snap_ts = 0
                source._invalidate_entry_cache()
            time.sleep(0)
        with self.lock:
            if self.jobs.get(key) is job and self.token(key[2]) == token:
                job["phase"] = "ready" if job["completed"] == job["requested"] else "calculating"

    def candidates(self, kind, config, side, symbol=""):
        job = self.request(kind, config, symbol)
        if job["token"] != self.token(job["symbol"]):
            return []
        return [self.book.sets[sid] for sid in list(job["ids"])
                if sid in self.book.sets and self.book.sets[sid].kind in ("base", "trail", "block", "axis")
                and not self.book.sets[sid].axis_key.startswith("block-add:")
                and self.book.execution_allowed(self.book.sets[sid], self.book.sets[sid].pack, side)]

    def current(self, state):
        """Submission gate: a retired worker result cannot execute."""
        job = self.jobs.get((state.indication_kind, state.indication_config, state.indication_symbol))
        return bool(job and job["token"] == self.token(state.indication_symbol) and state.id in job["_ids"])

    def record_qualification(self, state, job=None):
        """Update two counters for a published tile or one changed live Set."""
        with self.lock:
            job = job or self.jobs.get((state.indication_kind, state.indication_config, state.indication_symbol))
            if job is None or state.id not in job["_ids"]:
                return
            value = int(bool(state.stage_ledger.get("base"))) | (int(bool(state.stage_ledger.get("real"))) << 1)
            old = job["_qualification"].get(state.id, 0)
            job["qualifiedBase"] += (value & 1) - (old & 1)
            job["qualifiedReal"] += (value >> 1) - (old >> 1)
            job["_qualification"][state.id] = value

    def snapshot(self):
        with self.lock:
            rows = []
            for (kind, config, symbol), job in self.jobs.items():
                current = job["token"] == self.token(symbol)
                rows.append(dict(kind=kind, config=config, symbol=symbol, requested=job["requested"],
                    completed=job["completed"] if current else 0, phase=job["phase"] if current else "stale",
                    requestedBehaviors=job["requestedBehaviors"], completedBehaviors=job["completedBehaviors"] if current else 0,
                    derived=job["derived"] if current else 0, derivedByKind=dict(job["derivedByKind"]) if current else {},
                    qualifiedBase=job["qualifiedBase"] if current else 0,
                    qualifiedReal=job["qualifiedReal"] if current else 0,
                    minPf=self.book.min_pf, elapsedMs=job["elapsedMs"], error=job["error"]))
            return dict(version=1, tile=self.TILE, workers=sum(worker.is_alive() for worker in self.workers),
                        workerLimit=min(2, max(1, int(getattr(self.book, "system_workers", 1)))),
                        configurations=rows, independent=True,
                        pfDefinition="return/cost", qualificationScope="current-requested-sets",
                        coverageScope="requested-configurations-only",
                        inputWatermarks=dict(getattr(self.book, "_bar_end_ts", {})),
                        complete=bool(rows) and all(r["phase"] == "ready" for r in rows))
