"""Logistics suite: configuration, deploy pin, repository hygiene and the scripts the repo relies on."""
from __future__ import annotations

import json
import configparser
import math
import os
import re
import subprocess
import tempfile

from common import PULSE, ROOT, Skip, git, overlay

X01 = os.path.join(PULSE, "overlay-bingx-x01.json")
X02 = os.path.join(PULSE, "overlay-bingx-x02.json")
CONFIG_MODEL = os.path.join(ROOT, "src", "lib", "config-model.ts")
PIN_WANT = "b3fb147a851751303af17da950b146368ae52d9b"
PIN_BASE = "b3a9ff3c60c72864ac5558f488d7e6991bb31d76"
RESTORE_PATCH = os.path.join(ROOT, "restore", "pulse_trader.py.patch")


def overlays_share_grid_contract():
    ok_all = True
    detail = []
    for path in (X01, X02):
        ov = json.load(open(path))
        want = {"setMinStep": 2, "setStepMax": 32, "trailMinStep": 3, "histLookbackBars": 4320,
                "setMinPf": 1.25, "setGateWindow": 50, "setGateMinTrades": 0,   # owner 2026-10-28: Base double last N, long window 50 (short setGateShortN); 2026-10-29: minimum order count off
                "setMaxDdtHours": 24}   # owner 2026-10-29: drawdown time 24 h
        bad = {k: ov.get(k) for k, v in want.items() if ov.get(k) != v}
        ok_all = ok_all and not bad
        detail.append(f"{os.path.basename(path)}:{'ok' if not bad else bad}")
    return ok_all, " ".join(detail)


def desk_defaults_match_engine():
    src = open(CONFIG_MODEL, encoding="utf-8").read()
    need = [r"setMinStep: 2,", r"setStepMax: TP_STEP_DEFAULT_MAX,", r"export const TP_STEP_DEFAULT_MAX = 32;", r"export const TP_STEP_MAX = 50;", r"trailMinStep: 3,", r"histLookbackBars: 4320",
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
        return 1.25
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
    "symbolCap": "x01 uses 30 symbols by owner request; the shared desk default remains 50",
    # owner decision 2026-10-28, x01 only: minimum order size and the drawdown halt off; the desk default keeps the
    # balance-based size and the 18 % halt for every other connection
    "targetNotional": "x01 runs the minimum order size (0.2, raised to the venue minimum); desk default 2.15",
    "sizePctOfBalance": "x01 runs the minimum order size (0 = fixed notional); desk default 1 % of the balance",
    "volumeFactor": "owner 2026-11-05: x01 runs the volume factor 5.0 (earlier 0.4, then 1.0); the desk default stays the minimum 0.05",
    "minPfStepRatio": "owner 2026-11-04: x01 asks 1 + TP % x 0.1 (lower floors, more positions open); the shared desk default is 0.2",
    "minPfStepAutoHigh": "owner 2026-11-04: x01 caps every TP step's floor at 1.15; the shared desk default is uncapped (2.0)",
    "minPfStepAuto": "owner 2026-11-04: x01 runs the manual per-step floors (1 + TP % x ratio), Auto PF off; the shared desk default keeps Auto on",
    "stratTrend": "owner 2026-11-04: signals on at x01 and x02 (trend + breakout packs); the shared desk default keeps the extra packs off",
    "stratBreakout": "owner 2026-11-04: signals on at x01 and x02 (trend + breakout packs); the shared desk default keeps the extra packs off",
    "stratShortRange": "owner 2026-11-05: x01 switches Short Range off (live 0.26 on 5, replay 0.76 on 39 orders over C / B / A); the shared desk default keeps it on",
    "trailMinLockPct": "owner 2026-11-05: x01 builds no trailing pair that locks under 0.2 % (live 0.14 PF on 53 closes); the shared desk default is 0 (every pair)",
    "ddHaltPct": "owner 2026-10-28: the drawdown halt is 80 % of the active session's start equity (x01 and x02); the Bybit overlays keep 18",
}


