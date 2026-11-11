#!/usr/bin/env python3
"""Offline Block sensitivity on frozen native Micro parents; never starts Pulse.

Uses SetBlockBook itself, on completed-minute prices before the parent exit bar.
This is an adaptive historical diagnostic, not a prospective profitability test.
Cash is normalized to one unit of base entry notional, not a funded account.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import stage_eval as sg
import replay_cache
from stack_eval import qualified_parent_lanes
from coord_engine import Coordinator
from set_block import SetBlockBook
from pulse_trader import Position
from set_engine import lane_honors_tp
import block_rules as br


def offline(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise RuntimeError("Offline study: network access prohibited")


def metric(values):
    gain = sum(max(0., v) for v in values)
    loss = sum(max(0., -v) for v in values)
    running = peak = dd = 0.
    for v in values:
        running += v; peak = max(peak, running); dd = max(dd, peak - running)
    return {"n": len(values), "gain": gain, "loss": loss, "pf": gain / loss if loss else None,
            "pfState": "finite" if loss else "no-loss" if gain else "flat" if values else "empty",
            "netUnitPct": 100 * running, "settledUnitDrawdownPct": 100 * dd}


def matrix(overlay):
    cases = [{"id": "block-off", "cfg": None}, {"id": "shipped", "cfg": asdict(br.clean_cfg(overlay))}]
    for stack in ("shared", "additive"):
        for step in (.5, 1.):
            for budget in (.2, .6):
                for relations in (("set",), ("symbol", "step"), ("symbol", "indication", "set", "step")):
                    cfg = br.clean_cfg({"blockStackType": stack, "blockSharedTotal": budget,
                        "blockCounts": [{"step": step, "ratio": budget / 2}] * 2,
                        "blockRelations": relations, "blockSkipFirst": 0,
                        "blockCarry": False, "blockMinObservations": 1, "blockProfitFactorRatio": .8})
                    cases.append({"id": f"{stack}-step{step}-budget{budget}-{'-'.join(relations)}", "cfg": asdict(cfg)})
    return cases


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reference-repo", required=True, type=Path)
    ap.add_argument("--micro-report", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(); sys.addaudithook(offline)
    old = a.reference_repo.resolve(); report = a.micro_report.resolve(); out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    data = old / "evidence/micro-small-account-20261109/market-80"
    primary = data.parent / "study"
    original = json.loads((primary / "protocol.json").read_text())
    group = next(g for g in original["groups"] if g["id"] == "public-baseline")
    cases = matrix(group["overlay"])
    protocol = {"schema": 1, "mode": "offline-only", "accountStarted": False, "exchangeOrdersSubmitted": 0,
        "type": "adaptive historical sensitivity; prior parent outcomes were already inspected",
        "priceConvention": "completed minute closes strictly before parent exit bar; same-clock decisions precede closes",
        "scope": "native SetBlockBook; fixed parent cohort with current paper stages, Axis and recorded parent-close gate at each add; no funding, lot rounding, allocation or venue latency",
        "volume": "one unit of base entry notional; quantity ratios translated to entry-notional weights",
        "held": "marked separately at end; never force-closed or added to qualification evidence",
        "cases": cases, "scriptSha256": replay_cache.file_sha(Path(__file__)),
        "engine": {p.name: replay_cache.file_sha(p) for p in Path(sg.PULSE).glob("*.py")},
        "parentSources": {phase: str(report / folder / filename) for phase, folder, filename in (
            ("development", "lower-pf-followup", "public-baseline-pf1.01-development.json"),
            ("confirmation", "lower-default-confirmation", "public-baseline-pf1.01-confirmation.json"))},
        "promotionRule": "no automatic promotion; positive full-period and both-half PF with >=10 closes each half required even for further consideration"}
    protocol["parentHashes"] = {k: replay_cache.file_sha(v) for k, v in protocol["parentSources"].items()}
    frozen = out / "protocol.json"
    if frozen.exists():
        assert json.loads(frozen.read_text()) == protocol, "Frozen protocol changed"
    else:
        frozen.write_text(json.dumps(protocol, indent=2))
    names, bars, times = sg.load_bars(str(data), 80)
    results = []
    for phase in ("development", "confirmation"):
        source = json.loads(Path(protocol["parentSources"][phase]).read_text())
        symbols = source["symbols"]
        start, end = original[phase]
        # Reuse only our hashed native tape cache, after verifying its original
        # source, data and overlay. Changed modules cannot generate SetLane tapes.
        os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(group["overlay"])
        current_overlay = sg.OVERLAY
        sg.OVERLAY = str(old / "server/pulse/overlay-bingx-x01.json")
        reference_overlay = sg.replay_overlay(); sg.OVERLAY = current_overlay
        fp = replay_cache.fingerprint(str(data), symbols, reference_overlay, str(old / "server/pulse"), str(old / "scripts/stage_eval.py"))
        allowed = {"block_engine.py", "block_rules.py", "set_block.py", "pulse_trader.py"}
        for filename, sha in fp["engine"].items():
            assert filename in allowed or replay_cache.file_sha(Path(sg.PULSE) / filename) == sha, filename
        assert replay_cache.file_sha(sg.__file__) == fp["simulator"]
        shadow = replay_cache.load(str(primary / f"{phase}-public-baseline.pkl"), fp)
        os.environ["STAGE_OVERLAY_PATCH"] = json.dumps({**group["overlay"], "minPfByRange": {"micro": 1.01}})
        book = sg.make_book(); by_id = book.sets
        tapes = sg.Tapes(book, symbols, times, shadow)
        coord = Coordinator(); coord.load({}, sg.replay_overlay())
        cf = book.cost_pct / 100
        assert abs(cf - source["costPct"] / 100) < 1e-15
        parents = [dict(r, held=False) for r in source["trades"]]
        parents += [dict(r, held=True, t=r["t_mark"], exit=r["mark"], net=r["mark_net"],
                         side=1 if r["side"] == "LONG" else -1) for r in source["open"]]
        parents.sort(key=lambda r: (r["t_in"], r["symbol"], r["set"]))
        events = []
        for i, r in enumerate(parents):
            events.append((r["t_in"], 2, i, r["entry"]))
            events.append((r["t"], 1, i, r["exit"]))
            first = int((r["t_in"] - times[r["symbol"]][0]) // 60)
            stop = int((r["t"] - times[r["symbol"]][0]) // 60) - (0 if r["held"] else 1)
            for j in range(first, stop):
                clock = times[r["symbol"]][j] + 60
                if r["t_in"] < clock < r["t"]:
                    events.append((clock, 0, i, bars[r["symbol"]][j][3]))
        events.sort()
        # Pure stage/Axis decisions may be shared across sensitivity cases.
        gate_cache, axis_cache = {}, {}
        def allowed_at(r, at):
            st = by_id[r["set"]]; key = (st.idx, r["lane"], at)
            if key not in gate_cache:
                valid = r["lane"] in qualified_parent_lanes(book, tapes, st, at)
                if valid:
                    valid = sg.axis_judge(book, tapes, coord, st, at, r["lane"], axis_cache)[0]
                gate_cache[key] = bool(valid)
            return gate_cache[key] and book.execution_stats(st, r["lane"], now=math.nextafter(at, -math.inf))["allow"]
        for case in cases:
            begun = time.perf_counter()
            output = out / f"{phase}-{case['id']}.json"
            if output.exists():
                results.append(json.loads(output.read_text())); continue
            for st in book.by_idx:
                st.live = []
            with tempfile.TemporaryDirectory() as temp:
                block = SetBlockBook(str(Path(temp) / "state.json"))
                block.save = lambda: None  # same rules, no disk IO for research ticks
                block.cfg = br.BlockCfg(**case["cfg"]) if case["cfg"] else None
                active, closed, held, fills = {}, [], [], []
                proofs, ledger_checks = 0, 0
                for at, order, i, px in events:
                    r = parents[i]; st = by_id[r["set"]]
                    if order == 2:
                        sl, tp = book.lane_fracs(st); sd = r["side"]; entry = r["entry"]
                        active[i] = Position(r["symbol"], "LONG" if sd > 0 else "SHORT", 1 / entry, entry, at,
                            entry * (1 - sd * sl), entry * (1 + sd * tp), entry,
                            set_id=st.id, lane=r["lane"], client_id=f"research-{i}",
                            base_qty=1 / entry, base_entry=entry, pack=st.pack)
                        continue
                    pos = active[i]
                    if order == 0:
                        if not case["cfg"]: continue
                        floor = book.floor_for(book.win(st).real_n, st.pack, st)
                        honor_tp = lane_honors_tp(st.kind)
                        honor_tp = bool(book.hist_honor_tp) if honor_tp is None else honor_tp
                        candidate = block.candidate(pos, px, at, None, book.cost_pct, book.lookback * 60, floor=floor, honor_tp=honor_tp)
                        if candidate and allowed_at(r, at):
                            while candidate:
                                q = candidate["qty"]
                                pos.block_fills.append({"group": candidate["group"], "qty": q, "price": px, "t": at})
                                pos.entry = (pos.entry * pos.qty + px * q) / (pos.qty + q); pos.qty += q
                                proofs += 1
                                fills.append({"parent": i, "t": at, "symbol": r["symbol"], "set": st.id,
                                              "lane": r["lane"], "group": candidate["group"], "qtyRatio": q / pos.base_qty,
                                              "entry": px, "minPf": candidate["minPf"], "paperPf": candidate["paper"]["pf"],
                                              "executedPf": candidate["executed"]["pf"]})
                                candidate = block.candidate(pos, px, at, None, book.cost_pct, book.lookback * 60, floor=floor, honor_tp=honor_tp)
                        continue
                    # Independent cash oracle; no averaging of leg PFs.
                    sd = r["side"]
                    base = pos.base_qty * ((px - pos.base_entry) * sd - cf * pos.base_entry)
                    assert abs(base - r["net"]) < 1e-10
                    add_values = [f["qty"] * ((px - f["price"]) * sd - cf * f["price"]) for f in pos.block_fills]
                    record = {"parent": i, "t": at, "t_in": r["t_in"], "symbol": r["symbol"], "set": st.id,
                              "kind": st.kind, "pack": st.pack, "lane": r["lane"], "side": pos.side,
                              "base": base, "adds": add_values, "net": base + sum(add_values), "addCount": len(add_values)}
                    if r["held"]:
                        held.append(record)
                    else:
                        closed.append(record)
                        if case["cfg"]:
                            before = {k: block.ledger(k, True)["life"]["net"] for k in block.rel_keys(block.cfg, pos)}
                            tracked = block.parents.get(block.key(pos), {}).get("parent") == block.parent_id(pos)
                            block.close(pos, px, at, None, book.cost_pct)
                            for k, previous in before.items():
                                # A parent with no observed tick has no Block lifecycle.
                                if tracked:
                                    assert abs(block.ledgers[k]["life"]["net"] - previous - record["net"]) < 1e-9, (case["id"], record, k)
                                    ledger_checks += 1
                        st.live.append({"t": at, "lane": r["lane"], "net_pnl_pct": base,
                                        "pnl_pct": base + cf, "fee_source": "modeled", "ownership_explicit": True})
                    del active[i]
                closed.sort(key=lambda r: (r["t"], r["symbol"], r["set"]))
                assert len(closed) == source["closed"]["n"] and len(held) == source["held"]
                mid = (start + end) / 2
                halves = [metric([r["net"] for r in closed if (r["t"] <= mid) == first]) for first in (True, False)]
                hourly = []
                for hour in range(start, end, 3600):
                    subset = [r for r in closed if hour < r["t"] <= hour + 3600]
                    hourly.append({"t": hour, **metric([r["net"] for r in subset]), "adds": sum(r["addCount"] for r in subset)})
                result = {"phase": phase, "id": case["id"], "cfg": case["cfg"], "closed": metric([r["net"] for r in closed]),
                    "base": metric([r["base"] for r in closed]), "adds": metric([v for r in closed for v in r["adds"]]),
                    "marked": metric([r["net"] for r in closed] + [r["net"] for r in held]), "held": len(held),
                    "halves": halves, "hourly": hourly, "trades": closed, "open": held, "fills": fills,
                    "nativeLedgerChecks": ledger_checks, "qualifiedAdds": proofs,
                    "postBaseSnapshots": source["stageSnapshots"], "elapsedSeconds": time.perf_counter() - begun,
                    "furtherResearchEligible": all(h["n"] >= 10 and h["pf"] is not None and h["pf"] >= 1 for h in halves)}
                assert sum(h["n"] for h in hourly) == len(closed)
                output.write_text(json.dumps(result, indent=2)); results.append(result)
                print(json.dumps({k: result[k] for k in ("phase", "id", "closed", "adds", "held", "elapsedSeconds", "furtherResearchEligible")}), flush=True)
        del shadow, tapes, book
    summary = [{k: r[k] for k in ("phase", "id", "closed", "adds", "marked", "held", "halves", "nativeLedgerChecks", "qualifiedAdds", "furtherResearchEligible")} for r in results]
    (out / "summary.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
