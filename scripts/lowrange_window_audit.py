#!/usr/bin/env python3
"""Choose per-range last-N rules using only prehistory, then replay the next day.

An exploratory calibration, not an untouched holdout claim. Only stage windows
and the minimum sample count vary; the verified signal/trade tapes stay fixed.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server/pulse"))

ARMS = [
    ("current", None, None),
    ("fast 10/10/4", {"baseLong": 10, "baseShort": 5, "mainLong": 10, "mainShort": [1, 2, 3], "realN": 4, "realNegN": 4}, 8),
    ("middle 20/15/8", {"baseLong": 20, "baseShort": 10, "mainLong": 15, "mainShort": [3, 5], "realN": 8, "realNegN": 8}, 8),
    ("longer 30/20/12", {"baseLong": 30, "baseShort": 15, "mainLong": 20, "mainShort": [5, 10], "realN": 12, "realNegN": 12}, 8),
]


def evaluate(args):
    data, cache, patch_file, rng, arm = args
    patch = json.loads(Path(patch_file).read_text())
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(patch)
    import stage_eval as sg
    import replay_cache
    from coord_engine import Coordinator
    from position_cost import tp_range_of
    from stack_eval import summarize
    label, windows, minimum = arm
    ov = sg.replay_overlay()
    names, bars, times = sg.load_bars(data, 12)
    shadow = replay_cache.load(cache, replay_cache.fingerprint(data, names, ov, sg.PULSE, sg.__file__))
    b = sg.make_book()
    # These changes cannot alter entries/exits of a paper Set. They only
    # determine which finished paper windows may admit an executed position.
    if windows:
        b.last_n_by_range = {**b.last_n_by_range, rng: windows}
    if minimum is not None:
        b.gate_min = minimum
    b.by_idx = [s for s in b.by_idx if s.kind == "base" and tp_range_of(s.step) == rng]
    tapes = sg.Tapes(b, names, times, shadow)
    coordinator = Coordinator(); coordinator.load({}, ov)
    end = int(min(times[s][-1] + 60 for s in names))
    boundary = end - 24 * 3600
    results = {}
    for name, hours in (("training", list(range(boundary - 12 * 3600, boundary, 3600))),
                        ("forward", list(range(boundary, end, 3600)))):
        rows = sg.execute(b, names, bars, times, shadow, tapes, hours, refresh_s=0, include_open=True, axis_coord=coordinator)
        trades = [r for r in rows if "net" in r and r["t_in"] < hours[-1] + 3600]
        marked = [r["mark_net"] for r in rows if r.get("open_at_end") and r["t_in"] < hours[-1] + 3600]
        from trade_sim import acc_pf
        results[name] = {**summarize([(r["t"], r["net"], 1) for r in trades], len(trades), 0, hours),
                         "marked_pf": acc_pf([r["net"] for r in trades] + marked), "open": len(marked)}
    return {"range": rng, "label": label, "windows": windows, "minimum": minimum, **results}


def main():
    p = argparse.ArgumentParser()
    for key in ("data", "cache", "patch-file", "out"):
        p.add_argument("--" + key, required=True)
    a = p.parse_args()
    began = time.perf_counter()
    tasks = [(a.data, a.cache, a.patch_file, rng, arm) for rng in ("micro", "minimal", "short") for arm in ARMS]
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(evaluate, tasks))
    chosen = []
    for rng in ("micro", "minimal", "short"):
        candidates = [r for r in results if r["range"] == rng]
        def score(r):
            s = r["training"]
            n = s["parents"]
            # The forward outcomes are deliberately not used for selection.
            return (n >= 12 and (s["marked_pf"] or 0) > 1,
                    min(s["marked_pf"] or 0, 5) * n / (n + 25) / (1 + s["max_dd_pct"]))
        chosen.append(max(candidates, key=score))
    Path(a.out).write_text(json.dumps({"scope": "exploratory; chosen by previous 12 hours only; forward is next 24 hours; each range is an independent portfolio",
                                     "results": results, "chosen": chosen, "seconds": time.perf_counter() - began}, indent=2))
    for r in chosen:
        print(json.dumps({"range": r["range"], "label": r["label"], "training_pf": r["training"]["pf"],
                          "forward_pf": r["forward"]["pf"], "marked_pf": r["forward"]["marked_pf"], "orders": r["forward"]["parents"]}), flush=True)


if __name__ == "__main__":
    main()
