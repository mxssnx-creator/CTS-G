#!/usr/bin/env python3
"""Read-only: simulated against live results of the settled Set closes in a trades file (see server/pulse/sim_diff.py).

Pass --session for an isolated x02 run. Its comparison uses the active window,
owned positions, and measured paper cost. Nothing is written or sent to the venue.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server" / "pulse"))
from position_cost import TP_UNIT_PCT  # noqa: E402
from set_engine import TRAIL_SET_ID  # noqa: E402
from sim_diff import diff_note, diff_report, new_diff  # noqa: E402
from x02_native_session import CONN, HOST  # noqa: E402

TP_STEP = re.compile(r":st(\d+)(?::|$)")


def load_session(path):
    record = json.loads((path / "session.json").read_text())
    if (not isinstance(record, dict) or record.get("connection") != CONN or record.get("host") != HOST
            or not re.fullmatch(r"G[\da-f]{4}", str(record.get("tag"))) or not isinstance(record.get("symbols"), list)):
        raise ValueError("expected a native x02 demo session")
    start = record.get("activeStartedAt")
    if not isinstance(start, (int, float)) or not math.isfinite(start):
        raise ValueError("x02 session has not admitted entries")
    end = record.get("activeEndedAt", time.time())
    if not isinstance(end, (int, float)) or not math.isfinite(end) or end < start:
        raise ValueError("invalid x02 observation window")
    return record, start, end


def settled_rows(trades):
    with trades.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("ours") is not False and row.get("set_id"):
                yield row


def observe_session(rows, record, start, end):
    tag = record["tag"].lower()
    symbols = set(record["symbols"])
    for row in rows:
        if (row.get("ours") is not True or row.get("conn") != CONN or row.get("symbol") not in symbols
                or not str(row.get("client_id") or "").lower().startswith(tag)
                or row.get("reason") == "bounded-session-cleanup"):
            continue
        try:
            closed_at = float(row.get("t"))
        except (TypeError, ValueError):
            continue
        if start <= closed_at <= end:
            yield row


def grid_steps(set_id):
    trail = TRAIL_SET_ID.search(set_id)
    if trail:
        arm, gap = (round(float(distance) / TP_UNIT_PCT, 3) for distance in trail.groups())
        return (("trailArm", arm), ("trailGap", gap))
    match = TP_STEP.search(set_id)
    return (("tp", int(match[1])),) if match else ()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    source = ap.add_mutually_exclusive_group()
    source.add_argument("--trades", type=Path)
    source.add_argument("--session", type=Path, help="directory of a native x02 session")
    ap.add_argument("--overlay", type=Path)
    ap.add_argument("--cost-pct", type=float)
    ap.add_argument("--stats", type=Path, help="engine stats containing costModel.paperCostPct")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    session_info = None
    trades = a.trades or Path(f"/opt/grok-x01-pulse/trades-{CONN}.jsonl")
    if a.session:
        try:
            session_info = load_session(a.session)
        except (OSError, ValueError) as exc:
            ap.error(str(exc))
        trades = a.session / "state" / f"trades-{CONN}.jsonl"

    cost = a.cost_pct
    if cost is None and a.stats and a.stats.exists():
        cost = (json.loads(a.stats.read_text()).get("costModel") or {}).get("paperCostPct")
    if cost is None and session_info:
        cost = (session_info[0].get("accountRates") or {}).get("paperCostPct")
        if cost is None:
            ap.error("x02 session is missing its measured paper cost")
    if cost is None and a.overlay:
        cost = json.loads(a.overlay.read_text()).get("positionCostPct")
    cost = 0.12 if cost is None else float(cost)
    if not math.isfinite(cost) or cost < 0:
        ap.error("paper cost must be a finite nonnegative percent")

    agg = new_diff()
    steps = {"tp": {}, "trailArm": {}, "trailGap": {}}
    rows = () if session_info and not trades.exists() else settled_rows(trades)
    if session_info:
        rows = observe_session(rows, *session_info)
    for row in rows:
        diff_note(agg, row, cost / 100)
        if session_info:
            for kind, step in grid_steps(str(row["set_id"])):
                if step not in steps[kind]:
                    steps[kind][step] = new_diff()
                diff_note(steps[kind][step], row, cost / 100)
    rep = diff_report(agg)
    if session_info:
        record, start, end = session_info
        rep["session"] = {"tag": record["tag"], "activeFrom": start, "activeThrough": end,
                          "symbols": len(record["symbols"]), "complete": record.get("complete") is True,
                          "paperCostPct": cost}
        rep["byStep"] = {kind: {f"{step:g}": diff_report(group)["total"] for step, group in sorted(groups.items())}
                         for kind, groups in steps.items()}
    if a.json:
        print(json.dumps(rep, indent=1))
        return 0
    t = rep["total"]
    print(f"{rep['closes']} settled Set closes ({rep['unknown']} unscored/unknown) at PositionCost {cost:.3f} %")
    print(f"  live PF {t['livePf']}  sim PF {t['simPf']}  | live net {t['liveNetPct']} %  sim net {t['simNetPct']} %  gap {t['netGapPct']} % "
          f"(fill {t['fillGapPct']} %, cost {t['costGapPct']} %)")
    for k, v in rep["byExit"].items():
        print(f"  {k:6s} n={v['n']:4d} livePF={v['livePf']} simPF={v['simPf']} fill gap {v['fillGapPct']} % cost gap {v['costGapPct']} %")
    s, g = rep["stopFactor"], rep["targetFactor"]
    print(f"  stop fill factor (live loss / planned stop): n={s['n']} median={s['median']} mean={s['mean']} p90={s['p90']}")
    print(f"  target fill factor: n={g['n']} median={g['median']} mean={g['mean']} p10={g['p10']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
