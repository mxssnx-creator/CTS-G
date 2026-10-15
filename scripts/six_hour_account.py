"""Causal six-hour paper account: native exits, shared capital and queued orders.

OHLC execution is deterministic, at candle close for new orders; protection
installed before a candle is checked SL-first by the native transition. Rate
limits are minute capacity bounds, not a claim of measured network latency.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
import math
import time

import numpy as np
import sim_12h_account as model

model._engine_path()
from position_cost import net_return_pf, clears_pf
from risk_variants import parse_trail
from indication_engine import tactic_should_close
from set_engine import hit_exit
from dca_engine import DcaBook
from block_engine import BlockBook

BAR = 60
STRATEGIES = ("normal", "trailing", "axis", "block", "dca")


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def account_stats(rows, cost_pct):
    values = [r["net"] / r["notional"] for r in rows if r["notional"] > 0]
    return dict(n=len(values), pf=round(net_return_pf(sum(values) / len(values), cost_pct), 6) if values else None,
                netAvgPct=sum(values) / len(values) * 100 if values else None,
                wins=sum(v > 0 for v in values), losses=sum(v < 0 for v in values),
                netUsdt=sum(r["net"] for r in rows), grossUsdt=sum(r["gross"] for r in rows))


def own_gate(history, live, minute, book, only_base=False):
    """Own actual fills have priority; replay only fills their missing slots."""
    actual = [r for r in live if r[0] < minute]
    executed_signals = {int(r[2]) for r in actual if len(r) > 2}
    past = [r for r in history if int(r[1]) < minute and int(r[0]) not in executed_signals]
    net_hist = [(int(r[1]), float(r[2]) - book.cost_pct / 100) for r in past]
    windows = book._stage_window_ns()
    need = book.eval_need()
    metrics = {}
    for stage, window in zip(("base", "main", "real"), windows):
        if only_base and stage != "base":
            break
        actual_tail = actual[-window:]
        missing = max(0, window - len(actual_tail))
        sample = (net_hist[-missing:] if missing else []) + [(r[0], r[1]) for r in actual_tail]
        minimum = need if stage == "base" else window
        value = net_return_pf(sum(r[1] for r in sample) / len(sample), book.cost_pct) if sample else None
        metrics[stage] = dict(n=len(sample), pf=value, source="live" if len(actual_tail) >= window else "bootstrap")
        if len(sample) < minimum or value is None or not clears_pf(value, book.stage_min_pf[stage]):
            return False, stage + "OwnEvidence", metrics
    if getattr(book, "live_negative_deact", True) and len(actual) >= book.deact_n:
        if sum(r[1] for r in actual[-book.deact_n:]) < 0:
            return False, "ownLiveNegative", metrics
    # Native DDT uses its configured latest close window (Base by default).
    maximum = float(getattr(book, "max_dd_s", 0) or 0)
    if maximum:
        merged = sorted(net_hist + [(r[0], r[1]) for r in actual])
        ddt_rows = [dict(t=(t+1)*BAR,pnl_pct=value,position_cost_pct=0.0) for t,value in merged]
        metric = book.ddt_when_enough(ddt_rows,need,ordered=True)
        if float(metric["maxS"]) > maximum:
            return False, "ownDrawdownTime", metrics
    return True, "pass", metrics


def axis_gate(history, live, minute, book, axis, count):
    """A deferred Axis recomputes its exact parent's current own window."""
    ok, why, metrics = own_gate(history, live, minute, book, only_base=True)
    if not ok:
        return False, "axisParent" + why
    actual = [r for r in live if r[0] < minute]
    executed = {r[2] for r in actual if len(r) > 2}
    replay = [(r[1], r[2]-book.cost_pct/100) for r in history if r[1] < minute and r[0] not in executed]
    # Live fills have priority in the same window used by the parent's gate.
    window_n = count*2 if axis == "prev" else count
    missing = max(0, window_n-len(actual))
    tape = (replay[-missing:] if missing else []) + [(r[0], r[1]) for r in actual[-window_n:]]
    sample = tape[-2*count:-count] if axis == "prev" and len(tape) >= 2*count else ([] if axis == "prev" else tape[-count:])
    if len(sample) < min(3, count):
        return False, "axisParentWindowInsufficient"
    if axis == "pause" and len(sample) >= count and all(r[1] < 0 for r in sample[-count:]):
        return False, "axisParentPauseNegative"
    value = net_return_pf(sum(r[1] for r in sample)/len(sample), book.cost_pct)
    return (True, "pass") if clears_pf(value, book.stage_min_pf["base"]) else (False, "axisParentWindowPf")


def additional_strategy_gate(history, live, minute, book, overlay, kind):
    """Independent Block/DCA windows veto proven negatives after full samples."""
    if kind not in ("block","dca"):
        return True, "pass"
    actual = [r for r in live if r[0] < minute]
    executed = {r[2] for r in actual if len(r) > 2}
    replay = [(r[1],r[2]-book.cost_pct/100) for r in history if r[1] < minute and r[0] not in executed]
    need = max(5,min(75,int(book.block_eval_pos))) if kind == "block" else max(5,int(overlay.get("dcaPfWindow") or 15))
    missing = max(0,need-len(actual))
    sample = (replay[-missing:] if missing else []) + [(r[0],r[1]) for r in actual[-need:]]
    floor = book.stage_min_pf["real"] if kind == "block" else float(overlay.get("dcaMinPf") or 1.1)
    if len(sample) >= need and not clears_pf(net_return_pf(sum(r[1] for r in sample)/len(sample),book.cost_pct),floor):
        return False, kind+"AdditionalOwnWindowPf"
    if kind == "dca" and overlay.get("dcaAutoDeact",True):
        deact = max(5,int(overlay.get("dcaDeactN") or 25))
        missing = max(0,deact-len(actual))
        sample = (replay[-missing:] if missing else []) + [(r[0],r[1]) for r in actual[-deact:]]
        if len(sample) >= deact and sum(r[1] for r in sample) < 0:
            return False,"dcaAdditionalOwnNegative"
    return True,"pass"


