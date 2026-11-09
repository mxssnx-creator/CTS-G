#!/usr/bin/env python3
"""Offline Micro study and small-account feasibility. Never starts a trader.

Freeze the matrix before scoring, screen on 12 symbols in the middle day,
then confirm the frozen finalist and baseline on all 80 in the last day.
No configuration is promoted to a live connection by this script.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import micro_short_study as study


def no_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise RuntimeError("This study is offline; network access is prohibited")


def plans(base, maker, taker):
    ranges = {k: k == "micro" for k in ["micro", "minimal", "short", "normal", "long", "wide"]}
    common = copy.deepcopy(base)
    common.update(symbolCap=80, volumeFactor=.05, strictSizeCap=True,
                  tpRanges=ranges, trailRanges=ranges, setMinStep=2, setStepMax=3,
                  shortRangeStepMin=2, shortRangeStepMax=3, holdAuto=False,
                  histLookbackBars=1440, histLookbackMax=1440, histLookbackAuto=False,
                  setSlRatios=[i / 4 for i in range(1, 11)], minPfStepAuto=False,
                  makerFeeRate=maker, takerFeeRate=taker, slippageFeeRatio=.1)
    # Let the canonical fee policy derive the cost from these quoted rates.
    common.pop("positionCostPct", None)
    groups = []
    for label, fee in [("public-baseline", (maker, taker)), ("fallback-baseline", (.0005, .0008))]:
        o = copy.deepcopy(common)
        o.update(makerFeeRate=fee[0], takerFeeRate=fee[1], indicationConfigs=[],
                 stepHoldOn=[False] * 12)
        groups.append({"id": label, "overlay": o, "profiles": [{"id": label, "patch": {}}]})
    for source in ["core", "momentum", "reversal"]:
        for hold in [15, 60]:
            for step in [2, 3]:
                o = copy.deepcopy(common)
                o.update(setMinStep=step, setStepMax=step, shortRangeStepMin=step, shortRangeStepMax=step,
                         exitIgnoreTp=False, stepHoldOn=[True] * 12, stepHoldMin=[hold] * 12,
                         indicationConfigs=[])
                if source != "core":
                    families = ["burstgo", "momchg", "vwapx", "mpull"] if source == "momentum" else ["sweep", "wickrev", "burstfade", "exhaust", "volclimax"]
                    o["indicationConfigs"] = [{"label": f"Micro {source} {relation or 'independent'}",
                        "names": [f"{family}_n5_t1" for family in families], "relations": [relation],
                        "stepMin": step, "stepMax": step, "slRatios": [i / 4 for i in range(1, 11)],
                        "types": ["base"], "major": "micro", "minor": f"{source}-{relation or 'own'}"}
                        for relation in ["", "T5", "B5"]]
                ident = f"{source}-hold{hold}-step{step}"
                profiles = []
                for label, bl, ml, rn in [("fast", 10, 10, 5), ("medium", 20, 15, 8)]:
                    patch = {"lastNByRange": {"micro": {"baseLong": bl, "baseShort": bl // 2,
                        "mainLong": ml, "mainShort": [ml // 2], "realN": rn, "realNegN": rn,
                        "negWindows": [5, 10]}}}
                    profiles.append({"id": f"{ident}-{label}", "patch": patch})
                groups.append({"id": ident, "overlay": o, "profiles": profiles})
    return groups


def drawdown(values):
    running = peak = worst = 0.0
    for value in values:
        running += value
        peak = max(peak, running)
        worst = max(worst, peak - running)
    return 100 * worst


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()
    if not 1 <= args.jobs <= 6:
        ap.error("--jobs must be 1..6")
    sys.addaudithook(no_network)
    import stage_eval as sg
    import replay_cache
    from coord_engine import Coordinator
    data, out = Path(args.market), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    meta = json.loads((data / "_meta.json").read_text())
    assert len(meta["symbols"]) == 80 and meta["hours"] == 72
    rates = {(r["maker_rate"], r["taker_rate"]) for r in meta["checks"]}
    if len(rates) != 1:
        raise ValueError("Heterogeneous quoted fees need per-symbol simulation; no averaging permitted")
    maker, taker = next(iter(rates))
    names, bars, times = sg.load_bars(str(data), 80)
    assert set(names) == set(meta["symbols"])
    for name in names:
        assert len(times[name]) == 4320 and times[name] == times[names[0]]
        assert all(b - a == 60 for a, b in zip(times[name], times[name][1:]))
        assert replay_cache.file_sha(data / (name + ".json")) == next(r["sha256"] for r in meta["checks"] if r["symbol"] == name)
    development = sorted(meta["symbols"][:12])
    start, end = times[names[0]][0], times[names[0]][-1] + 60
    groups = plans(json.loads(Path(sg.OVERLAY).read_text()), maker, taker)
    protocol = {"schema": 1, "mode": "offline-only", "accountAuthenticated": False,
        "studySourceSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "exchangeOrdersSubmitted": 0, "symbolCount": 80, "developmentSymbols": development,
        "confirmationSymbols": names, "prehistory": [start, start + 86400],
        "development": [start + 86400, start + 172800], "confirmation": [start + 172800, end],
        "paperBalance": 3, "volumeFactor": .05, "strictSizeCap": True,
        "feeBasis": "Public contract quote; account-specific fees, funding and fill slippage are unverified",
        "selection": "Same loss-aware eligibility as micro_short_study; tie by lower settled unit-return drawdown, then count",
        "priorDataOverlap": "This follow-up partly overlaps earlier research; confirmation is chronological, not a pristine prospective holdout",
        "scope": "Native parent simulation with Axis; excludes Block/DCA added volume, account allocation and venue fills",
        "sourceFingerprint": replay_cache.fingerprint(str(data), names, {}, sg.PULSE, sg.__file__),
        "groups": groups}
    path = out / "protocol.json"
    if path.exists():
        assert json.loads(path.read_text()) == protocol, "Frozen study inputs changed"
    else:
        path.write_text(json.dumps(protocol, indent=2))
    begun = time.perf_counter()

    def progress(**values):
        values.update(elapsedSeconds=time.perf_counter() - begun, updatedAt=time.time(),
                      mode="offline-only", exchangeOrdersSubmitted=0)
        tmp = out / "status.tmp"
        tmp.write_text(json.dumps(values, indent=2)); tmp.replace(out / "status.json")
        print(json.dumps(values), flush=True)

    def build(group, symbols, label):
        os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(group["overlay"])
        b = sg.make_book()
        assert not b.config_report["dropped"], b.config_report["dropped"]
        fp = replay_cache.fingerprint(str(data), symbols, sg.replay_overlay(), sg.PULSE, sg.__file__)
        cache = str(out / f"{label}-{group['id']}.pkl")
        progress(phase="building native tapes", group=group["id"], symbols=len(symbols))
        shadow = replay_cache.load(cache, fp) if Path(cache).exists() else sg.build(symbols, bars, times, args.jobs)
        if not Path(cache).exists(): replay_cache.save(cache, shadow, fp)
        return shadow, [(s.id, s.tp_pct, s.hold_bars) for s in b.by_idx]

    def run(group, profile, symbols, shadow, topology, hours, suffix):
        ident = profile["id"] + suffix
        path = out / (ident + ".json")
        if path.exists(): return json.loads(path.read_text())
        overlay = {**group["overlay"], **profile["patch"]}
        os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(overlay)
        b = sg.make_book()
        assert topology == [(s.id, s.tp_pct, s.hold_bars) for s in b.by_idx]
        tapes = sg.Tapes(b, symbols, times, shadow)
        got, bad = sg.self_check(b, tapes, hours, samples=20)
        assert bad == 0, (ident, got, bad)
        coordinator = Coordinator(); coordinator.load({}, sg.replay_overlay())
        rows = sg.execute(b, symbols, bars, times, shadow, tapes, hours,
                          refresh_s=0, include_open=True, axis_coord=coordinator)
        closed = sorted([r for r in rows if "net" in r], key=lambda r: (r["t"], r["symbol"], r["set"]))
        held = [r for r in rows if r.get("open_at_end")]
        middle = hours[len(hours) // 2]
        halves = [study.metric([r["net"] for r in closed if (r["t"] <= middle) == first]) for first in [True, False]]
        snaps = []
        for at in hours:
            base = execute = orders = 0; gp = gl = 0.0
            for st in b.by_idx:
                ok, _, _, _ = sg.gate_base(b, tapes, st, at)
                if ok:
                    e = sg.base_eval(b, tapes, st, at)
                    nets = [x - e["cf"] for x in e["w"]]
                    base += 1; orders += len(nets)
                    gp += sum(max(0, x) for x in nets); gl += sum(max(0, -x) for x in nets)
                execute += bool(sg.gate_metrics(b, tapes, st, at)[0])
            settled = [r["net"] for r in closed if at < r["t"] <= at + 3600]
            snaps.append({"t": at, "evaluated": len(b.by_idx), "basePassed": base, "executable": execute,
                "postBasePf": gp / gl if gl else None, "postBaseNoLoss": bool(gp and not gl),
                "postBaseObservations": orders, "settled": study.metric(settled)})
        rr = {"id": ident, "symbols": symbols, "costPct": b.cost_pct, "sets": len(b.by_idx),
            "closed": study.metric([r["net"] for r in closed]),
            "marked": study.metric([r["net"] for r in closed] + [r["mark_net"] for r in held]),
            "held": len(held), "halves": halves, "stageSnapshots": snaps,
            "settledUnitDrawdownPct": drawdown([r["net"] for r in closed]),
            "engineParity": {"samples": got, "mismatches": bad},
            "effectiveWindows": asdict(b.win(b.by_idx[0])) if b.by_idx else None,
            "trades": [{k: r[k] for k in ["t", "t_in", "symbol", "set", "pack", "kind", "lane", "reason", "net"]} |
                {"side": r["tr"]["dir"], "entry": r["tr"]["entry_px"], "exit": r["tr"]["exit_px"]} for r in closed],
            "open": held}
        rr["researchEligible"] = bool(study.candidate_rank(rr)[0])
        path.write_text(json.dumps(rr, indent=2))
        progress(phase="scored", profile=ident, symbols=len(symbols), closed=rr["closed"], held=rr["held"],
                 researchEligible=rr["researchEligible"], settledUnitDrawdownPct=rr["settledUnitDrawdownPct"])
        return rr

    results = []
    for group in groups:
        shadow, topology = build(group, development, "development")
        for profile in group["profiles"]:
            rr = run(group, profile, development, shadow, topology,
                     list(range(start + 86400, start + 172800, 3600)), "-development")
            results.append((rr, group, profile))
        del shadow
    def rank(row):
        r = row[0]
        old = study.candidate_rank(r)
        return old[:3] + (-r["settledUnitDrawdownPct"], r["closed"]["n"])
    candidates = [r for r in results if r[1]["id"] != "fallback-baseline"]
    chosen = max(candidates, key=rank)
    frozen = {"chosen": chosen[2]["id"], "developmentEligible": chosen[0]["researchEligible"],
              "diagnosticOnly": not chosen[0]["researchEligible"], "candidateCount": len(candidates),
              "results": [{k: r[k] for k in ["id", "closed", "marked", "halves", "held", "researchEligible", "settledUnitDrawdownPct"]} for r, _, _ in results]}
    fp = out / "frozen-finalist.json"
    if fp.exists(): assert json.loads(fp.read_text()) == frozen
    else: fp.write_text(json.dumps(frozen, indent=2))
    confirmations = []
    controls = [next(row for row in results if row[1]["id"] == label) for label in ["public-baseline", "fallback-baseline"]]
    seen = set()
    for _, group, profile in [chosen, *controls]:
        if profile["id"] in seen: continue
        seen.add(profile["id"])
        shadow, topology = build(group, names, "confirmation")
        confirmations.append(run(group, profile, names, shadow, topology,
            list(range(start + 172800, end, 3600)), "-confirmation"))
        del shadow
    result = {"chosen": frozen["chosen"], "developmentEligible": frozen["developmentEligible"],
              "confirmationEligible": confirmations[0]["researchEligible"],
              "productionPromoted": False, "accountStarted": False,
              "confirmation": [{k: r[k] for k in ["id", "closed", "marked", "halves", "held", "researchEligible", "settledUnitDrawdownPct"]} for r in confirmations]}
    (out / "summary.json").write_text(json.dumps(result, indent=2))
    progress(phase="completed", **result, peakRssMiB=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)


if __name__ == "__main__":
    main()
