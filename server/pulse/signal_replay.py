"""Offline-only, independent-context Signals experiment. No exchange client.

Each exact source/range/timeframe/symbol/direction/protection/Last-N branch
owns three separately simulated shadow positions and histories. A fourth,
capital-limited modeled book consumes the Real verdict. This is deliberately
not wired into Pulse live execution, nor a full port of CTS-A-O's fitting,
coordinators, DCA, Active variants or source catalogue.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
import hashlib
import itertools
import json
import math

from signal_sources import SOURCES, RANGES, TIMEFRAMES, onsets, validate_bars, completed_bars, warmup_bars

MINUTE = 60000
HOUR = 3600000
STAGES = ("base", "main", "real")
SIGNAL_MIN_PF = 2.2  # User's Signals-only floor; ordinary strategy thresholds are separate.


def evidence(values):
    gain = math.fsum(x for x in values if x > 0)
    loss = -math.fsum(x for x in values if x < 0)
    state = "empty" if not values else "finite" if loss else "no-loss" if gain else "flat"
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {"n": len(values), "gain": gain, "loss": loss, "pf": gain / loss if loss else None,
            "pfState": state, "net": gain - loss, "maxReturnDrawdown": drawdown}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_config(raw):
    """Strict research schema: misspelled/unsupported controls cannot silently do nothing."""
    required = {"schema", "sources", "ranges", "timeframes", "protections", "lastNLanes", "minPf", "historyMinutes",
                "maxContextDrawdown", "normalEnabled", "trailingEnabled", "fees", "risk", "mode"}
    if set(raw) != required or raw["schema"] != "cts-g-signals-replay-v1" or raw["mode"] != "offline-only":
        raise ValueError("unsupported Signals research schema/keys/mode")
    cfg = json.loads(canonical(raw))
    for key, supported in (("sources", SOURCES), ("ranges", RANGES), ("timeframes", TIMEFRAMES)):
        if not cfg[key] or len(set(cfg[key])) != len(cfg[key]) or any(x not in supported for x in cfg[key]):
            raise ValueError(f"unsupported or repeated {key}")
    for key in ("normalEnabled", "trailingEnabled"):
        if type(cfg[key]) is not bool:
            raise ValueError("strategy switches must be booleans")
    for key in ("minPf", "maxContextDrawdown"):
        if not isinstance(cfg[key], (int, float)) or not math.isfinite(cfg[key]) or cfg[key] <= (1 if key == "minPf" else 0):
            raise ValueError(f"invalid {key}")
    if cfg["minPf"] < SIGNAL_MIN_PF:
        raise ValueError(f"Signals PF floor must remain at least {SIGNAL_MIN_PF}")
    if type(cfg["historyMinutes"]) is not int or not 1 <= cfg["historyMinutes"] <= 4320:
        raise ValueError("invalid historyMinutes")
    if not cfg["lastNLanes"] or len({canonical(x) for x in cfg["lastNLanes"]}) != len(cfg["lastNLanes"]):
        raise ValueError("empty or repeated Last-N branch")
    for lane in cfg["lastNLanes"]:
        if set(lane) != set(STAGES) or any(type(n) is not int or not 1 <= n <= 200 for n in lane.values()):
            raise ValueError("invalid independent Last-N branch")
    fees = cfg["fees"]
    if set(fees) != {"maker", "taker", "slippagePerLeg", "entryLiquidity", "exitLiquidity", "source"}:
        raise ValueError("incomplete cost provenance")
    if fees["source"] != "configured-scenario" or any(fees[k] not in ("maker", "taker") for k in ("entryLiquidity", "exitLiquidity")):
        raise ValueError("only explicitly labeled modeled liquidity is supported")
    if any(type(fees[k]) not in (int, float) or not 0 <= fees[k] < .1 for k in ("maker", "taker", "slippagePerLeg")):
        raise ValueError("invalid fee/slippage fraction")
    risk = cfg["risk"]
    if set(risk) != {"initialEquity", "notionalPerOrder", "maxOpen", "maxGrossExposure", "maxDrawdown", "maxHourlyLoss"}:
        raise ValueError("incomplete modeled capital controls")
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in risk.values()):
        raise ValueError("risk limits must be positive, finite and explicit")
    if type(risk["maxOpen"]) is not int or max(risk["maxDrawdown"], risk["maxHourlyLoss"]) >= 1:
        raise ValueError("invalid drawdown/open limit")
    if not risk["notionalPerOrder"] <= risk["maxGrossExposure"] <= risk["initialEquity"]:
        raise ValueError("research book must be bounded to at most unlevered initial capital")
    if not cfg["protections"]:
        raise ValueError("empty protection grid")
    ids = set()
    floor = fees[fees["entryLiquidity"]] + fees[fees["exitLiquidity"]] + 2 * fees["slippagePerLeg"]
    for p in cfg["protections"]:
        if set(p) != {"id", "kind", "tp", "sl", "trailArm", "trailGap", "holdMinutes"} or p["kind"] not in ("normal", "trailing", "trailing-only"):
            raise ValueError("unsupported protection/strategy type")
        if not isinstance(p["id"], str) or not p["id"] or p["id"] in ids:
            raise ValueError("protection IDs must be unique")
        ids.add(p["id"])
        if any(type(p[k]) not in (int, float) or not math.isfinite(p[k]) or not 0 <= p[k] < 1 for k in ("tp", "sl", "trailArm", "trailGap")):
            raise ValueError("invalid protection fraction")
        if p["sl"] <= floor or type(p["holdMinutes"]) is not int or not 1 <= p["holdMinutes"] <= 4320:
            raise ValueError("stop must exceed modeled costs and hold must be bounded")
        if p["kind"] == "normal" and (p["tp"] <= floor or p["trailArm"] or p["trailGap"]):
            raise ValueError("normal requires TP and no trailing")
        if p["kind"] != "normal" and not floor < p["trailGap"] <= p["trailArm"]:
            raise ValueError("trailing distance must exceed costs")
        if (p["kind"] == "trailing" and p["tp"] <= floor) or (p["kind"] == "trailing-only" and p["tp"] != 0):
            raise ValueError("invalid trailing TP policy")
    return cfg


@dataclass
class Position:
    entry_t: int
    entry: float
    side: int
    stop: float
    best: float
    qty: float = 1.0
    entry_fee: float = 0.0


def open_position(t, price, side, protect, fees, notional=1.0):
    fill = price * (1 + side * fees["slippagePerLeg"])
    qty = notional / fill
    return Position(t, fill, side, fill * (1 - side * protect["sl"]), fill,
                    qty, notional * fees[fees["entryLiquidity"]])


def close_result(position, price, t, reason, fees):
    fill = price * (1 - position.side * fees["slippagePerLeg"])
    gross = position.qty * (fill - position.entry) * position.side
    exit_fee = position.qty * fill * fees[fees["exitLiquidity"]]
    cash = gross - position.entry_fee - exit_fee
    return {"entryT": position.entry_t, "exitT": t, "entry": position.entry, "exit": fill,
            "side": position.side, "qty": position.qty, "entryFee": position.entry_fee, "exitFee": exit_fee,
            "netCash": cash, "unitNet": cash / (position.qty * position.entry), "reason": reason}


def advance_position(position, bar, protect, fees):
    """Prior stop wins ambiguous OHLC; gaps and slippage are adverse, never a guarantee."""
    t, o, h, low, close, _ = bar
    side, stop = position.side, position.stop
    gap_stop = o <= stop if side > 0 else o >= stop
    target = position.entry * (1 + side * protect["tp"]) if protect["tp"] else None
    if gap_stop:
        return close_result(position, o, t + MINUTE, "gap-stop", fees)
    hit_stop = low <= stop if side > 0 else h >= stop
    if hit_stop:
        return close_result(position, stop, t + MINUTE, "stop", fees)
    hit_target = target is not None and (h >= target if side > 0 else low <= target)
    if hit_target:
        return close_result(position, target, t + MINUTE, "target", fees)
    if protect["kind"] != "normal":
        position.best = max(position.best, h) if side > 0 else min(position.best, low)
        if side * (position.best / position.entry - 1) >= protect["trailArm"]:
            tightened = position.best - side * position.entry * protect["trailGap"]
            position.stop = max(stop, tightened) if side > 0 else min(stop, tightened)
            # The close proves a crossing after the favorable extreme. Unknown
            # low/high order alone may not trigger a newly tightened stop.
            if side * (close - position.stop) <= 0:
                return close_result(position, position.stop, t + MINUTE, "trailing-close-confirmed", fees)
    if t + MINUTE - position.entry_t >= protect["holdMinutes"] * MINUTE:
        return close_result(position, close, t + MINUTE, "time", fees)
    return None


@dataclass
class Stage:
    position: Position | None = None
    rows: deque = field(default_factory=deque)
    closes: int = 0
    gain: float = 0.0
    loss: float = 0.0

    def record(self, result):
        self.rows.append(result)
        self.closes += 1
        value = result["unitNet"]
        self.gain += max(0, value)
        self.loss += max(0, -value)

    def cumulative(self):
        return {"n": self.closes, "gain": self.gain, "loss": self.loss,
                "net": self.gain - self.loss, "pf": self.gain / self.loss if self.loss else None,
                "pfState": "finite" if self.loss else "no-loss" if self.gain else "flat" if self.closes else "empty"}

    def figures(self, t, n, history_minutes):
        cutoff = t - history_minutes * MINUTE
        while self.rows and self.rows[0]["exitT"] < cutoff:
            self.rows.popleft()
        values = [r["unitNet"] for r in self.rows if r["exitT"] < t][-n:]
        return evidence(values)


def qualifies(figures, n, cfg):
    pf_ok = figures["pfState"] == "no-loss" or (figures["pf"] is not None and figures["pf"] >= cfg["minPf"])
    return figures["n"] == n and pf_ok and figures["maxReturnDrawdown"] <= cfg["maxContextDrawdown"]


def hourly_audit(marks, trades, start, end):
    """Every whole requested hour, including inactive and losing MTM hours."""
    if start % HOUR or end % HOUR or end <= start:
        raise ValueError("hourly audit requires explicit whole-hour bounds")
    times = {}
    for row in marks:
        if not math.isfinite(row["equity"]) or row["t"] in times:
            raise ValueError("invalid or duplicate equity mark")
        times[row["t"]] = row["equity"]
    hours = []
    for t in range(start, end, HOUR):
        # A candle close at the boundary settles in the preceding bar's hour.
        selected = [r for r in trades if t < r["exitT"] <= t + HOUR]
        net = times[t + HOUR] - times[t] if t in times and t + HOUR in times else None
        metrics = evidence([r["unitNet"] for r in selected])
        state = "unknown" if net is None else "positive" if net > 0 else "negative" if net < 0 else "flat"
        hours.append({"from": t, "to": t + HOUR, "netEquity": net, "state": state,
                      "settledOrders": len(selected), "settledNetCash": math.fsum(r["netCash"] for r in selected),
                      "settledReturns": metrics})
    return {"hours": hours, "counts": dict(Counter(h["state"] for h in hours)),
            "everyHourNetPositive": bool(hours) and all(h["state"] == "positive" for h in hours)}


def replay(data, raw_config, start, trade_start, end, *, freeze_prehistory=False):
    if type(freeze_prehistory) is not bool:
        raise ValueError("freeze_prehistory must be explicit boolean")
    cfg = validate_config(raw_config)
    if not start < trade_start < end or any(t % HOUR for t in (start, trade_start, end)):
        raise ValueError("explicit chronological hour boundaries required")
    if len(data) > 30 or not data:
        raise ValueError("replay requires 1..30 explicit symbols")
    steps = (end - start) // MINUTE
    for symbol, bars in data.items():
        validate_bars(bars)
        if len(bars) != steps or bars[0][0] != start or bars[-1][0] + MINUTE != end:
            raise ValueError(f"incomplete requested history: {symbol}")
    fee, risk = cfg["fees"], cfg["risk"]
    protection_count = sum(p["kind"] == "normal" or cfg["trailingEnabled"] for p in cfg["protections"])
    context_count = len(data)*len(cfg["sources"])*len(cfg["ranges"])*len(cfg["timeframes"])*protection_count*len(cfg["lastNLanes"])*2
    if context_count > 12000:
        raise ValueError("research batch exceeds 12000 exact contexts; split explicitly, do not truncate")
    contexts, groups, coverage = [], {}, []
    for symbol, source, range_name, tf in itertools.product(sorted(data), cfg["sources"], cfg["ranges"], cfg["timeframes"]):
        key = (symbol, source, range_name, tf)
        groups[key] = {"onsets": onsets(data[symbol], source, range_name, tf), "contexts": []}
        completed = completed_bars(data[symbol], tf)
        before_trade = sum(b[0] + tf * MINUTE <= trade_start for b in completed)
        required = warmup_bars(source, range_name)
        events = groups[key]["onsets"]
        coverage.append({"symbol": symbol, "source": source, "range": range_name, "tf": tf,
                         "requiredBars": required, "prehistoryBars": before_trade, "totalBars": len(completed),
                         "sourceWarmAtTradeStart": before_trade >= required, "sourceWarmAtEnd": len(completed) >= required,
                         "prehistoryOnsets": sum(start <= t < trade_start for t in events),
                         "tradingOnsets": sum(trade_start <= t < end for t in events)})
        for p, lane, side in itertools.product(cfg["protections"], cfg["lastNLanes"], (-1, 1)):
            if p["kind"] != "normal" and not cfg["trailingEnabled"]:
                continue
            identity = {"symbol": symbol, "source": source, "range": range_name, "tf": tf, "side": side,
                        "protection": p, "lastN": lane}
            ctx = {"id": hashlib.sha256(canonical(identity).encode()).hexdigest()[:24], **identity,
                   "stages": {stage: Stage() for stage in STAGES}, "decisions": Counter()}
            contexts.append(ctx)
            groups[key]["contexts"].append(ctx)
    if len(contexts) > 12000:
        raise ValueError("research batch exceeds 12000 exact contexts; split explicitly, do not truncate")
    cash, peak = risk["initialEquity"], risk["initialEquity"]
    open_book, trades, marks, admission_marks, risk_counts = {}, [], [], [], Counter()
    hour_start_equity = cash
    last_hour = None
    max_dd = 0.0
    dd_latched = False
    stage_counts = []
    opened = 0
    active_stages = {}
    historical_selection = None
    frozen_ids = None

    def marked(prices, t):
        value = cash
        for item in open_book.values():
            pos = item["position"]
            # Liquidation mark includes adverse exit slippage and estimated exit fee.
            fill = prices[item["context"]["symbol"]] * (1 - pos.side * fee["slippagePerLeg"])
            value += pos.qty * (fill - pos.entry) * pos.side - pos.qty * fill * fee[fee["exitLiquidity"]]
        return {"t": t, "equity": value, "openOrders": len(open_book)}

    def observe_admission(prices, t):
        nonlocal peak, max_dd, dd_latched
        mark = {**marked(prices, t), "openedCount": opened}
        admission_marks.append(mark)
        peak = max(peak, mark["equity"])
        max_dd = max(max_dd, (peak - mark["equity"]) / peak)
        dd_latched |= max_dd >= risk["maxDrawdown"]
        return mark

    for i in range(steps):
        t = start + i * MINUTE
        prices = {s: bars[i][1] for s, bars in data.items()}
        if t == trade_start:
            selected = {s: [] for s in STAGES}
            for ctx in contexts:
                for s in STAGES:
                    n = ctx["lastN"][s]
                    if qualifies(ctx["stages"][s].figures(t,n,cfg["historyMinutes"]),n,cfg):
                        selected[s].append(ctx["id"])
            frozen_ids = set.intersection(*(set(selected[s]) for s in STAGES))
            historical_selection = {"at": t, "strictExitBefore": t, "population": len(contexts),
                                    "stageQualifiedIds": selected, "chainQualifiedIds": sorted(frozen_ids),
                                    "frozenAdmissions": freeze_prehistory}
        if t >= trade_start:
            before = observe_admission(prices, t)
            if not marks:
                marks.append({k:v for k,v in before.items() if k != "openedCount"})
            # Preserve the preceding close mark at hour boundaries; a gap belongs
            # to the new hour, not to an invented cash reset.
            if last_hour != t // HOUR:
                hour_start_equity = marks[-1]["equity"]
                last_hour = t // HOUR
            hour_loss = (hour_start_equity - before["equity"]) / hour_start_equity
        else:
            hour_loss = 0
        candidates = []
        for group in groups.values():
            side = group["onsets"].get(t)
            if not side:
                continue
            for ctx in group["contexts"]:
                if side != ctx["side"]:
                    continue
                p, windows = ctx["protection"], ctx["lastN"]
                figures = {stage: ctx["stages"][stage].figures(t, windows[stage], cfg["historyMinutes"]) for stage in STAGES}
                passed = {stage: qualifies(figures[stage], windows[stage], cfg) for stage in STAGES}
                # Evaluate all gates before this minute's exits: equal-clock or
                # future closes cannot finance a past decision.
                for stage, admitted in (("base", True), ("main", passed["base"]), ("real", passed["base"] and passed["main"])):
                    state = ctx["stages"][stage]
                    if state.position is None and admitted:
                        state.position = open_position(t, prices[ctx["symbol"]], side, p, fee)
                        active_stages[(ctx["id"], stage)] = (ctx, state)
                        ctx["decisions"][stage + "Opened"] += 1
                if t >= trade_start and all(passed.values()):
                    ctx["decisions"]["finalQualified"] += 1
                    if (p["kind"] != "normal" or cfg["normalEnabled"]) and (not freeze_prehistory or ctx["id"] in frozen_ids):
                        candidates.append((figures["real"]["maxReturnDrawdown"], -figures["real"]["net"], ctx["id"], ctx))
        for _, _, key, ctx in sorted(candidates, key=lambda x: x[:3]):
            if key in open_book:
                continue
            # Re-check after each same-clock admission: its spread/fees count now,
            # and existing exposure uses current prices, not original notionals.
            current = observe_admission(prices, t)["equity"]
            hour_loss = (hour_start_equity - current) / hour_start_equity
            exposure = sum(item["position"].qty * prices[item["context"]["symbol"]] for item in open_book.values())
            reason = "drawdown" if dd_latched else "hourly-loss" if hour_loss >= risk["maxHourlyLoss"] else "max-open" if len(open_book) >= risk["maxOpen"] else "exposure" if exposure + risk["notionalPerOrder"] > min(risk["maxGrossExposure"], current) else "cash" if cash <= 0 else ""
            if reason:
                risk_counts[reason] += 1
                continue
            pos = open_position(t, prices[ctx["symbol"]], ctx["side"], ctx["protection"], fee, risk["notionalPerOrder"])
            cash -= pos.entry_fee
            opened += 1
            open_book[key] = {"context": ctx, "position": pos, "entrySequence": opened}
            observe_admission(prices, t)
        # Cold/insufficient contexts need no per-minute position update. Every
        # active stage still has its own state, fill and close stream.
        for active_key, (ctx, state) in list(active_stages.items()):
            result = advance_position(state.position, data[ctx["symbol"]][i], ctx["protection"], fee)
            if result is not None:
                state.record(result)
                state.position = None
                del active_stages[active_key]
        for key, item in list(open_book.items()):
            ctx, pos = item["context"], item["position"]
            result = advance_position(pos, data[ctx["symbol"]][i], ctx["protection"], fee)
            if result is not None:
                cash += result["netCash"] + pos.entry_fee  # entry fee was booked once at open
                trades.append({**result, "context": key, "symbol": ctx["symbol"], "kind": ctx["protection"]["kind"], "entrySequence": item["entrySequence"]})
                del open_book[key]
        if t >= trade_start:
            mark = marked({s: bars[i][4] for s, bars in data.items()}, t + MINUTE)
            marks.append(mark)
            peak = max(peak, mark["equity"])
            max_dd = max(max_dd, (peak - mark["equity"]) / peak)
            dd_latched |= max_dd >= risk["maxDrawdown"]
        if (t + MINUTE) % HOUR == 0:
            # The report has the same strict clock as admission.
            counts = {}
            own_passes = []
            for stage in STAGES:
                vals = [ctx["stages"][stage].figures(t + MINUTE, ctx["lastN"][stage], cfg["historyMinutes"]) for ctx in contexts]
                passing = [(ctx, value) for ctx, value in zip(contexts, vals) if qualifies(value, ctx["lastN"][stage], cfg)]
                own_passes.append({c["id"] for c, _ in passing})
                gain, loss = math.fsum(v["gain"] for _, v in passing), math.fsum(v["loss"] for _, v in passing)
                counts[stage] = {"evaluated": len(contexts), "passed": len(passing),
                                 "pct": 100 * len(passing) / len(contexts) if contexts else None,
                                 "chainPassed": len(set.intersection(*own_passes)),
                                 "windowSamples": sum(v["n"] for _, v in passing),
                                 "pooledWindowPf": gain / loss if loss else None,
                                 "pooledWindowState": "finite" if loss else "no-loss" if gain else "empty",
                                 "insufficient": sum(v["n"] < c["lastN"][stage] for c, v in zip(contexts, vals))}
            stage_counts.append({"t": t + MINUTE, "stages": counts})
    details = []
    for ctx in contexts:
        figures = {stage: ctx["stages"][stage].figures(end, ctx["lastN"][stage], cfg["historyMinutes"]) for stage in STAGES}
        details.append({k: v for k, v in ctx.items() if k != "stages"} | {"stages": {s: {**figures[s], "closes": ctx["stages"][s].closes,
                        "cumulativeReturns": ctx["stages"][s].cumulative(),
                        "qualified": qualifies(figures[s], ctx["lastN"][s], cfg), "open": ctx["stages"][s].position is not None} for s in STAGES}})
    return {"schema": "cts-g-signals-replay-result-v1", "scope": "offline experimental Signals; no live/exchange acceptance",
            "config": cfg, "configSha256": hashlib.sha256(canonical(cfg).encode()).hexdigest(),
            "window": {"start": start, "tradeStart": trade_start, "end": end}, "contexts": details,
            "sourceCoverage": coverage, "stageProgress": stage_counts, "modeledExecution": evidence([r["unitNet"] for r in trades]),
            "prehistorySelection": historical_selection,
            "modelCounts": {"ordersOpened": opened, "ordersSettled": len(trades), "ordersOpen": len(open_book),
                            "positionsOpened": opened, "positionsSettled": len(trades), "positionsOpen": len(open_book)},
            "initialEquity": risk["initialEquity"], "finalEquity": marks[-1]["equity"], "maxEquityDrawdown": max_dd,
            "drawdownLatched": dd_latched, "riskRejections": dict(risk_counts), "trades": trades,
            "marks": marks, "admissionMarks": admission_marks, "hourly": hourly_audit(marks, trades, trade_start, end),
            "openPositions": [{"context": key, "symbol": item["context"]["symbol"], "entryT": item["position"].entry_t,
                               "entry": item["position"].entry, "qty": item["position"].qty, "side": item["position"].side,
                               "entryFee": item["position"].entry_fee, "entrySequence": item["entrySequence"]} for key, item in open_book.items()],
            "limitations": ["nine supported source families, no full CTS-A-O catalogue", "fixed native-distance protection grid, not CTS-A-O lane scaling or MFE fitting",
                            "no DCA, Active, coordination or venue adapter", "OHLC stop-first and modeled liquidity, not fill proof",
                            "no funding, liquidation, margin reservation or venue netting model", "minute close/open drawdown marks do not bound intraminute drawdown",
                            "drawdown limits pause new admissions, not guarantee a loss bound"]}
