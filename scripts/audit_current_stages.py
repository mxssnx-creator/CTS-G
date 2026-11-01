"""Replay the unchanged installed overlay on archived bars, without a trader/API.

This is one cold SetBook pass, not a funded execution simulation or live proof.
Each process loads an explicitly selected engine tree for before/after parity.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import gzip
import json
import math
from pathlib import Path
import resource
import sys
import time


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def returns(values):
    gain = sum(v for v in values if v > 0)
    loss = -sum(v for v in values if v < 0)
    return {"n": len(values), "gain": gain, "loss": loss,
            "pf": gain / loss if loss else None,
            "pfState": "finite" if loss else "no-loss" if gain else "flat" if values else "empty"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine-dir", type=Path, required=True)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--symbols", type=int, default=8)
    ap.add_argument("--ranges-out", type=Path, help="independent per-Set/range stage accounting as JSONL")
    args = ap.parse_args()
    if args.out.exists():
        ap.error("output already exists; preserve the prior audit")
    if args.ranges_out and args.ranges_out.exists():
        ap.error("ranges output already exists")
    engine = args.engine_dir.resolve()
    sys.path.insert(0, str(engine))
    import set_engine as se
    overlay_path = engine / "overlay-bingx-x01.json"
    overlay = json.loads(overlay_path.read_text())
    names = sorted(p.stem for p in args.data.glob("*-USDT.json"))[:args.symbols]
    if len(names) != args.symbols:
        ap.error("requested symbol coverage is unavailable")
    files = sorted(engine.glob("*.py")) + [overlay_path]
    code_hashes = {p.name: checksum(p) for p in files}
    data_hashes = {name: checksum(args.data / f"{name}.json") for name in names}
    book = se.SetBook()
    book.load(overlay)
    range_rows = []
    parity = {"sets": 0, "differences": []}
    if args.ranges_out:
        from independent_range_audit import reconstruct
        production = book.set_processed

        def audited_processed(st, full, now=None):
            production(st, full, now=now)
            if now is None:
                raise ValueError("range accounting requires an explicit decision clock")
            ts = [row[0] for row in full]
            entries = [row[0] - row[5] for row in full]
            values = [row[4] - book.cost_pct / 100 for row in full]
            legacy, records = reconstruct(ts, entries, values, asdict(book.stage_params(st.pack)), now)
            parity["sets"] += 1
            for key, actual in (("main", st.proc), ("real", st.exe), ("realneg", st.neg)):
                expected = [(ts[i], values[i]) for i in legacy[key]][-se.PROC_KEEP:]
                if actual != expected:
                    parity["differences"].append({"set": st.id, "stream": key})
            if st.main_short_win != legacy["short_n"]:
                parity["differences"].append({"set": st.id, "stream": "short_n"})
            for record in records:
                range_rows.append({"set": st.id, "pack": st.pack, "kind": st.kind, "costPct": book.cost_pct, **record})
        book.set_processed = audited_processed
    starts, ends = [], []
    for name in names:
        raw = json.loads((args.data / f"{name}.json").read_text())
        ts = [int(row[0]) / 1000 for row in raw]
        bars = [[float(v) for v in row[1:6]] for row in raw]
        if (not ts or any(b - a != 60 for a, b in zip(ts, ts[1:]))
                or any(len(row) != 5 or not all(math.isfinite(v) for v in row)
                       or min(row[:4]) <= 0 for row in bars)):
            ap.error(f"invalid chronological OHLCV coverage: {name}")
        book.bars[name], book.bar_t[name] = bars, ts[-1]
        starts.append(ts[0]); ends.append(ts[-1] + 60)
    if len(set(starts)) != 1 or len(set(ends)) != 1:
        ap.error("symbol coverage differs")
    before = {"sets": len(book.by_idx), "packs": list(book.packs), "lookbackBars": book.lookback,
              "contextBars": book.history_bars(), "baseLong": book.gate_window, "baseShort": book.base_short,
              "minimumSamples": book.gate_min, "mainLong": book.main_n,
              "mainShortCandidates": list(book.short_windows), "realLastN": book.realst_n,
              "lastNMode": "one selected Main short candidate per Set, not independent candidate lanes",
              "signalSourcePacks": [p for p in book.packs if se.is_signal_pack(p)], "costPct": book.cost_pct}
    print(json.dumps({"start": before, "symbols": names}), flush=True)
    last = time.monotonic()
    def heartbeat():
        nonlocal last
        if time.monotonic() - last > 20:
            print(f"replay {book.progress.phase}: symbols={book.progress.symbols_done}/{len(names)}", flush=True)
            last = time.monotonic()
    started = time.perf_counter()
    book.replay_all(now=ends[0], expected_symbols=names, on_step=heartbeat)
    elapsed = time.perf_counter() - started
    base = [st for st in book.by_idx if not book.base_validation_reason(st)]
    main = [st for st in base if book._main_pf_pass(st)]
    positive = [st for st in book.by_idx if book.validation_lane(st) == "pos"]
    negative = [st for st in book.by_idx if book.validation_lane(st) == "neg"]
    groups = {"base": (base, lambda st: [r["pnl_pct"] - book.cost_pct / 100 for r in st.hist[-book.gate_window:]]),
              "main": (main, lambda st: [v for _, v in st.proc[-book.main_n:]]),
              "real": (positive, lambda st: [v for _, v in st.exe[-book.realst_n:]]),
              "realNegative": (negative, lambda st: [v for _, v in st.neg[-book.realst_n:]])}
    figures = {key: {"sets": len(states), "pctAll": 100 * len(states) / len(book.by_idx),
                     **returns([v for st in states for v in get_values(st)])}
               for key, (states, get_values) in groups.items()}
    scope_start = time.perf_counter()
    scopes = book.real_paper_scopes(200)
    scope_ms = 1000 * (time.perf_counter() - scope_start)
    result = {"scope": "single cold paper replay of the unchanged x01 overlay; not venue/funded execution or forward profitability",
              "symbols": names, "dataStart": starts[0], "decisionTime": ends[0],
              "availableHours": (ends[0] - starts[0]) / 3600,
              "fullRequestedContextAvailable": all(start <= ends[0] - before["contextBars"] * 60 for start in starts),
              "initialConfiguration": before, "finalLookbackBars": book.lookback,
              "historyPending": book._lookback_pending, "progress": asdict(book.progress),
              "postBaseValidatedPct": figures["base"]["pctAll"], "stages": figures,
              "baseRejectionReasons": dict(Counter(book.base_validation_reason(st) or "pass" for st in book.by_idx)),
              "entryRejectionReasons": dict(Counter(book.gate_reason(st) or "pass" for st in book.by_idx)),
              "paperRows": sum(st.n for st in book.by_idx),
              "paperScopes": {key: returns([r["pnl"] for r in rows]) for key, rows in scopes.items()},
              "scopeMilliseconds": scope_ms, "coldReplaySeconds": elapsed,
              "peakRssKiB": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "engineSha256": code_hashes, "dataSha256": data_hashes,
              "execution": {"tested": False, "ordersSent": 0, "livePf": None}}
    if args.ranges_out:
        result["independentRangeAudit"] = {"parity": parity, "rows": len(range_rows),
            "scope": "independent accounting and stage decisions on engine-generated shadow candidates; not independent signals/exits or funded execution",
            "file": args.ranges_out.name}
        encoded = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in range_rows).encode()
        args.ranges_out.write_bytes(gzip.compress(encoded, mtime=0) if args.ranges_out.suffix == ".gz" else encoded)
    if code_hashes != {p.name: checksum(p) for p in files}:
        raise RuntimeError("engine changed during the audit")
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in ("postBaseValidatedPct", "stages", "historyPending", "coldReplaySeconds", "peakRssKiB")}), flush=True)
    return 1 if book.progress.errors or parity["differences"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
