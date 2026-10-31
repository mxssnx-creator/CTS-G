#!/usr/bin/env python3
"""Recount the archived CTS-A-O Last-N diagnostic and stress its fixed closes.

This is an offline comparison, not a strategy replay.  Qualification windows,
entries, allocation and liquidity are frozen at the archive's original cost.
The hypothetical cost variants apply only to simple single-entry/single-exit
rows; DCA and Axis have additional legs whose costs cannot be reconstructed
from this compact archive.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import gzip
import hashlib
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "pulse"))
from position_cost import last_n_cost_pf  # noqa: E402

SIMPLE = {"normal", "trailing", "trailing-only"}


def figures(values):
    gains = math.fsum(x for x in values if x > 0)
    losses = -math.fsum(x for x in values if x < 0)
    return {"n": len(values), "gp": gains, "gl": losses,
            "pf": gains / losses if losses else None,
            "noLoss": gains > 0 and losses == 0}


def compare_archived(actual, expected, label):
    for key in ("n", "gp", "gl", "pf", "noLoss"):
        x, y = actual[key], expected[key]
        if isinstance(x, float) and isinstance(y, (float, int)):
            same = math.isclose(x, y, rel_tol=1e-10, abs_tol=1e-10)
        else:
            same = x == y
        if not same:
            raise ValueError(f"{label}: {key} mismatch: {x!r} != {y!r}")


def compare_engine(values, label):
    if not values:
        return
    # A finite gross field is required by CTS-G's input contract.  The recorded
    # net return remains authoritative: the gross placeholder must not be
    # used to deduct another paper allowance.
    rows = [{"pnl_pct": 0.0, "net_pnl_pct": x} for x in values]
    reference = figures(values)
    for cost_pct in (0.12, 0.30):
        result = last_n_cost_pf(rows, len(rows), cost_pct)
        pf = reference["pf"] if reference["pf"] is not None else (99.0 if reference["noLoss"] else 0.0)
        if result["count"] != len(values) or abs(result["pf"] - round(pf, 4)) > 1e-9:
            raise ValueError(f"{label}: CTS-G net-return PF mismatch at cost {cost_pct}")


def load_archive(path):
    raw = path.read_bytes()
    match = re.search(rb'<script id="payload"[^>]*>(.*?)</script>', raw, re.S)
    if not match:
        raise ValueError("archive payload not found")
    payload = json.loads(gzip.decompress(base64.b64decode(match.group(1), validate=True)))
    data = payload["data"]
    if data.get("schema") != "cts-independent-lastn-diagnostic-v1":
        raise ValueError("unexpected archive schema")
    return raw, payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    raw, payload = load_archive(args.archive)
    data = payload["data"]
    orders = data["orders"]
    contexts = data["contexts"]
    context_by_key = {(row["cfg"], row["sym"], row["side"]): row for row in contexts}
    if len(context_by_key) != len(contexts):
        raise ValueError("duplicate configuration/symbol/direction context")
    cost = float(data["study"]["cost"])
    if not math.isfinite(cost) or cost < 0:
        raise ValueError("invalid archived modeled cost")
    for row in orders:
        if not math.isfinite(float(row["unitReturn"])):
            raise ValueError("non-finite modeled return")

    all_values = [float(o["unitReturn"]) for o in orders]
    overall = figures(all_values)
    compare_archived(overall, data["original"]["execution"], "all closes")
    compare_engine(all_values, "all closes")
    rolling, initial = {}, {}
    for window in data["windows"]:
        label = str(window)
        context_states = Counter(row["windows"][label]["state"] for row in contexts)
        observed_states = Counter(row["windows"][label]["state"] for row in orders)
        for state in ("qualified", "failed", "insufficient", "flat", "unknown"):
            if context_states[state] != data["byWindow"][label][state]:
                raise ValueError(f"Last-{label}: initial {state} context count mismatch")
            if observed_states[state] != data["rolling"][label][state]:
                raise ValueError(f"Last-{label}: rolling {state} count mismatch")
        try:
            selected_initial = [o for o in orders if context_by_key[(o["cfg"], o["sym"], o["side"])]["windows"][label]["state"] == "qualified"]
        except KeyError as exc:
            raise ValueError(f"Last-{label}: order has no archived context") from exc
        initial_result = figures([float(o["unitReturn"]) for o in selected_initial])
        compare_archived(initial_result, data["byWindow"][label]["laterExistingFunded"], f"initial Last-{label}")
        initial[label] = {"contexts": len(contexts), "qualified": context_states["qualified"],
                          "qualifiedPct": 100 * context_states["qualified"] / len(contexts),
                          "laterExistingFunded": initial_result}
        selected = [o for o in orders if o["windows"][label]["state"] == "qualified"]
        values = [float(o["unitReturn"]) for o in selected]
        measured = figures(values)
        recorded = data["rolling"][label]
        if len(selected) != recorded["qualified"]:
            raise ValueError(f"Last-{label}: qualified count mismatch")
        compare_archived(measured, recorded["laterExistingFunded"], f"Last-{label}")
        compare_engine(values, f"Last-{label}")
        rolling[label] = {"evaluated": len(orders), "qualifiedPct": 100 * len(selected) / len(orders),
                          **measured}

    simple = [o for o in orders if o["kind"] in SIMPLE]
    # Fees and slippage below are hypothetical fractions, not observed fills.
    # Each case replaces the single archived flat round-trip deduction.  It
    # assumes rates are fractions of entry notional for both legs; actual fee
    # accounting instead uses each fill's own notional.
    maker, taker, slip = .0002, .0005, .00025
    scenarios = {"maker_maker": maker + maker + 2 * slip,
                 "maker_taker": maker + taker + 2 * slip,
                 "taker_taker": taker + taker + 2 * slip}
    sensitivity = {}
    for name, assumed in scenarios.items():
        groups = {"all": simple,
                  "rollingLast5Qualified": [o for o in simple if o["windows"]["5"]["state"] == "qualified"],
                  "rollingLast10Qualified": [o for o in simple if o["windows"]["10"]["state"] == "qualified"]}
        sensitivity[name] = {"roundTripFraction": assumed,
                             **{key: figures([float(o["unitReturn"]) + cost - assumed for o in rows])
                                for key, rows in groups.items()}}
    result = {
        "scope": "fixed archived modeled closes; no resimulation or requalification; no venue fills",
        "archiveSha256": hashlib.sha256(raw).hexdigest(),
        "study": data["study"]["id"],
        "archivedRoundTripFraction": cost,
        "archivedVerification": payload["verification"],
        "overall": overall,
        "initialContexts": initial,
        "rollingQualified": rolling,
        "costSensitivity": {"assumptions": "simple rows only; equal entry-notional fees; two 0.025% slippage legs; original selection fixed",
                            "simpleRows": len(simple), "excludedAdditionalLegRows": len(orders) - len(simple),
                            "scenarios": sensitivity},
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
