"""Ad-hoc coordination sweep for the 3h XRP/BCH/SOL historic calc.

Runs the same real-data 3-hour full-grid replay across a small matrix of
`additionalCoordination` / `coordOptimizationN` settings and reports which
setting produces the best qualifying-set outcome (validated count, PF
quality). Not part of the permanent test suite -- ad hoc analysis only.
"""
import json
import sys
import time

sys.path.insert(0, "/vercel/share/v0-project/server/pulse")

from hist_calc import run_calc  # noqa: E402

SYMBOLS = ["XRP-USDT", "BCH-USDT", "SOL-USDT"]

BASE_BODY = {
    "hours": 3,
    "symbols": SYMBOLS,
    "allSymbols": False,
    "allConfigs": True,
    "trailing": True,
    "stratBlock": True,
    "stratDca": True,
    "stratIndications": True,
    "stratGeneral": True,
    "indTypeSignals": True, "indTypeState": True, "indTypeDirection": True, "indTypeMove": True,
    "indTypeActive": True, "indTypeCommon": True, "indTypeTrend": True, "indTypeBreak": True,
    "preferMinimalRange": True,
    "workers": 8,
    "synth": False,
}

MATRIX = [
    {"label": "coord-off", "additionalCoordination": False, "coordOptimizationN": 50},
    {"label": "coord-on-n50", "additionalCoordination": True, "coordOptimizationN": 50},
    {"label": "coord-on-n100", "additionalCoordination": True, "coordOptimizationN": 100},
    {"label": "coord-on-n150", "additionalCoordination": True, "coordOptimizationN": 150},
    {"label": "coord-on-n200", "additionalCoordination": True, "coordOptimizationN": 200},
]

results = []
for cfg in MATRIX:
    body = dict(BASE_BODY)
    body["additionalCoordination"] = cfg["additionalCoordination"]
    body["coordOptimizationN"] = cfg["coordOptimizationN"]
    t0 = time.time()
    job = run_calc(body, persist=False)
    elapsed = time.time() - t0
    rows = job.get("rows") or []
    row_count = job.get("rowCount") or 0
    validated = job.get("validatedCount") or 0
    pfs = [float(r.get("pf") or 0) for r in rows if r.get("validated")]
    avg_pf = sum(pfs) / len(pfs) if pfs else 0.0
    max_pf = max(pfs) if pfs else 0.0
    entry = {
        "label": cfg["label"],
        "additionalCoordination": cfg["additionalCoordination"],
        "coordOptimizationN": cfg["coordOptimizationN"],
        "elapsedSec": round(elapsed, 1),
        "rowCount": row_count,
        "validatedCount": validated,
        "avgValidatedPf": round(avg_pf, 3),
        "maxValidatedPf": round(max_pf, 3),
        "phase": job.get("phase"),
        "error": job.get("error"),
        "source": job.get("source"),
    }
    results.append(entry)
    print(json.dumps(entry), flush=True)

print("=== SUMMARY ===")
for r in sorted(results, key=lambda x: -x["validatedCount"]):
    print(json.dumps(r))

with open("/vercel/share/v0-project/reports/14d-run/coord_sweep_result.json", "w") as f:
    json.dump(results, f, indent=2)