def shipped_overlays_run_the_base_gate():
    """The Base gate is the standard: both shipped overlays and the desk default open entries only under a Base-validated
    Set (setUseHistoricGate true). x02 ran without it from 01e842b until the owner switched it back on."""
    out = {}
    for conn in ("x01", "x02"):
        ov = json.load(open(os.path.join(PULSE, f"overlay-bingx-{conn}.json"), encoding="utf-8"))
        out[conn] = ov.get("setUseHistoricGate", True)
    src = open(CONFIG_MODEL, encoding="utf-8").read()
    start = src.index("export const DEFAULT_OVERLAY")
    desk = re.search(r"^  setUseHistoricGate: (true|false)", src[start:src.index("\n};", start)], re.M)
    out["desk"] = desk.group(1) == "true" if desk else None
    return all(v is True for v in out.values()), f"setUseHistoricGate: {out}"


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


def dca_is_off_by_default_with_set_and_axis_options_off_everywhere():
    dca_src = open(os.path.join(PULSE, "dca_engine.py"), encoding="utf-8").read()
    mod_src = open(os.path.join(PULSE, "modules.py"), encoding="utf-8").read()
    hard = re.search(r"DCA_HARD_OFF\s*=\s*False", dca_src) is not None   # the kill switch exists and is not thrown
    ok_all, detail = hard, [f"kill_switch_not_thrown={hard}"]
    for path in (X01, X02):
        ov = json.load(open(path))
        good = (ov.get("dcaEnabled") is False and ov.get("stratDca") is False and ov.get("dcaAuto") is True
                and ov.get("dcaOnSets") is False and ov.get("dcaOnAxis") is False)
        ok_all = ok_all and good
        detail.append(f"{os.path.basename(path)}: enabled={ov.get('dcaEnabled')} auto={ov.get('dcaAuto')} onSets={ov.get('dcaOnSets')} onAxis={ov.get('dcaOnAxis')}")
    return ok_all, " ".join(detail)


def block_is_on_with_the_symbol_and_overall_relations_everywhere():
    """Owner 2026-11-05 ("enable block and also subtype Overall"): every shipped overlay and the desk default run Block with the symbol relation and the Overall (step) relation."""
    ok_all, detail = True, []
    for path in (X01, X02):
        ov = json.load(open(path))
        good = (ov.get("blockEnabled") is True and ov.get("stratBlock") is True
                and list(ov.get("blockRelations") or []) == ["symbol", "step"])
        ok_all = ok_all and good
        detail.append(f"{os.path.basename(path)}: enabled={ov.get('blockEnabled')} relations={ov.get('blockRelations')}")
    cm = open(os.path.join(ROOT, "src", "lib", "config-model.ts"), encoding="utf-8").read()
    desk = re.search(r'^\s*blockRelations:\s*\[([^\]]*)\],', cm, re.M)
    got = [x.strip().strip('"') for x in desk.group(1).split(",")] if desk else None
    ok_all = ok_all and got == ["symbol", "step"]
    detail.append(f"desk default={got}")
    return ok_all, " ".join(detail)


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
        # Exercise the installer's actual git-apply path as well as GNU patch.
        tree = os.path.join(td, "tree")
        source = os.path.join(tree, "server", "pulse", "pulse_trader.py")
        os.makedirs(os.path.dirname(source))
        with open(source, "w", encoding="utf-8") as fh:
            fh.write(base)
        applied = subprocess.run(["git", "-C", tree, "apply", "--unidiff-zero", RESTORE_PATCH],
                                 capture_output=True, text=True, timeout=60)
        if applied.returncode != 0:
            return False, f"git apply exit {applied.returncode}: {applied.stderr.strip()[:120]}"
        deployed = git("hash-object", source).strip()
    return got == deployed == PIN_WANT, f"patch={got[:10]} git-apply={deployed[:10]}"