def execute_account(results, catalog, candidates, book, overlay, contracts, equity, gated=True):
    """Execute independent exact identities from immutable public candles."""
    if not results:
        raise ValueError("An account requires at least one complete market tape")
    symbols = [r["symbol"] for r in results]
    by_symbol = {r["symbol"]: r for r in results}
    n, start_s = len(results[0]["bars"]), results[0]["startS"]
    start = n - 360
    if any(len(r["bars"]) != n or r["startS"] != start_s for r in results):
        raise ValueError("Market tapes must have identical closed-candle boundaries")
    sizer = model.Sizer(contracts, overlay, None)
    # Bind the same venue quantization and combined parent-book cap used by
    # live Block/DCA submission, rather than independently summing both lanes.
    sizer.dca = DcaBook(); sizer.dca.load(overlay)
    sizer.block = BlockBook("",overlay)
    for method in ("round_qty","notional_cap","max_book_notional","cap_order_qty"):
        setattr(sizer,method,getattr(sizer.pt.Pulse,method).__get__(sizer))
    sizer.pt.SIZING_MODE = str(overlay.get("sizingMode") or sizer.pt.SIZING_MODE)
    sizer.pt.TARGET_NOTIONAL_PCT = float(overlay.get("targetNotionalPct") or sizer.pt.TARGET_NOTIONAL_PCT)
    account = model.Account(equity, book.cost_pct)
    controls = model.ControlOrders()
    drawdown = model.DrawdownTracker(equity)
    own_history = {}
    parent_history = {}
    offset = 0
    for result in results:
        for uid, sides in result.get("ownEvidence", {}).items():
            own_history[offset + int(uid)] = {int(side): rows for side, rows in sides.items()}
        for parent_id, sides in (result.get("parentOwnEvidence") or result.get("parentEvidence") or {}).items():
            parent_history[parent_id] = {int(side): rows for side, rows in sides.items()}
        for uid, meta in enumerate(result.get("catalog", [])):
            if meta["kind"] not in ("axis", "block", "dca"):
                for original in meta["set_ids"]:
                    parent_history[f"{result['symbol']}|{meta['lane']}|{original}"] = own_history.get(offset+uid, {})
        offset += len(result.get("catalog", []))
    by_bar = defaultdict(list)
    valid = candidates["admitted"] if gated else np.ones(len(candidates["uid"]), dtype=bool)
    for idx in np.flatnonzero(valid):
        by_bar[int(candidates["entry"][idx])].append(int(idx))
    # Each prototype has a fair fresh cursor and sparse alias retries. A failed
    # alias never vetoes siblings, and refreshing a signal never resets progress.
    pending = {}
    live = defaultdict(lambda: deque(maxlen=max(100, book.deact_n, *book._stage_window_ns())))
    occupied = {}
    closed, opened, curve, hourly = [], [], [], []
    orders, rejected = defaultdict(int), defaultdict(int)
    api, strategies_order = defaultdict(int), defaultdict(lambda: defaultdict(int))
    contributions = dict.fromkeys(STRATEGIES, 0.0)
    strategy_peak, strategy_dd = dict(contributions), dict(contributions)
    strategy_hour_start = dict(contributions)
    strategy_hour_funding = dict(contributions)
    strategy_hour_dd = dict(contributions)
    funding_total = prior_fees = prior_funding = prior_gross = 0.0
    funding_by_bar = defaultdict(list)
    for result in results:
        for tick in result.get("funding", []):
            minute = int(round((float(tick["fundingTime"]) / 1000 - start_s) / BAR))
            funding_by_bar[minute].append((result["symbol"], float(tick["fundingRate"]), float(tick.get("markPrice") or 0)))
    sequence, peak_equity = 0, float(equity)
    installed_groups, installed_prices, control_backlog = {}, {}, set()
    max_groups = int(overlay.get("maxOpen") or 100)
    ordinary_limit = max(1, int(float(overlay.get("systemOrderRps") or 4) * BAR))
    batch_limit = max(1, int(float(overlay.get("systemBatchRps") or 2) * BAR))
    batch_size = max(1, int(overlay.get("entryBatchSize") or 5))
    batch_enabled = bool(overlay.get("entryBatchOrders"))
    per_config_controls = bool(overlay.get("controlOrdersPerConfig", True))
    overall_controls = bool(overlay.get("controlOrdersOverall",True))
    halt = float(overlay.get("drawdownHaltPct") or overlay.get("ddPausePct") or 0) / 100
    max_queue_bars = max(1, int(book.hist_time_bars))
    rate_minutes = []

    def strategy(meta):
        kind = meta["kind"]
        return "trailing" if kind == "trail" else kind if kind in ("axis", "block", "dca") else "normal"

    def identity(meta, original, symbol):
        return original if meta["kind"] in ("axis", "block", "dca") else f"{symbol}|{meta['lane']}|{original}"

    def parent_identity(meta, cursor, symbol):
        aliases = meta.get("parentSetIds") or [meta.get("parentTemplateId")]
        original = str(aliases[min(cursor, len(aliases)-1)])
        prefix = f"{symbol}|{meta['lane']}|"
        return original if original.startswith(prefix) else prefix + original

    def contribution_values(prices):
        values = dict(contributions)
        for lot in account.lots.values():
            values[lot["strategy"]] += lot["qty"] * (prices[lot["symbol"]] - lot["entry"]) * lot["side"]
        return values

    def scope_key(symbol,side,set_id,lane):
        group = (symbol,side)
        htf = str(lane).startswith("htf")
        # Native ensure_controls routes Overall before PerConfig. HTF keeps
        # its own protection even when the rest of the account is overall.
        return (*group,set_id) if htf or (per_config_controls and not overall_controls) else group

    def control_scope(lot):
        return scope_key(lot["symbol"],lot["side"],lot.get("controlSetId") or lot["setId"],lot["lane"])

    def physical_control_groups():
        groups = defaultdict(int)
        for lot in account.lots.values():
            groups[control_scope(lot)] += 1
        return dict(groups)

    def desired_control_prices(group):
        members = [lot for lot in account.lots.values() if control_scope(lot) == group]
        if not members:
            return None
        if group[1] > 0:
            return (min(lot["nativePosition"]["sl"] for lot in members),max(lot["nativePosition"]["tp"] for lot in members))
        return (max(lot["nativePosition"]["sl"] for lot in members),min(lot["nativePosition"]["tp"] for lot in members))

    for minute in range(start, n):
        hour = (minute - start) // BAR
        prices = {r["symbol"]: float(r["bars"][minute][3]) for r in results}
        high = {r["symbol"]: float(r["bars"][minute][1]) for r in results}
        low = {r["symbol"]: float(r["bars"][minute][2]) for r in results}
        before = physical_control_groups()
        before_controls = dict(installed_groups)
        group_hits = {}
        for group, (stop, target) in installed_prices.items():
            symbol, side = group[:2]
            why, price = hit_exit(side, prices[symbol], stop, target, None,
                                  by_symbol[symbol]["bars"][minute], ignore_tp=not bool(book.hist_honor_tp))
            if why:
                group_hits[group] = (why, price)
        changed = set()
        ordinary_used = entry_fills = 0
        control_reserve = {}
        closed_bundles = {}

        def reserve(group, will_exist=True):
            nonlocal ordinary_used
            # Membership changes are coalesced once per group / minute.
            want = (4 if before_controls.get(group) else 2) if will_exist else (2 if before_controls.get(group) else 0)
            old = control_reserve.get(group, 0)
            extra = max(0, want - old)
            if ordinary_used + extra > ordinary_limit:
                return False
            ordinary_used += extra
            control_reserve[group] = max(old, want)
            changed.add(group)
            return True

        # Outstanding cancel/replacements are serviced before new entries.
        for group in sorted(control_backlog):
            if reserve(group, bool(before.get(group))):
                control_backlog.discard(group)

        # Funding settles at the candle open. A fill at the preceding close
        # occurs at this instant and does not inherit earlier ownership.
        for symbol, rate, mark in funding_by_bar[minute]:
            for lot in account.lots.values():
                if lot["symbol"] == symbol and lot["entryBar"] < minute - 1:
                    payment = -lot["side"] * lot["qty"] * (mark or prices[symbol]) * rate
                    account.cash += payment
                    lot["fundingUsdt"] += payment
                    contributions[lot["strategy"]] += payment
                    strategy_hour_funding[lot["strategy"]] += payment
                    funding_total += payment
        # Only positions that existed before this candle encounter its wick.
        adverse_prices = {}
        for lot_id, lot in account.lots.items():
            pos = lot["nativePosition"]
            confirmed = installed_prices.get(control_scope(lot))
            if lot["side"] > 0:
                stop = confirmed[0] if confirmed else 0.0
                adverse_prices[lot_id] = max(low[lot["symbol"]], stop)
            else:
                stop = confirmed[0] if confirmed else float("inf")
                adverse_prices[lot_id] = min(high[lot["symbol"]], stop)
        worst = account.cash + sum(lot["qty"]*(adverse_prices[lot_id]-lot["entry"])*lot["side"] for lot_id,lot in account.lots.items())
        worst_contribution = contribution_values({s: prices[s] for s in symbols})
        for name in STRATEGIES:
            worst_contribution[name] = contributions[name] + sum(
                lot["qty"] * (adverse_prices[lot_id] - lot["entry"]) * lot["side"]
                for lot_id,lot in account.lots.items() if lot["strategy"] == name)
        if (minute - start) % BAR == 0:
            for symbol in symbols:
                bars = by_symbol[symbol]["bars"][max(0, minute - BAR):minute]
                if bars:
                    sizer.vol1h[symbol] = (max(r[1] for r in bars) - min(r[2] for r in bars)) / bars[-1][3] * 100
        # Fresh native transitions replace cached future exits entirely.
        for lot_id, lot in list(account.lots.items()):
            meta = catalog[lot["uid"]]
            bundle_parent = account.lots.get(lot.get("bundleParentLot"))
            if bundle_parent is not None:
                lot["nativePosition"] = bundle_parent["nativePosition"]
            old_pos = dict(lot["nativePosition"])
            arm, give = parse_trail(str(meta.get("trail") or "")) if meta.get("trail") else (0.0, 0.0)
            arm, give = arm / 100.0, give / 100.0
            if bundle_parent is not None:
                pos, event = lot["nativePosition"], None
            else:
                pos, event = book._advance_pos(lot["nativePosition"], by_symbol[lot["symbol"]]["bars"][minute], minute,
                    float(meta["sl"]), float(meta["tp"]), bool(meta.get("trail")), arm, give,
                    10**9, 10**9, bool(book.hist_honor_tp),
                    start_s + minute * BAR, lot["symbol"], lot["setId"], str(meta.get("pack") or ""), "core")
            if event is None and bundle_parent is None:
                held = minute - lot["entryBar"]
                tactic, level = lot.get("tacticExit", ("", 0.0))
                close = prices[lot["symbol"]]
                why = ""
                if book.ind_settings.get("exitTacticOn", True) and held >= 1 and tactic_should_close(
                        tactic, lot["side"], close, level, old_pos["entry"],
                        float(book.ind_settings.get("exitTacticBufferPct", .1)),
                        float(book.ind_settings.get("exitTacticMinGainPct", .15))):
                    why = "tactic-" + tactic
                elif held >= max_queue_bars:
                    why = "time"
                elif held >= max(8, int(book.scratch_s / BAR)) and (close-old_pos["entry"])/old_pos["entry"]*lot["side"] >= book.scratch_min:
                    why = "scratch+"
                if why:
                    event = dict(pnl_pct=(close-old_pos["entry"])/old_pos["entry"]*lot["side"], reason=why)
            bundle_follower = lot.get("bundleParentLot") in closed_bundles
            if bundle_follower:
                bundle_exit, bundle_reason = closed_bundles[lot["bundleParentLot"]]
                event = dict(pnl_pct=(bundle_exit-old_pos["entry"])/old_pos["entry"]*lot["side"], reason=bundle_reason)
            group = control_scope(lot)
            outer = group_hits.get(group)
            # An installed aggregate protection closes every remaining member.
            if event is None and outer is not None:
                event = dict(pnl_pct=(outer[1]-old_pos["entry"])/old_pos["entry"]*lot["side"], reason=outer[0])
            if event is not None:
                existing = physical_control_groups().get(group, 0)
                requested_px = old_pos["entry"] * (1 + float(event["pnl_pct"]) * lot["side"])
                automatic = outer is not None and abs(requested_px-outer[1]) <= max(1e-9,abs(outer[1])*1e-10)
                if not automatic and not bundle_follower and ordinary_used + 1 > ordinary_limit and outer is not None:
                    event = dict(pnl_pct=(outer[1]-old_pos["entry"])/old_pos["entry"]*lot["side"], reason=outer[0])
                    automatic = True
                if not automatic and not bundle_follower and ordinary_used + 1 > ordinary_limit:
                    lot["nativePosition"] = old_pos
                    rejected["closeDeferredRate"] += 1
                    continue
                if not automatic and not bundle_follower:
                    ordinary_used += 1
                if not reserve(group, existing > 1):
                    control_backlog.add(group)
                exit_px = old_pos["entry"] * (1 + float(event["pnl_pct"]) * lot["side"])
                if any(member.get("bundleParentLot") == lot_id for member in account.lots.values()):
                    closed_bundles[lot_id] = (exit_px, event["reason"])
                row = account.close_lot(lot_id, exit_px)
                row.pop("nativePosition", None)
                row["net"] += row.get("fundingUsdt", 0)
                row.update(exitBar=minute, exitUtc=utc(start_s + (minute + 1) * BAR), reason=event["reason"], hour=hour)
                row.update(exitBarStartUtc=utc(start_s+minute*BAR),exitBarEndUtc=row["exitUtc"],
                           exitTiming="within candle; exitUtc is the reporting close boundary" if event["reason"] in ("sl","tp","trail") else "modeled candle-close fill",
                           exitExactTickKnown=False)
                contributions[row["strategy"]] += row["gross"] - row["fee_out"]
                closed.append(row)
                live[(row["symbol"], row["setId"], row["side"])].append((minute, row["net"] / row["notional"], row["signalBar"]))
                occupied.pop((row["symbol"], row["setId"], row["side"]), None)
                sizer.open.pop(lot_id, None)
                orders["close"] += 1
                if automatic:
                    orders["protectiveCloseFills"] += 1
                elif bundle_follower:
                    orders["bundleCloseLegs"] += 1
                else:
                    api["closeRequests"] += 1
                strategies_order[row["strategy"]]["close"] += 1
            elif pos.get("trail") is not None:
                # Pulse.replace_sl tightens member.sl from the native trail.
                # Internal state progresses even if its physical pair update
                # must wait for REST capacity; installed_prices stays causal.
                pos["sl"] = max(pos["sl"],pos["trail"]) if lot["side"] > 0 else min(pos["sl"],pos["trail"])
        for group in physical_control_groups():
            desired = desired_control_prices(group)
            if desired != installed_prices.get(group) and group not in changed:
                if not reserve(group,True):
                    control_backlog.add(group)
                    rejected["protectionDeferredRate"] += 1
        for idx in by_bar[minute]:
            key = (int(candidates["uid"][idx]), int(candidates["side"][idx]))
            if key in pending:
                pending[key][0] = idx
            else:
                pending[key] = [idx, 0, deque(), set(), 0]
        queues = defaultdict(deque)
        for key, state in list(pending.items()):
            idx, cursor = state[:2]
            entered, exited = int(candidates["entry"][idx]), int(candidates["exit"][idx])
            if minute - entered > max_queue_bars or (exited >= 0 and minute >= exited):
                rejected["queuedSignalExpired"] += len(catalog[key[0]]["set_ids"]) - cursor + len(state[3])
                pending.pop(key, None)
                continue
            state[4] = len(state[2])
            meta = catalog[key[0]]
            queues[(int(candidates["sym"][idx]), key[1], meta["lane"])].append(key)
        budget_exhausted = False
        while queues and not budget_exhausted:
            progress = False
            for lane_key, queue in list(queues.items()):
                if not queue:
                    queues.pop(lane_key, None)
                    continue
                key = queue.popleft()
                if key not in pending:
                    continue
                state = pending[key]
                idx = state[0]
                if state[4]:
                    cursor = state[2].popleft()
                    state[3].discard(cursor)
                    state[4] -= 1
                else:
                    cursor = state[1]
                    state[1] += 1
                uid, side = key
                meta, si = catalog[uid], int(candidates["sym"][idx])
                symbol = symbols[si]
                original = meta["set_ids"][cursor]
                set_id = identity(meta, original, symbol)
                own_key = (symbol, set_id, side)
                advance = True
                rejection = None
                if own_key in occupied:
                    rejection = "occupiedExactConfiguration"
                else:
                    ok, why, metrics = own_gate(own_history.get(uid, {}).get(side, []), live[own_key], minute, book) if gated else (True, "pass", {})
                    if not ok:
                        rejection, advance = why, False
                    if gated and not rejection and meta["kind"] in ("axis","block","dca"):
                        parent_id = parent_identity(meta, cursor, symbol)
                        source = parent_history.get(parent_id) or parent_history.get(meta.get("parentId"))
                        if source is None:
                            rejection, advance = "descendantParentEvidenceMissing", False
                        else:
                            parent_ok, why, _ = own_gate(source.get(side,[]),live[(symbol,parent_id,side)],minute,book)
                            if not parent_ok:
                                rejection, advance = "descendantParent"+why,False
                            elif meta["kind"] == "axis":
                                axis = str(meta.get("axisKey") or "").split(":")[0]
                                axis_ok, why = axis_gate(source.get(side, []), live[(symbol,parent_id,side)], minute,
                                                         book, axis, max(1,int(meta.get("relativeCount") or 1)))
                                if not axis_ok:
                                    rejection, advance = why, False
                    if gated and not rejection:
                        extra_ok, why = additional_strategy_gate(own_history.get(uid,{}).get(side,[]),live[own_key],minute,book,overlay,meta["kind"])
                        if not extra_ok:
                            rejection,advance = why,False
                    effective_group = (symbol, side)
                    if not rejection and effective_group not in account.groups() and len(account.groups()) >= max_groups:
                        rejection, advance = "effectiveGroupCap", False
                    current = account.equity(prices)
                    peak_equity = max(peak_equity, current)
                    if not rejection and (current < float(sizer.pt.EQ_MIN) or (halt and current < peak_equity * (1 - halt))):
                        rejection, advance = "equityHalt", False
                    parent = None
                    qty_ratio = 1.0
                    is_add = meta["kind"] == "dca" or str(meta.get("axisKey") or "").startswith("block-add:")
                    if not rejection and is_add:
                        parent_id = parent_identity(meta, cursor, symbol)
                        parent_lot = occupied.get((symbol, parent_id, side))
                        parent = account.lots.get(parent_lot)
                        if parent is None:
                            rejection, advance = "actualParentNotOpen", False
                        elif "parentEntry" in candidates and parent["signalBar"] != int(candidates["parentEntry"][idx]):
                            rejection, advance = "differentActualParentEntry", False
                        elif parent["entryBar"] >= minute:
                            rejection, advance = "parentFillNotEarlier", False
                        else:
                            count = max(1, int(meta.get("relativeCount") or 1))
                            basis = parent["nativePosition"]["entry"]
                            movement = (prices[symbol]-basis)/basis*side
                            if meta["kind"] == "dca":
                                prior_steps = int(parent.get("actualDcaSteps", 0))
                                distances = list(book.dca_dist)
                                if count != prior_steps + 1 or count > len(distances):
                                    rejection, advance = "actualDcaRungNotNext", False
                                elif -movement < distances[count-1] or -movement > max(float(meta["sl"])*1.5, .012)+1e-12:
                                    rejection, advance = "actualDcaTriggerNotMet", False
                                elif (minute-parent.get("lastActualAddBar", parent["entryBar"])) * BAR < float(book.dca_cooldown_s):
                                    rejection, advance = "actualDcaCooldown", False
                                else:
                                    qty_ratio = float(book.dca_mult[count-1])
                            else:
                                old_count = int(parent.get("actualBlockCount", 0))
                                increment = book._block_specified_ratio() if getattr(book,"block_active",True) else book.block_vr
                                target = min(max(0, float(book.block_max_multiplier)-1), increment*count)
                                qty_ratio = max(0, target-float(parent.get("actualBlockRatio", 0)))
                                threshold = .002*count if getattr(book, "block_active", True) else .002
                                if count <= old_count or qty_ratio <= 1e-12:
                                    rejection = "actualBlockRungAlreadyReached"
                                elif movement < threshold:
                                    rejection, advance = "actualBlockTriggerNotMet", False
                    if not rejection:
                        sizer.available, sizer.equity, sizer.wallet_equity = max(0, current - account.used_margin), current, current
                        sizer.foreign_upnl = sizer.foreign_realized = 0.0
                        sizer.last_bal = time.time()
                        contract, price = contracts[symbol], prices[symbol]
                        lev = sizer.leverage_for(contract)
                        ratio = float(meta.get("volumeRatio") or 1)
                        quantity = sizer.size_qty(contract, price, ratio=ratio)
                        if is_add:
                            seed = parent["unit_qty"]
                            members = [parent]+[lot for lot in account.lots.values() if lot.get("bundleParentLot") == parent["id"]]
                            bundle_qty = sum(lot["qty"] for lot in members)
                            book_cap = sizer.max_book_notional(parent_notional=seed*price,c=contract)
                            room = max(0,book_cap-bundle_qty*price)
                            floor = sizer.min_order_qty(contract,price)
                            if meta["kind"] == "dca":
                                want = min(seed*qty_ratio,seed*2.5)
                                cap = min(max(sizer.notional_cap(),seed*price)*max(1,min(2.5,qty_ratio)),room)
                                quantity = sizer.cap_order_qty(contract,price,want,cap)
                                if quantity < floor:
                                    quantity = floor if floor*price <= cap*1.08 and floor <= seed*2.5 else 0
                                if quantity > seed*2.55 or (bundle_qty+quantity)*price > book_cap*1.02:
                                    quantity = 0
                            else:
                                extra_room = seed*max(0,float(book.block_max_multiplier)-1)-seed*float(parent.get("actualBlockRatio",0))
                                want = seed*qty_ratio
                                if want < floor and floor <= extra_room:
                                    want = floor
                                cap = min(max(sizer.notional_cap(),seed*price)*max(1,qty_ratio),room,extra_room*price)
                                quantity = sizer.cap_order_qty(contract,price,want,cap)
                                quantity = min(quantity,sizer.round_qty(contract,extra_room))
                                if quantity < floor or quantity > extra_room+1e-12 or (bundle_qty+quantity)*price > book_cap*1.05:
                                    quantity = 0
                        if quantity <= 0 or quantity * price / lev > sizer.margin_headroom() * .95:
                            rejection, advance = "minimumQuantityOrMargin", False
                    if not rejection:
                        control_id = parent.get("controlSetId",parent["setId"]) if is_add else set_id
                        group = scope_key(symbol,side,control_id,meta["lane"])
                        request_count = math.ceil((entry_fills + 1) / batch_size) if batch_enabled else entry_fills + 1
                        control_cost = 4 if before_controls.get(group) else 2
                        extra = max(0, control_cost - control_reserve.get(group, 0))
                        if (batch_enabled and request_count > batch_limit) or ordinary_used + extra + (0 if batch_enabled else 1) > ordinary_limit:
                            rejection, advance = "entryDeferredRate", False
                            budget_exhausted = True
                        else:
                            reserve(group)
                            if not batch_enabled:
                                ordinary_used += 1
                            sequence += 1
                            pos = book._seed_pos(side, price, 0, 0, minute, str(meta.get("axisKey") or ""))
                            book._rearm_stops(pos, float(meta["sl"]), float(meta["tp"]))
                            name = strategy(meta)
                            row = account.open_lot(sequence, symbol, side, quantity, price, lev,
                                uid=uid, setId=set_id, templateId=original, lane=meta["lane"], strategy=name,
                                controlSetId=control_id,
                                axisKey=meta.get("axisKey"), relativeCount=meta.get("relativeCount"),
                                entryBar=minute, entryUtc=utc(start_s + (minute + 1) * BAR), signalBar=int(candidates["entry"][idx]),
                                entryBarStartUtc=utc(start_s+minute*BAR),
                                signalBarStartUtc=utc(start_s+int(candidates["entry"][idx])*BAR),
                                signalKnownUtc=utc(start_s+(int(candidates["entry"][idx])+1)*BAR),
                                deferredBars=minute-int(candidates["entry"][idx]), hour=hour, unit_qty=quantity,
                                bundleParentLot=parent["id"] if is_add else None,
                                fundingUsdt=0.0, nativePosition=pos,
                                tacticExit=by_symbol[symbol].get("laneExits", {}).get(meta["lane"], {}).get(
                                    int(candidates["entry"][idx]), ("", 0.0)),
                                priorSamples=int(candidates["countBeforeEntry"][idx]),
                                priorBasePf=float(candidates["r30"][idx]), ownStageMetrics=metrics)
                            snapshot = dict(row); snapshot.pop("nativePosition")
                            opened.append(snapshot)
                            occupied[own_key], sizer.open[sequence] = sequence, row
                            if is_add:
                                members = [parent] + [member for member in account.lots.values() if member.get("bundleParentLot") == parent["id"]]
                                average = sum(member["entry"]*member["qty"] for member in members)/sum(member["qty"] for member in members)
                                native = parent["nativePosition"]
                                native["entry"] = average
                                native["i"] = parent["entryBar"]
                                book._rearm_stops(native, float(meta["sl"]), float(meta["tp"]))
                                for member in members:
                                    member["nativePosition"] = native
                                parent["lastActualAddBar"] = minute
                                if meta["kind"] == "dca":
                                    parent["actualDcaSteps"] = int(meta.get("relativeCount") or 1)
                                else:
                                    parent["actualBlockCount"] = int(meta.get("relativeCount") or 1)
                                    parent["actualBlockRatio"] = float(parent.get("actualBlockRatio", 0)) + quantity/parent["unit_qty"]
                            contributions[name] -= row["fee_in"]
                            entry_fills += 1
                            orders["entry"] += 1
                            strategies_order[name]["entry"] += 1
                            progress = True
                if rejection:
                    rejected[rejection] += 1
                if not advance and cursor not in state[3]:
                    state[2].append(cursor)
                    state[3].add(cursor)
                # Every alias is considered at most once this bar. Failed own
                # evidence is retained for a future bar without blocking peers.
                progress = True
                if state[1] >= len(meta["set_ids"]) and not state[3]:
                    pending.pop(key, None)
                elif state[1] < len(meta["set_ids"]) or state[4]:
                    queue.append(key)
                if not queue:
                    queues.pop(lane_key, None)
            if not progress:
                break
        # Exact coalesced controls and REST request costs, with reservations
        # bounded above during processing (some reserved requests remain unused).
        after = physical_control_groups()
        previous = controls.as_dict()
        controls.step(before_controls, after, changed)
        for group in changed:
            if after.get(group):
                installed_groups[group] = after[group]
                installed_prices[group] = desired_control_prices(group)
            else:
                installed_groups.pop(group, None)
                installed_prices.pop(group, None)
        control_delta = {k: controls.as_dict()[k] - previous[k] for k in previous}
        control_requests = control_delta["controlPlace"] + control_delta["controlCancel"] + 2 * control_delta["controlCancelReplace"]
        # Trail-only changes count as group cancel-replacements as well.
        api["controlRequests"] += control_requests
        batch_requests = math.ceil(entry_fills / batch_size) if batch_enabled else 0
        api["entryBatchRequests"] += batch_requests
        api["entryOrdinaryRequests"] += 0 if batch_enabled else entry_fills
        minute_close = ordinary_used - sum(control_reserve.values()) - (0 if batch_enabled else entry_fills)
        actual_ordinary = minute_close + control_requests + (0 if batch_enabled else entry_fills)
        if actual_ordinary > ordinary_limit or batch_requests > batch_limit:
            raise AssertionError("Modeled REST requests exceed their configured minute budget")
        rate_minutes.append(dict(bar=minute, ordinaryRequests=actual_ordinary, batchRequests=batch_requests,
                                 controlRequests=control_requests, closeRequests=minute_close,
                                 entries=entry_fills, ordinaryLimit=ordinary_limit, batchLimit=batch_limit,
                                 pendingPrototypes=len(pending), pendingControlGroups=len(control_backlog)))
        eq = account.equity(prices)
        dd, wick_dd = drawdown.update(minute - start, eq, min(worst, eq))
        values = contribution_values(prices)
        if abs(sum(values.values()) - (eq - equity)) > max(1e-7, abs(eq) * 1e-10):
            raise AssertionError("Strategy contributions do not reconcile to shared account equity")
        for name in STRATEGIES:
            wick_loss = max(0,strategy_peak[name]-worst_contribution[name])
            strategy_peak[name] = max(strategy_peak[name],values[name])
            close_loss = max(0,strategy_peak[name]-values[name])
            strategy_dd[name] = max(strategy_dd[name],wick_loss,close_loss)
            strategy_hour_dd[name] = max(strategy_hour_dd[name],wick_loss,close_loss)
        curve.append(dict(t=utc(start_s + (minute + 1) * BAR), balance=account.cash, equity=eq,
                          margin=account.used_margin, openPositions=len(account.lots), openGroups=len(account.groups()),
                          openControlGroups=len(after),
                          ddPct=dd, worstDdPct=wick_dd, strategyContributions=dict(values)))
        if (minute - start) % BAR == BAR - 1:
            rows = [r for r in closed if r["hour"] == hour]
            entries = [r for r in opened if r["hour"] == hour]
            hour_orders = rate_minutes[-BAR:]
            hourly.append(dict(hour=hour + 1, startUtc=utc(start_s + (start + hour * BAR) * BAR),
                endUtc=utc(start_s + (minute + 1) * BAR), balanceEnd=account.cash, equityEnd=eq,
                pnl=eq - (equity if not hourly else hourly[-1]["equityEnd"]), positionsOpened=len(entries),
                feesUsdt=account.fees-prior_fees, fundingUsdt=funding_total-prior_funding,
                grossRealizedUsdt=account.gross_realized-prior_gross,
                positionsClosed=len(rows), openPositions=len(account.lots), openGroups=len(account.groups()), openControlGroups=len(after),
                closed=account_stats(rows, book.cost_pct), cumulative=account_stats(closed, book.cost_pct),
                ddMaxPct=max(r["ddPct"] for r in curve[-BAR:]), intrabarDdMaxPct=max(r["worstDdPct"] for r in curve[-BAR:]),
                orders=dict(entries=len(entries), closes=len(rows), ordinaryRequests=sum(r["ordinaryRequests"] for r in hour_orders),
                            controlRequests=sum(r["controlRequests"] for r in hour_orders),
                            closeRequests=sum(r["closeRequests"] for r in hour_orders),
                            batchRequests=sum(r["batchRequests"] for r in hour_orders)),
                byStrategy={name: dict(account_stats([r for r in rows if r["strategy"] == name], book.cost_pct),
                    closedNetUsdtToDate=sum(r["net"] for r in closed if r["strategy"] == name),
                    openMarkUsdt=sum(r["qty"]*(prices[r["symbol"]]-r["entry"])*r["side"] for r in account.lots.values() if r["strategy"] == name),
                    openEntryFeesUsdt=sum(r["fee_in"] for r in account.lots.values() if r["strategy"] == name),
                    openFundingUsdt=sum(r["fundingUsdt"] for r in account.lots.values() if r["strategy"] == name),
                    feesUsdtToDate=sum(r["fee_in"]+r.get("fee_out", 0) for r in closed+list(account.lots.values()) if r["strategy"] == name),
                    fundingUsdtToDate=sum(r["fundingUsdt"] for r in closed+list(account.lots.values()) if r["strategy"] == name),
                    feesUsdt=sum(r["fee_in"] for r in entries if r["strategy"] == name)+sum(r["fee_out"] for r in rows if r["strategy"] == name),
                    fundingUsdt=strategy_hour_funding[name],
                    positionsOpened=sum(r["strategy"] == name for r in entries),
                    positionsClosed=sum(r["strategy"] == name for r in rows),
                    openPositions=sum(r["strategy"] == name for r in account.lots.values()),
                    contributionUsdt=values[name], hourContributionUsdt=values[name]-strategy_hour_start[name],
                    cumulativeDdUsdt=strategy_dd[name], sharedCapitalDdPct=strategy_dd[name]/equity*100,
                    hourDdMaxUsdt=strategy_hour_dd[name], hourSharedCapitalDdPct=strategy_hour_dd[name]/equity*100,
                    orders=dict(strategies_order[name])) for name in STRATEGIES}))
            strategy_hour_start = dict(values)
            strategy_hour_funding = dict.fromkeys(STRATEGIES,0.0)
            strategy_hour_dd = dict.fromkeys(STRATEGIES,0.0)
            prior_fees, prior_funding, prior_gross = account.fees, funding_total, account.gross_realized
    orders.update(controls.as_dict())
    orders["total"] = orders["entry"] + orders["close"] + orders["controlPlace"] + orders["controlCancel"] + 2*orders["controlCancelReplace"]
    api["total"] = sum(api.values())
    open_rows = []
    for lot in account.lots.values():
        row = dict(lot); row.pop("nativePosition", None)
        row["unrealizedUsdt"] = lot["qty"] * (prices[lot["symbol"]] - lot["entry"]) * lot["side"]
        open_rows.append(row)
    return dict(hourly=hourly, closedPositions=closed, openedPositions=opened, openPositions=open_rows,
                equityCurve=curve, orderRateMinutes=rate_minutes,
                totals=dict(balanceEnd=account.cash, equityEnd=curve[-1]["equity"], pnl=curve[-1]["equity"]-equity,
                    fees=account.fees, grossRealizedUsdt=account.gross_realized,
                    fundingUsdt=sum(r.get("fundingUsdt", 0) for r in closed + open_rows),
                    maxDdPct=drawdown.max_dd_pct, maxIntrabarDdPct=drawdown.max_dd_intrabar_pct,
                    maxDdtMinutes=drawdown.max_under_min, orders=dict(orders), apiRequests=dict(api), rejected=dict(rejected),
                    pendingPrototypes=len(pending), closed=account_stats(closed, book.cost_pct),
                    pendingControlGroups=len(control_backlog),
                    controlMode="overall" if overall_controls else "per-config" if per_config_controls else "aggregate-symbol-direction",
                    strategyContributions=dict(values), assumptions=[
                        "Paper fills use completed 1m candles; no measured spread, slippage, latency, partial fills or exchange rejection.",
                        "Minute REST capacity bounds use configured ordinary and batch RPS; protective cancel-replace counts both cancel and placement requests. Internal Set exits consume ordinary close calls; only confirmed outer group-protection fills need no close call. Outstanding control updates queue ahead of entries.",
                        "Native Overall protection takes physical precedence over PerConfig for non-HTF symbol/direction exposure: LONG min(member SL), max(member TP); SHORT max(member SL), min(member TP). Internal Set identities and exits remain independent. Without Overall, PerConfig uses each own execution scope; actual Block/DCA bundle children share their parent's scope. maxOpen counts effective symbol/direction groups separately. Venue price clamps and security stop optimization are not reconstructed.",
                        f"Every descendant submission requires current own parent Base/Main/Real proof; Axis adds its count-specific window. Block {book.block_eval_pos} and DCA {int(overlay.get('dcaPfWindow') or 15)} additional windows retain configured veto-on-full policy independently of experimental generic stage windows.",
                        "Cross-margin liquidation is not modeled because public contract files do not expose account tier-specific maintenance margins.",
                        "New fills see subsequent candles only; queued signals expire at their hypothesis close or configured maximum hold.",
                        "Intrabar drawdown marks preexisting lots at adverse candle extremes clipped to their pre-bar confirmed SL/trail; cross-symbol simultaneity remains a conservative OHLC assumption.",
                        "Strategy drawdown is the drawdown of its equity contribution in USDT and percent of shared initial capital."]))
