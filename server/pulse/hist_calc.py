#!/usr/bin/env python3
"""Independent historic calc — no Pulse.run() / grok-pulse@ required.

Walks every selected pack × SL:TP × trail × step across all symbols on 1m
bars, scores PositionCost PF + drawdown-time, and ranks for positive PF
with low SL and low DD. Public BingX klines; synth fallback if the venue
is unreachable (sandbox / tests).
"""
from __future__ import annotations

import gc
import json
import multiprocessing as mp
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from typing import Any, Dict, List, Optional, Sequence, Tuple

from position_cost import (
    EVALUATION_WINDOWS,
    POSITIVE_PF,
    SL_TP_RATIOS,
    SL_TP_MIN,
    SL_TP_MAX,
    SL_TP_STEP,
    evaluation_windows,
    is_positive_pf,
    last_n_cost_pf,
    row_net_pnl,
    filter_side,
)
from set_engine import (
    DIRECTIONS,
    HIST_CAP,
    IND_KINDS,
    LOOKBACK_MAX,
    SetBook,
    drawdown_time,
    drawdown_time_by_symbol,
    last_n_chrono,
    merge_hist_rows,
    pin_compute_threads,
    synth_trend,
)
from storage_paths import atomic_write as storage_atomic_write, path_for
from forced_configs import FORCED_SYMBOLS, mandatory_symbols, evaluate_symbol as evaluate_forced_symbol, summary as forced_summary

DEFAULT_SYMBOL_CAP = 50
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
HOURS_DEFAULT = 7
HOURS_MIN = 1
# The bounded fourteen-day/336-hour validation window is the maximum
# supported public window. Keep the exchange request bounded to avoid
# unbounded RAM/CPU.
HOURS_MAX = LOOKBACK_MAX // 60  # never claim more history than the bounded replay holds
BARS_PER_HOUR = 60
HIST_WARMUP_BARS = 30
KLINE_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/klines"
KLINE_URL_V3 = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"
CONTRACTS_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/contracts"
KLINE_PAGE_MAX = 1440
REPLAY_SET_CHUNK = 96  # scalar fallback chunk; vector workers use larger tiles
REPLAY_TILE_SIZE = 1024
REPLAY_QUEUE_MULTIPLIER = 4
_PUBLIC_REQUEST_INTERVAL_S = 1.05
_PUBLIC_REQUEST_LOCK = threading.Lock()
_PUBLIC_REQUEST_LAST = 0.0
_PUBLIC_REQUEST_WAIT_S = 0.0
_PUBLIC_REQUEST_COUNT = 0

# Coordinated low-DD books. Block ON, DCA OFF, SL 0.3 or 0.6, tight DD.
_SHARED = {
    "blockEnabled": True,
    "stratBlock": True,
    "blockMaxStack": 6,
    "blockVolumeRatio": 0.25,
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
    "axisPrevEnabled": False,
    "axisLastEnabled": False,
    "axisContEnabled": False,
    "axisPauseEnabled": False,
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
            "setMinPf": POSITIVE_PF,
            "minPf": POSITIVE_PF,
            "baseMinPf": POSITIVE_PF,
            "mainMinPf": POSITIVE_PF,
            "realMinPf": POSITIVE_PF,
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
            "setMinPf": POSITIVE_PF,
            "minPf": POSITIVE_PF,
            "baseMinPf": POSITIVE_PF,
            "mainMinPf": POSITIVE_PF,
            "realMinPf": POSITIVE_PF,
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
            "setMinPf": POSITIVE_PF,
            "minPf": POSITIVE_PF,
            "baseMinPf": POSITIVE_PF,
            "mainMinPf": POSITIVE_PF,
            "realMinPf": POSITIVE_PF,
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
            "setMinPf": POSITIVE_PF,
            "minPf": POSITIVE_PF,
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
            "setMinPf": POSITIVE_PF,
            "minPf": POSITIVE_PF,
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
            "blockMaxStack": 6,
            "blockVolumeRatio": 0.25,
            "setMinPf": POSITIVE_PF,
            "minPf": POSITIVE_PF,
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
            "baseMinPf": POSITIVE_PF,
            "mainMinPf": POSITIVE_PF,
            "realMinPf": POSITIVE_PF,
            "setMinPf": POSITIVE_PF,
            "minPf": POSITIVE_PF,
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
            "setMinPf": POSITIVE_PF,
            "minPf": POSITIVE_PF,
            "setMaxDdTimeS": 1800,
            "histLookbackBars": 1200,
            "stratGeneral": True,
        },
    },
]
def hours_to_bars(hours: Any, default: int = HOURS_DEFAULT) -> int:
    """Convert a requested evaluation window to bounded one-minute bars."""
    try:
        h = float(hours)
    except Exception:
        h = float(default)
    h = max(float(HOURS_MIN), min(float(HOURS_MAX), h))
    return max(BARS_PER_HOUR, min(LOOKBACK_MAX, int(round(h * BARS_PER_HOUR))))


def _connection_id(connection: Optional[str] = None) -> str:
    raw = str(connection or os.environ.get("PULSE_CONN") or "bingx-x02").replace("connection:", "")
    return "".join(ch for ch in raw if ch.isalnum() or ch in "._-") or "bingx-x02"


def job_path(connection: Optional[str] = None) -> str:
    env = (os.environ.get("CTS_HIST_CALC_PATH") or "").strip()
    if env and connection in (None, ""):
        return env
    return path_for(f"hist-calc-{_connection_id(connection)}.json")


def req_path(connection: Optional[str] = None) -> str:
    env = (os.environ.get("CTS_HIST_CALC_PATH") or "").strip()
    if env and connection in (None, ""):
        return env.replace("hist-calc.json", "hist-calc-req.json")
    return path_for(f"hist-calc-req-{_connection_id(connection)}.json")


def _pid_path(connection: Optional[str] = None) -> str:
    return path_for(f"hist-calc-{_connection_id(connection)}.pid")


def _write_pid(pid: Optional[int] = None) -> None:
    try:
        with open(_pid_path(), "w") as f:
            f.write(str(int(pid or os.getpid())))
    except Exception:
        pass


def _clear_pid(connection: Optional[str] = None) -> None:
    try:
        os.remove(_pid_path(connection))
    except Exception:
        pass


def _pid_alive() -> bool:
    try:
        pid = int(open(_pid_path()).read().strip())
    except Exception:
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        _clear_pid()
        return False


_LOCK = threading.Lock()
_RUNNING = False


def _set_running(v: bool) -> None:
    global _RUNNING
    with _LOCK:
        _RUNNING = bool(v)


def is_running() -> bool:
    with _LOCK:
        if _RUNNING:
            return True
    return _pid_alive()


def _atomic_write(path: str, blob: Dict[str, Any]) -> None:
    storage_atomic_write(path, blob)


def read_job(connection: Optional[str] = None) -> Dict[str, Any]:
    p = job_path(connection)
    if not os.path.exists(p):
        return idle_job(connection)
    try:
        with open(p) as f:
            j = json.load(f)
        if isinstance(j, dict):
            return j
    except Exception:
        pass
    return idle_job(connection)


def idle_job(connection: Optional[str] = None) -> Dict[str, Any]:
    return {
        "ok": True,
        "phase": "idle",
        "pct": 0.0,
        "detail": "no calc yet",
        "connection": _connection_id(connection),
        "runId": "",
        "generation": 0,
        "mode": "idle",
        "selectedSymbols": [],
        "validSymbols": [],
        "invalidSymbols": [],
        "missingSymbols": [],
        "requestedStart": 0,
        "requestedEnd": 0,
        "watermark": {},
        "lastPublishedWatermark": {},
        "lastCompleteRun": 0,
        "nextRunAt": 0,
        "stale": False,
        "deferredReason": "",
        "coordinationComplete": False,
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
        "evaluationWindows": {"windows": list(EVALUATION_WINDOWS)},
        "winner": None,
        "presets": public_presets(),
        "error": "",
        "elapsedMs": 0,
        "startedAt": 0,
        "finishedAt": 0,
        "source": "",
        "shared": True,
        "independent": False,
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
        "minStep": 1,
        "stepMax": 22,
        "trailing": True,
        "stratBlock": True,
        "stratDca": False,
        "stratIndications": True,
        "stratGeneral": True,
        "allConfigs": True,
        "allSymbols": True,
        "indTypeSignals": True,
        "indTypeState": True,
        "indTypeDirection": True,
        "indTypeMove": True,
        "indTypeActive": True,
        "indTypeCommon": True,
        "indTypeTrend": True,
        "indTypeBreak": True,
        # These live-selection coordination layers are opt-in. A historic
        # matrix still evaluates every catalog row regardless of these flags.
        "preferMinimalRange": False,
        "additionalCoordination": False,
        "coordOptimizationN": 50,
    }