def no_leftover_zest_names():
    # Check-name lists and saved verification results contain this check's name themselves.
    hits = git("grep", "-l", "-i", "zest", "--", ".", ":!scripts/regression/logistics.py",
               ":!scripts/regression/check_durations.json", ":!scripts/regression/mutant_catchers.json",
               ":!reports/regression-verification-*.json", ":!reports/engine-regression-*.json",
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
    set_fill_override = "if self.exits.enabled and self.exits.ignore_tp and not chosen:" in src   # every Set fill, base or trailing
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


def node_floor_covers_vite_and_nitro():
    """The Vercel build runs on whatever Node the project selects. Vite and nitro need ^20.19 or >=22.12, so the
    package declares a floor that satisfies both, and the floor is not lower than 22.12."""
    pkg = json.load(open(os.path.join(ROOT, "package.json"), encoding="utf-8"))
    floor = str((pkg.get("engines") or {}).get("node") or "")
    m = re.fullmatch(r">=\s*(\d+)\.(\d+)", floor.strip())
    if not m:
        return False, f"engines.node is {floor!r}, expected a >= floor"
    major, minor = int(m.group(1)), int(m.group(2))
    ok = (major, minor) >= (22, 12)
    return ok, f"engines.node={floor!r}"


def update_preserves_local_work_without_force():
    """An updater cannot silently reset a diverged or dirty checkout."""
    import sys
    result = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "test_safe_git_update.py")],
                            capture_output=True, text=True, timeout=60)
    return result.returncode == 0, (result.stdout.strip() or result.stderr.strip())[-400:]


def regression_runner_reports_progress():
    """Slow parallel checks show a heartbeat without changing ordered results."""
    import sys
    result = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "test_regression_progress.py")],
                            capture_output=True, text=True, timeout=30)
    return result.returncode == 0, (result.stdout.strip() or result.stderr.strip())[-400:]


def remote_installer_preserves_local_work_and_forwards_flags():
    """Use disposable local repositories and a fake SSH; no remote host is contacted."""
    import sys
    result = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "test_safe_remote_install.py")],
                            capture_output=True, text=True, timeout=60)
    return result.returncode == 0, (result.stdout.strip() or result.stderr.strip())[-400:]


def systemd_services_retry_without_exhausting_their_start_limit():
    """Automatic restarts remain rate-limited but cannot themselves exhaust the configured start window."""
    issues = []
    details = []
    for name in ("grok-pulse@.service", "grok-pulse-http.service", "grok-desk.service"):
        cfg = configparser.ConfigParser(interpolation=None, strict=False)
        cfg.read(os.path.join(ROOT, "deploy", name), encoding="utf-8")
        try:
            interval = cfg.getfloat("Unit", "StartLimitIntervalSec")
            burst = cfg.getint("Unit", "StartLimitBurst")
            delay = cfg.getfloat("Service", "RestartSec")
            restart = cfg.get("Service", "Restart")
        except (configparser.Error, ValueError) as exc:
            issues.append(f"{name}: {exc}")
            continue
        attempts = math.ceil(interval / delay) + 1 if delay > 0 else float("inf")
        if (restart != "always" or not 0 < delay < 60 or interval <= 0 or attempts >= burst
                or cfg.has_option("Service", "StartLimitIntervalSec")):
            issues.append(f"{name}: restart={restart}, delay={delay}, attempts={attempts}, burst={burst}")
        details.append(f"{name}: <= {attempts} attempts/{interval:g}s, limit={burst}")
    return not issues, "; ".join(issues or details)


def startup_controls_fail_closed_and_respect_disabled_lanes():
    """All service and Redis calls are stubs; this check cannot start a trader."""
    import sys
    result = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "test_startup_controls.py")],
                            capture_output=True, text=True, timeout=30)
    return result.returncode == 0, (result.stdout.strip() or result.stderr.strip())[-500:]


