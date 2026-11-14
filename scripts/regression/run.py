"""Regression runner. Runs every suite and the engine self-tests, prints PASS / FAIL / SKIP per check.

  python3 scripts/regression/run.py                       run everything (exit 1 on any FAIL)
  python3 scripts/regression/run.py --only processing     run checks whose name starts with the prefix
  python3 scripts/regression/run.py --list                list checks
  python3 scripts/regression/run.py --jobs 4              worker processes (default: CTSG_JOBS, else min(4, cores))
  python3 scripts/regression/run.py --fail-fast           stop at the first FAIL (serial)
  python3 scripts/regression/run.py --update-golden       rewrite golden.json after an intended processing change (serial)
  CTSG_PULSE_DIR=/path/to/server/pulse                    run against another copy (used for fault injection)

--fail-fast runs the checks fastest first, by the durations a full run records in check_durations.json: a fault is found
by its cheapest failing check instead of after every slower check before it (mutate.py runs this way).

Parallel layout: the memoized processing checks form one group, run in order in one process so the replay memo
stays warm. Other checks are balanced by their measured duration over the remaining workers. Results print in the
original order. Checks are forked from this process, so the registry is shared and nothing is pickled.
"""
from __future__ import annotations

import argparse
import importlib
import json
import multiprocessing
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
DURATIONS_PATH = os.path.join(HERE, "check_durations.json")
sys.path.insert(0, HERE)
from common import Skip  # noqa: E402

# logistics is static and fails in well under a second: it runs first so a broken pin or config fails fast
SUITES = ["logistics", "exchange", "demo_endpoints", "relate", "lane_logistics", "sidecar", "contract", "bybit", "trader", "lanes", "grid_and_units", "exits_and_gate", "gates", "lifecycle", "indication", "extra_indicators", "report", "calc", "processing", "stages", "hold_causal", "pause", "ranges", "step_pf", "direction", "speed", "active_modes", "lane_rank", "golden", "correctness", "cost_accounting", "replay_provenance", "replay_runtime", "monitoring", "history_snapshot", "signals", "dca_replay", "protection_policy", "sample_coverage", "relations", "block_structure", "simdiff_checks", "signalpool_checks", "signals_pack"]


def selftests():
    """The engine modules' own self_test() batteries, folded into the same report."""
    out = []
    import common  # noqa: F401  (sets sys.path to the engine)
    for mod in ("set_engine", "indication_engine", "dca_engine", "position_cost", "exit_engine",
                "risk_variants", "stats_report", "coord_engine", "relation_lanes", "block_rounds"):
        m = importlib.import_module(mod)
        fn = getattr(m, "self_test", None)
        if fn is None:
            # no self_test(): the module's own __main__ checks, run as a process; exit 0 is the result
            def run_main(mod=mod):
                import subprocess
                from common import PULSE
                proc = subprocess.run([sys.executable, "-B", os.path.join(PULSE, mod + ".py")],
                                      cwd=PULSE, capture_output=True, text=True, timeout=600)
                tail = (proc.stdout.strip().splitlines() or [""])[-1][:80]
                return (proc.returncode == 0 and "FAIL" not in proc.stdout), f"__main__ exit={proc.returncode} {tail}"
            out.append((f"selftest.{mod}", run_main))
            continue

        def run_one(fn=fn, mod=mod):
            results = fn()
            bad = [name for name, ok, _ in results if not ok]
            return (not bad, f"{len(results)} checks" + (f" FAILED: {bad[:4]}" if bad else ""))
        out.append((f"selftest.{mod}", run_one))
    return out


def _run_one(cname, fn):
    t0 = time.time()
    try:
        ok, detail = fn()
        status = "PASS" if ok else "FAIL"
    except Skip as exc:
        status, ok, detail = "SKIP", True, str(exc)
    except Exception as exc:  # any unexpected error is a failure, with its location
        status, ok = "FAIL", False
        tb = traceback.extract_tb(exc.__traceback__)[-1]
        detail = f"{type(exc).__name__}: {exc} @ {os.path.basename(tb.filename)}:{tb.lineno}"
    return {"name": cname, "status": status, "detail": str(detail), "sec": time.time() - t0}


def _worker(indices, checks, q):
    try:
        for i in indices:
            cname, fn = checks[i]
            q.put((i, {"state": "running", "started": time.monotonic()}))
            q.put((i, _run_one(cname, fn)))
    finally:
        q.put(None)


def _groups(checks, jobs, durations=None):
    """Keep the warm replay chain together and balance remaining work by cost.

    Durations are scheduling hints only: every check still runs exactly once,
    and the parent emits results in registry order. Unknown checks cost one
    second; corrupt timing data must never drop a check or break a run.
    """
    if not checks:
        return []
    if durations is None:
        try:
            with open(DURATIONS_PATH, encoding="utf-8") as stream:
                durations = json.load(stream)
        except (OSError, ValueError):
            durations = {}
    if not isinstance(durations, dict):
        durations = {}

    def cost(i):
        import math
        try:
            value = float(durations.get(checks[i][0], 1.0))
            return value if math.isfinite(value) and value > 0 else 1.0
        except (ValueError, TypeError, OverflowError):
            return 1.0

    chain = [i for i, (_n, fn) in enumerate(checks) if getattr(fn, "__memoized__", False)]
    chain_set = set(chain)
    rest = [i for i in range(len(checks)) if i not in chain_set]
    buckets = [[] for _ in range(max(1, min(jobs, len(rest) + bool(chain))))]
    loads = [0.0] * len(buckets)
    if chain:
        buckets[0] = chain
        loads[0] = sum(cost(i) for i in chain)
    for i in sorted(rest, key=lambda i: -cost(i)):
        target = min(range(len(buckets)), key=lambda k: (loads[k], k))
        buckets[target].append(i)
        loads[target] += cost(i)
    return [bucket for bucket in buckets if bucket]


