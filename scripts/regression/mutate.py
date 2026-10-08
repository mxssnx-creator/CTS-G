"""Fault injection: the regression suite must catch each planted fault.

Each mutation edits a temporary copy of server/pulse (the repo is never touched), runs the full suite
against that copy, and reports the checks that failed. A mutation that leaves the suite green is a gap.
  python3 scripts/regression/mutate.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
PULSE = os.path.join(ROOT, "server", "pulse")

MUTATIONS = [
    ("scan: TP wins a same-bar tie against the stop",
     "set_engine.py", "if low <= stop:", "if low <= stop and not (honor_tp and high >= tp):"),
    ("scan: time exit one bar late",
     "set_engine.py", "if why is None and held >= time_bars:", "if why is None and held > time_bars:"),
    ("gate: boundary moved below 1.10",
     "set_engine.py", "st.gate_pf + 1e-9 >= self.min_pf", "st.gate_pf - 1e-9 > self.min_pf"),
    ("cost: percent read as tenths",
     "position_cost.py", "    c = max(0.0, finite(cost_pct, POSITION_COST_PCT_DEFAULT))\n    return c / 100.0",
     "    c = max(0.0, finite(cost_pct, POSITION_COST_PCT_DEFAULT))\n    return c / 10.0"),
    ("grid: trail give may exceed arm",
     "risk_variants.py", "for j in range(m, k + 1):", "for j in range(m, k + 3):"),
    ("grid: TP steps capped at 22",
     "position_cost.py", "TP_STEP_MAX = 30", "TP_STEP_MAX = 22"),
    ("cache: signals keyed by bar index only",
     "set_engine.py", "key = hash((salt, tuple(hs[lo_w : i + 1])))", "key = hash((salt, i))"),
    ("cooldown: skipped on flat jumps",
     "set_engine.py", "cool = max(0, cool - (j - i))", "cool = cool"),
    ("live close: duplicates not removed",
     "set_engine.py", "if key in self._live_seen:", "if False and key in self._live_seen:"),
    ("chunks: a refresh replaces other symbols' rows",
     "set_engine.py", "self._hist_rows[symbol] = per_set", "self._hist_rows = {symbol: per_set}"),
    ("DCA switched on",
     "dca_engine.py", "DCA_HARD_OFF = True", "DCA_HARD_OFF = False"),
    ("pinned engine edited without re-pinning",
     "pulse_trader.py", None, "\n# edited\n"),
]


def run_once(pulse_dir: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, CTSG_PULSE_DIR=pulse_dir)
    return subprocess.run([sys.executable, "-B", os.path.join(HERE, "run.py")], env=env,
                          capture_output=True, text=True, timeout=1500)


def main() -> int:
    base = run_once(PULSE)
    if base.returncode != 0:
        print("baseline suite is not green; fix it before injecting faults", flush=True)
        print("\n".join(l for l in base.stdout.splitlines() if l.startswith("FAIL")), flush=True)
        return 1
    survivors = []
    for name, fname, old, new in MUTATIONS:
        with tempfile.TemporaryDirectory() as td:
            copy = os.path.join(td, "pulse")
            shutil.copytree(PULSE, copy, ignore=shutil.ignore_patterns("*.jsonl", "*.log", "__pycache__"))
            path = os.path.join(copy, fname)
            text = open(path, encoding="utf-8").read()
            if old is None:
                text = text + new
            else:
                count = text.count(old)
                if count != 1:
                    print(f"SKIP  {name}: anchor found {count} times, mutation not applied", flush=True)
                    continue
                text = text.replace(old, new)
            open(path, "w", encoding="utf-8").write(text)
            out = run_once(copy)
            fails = [l.split()[1] for l in out.stdout.splitlines() if l.startswith("FAIL ")]
        if out.returncode == 0:
            survivors.append(name)
            print(f"SURVIVED  {name}", flush=True)
        else:
            print(f"caught    {name}: {len(fails)} failing check(s), e.g. {fails[:2]}", flush=True)
    print(f"\nmutations: {len(MUTATIONS)}  survived: {len(survivors)}", flush=True)
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