def install_can_keep_the_virtual_instance_off():
    """A live-only install disables/stops x02 without changing the default for other installations."""
    common = os.path.join(ROOT, "deploy", "linux-common.sh")
    script = r'''source "$1"
systemctl() {
  printf 'CMD' >> "$CTSG_STUB_LOG"; printf ' <%s>' "$@" >> "$CTSG_STUB_LOG"; printf '\n' >> "$CTSG_STUB_LOG"
  if [[ "$1" == is-enabled ]]; then [[ "$CTSG_VST_ENABLED" == 1 ]]; fi
}
redis_has_keys() { return 1; }
if [[ "$2" != preserve ]]; then enable_stack "$2"; fi
start_stack 1 "$2"
'''
    outputs = []
    with tempfile.TemporaryDirectory() as td:
        for active in ("0", "1", "preserve"):
            log = os.path.join(td, f"systemctl-{active}.log")
            env = dict(os.environ, CTSG_STUB_LOG=log, CTSG_VST_ENABLED="0" if active == "preserve" else "1")
            proc = subprocess.run(["bash", "-c", script, "--", common, active], capture_output=True, text=True, timeout=10, env=env)
            if proc.returncode:
                return False, f"stub systemctl failed: {proc.stderr[-200:]}"
            with open(log, encoding="utf-8") as fh:
                outputs.append(fh.read())
    off, default, preserved = outputs
    unit = "<grok-pulse@bingx-x02.service>"
    has = lambda out, op: any(line.startswith(f"CMD <{op}>") and unit in line for line in out.splitlines())
    ok = (has(off, "disable") and has(off, "stop") and not has(off, "enable") and not has(off, "restart")
          and has(default, "enable") and has(default, "restart") and not has(default, "disable")
          and not has(preserved, "restart") and not has(preserved, "stop")
          and "CMD <restart> <grok-pulse@bingx-x01.service>" in off)
    return ok, f"live-only x02 disabled/stopped={has(off, 'disable')}/{has(off, 'stop')}; default enabled/restarted={has(default, 'enable')}/{has(default, 'restart')}; disabled update preserved={not has(preserved, 'restart')}"


def live_only_does_not_continue_after_x02_control_failure():
    """A failed disable or stop cannot be hidden by starting the target or the live engine."""
    common = os.path.join(ROOT, "deploy", "linux-common.sh")
    script = r'''source "$1"
systemctl() {
  printf 'CMD'; printf ' <%s>' "$@"; printf '\n'
  [[ "$1" != "$CTSG_FAIL_OP" ]]
}
redis_has_keys() { return 1; }
if [[ "$2" == disable ]]; then enable_stack 0 && start_stack 1 0; else start_stack 1 0; fi
'''
    results = []
    for op in ("disable", "stop"):
        proc = subprocess.run(["bash", "-c", script, "--", common, op], capture_output=True, text=True, timeout=10,
                              env=dict(os.environ, CTSG_FAIL_OP=op))
        continued = ("<restart> <grok-pulse@bingx-x01.service>" in proc.stdout or
                     "<start> <grok-pulse.target>" in proc.stdout)
        results.append(proc.returncode != 0 and not continued)
    return all(results), f"disable/stop failures abort before live or target: {results}"


