#!/usr/bin/env python3
"""Independent BingX X01 live pulse scalper with exchange control orders."""
from __future__ import annotations

import hmac
import hashlib
import json
import math
import os
import random
import re
import string
import subprocess
import time
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from typing import Any, Deque, Dict, List, Optional, Tuple
from block_engine import BlockBook, calculate_block_volume_increment_ratio, calculate_block_minimum_profit_factor
from coord_engine import Coordinator
from bingx_fast import FastBingX, ErrorLog
from modules import resolve as resolve_modules
from position_cost import last_n_cost_pf, resolve_sl_tp, POSITION_COST_PCT_DEFAULT, cost_as_frac, net_pnl_pct, net_pnl_usdt
from indication_engine import IndicationBook, self_test as indication_self_test, TIMEFRAMES
from risk_variants import VariantBook, self_test as variants_self_test
from set_engine import SetBook, self_test as sets_self_test
from exit_engine import ExitBook, self_test as exit_self_test
from dca_engine import DcaBook, self_test as dca_self_test
from load_engine import LoadGovernor, BoundedSet, trim_map, cap_map, prune_ttl, cap_list

CONN_SHORT = os.environ.get("PULSE_CONN", "bingx-x02").replace("connection:", "")
REDIS_CONN = f"connection:{CONN_SHORT}"
BASE = os.environ.get("PULSE_BASE", "") or "https://open-api.bingx.com"
DIR = "/opt/grok-x01-pulse"
STATS_PATH = os.path.join(DIR, f"stats-{CONN_SHORT}.json")
TRADES_PATH = os.path.join(DIR, f"trades-{CONN_SHORT}.jsonl")
STOP_PATH = os.path.join(DIR, f"STOP-{CONN_SHORT}")
PAUSE_PATH = os.path.join(DIR, f"PAUSE-{CONN_SHORT}")
STOP_ALL = os.path.join(DIR, "STOP")
LOG_PATH = os.path.join(DIR, f"pulse-{CONN_SHORT}.log")
BLOCK_PATH = os.path.join(DIR, f"block-state-{CONN_SHORT}.json")
OVERLAY_PATH = os.path.join(DIR, f"overlay-{CONN_SHORT}.json")
OPEN_PATH = os.path.join(DIR, f"open-{CONN_SHORT}.json")
CTS_PATH = os.path.join(DIR, f"cts-settings-{CONN_SHORT}.json")
ERR_PATH = os.path.join(DIR, f"errors-{CONN_SHORT}.jsonl")
LEV_PATH = os.path.join(DIR, f"lev-set-{CONN_SHORT}.json")
START_EQ_PATH = os.path.join(DIR, f"start-eq-{CONN_SHORT}.json")
RESET_EQ_PATH = os.path.join(DIR, f"reset-eq-{CONN_SHORT}")

UNIVERSE_PATH = os.path.join(DIR, "universe.json")
MAX_SYMBOLS = 0  # 0 = unlimited
SYMBOLS = [
    "SOL-USDT", "XRP-USDT", "HYPE-USDT", "JUP-USDT", "ETC-USDT", "TRX-USDT",
    "DOGE-USDT", "APT-USDT", "ENA-USDT", "LDO-USDT", "1000PEPE-USDT", "KAS-USDT",
]
GROUPS = {
    "majors": {"SOL-USDT", "XRP-USDT", "ETC-USDT"},
    "meme": {"DOGE-USDT", "1000PEPE-USDT", "KAS-USDT", "JUP-USDT"},
    "l1": {"APT-USDT", "HYPE-USDT", "TRX-USDT"},
    "defi": {"ENA-USDT", "LDO-USDT", "JTO-USDT", "ZRO-USDT", "COMP-USDT", "ORDI-USDT"},
}


