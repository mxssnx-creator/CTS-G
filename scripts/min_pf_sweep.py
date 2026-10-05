#!/usr/bin/env python3
"""Walk-forward effect of the shared evaluation minimum PF on entry selectivity and PF.

For each floor the stage chain of the 12h simulation (Base last-30, Main last-5,
Real last-3, drawdown time) is rerun on the existing replay cache with all seven
stage floors set to that value, and the admitted Set trades are scored net of
PositionCost: share of entry candidates admitted, PF normal, cost PF, win rate and
net result per trade. Only the floors change between rows; evidence is walk-forward
(closes strictly before each entry bar), so the numbers are out of sample per row.

Run it on several windows before trusting a floor: the effect can flip sign
between consecutive 12h windows (see reports/min-pf-sweep.md).

    python3 scripts/min_pf_sweep.py --data-dir DATA --cache CACHE \
        --floors 1.02,1.10,1.20,1.30 --window-ends 0,9360 --out sweep.jsonl

``--window-ends`` are bar indexes (0 = end of data); the cache must hold replay
closes for the window (a fresh ``sim_12h_account.py`` run builds it for the final
12h only, so earlier windows return no candidates).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "server", "pulse"))

STAGE_KEYS = ("minPf", "baseMinPf", "mainMinPf", "realMinPf", "setMinPf", "dcaMinPf", "exitMinPf")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--cache", required=True, help="replay cache built by sim_12h_account.py")
    ap.add_argument("--overlay", default=os.path.join(ROOT, "server", "pulse", "overlay-bingx-x02.json"))
    ap.add_argument("--floors", default="1.02,1.10,1.20,1.30")
    ap.add_argument("--ddt-s", default="", help="comma list of setMaxDdTimeS values (seconds, 600..57600); empty keeps the overlay value")
    ap.add_argument("--window-ends", default="0", help="comma list of bar indexes, 0 = end of data")
    ap.add_argument("--hours", type=int, default=12)
    ap.add_argument("--out", required=True, help="JSON lines, one row per window and floor")
    args = ap.parse_args()
    os.environ.setdefault("CTS_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(args.out)), "sweep-data"))

    import connection_profile as cp
    import sim_12h_account as sim

    sim._engine_path()
    symbols = sorted(os.path.basename(f)[:-5] for f in glob.glob(os.path.join(args.data_dir, "*.json")))
    n_all = len(sim.load_symbol(args.data_dir, symbols[0])[0])
    original = cp.processing_profile
    ddt_cache: dict = {}
    for end in [int(x) for x in args.window_ends.split(",")]:
        sim_end = end or n_all
        sim_start = sim_end - args.hours * 60
        ddt_values = [float(x) for x in args.ddt_s.split(",") if x] or [None]
        for floor, ddt in [(float(f), d) for f in args.floors.split(",") for d in ddt_values]:
            def profile(floor=floor, ddt=ddt):
                out = original()
                out.update({key: floor for key in STAGE_KEYS})
                if ddt is not None:
                    out.update(setMaxDdTimeS=ddt, maxDdTimeS=ddt)
                return out

            cp.processing_profile = profile
            t0 = time.time()
            book = sim.make_book(args.overlay)
            catalog = sim.build_catalog(book)
            caches = sim.load_cache(args.cache, symbols)
            cands = sim.build_candidates(caches, catalog, symbols, sim_start, sim_end, book, True, ddt_cache)
            cost = float(book.cost_pct)
            n_cand = int(len(cands["uid"]))
            admitted = cands["admitted"]
            row = dict(
                windowEnd=sim_end, windowStart=sim_start, floor=floor, maxDdS=float(book.max_dd_s),
                stageFloors={k: float(v) for k, v in book.stage_min_pf.items()},
                candidates=n_cand, seconds=round(time.time() - t0),
                base_ok=int(cands["base_ok"].sum()), main_ok=int(cands["main_ok"].sum()),
                real_ok=int(cands["real_ok"].sum()), admitted=int(admitted.sum()),
                admittedPct=round(100.0 * int(admitted.sum()) / max(1, n_cand), 3),
                walkForward=sim.tape_metrics(cands, catalog, admitted, cost),
                baseOnly=sim.tape_metrics(cands, catalog, cands["base_ok"], cost),
                unfiltered=sim.tape_metrics(cands, catalog, np.ones(n_cand, bool), cost),
            )
            with open(args.out, "a") as handle:
                handle.write(json.dumps(row) + "\n")
            print("floor", floor, "ddt", book.max_dd_s, "window", sim_end, "admitted", row["admittedPct"], "%", row["walkForward"], flush=True)
            del cands, caches
    cp.processing_profile = original
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
