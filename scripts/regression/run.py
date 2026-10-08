"""Regression runner. Runs every suite and the engine self-tests, prints PASS / FAIL / SKIP per check.

  python3 scripts/regression/run.py                  run everything (exit 1 on any FAIL)
  python3 scripts/regression/run.py --only processing   run checks whose name starts with the prefix
  python3 scripts/regression/run.py --list           list checks
  python3 scripts/regression/run.py --update-golden  rewrite golden.json after an intended processing change
  CTSG_PULSE_DIR=/path/to/server/pulse               run against another copy (used for fault injection)
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import Skip  # noqa: E402

SUITES = ["grid_and_units", "exits_and_gate", "processing", "logistics", "golden"]


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


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--update-golden", action="store_true")
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
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    t_all = time.time()
    for cname, fn in checks:
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
        counts[status] += 1
        print(f"{status} {cname:58s} {time.time() - t0:6.2f}s  {detail}", flush=True)
    print(f"\nregression: {counts['PASS']} pass, {counts['FAIL']} fail, {counts['SKIP']} skip "
          f"({len(checks)} checks, {time.time() - t_all:.0f}s)", flush=True)
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
