#!/usr/bin/env python3
"""Walk-forward 20h windows over a long 1m tape, one account sim per window.

Slices the cached candles (``<data-dir>/*/SYMBOL.json`` or ``<data-dir>/SYMBOL.json``,
fetch_sim_window format) into consecutive windows of ``--window-hours`` that end
at each step, each with ``--pre-hours`` of pre-history for the engine replay
lookback, runs ``scripts/sim_12h_account.py`` on every window (in parallel)
and writes one summary: per window the account sum, PF, trades per hour,
positive hours, the exit mix and the trade-level stats of the unfiltered and
admitted Set trades.

Usage
  python3 scripts/sim_wf_windows.py --data-dir /tmp/claude-0/wf60 --root /tmp/claude-0/wfroot \\
      --window-hours 20 --pre-hours 24 --first-day 1 --last-day 40 --jobs 2 --out summary.json \\
      -- --start-equity 10 --sensitivity-leverage 0
Everything after ``--`` is passed to sim_12h_account.py.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H_MS = 3_600_000


def tape_paths(data_dir: str) -> Dict[str, str]:
    paths = sorted(glob.glob(os.path.join(data_dir, "*.json"))) or sorted(glob.glob(os.path.join(data_dir, "*", "*.json")))
    return {os.path.basename(p)[:-5]: p for p in paths}


def slice_window(src: Dict[str, str], out_dir: str, end_ms: int, span_h: int) -> List[str]:
    """Write SYMBOL.json files holding the closed 1m bars in [end - span, end)."""
    os.makedirs(out_dir, exist_ok=True)
    start_ms = end_ms - span_h * H_MS
    done = []
    for sym, path in src.items():
        blob = json.load(open(path))
        rows = [r for r in blob.get("rows") or [] if start_ms <= int(r[0]) < end_ms]
        if len(rows) < span_h * 60 * 0.98:
            continue  # gap in the tape: leave the symbol out of this window
        # one row per minute, no gaps (the sim indexes bars by position)
        by_t = {int(r[0]): r for r in rows}
        t, last, filled = start_ms, None, []
        while t < end_ms:
            r = by_t.get(t)
            if r is None and last is not None:
                c = float(last[1][3])
                r = [t, [c, c, c, c, 0.0]]
            if r is not None:
                filled.append(r)
                last = r
            t += 60_000
        if len(filled) != span_h * 60:
            continue
        out = {k: v for k, v in blob.items() if k != "rows"}
        out.update(start=start_ms, end=end_ms, rows=filled)
        with open(os.path.join(out_dir, f"{sym}.json"), "w") as fh:
            json.dump(out, fh)
        done.append(sym)
    return done


def window_summary(sim: Dict) -> Dict:
    out = {"window": sim.get("window"), "symbols": len(sim.get("symbols") or [])}
    for r in sim.get("runs") or []:
        if r.get("run") not in ("post-base", "unfiltered"):
            continue
        t = r.get("totals") or {}
        hours = r.get("hourly") or []
        pos_h = sum(1 for h in hours if float(h.get("pnl") or 0) > 0)
        act_h = sum(1 for h in hours if int(((h.get("closed") or {}).get("n")) or 0) > 0)
        cl = t.get("closed") or {}
        out[r["run"]] = {
            "pnl": t.get("pnl"), "returnPct": t.get("returnPct"), "fees": t.get("fees"),
            "realizedGross": t.get("realizedGross"), "closed": cl.get("n"), "pf": cl.get("pfNormal"),
            "costPf": cl.get("costPf"), "winRate": cl.get("winRate"),
            "entries": (t.get("orders") or {}).get("entry"), "entriesPerHour": round(float((t.get("orders") or {}).get("entry") or 0) / max(1, len(hours)), 2),
            "closeFills": (t.get("orders") or {}).get("closeFills"), "hoursPositive": pos_h, "hoursActive": act_h, "hours": len(hours),
            "skipped": t.get("skipped"), "candidates": t.get("candidates"), "ddMaxPct": t.get("ddMaxPct"),
            "liquidations": len(t.get("liquidations") or []),
        }
    tl = sim.get("tradeLevel") or {}
    out["tradeLevel"] = {k: v for k, v in tl.items() if not k.startswith("per distinct") and "axis" not in k}
    out["gate"] = sim.get("gate")
    return out


def run_window(args, end_ms: int, sim_args: List[str]) -> Dict:
    tag = time.strftime("w%m%d-%H", time.gmtime(end_ms / 1000))
    wdir = os.path.join(args.root, tag)
    data = os.path.join(wdir, "data")
    span = args.window_hours + args.pre_hours + 1
    if not (os.path.isdir(data) and len(glob.glob(os.path.join(data, "*.json"))) >= 2):
        slice_window(tape_paths(args.data_dir), data, end_ms, span)
    out_json = os.path.join(wdir, "sim.json")
    if args.force or not os.path.exists(out_json):
        env = dict(os.environ, CTS_DATA_DIR=os.path.join(wdir, "_data"), PYTHONDONTWRITEBYTECODE="1")
        os.makedirs(env["CTS_DATA_DIR"], exist_ok=True)
        cmd = [sys.executable, os.path.join(ROOT, "scripts", "sim_12h_account.py"), "--data-dir", data,
               "--hours", str(args.window_hours), "--out", out_json, "--html", os.path.join(wdir, "sim.html"),
               "--cache", os.path.join(wdir, "_cache")]
        if args.contracts:
            cmd += ["--contracts", args.contracts]
        cmd += sim_args
        with open(os.path.join(wdir, "log.txt"), "w") as log:
            rc = subprocess.call(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)
        if rc != 0 or not os.path.exists(out_json):
            return {"tag": tag, "endMs": end_ms, "error": f"sim rc={rc}, see {wdir}/log.txt"}
    res = window_summary(json.load(open(out_json)))
    res.update(tag=tag, endMs=end_ms)
    return res


def main() -> int:
    argv = sys.argv[1:]
    sim_args: List[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, sim_args = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--root", required=True, help="working dir (one sub dir per window)")
    ap.add_argument("--window-hours", type=int, default=20)
    ap.add_argument("--pre-hours", type=int, default=24)
    ap.add_argument("--step-hours", type=int, default=0, help="default = window hours (no overlap)")
    ap.add_argument("--first-day", type=float, default=0.0, help="first window end, in days from the tape start (after pre-history)")
    ap.add_argument("--last-day", type=float, default=1e9, help="last window end, in days from the tape start")
    ap.add_argument("--max-windows", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--contracts", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    src = tape_paths(args.data_dir)
    if not src:
        sys.exit("no data")
    first_path = next(iter(src.values()))
    rows = json.load(open(first_path))["rows"]
    t0 = (int(rows[0][0]) // H_MS + 1) * H_MS
    t1 = (int(rows[-1][0]) // H_MS) * H_MS + H_MS
    step = (args.step_hours or args.window_hours) * H_MS
    first_end = t0 + (args.pre_hours + args.window_hours + 1) * H_MS
    ends = []
    e = max(first_end, t0 + int(args.first_day * 24) * H_MS)
    while e <= t1 and (e - t0) / (24 * H_MS) <= args.last_day:
        ends.append(e)
        e += step
    if args.max_windows:
        ends = ends[: args.max_windows]
    os.makedirs(args.root, exist_ok=True)
    print(f"{len(ends)} windows of {args.window_hours}h", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for r in pool.map(lambda e: run_window(args, e, sim_args), ends):
            pb = r.get("post-base") or {}
            print(f"{r['tag']}: pnl={pb.get('pnl')} pf={pb.get('pf')} entries={pb.get('entries')} "
                  f"hours+={pb.get('hoursPositive')}/{pb.get('hours')} {r.get('error', '')}", flush=True)
            results.append(r)
    ok = [r for r in results if "post-base" in r]
    agg = {}
    for run in ("post-base", "unfiltered"):
        rs = [r[run] for r in ok if run in r]
        if not rs:
            continue
        pnl = [float(x.get("pnl") or 0) for x in rs]
        agg[run] = {"windows": len(rs), "windowsPositive": sum(1 for v in pnl if v > 0), "sumPnl": round(sum(pnl), 4),
                    "medianPnl": sorted(pnl)[len(pnl) // 2], "entries": sum(int(x.get("entries") or 0) for x in rs),
                    "hoursPositive": sum(int(x.get("hoursPositive") or 0) for x in rs), "hours": sum(int(x.get("hours") or 0) for x in rs)}
    blob = {"generatedAt": time.time(), "args": vars(args), "simArgs": sim_args, "aggregate": agg, "windows": results}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump(blob, open(args.out, "w"), indent=1)
    print(json.dumps(agg, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