def ensure_x01_script_is_idempotent_and_starts_only_x01():
    """The container restarted five times on 2026-11-04 and the engine stayed dead for 25 minutes to 4 hours each time. scripts/ensure_x01.sh (run by the SessionStart hook in
    .claude/settings.json) starts the x01 engine when, and only when, none runs: with a x01 process present it reports and starts nothing, with none it reports missing
    (dry run here: nothing is launched by the test); a process of another connection does not count; the script names no other connection, never touches the halt,
    start equity, EQ_MIN or the STOP / PAUSE / reset files, and the hook is committed."""
    import shutil
    import time as _t
    script = os.path.join(ROOT, "scripts", "ensure_x01.sh")
    text = open(script, encoding="utf-8").read()
    code = re.sub(r"(?m)^\s*#.*$", "", text)
    hook = json.load(open(os.path.join(ROOT, ".claude", "settings.json"), encoding="utf-8"))
    cmds = [h["command"] for g in hook["hooks"]["SessionStart"] for h in g["hooks"]]
    with tempfile.TemporaryDirectory() as d:
        stub = os.path.join(d, "bin")
        os.makedirs(stub)
        mine = subprocess.Popen(["sleep", "30"], env={**os.environ, "PULSE_CONN": "bingx-x01"})
        other = subprocess.Popen(["sleep", "30"], env={**os.environ, "PULSE_CONN": "bingx-other"})
        try:
            def run(pids):
                with open(os.path.join(stub, "pgrep"), "w") as f:
                    f.write("#!/bin/sh\n" + "".join(f"echo {p}\n" for p in pids))
                os.chmod(os.path.join(stub, "pgrep"), 0o755)
                out = subprocess.run(["bash", script], env={**os.environ, "PATH": stub + os.pathsep + os.environ["PATH"], "ENSURE_X01_DRY": "1"},
                                     capture_output=True, text=True, timeout=30)
                return out.stdout.strip()
            _t.sleep(0.2)
            present, absent, foreign = run([mine.pid]), run([]), run([other.pid])
        finally:
            for pr in (mine, other):
                pr.kill()
                pr.wait()
    forbidden = ("x02", "reset-eq", "STOP", "PAUSE", "ddHalt", "EQ_MIN", "start-eq", "monitor_engines")
    ok = (present.startswith("x01 engine running") and "missing" in absent and "missing" in foreign and not any(w in code for w in forbidden)
          and cmds and all("ensure_x01.sh" in c for c in cmds) and os.access(script, os.X_OK))
    return ok, f"running -> {present!r}; none -> {absent!r}; another connection -> {foreign!r}; forbidden words in the code: {[w for w in forbidden if w in code]}; hook {cmds}"


