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
PIN_WANT = "6d57fdaab765025fd4988ea3dc503d65da1f51db"
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


DESK_SHARED_NUMERIC = ("cooldownS", "staggerS", "minStep", "trailingMinStep", "setMinStep", "setStepMax",
                       "scanS", "timeStopS", "scratchS", "tpPct", "slPct")


def desk_defaults_equal_shipped_overlay():
    """The desk's DEFAULT_OVERLAY and the shipped x01 overlay agree on each shared numeric key.

    The engine and the shipped overlays are the reference (decision D1): a desk default that differs from the value
    the live config runs is a number the operator sees and the engine never uses.
    """
    src = open(CONFIG_MODEL, encoding="utf-8").read()
    start = src.index("export const DEFAULT_OVERLAY")
    block = src[start:src.index("};", start)]
    shipped = json.load(open(os.path.join(PULSE, "overlay-bingx-x01.json"), encoding="utf-8"))
    drift = []
    checked = []
    for key in DESK_SHARED_NUMERIC:
        m = re.search(r"\b" + key + r":\s*(-?[0-9.]+)", block)
        if not m or key not in shipped:
            continue
        checked.append(key)
        if abs(float(m.group(1)) - float(shipped[key])) > 1e-9:
            drift.append(f"{key}: desk={m.group(1)} shipped={shipped[key]}")
    ok = not drift and len(checked) >= 6
    return ok, f"checked={len(checked)} drift={drift}" if drift else f"{len(checked)} shared numeric defaults equal the shipped overlay"


def _ts_default(raw):
    """A single-line TypeScript default: bool, number, numeric list, string, or GATE_MIN_PF_DEFAULT. None if unparsed."""
    raw = raw.strip().rstrip(",").strip()
    if raw in ("true", "false"):
        return raw == "true"
    if raw == "GATE_MIN_PF_DEFAULT":
        return 1.1
    if raw.startswith("["):
        inner = raw.strip("[]").strip()
        try:
            return [float(x) for x in inner.split(",")] if inner else []
        except ValueError:
            return None
    if raw.startswith('"'):
        return raw.strip('"')
    try:
        return float(raw)
    except ValueError:
        return None


# A desk default that differs from the shipped overlay on purpose. Each one names its decision.
DESK_DEFAULT_EXCEPTIONS = {
    "symbolsAll": "decision H (answered): the desk pre-load matches the engine absent default (non-wild); the shipped files set true",
}


def desk_default_overlay_equals_shipped_x01():
    """Every single-line DEFAULT_OVERLAY key that the shipped x01 overlay defines has the same value there.

    The desk shows these as the value before a config loads. A difference here is a number the operator reads that the
    live config does not run (decision D1: the engine and the shipped overlay are the reference).
    """
    src = open(CONFIG_MODEL, encoding="utf-8").read()
    start = src.index("export const DEFAULT_OVERLAY")
    block = src[start:src.index("\n};", start)]
    shipped = json.load(open(os.path.join(PULSE, "overlay-bingx-x01.json"), encoding="utf-8"))
    drift, checked, excepted = [], 0, []
    for m in re.finditer(r"^  ([A-Za-z0-9]+): (.+?),?$", block, re.M):
        key, raw = m.group(1), m.group(2)
        if key not in shipped or raw.strip().endswith("["):   # a multi-line value is compared by its own test
            continue
        want = _ts_default(raw)
        if want is None:
            continue
        checked += 1
        have = shipped[key]
        if isinstance(want, bool) or isinstance(have, bool) or isinstance(want, str) or isinstance(have, str):
            same = want == have
        elif isinstance(want, list) or isinstance(have, list):
            same = isinstance(want, list) and isinstance(have, list) and len(want) == len(have) and all(
                abs(float(a) - float(b)) <= 1e-9 for a, b in zip(want, have))
        else:
            same = abs(float(want) - float(have)) <= 1e-9
        if not same and key in DESK_DEFAULT_EXCEPTIONS:
            excepted.append(key)
            continue
        if not same:
            drift.append(f"{key}: desk={raw.strip()} shipped={have}")
    ok = not drift and checked >= 20
    note = f" ({len(excepted)} documented exception: {', '.join(excepted)})" if excepted else ""
    return ok, f"checked={checked} drift={drift}" if drift else f"{checked} single-line DEFAULT_OVERLAY keys equal the shipped x01 overlay{note}"


def desk_symbols_equal_engine_fixed_list():
    """The desk's default symbol list is the engine's fixed SYMBOLS list (the value when symbolsAll is off)."""
    src = open(CONFIG_MODEL, encoding="utf-8").read()
    start = src.index("export const DEFAULT_OVERLAY")
    desk = re.search(r"^  symbols: \[(.*?)\],", src[start:], re.M | re.S)
    engine = re.search(r"^SYMBOLS = \[(.*?)\]", open(os.path.join(PULSE, "pulse_trader.py"), encoding="utf-8").read(), re.M | re.S)
    if not (desk and engine):
        return False, "symbols list not found on one side"
    want = re.findall(r'"([A-Z0-9-]+)"', engine.group(1))
    have = re.findall(r'"([A-Z0-9-]+)"', desk.group(1))
    return want == have, f"desk={len(have)} engine={len(want)} equal={want == have}"