def _run_parallel(checks, jobs, progress_interval_s=30.0, queue_timeout_s=5.0):
    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    groups = _groups(checks, jobs)
    procs = [ctx.Process(target=_worker, args=(g, checks, q)) for g in groups]
    for p in procs:
        p.start()
    results = {}
    active = {}
    finished = 0
    started = last_progress = time.monotonic()

    def progress():
        nonlocal last_progress
        now = time.monotonic()
        if progress_interval_s > 0 and now - last_progress >= progress_interval_s:
            alive = sum(p.is_alive() for p in procs)
            longest=sorted(active,key=lambda i:active[i])[:2]
            detail='; running '+', '.join(f'{checks[i][0]} ({now-active[i]:.0f}s)' for i in longest) if longest else ''
            print(f"regression progress: {len(results)}/{len(checks)} checks reported; "
                  f"{alive} workers active; {now - started:.0f}s elapsed{detail}", file=sys.stderr, flush=True)
            last_progress = now

    while finished < len(groups):
        try:
            item = q.get(timeout=queue_timeout_s)
        except Exception:  # queue.Empty: no news for 5 s; a worker that died without its sentinel must not hang us
            if not any(p.is_alive() for p in procs) and finished < len(groups):
                break
            progress()
            continue
        if item is None:
            finished += 1
            progress()
            continue
        i, res = item
        if res.get('state') == 'running':
            active[i]=res['started']
            progress()
            continue
        active.pop(i,None)
        results[i] = res
        if res["status"] == "FAIL":
            print(f"regression early failure: {res['name']}: {res['detail']}", file=sys.stderr, flush=True)
        progress()
    for p in procs:
        p.join(timeout=10)
    out = []
    for i, (cname, _fn) in enumerate(checks):
        if i in results:
            out.append(results[i])
        else:  # the worker died before reporting this check
            out.append({"name": cname, "status": "FAIL", "sec": 0.0, "detail": "worker exited before reporting"})
    return out


def _run_serial(checks, fail_fast):
    out = []
    for cname, fn in checks:
        res = _run_one(cname, fn)
        out.append(res)
        if fail_fast and res["status"] == "FAIL":
            break
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--update-golden", action="store_true")
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument("--jobs", type=int, default=0)
    args = ap.parse_args(argv)
    if args.update_golden:
        os.environ["CTSG_UPDATE_GOLDEN"] = "1"
    checks = []
    for name in SUITES + ["lowrange_integration"]:
        mod = importlib.import_module(name)
        for cname, fn in mod.CHECKS:
            checks.append((cname, fn))
    checks.extend(selftests())
    if args.only:
        checks = [c for c in checks if c[0].startswith(args.only)]
    skip = {x.strip() for x in os.environ.get("CTSG_SKIP_CHECKS", "").split(",") if x.strip()}
    if skip:                                   # mutate.py: a fault in the pinned trader must be judged by behaviour checks, not by the pin any edit of that file trips
        checks = [c for c in checks if c[0] not in skip]
    if args.list:
        for cname, _ in checks:
            print(cname)
        return 0
    jobs = args.jobs or int(os.environ.get("CTSG_JOBS") or min(4, os.cpu_count() or 1))
    serial = args.update_golden or args.fail_fast or jobs <= 1 or len(checks) <= 1
    if args.fail_fast:
        try:
            known = json.load(open(DURATIONS_PATH, encoding="utf-8"))
        except (OSError, ValueError):
            known = {}
        checks.sort(key=lambda c: float(known.get(c[0], 1.0)))      # stable: unknown checks keep their order at 1 s
    t_all = time.time()
    if serial:
        results = _run_serial(checks, args.fail_fast)
    else:
        results = _run_parallel(checks, jobs)
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for res in results:
        counts[res["status"]] += 1
        print(f"{res['status']} {res['name']:58s} {res['sec']:6.2f}s  {res['detail']}", flush=True)
    if not args.only and not args.fail_fast and not os.environ.get("CTSG_PULSE_DIR"):
        try:
            with open(DURATIONS_PATH, "w", encoding="utf-8") as fh:     # a full run on the repo's own engine records the costs
                json.dump({r["name"]: round(r["sec"], 2) for r in sorted(results, key=lambda r: r["name"])}, fh, indent=1)
                fh.write("\n")
        except OSError:
            pass
    mode = "serial" if serial else f"{jobs} workers"
    print(f"\nregression: {counts['PASS']} pass, {counts['FAIL']} fail, {counts['SKIP']} skip "
          f"({len(results)} checks, {time.time() - t_all:.0f}s, {mode})", flush=True)
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
