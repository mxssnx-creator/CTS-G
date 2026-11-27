#!/usr/bin/env python3
"""Check the frozen post-Base configurations against independent replay evidence."""
import argparse
import hashlib
import json
import math
from pathlib import Path


HOUR = 3600


def case_key(row):
    return row["base"], row["final"], tuple(row["proof"] or ()), row["scope"], row["alignment"]


def near(a, b):
    return math.isfinite(a) and math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-12)


def verify_case(case, hours):
    hourly = case["hourly"]
    closes, opened = case["closed"], case["opened"]
    gain, loss = case["gain"], case["loss"]
    positive = sum(row["net"] > 1e-12 for row in hourly)
    active = sum(row["closed"] > 0 for row in hourly)
    valid = (type(closes) is int and type(opened) is int and type(case["held"]) is int
             and 0 <= closes <= opened and case["held"] >= 0 and math.isfinite(case["heldMarkNet"])
             and all(v >= 0 and math.isfinite(v) for v in (gain, loss))
             and (near(case["pf"], gain / loss) if loss > 0 and case["pf"] is not None
                  else loss == 0 and case["pf"] is None)
             and near(case["net"], gain - loss) and opened == closes + case["held"]
             and all(type(row["closed"]) is int and row["closed"] >= 0 and row["opened"] >= 0
                     and row["gain"] >= 0 and row["loss"] >= 0
                     and near(row["net"], row["gain"] - row["loss"]) for row in hourly)
             and len(hourly) == hours and sum(row["closed"] for row in hourly) == closes
             and sum(row["opened"] for row in hourly) == opened and case["positiveHours"] == positive
             and case["activeHours"] == active and near(case["hourSuccessPct"], 100 * positive / hours)
             and near(gain, math.fsum(row["gain"] for row in hourly))
             and near(loss, math.fsum(row["loss"] for row in hourly)))
    if not valid:
        raise ValueError(f"inconsistent settled and hourly evidence for {case['scope']}")


def assess(selection_path, research, evaluation, stress):
    encoded = selection_path.read_bytes()
    digest = hashlib.sha256(encoded).hexdigest()
    selection = json.loads(encoded)
    required = selection["decision"]
    keys = [case_key(row) for row in selection["cases"]]
    if not keys or len(set(keys)) != len(keys):
        raise ValueError("selection needs distinct frozen cases")
    if not research or len(evaluation) < required["minimumEvaluationWindows"]:
        raise ValueError("independent evaluation windows or research evidence are missing")
    windows = sorted(evaluation, key=lambda run: run["start"])
    latest = windows[-1]
    chronological = latest["start"] >= max(run["end"] for run in research)
    if not chronological or any(a["end"] > b["start"] for a, b in zip(windows, windows[1:])):
        raise ValueError("evaluation periods overlap or the newest period precedes the research cutoff")
    if any(a["start"] < b["end"] and b["start"] < a["end"] for a in windows for b in research):
        raise ValueError("an evaluation period was used for research")
    for field in ("start", "end", "prehistory", "symbols", "inputHashes", "overlayHash", "taker"):
        if stress[field] != latest[field]:
            raise ValueError(f"stress uses a different {field} from the latest window")
    if not near(stress["slippage"] - latest["slippage"], required["stressAdditionalPerSideBps"] / 10000):
        raise ValueError("stress uses a different slippage increment from the frozen rule")

    indexed = []
    for i, run in enumerate([*research, *windows, stress]):
        hours, remainder = divmod(run["end"] - run["start"], HOUR)
        benchmark = run is not stress
        if (run["selectionHash"] != digest or hours not in (8, 24) or remainder
                or run["start"] % HOUR or run["start"] - run["prehistory"] != 48 * HOUR
                or run["overlayHash"] != latest["overlayHash"] or run["taker"] != latest["taker"]
                or not math.isfinite(run["slippage"]) or run["slippage"] < 0
                or benchmark and run["slippage"] != latest["slippage"]
                or set(run["symbols"]) != set(run["inputHashes"]) or len(run["symbols"]) != len(latest["symbols"])):
            raise ValueError(f"incompatible replay evidence in {run['label']}")
        cases = {case_key(row): row for row in run["cases"]}
        if set(cases) != set(keys) or len(cases) != len(run["cases"]):
            raise ValueError(f"changed or duplicate scenarios in {run['label']}")
        for case in cases.values():
            verify_case(case, hours)
        if i >= len(research):
            indexed.append((run, cases, hours))

    reports = []
    for select, key in zip(selection["cases"], keys):
        failures, observations = [], []
        total = 0
        if not latest.get("sourceArchiveChecksumsVerified") or not stress.get("sourceArchiveChecksumsVerified"):
            failures.append("Newest window or its stress replay has no verified archive checksums")
        for run, rows, hours in indexed:
            # Replayed stress trades check robustness and do not add sample support.
            row = rows[key]
            if run is not stress:
                total += row["closed"]
            minimum = required[f"minimumClosesPer{hours}Hours"]
            if row["closed"] < minimum:
                failures.append(f"{run['label']} closed {row['closed']} of {minimum} required positions")
            if row["pf"] is None or row["pf"] <= required["minimumPf"]:
                measured = "unavailable" if row["pf"] is None else f"{row['pf']:.3f}"
                failures.append(f"{run['label']} settled PF {measured}")
            if row["net"] + row["heldMarkNet"] <= 0:
                failures.append(f"{run['label']} net including marked holdings is nonpositive")
            observations.append({"label": run["label"], "closed": row["closed"], "pf": row["pf"],
                                 "hourSuccessPct": row["hourSuccessPct"]})
        if total < required["minimumTotalIndependentCloses"]:
            failures.append(f"{total} settled positions across distinct periods")
        reports.append({**select, "eligible": not failures, "reasons": failures, "evidence": observations})
    return {"selectionHash": digest, "eligible": any(row["eligible"] for row in reports), "cases": reports}


def read(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--research", type=Path, nargs="+", required=True)
    parser.add_argument("--evaluate", type=Path, nargs="+", required=True)
    parser.add_argument("--stress", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = assess(args.selection, [read(path) for path in args.research],
                        [read(path) for path in args.evaluate], read(args.stress))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
