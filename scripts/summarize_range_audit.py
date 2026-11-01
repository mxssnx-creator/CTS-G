"""Render one line per Set/range and group summaries from the saved audit."""
import argparse
from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path


def pf(rows, stage):
    qualified = [r[stage] for r in rows if r[stage]["qualified"]]
    gain = sum(r["gain"] for r in qualified)
    loss = sum(r["loss"] for r in qualified)
    return f"{gain / loss:.6f}" if loss else "no-loss" if gain else "—"


def cell(stage):
    value = stage["pf"]
    return f"{stage['n']} / {value:.6f}" if value is not None else f"{stage['n']} / {stage['pfState']}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    if args.out.exists():
        ap.error("report exists; preserve prior evidence")
    raw = args.rows.read_bytes()
    raw = gzip.decompress(raw) if args.rows.suffix == ".gz" else raw
    rows = [json.loads(line) for line in raw.splitlines()]
    summary = json.loads(args.summary.read_text())
    groups = defaultdict(list)
    for row in rows:
        groups[(row["pack"], row["kind"], row["lane"])].append(row)
    text = ["# Independent stage/range accounting — 1 November 2026", "",
            "Research only. These are shadow closes, not venue orders or funded positions. The independent implementation imports no engine code; signals, candidate entries and exits/holds are still supplied by the engine.", "",
            f"Checked {summary['independentRangeAudit']['parity']['sets']} Sets, {len(rows)} fixed-range rows; {len(summary['independentRangeAudit']['parity']['differences'])} differences against the existing adaptive production chain. The fixed ranges below are separate experiments, not simultaneous production order lanes.", "",
            "Base Last-50 + Last-25; Main long Last-25 with short Last-5, Last-10 and long-reference Last-25; Negative Last-5 and Last-10; Real Last-15. Configured short Last-15 is excluded by 25 × 0.5 = 12.5 (integer cap 12), not silently substituted. Static minimal 1–3 windows are off; at least one measurable result remains necessary.", "",
            "Eight symbols, 85 h available; full requested stage context is 108.5 h and is NOT covered. Paper cost is the unchanged configured 0.12% round-trip estimate, not a retrieved account fee. Current production admission remains adaptive candidate selection. No actual CTS-A-O Signals or -Active position types are established by this audit.", "",
            "PF is positive net-return sum / absolute negative net-return sum. Empty, flat and no-loss are separate. Selected endpoint PF is not subsequent execution PF. Each Set may share entry opportunities with other Sets: never sum these rows as portfolio positions. A fixed range's Main/Real history is its own causal upstream-processed tape, not the raw Base tape.", "",
            "## Per strategy, type and range", "",
            "Each percentage uses the Sets in that row as denominator. PF pools only qualified Sets' corresponding window samples. Counts are shadow paper closes; missing funded positions remain unavailable.", "",
            "| Strategy | Type | Range lane | Sets | Base qualified / % / PF | Main evaluated / qualified / % / PF | Real evaluated / qualified / % / PF |",
            "| --- | --- | --- | ---: | --- | --- | --- |"]
    for (pack, kind, lane), items in sorted(groups.items()):
        n = len(items)
        passed = {s: sum(r[s]["qualified"] for r in items) for s in ("base", "main", "real")}
        evaluated = {s: sum(r[s]["evaluated"] for r in items) for s in ("main", "real")}
        text.append(f"| {pack} | {kind} | {lane} | {n} | {passed['base']} / {100 * passed['base'] / n:.4f}% / {pf(items, 'base')} | {evaluated['main']} / {passed['main']} / {100 * passed['main'] / n:.4f}% / {pf(items, 'main')} | {evaluated['real']} / {passed['real']} / {100 * passed['real'] / n:.4f}% / {pf(items, 'real')} |")
    text += ["", "## Every Set and range, line by line", "",
             "`N / PF` describes the available window even when its preceding stage has not passed; the evaluated/qualified flags control its interpretation. `Raw/Main/Real closes` are counts in the current lookback, not order submissions. Subsequent Real-admitted shadow results are in the machine-readable archive and are not a funded portfolio simulation.", "",
             "| Set | Range lane | Base qualifies | Base N / PF | Main evaluated / qualifies | Main N / PF | Real evaluated / qualifies | Real N / PF | Raw/Main/Real closes |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    flag = lambda value: "yes" if value else "no"
    for r in rows:
        b, m, real = r["base"], r["main"], r["real"]
        counts = r["shadowCloses"]
        text.append(f"| {r['set']} | {r['lane']} | {flag(b['qualified'])} | {cell(b)} | {flag(m['evaluated'])}/{flag(m['qualified'])} | {cell(m)} | {flag(real['evaluated'])}/{flag(real['qualified'])} | {cell(real)} | {counts['base']}/{counts['main']}/{counts['real']} |")
    args.out.write_text("\n".join(text) + "\n")
    if args.rows.suffix != ".gz":
        compressed = args.rows.with_suffix(args.rows.suffix + ".gz")
        if compressed.exists():
            raise ValueError("compressed evidence exists")
        compressed.write_bytes(gzip.compress(raw, mtime=0))
        if gzip.decompress(compressed.read_bytes()) != raw:
            raise ValueError("evidence compression mismatch")
        summary["independentRangeAudit"]["file"] = compressed.name
        summary["independentRangeAudit"]["uncompressedSha256"] = hashlib.sha256(raw).hexdigest()
        args.summary.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"rows": len(rows), "strategyTypeRanges": len(groups), "report": str(args.out)}))


if __name__ == "__main__":
    main()
