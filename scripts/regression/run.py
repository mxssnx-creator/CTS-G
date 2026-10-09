"""Regression runner. Runs every suite and the engine self-tests, prints PASS / FAIL / SKIP per check.

  python3 scripts/regression/run.py                       run everything (exit 1 on any FAIL)
  python3 scripts/regression/run.py --only processing     run checks whose name starts with the prefix
  python3 scripts/regression/run.py --list                list checks
  python3 scripts/regression/run.py --jobs 4              worker processes (default: CTSG_JOBS, else min(4, cores))
  python3 scripts/regression/run.py --fail-fast           stop at the first FAIL (serial)
  python3 scripts/regression/run.py --update-golden       rewrite golden.json after an intended processing change (serial)
  CTSG_PULSE_DIR=/path/to/server/pulse                    run against another copy (used for fault injection)

Parallel layout: the memoized processing checks form one group, run in order in one process so the replay memo
stays warm. Every other check is independent and is spread over the remaining workers. Results print in the
original order. Checks are forked from this process, so the registry is shared and nothing is pickled.
"""
from __future__ import annotations

import argparse
import importlib
import multiprocessing
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import Skip  # noqa: E402

# logistics is static and fails in well under a second: it runs first so a broken pin or config fails fast
SUITES = ["logistics", "exchange", "sidecar", "contract", "trader", "grid_and_units", "exits_and_gate", "gates", "lifecycle", "indication", "calc", "processing", "golden"]


def selftests():
    """The engine modules' own self_test() batteries, folded into the same report."""
    out = []
    import common  # noqa: F401  (sets sys.path to the engine)
    for mod in ("set_engine", "indication_engine", "dca_engine", "position_cost", "exit_engine",
                "risk_variants", "stats_report", "coord_engine"):
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
            q.put((i, _run_one(cname, fn)))
    finally:
        q.put(None)


def _groups(checks, jobs):
    """Memoized processing checks form one ordered group. The rest are dealt round-robin to the other workers."""
    chain = [i for i, (_n, fn) in enumerate(checks) if getattr(fn, "__memoized__", False)]
    chain_set = set(chain)
    rest = [i for i in range(len(checks)) if i not in chain_set]
    spare = max(1, jobs - 1) if chain else jobs
    groups = [chain] if chain else []
    buckets = [[] for _ in range(max(1, min(spare, len(rest))))]
    for k, i in enumerate(rest):
        buckets[k % len(buckets)].append(i)
    groups.extend(b for b in buckets if b)
    return groups


def _run_parallel(checks, jobs):
    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    groups = _groups(checks, jobs)
    procs = [ctx.Process(target=_worker, args=(g, checks, q)) for g in groups]
    for p in procs:
        p.start()
    results = {}
    finished = 0
    while finished < len(groups):
        try:
            item = q.get(timeout=5)
        except Exception:  # queue.Empty: no news for 5 s; a worker that died without its sentinel must not hang us
            if not any(p.is_alive() for p in procs) and finished < len(groups):
                break
            continue
        if item is None:
            finished += 1
            continue
        i, res = item
        results[i] = res
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
    for name in SUITES:
        mod = importlib.import_module(name)
        for cname, fn in mod.CHECKS:
            checks.append((cname, fn))
    checks.extend(selftests())
    if args.only:
        checks = [c for c in checks if c[0].startswith(args.only)]
    if args.list:
        for cname, _ in checks:
            print(cname)
        return 0
    jobs = args.jobs or int(os.environ.get("CTSG_JOBS") or min(4, os.cpu_count() or 1))
    serial = args.update_golden or args.fail_fast or jobs <= 1 or len(checks) <= 1
    t_all = time.time()
    if serial:
        results = _run_serial(checks, args.fail_fast)
    else:
        results = _run_parallel(checks, jobs)
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for res in results:
        counts[res["status"]] += 1
        print(f"{res['status']} {res['name']:58s} {res['sec']:6.2f}s  {res['detail']}", flush=True)
    mode = "serial" if serial else f"{jobs} workers"
    print(f"\nregression: {counts['PASS']} pass, {counts['FAIL']} fail, {counts['SKIP']} skip "
          f"({len(results)} checks, {time.time() - t_all:.0f}s, {mode})", flush=True)
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
