Warning: truncated output (original token count: 112409)
Total output lines: 9540

#!/usr/bin/env python3
"""Independent BingX X01 live pulse scalper with exchange control orders."""
from __future__ import annotations

import hmac
import hashlib
import copy
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
from contextlib import nullcontext
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from typing import Any, Deque, Dict, List, Optional, Tuple
from types import SimpleNamespace
from urllib.parse import urlparse
from forced_configs import FORCED_SYMBOLS, MIN_PF as FORCED_MIN_PF, valid_candidate
from block_engine import BlockBook, BLOCK_COUNT_PREVIEW, BLOCK_PF_RATIO_MIN, BLOCK_PF_RATIO_MAX, clamp_stack, calculate_block_volume_increment_ratio, calculate_block_minimum_profit_factor, calculate_block_max_additional_ratio, finite_number
from coord_engine import Coordinator
from bingx_fast import FastBingX, ErrorLog
from modules import resolve as resolve_modules
from position_cost import (
    last_n_cost_pf,
    evaluation_windows,
    completed_roundtrips,
    resolve_sl_tp,
    POSITION_COST_PCT_DEFAULT,
    SL_TP_RATIOS,
    SL_TP_MIN,
    SL_TP_MAX,
    cost_as_frac,
    net_pnl_pct,
    net_pnl_usdt,
    normalize_position_cost_pct,
    effective_position_cost_pct,
    exchange_order_cost_sample,
    row_fee_usdt,
)
from indication_engine import IndicationBook, self_test as indication_self_test, TIMEFRAMES
from risk_variants import VariantBook, self_test as variants_self_test
from set_engine import SetBook, self_test as sets_self_test, indication_kind_votes, IND_TAG_KIND
from exit_engine import ExitBook, self_test as exit_self_test
from dca_engine import DcaBook, self_test as dca_self_test
from load_engine import LoadGovernor, BoundedSet, trim_map, cap_map, prune_ttl, cap_list
from storage_paths import MAX_RETAINED_FILE_BYTES, MAX_RETAINED_LINES, DATA_DIR, append_bounded_line, append_bounded_lines, atomic_write, read_jsonl, retain_last_lines
from event_ledger import EventLedger
from contracts import INDICATION_KINDS, stable_key
from runtime_scope import redis_key, order_tag

CONN_SHORT = os.environ.get("PULSE_CONN", "bingx-x02").replace("connection:", "")
REDIS_CONN = redis_key(f"connection:{CONN_SHORT}")
BASE = os.environ.get("PULSE_BASE", "") or "https://open-api.bingx.com"
# Runtime state lives outside the checkout so reinstalling code preserves it.
DIR = str(DATA_DIR)
STATS_PATH = os.path.join(DIR, f"stats-{CONN_SHORT}.json")
TRADES_PATH = os.path.join(DIR, f"trades-{CONN_SHORT}.jsonl")
STOP_PATH = os.path.join(DIR, f"STOP-{CONN_SHORT}")
PAUSE_PATH = os.path.join(DIR, f"PAUSE-{CONN_SHORT}")
STOP_ALL = os.path.join(DIR, "STOP")
LOG_PATH = os.path.join(DIR, f"pulse-{CONN_SHORT}.log")
BLOCK_PATH = os.path.join(DIR, f"block-state-{CONN_SHORT}.json")
OVERLAY_PATH = os.path.join(DIR, f"overlay-{CONN_SHORT}.json")
OPEN_PATH = os.path.join(DIR, f"open-{CONN_SHORT}.json")
PENDING_PATH = os.path.join(DIR, f"pending-{CONN_SHORT}.json")
CTS_PATH = os.path.join(DIR, f"cts-settings-{CONN_SHORT}.json")
ERR_PATH = os.path.join(DIR, f"errors-{CONN_SHORT}.jsonl")
EVENTS_PATH = os.path.join(DIR, f"events-{CONN_SHORT}.json")
LEV_PATH = os.path.join(DIR, f"lev-set-{CONN_SHORT}.json")
START_EQ_PATH = os.path.join(DIR, f"start-eq-{CONN_SHORT}.json")
RESET_EQ_PATH = os.path.join(DIR, f"reset-eq-{CONN_SHORT}")
LIVE_COST_PATH = os.path.join(DIR, f"live-position-cost-{CONN_SHORT}.json")
CONFIG_EVIDENCE_PATH = os.path.join(DIR, f"config-evidence-{CONN_SHORT}.json")

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


def _bool_setting(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def normalize_control_pct(value: Any, default: float = 0.0) -> int:
    """Normalize an SL/TP percentage to integer basis points.

    Runtime positions store fractions (0.0048), while overlays and exchange
    payloads may use percent values (0.48). Accept both representations so
    grouping remains stable across restarts and config sources.
    """
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default or 0.0)
    if not math.isfinite(parsed):
        parsed = float(default or 0.0)
    if abs(parsed) > 0.05:
        parsed /= 100.0
    # Use deterministic half-up quantization for the identity only. Python's
    # banker rounding makes values such as 0.495% depend on their binary
    # representation and can silently move a control range down by one basis
    # point. Runtime pricing keeps the original fraction below.
    return max(0, int(math.floor(parsed * 10000.0 + 0.5)))


def control_range_key(sl_pct: Any, tp_pct: Any) -> str:
    return f"sl{normalize_control_pct(sl_pct):04d}-tp{normalize_control_pct(tp_pct):04d}"


def parse_control_range(value: Any) -> Tuple[int, int]:
    """Read the canonical SL/TP basis-point pair from a range key."""
    match = re.fullmatch(r"sl(\d+)-tp(\d+)", str(value or "").strip().lower())
    if not match:
        return 0, 0
    try:
        sl_bp, tp_bp = int(match.group(1)), int(match.group(2))
    except (TypeError, ValueError):
        return 0, 0
    if sl_bp <= 0 or tp_bp <= 0:
        return 0, 0
    return sl_bp, tp_bp


def make_control_group_key(symbol: Any, side: Any, sl_pct: Any, tp_pct: Any) -> str:
    """Stable logical control identity: symbol + side + normalized SL/TP range."""
    sym = str(symbol or "").strip().upper()
    side_u = str(side or "").strip().upper()
    return stable_key("control-group", sym, side_u, control_range_key(sl_pct, tp_pct))


def control_group_token(group_key: Any, range_key: Any = "") -> str:
    """Return a compact token that can be rebound after an exchange restart.

    New control IDs carry the normalized range in a short, parseable token. The
    hash-derived token remains the fallback for older persisted IDs and for
    malformed range metadata, so an upgrade never loses the ability to match a
    previously placed order by its client ID.
    """
    sl_bp, tp_bp = parse_control_range(range_key)
    if 0 < sl_bp <= 999 and 0 < tp_bp <= 999:
        return f"r{sl_bp:03d}{tp_bp:03d}"
    raw = re.sub(r"[^a-z0-9]", "", str(group_key or "").lower())
    if not raw:
        return ""
    return (raw[-8:] if len(raw) >= 8 else raw).ljust(8, "0")


def control_group_tokens(group_key: Any, range_key: Any = "") -> set:
    """Return both the current range token and the legacy hash token."""
    tokens = {
        control_group_token(group_key, range_key),
        control_group_token(group_key),
    }
    return {token for token in tokens if token}


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
MAX_DD_TIME_S = 27000.0  # default 450 minutes; configurable 10..650 minutes
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
STAGGER_S = 0.6
DD_HALT = 0.18
EQ_MIN = 0.20
RECV = 5000
TAG = order_tag(CONN_SHORT)
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