def desk_cards_read_effective_overlay():
    """No desk card reads an overlay key from runtime cts data: the card must show the value the config runs."""
    src = open(CONFIG_MODEL, encoding="utf-8").read()
    start = src.index("export const DEFAULT_OVERLAY")
    keys = set(re.findall(r"^  ([A-Za-z0-9]+): ", src[start:src.index("\n};", start)], re.M))
    root = os.path.join(ROOT, "src", "routes")
    hits = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".tsx"):
            continue
        text = open(os.path.join(root, name), encoding="utf-8").read()
        for m in re.finditer(r"\bcts\??\.([A-Za-z0-9_]+)", text):
            if m.group(1) in keys:
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{name}:{line} cts.{m.group(1)}")
    return not hits, f"runtime cts reads of overlay keys: {hits}" if hits else f"no card reads an overlay key from cts ({len(keys)} keys checked)"


def engine_absent_defaults_agree():
    """Engines that read the same key agree on its absent default: scratchMin (exit book and trader) and minStep
    (coordinator constructor and its loaded default)."""
    exit_src = open(os.path.join(PULSE, "exit_engine.py"), encoding="utf-8").read()
    trader = open(os.path.join(PULSE, "pulse_trader.py"), encoding="utf-8").read()
    coord = open(os.path.join(PULSE, "coord_engine.py"), encoding="utf-8").read()
    exit_scratch = re.search(r'"scratchMin", "scratchMinPct"\), ([0-9.]+)\)', exit_src)
    trader_scratch = re.search(r"^SCRATCH_MIN = ([0-9.]+)", trader, re.M)
    coord_init = re.search(r"^        self\.min_step = ([0-9]+)$", coord, re.M)
    coord_load = re.search(r'int\(ov\.get\("minStep"\) or coord\.get\("minStep"\) or ([0-9]+)\)', coord)
    problems = []
    exit_scratch_s = re.search(r'"scratchS", ([0-9.]+)\)', exit_src)
    set_scratch_s = re.search(r'self\.scratch_s = num\(ov, "scratchS", ([0-9.]+)\)', open(os.path.join(PULSE, "set_engine.py"), encoding="utf-8").read())
    if not (exit_scratch_s and set_scratch_s and exit_scratch_s.group(1) == set_scratch_s.group(1)):
        problems.append(f"scratchS exit={exit_scratch_s and exit_scratch_s.group(1)} set={set_scratch_s and set_scratch_s.group(1)}")
    if not (exit_scratch and trader_scratch and abs(float(exit_scratch.group(1)) - float(trader_scratch.group(1)) * 100) < 1e-9):
        problems.append(f"scratchMin exit={exit_scratch and exit_scratch.group(1)} trader={trader_scratch and trader_scratch.group(1)}")
    if not (coord_init and coord_load and coord_init.group(1) == coord_load.group(1)):
        problems.append(f"minStep init={coord_init and coord_init.group(1)} load={coord_load and coord_load.group(1)}")
    return not problems, f"problems={problems}" if problems else "absent defaults agree: scratchMin 0.16%, minStep 6"


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


def zero_is_a_value_not_a_default():
    """An explicit 0 in a config read is used. Only a missing key takes the default."""
    from config_num import num
    from exit_engine import ExitBook
    from set_engine import SetBook
    e = ExitBook()
    e.load({"exitMinHoldS": 0, "exitLockPct": 0, "trailingMinStep": 0, "scratchMin": 0})
    s = SetBook()
    s.load(overlay(scratchS=0, indMinConfidence=0, tfMinAgree=0))
    zero = {
        "exitMinHoldS": e.min_hold_s == 0,
        "exitLockPct": e.lock_pct == 0,
        "trailingMinStep": e.trail_min_step == 0,
        "scratchMin": e.scratch_min == 0,
        "scratchS": s.scratch_s == 0,
        "indMinConfidence": s.ind_settings["minimumConfidence"] == 0,
        "tfMinAgree clamped to 1": s.ind_settings["tfMinAgree"] == 1,
    }
    d = ExitBook()
    d.load({})
    missing_default = num({}, "k", 45) == 45 and d.min_hold_s == 45 and d.lock_pct == 0.0015
    ok = all(zero.values()) and missing_default
    return ok, f"zero_kept={[k for k, v in zero.items() if v]} failed={[k for k, v in zero.items() if not v]} default_ok={missing_default}"


CHECKS = [
    ("logistics.zero-is-a-value-not-a-default", zero_is_a_value_not_a_default),
    ("logistics.overlays-share-grid-contract", overlays_share_grid_contract),
    ("logistics.desk-defaults-match-engine", desk_defaults_match_engine),
    ("logistics.desk-defaults-equal-shipped-overlay", desk_defaults_equal_shipped_overlay),
    ("logistics.desk-default-overlay-equals-shipped-x01", desk_default_overlay_equals_shipped_x01),
    ("logistics.desk-symbols-equal-engine-fixed-list", desk_symbols_equal_engine_fixed_list),
    ("logistics.desk-cards-read-effective-overlay", desk_cards_read_effective_overlay),
    ("logistics.engine-absent-defaults-agree", engine_absent_defaults_agree),
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