def x01_watchdog_keeps_the_engine_running_once_and_backs_off():
    """Owner 2026-11-06 ("Always keep engine continuously running"): scripts/x01_watchdog.sh loops over scripts/ensure_x01.sh (which starts the x01 engine only when none runs),
    runs as ONE instance (pidfile), logs each start and backs off after three starts inside ten minutes (a crash loop); ensure_x01.sh starts the watchdog (not in a dry run, not
    from the loop itself); neither script names x02 or touches the halt, start equity, EQ_MIN or the control files. Run here against a stub ensure script in a temp directory."""
    import shutil
    import time as _t
    wd = os.path.join(ROOT, "scripts", "x01_watchdog.sh")
    ens = os.path.join(ROOT, "scripts", "ensure_x01.sh")
    wtext, etext = open(wd, encoding="utf-8").read(), open(ens, encoding="utf-8").read()
    strip = lambda s: re.sub(r"(?m)^\s*#.*$", "", s)
    forbidden = ("x02", "reset-eq", "STOP", "PAUSE", "ddHalt", "EQ_MIN", "start-eq", "monitor_engines")
    bad = [w for w in forbidden if w in strip(wtext) or w in strip(etext)]
    with tempfile.TemporaryDirectory() as d:
        repo, run = os.path.join(d, "repo", "scripts"), os.path.join(d, "dir")
        os.makedirs(repo)
        os.makedirs(run)
        shutil.copy(wd, os.path.join(repo, "x01_watchdog.sh"))
        with open(os.path.join(repo, "ensure_x01.sh"), "w") as f:
            f.write("#!/usr/bin/env bash\necho \"x01 engine started (pid 1)\"\n")
        env = {**os.environ, "REPO": os.path.join(d, "repo"), "PULSE_DIR": run, "WATCHDOG_EVERY": "1"}
        log = os.path.join(d, "out.log")
        with open(log, "w") as lf:
            proc = subprocess.Popen(["bash", os.path.join(repo, "x01_watchdog.sh")], env=env, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = _t.time() + 15
            while _t.time() < deadline and "backing off" not in open(log).read():
                _t.sleep(0.3)
            second = subprocess.run(["bash", os.path.join(repo, "x01_watchdog.sh")], env=env, capture_output=True, text=True, timeout=20).stdout.strip()
            out = open(log).read()
        finally:
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                pass
            proc.wait()
    starts = out.count("watchdog: x01 engine started")
    ok = (starts >= 3 and "backing off" in out and second.startswith("watchdog already running") and not bad and os.access(wd, os.X_OK)
          and "x01_watchdog.sh" in etext and "ENSURE_X01_NO_WATCHDOG" in etext and "ENSURE_X01_DRY" in etext and "ENSURE_X01_NO_WATCHDOG=1" in wtext)
    return ok, f"starts logged {starts}, backoff {'backing off' in out}, second instance {second!r}, forbidden words {bad}"


CHECKS = [
    ("logistics.ensure-x01-script-is-idempotent-and-starts-only-x01", ensure_x01_script_is_idempotent_and_starts_only_x01),
    ("logistics.x01-watchdog-keeps-the-engine-running-once-and-backs-off", x01_watchdog_keeps_the_engine_running_once_and_backs_off),
    ("logistics.startup-controls-fail-closed", startup_controls_fail_closed_and_respect_disabled_lanes),
    ("logistics.remote-installer-preserves-local-work", remote_installer_preserves_local_work_and_forwards_flags),
    ("logistics.live-only-fails-closed-on-x02-control-error", live_only_does_not_continue_after_x02_control_failure),
    ("logistics.live-only-install-keeps-x02-off", install_can_keep_the_virtual_instance_off),
    ("logistics.systemd-services-retry-within-start-limit", systemd_services_retry_without_exhausting_their_start_limit),
    ("logistics.regression-runner-reports-progress", regression_runner_reports_progress),
    ("logistics.update-preserves-local-work-without-force", update_preserves_local_work_without_force),
    ("logistics.node-floor-covers-vite-and-nitro", node_floor_covers_vite_and_nitro),
    ("logistics.zero-is-a-value-not-a-default", zero_is_a_value_not_a_default),
    ("logistics.overlays-share-grid-contract", overlays_share_grid_contract),
    ("logistics.desk-defaults-match-engine", desk_defaults_match_engine),
    ("logistics.desk-defaults-equal-shipped-overlay", desk_defaults_equal_shipped_overlay),
    ("logistics.desk-default-overlay-equals-shipped-x01", desk_default_overlay_equals_shipped_x01),
    ("logistics.shipped-overlays-run-the-base-gate", shipped_overlays_run_the_base_gate),
    ("logistics.desk-symbols-equal-engine-fixed-list", desk_symbols_equal_engine_fixed_list),
    ("logistics.desk-cards-read-effective-overlay", desk_cards_read_effective_overlay),
    ("logistics.engine-absent-defaults-agree", engine_absent_defaults_agree),
    ("logistics.no-ratio-as-pf", no_ratio_as_pf),
    ("logistics.conn-pinned-per-connection", conn_pinned_per_connection),
    ("logistics.no-fabricated-pass-and-one-entry-conf", no_fabricated_pass_and_one_entry_conf),
    ("logistics.dca-off-by-default-with-set-and-axis-options-off", dca_is_off_by_default_with_set_and_axis_options_off_everywhere),
    ("logistics.block-is-on-with-the-symbol-and-overall-relations", block_is_on_with_the_symbol_and_overall_relations_everywhere),
    ("logistics.deploy-pin-matches-engine", deploy_pin_matches_engine),
    ("logistics.restore-patch-reproduces-pin", restore_patch_reproduces_pin),
    ("logistics.no-leftover-zest-names", no_leftover_zest_names),
    ("logistics.no-secret-literals", no_secret_literals),
    ("logistics.package-scripts-exist", package_scripts_exist),
    ("logistics.engine-modules-compile", engine_modules_compile),
]