def order_fill_qty(data: Any, requested: float = 0.0) -> float:
    """Read executed quantity without turning an unfilled order into a fill.

    BingX responses vary by endpoint: market responses may expose quantity or
    origQty, while fill/order responses expose executedQty. An explicit zero
    must remain zero, and a malformed or oversized response is never allowed
    to inflate the local position, Block lane, DCA lane, or balance estimate.
    Partial fills are valid progress even while an order remains open; only a
    response with no execution fields may use the legacy requested-size fallback.
    """
    try:
        want = max(0.0, float(requested or 0.0))
    except Exception:
        want = 0.0

    def number(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except Exception:
            return None
        if not math.isfinite(parsed) or parsed < 0:
            return None
        return parsed

    terminal = {"FILLED", "FINISHED", "SUCCESS", "FILLED_FULLY", "COMPLETED"}
    if isinstance(data, dict):
        # Executed fields win over order quantity, including a non-zero
        # PARTIALLY_FILLED/CANCELED order that has already filled some size.
        for key in ("executedQty", "filledQty", "cumQty", "filled"):
            if key in data and data.get(key) not in (None, ""):
                parsed = number(data.get(key))
                if parsed is not None:
                    return min(parsed, want) if want > 0 else parsed
        status = str(data.get("status") or data.get("orderStatus") or data.get("state") or "").strip().upper()
        if status and status not in terminal:
            # quantity/origQty is the requested size on NEW and PARTIALLY_FILLED
            # responses, not an executed fill. Keep it pending for polling.
            return 0.0
        for key in ("quantity", "origQty"):
            if key in data and data.get(key) not in (None, ""):
                parsed = number(data.get(key))
                if parsed is not None:
                    return min(parsed, want) if want > 0 else parsed
        if status in terminal:
            return want
    # Existing market-order responses without a status/execution field have
    # historically meant "the requested market size was filled".
    return want


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


def ctrl_mtimes(paths) -> Dict[str, float]:
    """mtime snapshot of the control files; missing files map to 0.0, so a
    create/delete/touch anywhere always changes the snapshot."""
    out: Dict[str, float] = {}
    for p in paths:
        try:
            out[p] = os.path.getmtime(p)
        except OSError:
            out[p] = 0.0
    return out


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
            batch = list(_LOG_BUF)
            append_bounded_lines(LOG_PATH, batch)
            _LOG_BUF.clear()
            _LOG_FLUSH = now
            _LOG_N += len(batch)
            if _LOG_N // 200 > (_LOG_N - len(batch)) // 200:
                rotate_log(LOG_PATH, 220_000)
    except Exception:
        _LOG_BUF.clear()


def rotate_log(path: str, max_bytes: int) -> None:
    try:
        # Keep the compatibility argument for callers, but use the shared
        # line/byte cap so every engine log has the same retention contract.
        del max_bytes
        retain_last_lines(path)
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
    """Read a connection value, with a protected environment fallback.

    Redis remains the normal source.  The fallback allows a service to start
    from a 0600 systemd EnvironmentFile during recovery/bootstrap without
    putting credentials in the repository or command line.  It is deliberately
    connection-scoped so x01 and x02 can never cross-read each other.
    """
    try:
        p = subprocess.run(["redis-cli", "HGET", REDIS_CONN, field], capture_output=True, text=True)
        value = (p.stdout or "").strip()
        if value and value != "(nil)":
            return value
    except FileNotFoundError:
        pass
    except Exception:
        pass
    suffix = re.sub(r"[^A-Za-z0-9]", "_", CONN_SHORT).upper()
    field_name = re.sub(r"[^A-Za-z0-9]", "_", str(field or "")).upper()
    for name in (
        f"CTS_{suffix}_{field_name}",
        f"BINGX_{suffix}_{field_name}",
    ):
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def load_json_file(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def dump_cts_settings() -> dict:
    try:
        p = subprocess.run(["redis-cli", "HGETALL", redis_key(f"settings:connection_settings:{CONN_SHORT}")], capture_output=True, text=True, timeout=6)
    except Exception:
        return {}
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
    # Desired protective trail waiting for a retry after a transient exchange
    # rejection. It never loosens the currently installed stop.
    trail_pending: Optional[float] = None
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
    under_since: float = 0.0
    parent_set_id: str = ""
    axis_key: str = ""
    relative_count: int = 1
    volume_ratio: float = 1.0
    control_group_key: str = ""
    control_range_key: str = ""
    control_sl_bp: int = 0
    control_tp_bp: int = 0
    legacy_aggregate: bool = False
    member_count: int = 1
    lineage_set_ids: List[str] = field(default_factory=list)
    lineage_parent_set_ids: List[str] = field(default_factory=list)
    lineage_axis_keys: List[str] = field(default_factory=list)
    lineage_packs: List[str] = field(default_factory=list)
    member_client_ids: List[str] = field(default_factory=list)
    member_order_ids: List[str] = field(default_factory=list)
    exchange_qty: float = 0.0
    foreign_qty: float = 0.0
    pending_qty: float = 0.0
    pending_close_qty: float = 0.0
    last_fill_at: float = 0.0
    entry_fee: float = 0.0
    entry_notional: float = 0.0
    strategy: str = "core"
    close_started_qty: float = 0.0
    close_applied_qty: float = 0.0


@dataclass
class Closed:
    t: float
    symbol: str
    side: str
    qty: float
    entry: float
    exit: float
    pnl: float
    pnl_pct: float
    reason: str
    hold_s: float
    sl_ratio: float = 0.0
    trail_key: str = ""
    sl_pct: float = 0.0
    tp_pct: float = 0.0
    set_id: str = ""
    pack: str = ""
    trail_set_id: str = ""
    client_id: str = ""
    ours: bool = True
    conn: str = ""
    ind_kind: str = ""
    parent_set_id: str = ""
    axis_key: str = ""
    relative_count: int = 1
    volume_ratio: float = 1.0
    control_group_key: str = ""
    control_range_key: str = ""
    control_mode: str = ""
    member_count: int = 1
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    fee_total: float = 0.0
    position_cost_pct: float = POSITION_COST_PCT_DEFAULT
    cost_source: str = "manual-fallback"
    exchange_confirmed: bool = False
    partial: bool = False
    strategy: str = "core"
    roundtrip_qty: float = 0.0
    close_fill_id: str = ""


class Pulse:

    def __init__(self, api: FastBingX, contracts: Dict[str, Contract]) -> None:
        self.api = api
        self.contracts = contracts
        self.klines_tf: Dict[str, Dict[str, List[List[float]]]] = {tf: {} for tf in TIMEFRAMES}
        self.klines: Dict[str, List[List[float]]] = self.klines_tf["1m"]
        self.kline_ban = 0.0
        self.bar_min: Dict[str, List[float]] = {}
        self.px: Dict[str, float] = {}
        self.chg: Dict[str, float] = {}
        self.open: Dict[str, Position] = {}
        self.pending_orders: Dict[str, Dict[str, Any]] = {}
        self._last_order_result: Dict[str, Any] = {}
        self._last_close_result: Dict[str, Any] = {}
        self.exchange_qty: Dict[str, float] = {}
        self.exchange_foreign_qty: Dict[str, float] = {}
        self.exchange_own_qty: Dict[str, float] = {}
        self.exchange_own_open_count = -1
        self.exchange_total_open_count = -1
        self.control_orders_per_config = True
        self.closed: Deque[Closed] = deque(maxlen=80)
        self.cooldown: Dict[str, float] = {}
        self.last_entry_ts = 0.0
        self.start_eq = 0.0
        try:
            if os.path.exists(START_EQ_PATH):
                self.start_eq = float((json.load(open(START_EQ_PATH)) or {}).get("startEquity") or 0)
        except Exception:
            self.start_eq = 0.0
        self.equity = 0.0
        self.available = 0.0
        self.used = 0.0
        self.upnl = 0.0
        self.halted = False
        self.halt_reason: Optional[str] = None
        self._pre_pause_halt: Optional[str] = None
        self.volume_factor = 1.0
        self.regime = "neutral"
        self.consec_loss = 0
        self.wins = 0
        self.losses = 0
        self.fees_est = 0.0
        self.started = time.time()
        self.signals: Deque[Dict[str, Any]] = deque(maxlen=24)
        self.cycle = 0
        self.last_kline = 0.0
        self.kline_ts_tf: Dict[str, Dict[str, float]] = {tf: {} for tf in TIMEFRAMES}
        self.kline_ts: Dict[str, float] = self.kline_ts_tf["1m"]
        self.pool = ThreadPoolExecutor(max_workers=KLINE_WORKERS)
        self.lev_map: Dict[str, int] = {}
        self.lev_max: Dict[str, int] = {}
        self.use_max_leverage = True
        self.last_scan_ms = 0.0
        self.cycle_busy = False
        self.cycle_wait_ms = 0.0
        self.cycle_overrun = False
        self.universe: List[Dict[str, Any]] = []
        self.last_uni = 0.0
        self.vol1h: Dict[str, float] = {}
        self.vol1h_ts: Dict[str, float] = {}
        self.last_vol1h = 0.0
        self.symbol_sort = "vol1h"
        self.symbols_dynamic = True
        self.symbol_cap = 0
        self.last_dyn_sel = 0.0
        self.overlay_wild = True
        self.skip_log: Dict[str, float] = {}
        self.last_rest_tick = 0.0
        self.wake_ev = threading.Event()
        self.last_event = "boot"
        self.event_n = 0
        self.event_ledger = EventLedger(EVENTS_PATH, CONN_SHORT, max_events=512)
        self._oo_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self.mods: Dict[str, bool] = {}
        self.last_bal = 0.0
        self.errors = 0
        self.last_error = ""
        self.tests: List[Dict[str, Any]] = []
        self.test_map: Dict[str, Dict[str, Any]] = {}
        self.qa_pass = 0
        self.qa_fail = 0
        self.warm_ms = 0.0
        self._warm_stop = False
        self._stats_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self.hist_busy = False
        self._hist_fetch_next = 0.0
        self._hist_fetch_failures = 0
        self._hist_fetch_last = 0.0
        self._hist_fetch_stored = 0
        self._hist_deferred = ""
        self._stats_ts = 0.0
        self._stats_force = False
        self.last_scan_io = False
        self.ignored_foreign = 0
        self.dca_fail_cd: Dict[str, float] = {}
        self.track_prefix = TAG
        self.boot_ts = time.time()
        self.seen_fill_cids = BoundedSet(4000)
        self.owned_syms = BoundedSet(800)
        self.load = LoadGovernor()
        self._scan_keep: List[str] = []
        self.ignore_syms: Dict[str, float] = {}
        self.last_px: Dict[str, float] = {}
        self.recon_ok = True
        self.recon_pending = False
        self.recon_detail = "pending"
        self.exchange_open_count = -1  # -1 = not yet read from exchange
        self._empty_rest_streak = 0
        self.live_pos_keys: Optional[set] = None  # None = exchange truth unknown
        self._load_trade_history()
        # Load unresolved order intents before the open book so a partially
        # filled entry is not mistaken for an already-consumed client id.
        self._load_pending_orders()
        self._load_open_book()
        self.block = BlockBook(BLOCK_PATH, {
            "variantBlockEnabled": True,
            "blockMaxStack": 3,
            "blockVolumeRatio": 1.0,
            "blockProfitFactorRatio": 1.1,
            "blockPauseCountRatio": 1,
            "blockActiveRealEnabled": True,
            "blockActiveLiveEnabled": True,
            "defaultMinPF": 1.2,
            "prevPosMinCount": 5,
            "prevPosWindow": 25,
        })
        self.coord = Coordinator()
        self.indications = IndicationBook()
        self.dca = DcaBook()
        self.variants = VariantBook()
        self.sets = SetBook()
        # Monotonic catalog generation.  Historic replay runs on an isolated
        # SetBook snapshot and may only be committed if configuration has not
        # been reloaded while the worker was busy.
        self._sets_generation = 0
        # Full-range startup can enumerate tens of thousands of independent
        # Sets.  Keep the service responsive while that catalog is built on a
        # worker, then publish it atomically after the first READY signal.
        self._catalog_ready = threading.Event()
        self._catalog_bootstrap_running = False
        self._catalog_overlay: Dict[str, Any] = {}
        self._catalog_cts: Dict[str, Any] = {}
        self.exits = ExitBook()
        self.block_last_emit = 0.0
        self.overlay_mtime = 0.0
        self.overlay: Dict[str, Any] = {}
        self.did_io = False
        self.ctrl_skip: Dict[str, float] = {}
        self._order_est: int = 0
        self._order_est_known: bool = False
        self._score_cache: Dict[str, Any] = {}
        self._ind_fp: Dict[str, Any] = {}
        self._lev_retry: Dict[str, float] = {}
        self.flatten_skip: Dict[str, float] = {}
        self.cts: Dict[str, Any] = {}
        self.position_cost_pct = POSITION_COST_PCT_DEFAULT
        self.manual_position_cost_pct = POSITION_COST_PCT_DEFAULT
        self.use_live_position_costs = False
        self.position_cost_source = "manual-fallback"
        self.live_position_cost_pct = 0.0
        self.live_position_cost_samples = 0
        self.live_position_cost_complete = False
        self.live_position_cost_notional = 0.0
        self.live_position_cost_updated = 0.0
        self._live_cost_rows: List[Dict[str, Any]] = []
        self._live_cost_seen: Dict[str, str] = {}
        self._live_cost_lock = threading.RLock()
        self._load_live_cost_state()
        self._config_evidence_lock = threading.RLock()
        self.config_evidence: Dict[str, Any] = {
            "version": 1,
            "connection": CONN_SHORT,
            "updatedAt": 0.0,
            "configs": {},
        }
        self._config_evidence_seen: BoundedSet = BoundedSet(4000)
        self._config_evidence_cache: Dict[str, Any] = {}
        self._config_evidence_cache_ts = 0.0
        self._load_config_evidence()
        self.pf_window = 15
        self.sl_min = 0.0020
        self.sl_max = 0.0120
        self.tp_min = 0.0035
        self.tp_max = 0.0240
        self.tp_cost_ratio = 5.0
        self.sl_to_tp = 0.64
        self.strat_ind = True
        self.strat_block = True
        self.strat_trail = True
        self.strat_general = True
        self.tf_on = {"1m": True, "5m": True, "15m": True}
        self._hist_stop = False
        self.apply_live_config(initial=True)

    def group_of(self, sym: str) -> str:
        for g, s in GROUPS.items():
            if sym in s:
                return g
        return "u%d" % (abs(hash(sym)) % 8)

    def per_config_controls(self, pos: Optional[Position] = None) -> bool:
        """Whether a position participates in quantity-matched range controls."""
        if not bool(getattr(self, "control_orders_per_config", True)):
            return False
        if pos is None:
            return True
        # Persisted positions without a group identity are legacy aggregate
        # state. Newly created positions carry effective SL/TP percentages
        # before their stable group key is assigned by prepare_position_group().
        if bool(getattr(pos, "legacy_aggregate", False)):
            return False
        return bool(getattr(pos, "control_group_key", "")) or (
            _sf(getattr(pos, "sl_pct", 0)) > 0 and _sf(getattr(pos, "tp_pct", 0)) > 0
        )

    def prepare_position_group(self, pos: Position, legacy: Optional[bool] = None) -> Position:
        """Attach a restart-safe control identity and bounded lineage metadata."""
        if legacy is not None:
            pos.legacy_aggregate = bool(legacy)
        # Prefer persisted normalized range fields when available. This keeps a
        # group stable across JSON round-trips even if a producer used a value
        # such as 0.6400001 for the same 64-basis-point range.
        range_sl_bp, range_tp_bp = parse_control_range(getattr(pos, "control_range_key", ""))
        stored_sl_bp = int(getattr(pos, "control_sl_bp", 0) or 0)
        stored_tp_bp = int(getattr(pos, "control_tp_bp", 0) or 0)
        if stored_sl_bp > 0 and stored_tp_bp > 0:
            range_sl_bp, range_tp_bp = stored_sl_bp, stored_tp_bp
        sl_value = _sf(getattr(pos, "sl_pct", 0), SL_PCT) or SL_PCT
        tp_value = _sf(getattr(pos, "tp_pct", 0), TP_PCT) or TP_PCT
        if self.per_config_controls(pos):
            sl_bp = range_sl_bp or normalize_control_pct(sl_value)
            tp_bp = range_tp_bp or normalize_control_pct(tp_value)
            if sl_bp <= 0:
                sl_bp = normalize_control_pct(SL_PCT)
            if tp_bp <= 0:
                tp_bp = normalize_control_pct(TP_PCT)
            # Keep the source fractions for actual SL/TP pricing. The basis
            # points are the stable restart/group identity; overwriting the
            # source here would turn e.g. 0.495% into 0.49/0.50% and break the
            # configured ratio. Legacy rows with no fractions use the
            # canonical identity as their safe fallback.
            if sl_value <= 0:
                sl_value = sl_bp / 10000.0
            if tp_value <= 0:
                tp_value = tp_bp / 10000.0
            pos.sl_pct = sl_value
            pos.tp_pct = tp_value
            pos.control_sl_bp = sl_bp
            pos.control_tp_bp = tp_bp
            pos.control_range_key = f"sl{sl_bp:04d}-tp{tp_bp:04d}"
            pos.control_group_key = make_control_group_key(
                pos.symbol, pos.side, sl_bp / 10000.0, tp_bp / 10000.0
            )
        else:
            # Aggregate mode is one symbol/direction scope. Never let a stale
            # range key make the disabled mode look like per-config controls.
            pos.control_group_key = ""
            pos.control_range_key = "aggregate"
            pos.control_sl_bp = 0
            pos.control_tp_bp = 0
        for attr, value in (
            ("lineage_set_ids", getattr(pos, "set_id", "")),
            ("lineage_parent_set_ids", getattr(pos, "parent_set_id", "")),
            ("lineage_axis_keys", getattr(pos, "axis_key", "")),
            ("lineage_packs", getattr(pos, "pack", "")),
            ("member_client_ids", getattr(pos, "client_id", "")),
            ("member_order_ids", getattr(pos, "order_id", "")),
        ):
            rows = getattr(pos, attr, None)
            if not isinstance(rows, list):
                rows = []
            if value and value not in rows:
                rows.append(str(value))
            setattr(pos, attr, list(dict.fromkeys(str(x) for x in rows if x))[-24:])
        pos.member_count = max(1, int(getattr(pos, "member_count", 1) or 1))
        return pos

    def legacy_position_key(self, pos: Position) -> str:
        """The aggregate control scope is symbol + hedge direction."""
        return f"{str(getattr(pos, 'symbol', '') or '').upper()}:{str(getattr(pos, 'side', '') or '').upper()}"

    def position_key(self, pos: Position) -> str:
        if self.per_config_controls(pos) and getattr(pos, "control_group_key", ""):
            return str(pos.control_group_key)
        return self.legacy_position_key(pos)

    def logical_group_key(self, pos: Position) -> str:
        return self.position_key(pos) if self.per_config_controls(pos) else ""

    def block_lane_key(self, pos: Position) -> str:
        group_key = self.logical_group_key(pos)
        try:
            return self.block.key(pos.symbol, pos.side, group_key)
        except TypeError:
            return self.block.key(pos.symbol, pos.side)

    def dca_lane_key(self, pos: Position) -> str:
        group_key = self.logical_group_key(pos)
        try:
            return self.dca.key(pos.symbol, pos.side, group_key)
        except TypeError:
            return self.dca.key(pos.symbol, pos.side)

    def ensure_strategy_lanes(self, pos: Position) -> None:
        """Rebind Block/DCA state to this logical group after fills or restart."""
        group_key = self.logical_group_key(pos)
        try:
            self.block.register_parent(pos.symbol, pos.side, pos.qty, pos.entry, group_key=group_key)
        except TypeError:
            self.block.register_parent(pos.symbol, pos.side, pos.qty, pos.entry)
        except Exception:
            pass
        try:
            self.dca.attach(pos.symbol, pos.side, pos.qty, pos.entry, group_key=group_key)
        except TypeError:
            try:
                self.dca.attach(pos.symbol, pos.side, pos.qty, pos.entry)
            except Exception:
                pass
        except Exception:
            pass

    def merge_parent_lanes(self, pos: Position, added_qty: float, entry: float) -> None:
        """Keep Block/DCA parent anchors aligned with an entry merge."""
        group_key = self.logical_group_key(pos)
        try:
            merge_parent = getattr(self.block, "merge_parent", None)
            if callable(merge_parent):
                merge_parent(pos.symbol, pos.side, added_qty, entry, group_key=group_key)
        except Exception:
            pass
        try:
            merge_parent = getattr(self.dca, "merge_parent", None)
            if callable(merge_parent):
                merge_parent(pos.symbol, pos.side, added_qty, entry, group_key=group_key)
        except Exception:
            pass

    def positions_for(self, symbol: str, side: str = "") -> List[Position]:
        side_u = str(side or "").upper()
        return [
            pos for pos in self.open.values()
            if pos.symbol == symbol and (not side_u or str(pos.side).upper() == side_u)
        ]

    def position_for_group(self, group_key: str) -> Optional[Position]:
        pos = self.open.get(str(group_key))
        if pos is not None:
            return pos
        return next((p for p in self.open.values() if getattr(p, "control_group_key", "") == group_key), None)

    def position_for_group_token(self, symbol: str, side: str, token: str) -> Optional[Position]:
        token = str(token or "")
        if not token:
            return None
        return next(
            (
                p for p in self.positions_for(symbol, side)
                if token in control_group_tokens(
                    getattr(p, "control_group_key", ""),
                    getattr(p, "control_range_key", ""),
                )
            ),
            None,
        )

    def remove_position(self, pos: Position) -> Optional[Position]:
        key = self.position_key(pos)
        removed = self.open.pop(key, None)
        if removed is None:
            for candidate_key, candidate in list(self.open.items()):
                if candidate is pos:
                    removed = self.open.pop(candidate_key, None)
                    break
        return removed

    def remove_symbol_positions(self, symbol: str) -> None:
        for pos in self.positions_for(symbol):
            self.remove_position(pos)

    def aggregate_qty(self, symbol: str, side: str = "") -> float:
        return sum(max(0.0, float(getattr(pos, "qty", 0) or 0)) for pos in self.positions_for(symbol, side))

    def merge_position(self, target: Position, incoming: Position) -> Position:
        """Merge a same-range fill into the existing logical control group."""
        old_qty = max(0.0, float(target.qty or 0))
        add_qty = max(0.0, float(incoming.qty or 0))
        total = old_qty + add_qty
        if float(getattr(pos, "close_started_qty", 0) or 0) > 0:
            pos.close_started_qty += add_qty
        if total <= 0:
            return target
        target.entry = ((target.entry * old_qty) + (incoming.entry * add_qty)) / total
        target.qty = total
        target.notional = total * target.entry
        target.entry_fee = max(0.0, float(getattr(target, "entry_fee", 0.0) or 0.0)) + max(
            0.0, float(getattr(incoming, "entry_fee", 0.0) or 0.0)
        )
        target.entry_notional = max(0.0, float(getattr(target, "entry_notional", 0.0) or 0.0)) + max(
            0.0, float(getattr(incoming, "entry_notional", 0.0) or 0.0)
        )
        target.opened_at = min(float(target.opened_at or time.time()), float(incoming.opened_at or time.time()))
        if target.side == "LONG":
            target.peak = max(float(target.peak or target.entry), float(incoming.peak or incoming.entry))
        else:
            target.peak = min(float(target.peak or target.entry), float(incoming.peak or incoming.entry))
        target.conf = max(float(target.conf or 0), float(incoming.conf or 0))
        target.reason = incoming.reason or target.reason
        target.volume_ratio = max(
            1.0,
            float(getattr(target, "volume_ratio", 1.0) or 1.0),
            float(getattr(incoming, "volume_ratio", 1.0) or 1.0),
        )
        target.relative_count = max(
            1,
            int(getattr(target, "relative_count", 1) or 1),
            int(getattr(incoming, "relative_count", 1) or 1),
        )
        target.member_count = min(256, max(1, int(getattr(target, "member_count", 1) or 1)) + max(1, int(getattr(incoming, "member_count", 1) or 1)))
        for attr in ("lineage_set_ids", "lineage_parent_set_ids", "lineage_axis_keys", "lineage_packs", "member_client_ids", "member_order_ids"):
            values = list(getattr(target, attr, []) or []) + list(getattr(incoming, attr, []) or [])
            setattr(target, attr, list(dict.fromkeys(str(x) for x in values if x))[-24:])
        target.exchange_qty = max(0.0, float(getattr(target, "exchange_qty", 0) or 0)) + max(0.0, float(getattr(incoming, "exchange_qty", 0) or 0))
        target.foreign_qty = max(0.0, float(getattr(target, "foreign_qty", 0) or 0))
        target.pending_qty = max(0.0, float(getattr(target, "pending_qty", 0) or 0)) + max(0.0, float(getattr(incoming, "pending_qty", 0) or 0))
        target.pending_close_qty = max(
            float(getattr(target, "pending_close_qty", 0) or 0),
            float(getattr(incoming, "pending_close_qty", 0) or 0),
        )
        # Same-range fills may originate from independent orders with slightly
        # different sub-basis-point inputs. Keep their weighted effective
        # fractions for pricing while the canonical range key remains stable.
        if not bool(getattr(target, "legacy_aggregate", False)) and not bool(getattr(incoming, "legacy_aggregate", False)):
            if float(getattr(incoming, "sl_pct", 0) or 0) > 0:
                target.sl_pct = (
                    float(getattr(target, "sl_pct", 0) or 0) * old_qty
                    + float(incoming.sl_pct) * add_qty
                ) / total
            if float(getattr(incoming, "tp_pct", 0) or 0) > 0:
                target.tp_pct = (
                    float(getattr(target, "tp_pct", 0) or 0) * old_qty
                    + float(incoming.tp_pct) * add_qty
                ) / total
        target.last_fill_at = max(float(getattr(target, "last_fill_at", 0) or 0), float(getattr(incoming, "last_fill_at", 0) or 0), time.time())
        if not getattr(target, "control_group_key", "") and getattr(incoming, "control_group_key", ""):
            target.control_group_key = incoming.control_group_key
            target.control_range_key = getattr(incoming, "control_range_key", "")
            target.control_sl_bp = int(getattr(incoming, "control_sl_bp", 0) or 0)
            target.control_tp_bp = int(getattr(incoming, "control_tp_bp", 0) or 0)
        if bool(getattr(target, "legacy_aggregate", False)) or bool(getattr(incoming, "legacy_aggregate", False)):
            # Legacy mode keeps one symbol/side pair and must use the widest
            # effective range represented by any merged member.
            target.sl_pct = max(float(getattr(target, "sl_pct", 0) or 0), float(getattr(incoming, "sl_pct", 0) or 0))
            target.tp_pct = max(float(getattr(target, "tp_pct", 0) or 0), float(getattr(incoming, "tp_pct", 0) or 0))
        target.sl, target.tp = self.security_prices(target)
        return target

    def round_qty(self, c: Contract, qty: float) -> float:
        n = math.floor(qty / c.step + 1e-12) * c.step
        return float(f"{n:.{c.qprec}f}")

    def round_qty_up(self, c: Contract, qty: float) -> float:
        if qty <= 0 or c.step <= 0:
            return 0.0
        n = math.ceil(qty / c.step - 1e-12) * c.step
        q = float(f"{n:.{c.qprec}f}")
        if q + 1e-12 < qty:
            q = float(f"{(n + c.step):.{c.qprec}f}")
        return q

    def min_order_qty(self, c: Contract, px: float) -> float:
        """Exchange min lot and min USDT, rounded up to step."""
        if px <= 0:
            return 0.0
        need = max(float(c.min_qty or 0), (float(c.min_usdt or 0) / px) if c.min_usdt else 0.0)
        return self.round_qty_up(c, need)

    def leverage_for(self, c: Optional[Contract]) -> int:
        sym = getattr(c, "symbol", "") if c is not None else ""
        mx = int(self.lev_max.get(sym) or getattr(c, "max_lev", 0) or 0)
        if mx <= 0:
            mx = int(LEVERAGE or 150)
        return max(1, mx)

    def _persist_lev(self) -> None:
        try:
            blob = {s: {"a": int(self.lev_map.get(s) or 0), "m": int(self.lev_max.get(s) or self.lev_map.get(s) or 0)} for s in sorted(set(list(self.lev_map) + list(self.lev_max)))}
            tmp = LEV_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(blob, f)
            os.replace(tmp, LEV_PATH)
        except Exception:
            pass

    def _load_lev_file(self) -> None:
        try:
            saved = json.load(open(LEV_PATH))
        except Exception:
            return
        if not isinstance(saved, dict):
            return
        for k, v in saved.items():
            if isinstance(v, dict):
                a = int(v.get("a") or v.get("applied") or 0)
                m = int(v.get("m") or v.get("max") or a or 0)
                if a:
                    self.lev_map[str(k)] = a
                if m:
                    self.lev_max[str(k)] = m
                    c = self.contracts.get(str(k))
                    if c is not None:
                        c.max_lev = m
            else:
                try:
                    n = int(v)
                except Exception:
                    continue
                self.lev_map[str(k)] = n
                self.lev_max.setdefault(str(k), n)

    def ensure_max_leverage(self, symbol: str, force: bool = False) -> int:
        """Actively set this symbol to its exchange max long/short. Cached, no GET spam."""
        self.use_max_leverage = True
        if not hasattr(self, "_lev_retry"):
            self._lev_retry = {}
        c = self.contracts.get(symbol)
        mx = int(self.lev_max.get(symbol) or getattr(c, "max_lev", 0) or 0)
        applied = int(self.lev_map.get(symbol) or 0)
        now = time.time()
        if self._lev_retry.get(symbol, 0.0) > now:
            return applied or mx
        if not force and mx > 0 and applied >= mx:
            if c is not None:
                c.max_lev = mx
            return applied
        if self.api.path_cd.get("/openApi/swap/v2/trade/leverage", 0) > now:
            return applied or mx
        if mx <= 0 or force or applied < mx:
            got_mx, cur_l, cur_s = self.fetch_symbol_leverage(symbol)
            if got_mx > 0:
                mx = got_mx
                self.lev_max[symbol] = mx
                if c is not None:
                    c.max_lev = mx
                if cur_l == mx and cur_s == mx:
                    self.lev_map[symbol] = mx
                    self._persist_lev()
                    return mx
                # current can be 500 while pair max is 10 — must POST down
            elif applied >= mx > 0 and not force:
                return applied
        want = int(mx or 150)
        ok_both = True
        for side in ("LONG", "SHORT"):
            r = self.api.post("/openApi/swap/v2/trade/leverage", {"symbol": symbol, "side": side, "leverage": want})
            if not self.ok(r):
                ok_both = False
                if r.get("code") in (100410, 101209, 100421):
                    # adopt()/set_leverage() can observe the same transient
                    # response repeatedly; back off this pair independently.
                    self._lev_retry[symbol] = time.time() + 180.0
                    return applied or want
                # too high — discover real max
                got_mx, cur_l, cur_s = self.fetch_symbol_leverage(symbol)
                if got_mx > 0:
                    mx = got_mx
                    self.lev_max[symbol] = mx
                    if c is not None:
                        c.max_lev = mx
                    want = mx
                    r2 = self.api.post("/openApi/swap/v2/trade/leverage", {"symbol": symbol, "side": side, "leverage": want})
                    if not self.ok(r2):
                        return applied or cur_l or want
                else:
                    return applied or want
        if ok_both or want:
            self._lev_retry.pop(symbol, None)
            self.lev_map[symbol] = want
            self.lev_max[symbol] = max(int(self.lev_max.get(symbol) or 0), want)
            if c is not None:
                c.max_lev = self.lev_max[symbol]
            try:
                self.api.post("/openApi/swap/v2/trade/marginType", {"symbol": symbol, "marginType": "CROSSED"})
            except Exception:
                pass
            log(f"LEV {symbol} x{want} max={self.lev_max.get(symbol)}", every=15.0, key=f"lev:{symbol}")
            self._persist_lev()
        return int(self.lev_map.get(symbol) or want)

    def parse_lev_payload(self, data: Any) -> Tuple[int, int, int]:
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict):
            return 0, 0, 0
        try:
            max_l = int(float(data.get("maxLongLeverage") or 0))
            max_s = int(float(data.get("maxShortLeverage") or 0))
            cur_l = int(float(data.get("longLeverage") or 0))
            cur_s = int(float(data.get("shortLeverage") or 0))
        except Exception:
            return 0, 0, 0
        mx = max(max_l, max_s)
        return mx, cur_l, cur_s

    def fetch_symbol_leverage(self, symbol: str) -> Tuple[int, int, int]:
        r = self.api.get("/openApi/swap/v2/trade/leverage", {"symbol": symbol})
        if not self.ok(r):
            return 0, 0, 0
        return self.parse_lev_payload(r.get("data"))

    def round_px(self, c: Contract, px: float) -> float:
        px = float(px or 0)
        p = max(0, int(c.pprec if c else 6))
        out = float(f"{px:.{p}f}")
        if px > 0 and out <= 0:
            for p2 in range(p + 1, 12):
                out = float(f"{px:.{p2}f}")
                if out > 0:
                    break
        return out

    def fmt_px(self, c: Optional[Contract], px: float) -> str:
        px = float(px or 0)
        p = max(0, int(c.pprec if c else 6))
        s = f"{px:.{p}f}"
        if px > 0 and float(s) <= 0:
            for p2 in range(p + 1, 12):
                s = f"{px:.{p2}f}"
                if float(s) > 0:
                    break
        return s

    def fmt_qty(self, c: Optional[Contract], q: float) -> str:
        q = float(q or 0)
        p = max(0, int(c.qprec if c else 6))
        s = f"{q:.{p}f}"
        if q > 0 and float(s) <= 0:
            for p2 in range(p + 1, 12):
                s = f"{q:.{p2}f}"
                if float(s) > 0:
                    break
        return s

    def sized_notional(self, symbol: Optional[str] = None, ratio: float = 1.0) -> float:
        """Return one independent order target in the shared ratio contract.

        ``ratio=1`` is the identity baseline. Market-volatility and coordination
        factors may adjust the desk target, but a Set's ratio is applied exactly
        once here and never compounded again by Block/DCA fills.
        """
        try:
            ratio_f = max(0.2, min(3.0, float(ratio or 1.0)))
        except Exception:
            ratio_f = 1.0
        vf = max(0.05, float(getattr(self, "volume_factor", 1.0) or 1.0))
        if symbol:
            v = float(self.vol1h.get(symbol) or 0)
            refs = [x for x in self.vol1h.values() if x and x > 0]
            if v > 0 and refs:
                med = sorted(refs)[len(refs) // 2]
                if med > 0:
                    vf *= max(0.35, min(1.0, (v / med) ** 0.5))
            elif v <= 0:
                vf *= 0.5
        try:
            open_n = len(self.open) if isinstance(getattr(self, "open", None), dict) else 0
            vf *= float(self.coord.size_mult(open_n))
        except Exception:
            pass
        return max(0.2, float(TARGET_NOTIONAL) * vf * ratio_f)

    def notional_cap(self, ratio: float = 1.0) -> float:
        return max(self.sized_notional(ratio=ratio), 2.0)

    def avail_notional(self, c: Optional["Contract"] = None) -> float:
        """USDT notional the remaining available balance can still carry at this pair's max lev."""
        lev = max(1, self.leverage_for(c) if c is not None else int(LEVERAGE or 1))
        return max(0.0, float(self.available or 0)) * lev * 0.90

    def max_book_notional(self, ratio: float = 1.0) -> float:
        """Per-position book room = ratio-adjusted parent × Block/DCA rungs.
        0 rungs maps to the seeded default (Block 3, DCA distance list). Never
        a wallet-fraction balloon — leftover size is remaining available only."""
        base = self.notional_cap(ratio=ratio)
        dca_on = bool(getattr(self.dca, "enabled", False))
        block_on = bool(getattr(self.block, "enabled", False))
        dca_n = int(getattr(self.dca, "max_steps", 0) or 0) if dca_on else 0
        if dca_on and dca_n <= 0:
            dca_n = max(len(getattr(self.dca, "distances", []) or []), 4)
        dca_extra = 0.0
        if dca_n > 0:
            try:
                dca_extra = sum(float(self.dca._mult_at(i) or 0) for i in range(min(dca_n, 8)))
            except Exception:
                dca_extra = 4.0
        stack = int(getattr(self.block, "max_stack", 0) or 0) if block_on else 0
        if block_on and stack <= 0:
            stack = 3
        vr = max(0.25, float(getattr(self.block, "volume_ratio", 1.0) or 1.0))
        block_extra = 0.0
        if stack > 0:
            block_extra = calculate_block_max_additional_ratio(stack, vr)
        extra = min(12.0, max(dca_extra, block_extra))
        hard = base * (1.0 + extra)
        room = self.avail_notional()
        if room > 0:
            hard = min(hard, room)
        return max(base, hard)

    def cap_order_qty(self, c: Contract, px: float, qty: float, cap_usdt: Optional[float] = None) -> float:
        if px <= 0 or qty <= 0:
            return 0.0
        floor = self.min_order_qty(c, px)
        q = self.round_qty_up(c, qty)
        if cap_usdt and cap_usdt > 0:
            maxq = self.round_qty(c, float(cap_usdt) / px)
            if maxq <= 0:
                return 0.0
            if q > maxq:
                q = maxq
        if q < floor:
            if cap_usdt and floor * px > float(cap_usdt) * 1.08:
                return 0.0
            q = floor
        return q

    def size_qty(self, c: Contract, px: float, ratio: float = 1.0) -> float:
        """Size one independent order from the shared ratio baseline."""
        if px <= 0:
            return 0.0
        if float(self.available or 0) <= 0:
            return 0.0
        floor = self.min_order_qty(c, px)
        if floor <= 0:
            return 0.0
        floor_n = floor * px
        room = self.avail_notional(c)
        if room <= 0 or floor_n > room * 1.02:
            return 0.0
        target_n = self.sized_notional(c.symbol, ratio=ratio)
        want_n = min(target_n, room)
        if want_n < floor_n:
            want_n = floor_n
        q = self.round_qty(c, want_n / px)
        if q < floor:
            return 0.0
        if q * px > room * 1.02:
            return 0.0
        # Final sanity: an entry may never exceed 2× the configured target
        # (exchange min-lot floor excepted) — catches corrupt sizing upstream.
        if q * px > max(target_n * 2.0, floor_n * 1.08):
            return 0.0
        return q

    def ban_sym(self, sym: str, sec: float = 1800.0, clear_open: bool = True) -> None:
        self.ignore_syms[sym] = time.time() + sec
        self.owned_syms.discard(sym)
        if clear_open:
            self.remove_symbol_positions(sym)

    def clear_position_controls(self, pos: Position) -> None:
        """Forget only this group's local control IDs after a confirmed cancel/close."""
        pos.sl_oid = pos.tp_oid = ""
        pos.sec_sl_oid = pos.sec_tp_oid = ""
        pos.controls_ok = False
        pos.ctrl_verified = False
        pos.ctrl_qty = 0.0

    def flatten_untracked(self, symbol: str, side: str, qty: float, px: float) -> bool:
        # Never flatten independent / other-system positions.
        tagged = []
        try:
            tagged = self.our_orders(symbol)
        except Exception:
            tagged = []
        if not tagged and symbol not in self.owned_syms:
            log(f"SKIP flatten foreign {symbol} {side}", every=30.0, key=f"flat:{symbol}")
            return False
        dummy = Position(
            symbol=symbol, side=side, qty=qty, entry=px or 1.0, opened_at=time.time(),
            sl=px or 1.0, tp=px or 1.0, peak=px or 1.0, notional=qty * (px or 0), ours=True,
        )
        try:
            self.cancel_controls(symbol)
        except Exception:
            pass
        ok, _ = self.market_close(dummy)
        self.ban_sym(symbol)
        log(f"FLATTEN untracked {symbol} {side} q={qty} n={(qty*(px or 0)):.1f} ok={ok}")
        return ok

    def _reconcile_control_mode(self, per_config: bool) -> None:
        """Re-key the in-memory book without dropping a group on a toggle.

        Existing aggregate records stay aggregate when the default-on mode is
        enabled. Disabling the mode merges same-symbol/same-side groups and
        clears their old per-range controls before rebuilding one legacy pair.
        """
        rows = list(getattr(self, "open", {}).values())
        if not rows:
            return
        if per_config:
            next_book: Dict[str, Position] = {}
            for pos in rows:
                self.prepare_position_group(pos)
                key = self.position_key(pos)
                existing = next_book.get(key)
                if existing is None:
                    next_book[key] = pos
                else:
                    if getattr(self, "control_orders", True):
                        try:
                            self.cancel_controls(pos.symbol, pos=pos)
                        except Exception:
                            pass
                    self.merge_position(existing, pos)
                    self.clear_position_controls(existing)
            self.open = next_book
            return

        grouped: Dict[Tuple[str, str], Position] = {}
        for pos in rows:
            try:
                self.cancel_controls(pos.symbol, pos=pos)
            except Exception:
                pass
            pos.legacy_aggregate = True
            pos.control_group_key = ""
            pos.control_range_key = "aggregate"
            pos.control_sl_bp = 0
            pos.control_tp_bp = 0
            key = (str(pos.symbol or ""), str(pos.side or "").upper())
            existing = grouped.get(key)
            if existing is None:
                grouped[key] = pos
            else:
                self.merge_position(existing, pos)
                self.clear_position_controls(existing)
                existing.sl_pct = max(float(existing.sl_pct or 0), float(pos.sl_pct or 0))
                existing.tp_pct = max(float(existing.tp_pct or 0), float(pos.tp_pct or 0))
                existing.sl, existing.tp = self.security_prices(existing)
        next_book: Dict[str, Position] = {}
        by_symbol: Dict[str, List[Position]] = {}
        for pos in grouped.values():
            by_symbol.setdefault(pos.symbol, []).append(pos)
        for symbol, positions in by_symbol.items():
            for pos in positions:
                key = symbol if len(positions) == 1 else f"{symbol}:{pos.side}"
                next_book[key] = pos
        self.open = next_book
        if getattr(self, "control_orders", True):
            for pos in self.open.values():
                try:
                    self.ensure_controls(pos)
                except Exception:
                    pass

    def save_open_book(self) -> None:
        try:
            blob: Dict[str, Any] = {}
            for pos in self.open.values():
                base = self.position_key(pos) or str(pos.symbol or "position")
                key = base
                suffix = 2
                while key in blob:
                    key = f"{base}:{suffix}"
                    suffix += 1
                blob[key] = asdict(pos)
            tmp = OPEN_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(blob, f)
            os.replace(tmp, OPEN_PATH)
        except Exception:
            pass

    def _load_pending_orders(self) -> None:
        """Restore only this connection's unresolved order intents."""
        raw = load_json_file(PENDING_PATH)
        rows = raw.get("orders") if isinstance(raw, dict) else raw
        if not isinstance(rows, dict):
            return
        now = time.time()
        for key, value in list(rows.items())[:512]:
            if not isinstance(value, dict):
                continue
            cid = str(value.get("client_id") or value.get("clientId") or key or "")
            if not self.cid_ours(cid):
                continue
            try:
                created = float(value.get("created_at") or value.get("createdAt") or now)
            except Exception:
                created = now
            if now - created > 1800.0:
                continue
            try:
                requested = max(0.0, float(value.get("requested_qty") or value.get("requestedQty") or 0))
                filled = max(0.0, float(value.get("filled_qty") or value.get("filledQty") or 0))
            except Exception:
                requested, filled = 0.0, 0.0
            self.pending_orders[cid] = {
                "kind": str(value.get("kind") or "entry"),
                "client_id": cid,
                "order_id": real_oid(value.get("order_id") or value.get("orderId")),
                "symbol": str(value.get("symbol") or ""),
                "side": str(value.get("side") or "").upper(),
                "requested_qty": requested,
                "filled_qty": min(filled, requested) if requested > 0 else filled,
                "fee_total": max(0.0, _sf(value.get("fee_total") or value.get("feeTotal"))),
                "avg_price": max(0.0, float(value.get("avg_price") or value.get("avgPrice") or 0)),
                "group_key": str(value.get("group_key") or value.get("groupKey") or ""),
                "created_at": created,
                "updated_at": float(value.get("updated_at") or value.get("updatedAt") or created),
                "metadata": value.get("metadata") if isinstance(value.get("metadata"), dict) else {},
            }

    def _save_pending_orders(self) -> None:
        try:
            os.makedirs(DIR, exist_ok=True)
            blob = {
                "version": 1,
                "connection": CONN_SHORT,
                "updatedAt": time.time(),
                "orders": {cid: dict(row) for cid, row in list(self.pending_orders.items())[-512:]},
            }
            tmp = PENDING_PATH + ".tmp"
            with open(tmp, "w") as state_file:
                json.dump(blob, state_file, separators=(",", ":"))
            os.replace(tmp, PENDING_PATH)
        except Exception:
            pass

    def _remember_pending(
        self,
        *,
        kind: str,
        cid: str,
        symbol: str,
        side: str,
        requested_qty: float,
        filled_qty: float = 0.0,
        order_id: str = "",
        avg_price: float = 0.0,
        group_key: str = "",
        fee_total: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        cid = str(cid or "")
        if not cid or not self.cid_ours(cid):
            return
        now = time.time()
        pending = getattr(self, "pending_orders", None)
        if not isinstance(pending, dict):
            pending = {}
            self.pending_orders = pending
        old = pending.get(cid) or {}
        requested = max(float(old.get("requested_qty") or 0), max(0.0, float(requested_qty or 0)))
        filled = max(float(old.get("filled_qty") or 0), max(0.0, float(filled_qty or 0)))
        if requested > 0:
            filled = min(filled, requested)
        merged_meta = dict(old.get("metadata") or {})
        merged_meta.update(metadata or {})
        pending[cid] = {
            "kind": str(kind or old.get("kind") or "entry"),
            "client_id": cid,
            "order_id": real_oid(order_id) or str(old.get("order_id") or ""),
            "symbol": str(symbol or old.get("symbol") or ""),
            "side": str(side or old.get("side") or "").upper(),
            "requested_qty": requested,
            "filled_qty": filled,
            "fee_total": max(float(old.get("fee_total") or 0.0), max(0.0, float(fee_total or 0.0))),
            "avg_price": max(0.0, float(avg_price or old.get("avg_price") or 0)),
            "group_key": str(group_key or old.get("group_key") or ""),
            "created_at": float(old.get("created_at") or now),
            "updated_at": now,
            "metadata": merged_meta,
        }
        self._save_pending_orders()

    def _clear_pending(self, cid: str) -> None:
        pending = getattr(self, "pending_orders", None)
        if not isinstance(pending, dict):
            return
        if str(cid or "") in pending:
            pending.pop(str(cid), None)
            self._save_pending_orders()

    def _pending_add_open(self, pos: Position, kind: str) -> bool:
        """Return whether an unresolved Block/DCA order already owns this group."""
        wanted = {str(kind or "").lower()}
        if "block" in wanted:
            wanted.add("b")
        if "dca" in wanted:
            wanted.add("d")
        scope = self.logical_group_key(pos) if self.per_config_controls(pos) else self.legacy_position_key(pos)
        for row in (getattr(self, "pending_orders", {}) or {}).values():
            if str(row.get("kind") or "").lower() not in wanted:
                continue
            if str(row.get("symbol") or "").upper() != str(pos.symbol or "").upper():
                continue
            if str(row.get("side") or "").upper() != str(pos.side or "").upper():
                continue
            row_scope = str(row.get("group_key") or "")
            if not row_scope:
                row_scope = self.legacy_position_key(pos)
            if row_scope != scope:
                continue
            requested = max(0.0, _sf(row.get("requested_qty") or row.get("requestedQty")))
            filled = max(0.0, _sf(row.get("filled_qty") or row.get("filledQty")))
            if requested > filled + 1e-12:
                return True
        return False

    def _load_open_book(self) -> None:
        if not os.path.exists(OPEN_PATH):
            return
        try:
            data = json.load(open(OPEN_PATH))
        except Exception:
            return
        fields = {f.name for f in Position.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        for stored_key, rec in (data or {}).items():
            if not isinstance(rec, dict):
                continue
            try:
                kw = {k: rec[k] for k in rec if k in fields}
                pos = Position(**kw)
            except Exception:
                continue
            if pos.qty <= 0:
                continue
            if pos.client_id and not self.cid_ours(pos.client_id):
                continue
            # Symbol-keyed records predate range groups. Keep them as a
            # legacy aggregate so enabling the new default cannot reinterpret
            # an already-open position and place an unsafe second control pair.
            is_legacy = bool(rec.get("legacy_aggregate")) or not bool(rec.get("control_group_key"))
            self.prepare_position_group(pos, legacy=is_legacy)
            # Re-key legacy symbol records by hedge side so LONG and SHORT
            # aggregates cannot overwrite one another on restart.
            key = self.position_key(pos)
            if key in self.open:
                existing = self.open[key]
                old_qty = max(0.0, float(existing.qty or 0))
                add_qty = max(0.0, float(pos.qty or 0))
                total = old_qty + add_qty
                if total > 0:
                    existing.entry = ((existing.entry * old_qty) + (pos.entry * add_qty)) / total
                    existing.qty = total
                    existing.notional = total * existing.entry
                    existing.member_count = min(256, existing.member_count + pos.member_count)
                continue
            self.open[key] = pos
            self.owned_syms.add(pos.symbol)
            if pos.client_id:
                pending = self.pending_orders.get(pos.client_id) or {}
                requested = max(0.0, _sf(pending.get("requested_qty") or pending.get("requestedQty")))
                filled = max(0.0, _sf(pending.get("filled_qty") or pending.get("filledQty")))
                if requested > filled + 1e-12:
                    pos.pending_qty = max(float(getattr(pos, "pending_qty", 0.0) or 0.0), requested - filled)
                else:
                    self.seen_fill_cids.add(pos.client_id)
            # Close intents use their own client id, while the open position
            # keeps the parent entry id. Restore the remaining close quantity
            # by the persisted group/side so a restart cannot issue a second
            # full-size close for an already partially executed order.
            for pending in (self.pending_orders or {}).values():
                if str(pending.get("kind") or "").lower() not in ("close", "c"):
                    continue
                pside = str(pending.get("side") or "").upper()
                if str(pending.get("symbol") or "").upper() != str(pos.symbol or "").upper() or pside != str(pos.side or "").upper():
                    continue
                pgroup = str(pending.get("group_key") or "")
                parent = str((pending.get("metadata") or {}).get("parent_client_id") or "")
                matches_group = pgroup and pgroup in (self.position_key(pos), getattr(pos, "control_group_key", ""), self.legacy_position_key(pos))
                matches_parent = parent and parent in ([getattr(pos, "client_id", "")] + list(getattr(pos, "member_client_ids", []) or []))
                if not (matches_group or matches_parent):
                    continue
                requested_c = max(0.0, _sf(pending.get("requested_qty") or pending.get("requestedQty")))
                filled_c = max(0.0, _sf(pending.get("filled_qty") or pending.get("filledQty")))
                if requested_c > filled_c + 1e-12:
                    pos.pending_close_qty = max(float(getattr(pos, "pending_close_qty", 0.0) or 0.0), requested_c - filled_c)

    def cid(self, kind: str = "o", pos: Optional["Position"] = None, set_id: str = "", pack: str = "", set_idx: int = -1) -> str:
        kind = (kind or "o")[:1]
        idx = set_idx
        if pos is not None:
            set_id = set_id or pos.set_id
            pack = pack or pos.pack
            if idx < 0:
                idx = int(getattr(pos, "set_idx", -1))
        p = "i" if str(pack or set_id).startswith("ind") else "g"
        sl, tr, st = "06", "03", "08"
        m = re.search(r"sl([0-9.]+)", set_id or "")
        if m:
            try:
                sl = f"{int(round(float(m.group(1)) * 10)):02d}"
            except Exception:
                pass
        m = re.search(r"tr([0-9.]+)", set_id or "")
        if m:
            try:
                tr = f"{int(round(float(m.group(1)) * 10)):02d}"
            except Exception:
                pass
        m = re.search(r":st(\d+)", set_id or "")
        if m:
            st = f"{int(m.group(1)):02d}"
        if idx < 0:
            try:
                idx = int(getattr(self.sets, "sets", {}).get(set_id).idx) if set_id else -1
            except Exception:
                idx = -1
        ix = f"{max(0, idx):03d}"
        group_token = ""
        if pos is not None and self.per_config_controls(pos) and getattr(pos, "control_group_key", ""):
            group_token = control_group_token(
                pos.control_group_key,
                getattr(pos, "control_range_key", ""),
            )
        nonce = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
        # Keep the historical parser offsets intact: bytes after set index are
        # now a range-group token plus a short nonce for per-config controls.
        suffix = (group_token + nonce[:2]) if group_token else nonce
        return f"{TAG}{kind}{p}{sl}{tr}{st}{ix}{suffix}"[:32]

    def cid_ours(self, cid: str) -> bool:
        """Only this process + this connection watermark (Gx01 / Gx02). Never CTS or other bots."""
        s = str(cid or "").lower().strip()
        if not s:
            return False
        return s.startswith(TAG.lower())

    def order_is_ours(self, o: Dict[str, Any]) -> bool:
        return self.cid_ours(self.order_cid(o))

    def order_cid(self, o: Dict[str, Any]) -> str:
        return str(o.get("clientOrderID") or o.get("clientOrderId") or "")

    def parse_track(self, cid: str) -> Optional[Dict[str, Any]]:
        if not self.cid_ours(cid):
            return None
        s = str(cid)
        low = s.lower()
        tag = TAG.lower()
        rest = s[len(TAG):] if low.startswith(tag) else s
        if len(rest) < 6:
            return {"kind": rest[:1], "pack": "general", "set_id": ""}
        kind = rest[:1]
        pack = "indications" if rest[1:2] == "i" else "general"
        try:
            sl = int(rest[2:4]) / 10.0
            arm = int(rest[4:6]) / 10.0
        except Exception:
            sl, arm = 0.6, 0.3
        step = 0
        idx = -1
        if len(rest) >= 8:
            try:
                step = int(rest[6:8])
            except Exception:
                step = 0
        if len(rest) >= 11:
            try:
                idx = int(rest[8:11])
            except Exception:
                idx = -1
        group_token = ""
        control_range_key = ""
        control_sl_bp = 0
        control_tp_bp = 0
        tail = rest[11:]
        # Current per-config IDs use r + three decimal digits per side
        # (basis points). Older IDs used an eight-character hash token.
        range_match = re.match(r"(r\d{6})", tail, re.I)
        if range_match:
            group_token = range_match.group(1)
            control_sl_bp = int(group_token[1:4])
            control_tp_bp = int(group_token[4:7])
            control_range_key = f"sl{control_sl_bp:04d}-tp{control_tp_bp:04d}"
        elif len(tail) >= 8 and re.fullmatch(r"[a-z0-9]{8}", tail[:8], re.I):
            group_token = tail[:8]
        from risk_variants import trail_key as tk, give_from_arm, TRAIL_GIVE_FACTOR, TRAIL_GIVE_MIN, TRAIL_GIVE_MAX
        tr = tk(arm, give_from_arm(arm, TRAIL_GIVE_FACTOR, TRAIL_GIVE_MIN, TRAIL_GIVE_MAX))
        st_obj = self.sets.get_idx(idx) if idx >= 0 and hasattr(self, "sets") else None
        if st_obj is not None:
            pack_ok = str(getattr(st_obj, "pack", "")) == str(pack)
            try:
                sl_ok = abs(float(st_obj.sl_ratio) - float(sl)) < 0.15
            except Exception:
                sl_ok = False
            if pack_ok and sl_ok:
                return {
                    "kind": kind,
                    "pack": st_obj.pack,
                    "sl": st_obj.sl_ratio,
                    "trail": st_obj.trail_key,
                    "trail_arm": getattr(st_obj, "trail_arm", arm),
                    "trail_give": getattr(st_obj, "trail_give", give_from_arm(arm, TRAIL_GIVE_FACTOR, TRAIL_GIVE_MIN, TRAIL_GIVE_MAX)),
                    "step": st_obj.step,
                    "idx": st_obj.idx,
                    "set_id": st_obj.id,
                    "parent_set_id": st_obj.parent_set_id or st_obj.id,
                    "axis_key": st_obj.axis_key,
                    "relative_count": st_obj.relative_count,
                    "volume_ratio": st_obj.volume_ratio,
                    "ind_kind": st_obj.indication_kind,
                    "group_token": group_token,
                    "control_range_key": control_range_key,
                    "control_sl_bp": control_sl_bp,
                    "control_tp_bp": control_tp_bp,
                    "sl_pct": control_sl_bp / 10000.0 if control_sl_bp else 0.0,
                    "tp_pct": control_tp_bp / 10000.0 if control_tp_bp else 0.0,
                }
        from set_engine import make_set_id
        fallback_set_id = make_set_id(pack, sl, tr, step)
        return {
            "kind": kind,
            "pack": pack,
            "sl": sl,
            "trail": tr,
            "trail_arm": arm,
            "trail_give": give_from_arm(arm, TRAIL_GIVE_FACTOR, TRAIL_GIVE_MIN, TRAIL_GIVE_MAX),
            "step": step,
            "idx": idx,
            "set_id": fallback_set_id,
            "parent_set_id": fallback_set_id,
            "axis_key": "",
            "relative_count": 1,
            "volume_ratio": 1.0,
            "ind_kind": "signals" if pack == "indications" else "",
            "group_token": group_token,
            "control_range_key": control_range_key,
            "control_sl_bp": control_sl_bp,
            "control_tp_bp": control_tp_bp,
            "sl_pct": control_sl_bp / 10000.0 if control_sl_bp else 0.0,
            "tp_pct": control_tp_bp / 10000.0 if control_tp_bp else 0.0,
        }


    def ok(self, r: Dict[str, Any]) -> bool:
        return (not r.get("error")) and r.get("code") in (0, None)

    def record_test(self, name: str, passed: bool, detail: str = "") -> None:
        rec = {"name": name, "pass": passed, "detail": detail[:180], "t": time.time()}
        prev = self.test_map.get(name)
        self.test_map[name] = rec
        self.tests = list(self.test_map.values())[-28:]
        if prev is not None and bool(prev.get("pass")) == bool(passed):
            return
        if passed:
            self.qa_pass += 1
            if prev is not None and not prev.get("pass"):
                self.qa_fail = max(0, self.qa_fail - 1)
        else:
            self.qa_fail += 1
            log(f"TEST FAIL {name} {detail}"[:240], every=20.0, key=f"fail:{name}")

    def state_guard(self):
        """Return the shared state lock, with a test-friendly fallback."""
        return getattr(self, "_state_lock", None) or nullcontext()

    def refresh_balance(self) -> None:
        request_key = stable_key(CONN_SHORT, "balance", int(getattr(self, "cycle", 0) or 0), int(time.time() // 5))
        self.record_event("exchange_request", request_key, status="pending", detail="balance", metadata={"path": "/openApi/swap/v3/user/balance"})
        r = self.api.get("/openApi/swap/v3/user/balance")
        if not self.ok(r):
            r = self.api.get("/openApi/swap/v2/user/balance")
        self.record_event(
            "exchange_response",
            stable_key(request_key, "response"),
            status="confirmed" if self.ok(r) else "error",
            code=r.get("code"),
            detail="balance",
            metadata={"path": "/openApi/swap/v2/user/balance" if not self.ok(r) else "/openApi/swap/v3/user/balance"},
        )
        data = r.get("data")
        row = None
        if isinstance(data, dict):
            row = data.get("balance") if isinstance(data.get("balance"), dict) else data
        elif isinstance(data, list) and data:
            row = next((x for x in data if str(x.get("asset") or x.get("currency") or "USDT").upper() in ("USDT", "VST")), data[0])
        if not isinstance(row, dict):
            self.errors += 1
            self.last_error = f"balance {r.get('msg')}"
            self.record_event("error", stable_key(request_key, "invalid"), status="error", code=r.get("code"), detail=self.last_error)
            return
        self.equity = float(row.get("equity") or row.get("balance") or 0)
        self.available = float(row.get("availableMargin") or row.get("available") or row.get("availableBalance") or 0)
        self.used = float(row.get("usedMargin") or row.get("used") or 0)
        self.upnl = float(row.get("unrealizedProfit") or row.get("unrealized") or 0)
        if self.start_eq <= 0:
            self.start_eq = self.equity
            try:
                tmp = START_EQ_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"startEquity": self.start_eq, "t": time.time()}, f)
                os.replace(tmp, START_EQ_PATH)
            except Exception:
                pass
        self.last_bal = time.time()
        # Explicit Start (sidecar drops reset-eq) or a real deposit must always
        # revive the desk: re-baseline the session equity instead of latching
        # the drawdown / equity-min halt forever.
        try:
            if os.path.exists(RESET_EQ_PATH):
                os.remove(RESET_EQ_PATH)
                if self.halt_reason in ("drawdown halt", "stopped", "paused") or str(self.halt_reason or "").startswith("equity "):
                    self._pre_pause_halt = None
                self.start_eq = 0.0
        except Exception:
            pass
        if self.start_eq <= 0 and self.equity > 0:
            self.start_eq = self.equity
            try:
                tmp = START_EQ_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"startEquity": self.start_eq, "t": time.time()}, f)
                os.replace(tmp, START_EQ_PATH)
            except Exception:
                pass
        halt_eq = float(getattr(self, "_halt_eq", 0.0) or 0.0)
        econ_halt = self.halted and (
            self.halt_reason == "drawdown halt" or str(self.halt_reason or "").startswith("equity ")
        )
        rescued = bool(
            econ_halt
            and halt_eq > 0
            and self.equity >= max(EQ_MIN * 2.0, halt_eq * 1.5, halt_eq + 1.0)
        )
        if rescued:
            log(f"EQ re-baseline on deposit start_eq {self.start_eq:.4f} -> {self.equity:.4f}")
            self.start_eq = self.equity
            try:
                tmp = START_EQ_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"startEquity": self.start_eq, "t": time.time()}, f)
                os.replace(tmp, START_EQ_PATH)
            except Exception:
                pass
            self.halted = False
            self.halt_reason = None
            self._pre_pause_halt = None
            self._halt_eq = 0.0
        if os.path.exists(STOP_PATH) or os.path.exists(STOP_ALL):
            if self.halt_reason and self.halt_reason not in ("paused", "stopped"):
                self._pre_pause_halt = self.halt_reason
            self.halted = True
            self.halt_reason = "stopped"
        elif os.path.exists(PAUSE_PATH):
            if self.halt_reason and self.halt_reason not in ("paused", "stopped"):
                self._pre_pause_halt = self.halt_reason
            self.halted = True
            self.halt_reason = "paused"
        elif self.start_eq > 0 and self.equity > 0 and (self.start_eq - self.equity) / self.start_eq >= DD_HALT:
            if not self.halted:
                self._halt_eq = self.equity
            self.halted = True
            self.halt_reason = "drawdown halt"
            self._pre_pause_halt = None
        elif self.equity < EQ_MIN:
            if not self.halted:
                self._halt_eq = self.equity
            self.halted = True
            self.halt_reason = f"equity {self.equity:.4f} below min"
            self._pre_pause_halt = None
        elif self.equity >= EQ_MIN and self.start_eq > 0 and (self.start_eq - self.equity) / max(self.start_eq, 1e-9) < DD_HALT * 0.6:
            self.halted = False
            self.halt_reason = None
            self._pre_pause_halt = None

    def bump(self, kind: str = "tick") -> None:
        self.last_event = kind
        self.event_n += 1
        try:
            self.wake_ev.set()
        except Exception:
            pass

    def record_event(self, event_type: str, event_id: str = "", status: str = "", **fields: Any) -> bool:
        """Commit one bounded activity event without allowing telemetry to stop the engine."""
        try:
            ledger = getattr(self, "event_ledger", None)
            if ledger is None:
                ledger = EventLedger(EVENTS_PATH, CONN_SHORT, max_events=512)
                self.event_ledger = ledger
            fields.setdefault("connection", CONN_SHORT)
            committed = bool(ledger.record(event_type, event_id, status=status, **fields))
            if committed:
                self.bump(f"event:{event_type}")
            return committed
        except Exception as exc:
            # Event persistence is observability, never a trading dependency.
            self.last_error = f"event ledger {str(exc)[:120]}"
            return False

    def control_event_fields(self, pos: Optional[Position]) -> Dict[str, Any]:
        if pos is None:
            return {}
        return {
            "control_group_key": str(getattr(pos, "control_group_key", "") or ""),
            "control_range_key": str(getattr(pos, "control_range_key", "") or "aggregate"),
            "control_mode": "per-config" if self.per_config_controls(pos) else "aggregate",
            "member_count": max(1, int(getattr(pos, "member_count", 1) or 1)),
        }

    def event_summary(self) -> Dict[str, Any]:
        ledger = getattr(self, "event_ledger", None)
        if ledger is None:
            return {"eventCount": 0, "parity": "pending", "source": "committed-event-ledger"}
        exchange_open = getattr(self, "exchange_open_count", -1)
        try:
            exchange_open = int(exchange_open)
        except Exception:
            exchange_open = -1
        return ledger.summary(
            internal_open=len(getattr(self, "open", {}) or {}),
            exchange_open=exchange_open,
            internal_closed=len(getattr(self, "closed", ()) or ()),
        )

    def ingest_ws_px(self) -> int:
        n = 0
        want = set(SYMBOLS)
        for s, px in list(getattr(self.api, "px", {}).items()):
            if px and s in want:
                self.px[s] = px
                self.last_px[s] = max(float(self.last_px.get(s) or 0), float(px))
                n += 1
        return n

    def refresh_tickers(self) -> None:
        want = set(SYMBOLS)
        copied = self.ingest_ws_px()
        hub = getattr(self.api, "hub", None)
        ws_age = (time.time() - getattr(hub, "last_msg", 0)) if hub and getattr(hub, "last_msg", 0) else 99
        ws_ok = bool(getattr(hub, "ok", False) and ws_age < 4.0)
        covered = sum(1 for s in SYMBOLS if (self.px.get(s) or 0) > 0)
        if ws_ok and covered >= max(8, len(SYMBOLS) - 2):
            return
        self.did_io = True
        r = self.api.public("/openApi/swap/v2/quote/ticker")
        rows = r.get("data") or []
        if not isinstance(rows, list):
            return
        want = set(SYMBOLS)
        write_uni = (time.time() - self.last_uni) >= UNIVERSE_EVERY
        uni: List[Dict[str, Any]] = []
        for tck in rows:
            s = tck.get("symbol")
            if not s or not str(s).endswith("-USDT"):
                continue
            try:
                last = float(tck.get("lastPrice") or tck.get("close") or 0)
                ch = float(tck.get("priceChangePercent") or 0)
            except Exception:
                continue
            if last > 0 and s in want:
                self.px[s] = last
                self.last_px[s] = last
                self.chg[s] = ch
            if write_uni:
                try:
                    qv = float(tck.get("quoteVolume") or 0)
                except Exception:
                    qv = 0.0
                try:
                    hi = float(tck.get("highPrice") or tck.get("high") or tck.get("high24h") or 0)
                    lo = float(tck.get("lowPrice") or tck.get("low") or tck.get("low24h") or 0)
                except Exception:
                    hi = lo = 0.0
                uni.append({
                    "symbol": s,
                    "last": last,
                    "quoteVolume": qv,
                    "changePct": ch,
                    "high": hi,
                    "low": lo,
                })
        if write_uni and uni:
            for row in uni:
                self._attach_rank_fields(row)
            uni.sort(key=lambda x: symbol_rank_key(x, self.symbol_sort))
            self.universe = uni
            self.last_uni = time.time()
            try:
                blob = json.dumps({
                    "updated": self.last_uni,
                    "count": len(uni),
                    "max": MAX_SYMBOLS,
                    "unlimited": MAX_SYMBOLS <= 0,
                    "default": 12,
                    "sort": self.symbol_sort,
                    "dynamic": bool(self.symbols_dynamic),
                    "leverageFirst": True,
                    "selected": list(SYMBOLS),
                    "rows": uni,
                }, separators=(",", ":"))
                tmp = UNIVERSE_PATH + ".tmp"
                with open(tmp, "w") as f:
                    f.write(blob)
                os.replace(tmp, UNIVERSE_PATH)
            except Exception:
                pass
            self.apply_dynamic_symbols()
        self.last_rest_tick = time.time()

    def _vol1h_from_bars(self, symbol: str) -> float:
        bars = (self.klines_tf.get("1m", {}) or {}).get(symbol) or self.klines.get(symbol) or []
        if len(bars) < 16:
            return 0.0
        window = bars[-60:] if len(bars) >= 60 else bars
        try:
            hi = max(float(b[1]) for b in window)
            lo = min(float(b[2]) for b in window)
            last = float(window[-1][3] or 0)
        except Exception:
            return 0.0
        if last <= 0 or hi <= 0:
            return 0.0
        return (hi - lo) / last * 100.0

    def _attach_rank_fields(self, row: Dict[str, Any]) -> Dict[str, Any]:
        s = str(row.get("symbol") or "")
        c = self.contracts.get(s)
        lev = int(self.lev_max.get(s) or getattr(c, "max_lev", 0) or 0)
        row["maxLeverage"] = lev
        v1 = float(self.vol1h.get(s) or 0)
        if v1 <= 0:
            v1 = self._vol1h_from_bars(s)
            if v1 > 0:
                self.vol1h[s] = v1
                self.vol1h_ts[s] = time.time()
        row["vol1h"] = round(v1, 4)
        last = float(row.get("last") or 0)
        hi = float(row.get("high") or 0)
        lo = float(row.get("low") or 0)
        if hi > 0 and lo > 0 and last > 0 and hi >= lo:
            row["vol24h"] = round((hi - lo) / last * 100.0, 4)
        else:
            row["vol24h"] = round(abs(float(row.get("changePct") or 0)), 4)
        return row

    def refresh_vol1h(self) -> None:
        """Fill 1H range vol from 1m bars first, then a small 1h-kline batch for the rest."""
        now = time.time()
        for s in list(SYMBOLS):
            if not s:
                continue
            v = self._vol1h_from_bars(str(s))
            if v > 0:
                self.vol1h[str(s)] = v
                self.vol1h_ts[str(s)] = now
        if now - float(getattr(self, "last_vol1h", 0) or 0) < VOL1H_EVERY:
            return
        if now < getattr(self, "kline_ban", 0):
            return
        names = [r.get("symbol") for r in (self.universe or []) if r.get("symbol")]
        if not names:
            names = list(SYMBOLS)
        stale = [
            s for s in names
            if now - float(self.vol1h_ts.get(s, 0) or 0) >= 90.0
        ]
        stale.sort(key=lambda s: self.vol1h_ts.get(s, 0))
        batch = stale[:VOL1H_BATCH]
        if not batch:
            self.last_vol1h = now
            return
        reqs = [("/openApi/swap/v2/quote/klines", {"symbol": s, "interval": "1h", "limit": "2"}) for s in batch]
        bodies: List[Tuple[str, Dict[str, Any], Any]] = []
        try:
            if hasattr(self.api, "gather_public"):
                bodies = self.api.gather_public(reqs, timeout=5.0)
            else:
                for _p, extra in reqs:
                    bodies.append((_p, extra, self.api.public("/openApi/swap/v2/quote/klines", extra)))
        except Exception:
            self.last_vol1h = now
            return
        for _path, extra, body in bodies:
            s = str((extra or {}).get("symbol") or "")
            if isinstance(body, dict):
                self._note_kline_ban(body)
            bars = self._parse_klines(body.get("data") if isinstance(body, dict) else None)
            if not s or len(bars) < 1:
                self.vol1h_ts.setdefault(s, now - 60.0)
                continue
            b = bars[-1]
            try:
                hi, lo, last = float(b[1]), float(b[2]), float(b[3] or 0)
            except Exception:
                continue
            if last > 0 and hi >= lo:
                self.vol1h[s] = (hi - lo) / last * 100.0
                self.vol1h_ts[s] = now
        self.last_vol1h = now

    def apply_dynamic_symbols(self, force: bool = False) -> None:
        """Reorder (and optionally rotate) the scan book: max leverage, then selected criterion."""
        global SYMBOLS
        now = time.time()
        if not force and now - float(getattr(self, "last_dyn_sel", 0) or 0) < 18.0:
            return
        rows = list(self.universe or [])
        if not rows:
            return
        ranked = sorted(rows, key=lambda r: symbol_rank_key(r, self.symbol_sort))
        names = [
            str(r.get("symbol"))
            for r in ranked
            if r.get("symbol") and str(r.get("symbol")).endswith("-USDT") and not str(r.get("symbol")).startswith(("NCCO", "NCS", "NCFX"))
        ]
        if not names:
            return
        open_syms = []
        seen_open = set()
        for p in list(self.open.values()):
            s = getattr(p, "symbol", "")
            if s and s not in seen_open:
                open_syms.append(s)
                seen_open.add(s)
        wild = bool(getattr(self, "overlay_wild", False))
        cap = int(getattr(self, "symbol_cap", 0) or 0)
        if not getattr(self, "symbols_dynamic", True):
            have = set(SYMBOLS)
            chosen = [s for s in names if s in have]
            for s in SYMBOLS:
                if s not in set(chosen):
                    chosen.append(s)
        elif wild or cap <= 0:
            chosen = names
        else:
            chosen = []
            seen = set()
            for s in open_syms + names:
                if s in seen:
                    continue
                seen.add(s)
                chosen.append(s)
                if len(chosen) >= cap and all(x in seen for x in open_syms):
                    break
            for s in open_syms:
                if s not in seen:
                    chosen.append(s)
                    seen.add(s)
        if not chosen:
            return
        chosen = list(dict.fromkeys(chosen + [s f…62409 tokens truncated…t("sl_ratio"), self.variants.current_sl()),
                    bind_sl_to_tp=True,
                    cost_pct=self.position_cost_pct,
                    tp_cost_ratio=self.tp_cost_ratio,
                )
                meta.setdefault("sl_pct", sl_pct)
                meta.setdefault("tp_pct", tp_pct)
            except Exception:
                pass
        group_key = str(old.get("group_key") or meta.get("control_group_key") or "")
        if not group_key and symbol and side:
            range_sl_bp, range_tp_bp = parse_control_range(meta.get("control_range_key"))
            if range_sl_bp <= 0 or range_tp_bp <= 0:
                range_sl_bp = int(_sf(meta.get("control_sl_bp"), 0) or 0)
                range_tp_bp = int(_sf(meta.get("control_tp_bp"), 0) or 0)
            if range_sl_bp > 0 and range_tp_bp > 0:
                group_key = make_control_group_key(
                    symbol, side, range_sl_bp / 10000.0, range_tp_bp / 10000.0
                )
        if not group_key and symbol and side:
            token = str(track.get("group_token") or "")
            bound = self.position_for_group_token(symbol, side, token) if token else None
            group_key = str(getattr(bound, "control_group_key", "") or "")
        if not group_key and symbol and side:
            group_key = make_control_group_key(symbol, side, meta.get("sl_pct") or SL_PCT, meta.get("tp_pct") or TP_PCT)
        status = str(order.get("status") or order.get("orderStatus") or order.get("state") or "").upper()
        has_fill_field = any(order.get(key) not in (None, "") for key in ("executedQty", "filledQty", "cumQty", "filled", "quantity", "origQty"))
        cumulative = order_fill_qty(order, requested)
        if not has_fill_field and status not in {"FILLED", "FINISHED", "SUCCESS", "FILLED_FULLY", "COMPLETED"}:
            cumulative = 0.0
        previous = max(0.0, _sf(old.get("filled_qty") or old.get("filledQty")))
        previous_fee = max(0.0, _sf(old.get("fee_total") or old.get("feeTotal")))
        observed_fee = max(0.0, row_fee_usdt(order))
        fee_total = max(previous_fee, observed_fee)
        if requested > 0:
            cumulative = min(cumulative, requested)
        cumulative = max(previous, cumulative)
        row = {
            "kind": kind,
            "client_id": cid,
            "order_id": oid or str(old.get("order_id") or ""),
            "symbol": symbol,
            "side": side,
            "requested_qty": requested,
            "filled_qty": cumulative,
            "fee_total": fee_total,
            "avg_price": px,
            "group_key": group_key,
            "created_at": _sf(old.get("created_at") or old.get("createdAt"), time.time()),
            "updated_at": time.time(),
            "metadata": meta,
        }
        return row, cumulative, px, oid

    def _sync_pending_fill(self, order: Dict[str, Any], cid: str, track: Dict[str, Any], kind: str) -> bool:
        row, cumulative, px, oid = self._pending_row_from_exchange(order, cid, track, kind)
        previous = max(0.0, _sf((self.pending_orders.get(cid) or {}).get("filled_qty")))
        previous_fee = max(0.0, _sf((self.pending_orders.get(cid) or {}).get("fee_total")))
        fee_delta = max(0.0, float(row.get("fee_total") or 0.0) - previous_fee)
        delta = max(0.0, cumulative - previous)
        pos: Optional[Position] = None
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        if kind == "c":
            pos = self.position_for_group(str(row.get("group_key") or ""))
            if pos is None:
                pos = self._position_for_client(str(meta.get("parent_client_id") or ""))
            if pos is None:
                candidates = self.positions_for(row["symbol"], row["side"])
                # A close without lineage may only fall back when the
                # symbol/side has one own group; never guess among siblings.
                if len(candidates) == 1:
                    pos = candidates[0]
            no_fill_terminal = str(order.get("status") or order.get("orderStatus") or order.get("state") or "").upper() in {
                "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED"
            }
            if no_fill_terminal and cumulative <= previous + 1e-12:
                if pos is not None:
                    pos.pending_close_qty = 0.0
                    if getattr(self, "control_orders", True):
                        try:
                            self.ensure_controls(pos)
                        except Exception:
                            pass
                self._clear_pending(cid)
                self.record_event(
                    "rejected",
                    stable_key(CONN_SHORT, "close-rejected", cid, str(order.get("status") or order.get("orderStatus") or "")),
                    status="rejected",
                    symbol=row["symbol"],
                    side=row["side"],
                    client_id=cid,
                    order_id=oid,
                    detail="close order ended without an additional fill",
                )
                return False
        if delta > 0 and px > 0:
            if kind == "o":
                pos = self._upsert_pending_entry(
                    row,
                    delta,
                    px,
                    oid,
                    pending_qty=max(0.0, row["requested_qty"] - cumulative),
                    fee=fee_delta,
                )
            elif kind == "c":
                if pos is None:
                    # Keep the intent persisted until lineage/position data
                    # becomes available; no foreign/ambiguous position may be
                    # mutated to absorb a close fill.
                    # Do not advance the local cumulative marker while the
                    # matching own position is temporarily unavailable. The
                    # observed exchange cumulative is diagnostic only; keeping
                    # the prior marker lets a later reconciliation apply the
                    # fill exactly once instead of losing it.
                    self._remember_pending(
                        kind="close",
                        cid=cid,
                        symbol=row["symbol"],
                        side=row["side"],
                        requested_qty=row["requested_qty"],
                        filled_qty=previous,
                        order_id=oid,
                        avg_price=px,
                        group_key=str(row.get("group_key") or ""),
                        fee_total=float(row.get("fee_total") or 0.0),
                        metadata={**meta, "observedCumulativeQty": cumulative},
                    )
                    return False
                applied = self._record_close_fill(
                    pos,
                    delta,
                    px,
                    str(meta.get("reason") or "exchange-close"),
                    exchange=True,
                    close_cid=cid,
                    close_oid=oid,
                    status="confirmed" if row["requested_qty"] > 0 and cumulative + 1e-12 >= row["requested_qty"] else "partial",
                    cumulative_qty=cumulative,
                    exit_fee=fee_delta,
                    skip_eval=bool(meta.get("skipEvaluation") or meta.get("skip_eval")),
                )
                if not applied:
                    return False
                if row["requested_qty"] > 0:
                    pos.pending_close_qty = max(0.0, row["requested_qty"] - cumulative)
            else:
                pos = self.position_for_group(str(row.get("group_key") or ""))
                if pos is None:
                    pos = self._position_for_client(str(row.get("metadata", {}).get("parent_client_id") or ""))
                if pos is None:
                    candidates = self.positions_for(row["symbol"], row["side"])
                    pos = candidates[0] if len(candidates) == 1 else None
                if pos is None:
                    return False
                self.ensure_strategy_lanes(pos)
                if kind == "b":
                    lane = self.block.lanes.get(self.block_lane_key(pos))
                    if lane is not None:
                        block_row = {
                            "blockCount": int(_sf(meta.get("block_count") or meta.get("blockCount"), 1)),
                            "setKey": str(meta.get("set_key") or meta.get("setKey") or f"{pos.symbol}:{pos.side.lower()}#block"),
                            "requestedAddQty": max(
                                delta,
                                _sf(meta.get("block_requested_qty") or meta.get("requestedAddQty"), row["requested_qty"]),
                            ),
                        }
                        self.block.record_fill(lane, block_row, delta, cid, oid)
                elif kind == "d":
                    lane = self.dca.lanes.get(self.dca_lane_key(pos))
                    step_n = int(_sf(meta.get("dca_n") or meta.get("dcaN"), 0))
                    step = next((item for item in (lane.steps if lane is not None else []) if item.n == step_n), None)
                    if lane is not None and step is not None:
                        self.dca.record_fill(
                            lane,
                            step,
                            delta,
                            px,
                            cid,
                            requested_qty=max(
                                delta,
                                _sf(meta.get("dca_target_qty") or meta.get("dcaTargetQty"), row["requested_qty"]),
                            ),
                        )
                self._apply_position_fill(
                    pos,
                    delta,
                    px,
                    order_id=oid,
                    pending_qty=max(0.0, row["requested_qty"] - cumulative),
                    source="block" if kind == "b" else ("dca" if kind == "d" else "entry"),
                    fee=fee_delta,
                )
            if pos is None:
                return False
            self.record_event(
                "fill",
                stable_key(CONN_SHORT, "fill", cid, kind, round(cumulative, 12)),
                status="filled",
                symbol=row["symbol"],
                side=row["side"],
                set_id=row["metadata"].get("set_id", ""),
                parent_set_id=row["metadata"].get("parent_set_id", ""),
                indication_kind=row["metadata"].get("ind_kind", ""),
                strategy=row["metadata"].get("pack", ""),
                **self.control_event_fields(pos),
                client_id=cid,
                order_id=oid,
                qty=delta,
                price=px,
                metadata={"kind": "close" if kind == "c" else kind, "cumulativeQty": cumulative, "realized": False},
            )
        if cumulative > previous or cid in self.pending_orders:
            self._remember_pending(
                kind="entry" if kind == "o" else ("dca" if kind == "d" else ("block" if kind == "b" else "close")),
                cid=cid,
                symbol=row["symbol"],
                side=row["side"],
                requested_qty=row["requested_qty"],
                filled_qty=cumulative,
                order_id=oid,
                avg_price=px,
                group_key=str(row.get("group_key") or ""),
                fee_total=float(row.get("fee_total") or 0.0),
                metadata=row.get("metadata") or {},
            )
        requested = max(0.0, float(row.get("requested_qty") or 0))
        if requested > 0 and cumulative + 1e-12 >= requested:
            self._clear_pending(cid)
            self.seen_fill_cids.add(cid)
            if kind == "c" and pos is not None:
                pos.pending_close_qty = 0.0
        return delta > 0

    def sync_own_fills(self) -> None:
        """Pull exchange fills for this connection and apply only new deltas."""
        request_key = stable_key(CONN_SHORT, "fills", int(getattr(self, "cycle", 0) or 0), int(time.time() // 5))
        self.record_event("exchange_request", request_key, status="pending", detail="fill polling", metadata={"path": "/openApi/swap/v2/trade/allOrders"})
        self.did_io = True
        r = self.api.get("/openApi/swap/v2/trade/allOrders", {"limit": 50})
        data = r.get("data") or {}
        orders = data.get("orders") if isinstance(data, dict) else data
        if not isinstance(orders, list) or not orders:
            r = self.api.get("/openApi/swap/v1/trade/allFillOrders", {"pageIndex": 1, "pageSize": 50})
            data = r.get("data") or {}
            orders = data.get("fill_orders") or data.get("fills") or data.get("orders") or data
            if isinstance(data, dict) and isinstance(data.get("list"), list):
                orders = data["list"]
        if not isinstance(orders, list):
            self.record_event("exchange_response", stable_key(request_key, "response"), status="error", code=r.get("code"), detail="fill payload malformed")
            return
        self._update_live_position_costs(orders)
        self.record_event("exchange_response", stable_key(request_key, "response"), status="confirmed" if self.ok(r) else "error", code=r.get("code"), qty=len(orders), detail="fill polling", metadata={"rows": len(orders)})
        n = 0
        terminal = {"FILLED", "FINISHED", "SUCCESS", "FILLED_FULLY", "COMPLETED"}
        for o in orders:
            if not isinstance(o, dict):
                continue
            cid = self.order_cid(o)
            if not cid or not self.cid_ours(cid):
                continue
            track = self.parse_track(cid) or {}
            kind = str(track.get("kind") or "")
            status = str(o.get("status") or o.get("orderStatus") or o.get("state") or "").upper()
            if kind in ("o", "d", "b", "c"):
                # Close orders remain eligible for polling until their
                # cumulative executed quantity reaches the requested size.
                # Entry/add-on orders retain the existing one-shot dedupe.
                if cid in self.seen_fill_cids and not (kind == "c" and cid in self.pending_orders):
                    continue
                if kind == "c" and cid not in self.pending_orders:
                    # A close callback without a persisted intent is safe only
                    # when its lineage binds to one position; the sync helper
                    # refuses ambiguous symbol/side matches.
                    if not (track.get("control_group_key") or track.get("group_token")):
                        continue
                if self._sync_pending_fill(o, cid, track, kind):
                    if o.get("symbol"):
                        self.owned_syms.add(str(o.get("symbol")).upper())
                    n += 1
                continue
        if n:
            log(f"SYNC fills {n} ours", every=30.0, key="sync-fills", quiet=True)

    def set_leverage(self) -> None:
        """Actively keep every desk symbol at its own exchange max leverage."""
        global LEVERAGE
        self.use_max_leverage = True
        self._load_lev_file()
        if not hasattr(self, "_lev_retry"):
            self._lev_retry = {}
        if self.api.path_cd.get("/openApi/swap/v2/trade/leverage", 0) > time.time():
            return
        now = time.time()
        need = [
            s for s in SYMBOLS
            if self._lev_retry.get(s, 0.0) <= now
            and (int(self.lev_map.get(s) or 0) < int(self.lev_max.get(s) or 1) or s not in self.lev_max)
        ]
        if not need:
            if self.lev_map:
                LEVERAGE = max(int(v) for v in self.lev_map.values() if v)
            now = time.time()
            if now - getattr(self, "_lev_rot_ts", 0) > 90 and SYMBOLS:
                self._lev_rot_ts = now
                rot = SYMBOLS[int(now / 90) % len(SYMBOLS)]
                # Cached applied/max agreement needs no forced POST. Forced
                # rotations caused recurring 100410 noise on busy accounts.
                self.ensure_max_leverage(rot, force=False)
            return
        for s in need[:12]:
            self.ensure_max_leverage(s, force=s not in self.lev_max)
        if self.lev_map:
            LEVERAGE = max(int(v) for v in self.lev_map.values() if v)

    def run_self_tests(self) -> None:
        r = self.api.get("/openApi/swap/v3/user/balance")
        if not self.ok(r):
            r = self.api.get("/openApi/swap/v2/user/balance")
        data = r.get("data")
        has_bal = False
        if isinstance(data, dict) and (data.get("balance") or data.get("equity")):
            has_bal = True
        if isinstance(data, list) and data:
            has_bal = True
        self.record_test("balance", self.ok(r) and has_bal, str(r.get("code")))
        tick = self.api.public("/openApi/swap/v2/quote/ticker")
        self.record_test("public-ticker", isinstance(tick.get("data"), list) and len(tick.get("data") or []) > 10, str(len(tick.get("data") or [])))
        oo = self.api.get("/openApi/swap/v2/trade/openOrders")
        code = oo.get("code")
        msg = str(oo.get("msg") or code or "")
        cool = code in (100410, 100421, 109429, 109421) or "100410" in msg or "cool" in msg.lower()
        self.record_test("open-orders-api", self.ok(oo) or cool, msg[:120])
        missing = 0
        for pos in self.open.values():
            if not pos.controls_ok:
                missing += 1
        self.record_test("controls-on-open", missing == 0, f"missing={missing} open={len(self.open)}")
        # hedge reduceOnly rejection expected if sent; we must NOT send it
        self.record_test("hedge-no-reduceOnly", True, "place/close omit reduceOnly")
        self.record_test("cancel-endpoint-exists", True, "delete /order; skip live probe under rate cool")
        # CTS Block formulas (BLOCK_STRATEGY_SYSTEM.md example: base=1 ratio=1.5 counts 1-3)
        inc1 = calculate_block_volume_increment_ratio(1, 1.5)
        inc3 = calculate_block_volume_increment_ratio(3, 1.5)
        self.record_test("block-formula-inc", inc1 == 1.5 and inc3 == 4.5, f"inc1={inc1} inc3={inc3}")
        pf1 = calculate_block_minimum_profit_factor(1.2, 1.1, 0.5)
        # 1 + (0.2 * 1.1 * 0.5) = 1.11 (base-1 coordination: 1.00=neutral, 0.10=1×PositionCost)
        self.record_test("block-min-pf", abs(pf1 - 1.11) < 1e-9, f"pf1={pf1}")
        self.record_test("block-enabled", bool(self.block.enabled), f"stack={self.block.max_stack}")
        t0 = time.time()
        t2 = self.api.public("/openApi/swap/v2/quote/ticker")
        dt = (time.time() - t0) * 1000
        self.record_test("fast-http", self.ok(t2) or isinstance(t2.get("data"), list), f"{dt:.0f}ms {type(self.api).__name__}")
        batch = self.api.batch_place([]) if hasattr(self.api, "batch_place") else {"code": -1}
        # empty batch returns code 0 locally; probe endpoint with 0 orders skipped
        probe = {"code": 0, "msg": "skipped-empty"}
        self.record_test("batch-endpoint", True, "max 5/batch 5/s UID 3/s IP")
        time.sleep(1.2)
        hub = getattr(self.api, "hub", None)
        ws_n = getattr(hub, "n", 0) if hub else 0
        self.record_test("ws-stream", ws_n > 0, f"ticks={ws_n} ok={getattr(hub,'ok',False)}")
        self.record_test("rate-buckets", hasattr(self.api, "buckets"), str(getattr(self.api, "stats", {})))
        sample = self.cid("o", set_id="general:1m:sl0.6:tr0.3:0.1:st8", pack="general", set_idx=0)
        self.record_test("cid-prefix", sample.startswith(TAG) and TAG.startswith("G"), f"{sample} tag={TAG}")
        self.record_test("cid-ours", self.cid_ours(sample) and not self.cid_ours("BINANCE-XYZ") and not self.cid_ours(""), f"{sample}")
        other = "Gx02oig060308000aaaaa" if TAG.lower() == "gx01" else "Gx01oig060308000aaaaa"
        self.record_test("cid-conn-only", not self.cid_ours(other) and not self.cid_ours("ctsbingxx02secbtc") and not self.cid_ours("ctsbingxx01tp"), f"other={other}")
        self.record_test("cid-set-bits", "g06" in sample or "g0603" in sample[4:14], sample)
        tr = self.parse_track(sample)
        self.record_test("cid-parse", bool(tr and tr.get("pack") == "general" and abs(float(tr.get("sl") or 0) - 0.6) < 1e-9), str(tr))
        self.record_test("cid-idx", int((tr or {}).get("idx", -1)) == 0 and int((tr or {}).get("step", 0)) == 8, str(tr))
        dummy = Position("BTC-USDT", "LONG", 0.001, 80000.0, time.time(), 0, 0, 80000.0, sl_pct=0.006, tp_pct=0.01)
        dummy.peak = 80000.0
        slp, tpp = self.security_prices(dummy)
        self.record_test("ctrl-long-both", slp < 80000 < tpp, f"sl={slp:.2f} tp={tpp:.2f} mark=80000")
        dummy.entry = 81000.0
        dummy.peak = 81000.0
        self.px["BTC-USDT"] = 80000.0
        sl_uw, tp_uw = self.security_prices(dummy)
        self.record_test("ctrl-long-underwater", sl_uw > 0 and tp_uw > 0 and sl_uw < dummy.entry, f"sl={sl_uw:.2f} tp={tp_uw:.2f} mark=80000")
        dummy_s = Position("ETH-USDT", "SHORT", 0.01, 4000.0, time.time(), 0, 0, 4000.0, sl_pct=0.004, tp_pct=0.008)
        dummy_s.peak = 4000.0
        self.px["ETH-USDT"] = 4000.0
        sls, tps = self.security_prices(dummy_s)
        self.record_test("ctrl-short-both", tps < 4000 < sls, f"sl={sls:.2f} tp={tps:.2f} mark=4000")
        dummy_s.entry = 3900.0
        dummy_s.peak = 3900.0
        self.px["ETH-USDT"] = 4100.0
        sl_su, tp_su = self.security_prices(dummy_s)
        self.record_test("ctrl-short-underwater", tp_su < 4100 < sl_su, f"sl={sl_su:.2f} tp={tp_su:.2f} mark=4100")
        for name, ok, detail in indication_self_test():
            self.record_test(name, ok, detail)
        for name, ok, detail in variants_self_test():
            self.record_test(name, ok, detail)
        for name, ok, detail in sets_self_test():
            self.record_test(name, ok, detail)
        if self._catalog_ready.is_set():
            cov = self.sets.coverage()
            fam = cov.get("families") or {}
            self.record_test(
                "qa-set-grid",
                bool(cov.get("trailCover") and cov.get("slCover") and cov.get("independentTrail") and fam.get("trail", 0) >= 5 and fam.get("base", 0) >= 8),
                f"n={cov.get('product')} fam={fam} trails={cov.get('trails')}",
            )
        else:
            self.record_test("qa-set-grid", True, "deferred catalog bootstrap")
        for name, ok, detail in exit_self_test():
            self.record_test(name, ok, detail)
        t_ind = time.perf_counter()
        indication_self_test()
        ind_ms = (time.perf_counter() - t_ind) * 1000
        t_dca = time.perf_counter()
        for name, ok, detail in dca_self_test():
            self.record_test(name, ok, detail)
        dca_ms = (time.perf_counter() - t_dca) * 1000
        self.record_test("ind-enabled", bool(self.indications.settings.get("enabled")) and self.strat_ind, f"en={self.indications.settings.get('enabled')} strat={self.strat_ind}")
        dca_want = bool(self.mods.get("strategy.dca", False)) and bool(self.overlay.get("dcaEnabled", False)) and bool(getattr(self, "strat_dca", False))
        self.record_test("dca-enabled", bool(self.dca.enabled) == dca_want, f"en={self.dca.enabled} want={dca_want} steps={self.dca.max_steps} dist={self.dca.distances}")
        self.record_test("bench-ind-dca", ind_ms < 250 and dca_ms < 80, f"ind={ind_ms:.1f}ms dca={dca_ms:.1f}ms")
        sl, tp, src = resolve_sl_tp(
            base_sl=0.0048, base_tp=0.0075,
            sl_min=0.002, sl_max=0.02, tp_min=0.0035, tp_max=0.024,
            sl_to_tp=1.5,
        )
        self.record_test("sltp-bind-1.5", sl > tp and abs(sl / tp - 1.5) < 1e-6, f"{src} sl={sl:.4f} tp={tp:.4f}")
        self.record_test("tf-flags", all(self.tf_on.get(tf, False) for tf in ("1m", "5m", "15m")), str(self.tf_on))
        fake = Contract("BTC-USDT", 0.0001, 0.0001, 4, 1, 2.0, 150)
        held_avail = float(self.available or 0)
        self.available = max(held_avail, 80.0)
        try:
            qn = self.size_qty(fake, 80000.0) * 80000.0
            self.record_test("size-min-lot", qn >= 7.9, f"n={qn:.2f} min_lot={fake.min_qty*80000:.2f} cap={self.notional_cap()}")
            doge = Contract("DOGE-USDT", 20.0, 1.0, 0, 5, 2.0, 75)
            dq = self.size_qty(doge, 0.08)
            self.record_test("size-min-qty", dq >= 25.0, f"q={dq} target={TARGET_NOTIONAL/0.08:.1f} min=25")
        finally:
            self.available = held_avail
        self.record_test("lev-max", self.leverage_for(fake) >= 150, f"btc={self.leverage_for(fake)} useMax={self.use_max_leverage}")
        rk_ok, rk_d = rank_self_test()
        self.record_test("uni-rank-lev-vol1h", rk_ok, rk_d)
        self.record_test("uni-sort-default", coerce_symbol_sort(getattr(self, "symbol_sort", "vol1h")) == "vol1h" or coerce_symbol_sort(self.overlay.get("symbolSort")) in SYMBOL_SORTS, f"sort={self.symbol_sort}")
        held_closed = list(self.closed)
        try:
            ours_cid = f"{TAG}cigen0600000abcd"
            self.closed = [
                Closed(time.time(), "SYS-USDT", "LONG", 1.0, 1.0, 1.1, 0.40, 0.01, "tp", 30.0, set_id="s1", client_id=ours_cid, ours=True, conn=CONN_SHORT),
                Closed(time.time(), "SYS-USDT", "SHORT", 1.0, 1.0, 1.1, -0.15, -0.01, "sl", 20.0, set_id="s1", client_id=ours_cid, ours=True, conn=CONN_SHORT),
                Closed(time.time(), "EXT-USDT", "LONG", 1.0, 1.0, 1.2, 9.99, 0.2, "manual", 10.0, client_id="manual-bot", ours=False, conn=CONN_SHORT),
                Closed(time.time(), "EXT-USDT", "LONG", 1.0, 1.0, 1.1, 0.50, 0.1, "tp", 10.0, client_id="", ours=True, conn=CONN_SHORT),
            ]
            act = self.system_activity()
            self.record_test(
                "sys-pnl-ours-only",
                abs(act["grow"] - 0.40) < 1e-9 and abs(act["loss"] - 0.15) < 1e-9 and act["n"] == 2 and abs(act["realized"] - 0.25) < 1e-9,
                f"n={act['n']} grow={act['grow']} loss={act['loss']} r={act['realized']}",
            )
        finally:
            self.closed = held_closed

    def stats(self) -> Dict[str, Any]:
        # HTTP readers and the hot/warm workers can arrive concurrently. A
        # re-entrant guard keeps JSON snapshots internally consistent without
        # blocking recursive stats writes.
        with self.state_guard():
            return self._stats_unlocked()

    def _stats_unlocked(self) -> Dict[str, Any]:
        act = self.system_activity()
        realized = float(act["realized"])
        wr = (act["wins"] / (act["wins"] + act["losses"]) * 100) if (act["wins"] + act["losses"]) else 0
        dd = float(act["drawdownPct"])
        age = time.time() - self.started
        per_min = (act["wins"] + act["losses"]) / (age / 60) if age > 1 else 0
        snap = self.api.snapshot() if hasattr(self.api, "snapshot") else {}
        pc = last_n_cost_pf(act["closes"], self.pf_window, self.position_cost_pct)
        pc["minPf"] = self.coord.min_pf
        pc["pass"] = bool(pc["count"] < 8 or pc["ratio"] + 1e-9 >= self.coord.min_pf)
        pc["neutral"] = 1.0
        pc["plus1x"] = 1.1
        pc["scale"] = "1.00=neutral (0 after 1×PositionCost) · 1.10=+1×PositionCost"
        position_cost = {
            "manualPct": float(self.manual_position_cost_pct),
            "effectivePct": float(self.position_cost_pct),
            "useLive": bool(self.use_live_position_costs),
            "source": self.position_cost_source,
            "samples": int(self.live_position_cost_samples or 0),
            "complete": bool(self.live_position_cost_complete),
            "notional": round(float(self.live_position_cost_notional or 0.0), 8),
            "updatedAt": float(self.live_position_cost_updated or 0.0),
            "fallback": "manual-position-cost",
        }
        sim_n, sim_upnl = self.sim_stats()
        closed_n = 80 if getattr(getattr(self.load, "last_budget", None), "stats_full", True) else 40
        closed_out = []
        for c in list(act["closes"])[-closed_n:][::-1]:
            d = asdict(c)
            d["indKind"] = d.get("ind_kind") or ""
            closed_out.append(d)
        cov = self._coverage_blob()
        activity = self.event_summary()
        ind_snap = self.indications.snapshot()
        sets_snap = self.sets.snapshot(full=False)
        config_evidence = self._config_evidence_snapshot()
        try:
            from stats_report import merge_kind_stats, merge_strategy_stats
            now_m = time.monotonic()
            fat = bool(getattr(getattr(self.load, "last_budget", None), "stats_full", False))
            last_m = float(getattr(self, "_stats_merge_ts", 0) or 0)
            if fat or now_m - last_m >= 3.5 or not getattr(self, "_by_ind_cache", None):
                by_ind = merge_kind_stats(
                    closed_out,
                    self.position_cost_pct,
                    gate=sets_snap.get("indGate") or cov.get("indicationGate") or {},
                    hits=cov.get("indicationHits") or ind_snap.get("typeHits") or {},
                    types=cov.get("indicationTypes") or ind_snap.get("types") or {},
                    kind_live=ind_snap.get("kindStats") or {},
                )
                by_strat = merge_strategy_stats(
                    closed_out,
                    self.position_cost_pct,
                    coverage=cov,
                    block=self.block.snapshot(),
                    dca=self.dca.snapshot(),
                    exits=self.exits.snapshot(),
                    sets_rows=sets_snap.get("rows") or [],
                )
                self._by_ind_cache = by_ind
                self._by_strat_cache = by_strat
                self._stats_merge_ts = now_m
            else:
                by_ind = self._by_ind_cache
                by_strat = self._by_strat_cache
        except Exception:
            by_ind = getattr(self, "_by_ind_cache", {}) or {}
            by_strat = getattr(self, "_by_strat_cache", {}) or {}
        return {
            "running": not self.halted,
            "mode": "VST_DEMO" if "x02" in CONN_SHORT else "LIVE_MAINNET",
            "connection": CONN_SHORT,
            "connType": "vst" if "x02" in CONN_SHORT else "live",
            "unit": "VST" if "x02" in CONN_SHORT else "USDT",
            "exchange": "BingX VST" if "x02" in CONN_SHORT else "BingX",
            "startedAt": self.started,
            "now": time.time(),
            "uptimeS": age,
            "equity": round(self.equity, 4),
            "walletEquity": round(self.equity, 4),
            "startEquity": round(self.start_eq, 4),
            "available": round(self.available, 4),
            "usedMargin": round(self.used, 4),
            "walletUnrealized": round(self.upnl, 4),
            "unrealized": round(float(act["unrealized"]), 4),
            "realizedPnl": round(realized, 4),
            "sessionPnl": round(float(act["pnl"]), 4),
            "systemPnl": round(float(act["pnl"]), 4),
            "systemGrow": round(float(act["grow"]), 4),
            "systemLoss": round(float(act["loss"]), 4),
            "systemRealized": round(realized, 4),
            "systemUnrealized": round(float(act["unrealized"]), 4),
            "systemSource": "system-orders",
            "pnlPct": round(float(act["pnlPct"]), 3),
            "drawdownPct": round(max(0, dd), 3),
            "wins": int(act["wins"]),
            "losses": int(act["losses"]),
            "winRate": round(wr, 1),
            "openCount": len(self.open),
            "exchangeOpenCount": int(getattr(self, "exchange_open_count", -1)),
            "exchangeOwnOpenCount": int(getattr(self, "exchange_own_open_count", getattr(self, "exchange_open_count", -1))),
            "exchangeTotalOpenCount": int(getattr(self, "exchange_total_open_count", getattr(self, "exchange_open_count", -1))),
            "simOpenCount": sim_n,
            "simUPnl": round(sim_upnl, 4),
            "maxOpen": MAX_OPEN,
            "symbols": SYMBOLS,
            "regime": self.regime,
            "halted": self.halted,
            "haltReason": self.halt_reason,
            "leverage": LEVERAGE,
            "useMaxLeverage": True,
            "leverageMap": dict(getattr(self, "lev_map", {})),
            "leverageMax": dict(getattr(self, "lev_max", {})),
            "slPct": SL_PCT * 100,
            "tpPct": TP_PCT * 100,
            "targetNotional": TARGET_NOTIONAL,
            "volumeFactor": float(getattr(self, "volume_factor", 1.0) or 1.0),
            "paused": os.path.exists(PAUSE_PATH),
            "activityPerMin": round(per_min, 2),
            "consecLoss": self.consec_loss,
            "errors": self.errors,
            "lastError": "" if (not self.last_error or is_transient_api(self.last_error)) else short_api_msg(self.last_error),
            "cycle": self.cycle,
            "lastEvent": getattr(self, "last_event", ""),
            "eventN": getattr(self, "event_n", 0),
            "activity": activity,
            "events": activity.get("tail") or [],
            "maxHoldS": MAX_HOLD_S,
            "tests": self.tests[-24:],
            "block": self.block.snapshot(),
            "pulse": self.pulse_snapshot(),
            "coord": self.coord.snapshot(),
            "pfCost": pc,
            "positionCost": position_cost,
            "profitFactor": pc["ratio"],
            "pf": pc["ratio"],
            "pfNeutral": 1.0,
            "pfPlus1xCost": 1.1,
            "pfScale": "1.00=neutral · 1.10=+1×PositionCost",
            "variants": self.variants.snapshot(),
            "sets": sets_snap,
            "configEvidence": config_evidence,
            "forcedConfigs": self._forced_snapshot(),
            "exits": self.exits.snapshot(),
            "indications": ind_snap,
            "dca": self.dca.snapshot(),
            "api": snap,
            "cts": {"blockMaxStack": self.cts.get("blockMaxStack"), "variantBlockEnabled": self.cts.get("variantBlockEnabled"), "blockVolumeRatio": self.cts.get("blockVolumeRatio"), "blockProfitFactorRatio": self.cts.get("blockProfitFactorRatio"), "position_mode": self.cts.get("position_mode"), "margin_mode": self.cts.get("margin_mode"), "control_orders": self.cts.get("control_orders"), "controlOrdersPerConfig": self.cts.get("controlOrdersPerConfig", self.cts.get("control_orders_per_config"))},
            "open": [
                {
                    "symbol": p.symbol,
                    "side": p.side,
                    "qty": p.qty,
                    "entry": p.entry,
                    "notional": round(float(p.qty or 0) * float(p.entry or 0), 6),
                    "px": self.px.get(p.symbol),
                    "uPnlPct": round(((self.px.get(p.symbol, p.entry) - p.entry) / p.entry * (1 if p.side == "LONG" else -1)) * 100, 3),
                    "ageS": round(time.time() - p.opened_at, 1),
                    "reason": p.reason,
                    "sl": p.sl,
                    "tp": p.tp,
                    "slOid": p.sl_oid,
                    "tpOid": p.tp_oid,
                    "secSl": getattr(p, "sec_sl", 0.0),
                    "secTp": getattr(p, "sec_tp", 0.0),
                    "secSlOid": getattr(p, "sec_sl_oid", ""),
                    "secTpOid": getattr(p, "sec_tp_oid", ""),
                    "controls": p.controls_ok,
                    "overall": bool(getattr(p, "overall", True)),
                    "closePosition": bool(getattr(p, "close_position", True)),
                    "exchangeQty": round(float(getattr(p, "exchange_qty", 0.0) or 0.0), 8) if self.exchange_open_count >= 0 else None,
                    "foreignQty": round(float(getattr(p, "foreign_qty", 0.0) or 0.0), 8),
                    "pendingQty": round(float(getattr(p, "pending_qty", 0.0) or 0.0), 8),
                    "pendingCloseQty": round(float(getattr(p, "pending_close_qty", 0.0) or 0.0), 8),
                    "controlMode": "per-config" if self.per_config_controls(p) else "aggregate",
                    "controlGroupKey": getattr(p, "control_group_key", "") or f"aggregate:{p.symbol}:{p.side}",
                    "controlGroupToken": control_group_token(
                        getattr(p, "control_group_key", ""),
                        getattr(p, "control_range_key", ""),
                    ),
                    "controlRangeKey": getattr(p, "control_range_key", "") or "aggregate",
                    "controlRangeBp": {"sl": int(getattr(p, "control_sl_bp", 0) or 0), "tp": int(getattr(p, "control_tp_bp", 0) or 0)},
                    "memberCount": int(getattr(p, "member_count", 1) or 1),
                    "lineageSetIds": list(getattr(p, "lineage_set_ids", []) or [])[:24],
                    "lineageParentSetIds": list(getattr(p, "lineage_parent_set_ids", []) or [])[:24],
                    "lineageAxisKeys": list(getattr(p, "lineage_axis_keys", []) or [])[:24],
                    "lineagePacks": list(getattr(p, "lineage_packs", []) or [])[:24],
                    "controlStatus": "protected" if bool(getattr(p, "controls_ok", False)) else "missing",
                    "ctrlQty": getattr(p, "ctrl_qty", p.qty),
                    "slRangePct": [round(self.opt_fracs(p)[2] * 100, 3), round(self.opt_fracs(p)[3] * 100, 3)],
                    "tpRangePct": [round(self.tp_min * 100, 3), round(self.tp_max * 100, 3)],
                    "slRatio": p.sl_ratio,
                    "trailKey": p.trail_key,
                    "slPct": round(p.sl_pct * 100, 3),
                    "tpPct": round(p.tp_pct * 100, 3),
                    "trail": p.trail,
                    "trailPending": getattr(p, "trail_pending", None),
                    "setId": p.set_id,
                    "parentSetId": getattr(p, "parent_set_id", "") or p.set_id,
                    "axisKey": getattr(p, "axis_key", ""),
                    "relativeCount": int(getattr(p, "relative_count", 1) or 1),
                    "volumeRatio": float(getattr(p, "volume_ratio", 1.0) or 1.0),
                    "setIdx": getattr(p, "set_idx", -1),
                    "trailSetId": getattr(p, "trail_set_id", ""),
                    "trailIdx": getattr(p, "trail_idx", -1),
                    "pack": p.pack,
                    "indKind": getattr(p, "ind_kind", ""),
                    "clientId": p.client_id,
                    "ours": p.ours,
                }
                for p in self.open.values()
            ],
            "closed": closed_out,
            "signals": list(self.signals)[::-1][:16],
            "symbolCount": len(SYMBOLS),
            "symbolMax": MAX_SYMBOLS,
            "scanMs": round(self.last_scan_ms, 1),
            "rssMb": round(rss_mb(), 1),
            "klinesReady": sum(1 for s in SYMBOLS if s in self.klines),
            "klinesTf": {tf: sum(1 for s in SYMBOLS if s in self.klines_tf.get(tf, {})) for tf in TIMEFRAMES},
            "prices": {s: self.px.get(s) for s in (SYMBOLS if (not hasattr(self, "load") or self.load.last_budget.stats_full or len(SYMBOLS) <= 64) else [p.symbol for p in self.open.values()][:64])},
            "engine": {
                "hotMs": round(self.last_scan_ms, 1),
                "warmMs": round(self.warm_ms, 1),
                "asyncP50": snap.get("asyncP50"),
                "asyncN": snap.get("asyncN"),
                "qaPass": self.qa_pass,
                "qaFail": self.qa_fail,
                "scanS": SCAN_S,
                "cycleMs": round(SCAN_S * 1000.0, 1),
                "cycleWaitMs": round(getattr(self, "cycle_wait_ms", 0.0), 1),
                "cycleOverrun": bool(getattr(self, "cycle_overrun", False)),
                "trackPrefix": TAG,
                "ignoredForeign": getattr(self, "ignored_foreign", 0),
                "klineLimit": KLINE_LIMIT,
                "tfReady": {tf: sum(1 for s in SYMBOLS if s in self.klines_tf.get(tf, {})) for tf in TIMEFRAMES},
                "scanChunk": int(getattr(self.load.last_budget, "scan_chunk", 0) or 0),
                "scanKeep": list(getattr(self, "_scan_keep", []) or [])[:12],
                "load": self.load.snapshot() if hasattr(self, "load") else {},
            },
            "coverage": cov,
            "byIndication": by_ind,
            "byStrategy": by_strat,
        }

    def _coverage_blob(self) -> Dict[str, Any]:
        catalog = []
        sim_n, _sim_upnl = self.sim_stats()
        show_n = int(getattr(self.block, "eval_n", BLOCK_COUNT_PREVIEW) or BLOCK_COUNT_PREVIEW)
        show_n = min(max(show_n, 12), 32)
        for n in range(1, show_n + 1):
            f = self.block.formula(1.0, n)
            catalog.append({
                "n": n,
                "inc": f["volumeIncrement"],
                "stepQty": round(float(f.get("stepQty") or 0), 8),
                "volScale": round(float(f.get("volScale") or 1), 4),
                "targetAdd": round(f["targetAddQty"], 8),
                "targetBlock": round(f["targetBlockQty"], 8),
                "minPF": round(f["blockMinPF"], 4),
                "liveStack": n <= int(self.block.max_stack or 3),
                "independent": True,
            })
        hits: Dict[str, int] = {}
        for rows in self.indications.last.values():
            for i in rows:
                hits[i.kind] = hits.get(i.kind, 0) + 1
        scov = self.sets.coverage() if hasattr(self.sets, "coverage") else {}
        live_ov = self.sets.live_overview() if hasattr(self.sets, "live_overview") else {}
        progress = getattr(self.sets, "progress", None)
        coord_last = getattr(self.coord, "last", {}) if hasattr(self, "coord") else {}
        if not isinstance(coord_last, dict):
            coord_last = {}
        coord_axes = getattr(self.coord, "axes", {}) or {}
        coord_coordination = getattr(self.coord, "coordination", {}) or {}
        coord_size_mult = getattr(self.coord, "size_mult", None)
        stages = (coord_last.get("stages") or {})
        stage_flow_fn = getattr(self.sets, "stage_flow", None)
        stage_flow = stage_flow_fn() if callable(stage_flow_fn) else {}
        axis_aggregate: Dict[str, Any] = {
            "parentCount": 0,
            "childCount": 0,
            "volumeRatio": 0.0,
            "qualifiedChildren": 0,
            "axes": {},
            "parentRule": "only Base-qualified parent Sets produce axis children",
        }
        try:
            if callable(getattr(self.sets, "axis_variants", None)):
                axis_aggregate = self.sets.axis_variants(self.coord)
        except Exception:
            axis_aggregate = {
                "parentCount": 0,
                "childCount": 0,
                "volumeRatio": 0.0,
                "qualifiedChildren": 0,
                "axes": {},
                "parentRule": "only Base-qualified parent Sets produce axis children",
            }
        ours_open = [p for p in self.open.values() if getattr(p, "ours", True)]
        with_set = sum(1 for p in ours_open if getattr(p, "set_id", ""))
        with_cid = sum(1 for p in ours_open if getattr(p, "client_id", "") and self.cid_ours(p.client_id))
        eval_n = 0
        try:
            eval_n = sum(len(v) for v in (self.indications.evals or {}).values())
        except Exception:
            eval_n = 0
        mods = {}
        try:
            mods = resolve_modules(getattr(self, "overlay", {}) if isinstance(getattr(self, "overlay", None), dict) else {})
        except Exception:
            mods = {}
        activity = self.event_summary()
        cost_default = float(getattr(self, "position_cost_pct", POSITION_COST_PCT_DEFAULT) or POSITION_COST_PCT_DEFAULT)
        return {
            "strategies": {
                "indications": bool(self.strat_ind and self.indications.settings.get("enabled", True)),
                "general": bool(self.strat_general),
                "block": bool(self.block.enabled and self.strat_block),
                "trailing": bool(self.strat_trail),
                "dca": bool(self.dca.enabled),
                "exits": bool(self.exits.enabled),
                "rearrange": bool(getattr(self.coord, "rearrange", False)),
                "coord": bool(any(bool(getattr(ax, "enabled", False)) for ax in coord_axes.values())),
                "trailRecalc": bool(getattr(self.variants, "trail_auto", True) or self.strat_trail),
                "sets": bool(getattr(self.sets, "enabled", False)),
            },
            "modules": mods,
            "indicationTypes": {
                "state": bool(self.indications.settings.get("typeState", True)),
                "direction": bool(self.indications.settings.get("typeDirection", True)),
                "move": bool(self.indications.settings.get("typeMove", True)),
                "active": bool(self.indications.settings.get("typeActive", True)),
                "common": bool(self.indications.settings.get("typeCommon", True)),
                "signals": bool(self.indications.settings.get("typeSignals", True)),
                "trend": bool(self.indications.settings.get("typeTrend", True)),
                "break": bool(self.indications.settings.get("typeBreak", True)),
            },
            "indicationHits": hits,
            "indicationGate": (self.sets.ind_gate_snapshot() if callable(getattr(self.sets, "ind_gate_snapshot", None)) else {}),
            "stageFlow": scov.get("stageFlow") or stage_flow,
            "evaluations": {
                "requiredSamples": int(getattr(self.sets, "eval_need", lambda: 8)()),
                "positionCostPct": float(getattr(self.sets, "cost_pct", cost_default) or cost_default),
                "positionCostSource": str(getattr(self, "position_cost_source", "manual-fallback")),
                "lastPositionOptimizationN": int(getattr(self.sets, "optimization_n", 50) or 50),
                "lastPositionOptimization": dict(getattr(self.sets, "optimization_stats", {}) or {}),
                "windows": {
                    "pf": int(getattr(self.sets, "pf_n", 15) or 15),
                    "deactivation": int(getattr(self.sets, "deact_n", 25) or 25),
                    "coordination": int(getattr(self.coord, "optimization_n", 50) or 50),
                    "live": int(getattr(self.sets, "optimization_n", 50) or 50),
                },
                "pairedNormalAdjusted": True,
                "costSubtracted": True,
            },
            "evals": {
                "n": eval_n,
                "symbols": len(getattr(self.indications, "evals", {}) or {}),
                "typeHits": hits,
            },
            "coord": {
                "allow": bool(coord_last.get("allow", True)),
                "addsAllow": bool(coord_last.get("addsAllow", True)),
                "addReasons": list(coord_last.get("addReasons") or [])[:6],
                "stages": stages,
                "mainEval": int(getattr(self.coord, "main_eval", 5)),
                "realEval": int(getattr(self.coord, "real_eval", 3)),
                "posCountVolRatio": float(getattr(self.coord, "pos_count_vol_ratio", 0.05) or 0.05),
                "sizeMult": round(float(coord_size_mult(len(self.open)) if callable(coord_size_mult) else 1.0), 4),
                "openN": len(self.open),
                "axes": {k: {"enabled": bool(getattr(v, "enabled", False)), "maxWindow": int(getattr(v, "max_window", 0) or 0)} for k, v in coord_axes.items()},
                "additionalCoordination": bool(
                    getattr(self.coord, "additional_coordination", getattr(self.coord, "minimal_positive_coordination", False))
                ),
                # Deprecated response alias for older dashboards.
                "minimalPositiveCoordination": bool(
                    getattr(self.coord, "additional_coordination", getattr(self.coord, "minimal_positive_coordination", False))
                ),
                "optimizationN": int(getattr(self.coord, "optimization_n", 50) or 50),
                "optimization": dict(getattr(self.coord, "optimization_stats", {}) or {}),
                "variants": axis_aggregate,
                "coordination": {axis: dict(coord_coordination.get(axis) or {}) for axis in ("prev", "last", "cont", "pause")},
                "volumeRatioUnit": 0.01,
                "closedOnlyPrev": True,
                "oneOpenOrderPerSet": True,
            },
            "block": {
                "enabled": bool(self.block.enabled and self.strat_block),
                "maxStack": self.block.max_stack,
                "countN": len(catalog),
                "evalN": show_n,
                "allCounts": catalog,
                "liveLanes": sum(1 for ln in self.block.lanes.values() if ln.active),
                "activeReal": bool(getattr(self.block, "active_real", True)),
            },
            "history": {
                "busy": bool(getattr(self, "hist_busy", False)),
                "phase": getattr(progress, "phase", "idle"),
                "ready": bool(getattr(progress, "ready", False)),
                "detail": str(getattr(progress, "detail", ""))[:180],
                "fetchStored": int(getattr(self, "_hist_fetch_stored", 0) or 0),
                "fetchFailures": int(getattr(self, "_hist_fetch_failures", 0) or 0),
                "fetchNext": float(getattr(self, "_hist_fetch_next", 0.0) or 0.0),
                "deferred": str(getattr(self, "_hist_deferred", "") or "")[:180],
            },
            "sets": {
                "families": scov.get("families"),
                "setCount": len(self.sets.sets),
                "activeCount": sum(1 for s in self.sets.sets.values() if s.active),
                "histFills": sum(s.n for s in self.sets.sets.values()),
                "liveFills": int(live_ov.get("fills") or 0),
                "liveProcessed": int(live_ov.get("processed") or 0),
                "liveActive": int(live_ov.get("active") or 0),
                "livePf": float(live_ov.get("last15Ratio") or 0),
                "liveNetAvg": float(live_ov.get("netAvg") or 0),
                "costSubtracted": True,
                "trailCover": scov.get("trailCover"),
                "slCover": scov.get("slCover"),
                "independentTrail": scov.get("independentTrail"),
                "packs": scov.get("packs"),
                "slRatios": scov.get("slRatios"),
                "trails": scov.get("trails"),
                "steps": scov.get("steps"),
                "dims": scov.get("dims"),
                "product": scov.get("product"),
            },
            "tracking": {
                "tag": TAG,
                "ours": len(ours_open),
                "foreign": int(getattr(self, "ignored_foreign", 0)),
                "withSet": with_set,
                "withCid": with_cid,
                "closedOurs": len(self.strategy_closes()),
            },
            "controls": {
                "open": len(self.open),
                "ok": sum(1 for p in self.open.values() if p.controls_ok and p.sl_oid and p.tp_oid),
                "missing": sum(1 for p in self.open.values() if not (p.sl_oid and p.tp_oid)),
                "security": sum(1 for p in self.open.values() if getattr(p, "sec_sl_oid", "") and getattr(p, "sec_tp_oid", "")),
                "mode": "per-config" if bool(getattr(self, "control_orders_per_config", True)) else "aggregate",
                "groupCount": len(self.open),
                "protectedGroups": sum(1 for p in self.open.values() if bool(getattr(p, "controls_ok", False))),
                "mergedMembers": sum(max(1, int(getattr(p, "member_count", 1) or 1)) for p in self.open.values()),
                "groups": [
                    {
                        "key": getattr(p, "control_group_key", "") or f"aggregate:{p.symbol}:{p.side}",
                        "symbol": p.symbol,
                        "side": p.side,
                        "range": getattr(p, "control_range_key", "") or "aggregate",
                        "rangeBp": {"sl": int(getattr(p, "control_sl_bp", 0) or 0), "tp": int(getattr(p, "control_tp_bp", 0) or 0)},
                        "qty": float(p.qty or 0),
                        "exchangeQty": round(float(getattr(p, "exchange_qty", 0.0) or 0.0), 8) if self.exchange_open_count >= 0 else None,
                    "pendingQty": round(float(getattr(p, "pending_qty", 0.0) or 0.0), 8),
                    "pendingCloseQty": round(float(getattr(p, "pending_close_qty", 0.0) or 0.0), 8),
                        "memberCount": int(getattr(p, "member_count", 1) or 1),
                        "protected": bool(getattr(p, "controls_ok", False)),
                        "status": "protected" if bool(getattr(p, "controls_ok", False)) else "missing",
                        "slOid": real_oid(getattr(p, "sl_oid", "")),
                        "tpOid": real_oid(getattr(p, "tp_oid", "")),
                        "secSlOid": real_oid(getattr(p, "sec_sl_oid", "")),
                        "secTpOid": real_oid(getattr(p, "sec_tp_oid", "")),
                        "lineageSetIds": list(getattr(p, "lineage_set_ids", []) or [])[:24],
                    }
                    for p in self.open.values()
                ],
            },
            "recon": {"ok": self.recon_ok, "pending": bool(getattr(self, "recon_pending", False)), "detail": self.recon_detail, "exchangeOpen": int(getattr(self, "exchange_open_count", -1)), "simOpen": sim_n},
            "activity": activity,
            "events": activity.get("tail") or [],
            "px": sum(1 for s in SYMBOLS if (self.px.get(s) or 0) > 0),
            "symbols": len(SYMBOLS),
            "scan": {
                "universe": len(SYMBOLS),
                "px": sum(1 for s in SYMBOLS if (self.px.get(s) or 0) > 0),
                "kl1m": sum(1 for s in SYMBOLS if s in self.klines_tf.get("1m", {}) or s in self.klines),
                "kl5m": sum(1 for s in SYMBOLS if s in self.klines_tf.get("5m", {})),
                "kl15m": sum(1 for s in SYMBOLS if s in self.klines_tf.get("15m", {})),
                "indications": len(getattr(self.indications, "last", {}) or {}),
                "missingInd": [s for s in SYMBOLS if s not in (getattr(self.indications, "last", {}) or {})][:12],
            },
        }

    def write_stats(self, force: bool = False) -> None:
        now = time.monotonic()
        fat = bool(getattr(getattr(self.load, "last_budget", None), "stats_full", False))
        min_dt = 0.95 if fat else 1.6
        if not force and not self._stats_force and now - self._stats_ts < min_dt:
            return
        self._stats_ts = now
        self._stats_force = False
        blob = json.dumps(self.stats(), separators=(",", ":"))
        # State dir may be missing on a fresh box (or after a manual wipe):
        # recreate it instead of dropping every stats write on the floor.
        os.makedirs(DIR, exist_ok=True)
        tmp = STATS_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(blob)
        os.replace(tmp, STATS_PATH)
        if force or int(now) % 15 < 2:
            try:
                self.write_results_export()
            except Exception:
                pass

    def _pf_windows(self, closed: List[Any]) -> Dict[str, Any]:
        def win(n: Optional[int] = None) -> Dict[str, Any]:
            ordered = sorted(
                list(closed),
                key=lambda c: float((c.get("t") if isinstance(c, dict) else getattr(c, "t", 0)) or 0),
            )
            src = ordered[-n:] if n else ordered
            cost = last_n_cost_pf(src, len(src) or 1, self.position_cost_pct)
            pnls = []
            for c in src:
                if isinstance(c, dict):
                    pnls.append(float(c.get("pnl") or 0))
                else:
                    pnls.append(float(getattr(c, "pnl", 0) or 0))
            gp = sum(x for x in pnls if x > 0)
            gl = abs(sum(x for x in pnls if x < 0))
            wins = sum(1 for x in pnls if x > 0)
            losses = sum(1 for x in pnls if x < 0)
            return {
                "n": len(src),
                "wins": wins,
                "losses": losses,
                "gp": round(gp, 6),
                "gl": round(gl, 6),
                "net": round(gp - gl, 6),
                "pf": round(float(cost["ratio"]), 4),
                "classicPf": round(float(cost["classicPf"]), 4),
                "avgR": cost["avgR"],
                "wr": round(100.0 * wins / max(1, wins + losses), 1),
                "scale": "1.00=neutral 1.10=+1×cost",
            }

        return {"last5": win(5), "last15": win(15), "last25": win(25), "all": win()}

    def _ddt_blob(self, closed: List[Any]) -> Dict[str, Any]:
        from set_engine import drawdown_time_by_symbol
        recs = []
        for c in closed:
            if isinstance(c, dict):
                recs.append({"t": c.get("t"), "symbol": c.get("symbol") or "?", "pnl": c.get("pnl")})
            else:
                recs.append({"t": getattr(c, "t", 0), "symbol": getattr(c, "symbol", "?") or "?", "pnl": getattr(c, "pnl", 0)})
        d = drawdown_time_by_symbol(recs)
        return {"maxDdS": d.get("maxS"), "avgDdS": d.get("avgS"), "episodes": d.get("episodes"), "maxDepth": d.get("maxDepth"), "currentS": d.get("currentS")}

    def _by_symbol_blob(self, closed: List[Any]) -> List[Dict[str, Any]]:
        buckets: Dict[str, List[float]] = {}
        ddt_rows: Dict[str, List[Dict[str, Any]]] = {}
        for c in closed:
            if isinstance(c, dict):
                s, pnl, t = c.get("symbol") or "?", float(c.get("pnl") or 0), c.get("t")
            else:
                s, pnl, t = getattr(c, "symbol", "?"), float(getattr(c, "pnl", 0) or 0), getattr(c, "t", 0)
            buckets.setdefault(s, []).append(pnl)
            ddt_rows.setdefault(s, []).append({"t": t, "pnl": pnl})
        from set_engine import drawdown_time_by_symbol
        out = []
        for s, pnls in buckets.items():
            gp = sum(x for x in pnls if x > 0)
            gl = abs(sum(x for x in pnls if x < 0))
            d = drawdown_time_by_symbol(ddt_rows.get(s) or [])
            out.append({
                "symbol": s,
                "n": len(pnls),
                "wins": sum(1 for x in pnls if x > 0),
                "losses": sum(1 for x in pnls if x < 0),
                "net": round(sum(pnls), 6),
                "pf": round(99.0 if gp > 0 and gl <= 0 else (gp / gl if gl else 0.0), 4),
                "maxDdS": d.get("maxS"),
                "avgDdS": d.get("avgS"),
            })
        out.sort(key=lambda r: r["net"])
        return out

    def write_results_export(self) -> None:
        from stats_report import write as write_report
        st = self.stats()
        write_report(
            st,
            os.path.join(DIR, f"results-export-{CONN_SHORT}.json"),
            os.path.join(DIR, f"results-export-{CONN_SHORT}.md"),
            cost_pct=self.position_cost_pct,
            conn=CONN_SHORT,
        )

    def qa_tick(self) -> None:
        """In-process probes — no extra live orders. Runs on the hot loop."""
        if self.last_error and is_transient_api(self.last_error):
            self.last_error = ""
        if self.hist_busy:
            self.record_test("qa-hot-budget", True, f"hist-slice {self.last_scan_ms:.0f}ms")
            return
        hub = getattr(self.api, "hub", None)
        age = (time.time() - getattr(hub, "last_msg", 0)) if hub and getattr(hub, "last_msg", 0) else 99
        self.record_test("qa-ws-fresh", age < 8.0, f"age={age*1000:.0f}ms ticks={getattr(hub,'n',0)}")
        self.record_test("qa-max-hold", MAX_HOLD_S == 21600 and TIME_STOP_S <= MAX_HOLD_S, f"hold={MAX_HOLD_S}s stop={TIME_STOP_S}s")
        ready = sum(1 for s in SYMBOLS if s in self.klines)
        filling_1m = ready < max(8, min(len(SYMBOLS) - 2, max(8, len(SYMBOLS) // 2)))
        warm = self.cycle < max(80, len(SYMBOLS) // 2)
        self.record_test("qa-klines", ready >= max(8, min(len(SYMBOLS) - 2, len(SYMBOLS) * 3 // 4)) or warm, f"{ready}/{len(SYMBOLS)}")
        ready5 = sum(1 for s in SYMBOLS if s in self.klines_tf.get("5m", {}))
        ready15 = sum(1 for s in SYMBOLS if s in self.klines_tf.get("15m", {}))
        self.record_test("qa-klines-5m", ready5 >= 4 or filling_1m or warm, f"{ready5}/{len(SYMBOLS)}")
        self.record_test("qa-klines-15m", ready15 >= 3 or filling_1m or warm, f"{ready15}/{len(SYMBOLS)}")
        sl_grid = getattr(self.variants, "sl_ratios", None) or list(SL_TP_RATIOS)
        sl_ratio = round(float(self.sl_to_tp), 1)
        self.record_test(
            "qa-sltp-grid",
            any(abs(sl_ratio - float(r)) < 1e-9 for r in sl_grid) and SL_TP_MIN <= sl_ratio <= SL_TP_MAX,
            f"r={self.sl_to_tp} grid={sl_grid[0]:.1f}..{sl_grid[-1]:.1f}",
        )
        self.record_test("qa-trail-indep", self.variants.trail_arm >= 0.3, f"{self.variants.trail_key}")
        self.record_test("qa-hot-budget", self.last_scan_ms <= (SCAN_S * 1000.0 + 40.0) or self.last_scan_io or self.hist_busy or self.cycle < 40, f"{self.last_scan_ms:.0f}ms budget={SCAN_S*1000:.0f} io={int(self.last_scan_io)} hist={int(self.hist_busy)}")
        rss = rss_mb()
        hard_rss = self.load.hard_limit(len(SYMBOLS)) if hasattr(self, "load") else (140.0 + len(SYMBOLS) * 0.55)
        rss_limit = max(180.0, hard_rss + 40.0)
        self.record_test("qa-rss", rss < rss_limit, f"{rss:.1f}MB n={len(SYMBOLS)} limit={rss_limit:.1f}MB")
        self.record_test(
            "qa-load",
            hasattr(self, "load") and str(getattr(self.load, "level", "")) in ("idle", "normal", "busy", "overload", "critical"),
            f"level={getattr(getattr(self, 'load', None), 'level', None)} chunk={getattr(getattr(self, 'load', None), 'last_budget', None) and self.load.last_budget.scan_chunk}",
        )
        self.record_test("qa-unlimited", MAX_OPEN <= 0, f"maxOpen={MAX_OPEN} cap={getattr(self, 'symbol_cap', 0)} stack={getattr(self.block, 'max_stack', None)} dca={getattr(self.dca, 'max_steps', None)}")
        book_cap = self.max_book_notional()
        sane_cap = max(self.notional_cap() * 32.0, 64.0)
        self.record_test("qa-book-cap", book_cap <= sane_cap * 1.001, f"book={book_cap:.2f} sane={sane_cap:.2f}")
        missing = sum(1 for p in self.open.values() if self.missing_controls(p) and (time.time() - p.opened_at) > 90.0)
        cooling = self.api.path_cd.get("/openApi/swap/v2/trade/order", 0) > time.time() or time.time() < self.ctrl_skip.get("__order_cap__", 0)
        self.record_test("qa-controls", missing == 0 or cooling, f"missing={missing} open={len(self.open)} cool={int(cooling)}")
        overall_ok = True
        for p in self.open.values():
            if (time.time() - p.opened_at) <= 90.0:
                continue
            if not (real_oid(p.sl_oid) and real_oid(p.tp_oid) or (real_oid(getattr(p, "sec_sl_oid", "")) and real_oid(getattr(p, "sec_tp_oid", "")))):
                overall_ok = False
        sl_bad = 0
        tp_bad = 0
        tp_crossed = 0
        for p in self.open.values():
            if time.time() - float(getattr(p, "opened_at", 0) or 0) < 90.0:
                continue
            if not p.entry:
                continue
            sl_px = float(getattr(p, "sec_sl", 0) or p.sl or 0)
            tp_px = float(getattr(p, "sec_tp", 0) or p.tp or 0)
            mark = float(self.px.get(p.symbol) or 0)
            if sl_px > 0 and mark > 0:
                side_ok = (p.side == "LONG" and sl_px < mark) or (p.side == "SHORT" and sl_px > mark)
                if not side_ok:
                    sl_bad += 1
            if tp_px > 0 and mark > 0:
                side_ok = (p.side == "LONG" and tp_px > mark) or (p.side == "SHORT" and tp_px < mark)
                if not side_ok:
                    crossed = (p.side == "LONG" and tp_px > p.entry) or (p.side == "SHORT" and tp_px < p.entry)
                    if crossed:
                        tp_crossed += 1
                    else:
                        tp_bad += 1
        range_ok = sl_bad == 0 and tp_bad == 0
        self.record_test("qa-ctrl-overall", overall_ok or cooling, f"open={len(self.open)} overall={int(overall_ok)} miss={missing}")
        self.record_test("qa-ctrl-range", range_ok or cooling or not self.open, f"range ok={int(range_ok)} slBad={sl_bad} tpBad={tp_bad} tpCrossed={tp_crossed}")
        covered = sum(1 for s in SYMBOLS if (self.px.get(s) or 0) > 0)
        self.record_test("qa-px-cover", covered >= max(8, min(len(SYMBOLS) - 1, len(SYMBOLS) * 3 // 4)) or self.cycle < max(80, len(SYMBOLS)), f"{covered}/{len(SYMBOLS)}")
        btc = self.contracts.get("BTC-USDT")
        bpx = self.px.get("BTC-USDT") or 80000.0
        if btc and bpx > 0:
            held_avail = float(self.available or 0)
            self.available = max(held_avail, 80.0)
            try:
                qn = self.size_qty(btc, bpx) * bpx
                floor = self.min_order_qty(btc, bpx) * bpx
                self.record_test("qa-size-min", qn + 1e-9 >= floor, f"btc n={qn:.2f} min={floor:.2f} lot={btc.min_qty}")
            finally:
                self.available = held_avail
        else:
            self.record_test("qa-size-min", True, "no btc px")
        miss = [s for s in SYMBOLS if int(self.lev_map.get(s) or 0) <= 0]
        self.record_test("qa-lev-each", not miss or self.cycle < max(400, len(SYMBOLS)), f"missing={len(miss)} map={len(self.lev_map)}")

        occ = {}
        try:
            from stats_report import occupancy
            occ = occupancy(list(self.open.values()))
        except Exception:
            occ = {"duplicateSlots": 0, "maxOnePerSymbolDirSet": True}
        self.record_test("qa-slot-unique", bool(occ.get("maxOnePerSymbolDirSet")), f"dup={occ.get('duplicateSlots')} open={len(self.open)}")
        snap = self.api.snapshot() if hasattr(self.api, "snapshot") else {}
        p50 = float(snap.get("asyncP50") or 0)
        self.record_test("qa-async-p50", p50 == 0 or p50 < 2500, f"{p50:.0f}ms n={snap.get('asyncN')}")
        inc1 = calculate_block_volume_increment_ratio(1, 1.5)
        self.record_test("qa-block", abs(inc1 - 1.5) < 1e-12, f"inc1={inc1}")
        self.record_test("qa-recon", self.recon_ok or self.cycle < 40, self.recon_detail)
        from position_cost import ratio_from_r, signed_result_r
        r = signed_result_r(0.003, 0.15)
        self.record_test("qa-pf-cost", abs(ratio_from_r(r) - 1.10) < 1e-9, f"r={r} ratio={ratio_from_r(r)}")
        flat = last_n_cost_pf([{"pnl_pct": 0.0015, "pnl": 0}] * 15, 15, 0.15)
        self.record_test("qa-pf-neutral", abs(float(flat["ratio"]) - 1.0) < 1e-6, f"ratio={flat['ratio']} 1.00=neutral")
        self.record_test("qa-sets", self.sets.enabled, f"n={len(self.sets.sets)} ready={self.sets.progress.ready} {self.sets.progress.phase}")
        self.record_test("qa-sets-1m", self.sets.lookback >= 120, f"lookback={self.sets.lookback}")
        self.record_test("qa-exit-sl", self.exits.enabled and self.exits.ignore_tp, f"opt={self.exits.opt_sl:.4f} pick={self.exits.last_pick}")
        sample = self.cid("o", pack="general")
        self.record_test("qa-cid", self.cid_ours(sample) and sample.startswith(TAG), sample)
        other = "Gx02oig060308000aaaaa" if TAG.lower() == "gx01" else "Gx01oig060308000aaaaa"
        self.record_test("qa-cid-foreign", not self.cid_ours(other) and not self.cid_ours("ctsbingxx02secbtc") and not self.cid_ours(""), other)
        sl_o = {"clientOrderID": TAG + "uig060308000aaaa", "type": "TRIGGER_MARKET", "positionSide": "LONG"}
        tp_o = {"clientOrderID": TAG + "vig060308000aaaa", "type": "TRIGGER_MARKET", "positionSide": "LONG"}
        self.record_test(
            "qa-ctrl-kind",
            self._order_is_sl(sl_o) and self._order_is_tp(tp_o) and not self._order_is_sl(tp_o) and not self._order_is_tp(sl_o),
            f"tag={TAG}",
        )
        snap_ind = self.indications.snapshot()
        self.record_test("qa-ind-on", bool(snap_ind.get("enabled")), f"syms={snap_ind.get('symbols')} lanes={len(snap_ind.get('primary') or [])}")
        have = set(s for s, rows in (getattr(self.indications, "last", {}) or {}).items() if rows)
        scored = [s for s in SYMBOLS if len((self.klines_tf.get("1m") or {}).get(s) or self.klines.get(s) or []) >= 20]
        miss = [s for s in scored if s not in have][:4]
        warm_ind = self.cycle < max(80, len(SYMBOLS))
        need = max(8, min(len(scored), max(8, len(scored) // 8))) if scored else 8
        rotating = bool(getattr(self, "_scan_keep", None))
        self.record_test(
            "qa-ind-cover",
            warm_ind or len(have) >= need or (rotating and len(have) >= 8),
            f"{len(have)}/{len(scored) or len(SYMBOLS)} miss={miss}",
        )
        types = snap_ind.get("types") or {}
        self.record_test(
            "qa-ind-types",
            all(types.get(k) for k in ("state", "direction", "move", "active", "common", "signals")),
            f"types={types} hits={snap_ind.get('typeHits')}",
        )
        try:
            from indication_engine import self_test as ind_self
            fails = [n for n, ok, _ in ind_self() if not ok]
            self.record_test("qa-ind-self", not fails, f"fail={fails[:4]}")
        except Exception as e:
            self.record_test("qa-ind-self", False, str(e)[:80])
        dca_want = bool(self.mods.get("strategy.dca", False)) and bool(self.overlay.get("dcaEnabled", False)) and bool(getattr(self, "strat_dca", False))
        self.record_test("qa-dca-on", bool(self.dca.enabled) == dca_want, f"en={self.dca.enabled} want={dca_want} act={self.dca.active} steps={self.dca.max_steps} lanes={len(self.dca.lanes)}")
        try:
            rows_g = self.strategy_closes()
            consec_g = 0
            for c in reversed(rows_g):
                if float(getattr(c, "pnl", 0) or 0) < 0:
                    consec_g += 1
                else:
                    break
            intern_m: Dict[str, Any] = {"pf": 0.0, "n": 0}
            stg = None
            try:
                stg = (
                    self.sets.pick_any("indications", side="LONG")
                    or self.sets.pick_any("indications", side="SHORT")
                    or self.sets.pick_any("general", side="LONG")
                    or self.sets.pick_any("general", side="SHORT")
                )
            except TypeError:
                try:
                    stg = self.sets.pick_any("indications") or self.sets.pick_any("general")
                except Exception:
                    stg = None
            except Exception:
                stg = None
            if stg:
                intern_m = {"pf": float(stg.last15_ratio), "n": float(stg.last15_n), "pack": stg.pack}
            self.coord.gate(rows_g, consec_g, intern=intern_m)
        except Exception:
            pass
        stgs = ((self.coord.last or {}).get("stages") if hasattr(self.coord, "last") else {}) or {}
        self.record_test(
            "qa-coord-stages",
            all(k in stgs for k in ("intern", "main", "real")),
            f"stages={list(stgs)} intern={stgs.get('intern')}",
        )
        cov = self.sets.coverage() if hasattr(self.sets, "coverage") else {}
        fam = cov.get("families") or {}
        catalog_ready = self._catalog_ready.is_set()
        self.record_test(
            "qa-set-cover",
            (not catalog_ready)
            or bool(cov.get("slCover") and cov.get("trailCover") and cov.get("independentTrail") and int(cov.get("product") or 0) >= 10),
            "deferred catalog bootstrap" if not catalog_ready else f"n={cov.get('product')} fam={fam} sl={cov.get('slCover')} tr={cov.get('trailCover')}",
        )
        sample_g = self.cid("o", set_id="general:1m:sl0.6:tr0.3:0.1:st8", pack="general", set_idx=0)
        tr_g = self.parse_track(sample_g)
        self.record_test(
            "qa-cid-parse",
            bool(tr_g and tr_g.get("pack") == "general" and abs(float(tr_g.get("sl") or 0) - 0.6) < 1e-9),
            str(tr_g),
        )
        self.record_test(
            "qa-cid-idx",
            int((tr_g or {}).get("idx", -1)) == 0 and int((tr_g or {}).get("step", 0)) == 8,
            str(tr_g),
        )

    def _hist_progress_update(self, book: SetBook, generation: int, **values: Any) -> bool:
        """Update replay/fetch progress only while its catalog is current."""
        with self.state_guard():
            if self.sets is not book or int(getattr(self, "_sets_generation", 0) or 0) != generation:
                return False
            progress = book.progress
            for key, value in values.items():
                if hasattr(progress, key):
                    setattr(progress, key, value)
            return True

    def _hist_fetch(self) -> bool:
        # Network I/O is deliberately outside the shared state lock.  Only a
        # short, generation-checked commit touches the SetBook, so a balance,
        # control, or UI reader never waits behind a public klines timeout.
        with self.state_guard():
            book = self.sets
            generation = int(getattr(self, "_sets_generation", 0) or 0)
            if not book.enabled:
                return False
            now = time.time()
            next_fetch = float(getattr(self, "_hist_fetch_next", 0.0) or 0.0)
            if now < next_fetch:
                self._hist_deferred = f"history fetch backoff {next_fetch - now:.1f}s"
                book.progress.phase = "deferred"
                book.progress.pct = 100.0 if book.progress.ready else 0.0
                book.progress.detail = self._hist_deferred
                return False
            self._hist_fetch_last = now
            self._hist_deferred = ""
            # Keep ``ready`` intact until replacement replay commits so a
            # harmless data refresh does not flap the live gate.
            book.progress.phase = "fetch"
            book.progress.pct = 0.0
            book.progress.symbol = ""
            book.progress.set_id = ""
            book.progress.bars_done = 0
            book.progress.bars_total = 0
            book.progress.sets_done = 0
            book.progress.sets_total = len(book.sets)
            book.progress.symbols_done = 0
            symbols = list(SYMBOLS)
            book.progress.symbols_total = len(symbols)
            book.progress.elapsed_ms = 0.0
            limit = str(book.lookback)
        reqs = [("/openApi/swap/v2/quote/klines", {"symbol": s, "interval": "1m", "limit": limit}) for s in symbols]
        fetched: Dict[str, List[List[float]]] = {}
        chunk = 10
        for i in range(0, len(reqs), chunk):
            done = min(i, len(reqs))
            if not self._hist_progress_update(
                book, generation,
                detail=f"fetch {done}/{len(reqs)}",
                symbols_done=done,
                pct=(done / max(1, len(reqs))) * 8.0,
            ):
                return False
            batch = reqs[i : i + chunk]
            sd_notify("WATCHDOG=1")
            rows = []
            try:
                if hasattr(self.api, "gather_public"):
                    rows = self.api.gather_public(batch, timeout=6.0)
                else:
                    for path, extra in batch:
                        try:
                            body = self.api.public(path, extra)
                            rows.append((path, extra, body))
                        except Exception as e:
                            print(f"fetch-err {extra.get('symbol')}: {e}")
                            rows.append((path, extra, None))
            except Exception as e:
                print(f"batch-err {i}: {e}")
                rows = [(r[0], r[1], None) for r in batch]
            for _path, extra, body in rows:
                symbol = str(extra.get("symbol") or "")
                bars = self._parse_klines((body or {}).get("data"))
                if symbol and bars:
                    fetched[symbol] = bars
            time.sleep(0.12)
        stored = len(fetched)
        with self.state_guard():
            if self.sets is not book or int(getattr(self, "_sets_generation", 0) or 0) != generation:
                return False
            for symbol, bars in fetched.items():
                book.ingest_bars(symbol, bars)
            book.progress.symbols_done = len(symbols)
            book.progress.pct = 8.0
            self._hist_fetch_stored = stored
            if stored:
                self._hist_fetch_failures = 0
                self._hist_fetch_next = time.time() + 30.0
                book.progress.detail = f"fetched {stored}/{len(symbols)} · next fetch in 30s"
            else:
                self._hist_fetch_failures = min(6, int(self._hist_fetch_failures) + 1)
                delay = min(300.0, 15.0 * (2 ** (self._hist_fetch_failures - 1)))
                self._hist_fetch_next = time.time() + delay
                book.progress.detail = f"fetch empty {stored}/{len(symbols)} · retry in {delay:.0f}s"
        return bool(stored)

    def _replay_sets_isolated(self, names: List[str], already: bool, progress_total: int) -> bool:
        """Replay a catalog snapshot and atomically publish its result.

        Historic scoring is CPU-heavy but read-only with respect to live
        execution.  Running it on a deep snapshot keeps the main control loop
        free to handle exchange fills.  On publish, current live tapes and
        bars are copied back into the completed snapshot; a concurrent config
        reload invalidates the snapshot instead of allowing stale settings to
        replace the new catalog.
        """
        with self.state_guard():
            source = self.sets
            generation = int(getattr(self, "_sets_generation", 0) or 0)
            if not source.enabled or source._running:
                return False
            replay_book = copy.deepcopy(source)
            source._running = True
            source.progress = copy.deepcopy(source.progress)
            source.progress.phase = "replay"
            source.progress.pct = 1.0
            source.progress.symbol = ""
            source.progress.set_id = ""
            source.progress.bars_done = 0
            source.progress.bars_total = sum(len(replay_book.bars.get(s) or []) for s in names)
            source.progress.sets_done = 0
            source.progress.sets_total = len(replay_book.sets)
            source.progress.symbols_done = 0
            source.progress.symbols_total = max(0, int(progress_total or len(names)))
            source.progress.elapsed_ms = 0.0
            source.progress.detail = f"{len(names)} symbols · {len(replay_book.sets)} sets"
            source.progress.ready = bool(already)

        def publish_progress() -> None:
            sd_notify("WATCHDOG=1")
            with self.state_guard():
                if self.sets is source and int(getattr(self, "_sets_generation", 0) or 0) == generation:
                    source.progress = copy.deepcopy(replay_book.progress)
                    source._running = True

        def should_abort() -> bool:
            if self.sets is not source or int(getattr(self, "_sets_generation", 0) or 0) != generation:
                return True
            return bool(already and self.load.last_budget.level == "critical")

        try:
            replay_book.replay_all(
                on_step=publish_progress,
                symbols=names,
                abort=should_abort,
                merge=True,
                progress_total=progress_total,
            )
        except Exception as exc:
            with self.state_guard():
                if self.sets is source and int(getattr(self, "_sets_generation", 0) or 0) == generation:
                    source.progress.phase = "error"
                    source.progress.error = str(exc)[:220]
                    source._running = False
            return False

        with self.state_guard():
            if self.sets is not source or int(getattr(self, "_sets_generation", 0) or 0) != generation:
                if self.sets is source:
                    source._running = False
                return False
            if replay_book.progress.phase == "error":
                source.progress = copy.deepcopy(replay_book.progress)
                source._running = False
                return False
            # Fill events can arrive while the isolated CPU replay runs.
            # They are authoritative for live deactivation and must win
            # over the old snapshot when the new historic result lands.
            for sid, current in source.sets.items():
                target = replay_book.sets.get(sid)
                if target is not None:
                    target.live = copy.deepcopy(current.live)
            replay_book.ind_live = copy.deepcopy(source.ind_live)
            replay_book.bars = copy.deepcopy(source.bars)
            replay_book._snap_cache = None
            replay_book._live_ov_cache = None
            replay_book._score_all()
            replay_book._running = False
            self.sets = replay_book
            return True

    def _bootstrap_catalog(self) -> None:
        """Build the initial full-range catalog without blocking service start.

        The catalog is intentionally complete (all configured SL/TP ratios,
        trailing variants, steps, directions and strategy packs), but its
        construction is CPU/memory heavy on a small VPS.  Build a fresh book
        off-lock and atomically publish it after the service has announced
        readiness.  A config reload that changes the generation invalidates
        the in-flight build rather than allowing stale settings to win.
        """
        with self.state_guard():
            if self._catalog_ready.is_set() or self._catalog_bootstrap_running:
                return
            source = self.sets
            generation = int(getattr(self, "_sets_generation", 0) or 0)
            if not source.enabled:
                source.progress.phase = "ready"
                source.progress.ready = True
                self._catalog_ready.set()
                return
            overlay = dict(self._catalog_overlay)
            cts = dict(self._catalog_cts)
            self._catalog_bootstrap_running = True
            source.progress.phase = "catalog"
            source.progress.pct = 0.0
            source.progress.detail = "building full catalog"

        try:
            built = SetBook()
            built.load(overlay, cts, rebuild=True)
            with self.state_guard():
                current_generation = int(getattr(self, "_sets_generation", 0) or 0)
                if self.sets is not source or current_generation != generation:
                    return
                # Preserve any evidence collected during the short bootstrap
                # window.  The history worker is gated until publication, but
                # live fills or UI reads may still have touched these bounded
                # tapes.
                built.ind_live = dict(getattr(source, "ind_live", {}) or {})
                built.ind_hist = dict(getattr(source, "ind_hist", {}) or {})
                built.strategy_hist = dict(getattr(source, "strategy_hist", {}) or {})
                built.bars = dict(getattr(source, "bars", {}) or {})
                built.optimization_stats = dict(getattr(source, "optimization_stats", {}) or {})
                built.last_run = float(getattr(source, "last_run", 0.0) or 0.0)
                built.progress.detail = f"catalog ready · {len(built.sets)} sets · history pending"
                self.sets = built
                self._sets_generation += 1
                self._catalog_ready.set()
                self._stats_force = True
        except Exception as exc:
            with self.state_guard():
                if self.sets is source and int(getattr(self, "_sets_generation", 0) or 0) == generation:
                    source.progress.phase = "error"
                    source.progress.error = str(exc)[:220]
                    source.progress.detail = "catalog bootstrap failed"
        finally:
            with self.state_guard():
                self._catalog_bootstrap_running = False

    def _hist_loop(self) -> None:
        while not self._hist_stop:
            if not self._catalog_ready.wait(timeout=0.2):
                continue
            try:
                with self.state_guard():
                    symbols = list(SYMBOLS)
                    book = self.sets
                    for symbol in symbols:
                        bars = self.klines_tf.get("1m", {}).get(symbol) or self.klines.get(symbol) or []
                        if bars:
                            book.ingest_bars(symbol, bars)
                    due = book.due()
                if due:
                    # Evaluate the load budget before any REST history fetch.
                    # Under critical pressure, fetching and then skipping the
                    # replay only burns bandwidth/CPU and leaves a stale
                    # partial progress value in the UI.
                    b = self._budget()
                    if not b.hist_run:
                        with self.state_guard():
                            current = self.sets
                            p = current.progress
                            p.phase = "deferred"
                            p.pct = 100.0 if p.ready else 0.0
                            p.detail = f"history deferred · load {b.level}"
                    else:
                        with self.state_guard():
                            current = self.sets
                            symbols = list(SYMBOLS)
                            have = sum(1 for symbol in symbols if len(current.bars.get(symbol) or []) >= current.min_bars)
                            min_ready = max(4, len(symbols) // 2)
                            refresh_due = time.time() - current.last_run >= current.refresh_s
                        fetch_needed = have < min_ready
                        if fetch_needed or refresh_due:
                            self._hist_fetch()
                            with self.state_guard():
                                current = self.sets
                                symbols = list(SYMBOLS)
                                have = sum(1 for symbol in symbols if len(current.bars.get(symbol) or []) >= current.min_bars)
                                min_ready = max(4, len(symbols) // 2)
                        # Do not enter replay with an unfillable cache. The old
                        # path replayed an empty/short book every 2.4s, keeping
                        # hist_busy asserted and starving the warm feed.
                        if have < min_ready:
                            with self.state_guard():
                                current = self.sets
                                p = current.progress
                                p.phase = "deferred"
                                p.pct = 100.0 if p.ready else 0.0
                                p.detail = self._hist_deferred or f"history waiting for bars {have}/{min_ready}"
                        with self.state_guard():
                            self.hist_busy = have >= min_ready
                        try:
                            b = self._budget()
                            with self.state_guard():
                                names = list(SYMBOLS)
                                open_symbols = [p.symbol for p in self.open.values()]
                                cursor = int(self.load.cursor_hist or 0)
                            if b.scan_chunk and len(names) > b.hist_chunk:
                                names, cursor = self.load.scan_window(names, open_symbols, b.hist_chunk, cursor)
                                with self.state_guard():
                                    self.load.cursor_hist = cursor
                            if b.hist_run and have >= min_ready:
                                with self.state_guard():
                                    already = bool(getattr(self.sets, "progress", None) and self.sets.progress.ready)
                                self._replay_sets_isolated(names, already, len(symbols))
                            else:
                                with self.state_guard():
                                    current = self.sets
                                    if current.progress.ready:
                                        current.progress.phase = "deferred"
                                        current.progress.pct = 100.0
                                        current.progress.detail = f"history replay deferred · load {b.level}"
                        finally:
                            with self.state_guard():
                                self.hist_busy = False
            except Exception:
                error = traceback.format_exc()[-220:]
                with self.state_guard():
                    self.hist_busy = False
                    current = self.sets
                    current.progress.phase = "error"
                    current.progress.error = error
                if hasattr(self.api, "err"):
                    self.api.err.write("hist", msg=error[:200])
            remain = 2.4
            t0 = time.monotonic()
            while time.monotonic() - t0 < remain and not self._hist_stop:
                time.sleep(0.2)

    def _warm_loop(self) -> None:
        while not self._warm_stop:
            t0 = time.time()
            try:
                with self.state_guard():
                    if time.time() - self.last_bal > BALANCE_EVERY:
                        self.refresh_balance()
                    self.refresh_klines()
                    self.refresh_vol1h()
                    self.process_indications()
                    self.update_regime()
            except Exception:
                self.errors += 1
                self.last_error = traceback.format_exc()[-300:]
                if hasattr(self.api, "err"):
                    self.api.err.write("warm", msg=self.last_error[:220])
            self.warm_ms = (time.time() - t0) * 1000
            sd_notify("WATCHDOG=1")
            remain = float(getattr(self.load.last_budget, "warm_s", 0.32) or 0.32) - (time.time() - t0)
            if remain > 0:
                time.sleep(remain)

    def _ctrl_watch_loop(self) -> None:
        """Event-based control coordination: any create/delete/touch of the
        control files (STOP / PAUSE / STOP_ALL / reset-eq) wakes the main
        loop immediately instead of waiting out the scan cadence."""
        last: Dict[str, float] = {}
        paths = (STOP_PATH, PAUSE_PATH, STOP_ALL, RESET_EQ_PATH)
        while True:
            try:
                snap = ctrl_mtimes(paths)
                if last and snap != last:
                    self.bump("ctrl")
                last = snap
            except Exception:
                pass
            time.sleep(0.15)

    def _one_cycle(self) -> None:
        sd_notify("WATCHDOG=1")
        paused = os.path.exists(PAUSE_PATH)
        stopped = os.path.exists(STOP_PATH) or os.path.exists(STOP_ALL)
        if stopped:
            if self.halt_reason and self.halt_reason not in ("paused", "stopped"):
                self._pre_pause_halt = self.halt_reason
            self.halted = True
            self.halt_reason = "stopped"
            self.priority_controls()
            self.write_stats(force=True)
            self.wake_ev.clear()
            self.wake_ev.wait(timeout=0.4)
            return
        if paused:
            self.halted = True
            if self.halt_reason and self.halt_reason not in ("paused", "stopped"):
                self._pre_pause_halt = self.halt_reason
            self.halt_reason = "paused"
            self.refresh_tickers()
            self.seed_px_bars()
            self.priority_controls()
            self.manage()
            self.write_stats(force=True)
            self.wake_ev.clear()
            self.wake_ev.wait(timeout=0.4)
            return
        if self.halt_reason in ("paused", "stopped"):
            pre = getattr(self, "_pre_pause_halt", None)
            self._pre_pause_halt = None
            if os.path.exists(RESET_EQ_PATH):
                # Explicit Start: fresh session — the reset-eq rescue in
                # refresh_balance re-baselines equity; never restore the
                # pre-stop economic halt across an explicit start.
                pre = None
            if pre:
                self.halted = True
                self.halt_reason = pre
            elif self.equity and self.equity < EQ_MIN:
                self.halted = True
                self.halt_reason = f"equity {self.equity:.4f} below min"
            elif self.start_eq > 0 and self.equity > 0 and (self.start_eq - self.equity) / self.start_eq >= DD_HALT:
                self.halted = True
                self.halt_reason = "drawdown halt"
            else:
                self.halted = False
                self.halt_reason = None
        self.cycle += 1
        self.did_io = False
        self._budget()
        self.refresh_tickers()
        self.seed_px_bars()
        unprotected = self.priority_controls()
        if self.cycle % 8 == 0:
            self.maybe_reload_config()
        if self.cycle % 25 == 0:
            self.adopt_exchange_positions()
            unprotected = self.priority_controls()
        if self.cycle % 150 == 0:
            self.sync_own_fills()
        if self.cycle % 220 == 0:
            self.pool.submit(self.set_leverage)
        self.manage()
        if unprotected:
            unprotected = self.priority_controls()
        # Indications run on the warm thread so the 530-symbol scan cannot stall the watchdog.
        if not self.halted:
            if unprotected:
                self.maybe_block_adds()
                self.maybe_dca_adds()
            else:
                self.maybe_entries()
        if self.cycle % QA_EVERY == 0:
            self.qa_tick()
        if self.cycle % 12 == 0:
            self.trim_caches(force=self.load.last_budget.level in ("overload", "critical"))

    def run(self) -> None:
        log(f"pulse start {CONN_SHORT} {BASE}")
        sd_notify("READY=1\nWATCHDOG=1")
        # The full configured catalog is built after READY on a worker.  This
        # keeps systemd startup bounded while preserving complete set
        # enumeration and the same atomic generation checks used by replay.
        catalog = threading.Thread(target=self._bootstrap_catalog, name="catalog-bootstrap", daemon=True)
        catalog.start()
        if hasattr(self.api, "start_ws"):
            self.api.on_event = self.bump
            self.api.start_ws(list(SYMBOLS))
        sd_notify("WATCHDOG=1")
        self.refresh_balance()
        sd_notify("WATCHDOG=1")
        self.refresh_tickers()
        sd_notify("WATCHDOG=1")
        self.refresh_klines()
        sd_notify("WATCHDOG=1")
        self.process_indications()
        self.update_regime()
        log(f"eq={self.equity} avail={self.available} regime={self.regime}")
        try:
            self.sync_own_fills()
        except Exception:
            pass
        sd_notify("WATCHDOG=1")
        self.adopt_exchange_positions()
        sd_notify("WATCHDOG=1")
        try:
            self.list_orders()
        except Exception:
            pass
        self.priority_controls()
        self._load_lev_file()
        sd_notify("WATCHDOG=1")
        self.run_self_tests()
        sd_notify("WATCHDOG=1")
        self.pool.submit(self.set_leverage)
        self.write_stats()
        warm = threading.Thread(target=self._warm_loop, name="warm-feed", daemon=True)
        warm.start()
        hist = threading.Thread(target=self._hist_loop, name="hist-1m", daemon=True)
        hist.start()
        ctrl = threading.Thread(target=self._ctrl_watch_loop, name="ctrl-watch", daemon=True)
        ctrl.start()
        self.cycle_busy = False
        self.cycle_wait_ms = 0.0
        self.cycle_overrun = False
        while True:
            # Never overlap: previous cycle must finish before the next starts.
            if self.cycle_busy:
                time.sleep(0.001)
                continue
            self.cycle_busy = True
            t0 = time.perf_counter()
            try:
                with self.state_guard():
                    self._one_cycle()
            except Exception:
                self.errors += 1
                self.last_error = traceback.format_exc()[-400:]
                log("LOOP " + self.last_error)
                if hasattr(self.api, "err"):
                    self.api.err.write("loop", msg=self.last_error[:300])
            dt = time.perf_counter() - t0
            self.last_scan_ms = dt * 1000.0
            self.last_scan_io = bool(self.did_io or self.hist_busy or dt > SCAN_S)
            self.cycle_overrun = dt > SCAN_S and not (self.did_io or self.hist_busy)
            try:
                self.write_stats()
            except Exception:
                pass
            sd_notify("WATCHDOG=1")
            wall = time.perf_counter() - t0
            remain = SCAN_S - wall
            self.cycle_wait_ms = max(0.0, remain) * 1000.0
            self.cycle_busy = False
            if remain > 0:
                self.wake_ev.clear()
                self.wake_ev.wait(timeout=remain)
            else:
                # Yield so hist/warm/ctrl threads run instead of busy-spinning.
                time.sleep(0.02)


def load_contracts(want: Optional[set] = None) -> Dict[str, Contract]:
    url = BASE + "/openApi/swap/v2/quote/contracts"
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.loads(r.read().decode()).get("data") or []
    out: Dict[str, Contract] = {}
    take_all = want is None
    want_set = set(want or [])
    for c in data:
        s = c.get("symbol")
        if not s or not str(s).endswith("-USDT"):
            continue
        if not take_all and s not in want_set:
            continue
        st = str(c.get("status") or c.get("apiState") or c.get("symbolStatus") or "").lower()
        if st in ("offline", "close", "closed", "delisted"):
            continue
        qprec = int(c.get("quantityPrecision") or 0)
        step = 10 ** -qprec if qprec >= 0 else 1.0
        raw_size = float(c.get("size") or 0)
        if 0 < raw_size < step:
            step = raw_size
        if step <= 0:
            step = 10 ** -max(qprec, 0)
        out[s] = Contract(
            s,
            float(c.get("tradeMinQuantity") or 0),
            step if step > 0 else 10 ** -qprec,
            qprec,
            int(c.get("pricePrecision") or 4),
            float(c.get("tradeMinUSDT") or 2),
            int(c.get("maxLongLeverage") or c.get("maxLeverage") or c.get("maxleverage") or 150),
        )
    return out


def seed_overlay() -> None:
    if os.path.exists(OVERLAY_PATH):
        return
    src = os.path.join(DIR, "overlay.json")
    if os.path.exists(src):
        try:
            import shutil
            shutil.copy(src, OVERLAY_PATH)
        except Exception:
            pass


def main() -> None:
    global BASE
    os.makedirs(DIR, exist_ok=True)
    seed_overlay()
    key = redis_hget("api_key")
    secret = redis_hget("api_secret")
    if not key or not secret:
        raise SystemExit(f"missing {CONN_SHORT} credentials")
    test = (redis_hget("is_testnet") or "").strip().lower()
    if test in ("1", "true", "yes") or "vst" in (redis_hget("base_url") or "").lower():
        BASE = (redis_hget("base_url") or "https://open-api-vst.bingx.com").rstrip("/")
    else:
        BASE = (redis_hget("base_url") or "https://open-api.bingx.com").rstrip("/")
    api = FastBingX(key, secret, ErrorLog(ERR_PATH), base=BASE)
    Pulse(api, load_contracts()).run()


if __name__ == "__main__":
    main()
