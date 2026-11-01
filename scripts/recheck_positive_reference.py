"""Recount the frozen positive CTS-A-O cohort, not a fresh strategy simulation.

Uses the documented archive contract: stored r = unit return * execution mult.
All fees already included in r stay included; known net is not charged again.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

from independent_range_audit import figures


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(directory):
    raw = json.loads((directory / "raw.json").read_text())
    report = json.loads((directory / "session.json").read_text())
    if raw["study"] != report["study"] or raw["at"] != report["at"]:
        raise ValueError("raw/report provenance differs")
    if raw["executionReturns"] != {"version": 1, "encoding": "tape-return-times-execution-mult"}:
        raise ValueError("unknown execution-return schema")
    records, sources = [], {}
    manifest = raw["arraySidecars"]["trades"]
    for chunk in manifest["chunks"]:
        path = (directory / chunk["path"]).resolve()
        if not path.is_relative_to(directory.resolve()) or digest(path) != chunk["sha256"]:
            raise ValueError("sidecar path/hash mismatch")
        sources[chunk["path"]] = chunk["sha256"]
        for line in path.read_text().splitlines():
            tag, index, row = json.loads(line)
            if tag != "i" or index != len(records):
                raise ValueError("sidecar order/record mismatch")
            if not math.isfinite(row["mult"]) or row["mult"] <= 0 or not math.isfinite(row["r"]):
                raise ValueError("unmeasurable execution multiplier/return")
            records.append(row)
    if len(records) != manifest["items"]:
        raise ValueError("sidecar count mismatch")
    values = [r["r"] / r["mult"] for r in records]
    measured = figures(values)
    expected = report["total"]
    for key, target in (("n", expected["orders"]), ("gain", expected["gpR"]),
                        ("loss", expected["glR"]), ("pf", expected["exactPf"])):
        if not math.isclose(measured[key], target, rel_tol=1e-10, abs_tol=1e-10):
            raise ValueError(f"original positive cohort changed: {key}")
    keys = Counter(json.dumps([r["cfg"], r["sym"], r["side"], r["entryT"], r["exitT"],
                               r.get("level"), r["reason"], r["r"] / r["mult"], r["mult"]]) for r in records)
    return {"measured": measured, "byType": {kind: figures([r["r"] / r["mult"] for r in records if r["kind"] == kind])
                                              for kind in sorted({r["kind"] for r in records})},
            "recordedCounts": expected["fundedCounts"], "study": raw["study"], "costFraction": raw["settings"]["cost"],
            "rawSha256": digest(directory / "raw.json"), "reportSha256": digest(directory / "session.json"),
            "sidecars": sources}, keys, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("output exists; preserve prior evidence")
    arms = {}
    control, control_keys, _ = load(args.root / "control")
    for directory in sorted(args.root.iterdir()):
        if not (directory / "raw.json").exists():
            continue
        report, keys, rows = load(directory)
        for field in ("feedSha256", "sourceSha256", "window"):
            if report["study"][field] != control["study"][field]:
                raise ValueError(f"arm changes comparison basis: {field}")
        report["addedOrChangedRows"] = sum((keys - control_keys).values())
        report["removedOrChangedRows"] = sum((control_keys - keys).values())
        if keys != control_keys:
            raise ValueError("ordinary cohort is not identical across frozen arms")
        # Only the one-leg types permit replacing the archived flat cost. These
        # scenarios do NOT change qualification, fills, hold or allocation.
        simple = [r for r in rows if r["kind"] in ("normal", "trailing", "trailing-only")]
        report["fixedCloseCostSensitivity"] = {
            "scope": "hypothetical equal-entry-notional fees/slippage on simple rows only; no requalification",
            "excludedMultiLegRows": len(rows) - len(simple),
            "scenarios": {label: {"roundTripCostFraction": cost,
                          **figures([r["r"] / r["mult"] + report["costFraction"] - cost for r in simple])}
                          for label, cost in (("maker-maker", .0009), ("maker-taker", .0012), ("taker-taker", .0015))}}
        arms[directory.name] = report
    result = {"scope": "frozen historical CTS-A-O ordinary modeled cohort; not current CTS-G, Signals profitability, a new replay or live fills",
              "normalization": "unit net return = stored r / recorded external execution multiplier; no second fee subtraction",
              "arms": arms}
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({name: arm["measured"] for name, arm in arms.items()}))


if __name__ == "__main__":
    main()
