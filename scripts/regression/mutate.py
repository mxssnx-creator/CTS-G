"""Fault injection: the regression suite must catch each planted fault.

Each mutation edits a temporary copy of server/pulse (the repo is never touched), runs the full suite
against that copy, and reports the checks that failed. A mutation that leaves the suite green is a gap.
  python3 scripts/regression/mutate.py
"""
from __future__ import annotations

import concurrent.futures
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
     "set_engine.py", "if st.gate_pf + 1e-9 < self.min_pf:", "if st.gate_pf - 1e-9 <= self.min_pf:"),
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
    ("lifecycle: global ready gate restored in _pick",
     "set_engine.py", "        if self.use_historic_gate:\n            ranked = self.valid_for(pack, kind)",
     "        if self.use_historic_gate and not self.progress.ready:\n            return None\n        if self.use_historic_gate:\n            ranked = self.valid_for(pack, kind)"),
    ("lifecycle: errored Set keeps trading (fail-open)",
     "set_engine.py", "        if st.last_error:\n            return \"error\"", "        if False:\n            return \"error\""),
    ("lifecycle: back-to-back replay before the first pass",
     "set_engine.py", "        wait = self.refresh_s if self.progress.ready else self.retry_s", "        wait = 0.0"),
    ("lifecycle: stale evaluation stays valid in live",
     "set_engine.py", "        if self.fresh_s > 0 and time.time() - st.evaluated_at > self.fresh_s:\n            return \"stale\"",
     "        if False:\n            return \"stale\""),
    ("lifecycle: only the top Set per pack may enter",
     "set_engine.py", "            rows.sort(key=lambda s: (s.gate_pf, s.last25_avg_r, -s.max_dd_s, s.n), reverse=True)\n            return rows",
     "            rows.sort(key=lambda s: (s.gate_pf, s.last25_avg_r, -s.max_dd_s, s.n), reverse=True)\n            return rows[:1]"),
    ("lifecycle: lock ignored (sticky lock removed)",
     "set_engine.py", "        if st.locked:\n            return \"lock\"", "        if False:\n            return \"lock\""),
    ("lifecycle: trader reads a removed Set attribute",
     "pulse_trader.py", "            \"setUseHistoricGate\": self.sets.use_historic_gate,",
     "            \"setUseHistoricGate\": self.sets.use_historic_gate,\n            \"setReactivate\": self.sets.reactivate,"),
    ("exchange: public GET ignores the path cooldown",
     "bingx_fast.py", "        if time.time() < self.path_cd.get(path, 0.0):\n            return None",
     "        if False:\n            return None"),
    ("exchange: async path bypasses the cooldown",
     "bingx_fast.py", "                    wait = self.gate.reserve_public(path)",
     "                    wait = self.gate.buckets[\"public\"].reserve()"),
    ("bars: a live refresh replaces the stored history",
     "set_engine.py", "self.bars[symbol] = _join_bars(self.bars.get(symbol) or [], cleaned)[-self.lookback :]",
     "self.bars[symbol] = cleaned[-self.lookback :]"),
    ("indications: reason matched by kind only, mode ignored",
     "indication_engine.py", "            bool(kind and mode) and (lambda i: i.kind == kind and i.mode == mode),",
     "            bool(kind) and (lambda i: i.kind == kind),"),
    ("config: an explicit zero falls back to the default",
     "exit_engine.py", "self.min_hold_s = num(ov, \"exitMinHoldS\", 45)", "self.min_hold_s = num(ov, \"exitMinHoldS\", 45) or 45"),
    ("sidecar: mutating POST accepted without the token",
     "pulse_http.py", "            if not _token_ok(self.headers.get(\"X-Pulse-Token\", \"\")):",
     "            if False:"),
    ("defaults: exit book scratchMin absent default drifts from the trader",
     "exit_engine.py", "(\"scratchMin\", \"scratchMinPct\"), 0.16))", "(\"scratchMin\", \"scratchMinPct\"), 0.25))"),
    ("defaults: coordinator minStep constructor differs from its loaded default",
     "coord_engine.py", "        self.min_step = 6\n", "        self.min_step = 8\n"),
    ("sidecar: a frozen stats file still reports running",
     "pulse_http.py", '        out["stale"] = True\n        out["running"] = False\n', '        out["stale"] = True\n'),
    ("sidecar: overall drawdown reported as 0 instead of null",
     "pulse_http.py", '"drawdownPct": None,', '"drawdownPct": 0,'),
    ("sidecar: overall usedMargin treats a silent lane as 0",
     "pulse_http.py", '"usedMargin": _sum_or_none(live, vst, "usedMargin"),',
     '"usedMargin": (live.get("usedMargin") or 0) + (vst.get("usedMargin") or 0),'),
    ("indications: snapshot iterates the live dicts",
     "indication_engine.py", "        last = dict(self.last)\n        evals = dict(self.evals)",
     "        last = self.last\n        evals = self.evals"),
    ("indications: the Set pack ignores the direction switch",
     "set_engine.py", 'if closes and settings.get("typeDirection", True) else None', "if closes else None"),
    ("indications: the Set pack forces the 1m lane on",
     "set_engine.py", '"tf1m": bool(ov.get("tf1m", True)),', '"tf1m": True,'),
    ("contract: the lane summary sends a key the desk type does not declare",
     "pulse_http.py", '        "svcActive": state == "active",', '        "svcActive": state == "active",\n        "undeclaredKey": 1,'),
    ("contract: a closed row loses the lane that closed it",
     "pulse_http.py", '            q = dict(c)\n            q["connection"] = lane["id"]', "            q = dict(c)"),
    ("pinned engine edited without re-pinning",
     "pulse_trader.py", None, "\n# edited\n"),
]


