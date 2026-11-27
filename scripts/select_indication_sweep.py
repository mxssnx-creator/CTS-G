#!/usr/bin/env python3
"""Freeze a diverse confirmation roster from the 96-hour development screen."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from indication_sweep import catalogue, GEOMETRIES
from indication_lib import GROUPS


def admission_score(row):
    """Rank by the weakest development segment, then hourly edge and drawdown."""
    def pf(key):
        value = row[key]
        return value if value is not None else 0.0
    stable = min(pf("first_pf"), pf("second_pf"), pf("stress_pf"))
    active = row["positive_hours"] / row["hours"]
    diversified = row["net_positive_symbols"]
    drawdown = row["hour_dd_pct"]
    qualified = (
        row["trades"] >= 120 and stable >= 1.02 and active >= .48 and diversified >= 15
        and row["largest_symbol_gain_share"] is not None and row["largest_symbol_gain_share"] <= .18
        and drawdown <= min(1.5, row["net_pct"])
    )
    return qualified, stable, active, row["net_pct"] / max(.05, drawdown), row["trades"]


def choose(report):
    if report["phase"] != "development" or len(report["symbols"]) != 50:
        raise ValueError("Selection requires the 50-symbol development screen")
    if len(report["results"]) != len(catalogue()) * len(GEOMETRIES):
        raise ValueError("Selection requires the complete trial matrix")
    ranked = sorted((r for r in report["results"] if r["trades"] >= 120),
                    key=admission_score, reverse=True)
    roster = {}

    def add_best(criterion, tag):
        for row in ranked:
            if criterion(row):
                key = (row["name"], row["step"], row["ratio"])
                if key not in roster:
                    roster[key] = {k: row[k] for k in ("name", "clock", "step", "ratio", "hold", "band", "family")}
                    roster[key]["selection_role"] = tag
                    roster[key]["screen_passed"] = admission_score(row)[0]
                break

    for group in ("rsi", "psar", "adx"):
        add_best(lambda r, family=group: r["family"] == family, group)
    for band in ("micro", "minimal", "short", "normal", "long", "wide"):
        add_best(lambda r, name=band: r["band"] == name, band)
    for group in ("trend", "break", "revert"):
        add_best(lambda r, g=group: GROUPS.get(r["family"]) == g, group)
    # Add the strongest still-unrepresented sources. Correlated parameter
    # variants of an already shortlisted family don't consume these slots.
    for _ in range(4):
        existing = {r["family"] for r in roster.values()}
        add_best(lambda r: r["family"] not in existing and admission_score(r)[0], "stable")
    return list(roster.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("screen", type=Path)
    parser.add_argument("roster", type=Path)
    args = parser.parse_args()
    report = json.loads(args.screen.read_text())
    rows = choose(report)
    body = json.dumps(rows, ensure_ascii=False, indent=2)
    args.roster.write_text(body + "\n", encoding="utf-8")
    print(body)


if __name__ == "__main__":
    main()
