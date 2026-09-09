"""Exact recent market replay using retained candles and public gap recovery.

No credentials, trading calls, settings writes or synthetic fallback. Run in a
separate copy; output is evidence, never a live profile selection.
"""
import argparse
from collections import Counter
import hashlib
import html
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
    parser.add_argument("--hours", type=int, default=12, help="Evaluation hours; 60 extra minutes are used as warmup")
    parser.add_argument("--end-ms", type=int, help="Exclusive closed-minute boundary for reproducible replay")
    args = parser.parse_args()
    if not 1 <= args.hours <= 98:
        raise ValueError("hours must be between 1 and 98")
    out = pathlib.Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    end = args.end_ms or int(time.time() // 60) * 60000
    if end % 60000 or end > int(time.time() // 60) * 60000:
        raise ValueError("End must be a closed-minute boundary")
    warmup_bars = 60
    evaluation_bars = args.hours * 60
    start = end - (evaluation_bars + warmup_bars) * 60000
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
    overlay = dict(histEnabled=True, histLookbackBars=evaluation_bars, histMinBars=evaluation_bars, histWarmup=warmup_bars, histExactWindow=True,
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
                picked = book.entry_sets(pack, side)
                assert len(picked) == len({st.id for st in picked})
                assert all(st.kind == "base" for st in picked)
                assert all(st.last15_n >= book.eval_need() and st.last15_ratio >= threshold for st in picked)
                assert all(book.execution_allowed(st, pack, side) for st in picked)
                scopes[pack + ":" + side] = len(picked)
        qualification[str(threshold)] = scopes
    for scope in qualification["1.05"]:
        counts = [row[scope] for row in qualification.values()]
        assert counts == sorted(counts, reverse=True), "Higher PF floor widened the qualified set"
    top_rows = []
    for st in sorted(
        (row for row in book.by_idx if row.kind == "base"),
        key=lambda row: (-float(row.last15_ratio or 0), float(row.max_dd_s or 0), row.idx),
    )[:100]:
        top_rows.append({
            "id": st.id, "pack": st.pack, "kind": st.kind, "side": "BOTH",
            "slRatio": round(st.sl_ratio, 4), "tpPct": round(st.tp_pct * 100, 4),
            "step": st.step, "last15N": st.last15_n, "last15PF": round(st.last15_ratio, 4),
            "expectancy": round(st.expectancy, 8), "maxDdS": round(st.max_dd_s, 3),
            "volumeRatio": round(st.volume_ratio, 6), "stage": st.stage,
            "validated": bool(st.last15_n >= book.eval_need() and st.last15_ratio >= 1.0),
            "active": bool(st.active),
        })
    summary = dict(source="retained and public exchange-confirmed 1m candles", evaluationHours=args.hours,
                   evaluationStartMs=end-evaluation_bars*60000, endMs=end, warmupBars=warmup_bars,
                   marketData=evidence, catalogSets=len(book.by_idx),
                   symbolConfigEvaluations=len(book.by_idx)*len(prepared), elapsedS=round(time.monotonic()-started, 2),
                   peakRssMiB=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024, 1),
                   activeSets=sum(st.active for st in book.by_idx), historySamples=sum(len(st.hist) for st in book.by_idx),
                   qualifiedEntryLanesByPf=qualification, activeMeaning="calculation enabled; qualified entry counts are separate",
                   indications={kind: len(book.ind_hist.get(kind, [])) for kind in IND_KINDS},
                   indicationConfigs={kind: sorted({r.get("ind_config") for r in tape if r.get("ind_config")}) for kind, tape in book.ind_hist.items()},
                   strategies={key: len(tape) for key, tape in book.strategy_hist.items()}, exits=dict(reasons),
                   topBaseRows=top_rows,
                   overviewBytes=len(json.dumps(overview, allow_nan=False)), overviewGroups=len(overview["groups"]),
                   progress={"ready": book.progress.ready, "done": book.progress.symbols_done, "total": book.progress.symbols_total},
                   exchangeOrdersSubmitted=0, profitabilityClaim=False)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False))
    report = out / "report.html"
    rows_html = "".join(
        "<tr>" + "".join(
            f"<td>{html.escape(str(row.get(key, '—')))}</td>"
            for key in ("pack", "id", "step", "slRatio", "tpPct", "last15N", "last15PF", "maxDdS", "volumeRatio", "stage", "validated", "active")
        ) + "</tr>"
        for row in top_rows
    )
    report.write_text(
        "<!doctype html><html lang='de'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>CTS-G · historischer Validierungslauf</title><style>body{background:#0b1421;color:#e6eef8;font:14px/1.5 system-ui;margin:0}main{max-width:1500px;margin:auto;padding:24px}section{background:#142337;border-radius:10px;padding:18px;margin:16px 0;overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border-bottom:1px solid #38506a;padding:7px;text-align:left;white-space:nowrap}pre{white-space:pre-wrap;overflow-wrap:anywhere}.ok{color:#7ee2a8}.note{border-left:4px solid #eac46e;padding-left:12px}</style><main>"
        f"<h1>CTS-G · {args.hours}h historischer Lauf</h1>"
        "<p class='note'>Nur abgeschlossene, exchange-bestätigte 1m-Kerzen; keine Orders, keine Settings-Schreibvorgänge und keine synthetischen Daten. Jede Base-Konfiguration bleibt eine unabhängige Lane.</p>"
        f"<section><h2>Abdeckung</h2><pre>{html.escape(json.dumps({k: summary[k] for k in ('evaluationHours','evaluationStartMs','endMs','warmupBars','marketData','catalogSets','symbolConfigEvaluations','elapsedS','peakRssMiB','activeSets','historySamples','qualifiedEntryLanesByPf','progress')}, ensure_ascii=False, indent=2))}</pre></section>"
        f"<section><h2>Indikationen, Strategien und Exit-Zählung</h2><pre>{html.escape(json.dumps({'indications':summary['indications'],'indicationConfigs':summary['indicationConfigs'],'strategies':summary['strategies'],'exits':summary['exits']}, ensure_ascii=False, indent=2))}</pre></section>"
        f"<section><h2>Top 100 Base-Rows</h2><p class='ok'>Entry policy: validated Base only; trailing rows are not new-entry sources. Existing retained live lineages are a separate processing concern.</p><table><thead><tr>"
        "<th>Pack</th><th>Set</th><th>Step</th><th>SL ratio</th><th>TP %</th><th>N</th><th>PF</th><th>DDT s</th><th>Volume</th><th>Stage</th><th>Validated</th><th>Active</th>"
        f"</tr></thead><tbody>{rows_html}</tbody></table></section>"
        f"<section><h2>Vollständige JSON-Daten</h2><pre>{html.escape(json.dumps(summary, ensure_ascii=False, indent=2))}</pre></section></main></html>",
        encoding="utf-8",
    )
    print(json.dumps(summary, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
