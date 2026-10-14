#!/usr/bin/env python3
"""Re-express archived hourly net-return summaries on the canonical PF scale.

This preserves the source report's original selected trades. It does not
rerun admission or infer missing event/weekly evidence from headline totals.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def metric(summary: dict, cost_pct: float) -> dict:
    count = int(summary.get("n") or 0)
    net_pct_sum = float(summary.get("net") or 0)
    average = net_pct_sum / count if count else 0.0
    reference = cost_pct if cost_pct > 0 else .10
    return {
        "n": count, "pf": 1 + .10 * average / reference if count else None,
        "netPctSum": net_pct_sum, "netAvgPct": average,
        "costPct": cost_pct, "pfReferenceCostPct": reference,
        "costSubtracted": True, "source": "archived-cost-and-funding-net-simulation",
    }


def rebase(path: Path, cost_pct: float) -> dict:
    original = json.loads(path.read_text())
    periods = {}
    for name, result in original.get("periods", {}).items():
        head = result["metrics"]
        summary = metric(head, cost_pct)
        # The archived stress variant subtracts another 0.20% on each trade;
        # denominator remains the original cost unit to compare return edge.
        summary["stressPf"] = metric({"n": summary["n"], "net": summary["netPctSum"] - .20 * summary["n"]}, cost_pct)["pf"]
        for side in ("long", "short"):
            if isinstance(head.get(side), dict):
                summary[side] = metric(head[side], cost_pct)
        periods[name] = {
            "start": result.get("start"), "end": result.get("end"),
            "metrics": summary,
            "forward": {stage: metric(row, cost_pct) for stage, row in result.get("forward", {}).items()},
            "bySet": {key: metric(row, cost_pct) for key, row in result.get("bySet", {}).items()},
            "byPair": {key: metric(row, cost_pct) for key, row in result.get("byPair", {}).items()},
            "shadow": metric(result["shadow"], cost_pct) if result.get("shadow") else None,
        }
    return {"sourceFile": str(path), "selectionPolicy": "original-legacy-admission-unchanged", "periods": periods}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("reports/hourly-wf-2026-10-14"))
    parser.add_argument("--out", type=Path, default=Path("reports/pf-contract-audit-20261015/hourly-archive-return-pf.json"))
    parser.add_argument("--cost-pct", type=float, default=.20)
    args = parser.parse_args()
    sources = sorted(args.source.glob("*.json"))
    if not sources:
        parser.error("No archived hourly reports found")
    output = {
        "version": 1,
        "formula": "PF = 1 + 0.10 * (sum(net return percent) / closed count) / roundtrip PositionCost percent",
        "costPct": args.cost_pct,
        "evidence": "Historical simulation summaries; returns already include cost and funding. No account cash or live orders.",
        "limitations": ["Original admission used classic PF; selected trades are unchanged.",
                        "Raw candles and trade caches unavailable in this checkout; corrected admission was not rerun.",
                        "Event PF and weekly admission cannot be reconstructed from headline sums and are omitted."],
        "reports": [rebase(source, args.cost_pct) for source in sources],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
