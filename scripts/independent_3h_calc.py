#!/usr/bin/env python3
"""Independent last-3h calc + PF/value check. Does not touch Pulse.run()."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from typing import Any, Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "pulse"))

from hist_calc import run_calc  # noqa: E402
from position_cost import POSITIVE_PF, is_positive_pf, last_n_cost_pf, evaluation_windows  # noqa: E402
from coord_engine import Coordinator, recent_closed_rows  # noqa: E402
from set_engine import last_n_chrono, row_equity_pnl  # noqa: E402

DESK = os.environ.get("PULSE_URL", "http://152.53.114.112:3102").rstrip("/")
SYMBOLS = ["XRP-USDT", "BCH-USDT", "SOL-USDT"]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reports", "independent-3h.json")


def get_json(path: str) -> Any:
    with urllib.request.urlopen(DESK + path, timeout=25) as r:
        return json.loads(r.read().decode())


def pf_from_closed(closed: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    now = time.time()
    full = last_n_cost_pf(closed, 15)
    windows = evaluation_windows(closed, required_samples=8)
    recent = recent_closed_rows(closed, now=now)
    recent_pf = last_n_cost_pf(recent, 15) if recent else {"ratio": None, "count": 0}
    coord = Coordinator()
    coord.load({}, {})
    allow, reasons, metrics = coord.gate(closed, 0)
    return {
        "label": label,
        "nClosed": len(closed),
        "nRecent3h": len(recent),
        "last15": {"pf": full.get("ratio"), "n": full.get("count"), "netAvg": full.get("netAvg"), "classic": full.get("classicPf")},
        "windows": {k: {"n": v.get("n"), "pf": v.get("pf"), "validated": v.get("validated")} for k, v in windows.items()},
        "recent3h": {"pf": recent_pf.get("ratio"), "n": recent_pf.get("count")},
        "coordAllow": allow,
        "coordReasons": reasons,
        "coordStages": (coord.last or {}).get("stages"),
        "positiveFloor": POSITIVE_PF,
        "last15Positive": is_positive_pf(full.get("ratio")),
        "ddtUnits": [row_equity_pnl(r) for r in closed[-3:]],
        "chronoN": len(last_n_chrono(closed, 15)),
    }


def check_sl_tp(closed: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = []
    mismatches = 0
    for c in closed:
        sl_ratio = float(c.get("sl_ratio") or 0)
        sl_pct = float(c.get("sl_pct") or 0)
        tp_pct = float(c.get("tp_pct") or 0)
        set_id = str(c.get("set_id") or "")
        actual = (sl_pct / tp_pct) if tp_pct > 1e-12 else None
        # ignore-tp widens exchange TP to ~3× SL (safety), so sl/tp ≈ 0.33
        ignore_tp_ratio = (sl_pct / tp_pct) if tp_pct else None
        ok = True
        detail = ""
        if sl_ratio > 0 and sl_pct > 0 and abs(sl_pct - tp_pct * sl_ratio) > 1e-4 and abs((tp_pct or 0) - sl_pct * 3.0) > 1e-4:
            ok = False
            detail = "sl not bound to set ratio nor ignore-tp 3x"
            mismatches += 1
        rows.append({
            "symbol": c.get("symbol"),
            "set_id": set_id,
            "sl_ratio": sl_ratio,
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
            "actualSlOverTp": actual,
            "reason": c.get("reason"),
            "pnl_pct": c.get("pnl_pct"),
            "ok": ok,
            "detail": detail,
        })
    return {"checked": len(rows), "mismatches": mismatches, "rows": rows[-12:]}


def main() -> int:
    report: Dict[str, Any] = {"startedAt": time.time(), "desk": DESK, "floor": POSITIVE_PF}
    for conn in ("live", "vst"):
        try:
            st = get_json("/stats.json?conn=" + conn)
        except Exception as e:
            report[conn] = {"error": str(e)}
            continue
        closed = [c for c in (st.get("closed") or []) if isinstance(c, dict)]
        report[conn] = {
            "running": st.get("running"),
            "n": len(st.get("symbols") or []),
            "openCount": st.get("openCount"),
            "equity": st.get("equity"),
            "available": st.get("available"),
            "pf": st.get("pf"),
            "sets": {
                "minPf": (st.get("sets") or {}).get("minPf"),
                "enablePf": (st.get("sets") or {}).get("enablePf"),
                "ready": (st.get("sets") or {}).get("ready"),
                "setCount": (st.get("sets") or {}).get("setCount") or (st.get("sets") or {}).get("count"),
                "validatedCount": (st.get("sets") or {}).get("validatedCount"),
            },
            "independentPf": pf_from_closed(closed, conn),
            "orderValues": check_sl_tp(closed),
        }
        print(json.dumps({"conn": conn, "pf": report[conn]["independentPf"], "orders": report[conn]["orderValues"]["mismatches"]}, default=str), flush=True)

    print("=== independent 3h hist calc ===", flush=True)
    t0 = time.time()
    job = run_calc({
        "hours": 3,
        "symbols": SYMBOLS,
        "allSymbols": False,
        "symbolCap": 3,
        "allConfigs": True,
        "trailing": True,
        "stratBlock": True,
        "stratDca": False,
        "stratIndications": True,
        "stratGeneral": True,
        "indTypeSignals": True, "indTypeState": True, "indTypeDirection": True, "indTypeMove": True,
        "indTypeActive": True, "indTypeCommon": True, "indTypeTrend": True, "indTypeBreak": True,
        "preferMinimalRange": True,
        "workers": max(1, min(4, os.cpu_count() or 2)),
        "synth": False,
    }, persist=False)
    elapsed = time.time() - t0
    rows = job.get("rows") or []
    validated = [r for r in rows if r.get("validated")]
    pfs = [float(r.get("last15Ratio") or r.get("pf") or 0) for r in validated]
    report["calc3h"] = {
        "elapsedSec": round(elapsed, 1),
        "phase": job.get("phase"),
        "error": job.get("error"),
        "source": job.get("source"),
        "rowCount": job.get("rowCount") or len(rows),
        "validatedCount": job.get("validatedCount") or len(validated),
        "avgValidatedPf": round(sum(pfs) / len(pfs), 4) if pfs else 0.0,
        "maxValidatedPf": round(max(pfs), 4) if pfs else 0.0,
        "allValidatedPositive": all(is_positive_pf(p) for p in pfs) if pfs else True,
        "symbols": SYMBOLS,
        "detail": job.get("detail"),
        "coverage": {k: (job.get("coverage") or {}).get(k) for k in ("product", "setCount", "validatedCount", "slCover", "trailCover", "packs")},
        "top": [
            {k: r.get(k) for k in ("id", "pack", "kind", "slRatio", "step", "last15Ratio", "pf", "validated", "n", "maxDdS")}
            for r in sorted(validated, key=lambda x: -float(x.get("last15Ratio") or x.get("pf") or 0))[:8]
        ],
    }
    print(json.dumps(report["calc3h"], default=str), flush=True)
    report["finishedAt"] = time.time()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print("wrote", OUT, flush=True)
    ok = (job.get("phase") == "ready" and not job.get("error")
          and report.get("live", {}).get("independentPf") is not None)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
