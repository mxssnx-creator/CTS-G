#!/usr/bin/env python3
"""Exact public-candle six-hour execution audit after six-hour preparation.

Earlier candles warm indicators only. Every configured exit is retained;
identical effective exit behaviours share numerical work, not identities.
Independent indication evidence is symbol × configuration × exit × side.
The native combined packs retain their existing Set × side evidence scope.
No private API or order endpoint is called.
"""
from __future__ import annotations

import argparse
import ast
import bisect
from collections import defaultdict, deque
from concurrent.futures import ProcessPoolExecutor, as_completed
import gzip
import gc
import hashlib
import html
import json
import math
from pathlib import Path
import pickle
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/pulse"))
sys.path.insert(0, str(ROOT / "scripts"))
import sim_12h_account as account_model
import set_engine as se
from contracts import INDICATION_KINDS
from position_cost import last_n_cost_pf, net_return_pf, clears_pf, cost_as_frac

BAR = 60


def release_working_memory():
    """Release completed lane cycles and free numeric allocator pages."""
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL(None).malloc_trim(0)
    except (AttributeError, OSError):
        pass


def physics_ast(function):
    """Hash math unchanged by explicit, unused-object lifetime releases."""
    import inspect
    class LifetimeOnly(ast.NodeTransformer):
        def visit_Expr(self, node):
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "release_working_memory":
                return None
            return self.generic_visit(node)
        def visit_Delete(self, node):
            if all(isinstance(target, ast.Name) and target.id in
                   {"axes", "adjustments", "child", "derived", "catalog", "tapes", "candidates", "evidence"}
                   for target in node.targets):
                return None
            return node
    tree = LifetimeOnly().visit(ast.parse(inspect.getsource(function)))
    return ast.dump(tree, include_attributes=False)

PROFILES = {
    "baseline": {},
    "short-history-5": {"baseEvalPosCount": 5, "setPfWindow": 5,
        "setMinSamples": 5, "mainEvalPosCount": 5, "realEvalPosCount": 5},
}


def profile_overlay(overlay, profile):
    """Frozen diagnostic change affects admission windows, never price replay."""
    if profile not in PROFILES:
        raise ValueError("Unknown audit profile: " + str(profile))
    return dict(overlay, **PROFILES[profile])


