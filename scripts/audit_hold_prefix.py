#!/usr/bin/env python3
"""Compare archived-data auto-hold trades before a cutoff with and without later candles."""
import argparse
import bisect
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PULSE = HERE.parent / "server" / "pulse"
sys.path.insert(0, str(PULSE))
import set_engine as se  # noqa: E402
import stage_eval as sg  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--preset", required=True)
    ap.add_argument("--cut", required=True, type=int)
    ap.add_argument("--symbols", default="ADA-USDT,AVAX-USDT")
    ap.add_argument("--sets", default="indications:1m:sl0.75:st5,indications:1m:tr0.36:0.36")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    names, bars, times = sg.load_bars(args.data, 100)
    chosen = args.symbols.split(",")
    ids = args.sets.split(",")
    if not set(chosen) <= set(names):
        raise ValueError("requested symbols absent")
    overlay = json.loads((PULSE / "overlay-bingx-x01.json").read_text())
    overlay.update(json.loads(Path(args.preset).read_text()))

    def make_book(cropped):
        b = se.SetBook(); b.load(overlay)
        b.by_idx = [st for st in b.by_idx if st.id in ids]
        if {st.id for st in b.by_idx} != set(ids):
            raise ValueError("requested Set absent")
        b.sets = {st.id: st for st in b.by_idx}
        for sym in chosen:
            end = bisect.bisect_left(times[sym], args.cut) if cropped else len(times[sym])
            if end < b.lookback or any(y - x != 60 for x, y in zip(times[sym][:end], times[sym][1:end])):
                raise ValueError(f"insufficient or noncontiguous bars: {sym}")
            b.bars[sym] = bars[sym][:end]
            b.bar_t[sym] = times[sym][end - 1]
            out = {sid: [] for sid in b.sets}
            info = b._replay_symbol(sym, out, b.bar_t[sym] + 60)
            b._hist_rows[sym] = {sid: se.RowCols(rows) for sid, rows in out.items() if rows}
            b._hist_inputs[sym] = info
        return b

    full, prefix = make_book(False), make_book(True)
    checked = {}
    for sid in ids:
        a, b = full.sets[sid], prefix.sets[sid]
        arows = full._choose_hold(a, full._tape_rows(sid, chosen, float("-inf")), chosen, float("-inf"))
        brows = prefix._choose_hold(b, prefix._tape_rows(sid, chosen, float("-inf")), chosen, float("-inf"))
        expected = [r for r in arows if r[0] < args.cut]
        got = [r for r in brows if r[0] < args.cut]
        checked[sid] = {"fullPrefixCloses": len(expected), "croppedCloses": len(got), "identical": expected == got}
    sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    result = {"scope": "two-symbol archived-data prefix check; no venue or execution-parity claim",
              "cut": args.cut, "symbols": chosen, "sets": checked,
              "sourceSha256": {"set_engine.py": sha(PULSE / "set_engine.py"), "script": sha(__file__), "preset": sha(args.preset)},
              "marketSha256": {sym: sha(Path(args.data) / f"{sym}.json") for sym in chosen}}
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    if not all(v["identical"] and v["croppedCloses"] for v in checked.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
