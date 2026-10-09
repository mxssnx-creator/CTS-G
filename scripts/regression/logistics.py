"""Logistics suite: configuration, deploy pin, repository hygiene and the scripts the repo relies on."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile

from common import PULSE, ROOT, Skip, git, overlay

X01 = os.path.join(PULSE, "overlay-bingx-x01.json")
X02 = os.path.join(PULSE, "overlay-bingx-x02.json")
CONFIG_MODEL = os.path.join(ROOT, "src", "lib", "config-model.ts")
PIN_WANT = "b9f054d4ac7e0a1eedcf060279c196a50eb97d64"
PIN_BASE = "b3a9ff3c60c72864ac5558f488d7e6991bb31d76"
RESTORE_PATCH = os.path.join(ROOT, "restore", "pulse_trader.py.patch")


def overlays_share_grid_contract():
    ok_all = True
    detail = []
    for path in (X01, X02):
        ov = json.load(open(path))
        want = {"setMinStep": 2, "setStepMax": 30, "trailMinStep": 3, "histLookbackBars": 1920,
                "setMinPf": 1.1, "setGateWindow": 50, "setGateMinTrades": 30}
        bad = {k: ov.get(k) for k, v in want.items() if ov.get(k) != v}
        ok_all = ok_all and not bad
        detail.append(f"{os.path.basename(path)}:{'ok' if not bad else bad}")
    return ok_all, " ".join(detail)


def desk_defaults_match_engine():
    src = open(CONFIG_MODEL, encoding="utf-8").read()
    need = [r"setMinStep: 2,", r"setStepMax: 30,", r"trailMinStep: 3,", r"histLookbackBars: 1920",
            r"setMinPf: GATE_MIN_PF_DEFAULT,", r"export const SL_SET_RATIOS"]
    missing = [p for p in need if not re.search(p, src)]
    # the desk fallback and the engine gate default are one number kept in two files
    desk = re.search(r"export const GATE_MIN_PF_DEFAULT = ([0-9.]+);", src)
    eng = re.search(r"GATE_MIN_PF_DEFAULT\s*=\s*([0-9.]+)", open(os.path.join(PULSE, "position_cost.py"), encoding="utf-8").read())
    if not (desk and eng and float(desk.group(1)) == float(eng.group(1))):
        missing.append(f"GATE_MIN_PF_DEFAULT desk={desk and desk.group(1)} engine={eng and eng.group(1)}")
    return not missing, f"missing={missing}" if missing else f"{len(need)} defaults present; gate default equal in desk and engine"


def dca_is_off_everywhere():
    dca_src = open(os.path.join(PULSE, "dca_engine.py"), encoding="utf-8").read()
    mod_src = open(os.path.join(PULSE, "modules.py"), encoding="utf-8").read()
    hard = re.search(r"DCA_HARD_OFF\s*=\s*True", dca_src) is not None
    flag = re.search(r'"strategy\.dca"\s*:\s*False', mod_src) is not None
    x01 = overlay()
    ok = hard and flag and not x01.get("dcaEnabled", False)
    return ok, f"hard_off={hard} module_flag_off={flag} overlay_dcaEnabled={x01.get('dcaEnabled', False)}"


def deploy_pin_matches_engine():
    path = os.path.join(PULSE, "pulse_trader.py")
    got = git("hash-object", path).strip()
    pin = open(os.path.join(ROOT, "deploy", "linux-common.sh"), encoding="utf-8").read()
    ok = got == PIN_WANT and PIN_WANT in pin
    return ok, f"pulse_trader blob={got[:10]} pin={PIN_WANT[:10]}"


def restore_patch_reproduces_pin():
    if not os.path.exists(RESTORE_PATCH):
        raise Skip("restore patch missing")
    base = git("show", PIN_BASE)
    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "pulse_trader.py")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(base)
        proc = subprocess.run(["patch", "-p0", target], stdin=open(RESTORE_PATCH, "rb"),
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return False, f"patch exit {proc.returncode}: {proc.stderr.strip()[:120]}"
        got = git("hash-object", target).strip()
    return got == PIN_WANT, f"patched blob={got[:10]}"


def no_leftover_zest_names():
    # this file names the pattern itself, so it is excluded from the search
    hits = git("grep", "-l", "-i", "zest", "--", ".", ":!scripts/regression/logistics.py",
               no_match_ok=True).strip().splitlines()
    return not hits, f"files={hits}" if hits else "no 'zest' in tracked files"


def no_secret_literals():
    pat = re.compile(r"BEGIN (RSA|EC|OPENSSH|DSA|PGP)? ?PRIVATE KEY|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|"
                     r"github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{32,}")
    out = git("grep", "-n", "-I", "-E", pat.pattern, no_match_ok=True)
    hits = [line for line in out.splitlines() if line]
    return not hits, f"hits={len(hits)}" if hits else "no credential patterns"


def package_scripts_exist():
    pkg = json.load(open(os.path.join(ROOT, "package.json")))
    missing = []
    for name, cmd in pkg.get("scripts", {}).items():
        for tok in re.findall(r"(scripts/[A-Za-z0-9_./-]+)", cmd):
            base = tok.split("*")[0]
            if "*" in tok:
                continue
            if not os.path.exists(os.path.join(ROOT, base)):
                missing.append(f"{name}:{tok}")
    return not missing, f"missing={missing}" if missing else "every referenced script exists"


def engine_modules_compile():
    """Every engine module parses (syntax only: nothing is imported, nothing is written)."""
    import ast
    bad = []
    names = sorted(f for f in os.listdir(PULSE) if f.endswith(".py"))
    for f in names:
        try:
            with open(os.path.join(PULSE, f), encoding="utf-8") as fh:
                ast.parse(fh.read(), filename=f)
        except SyntaxError as exc:
            bad.append(f"{f}:{exc.lineno}")
    return not bad, f"bad={bad}" if bad else f"{len(names)} engine modules parse"


# A ratio is 1 + 0.10 x avgR (display only). It must never be compared with a PF threshold or stored under a PF key.
RATIO_PF_PATTERNS = [
    r"ratio[^\n]{0,30}(?:>=|<=|<|>)[^\n]{0,30}(?:min_pf|setMinPf|minPf|\b1\.[012]\b)",
    r'"pf"\s*:[^\n]*ratio',
    r"last15Classic|classicPf|classic_all|last15_classic",
]


def no_ratio_as_pf():
    paths = [os.path.join(PULSE, f) for f in os.listdir(PULSE) if f.endswith(".py")]
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "src")):
        paths += [os.path.join(dirpath, f) for f in files if f.endswith((".ts", ".tsx"))]
    hits = []
    for path in paths:
        text = open(path, encoding="utf-8").read()
        for pat in RATIO_PF_PATTERNS:
            for m in re.finditer(pat, text):
                hits.append(f"{os.path.relpath(path, ROOT)}:{text.count(chr(10), 0, m.start()) + 1}")
    return not hits, f"hits={hits[:8]}" if hits else "no ratio is compared with a PF threshold; no PF key holds a ratio"


def conn_pinned_per_connection():
    """Each connection is pinned to one BingX environment. The trader refuses to start on a contradiction, and no
    stored base URL falls back to mainnet (x02 shares x01's key, so the URL is the only switch)."""
    import sys
    if PULSE not in sys.path:
        sys.path.insert(0, PULSE)
    import conn_guard
    results = conn_guard.self_test()
    bad = [name for name, ok, _ in results if not ok]
    src = open(os.path.join(PULSE, "pulse_trader.py"), encoding="utf-8").read()
    mainnet_fallback = re.search(r'redis_hget\("base_url"\) or "https://open-api', src) is not None
    wired = "resolve_base(CONN_SHORT" in src and "from conn_guard import" in src
    ok = not bad and not mainnet_fallback and wired
    return ok, f"guard checks={len(results)} failed={bad} mainnet_fallback={mainnet_fallback} wired={wired}"


def no_fabricated_pass_and_one_entry_conf():
    """The block intern PF has no fabricated 1.2 pass, live entries read the one setEntryConf key (no 0.50 literal),
    and the live TP override does not touch Set fills."""
    src = open(os.path.join(PULSE, "pulse_trader.py"), encoding="utf-8").read()
    fabricated = "intern_pf = 1.2" in src
    literal_conf = re.search(r"if conf < 0\.50:", src) is not None
    one_key = "if conf < self.sets.entry_conf" in src
    set_fill_override = "not (chosen and getattr(chosen, \"step\", 0))" in src
    ok = (not fabricated) and (not literal_conf) and one_key and set_fill_override
    return ok, f"fabricated_pass={fabricated} literal_0.50={literal_conf} one_key={one_key} set_fill_tp_kept={set_fill_override}"


CHECKS = [
    ("logistics.overlays-share-grid-contract", overlays_share_grid_contract),
    ("logistics.desk-defaults-match-engine", desk_defaults_match_engine),
    ("logistics.no-ratio-as-pf", no_ratio_as_pf),
    ("logistics.conn-pinned-per-connection", conn_pinned_per_connection),
    ("logistics.no-fabricated-pass-and-one-entry-conf", no_fabricated_pass_and_one_entry_conf),
    ("logistics.dca-off-everywhere", dca_is_off_everywhere),
    ("logistics.deploy-pin-matches-engine", deploy_pin_matches_engine),
    ("logistics.restore-patch-reproduces-pin", restore_patch_reproduces_pin),
    ("logistics.no-leftover-zest-names", no_leftover_zest_names),
    ("logistics.no-secret-literals", no_secret_literals),
    ("logistics.package-scripts-exist", package_scripts_exist),
    ("logistics.engine-modules-compile", engine_modules_compile),
]
