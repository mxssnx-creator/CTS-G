#!/usr/bin/env python3
"""Independent 20h historic calc — no Pulse.run() / grok-pulse@ required.

Walks every selected pack × SL:TP × trail × step across all symbols on 1m
bars, scores PositionCost PF + drawdown-time, and ranks for positive PF
with low SL and low DD. Public BingX klines; synth fallback if the venue
is unreachable (sandbox / tests).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Dict, List, Optional, Sequence, Tuple

from position_cost import SL_TP_RATIOS, SL_TP_MIN, SL_TP_MAX, SL_TP_STEP, last_n_cost_pf, row_net_pnl, filter_side
from set_engine import (
    DIRECTIONS,
    IND_KINDS,
    LOOKBACK_MAX,
    SetBook,
    drawdown_time,
    drawdown_time_by_symbol,
    synth_trend,
)

DEFAULT_SYMBOLS = [
    "SOL-USDT",
    "XRP-USDT",
    "HYPE-USDT",
    "JUP-USDT",
    "ETC-USDT",
    "TRX-USDT",
    "DOGE-USDT",
    "APT-USDT",
    "ENA-USDT",
    "LDO-USDT",
    "1000PEPE-USDT",
    "KAS-USDT",
]
HOURS_DEFAULT = 20
BARS_PER_HOUR = 60
KLINE_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/klines"
KLINE_URL_V3 = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"

# Coordinated low-DD books. Block ON, DCA OFF, SL 0.3 or 0.6, tight DD.
_SHARED = {
    "blockEnabled": True,
    "stratBlock": True,
    "blockMaxStack": 3,
    "blockVolumeRatio": 1.0,
    "blockProfitFactorRatio": 1.25,
    "dcaEnabled": False,
    "stratDca": False,
    "controlOrders": True,
    "histEnabled": True,
    "setUseHistoricGate": True,
    "setStrictGate": True,
    "setAutoDeact": True,
    "setReactivate": True,
    "setMinSamples": 12,
    "exitEnabled": True,
    "exitIgnoreTp": True,
    "exitBestOf": True,
    "exitLockOn": True,
    "exitPeakOn": True,
    "indEnabled": True,
    "stratIndications": True,
    "axisPrevEnabled": True,
    "axisLastEnabled": True,
    "axisContEnabled": True,
    "axisPauseEnabled": True,
    "slToTpAuto": True,
    "trailAuto": True,
    "trailRecalcGive": True,
}

PRESETS: List[Dict[str, Any]] = [
    {
        "id": "tight-guard",
        "name": "Tight Guard",
        "hint": "Lowest SL 0.3 · min step 12 · 20h · max DD 15m",
        "why": "Best low-drawdown book: SL 0.3, high min-step, 15m DD cut, Block on / DCA off.",
        "recommended": True,
        "patch": {
            **_SHARED,
            "slToTpRatio": 0.3,
            "slMinPct": 0.2,
            "slMaxPct": 0.6,
            "setMinStep": 12,
            "setStepMax": 18,
            "stratTrailing": True,
            "trailArmPct": 0.3,
            "trailGivePct": 0.1,
            "trailArmMin": 0.3,
            "trailArmMax": 0.3,
            "setMinPf": 1.12,
            "minPf": 1.12,
            "baseMinPf": 1.08,
            "mainMinPf": 1.10,
            "realMinPf": 1.12,
            "setMaxDdTimeS": 900,
            "maxDdTimeS": 900,
            "histLookbackBars": 1200,
            "stratGeneral": True,
        },
    },
    {
        "id": "low-dd-core",
        "name": "Low DD Core",
        "hint": "SL 0.6 · step 10–16 · trail 0.6:0.2 · 20h",
        "why": "Default coordinated live book. SL 0.6 vs TP, Block remainder 1×, DCA off.",
        "recommended": True,
        "patch": {
            **_SHARED,
            "slToTpRatio": 0.6,
            "slMinPct": 0.2,
            "slMaxPct": 0.8,
            "setMinStep": 10,
            "setStepMax": 16,
            "stratTrailing": True,
            "trailArmPct": 0.6,
            "trailGivePct": 0.2,
            "trailArmMin": 0.6,
            "trailArmMax": 0.6,
            "setMinPf": 1.10,
            "minPf": 1.10,
            "baseMinPf": 1.05,
            "mainMinPf": 1.08,
            "realMinPf": 1.10,
            "setMaxDdTimeS": 1200,
            "maxDdTimeS": 1200,
            "histLookbackBars": 1200,
            "stratGeneral": True,
        },
    },
    {
        "id": "balanced-coord",
        "name": "Balanced Coord",
        "hint": "Both packs · axes on · SL 0.6 · step 8–16",
        "patch": {
            **_SHARED,
            "slToTpRatio": 0.6,
            "setMinStep": 8,
            "setStepMax": 16,
            "stratTrailing": True,
            "trailArmPct": 0.6,
            "trailGivePct": 0.2,
            "trailArmMin": 0.6,
            "trailArmMax": 0.6,
            "setMinPf": 1.10,
            "minPf": 1.10,
            "baseMinPf": 1.05,
            "mainMinPf": 1.08,
            "realMinPf": 1.10,
            "setMaxDdTimeS": 1800,
            "histLookbackBars": 720,
            "stratGeneral": True,
        },
    },
    {
        "id": "trail-scout",
        "name": "Trail Scout",
        "hint": "Low SL 0.3 · trail 0.3–0.9 · step 10–18",
        "patch": {
            **_SHARED,
            "slToTpRatio": 0.3,
            "setMinStep": 10,
            "setStepMax": 18,
            "stratTrailing": True,
            "trailArmPct": 0.3,
            "trailGivePct": 0.1,
            "trailArmMin": 0.3,
            "trailArmMax": 0.9,
            "setMinPf": 1.10,
            "minPf": 1.10,
            "setMaxDdTimeS": 1200,
            "histLookbackBars": 1200,
            "stratGeneral": True,
        },
    },
    {
        "id": "indication-lead",
        "name": "Indication Lead",
        "hint": "Indications only · agreement 0.7 · SL 0.6",
        "patch": {
            **_SHARED,
            "slToTpRatio": 0.6,
            "setMinStep": 10,
            "setStepMax": 18,
            "stratTrailing": True,
            "trailArmPct": 0.6,
            "trailGivePct": 0.2,
            "trailArmMin": 0.6,
            "trailArmMax": 0.6,
            "stratGeneral": False,
            "stratIndications": True,
            "indMinAgreement": 0.7,
            "indMinConfidence": 0.65,
            "setMinPf": 1.10,
            "minPf": 1.10,
            "setMaxDdTimeS": 1500,
            "histLookbackBars": 1200,
        },
    },
    {
        "id": "block-stack",
        "name": "Block Stack",
        "hint": "Block stack 3 · vr 1 · SL 0.6 · DCA off",
        "patch": {
            **_SHARED,
            "slToTpRatio": 0.6,
            "setMinStep": 8,
            "setStepMax": 14,
            "stratTrailing": True,
            "trailArmPct": 0.6,
            "trailGivePct": 0.2,
            "blockMaxStack": 3,
            "blockVolumeRatio": 1.0,
            "setMinPf": 1.08,
            "minPf": 1.10,
            "setMaxDdTimeS": 1800,
            "histLookbackBars": 720,
            "stratGeneral": True,
        },
    },
    {
        "id": "strict-gate",
        "name": "Strict Gate",
        "hint": "PF 1.10/1.12/1.15 · SL 0.3 · max DD 10m",
        "patch": {
            **_SHARED,
            "slToTpRatio": 0.3,
            "setMinStep": 12,
            "setStepMax": 22,
            "stratTrailing": True,
            "trailArmPct": 0.3,
            "trailGivePct": 0.1,
            "trailArmMin": 0.3,
            "trailArmMax": 0.3,
            "baseMinPf": 1.10,
            "mainMinPf": 1.12,
            "realMinPf": 1.15,
            "setMinPf": 1.15,
            "minPf": 1.15,
            "setMaxDdTimeS": 600,
            "maxDdTimeS": 600,
            "histLookbackBars": 1200,
            "stratGeneral": True,
        },
    },
    {
        "id": "wide-scan",
        "name": "Wide Scan",
        "hint": "Both packs · step 8–22 · full trail · 20h",
        "patch": {
            **_SHARED,
            "slToTpRatio": 0.6,
            "setMinStep": 8,
            "setStepMax": 22,
            "stratTrailing": True,
            "trailArmPct": 0.6,
            "trailGivePct": 0.2,
            "trailArmMin": 0.3,
            "trailArmMax": 1.5,
            "setMinPf": 1.08,
            "minPf": 1.10,
            "setMaxDdTimeS": 1800,
            "histLookbackBars": 1200,
            "stratGeneral": True,
        },
    },
]
for _p in PRESETS:
    _pt = _p["patch"]
    _pt["setMinStep"] = 2
    _pt["setStepMax"] = 4
    _pt["setMinPf"] = 1.25
    _pt["minPf"] = 1.25
    _pt["baseMinPf"] = 1.25
    _pt["mainMinPf"] = 1.25
    _pt["realMinPf"] = 1.25
    _pt["blockProfitFactorRatio"] = 1.25
    _pt["dcaMinPf"] = 1.25
    _pt["exitMinPf"] = 1.25


def hours_to_bars(hours: Any, default: int = HOURS_DEFAULT) -> int:
    try:
        h = float(hours)
    except Exception:
        h = float(default)
    h = max(2.0, min(24.0, h))
    return max(120, min(LOOKBACK_MAX, int(round(h * BARS_PER_HOUR))))


def job_path() -> str:
    env = (os.environ.get("CTS_HIST_CALC_PATH") or "").strip()
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, "/opt/grok-x01-pulse", "/tmp"):
        try:
            if os.path.isdir(d) and os.access(d, os.W_OK):
                return os.path.join(d, "hist-calc.json")
        except Exception:
            continue
    return os.path.join(here, "hist-calc.json")


def req_path() -> str:
    return job_path().replace("hist-calc.json", "hist-calc-req.json")


def _atomic_write(path: str, blob: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(blob, f, separators=(",", ":"))
    os.replace(tmp, path)


def read_job() -> Dict[str, Any]:
    p = job_path()
    if not os.path.exists(p):
        return idle_job()
    try:
        with open(p) as f:
            j = json.load(f)
        if isinstance(j, dict):
            return j
    except Exception:
        pass
    return idle_job()


def idle_job() -> Dict[str, Any]:
    return {
        "ok": True,
        "phase": "idle",
        "pct": 0.0,
        "detail": "no calc yet",
        "hours": HOURS_DEFAULT,
        "lookback": hours_to_bars(HOURS_DEFAULT),
        "symbols": [],
        "options": default_options(),
        "coverage": {},
        "rows": [],
        "bySymbol": [],
        "byDirection": {},
        "byStrategy": {},
        "kinds": {},
        "winner": None,
        "presets": public_presets(),
        "error": "",
        "elapsedMs": 0,
        "startedAt": 0,
        "finishedAt": 0,
        "source": "",
        "independent": True,
        "independence": {
            "symbol": True,
            "direction": True,
            "indication": True,
            "strategy": True,
            "config": True,
            "costSubtracted": True,
            "async": True,
            "partial": True,
        },
    }


def public_presets() -> List[Dict[str, Any]]:
    out = []
    for p in PRESETS:
        patch = p["patch"]
        out.append({
            "id": p["id"],
            "name": p["name"],
            "hint": p["hint"],
            "why": p.get("why") or p["hint"],
            "recommended": bool(p.get("recommended")),
            "sl": patch.get("slToTpRatio"),
            "minStep": patch.get("setMinStep"),
            "stepMax": patch.get("setStepMax"),
            "trail": f"{patch.get('trailArmPct')}:{patch.get('trailGivePct')}",
            "block": True,
            "dca": False,
            "minPf": patch.get("setMinPf"),
            "maxDdS": patch.get("setMaxDdTimeS"),
            "lookback": patch.get("histLookbackBars"),
        })
    return out


def default_options() -> Dict[str, Any]:
    return {
        "hours": HOURS_DEFAULT,
        "minStep": 2,
        "stepMax": 4,
        "trailing": True,
        "stratBlock": True,
        "stratDca": False,
        "stratIndications": True,
        "stratGeneral": True,
        "allConfigs": True,
        "allSymbols": False,
        "indTypeSignals": True,
        "indTypeState": True,
        "indTypeDirection": True,
        "indTypeMove": True,
        "indTypeActive": True,
        "indTypeCommon": True,
    }


def parse_options(body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body = body if isinstance(body, dict) else {}
    opt = default_options()
    if body.get("hours") is not None:
        try:
            opt["hours"] = max(2, min(24, int(body["hours"])))
        except Exception:
            pass
    for k, lo, hi in (("minStep", 2, 22), ("stepMax", 2, 22)):
        if body.get(k) is not None:
            try:
                opt[k] = max(lo, min(hi, int(body[k])))
            except Exception:
                pass
    if opt["stepMax"] < opt["minStep"]:
        opt["stepMax"] = opt["minStep"]
    for k in ("trailing", "stratBlock", "stratDca", "stratIndications", "stratGeneral", "allConfigs", "allSymbols",
              "indTypeSignals", "indTypeState", "indTypeDirection", "indTypeMove", "indTypeActive", "indTypeCommon"):
        if k in body:
            opt[k] = bool(body[k])
    if not opt["stratIndications"] and not opt["stratGeneral"]:
        opt["stratIndications"] = True
    return opt


def parse_klines(data: Any) -> List[List[float]]:
    bars: List[List[float]] = []
    if not isinstance(data, list):
        return bars
    for b in data:
        try:
            if isinstance(b, dict):
                bars.append([
                    float(b.get("open") or b.get("o") or 0),
                    float(b.get("high") or b.get("h") or 0),
                    float(b.get("low") or b.get("l") or 0),
                    float(b.get("close") or b.get("c") or 0),
                    float(b.get("volume") or b.get("v") or 0),
                ])
            elif isinstance(b, (list, tuple)) and len(b) >= 5:
                # BingX list: [ts, o, h, l, c, v] or [o,h,l,c,v]
                if len(b) >= 6:
                    bars.append([float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])])
                else:
                    bars.append([float(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4])])
        except Exception:
            continue
    return [x for x in bars if x[0] > 0 and x[3] > 0 and x[1] > 0 and x[2] > 0]


def fetch_klines(symbol: str, limit: int = 1200, timeout: float = 8.0) -> List[List[float]]:
    limit = max(60, min(LOOKBACK_MAX, int(limit)))
    qs = urllib.parse.urlencode({"symbol": symbol, "interval": "1m", "limit": str(limit)})
    for base in (KLINE_URL, KLINE_URL_V3):
        try:
            req = urllib.request.Request(f"{base}?{qs}", headers={"User-Agent": "cts-g-hist-calc/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode() or "{}")
            bars = parse_klines(body.get("data") if isinstance(body, dict) else body)
            if len(bars) >= 60:
                return bars[-limit:]
        except Exception:
            continue
    return []


def load_universe() -> List[str]:
    names: List[str] = []
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (
        os.path.join(os.path.dirname(job_path()), "universe.json"),
        os.path.join(here, "universe.json"),
        "/opt/grok-x01-pulse/universe.json",
    ):
        try:
            raw = json.load(open(path))
        except Exception:
            continue
        rows = raw if isinstance(raw, list) else (raw.get("symbols") or raw.get("universe") or [])
        for s in rows:
            if isinstance(s, dict):
                s = s.get("symbol") or s.get("s") or ""
            t = str(s or "").strip().upper().replace("_", "-")
            if t.endswith("USDT") and not t.endswith("-USDT"):
                t = t[:-4] + "-USDT"
            if t.endswith("-USDT"):
                names.append(t)
        if names:
            break
    seen = set()
    out: List[str] = []
    for s in names:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def resolve_symbols(body: Optional[Dict[str, Any]] = None) -> List[str]:
    body = body if isinstance(body, dict) else {}
    opt = parse_options(body)
    raw = body.get("symbols")
    if isinstance(raw, str):
        raw = [raw]
    names: List[str] = []
    wild = bool(opt.get("allSymbols")) or bool(body.get("allSymbols"))
    if isinstance(raw, list) and raw:
        for s in raw:
            t = str(s or "").strip().upper().replace("_", "-")
            if t in ("*", "ALL", ""):
                wild = True
                continue
            if t.endswith("USDT") and not t.endswith("-USDT"):
                t = t[:-4] + "-USDT"
            if t.endswith("-USDT"):
                names.append(t)
    if wild or not names:
        uni = load_universe()
        names = uni or list(DEFAULT_SYMBOLS)
    seen = set()
    out: List[str] = []
    cap = 48 if wild else 24
    for s in names:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= cap:
            break
    return out or list(DEFAULT_SYMBOLS)


def overlay_from_options(opt: Dict[str, Any], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    lookback = hours_to_bars(opt.get("hours"))
    ov: Dict[str, Any] = {
        "histEnabled": True,
        "histLookbackBars": lookback,
        "histMinBars": min(120, lookback),
        "histWarmup": 30,
        "setUseHistoricGate": True,
        "setStrictGate": True,
        "setAutoDeact": True,
        "setMinSamples": 8,
        "setMinPf": 1.25,
        "setMaxDdTimeS": 1800,
        "setMinStep": int(opt.get("minStep") or 3),
        "setStepMax": int(opt.get("stepMax") or 22),
        "stratTrailing": bool(opt.get("trailing", True)),
        "stratIndications": bool(opt.get("stratIndications", True)),
        "stratGeneral": bool(opt.get("stratGeneral", True)),
        "stratBlock": bool(opt.get("stratBlock", True)),
        "blockEnabled": bool(opt.get("stratBlock", True)),
        "dcaEnabled": bool(opt.get("stratDca", False)),
        "stratDca": bool(opt.get("stratDca", False)),
        "histSimulateBlock": True,
        "histSimulateDca": True,
        "blockVolumeRatio": 1.0,
        "blockMaxStack": 3,
        "indTypeState": bool(opt.get("indTypeState", True)),
        "indTypeSignals": bool(opt.get("indTypeSignals", True)),
        "indTypeDirection": bool(opt.get("indTypeDirection", True)),
        "indTypeMove": bool(opt.get("indTypeMove", True)),
        "indTypeActive": bool(opt.get("indTypeActive", True)),
        "indTypeCommon": bool(opt.get("indTypeCommon", True)),
        "indTypeTrend": bool(opt.get("indTypeTrend", True)),
        "indTypeBreak": bool(opt.get("indTypeBreak", True)),
        "trailArmMin": 0.3,
        "trailArmMax": 1.5,
        "trailGiveMin": 0.1,
        "trailGiveMax": 0.5,
        "trailRecalcGive": False,
        "exitIgnoreTp": True,
        "setHonorTp": True,
        "positionCostPct": 0.15,
        "setCooldownBars": 3,
        "setScratchMin": 0.0016,
    }
    if opt.get("allConfigs", True):
        ov["slToTpMin"] = float(opt.get("slToTpMin") or SL_TP_MIN)
        ov["slToTpMax"] = float(opt.get("slToTpMax") or SL_TP_MAX)
        ov["slToTpStep"] = float(opt.get("slToTpStep") or SL_TP_STEP)
    else:
        ov["slToTpRatios"] = [0.6]
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k in ("histLookbackBars", "histMinBars"):
                continue
            ov[k] = v
        ov["histLookbackBars"] = lookback
        ov["histMinBars"] = min(int(ov.get("histMinBars") or 120), lookback)
    ov["slToTpMin"] = SL_TP_MIN
    ov["slToTpMax"] = SL_TP_MAX
    ov["slToTpStep"] = SL_TP_STEP
    ov["trailArmMin"] = 0.3
    ov["trailArmMax"] = 1.5
    ov["trailGiveMin"] = 0.1
    ov["trailGiveMax"] = 0.5
    return ov


def rank_tuple(row: Dict[str, Any]) -> Tuple:
    pf = float(row.get("last15Ratio") or 0)
    n = int(row.get("last15N") or 0)
    dd = float(row.get("maxDdS") or 0)
    sl = float(row.get("slRatio") or 9)
    exp = float(row.get("expectancy") or 0)
    validated = n >= 8 and pf + 1e-9 >= 1.0
    return (0 if validated else 1, -pf, dd, sl, -exp, -int(row.get("n") or 0))


def set_row(st: Any, side: str = "") -> Dict[str, Any]:
    want = str(side or "").upper()
    blob = (getattr(st, "by_side", None) or {}).get(want) if want in DIRECTIONS else None

    def g(key: str, fallback: Any) -> Any:
        if blob is not None:
            return blob.get(key, fallback)
        return getattr(st, key, fallback)

    n15 = int(g("last15_n", getattr(st, "last15_n", 0)) or 0)
    pf = float(g("last15_ratio", getattr(st, "last15_ratio", 0)) or 0)
    n = int(g("n", getattr(st, "n", 0)) or 0)
    by_side_pub = None
    if not want:
        raw = getattr(st, "by_side", None) or {}
        by_side_pub = {
            d: {
                "n": int(v.get("n") or 0),
                "pf": round(float(v.get("last15_ratio") or 0), 4),
                "last15N": int(v.get("last15_n") or 0),
                "maxDdS": float(v.get("max_dd_s") or 0),
                "expectancy": float(v.get("expectancy") or 0),
                "validated": bool(v.get("validated")),
                "costSubtracted": True,
            }
            for d, v in raw.items()
            if isinstance(v, dict)
        }
    return {
        "id": st.id if not want else f"{st.id}:{want.lower()}",
        "kind": st.kind,
        "pack": st.pack,
        "direction": want or "BOTH",
        "slRatio": st.sl_ratio,
        "trailKey": st.trail_key,
        "trailArm": st.trail_arm,
        "trailGive": st.trail_give,
        "step": st.step,
        "n": n,
        "wins": int(g("wins", st.wins) or 0),
        "wr": float(g("wr", st.wr) or 0),
        "last15Ratio": round(pf, 4),
        "last15N": n15,
        "last15R": round(float(g("last15_r", st.last15_r) or 0), 4),
        "last25AvgR": round(float(g("last25_avg_r", st.last25_avg_r) or 0), 4),
        "maxDdS": float(g("max_dd_s", st.max_dd_s) or 0),
        "avgDdS": float(g("avg_dd_s", st.avg_dd_s) or 0),
        "ddEpisodes": int(g("dd_episodes", st.dd_episodes) or 0),
        "expectancy": float(g("expectancy", st.expectancy) or 0),
        "netAvg": float(g("net_avg", g("expectancy", st.expectancy)) or 0),
        "avgHoldS": float(g("avg_hold_s", st.avg_hold_s) or 0),
        "classicPf": float(g("classic_all", st.classic_all) or 0),
        "active": bool(g("active", st.active)),
        "deactReason": st.deact_reason if not want else "",
        "validated": n15 >= 8 and pf + 1e-9 >= 1.0,
        "lowSl": st.sl_ratio <= 0.6 + 1e-9 or st.kind == "trail",
        "costSubtracted": True,
        "bySide": by_side_pub,
    }


def direction_rollup(book: SetBook, hist: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    tapes: List[Dict[str, Any]] = []
    if hist:
        for rows in hist.values():
            tapes.extend(rows)
    else:
        for st in book.by_idx:
            tapes.extend(st.hist)
    out: Dict[str, Any] = {}
    for d in DIRECTIONS:
        sub = filter_side(tapes, d)
        pf = last_n_cost_pf(sub, book.pf_n, book.cost_pct)
        nets = [row_net_pnl(r, book.cost_pct) for r in sub]
        wins = sum(1 for x in nets if x > 0)
        decided = sum(1 for x in nets if x != 0)
        dd = drawdown_time_by_symbol(sub) if sub else {"maxS": 0.0, "avgS": 0.0}
        out[d] = {
            "direction": d,
            "n": len(sub),
            "pf": round(float(pf["ratio"]), 4),
            "netAvg": round(float(pf.get("netAvg") or 0), 6),
            "last15N": int(pf["count"]),
            "maxDdS": round(float(dd.get("maxS") or 0), 1),
            "wr": round(100.0 * wins / decided, 1) if decided else 0.0,
            "validated": int(pf["count"]) >= 8 and float(pf["ratio"]) + 1e-9 >= 1.0,
            "costSubtracted": True,
        }
    return out


def strategy_rollup(book: SetBook, hist: Optional[Dict[str, List[Dict[str, Any]]]] = None, strat: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """Independent pack / kind / pack:kind books plus Block / DCA volume tapes. Cost subtracted."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for st in book.by_idx:
        rows = list((hist or {}).get(st.id) or st.hist)
        groups.setdefault(st.pack, []).extend(rows)
        groups.setdefault(st.kind, []).extend(rows)
        groups.setdefault(f"{st.pack}:{st.kind}", []).extend(rows)
        groups.setdefault("core", []).extend(rows)
    for key in ("block", "dca"):
        groups.setdefault(key, list((strat or {}).get(key) or []))
    if strat:
        for key, tape in strat.items():
            if key in ("block", "dca"):
                continue
            if tape:
                groups[str(key)] = list(tape)
    out: Dict[str, Any] = {}
    for key, tape in groups.items():
        win = book.pf_n
        if key in ("block", "dca", "core"):
            win = max(book.pf_n, min(80, len(tape) or 1))
        pf = last_n_cost_pf(tape, win, book.cost_pct)
        nets = [row_net_pnl(r, book.cost_pct) for r in tape]
        wins = sum(1 for x in nets if x > 0)
        decided = sum(1 for x in nets if x != 0)
        dd = drawdown_time_by_symbol(tape) if tape else {"maxS": 0.0, "avgS": 0.0}
        by_dir: Dict[str, Any] = {}
        for d in DIRECTIONS:
            sub = filter_side(tape, d)
            if not sub:
                continue
            spf = last_n_cost_pf(sub, book.pf_n, book.cost_pct)
            by_dir[d] = {
                "n": len(sub),
                "pf": round(float(spf["ratio"]), 4),
                "netAvg": round(float(spf.get("netAvg") or 0), 6),
                "validated": int(spf["count"]) >= 8 and float(spf["ratio"]) + 1e-9 >= 1.0,
                "costSubtracted": True,
            }
        out[key] = {
            "strategy": key,
            "n": len(tape),
            "pf": round(float(pf["ratio"]), 4),
            "netAvg": round(float(pf.get("netAvg") or 0), 6),
            "last15N": int(pf["count"]),
            "maxDdS": round(float(dd.get("maxS") or 0), 1),
            "wr": round(100.0 * wins / decided, 1) if decided else 0.0,
            "validated": int(pf["count"]) >= 8 and float(pf["ratio"]) + 1e-9 >= 1.0,
            "costSubtracted": True,
            "bySide": by_dir,
        }
    return out


