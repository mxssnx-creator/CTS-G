#!/usr/bin/env python3
"""Compare completed x02 demo fills with native one-minute Set exit replay."""
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server/pulse"))
from set_engine import BAR_S, SetBook, SetLane, tp_range_of
from x02_native_session import CONN, HOST, atomic_json


def figures(rows):
    actual = [r["live"] for r in rows]
    matched = [r for r in rows if r["sim"] is not None]

    def metrics(values):
        gains = sum(v for v in values if v > 0)
        losses = -sum(v for v in values if v < 0)
        return {"count": len(values), "wins": sum(v > 0 for v in values),
                "losses": sum(v < 0 for v in values), "netReturnPct": 100 * sum(values),
                "pf": gains / losses if losses else None, "noLoss": bool(gains and not losses)}

    return {"live": metrics(actual), "matchedLive": metrics([r["matchedLive"] for r in matched]),
            "sim": metrics([r["sim"] for r in matched]),
            "meanGapPct": 100 * (sum(r["matchedLive"] - r["sim"] for r in matched) / len(matched)) if matched else None,
            "cashVST": sum(r["cash"] for r in rows),
            "entrySlippagePct": 100 * sum(r["entrySlip"] for r in rows) / len(rows) if rows else None,
            "exitSlippagePct": 100 * sum(r["exitSlip"] for r in rows) / len(rows) if rows else None}


def profitable(metrics, minimum=8):
    # Require gain and loss observations so a zero-loss, tiny window cannot
    # masquerade as a measured PF.
    return all(row["count"] >= minimum and row["pf"] is not None and row["pf"] > 1
               and row["netReturnPct"] > 0 for row in (metrics["matchedLive"], metrics["sim"]))


def main(run):
    session = json.loads((run / "session.json").read_text())
    if session["host"] != HOST or session["connection"] != CONN or not session.get("activeEndedAt"):
        raise ValueError("expected a finished BingX x02 session")
    start, end = session.get("activeStartedAt"), session["activeEndedAt"]
    market = json.loads((run / "final-candles.json").read_text())
    stats = json.loads((run / "final-stats.json").read_text())
    cost = float(stats["costModel"]["paperCostPct"])
    overlay = session["overlay"]
    book = SetBook()
    book.load({**overlay, "positionCostPct": cost})
    config = {"entry_conf": book.entry_conf, "time_bars": book.hist_time_bars,
              "scratch_bars": book.scratch_bars, "scratch_min": book.scratch_min,
              "cooldown": book.cooldown_bars, "cost_pct": cost, "honor_tp": book.hist_honor_tp,
              "min_sl_distance_pct": book.min_sl_distance_pct, "trail_armed_free": book.trail_armed_free}
    rows, missed = [], Counter()
    trades = run / "state" / "trades-bingx-x02.jsonl"
    for line in trades.read_text().splitlines() if trades.exists() else ():
        trade = json.loads(line)
        reason = str(trade.get("reason", ""))
        close_at = float(trade["t"])
        if (start is None or not start <= close_at <= end or not trade.get("ours")
                or reason in {"bounded-session-cleanup", "external-position-close", "bad-fill"}
                or "unverified" in reason or "downtime" in reason):
            missed["non-strategy-close"] += 1
            continue
        st = book.sets.get(trade.get("set_id"))
        live = trade.get("net_pnl_pct")
        if st is None or not isinstance(live, (int, float)) or not math.isfinite(live):
            missed["missing-set-or-verified-return"] += 1
            continue
        step = book.step_of(st)
        matched_live = trade.get("base_net_pnl_pct") if trade.get("block_fills") else live
        row = {"kind": st.kind, "range": tp_range_of(step), "step": step, "live": live, "sim": None,
               "matchedLive": matched_live,
               "cash": float(trade["pnl"]), "half": 0 if close_at < (start + end) / 2 else 1,
               "entrySlip": float(trade.get("entry_slippage", 0) or 0),
               "exitSlip": float(trade.get("exit_slippage", 0) or 0)}
        rows.append(row)
        if not isinstance(matched_live, (int, float)) or not math.isfinite(matched_live):
            missed["missing-parent-return"] += 1
            continue
        snapshot = market["symbols"].get(trade["symbol"])
        if not snapshot or not snapshot.get("lastClosedBarAt") or not snapshot["bars"]:
            missed["missing-symbol-candles"] += 1
            continue
        bars = snapshot["bars"]
        last_open = float(snapshot["lastClosedBarAt"])
        entry_at = close_at - float(trade.get("hold_s") or 0)
        first_open = last_open - (len(bars) - 1) * BAR_S
        # An entry decision can use only the preceding completed candle.
        index = math.floor((entry_at - first_open) / BAR_S) - 1
        cutoff = min(len(bars), math.floor((end - first_open) / BAR_S))
        if not 0 <= index < cutoff:
            missed["outside-candle-capture"] += 1
            continue
        direction = 1 if trade["side"] == "LONG" else -1
        lane = SetLane(st, trade["symbol"])
        lane.step(index, bars[index], direction, 1.0, True, config)
        for i in range(index + 1, cutoff):
            closed = lane.step(i, bars[i], 0, 0.0, False, config)
            if closed:
                row["sim"] = closed["pnl"]
                break
        else:
            missed["replay-still-open-at-cutoff"] += 1

    cohorts = defaultdict(list)
    per_step = defaultdict(list)
    for row in rows:
        cohorts[(row["kind"], row["range"])].append(row)
        per_step[(row["kind"], row["range"], row["step"])].append(row)

    def halves(group):
        return [figures([r for r in group if r["half"] == half]) for half in range(2)]

    ranges = []
    for (kind, name), group in sorted(cohorts.items()):
        periods = halves(group)
        supported_steps = [step for (k, n, step), part in sorted(per_step.items())
                           if (k, n) == (kind, name) and all(profitable(m) for m in halves(part))]
        ranges.append({"kind": kind, "range": name, "all": figures(group), "halves": periods,
                       "qualified": all(profitable(m) for m in periods), "supportedSteps": supported_steps})
    complete = bool(session["complete"] and session["cleanup"]["confirmedFlat"]
                    and session["observationSeconds"] >= 10800)
    candidates = [r for r in ranges if r["qualified"] and r["supportedSteps"]]
    changes = {}
    if complete and candidates:
        for kind, switch in (("base", "tpRanges"), ("trail", "trailRanges")):
            groups = [r for r in candidates if r["kind"] == kind]
            changes[switch] = {r: r != "wide" and any(g["range"] == r for g in groups)
                               for r in overlay[switch]}
            if groups:
                step = min(min(r["supportedSteps"]) for r in groups)
                if kind == "base":
                    changes.update(tpStepMin=max(5, step), setMinStep=max(5, step))
                    if step > overlay["shortRangeStepMax"]:
                        changes["stratShortRange"] = False
                    else:
                        changes["shortRangeStepMin"] = max(5, step)
                else:
                    changes.update(trailStepMin=max(7, step), trailMinStep=max(7, step))
    result = {"complete": complete, "costPct": cost, "totals": figures(rows), "excluded": dict(missed),
              "ranges": ranges, "candidateChanges": changes,
              "basis": "Observed live entries and native one-minute parent exits. Matched live returns exclude Block adds. Overall live returns include adds. Cleanup closes are excluded."}
    atomic_json(run.parent / "evaluation.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "ranges"}, allow_nan=False))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