def parse_options(body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body = body if isinstance(body, dict) else {}
    opt = default_options()
    raw_hours = body.get("hours")
    if raw_hours is None and body.get("lookback") is not None:
        try:
            raw_hours = float(body["lookback"]) / BARS_PER_HOUR
        except Exception:
            raw_hours = None
    if raw_hours is not None:
        try:
            opt["hours"] = max(HOURS_MIN, min(HOURS_MAX, int(float(raw_hours))))
        except Exception:
            pass
    for k, lo, hi in (("minStep", 1, 22), ("stepMax", 1, 22)):
        if body.get(k) is not None:
            try:
                opt[k] = max(lo, min(hi, int(body[k])))
            except Exception:
                pass
    if opt["stepMax"] < opt["minStep"]:
        opt["stepMax"] = opt["minStep"]
    for k in ("trailing", "stratBlock", "stratDca", "stratIndications", "stratGeneral", "allConfigs", "allSymbols",
              "indTypeSignals", "indTypeState", "indTypeDirection", "indTypeMove", "indTypeActive", "indTypeCommon",
              "indTypeTrend", "indTypeBreak", "preferMinimalRange", "additionalCoordination",
              "preferMinimalPositive", "minimalPositiveCoordination"):
        if k in body:
            opt[k] = bool(body[k])
    # Read old persisted names, but emit and process only the explicit
    # semantic names. The range option never changes the PF objective.
    if "preferMinimalRange" not in body and "preferMinimalPositive" in body:
        opt["preferMinimalRange"] = bool(body["preferMinimalPositive"])
    if "additionalCoordination" not in body and "minimalPositiveCoordination" in body:
        opt["additionalCoordination"] = bool(body["minimalPositiveCoordination"])
    if body.get("coordOptimizationN") is not None:
        try:
            opt["coordOptimizationN"] = max(50, min(200, int(body["coordOptimizationN"])))
        except Exception:
            pass
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


def _public_json(url: str, timeout: float = 12.0) -> Any:
    """Globally pace public BingX calls; the documented quote limit is 1/s/IP."""
    global _PUBLIC_REQUEST_LAST, _PUBLIC_REQUEST_WAIT_S, _PUBLIC_REQUEST_COUNT
    with _PUBLIC_REQUEST_LOCK:
        wait_s = max(0.0, _PUBLIC_REQUEST_INTERVAL_S - (time.monotonic() - _PUBLIC_REQUEST_LAST))
        if wait_s > 0:
            time.sleep(wait_s)
        _PUBLIC_REQUEST_WAIT_S += wait_s
        _PUBLIC_REQUEST_COUNT += 1
        req = urllib.request.Request(url, headers={"User-Agent": "cts-g-hist-calc/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        finally:
            _PUBLIC_REQUEST_LAST = time.monotonic()


def _reset_public_request_stats() -> None:
    global _PUBLIC_REQUEST_WAIT_S, _PUBLIC_REQUEST_COUNT
    with _PUBLIC_REQUEST_LOCK:
        _PUBLIC_REQUEST_WAIT_S = 0.0
        _PUBLIC_REQUEST_COUNT = 0


def _public_request_stats() -> Dict[str, Any]:
    with _PUBLIC_REQUEST_LOCK:
        return {
            "count": int(_PUBLIC_REQUEST_COUNT),
            "waitMs": round(_PUBLIC_REQUEST_WAIT_S * 1000.0, 1),
        }


def _timed_klines(data: Any) -> List[Tuple[int, List[float]]]:
    rows: List[Tuple[int, List[float]]] = []
    if not isinstance(data, list):
        return rows
    for i, raw in enumerate(data):
        parsed = parse_klines([raw])
        if not parsed:
            continue
        ts = 0
        try:
            if isinstance(raw, dict):
                ts = int(raw.get("time") or raw.get("timestamp") or raw.get("t") or 0)
            elif isinstance(raw, (list, tuple)) and len(raw) >= 6:
                ts = int(raw[0])
        except Exception:
            ts = 0
        rows.append((ts or i + 1, parsed[0]))
    return rows


def fetch_klines(symbol: str, limit: int = 1200, timeout: float = 12.0) -> List[List[float]]:
    """Fetch the full requested 1m window using BingX's 1,440-candle pages."""
    limit = max(60, min(LOOKBACK_MAX, int(limit)))
    end_ms = int(time.time() // 60 * 60 * 1000)
    start_ms = end_ms - limit * 60_000
    pages: Dict[int, List[float]] = {}
    cursor = start_ms
    while cursor < end_ms and len(pages) < limit:
        page_end = min(end_ms, cursor + KLINE_PAGE_MAX * 60_000)
        page_limit = min(KLINE_PAGE_MAX, max(1, (page_end - cursor) // 60_000))
        params = {
            "symbol": symbol,
            "interval": "1m",
            "startTime": str(cursor),
            "endTime": str(page_end),
            "limit": str(page_limit),
        }
        got: List[Tuple[int, List[float]]] = []
        for base in (KLINE_URL_V3, KLINE_URL):
            try:
                body = _public_json(f"{base}?{urllib.parse.urlencode(params)}", timeout=timeout)
                got = _timed_klines(body.get("data") if isinstance(body, dict) else body)
                if got:
                    break
            except Exception:
                continue
        if not got:
            break
        for ts, bar in got:
            pages[ts] = bar
        cursor = page_end
    return [bar for _ts, bar in sorted(pages.items())][-limit:]


def fetch_exchange_universe() -> List[str]:
    """Return every currently open USDT perpetual exposed by BingX."""
    try:
        body = _public_json(CONTRACTS_URL, timeout=20.0)
        rows = body.get("data") if isinstance(body, dict) else []
    except Exception:
        rows = []
    now_ms = int(time.time() * 1000)
    names: List[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol.endswith("-USDT"):
            continue
        status = str(row.get("status") or "").lower()
        open_state = str(row.get("apiStateOpen") or "").lower()
        launch_ms = int(row.get("launchTime") or 0)
        if status in ("0", "offline", "close", "closed", "delisted"):
            continue
        if open_state == "false" or (launch_ms and launch_ms > now_ms):
            continue
        names.append(symbol)
    return list(dict.fromkeys(names))


def load_universe() -> List[str]:
    names: List[str] = []
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (
        path_for("universe.json"),
        os.path.join(os.path.dirname(job_path()), "universe.json"),
        os.path.join(here, "universe.json"),
        os.path.join(os.environ.get("PULSE_DIR", ""), "universe.json") if os.environ.get("PULSE_DIR") else "",
        "/opt/grok-x01-pulse/universe.json",
    ):
        if not path:
            continue
        try:
            raw = json.load(open(path))
        except Exception:
            continue
        rows = raw if isinstance(raw, list) else (
            raw.get("selected") or raw.get("ranked") or raw.get("symbols") or raw.get("universe") or raw.get("live") or []
        )
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


def configured_symbol_cap(body: Optional[Dict[str, Any]] = None) -> int:
    """0 = unlimited. Missing overlay cap defaults to 50, not the full book."""
    body = body if isinstance(body, dict) else {}
    ov = body.get("overlay") if isinstance(body.get("overlay"), dict) else {}
    for src in (body, ov):
        if src.get("symbolCap") is not None:
            try:
                return max(0, int(src.get("symbolCap")))
            except (TypeError, ValueError):
                continue
    return DEFAULT_SYMBOL_CAP


def resolve_symbols(body: Optional[Dict[str, Any]] = None) -> List[str]:
    body = body if isinstance(body, dict) else {}
    opt = parse_options(body)
    raw = body.get("symbols")
    if isinstance(raw, str):
        raw = [raw]
    names: List[str] = []
    # An explicit symbol list is authoritative.  The UI defaults
    # ``allSymbols`` to true, but that must not turn a targeted VST/replay
    # request into a remote universe lookup (and a proxy timeout).  Wildcard
    # markers and an explicit allSymbols flag still intentionally use the
    # configured universe.
    requested = isinstance(raw, list) and bool(raw)
    wild = bool(body.get("allSymbols")) or (not requested and bool(opt.get("allSymbols")))
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
        names = uni or fetch_exchange_universe() or list(DEFAULT_SYMBOLS)
    seen = set()
    out: List[str] = []
    for s in names:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    out = mandatory_symbols(out or list(DEFAULT_SYMBOLS))
    cap = configured_symbol_cap(body)
    if cap > 0 and len(out) > cap:
        must = [s for s in FORCED_SYMBOLS if s in out]
        rest = [s for s in out if s not in must]
        out = (must + rest)[: max(cap, len(must))]
    return out


def overlay_from_options(opt: Dict[str, Any], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    lookback = hours_to_bars(opt.get("hours"))
    ov: Dict[str, Any] = {
        "histEnabled": True,
        "histLookbackBars": lookback,
        "histMinBars": min(BARS_PER_HOUR, lookback),
        "histWarmup": HIST_WARMUP_BARS,
        "histExactWindow": True,
        "setUseHistoricGate": True,
        "setStrictGate": True,
        "preferMinimalRange": bool(opt.get("preferMinimalRange", opt.get("preferMinimalPositive", False))),
        "additionalCoordination": bool(opt.get("additionalCoordination", opt.get("minimalPositiveCoordination", False))),
        "coordOptimizationN": int(opt.get("coordOptimizationN") or 50),
        "setAutoDeact": True,
        "setMinSamples": 8,
        "setMinPf": POSITIVE_PF,
        "setMaxDdTimeS": 57600,
        "setLiveNegativeDeact": False,
        "setMinStep": int(opt.get("minStep") or 1),
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
        "blockVolumeRatio": 0.25,
        "blockMaxStack": 6,
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
        "positionCostPct": 0.10,
        "positionCostFallbackPct": 0.10,
        "useLivePositionCosts": True,
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
    ov["setMinStep"] = int(opt.get("minStep") or 1)
    ov["setStepMax"] = max(ov["setMinStep"], int(opt.get("stepMax") or 22))
    ov["stratTrailing"] = bool(opt.get("trailing", True))
    return ov


def rank_tuple(row: Dict[str, Any]) -> Tuple:
    pf = float(row.get("last15Ratio") or 0)
    n = int(row.get("last15N") or 0)
    dd = float(row.get("maxDdS") or 0)
    sl = float(row.get("slRatio") or 9)
    exp = float(row.get("expectancy") or 0)
    validated = n >= 8 and is_positive_pf(pf)
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
                "evaluationWindows": dict(v.get("evaluation_windows") or {}),
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
        "evaluationWindows": dict(g("evaluation_windows", getattr(st, "evaluation_windows", {})) or {}),
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
        "validated": n15 >= 8 and is_positive_pf(pf),
        "lowSl": st.sl_ratio <= 0.6 + 1e-9 or st.kind == "trail",
        "costSubtracted": True,
        "bySide": by_side_pub,
    }


def _bounded_tape(parts: Any, cap: int) -> List[Dict[str, Any]]:
    """Keep the chronological last-N while streaming large independent tapes.

    Pack/core rollups used to concatenate every Set's history (tens of
    millions of rows) before last-N PF. The published windows are last 5–75
    (plus the 80/150 coordination views), so bounding to that cap keeps the
    same PF/EV contract without the full cartesian product in RAM. PF gates
    must see the actual recent tail, not a per-symbol balanced subsample.
    """
    cap = max(8, int(cap or 8))
    keep = max(cap * 2, 32)
    buf: List[Dict[str, Any]] = []
    for rows in parts:
        if not rows:
            continue
        buf.extend(rows)
        if len(buf) > keep * 4:
            buf = last_n_chrono(buf, keep)
    if len(buf) > keep:
        buf = last_n_chrono(buf, keep)
    return buf


def direction_rollup(book: SetBook, hist: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    cap = max(book.pf_n, max(EVALUATION_WINDOWS), int(getattr(book, "optimization_n", 0) or 0) or 0)
    if hist:
        parts = hist.values()
    else:
        parts = (st.hist for st in book.by_idx)
    by_dir: Dict[str, List[Dict[str, Any]]] = {"LONG": [], "SHORT": []}
    dir_n = {"LONG": 0, "SHORT": 0}
    trim_at = max(cap * 2, 32)
    for rows in parts:
        for row in rows:
            d = str(row.get("side") or row.get("direction") or "")
            if not d:
                continue
            key = "LONG" if d[0] in "Ll" else ("SHORT" if d[0] in "Ss" else "")
            if not key:
                continue
            by_dir[key].append(row)
            dir_n[key] += 1
            if len(by_dir[key]) > trim_at:
                by_dir[key] = last_n_chrono(by_dir[key], cap)
    out: Dict[str, Any] = {}
    need = book.eval_need()
    win_n = max(book.pf_n, max(EVALUATION_WINDOWS))
    for d in DIRECTIONS:
        seq = last_n_chrono(by_dir.get(d) or [], cap)
        pf = last_n_cost_pf(seq, book.pf_n, book.cost_pct, ordered=True, simple=True)
        nets = [row_net_pnl(r, book.cost_pct) for r in seq]
        wins = sum(1 for x in nets if x > 0)
        decided = sum(1 for x in nets if x != 0)
        dd = drawdown_time_by_symbol(seq, ordered=True) if seq else {"maxS": 0.0, "avgS": 0.0}
        out[d] = {
            "direction": d,
            "n": int(dir_n.get(d) or 0),
            "pf": round(float(pf["ratio"]), 4),
            "netAvg": round(float(pf.get("netAvg") or 0), 6),
            "last15N": int(pf["count"]),
            "maxDdS": round(float(dd.get("maxS") or 0), 1),
            "wr": round(100.0 * wins / decided, 1) if decided else 0.0,
            "validated": int(pf["count"]) >= 8 and is_positive_pf(pf["ratio"]),
            "costSubtracted": True,
            "evaluationWindows": evaluation_windows(
                seq, book.cost_pct, required_samples=need, ordered=True, simple=True
            ),
        }
    return out


def strategy_rollup(book: SetBook, hist: Optional[Dict[str, List[Dict[str, Any]]]] = None, strat: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """Independent pack / kind / pack:kind books plus Block / DCA volume tapes. Cost subtracted."""
    cap = max(book.pf_n, max(EVALUATION_WINDOWS), int(getattr(book, "optimization_n", 0) or 0) or 0, 80)
    groups: Dict[str, List[Dict[str, Any]]] = {}
    group_n: Dict[str, int] = {}
    trim_at = max(cap * 2, 32)
    need = book.eval_need()
    win_n = max(book.pf_n, max(EVALUATION_WINDOWS))

    def add(key: str, rows: Sequence[Dict[str, Any]]) -> None:
        groups.setdefault(key, [])
        group_n.setdefault(key, 0)
        if not rows:
            return
        buf = groups[key]
        buf.extend(rows)
        group_n[key] += len(rows)
        if len(buf) > trim_at:
            groups[key] = last_n_chrono(buf, cap)

    for st in book.by_idx:
        rows = (hist or {}).get(st.id) or st.hist
        add(st.pack, rows)
        add(st.kind, rows)
        add(f"{st.pack}:{st.kind}", rows)
        add("core", rows)
    for key in ("block", "block:signals", "dca"):
        add(key, (strat or {}).get(key) or [])
    if strat:
        for key, tape in strat.items():
            if key in ("block", "block:signals", "dca"):
                continue
            if tape:
                add(str(key), tape)
    out: Dict[str, Any] = {}
    for key, tape in groups.items():
        win = book.pf_n
        if key in ("block", "block:signals", "dca", "core"):
            win = max(book.pf_n, min(80, int(group_n.get(key) or len(tape) or 1)))
        bounded = last_n_chrono(tape, max(win, cap))
        pf = last_n_cost_pf(bounded, win, book.cost_pct, ordered=True, simple=True)
        nets = [row_net_pnl(r, book.cost_pct) for r in bounded]
        wins = sum(1 for x in nets if x > 0)
        decided = sum(1 for x in nets if x != 0)
        dd = drawdown_time_by_symbol(bounded, ordered=True) if bounded else {"maxS": 0.0, "avgS": 0.0}
        by_dir: Dict[str, Any] = {}
        for d in DIRECTIONS:
            sub = filter_side(bounded, d)
            if not sub:
                continue
            stail = last_n_chrono(sub, win_n, ordered=True)
            spf = last_n_cost_pf(stail, book.pf_n, book.cost_pct, ordered=True, simple=True)
            by_dir[d] = {
                "n": len(sub),
                "pf": round(float(spf["ratio"]), 4),
                "netAvg": round(float(spf.get("netAvg") or 0), 6),
                "validated": int(spf["count"]) >= 8 and is_positive_pf(spf["ratio"]),
                "costSubtracted": True,
                "evaluationWindows": evaluation_windows(
                    stail, book.cost_pct, required_samples=need, ordered=True, simple=True
                ),
            }
        out[key] = {
            "strategy": key,
            "n": int(group_n.get(key) or len(tape)),
            "pf": round(float(pf["ratio"]), 4),
            "netAvg": round(float(pf.get("netAvg") or 0), 6),
            "last15N": int(pf["count"]),
            "maxDdS": round(float(dd.get("maxS") or 0), 1),
            "wr": round(100.0 * wins / decided, 1) if decided else 0.0,
            "validated": int(pf["count"]) >= 8 and is_positive_pf(pf["ratio"]),
            "costSubtracted": True,
            "evaluationWindows": evaluation_windows(
                bounded, book.cost_pct, required_samples=need, ordered=True, simple=True
            ),
            "bySide": by_dir,
        }
    return out


def catalog_listings(
    book: SetBook,
    ranked: Sequence[Tuple[Tuple, Any, str, bool, bool]],
    symbols: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Complete id/key listings. Preview rows stay bounded; identity is full."""
    ranked_ids: List[str] = []
    validated_ids: List[str] = []
    seen: set[str] = set()
    by_pack: Dict[str, List[str]] = {}
    by_kind: Dict[str, List[str]] = {}
    by_dir: Dict[str, List[str]] = {"LONG": [], "SHORT": []}
    for _key, st, side, validated, _low in ranked:
        sid = str(st.id)
        if side:
            if side in by_dir:
                by_dir[side].append(sid)
            continue
        if sid in seen:
            continue
        seen.add(sid)
        ranked_ids.append(sid)
        by_pack.setdefault(str(st.pack), []).append(sid)
        by_kind.setdefault(str(st.kind), []).append(sid)
        if validated:
            validated_ids.append(sid)
    index_by_id = {st.id: int(st.idx) for st in book.by_idx}
    return {
        "rankedIds": ranked_ids,
        "validatedIds": validated_ids,
        "byPack": by_pack or dict(getattr(book, "_ids_by_pack", {}) or {}),
        "byKind": by_kind or dict(getattr(book, "_ids_by_kind", {}) or {}),
        "byDirection": by_dir,
        "indexById": index_by_id,
        "symbols": [str(s) for s in (symbols or [])],
        "relative": {
            "sets": len(book.by_idx),
            "ranked": len(ranked_ids),
            "validated": len(validated_ids),
            "packs": {k: len(v) for k, v in by_pack.items()},
            "kinds": {k: len(v) for k, v in by_kind.items()},
        },
    }


def expand_rows(book: SetBook, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    ranked = _rank_set_rows(book)
    picked = ranked if limit is None else ranked[:limit]
    return [set_row(st, side) for _key, st, side, _validated, _low_sl in picked]


def _rank_set_info(st: Any, side: str = "") -> Tuple[Tuple, bool, bool, int]:
    want = str(side or "").upper()
    blob = (getattr(st, "by_side", None) or {}).get(want) if want in DIRECTIONS else None
    n15 = int((blob.get("last15_n") if blob is not None else st.last15_n) or 0)
    pf = float((blob.get("last15_ratio") if blob is not None else st.last15_ratio) or 0)
    n = int((blob.get("n") if blob is not None else st.n) or 0)
    dd = float((blob.get("max_dd_s") if blob is not None else st.max_dd_s) or 0)
    exp = float((blob.get("expectancy") if blob is not None else st.expectancy) or 0)
    sl = float(st.sl_ratio or 9)
    validated = n15 >= 8 and is_positive_pf(pf)
    low_sl = sl <= 0.6 + 1e-9 or st.kind == "trail"
    return (0 if validated else 1, -pf, dd, sl, -exp, -n), validated, low_sl, n


def _rank_set_rows(book: SetBook) -> List[Tuple[Tuple, Any, str, bool, bool]]:
    ranked: List[Tuple[Tuple, Any, str, bool, bool]] = []
    for st in book.by_idx:
        key, validated, low_sl, _n = _rank_set_info(st, "")
        ranked.append((key, st, "", validated, low_sl))
        for d in DIRECTIONS:
            skey, sval, slow, sn = _rank_set_info(st, d)
            if sn > 0:
                ranked.append((skey, st, d, sval, slow))
    ranked.sort(key=lambda item: item[0])
    return ranked


def pick_winner_row(book: SetBook, ranked: Optional[Sequence[Tuple[Tuple, Any, str, bool, bool]]] = None) -> Optional[Dict[str, Any]]:
    items = list(ranked) if ranked is not None else _rank_set_rows(book)
    if not items:
        return None
    chosen = next((item for item in items if item[3] and item[4]), None)
    if chosen is None:
        chosen = next((item for item in items if item[3]), None)
    if chosen is None:
        chosen = items[0]
    return set_row(chosen[1], chosen[2])


def symbol_rollup(book: SetBook, hist: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> List[Dict[str, Any]]:
    cap = max(book.pf_n, max(EVALUATION_WINDOWS), int(getattr(book, "optimization_n", 0) or 0) or 0, 80)
    trim_at = max(cap * 2, 32)
    need = book.eval_need()
    win_n = max(book.pf_n, max(EVALUATION_WINDOWS))
    by_tape: Dict[str, List[Dict[str, Any]]] = {}
    by_n: Dict[str, int] = {}
    by_wins: Dict[str, int] = {}
    by_decided: Dict[str, int] = {}
    by_dd: Dict[str, List[Dict[str, float]]] = {}
    by_dir_n: Dict[str, Dict[str, int]] = {}
    parts = hist.values() if hist else (st.hist for st in book.by_idx)
    cost = book.cost_pct
    for tape in parts:
        per_set: Dict[str, List[Dict[str, Any]]] = {}
        for r in tape:
            s = str(r.get("symbol") or "")
            if not s:
                continue
            by_n[s] = by_n.get(s, 0) + 1
            net = row_net_pnl(r, cost)
            if net > 0:
                by_wins[s] = by_wins.get(s, 0) + 1
            if net != 0:
                by_decided[s] = by_decided.get(s, 0) + 1
            d = str(r.get("side") or r.get("direction") or "")
            dkey = "LONG" if d[:1] in "Ll" else ("SHORT" if d[:1] in "Ss" else "")
            if dkey:
                dir_n = by_dir_n.setdefault(s, {"LONG": 0, "SHORT": 0})
                dir_n[dkey] += 1
            buf = by_tape.setdefault(s, [])
            buf.append(r)
            if len(buf) > trim_at:
                by_tape[s] = last_n_chrono(buf, cap)
            per_set.setdefault(s, []).append(r)
        for s, rows in per_set.items():
            by_dd.setdefault(s, []).append(drawdown_time(rows, ordered=True))
    out: List[Dict[str, Any]] = []
    for s, tape in by_tape.items():
        tail = last_n_chrono(tape, win_n)
        pf = last_n_cost_pf(tail, book.pf_n, cost, ordered=True, simple=True)
        dd_parts = by_dd.get(s) or []
        if dd_parts:
            dd = {
                "maxS": max(p["maxS"] for p in dd_parts),
                "avgS": sum(p["avgS"] for p in dd_parts) / len(dd_parts),
            }
        else:
            dd = drawdown_time(tail, ordered=True)
        decided = int(by_decided.get(s) or 0)
        wins = int(by_wins.get(s) or 0)
        by_dir: Dict[str, Any] = {}
        for d in DIRECTIONS:
            sub = filter_side(tail, d)
            if not sub:
                continue
            stail = last_n_chrono(sub, win_n, ordered=True)
            spf = last_n_cost_pf(stail, book.pf_n, cost, ordered=True, simple=True)
            by_dir[d] = {
                "n": int((by_dir_n.get(s) or {}).get(d) or len(sub)),
                "pf": round(float(spf["ratio"]), 4),
                "netAvg": round(float(spf.get("netAvg") or 0), 6),
                "validated": int(spf["count"]) >= 8 and is_positive_pf(spf["ratio"]),
                "evaluationWindows": evaluation_windows(
                    stail, cost, required_samples=need, ordered=True, simple=True
                ),
            }
        out.append({
            "symbol": s,
            "n": int(by_n.get(s) or len(tape)),
            "pf": round(float(pf["ratio"]), 4),
            "netAvg": round(float(pf.get("netAvg") or 0), 6),
            "last15N": int(pf["count"]),
            "maxDdS": round(float(dd.get("maxS") or 0), 1),
            "avgDdS": round(float(dd.get("avgS") or 0), 1),
            "wr": round(100.0 * wins / decided, 1) if decided else 0.0,
            "validated": int(pf["count"]) >= 8 and is_positive_pf(pf["ratio"]),
            "costSubtracted": True,
            "evaluationWindows": evaluation_windows(
                tail, cost, required_samples=need, ordered=True, simple=True
            ),
            "bySide": by_dir,
        })
    out.sort(key=lambda r: (0 if r["validated"] else 1, -r["pf"], r["maxDdS"]))
    return out


def winner_patch(row: Optional[Dict[str, Any]], opt: Dict[str, Any], by_strat: Optional[Dict[str, Any]] = None, source: str = "") -> Dict[str, Any]:
    lookback = hours_to_bars(opt.get("hours"))
    patch: Dict[str, Any] = {
        "histLookbackBars": lookback,
        "histMinBars": min(BARS_PER_HOUR, lookback),
        "histExactWindow": True,
        "histEnabled": True,
        "setUseHistoricGate": True,
        "setStrictGate": True,
        "preferMinimalRange": bool(opt.get("preferMinimalRange", opt.get("preferMinimalPositive", False))),
        "additionalCoordination": bool(opt.get("additionalCoordination", opt.get("minimalPositiveCoordination", False))),
        "coordOptimizationN": int(opt.get("coordOptimizationN") or 50),
        "setLiveNegativeDeact": False,
        "stratTrailing": bool(opt.get("trailing", True)),
        "stratBlock": bool(opt.get("stratBlock", True)),
        "blockEnabled": bool(opt.get("stratBlock", True)),
        "dcaEnabled": bool(opt.get("stratDca", False)),
        "stratDca": bool(opt.get("stratDca", False)),
        "blockVolumeRatio": 0.25,
        "blockMaxStack": 6,
        "dcaStepDistancesPct": [1.2, 1.6, 2.0, 2.4],
        "dcaStepVolumeMultipliers": [1.5, 2.0, 2.3, 2.5],
        "dcaMaxSteps": 4,
        "dcaCooldownSeconds": 45,
        "stratIndications": bool(opt.get("stratIndications", True)),
        "stratGeneral": bool(opt.get("stratGeneral", True)),
        "setMinStep": 1,
        "setStepMax": 22,
    }
    if not row:
        return patch
    sl = float(row.get("slRatio") or 0.6)
    if sl > 0:
        patch["slToTpRatio"] = sl
    step = int(row.get("step") or 0)
    if step >= 3:
        patch["setMinStep"] = 1
        patch["setStepMax"] = 22
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
    block_ok = bool(block.get("validated")) and is_positive_pf(block_pf) and float(block.get("netAvg") or 0) >= 0
    # Stable continuous: Block remainder stays on when it doesn't destroy PF.
    # DCA only when its independent tape is validated, +EV, PF≥1.25, DD capped.
    patch["blockEnabled"] = True
    patch["stratBlock"] = True
    patch["dcaEnabled"] = bool(dca_ok)
    patch["stratDca"] = bool(dca_ok)
    if block_ok:
        patch["blockVolumeRatio"] = 0.25
        patch["blockMaxStack"] = 6
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


_HIST_WORKER_BOOK: Optional[SetBook] = None


def replay_pool_workers(
    n_symbols: int,
    n_sets: int = 0,
    requested: Optional[int] = None,
    cpu: Optional[int] = None,
) -> int:
    """Process-pool size follows CPU and symbol count. No extra fat-catalog cap."""
    n_symbols = max(1, int(n_symbols or 1))
    try:
        cpu_n = max(1, int(cpu if cpu is not None else (os.cpu_count() or 1)))
    except Exception:
        cpu_n = 2
    cap = max(1, min(cpu_n, n_symbols))
    if requested is None:
        return cap
    try:
        want = int(requested)
    except Exception:
        return cap
    return max(1, min(n_symbols, want))


def _init_replay_worker(overlay: Dict[str, Any]) -> None:
    """Build one reusable catalog per process instead of once per tile."""
    global _HIST_WORKER_BOOK
    pin_compute_threads(1)
    book = SetBook()
    book.load(dict(overlay or {}))
    _HIST_WORKER_BOOK = book


def _worker_book() -> SetBook:
    book = _HIST_WORKER_BOOK
    if book is None:
        raise RuntimeError("historic replay worker was not initialized")
    return book


def _prepare_symbol_worker(payload: Tuple[str, List[List[float]], float]) -> Tuple[
    str, int, Tuple[Dict[str, List[Tuple[int, float, str]]], Dict[str, List[Tuple[int, float]]], int], Dict[str, Any], float
]:
    """Prepare all indication lanes once for one symbol."""
    sym, bars, now = payload
    book = _worker_book()
    started = time.perf_counter()
    book.bars[sym] = bars
    try:
        prepared = book.prepare_replay_signals(sym, now)
        forced = evaluate_forced_symbol(sym, bars, book.ind_settings, now, cost_pct=book.cost_pct)
        return sym, len(bars), prepared, forced, (time.perf_counter() - started) * 1000.0
    finally:
        book.bars.pop(sym, None)


def _replay_tile_worker(payload: Tuple[Any, ...]) -> Tuple[
    str,
    int,
    Dict[str, List[Dict[str, Any]]],
    Dict[str, List[Dict[str, Any]]],
    Dict[str, List[Dict[str, Any]]],
    Dict[str, int],
    float,
]:
    """Replay one bounded symbol/config tile without scoring the full catalog."""
    if len(payload) >= 7:
        sym, bars, now, set_ids, prepared, capture_ind, capture_strategy = payload[:7]
    else:
        raise ValueError("replay tile payload is incomplete")
    book = _worker_book()
    started = time.perf_counter()
    book.bars[str(sym)] = bars
    try:
        ids = [str(sid) for sid in set_ids]
        local_hist: Dict[str, List[Dict[str, Any]]] = {}
        local_ind: Optional[Dict[str, List[Dict[str, Any]]]] = {} if capture_ind else None
        local_strat: Optional[Dict[str, List[Dict[str, Any]]]] = {} if capture_strategy else None
        local_counts: Dict[str, int] = {}
        nbar = book.replay_symbol_partial(
            str(sym),
            local_hist,
            now=now,
            ind_hist=local_ind,
            drop_bars=True,
            strat_hist=local_strat,
            set_ids=ids,
            prepared=prepared,
            hist_counts=local_counts,
        )
        for sid, rows in local_hist.items():
            if len(rows) > HIST_CAP:
                local_hist[sid] = rows[-HIST_CAP:]
        if local_ind is not None:
            for kind, rows in local_ind.items():
                if len(rows) > HIST_CAP:
                    local_ind[kind] = rows[-HIST_CAP:]
        if local_strat is not None:
            for key, rows in local_strat.items():
                if len(rows) > 2400:
                    local_strat[key] = rows[-2400:]
        return (
            str(sym),
            nbar,
            local_hist,
            local_ind or {},
            local_strat or {},
            local_counts,
            (time.perf_counter() - started) * 1000.0,
        )
    finally:
        book.bars.pop(str(sym), None)


def _replay_symbol_worker(payload: Tuple[str, List[List[float]], float]) -> Tuple[
    str,
    int,
    Dict[str, List[Dict[str, Any]]],
    Dict[str, List[Dict[str, Any]]],
    Dict[str, List[Dict[str, Any]]],
    Dict[str, int],
    Dict[str, Any],
    float,
]:
    """Prepare + replay one symbol in a single process hop (small books)."""
    pin_compute_threads(1)
    sym, bars, now = payload
    book = _worker_book()
    started = time.perf_counter()
    book.bars[str(sym)] = bars
    try:
        local_hist: Dict[str, List[Dict[str, Any]]] = {}
        local_ind: Dict[str, List[Dict[str, Any]]] = {}
        local_strat: Dict[str, List[Dict[str, Any]]] = {}
        local_counts: Dict[str, int] = {}
        nbar = book.replay_symbol_partial(
            str(sym),
            local_hist,
            now=now,
            ind_hist=local_ind,
            drop_bars=True,
            strat_hist=local_strat,
            hist_counts=local_counts,
        )
        for sid, rows in local_hist.items():
            if len(rows) > HIST_CAP:
                local_hist[sid] = rows[-HIST_CAP:]
        for kind, rows in local_ind.items():
            if len(rows) > HIST_CAP:
                local_ind[kind] = rows[-HIST_CAP:]
        for key, rows in local_strat.items():
            if len(rows) > 2400:
                local_strat[key] = rows[-2400:]
        forced = evaluate_forced_symbol(str(sym), bars, book.ind_settings, now, cost_pct=book.cost_pct)
        return (
            str(sym),
            int(nbar),
            local_hist,
            local_ind,
            local_strat,
            local_counts,
            forced if isinstance(forced, dict) else {},
            (time.perf_counter() - started) * 1000.0,
        )
    finally:
        book.bars.pop(str(sym), None)


def coverage_counter(requested: int, completed: int, skipped: int = 0, failed: int = 0) -> Dict[str, Any]:
    requested = max(0, int(requested))
    completed = max(0, min(requested, int(completed)))
    skipped = max(0, int(skipped))
    failed = max(0, int(failed))
    return {
        "requested": requested,
        "started": min(requested, completed + skipped + failed),
        "completed": completed,
        "skipped": skipped,
        "failed": failed,
        "coveragePct": round(100.0 * completed / requested, 2) if requested else 100.0,
    }


def run_forced_calc(body: Dict[str, Any], persist: bool = True) -> Dict[str, Any]:
    """Focused forced sweep; same public data path, no full-catalog allocation."""
    opt = parse_options(body)
    now = time.time()
    results: List[Dict[str, Any]] = []
    sources: Dict[str, str] = {}
    book = SetBook()
    # Only construct one normal Set to obtain the shared indication settings.
    book.load({"stratGeneral": False, "stratIndications": True, "stratTrailing": False,
               "slToTpRatios": [.6], "setMinStep": 1, "setStepMax": 1})
    job: Dict[str, Any] = {"phase": "fetch", "pct": 0, "detail": "Forced baseline sweep",
                           "options": opt, "startedAt": now, "forcedOnly": True,
                           "hours": opt["hours"], "lookback": hours_to_bars(opt["hours"])}
    _set_running(True)
    if persist:
        _write_pid()
    try:
        def item(sym, bars, src, done, total):
            results.append(evaluate_forced_symbol(sym, bars, book.ind_settings, now, cost_pct=book.cost_pct))
            sources[sym] = "historical-market" if src == "live" else src
            job.update(phase="replay", pct=round(done / total * 95, 1),
                       detail=f"{done}/{total} forced symbols", forcedConfigs=forced_summary(results, sources, now))
            if persist:
                _atomic_write(job_path(), job)
        source = pipeline_symbols(list(FORCED_SYMBOLS), hours_to_bars(opt["hours"]), bool(body.get("synth")), 2, item)
        result = forced_summary(results, sources, now)
        job.update(phase="ready", pct=100, ready=True, source=source, forcedConfigs=result,
                   detail=f"{result['completed']}/{result['requested']} baseline configs · {result['selectedCount']} selected",
                   elapsedMs=round((time.time() - now) * 1000, 1), finishedAt=time.time())
        if persist:
            _atomic_write(path_for("forced-configs.json"), {**result, "matrix": results})
            _atomic_write(job_path(), job)
        return job
    except Exception:
        job.update(phase="error", error=traceback.format_exc()[-400:], detail="Forced calculation failed")
        if persist:
            _atomic_write(job_path(), job)
        return job
    finally:
        _set_running(False)
        if persist:
            _clear_pid()


def run_calc(body: Optional[Dict[str, Any]] = None, persist: bool = True) -> Dict[str, Any]:
    """Synchronous calc. persist=True writes hist-calc.json as it goes."""
    body = body if isinstance(body, dict) else {}
    if body.get("forcedOnly"):
        return run_forced_calc(body, persist=persist)
    opt = parse_options(body)
    symbols = resolve_symbols(body)
    lookback = hours_to_bars(opt["hours"])
    warmup_bars = HIST_WARMUP_BARS
    fetch_bars = lookback + warmup_bars
    synth = bool(body.get("synth"))
    extra = body.get("overlay") if isinstance(body.get("overlay"), dict) else None
    t0 = time.time()
    request_end = int(t0)
    evaluation_start = request_end - lookback * 60
    fetch_start = request_end - fetch_bars * 60
    mono0 = time.perf_counter()
    _reset_public_request_stats()
    timings: Dict[str, Any] = {
        "fetchMs": 0.0,
        "fetchWaitMs": 0.0,
        "fetchRequests": 0,
        "signalMs": 0.0,
        "signalWallMs": 0.0,
        "replayMs": 0.0,
        "replayWallMs": 0.0,
        "mergeMs": 0.0,
        "scoreMs": 0.0,
        "reportMs": 0.0,
        "totalMs": 0.0,
    }
    _set_running(True)
    if persist:
        _write_pid()
    job: Dict[str, Any] = {
        **idle_job(),
        "phase": "fetch",
        "pct": 1.0,
        "detail": f"{len(symbols)} symbols · {lookback} evaluation bars + {warmup_bars} warmup · {opt['hours']}h",
        "hours": opt["hours"],
        "lookback": lookback,
        "evaluationBars": lookback,
        "warmupBars": warmup_bars,
        "requestedBars": fetch_bars,
        "requestedStart": evaluation_start,
        "requestedEnd": request_end,
        "evaluationStart": evaluation_start,
        "evaluationEnd": request_end,
        "fetchStart": fetch_start,
        "fetchEnd": request_end,
        "symbols": list(symbols),
        "options": opt,
        "startedAt": t0,
        "timings": timings,
        "independent": True,
        "coverage": {
            **SetBook().coverage(),
            "symbols": coverage_counter(len(symbols), 0),
            "bars": coverage_counter(len(symbols) * fetch_bars, 0),
            "evaluationBars": coverage_counter(len(symbols) * lookback, 0),
            "sets": coverage_counter(0, 0),
            "evaluations": coverage_counter(0, 0),
        },
        "checkpoint": {
            "cycle": 1,
            "symbolCursor": 0,
            "symbol": "",
            "barsProcessed": 0,
            "setsProcessed": 0,
            "evaluationsProcessed": 0,
            "fills": 0,
            "source": "",
        },
    }

    def prog(phase: str, pct: float, detail: str) -> None:
        job["phase"] = phase
        job["pct"] = round(pct, 1)
        job["detail"] = detail
        timings["fetchWaitMs"] = _public_request_stats()["waitMs"]
        timings["fetchRequests"] = _public_request_stats()["count"]
        timings["totalMs"] = round((time.perf_counter() - mono0) * 1000.0, 1)
        job["timings"] = dict(timings)
        job["elapsedMs"] = round((time.time() - t0) * 1000, 1)
        if persist:
            try:
                _atomic_write(job_path(), job)
            except Exception:
                pass

    prog("fetch", 2.0, f"async {len(symbols)} × {lookback} 1m")
    replay_pool: Optional[ProcessPoolExecutor] = None
    try:
        ov = overlay_from_options(opt, extra)
        try:
            warmup_bars = max(16, min(80, int(ov.get("histWarmup") or HIST_WARMUP_BARS)))
        except Exception:
            warmup_bars = HIST_WARMUP_BARS
        fetch_bars = lookback + warmup_bars
        job.update({
            "warmupBars": warmup_bars,
            "requestedBars": fetch_bars,
            "detail": f"{len(symbols)} symbols · {lookback} evaluation bars + {warmup_bars} warmup · {opt['hours']}h",
        })
        book = SetBook()
        book.load(ov)
        job["coverage"] = book.coverage()
        ind_hist: Dict[str, List[Dict[str, Any]]] = {}
        strat_hist: Dict[str, List[Dict[str, Any]]] = {"block": [], "dca": []}
        now = time.time()
        request_end = int(now)
        evaluation_start = request_end - lookback * 60
        fetch_start = request_end - fetch_bars * 60
        job.update({
            "requestedStart": evaluation_start,
            "requestedEnd": request_end,
            "evaluationStart": evaluation_start,
            "evaluationEnd": request_end,
            "fetchStart": fetch_start,
            "fetchEnd": request_end,
        })
        try:
            cpu = max(1, int(os.cpu_count() or 1))
            requested_raw = body.get("workers")
            requested_workers: Optional[int] = None
            if requested_raw not in (None, "", 0, "0"):
                requested_workers = int(requested_raw)
            workers = replay_pool_workers(len(symbols), len(book.by_idx), requested_workers, cpu)
        except Exception:
            workers = 2
        job["workers"] = workers
        job["partial"] = True
        job["async"] = True
        from set_engine import HIST_CAP as _HC
        hist_cap = max(24, min(80, int(_HC or 80)))
        requested_sets = len(book.by_idx) * len(symbols)
        forced_results: List[Dict[str, Any]] = []
        forced_sources: Dict[str, str] = {}

        def _trim_maps() -> None:

            for k, v in list(ind_hist.items()):
                if len(v) > hist_cap:
                    ind_hist[k] = v[-hist_cap:]
            for k, v in list(strat_hist.items()):
                if len(v) > 400:
                    strat_hist[k] = v[-240:]

        def update_coverage(done: int, bars_done: int, fills: int, source: str) -> None:
            job["coverage"] = {
                **book.coverage(),
                "symbols": coverage_counter(len(symbols), done),
                "bars": coverage_counter(len(symbols) * fetch_bars, bars_done),
                "evaluationBars": coverage_counter(len(symbols) * lookback, int(job.get("_evaluationBarsDone") or 0)),
                "sets": coverage_counter(requested_sets, done * len(book.by_idx)),
                "evaluations": coverage_counter(requested_sets, done * len(book.by_idx)),
                "source": source,
            }
            job["checkpoint"] = {
                "cycle": 1,
                "symbolCursor": done,
                "symbol": job.get("checkpoint", {}).get("symbol", ""),
                "barsProcessed": bars_done,
                "setsProcessed": done * len(book.by_idx),
                "evaluationsProcessed": done * len(book.by_idx),
                "fills": fills,
                "source": source,
            }

        def snapshot(done: int, total: int, phase: str, heavy: bool = False) -> None:
            fills = sum(int(st.n or 0) for st in book.sets.values())
            update_coverage(done, int(job.get("_barsDone") or 0), fills, str(job.get("source") or ""))
            job["phase"] = phase
            job["pct"] = round(8.0 + (done / max(1, total)) * 82.0, 1)
            job["elapsedMs"] = round((time.time() - t0) * 1000, 1)
            if heavy:
                # Each symbol is committed atomically in on_item(). Replaying
                # the still-empty aggregate maps here would erase those tapes
                # and make indications appear gate-closed at the end of a run.
                ranked = _rank_set_rows(book)
                rows = [set_row(st, side) for _key, st, side, _v, _l in ranked[:80]]
                job["rows"] = rows
                job["rowCount"] = len(ranked)
                job["validatedCount"] = sum(1 for item in ranked if item[3])
                job["listings"] = catalog_listings(book, ranked, symbols)
                job["index"] = (job["listings"] or {}).get("indexById") or {}
                job["kinds"] = book.ind_gate_snapshot()
                # Direction/strategy rollups walk the full per-Set tape and
                # duplicate rows across groups. They are deliberately built
                # once after the final catalog score below; doing them on each
                # progress snapshot made a healthy replay appear stalled.
                job.setdefault("byDirection", {})
                job.setdefault("byStrategy", {})
            job["detail"] = (
                f"{phase} {done}/{total} · {int(job.get('validatedCount') or 0)}/"
                f"{int(job.get('rowCount') or len(book.by_idx))} validated · {fills} fills"
            )
            if persist and time.time() - float(job.get("_lastWrite") or 0) > 0.8:
                job["_lastWrite"] = time.time()
                try:
                    slim = {k: v for k, v in job.items() if k != "_lastWrite"}
                    slim.pop("bySymbol", None)
                    _atomic_write(job_path(), slim)
                except Exception:
                    pass

        replay_pending: Dict[Any, Dict[str, Any]] = {}
        symbol_states: Dict[str, Dict[str, Any]] = {}
        replayed_set_ids = tuple(str(st.id) for st in book.by_idx)
        replay_done = 0
        replay_tasks_submitted = 0
        replay_tasks_completed = 0
        replay_tiles_total = 0
        replay_tiles_completed = 0
        tile_cursor = 0
        next_symbol_index = 0
        completed_states: Dict[str, Dict[str, Any]] = {}
        prep_completed = 0
        prep_started_at: Optional[float] = None
        tile_started_at: Optional[float] = None
        queue_limit = max(workers, workers * REPLAY_QUEUE_MULTIPLIER)
        all_tile_specs: List[Tuple[str, int, List[str], bool, bool]] = []
        for pack in book.packs:
            pack_ids = book.ids_for_pack(pack)
            if not pack_ids:
                continue
            seed = next((sid for sid in pack_ids if book.sets[sid].kind == "base"), pack_ids[0])
            ordered_ids = [seed] + [sid for sid in pack_ids if sid != seed]
            for tile_i, start in enumerate(range(0, len(ordered_ids), REPLAY_TILE_SIZE)):
                ids = ordered_ids[start:start + REPLAY_TILE_SIZE]
                all_tile_specs.append((
                    pack,
                    tile_i,
                    ids,
                    pack == "indications" and tile_i == 0,
                    tile_i == 0,
                ))
        use_oneshot = len(symbols) <= 32 or len(all_tile_specs) <= 1
        if use_oneshot:
            replay_tiles_total = 0
            queue_limit = max(1, min(len(symbols), max(workers, workers * 2)))
        else:
            replay_tiles_total = len(symbols) * len(all_tile_specs)
        job["replayMode"] = "oneshot" if use_oneshot else "tiled"
        job["replayTiles"] = {
            "requested": replay_tiles_total,
            "submitted": 0,
            "completed": 0,
            "tileSize": REPLAY_TILE_SIZE,
            "queueLimit": queue_limit,
        }

        def update_task_status() -> None:
            job["replayTasks"] = {
                "requested": len(symbols) + replay_tiles_total,
                "submitted": replay_tasks_submitted,
                "completed": replay_tasks_completed,
                "inFlight": len(replay_pending),
                "workers": workers,
            }
            job["replayTiles"] = {
                "requested": replay_tiles_total,
                "submitted": max(0, replay_tasks_submitted - len(symbols)),
                "completed": replay_tiles_completed,
                "tileSize": REPLAY_TILE_SIZE,
                "queueLimit": queue_limit,
            }

        def finish_symbol(state: Dict[str, Any], total: int) -> None:
            nonlocal replay_done
            sym = str(state["symbol"])
            merge_started = time.perf_counter()
            book._commit_hist(
                state["hist"],
                state["ind"] or None,
                merge=True,
                replayed_symbols=[sym],
                hist_counts=state["counts"],
                score=False,
                replayed_set_ids=state["replayedIds"],
            )
            timings["mergeMs"] += (time.perf_counter() - merge_started) * 1000.0
            for kind, rows in state["ind"].items():
                if rows:
                    ind_hist[kind] = (ind_hist.get(kind) or []) + rows
                    if len(ind_hist[kind]) > hist_cap:
                        ind_hist[kind] = ind_hist[kind][-hist_cap:]
            for key, rows in state["strat"].items():
                if rows:
                    strat_hist.setdefault(key, []).extend(rows)
            forced = state.get("forced") or {}
            if forced.get("completed"):
                forced_results.append(forced)
                forced_sources[sym] = "historical-market" if state["src"] == "live" else state["src"]
            job["_barsDone"] = int(job.get("_barsDone") or 0) + int(state["nbar"])
            job["_evaluationBarsDone"] = int(job.get("_evaluationBarsDone") or 0) + min(
                lookback, max(0, int(state["nbar"]) - warmup_bars)
            )
            replay_done += 1
            job["checkpoint"]["symbol"] = sym
            job["source"] = state["src"]
            _trim_maps()
            heavy = persist and (replay_done == total or replay_done % 8 == 0)
            if persist or total <= 8:
                if heavy or replay_done == total or total <= 8 or replay_done % 4 == 0:
                    snapshot(replay_done, total, "replay", heavy=heavy)
            state["bars"] = None
            state["prepared"] = None
            symbol_states.pop(sym, None)

        def flush_completed_symbols(total: int) -> None:
            nonlocal next_symbol_index
            while next_symbol_index < len(symbols):
                sym = symbols[next_symbol_index]
                state = completed_states.pop(sym, None)
                if state is None:
                    return
                finish_symbol(state, total)
                next_symbol_index += 1

        def merge_tile(result: Tuple[Any, ...], meta: Dict[str, Any], total: int) -> None:
            nonlocal replay_tiles_completed, replay_tasks_completed, tile_started_at
            sym, _nbar, local_hist, local_ind, local_strat, local_counts, tile_ms = result[:7]
            state = symbol_states.get(str(sym))
            if state is None:
                raise RuntimeError(f"replay tile completed for unknown symbol {sym}")
            timings["replayMs"] += float(tile_ms or 0.0)
            for sid, rows in local_hist.items():
                if not rows:
                    continue
                previous = state["hist"].get(sid) or []
                state["hist"][sid] = merge_hist_rows(previous, rows, [str(sym)]) if previous else rows[-hist_cap:]
            for kind, rows in local_ind.items():
                if rows:
                    target = state["ind"].get(kind) or []
                    state["ind"][kind] = (target + rows)[-hist_cap:]
            for key, rows in local_strat.items():
                if rows:
                    target = state["strat"].get(key) or []
                    state["strat"][key] = (target + rows)[-2400:]
            for sid, count in local_counts.items():
                state["counts"][sid] = int(state["counts"].get(sid, 0)) + int(count)
            state["tilesDone"] += 1
            replay_tiles_completed += 1
            replay_tasks_completed += 1
            if replay_tiles_completed == replay_tiles_total and tile_started_at is not None:
                timings["replayWallMs"] = (time.perf_counter() - tile_started_at) * 1000.0
            if state["tilesDone"] >= state["tilesTotal"]:
                finish_symbol(state, total)

        def handle_symbol(result: Tuple[Any, ...], meta: Dict[str, Any], total: int) -> None:
            nonlocal replay_tasks_completed, tile_started_at
            sym, nbar, local_hist, local_ind, local_strat, local_counts, forced, ms = result[:8]
            state = symbol_states.get(str(sym))
            if state is None:
                raise RuntimeError(f"oneshot replay completed for unknown symbol {sym}")
            timings["replayMs"] += float(ms or 0.0)
            state["nbar"] = int(nbar)
            state["hist"] = local_hist or {}
            state["ind"] = local_ind or {}
            state["strat"] = local_strat or {}
            state["counts"] = local_counts or {}
            state["forced"] = forced if isinstance(forced, dict) else {}
            replay_tasks_completed += 1
            if tile_started_at is not None and replay_done + 1 >= total:
                timings["replayWallMs"] = (time.perf_counter() - tile_started_at) * 1000.0
            finish_symbol(state, total)

        def fill_tile_queue() -> None:
            nonlocal tile_cursor, replay_tasks_submitted, tile_started_at
            if replay_pool is None:
                return
            while len(replay_pending) < queue_limit:
                ready = [
                    state for state in symbol_states.values()
                    if state.get("prepared") is not None
                    and int(state.get("nextTile") or 0) < len(state.get("tiles") or [])
                ]
                if not ready:
                    return
                state = ready[tile_cursor % len(ready)]
                tile_cursor += 1
                tile_i = int(state["nextTile"])
                pack, _pack_tile_i, ids, capture_ind, capture_strategy = state["tiles"][tile_i]
                state["nextTile"] = tile_i + 1
                if tile_started_at is None:
                    tile_started_at = time.perf_counter()
                future = replay_pool.submit(
                    _replay_tile_worker,
                    (
                        state["symbol"],
                        state["bars"],
                        now,
                        ids,
                        state["prepared"],
                        capture_ind,
                        capture_strategy,
                    ),
                )
                replay_pending[future] = {
                    "kind": "tile",
                    "symbol": state["symbol"],
                    "pack": pack,
                    "tile": tile_i,
                }
                replay_tasks_submitted += 1

        def handle_prepare(result: Tuple[Any, ...], meta: Dict[str, Any], total: int) -> None:
            nonlocal prep_completed
            sym, nbar, prepared, forced, signal_ms = result
            state = symbol_states.get(str(sym))
            if state is None:
                raise RuntimeError(f"replay preparation completed for unknown symbol {sym}")
            state["nbar"] = int(nbar)
            state["prepared"] = prepared
            state["forced"] = forced
            state["prepMs"] = float(signal_ms or 0.0)
            timings["signalMs"] += float(signal_ms or 0.0)
            prep_completed += 1
            if prep_completed == len(symbols) and prep_started_at is not None:
                timings["signalWallMs"] = (time.perf_counter() - prep_started_at) * 1000.0
            fill_tile_queue()

        def drain_replay(total: int) -> None:
            """Drain any completed preparation/tile without queue-order blocking."""
            nonlocal replay_tasks_completed
            if not replay_pending:
                return
            finished, _ = wait(tuple(replay_pending), return_when=FIRST_COMPLETED)
            for future in finished:
                meta = replay_pending.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    job["replayFailure"] = {
                        "symbol": meta.get("symbol", ""),
                        "kind": meta.get("kind", ""),
                        "tile": meta.get("tile"),
                        "error": str(exc)[:240],
                    }
                    raise
                if meta.get("kind") == "prepare":
                    replay_tasks_completed += 1
                    handle_prepare(result, meta, total)
                elif meta.get("kind") == "symbol":
                    handle_symbol(result, meta, total)
                else:
                    merge_tile(result, meta, total)
                if not use_oneshot:
                    fill_tile_queue()
                update_task_status()

        def on_item(sym: str, bars: List[List[float]], src: str, done: int, total: int) -> None:
            nonlocal replay_tasks_submitted, prep_started_at, tile_started_at
            state = {
                "symbol": sym,
                "src": src,
                "bars": bars,
                "prepared": None,
                "forced": None,
                "nbar": len(bars),
                "hist": {},
                "ind": {},
                "strat": {},
                "counts": {},
                "replayedIds": replayed_set_ids,
                "tiles": list(all_tile_specs),
                "nextTile": 0,
                "tilesDone": 0,
                "tilesTotal": len(all_tile_specs),
            }
            symbol_states[sym] = state
            if prep_started_at is None:
                prep_started_at = time.perf_counter()
            if replay_pool is None:
                raise RuntimeError("historic replay pool was not created")
            if use_oneshot:
                if tile_started_at is None:
                    tile_started_at = time.perf_counter()
                future = replay_pool.submit(_replay_symbol_worker, (sym, bars, now))
                replay_pending[future] = {"kind": "symbol", "symbol": sym, "tile": None}
            else:
                future = replay_pool.submit(_prepare_symbol_worker, (sym, bars, now))
                replay_pending[future] = {"kind": "prepare", "symbol": sym, "tile": None}
            replay_tasks_submitted += 1
            update_task_status()
            while len(replay_pending) >= queue_limit:
                drain_replay(total)

        try:
            ctx = mp.get_context("fork")
        except Exception:
            ctx = None
        pool_kwargs: Dict[str, Any] = {
            "max_workers": workers,
            "initializer": _init_replay_worker,
            "initargs": (ov,),
        }
        if ctx is not None:
            pool_kwargs["mp_context"] = ctx
        replay_pool = ProcessPoolExecutor(**pool_kwargs)
        source_started = time.perf_counter()
        fetch_workers = max(1, min(len(symbols), max(workers, 4)))
        source = pipeline_symbols(symbols, fetch_bars, synth, fetch_workers, on_item, on_prog=prog)
        timings["fetchMs"] = (time.perf_counter() - source_started) * 1000.0
        while replay_pending:
            drain_replay(len(symbols))
        flush_completed_symbols(len(symbols))
        update_task_status()
        if replay_pool is not None:
            replay_pool.shutdown(wait=True, cancel_futures=True)
            replay_pool = None
        if replay_done != len(symbols) or (not use_oneshot and replay_tiles_completed != replay_tiles_total):
            raise RuntimeError(
                f"historic replay incomplete: symbols {replay_done}/{len(symbols)}, "
                f"tiles {replay_tiles_completed}/{replay_tiles_total} mode={job.get('replayMode')}"
            )
        job["source"] = source
        job["barsHeld"] = len(book.bars)
        prog("score", 94.0, "score PF · DDT")
        # Each completed symbol was committed atomically after all of its
        # tiles. Do not replay an empty aggregate here: it would erase tapes.
        book.progress.phase = "ready"
        book.progress.ready = True
        book.progress.pct = 100.0
        report_started = time.perf_counter()
        score_started = time.perf_counter()
        try:
            report_cpu = max(1, int(os.cpu_count() or 1))
        except Exception:
            report_cpu = 2
        if report_cpu <= 2:
            book._score_all()
            timings["scoreMs"] = (time.perf_counter() - score_started) * 1000.0
            kinds = book.ind_gate_snapshot()
            ranked = _rank_set_rows(book)
            by_sym = symbol_rollup(book)
            by_dir = direction_rollup(book)
            by_strat = strategy_rollup(book, strat=strat_hist)
        else:
            with ThreadPoolExecutor(max_workers=min(4, report_cpu), thread_name_prefix="hist-report") as report_pool:
                score_fut = report_pool.submit(book._score_all)
                sym_fut = report_pool.submit(symbol_rollup, book)
                dir_fut = report_pool.submit(direction_rollup, book)
                strat_fut = report_pool.submit(strategy_rollup, book, None, strat_hist)
                score_fut.result()
                timings["scoreMs"] = (time.perf_counter() - score_started) * 1000.0
                kinds = book.ind_gate_snapshot()
                ranked = _rank_set_rows(book)
                by_sym = sym_fut.result()
                by_dir = dir_fut.result()
                by_strat = strat_fut.result()
        rows = [set_row(st, side) for _key, st, side, _v, _l in ranked[:120]]
        listings = catalog_listings(book, ranked, symbols)
        evaluation_summary = {
            "windows": list(EVALUATION_WINDOWS),
            "directions": {k: v.get("evaluationWindows") or {} for k, v in by_dir.items()},
            "strategies": {k: v.get("evaluationWindows") or {} for k, v in by_strat.items()},
            "indications": {k: v.get("evaluationWindows") or {} for k, v in kinds.items() if isinstance(v, dict)},
            "symbols": {str(v.get("symbol")): v.get("evaluationWindows") or {} for v in by_sym if isinstance(v, dict)},
        }
        winner = pick_winner_row(book, ranked)
        final_coverage = {
            **book.coverage(),
            "symbols": coverage_counter(len(symbols), replay_done),
            "bars": coverage_counter(len(symbols) * fetch_bars, int(job.get("_barsDone") or 0)),
            "evaluationBars": coverage_counter(len(symbols) * lookback, int(job.get("_evaluationBarsDone") or 0)),
            "sets": coverage_counter(requested_sets, replay_done * len(book.by_idx)),
            "evaluations": coverage_counter(requested_sets, replay_done * len(book.by_idx)),
            "tasks": coverage_counter(len(symbols) + replay_tiles_total, replay_tasks_completed),
            "source": source,
        }
        timings["reportMs"] = (time.perf_counter() - report_started) * 1000.0
        timings["fetchWaitMs"] = _public_request_stats()["waitMs"]
        timings["fetchRequests"] = _public_request_stats()["count"]
        timings["totalMs"] = (time.perf_counter() - mono0) * 1000.0
        job.update({
            "phase": "ready",
            "pct": 100.0,
            "ready": True,
            "detail": (
                f"{sum(1 for item in ranked if item[3])}/{len(ranked)} validated · "
                f"{sum(s.n for s in book.sets.values())} fills · {source}"
            ),
            "coverage": final_coverage,
            "rows": rows[:120],
            "rowCount": len(ranked),
            "validatedCount": sum(1 for item in ranked if item[3]),
            "bySymbol": by_sym,
            "byDirection": by_dir,
            "byStrategy": by_strat,
            "listings": listings,
            "index": listings.get("indexById") or {},
            "kinds": kinds,
            "evaluationWindows": evaluation_summary,
            "forcedConfigs": forced_summary(forced_results, forced_sources, now),
            "winner": winner,
            "apply": winner_patch(winner, opt, by_strat, source=str(job.get("source") or source or "")),
            "presets": public_presets(),
            "timings": dict(timings),
            "replayTasks": job.get("replayTasks") or {},
            "replayTiles": job.get("replayTiles") or {},
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
            _atomic_write(path_for("forced-configs.json"), {**job["forcedConfigs"], "matrix": forced_results})
            _atomic_write(job_path(), job)
        return job
    except Exception:
        job["phase"] = "error"
        job["error"] = traceback.format_exc()[-400:]
        job["detail"] = job["error"][:180]
        timings["fetchWaitMs"] = _public_request_stats()["waitMs"]
        timings["fetchRequests"] = _public_request_stats()["count"]
        timings["totalMs"] = (time.perf_counter() - mono0) * 1000.0
        job["timings"] = dict(timings)
        job["finishedAt"] = time.time()
        job["elapsedMs"] = round((time.time() - t0) * 1000, 1)
        if persist:
            try:
                _atomic_write(job_path(), job)
            except Exception:
                pass
        return job
    finally:
        if replay_pool is not None:
            replay_pool.shutdown(wait=False, cancel_futures=True)
        _set_running(False)
        if persist:
            _clear_pid()


def write_job(job: Dict[str, Any], connection: Optional[str] = None) -> Dict[str, Any]:
    """Persist the status consumed by both the HTTP lane and the engine lane."""
    payload = dict(job or {})
    payload.setdefault("connection", _connection_id(connection))
    payload.setdefault("ok", True)
    try:
        _atomic_write(job_path(connection), payload)
    except Exception:
        pass
    return payload


def read_request(connection: Optional[str] = None) -> Dict[str, Any]:
    path = req_path(connection)
    try:
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def start_job(body: Optional[Dict[str, Any]] = None, connection: Optional[str] = None) -> Dict[str, Any]:
    """Queue one generation for the running Pulse lane.

    The request file is intentionally the hand-off boundary.  The engine owns
    fetching, replay, coordination, and publication; repeated UI clicks only
    replace this newest request and never create a divergent subprocess.
    """
    cid = _connection_id(connection)
    body = dict(body) if isinstance(body, dict) else {}
    current = read_job(cid)
    previous_request = read_request(cid)
    generation = max(
        int(current.get("generation") or 0),
        int(previous_request.get("generation") or 0),
    ) + 1
    requested_at = time.time()
    run_id = f"{cid}:{generation}:{int(requested_at * 1000)}"
    options = parse_options(body)
    request = {
        **body,
        "connection": cid,
        "runId": run_id,
        "generation": generation,
        "mode": str(body.get("mode") or "manual"),
        "requestedAt": requested_at,
        "options": options,
    }
    try:
        _atomic_write(req_path(cid), request)
    except Exception:
        pass
    # Keep the last published rows, coverage, winner, and watermark visible
    # while the newest request waits for the lane. The request is a coalescing
    # hand-off, not a reason to erase the only good snapshot.
    seed = idle_job(cid)
    seed.update(current if isinstance(current, dict) else {})
    selected = body.get("selectedSymbols") or body.get("symbols") or []
    seed.update({
        "ok": True,
        "phase": "queued",
        "pct": 0.5,
        "detail": "queued on shared historic lane",
        "options": options,
        "hours": options["hours"],
        "lookback": hours_to_bars(options["hours"]),
        "startedAt": requested_at,
        "runId": run_id,
        "generation": generation,
        "mode": request["mode"],
        "symbols": list(selected) if isinstance(selected, list) else [],
        "selectedSymbols": list(selected) if isinstance(selected, list) else [],
        "requestOptions": options,
        "requestOverlay": dict(body.get("overlay")) if isinstance(body.get("overlay"), dict) else {},
        "stale": bool(current.get("ready") or current.get("stale")),
        "deferredReason": "awaiting running connection worker",
        "shared": True,
        "independent": False,
    })
    return write_job(seed, cid)


def stop_job(connection: Optional[str] = None) -> Dict[str, Any]:
    """Drop a queued historic request and kill a leftover calc pid.

    Exchange positions are not touched. A later Start must not resume a
    half-dead worker from this connection.
    """
    cid = _connection_id(connection)
    try:
        os.remove(req_path(cid))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    pid = 0
    try:
        pid = int(open(_pid_path(cid)).read().strip())
    except Exception:
        pid = 0
    me = os.getpid()
    killed = 0
    if pid > 1 and pid != me:
        for sig in (15, 9):
            try:
                os.kill(pid, sig)
                killed = pid
            except OSError:
                break
            if sig == 15:
                time.sleep(0.15)
    _clear_pid(cid)
    _set_running(False)
    job = read_job(cid)
    phase = str((job or {}).get("phase") or "")
    if job and phase not in ("", "ready", "error", "stopped"):
        job.update(phase="stopped", running=False, detail="stopped with connection")
        write_job(job, cid)
    return {"ok": True, "connection": cid, "killed": killed}


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
    src = open(__file__, encoding="utf-8").read()
    prod_src = src.split("def self_test", 1)[0]
    rec("calc-pf-chrono-not-balanced", "last_n_balanced(" not in prod_src)
    from set_engine import last_n_balanced as _bal
    mixed = (
        [{"t": 1000 + i, "symbol": "A-USDT", "pnl_pct": 0.004, "side": "LONG"} for i in range(20)]
        + [{"t": 2000 + i, "symbol": "B-USDT", "pnl_pct": -0.003, "side": "LONG"} for i in range(15)]
    )
    chrono15 = last_n_chrono(mixed, 15)
    bal15 = _bal(mixed, 15)
    rec("chrono-last15-is-recent-hot", {r["symbol"] for r in chrono15} == {"B-USDT"}, str({r["symbol"] for r in chrono15}))
    rec(
        "balanced-last15-mixes-symbols",
        {"A-USDT", "B-USDT"} <= {r["symbol"] for r in bal15},
        str({r["symbol"] for r in bal15}),
    )
    c_pf = float(last_n_cost_pf(chrono15, 15, ordered=True)["ratio"])
    b_pf = float(last_n_cost_pf(bal15, 15, ordered=True)["ratio"])
    rec("chrono-pf-differs-from-balanced", abs(c_pf - b_pf) > 1e-4, f"chrono={c_pf} bal={b_pf}")
    rec("chrono-last15-not-positive", not is_positive_pf(c_pf), f"chrono={c_pf}")
    rec("preset-recommended", sum(1 for p in PRESETS if p.get("recommended")) >= 2)
    ids = [p["id"] for p in PRESETS]
    rec("preset-unique", len(ids) == len(set(ids)), str(ids))
    rec("preset-block-on", all(p["patch"].get("blockEnabled") and p["patch"].get("stratBlock") for p in PRESETS))
    rec("preset-dca-off", all(p["patch"].get("dcaEnabled") is False and p["patch"].get("stratDca") is False for p in PRESETS))
    rec("preset-low-sl", all(float(p["patch"].get("slToTpRatio") or 9) <= 0.6 + 1e-9 for p in PRESETS), str([p["patch"].get("slToTpRatio") for p in PRESETS]))
    rec("preset-gate", all(p["patch"].get("setUseHistoricGate") and p["patch"].get("setStrictGate") for p in PRESETS))
    rec("preset-lookback", all(int(p["patch"].get("histLookbackBars") or 0) >= 720 for p in PRESETS))
    step_grid = [int(p["patch"].get("setMinStep") or 0) for p in PRESETS]
    step_max = [int(p["patch"].get("setStepMax") or 0) for p in PRESETS]
    rec("preset-step-grid-preserved", step_grid == [12, 10, 8, 10, 10, 8, 12, 8], str(step_grid))
    rec("preset-step-bounds", all(3 <= lo <= hi <= 22 for lo, hi in zip(step_grid, step_max)), str(list(zip(step_grid, step_max))))
    rec("hours-20h", hours_to_bars(20) == 1200, str(hours_to_bars(20)))
    rec("hours-72h", hours_to_bars(72) == 4320 and parse_options({"hours": 72})["hours"] == 72, str(hours_to_bars(72)))
    rec("hours-336h", hours_to_bars(336) == LOOKBACK_MAX and parse_options({"hours": 336})["hours"] == 336, str(hours_to_bars(336)))
    rec("hours-one-hour", hours_to_bars(1) == 60 and parse_options({"hours": 1})["hours"] == 1)
    rec("hours-clamp", hours_to_bars(9999) == LOOKBACK_MAX and hours_to_bars(1) == 60)
    range_series = {hours: hours_to_bars(hours) for hours in (1, 2, 4, 20, 24, 48, 72, 120, 336)}
    rec(
        "hours-range-series",
        range_series == {1: 60, 2: 120, 4: 240, 20: 1200, 24: 1440, 48: 2880, 72: 4320, 120: 7200, 336: 20160},
        str(range_series),
    )
    bounded = parse_options({"hours": 999, "minStep": -3, "stepMax": 999})
    rec("options-range-step-clamp", bounded["hours"] == HOURS_MAX and bounded["minStep"] == 1 and bounded["stepMax"] == 22, str(bounded))
    rec("opt-dca-default-off", parse_options({})["stratDca"] is False)
    rec("opt-block-default-on", parse_options({})["stratBlock"] is True)
    rec("opt-trailing-default-on", parse_options({})["trailing"] is True)
    rec("opt-all-symbols-default-on", parse_options({})["allSymbols"] is True)
    rec("opt-all-symbols-on", parse_options({"allSymbols": True})["allSymbols"] is True)
    rec("symbol-cap-default-50", configured_symbol_cap({}) == 50)
    rec("symbol-cap-explicit-unlimited", configured_symbol_cap({"symbolCap": 0}) == 0)
    rec("symbol-cap-from-overlay", configured_symbol_cap({"overlay": {"symbolCap": 12}}) == 12)
    capped = resolve_symbols({"symbols": [f"S{i}-USDT" for i in range(40)], "allSymbols": False, "symbolCap": 25})
    rec("resolve-respects-cap", 1 <= len(capped) <= max(25, len(FORCED_SYMBOLS)), str(len(capped)))
    rec("opt-steps-full-default", parse_options({})["minStep"] == 1 and parse_options({})["stepMax"] == 22, str(parse_options({})))
    rec("opt-ind-types-on", all(parse_options({})[k] is True for k in (
        "indTypeSignals", "indTypeState", "indTypeDirection", "indTypeMove",
        "indTypeActive", "indTypeCommon", "indTypeTrend", "indTypeBreak",
    )))
    rec("opt-hours-default-7", parse_options({})["hours"] == 7)
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
    covj = job.get("coverage") or {}
    rec("calc-validated-count", 0 <= int(job.get("validatedCount") or 0) <= int(job.get("rowCount") or 0) and 0 <= int(covj.get("validatedCount") or 0) <= int(covj.get("setCount") or covj.get("product") or 0), f"rows={job.get('validatedCount')}/{job.get('rowCount')} catalog={covj.get('validatedCount')}/{covj.get('setCount')}")
    rec("calc-positive-pf-validation", all(is_positive_pf(r.get("last15Ratio")) for r in (job.get("rows") or []) if r.get("validated")), "validated rows have PF >= 1.10 after cost")
    rec(
        "calc-evaluation-windows",
        set((job.get("evaluationWindows") or {}).get("windows") or []) == set(EVALUATION_WINDOWS)
        and all(set((r.get("evaluationWindows") or {}).keys()) >= {"last5", "last15", "last50", "last75"} for r in (job.get("rows") or [])[:8]),
        str((job.get("evaluationWindows") or {}).get("windows")),
    )
    rec("calc-source", job.get("source") in ("synth", "mixed"), str(job.get("source")))
    packs = set((job.get("coverage") or {}).get("packs") or [])
    rec("calc-packs", "indications" in packs and "general" in packs, str(packs))
    sls = {round(float(r["slRatio"]), 1) for r in (job.get("rows") or []) if r.get("kind") == "base"}
    cov_sl = set((job.get("coverage") or {}).get("slRatios") or []) or set(((job.get("coverage") or {}).get("bySl") or {}).keys())
    rec("calc-all-sl", sls >= {0.1, 0.6, 1.0, 1.6, 2.6, 3.0} or len(cov_sl) >= 30, str(sorted(sls)))
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
    cov_run = job.get("coverage") or {}
    rec(
        "calc-coverage-symbols-complete",
        float(((cov_run.get("symbols") or {}).get("coveragePct") or 0)) == 100.0
        and int((cov_run.get("symbols") or {}).get("completed") or 0) == int((cov_run.get("symbols") or {}).get("requested") or 0),
        str(cov_run.get("symbols")),
    )
    rec(
        "calc-coverage-sets-complete",
        float(((cov_run.get("sets") or {}).get("coveragePct") or 0)) == 100.0
        and int((cov_run.get("sets") or {}).get("completed") or 0) == int((cov_run.get("sets") or {}).get("requested") or 0)
        and int((cov_run.get("sets") or {}).get("requested") or 0) == int(cov_run.get("product") or 0) * int((cov_run.get("symbols") or {}).get("requested") or 0),
        str(cov_run.get("sets")),
    )
    rec(
        "calc-coverage-tasks-complete",
        float(((cov_run.get("tasks") or {}).get("coveragePct") or 0)) == 100.0,
        str(cov_run.get("tasks")),
    )
    rec(
        "calc-listings-complete",
        len((job.get("listings") or {}).get("rankedIds") or []) == int(cov_run.get("product") or 0)
        and len((job.get("listings") or {}).get("indexById") or {}) == int(cov_run.get("product") or 0)
        and int(((job.get("listings") or {}).get("relative") or {}).get("sets") or 0) == int(cov_run.get("product") or 0),
        str((job.get("listings") or {}).get("relative")),
    )
    rec(
        "calc-windows-complete",
        all(
            set(((r.get("evaluationWindows") or {})).keys()) >= {"last5", "last15", "last25"}
            for r in (job.get("rows") or [])[:8]
        ) if job.get("rows") else False,
        str(list(((job.get("rows") or [{}])[0].get("evaluationWindows") or {}).keys())),
    )

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
    rec("calc-oneshot-worker", "def _replay_symbol_worker" in prod_src)
    rec("calc-pool-workers-helper", "def replay_pool_workers" in prod_src)
    rec("calc-oneshot-small-book", job.get("replayMode") == "oneshot", str(job.get("replayMode")))
    rec("calc-20h-oneshot", long_job.get("replayMode") == "oneshot", str(long_job.get("replayMode")))
    rec("pool-workers-small-cpu", replay_pool_workers(8, 780, None, 2) == 2, str(replay_pool_workers(8, 780, None, 2)))
    rec("pool-workers-eight-cpu", replay_pool_workers(8, 780, None, 8) == 8, str(replay_pool_workers(8, 780, None, 8)))
    rec("pool-workers-fat-uses-cpu", replay_pool_workers(25, 34000, None, 8) == 8, str(replay_pool_workers(25, 34000, None, 8)))
    rec("pool-workers-honor", replay_pool_workers(8, 780, 4, 2) == 4, str(replay_pool_workers(8, 780, 4, 2)))
    rec(
        "calc-listings-index",
        isinstance(job.get("listings"), dict)
        and len((job.get("listings") or {}).get("rankedIds") or []) >= int(job.get("rowCount") or 0) / 4
        and isinstance((job.get("listings") or {}).get("relative"), dict),
        str({k: (len(v) if isinstance(v, list) else v) for k, v in ((job.get("listings") or {}).items()) if k != "indexById"}),
    )

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


def cli_options(args: Sequence[str]) -> Dict[str, Any]:
    """Translate direct CLI flags into the same request schema used by the UI."""
    body: Dict[str, Any] = {}
    value_flags = {"--hours": "hours", "--workers": "workers", "--min-step": "minStep", "--step-max": "stepMax"}
    for flag, key in value_flags.items():
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                raw = args[i + 1]
                try:
                    body[key] = float(raw) if key == "hours" else int(raw)
                except ValueError:
                    pass
    bool_flags = {
        "--forced-only": "forcedOnly",
        "--all-symbols": "allSymbols",
        "--all-configs": "allConfigs",
        "--all-steps": "allSteps",
        "--trailing": "trailing",
        "--block": "stratBlock",
        "--general": "stratGeneral",
        "--indications": "stratIndications",
    }
    for flag, key in bool_flags.items():
        if flag in args:
            body[key] = True
    if "--indication-types" in args:
        i = args.index("--indication-types")
        if i + 1 < len(args):
            kinds = {x.strip().lower() for x in args[i + 1].split(",") if x.strip()}
            for kind in IND_KINDS:
                body[f"indType{kind.title()}"] = kind in kinds
    return body


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
    body: Dict[str, Any] = cli_options(args)
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
    elif not body:
        try:
            if os.path.exists(req_path()):
                body = json.loads(open(req_path()).read() or "{}")
        except Exception:
            body = {}
    if "--bg" in args:
        # Production HTTP requests are lane requests.  The direct CLI remains
        # an offline/compatibility harness and owns its synchronous worker.
        job = run_calc(body, persist=True)
        print(json.dumps(job))
        raise SystemExit(0 if job.get("phase") == "ready" else 1)
    job = run_calc(body, persist=True)
    print(json.dumps({k: job.get(k) for k in ("ok", "phase", "pct", "detail", "hours", "lookback", "rowCount", "validatedCount", "source", "error", "elapsedMs", "winner")}))
    raise SystemExit(0 if job.get("phase") == "ready" else 1)
