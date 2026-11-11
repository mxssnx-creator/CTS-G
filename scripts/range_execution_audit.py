#!/usr/bin/env python3
"""Counterfactual execution of each low range/type, on the same frozen snapshot.

These portfolios are independent experiments; their counts/returns must not be
added to the combined portfolio. They expose eligible ranges masked by ranking.
"""
import argparse
import json
import os
from pathlib import Path
import resource
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server/pulse"))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--patch-file", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--axis-current", action="store_true")
    a = p.parse_args()
    patch = json.loads(Path(a.patch_file).read_text())
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(patch)
    import stage_eval as sg
    import replay_cache
    from position_cost import tp_range_of
    from stack_eval import summarize
    began = time.perf_counter()
    ov = sg.replay_overlay()
    names, bars, times = sg.load_bars(a.data, 12)
    shadow = replay_cache.load(a.cache, replay_cache.fingerprint(a.data, names, ov, sg.PULSE, sg.__file__))
    b = sg.make_book()
    tapes = sg.Tapes(b, names, times, shadow)
    end = min(times[s][-1] + 60 for s in names)
    hours = list(range(int(end) - 24 * 3600, int(end), 3600))
    all_sets = b.by_idx
    results = []
    coordinator = None
    if a.axis_current:
        from coord_engine import Coordinator
        coordinator = Coordinator()
        coordinator.load({}, ov)
    for kind in ("base", "exit", "trail"):
        for rng in ("micro", "minimal", "short"):
            b.by_idx = [s for s in all_sets if s.kind == kind and tp_range_of(b.step_bucket(s)) == rng]
            rows = sg.execute(b, names, bars, times, shadow, tapes, hours, refresh_s=0, include_open=True, axis_coord=coordinator) if b.by_idx else []
            trades = [r for r in rows if "net" in r and r["t_in"] < end]
            result = {"type": kind, "range": rng, "sets": len(b.by_idx),
                      **summarize([(r["t"], r["net"], 1) for r in trades], len(trades), 0, hours),
                      "open_at_end": sum(bool(r.get("open_at_end")) for r in rows),
                      "ao_orders": sum(r["pack"].startswith("extra.ao_lr") for r in trades)}
            results.append(result)
            print(json.dumps({k: v for k, v in result.items() if k != "hourly"}), flush=True)
    b.by_idx = all_sets
    Path(a.out).write_text(json.dumps({"scope": "independent counterfactual portfolios, not additive", "axis": a.axis_current, "results": results,
                           "seconds": time.perf_counter() - began, "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}, indent=2))


if __name__ == "__main__":
    main()