def scoring_signature(data_dir, symbols, overlay, profile):
    import inspect
    functions = (build_causal_candidates, derived_replay, base_qualified_parent_uids, materialize_aliases,
                 summarize_one_cache, canonical_stats, stage_report, account_model.stage_flags,
                 account_model.Evidence, account_model._rolling_ratio)
    parts = [physics_ast(fn) for fn in functions]
    parts.extend(sha(ROOT / "server/pulse" / name) for name in
                 ("axis_grid.py", "indication_adjustment_grid.py", "position_cost.py"))
    parts.extend(sha(Path(data_dir) / "funding" / (symbol + ".json")) for symbol in symbols)
    parts.append(json.dumps(profile_overlay(overlay, profile), sort_keys=True))
    return hashlib.sha256("".join(parts).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def utc(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def model_signature():
    dependencies = {
        Path(__file__): {"replay_worker", "load_market"},
        ROOT / "scripts/sim_12h_account.py": {"build_catalog"},
        ROOT / "server/pulse/set_engine.py": {"load", "pair_sl_tp", "sl_scope", "set_symbol_sl_floors",
            "prepare_replay_signals", "_replay_core_vectorized", "_replay_core_vectorized_scoped",
            "hist_fill", "recent_direction_rows", "indication_kind_votes", "indication_kind_votes_frame",
            "votes_to_signal", "core_pack_votes", "general_signal", "ingest_bars"},
    }
    pieces = []
    for path, names in dependencies.items():
        nodes = [node for node in ast.walk(ast.parse(path.read_text()))
                 if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
        pieces.append(ast.dump(ast.Module(body=nodes, type_ignores=[]), include_attributes=False).encode())
    for filename in ("indication_engine.py", "position_cost.py", "tactic_indications.py", "risk_variants.py", "contracts.py"):
        pieces.append((ROOT / "server/pulse" / filename).read_bytes())
    return hashlib.sha256(b"".join(pieces)).hexdigest()


def load_market(path, end_ms):
    data = json.loads(Path(path).read_text())
    rows = data["rows"]
    required = list(range(end_ms - 720 * 60000, end_ms, 60000))
    actual = [r[0] for r in rows if r[0] >= required[0]]
    if actual != required:
        raise ValueError(f"{data['symbol']}: the preparation/execution candles are incomplete")
    if [r[0] for r in rows] != list(range(rows[0][0], end_ms, 60000)):
        raise ValueError(f"{data['symbol']}: indicator warmup has missing or duplicated candles")
    for stamp, row in rows:
        if len(row) != 5 or not all(math.isfinite(float(v)) for v in row):
            raise ValueError(f"{data['symbol']}: invalid OHLCV at {stamp}")
        o, h, l, c, volume = row
        if min(o, h, l, c) <= 0 or volume < 0 or h < max(o, l, c) or l > min(o, h, c):
            raise ValueError(f"{data['symbol']}: inconsistent OHLCV at {stamp}")
    return data


def replay_worker(job):
    """Replay full configured Normal/Trailing grids, retaining every close."""
    started = time.perf_counter()
    path = Path(job["cache"]) / (job["symbol"] + ".pkl")
    signature = (job["model"], sha(Path(job["data"]) / (job["symbol"] + ".json")), sha(job["overlay"]))
    if path.exists():
        prior = pickle.loads(path.read_bytes())
        if prior.get("signature") == signature:
            return dict(symbol=job["symbol"], path=str(path), cached=True, seconds=prior["seconds"])
    source = load_market(Path(job["data"]) / (job["symbol"] + ".json"), job["end"])
    bars = [row[1] for row in source["rows"]]
    start_s = source["rows"][0][0] / 1000
    now = source["rows"][-1][0] / 1000
    n, warmup = len(bars), len(bars) - 720
    book = se.SetBook()
    overlay = json.loads(Path(job["overlay"]).read_text())
    book.load(overlay)
    book.lookback = n
    book.exact_replay_window = False
    book.ingest_bars(job["symbol"], [[stamp, *row] for stamp, row in source["rows"]])
    book.last_run = now
    native = account_model.build_catalog(book)
    signals, kinds, _ = book.prepare_replay_signals(job["symbol"], now=now)
    lanes = {"pack:" + pack: (values, .58, {}) for pack, values in signals.items()}
    lanes.update({key: ([(d, confidence, key) for d, confidence in values], .52,
                        (getattr(kinds, "exits", {}) or {}).get(key, {}))
                  for key, values in kinds.items() if "|" in key})
    columns = defaultdict(list)
    descriptions = []
    coverage = []
    reasons = []
    reason_index = {}
    se.REPLAY_HIST_CAP = max(192, 720)
    native_by_pack = {pack: [row for row in native if row["pack"] == pack] for pack in book.packs}
    for lane_number, (lane_key, (lane, confidence, exits)) in enumerate(sorted(lanes.items())):
        pack = lane_key.removeprefix("pack:") if lane_key.startswith("pack:") else "indications"
        templates = native_by_pack[pack]
        count_requested = sum(row["mult"] for row in templates)
        hits = sum(d != 0 and conf >= confidence for d, conf, _ in lane[warmup:])
        coverage.append(dict(lane=lane_key, requestedConfigs=count_requested, evaluatedConfigs=count_requested,
                             uniqueExitBehaviours=len(templates), signalBars=hits))
        if hits == 0:
            continue
        states = [book.sets[row["set_id"]] for row in templates]
        hist, counts = {}, {}
        book._replay_core_vectorized(job["symbol"], bars, states, lane, now, warmup,
            book.hist_time_bars, max(8, int(book.scratch_s / BAR)), bool(book.hist_honor_tp), hist, counts,
            confidence_floor=confidence, lane_exits=exits)
        eligible = {side: np.flatnonzero(np.array([d == side and conf >= confidence
                                                 for d, conf, _ in lane])) for side in (1, -1)}
        for description, state in zip(templates, states):
            tape = hist.get(state.id, [])
            if len(tape) != counts.get(state.id, 0):
                raise AssertionError("Replay retention discarded a requested close")
            local_uid = len(descriptions)
            descriptions.append(dict(description, lane=lane_key, localUid=local_uid, symbolScoped=True))
            latest = {1: None, -1: None}
            for row in tape:
                exit_bar = int(round((row["t"] - start_s) / BAR))
                entry_bar = exit_bar - int(round(row["hold_s"] / BAR))
                side = 1 if str(row["side"]).startswith("L") else -1
                reason = str(row["reason"])
                if reason not in reason_index:
                    reason_index[reason] = len(reasons)
                    reasons.append(reason)
                for name, value in dict(uid=local_uid, side=side, entry=entry_bar, exit=exit_bar,
                                        raw=float(row["pnl_pct"]), reason=reason_index[reason]).items():
                    columns[name].append(value)
                latest[side] = max(latest[side] or -1, exit_bar)
            for side in (1, -1):
                first = warmup if latest[side] is None else latest[side] + max(0, int(book.cooldown_bars))
                positions = eligible[side]
                index = int(np.searchsorted(positions, first))
                if index < len(positions):
                    entry_bar = int(positions[index])
                    if entry_bar < warmup:
                        continue
                    if n - 1 - entry_bar >= book.hist_time_bars:
                        raise AssertionError("Open-at-end reconstruction contradicts the engine time stop")
                    for name, value in dict(uid=local_uid, side=side, entry=entry_bar, exit=-1,
                                            raw=0.0, reason=-1).items():
                        columns[name].append(value)
        print(json.dumps(dict(symbol=job["symbol"], lane=lane_key, configs=count_requested,
                              signalBars=hits, elapsedS=round(time.perf_counter() - started, 2))), flush=True)
    arrays = {key: np.asarray(columns[key], dtype=np.float64 if key == "raw" else np.int32)
              for key in ("uid", "side", "entry", "exit", "raw", "reason")}
    result = dict(symbol=job["symbol"], signature=signature, startS=start_s, bars=bars,
                  catalog=descriptions, coverage=coverage, reasons=reasons, core=arrays,
                  seconds=round(time.perf_counter() - started, 3))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".pkl.tmp")
    temporary.write_bytes(pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL))
    temporary.replace(path)
    return dict(symbol=job["symbol"], path=str(path), cached=False, seconds=result["seconds"])


def canonical_stats(gross_returns, cost_pct, weights=None):
    if not len(gross_returns):
        return dict(n=0, pf=None, netAvgPct=None, wins=0, losses=0)
    gross = np.asarray(gross_returns, dtype=float)
    weights = np.ones(len(gross)) if weights is None else np.asarray(weights, dtype=float)
    if gross.shape != weights.shape:
        raise ValueError("Return observations and multiplicities must align")
    valid = np.isfinite(gross) & np.isfinite(weights) & (weights > 0)
    invalid = int((~valid).sum())
    gross, weights = gross[valid], weights[valid]
    if not len(gross):
        return dict(n=0, pf=None, netAvgPct=None, wins=0, losses=0, invalidObservations=invalid)
    net = gross - cost_as_frac(cost_pct)
    denominator = float(weights.sum())
    mean = float(np.dot(net, weights) / denominator)
    stats = dict(n=int(denominator), pf=round(net_return_pf(mean, cost_pct), 6),
                netAvgPct=round(mean * 100, 8), netSumPctUnits=round(float(np.dot(net, weights)) * 100, 6),
                wins=int(weights[net > 0].sum()), losses=int(weights[net < 0].sum()))
    if invalid:
        stats["invalidObservations"] = invalid
    return stats


def build_causal_candidates(results, book):
    """Every entry sees only closes strictly earlier than its entry minute."""
    catalogs, catalog_index = [], {}
    values = defaultdict(list)
    for symbol_index, result in enumerate(results):
        remap = {}
        for row in result["catalog"]:
            scope = result["symbol"] if row["symbolScoped"] else ""
            key = (scope, row["lane"], row["set_id"])
            if key not in catalog_index:
                catalog_index[key] = len(catalogs)
                catalogs.append(dict(row, uid=len(catalogs), symbol=scope))
            remap[row["localUid"]] = catalog_index[key]
        source = result["core"]
        values["uid"].append(np.asarray([remap[int(u)] for u in source["uid"]], dtype=np.int32))
        for key in ("side", "entry", "exit", "raw"):
            values[key].append(source[key])
        for key, default in (("parentEntry", -1), ("qtyRatio", 1.0), ("entryPrice", 0.0)):
            values[key].append(source.get(key, np.full(len(source["uid"]), default)))
        values["sym"].append(np.full(len(source["uid"]), symbol_index, dtype=np.int16))
        values["reason"].append(np.asarray([result["reasons"][int(r)] if r >= 0 else "open"
                                             for r in source["reason"]], dtype=object))
    tapes = {key: np.concatenate(parts) for key, parts in values.items()}
    closed = tapes["exit"] >= 0
    group = tapes["uid"].astype(np.int64) * 2 + (tapes["side"] > 0)
    funding = np.zeros(len(group), dtype=float)
    for symbol_index, result in enumerate(results):
        selected = tapes["sym"] == symbol_index
        indices = np.flatnonzero(selected)
        entry_prices = np.asarray([result["bars"][int(i)][3] for i in tapes["entry"][selected]], dtype=float)
        entry_prices = np.where(tapes["entryPrice"][selected] > 0, tapes["entryPrice"][selected], entry_prices)
        for tick in result.get("funding", []):
            funding_bar = int((float(tick["fundingTime"]) / 1000 - result["startS"]) / BAR)
            held = (tapes["entry"][selected] < funding_bar - 1) & ((tapes["exit"][selected] >= funding_bar) | (tapes["exit"][selected] < 0))
            mark = float(tick.get("markPrice") or result["bars"][funding_bar][3])
            funding[indices[held]] += tapes["side"][selected][held] * float(tick["fundingRate"]) * mark / entry_prices[held]
    tapes["fundingPaidReturn"] = funding
    tapes["evidenceRaw"] = tapes["raw"] - funding
    evidence = account_model.Evidence(group[closed], tapes["exit"][closed], tapes["evidenceRaw"][closed], book.cost_pct,
                                      tiebreak=tapes["sym"][closed])
    sim_start = len(results[0]["bars"]) - 360
    mask = (tapes["entry"] >= sim_start)
    candidates = {key: value[mask] for key, value in tapes.items()}
    own_group = group[mask]
    previous, starts = evidence.lookup(own_group, candidates["entry"])
    flags = account_model.stage_flags(evidence, previous, starts, book.eval_need(),
                                      book._stage_window_ns(), book.stage_min_pf)
    # The shared strict >1.02 guard applies even if a caller sets that floor.
    flags["base_ok"] &= flags["r30"] > 1.02 + 1e-9
    flags["main_ok"] &= flags["base_ok"]
    flags["real_ok"] &= flags["main_ok"]
    candidates.update(flags)
    candidates["countBeforeEntry"] = np.where(previous >= 0, previous - starts + 1, 0)
    candidates["dd_ok"] = np.ones(len(previous), dtype=bool)
    # Only Base-qualified Sets reach downstream DDT work. Cache one exact
    # own-tape episode calculation per evidence endpoint, never a pooled kind.
    dd_cache = {}
    for index in np.flatnonzero(flags["base_ok"]):
        end, first = int(previous[index]), int(starts[index])
        key = (int(own_group[index]), end)
        if key not in dd_cache:
            lo = max(first, end - int(book.ddt_window()) + 1)
            dd_cache[key] = account_model.ddt_max_s_fast(
                evidence.exit[lo:end + 1].astype(float) * BAR,
                evidence.tiebreak[lo:end + 1], evidence.moves[lo:end + 1], cost_as_frac(book.cost_pct))
        candidates["dd_ok"][index] = dd_cache[key] <= book.max_dd_s + 1e-9
    candidates["admitted"] = flags["real_ok"] & candidates["dd_ok"]
    return catalogs, tapes, candidates, evidence


def merge_replays(parent, child):
    if child is None:
        return parent
    result = dict(parent)
    offset = len(parent["catalog"])
    result["catalog"] = parent["catalog"] + [dict(row, localUid=int(row["localUid"]) + offset)
                                             for row in child["catalog"]]
    reason_offset = len(parent["reasons"])
    result["reasons"] = parent["reasons"] + child["reasons"]
    result["core"] = {}
    result["parentOwnEvidence"] = child.get("parentOwnEvidence", {})
    for key in set(parent["core"]) | set(child["core"]):
        left = parent["core"].get(key, np.full(len(parent["core"]["uid"]), -1 if key == "parentEntry" else 1. if key == "qtyRatio" else 0.))
        right = child["core"].get(key, np.full(len(child["core"]["uid"]), 0.))
        if key == "uid":
            right = right + offset
        elif key == "reason":
            right = np.where(right >= 0, right + reason_offset, right)
        result["core"][key] = np.concatenate((left, right))
    return result


def account_stats(rows, cost_pct):
    net = [row["net"] / row["notional"] for row in rows]
    stats = dict(n=len(rows), pf=round(net_return_pf(sum(net) / len(net), cost_pct), 6) if net else None,
                 netAvgPct=sum(net) / len(net) * 100 if net else None,
                 wins=sum(v > 0 for v in net), losses=sum(v < 0 for v in net))
    stats.update(netUsdt=round(sum(row["net"] for row in rows), 8),
                 grossUsdt=round(sum(row["gross"] for row in rows), 8))
    return stats


def materialize_aliases(meta):
    """Expand identities only when an own prototype reaches real admission."""
    if meta.get("set_ids") is not None:
        return meta
    from contracts import stable_key
    from indication_adjustment_grid import adjustment_set_id
    result = dict(meta)
    parents = tuple(meta["parentSetIds"])
    if meta["aliasKind"] == "axis":
        result["set_ids"] = ["axis:" + stable_key(parent, meta["symbol"], meta["aliasSide"],
                             str(meta["axisKey"]).split(":")[0], meta["relativeCount"]) for parent in parents]
    else:
        result["set_ids"] = [adjustment_set_id(parent, meta["aliasKind"], meta["relativeCount"])
                             for parent in parents]
    if len(result["set_ids"]) != int(meta["mult"]):
        raise AssertionError("Compact alias expansion changed the logical configuration count")
    return result


def base_qualified_parent_uids(evidence, book, end_bar):
    """Own parents qualifying at any closed endpoint before the final bar."""
    prior = np.arange(len(evidence.exit), dtype=np.int64)
    lengths = np.diff(np.append(evidence.first, len(evidence.exit)))
    first = np.repeat(evidence.first, lengths)
    ratio, enough = evidence.partial_ratio(prior, first, book.pf_n, book.eval_need())
    counts = prior - first + 1
    qualified = enough & (counts >= book.eval_need()) & (ratio > 1.02 + 1e-9) & (ratio + 1e-9 >= book.stage_min_pf["base"]) & (evidence.exit < end_bar)
    return np.unique(evidence.group[qualified] // 2)


def derived_replay(result, book, catalog, tapes, evidence):
    """Replay every descendant of a causally Base-qualified own parent.

    The numerical replay covers the preparation and execution tape. Parent
    selection uses completed own evidence and child admission is subsequently
    computed from its own independent closes. Rejected parents retain their
    requested descendant count without consuming price replay work.
    """
    from axis_grid import calculate_axes, axis_ranges
    from indication_adjustment_grid import calculate_adjustments, adjustment_set_id
    from contracts import stable_key
    n = len(result["bars"])
    offset = n - 720
    owners = base_qualified_parent_uids(evidence, book, n - 1)
    counts_by_uid = np.bincount(tapes["uid"], minlength=len(catalog))
    starts = np.concatenate(([0], np.cumsum(counts_by_uid)))
    if len(tapes["uid"]) and np.any(np.diff(tapes["uid"]) < 0):
        raise AssertionError("Raw replay is expected to preserve contiguous prototype tapes")
    requested = sum(row["requestedConfigs"] for row in result["coverage"]) * (2 * len(axis_ranges(book)) + 2 * (2 * len(book.block_counts) + book.dca_max_steps))
    diagnostics = dict(requestedDescendantDirectionConfigs=requested, ownBaseEligibleParents=int(len(owners)),
                       replayedDescendantConfigs=0, hypothesisClosedRows=0, complete=True)
    if not len(owners):
        return None, diagnostics
    book.lookback = n
    book.ingest_bars(result["symbol"], [[(result["startS"] + i * BAR) * 1000, *bar]
                                      for i, bar in enumerate(result["bars"])])
    book._bar_end_ts[result["symbol"]] = result["startS"] + (n - 1) * BAR
    core_signals, kind_signals, _ = book.prepare_replay_signals(result["symbol"], now=book._bar_end_ts[result["symbol"]])
    lane_values = {"pack:" + pack: values for pack, values in core_signals.items()}
    lane_values.update({key: [(d, conf, key) for d, conf in values]
                        for key, values in kind_signals.items() if "|" in key})
    lane_exits = getattr(kind_signals, "exits", {}) or {}
    result["laneExits"] = lane_exits
    grouped = defaultdict(list)
    prototypes, captures = {}, {}
    shared_parent_aliases = {}
    for uid in owners:
        uid = int(uid)
        meta = catalog[uid]
        parent = book.sets[meta["set_id"]].catalog_copy()
        parent.id = result["symbol"] + "|" + meta["lane"] + "|" + meta["set_id"]
        if meta["lane"].startswith("pack:"):
            parent.indication_kind = "general" if parent.pack == "general" else "combined"
            parent.indication_config = "general_signal" if parent.pack == "general" else "combined_vote"
        else:
            parent.indication_kind, parent.indication_config = meta["lane"].split("|", 1)
        parent.indication_symbol = result["symbol"]
        parent.template_set_id = meta["set_id"]
        prototypes[parent.id] = (uid, meta)
        shared_parent_aliases[parent.id] = tuple(result["symbol"] + "|" + meta["lane"] + "|" + original
                                                 for original in meta["set_ids"])
        grouped[meta["lane"]].append(parent)
        rows = []
        for i in range(int(starts[uid]), int(starts[uid + 1])):
            if tapes["exit"][i] < 0:
                continue
            rows.append(se.hist_fill(result["startS"] + int(tapes["exit"][i]) * BAR, result["symbol"],
                int(tapes["side"][i]), float(tapes["evidenceRaw"][i]),
                (int(tapes["exit"][i]) - int(tapes["entry"][i])) * BAR, str(tapes["reason"][i])))
        captures[parent.id] = sorted(rows, key=lambda row: row["t"])
    # Scoring is performed below from full own chronological tapes. Avoid
    # duplicate terminal score work and its bounded runtime retention here.
    original_score, original_bundle = book._score_pair, book._fast_historic_bundle
    book._score_pair = lambda pair: None
    book._fast_historic_bundle = lambda *args, **kwargs: None
    descriptions, columns, reasons = [], defaultdict(list), []
    reason_index = {}
    try:
        for lane_key, parents in grouped.items():
            lane_hash = hashlib.sha256(lane_key.encode()).hexdigest()[:20]
            lane_dir = getattr(book, "_audit_lane_cache_dir", None)
            lane_path = Path(lane_dir) / (lane_hash + ".pkl") if lane_dir else None
            lane_signature = getattr(book, "_audit_derived_signature", "")
            if lane_path and lane_path.exists():
                with lane_path.open("rb") as file:
                    saved_lane = pickle.load(file)
                if saved_lane.get("signature") == lane_signature and saved_lane.get("lane") == lane_key:
                    uid_offset = len(descriptions)
                    for row in saved_lane["catalog"]:
                        descriptions.append(dict(row, localUid=int(row["localUid"]) + uid_offset))
                    reason_remap = {}
                    for code, why in enumerate(saved_lane["reasons"]):
                        if why not in reason_index:
                            reason_index[why] = len(reasons); reasons.append(why)
                        reason_remap[code] = reason_index[why]
                    for name, values in saved_lane["columns"].items():
                        if name == "uid":
                            values = values + uid_offset
                        elif name == "reason":
                            values = np.asarray([reason_remap[int(code)] for code in values], dtype=np.int32)
                        columns[name].extend(values.tolist())
                    diagnostics["replayedDescendantConfigs"] += saved_lane["evaluated"]
                    diagnostics["hypothesisClosedRows"] += saved_lane["closes"]
                    print(json.dumps(dict(phase="descendants", symbol=result["symbol"], lane=lane_key,
                        cached=True, childConfigs=diagnostics["replayedDescendantConfigs"],
                        closes=diagnostics["hypothesisClosedRows"])), flush=True)
                    del saved_lane
                    release_working_memory()
                    continue
            first_uid, first_column = len(descriptions), len(columns["uid"])
            first_evaluated, first_closed = diagnostics["replayedDescendantConfigs"], diagnostics["hypothesisClosedRows"]
            if len(lane_values[lane_key]) != n:
                raise AssertionError("Replay signals were truncated before descendant preparation")
            lane = lane_values[lane_key][offset:]
            exits = {i - offset: value for i, value in lane_exits.get(lane_key, {}).items() if i >= offset}
            axes = calculate_axes(book, result["symbol"], result["bars"][offset:], parents, lane, 0,
                                  book._bar_end_ts[result["symbol"]], lane_exits=exits, capture=True,
                                  parent_captures={parent.id: captures[parent.id] for parent in parents})
            adjustments = calculate_adjustments(book, result["symbol"], result["bars"][offset:], parents, lane, 0,
                                  book._bar_end_ts[result["symbol"]], lane_exits=exits, capture=True,
                                  assume_qualified=True)
            open_by_id = defaultdict(list)
            for row in [*getattr(axes, "open", []), *adjustments.open]:
                open_by_id[row["set_id"]].append(row)
            for child in [*axes, *adjustments]:
                parent_uid, parent_meta = prototypes[child.parent_set_id]
                diagnostics["replayedDescendantConfigs"] += parent_meta["mult"]
                if not len(child.hist) and not open_by_id[child.id]:
                    continue
                uid = len(descriptions)
                axis = str(child.axis_key)
                if child.kind == "axis":
                    side_name = next((row["side"] for row in child.hist), "LONG")
                    # Axis IDs include their explicit parent direction even
                    # when the child has no closes; inspect helper metadata.
                    side_name = str(child.stage_ledger.get("parentDirection") or side_name)
                    variant = "axis"
                else:
                    variant = "block-add" if axis.startswith("block-add:") else "block" if axis.startswith("block-active:") else "dca"
                    side_name = ""
                descriptions.append(dict(parent_meta, localUid=uid, set_id=child.id, set_ids=None,
                    mult=parent_meta["mult"], kind=child.kind, parentId=child.parent_set_id,
                    parentSetIds=shared_parent_aliases[child.parent_set_id], aliasKind=variant, aliasSide=side_name,
                    parentTemplateId=parent_meta["set_id"], axisKey=axis, relativeCount=child.relative_count,
                    volumeRatio=child.volume_ratio, symbolScoped=True))
                events = [(row, False) for row in child.hist] + [(row, True) for row in open_by_id[child.id]]
                for row, is_open in events:
                    exited = -1 if is_open else int(round((float(row["t"]) - result["startS"]) / BAR))
                    entered = int(row.get("_entry", (float(row["t"]) - result["startS"] - float(row["hold_s"])) / BAR))
                    if "_entry" in row:
                        entered += offset
                    parent_entry = int(row.get("_parent_entry", entered - offset)) + offset
                    direction = 1 if str(row["side"]).startswith("L") else -1
                    # Adjustment histories created before parent qualification
                    # remain outside the child's eligible evidence tape.
                    if child.kind != "axis":
                        index, first_index = evidence.lookup(np.array([parent_uid * 2 + (direction > 0)]), np.array([parent_entry]))
                        flags = account_model.stage_flags(evidence, index, first_index, book.eval_need(), book._stage_window_ns(), book.stage_min_pf)
                        if not flags["base_ok"][0] or flags["r30"][0] <= 1.02 + 1e-9:
                            continue
                    why = "open" if is_open else str(row.get("reason") or "native")
                    if why not in reason_index:
                        reason_index[why] = len(reasons); reasons.append(why)
                    values = dict(uid=uid, side=direction, entry=entered, exit=exited,
                        raw=0.0 if is_open else float(row["pnl_pct"]), reason=reason_index[why],
                        parentEntry=parent_entry, qtyRatio=float(row.get("_qty", child.volume_ratio or 1)),
                        entryPrice=float(row.get("_avg", result["bars"][entered][3])))
                    for name, value in values.items():
                        columns[name].append(value)
                    diagnostics["hypothesisClosedRows"] += int(not is_open)
            print(json.dumps(dict(phase="descendants", symbol=result["symbol"], lane=lane_key,
                parents=len(parents), childConfigs=diagnostics["replayedDescendantConfigs"],
                closes=diagnostics["hypothesisClosedRows"])), flush=True)
            if lane_path:
                lane_path.parent.mkdir(parents=True, exist_ok=True)
                lane_columns = {name: np.asarray(values[first_column:]) for name, values in columns.items()}
                lane_columns["uid"] -= first_uid
                payload = dict(signature=lane_signature, lane=lane_key,
                    catalog=[dict(row, localUid=int(row["localUid"]) - first_uid) for row in descriptions[first_uid:]],
                    columns=lane_columns, reasons=reasons,
                    evaluated=diagnostics["replayedDescendantConfigs"] - first_evaluated,
                    closes=diagnostics["hypothesisClosedRows"] - first_closed)
                temporary = lane_path.with_suffix(".tmp")
                with temporary.open("wb") as file:
                    pickle.dump(payload, file, protocol=pickle.HIGHEST_PROTOCOL)
                temporary.replace(lane_path)
                del payload, lane_columns
            del axes, adjustments
            release_working_memory()
    finally:
        book._score_pair, book._fast_historic_bundle = original_score, original_bundle
    if not columns["uid"]:
        return None, diagnostics
    core = {key: np.asarray(value, dtype=np.float64 if key in ("raw", "qtyRatio", "entryPrice") else np.int32)
            for key, value in columns.items()}
    parent_evidence = {parent_id: {side: [[int(round((row["t"] - result["startS"] - row["hold_s"]) / BAR)),
                    int(round((row["t"] - result["startS"]) / BAR)), float(row["pnl_pct"])]
                for row in rows if row["side"] == ("LONG" if side > 0 else "SHORT")]
                for side in (1, -1)} for parent_id, rows in captures.items()}
    return dict(symbol=result["symbol"], startS=result["startS"], bars=result["bars"], funding=result["funding"],
                catalog=descriptions, core=core, reasons=reasons, coverage=[], parentOwnEvidence=parent_evidence), diagnostics


def summarize_one_cache(path, overlay_path, data_dir, profile="baseline"):
    """Bound scoring memory to one symbol and retain only admitted events."""
    overlay = profile_overlay(json.loads(Path(overlay_path).read_text()), profile)
    summary_signature = scoring_signature(data_dir, [Path(path).stem], overlay, profile)
    summary_path = Path(path).with_name(Path(path).stem + "." + profile + ".summary.pkl")
    if summary_path.exists():
        saved_summary = pickle.loads(summary_path.read_bytes())
        if saved_summary.get("signature") == summary_signature:
            return saved_summary["result"]
    result = pickle.loads(Path(path).read_bytes())
    result["funding"] = json.loads((Path(data_dir) / "funding" / (result["symbol"] + ".json")).read_text())
    book = se.SetBook()
    book.load(overlay)
    catalog, tapes, candidates, evidence = build_causal_candidates([result], book)
    derived_key = hashlib.sha256((profile + repr(result["signature"]) + json.dumps(overlay, sort_keys=True)
        + sha(Path(data_dir) / "funding" / (result["symbol"] + ".json"))
        + sha(ROOT / "server/pulse/axis_grid.py") + sha(ROOT / "server/pulse/indication_adjustment_grid.py")
        + sha(ROOT / "server/pulse/position_cost.py")
        + physics_ast(derived_replay) + physics_ast(base_qualified_parent_uids)
        + physics_ast(account_model.Evidence) + physics_ast(account_model._rolling_ratio)).encode()).hexdigest()
    derived_path = Path(path).parent / (result["symbol"] + "." + profile + ".derived.pkl")
    book._audit_lane_cache_dir = Path(path).parent / "derived-lanes" / profile / result["symbol"]
    book._audit_derived_signature = derived_key
    saved = pickle.loads(derived_path.read_bytes()) if derived_path.exists() else None
    if saved and saved["signature"] == derived_key:
        derived, derived_diagnostics = saved["result"], saved["coverage"]
    else:
        derived, derived_diagnostics = derived_replay(result, book, catalog, tapes, evidence)
        temporary = derived_path.with_suffix(".tmp")
        temporary.write_bytes(pickle.dumps(dict(signature=derived_key, result=derived, coverage=derived_diagnostics), protocol=pickle.HIGHEST_PROTOCOL))
        temporary.replace(derived_path)
    if derived is not None:
        result = merge_replays(result, derived)
        del derived, catalog, tapes, candidates, evidence
        release_working_memory()
        catalog, tapes, candidates, evidence = build_causal_candidates([result], book)
    start = len(result["bars"]) - 360
    hours = stage_report(catalog, candidates, book.cost_pct, start, result["startS"])
    prep_mask = (tapes["entry"] >= start - 360) & (tapes["entry"] < start)
    prep = {key: value[prep_mask] for key, value in tapes.items()}
    prior, first = evidence.lookup(prep["uid"].astype(np.int64) * 2 + (prep["side"] > 0), prep["entry"])
    prep_flags = account_model.stage_flags(evidence, prior, first, book.eval_need(), book._stage_window_ns(), book.stage_min_pf)
    prep_flags["base_ok"] &= prep_flags["r30"] > 1.02 + 1e-9
    prep_flags["main_ok"] &= prep_flags["base_ok"]
    prep_flags["real_ok"] &= prep_flags["main_ok"]
    prep.update(prep_flags)
    prep["admitted"] = prep_flags["real_ok"]
    prep_hours = stage_report(catalog, prep, book.cost_pct, start - 360, result["startS"])
    # Drawdown duration is computed inside each own closed-return tape. The
    # maximum observed episode is reported; no separate wallet is assigned to
    # hypothetical configurations during preparation.
    lengths = np.diff(np.append(evidence.first, len(evidence.exit)))
    own_cumulative = evidence.cs[1:] - np.repeat(evidence.cs[evidence.first], lengths)
    peaks = np.empty(len(own_cumulative))
    for lo, size in zip(evidence.first, lengths):
        peaks[lo:lo + size] = np.maximum.accumulate(np.maximum(own_cumulative[lo:lo + size], 0.0))
    below = own_cumulative < peaks - 1e-12
    before_below = np.r_[False, below[:-1]]
    before_below[evidence.first] = False
    beginning = np.maximum.accumulate(np.where(below & ~before_below, np.arange(len(below)), 0))
    durations = np.where(below | before_below, (evidence.exit - evidence.exit[beginning]) * BAR, 0)
    snapshots = []
    meta_weights = np.asarray([row["mult"] for row in catalog], dtype=np.int32)
    for bar in [start - 360] + list(range(start - 300, start + 361, 60)):
        groups = evidence.ug
        previous, first = evidence.lookup(groups, np.full(len(groups), bar, dtype=np.int32))
        flags = account_model.stage_flags(evidence, previous, first, book.eval_need(), book._stage_window_ns(), book.stage_min_pf)
        flags["base_ok"] &= flags["r30"] > 1.02 + 1e-9
        flags["main_ok"] &= flags["base_ok"]
        flags["real_ok"] &= flags["main_ok"]
        multiplicity = meta_weights[groups // 2]
        observed = previous >= 0
        own_ddt = int(durations[evidence.exit < bar].max(initial=0))
        snapshots.append(dict(t=utc(result["startS"] + bar * BAR), phase="preparation" if bar <= start else "execution",
            maxOwnObservedDdtS=own_ddt,
            qualifiedDirectionConfigs={name: int(multiplicity[flags[key]].sum())
                for name, key in (("Base", "base_ok"), ("Main", "main_ok"), ("Real", "real_ok"))},
            qualifiedSets={name: int(meta_weights[np.unique(groups[flags[key]] // 2)].sum())
                for name, key in (("Base", "base_ok"), ("Main", "main_ok"), ("Real", "real_ok"))},
            requestedDirectionConfigs=sum(c["requestedConfigs"] for c in result["coverage"]) * 2 + derived_diagnostics["requestedDescendantDirectionConfigs"],
            observedDirectionConfigs=int(multiplicity[observed].sum()),
            insufficientBaseSamples=int(multiplicity[observed & ~flags["ok30"]].sum()),
            unobservedDirectionConfigs=sum(c["requestedConfigs"] for c in result["coverage"]) * 2 + derived_diagnostics["requestedDescendantDirectionConfigs"] - int(multiplicity[observed].sum())))
    selected = candidates["admitted"]
    if selected.any() and "laneExits" not in result:
        book.lookback = len(result["bars"])
        book.ingest_bars(result["symbol"], [[(result["startS"] + i * BAR) * 1000, *bar]
                                          for i, bar in enumerate(result["bars"])])
        _, kinds, _ = book.prepare_replay_signals(result["symbol"], now=result["startS"] + (len(result["bars"]) - 1) * BAR)
        result["laneExits"] = getattr(kinds, "exits", {}) or {}
    ids = np.unique(candidates["uid"][selected])
    small_catalog = [materialize_aliases(catalog[int(uid)]) for uid in ids]
    mapping = {int(old): new for new, old in enumerate(ids)}
    small = {name: value[selected] for name, value in candidates.items() if isinstance(value, np.ndarray)}
    small["uid"] = np.asarray([mapping[int(uid)] for uid in small["uid"]], dtype=np.int32)
    own_evidence = {}
    # Keep each admitted prototype's complete own closed tape for live-first
    # stage evaluation. Equivalent Set identities reference these price events
    # independently; actual fills will replace their own windows individually.
    tape_counts = np.bincount(tapes["uid"], minlength=len(catalog))
    tape_starts = np.concatenate(([0], np.cumsum(tape_counts)))
    if np.any(np.diff(tapes["uid"]) < 0):
        raise AssertionError("Independent tape scopes must remain contiguous")
    for old in ids:
        rows_by_side = {}
        lo, hi = int(tape_starts[old]), int(tape_starts[old + 1])
        for side in (1, -1):
            selected_tape = (tapes["side"][lo:hi] == side) & (tapes["exit"][lo:hi] >= 0)
            rows_by_side[side] = sorted([[int(entered), int(exited), float(move)] for entered, exited, move in
                zip(tapes["entry"][lo:hi][selected_tape], tapes["exit"][lo:hi][selected_tape],
                    tapes["evidenceRaw"][lo:hi][selected_tape])], key=lambda row: row[1])
        own_evidence[mapping[int(old)]] = {side: np.asarray(rows, dtype=np.float64).reshape(-1, 3)
                                         for side, rows in rows_by_side.items()}
    kind_of = np.asarray([INDICATION_KINDS.index(row["lane"].split("|")[0]) if "|" in row["lane"]
                          and row["lane"].split("|")[0] in INDICATION_KINDS else -1 for row in catalog], dtype=np.int16)
    candidate_kinds = kind_of[candidates["uid"]]
    weights = meta_weights[candidates["uid"]]
    by_kind = {}
    for kind_index, kind in enumerate(INDICATION_KINDS):
        belongs = (candidate_kinds == kind_index) & (candidates["exit"] >= 0)
        by_kind[kind] = {name: canonical_stats(candidates["evidenceRaw"][belongs & mask], book.cost_pct, weights[belongs & mask])
                        for name, mask in (("preBase", np.ones(len(belongs), bool)),
                                           ("Base", candidates["base_ok"]), ("Real", candidates["real_ok"]))}
    family = np.asarray(["axis:" + str(row.get("axisKey", "unknown")) if row["kind"] == "axis"
        else "block:" + str(row.get("axisKey", "unknown")) if row["kind"] == "block"
        else "dca:" + str(row.get("relativeCount", "unknown")) if row["kind"] == "dca"
        else "trailing" if row["kind"] == "trail" else "normal" for row in catalog], dtype=object)
    candidate_family = family[candidates["uid"]]
    by_strategy = {}
    for name in np.unique(family):
        belongs = (candidate_family == name) & (candidates["exit"] >= 0)
        by_strategy[str(name)] = {stage: canonical_stats(candidates["evidenceRaw"][belongs & mask], book.cost_pct, weights[belongs & mask])
            for stage, mask in (("preBase", np.ones(len(belongs), bool)), ("Base", candidates["base_ok"]),
                                ("Main", candidates["main_ok"]), ("Real", candidates["real_ok"]))}
    parent_evidence = {parent_id: {side: np.asarray(rows, dtype=np.float64).reshape(-1, 3)
                      for side, rows in sides.items()} for parent_id, sides in result.get("parentOwnEvidence", {}).items()}
    summary = dict(symbol=result["symbol"], bars=result["bars"], funding=result["funding"], startS=result["startS"], laneExits=result.get("laneExits", {}), parentOwnEvidence=parent_evidence,
                coverage=result["coverage"], catalog=small_catalog, candidates=small, stageHourly=hours,
                stageSnapshots=snapshots, preparationHourly=prep_hours, byKind=by_kind, byStrategy=by_strategy,
                ownEvidence=own_evidence,
                derivedCoverage=derived_diagnostics,
                hypotheticalCloses=int((tapes["exit"] >= 0).sum()), replaySeconds=result["seconds"])
    temporary = summary_path.with_suffix(".tmp")
    temporary.write_bytes(pickle.dumps(dict(signature=summary_signature,result=summary),protocol=pickle.HIGHEST_PROTOCOL))
    temporary.replace(summary_path)
    del catalog, tapes, candidates, evidence
    release_working_memory()
    return summary


def combine_summaries(summaries, cost):
    catalog, columns = [], defaultdict(list)
    for symbol_index, summary in enumerate(summaries):
        offset = len(catalog)
        catalog.extend(summary["catalog"])
        for name, values in summary["candidates"].items():
            columns[name].append(values + offset if name == "uid" else values)
        columns["sym"][-1] = np.full(len(summary["candidates"]["uid"]), symbol_index, dtype=np.int16)
    candidates = {key: np.concatenate(parts) for key, parts in columns.items()}
    hours = []
    for hour in range(6):
        row = dict(hour=hour + 1, startUtc=summaries[0]["stageHourly"][hour]["startUtc"], stages={})
        for stage in ("preBase", "Base", "Main", "Real", "admitted"):
            values = [summary["stageHourly"][hour]["stages"][stage] for summary in summaries]
            n = sum(value["n"] for value in values)
            summed = sum(value.get("netSumPctUnits", 0) for value in values)
            row["stages"][stage] = dict(n=n, pf=round(net_return_pf(summed / n / 100, cost), 6) if n else None,
                netAvgPct=summed / n if n else None, netSumPctUnits=summed,
                wins=sum(v["wins"] for v in values), losses=sum(v["losses"] for v in values),
                entryPositions=sum(v["entryPositions"] for v in values),
                distinctExitBehaviours=sum(v["distinctExitBehaviours"] for v in values))
        hours.append(row)
    return catalog, candidates, hours


def execute_account(results, catalog, candidates, book, overlay, contracts, equity, gated=True):
    """Public compatibility entry point for the corrected causal account."""
    from six_hour_account import execute_account as corrected_execute
    return corrected_execute(results, catalog, candidates, book, overlay, contracts, equity, gated=gated)


def stage_report(catalog, candidates, cost, start, start_s):
    weights = np.asarray([catalog[int(u)]["mult"] for u in candidates["uid"]], dtype=np.int32)
    stages = {"preBase": np.ones(len(weights), dtype=bool), "Base": candidates["base_ok"],
              "Main": candidates["main_ok"], "Real": candidates["real_ok"],
              "admitted": candidates["admitted"]}
    result = []
    for hour in range(6):
        closed_hour = (candidates["exit"] >= start + hour * 60) & (candidates["exit"] < start + (hour + 1) * 60)
        entry_hour = (candidates["entry"] >= start + hour * 60) & (candidates["entry"] < start + (hour + 1) * 60)
        row = dict(hour=hour + 1, startUtc=utc(start_s + (start + hour * 60) * BAR), stages={})
        for name, admitted in stages.items():
            selected = closed_hour & admitted
            stats = canonical_stats(candidates["evidenceRaw"][selected], cost, weights[selected])
            stats["entryPositions"] = int(weights[entry_hour & admitted].sum())
            stats["distinctExitBehaviours"] = int(len(np.unique(candidates["uid"][selected])))
            row["stages"][name] = stats
        result.append(row)
    return result


def render_report(report, path):
    try:
        from render_six_hour_audit import render_report as detailed_render
    except ImportError:
        detailed_render = None
    if detailed_render:
        return detailed_render(report, path)
    def table(headers, rows):
        return "<div class='scroll'><table><thead><tr>" + "".join("<th>" + html.escape(h) + "</th>" for h in headers) + "</tr></thead><tbody>" + "".join("<tr>" + "".join("<td>" + html.escape(str(v)) + "</td>" for v in row) + "</tr>" for row in rows) + "</tbody></table></div>"
    rows = [[h["hour"], h["balanceEnd"], h["equityEnd"], h["pnl"], h["closed"]["pf"],
             h["ddMaxPct"], h["positionsOpened"], h["positionsClosed"], h["openPositions"]]
            for h in report["account"]["hourly"]]
    stage_rows = [[h["hour"], name, s["n"], s["pf"], s["netAvgPct"], s["entryPositions"]]
                  for h in report["stageHourly"] for name, s in h["stages"].items()]
    document = """<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>CTS-G six-hour audit</title><style>body{font:15px/1.5 system-ui;background:#f8f9fc;color:#182238;margin:0}main{max-width:1400px;margin:auto;padding:24px}h1{font-size:28px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;margin:16px 0;background:white}th,td{padding:8px;border-bottom:1px solid #dce2ed;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:16px}.note{border-left:4px solid #3563bb;padding:12px;background:#edf3ff}</style><main><h1>CTS-G · six-hour execution audit</h1>"""
    document += "<p>" + html.escape(report["preparation"]["startUtc"] + " → " + report["preparation"]["endUtc"] + " preparation; " + report["execution"]["startUtc"] + " → " + report["execution"]["endUtc"] + " execution (UTC).") + "</p>"
    document += "<p class='note'>Real public market candles; simulated orders and balance. Return PF uses already cost-net return / declared PositionCost. Configurations, fees and thresholds were frozen before evaluation. Empty evidence remains empty.</p>"
    document += table(["Hour", "Balance", "Equity", "PnL", "Return PF", "DD %", "Opened", "Closed", "Open end"], rows)
    document += "<h2>Independent stages</h2>" + table(["Hour", "Stage", "Closed positions", "Return PF", "Net average %", "Entry cohort"], stage_rows)
    document += "<h2>Coverage and accounting</h2><pre>" + html.escape(json.dumps({"coverage": report["coverage"], "totals": report["account"]["totals"], "assumptions": report["assumptions"]}, indent=2)) + "</pre></main></html>"
    Path(path).write_text(document)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--start-equity", type=float, default=1000)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--cache-dir", default="")
    parser.add_argument("--replay-only", action="store_true")
    parser.add_argument("--score-only", action="store_true", help="Use completed immutable raw price replay caches")
    parser.add_argument("--profile", choices=list(PROFILES), default="baseline")
    parser.add_argument("--account-only", action="store_true", help="Rebuild account/report from scored checkpoint")
    args = parser.parse_args()
    data, output = Path(args.data_dir), Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((data / "manifest.json").read_text())
    allowed = {row["symbol"] for row in manifest["symbols"] if row["complete"]}
    requested = args.symbols.split(",") if args.symbols else manifest["requestedSymbols"]
    symbols = [symbol for symbol in requested if symbol in allowed]
    if not symbols:
        raise ValueError("No complete requested public candle files")
    overlay_path = data / "overlay.json"
    overlay = json.loads(overlay_path.read_text())
    book = se.SetBook()
    execution_overlay = profile_overlay(overlay, args.profile)
    book.load(execution_overlay)
    contracts, contract_meta = account_model.load_contracts(str(data / "contracts.json"), symbols, fetch=False)
    missing = [s for s in symbols if s not in contracts]
    if missing:
        raise ValueError("Public contract specifications missing for: " + ", ".join(missing))
    signature = model_signature()
    cache_dir = Path(args.cache_dir) if args.cache_dir else output / "cache"
    jobs = [dict(symbol=symbol, data=str(data), overlay=str(overlay_path), cache=str(cache_dir),
                 end=manifest["end"], model=signature) for symbol in symbols]
    completed = []
    if not args.score_only and not args.account_only:
        with ProcessPoolExecutor(max_workers=max(1, min(args.workers, 4))) as pool:
            for future in as_completed([pool.submit(replay_worker, job) for job in jobs]):
                result = future.result()
                completed.append(result)
                atomic_json(output / "progress.json", dict(phase="replay", completedSymbols=len(completed),
                    requestedSymbols=len(symbols), results=completed, modelSignature=signature))
                print(json.dumps(result), flush=True)
    elif not args.account_only:
        # This mode changes only stage windows. Verify all immutable market
        # and original geometry inputs before using previously captured paths.
        current_templates = {row["set_id"]: row for row in account_model.build_catalog(book)}
        for job in jobs:
            cached = pickle.loads((cache_dir / (job["symbol"] + ".pkl")).read_bytes())
            expected = (sha(data / (job["symbol"] + ".json")), sha(overlay_path))
            if tuple(cached["signature"][1:]) != expected:
                raise ValueError("Raw cache input provenance mismatch: " + job["symbol"])
            for row in cached["catalog"]:
                original = current_templates[row["set_id"]]
                for key in ("kind", "sl", "tp", "trail", "mult", "set_ids"):
                    if row[key] != original[key]:
                        raise ValueError("Raw exit geometry differs: " + job["symbol"] + ":" + key)
            completed.append(dict(symbol=job["symbol"], originalModelSignature=cached["signature"][0],
                                  inputGeometryVerified=True, originalReplaySeconds=cached["seconds"]))
            del cached
    if args.replay_only:
        return
    checkpoint = cache_dir / (args.profile + ".scored.pkl")
    scoring_model = scoring_signature(data, symbols, overlay, args.profile)
    if args.account_only:
        state = pickle.loads(checkpoint.read_bytes())
        if state["profile"] != args.profile or state["symbols"] != symbols or state["overlaySha"] != sha(overlay_path) or state["scoringSignature"] != scoring_model:
            raise ValueError("Scoring checkpoint profile/input provenance differs")
        summaries, completed = state["summaries"], state["rawProvenance"]
    else:
        summaries = []
        with ProcessPoolExecutor(max_workers=max(1, min(args.workers, 3))) as pool:
            futures = [pool.submit(summarize_one_cache, cache_dir / (symbol + ".pkl"), overlay_path, data, args.profile)
                       for symbol in symbols]
            for future in as_completed(futures):
                summary = future.result()
                summaries.append(summary)
                atomic_json(output / "progress.json", dict(phase="score", profile=args.profile,
                    completedSymbols=len(summaries), requestedSymbols=len(symbols),
                    derivedCoverage=[dict(symbol=s["symbol"], **s["derivedCoverage"]) for s in summaries]))
                print(json.dumps(dict(phase="score", profile=args.profile, symbol=summary["symbol"],
                                      admittedEntries=len(summary["candidates"]["uid"]), derived=summary["derivedCoverage"])), flush=True)
        temporary = checkpoint.with_suffix(".tmp")
        temporary.write_bytes(pickle.dumps(dict(profile=args.profile,symbols=symbols,overlaySha=sha(overlay_path),
            scoringSignature=scoring_model,summaries=summaries,rawProvenance=completed),protocol=pickle.HIGHEST_PROTOCOL))
        temporary.replace(checkpoint)
    summaries.sort(key=lambda summary: symbols.index(summary["symbol"]))
    catalog, candidates, stage_hourly = combine_summaries(summaries, book.cost_pct)
    try:
        from six_hour_account import execute_account as corrected_execute
    except ImportError:
        raise RuntimeError("Corrected account module is required for a final six-hour audit")
    atomic_json(output / "progress.json", dict(phase="account", profile=args.profile, completedSymbols=len(summaries),
        requestedSymbols=len(symbols), admittedBehaviours=len(catalog), eligibleEntryEvents=len(candidates["uid"])))
    account = corrected_execute(summaries, catalog, candidates, book, execution_overlay, contracts, args.start_equity)
    report = dict(generatedAt=utc(time.time()), modelSignature=signature, scoringSignature=scoring_model, sourceManifest=manifest,
        accountModelSha256=sha(ROOT / "scripts/six_hour_account.py"), admissionCheckpointSha256=sha(checkpoint),
        preparation=dict(startUtc=utc(manifest["preparationStart"] / 1000), endUtc=utc(manifest["executionStart"] / 1000), minutes=360),
        execution=dict(startUtc=utc(manifest["executionStart"] / 1000), endUtc=utc(manifest["end"] / 1000), minutes=360),
        symbols=symbols, skippedSymbols=[s for s in requested if s not in symbols],
        startEquity=args.start_equity, costPct=book.cost_pct, costSource="frozen system overlay, not measured account fees",
        profile=args.profile, profileChanges=PROFILES[args.profile],
        profileProvenance="Admission-only diagnostic frozen before execution results; five samples have limited confidence." if args.profile != "baseline" else "Unchanged frozen production thresholds.",
        rawReplayProvenance=completed,
        engine=dict(baseWindow=book._stage_window_ns()[0], mainWindow=book._stage_window_ns()[1],
                    realWindow=book._stage_window_ns()[2], requiredSamples=book.eval_need(), minPf=book.min_pf,
                    catalogSets=len(book.by_idx)),
        coverage=dict(symbols=len(symbols), requestedSymbols=len(requested), kinds=list(INDICATION_KINDS),
                      lanes=[dict(symbol=r["symbol"], **c) for r in summaries for c in r["coverage"]],
                      admittedBehaviours=len(catalog), hypotheticalCloses=sum(s["hypotheticalCloses"] for s in summaries),
                      adjustmentsComplete=all(s["derivedCoverage"]["complete"] for s in summaries),
                      descendantCoverage=[dict(symbol=s["symbol"], **s["derivedCoverage"]) for s in summaries],
                      htf=dict(enabled=bool(overlay.get("htfEnabled")), status="insufficient historical evidence",
                          availableHourlyBars=48, requiredHistoryBars=overlay.get("htfHistoryBars", 1000),
                          qualified=0, executions=0)),
        stageHourly=stage_hourly, stageSnapshots=[dict(symbol=s["symbol"], snapshots=s["stageSnapshots"]) for s in summaries],
        preparationBySymbol=[dict(symbol=s["symbol"], hourly=s["preparationHourly"]) for s in summaries],
        byIndication=[dict(symbol=s["symbol"], types=s["byKind"]) for s in summaries],
        byStrategyStage=[dict(symbol=s["symbol"], strategies=s["byStrategy"]) for s in summaries], account=account,
        assumptions=["OHLC replay uses the engine's stop-before-target, previous-trail and time/scratch policy.",
            "Orders, fees, equity and controls are simulated, not exchange confirmations.",
            "The six-hour preparation tape starts flat; older bars warm indicators only and never count as PF evidence.",
            "Every indication and native pack uses its own symbol/configuration/exit/direction evidence.",
            "Identical effective exits share computation; every configured Set identity and its multiplicity are retained.",
            "Stage hourly statistics follow execution-period entry cohorts; preparation entries closing in execution remain in own admission evidence and are excluded from those cohort results.",
            "OHLC data cannot reproduce order-book slippage, partial fills or exact intraminute ordering."])
    atomic_json(output / "six-hour-report.json", report)
    render_report(report, output / "six-hour-report.html")
    atomic_json(output / "progress.json", dict(phase="complete", profile=args.profile,
        completedSymbols=len(summaries), requestedSymbols=len(symbols), totals=account["totals"]))
    print(json.dumps(dict(report=str(output / "six-hour-report.json"), totals=account["totals"])), flush=True)


if __name__ == "__main__":
    main()