def _sf(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def coerce_symbol_sort(raw: Any) -> str:
    s = str(raw or "vol1h").strip()
    return s if s in SYMBOL_SORTS else "vol1h"


def symbol_metric(row: Dict[str, Any], sort: str) -> float:
    s = coerce_symbol_sort(sort)
    if s == "vol24h":
        return _sf(row.get("vol24h"))
    if s == "quoteVolume":
        return _sf(row.get("quoteVolume"))
    if s == "changeAbs":
        return abs(_sf(row.get("changePct")))
    if s == "changePct":
        return _sf(row.get("changePct"))
    if s == "leverage":
        return _sf(row.get("maxLeverage"))
    v1 = _sf(row.get("vol1h"))
    if v1 > 0:
        return v1
    v24 = _sf(row.get("vol24h"))
    if v24 > 0:
        return v24 / 4.9
    return abs(_sf(row.get("changePct")))


def symbol_rank_key(row: Dict[str, Any], sort: str) -> Tuple[float, float]:
    """Always highest exchange leverage first, then the selected criterion (default 1H vol)."""
    lev = -_sf(row.get("maxLeverage"))
    s = coerce_symbol_sort(sort)
    if s == "leverage":
        return (lev, -symbol_metric(row, "vol1h"))
    return (lev, -symbol_metric(row, s))


def rank_self_test() -> Tuple[bool, str]:
    rows = [
        {"symbol": "A-USDT", "maxLeverage": 50, "vol1h": 9.0, "vol24h": 0, "quoteVolume": 1, "changePct": 0},
        {"symbol": "B-USDT", "maxLeverage": 150, "vol1h": 1.0, "vol24h": 0, "quoteVolume": 1, "changePct": 0},
        {"symbol": "C-USDT", "maxLeverage": 150, "vol1h": 8.0, "vol24h": 0, "quoteVolume": 1, "changePct": 0},
    ]
    got = [r["symbol"] for r in sorted(rows, key=lambda r: symbol_rank_key(r, "vol1h"))]
    ok = got == ["C-USDT", "B-USDT", "A-USDT"]
    return ok, f"got={got}"

TARGET_NOTIONAL = 2.15
LEVERAGE = 150
USE_MAX_LEVERAGE = True
MAX_OPEN = 0  # 0 = unlimited
MAX_PER_GROUP = 0  # 0 = unlimited
SL_PCT = 0.0048
TP_PCT = 0.0075
TRAIL_ARM = 0.0032
TRAIL_GIVE = 0.0016
TIME_STOP_S = 21600
MAX_HOLD_S = 21600
SCRATCH_S = 600
SCRATCH_MIN = 0.0016
SCAN_S = 0.20
KLINE_EVERY = 2.4
KLINE_WORKERS = 4
KLINE_LIMIT = 60
KLINE_BATCH = 12
TF_EVERY = {"1m": 2.0, "5m": 6.0, "15m": 12.0}
TF_BATCH = {"1m": 8, "5m": 12, "15m": 8}
UNIVERSE_EVERY = 12.0
VOL1H_EVERY = 8.0
VOL1H_BATCH = 10
SYMBOL_SORTS = ("vol1h", "vol24h", "quoteVolume", "changeAbs", "changePct", "leverage")
BALANCE_EVERY = 6.0
QA_EVERY = 5
COOLDOWN_S = 9.0
STAGGER_S = 0.12
DD_HALT = 0.18
EQ_MIN = 0.20
RECV = 5000
TAG = "G" + (
    CONN_SHORT.split("-")[-1]
    if CONN_SHORT.split("-")[-1].startswith("x")
    else "x01"
)
SL_TYPES = {"STOP_MARKET", "STOP", "TRIGGER_MARKET"}
TP_TYPES = {"TAKE_PROFIT_MARKET", "TAKE_PROFIT", "TP_MARKET"}


def real_oid(v: Any) -> str:
    s = str(v or "").strip()
    if not s:
        return ""
    if s.lower() in ("exists", "none", "null", "0", "true", "false"):
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", s):
        return ""
    return s


_DOC_URL_RE = re.compile(r"https?://\S+", re.I)
_AUTH_TAIL_RE = re.compile(r"please verify our authentication.*", re.I)
_TRANSIENT_API = (
    "signature",
    "insufficient margin",
    "insufficient liquidity",
    "cooling",
    "rate limit",
    "frequency",
    "order not exist",
    "position not exist",
    "quantity or stopprice is must",
    "parameter quantity",
    "order size must be less",
    "available amount",
    "stop loss price should",
    "take profit price should",
)


def short_api_msg(msg: str) -> str:
    m = " ".join(str(msg or "").split())
    m = _DOC_URL_RE.sub("", m)
    m = _AUTH_TAIL_RE.sub("", m)
    low = m.lower()
    if "signature" in low:
        return "signature mismatch"
    if "insufficient" in low and "liquidity" in low:
        return "insufficient liquidity"
    if "insufficient" in low and "margin" in low:
        return "insufficient margin"
    if "quantity or stopprice is must" in low or "parameter quantity" in low:
        return "ctrl qty/stop"
    if "order size must be less" in low or "available amount" in low:
        return "order too large"
    if "cooling" in low:
        return "cooling"
    return m.strip(" ,.")[:120]


def is_transient_api(msg: str) -> bool:
    low = str(msg or "").lower()
    return any(k in low for k in _TRANSIENT_API)


def extract_oid(r: Any) -> str:
    if not isinstance(r, dict):
        return real_oid(r)
    data = r.get("data") if isinstance(r.get("data"), dict) else r
    order = data.get("order") if isinstance(data, dict) and isinstance(data.get("order"), dict) else data
    if isinstance(order, dict):
        for k in ("orderId", "orderID", "orderid"):
            oid = real_oid(order.get(k))
            if oid:
                return oid
    if isinstance(data, dict):
        for nest in ("stopLoss", "takeProfit", "sl", "tp"):
            sub = data.get(nest)
            if isinstance(sub, dict):
                oid = real_oid(sub.get("orderId") or sub.get("orderID"))
                if oid:
                    return oid
            elif isinstance(sub, list):
                for row in sub:
                    if isinstance(row, dict):
                        oid = real_oid(row.get("orderId") or row.get("orderID"))
                        if oid:
                            return oid
    return real_oid(r.get("orderId") or r.get("orderID"))


def ctrl_payload(
    symbol: str,
    side: str,
    kind: str,
    stop_px: str,
    qty: str,
    cid: str,
    close_pos: bool = True,
    with_qty: bool = False,
    otype: str = "",
    working: str = "MARK_PRICE",
) -> Dict[str, Any]:
    """BingX rejects quantity + closePosition together. Never mix them."""
    is_sl = str(kind).lower() in ("sl", "s", "u", "sec-sl", "sec_sl")
    close_side = "SELL" if str(side).upper() == "LONG" else "BUY"
    if not otype:
        otype = "STOP_MARKET" if is_sl else "TAKE_PROFIT_MARKET"
    body: Dict[str, Any] = {
        "symbol": symbol,
        "type": otype,
        "side": close_side,
        "positionSide": str(side).upper(),
        "stopPrice": str(stop_px),
        "workingType": working or "MARK_PRICE",
        "clientOrderID": cid,
    }
    if close_pos:
        body["closePosition"] = "true"
    elif with_qty and str(qty):
        body["quantity"] = str(qty)
    if otype in ("STOP", "TAKE_PROFIT"):
        body["price"] = str(stop_px)
    return body


def tpsl_attach_json(sl_px: str, tp_px: str) -> Dict[str, str]:
    sl = {"type": "STOP_MARKET", "stopPrice": str(sl_px), "price": str(sl_px), "workingType": "MARK_PRICE"}
    tp = {"type": "TAKE_PROFIT_MARKET", "stopPrice": str(tp_px), "price": str(tp_px), "workingType": "MARK_PRICE"}
    return {
        "stopLoss": json.dumps(sl, separators=(",", ":")),
        "takeProfit": json.dumps(tp, separators=(",", ":")),
    }


def sl_bounds(side: str, mark: float, last: float, entry: float, liq: float, tick: float) -> Tuple[float, float]:
    nums = [float(x) for x in (mark, last) if x and float(x) > 0]
    e = float(entry or 0)
    if not nums and e > 0:
        nums = [e]
    if not nums:
        return 0.0, 0.0
    hi_px, lo_px = max(nums), min(nums)
    tick = max(float(tick or 0) or 0.0, hi_px * 1e-6, 1e-8)
    pad = max(16 * tick, hi_px * 0.0050)
    liq = float(liq or 0)
    if liq > 0:
        dist = abs(liq - (lo_px if str(side).upper() == "LONG" else hi_px))
        if dist > 0:
            pad = min(pad, max(2 * tick, dist * 0.40))
    is_long = str(side).upper() == "LONG"
    if is_long:
        upper = lo_px - pad
        lower = (liq * 1.001) if liq > 0 else (e * (1.0 - 0.0055) if e > 0 else lo_px * 0.9945)
        if e > 0 and hi_px > e * 1.0015:
            lower = max(lower, min(e * 1.0001, upper - tick))
        if lower >= upper:
            lower = upper - max(tick, hi_px * 0.002)
        return float(lower), float(upper)
    lower = hi_px + pad
    upper = (liq * 0.999) if liq > 0 else (e * (1.0 + 0.0055) if e > 0 else hi_px * 1.0055)
    if e > 0 and lo_px < e * 0.9985:
        upper = max(upper, lower + tick)
        upper = min(upper, max(e * 0.9999, lower + tick))
    if lower >= upper:
        upper = lower + max(tick, hi_px * 0.002)
    return float(lower), float(upper)


def ctrl_err_kind(msg: str) -> str:
    m = str(msg or "").lower()
    compact = m.replace(" ", "")
    if "liquidation" in m:
        return "liq"
    if "already exists" in m:
        return "exists"
    if "quantity" in m and "closeposition" in compact:
        return "qty_close"
    if "quantity or stopprice is must" in compact or "parameterquantity" in compact:
        return "px"
    if "insufficient liquidity" in m:
        return "liq"
    if "trigger price" in m or "current price" in m or "stop loss price" in m or "take profit price" in m:
        return "px"
    if "exceeded" in m and "limit" in m:
        return "cap"
    if "position not exist" in m or "position does not exist" in m:
        return "flat"
    if "order size" in m or "available amount" in m:
        return "qty"
    return "other"


_LOG_N = 0
_LOG_LAST: Dict[str, float] = {}
_LOG_BUF: List[str] = []
_LOG_FLUSH = 0.0


def log(msg: str, every: float = 0.0, key: str = "", quiet: bool = False) -> None:
    global _LOG_N, _LOG_FLUSH
    if every > 0:
        k = key or msg[:48]
        now = time.time()
        if now - _LOG_LAST.get(k, 0.0) < every:
            return
        _LOG_LAST[k] = now
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    if not quiet:
        print(line, flush=False)
    try:
        _LOG_BUF.append(line + "\n")
        now = time.time()
        if len(_LOG_BUF) >= 8 or now - _LOG_FLUSH >= 1.2:
            with open(LOG_PATH, "a") as f:
                f.writelines(_LOG_BUF)
            _LOG_BUF.clear()
            _LOG_FLUSH = now
            _LOG_N += 8
            if _LOG_N % 200 == 0:
                rotate_log(LOG_PATH, 220_000)
    except Exception:
        _LOG_BUF.clear()


def rotate_log(path: str, max_bytes: int) -> None:
    try:
        if os.path.getsize(path) < max_bytes:
            return
        with open(path, "rb") as f:
            f.seek(-min(max_bytes // 2, os.path.getsize(path)), os.SEEK_END)
            f.readline()
            tail = f.read()
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(tail)
        os.replace(tmp, path)
    except Exception:
        pass


def sd_notify(msg: str) -> None:
    sock = os.environ.get("NOTIFY_SOCKET")
    if not sock:
        return
    try:
        import socket as _s
        s = _s.socket(_s.AF_UNIX, _s.SOCK_DGRAM)
        addr = "\0" + sock[1:] if sock.startswith("@") else sock
        s.connect(addr)
        s.sendall(msg.encode())
        s.close()
    except Exception:
        pass


def rss_mb() -> float:
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * 4096 / 1048576.0
    except Exception:
        return 0.0


def redis_hget(field: str) -> str:
    p = subprocess.run(["redis-cli", "HGET", REDIS_CONN, field], capture_output=True, text=True)
    return (p.stdout or "").strip()


def load_json_file(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def dump_cts_settings() -> dict:
    p = subprocess.run(["redis-cli", "HGETALL", f"settings:connection_settings:{CONN_SHORT}"], capture_output=True, text=True)
    lines = (p.stdout or "").splitlines()
    out = {}
    for i in range(0, len(lines) - 1, 2):
        v = lines[i + 1]
        if v[:1] in "{[":
            try:
                out[lines[i]] = json.loads(v)
                continue
            except Exception:
                pass
        if v in ("true", "false"):
            out[lines[i]] = v == "true"
            continue
        try:
            out[lines[i]] = float(v) if "." in v else int(v)
        except Exception:
            out[lines[i]] = v
    try:
        tmp = CTS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f)
        os.replace(tmp, CTS_PATH)
    except Exception:
        pass
    return out


class BingX:
    """Compatibility alias — live client is FastBingX."""

    pass


@dataclass
class Contract:
    symbol: str
    min_qty: float
    step: float
    qprec: int
    pprec: int
    min_usdt: float
    max_lev: int = 150


@dataclass
class Position:
    symbol: str
    side: str
    qty: float
    entry: float
    opened_at: float
    sl: float
    tp: float
    peak: float
    trail_armed: bool = False
    trail: Optional[float] = None
    order_id: str = ""
    sl_oid: str = ""
    tp_oid: str = ""
    notional: float = 0.0
    reason: str = ""
    controls_ok: bool = False
    conf: float = 0.3
    sl_ratio: float = 0.6
    trail_key: str = "0.3:0.1"
    trail_arm: float = 0.003
    trail_give: float = 0.001
    sl_pct: float = 0.0
    tp_pct: float = 0.0
    set_id: str = ""
    set_idx: int = -1
    trail_set_id: str = ""
    trail_idx: int = -1
    pack: str = ""
    client_id: str = ""
    ours: bool = True
    overall: bool = True
    close_position: bool = True
    ctrl_qty: float = 0.0
    sec_sl_oid: str = ""
    sec_tp_oid: str = ""
    sec_sl: float = 0.0
    sec_tp: float = 0.0
    ind_kind: str = ""
    liq: float = 0.0
    position_id: str = ""
    ctrl_verified: bool = False


@dataclass
class Closed:
    t: f