def run_once(pulse_dir: str, fail_fast: bool = True) -> subprocess.CompletedProcess:
    """One suite run against a pulse copy. A mutant run is serial and stops at its first FAIL: a caught mutant
    needs only one failing check, and a survivor never fails, so it runs to the end either way."""
    env = dict(os.environ, CTSG_PULSE_DIR=pulse_dir, CTSG_JOBS="1" if fail_fast else (os.environ.get("CTSG_JOBS") or ""))
    args = [sys.executable, "-B", os.path.join(HERE, "run.py")] + (["--fail-fast"] if fail_fast else [])
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=1500)


def _mutant(item):
    """Apply one fault to a private copy of server/pulse and run the suite against it. Returns (name, verdict, info).
    verdict is caught, survived, or anchor. An anchor that no longer matches is a verdict of its own, never a pass."""
    name, fname, old, new = item
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
                return name, "anchor", f"anchor found {count} times, mutation not applied"
            text = text.replace(old, new)
        open(path, "w", encoding="utf-8").write(text)
        out = run_once(copy, fail_fast=True)
    fails = [l.split()[1] for l in out.stdout.splitlines() if l.startswith("FAIL ")]
    if out.returncode == 0:
        return name, "survived", ""
    return name, "caught", f"{len(fails)} failing check(s), e.g. {fails[:2]}"


def main() -> int:
    base = run_once(PULSE, fail_fast=False)
    if base.returncode != 0:
        print("baseline suite is not green; fix it before injecting faults", flush=True)
        print("\n".join(l for l in base.stdout.splitlines() if l.startswith("FAIL")), flush=True)
        return 1
    jobs = int(os.environ.get("CTSG_MUT_JOBS") or 4)
    survivors, anchors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for name, verdict, info in pool.map(_mutant, MUTATIONS):
            if verdict == "survived":
                survivors.append(name)
                print(f"SURVIVED  {name}", flush=True)
            elif verdict == "anchor":
                anchors.append(name)
                print(f"ANCHOR    {name}: {info}", flush=True)
            else:
                print(f"caught    {name}: {info}", flush=True)
    print(f"\nmutations: {len(MUTATIONS)}  survived: {len(survivors)}  anchor-missing: {len(anchors)}  "
          f"workers: {jobs}", flush=True)
    return 1 if (survivors or anchors) else 0


if __name__ == "__main__":
    sys.exit(main())
