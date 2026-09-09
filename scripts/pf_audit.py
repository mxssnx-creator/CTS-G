#!/usr/bin/env python3
"""Independent PF / intern / DDT / SL-TP audit against the live desk tape."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from typing import Any, Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "pulse"))

from position_cost import (  # noqa: E402
    POSITIVE_PF,
    POSITION_COST_PCT_DEFAULT,
    cost_as_frac,
    evaluation_windows,
    is_positive_pf,
    last_n_cost_pf,
    net_pnl_pct,
    ratio_from_r,
    signed_result_r,
)
from coord_engine import Coordinator, recent_closed_rows  # noqa: E402
from set_engine import last_n_chrono, row_equity_pnl, drawdown_time  # noqa: E402

DESK = os.environ.get("PULSE_URL", "http://152.53.114.112:3102").rstrip("/")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reports", "pf-audit.json")


def get_json(path: str) -> Any:
    with urllib.request.urlopen(DESK + path, timeout=25) as r:
        return json.loads(r.read().decode())


def identities() -> List[Dict[str, Any]]:
    rows = []

    def rec(name: str, ok: bool, detail: Any = "") -> None:
        rows.append({"name": name, "ok": bool(ok), "detail": detail})

    rec("floor-is-1.10", POSITIVE_PF == 1.10, POSITIVE_PF)
    rec("1.02-is-not-positive", not is_positive_pf(1.02))
    rec("1.10-is-positive", is_positive_pf(1.10))
    rec("1.00-is-neutral-not-positive", not is_positive_pf(1.00))
    # pnl_pct=0.002 fraction = +0.20% gross; cost 0.10% → +1R → PF 1.10
    zero_net_plus1 = last_n_cost_pf([{"t": i, "pnl_pct": 0.002} for i in range(15)], 15)
    rec("plus1x-cost-is-1.10", abs(float(zero_net_plus1["ratio"]) - 1.10) < 1e-6, zero_net_plus1["ratio"])
    # gross 0 → signed_r = -1 → PF 0.90
    flat = last_n_cost_pf([{"t": i, "pnl_pct": 0.0} for i in range(15)], 15)
    rec("zero-gross-is-0.90", abs(float(flat["ratio"]) - 0.90) < 1e-6, flat["ratio"])
    # classic vs cost-scale: all +0.20% gross, no losses → classic 99, cost-scale 1.10
    rec("classic-not-cost-scale", float(zero_net_plus1["classicPf"]) >= 10, zero_net_plus1["classicPf"])
    rec("cost-frac-0.10pct", abs(cost_as_frac(0.10) - 0.001) < 1e-12, cost_as_frac(0.10))
    rec("net-pnl-plus1x", abs(net_pnl_pct(0.002, 0.10) - 0.001) < 1e-12, net_pnl_pct(0.002, 0.10))
    rec("signed-r-plus1", abs(signed_result_r(0.002, 0.10) - 1.0) < 1e-9, signed_result_r(0.002, 0.10))
    rec("ratio-from-r-1", abs(ratio_from_r(1.0) - 1.10) < 1e-12)
    # chrono last-15, not balanced
    mixed = (
        [{"t": 1000 + i, "symbol": "A-USDT", "pnl_pct": 0.004} for i in range(20)]
        + [{"t": 2000 + i, "symbol": "B-USDT", "pnl_pct": -0.003} for i in range(15)]
    )
    ch = last_n_chrono(mixed, 15)
    rec("chrono-last15-hot-symbol", all(r["symbol"] == "B-USDT" for r in ch), {r["symbol"] for r in ch})
    ch_pf = last_n_cost_pf(ch, 15, ordered=True)
    rec("chrono-hot-not-positive", not is_positive_pf(ch_pf["ratio"]), ch_pf["ratio"])
    # intern vs real
    coord = Coordinator()
    coord.load({}, {})
    intern_open = 1.00
    rec("intern-neutral-is-1.00-not-1.10", intern_open + 1e-9 >= 1.0 and not is_positive_pf(intern_open))
    rec("coord-stage-floors-1.10", all(abs(float(v) - 1.10) < 1e-9 for v in coord.stage_min_pf.values()), coord.stage_min_pf)
    rec("coord-min-pf-1.10", abs(float(coord.min_pf) - 1.10) < 1e-9, coord.min_pf)
    # DDT uses net pnl_pct
    now = 10_000.0
    dd_rows = [
        {"t": now - 180, "pnl_pct": 0.004, "symbol": "X-USDT"},
        {"t": now - 120, "pnl_pct": -0.003, "symbol": "X-USDT"},
        {"t": now - 60, "pnl_pct": 0.004, "symbol": "X-USDT"},
    ]
    dd = drawdown_time(dd_rows, now=now)
    rec("ddt-episode-from-pct", int(dd.get("episodes") or 0) >= 1 and float(dd.get("maxS") or 0) >= 60, dd)
    rec("ddt-units-are-net-frac", abs(row_equity_pnl(dd_rows[0]) - net_pnl_pct(0.004)) < 1e-12, row_equity_pnl(dd_rows[0]))
    # Raw formula identity — no last_n_cost_pf in the expected value.
    mixed_tape = (
        [{"t": i, "pnl_pct": 0.004, "symbol": "W"} for i in range(8)]
        + [{"t": 100 + i, "pnl_pct": -0.006, "symbol": "L"} for i in range(7)]
    )
    raw_ratio, raw_r, raw_classic, raw_n = raw_cost_pf(mixed_tape, 15, 0.10)
    lib_m = last_n_cost_pf(mixed_tape, 15)
    rec("raw-formula-matches-lib", abs(raw_ratio - float(lib_m["ratio"])) < 1e-6, {"raw": raw_ratio, "lib": lib_m["ratio"], "r": raw_r, "classic": raw_classic, "n": raw_n})
    rec("raw-avg-r-matches-lib", abs(raw_r - float(lib_m["avgR"])) < 1e-4, {"raw": raw_r, "lib": lib_m["avgR"]})
    rec("raw-classic-matches-lib", abs(raw_classic - float(lib_m["classicPf"])) < 1e-4, {"raw": raw_classic, "lib": lib_m["classicPf"]})
    rec("raw-window-last5", abs(raw_cost_pf(mixed_tape, 5, 0.10)[0] - float(evaluation_windows(mixed_tape)["last5"]["pf"])) < 1e-6)
    rec("raw-window-last15", abs(raw_ratio - float(evaluation_windows(mixed_tape)["last15"]["pf"])) < 1e-6)
    # intern 0.0 must not be treated as missing
    allow0, _, m0 = coord.gate([{"t": i, "pnl_pct": 0.002} for i in range(15)], 0, intern={"pf": 0.0, "n": 15})
    rec("intern-zero-pf-preserved", abs(float(m0.get("internPf") or 0) - 0.0) < 1e-12, m0.get("internPf"))
    rec("intern-zero-not-open", not bool((coord.last or {}).get("stages", {}).get("intern", {}).get("open")), (coord.last or {}).get("stages", {}).get("intern"))
    allow1, _, m1 = coord.gate([{"t": i, "pnl_pct": 0.002} for i in range(15)], 0, intern={"pf": 1.0, "n": 15})
    rec("intern-open-at-neutral", bool((coord.last or {}).get("stages", {}).get("intern", {}).get("open")), (coord.last or {}).get("stages", {}).get("intern"))
    rec("intern-1.00-does-not-satisfy-real", not is_positive_pf(1.0))
    # hist_calc production must not subsample last-N for PF
    hist_src = open(os.path.join(os.path.dirname(__file__), "..", "server", "pulse", "hist_calc.py"), encoding="utf-8").read()
    rec("hist-calc-prod-chrono", "last_n_balanced(" not in hist_src.split("def self_test", 1)[0])
    rec("set-engine-gates-chrono", "last_n_chrono" in open(os.path.join(os.path.dirname(__file__), "..", "server", "pulse", "set_engine.py"), encoding="utf-8").read())
    return rows


def raw_cost_pf(closed: List[Dict[str, Any]], n: int = 15, cost: float = POSITION_COST_PCT_DEFAULT) -> tuple:
    """Hand PF from the contract: signed_r = (gross_move_pct − cost) / cost; ratio = 1 + signed_r × 0.10."""
    seq = last_n_chrono(closed, n)
    rs: List[float] = []
    nets: List[float] = []
    gp = gl = 0.0
    for r in seq:
        pnl_pct = float(r.get("pnl_pct") or 0)
        c = float(r.get("position_cost_pct") or r.get("costPct") or cost)
        if c <= 0:
            c = cost
        signed = (pnl_pct * 100.0 - c) / c
        net = pnl_pct - (c / 100.0 if c > 0.02 else c)
        rs.append(signed)
        nets.append(net)
        if net > 0:
            gp += net
        elif net < 0:
            gl += abs(net)
    avg_r = sum(rs) / len(rs) if rs else 0.0
    ratio = 1.0 + avg_r * 0.10
    classic = (gp / gl) if gl > 0 else (99.0 if gp > 0 else 0.0)
    return round(ratio, 4), round(avg_r, 4), round(classic, 4), len(seq)


def hand_pf(closed: List[Dict[str, Any]], n: int = 15, cost: float = POSITION_COST_PCT_DEFAULT) -> Dict[str, Any]:
    seq = last_n_chrono(closed, n)
    rs = [signed_result_r(float(r.get("pnl_pct") or 0), float(r.get("position_cost_pct") or r.get("costPct") or cost)) for r in seq]
    avg_r = sum(rs) / len(rs) if rs else 0.0
    return {
        "n": len(seq),
        "handRatio": round(ratio_from_r(avg_r), 4),
        "libRatio": last_n_cost_pf(seq, n, cost, ordered=True).get("ratio"),
        "avgR": round(avg_r, 4),
        "symbols": list(dict.fromkeys(str(r.get("symbol") or "") for r in seq)),
        "pnlPct": [round(float(r.get("pnl_pct") or 0), 6) for r in seq],
        "ts": [float(r.get("t") or 0) for r in seq],
    }


def audit_lane(conn: str) -> Dict[str, Any]:
    st = get_json("/stats.json?conn=" + conn)
    cfg = get_json("/config.json?conn=" + conn)
    ov = cfg.get("overlay") or {}
    closed = [c for c in (st.get("closed") or []) if isinstance(c, dict)]
    pc = st.get("pfCost") or {}
    sets = st.get("sets") or {}
    coord_snap = st.get("coord") or (st.get("coverage") or {}).get("coord") or {}
    hand = hand_pf(closed, 15)
    lib = last_n_cost_pf(closed, 15)
    windows = evaluation_windows(closed, required_samples=8)
    recent = recent_closed_rows(closed)
    recent_pf = last_n_cost_pf(recent, 15) if recent else {"ratio": None, "count": 0}
    coord = Coordinator()
    coord.load({}, ov)
    allow, reasons, metrics = coord.gate(closed, 0)
    desk_pf = pc.get("ratio") if pc.get("ratio") is not None else st.get("pf")
    match = abs(float(hand["handRatio"] or 0) - float(lib.get("ratio") or 0)) < 1e-6
    desk_match = desk_pf is None or abs(float(desk_pf) - float(lib.get("ratio") or 0)) < 1e-3
    floors = {
        "overlayMinPf": ov.get("minPf"),
        "overlayBase": ov.get("baseMinPf"),
        "overlayMain": ov.get("mainMinPf"),
        "overlayReal": ov.get("realMinPf"),
        "overlayDca": ov.get("dcaMinPf"),
        "overlayExit": ov.get("exitMinPf"),
        "setsMinPf": sets.get("minPf"),
        "setsEnablePf": sets.get("enablePf"),
        "coordMinPf": (coord_snap.get("gate") or coord_snap).get("minPf") if isinstance(coord_snap, dict) else None,
    }
    floor_ok = all(abs(float(v) - 1.10) < 1e-6 for k, v in floors.items() if v is not None and k != "coordMinPf")
    sl_mismatch = 0
    for c in closed:
        sl_pct = float(c.get("sl_pct") or 0)
        tp_pct = float(c.get("tp_pct") or 0)
        sl_ratio = float(c.get("sl_ratio") or 0)
        if sl_pct > 0 and tp_pct > 0 and sl_ratio > 0:
            if abs(sl_pct - tp_pct * sl_ratio) > 1e-4 and abs(tp_pct - sl_pct * 3.0) > 1e-4:
                sl_mismatch += 1
    ages = [time.time() - float(c.get("t") or 0) for c in closed if float(c.get("t") or 0) > 1e9]
    raw_ratio, raw_r, raw_classic, raw_n = raw_cost_pf(closed, 15)
    raw_match = desk_pf is None or abs(raw_ratio - float(desk_pf)) < 1e-3
    desk_windows = (pc.get("evaluationWindows") or {})
    window_match = True
    window_diff: Dict[str, Any] = {}
    for k, v in windows.items():
        dw = desk_windows.get(k) or {}
        if dw.get("pf") is None:
            continue
        desk_n = int(dw.get("n") or dw.get("count") or 0)
        lib_n = int(v.get("n") or 0)
        # stats.closed is a truncated newest window (40/80). last50/75 on the
        # desk use the full retained tape, so n must match before comparing PF.
        if desk_n and lib_n and desk_n != lib_n:
            continue
        if abs(float(dw.get("pf") or 0) - float(v.get("pf") or 0)) > 1e-3:
            window_match = False
            window_diff[k] = {"desk": dw.get("pf"), "lib": v.get("pf"), "deskN": desk_n, "libN": lib_n}
    double = {"grossOk": 0, "alreadyNet": 0, "skipped": 0, "mismatchN": 0, "samples": []}
    for c in closed:
        qty = float(c.get("qty") or 0)
        entry = float(c.get("entry") or 0)
        notion = qty * entry
        pnl_pct = c.get("pnl_pct")
        if pnl_pct is None or notion <= 1e-12:
            double["skipped"] += 1
            continue
        pnl_pct_f = float(pnl_pct)
        pnl = float(c.get("pnl") or 0)
        ccost = float(c.get("position_cost_pct") or c.get("costPct") or POSITION_COST_PCT_DEFAULT)
        cost_frac = ccost / 100.0 if ccost > 0.02 else ccost
        recon_gross = pnl / notion + cost_frac
        if abs(recon_gross - pnl_pct_f) < 8e-4:
            double["grossOk"] += 1
        elif abs((pnl / notion) - pnl_pct_f) < 8e-4:
            double["alreadyNet"] += 1
            if len(double["samples"]) < 4:
                double["samples"].append({"symbol": c.get("symbol"), "pnl_pct": pnl_pct_f, "pnlOverN": pnl / notion, "recon": recon_gross})
        else:
            double["mismatchN"] += 1
            if len(double["samples"]) < 4:
                double["samples"].append({"symbol": c.get("symbol"), "pnl_pct": pnl_pct_f, "recon": recon_gross, "pnl": pnl, "notion": notion})
    set_issues = []
    set_rows = [r for r in (sets.get("rows") or []) if isinstance(r, dict)]
    for r in set_rows:
        pf = float(r.get("last15Ratio") or 0)
        n15 = int(r.get("last15N") or 0)
        intern = r.get("intern") or {}
        w15 = ((r.get("evaluationWindows") or {}).get("last15") or {}).get("pf")
        if w15 is not None and abs(float(w15) - pf) > 1e-3:
            set_issues.append({"kind": "window", "id": r.get("id"), "last15": pf, "window": w15})
        intern_pf = intern.get("pf15")
        if intern_pf is not None and abs(float(intern_pf) - pf) > 1e-3:
            set_issues.append({"kind": "intern", "id": r.get("id"), "last15": pf, "intern": intern_pf})
        expect_val = n15 >= 8 and pf + 1e-9 >= 1.10
        if bool(r.get("validated")) != expect_val:
            set_issues.append({"kind": "validated", "id": r.get("id"), "validated": r.get("validated"), "expect": expect_val, "pf": pf, "n": n15})
    pfs = [float(r.get("last15Ratio") or 0) for r in set_rows]
    cost_vals = [float(c.get("position_cost_pct") or c.get("costPct") or 0) for c in closed]
    measured = [c for c in cost_vals if c > 0]
    cost_units_ok = all(0.02 < c <= 1.0 for c in measured) if measured else True
    return {
        "running": st.get("running"),
        "nClosed": len(closed),
        "equity": st.get("equity"),
        "available": st.get("available"),
        "deskPf": desk_pf,
        "deskClassic": pc.get("classicPf"),
        "deskCostPct": pc.get("costPct"),
        "deskCostSource": pc.get("costSource"),
        "hand": hand,
        "rawLast15": {"pf": raw_ratio, "avgR": raw_r, "classic": raw_classic, "n": raw_n},
        "libLast15": {"pf": lib.get("ratio"), "n": lib.get("count"), "netAvg": lib.get("netAvg"), "classic": lib.get("classicPf"), "avgR": lib.get("avgR")},
        "handMatchesLib": match,
        "deskMatchesLib": desk_match,
        "rawMatchesDesk": raw_match,
        "windows": {k: {"n": v.get("n"), "pf": v.get("pf"), "validated": v.get("validated")} for k, v in windows.items()},
        "deskWindowsMatch": window_match,
        "windowDiff": window_diff,
        "recent3h": {"n": len(recent), "pf": recent_pf.get("ratio"), "newestAgeS": min(ages) if ages else None},
        "coordAllow": allow,
        "coordReasons": reasons,
        "coordStages": (coord.last or {}).get("stages"),
        "floors": floors,
        "floorsAre1_10": floor_ok,
        "slTpMismatches": sl_mismatch,
        "doubleCost": double,
        "pnlPctIsGross": double["alreadyNet"] == 0 and double["mismatchN"] == 0,
        "costUnitsOk": cost_units_ok,
        "setPreview": {
            "n": len(set_rows),
            "issues": set_issues,
            "topPf": pfs[0] if pfs else None,
            "maxPf": max(pfs) if pfs else None,
            "validatedFlags": sum(1 for r in set_rows if r.get("validated")),
            "ok": not set_issues,
        },
        "sets": {
            "ready": sets.get("ready"),
            "setCount": sets.get("setCount") or sets.get("count"),
            "validatedCount": sets.get("validatedCount"),
            "activeCount": sets.get("activeCount"),
            "livePf": sets.get("livePf") or (sets.get("liveOverview") or {}).get("last15Ratio"),
            "histFills": sets.get("histFills"),
            "minPf": sets.get("minPf"),
            "enablePf": sets.get("enablePf"),
        },
        "progress": {
            "ready": st.get("progressReady"),
            "done": st.get("progressSymbolsDone"),
            "total": st.get("progressSymbolsTotal"),
            "detail": st.get("progressDetail"),
        },
    }


def main() -> int:
    report: Dict[str, Any] = {"startedAt": time.time(), "desk": DESK, "floor": POSITIVE_PF}
    ids = identities()
    report["identities"] = ids
    id_fail = [r for r in ids if not r["ok"]]
    print("identities", f"{len(ids) - len(id_fail)}/{len(ids)}", "fail", [r["name"] for r in id_fail], flush=True)
    for conn in ("live", "vst"):
        try:
            lane = audit_lane(conn)
        except Exception as exc:
            lane = {"error": str(exc)}
        report[conn] = lane
        print(json.dumps({"conn": conn, "deskPf": lane.get("deskPf"), "lib": (lane.get("libLast15") or {}).get("pf"),
                          "raw": (lane.get("rawLast15") or {}).get("pf"),
                          "match": lane.get("deskMatchesLib"), "hand": lane.get("handMatchesLib"),
                          "rawMatch": lane.get("rawMatchesDesk"), "windows": lane.get("deskWindowsMatch"),
                          "gross": lane.get("pnlPctIsGross"), "costUnits": lane.get("costUnitsOk"),
                          "double": lane.get("doubleCost"), "setPreview": lane.get("setPreview"),
                          "floors": lane.get("floorsAre1_10"), "floorsRaw": lane.get("floors"),
                          "recent3h": lane.get("recent3h"), "coordAllow": lane.get("coordAllow"),
                          "slTp": lane.get("slTpMismatches"), "sets": lane.get("sets"),
                          "progress": lane.get("progress")}, default=str), flush=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print("wrote", OUT, flush=True)
    ok = (not id_fail
          and all(isinstance(report.get(c), dict) and not report[c].get("error") for c in ("live", "vst"))
          and all(report[c].get("handMatchesLib") for c in ("live", "vst"))
          and all(report[c].get("deskMatchesLib") for c in ("live", "vst"))
          and all(report[c].get("rawMatchesDesk") for c in ("live", "vst"))
          and all(report[c].get("pnlPctIsGross") for c in ("live", "vst"))
          and all(report[c].get("costUnitsOk") for c in ("live", "vst"))
          and all(report[c].get("deskWindowsMatch") for c in ("live", "vst"))
          and all((report[c].get("setPreview") or {}).get("ok", True) for c in ("live", "vst")))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print("wrote", OUT, flush=True)
    ok = (not id_fail
          and all(isinstance(report.get(c), dict) and not report[c].get("error") for c in ("live", "vst"))
          and all(report[c].get("handMatchesLib") for c in ("live", "vst"))
          and all(report[c].get("deskMatchesLib") for c in ("live", "vst")))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
