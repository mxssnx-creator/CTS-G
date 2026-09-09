"""Exact recent market replay using retained candles and public gap recovery.

No credentials, trading calls, settings writes or synthetic fallback. Run in a
separate copy; output is evidence, never a live profile selection.
"""
import argparse
from collections import Counter
import hashlib
import json
import pathlib
import resource
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from fetch_historic_window import fetch, validate
from set_engine import SetBook, IND_KINDS
from set_overview import build_overview


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--symbols", nargs="+", default=["XRP-USDT", "BCH-USDT", "SOL-USDT"])
    parser.add_argument("--end-ms", type=int, help="Exclusive closed-minute boundary for reproducible replay")
    args = parser.parse_args()
    out = pathlib.Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    end = args.end_ms or int(time.time() // 60) * 60000
    if end % 60000 or end > int(time.time() // 60) * 60000:
        raise ValueError("End must be a closed-minute boundary")
    start = end - 780 * 60000
    history = json.loads(pathlib.Path(args.history).read_text()).get("symbols", {})
    prepared = {}
    evidence = []
    for symbol in args.symbols:
        tape = history.get(symbol, {})
        rows = [(minute * 60000, tape[str(minute)]["bar"]) for minute in range(start // 60000, end // 60000)
                if str(minute) in tape and tape[str(minute)].get("closed") and tape[str(minute)].get("quality") == "exchange-confirmed"]
        reused = len(rows)
        missing = sorted(set(range(start, end, 60000)) - {t for t, _ in rows})
        if missing:
            # One bounded public interval; known candles are retained.
            recovered = dict(fetch(symbol, missing[0], missing[-1] + 60000))
            recovered.update(rows)
            rows = sorted(recovered.items())
        validate(rows, start, end)
        raw = json.dumps(rows, separators=(",", ":"), allow_nan=False).encode()
        (out / (symbol + ".candles.json")).write_bytes(raw)
        prepared[symbol] = [bar for _, bar in rows]
        evidence.append({"symbol": symbol, "bars": len(rows), "reusedBars": reused, "sha256": hashlib.sha256(raw).hexdigest()})
    book = SetBook()
    overlay = dict(histEnabled=True, histLookbackBars=720, histMinBars=720, histWarmup=60, histExactWindow=True,
                   stratGeneral=True, stratIndications=True, stratTrailing=True, stratBlock=True, stratDca=True,
                   histSimulateBlock=True, histSimulateDca=True, setMinStep=3, setStepMax=30,
                   setMinPf=1.05, realMinPf=1.05, setAutoDeact=False, setMaxActive=0,
                   slMinPct=.15, slMaxPct=3, tpMinPct=.3, tpMaxPct=0)
    book.load(overlay)
    for symbol, bars in prepared.items():
        book.ingest_bars(symbol, bars)
    started = time.monotonic()
    book.replay_all(now=(end - 60000) / 1000, workers=2,
                    on_symbol=lambda sym, done, total: print(json.dumps({"symbol": sym, "done": done, "total": total}), flush=True))
    if book.progress.error or not book.progress.ready:
        raise RuntimeError("Replay incomplete: " + str(book.progress.error))
    assert len({st.id for st in book.by_idx}) == len(book.by_idx)
    assert {st.step for st in book.by_idx} == set(range(3, 31))
    assert {st.pack for st in book.by_idx} == {"general", "indications"}
    assert {st.kind for st in book.by_idx} == {"base", "trail"}
    assert all(.003 <= st.tp_pct for st in book.by_idx)
    assert all(st.last15_ratio >= 0 and st.max_dd_s >= 0 for st in book.by_idx)
    overview = build_overview(book)
    reasons = Counter(row.get("reason") for st in book.by_idx for row in st.hist)
    qualification = {}
    for threshold in (1.05, 1.10, 1.20, 1.35):
        book.real_min_pf = threshold
        scopes = {}
        for pack in ("general", "indications"):
            for side in ("LONG", "SHORT"):
                picked = book.pick_all(pack, side)
                assert len(picked) == len({st.id for st in picked})
                assert all(book.execution_allowed(st, pack, side) for st in picked)
                scopes[pack + ":" + side] = len(picked)
        qualification[str(threshold)] = scopes
    for scope in qualification["1.05"]:
        counts = [row[scope] for row in qualification.values()]
        assert counts == sorted(counts, reverse=True), "Higher PF floor widened the qualified set"
    summary = dict(source="retained and public exchange-confirmed 1m candles", evaluationStartMs=end-720*60000,
                   endMs=end, warmupBars=60, marketData=evidence, catalogSets=len(book.by_idx),
                   symbolConfigEvaluations=len(book.by_idx)*len(prepared), elapsedS=round(time.monotonic()-started, 2),
                   peakRssMiB=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024, 1),
                   activeSets=sum(st.active for st in book.by_idx), historySamples=sum(len(st.hist) for st in book.by_idx),
                   qualifiedEntryLanesByPf=qualification, activeMeaning="calculation enabled; qualified entry counts are separate",
                   indications={kind: len(book.ind_hist.get(kind, [])) for kind in IND_KINDS},
                   indicationConfigs={kind: sorted({r.get("ind_config") for r in tape if r.get("ind_config")}) for kind, tape in book.ind_hist.items()},
                   strategies={key: len(tape) for key, tape in book.strategy_hist.items()}, exits=dict(reasons),
                   overviewBytes=len(json.dumps(overview, allow_nan=False)), overviewGroups=len(overview["groups"]),
                   progress={"ready": book.progress.ready, "done": book.progress.symbols_done, "total": book.progress.symbols_total},
                   exchangeOrdersSubmitted=0, profitabilityClaim=False)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False))
    print(json.dumps(summary, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