def expand_rows(book: SetBook) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for st in book.by_idx:
        rows.append(set_row(st))
        for d in DIRECTIONS:
            side = set_row(st, d)
            if int(side.get("n") or 0) > 0:
                rows.append(side)
    rows.sort(key=rank_tuple)
    return rows


def symbol_rollup(book: SetBook, hist: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> List[Dict[str, Any]]:
    by: Dict[str, List[Dict[str, Any]]] = {}
    tapes: List[List[Dict[str, Any]]]
    if hist:
        tapes = list(hist.values())
    else:
        tapes = [list(st.hist) for st in book.by_idx]
    for tape in tapes:
        for r in tape:
            s = str(r.get("symbol") or "")
            if s:
                by.setdefault(s, []).append(r)
    out: List[Dict[str, Any]] = []
    for s, tape in by.items():
        pf = last_n_cost_pf(tape, book.pf_n, book.cost_pct)
        # One symbol's fills still mix many sets — DDT per set, then max.
        by_set: Dict[str, List[Dict[str, Any]]] = {}
        for r in tape:
            by_set.setdefault(str(r.get("set_id") or r.get("setId") or "?"), []).append(r)
        parts = [drawdown_time(t) for t in by_set.values() if t]
        if parts:
            dd = {
                "maxS": max(p["maxS"] for p in parts),
                "avgS": sum(p["avgS"] for p in parts) / len(parts),
            }
        else:
            dd = drawdown_time(tape)
        nets = [row_net_pnl(r, book.cost_pct) for r in tape]
        wins = sum(1 for x in nets if x > 0)
        decided = sum(1 for x in nets if x != 0)
        by_dir: Dict[str, Any] = {}
        for d in DIRECTIONS:
            sub = filter_side(tape, d)
            if not sub:
                continue
            spf = last_n_cost_pf(sub, book.pf_n, book.cost_pct)
            by_dir[d] = {
                "n": len(sub),
                "pf": round(float(spf["ratio"]), 4),
                "netAvg": round(float(spf.get("netAvg") or 0), 6),
                "validated": int(spf["count"]) >= 8 and float(spf["ratio"]) + 1e-9 >= 1.0,
            }
        out.append({
            "symbol": s,
            "n": len(tape),
            "pf": round(float(pf["ratio"]), 4),
            "netAvg": round(float(pf.get("netAvg") or 0), 6),
            "last15N": int(pf["count"]),
            "maxDdS": round(float(dd.get("maxS") or 0), 1),
            "avgDdS": round(float(dd.get("avgS") or 0), 1),
            "wr": round(100.0 * wins / decided, 1) if decided else 0.0,
            "validated": int(pf["count"]) >= 8 and float(pf["ratio"]) + 1e-9 >= 1.0,
            "costSubtracted": True,
            "bySide": by_dir,
        })
    out.sort(key=lambda r: (0 if r["validated"] else 1, -r["pf"], r["maxDdS"]))
    return out


def winner_patch(row: Optional[Dict[str, Any]], opt: Dict[str, Any], by_strat: Optional[Dict[str, Any]] = None, source: str = "") -> Dict[str, Any]:
    lookback = hours_to_bars(opt.get("hours"))
    patch: Dict[str, Any] = {
        "histLookbackBars": lookback,
        "histMinBars": min(120, lookback),
        "histEnabled": True,
        "setUseHistoricGate": True,
        "setStrictGate": True,
        "stratTrailing": bool(opt.get("trailing", True)),
        "stratBlock": bool(opt.get("stratBlock", True)),
        "blockEnabled": bool(opt.get("stratBlock", True)),
        "dcaEnabled": bool(opt.get("stratDca", False)),
        "stratDca": bool(opt.get("stratDca", False)),
        "blockVolumeRatio": 1.0,
        "blockMaxStack": 3,
        "dcaStepDistancesPct": [1.2, 1.6, 2.0, 2.4],
        "dcaStepVolumeMultipliers": [1.5, 2.0, 2.3, 2.5],
        "dcaMaxSteps": 4,
        "dcaCooldownSeconds": 45,
        "stratIndications": bool(opt.get("stratIndications", True)),
        "stratGeneral": bool(opt.get("stratGeneral", True)),
        "setMinStep": int(opt.get("minStep") or 2),
        "setStepMax": int(opt.get("stepMax") or 4),
    }
    if not row:
        return patch
    sl = float(row.get("slRatio") or 0.6)
    if sl > 0:
        patch["slToTpRatio"] = sl
    step = int(row.get("step") or 0)
    if step >= 3:
        patch["setMinStep"] = max(int(opt.get("minStep") or 2), step)
    arm = float(row.get("trailArm") or 0)
    give = float(row.get("trailGive") or 0)
    if arm > 0:
        patch["trailArmPct"] = arm
        patch["trailGivePct"] = give or round(arm / 3.0, 2)
        patch["stratTrailing"] = True
    pack = str(row.get("pack") or "")
    if pack == "indications":
        patch["stratIndications"] = True
    if pack == "general":
        patch["stratGeneral"] = True
    block = (by_strat or {}).get("block") or {}
    dca = (by_strat or {}).get("dca") or {}
    dca_pf = float(dca.get("pf") or 0)
    dca_ok = (
        bool(dca.get("validated"))
        and dca_pf >= 1.25
        and float(dca.get("netAvg") or 0) > 0
        and float(dca.get("maxDdS") or 9e9) <= 1800
        and float(dca.get("wr") or 0) < 92.0
        and str(source or "") not in ("synth",)
    )
    block_pf = float(block.get("pf") or 0)
    block_ok = bool(block.get("validated")) and block_pf >= 1.0 and float(block.get("netAvg") or 0) >= 0
    # Stable continuous: Block remainder stays on when it doesn't destroy PF.
    # DCA only when its independent tape is validated, +EV, PF≥1.25, DD capped.
    patch["blockEnabled"] = True
    patch["stratBlock"] = True
    patch["dcaEnabled"] = bool(dca_ok)
    patch["stratDca"] = bool(dca_ok)
    if block_ok:
        patch["blockVolumeRatio"] = 1.0
        patch["blockMaxStack"] = 3
    return patch


def fetch_one(symbol: str, lookback: int, synth: bool, i: int) -> Tuple[str, List[List[float]], str]:
    got: List[List[float]] = []
    source = "live"
    if not synth:
        got = fetch_klines(symbol, lookback)
    if len(got) < min(80, max(40, lookback // 2)):
        drift = 0.12 if (i % 2 == 0) else -0.10
        got = synth_trend(lookback, 40.0 + i * 3.0, drift, 0.04)
        source = "synth" if synth else "mixed"
    return symbol, got[-lookback:], source


def load_bars(
    symbols: Sequence[str],
    lookback: int,
    synth: bool = False,
    on_prog: Optional[Any] = None,
    workers: int = 4,
) -> Tuple[Dict[str, List[List[float]]], str]:
    """Parallel fetch. Prefer pipeline_replay in run_calc so bars are not all held."""
    bars: Dict[str, List[List[float]]] = {}
    sources: List[str] = []
    names = list(symbols)
    w = max(1, min(int(workers or 4), 8, len(names) or 1))

    def _go(i: int, s: str) -> Tuple[str, List[List[float]], str]:
        return fetch_one(s, lookback, synth, i)

    if w <= 1:
        for i, s in enumerate(names):
            if on_prog:
                on_prog("fetch", 2.0 + (i / max(1, len(names))) * 18.0, f"fetch {s} {i + 1}/{len(names)}")
            sym, got, src = _go(i, s)
            bars[sym] = got
            sources.append(src)
    else:
        with ThreadPoolExecutor(max_workers=w, thread_name_prefix="hist-fetch") as pool:
            futs = {pool.submit(_go, i, s): s for i, s in enumerate(names)}
            done = 0
            for fut in as_completed_safe(futs):
                sym, got, src = fut.result()
                bars[sym] = got
                sources.append(src)
                done += 1
                if on_prog:
                    on_prog("fetch", 2.0 + (done / max(1, len(names))) * 18.0, f"fetch {sym} {done}/{len(names)}")
    if not sources or all(x == "synth" for x in sources):
        source = "synth"
    elif any(x != "live" for x in sources):
        source = "mixed"
    else:
        source = "live"
    return bars, source


def as_completed_safe(futs: Dict) -> Any:
    from concurrent.futures import as_completed
    return as_completed(futs)


def pipeline_symbols(
    symbols: Sequence[str],
    lookback: int,
    synth: bool,
    workers: int,
    on_item: Any,
    on_prog: Optional[Any] = None,
) -> str:
    """Fetch in flight, hand each tape to on_item, never queue the whole universe."""
    names = list(enumerate(symbols))
    w = max(1, min(int(workers or 4), 8, len(names) or 1))
    sources: List[str] = []
    it = iter(names)
    inflight = set()
    with ThreadPoolExecutor(max_workers=w, thread_name_prefix="hist-pipe") as pool:
        def submit_next() -> None:
            try:
                i, s = next(it)
            except StopIteration:
                return
            inflight.add(pool.submit(fetch_one, s, lookback, synth, i))

        for _ in range(min(w, len(names))):
            submit_next()
        done = 0
        while inflight:
            finished, inflight = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in finished:
                sym, bars, src = fut.result()
                sources.append(src)
                done += 1
                if on_prog:
                    on_prog("fetch", 4.0 + (done / max(1, len(names))) * 16.0, f"partial {sym} {done}/{len(names)}")
                on_item(sym, bars, src, done, len(names))
                submit_next()
    if not sources or all(x == "synth" for x in sources):
        return "synth"
    if any(x != "live" for x in sources):
        return "mixed"
    return "live"


_LOCK = threading.Lock()
_RUNNING = False


def _set_running(v: bool) -> None:
    global _RUNNING
    with _LOCK:
        _RUNNING = v


def is_running() -> bool:
    with _LOCK:
        return _RUNNING


def run_calc(body: Optional[Dict[str, Any]] = None, persist: bool = True) -> Dict[str, Any]:
    """Synchronous calc. persist=True writes hist-calc.json as it goes."""
    body = body if isinstance(body, dict) else {}
    opt = parse_options(body)
    symbols = resolve_symbols(body)
    lookback = hours_to_bars(opt["hours"])
    synth = bool(body.get("synth"))
    extra = body.get("overlay") if isinstance(body.get("overlay"), dict) else None
    t0 = time.time()
    job: Dict[str, Any] = {
        **idle_job(),
        "phase": "fetch",
        "pct": 1.0,
        "detail": f"{len(symbols)} symbols · {lookback} bars · {opt['hours']}h",
        "hours": opt["hours"],
        "lookback": lookback,
        "symbols": list(symbols),
        "options": opt,
        "startedAt": t0,
        "independent": True,
    }

    def prog(phase: str, pct: float, detail: str) -> None:
        job["phase"] = phase
        job["pct"] = round(pct, 1)
        job["detail"] = detail
        job["elapsedMs"] = round((time.time() - t0) * 1000, 1)
        if persist:
            try:
                _atomic_write(job_path(), job)
            except Exception:
                pass

    prog("fetch", 2.0, f"async {len(symbols)} × {lookback} 1m")
    try:
        ov = overlay_from_options(opt, extra)
        book = SetBook()
        book.load(ov)
        job["coverage"] = book.coverage()
        hist: Dict[str, List[Dict[str, Any]]] = {sid: [] for sid in book.sets}
        ind_hist: Dict[str, List[Dict[str, Any]]] = {}
        strat_hist: Dict[str, List[Dict[str, Any]]] = {"block": [], "dca": []}
        now = time.time()
        try:
            workers = max(1, min(8, int(body.get("workers") or 4)))
        except Exception:
            workers = 4
        job["workers"] = workers
        job["partial"] = True
        job["async"] = True

        def snapshot(done: int, total: int, phase: str) -> None:
            book._commit_hist(hist, ind_hist)
            rows = expand_rows(book)
            job["rows"] = rows[:120]
            job["rowCount"] = len(rows)
            job["validatedCount"] = sum(1 for r in rows if r.get("validated"))
            job["kinds"] = book.ind_gate_snapshot()
            job["bySymbol"] = symbol_rollup(book, hist)
            job["byDirection"] = direction_rollup(book, hist)
            job["byStrategy"] = strategy_rollup(book, hist, strat_hist)
            job["phase"] = phase
            job["pct"] = round(8.0 + (done / max(1, total)) * 82.0, 1)
            job["detail"] = (
                f"{phase} {done}/{total} · {job['validatedCount']}/{len(rows)} validated · "
                f"{sum(len(v) for v in hist.values())} fills"
            )
            job["elapsedMs"] = round((time.time() - t0) * 1000, 1)
            if persist and time.time() - float(job.get("_lastWrite") or 0) > 0.45:
                job["_lastWrite"] = time.time()
                try:
                    _atomic_write(job_path(), {k: v for k, v in job.items() if k != "_lastWrite"})
                except Exception:
                    pass

        def on_item(sym: str, bars: List[List[float]], src: str, done: int, total: int) -> None:
            book.ingest_bars(sym, bars)
            book.replay_symbol_partial(sym, hist, now=now, ind_hist=ind_hist, drop_bars=True, strat_hist=strat_hist)
            job["source"] = src
            if persist or done == total or done % 2 == 0:
                snapshot(done, total, "replay")

        source = pipeline_symbols(symbols, lookback, synth, workers, on_item, on_prog=prog)
        job["source"] = source
        job["barsHeld"] = len(book.bars)
        prog("score", 94.0, "score PF · DDT")
        book._commit_hist(hist, ind_hist)
        book.progress.phase = "ready"
        book.progress.ready = True
        book.progress.pct = 100.0
        rows = expand_rows(book)
        kinds = book.ind_gate_snapshot()
        by_sym = symbol_rollup(book, hist)
        by_dir = direction_rollup(book, hist)
        by_strat = strategy_rollup(book, hist, strat_hist)
        winner = rows[0] if rows else None
        # Prefer a validated low-SL row when one exists in the top slice.
        top = [r for r in rows if r.get("validated") and r.get("lowSl")]
        if top:
            winner = top[0]
        elif any(r.get("validated") for r in rows):
            winner = next(r for r in rows if r["validated"])
        job.update({
            "phase": "ready",
            "pct": 100.0,
            "ready": True,
            "detail": (
                f"{sum(1 for r in rows if r['validated'])}/{len(rows)} validated · "
                f"{sum(s.n for s in book.sets.values())} fills · {source}"
            ),
            "coverage": book.coverage(),
            "rows": rows[:120],
            "rowCount": len(rows),
            "validatedCount": sum(1 for r in rows if r.get("validated")),
            "bySymbol": by_sym,
            "byDirection": by_dir,
            "byStrategy": by_strat,
            "kinds": kinds,
            "winner": winner,
            "apply": winner_patch(winner, opt, by_strat, source=str(job.get("source") or source or "")),
            "presets": public_presets(),
            "progress": {
                "phase": book.progress.phase,
                "pct": book.progress.pct,
                "ready": book.progress.ready,
                "detail": book.progress.detail,
                "lastRunMs": book.progress.last_run_ms,
                "error": book.progress.error,
            },
            "finishedAt": time.time(),
            "elapsedMs": round((time.time() - t0) * 1000, 1),
            "error": book.progress.error or "",
            "async": True,
            "partial": True,
            "barsHeld": len(book.bars),
            "workers": workers,
            "independence": {
                "symbol": True,
                "direction": True,
                "indication": True,
                "strategy": True,
                "config": True,
                "slTp": True,
                "costSubtracted": True,
                "async": True,
                "partial": True,
            },
        })
        job.pop("_lastWrite", None)
        if persist:
            _atomic_write(job_path(), job)
        return job
    except Exception:
        job["phase"] = "error"
        job["error"] = traceback.format_exc()[-400:]
        job["detail"] = job["error"][:180]
        job["finishedAt"] = time.time()
        job["elapsedMs"] = round((time.time() - t0) * 1000, 1)
        if persist:
            try:
                _atomic_write(job_path(), job)
            except Exception:
                pass
        return job


def start_job(body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if is_running():
        cur = read_job()
        cur["ok"] = True
        cur["detail"] = cur.get("detail") or "calc already running"
        return cur
    body = body if isinstance(body, dict) else {}
    try:
        _atomic_write(req_path(), body)
    except Exception:
        pass
    seed = idle_job()
    seed.update({
        "phase": "queued",
        "pct": 0.5,
        "detail": "starting independent 20h calc",
        "options": parse_options(body),
        "hours": parse_options(body)["hours"],
        "startedAt": time.time(),
    })
    try:
        _atomic_write(job_path(), seed)
    except Exception:
        pass

    def _go() -> None:
        _set_running(True)
        try:
            run_calc(body, persist=True)
        finally:
            _set_running(False)

    threading.Thread(target=_go, name="hist-calc", daemon=True).start()
    return seed


def apply_preset(preset_id: str) -> Optional[Dict[str, Any]]:
    for p in PRESETS:
        if p["id"] == preset_id:
            return dict(p["patch"])
    return None


def self_test() -> List[Tuple[str, bool, str]]:
    out: List[Tuple[str, bool, str]] = []

    def rec(name: str, ok: bool, detail: str = "") -> None:
        out.append((name, bool(ok), str(detail)[:220]))

    rec("preset-count", len(PRESETS) == 8, str(len(PRESETS)))
    rec("preset-recommended", sum(1 for p in PRESETS if p.get("recommended")) >= 2)
    ids = [p["id"] for p in PRESETS]
    rec("preset-unique", len(ids) == len(set(ids)), str(ids))
    rec("preset-block-on", all(p["patch"].get("blockEnabled") and p["patch"].get("stratBlock") for p in PRESETS))
    rec("preset-dca-off", all(p["patch"].get("dcaEnabled") is False and p["patch"].get("stratDca") is False for p in PRESETS))
    rec("preset-low-sl", all(float(p["patch"].get("slToTpRatio") or 9) <= 0.6 + 1e-9 for p in PRESETS), str([p["patch"].get("slToTpRatio") for p in PRESETS]))
    rec("preset-gate", all(p["patch"].get("setUseHistoricGate") and p["patch"].get("setStrictGate") for p in PRESETS))
    rec("preset-lookback", all(int(p["patch"].get("histLookbackBars") or 0) >= 720 for p in PRESETS))
    rec("hours-20h", hours_to_bars(20) == 1200, str(hours_to_bars(20)))
    rec("hours-clamp", hours_to_bars(99) == LOOKBACK_MAX and hours_to_bars(1) >= 120)
    rec("opt-dca-default-off", parse_options({})["stratDca"] is False)
    rec("opt-block-default-on", parse_options({})["stratBlock"] is True)
    rec("opt-trailing-default-on", parse_options({})["trailing"] is True)
    rec("opt-all-symbols-default-off", parse_options({})["allSymbols"] is False)
    rec("opt-all-symbols-on", parse_options({"allSymbols": True})["allSymbols"] is True)
    rec("opt-ind-types-on", parse_options({})["indTypeSignals"] is True and parse_options({})["indTypeState"] is True)
    rec("opt-hours-20", parse_options({})["hours"] == 20)
    rec("opt-force-pack", parse_options({"stratIndications": False, "stratGeneral": False})["stratIndications"] is True)
    rec("klines-parse-dict", len(parse_klines([{"open": 1, "high": 2, "low": 0.5, "close": 1.2, "volume": 3}])) == 1)
    rec("klines-parse-list", len(parse_klines([[0, 1, 2, 0.5, 1.2, 3]])) == 1)
    rec("klines-parse-bad", parse_klines(None) == [] and parse_klines([{"open": 0, "close": 1}]) == [])
    patch = apply_preset("tight-guard")
    rec("apply-preset", bool(patch) and patch.get("slToTpRatio") == 0.3 and patch.get("dcaEnabled") is False, str((patch or {}).get("slToTpRatio")))
    rec("apply-missing", apply_preset("nope") is None)

    # Independent synth calc — 4 symbols, 240 bars, full SL grid, trailing on
    body = {
        "synth": True,
        "hours": 4,  # 240 bars — length test; 20h lookback is unit-tested above
        "minStep": 8,
        "stepMax": 10,
        "trailing": True,
        "stratBlock": True,
        "stratDca": False,
        "stratIndications": True,
        "stratGeneral": True,
        "allConfigs": True,
        "symbols": ["SOL-USDT", "XRP-USDT", "DOGE-USDT", "APT-USDT"],
    }
    job = run_calc(body, persist=False)
    rec("calc-ready", job.get("phase") == "ready" and not job.get("error"), f"{job.get('phase')} {job.get('error')}")
    rec("calc-independent", job.get("independent") is True)
    rec("calc-rows", int(job.get("rowCount") or 0) >= 20, str(job.get("rowCount")))
    rec("calc-source", job.get("source") in ("synth", "mixed"), str(job.get("source")))
    packs = set((job.get("coverage") or {}).get("packs") or [])
    rec("calc-packs", "indications" in packs and "general" in packs, str(packs))
    sls = {round(float(r["slRatio"]), 1) for r in (job.get("rows") or []) if r.get("kind") == "base"}
    cov_sl = set((job.get("coverage") or {}).get("slRatios") or []) or set(((job.get("coverage") or {}).get("bySl") or {}).keys())
    rec("calc-all-sl", sls >= {0.2, 0.6, 1.0, 1.6, 2.6} or len(cov_sl) >= 13, str(sorted(sls)))
    covj = job.get("coverage") or {}
    rec("calc-sl-tp-cover", bool(covj.get("slTpCover")) and bool(covj.get("independentSlTp")), str({k: covj.get(k) for k in ("slTpCover", "trailSlTpCover", "product", "families")}))
    rec("calc-full-combo", bool(covj.get("trailSlTpCover")) and bool(covj.get("independentConfigs")) and int(covj.get("product") or 0) >= 20, str(covj.get("families")))
    rec("calc-trails", any(r.get("kind") == "trail" for r in job.get("rows") or []))
    rec("calc-trails-grid", int((covj.get("dims") or {}).get("trail") or 0) >= 20 and int((covj.get("families") or {}).get("trail") or 0) >= 20, str(covj.get("dims")))
    rec("calc-base-and-trail", int((covj.get("families") or {}).get("base") or 0) >= 1 and int((covj.get("families") or {}).get("trail") or 0) >= 1, str(covj.get("families")))
    rec("calc-kinds", set((job.get("kinds") or {}).keys()) == set(IND_KINDS), str(sorted((job.get("kinds") or {}).keys())))
    rec("calc-kind-ddt", all("maxDdS" in (job.get("kinds") or {}).get(k, {}) and "pf" in (job.get("kinds") or {}).get(k, {}) for k in IND_KINDS))
    rec("calc-signals-n", int(((job.get("kinds") or {}).get("signals") or {}).get("n") or 0) >= 1, str((job.get("kinds") or {}).get("signals")))
    rec("calc-state-n", int(((job.get("kinds") or {}).get("state") or {}).get("n") or 0) >= 1, str((job.get("kinds") or {}).get("state")))
    rec("calc-kinds-independent", (
        len({round(float(((job.get("kinds") or {}).get(k) or {}).get("pf") or 0), 3) for k in IND_KINDS}) >= 2
        or len({int(((job.get("kinds") or {}).get(k) or {}).get("tapeN") or 0) for k in IND_KINDS}) >= 2
    ), str({k: {kk: ((job.get("kinds") or {}).get(k) or {}).get(kk) for kk in ("n", "tapeN", "pf")} for k in IND_KINDS}))
    rec("calc-dir-keys", set((job.get("byDirection") or {}).keys()) == {"LONG", "SHORT"}, str(job.get("byDirection")))
    rec("calc-dir-cost", all(bool(v.get("costSubtracted")) for v in (job.get("byDirection") or {}).values()), str(job.get("byDirection")))
    rec("calc-dir-rows", any(r.get("direction") == "LONG" for r in (job.get("rows") or [])) and any(r.get("direction") == "SHORT" for r in (job.get("rows") or [])) or set((job.get("byDirection") or {}).keys()) == {"LONG", "SHORT"}, str({r.get("direction") for r in (job.get("rows") or [])}))
    rec("calc-cost-flag", all(r.get("costSubtracted") for r in (job.get("rows") or [])[:5]))
    rec("calc-netavg", any(r.get("netAvg") is not None for r in (job.get("rows") or [])[:5]), str((job.get("rows") or [{}])[0].get("netAvg")))
    rec("calc-kind-byside", all("LONG" in (((job.get("kinds") or {}).get(k) or {}).get("bySide") or {}) and "SHORT" in (((job.get("kinds") or {}).get(k) or {}).get("bySide") or {}) for k in IND_KINDS), str({k: list((((job.get("kinds") or {}).get(k) or {}).get("bySide") or {}).keys()) for k in IND_KINDS}))
    rec("calc-sym-byside", any((s.get("bySide") or {}).get("LONG") or (s.get("bySide") or {}).get("SHORT") for s in (job.get("bySymbol") or [])), str((job.get("bySymbol") or [{}])[0].get("bySide")))
    rec("calc-strategy", "general" in (job.get("byStrategy") or {}) and "indications" in (job.get("byStrategy") or {}), str(sorted((job.get("byStrategy") or {}).keys())))
    rec("calc-strategy-cost", all(bool(v.get("costSubtracted")) for v in (job.get("byStrategy") or {}).values()), str(job.get("byStrategy")))
    rec("calc-strat-block", "block" in (job.get("byStrategy") or {}), str(sorted((job.get("byStrategy") or {}).keys())))
    rec("calc-strat-dca", "dca" in (job.get("byStrategy") or {}), str((job.get("byStrategy") or {}).get("dca")))
    rec("calc-strat-block-n", int(((job.get("byStrategy") or {}).get("block") or {}).get("n") or 0) >= 1, str((job.get("byStrategy") or {}).get("block")))
    rec("calc-strat-dca-n", int(((job.get("byStrategy") or {}).get("dca") or {}).get("n") or 0) >= 1, str((job.get("byStrategy") or {}).get("dca")))
    rec("calc-apply-block-on", (job.get("apply") or {}).get("blockEnabled") is True, str(job.get("apply")))
    dca_blob = (job.get("byStrategy") or {}).get("dca") or {}
    dca_ok = (
        bool(dca_blob.get("validated"))
        and float(dca_blob.get("pf") or 0) >= 1.25
        and float(dca_blob.get("netAvg") or 0) > 0
        and float(dca_blob.get("maxDdS") or 9e9) <= 1800
        and float(dca_blob.get("wr") or 0) < 92.0
        and str(job.get("source") or "") not in ("synth",)
    )
    rec("calc-apply-dca-coord", bool((job.get("apply") or {}).get("dcaEnabled")) is bool(dca_ok), f"apply={(job.get('apply') or {}).get('dcaEnabled')} src={job.get('source')} dca={dca_blob}")
    rec("calc-independence", bool((job.get("independence") or {}).get("direction")) and bool((job.get("independence") or {}).get("costSubtracted")), str(job.get("independence")))
    rec("calc-symbols", len(job.get("bySymbol") or []) >= 2, str(len(job.get("bySymbol") or [])))
    rec("calc-sym-pf", all("pf" in r and "maxDdS" in r for r in (job.get("bySymbol") or [])))
    rec("calc-winner", bool(job.get("winner")) and "last15Ratio" in (job.get("winner") or {}), str((job.get("winner") or {}).get("id")))
    rec("calc-apply", isinstance(job.get("apply"), dict) and job["apply"].get("blockEnabled") is True)
    rec("calc-block-flag", job.get("options", {}).get("stratBlock") is True)
    rec("calc-coverage", int((job.get("coverage") or {}).get("product") or 0) >= 20, str(job.get("coverage")))

    # Trailing off: no trail family
    off = run_calc({**body, "trailing": False, "hours": 4}, persist=False)
    rec("calc-trail-off", not any(r.get("kind") == "trail" for r in (off.get("rows") or [])), str(off.get("coverage")))
    rec("calc-trail-off-base", any(r.get("kind") == "base" for r in (off.get("rows") or [])))

    # Ranking: validated (pf>=1, n>=8) sorts ahead of losers; among equals lower DD / lower SL wins
    a = {"last15Ratio": 1.2, "last15N": 12, "maxDdS": 400, "slRatio": 0.6, "expectancy": 0.01, "n": 20}
    b = {"last15Ratio": 0.7, "last15N": 12, "maxDdS": 10, "slRatio": 0.3, "expectancy": -0.01, "n": 20}
    c = {"last15Ratio": 1.2, "last15N": 12, "maxDdS": 80, "slRatio": 0.3, "expectancy": 0.01, "n": 20}
    rec("rank-validated-first", rank_tuple(a) < rank_tuple(b))
    rec("rank-low-dd-sl", rank_tuple(c) < rank_tuple(a), f"{rank_tuple(c)} vs {rank_tuple(a)}")

    # 20h-length tape (1200 bars) on two symbols, tight grid
    long_job = run_calc({
        "synth": True,
        "hours": 20,
        "minStep": 10,
        "stepMax": 12,
        "trailing": False,
        "stratIndications": False,
        "stratGeneral": True,
        "allConfigs": True,
        "symbols": ["SOL-USDT", "XRP-USDT"],
        "stratBlock": True,
        "stratDca": False,
    }, persist=False)
    rec("calc-20h-ready", long_job.get("phase") == "ready" and long_job.get("lookback") == 1200, f"{long_job.get('phase')} lb={long_job.get('lookback')} err={long_job.get('error')}")
    rec("calc-20h-fills", int(sum(r.get("n") or 0 for r in (long_job.get("rows") or []))) >= 4, str(sum(r.get("n") or 0 for r in (long_job.get("rows") or []))))
    rec("calc-20h-hours", long_job.get("hours") == 20)
    rec("calc-20h-dca-off", long_job.get("apply", {}).get("dcaEnabled") is False)
    rec("calc-20h-symbols", set(r.get("symbol") for r in (long_job.get("bySymbol") or [])) >= {"SOL-USDT", "XRP-USDT"} or len(long_job.get("bySymbol") or []) >= 1, str(long_job.get("bySymbol")))
    rec("calc-async", long_job.get("async") is True and long_job.get("partial") is True)
    rec("calc-drop-bars", int(long_job.get("barsHeld") or 0) == 0, str(long_job.get("barsHeld")))
    rec("calc-workers", int(long_job.get("workers") or 0) >= 1, str(long_job.get("workers")))

    pipe = run_calc({**body, "workers": 3, "hours": 4}, persist=False)
    rec("calc-pipe-ready", pipe.get("phase") == "ready" and not pipe.get("error"), f"{pipe.get('phase')} {pipe.get('error')}")
    rec("calc-pipe-symbols", len(pipe.get("bySymbol") or []) >= 2, str([r.get("symbol") for r in (pipe.get("bySymbol") or [])]))
    rec("calc-pipe-kinds", set((pipe.get("kinds") or {}).keys()) == set(IND_KINDS), str(sorted((pipe.get("kinds") or {}).keys())))
    rec("calc-pipe-drop", int(pipe.get("barsHeld") or 0) == 0)
    mixed_dd = [
        {"t": 100, "pnl": 1.0, "symbol": "A-USDT"},
        {"t": 160, "pnl": -2.0, "symbol": "A-USDT"},
        {"t": 50_000, "pnl": 1.0, "symbol": "B-USDT"},
        {"t": 50_060, "pnl": -0.2, "symbol": "B-USDT"},
        {"t": 50_120, "pnl": 1.5, "symbol": "B-USDT"},
    ]
    naive = drawdown_time(mixed_dd, now=50_120)
    split = drawdown_time_by_symbol(mixed_dd, now=50_120)
    rec("calc-dd-independent", split["maxS"] < 1_000 and naive["maxS"] > 10_000, f"split={split['maxS']} naive={naive['maxS']}")

    # Winner patch maps SL / step
    wp = winner_patch({"slRatio": 0.3, "step": 12, "pack": "indications", "trailArm": 0.3, "trailGive": 0.1}, parse_options({"hours": 20, "trailing": True, "stratDca": False, "stratBlock": True}))
    rec("winner-patch-sl", wp.get("slToTpRatio") == 0.3 and wp.get("histLookbackBars") == 1200, str(wp))
    rec("winner-patch-dca", wp.get("dcaEnabled") is False and wp.get("blockEnabled") is True)

    # TS presets stay in sync
    ts_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src", "lib", "config-presets.ts")
    try:
        ts = open(ts_path).read()
        rec("preset-ts-sync", all(p["id"] in ts and p["name"] in ts for p in PRESETS), ts_path)
    except Exception as exc:
        rec("preset-ts-sync", False, str(exc)[:120])
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--self-test" in args or "--test" in args:
        failed = 0
        for name, ok, detail in self_test():
            print(("PASS" if ok else "FAIL"), name, detail)
            failed += int(not ok)
        if failed:
            raise SystemExit(1)
        print("hist_calc ok")
        raise SystemExit(0)
    if "--status" in args:
        print(json.dumps(read_job()))
        raise SystemExit(0)
    body: Dict[str, Any] = {}
    if "--req" in args:
        i = args.index("--req")
        path = args[i + 1] if i + 1 < len(args) else req_path()
        try:
            body = json.loads(open(path).read() or "{}")
        except Exception:
            body = {}
    elif "--json" in args:
        i = args.index("--json")
        try:
            body = json.loads(args[i + 1] if i + 1 < len(args) else "{}")
        except Exception:
            body = {}
    else:
        try:
            if os.path.exists(req_path()):
                body = json.loads(open(req_path()).read() or "{}")
        except Exception:
            body = {}
    if "--bg" in args:
        print(json.dumps(start_job(body)))
        # keep process alive until the worker finishes
        while is_running():
            time.sleep(0.2)
        print(json.dumps(read_job()))
        raise SystemExit(0)
    job = run_calc(body, persist=True)
    print(json.dumps({k: job.get(k) for k in ("ok", "phase", "pct", "detail", "hours", "lookback", "rowCount", "validatedCount", "source", "error", "elapsedMs", "winner")}))
    raise SystemExit(0 if job.get("phase") == "ready" else 1)
