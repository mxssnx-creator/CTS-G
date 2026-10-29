#!/usr/bin/env python3
"""Bounded market snapshot audit, using the live SetBook replay and scoring code.

Input: per-symbol JSON arrays of [timestamp_ms, open, high, low, close, volume],
as accepted by stage_eval.load_bars. The audit reads at most 11 hours per symbol:
three six-hour rolling snapshots over five hours of progress. Its shadow-order
counts are not exchange fills. No live credentials or exchange orders are used.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server" / "pulse"))
from set_engine import SetBook, pf_of  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--symbols", default="BTC-USDT,ETH-USDT")
    ap.add_argument("--mode", choices=("positive", "negative", "both", "avgpositive", "avgnegative", "avgboth"), default="negative")
    ap.add_argument("--minimal", type=int, choices=(0, 1, 2, 3), default=1)
    a = ap.parse_args()
    symbols = [s.strip() for s in a.symbols.split(",") if s.strip()]
    market = {s: json.loads((Path(a.data) / (s + ".json")).read_text())[-660:] for s in symbols}
    if not symbols or any(len(rows) < 660 for rows in market.values()):
        raise ValueError("each symbol must have at least 660 one-minute bars")
    for s, rows in market.items():
        if any(len(r) != 6 for r in rows) or any(rows[i][0] - rows[i - 1][0] != 60000 for i in range(1, len(rows))):
            raise ValueError(f"{s}: expected consecutive one-minute OHLCV rows")
    ov = json.loads((ROOT / "server/pulse/overlay-bingx-x01.json").read_text())
    ov.update({"histLookbackBars": 360, "histLookbackAuto": False, "setSlRatios": [1.0], "setMinStep": 2,
               "setStepMax": 30, "setStepAdapt": False, "trailVariants": ["0.60:0.36", "1.20:0.60", "2.40:1.20", "3.60:1.20"],
               "exitIgnoreTp": True, "setGateMinN": a.minimal, "setGateMinMode": a.mode, "mainMinN": a.minimal, "mainMinMode": a.mode})
    book = SetBook()
    book.load(ov)
    end = min(rows[-1][0] for rows in market.values()) // 1000
    points = [end - 300 * 60, end - 150 * 60, end]

    def pf(g, loss):
        return round(pf_of(g, loss), 4) if g or loss else None

    reports = []
    for ts in points:
        for s, rows in market.items():
            chosen = [row[1:] for row in rows if ts - 359 * 60 <= row[0] // 1000 <= ts]
            if len(chosen) != 360:
                raise ValueError(f"{s}: incomplete six-hour snapshot")
            book.bars[s] = chosen
        book.replay_all(now=float(ts))
        if not book.progress.ready or book.progress.errors:
            raise RuntimeError((book.progress.phase, book.progress.error))
        split = book.snapshot()["pfBase"]
        groups = {}
        for st in book.by_idx:
            step = book.step_of(st)
            rg = next((label for label, lo, hi in (("2–6", 2, 6), ("7–12", 7, 12), ("13–20", 13, 20), ("21–30", 21, 30)) if lo <= step <= hi), "other")
            row = groups.setdefault((st.pack, st.kind, rg), {"pack": st.pack, "type": st.kind, "range": rg, "sets": 0, "base_valid": 0,
                "main_valid": 0, "final_valid": 0, "ready": 0, "base_orders": 0, "main_orders": 0, "real_orders": 0,
                "base_gp": 0.0, "base_gl": 0.0, "main_gp": 0.0, "main_gl": 0.0})
            base = not book.base_validation_reason(st)
            main_pass = base and book._main_pf_pass(st)
            row["sets"] += 1
            row["base_valid"] += base
            row["main_valid"] += main_pass
            row["final_valid"] += not bool(book.validation_reason(st))
            row["ready"] += not bool(book.gate_reason(st))
            if base:
                row["base_orders"] += st.gate_n
                row["base_gp"] += st.gate_gp
                row["base_gl"] += st.gate_gl
                row["main_orders"] += st.proc_total
                row["real_orders"] += st.exe_total + st.neg_total
            if main_pass:
                row["main_gp"] += st.main_gp
                row["main_gl"] += st.main_gl
        for row in groups.values():
            row["base_pf"] = pf(row.pop("base_gp"), row.pop("base_gl"))
            row["main_pf"] = pf(row.pop("main_gp"), row.pop("main_gl"))
        reports.append({"at_utc": datetime.fromtimestamp(ts, timezone.utc).isoformat(), "cost_pct": book.cost_pct,
                        "total": len(book.by_idx), "base": split["baseOnly"], "main": split["main"], "final": split["base"],
                        "eligible": split["eligible"], "groups": list(groups.values())})
    Path(a.out).write_text(json.dumps({"symbols": symbols, "lookback_minutes": 360, "minimal": a.minimal, "mode": a.mode,
                                       "hold_auto": book.hold_auto, "points": reports}, indent=2) + "\n")
    for p in reports:
        print(p["at_utc"], "base", p["base"], "main", p["main"], "final", p["final"], "ready", p["eligible"])


if __name__ == "__main__":
    main()
