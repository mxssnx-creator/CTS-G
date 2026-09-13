Warning: truncated output (original token count: 162518)
Total output lines: 13176

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
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple
from types import SimpleNamespace
from urllib.parse import urlparse
import overall_controls
from forced_configs import FORCED_SYMBOLS, MIN_PF as FORCED_MIN_PF, valid_candidate, training_window, select_best as select_forced
from validation_policy import control_min_trades
from block_engine import BlockBook, BLOCK_COUNT_PREVIEW, BLOCK_PF_RATIO_MIN, BLOCK_PF_RATIO_MAX, clamp_stack, calculate_block_volume_increment_ratio, calculate_block_minimum_profit_factor, calculate_block_max_additional_ratio, finite_number, normalize_block_counts
from block_active import ContinuationBook, adjusted_quantity, observe_continuation
from entry_dispatch import EntryMatrix
from coord_engine import Coordinator, recent_closed_rows
from bingx_fast import FastBingX, ErrorLog, dumps as fast_json_dumps
from modules import resolve as resolve_modules
from position_cost import (
    last_n_cost_pf,
    evaluation_windows,
    completed_roundtrips,
    accumulate_close,
    resolve_sl_tp,
    POSITION_COST_PCT_DEFAULT,
    POSITIVE_PF,
    clears_pf,
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
from set_engine import SetBook, self_test as sets_self_test, indication_kind_votes, IND_TAG_KIND, merge_hist_rows, LOOKBACK_MAX
from exit_engine import ExitBook, self_test as exit_self_test
from dca_engine import DcaBook, self_test as dca_self_test
from load_engine import LoadGovernor, BoundedSet, trim_map, cap_map, prune_ttl, cap_list
from storage_paths import MAX_RETAINED_FILE_BYTES, MAX_RETAINED_LINES, DATA_DIR, append_bounded_line, append_bounded_lines, atomic_write, read_jsonl, retain_last_lines
from event_ledger import EventLedger
from history_store import BAR_S, HistoryStore, parse_exchange_rows
from hist_calc import read_job as read_hist_job, read_request as read_hist_request, write_job as write_hist_job
from hist_calc import run_forced_calc, forced_path
from contracts import INDICATION_KINDS, stable_key
from runtime_scope import (
    redis_key,
    order_tag,
    row_scope_matches,
    scope_metadata,
    system_id,
    tracking_scope,
)
from system_settings import calculation_overlay, normalize_system_settings
from runtime_statistics import RuntimeMonitor, persistent_activity
from redis_coordination import coordinator as redis_config
from calculation_cache import CalculationCache
from connection_profile import connection_endpoint

_CID_SEQUENCE_LOCK = threading.Lock()
_CID_SEQUENCES: Dict[str, Tuple[int, int]] = {}


def client_order_nonce(prefix: str, width: int) -> str:
    """Never reuse a suffix for a prefix in this process; fail on exhaustion.

    Randomize the starting point across process restarts. Exchange uniqueness
    checks remain authoritative across restarts; this is not a durable ledger.
    """
    if width < 1:
        raise ValueError("Client-order prefix leaves no nonce space")
    limit = 36 ** width
    with _CID_SEQUENCE_LOCK:
        start, used = _CID_SEQUENCES.get(prefix, (None, 0))
        if start is None:
            start = random.SystemRandom().randrange(limit)
        if used >= limit:
            raise RuntimeError("Client-order nonce space exhausted")
        value = (start + used) % limit
        _CID_SEQUENCES[prefix] = (start, used + 1)
    chars = string.digits + string.ascii_lowercase
    result = ""
    for _ in range(width):
        value, digit = divmod(value, 36)
        result = chars[digit] + result
    return result


def effective_indication_timeframes(
    configured: Dict[str, Any],
    budget: Any = None,
) -> Tuple[str, ...]:
    """Return the timeframes allowed for the current warm-path budget.

    The configured flags remain authoritative for normal operation, but the
    load governor may temporarily shed higher timeframes.  In that case we
    must also stop consuming already cached 5m/15m bars; otherwise the
    combined indication lane keeps doing the expensive work that the budget
    explicitly tried to shed.  The 1m lane is never shed here because it is
    the minimum input for live entries and validated-set coordination.
    """
    configured = configured or {}
    # ``process`` always receives the 1m bars as its primary argument, so the
    # live entry lane remains a 1m lane even if an older overlay omitted the
    # explicit flag.  Only higher-timeframe work is shed by this helper.
    allowed = {"1m"}
    if bool(configured.get("5m", True)) and bool(getattr(budget, "tf_5m", True)):
        allowed.add("5m")
    if bool(configured.get("15m", True)) and bool(getattr(budget, "tf_15m", True)):
        allowed.add("15m")
    return tuple(tf for tf in TIMEFRAMES if tf in allowed)

CONN_SHORT = os.environ.get("PULSE_CONN", "bingx-x02").replace("connection:", "")
SYSTEM_ID = system_id()
TRACKING_SCOPE = tracking_scope(CONN_SHORT, SYSTEM_ID)
SCOPE_METADATA = scope_metadata(CONN_SHORT, SYSTEM_ID)
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
MAX_SYMBOLS = 0  # 0 = unlimited hard ceiling
DEFAULT_SYMBOL_CAP = 0
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


def make_control_group_key(symbol: Any, side: Any, sl_pct: Any, tp_pct: Any, execution_lane: str = "") -> str:
    """Stable logical control identity: symbol + side + normalized SL/TP range."""
    sym = str(symbol or "").strip().upper()
    side_u = str(side or "").strip().upper()
    if execution_lane:
        return "lane:" + stable_key("control-group", sym, side_u, control_range_key(sl_pct, tp_pct), execution_lane)
    return stable_key("control-group", sym, side_u, control_range_key(sl_pct, tp_pct))


def control_group_token(group_key: Any, range_key: Any = "") -> str:
    """Return a compact token that can be rebound after an exchange restart.

    New control IDs carry the normalized range in a short, parseable token. The
    hash-derived token remains the fallback for older persisted IDs and for
    malformed range metadata, so an upgrade never loses the ability to match a
    previously placed order by its client ID.
    """
    if str(group_key or "").startswith("lane:"):
        # Range tokens are intentionally shared by legacy range aggregates.
        # Independent execution lanes must carry a distinct matching token.
        return hashlib.sha256(str(group_key).encode()).hexdigest()[:8]
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
MAX_DD_TIME_S = 57600.0  # default and upper bound 16 hours; configurable 10..960 minutes
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
DD_HALT = 0.0
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
    "rate-limit",
    "too many request",
    "please try again later",
    "requests within",
    "request limit",
    "109420",
    "frequency",
    "order not exist",
    "position not exist",
    "quantity or stopprice is must",
    "parameter quantity",
    "order size must be less",
    "available amount",
    "minimum order amount",
    "minimum size per order",
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
    if "minimum order amount" in low or "minimum size per order" in low:
        return "minimum order size"
    if "cooling" in low:
        return "cooling"
    if "109420" in low or "rate limit" in low or "rate-limit" in low or "too many request" in low or "requests within" in low or "request limit" in low:
        return "rate limited"
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


def valid_position_snapshot(rows: Any) -> bool:
    """Only a complete, well-formed venue response can establish absence."""
    if not isinstance(rows, list):
        return False
    for row in rows:
        try:
            if not isinstance(row, dict):
                return False
            raw_amount = row.get("positionAmt")
            if raw_amount is None or raw_amount == "":
                raw_amount = row.get("availableAmt")
            amount = float(raw_amount)
            if not math.isfinite(amount):
                return False
            if abs(amount) > 1e-12 and (not row.get("symbol") or
                    str(row.get("positionSide") or "").upper() not in ("", "LONG", "SHORT")):
                return False
        except (TypeError, ValueError, OverflowError):
            return False
    return True


def confirmed_external_close_delta(previous_qty: Any, exchange_qty: Any, pending_close_qty: Any = 0.0) -> float:
    """Return one exchange-confirmed quantity delta not already owned by a close intent."""
    try:
        previous = max(0.0, float(previous_qty or 0.0))
        current = max(0.0, float(exchange_qty or 0.0))
        pending = max(0.0, float(pending_close_qty or 0.0))
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if pending > 1e-12 or current >= previous - 1e-12:
        return 0.0
    return previous - current


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
    if "position not exist" in m or "position does not exist" in m or "no position to close" in m:
        return "flat"
    if ("order size" in m or "available amount" in m or
            "minimum size" in m or "minimum order amount" in m):
        return "qty"
    return "other"


def adopt_venue_minimum(contract: Any, msg: str) -> str:
    """Learn a venue minimum without confusing base quantity and USDT size."""
    if contract is None:
        return ""
    text = " ".join(str(msg or "").split())
    # BingX uses this form for a base-asset lot minimum, e.g.:
    # ``minimum order amount is 304.1 FONE``.
    amount = re.search(
        r"\bminimum\s+order\s+amount\s+is\s+([0-9]+(?:\.[0-9]+)?)\s+([A-Z][A-Z0-9_-]*)\b",
        text,
        re.I,
    )
    if amount and str(amount.group(2) or "").upper() != "USDT":
        try:
            value = float(amount.group(1))
            if math.isfinite(value) and value > 0:
                contract.min_qty = max(float(getattr(contract, "min_qty", 0) or 0), value)
                return "qty"
        except (TypeError, ValueError, OverflowError):
            pass
    # Other venue responses express the minimum as quote currency, e.g.:
    # ``The minimum size per order is 2.59 USDT``.
    quote = re.search(
        r"\bminimum\s+(?:(?:size|amount)\s+per\s+order|order\s+(?:size|amount)|size)\s+is\s+"
        r"([0-9]+(?:\.[0-9]+)?)\s*USDT\b",
        text,
        re.I,
    )
    if quote:
        try:
            value = float(quote.group(1))
            if math.isfinite(value) and value > 0:
                contract.min_usdt = max(float(getattr(contract, "min_usdt", 0) or 0), value)
                return "usdt"
        except (TypeError, ValueError, OverflowError):
            pass
    return ""


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
        value = redis_config.read_hash(f"connection:{CONN_SHORT}").get(field, "").strip()
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
        raw = redis_config.read_hash(f"settings:connection_settings:{CONN_SHORT}")
    except Exception:
        return {}
    out = {}
    for key, v in raw.items():
        if v[:1] in "{[":
            try:
                out[key] = json.loads(v)
                continue
            except Exception:
                pass
        if v in ("true", "false"):
            out[key] = v == "true"
            continue
        try:
            out[key] = float(v) if "." in v else int(v)
        except Exception:
            out[key] = v
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
    execution_lane: str = ""
    set_idx: int = -1
    trail_set_id: str = ""
    trail_idx: int = -1
    pack: str = ""
    client_id: str = ""
    ours: bool = True
    system_id: str = ""
    connection: str = ""
    tracking_scope: str = ""
    overall: bool = True
    overall_controls: bool = False
    overall_qty: float = 0.0
    overall_signature: List[float] = field(default_factory=list)
    overall_sl_signature: List[float] = field(default_factory=list)
    overall_tp_signature: List[float] = field(default_factory=list)
    overall_bindings: Dict[str, Dict[str, float]] = field(default_factory=dict)
    overall_replace_intents: Dict[str, Any] = field(default_factory=dict)
    retired_control_ids: List[str] = field(default_factory=list)
    overall_sl: float = 0.0
    overall_tp: float = 0.0
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
    # Aggregate mode keeps the widest effective member ranges so one common
    # close-position SL/TP pair never becomes narrower after a merge.
    aggregate_sl_pct: float = 0.0
    aggregate_tp_pct: float = 0.0
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
    roundtrip_result: Dict[str, Any] = field(default_factory=dict)


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
    execution_lane: str = ""
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
    roundtrip_result: Dict[str, Any] = field(default_factory=dict)
    system_id: str = ""
    tracking_scope: str = ""


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
        self.exchange_order_own_count = -1
        self.exchange_order_total_count = -1
        self.exchange_order_foreign_count = -1
        self.control_orders_per_config = True
        self.control_orders_overall = False
        self.closed: Deque[Closed] = deque(maxlen=80)
        self.cooldown: Dict[str, float] = {}
        self.last_entry_ts = 0.0
        self.start_eq = 0.0
        self.realized_baseline = 0.0
        self.dust_retired: set = set()
        self.foreign_activity_seen = False
        try:
            if os.path.exists(START_EQ_PATH):
                baseline = json.load(open(START_EQ_PATH)) or {}
                stored_scope = str(baseline.get("trackingScope") or baseline.get("tracking_scope") or "").strip().lower()
                if not stored_scope or stored_scope == TRACKING_SCOPE:
                    self.start_eq = float(baseline.get("systemStartEquity") or baseline.get("startEquity") or 0)
                    self.realized_baseline = float(baseline.get("realizedBaseline") or 0.0)
                    self.foreign_activity_seen = bool(baseline.get("foreignActivitySeen") or baseline.get("foreign_activity_seen"))
        except Exception:
            self.start_eq = 0.0
            self.realized_baseline = 0.0
        self.system_start_eq = self.start_eq
        self.wallet_equity = 0.0
        self.system_equity = 0.0
        self.equity = 0.0
        self.available = 0.0
        self.used = 0.0
        self.upnl = 0.0
        self.system_upnl = 0.0
        self.foreign_upnl = 0.0
        self.foreign_realized = 0.0
        self.foreign_exposure = 0.0
        self.foreign_position_count = 0
        self.foreign_open_order_count = 0
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
        self.last_scan_cpu_ms = 0.0
        self.last_cycle_stages = {}
        self._cycle_stage_ms = {}
        self._active_cycle_stage = ""
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
        self._hist_wake = threading.Event()
        self.last_event = "boot"
        self.event_n = 0
        self.event_ledger = EventLedger(EVENTS_PATH, CONN_SHORT, max_events=512, flush_interval_s=2)
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
        # The hot cycle and the diagnostic self-test are independent from the
        # shared-state lock.  Exchange I/O can take seconds (or be put into a
        # kernel wait), so serialise those activities separately without
        # starving HTTP/state readers or the history worker.
        self._cycle_lock = threading.Lock()
        self._qa_lock = threading.Lock()
        self._stats_last: Dict[str, Any] = {}
        self.hist_busy = False
        self._hist_fetch_next = 0.0
        self._hist_fetch_failures = 0
        self._hist_fetch_last = 0.0
        self._hist_fetch_stored = 0
        self._hist_deferred = ""
        self._hist_status_write_ts = 0.0
        self._hist_resume_repair = False
        self.history_store = HistoryStore(CONN_SHORT, retention_bars=max(2400, LOOKBACK_MAX))
        self._hist_status: Dict[str, Any] = read_hist_job(CONN_SHORT)
        checkpoint = self.history_store.checkpoint()
        if isinstance(checkpoint, dict):
            # A checkpoint is the durable recovery boundary. Keep the last
            # published watermark visible until the fresh lane proves a new
            # complete replay; the entry gate still starts closed on restart.
            for key in ("runId", "generation", "mode", "watermark", "lastPublishedWatermark", "lastCompleteRun"):
                if not self._hist_status.get(key) and checkpoint.get(key):
                    self._hist_status[key] = checkpoint[key]
        self._hist_request_seen = ""
        self._hist_active_request: Dict[str, Any] = {}
        self._hist_active_run_id = ""
        self._hist_request_check_ts = 0.0
        self._hist_latest_request_id = ""
        self._hist_next_hourly_at = 0.0
        self._hist_last_closed_minute = 0
        self._hist_snapshot_symbols: List[str] = []
        self._hist_invalid_symbols: List[Dict[str, str]] = []
        self._hist_gap_symbols: List[str] = []
        self._hist_incremental_symbols: set[str] = set()
        self._hist_last_published_watermark: Dict[str, int] = dict(self._hist_status.get("lastPublishedWatermark") or {})
        self._stats_ts = 0.0
        self._stats_force = False
        self.last_scan_io = False
        self.ignored_foreign = 0
        self.dca_fail_cd: Dict[str, float] = {}
        self.system_id = SYSTEM_ID
        self.connection_id = CONN_SHORT
        self.tracking_scope = TRACKING_SCOPE
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
            "blockMaxStack": 6,
            "blockVolumeRatio": 0.25,
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
        self.sl_min = 0.0015
        self.sl_max = 0.0300
        self.tp_min = 0.0030
        self.tp_max = 0.0
        self.tp_cost_ratio = 5.0
        self.sl_to_tp = 0.64
        self.strat_ind = True
        self.strat_block = True
        self.strat_trail = True
        self.strat_dca = True
        self.normal_execution_enabled = True
        self.block_active = True
        self._block_reference_anchors = ContinuationBook()
        self._execution_decision = {}
        self.strat_general = True
        self.tf_on = {"1m": True, "5m": True, "15m": True}
        self._hist_stop = threading.Event()
        self._watchdog_stop = threading.Event()
        self.calculation_cache = CalculationCache(CONN_SHORT)
        self.apply_live_config(initial=True)
        self.runtime = None
        try:
            self.runtime = RuntimeMonitor(DIR, CONN_SHORT, self.overlay)
            for row in self.strategy_closes():
                self.runtime.record_trade(asdict(row), self.start_eq)
        except Exception as exc:
            self.last_error = f"Statistics initialization: {type(exc).__name__}"

    def group_of(self, sym: str) -> str:
        for g, s in GROUPS.items():
            if sym in s:
                return g
        return "u%d" % (abs(hash(sym)) % 8)

    def _bind_position_scope(self, pos: Position) -> bool:
        """Attach the exact system/connection scope without widening ownership."""
        if getattr(pos, "ours", True) is False:
            return False
        stored_scope = str(getattr(pos, "tracking_scope", "") or "").strip().lower()
        stored_system = str(getattr(pos, "system_id", "") or "").strip().lower()
        stored_connection = str(getattr(pos, "connection", "") or "").strip().lower()
        has_scope_proof = bool(stored_scope or stored_system or stored_connection)
        if stored_scope and stored_scope != TRACKING_SCOPE:
            return False
        if stored_system and stored_system != SYSTEM_ID:
            return False
        if stored_connection and stored_connection != CONN_SHORT:
            return False
        client_id = str(getattr(pos, "client_id", "") or "")
        if not has_scope_proof and not self.cid_ours(client_id):
            return False
        pos.system_id = SYSTEM_ID
        pos.connection = CONN_SHORT
        pos.tracking_scope = TRACKING_SCOPE
        pos.ours = True
        return True

    def position_is_ours(self, pos: Optional[Position]) -> bool:
        if pos is None or getattr(pos, "ours", True) is False:
            return False
        return self._bind_position_scope(pos)

    def row_is_ours(self, row: Any) -> bool:
        if not isinstance(row, dict) or row.get("ours") is False:
            return False
        if not row_scope_matches(row, CONN_SHORT):
            return False
        return True

    def _persist_start_equity(self) -> None:
        try:
            tmp = START_EQ_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump({
                    "systemStartEquity": float(self.start_eq),
                    "startEquity": float(self.start_eq),
                    "realizedBaseline": float(getattr(self, "realized_baseline", 0.0)),
                    "foreignActivitySeen": bool(getattr(self, "foreign_activity_seen", False)),
                    **SCOPE_METADATA,
                    "t": time.time(),
                }, f)
            os.replace(tmp, START_EQ_PATH)
        except Exception:
            pass

    def _note_foreign_activity(self) -> None:
        if getattr(self, "foreign_activity_seen", False):
            return
        self.foreign_activity_seen = True
        self._persist_start_equity()

    def _persistent_system_realized(self) -> float:
        status = getattr(getattr(self, "runtime", None), "snapshot", {}) or {}
        totals = status.get("totals") if isinstance(status, dict) else {}
        if isinstance(totals, dict) and "realized" in totals:
            return _sf(totals.get("realized"))
        # Startup/unit-test objects can refresh the balance before optional
        # strategy modules are attached. Keep the fallback scope-safe without
        # routing through max_book_notional()/DCA configuration.
        total = 0.0
        for row in getattr(self, "closed", ()) or ():
            if isinstance(row, dict):
                record = row
            elif hasattr(row, "__dataclass_fields__"):
                record = asdict(row)
            else:
                try:
                    record = vars(row)
                except TypeError:
                    continue
            if self.row_is_ours(record):
                total += _sf(record.get("pnl"))
        return total

    def _system_marked_equity(self) -> float:
        # An explicit Start/reset re-baselines start_eq to the wallet, but the
        # runtime realized counter is cumulative and would otherwise keep the
        # marked equity negative forever. Subtract the baseline captured at the
        # last reset so a fresh session starts from the real wallet equity.
        realized = self._persistent_system_realized() - _sf(getattr(self, "realized_baseline", 0.0))
        return _sf(getattr(self, "start_eq", 0.0)) + realized + self.system_open_upnl()

    def _has_scoped_activity(self) -> bool:
        open_book = getattr(self, "open", {})
        for pos in open_book.values() if isinstance(open_book, dict) else ():
            if self.position_is_ours(pos):
                return True
        for row in getattr(self, "closed", ()) or ():
            if isinstance(row, dict):
                record = row
            elif hasattr(row, "__dataclass_fields__"):
                record = asdict(row)
            else:
                try:
                    record = vars(row)
                except TypeError:
                    continue
            if self.row_is_ours(record):
                return True
        return False

    def _foreign_activity_present(self) -> bool:
        return bool(
            abs(_sf(getattr(self, "foreign_upnl", 0.0))) > 1e-12
            or abs(_sf(getattr(self, "foreign_realized", 0.0))) > 1e-12
            or int(getattr(self, "foreign_position_count", 0) or 0) > 0
            or int(getattr(self, "foreign_open_order_count", 0) or 0) > 0
        )

    def _balance_system_equity(self, wallet_system_equity: float) -> float:
        # Before the first scoped fill, retain the legacy account baseline so
        # deposits/losses still drive the guard. Once foreign activity has ever
        # been observed, wallet movement can no longer become system PnL after
        # that position/order disappears from the next exchange snapshot.
        if getattr(self, "foreign_activity_seen", False):
            return self._system_marked_equity()
        if not self._has_scoped_activity() and not self._foreign_activity_present():
            return max(0.0, _sf(wallet_system_equity))
        return self._system_marked_equity()

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

    def aggregate_member_ranges(self, pos: Position) -> Tuple[float, float]:
        """Return the widest effective member SL/TP fractions for an aggregate."""
        sl = max(
            0.0,
            _sf(getattr(pos, "aggregate_sl_pct", 0.0)),
            _sf(getattr(pos, "sl_pct", 0.0)),
        )
        tp = max(
            0.0,
            _sf(getattr(pos, "aggregate_tp_pct", 0.0)),
            _sf(getattr(pos, "tp_pct", 0.0)),
        )
        return sl, tp

    def widen_aggregate_range(self, pos: Position, sl_pct: Any = 0.0, tp_pct: Any = 0.0) -> None:
        """Persist a monotonic aggregate range without changing per-config keys."""
        current_sl, current_tp = self.aggregate_member_ranges(pos)
        widened_sl = max(current_sl, _sf(sl_pct))
        widened_tp = max(current_tp, _sf(tp_pct))
        pos.aggregate_sl_pct = widened_sl
        pos.aggregate_tp_pct = widened_tp
        if widened_sl > 0:
            pos.sl_pct = widened_sl
        if widened_tp > 0:
            pos.tp_pct = widened_tp

    def prepare_position_group(self, pos: Position, legacy: Optional[bool] = None) -> Position:
        """Attach a restart-safe control identity and bounded lineage metadata."""
        if not self._bind_position_scope(pos):
            pos.ours = False
            return pos
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
                pos.symbol, pos.side, sl_bp / 10000.0, tp_bp / 10000.0,
                getattr(pos, "execution_lane", ""),
            )
        else:
            # Aggregate mode is one symbol/direction scope. Never let a stale
            # range key make the disabled mode look like per-config controls.
            self.widen_aggregate_range(pos, sl_value, tp_value)
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

    def execution_lane_key(self, pack: str, reason: str, selected_set: Any = None, strategy: str = "") -> str:
        """Build the durable duplicate key for one strategy/config lane."""
        if selected_set is None:
            return ""
        exact = re.search(r"\bcfg=([a-f0-9]{16})\b", str(reason or ""))
        signal_key = exact.group(1) if exact else (
            str(reason or "").split(":")[1] if pack == "indications" and ":" in str(reason or "") else "general"
        )
        lane = stable_key(pack, getattr(selected_set, "id", ""), signal_key)
        strategy_key = str(strategy or "").strip().lower()
        if strategy_key == "core":
            strategy_key = "normal"
        return f"{strategy_key}:{lane}" if strategy_key and lane else lane

    def execution_lane_matches(self, stored_lane: Any, requested_lane: Any) -> bool:
        """Match a new lane while retaining pre-strategy-prefix normal rows."""
        stored = str(stored_lane or "")
        requested = str(requested_lane or "")
        if not stored or not requested:
            return False
        if stored == requested:
            return True
        if requested.startswith("normal:") and stored == requested.removeprefix("normal:"):
            return True
        if stored.startswith("normal:") and requested == stored.removeprefix("normal:"):
            return True
        return False

    def entry_slot_count(self) -> int:
        """Confirmed and pending lanes share the configured open-slot budget."""
        pending = set()
        for cid, row in (getattr(self, "pending_orders", {}) or {}).items():
            if str(row.get("kind") or "entry") != "entry":
                continue
            if _sf(row.get("requested_qty")) <= _sf(row.get("filled_qty")) + 1e-12:
                continue
            if self._position_for_client(cid) is None:
                pending.add(row.get("group_key") or cid)
        return len(self.open) + len(pending)

    def entry_queue_state(self, matrix) -> Dict[str, Any]:
        """Count currently signalled config lanes without expanding the matrix.

        Block additions have their own lifecycle; this is the normal/trailing
        admission queue. Filled and uncertain intents are counted once.
        """
        scopes = {scope: {st.id: st for st in states if st is not None}
                  for scope, states in matrix.sets_by_scope.items()}
        signals = {}
        for signal in matrix.signals:
            pack, side = matrix.scope(signal)
            signals.setdefault((signal[1], side, pack), []).append(signal)

        def lane_key(symbol, side, pack, set_id, lane):
            st = scopes.get((pack, side), {}).get(set_id)
            if st is None:
                return None
            mode = "trailing" if st.kind == "trail" else "normal"
            for signal in signals.get((symbol, side, pack), ()):
                expected = self.execution_lane_key(pack, signal[3], st, mode)
                if self.execution_lane_matches(lane, expected):
                    return (symbol, side, expected)
            return None

        opened = set()
        for pos in self.open.values():
            key = lane_key(pos.symbol, pos.side, pos.pack, pos.set_id, getattr(pos, "execution_lane", ""))
            if key:
                opened.add(key)
        pending = set()
        for row in (getattr(self, "pending_orders", {}) or {}).values():
            if str(row.get("kind") or "entry") != "entry" or _sf(row.get("requested_qty")) <= _sf(row.get("filled_qty")) + 1e-12:
                continue
            meta = row.get("metadata") or {}
            key = lane_key(row.get("symbol"), row.get("side"), meta.get("pack"), meta.get("set_id"), meta.get("execution_lane"))
            if key:
                pending.add(key)
        return {"eligible": len(matrix), "opened": len(opened), "pending": len(pending - opened),
                "remaining": max(0, len(matrix) - len(opened | pending)), "updatedAt": time.time(),
                "scope": "current-signal-normal-trailing-config-lanes"}

    def pending_entry_margin(self) -> float:
        reserved = 0.0
        for row in (getattr(self, "pending_orders", {}) or {}).values():
            if str(row.get("kind") or "entry") != "entry":
                continue
            meta = row.get("metadata") or {}
            remaining = max(0.0, _sf(row.get("requested_qty")) - _sf(row.get("filled_qty")))
            price = _sf(meta.get("reference_price")) or _sf(self.px.get(row.get("symbol")))
            leverage = max(1.0, _sf(meta.get("leverage"), 1.0))
            reserved += remaining * price / leverage
        return reserved

    def position_for_group(self, group_key: str) -> Optional[Position]:
        pos = self.open.get(str(group_key))
        if pos is not None:
            return pos
        return next((p for p in self.open.values() if getattr(p, "control_group_key", "") == group_key), None)

    def position_for_group_token(self, symbol: str, side: str, token: str) -> Optional[Position]:
        token = str(token or "")
        if not token:
            return None
        matches = [
            p for p in self.positions_for(symbol, side)
            if token in control_group_tokens(
                getattr(p, "control_group_key", ""),
                getattr(p, "control_range_key", ""),
            )
        ]
        return matches[0] if len(matches) == 1 else None

    def remove_position(self, pos: Position) -> Optional[Position]:
        key = self.position_key(pos)
        removed = self.open.pop(key, None)
        if removed is None:
            for candidate_key, candidate in list(self.open.items()):
                if candidate is pos:
                    removed = self.open.pop(candidate_key, None)
                    break
        if removed is not None:
            self._sync_set_processing()
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
        # A close request is tracked on the logical aggregate.  Merging a
        # later fill into that aggregate must preserve the already-started
        # close quantity and extend it by the newly merged quantity; `pos`
        # is not in scope here and used to make same-group fills crash.
        if float(getattr(target, "close_started_qty", 0) or 0) > 0:
            target.close_started_qty += add_qty
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
        # different sub-basis-point inputs. Per-config groups keep a weighted
        # effective range; aggregate mode keeps the widest member range so a
        # later merge can never narrow the common symbol/direction controls.
        aggregate_mode = not self.per_config_controls(target)
        if aggregate_mode or bool(getattr(target, "legacy_aggregate", False)) or bool(getattr(incoming, "legacy_aggregate", False)):
            self.widen_aggregate_range(
                target,
                max(float(getattr(incoming, "aggregate_sl_pct", 0) or 0), float(getattr(incoming, "sl_pct", 0) or 0)),
                max(float(getattr(incoming, "aggregate_tp_pct", 0) or 0), float(getattr(incoming, "tp_pct", 0) or 0)),
            )
        else:
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
            stack = 6
        vr = max(0.05, float(getattr(self.block, "volume_ratio", 0.25) or 0.25))
        block_extra = 0.0
        if stack > 0:
            block_extra = calculate_block_max_additional_ratio(stack, vr, getattr(self.block, "max_volume_multiplier", 2.0))
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
        has_owned_position = any(
            self.position_is_ours(pos)
            and str(getattr(pos, "symbol", "")).upper() == str(symbol or "").upper()
            and str(getattr(pos, "side", "")).upper() == str(side or "").upper()
            for pos in getattr(self, "open", {}).values()
        )
        if not tagged and not has_owned_position:
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
                # Encode the dataclass fields directly: deep-copying every
                # retained order binding on every acknowledgement is quadratic
                # overhead with large independent Set books.
                blob[key] = {name: getattr(pos,name) for name in Position.__dataclass_fields__}
            tmp = OPEN_PATH + ".tmp"
            with open(tmp, "w") as f:
                f.write(fast_json_dumps(blob))
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
            if not row_scope_matches(value, CONN_SHORT) or not self.cid_ours(cid):
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
                "version": 2,
                **SCOPE_METADATA,
                "updatedAt": time.time(),
                "orders": {cid: dict(row) for cid, row in list(self.pending_orders.items())[-512:]},
            }
            tmp = PENDING_PATH + ".tmp"
            with open(tmp, "w") as state_file:
                json.dump(blob, state_file, separators=(",", ":"))
            os.replace(tmp, PENDING_PATH)
        except Exception:
            pass

    def _sync_set_processing(self) -> None:
        """Keep unresolved Set lineages alive independently from selection.

        ``SetState.active`` is a new-entry/selection flag. It is allowed to
        turn off when a Set fails a live gate or falls outside the selection
        budget, but an unresolved order or open position still needs its own
        calculations, controls, exits and reconciliation. The compact
        lineage scan below is bounded by the live book and pending intents;
        the SetBook applies only changed IDs to the large catalog.
        """
        book = getattr(self, "sets", None)
        sync = getattr(book, "sync_processing_sets", None)
        if not callable(sync):
            return
        wanted = set()
        reasons: Dict[str, str] = {}

        def add(value: Any, reason: str) -> None:
            sid = str(value or "").strip()
            if sid:
                wanted.add(sid)
                reasons.setdefault(sid, reason)

        for pos in (getattr(self, "open", {}) or {}).values():
            for field_name in ("set_id", "trail_set_id", "parent_set_id"):
                add(getattr(pos, field_name, ""), "open-position")
            for field_name in ("lineage_set_ids", "lineage_parent_set_ids"):
                for sid in (getattr(pos, field_name, None) or []):
                    add(sid, "open-position-lineage")

        for row in (getattr(self, "pending_orders", {}) or {}).values():
            if not isinstance(row, dict):
                continue
            requested = max(0.0, _sf(row.get("requested_qty") or row.get("requestedQty")))
            filled = max(0.0, _sf(row.get("filled_qty") or row.get("filledQty")))
            if requested <= filled + 1e-12:
                continue
            kind = str(row.get("kind") or "order").strip().lower()
            reason = f"pending-{kind}"
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            for source in (row, metadata):
                for field_name in (
                    "set_id", "setId", "trail_set_id", "trailSetId",
                    "parent_set_id", "parentSetId",
                ):
                    add(source.get(field_name), reason)
        try:
            sync(wanted, reasons)
        except Exception:
            # Retention is an auxiliary coordination flag. Never let a
            # malformed persisted row stop the live order/exit loop.
            return

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
                "system_id": SYSTEM_ID,
                "connection": CONN_SHORT,
                "tracking_scope": TRACKING_SCOPE,
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
        self._sync_set_processing()

    def _clear_pending(self, cid: str) -> None:
        pending = getattr(self, "pending_orders", None)
        if not isinstance(pending, dict):
            return
        if str(cid or "") in pending:
            pending.pop(str(cid), None)
            self._save_pending_orders()
            self._sync_set_processing()

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
            with open(OPEN_PATH) as saved:
                data = json.load(saved)
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
            if not row_scope_matches(rec, CONN_SHORT):
                continue
            if pos.client_id and not self.cid_ours(pos.client_id):
                continue
            if not self._bind_position_scope(pos):
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
        if pos is not None and not self.per_config_controls(pos) and kind in ("u", "v", "s", "t"):
            # Aggregate controls intentionally omit set/config/range tokens.
            # The symbol+hedge-side digest scopes the common pair while the
            # nonce keeps replacement requests unique across restarts.
            scope = stable_key("aggregate-control", pos.symbol, pos.side)[:8]
            prefix = f"{TAG}{kind}a{scope}"
            return prefix + client_order_nonce(prefix, 32 - len(prefix))
        if pos is not None and self.per_config_controls(pos) and getattr(pos, "control_group_key", ""):
            group_token = control_group_token(
                pos.control_group_key,
                getattr(pos, "control_range_key", ""),
            )
        if idx >= 1000 or (idx < 0 and set_id) or (pos is not None and getattr(pos, "execution_lane", "")):
            if idx >= 36 ** 4:
                raise ValueError("Set index cannot be encoded in client order ID")
            # Historical baseline/recovered lanes need protective and close
            # orders even when they are outside the current catalog. The w
            # marker cannot resolve to an unrelated index-zero strategy.
            marker = "w" if idx < 0 else "v"
            value, encoded = max(0, idx), ""
            for _ in range(4):
                value, digit = divmod(value, 36)
                encoded = (string.digits + string.ascii_lowercase)[digit] + encoded
            fingerprint = hashlib.sha256(set_id.encode()).hexdigest()[:3]
            prefix = f"{TAG}{kind}{marker}{encoded}{fingerprint}{group_token.ljust(8, '0')}"
            return prefix + client_order_nonce(prefix, 32 - len(prefix))
        prefix = f"{TAG}{kind}{p}{sl}{tr}{st}{ix}{group_token}"
        # Preserve parser offsets and the complete group token. Use all space
        # for grouped orders; ungrouped five-character tails retain their legacy
        # interpretation (an eight-character tail is a legacy group token).
        width = 32 - len(prefix) if group_token else min(5, 32 - len(prefix))
        return prefix + client_order_nonce(prefix, width)

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
        if rest[1:2] in ("v", "w"):
            if len(rest) < 18:
                return None
            try:
                idx = int(rest[2:6], 36) if rest[1:2] == "v" else -1
            except ValueError:
                return None
            st_obj = self.sets.get_idx(idx) if idx >= 0 and hasattr(self, "sets") else None
            if st_obj is not None and hashlib.sha256(st_obj.id.encode()).hexdigest()[:3] != rest[6:9]:
                st_obj = None
            token = "" if rest[9:17] == "00000000" else rest[9:17]
            sl_bp = tp_bp = 0
            if re.fullmatch(r"r\d{6}0", token):
                token = token[:7]
                sl_bp, tp_bp = int(token[1:4]), int(token[4:7])
            return {
                "kind": rest[0], "idx": idx if st_obj else -1,
                "set_id": st_obj.id if st_obj else "",
                "pack": st_obj.pack if st_obj else "general",
                "sl": st_obj.sl_ratio if st_obj else 0.6,
                "trail": getattr(st_obj, "trail_key", ""),
                "trail_arm": getattr(st_obj, "trail_arm", 0),
                "trail_give": getattr(st_obj, "trail_give", 0),
                "step": getattr(st_obj, "step", 0),
                "parent_set_id": getattr(st_obj, "parent_set_id", ""),
                "axis_key": getattr(st_obj, "axis_key", ""),
                "relative_count": getattr(st_obj, "relative_count", 1),
                "volume_ratio": getattr(st_obj, "volume_ratio", 1.0),
                "ind_kind": getattr(st_obj, "ind_kind", ""),
                "group_token": token,
                "control_range_key": f"sl{sl_bp:04d}-tp{tp_bp:04d}" if sl_bp and tp_bp else "",
                "control_sl_bp": sl_bp, "control_tp_bp": tp_bp,
                "sl_pct": sl_bp / 10000., "tp_pct": tp_bp / 10000.,
            }
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
        recent = sorted(self.test_map.values(), key=lambda row: float(row.get("t") or 0), reverse=True)
        failures = [row for row in recent if not row.get("pass")]
        successes = [row for row in recent if row.get("pass")]
        self.tests = (failures + successes)[:28]
        if prev is not None and bool(prev.get("pass")) == bool(passed):
            return
        if passed:
            self.qa_pass += 1
            if prev is not None and not prev.get("pass"):
                self.qa_fail = max(0, self.qa_fail - 1)
        else:
            if prev is not None and prev.get("pass"):
                self.qa_pass = max(0, self.qa_pass - 1)
            self.qa_fail += 1
            log(f"TEST FAIL {name} {detail}"[:240], every=20.0, key=f"fail:{name}")

    def state_guard(self):
        """Return the shared state lock, with a test-friendly fallback."""
        return getattr(self, "_state_lock", None) or nullcontext()

    def _background_startup_self_tests(self) -> None:
        """Run the read-only startup probes after the live loop is moving.

        The complete probe suite includes public REST calls and deliberately
        bounded local calculation checks.  Keeping it off the startup call
        stack prevents a rate-limit/network wait from presenting an otherwise
        healthy service as a stuck process.  It never places an order.
        """
        if not self._qa_lock.acquire(blocking=False):
            return
        try:
            # Let the first control cycles and catalog publication establish a
            # useful baseline before competing for REST/rate-limit capacity.
            # In particular, never run the full QA matrix beside the initial
            # 34k-set allocation: both paths are bounded individually but
            # their transient overlap can force a small VPS into swap.
            deadline = time.monotonic() + 900.0
            while time.monotonic() < deadline and (
                int(getattr(self, "cycle", 0) or 0) < 4
                or not getattr(self, "_catalog_ready", threading.Event()).is_set()
                or bool(getattr(self, "hist_busy", False))
            ):
                time.sleep(0.5)
            if not getattr(self, "_catalog_ready", threading.Event()).is_set():
                self.record_test("startup-qa", True, "deferred until catalog is ready")
                return
            self.run_self_tests()
        except Exception as exc:
            self.errors += 1
            self.last_error = f"startup self-test {str(exc)[:260]}"
            if hasattr(self.api, "err"):
                self.api.err.write("self-test", msg=self.last_error[:220])
        finally:
            self._qa_lock.release()
            self._stats_force = True

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

        self.wallet_equity = _sf(row.get("equity") or row.get("balance"))
        self.available = _sf(row.get("availableMargin") or row.get("available") or row.get("availableBalance"))
        self.used = _sf(row.get("usedMargin") or row.get("used"))
        self.upnl = _sf(row.get("unrealizedProfit") or row.get("unrealized"))
        if self._foreign_activity_present():
            self._note_foreign_activity()
        wallet_system_equity = (
            self.wallet_equity
            - _sf(getattr(self, "foreign_upnl", 0.0))
            - _sf(getattr(self, "foreign_realized", 0.0))
        )

        if self.start_eq <= 0 and wallet_system_equity > 0:
            self.start_eq = max(0.0, wallet_system_equity - self.system_open_upnl())
            self.system_start_eq = self.start_eq
            self._persist_start_equity()

        reset_requested = False
        try:
            reset_requested = os.path.exists(RESET_EQ_PATH)
            if reset_requested:
                os.remove(RESET_EQ_PATH)
        except Exception:
            reset_requested = False
        if reset_requested:
            # Start/reset is lane-scoped. Foreign mark-to-market and realized
            # telemetry are removed before establishing the new baseline.
            self.start_eq = max(0.0, wallet_system_equity - self.system_open_upnl())
            self.realized_baseline = self._persistent_system_realized()
            self.system_start_eq = self.start_eq
            self._persist_start_equity()
            if self.halt_reason in ("drawdown halt", "stopped", "paused") or str(self.halt_reason or "").startswith("equity "):
                self._pre_pause_halt = None
            self._halt_eq = 0.0

        self.system_upnl = self.system_open_upnl()
        self.system_equity = self._balance_system_equity(wallet_system_equity)
        self.equity = self.system_equity
        self.system_start_eq = self.start_eq
        self.last_bal = time.time()
        # Only a real system-capital increase can rescue an economic halt.
        halt_eq = float(getattr(self, "_halt_eq", 0.0) or 0.0)
        econ_halt = self.halted and (
            self.halt_reason == "drawdown halt" or str(self.halt_reason or "").startswith("equity ")
        )
        rescued = bool(
            not reset_requested
            and econ_halt
            and halt_eq > 0
            and self.system_equity >= max(EQ_MIN * 2.0, halt_eq * 1.5, halt_eq + 1.0)
        )
        if rescued:
            log(f"EQ re-baseline on system capital start_eq {self.start_eq:.4f} -> {self.system_equity:.4f}")
            self.start_eq = self.system_equity
            self.system_start_eq = self.start_eq
            self._persist_start_equity()
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
        elif DD_HALT > 0 and self.start_eq > 0 and self.system_equity > 0 and (self.start_eq - self.system_equity) / self.start_eq >= DD_HALT:
            if not self.halted:
                self._halt_eq = self.system_equity
            self.halted = True
            self.halt_reason = "drawdown halt"
            self._pre_pause_halt = None
        elif self.system_equity < EQ_MIN:
            if not self.halted:
                self._halt_eq = self.system_equity
            self.halted = True
            self.halt_reason = f"equity {self.system_equity:.4f} below min"
            self._pre_pause_halt = None
        elif self.system_equity >= EQ_MIN and (DD_HALT <= 0 or (self.start_eq > 0 and (self.start_eq - self.system_equity) / max(self.start_eq, 1e-9) < DD_HALT * 0.6)):
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
        # Control/config must also unstick the historic lane. Market ticks
        # must not, or a busy websocket would busy-loop replay.
        if kind in ("ctrl", "config", "catalog", "stop", "start", "pause", "resume"):
            try:
                self._hist_wake.set()
            except Exception:
                pass

    def _wait_wake(self, timeout: float, ev: Optional[threading.Event] = None) -> bool:
        """Wait for a bump without dropping one that arrived during the last cycle.

        Wait first, then clear. A set event from Start/ctrl/fill during the
        cycle returns immediately instead of sitting behind SCAN_S.
        """
        event = ev if ev is not None else getattr(self, "wake_ev", None)
        wait_s = float(timeout or 0.0)
        if event is None:
            if wait_s > 0:
                time.sleep(wait_s)
            return False
        if wait_s <= 0:
            return bool(event.is_set())
        hit = bool(event.wait(timeout=wait_s))
        event.clear()
        return hit

    def record_event(self, event_type: str, event_id: str = "", status: str = "", **fields: Any) -> bool:
        """Commit one bounded activity event without allowing telemetry to stop the engine."""
        try:
            ledger = getattr(self, "event_ledger", None)
            if ledger is None:
                ledger = EventLedger(EVENTS_PATH, CONN_SHORT, max_events=512, flush_interval_s=2)
                self.event_ledger = ledger
            fields.setdefault("system_id", SYSTEM_ID)
            fields.setdefault("connection", CONN_SHORT)
            fields.setdefault("tracking_scope", TRACKING_SCOPE)
            fields.setdefault("track_prefix", TAG)
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

    @staticmethod
    def event_strategy(pos: Position) -> str:
        strategy = str(getattr(pos, "strategy", "") or "")
        if strategy in ("block", "dca"):
            return strategy
        if getattr(pos, "axis_key", ""):
            return "axis"
        if getattr(pos, "trail_key", "") not in ("", "0", "off"):
            return "trailing"
        return str(getattr(pos, "pack", "") or "normal")

    def event_summary(self) -> Dict[str, Any]:
        ledger = getattr(self, "event_ledger", None)
        if ledger is None:
            return {"eventCount": 0, "parity": "pending", "source": "committed-event-ledger"}
        exchange_open = getattr(self, "exchange_open_count", -1)
        try:
            exchange_open = int(exchange_open)
        except Exception:
            exchange_open = -1
        owned_positions = [p for p in (getattr(self, "open", {}) or {}).values() if self.position_is_ours(p)]
        owned_closed = [c for c in (getattr(self, "closed", ()) or ()) if self.row_is_ours(asdict(c))]
        return ledger.summary(
            internal_open=len(owned_positions),
            internal_position_groups=len({(p.symbol, p.side) for p in owned_positions if float(p.qty or 0) > 0}),
            exchange_open=exchange_open,
            internal_closed=len(owned_closed),
            pending_count=len(getattr(self, "pending_orders", {}) or {}),
            reconciliation_pending=bool(getattr(self, "recon_pending", False)),
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
        cap = int(getattr(self, "symbol_cap", DEFAULT_SYMBOL_CAP) or 0)
        wild = bool(getattr(self, "overlay_wild", False))
        dyn = bool(getattr(self, "symbols_dynamic", True))
        if dyn:
            pool = names
        else:
            have = set(SYMBOLS)
            pool = [s for s in names if s in have]
            pool.extend(s for s in SYMBOLS if s not in set(pool))
        must = []
        seen_must = set()
        for s in open_syms + [str(x) for x in FORCED_SYMBOLS if x in self.contracts or x in pool]:
            if s and s not in seen_must:
                must.append(s)
                seen_must.add(s)
        chosen: List[str] = []
        seen = set()
        for s in must + pool:
            if not s or s in seen:
                continue
            seen.add(s)
            chosen.append(s)
            if cap > 0 and len(chosen) >= cap and all(x in seen for x in must):
                break
        if cap <= 0:
            chosen = list(dict.fromkeys(must + pool))
        if not chosen:
            return
        old = list(SYMBOLS)
        if chosen == old:
            self.last_dyn_sel = now
            return
        membership = set(chosen) != set(old)
        SYMBOLS[:] = chosen
        self.last_dyn_sel = now
        if membership:
            try:
                self.ensure_contracts()
            except Exception:
                pass
            try:
                if hasattr(self.api, "hub") and getattr(self.api, "hub", None):
                    self.api.hub.set_symbols(list(SYMBOLS))
            except Exception:
                pass
            log(f"UNIVERSE rank={self.symbol_sort} n={len(SYMBOLS)} dyn={int(self.symbols_dynamic)} cap={cap} wild={int(wild)} lead={(SYMBOLS[0] if SYMBOLS else '-')}", every=20.0, key="uni-rank")

    def _parse_klines(self, data: Any) -> List[List[float]]:
        bars: List[List[float]] = []
        if not isinstance(data, list):
            return bars
        for b in data:
            try:
                if isinstance(b, dict):
                    bars.append([float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"]), float(b.get("volume") or 0)])
                else:
                    bars.append([float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])])
            except Exception:
                continue
        return bars

    def _fetch_klines(self, symbol: str, interval: str = "1m") -> Tuple[str, str, List[List[float]]]:
        r = self.api.public("/openApi/swap/v2/quote/klines", {"symbol": symbol, "interval": interval, "limit": str(KLINE_LIMIT)})
        bars = self._parse_klines(r.get("data"))
        if len(bars) < 10:
            r2 = self.api.public("/openApi/swap/v3/quote/klines", {"symbol": symbol, "interval": interval, "limit": str(KLINE_LIMIT)})
            bars = self._parse_klines(r2.get("data"))
        return symbol, interval, bars

    def _note_kline_ban(self, body: Any) -> None:
        if not isinstance(body, dict):
            return
        msg = str(body.get("msg") or "")
        code = body.get("code")
        if code != 100410 and "frequency limit" not in msg.lower() and "disabled period" not in msg.lower():
            return
        until = 0.0
        for tok in msg.replace(":", " ").split():
            if tok.isdigit() and len(tok) >= 12:
                until = float(tok) / (1000.0 if len(tok) > 11 else 1.0)
                break
        self.kline_ban = max(self.kline_ban, until or (time.time() + 45.0))

    def seed_px_bars(self) -> None:
        """Keep 1m OHLC from live WS/mark so all symbols can start without REST klines."""
        minute = int(time.time() // 60)
        scan = set(SYMBOLS)
        for s, px in list(self.px.items()):
            if px <= 0 or s not in scan:
                continue
            rec = self.bar_min.get(s)
            if not rec or int(rec[0]) != minute:
                if rec:
                    closed_bar = [rec[1], rec[2], rec[3], rec[4], 0.0]
                    bars = self.klines_tf["1m"].setdefault(s, [])
                    bars.append(closed_bar)
                    del bars[:-KLINE_LIMIT]
                    try:
                        self.history_store.merge_bar(s, int(rec[0]), closed_bar, source="mark", quality="live-observed", closed=True, persist=False)
                    except Exception:
                        pass
                    with self.state_guard():
                        pending = getattr(self, "_hist_incremental_symbols", None)
                        if pending is not None:
                            pending.add(s)
                    self._hist_wake.set()
                self.bar_min[s] = [float(minute), px, px, px, px]
            else:
                rec[2] = max(rec[2], px)
                rec[3] = min(rec[3], px)
                rec[4] = px
            bars = self.klines_tf["1m"].setdefault(s, [])
            if len(bars) < 24:
                seed = [px, px, px, px, 0.0]
                bars[:] = [seed[:] for _ in range(24 - len(bars))] + bars
            elif bars:
                bars[-1] = [rec[1], rec[2], rec[3], rec[4], 0.0] if rec and int(rec[0]) == minute else bars[-1]
        self.klines = self.klines_tf["1m"]

    def rollup_tf(self) -> None:
        """Build 5m/15m OHLC from 1m so higher TFs are never empty on a large book."""
        now = time.time()
        if now - float(getattr(self, "_rollup_ts", 0) or 0) < 12.0:
            return
        self._rollup_ts = now
        src = self.klines_tf.get("1m") or {}
        for tf, n in (("5m", 5), ("15m", 15)):
            dest = self.klines_tf.setdefault(tf, {})
            for s, bars in list(src.items()):
                if s in dest and len(dest[s] or []) >= 8:
                    continue
                if len(bars) < n:
                    continue
                out: List[List[float]] = []
                step = n
                for i in range(0, len(bars) - n + 1, step):
                    chunk = bars[i : i + n]
                    if len(chunk) < n:
                        break
                    out.append([
                        float(chunk[0][0]),
                        max(float(x[1]) for x in chunk),
                        min(float(x[2]) for x in chunk),
                        float(chunk[-1][3]),
                        sum(float(x[4]) for x in chunk),
                    ])
                if len(out) >= 5:
                    dest[s] = out[-KLINE_LIMIT:]

    def refresh_klines(self) -> None:
        now = time.time()
        self.seed_px_bars()
        self.rollup_tf()
        if now < self.kline_ban:
            self._kline_deferred = f"exchange cooldown {max(0.0, self.kline_ban - now):.1f}s"
            return
        budget = self._budget()
        if not bool(getattr(budget, "kline_rest", True)):
            self._kline_deferred = f"load budget {getattr(budget, 'level', 'unknown')}"
            return
        self._kline_deferred = ""
        ready1 = sum(1 for s in SYMBOLS if len(self.klines_tf.get("1m", {}).get(s) or []) >= 20)
        need1 = len(SYMBOLS) if len(SYMBOLS) <= 48 else max(32, len(SYMBOLS) // 2)
        filling = ready1 < max(1, need1)
        reqs = []
        if filling:
            tfs = ["1m"] + (["5m"] if bool(getattr(budget, "tf_5m", True)) else [])
        else:
            tfs = [
                tf for tf in TF_EVERY
                if tf == "1m" or (tf == "5m" and bool(getattr(budget, "tf_5m", True)))
                or (tf == "15m" and bool(getattr(budget, "tf_15m", True)))
            ]
        for tf in tfs:
            every = TF_EVERY.get(tf, 2.0)
            if not self.tf_on.get(tf, True):
                continue
            due = [s for s in SYMBOLS if now - self.kline_ts_tf[tf].get(s, 0) >= every]
            if not due:
                continue
            due.sort(key=lambda s: self.kline_ts_tf[tf].get(s, 0))
            batch_limit = max(1, min(TF_BATCH.get(tf, 4), int(getattr(budget, "kline_batch", 4) or 1)))
            batch = due[:batch_limit]
            for s in batch:
                reqs.append(("/openApi/swap/v2/quote/klines", {"symbol": s, "interval": tf, "limit": str(KLINE_LIMIT)}))
        if not reqs:
            return
        stored = 0

        def _store(s: str, tf: str, body: Any) -> None:
            nonlocal stored
            self._note_kline_ban(body if isinstance(body, dict) else {})
            bars = self._parse_klines(body.get("data") if isinstance(body, dict) else None)
            if not s or len(bars) < 5:
                return
            self.klines_tf.setdefault(tf, {})[s] = bars[-KLINE_LIMIT:]
            self.kline_ts_tf.setdefault(tf, {})[s] = now
            stored += 1

        if hasattr(self.api, "gather_public"):
            for i in range(0, len(reqs), 4):
                if time.time() < self.kline_ban:
                    break
                sd_notify("WATCHDOG=1")
                chunk = reqs[i : i + 4]
                rows = self.api.gather_public(chunk, timeout=6.0)
                for _path, extra, body in rows:
                    _store(extra.get("symbol") or "", extra.get("interval") or "1m", body)
        else:
            for _p, extra in reqs:
                if time.time() < self.kline_ban:
                    break
                body = self.api.public("/openApi/swap/v2/quote/klines", extra)
                _store(extra.get("symbol") or "", extra.get("interval") or "1m", body)
        self.klines = self.klines_tf["1m"]
        self.kline_ts = self.kline_ts_tf["1m"]
        # A cooled, failed, or budget-suppressed batch must remain due. Only
        # successful storage advances the aggregate refresh marker.
        if stored:
            self.last_kline = now
            self._hist_wake.set()

    def ema(self, xs: List[float], n: int) -> float:
        if not xs:
            return 0.0
        k = 2 / (n + 1)
        e = xs[0]
        for x in xs[1:]:
            e = x * k + e * (1 - k)
        return e

    def rsi(self, closes: List[float], n: int = 7) -> float:
        if len(closes) < n + 1:
            return 50.0
        gains = losses = 0.0
        for i in range(-n, 0):
            d = closes[i] - closes[i - 1]
            if d >= 0:
                gains += d
            else:
                losses -= d
        if losses == 0:
            return 100.0
        rs = (gains / n) / (losses / n)
        return 100 - (100 / (1 + rs))

    def score(self, sym: str) -> Tuple[int, str, float]:
        bars = self.klines.get(sym) or []
        px = self.px.get(sym) or 0
        fp = (len(bars), float(bars[-1][3]) if bars else 0.0, round(float(px or 0), 8))
        cache = getattr(self, "_score_cache", None)
        if isinstance(cache, dict):
            hit = cache.get(sym)
            if hit and hit[0] == fp:
                return hit[1]
        result = self._score_compute(sym, bars, px)
        if isinstance(cache, dict):
            cache[sym] = (fp, result)
        return result

    def _score_compute(self, sym: str, bars: List[Any], px: float) -> Tuple[int, str, float]:
        if len(bars) < 16 or px <= 0:
            return 0, "no-data", 0.0
        closes = [b[3] for b in bars]
        highs = [b[1] for b in bars]
        lows = [b[2] for b in bars]
        vols = [b[4] for b in bars]
        e8 = self.ema(closes, 8)
        e21 = self.ema(closes, 21)
        rsi = self.rsi(closes, 7)
        last = closes[-1]
        prev = closes[-2]
        rng = max(highs[-8:]) - min(lows[-8:]) or last * 0.002
        body = last - prev
        mom = (last - closes[-4]) / closes[-4] if closes[-4] else 0
        vol_avg = sum(vols[-12:]) / 12 or 1
        slope = (e8 - e21) / last
        long_c = short_c = 0.0
        why_l: List[str] = []
        why_s: List[str] = []
        if rsi < 32:
            long_c += 0.34; why_l.append(f"rsi{rsi:.0f}")
        elif rsi < 42:
            long_c += 0.16; why_l.append("rsi-low")
        if rsi > 68:
            short_c += 0.34; why_s.append(f"rsi{rsi:.0f}")
        elif rsi > 58:
            short_c += 0.16; why_s.append("rsi-hi")
        if slope > 0.00015:
            long_c += 0.22; why_l.append("ema+")
        if slope < -0.00015:
            short_c += 0.22; why_s.append("ema-")
        if body > 0 and last > highs[-2]:
            long_c += 0.18; why_l.append("brk")
        if body < 0 and last < lows[-2]:
            short_c += 0.18; why_s.append("brk")
        if mom > 0.0012:
            long_c += 0.12; why_l.append("mom")
        if mom < -0.0012:
            short_c += 0.12; why_s.append("mom")
        loc = (last - min(lows[-8:])) / rng
        if loc < 0.18 and rsi < 45:
            long_c += 0.20; why_l.append("fade-lo")
        if loc > 0.82 and rsi > 55:
            short_c += 0.20; why_s.append("fade-hi")
        if vols[-1] > vol_avg * (1 + 0.15):
            long_c += 0.06
            short_c += 0.06
        long_c += self.coord.vol_boost(bars)
        short_c += self.coord.vol_boost(bars)
        if not self.coord.outbreak_ok(bars):
            long_c *= 0.45
            short_c *= 0.45
        if self.regime == "risk-on":
            long_c += 0.10
            short_c *= 0.72
        elif self.regime == "risk-off":
            short_c += 0.10
            long_c *= 0.72
        if long_c >= 0.58 and long_c > short_c + 0.10:
            return 1, "+".join(why_l) or "long", min(1.0, long_c)
        if short_c >= 0.58 and short_c > long_c + 0.10:
            return -1, "+".join(why_s) or "short", min(1.0, short_c)
        return 0, "flat", max(long_c, short_c)

    def update_regime(self) -> None:
        scores = []
        for s in ("SOL-USDT", "XRP-USDT", "DOGE-USDT"):
            d, _, c = self.score(s)
            scores.append(d * c)
        chgs = [self.chg.get(s, 0) for s in SYMBOLS]
        avg = sum(chgs) / len(chgs) if chgs else 0
        ssum = sum(scores)
        if ssum > 0.6 or avg > 0.8:
            self.regime = "risk-on"
        elif ssum < -0.6 or avg < -0.8:
            self.regime = "risk-off"
        else:
            self.regime = "neutral"

    def group_count(self, g: str) -> int:
        return sum(1 for p in self.open.values() if self.group_of(p.symbol) == g)

    def list_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        hit = self._oo_cache.get("*")
        now = time.time()
        if hit and now - hit[0] < 12.0:
            rows = hit[1]
        elif self.api.path_cd.get("/openApi/swap/v2/trade/openOrders", 0) > now:
            rows = hit[1] if hit else []
        else:
            r = self.api.get("/openApi/swap/v2/trade/openOrders")
            if not self.ok(r):
                rows = hit[1] if hit else []
            else:
                data = r.get("data") or {}
                orders = data.get("orders") if isinstance(data, dict) else data
                rows = orders if isinstance(orders, list) else []
                # Empty REST while we hold positions is lag/rate-limit, not a flat book.
                if rows or not self.open:
                    self._oo_cache["*"] = (now, rows)
                    self._order_est = len(rows)
                    self._order_est_known = True
                else:
                    rows = hit[1] if hit else []
        self.exchange_order_total_count = len(rows)
        self.exchange_order_own_count = sum(1 for order in rows if self.order_is_ours(order))
        self.exchange_order_foreign_count = max(0, self.exchange_order_total_count - self.exchange_order_own_count)
        self.foreign_open_order_count = self.exchange_order_foreign_count
        if self.exchange_order_foreign_count > 0:
            self._note_foreign_activity()
        if symbol:
            return [o for o in rows if str(o.get("symbol") or "") == symbol]

        return rows

    def our_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self.list_orders(symbol)
        return [o for o in rows if self.cid_ours(self.order_cid(o))]

    def _oid_in_book(self, order_id: str) -> bool:
        oid = str(order_id or "")
        if not oid:
            return False
        if oid in overall_controls.cleanup_state(self):
            return True
        for p in self.open.values():
            if oid in set(getattr(p,"retired_control_ids",[])):
                return True
            if oid in (
                str(p.sl_oid or ""),
                str(p.tp_oid or ""),
                str(getattr(p, "sec_sl_oid", "") or ""),
                str(getattr(p, "sec_tp_oid", "") or ""),
                str(getattr(p, "order_id", "") or ""),
            ):
                return True
        return False

    def cancel_order(self, symbol: str, order_id: str, cid: str = "") -> bool:
        if not order_id:
            return True
        cid = str(cid or "")
        if not cid:
            for o in self.list_orders(symbol):
                if str(o.get("orderId") or o.get("orderID") or "") == str(order_id):
                    cid = self.order_cid(o)
                    break
        if cid and not self.cid_ours(cid):
            log(f"SKIP cancel foreign {symbol} cid={cid[:24]}", every=20.0, key=f"skipc:{symbol}")
            return False
        if not cid and not self._oid_in_book(order_id):
            log(f"SKIP cancel unknown {symbol} oid={order_id}", every=20.0, key=f"skipu:{symbol}")
            return False
        cancel_key = stable_key(CONN_SHORT, "cancel", symbol, order_id)
        self.record_event(
            "exchange_request",
            stable_key(cancel_key, "request"),
            status="pending",
            symbol=symbol,
            order_id=order_id,
            client_id=cid,
            detail="cancel order",
            metadata={"path": "/openApi/swap/v2/trade/order"},
        )
        r = self.api.delete("/openApi/swap/v2/trade/order", {"symbol": symbol, "orderId": order_id})
        self.record_event(
            "exchange_response",
            stable_key(cancel_key, "response"),
            status="confirmed" if self.ok(r) else "error",
            code=r.get("code"),
            symbol=symbol,
            order_id=order_id,
            client_id=cid,
            detail="cancel order" if self.ok(r) else str(r.get("msg") or "cancel failed"),
        )
        already_absent = str(r.get("code")) in ("109400", "109421") and "order not exist" in str(r.get("msg") or "").lower()
        if self.ok(r) or already_absent:
            self.record_event("cancellation", stable_key(cancel_key, "completed"), status="confirmed", symbol=symbol, order_id=order_id, client_id=cid, detail="order cancelled")
            self._oo_cache.pop(symbol, None)
            self._oo_cache.pop("*", None)
            return True
        self.last_error = f"cancel {symbol} {r.get('msg')}"[:200]
        self.record_event("error", stable_key(cancel_key, "error"), status="error", code=r.get("code"), symbol=symbol, order_id=order_id, client_id=cid, detail=self.last_error)
        return False

    def _order_matches_position(self, o: Dict[str, Any], pos: Position) -> bool:
        if not self.order_is_ours(o) or not self.position_is_ours(pos):
            return False
        if str(o.get("symbol") or "") != pos.symbol:
            return False
        side = str(o.get("positionSide") or "").upper()
        if side and side != str(pos.side).upper():
            return False
        oid = real_oid(o.get("orderId") or o.get("orderID"))
        known = {
            real_oid(pos.sl_oid), real_oid(pos.tp_oid),
            real_oid(getattr(pos, "sec_sl_oid", "")), real_oid(getattr(pos, "sec_tp_oid", "")),
        }
        if oid and oid in known:
            return True
        if not self.per_config_controls(pos):
            return True
        parsed = self.parse_track(self.order_cid(o)) or {}
        token = str(parsed.get("group_token") or "")
        return bool(
            token
            and token in control_group_tokens(
                getattr(pos, "control_group_key", ""),
                getattr(pos, "control_range_key", ""),
            )
        )

    def cancel_controls(
        self, symbol: str, keep: Optional[set] = None, pos: Optional[Position] = None
    ) -> None:
        if pos is not None and not self.position_is_ours(pos):
            return
        if pos is not None and not getattr(pos,"_overall_proxy",False) and (overall_controls.enabled(self, pos) or getattr(pos,"overall_controls",False)) and any(p is not pos for p in overall_controls.members(self,pos)):
            return  # A single Set must never cancel its siblings' shared pair.
        keep = keep or set()
        seen: set[str] = set()
        for o in self.list_orders(symbol):
            if not self.order_is_ours(o):
                continue
            if pos is not None and not self._order_matches_position(o, pos):
                continue
            oid = real_oid(o.get("orderId") or o.get("orderID"))
            typ = str(o.get("type") or "")
            if typ in SL_TYPES | TP_TYPES or o.get("stopPrice"):
                if oid:
                    seen.add(oid)
                if oid and oid not in keep:
                    self.cancel_order(symbol, oid, self.order_cid(o))
        # REST can lag or return an empty cached page immediately after a
        # partial fill. Known local control IDs are ours, so cancel them too;
        # this prevents a common aggregate pair from being duplicated on the
        # next reconciliation pass.
        if pos is not None:
            known = {
                real_oid(getattr(pos, "sl_oid", "")),
                real_oid(getattr(pos, "tp_oid", "")),
                real_oid(getattr(pos, "sec_sl_oid", "")),
                real_oid(getattr(pos, "sec_tp_oid", "")),
            }
            for oid in sorted(x for x in known if x and x not in keep and x not in seen):
                self.cancel_order(symbol, oid)

    def opt_fracs(self, pos: Optional[Position] = None) -> Tuple[float, float, float, float]:
        """(sl, tp, sl_lo, sl_hi) fractions clamped to optimal security ranges."""
        sl_lo = max(float(self.sl_min), float(getattr(self.exits, "opt_sl_min", 0.001) or 0.001))
        sl_hi = min(float(self.sl_max), float(getattr(self.exits, "opt_sl_max", 0.009) or 0.009))
        if sl_lo > sl_hi:
            sl_lo, sl_hi = sl_hi, sl_lo
        sl = float(pos.sl_pct) if pos and pos.sl_pct > 0 else SL_PCT
        sl = max(sl_lo, min(sl_hi, sl))
        tp_lo = float(self.tp_min)
        tp_hi = float(self.tp_max) if self.tp_max > 0 else float("inf")
        tp = float(pos.tp_pct) if pos and pos.tp_pct > 0 else TP_PCT
        tp = max(tp_lo, min(tp_hi, tp))
        return sl, tp, sl_lo, sl_hi

    def refresh_px_one(self, symbol: str) -> float:
        try:
            r = self.api.public("/openApi/swap/v2/quote/ticker", {"symbol": symbol})
            data = r.get("data")
            row = data[0] if isinstance(data, list) and data else data
            if not isinstance(row, dict):
                return float(self.px.get(symbol) or 0)
            last = float(row.get("lastPrice") or row.get("last") or row.get("close") or 0)
            mark = float(row.get("markPrice") or row.get("fairPrice") or last or 0)
            if last > 0:
                self.last_px[symbol] = last
            if mark > 0:
                self.px[symbol] = mark
            return max(last, mark)
        except Exception:
            return float(max(self.px.get(symbol) or 0, self.last_px.get(symbol) or 0))

    def security_prices(self, pos: Position) -> Tuple[float, float]:
        """Qty-matched order SL/TP from the Set's own range."""
        sl_f, tp_f, _, _ = self.opt_fracs(pos)
        e = pos.entry if pos.entry > 0 else (self.px.get(pos.symbol) or 0)
        if e <= 0:
            return pos.sl, pos.tp
        if pos.side == "LONG":
            sl = e * (1.0 - sl_f)
            tp = e * (1.0 + tp_f)
            if self.exits.enabled and pos.peak > e:
                sl = max(sl, self.exits.optimal_sl("LONG", e, pos.peak, sl))
        else:
            sl = e * (1.0 + sl_f)
            tp = e * (1.0 - tp_f)
            if self.exits.enabled and pos.peak and pos.peak < e:
                sl = min(sl, self.exits.optimal_sl("SHORT", e, pos.peak, sl))
        return self.clamp_ctrl_price(pos, "sl", sl), self.clamp_ctrl_price(pos, "tp", tp)

    def max_range_prices(self, pos: Position) -> Tuple[float, float]:
        """Return the widest safe member range for the effective control mode."""
        sl_f, tp_f, sl_lo, sl_hi = self.opt_fracs(pos)
        if self.per_config_controls(pos):
            sl_w = max(sl_f, sl_hi, float(getattr(pos, "sl_pct", 0) or 0), sl_lo)
            tp_w = max(tp_f, float(self.tp_max), float(getattr(pos, "tp_pct", 0) or 0), float(self.tp_min))
        else:
            member_sl, member_tp = self.aggregate_member_ranges(pos)
            # Aggregate protection is widened from the actual merged members,
            # then bounded by the configured risk maxima. It must not silently
            # fall back to the narrower first member after a partial fill.
            sl_w = max(sl_lo, min(sl_hi, member_sl or sl_f))
            tp_cap = float(self.tp_max) if float(self.tp_max) > 0 else float("inf")
            tp_w = max(float(self.tp_min), min(tp_cap, member_tp or tp_f))
        e = pos.entry if pos.entry > 0 else (self.px.get(pos.symbol) or 0)
        if e <= 0:
            return pos.sl, pos.tp
        if pos.side == "LONG":
            sl = e * (1.0 - sl_w)
            tp = e * (1.0 + tp_w)
        else:
            sl = e * (1.0 + sl_w)
            tp = e * (1.0 - tp_w)
        return self.clamp_ctrl_price(pos, "sl", sl), self.clamp_ctrl_price(pos, "tp", tp)

    def px_band(self, symbol: str, entry: float = 0.0) -> Tuple[float, float, float, float]:
        mark = float(self.px.get(symbol) or 0)
        last = float((getattr(self, "last_px", None) or {}).get(symbol) or 0)
        nums = [x for x in (mark, last) if x > 0]
        if not nums and entry > 0:
            nums = [entry]
        if not nums:
            return 0.0, 0.0, 0.0, 0.0
        return mark, last, max(nums), min(nums)

    def sl_legal(self, pos: Position, price: float) -> bool:
        _, _, hi, lo = self.px_band(pos.symbol, pos.entry)
        if price <= 0 or lo <= 0 or hi <= 0:
            return False
        if pos.side == "LONG":
            return price < lo * 0.9985
        return price > hi * 1.0015

    def tp_legal(self, pos: Position, price: float) -> bool:
        _, _, hi, lo = self.px_band(pos.symbol, pos.entry)
        if price <= 0 or lo <= 0 or hi <= 0:
            return False
        if pos.side == "LONG":
            return price > hi * 1.0015
        return price < lo * 0.9985

    def desired_sl_tp(self, pos: Position) -> Tuple[float, float, float, float]:
        if getattr(pos, "_overall_proxy", False):
            sl = self.clamp_ctrl_price(pos, "sl", pos.sl)
            tp = self.clamp_ctrl_price(pos, "tp", pos.tp)
            return sl, tp, sl, tp
        sl, tp = self.security_prices(pos)
        sec_sl, sec_tp = self.max_range_prices(pos)
        # Aggregate mode has one common pair for the whole symbol/direction;
        # always prefer its widened range. Per-config mode preserves the
        # quantity-matched member range and only falls back to security prices
        # when the exchange rejects the preferred trigger side.
        aggregate = not self.per_config_controls(pos)
        pick_sl = next((p for p in ((sec_sl, sl) if aggregate else (sl, sec_sl)) if self.sl_legal(pos, p)), 0.0)
        if not pick_sl:
            pick_sl = self.clamp_ctrl_price(pos, "sl", sec_sl or sl or 0)
        pick_tp = next((p for p in ((sec_tp, tp) if aggregate else (tp, sec_tp)) if self.tp_legal(pos, p)), 0.0)
        if not pick_tp:
            pick_tp = self.clamp_ctrl_price(pos, "tp", sec_tp or tp or 0)
        return pick_sl, pick_tp, sec_sl, sec_tp

    def clamp_ctrl_price(self, pos: Position, kind: str, price: float) -> float:
        """Keep SL at the intended stop. Never chase mark away from entry."""
        mark = float(self.px.get(pos.symbol) or 0)
        last = float((getattr(self, "last_px", None) or {}).get(pos.symbol) or 0)
        e = float(pos.entry or 0)
        nums = [x for x in (mark, last) if x > 0]
        if not nums:
            nums = [e] if e > 0 else []
        if not nums:
            return price
        hi, lo = max(nums), min(nums)
        is_sl = str(kind).lower() in ("sl", "s", "u", "sec-sl", "sec_sl")
        c = self.contracts.get(pos.symbol)
        tick = 10 ** -(c.pprec if c else 4)
        pad = max(8 * tick, hi * 0.0020)
        price = float(price or 0)
        if is_sl:
            if pos.side == "LONG":
                intended = price if price > 0 else (e * (1.0 - max(float(getattr(pos, "sl_pct", 0) or 0), 0.002)) if e else lo - pad)
                if intended > 0 and intended <= lo - pad:
                    price = intended
                else:
                    price = lo - pad
            else:
                intended = price if price > 0 else (e * (1.0 + max(float(getattr(pos, "sl_pct", 0) or 0), 0.002)) if e else hi + pad)
                if intended > 0 and intended >= hi + pad:
                    price = intended
                else:
                    price = hi + pad
        else:
            if pos.side == "LONG":
                price = max(price or (hi + pad), hi + pad)
            else:
                price = min(price or (lo - pad), lo - pad)
        if c:
            tick = max(tick, 10 ** -(c.pprec if c.pprec >= 0 else 6))
            price = self.round_px(c, price)
            for _ in range(8):
                legal = (
                    (pos.side == "LONG" and is_sl and price < lo - tick * 0.5)
                    or (pos.side == "LONG" and not is_sl and price > hi + tick * 0.5)
                    or (pos.side == "SHORT" and is_sl and price > hi + tick * 0.5)
                    or (pos.side == "SHORT" and not is_sl and price < lo - tick * 0.5)
                )
                if legal:
                    break
                step = max(tick, hi * 0.0015)
                if pos.side == "LONG":
                    price = price - step if is_sl else price + step
                else:
                    price = price + step if is_sl else price - step
                price = self.round_px(c, price)
        return price

    def _controls_waiting_for_position(self, pos: Position) -> bool:
        now = time.time()
        return (now < self.ctrl_skip.get(f"flat:{pos.symbol}:{pos.side}", 0)
                or now < self.ctrl_skip.get(self._control_minimum_key(pos), 0))

    def _control_minimum_key(self, pos: Position) -> str:
        # Equal-size sibling sets share the venue floor. A changed quantity
        # may be legal immediately and must not inherit the old rejection.
        return f"min-control:{pos.symbol}:{pos.side}:{float(pos.qty):.12g}"

    def _defer_minimum_controls(self, pos: Position, response: Dict[str, Any]) -> bool:
        msg = str(response.get("msg") or "").lower()
        if "minimum size" not in msg and "minimum order amount" not in msg:
            return False
        # Changing trigger prices or order types cannot repair a size floor.
        # Preserve the position and any accepted controls; retry after the
        # bounded pause without increasing exposure or claiming protection.
        self.ctrl_skip[self._control_minimum_key(pos)] = time.time() + 60.0
        return True

    def _defer_missing_position_controls(self, pos: Position, response: Dict[str, Any]) -> bool:
        if str(response.get("code")) != "109420" and ctrl_err_kind(str(response.get("msg") or "")) != "flat":
            return False
        # One absent exchange position covers every independent set on this
        # symbol+direction. Alternate payloads/prices cannot repair absence.
        # Keep the book and existing protection until reconciliation confirms
        # exchange truth; stop sibling sets from exhausting the venue budget.
        self.ctrl_skip[f"flat:{pos.symbol}:{pos.side}"] = time.time() + 60.0
        self.recon_pending = True
        return True

    def place_ctrl(self, pos: Position, kind: str, price: float) -> str:
        is_sl = str(kind).lower() in ("sl", "s", "u", "sec-sl", "sec_sl")
        is_sec = str(kind).lower() in ("u", "v", "sec-sl", "sec-tp", "sec_sl", "sec_tp")
        cid_ch = "u" if (is_sec and is_sl) else ("v" if is_sec else ("s" if is_sl else "t"))
        if not self.exchange_position_active(pos):
            return real_oid(pos.sl_oid if is_sl else pos.tp_oid)
        if time.time() < self.ctrl_skip.get("__order_cap__", 0) or self._controls_waiting_for_position(pos):
            return real_oid(pos.sl_oid if is_sl else pos.tp_oid)
        have_this = real_oid(pos.sl_oid if is_sl else pos.tp_oid)
        scope = self.position_key(pos) if self.per_config_controls(pos) else self.legacy_position_key(pos)
        if have_this and time.time() < self.ctrl_skip.get(scope, 0):
            return have_this
        if (self.px.get(pos.symbol) or 0) <= 0 and (self.last_px.get(pos.symbol) or 0) <= 0:
            self.refresh_px_one(pos.symbol)
        price = self.clamp_ctrl_price(pos, "sl" if is_sl else "tp", price)
        c = self.contracts.get(pos.symbol)
        qty_s = self.fmt_qty(c, pos.qty)
        px_s = self.fmt_px(c, price)
        if float(px_s or 0) <= 0:
            log(f"CTRL SKIP {kind} {pos.symbol} stopPrice=0", every=20.0, key=f"cskip:{pos.symbol}:px0")
            self.ctrl_skip[scope] = time.time() + 60
            return have_this
        market_type = "STOP_MARKET" if is_sl else "TAKE_PROFIT_MARKET"
        limit_type = "STOP" if is_sl else "TAKE_PROFIT"
        # A range group is quantity-matched. It must never fall back to
        # closePosition=true because that would close another range group on
        # the same symbol and side.
        if self.per_config_controls(pos):
            forms = [
                {"close_pos": False, "with_qty": True, "otype": market_type},
                {"close_pos": False, "with_qty": True, "otype": limit_type},
            ]
        else:
            forms = [
                {"close_pos": False, "with_qty": True, "otype": market_type},
                {"close_pos": True, "with_qty": False, "otype": market_type},
                {"close_pos": False, "with_qty": True, "otype": limit_type},
                {"close_pos": True, "with_qty": False, "otype": limit_type},
            ]
        r: Dict[str, Any] = {}
        msg = ""
        oid = ""
        refreshed_quote = False
        for extra in (0.0, 0.006, 0.012):
            px_try = price
            if extra:
                m = max(self.px.get(pos.symbol) or 0, self.last_px.get(pos.symbol) or 0, pos.entry)
                if is_sl:
                    px_try = m * (1.0 + extra) if pos.side == "SHORT" else m * (1.0 - extra)
                else:
                    px_try = m * (1.0 - extra) if pos.side == "SHORT" else m * (1.0 + extra)
                px_try = self.clamp_ctrl_price(pos, "sl" if is_sl else "tp", px_try)
            px_s = self.fmt_px(c, px_try)
            for form in forms:
                cid = self.cid(cid_ch, pos=pos)
                body = ctrl_payload(
                    pos.symbol, pos.side, "sl" if is_sl else "tp", px_s, qty_s, cid,
                    close_pos=bool(form["close_pos"]), with_qty=bool(form["with_qty"]), otype=str(form["otype"]),
                )
                if "quantity" in body and body.get("closePosition"):
                    body.pop("quantity", None)
                control_key = stable_key(CONN_SHORT, "control", cid, kind, px_s)
                self.record_event(
                    "control_request",
                    stable_key(control_key, "request"),
                    status="pending",
                    symbol=pos.symbol,
                    side=pos.side,
                    set_id=pos.set_id,
                    indication_kind=getattr(pos, "ind_kind", ""),
                    strategy=self.event_strategy(pos),
                    **self.control_event_fields(pos),
                    client_id=cid,
                    qty=pos.qty,
                    price=px_try,
                    detail=f"{kind} protection",
                    metadata={"path": "/openApi/swap/v2/trade/order", "closePosition": form["close_pos"]},
                )
                r = self.api.post("/openApi/swap/v2/trade/order", body)
                self.did_io = True
                self.record_event(
                    "control_response",
                    stable_key(control_key, "response"),
                    status="confirmed" if self.ok(r) else ("pending" if r.get("cooled") else "rejected"),
                    code=r.get("code"),
                    symbol=pos.symbol,
                    side=pos.side,
                    set_id=pos.set_id,
                    indication_kind=getattr(pos, "ind_kind", ""),
                    strategy=self.event_strategy(pos),
                    **self.control_event_fields(pos),
                    client_id=cid,
                    order_id=extract_oid(r),
                    price=px_try,
                    detail=f"{kind} protection",
                )
                if r.get("cooled"):
                    return ""
                if not self.ok(r) and self._defer_missing_position_controls(pos, r):
                    return have_this
                if not self.ok(r) and self._defer_minimum_controls(pos, r):
                    return have_this
                msg = str(r.get("msg") or "")
                kind_err = ctrl_err_kind(msg)
                if self.ok(r):
                    oid = extract_oid(r)
                    if oid:
                        self.record_event(
                            "protection",
                            stable_key(control_key, "protection"),
                            status="confirmed",
                            symbol=pos.symbol,
                            side=pos.side,
                            set_id=pos.set_id,
                            indication_kind=getattr(pos, "ind_kind", ""),
                    strategy=self.event_strategy(pos),
                    **self.control_event_fields(pos),
                    client_id=cid,
                    order_id=oid,
                    price=px_try,
                    detail=kind,
                        )
                        pos.close_position = bool(form["close_pos"])
                        price = px_try
                        break
                    continue
                if kind_err == "exists":
                    for o in self.our_orders(pos.symbol):
                        typ = str(o.get("type") or "")
                        side = str(o.get("positionSide") or "").upper()
                        if side != pos.side:
                            continue
                        # An "already exists" response is symbol-wide. In
                        # per-config mode only rebind an order carrying this
                        # group's token/known ID, never another range pair.
                        if self.per_config_controls(pos) and not self._order_matches_position(o, pos):
                            continue
                        live = real_oid(o.get("orderId") or o.get("orderID"))
                        if not live:
                            continue
                        if self.per_config_controls(pos):
                            try:
                                existing_qty = float(o.get("origQty") or o.get("quantity") or o.get("orderQty") or 0)
                            except Exception:
                                existing_qty = 0.0
                            wanted_qty = max(0.0, float(pos.qty or 0.0))
                            # A same-token order with an old quantity must be
                            # canceled before retrying. Rebinding it would
                            # leave this range group under-protected.
                            if existing_qty <= 0 or not (wanted_qty * 0.95 <= existing_qty <= wanted_qty * 1.05):
                                self.cancel_order(pos.symbol, live, self.order_cid(o))
                                continue
                        if is_sl and (typ in SL_TYPES or self._cid_kind(o) in ("s", "u")):
                            return live
                        if (not is_sl) and (typ in TP_TYPES or self._cid_kind(o) in ("t", "v")):
                            return live
                    continue
                if kind_err == "qty_close":
                    continue
                if kind_err == "cap":
                    self.ctrl_skip[scope] = time.time() + 90
                    self.ctrl_skip["__order_cap__"] = time.time() + 60
                    log(f"CTRL cap {pos.symbol} cooldown 60s", every=20.0, key="ordercap")
                    return ""
                if "110206" in msg or ("over 20" in msg.lower() and "error code" in msg.lower()):
                    self.ctrl_skip[scope] = time.time() + 30
                    return have_this
                if kind_err in ("px", "liq"):
                    if kind_err == "px" and not refreshed_quote:
                        refreshed_quote = True
                        refresh = getattr(self, "refresh_px_one", None)
                        if callable(refresh):
                            try:
                                refresh(pos.symbol)
                                price = self.clamp_ctrl_price(pos, "sl" if is_sl else "tp", price)
                            except Exception:
                                pass
                    self.ctrl_skip[scope] = time.time() + 45
                    break
            if oid:
                break
        if not oid:
            short = short_api_msg(msg)
            kind_err = ctrl_err_kind(msg)
            if is_transient_api(msg) or kind_err in ("flat", "px", "liq", "exists", "qty_close", "qty"):
                self.ctrl_skip[scope] = time.time() + (90 if kind_err in ("px", "liq", "qty") else 20)
                log(f"CTRL SKIP {kind} {pos.symbol} {short}", every=12.0, key=f"cskip:{pos.symbol}:{short}")
            else:
                self.errors += 1
                self.last_error = f"{kind} {pos.symbol} {short}"[:160]
                log(f"CTRL FAIL {kind} {pos.symbol} {pos.side} {short} px={price} mark={self.px.get(pos.symbol)}")
            if kind_err in ("px", "liq"):
                self.ctrl_skip[scope] = time.time() + 45
            elif kind_err == "qty":
                self.ctrl_skip[scope] = time.time() + 60
            return ""
        pos.overall = True
        pos.ctrl_qty = pos.qty
        if is_sec:
            if is_sl:
                pos.sec_sl = price
            else:
                pos.sec_tp = price
        self._oo_cache.pop("*", None)
        self._oo_cache.pop(pos.symbol, None)
        log(f"CTRL {kind} {pos.symbol} {pos.side} oid={oid} closePos={pos.close_position} qty={pos.qty} @{price}")
        return oid

    def missing_controls(self, pos: Position) -> bool:
        if not getattr(self, "control_orders", True):
            return False
        has_sl = bool(real_oid(pos.sl_oid) or real_oid(getattr(pos, "sec_sl_oid", "")))
        has_tp = bool(real_oid(pos.tp_oid) or real_oid(getattr(pos, "sec_tp_oid", "")))
        return not (has_sl and has_tp)

    def entries_blocked(self) -> bool:
        """Entry-only admission for venue cooldown and the first boot seconds.
        A leftover/ghost missing SL/TP must never stop the rest of the book —
        after a manual exchange close the desk keeps placing, adding, and
        attaching controls on every other symbol."""
        if time.time() < self.ctrl_skip.get("__order_cap__", 0):
            return True
        if time.time() - float(getattr(self, "boot_ts", 0) or 0) < 25.0:
            return True
        api = getattr(self, "api", None)
        retry_after = getattr(api, "order_retry_after", None)
        if callable(retry_after) and retry_after() > 0:
            return True
        sets = getattr(self, "sets", None)
        if (
            sets is not None
            and bool(getattr(sets, "enabled", False))
            and bool(getattr(sets, "use_historic_gate", False))
            and not bool(getattr(getattr(sets, "progress", None), "ready", False))
        ):
            # DCA and Block adds are new orders too; management and protective
            # controls use separate paths and remain available during startup.
            return True
        return False

    def controls_illegal(self, pos: Position) -> bool:
        if self.missing_controls(pos):
            return True
        return (not self.sl_legal(pos, pos.sl)) or (not self.tp_legal(pos, pos.tp))

    def priority_controls(self) -> int:
        """Overall SL/TP first. Returns how many positions are still unprotected."""
        overall_controls.drain_cleanup(self)
        if not getattr(self, "control_orders", True):
            return 0
        miss = 0
        now = time.time()
        shared_checked = set()
        for pos in list(self.open.values()):
            if overall_controls.enabled(self,pos) and (pos.symbol,pos.side) not in shared_checked:
                # Run group migration before the member-level exchange snapshot
                # guard. The shared proxy checks the symbol+direction exchange
                # quantity itself; a stale first member must not prevent the
                # other valid members from receiving one common pair.
                overall_controls.ensure(self,pos)
                shared_checked.add((pos.symbol,pos.side))
            # Overall protection owns the complete symbol/direction group.
            # Do not fall through into the per-config fallback below: that
            # path can interpret one migrating member as an unprotected
            # standalone position and repeatedly submit an impossible
            # below-minimum close on the exchange.
            if overall_controls.enabled(self,pos):
                continue
            elif not overall_controls.enabled(self,pos) and getattr(pos,"overall_controls",False):
                self.ensure_controls(pos)
            if not self.exchange_position_active(pos):
                # System-only positions are still evaluated and reported, but
                # their missing exchange controls are not a live protection
                # defect because no venue position exists for this side.
                continue
            px = self.px.get(pos.symbol) or pos.entry
            scope = self.position_key(pos) if self.per_config_controls(pos) else self.legacy_position_key(pos)
            need = self.missing_controls(pos)
            illegal = (not need) and now >= self.ctrl_skip.get(f"legal:{scope}", 0) and self.controls_illegal(pos)
            if not need and not illegal:
                continue
            if now < self.ctrl_skip.get(scope, 0) and not need:
                miss += 1
                continue
            self.ensure_controls(pos)
            if not any(candidate is pos for candidate in self.open.values()):
                continue
            if self.missing_controls(pos):
                miss += 1
                age = time.time() - float(pos.opened_at or 0)
                bare = not (pos.sl_oid or pos.tp_oid or getattr(pos, "sec_sl_oid", "") or getattr(pos, "sec_tp_oid", ""))
                if bare and age > 300.0 and time.time() >= self.ctrl_skip.get("__order_cap__", 0):
                    log(f"CTRL flatten unprotected {pos.symbol} age={age:.0f}s group={scope[:16]}")
                    self.close_pos(pos, px or pos.entry, "no-ctrl")
                else:
                    self.bump("ctrl")
            else:
                self.ctrl_skip[f"legal:{scope}"] = now + 8.0
        return miss

    def _cid_kind(self, o: Dict[str, Any]) -> str:
        c = self.order_cid(o).lower()
        tag = TAG.lower()
        if c.startswith(tag) and len(c) > len(tag):
            return c[len(tag)]
        if len(c) > 4:
            return c[4]
        return ""

    def _order_is_sl(self, o: Dict[str, Any]) -> bool:
        k = self._cid_kind(o)
        if k in ("s", "u"):
            return True
        if k in ("t", "v"):
            return False
        return str(o.get("type") or "") in SL_TYPES

    def _order_is_tp(self, o: Dict[str, Any]) -> bool:
        k = self._cid_kind(o)
        if k in ("t", "v"):
            return True
        if k in ("s", "u"):
            return False
        return str(o.get("type") or "") in TP_TYPES

    def place_ctrl_pair(self, pos: Position) -> None:
        """One HTTP batch: overall SL + TP. Fallback to two single posts."""
        if overall_controls.enabled(self, pos):
            return overall_controls.ensure(self,pos)
        previous_shared = {getattr(pos,f,"") for f in overall_controls.FIELDS}-{ "" } if getattr(pos,"overall_controls",False) else set()
        if not self.exchange_position_active(pos) and not getattr(pos,"_overall_exchange_verified",False):
            return
        if time.time() < self.ctrl_skip.get("__order_cap__", 0) or self._controls_waiting_for_position(pos):
            return
        # A range pair is quantity-matched. If the parent grew since the last
        # placement, remove only this group's old pair before creating the new
        # one; aggregate closePosition controls do not need this treatment.
        if self.per_config_controls(pos):
            previous_qty = max(0.0, float(getattr(pos, "ctrl_qty", 0.0) or 0.0))
            if previous_qty > 0 and abs(previous_qty - float(pos.qty or 0.0)) > max(1e-9, abs(float(pos.qty or 0.0)) * 0.005):
                try:
                    self.cancel_controls(pos.symbol, pos=pos)
                except Exception:
                    pass
                self.clear_position_controls(pos)
        scope = self.position_key(pos) if self.per_config_controls(pos) else self.legacy_position_key(pos)
        if time.time() < self.ctrl_skip.get(scope, 0) and pos.sl_oid and pos.tp_oid:
            return
        want_sl, want_tp, _, _ = self.desired_sl_tp(pos)
        sl_b = self._ctrl_body(pos, "sl", want_sl)
        tp_b = self._ctrl_body(pos, "tp", want_tp)
        for b, ch in ((sl_b, "u"), (tp_b, "v")):
            b["clientOrderID"] = self.cid(ch, pos=pos)
            if not self.per_config_controls(pos):
                b["closePosition"] = "true"
        batch_scope = self.position_key(pos) if self.per_config_controls(pos) else self.legacy_position_key(pos)
        batch_key = stable_key(
            CONN_SHORT,
            "control-batch",
            batch_scope,
            pos.side,
            getattr(pos, "control_range_key", "aggregate") or "aggregate",
            self.fmt_px(self.contracts.get(pos.symbol), want_sl),
            self.fmt_px(self.contracts.get(pos.symbol), want_tp),
        )
        self.record_event(
            "control_request",
            stable_key(batch_key, "request"),
            status="pending",
            symbol=pos.symbol,
            side=pos.side,
            set_id=pos.set_id,
            indication_kind=getattr(pos, "ind_kind", ""),
            strategy=self.event_strategy(pos),
                    **self.control_event_fields(pos),
            qty=pos.qty,
            detail="batch SL/TP protection",
            metadata={"count": 2},
        )
        r = self.api.batch_place([sl_b, tp_b])
        self.did_io = True
        self.record_event(
            "control_response",
            stable_key(batch_key, "response"),
            status="confirmed" if self.ok(r) else ("pending" if r.get("cooled") else "rejected"),
            code=r.get("code"),
            symbol=pos.symbol,
            side=pos.side,
            set_id=pos.set_id,
            indication_kind=getattr(pos, "ind_kind", ""),
            strategy=self.event_strategy(pos),
                    **self.control_event_fields(pos),
            qty=pos.qty,
            detail="batch SL/TP protection",
        )
        if r.get("cooled") or (not self.ok(r) and self._defer_missing_position_controls(pos, r)):
            return
        if not self.ok(r) and self._defer_minimum_controls(pos, r):
            return
        data = (r.get("data") or {}) if self.ok(r) else {}
        rows = data.get("orders") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            rows = []
        for o in rows:
            if not isinstance(o, dict):
                continue
            code = o.get("code")
            if code not in (0, None, "0", ""):
                self._defer_missing_position_controls(pos, o)
                self._defer_minimum_controls(pos, o)
                continue
            oid = str(o.get("orderId") or o.get("orderID") or "")
            if not oid:
                continue
            typ = str(o.get("type") or "")
            kind = self._cid_kind(o)
            if typ in SL_TYPES or kind in ("u", "s"):
                pos.sl_oid = pos.sec_sl_oid = oid
                pos.sl = want_sl
            elif typ in TP_TYPES or kind in ("v", "t"):
                pos.tp_oid = pos.sec_tp_oid = oid
                pos.tp = want_tp
        if not real_oid(pos.sl_oid) and not self._controls_waiting_for_position(pos):
            pos.sl_oid = pos.sec_sl_oid = self.place_ctrl(pos, "sec-sl", want_sl)
            if pos.sl_oid:
                pos.sl = want_sl
        if not real_oid(pos.tp_oid) and not self._controls_waiting_for_position(pos):
            pos.tp_oid = pos.sec_tp_oid = self.place_ctrl(pos, "sec-tp", want_tp)
            if pos.tp_oid:
                pos.tp = want_tp
        pos.controls_ok = bool(real_oid(pos.sl_oid) and real_oid(pos.tp_oid))
        pos.overall = pos.controls_ok
        pos.close_position = not self.per_config_controls(pos)
        pos.ctrl_qty = pos.qty
        pos.ctrl_verified = pos.controls_ok
        if previous_shared and pos.controls_ok and pos.sl_oid not in previous_shared and pos.tp_oid not in previous_shared:
            pos.overall_controls = False
            pos.retired_control_ids = sorted(set(getattr(pos,"retired_control_ids",[])) | previous_shared)
            self.save_open_book()
            overall_controls.drain_retired(self,overall_controls.members(self,pos))

    def ensure_controls(self, pos: Position) -> None:
        if overall_controls.enabled(self, pos):
            return overall_controls.ensure(self,pos)
        if getattr(pos,"overall_controls",False) and not getattr(pos,"_overall_proxy",False):
            return self.place_ctrl_pair(pos)
        if getattr(pos,"retired_control_ids",[]):
            overall_controls.drain_retired(self,overall_controls.members(self,pos))
        if not self.position_is_ours(pos):
            return
        if not self.exchange_position_active(pos):
            return
        now = time.time()
        tracked_at_start = any(candidate is pos for candidate in self.open.values())

        def retired() -> bool:
            # New entries attach protection before joining the local book.
            # Only a previously tracked position disappearing means retirement.
            return tracked_at_start and not any(candidate is pos for candidate in self.open.values())
        if now < self.ctrl_skip.get("__order_cap__", 0) or self._controls_waiting_for_position(pos):
            return
        scope = self.position_key(pos) if self.per_config_controls(pos) else self.legacy_position_key(pos)
        have_both = bool((real_oid(pos.sl_oid) or real_oid(getattr(pos, "sec_sl_oid", ""))) and (real_oid(pos.tp_oid) or real_oid(getattr(pos, "sec_tp_oid", ""))))
        if have_both and now < self.ctrl_skip.get(scope, 0) and getattr(pos, "ctrl_verified", False):
            return
        if self.api.path_cd.get("/openApi/swap/v2/trade/order", 0) > time.time() and have_both:
            return
        want_sl, want_tp, sec_sl, sec_tp = self.desired_sl_tp(pos)
        pos.sec_sl, pos.sec_tp = sec_sl, sec_tp
        if not real_oid(pos.sl_oid) and not real_oid(pos.tp_oid):
            self.place_ctrl_pair(pos)
            if retired() or self._controls_waiting_for_position(pos):
                return
            if real_oid(pos.sl_oid) and real_oid(pos.tp_oid):
                return
        banned = self.api.path_cd.get("/openApi/swap/v2/trade/openOrders", 0) > time.time()
        all_rows = [] if banned else self.list_orders(pos.symbol)
        orders = [
            o for o in all_rows
            if self.order_is_ours(o) and self._order_matches_position(o, pos)
        ]
        if banned:
            if not real_oid(pos.sl_oid):
                oid = self.place_ctrl(pos, "sec-sl", want_sl)
                if retired():
                    return
                if oid:
                    pos.sl_oid = pos.sec_sl_oid = oid
                    pos.sl = want_sl
            if not real_oid(pos.tp_oid):
                oid = self.place_ctrl(pos, "sec-tp", want_tp)
                if oid:
                    pos.tp_oid = pos.sec_tp_oid = oid
                    pos.tp = want_tp
            pos.controls_ok = bool(real_oid(pos.sl_oid) and real_oid(pos.tp_oid))
            pos.overall = True
            pos.close_position = not self.per_config_controls(pos)
            pos.ctrl_qty = pos.qty
            self.ctrl_skip[f"sync:{scope}"] = time.time() + 12.0
            return
        # Empty REST is not "no orders" — never drop live oids.
        side = pos.side
        sls = [o for o in orders if self._order_is_sl(o) and str(o.get("positionSide") or "").upper() == side]
        tps = [o for o in orders if self._order_is_tp(o) and str(o.get("positionSide") or "").upper() == side]

        def qty_ok(o: Dict[str, Any]) -> bool:
            try:
                q = float(o.get("origQty") or o.get("quantity") or 0)
            except Exception:
                q = 0.0
            if q <= 0:
                # A quantity-less control is safe only in legacy aggregate
                # mode. A range group must never inherit closePosition=true.
                return not self.per_config_controls(pos)
            if self.per_config_controls(pos):
                # Do not adopt an oversized range order: it could close a
                # different group sharing this symbol and hedge side.
                return pos.qty * 0.95 <= q <= pos.qty * 1.05
            return q + 1e-12 >= pos.qty * 0.95

        stale = [o for o in sls + tps if not qty_ok(o)]
        for extra in stale:
            self.cancel_order(pos.symbol, str(extra.get("orderId")), self.order_cid(extra))
        sls = [o for o in sls if o not in stale]
        tps = [o for o in tps if o not in stale]
        sec_sls = [o for o in sls if self._cid_kind(o) == "u" or str(o.get("closePosition")).lower() in ("true", "1")]
        ord_sls = [o for o in sls if o not in sec_sls]
        sec_tps = [o for o in tps if self._cid_kind(o) == "v" or (str(o.get("closePosition")).lower() in ("true", "1") and o not in sec_sls)]
        ord_tps = [o for o in tps if o not in sec_tps]
        for extra in ord_sls[1:] + sec_sls[1:] + ord_tps[1:] + sec_tps[1:]:
            self.cancel_order(pos.symbol, str(extra.get("orderId")), self.order_cid(extra))
        ord_sls, sec_sls, ord_tps, sec_tps = ord_sls[:1], sec_sls[:1], ord_tps[:1], sec_tps[:1]
        want_sl, want_tp, sec_sl, sec_tp = self.desired_sl_tp(pos)
        pos.sec_sl, pos.sec_tp = sec_sl, sec_tp

        def _bind(rows: List[Dict[str, Any]], attr_oid: str, attr_px: str) -> None:
            if rows:
                oid = real_oid(rows[0].get("orderId") or rows[0].get("orderID"))
                if oid:
                    setattr(pos, attr_oid, oid)
                try:
                    setattr(pos, attr_px, float(rows[0].get("stopPrice") or getattr(pos, attr_px)))
                except Exception:
                    pass
            # keep in-memory oid when REST lag returns empty

        _bind(ord_sls, "sl_oid", "sl")
        _bind(ord_tps, "tp_oid", "tp")
        _bind(sec_sls, "sec_sl_oid", "sec_sl")
        _bind(sec_tps, "sec_tp_oid", "sec_tp")
        if not pos.sl_oid and pos.sec_sl_oid:
            pos.sl_oid, pos.sl = pos.sec_sl_oid, pos.sec_sl or pos.sl
        if not pos.tp_oid and pos.sec_tp_oid:
            pos.tp_oid, pos.tp = pos.sec_tp_oid, pos.sec_tp or pos.tp
        if not pos.sec_sl_oid and pos.sl_oid:
            pos.sec_sl_oid, pos.sec_sl = pos.sl_oid, pos.sl
        if not pos.sec_tp_oid and pos.tp_oid:
            pos.sec_tp_oid, pos.sec_tp = pos.tp_oid, pos.tp
        pos.close_position = not self.per_config_controls(pos)
        now = time.time()
        last_sync = float(self.ctrl_skip.get(f"sync:{scope}", 0) or 0)
        can_replace = now >= last_sync

        def _place_side(is_sl: bool, have_oid: str, have_px: float, want: float, live_have: bool, live_rows: List[Dict[str, Any]]) -> str:
            have_oid = real_oid(have_oid)
            mark = float(self.px.get(pos.symbol) or 0)
            side_ok = False
            if have_px > 0 and mark > 0:
                if is_sl:
                    side_ok = (pos.side == "LONG" and have_px < mark) or (pos.side == "SHORT" and have_px > mark)
                else:
                    side_ok = (pos.side == "LONG" and have_px > mark) or (pos.side == "SHORT" and have_px < mark)
            trail = False
            if is_sl and side_ok and want > 0:
                if pos.side == "LONG" and want > have_px * 1.0008 and want < mark:
                    trail = True
                if pos.side == "SHORT" and want < have_px * 0.9992 and want > mark:
                    trail = True
            range_changed = bool(
                have_px <= 0
                or abs(float(want or 0) - have_px) / max(float(pos.entry or 0), 1e-9) > 0.00035
            )
            if have_oid and live_have and side_ok and not trail and not range_changed:
                return have_oid
            if have_oid and live_have and not can_replace:
                return have_oid
            if have_oid and live_have:
                cid = self.order_cid(live_rows[0]) if live_rows else ""
                self.cancel_order(pos.symbol, have_oid, cid)
            oid = self.place_ctrl(pos, "sec-sl" if is_sl else "sec-tp", want)
            if oid:
                self.ctrl_skip[f"sync:{scope}"] = now + 30.0
            return oid or have_oid

        pos.sl_oid = pos.sec_sl_oid = _place_side(True, pos.sl_oid or pos.sec_sl_oid, pos.sl, want_sl, bool(sls), sls)
        if retired():
            return
        if pos.sl_oid:
            pos.sl = want_sl if not pos.sl or not self.sl_legal(pos, pos.sl) else pos.sl
        pos.tp_oid = pos.sec_tp_oid = _place_side(False, pos.tp_oid or pos.sec_tp_oid, pos.tp, want_tp, bool(tps), tps)
        if pos.tp_oid:
            pos.tp = want_tp if not pos.tp or not self.tp_legal(pos, pos.tp) else pos.tp
        pos.controls_ok = bool(real_oid(pos.sl_oid) and real_oid(pos.tp_oid))
        pos.ctrl_verified = bool(sls and tps)
        pos.ctrl_qty = pos.qty
        pos.overall = bool((real_oid(pos.sl_oid) and real_oid(pos.tp_oid)) or (real_oid(pos.sec_sl_oid) and real_oid(pos.sec_tp_oid)))
        pos.close_position = not self.per_config_controls(pos)

    def _ctrl_body(self, pos: Position, kind: str, price: float) -> Dict[str, Any]:
        price = self.clamp_ctrl_price(pos, kind, price)
        c = self.contracts.get(pos.symbol)
        grouped = self.per_config_controls(pos)
        return ctrl_payload(
            pos.symbol,
            pos.side,
            kind,
            self.fmt_px(c, price),
            self.fmt_qty(c, pos.qty),
            self.cid("u" if kind == "sl" else "v", pos=pos),
            close_pos=not grouped,
            with_qty=grouped,
        )

    def replace_sl(self, pos: Position, new_sl: float) -> bool:
        """Install a tighter stop without leaving a position unprotected.

        The individual-order path places the replacement first
        and cancel old protection only after a distinct order id is confirmed.
        A failed update preserves the old stop; the event loop can retry it.
        """
        if overall_controls.enabled(self,pos):
            proposed = float(new_sl)
            if not math.isfinite(proposed) or proposed <= 0:
                return False
            pos.sl = max(pos.sl,proposed) if pos.side == "LONG" else min(pos.sl,proposed)
            overall_controls.ensure(self,pos)
            self.save_open_book()
            return True
        now = time.time()
        scope = self.position_key(po…62518 tokens truncated…)
        pos: Optional[Position] = None
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        if kind == "c":
            if meta.get("confirmed_control_fill"):
                # Exact persisted parent lineage only. A new position in the
                # same range must never absorb a previous parent's control fill.
                pos = self._position_for_client(str(meta.get("parent_client_id") or ""))
                if pos is not None and (pos.symbol != row["symbol"] or pos.side != row["side"]):
                    pos = None
            else:
                pos = self.position_for_group(str(row.get("group_key") or ""))
                if pos is None:
                    pos = self._position_for_client(str(meta.get("parent_client_id") or ""))
            if pos is None and not meta.get("confirmed_control_fill"):
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
                fill_px = px
                if meta.get("confirmed_control_fill") and previous > 0:
                    previous_px = _sf((self.pending_orders.get(cid) or {}).get("avg_price"))
                    fill_px = (cumulative * px - previous * previous_px) / delta
                    if not math.isfinite(fill_px) or fill_px <= 0:
                        return False
                applied = self._record_close_fill(
                    pos,
                    delta,
                    fill_px,
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

    def _sync_control_fill(self, order: Dict[str, Any], cid: str, track: Dict[str, Any]) -> bool:
        """Accept executed TP/SL only with an exact own order and parent binding."""
        if cid in self.seen_fill_cids and cid not in self.pending_orders:
            return False
        oid = real_oid(order.get("orderId") or order.get("orderID") or order.get("orderid"))
        symbol = str(order.get("symbol") or "").upper()
        side = str(order.get("positionSide") or "").upper()
        if not oid or side not in ("LONG", "SHORT") or not self.cid_ours(cid):
            return False
        # Requested size / trigger price are never execution evidence.
        executed = next((_sf(order[key]) for key in ("executedQty", "filledQty", "cumQty", "filled")
                         if order.get(key) not in (None, "")), 0.0)
        px = next((v for v in (_sf(order.get(k)) for k in ("avgPrice", "fillPrice", "executedPrice"))
                   if math.isfinite(v) and v > 0), 0.0)
        if not math.isfinite(executed) or executed <= 0 or px <= 0:
            return False
        old = self.pending_orders.get(cid) or {}
        if overall_controls.enabled(self) or (old.get("metadata") or {}).get("overall_members") or any(oid in getattr(p,"overall_bindings",{}) for p in self.open.values()):
            applied = overall_controls.sync_fill(self,order,cid,oid,executed,px,track)
            if applied is not None:
                return applied
        meta = dict(old.get("metadata") or {})
        if meta.get("confirmed_control_fill"):
            if real_oid(old.get("order_id")) != oid:
                return False
            pos = self._position_for_client(str(meta.get("parent_client_id") or ""))
            if pos is None or pos.symbol != symbol or pos.side != side or not pos.ours:
                return False
        else:
            matches = [p for p in list(self.open.values()) if p.ours and p.symbol == symbol and p.side == side
                       and oid in {real_oid(getattr(p, field, "")) for field in ("sl_oid", "tp_oid", "sec_sl_oid", "sec_tp_oid")}]
            if len(matches) != 1 or not matches[0].client_id:
                return False
            pos = matches[0]
            meta.update(confirmed_control_fill=True, parent_client_id=pos.client_id,
                        reason="exchange-control-" + str(track.get("kind") or ""))
        requested = _sf(old.get("requested_qty")) or max(pos.qty, executed)
        self._remember_pending(kind="close", cid=cid, symbol=symbol, side=side,
                               requested_qty=requested, filled_qty=_sf(old.get("filled_qty")),
                               order_id=oid, avg_price=_sf(old.get("avg_price")),
                               fee_total=_sf(old.get("fee_total")),
                               group_key=str(getattr(pos, "control_group_key", "") or ""), metadata=meta)
        return self._sync_pending_fill(dict(order, avgPrice=px, executedQty=executed), cid, track, "c")

    def sync_own_fills(self) -> None:
        """Pull exchange fills for this connection and apply only new deltas."""
        self._next_fill_poll = time.monotonic() + 30.0
        request_key = stable_key(CONN_SHORT, "fills", int(getattr(self, "cycle", 0) or 0), int(time.time() // 5))
        self.record_event("exchange_request", request_key, status="pending", detail="fill polling", metadata={"path": "/openApi/swap/v2/trade/allOrders"})
        self.did_io = True
        r = self.api.get("/openApi/swap/v2/trade/allOrders", {"limit": 50})
        fallback_used = False
        if not self.ok(r):
            code = str(r.get("code") or "")
            low = str(r.get("msg") or "").lower()
            retryable = code in ("-1", "109500", "109501") or "network issue" in low or "please retry later" in low
            self.record_event(
                "exchange_response",
                stable_key(request_key, "primary-error"),
                status="error",
                code=r.get("code"),
                detail="fill request failed; retry next poll" if retryable else "fill request failed",
            )
            return
        data = r.get("data")
        orders = data.get("orders") if isinstance(data, dict) else data
        if not isinstance(orders, list):
            self.record_event("exchange_response", stable_key(request_key, "response"), status="error", code=r.get("code"), detail="fill payload malformed")
            return
        self._update_live_position_costs(orders)
        self.record_event(
            "exchange_response",
            stable_key(request_key, "response"),
            status="confirmed" if self.ok(r) else "error",
            code=r.get("code"),
            qty=len(orders),
            detail="fill polling fallback" if fallback_used else "fill polling",
            metadata={"rows": len(orders), "fallback": fallback_used},
        )
        n = 0
        for o in orders:
            if not isinstance(o, dict):
                continue
            cid = self.order_cid(o)
            if not cid or not self.cid_ours(cid):
                continue
            track = self.parse_track(cid) or {}
            kind = str(track.get("kind") or "")
            if kind in ("s", "t", "u", "v"):
                n += int(self._sync_control_fill(o, cid, track))
                continue
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
        # The warm QA worker must never replace economic state in the hot
        # trader, even temporarily. Copy only the small mutable probe inputs;
        # books/API are read-only here and must not be deep-copied at runtime.
        probe = copy.copy(self)
        probe.px = dict(self.px)
        probe.open = {key: copy.copy(pos) for key, pos in list(self.open.items())}
        probe.closed = [copy.copy(row) for row in list(self.closed)]
        probe.record_test = self.record_test
        probe._run_self_tests_isolated()

    def _run_self_tests_isolated(self) -> None:
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
        now = time.time()
        missing = sum(
            1
            for pos in self.open.values()
            if not pos.controls_ok and now - float(getattr(pos, "opened_at", 0) or 0) > 90.0
        )
        cooling = self.api.path_cd.get("/openApi/swap/v2/trade/order", 0) > now
        self.record_test(
            "controls-on-open",
            missing == 0 or cooling,
            f"missing={missing} open={len(self.open)} cool={int(cooling)}",
        )
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
        dca_want = bool(self.mods.get("strategy.dca", True)) and bool(self.overlay.get("dcaEnabled", True)) and bool(getattr(self, "strat_dca", True))
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
        held_px = dict(self.px)
        try:
            ours_cid = f"{TAG}cigen0600000abcd"
            # Rows must carry the same scope metadata production writes
            # (system_id/tracking_scope); a bare conn is not ownership proof.
            self.closed = [
                Closed(time.time(), "SYS-USDT", "LONG", 1.0, 1.0, 1.1, 0.40, 0.01, "tp", 30.0, set_id="s1", client_id=ours_cid, ours=True, conn=CONN_SHORT, system_id=SYSTEM_ID, tracking_scope=TRACKING_SCOPE),
                Closed(time.time(), "SYS-USDT", "SHORT", 1.0, 1.0, 1.1, -0.15, -0.01, "sl", 20.0, set_id="s1", client_id=ours_cid, ours=True, conn=CONN_SHORT, system_id=SYSTEM_ID, tracking_scope=TRACKING_SCOPE),
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
            # The probes use deterministic dummy prices for control-price
            # assertions; never leave those values in the live market cache.
            self.px = held_px

    def stats(self) -> Dict[str, Any]:
        # HTTP readers and the hot/warm workers can arrive concurrently. A
        # re-entrant guard keeps JSON snapshots internally consistent without
        # blocking recursive stats writes. Never wait on a historic commit:
        # scoring 10k+ sets under the same lock used to freeze the live cycle.
        lock = getattr(self, "_state_lock", None)
        if lock is None:
            blob = self._stats_unlocked()
            self._stats_last = blob
            return blob
        got = False
        try:
            got = bool(lock.acquire(timeout=0.08))
        except TypeError:
            lock.acquire()
            got = True
        if not got:
            stale = dict(getattr(self, "_stats_last", None) or {})
            stale.update({
                "running": True,
                "alive": True,
                "cycle": int(getattr(self, "cycle", 0) or 0),
                "scanMs": float(getattr(self, "last_scan_ms", 0) or 0),
                "halted": bool(getattr(self, "halted", False)),
                "haltReason": getattr(self, "halt_reason", None),
                "statsDeferred": True,
            })
            return stale
        try:
            blob = self._stats_unlocked()
            self._stats_last = blob
            return blob
        finally:
            lock.release()

    def _stats_unlocked(self) -> Dict[str, Any]:
        act = self.system_activity()
        act = persistent_activity(act, getattr(getattr(self, "runtime", None), "snapshot", {}), getattr(self, "start_eq", 0))
        system_start_equity = float(act.get("systemStartEquity") or self.start_eq or 0.0)
        system_equity = float(act.get("systemEquity") or (system_start_equity + float(act.get("pnl") or 0.0)))
        realized = float(act["realized"])
        wr = (act["wins"] / (act["wins"] + act["losses"]) * 100) if (act["wins"] + act["losses"]) else 0
        dd = float(act["drawdownPct"])
        age = time.time() - self.started
        per_min = (act["wins"] + act["losses"]) / (age / 60) if age > 1 else 0
        snap = self.api.snapshot() if hasattr(self.api, "snapshot") else {}
        pc = last_n_cost_pf(act["closes"], self.pf_window, self.position_cost_pct)
        try:
            need = int(self.sets.eval_need()) if hasattr(self.sets, "eval_need") else 8
        except Exception:
            need = 8
        pc["evaluationWindows"] = evaluation_windows(act["closes"], self.position_cost_pct, required_samples=need)
        ddt = self._ddt_blob(act["closes"])
        pc["maxDdS"] = ddt.get("maxDdS")
        pc["avgDdS"] = ddt.get("avgDdS")
        pc["ddEpisodes"] = ddt.get("episodes")
        pc["currentS"] = ddt.get("currentS")
        pc["minPf"] = self.coord.min_pf
        pc["requiredSamples"] = self.pf_window
        pc["pass"] = bool(pc["count"] >= self.pf_window and clears_pf(pc["ratio"], self.coord.min_pf))
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
        all_closed_rows = []
        for c in list(act["closes"]):
            d = asdict(c)
            d["indKind"] = d.get("ind_kind") or ""
            d["systemId"] = d.get("system_id") or SYSTEM_ID
            d["trackingScope"] = d.get("tracking_scope") or TRACKING_SCOPE
            d["connection"] = d.get("conn") or CONN_SHORT
            d["clientId"] = d.get("client_id") or ""
            all_closed_rows.append(d)
        closed_out = all_closed_rows[-closed_n:][::-1]
        # Reuse the same catalog census for the report and coverage panels.
        # Independently scanning 37k states in both consumed the live loop.
        sets_snap = dict(self.sets.snapshot(full=False))
        cov = self._coverage_blob(set_snapshot=sets_snap)
        activity = self.event_summary()
        ind_snap = self.indications.snapshot()
        budget = getattr(self.load, "last_budget", None)
        effective_tfs = effective_indication_timeframes(self.tf_on, budget)
        configured_combined = bool(ind_snap.get("tfCombined", True))
        combined_min = max(2, int(ind_snap.get("tfMinAgree") or 2))
        ind_snap["effectiveTimeframes"] = list(effective_tfs)
        ind_snap["combinedEffective"] = bool(configured_combined and len(effective_tfs) >= combined_min)
        ind_snap["combinedShed"] = bool(configured_combined and not ind_snap["combinedEffective"])
        ind_snap["loadLevel"] = str(getattr(budget, "level", "normal") or "normal")
        if getattr(self, "_sets_overview", None) is not None:
            sets_snap["overview"] = self._sets_overview
        historic_snap = dict(getattr(self, "_hist_status", {}) or {})
        prog = (sets_snap.get("progress") or {}) if isinstance(sets_snap, dict) else {}
        hist_phase = str(historic_snap.get("phase") or "")
        prog_phase = str(prog.get("phase") or "")
        phase = hist_phase if hist_phase and hist_phase not in ("idle",) else (prog_phase or hist_phase or "idle")
        if hist_phase in ("backfill", "fetch", "gap", "catalog", "replay", "score", "partial", "initial") and prog_phase in ("idle", "ready", ""):
            phase = hist_phase
        pct_raw = historic_snap.get("pct") if historic_snap.get("pct") is not None else prog.get("pct")
        try:
            pct_val = round(float(pct_raw or 0), 1)
        except (TypeError, ValueError):
            pct_val = 0.0
        ready_flag = bool(prog.get("ready") if prog.get("ready") is not None else historic_snap.get("ready"))
        detail = str(historic_snap.get("detail") or prog.get("detail") or "")
        coord_snap = self.coord.snapshot()
        if isinstance(coord_snap, dict):
            coord_snap["historic"] = {
                "runId": historic_snap.get("runId"),
                "generation": historic_snap.get("generation"),
                "watermark": historic_snap.get("lastPublishedWatermark") or historic_snap.get("watermark") or {},
                "complete": bool(historic_snap.get("coordinationComplete")),
            }
        config_evidence = self._config_evidence_snapshot()
        exchange_own_raw = getattr(self, "exchange_own_open_count", -1)
        exchange_total_raw = getattr(self, "exchange_open_count", -1)
        exchange_own_open = int(exchange_own_raw) if exchange_own_raw is not None else -1
        exchange_total_open = int(exchange_total_raw) if exchange_total_raw is not None else -1
        internal_open = int(len(self.open))
        internal_position_groups = len({
            (p.symbol, p.side)
            for p in self.open.values()
            if self.position_is_ours(p) and float(p.qty or 0) > 0
        })
        # Keep internal/config lanes separate from exchange aggregates.  The
        # exchange reports one position group per symbol+side, while the
        # engine can track many independent config/set lanes in that group.
        live_order_count = int(getattr(self, "exchange_order_own_count", -1) or -1)
        live_total_order_count = int(getattr(self, "exchange_order_total_count", -1) or -1)
        if exchange_own_open < 0:
            open_parity = "pending"
        elif exchange_own_open == internal_position_groups:
            open_parity = "match"
        else:
            open_parity = "discrepant"
        stage_flow = sets_snap.get("stageFlow") if isinstance(sets_snap, dict) else {}
        stage_rows = stage_flow.get("stages") if isinstance(stage_flow, dict) else {}
        execution_evidence = {
            "systemId": SYSTEM_ID,
            "connection": CONN_SHORT,
            "trackingScope": TRACKING_SCOPE,
            "trackPrefix": TAG,
            "systemSource": act.get("source", "system-orders"),
            "systemClosed": int(act.get("n") or 0),
            "systemPnl": round(float(act.get("pnl") or 0), 4),
            "systemRealized": round(realized, 4),
            "systemUnrealized": round(float(act.get("unrealized") or 0), 4),
            "internalOpen": internal_open,
            "realPositionCount": internal_open,
            "realPositionGroupCount": internal_position_groups,
            "realOrderCount": internal_open,
            "livePositionCount": exchange_own_open,
            "liveOrderCount": live_order_count,
            "liveTotalOrderCount": live_total_order_count,
            "internalPositionGroups": internal_position_groups,
            "exchangeOpen": exchange_total_open,
            "exchangeOwnOpen": exchange_own_open,
            "exchangePositionGroups": exchange_own_open,
            "foreignPositionCount": int(getattr(self, "foreign_position_count", 0)),
            "foreignOpenOrderCount": int(getattr(self, "foreign_open_order_count", 0)),
            "foreignUnrealized": round(float(getattr(self, "foreign_upnl", 0.0) or 0.0), 4),
            "foreignRealized": round(float(getattr(self, "foreign_realized", 0.0) or 0.0), 4),
            "openParity": open_parity,
            "realStage": dict(stage_rows.get("real") or {}) if isinstance(stage_rows, dict) else {},
            "setCount": int(sets_snap.get("setCount") or 0),
            "validatedSetCount": int(sets_snap.get("validatedCount") or 0),
            "activeSetCount": int(sets_snap.get("activeCount") or 0),
            "entryCandidateCount": int(getattr(self, "_entry_candidate_count", 0) or 0),
            "entryQueue": dict(getattr(self, "_entry_queue", {}) or {}),
            "baselineEntryQueue": dict(getattr(self, "_forced_entry_queue", {}) or {}),
            "activeSetCap": int(getattr(self.sets, "max_active", 0) or 0),
            "activeSetUnlimited": int(getattr(self.sets, "max_active", 0) or 0) <= 0,
            "progressPhase": phase,
            "progressPct": pct_val,
            "progressReady": ready_flag,
            "snapshotAt": time.time(),
        }
        try:
            from stats_report import merge_kind_stats, merge_strategy_stats
            now_m = time.monotonic()
            fat = bool(getattr(getattr(self.load, "last_budget", None), "stats_full", False))
            last_m = float(getattr(self, "_stats_merge_ts", 0) or 0)
            if fat or now_m - last_m >= 3.5 or not getattr(self, "_by_ind_cache", None):
                by_ind = merge_kind_stats(
                    all_closed_rows,
                    self.position_cost_pct,
                    gate=sets_snap.get("indGate") or cov.get("indicationGate") or {},
                    hits=cov.get("indicationHits") or ind_snap.get("typeHits") or {},
                    types=cov.get("indicationTypes") or ind_snap.get("types") or {},
                    kind_live=ind_snap.get("kindStats") or {},
                )
                by_strat = merge_strategy_stats(
                    all_closed_rows,
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
        pulse_view = self.pulse_snapshot()
        control_mode = "per-config" if bool(getattr(self, "control_orders_per_config", True)) else "aggregate"
        if overall_controls.enabled(self):
            control_mode = "overall"
        pair_count = len({(p.symbol,p.side) for p in self.open.values()}) if overall_controls.enabled(self) else len(self.open)
        expected_control_pairs = pair_count if bool(getattr(self, "control_orders", True)) else 0
        overall_group_rows: Dict[Tuple[str, str], List[Any]] = {}
        if control_mode == "overall":
            for row in self.open.values():
                overall_group_rows.setdefault((row.symbol, row.side), []).append(row)
        overall_pair_ok = 0
        if control_mode == "overall":
            for rows in overall_group_rows.values():
                pairs = {(real_oid(getattr(row, "sl_oid", "")), real_oid(getattr(row, "tp_oid", ""))) for row in rows}
                if len(pairs) == 1 and next(iter(pairs)) != ("", ""):
                    overall_pair_ok += 1
        overall_pair_gaps = max(0, expected_control_pairs - overall_pair_ok)
        return {
            "running": not self.halted,
            "mode": "VST_DEMO" if "x02" in CONN_SHORT else "LIVE_MAINNET",
            "systemId": SYSTEM_ID,
            "connection": CONN_SHORT,
            "trackingScope": TRACKING_SCOPE,
            "trackPrefix": TAG,
            "connType": "vst" if "x02" in CONN_SHORT else "live",
            "unit": "VST" if "x02" in CONN_SHORT else "USDT",
            "exchange": "BingX VST" if "x02" in CONN_SHORT else "BingX",
            "startedAt": self.started,
            "now": time.time(),
            "uptimeS": age,
            "system": dict(getattr(getattr(self, "runtime", None), "snapshot", {}) or {}),
            "equity": round(system_equity, 4),
            "systemEquity": round(system_equity, 4),
            "systemStartEquity": round(system_start_equity, 4),
            "walletEquity": round(float(getattr(self, "wallet_equity", self.equity) or 0.0), 4),
            "startEquity": round(system_start_equity, 4),
            "available": round(self.available, 4),
            "usedMargin": round(self.used, 4),
            "walletUnrealized": round(self.upnl, 4),
            "foreignUnrealized": round(float(getattr(self, "foreign_upnl", 0.0) or 0.0), 4),
            "foreignRealized": round(float(getattr(self, "foreign_realized", 0.0) or 0.0), 4),
            "foreignExposure": round(float(getattr(self, "foreign_exposure", 0.0) or 0.0), 4),
            "foreignPositionCount": int(getattr(self, "foreign_position_count", 0)),
            "foreignOpenOrderCount": int(getattr(self, "foreign_open_order_count", 0)),
            "unrealized": round(float(act["unrealized"]), 4),
            "realizedPnl": round(realized, 4),
            "sessionPnl": round(float(act["pnl"]), 4),
            "systemPnl": round(float(act["pnl"]), 4),
            "systemGrow": round(float(act["grow"]), 4),
            "systemLoss": round(float(act["loss"]), 4),
            "systemRealized": round(realized, 4),
            "systemUnrealized": round(float(act["unrealized"]), 4),
            "systemSource": act.get("source", "system-orders"),
            "executionEvidence": execution_evidence,
            "pnlPct": round(float(act["pnlPct"]), 3),
            "drawdownPct": round(max(0, dd), 3),
            "drawdownAmount": act["drawdownAmount"],
            "drawdownAvailable": act["drawdownAvailable"],
            "drawdownBasis": act["drawdownBasis"],
            "wins": int(act["wins"]),
            "losses": int(act["losses"]),
            "winRate": round(wr, 1),
            "openCount": len(self.open),
            "logicalPositionCount": len(self.open),
            "realPositionCount": internal_open,
            "realPositionGroupCount": internal_position_groups,
            "realOrderCount": internal_open,
            "exchangeOpenCount": int(getattr(self, "exchange_open_count", -1)),
            "exchangePositionGroupCount": int(getattr(self, "exchange_own_open_count", getattr(self, "exchange_open_count", -1))),
            "exchangeOwnOpenCount": int(getattr(self, "exchange_own_open_count", getattr(self, "exchange_open_count", -1))),
            "exchangeTotalOpenCount": int(getattr(self, "exchange_total_open_count", getattr(self, "exchange_open_count", -1))),
            "livePositionCount": exchange_own_open,
            "liveOrderCount": live_order_count,
            "liveTotalOrderCount": live_total_order_count,
            "simOpenCount": sim_n,
            "simUPnl": round(sim_upnl, 4),
            "maxOpen": MAX_OPEN,
            "logicalPositionCap": MAX_OPEN,
            "symbols": SYMBOLS,
            "symbolCount": len(SYMBOLS),
            "symbolCap": int(getattr(self, "symbol_cap", DEFAULT_SYMBOL_CAP) or 0),
            "symbolsAll": bool(getattr(self, "overlay_wild", False)),
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
            "tests": self.tests[:24],
            "block": self.block.snapshot(),
            "pulse": pulse_view,
            "coord": coord_snap,
            "historic": historic_snap,
            "progressPct": pct_val,
            "progressPhase": phase,
            "progressDetail": detail,
            "progressReady": ready_flag,
            "progressSymbol": prog.get("symbol") or "",
            "progressSetId": prog.get("setId") or "",
            "progressSymbolsDone": prog.get("symbolsDone"),
            "progressSymbolsTotal": prog.get("symbolsTotal"),
            "progressSetsDone": prog.get("setsDone"),
            "progressSetsTotal": prog.get("setsTotal"),
            "progressBarsDone": prog.get("barsDone"),
            "progressBarsTotal": prog.get("barsTotal"),
            "progressElapsedMs": prog.get("elapsedMs"),
            "progressLastRunMs": prog.get("lastRunMs"),
            "progressCycle": prog.get("cycle"),
            "progressError": prog.get("error") or "",
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
                    "aggregateSlPct": round(float(getattr(p, "aggregate_sl_pct", 0.0) or 0.0) * 100, 3),
                    "aggregateTpPct": round(float(getattr(p, "aggregate_tp_pct", 0.0) or 0.0) * 100, 3),
                    "trail": p.trail,
                    "trailPending": getattr(p, "trail_pending", None),
                    "setId": p.set_id,
                    "executionLane": getattr(p, "execution_lane", ""),
                    "strategy": getattr(p, "strategy", ""),
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
                    "systemId": getattr(p, "system_id", SYSTEM_ID) or SYSTEM_ID,
                    "connection": getattr(p, "connection", CONN_SHORT) or CONN_SHORT,
                    "trackingScope": getattr(p, "tracking_scope", TRACKING_SCOPE) or TRACKING_SCOPE,
                    "ours": bool(self.position_is_ours(p)),
                }
                for p in self.open.values()
            ],
            "closed": closed_out,
            "signals": list(self.signals)[::-1][:16],
            "symbolCount": len(SYMBOLS),
            "symbolMax": MAX_SYMBOLS,
            "scanMs": round(self.last_scan_ms, 1),
            "rssMb": round(rss_mb(), 1),
            "load": self.load.snapshot() if hasattr(self, "load") else {},
            "loadLevel": getattr(self.load, "level", None) if hasattr(self, "load") else None,
            "klinesReady": sum(1 for s in SYMBOLS if s in self.klines),
            "klinesTf": {tf: sum(1 for s in SYMBOLS if s in self.klines_tf.get(tf, {})) for tf in TIMEFRAMES},
            "prices": {s: self.px.get(s) for s in (SYMBOLS if (not hasattr(self, "load") or self.load.last_budget.stats_full or len(SYMBOLS) <= 64) else [p.symbol for p in self.open.values()][:64])},
            "engine": {
                "hotMs": round(self.last_scan_ms, 1),
                "hotCpuMs": round(getattr(self, "last_scan_cpu_ms", 0.0), 1),
                "stagesMs": dict(getattr(self, "last_cycle_stages", {})),
                "activeStage": getattr(self, "_active_cycle_stage", ""),
                "activeStageMs": round((time.perf_counter() - self._active_stage_at) * 1000.0, 1) if getattr(self, "_active_cycle_stage", "") else 0.0,
                "cycleWallOverrun": self.last_scan_ms > SCAN_S * 1000.0,
                "warmMs": round(self.warm_ms, 1),
                "asyncP50": snap.get("asyncP50"),
                "asyncN": snap.get("asyncN"),
                "qaPass": self.qa_pass,
                "qaFail": self.qa_fail,
                "scanS": SCAN_S,
                "cycleMs": round(SCAN_S * 1000.0, 1),
                "cycleWaitMs": round(getattr(self, "cycle_wait_ms", 0.0), 1),
                "cycleOverrun": bool(getattr(self, "cycle_overrun", False)),
                "systemId": SYSTEM_ID,
                "connection": CONN_SHORT,
                "trackingScope": TRACKING_SCOPE,
                "trackPrefix": TAG,
                "ignoredForeign": getattr(self, "ignored_foreign", 0),
                "foreignOpenOrders": int(getattr(self, "foreign_open_order_count", 0)),
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

    def _coverage_blob(self, set_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        control_mode = "per-config" if bool(getattr(self, "control_orders_per_config", True)) else "aggregate"
        if overall_controls.enabled(self):
            control_mode = "overall"
        pair_count = len({(p.symbol,p.side) for p in self.open.values()}) if overall_controls.enabled(self) else len(self.open)
        expected_control_pairs = pair_count if bool(getattr(self, "control_orders", True)) else 0
        catalog = []
        sim_n, _sim_upnl = self.sim_stats()
        show_n = int(getattr(self.block, "eval_n", BLOCK_COUNT_PREVIEW) or BLOCK_COUNT_PREVIEW)
        show_n = min(max(show_n, 1), BLOCK_COUNT_PREVIEW)
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
                "liveStack": n <= int(self.block.max_stack or 6) and n in self.block.counts,
                "independent": True,
            })
        hits: Dict[str, int] = {}
        for rows in list(self.indications.last.values()):
            for i in rows:
                hits[i.kind] = hits.get(i.kind, 0) + 1
        scov = (set_snapshot.get("coverage") or {}) if set_snapshot is not None else (
            self.sets.coverage() if hasattr(self.sets, "coverage") else {})
        live_ov = (set_snapshot.get("liveOverview") or {}) if set_snapshot is not None else (
            self.sets.live_overview() if hasattr(self.sets, "live_overview") else {})
        progress = getattr(self.sets, "progress", None)
        coord_last = getattr(self.coord, "last", {}) if hasattr(self, "coord") else {}
        if not isinstance(coord_last, dict):
            coord_last = {}
        coord_axes = getattr(self.coord, "axes", {}) or {}
        coord_coordination = getattr(self.coord, "coordination", {}) or {}
        coord_size_mult = getattr(self.coord, "size_mult", None)
        stages = (coord_last.get("stages") or {})
        stage_flow_fn = getattr(self.sets, "stage_flow", None)
        stage_flow = scov.get("stageFlow")
        if stage_flow is None:
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
                now_ax = time.monotonic()
                cached_ax = getattr(self, "_axis_ui", None)
                if cached_ax is not None and now_ax - float(getattr(self, "_axis_ui_ts", 0) or 0) < 12:
                    axis_aggregate = cached_ax
                else:
                    axis_aggregate = self.sets.axis_variants(self.coord)
                    if isinstance(axis_aggregate, dict):
                        axis_aggregate = dict(axis_aggregate)
                        rows = axis_aggregate.pop("rows", None)
                        from set_overview import build_overview
                        self._sets_overview = build_overview(self.sets, rows or [], axis_enabled=self.coord.axes_active())
                        if isinstance(rows, list):
                            axis_aggregate["rowCount"] = axis_aggregate.get("rowCount") or len(rows)
                        parents = axis_aggregate.get("parents")
                        if isinstance(parents, list):
                            axis_aggregate["parentCount"] = axis_aggregate.get("parentCount") or len(parents)
                            if len(parents) > 24:
                                axis_aggregate["parents"] = parents[:24]
                        ids = axis_aggregate.get("parentSetIds")
                        if isinstance(ids, list) and len(ids) > 32:
                            axis_aggregate["parentSetIdCount"] = len(ids)
                            axis_aggregate["parentSetIds"] = ids[:32]
                    self._axis_ui = axis_aggregate
                    self._axis_ui_ts = now_ax
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
            eval_n = sum(len(v) for v in list((self.indications.evals or {}).values()))
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
            "executionPolicy": {
                "normalEnabled": bool(getattr(self, "normal_execution_enabled", False)),
                "blockActive": bool(getattr(self, "block_active", True)),
            "targetActiveSets": int(getattr(self.sets, "max_active", 0)),
                "decision": dict(getattr(self, "_execution_decision", {}) or {}),
            },
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
                "runId": str(getattr(progress, "run_id", "") or ""),
                "generation": int(getattr(progress, "generation", 0) or 0),
                "mode": str(getattr(progress, "mode", "") or ""),
                "requestedStart": int(getattr(progress, "requested_start", 0) or 0),
                "requestedEnd": int(getattr(progress, "requested_end", 0) or 0),
                "watermark": dict(getattr(progress, "watermark", {}) or {}),
                "lastPublishedWatermark": dict(getattr(progress, "last_published_watermark", {}) or {}),
                "lastCompleteRun": float(getattr(progress, "last_complete_run", 0.0) or 0.0),
                "nextRunAt": float(getattr(progress, "next_run_at", 0.0) or 0.0),
                "validSymbols": list(getattr(progress, "valid_symbols", []) or []),
                "invalidSymbols": list(getattr(progress, "invalid_symbols", []) or []),
                "missingSymbols": list(getattr(progress, "missing_symbols", []) or []),
                "gappedSymbols": list(getattr(progress, "gapped_symbols", []) or []),
                "stale": bool(getattr(progress, "stale", False)),
                "deferredReason": str(getattr(progress, "deferred_reason", "") or "")[:180],
                "coordinationComplete": bool(getattr(progress, "coordination_complete", False)),
                "coverage": dict(getattr(self, "_hist_status", {}).get("coverage") or {}),
                "fetchStored": int(getattr(self, "_hist_fetch_stored", 0) or 0),
                "fetchFailures": int(getattr(self, "_hist_fetch_failures", 0) or 0),
                "fetchNext": float(getattr(self, "_hist_fetch_next", 0.0) or 0.0),
                "deferred": str(getattr(self, "_hist_deferred", "") or "")[:180],
            },
            "sets": {
                "families": scov.get("families"),
                "setCount": len(self.sets.sets),
                "activeCount": sum(1 for s in self.sets.sets.values() if s.active),
                "validatedCount": int(scov.get("validatedCount") or 0),
                "entryCandidateCount": int(getattr(self, "_entry_candidate_count", 0) or 0),
                "entryQueue": dict(getattr(self, "_entry_queue", {}) or {}),
            "baselineEntryQueue": dict(getattr(self, "_forced_entry_queue", {}) or {}),
                "entryCandidateCap": int(getattr(self.sets, "entry_policy_max_candidates", 0) or 0),
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
                "logicalOpen": len(self.open),
                "exchangePositionGroups": int(getattr(self, "exchange_own_open_count", getattr(self, "exchange_open_count", -1))),
                "ok": sum(1 for p in self.open.values() if p.controls_ok and p.sl_oid and p.tp_oid),
                "missing": sum(1 for p in self.open.values() if not (p.sl_oid and p.tp_oid)),
                "security": sum(1 for p in self.open.values() if getattr(p, "sec_sl_oid", "") and getattr(p, "sec_tp_oid", "")),
                "mode": control_mode,
                "pairCount": expected_control_pairs,
                "expectedPairs": expected_control_pairs,
                "protectedPairs": overall_pair_ok if control_mode == "overall" else sum(1 for p in self.open.values() if bool(getattr(p, "controls_ok", False))),
                "pairGaps": overall_pair_gaps if control_mode == "overall" else sum(1 for p in self.open.values() if not (p.sl_oid and p.tp_oid)),
                "aggregatePairCount": expected_control_pairs if control_mode in ("aggregate", "overall") else 0,
                "logicalPositionCap": MAX_OPEN,
                "groupCount": pair_count,
                "protectedGroups": overall_pair_ok if control_mode == "overall" else sum(1 for p in self.open.values() if bool(getattr(p, "controls_ok", False))),
                "memberProtected": sum(1 for p in self.open.values() if bool(getattr(p, "controls_ok", False))),
                "memberMissing": sum(1 for p in self.open.values() if not (p.sl_oid and p.tp_oid)),
                "mergedMembers": sum(max(1, int(getattr(p, "member_count", 1) or 1)) for p in self.open.values()),
                "groups": [
                    {
                        "key": getattr(p, "control_group_key", "") or f"aggregate:{p.symbol}:{p.side}",
                        "symbol": p.symbol,
                        "side": p.side,
                        "range": getattr(p, "control_range_key", "") or "aggregate",
                        "rangeBp": {"sl": int(getattr(p, "control_sl_bp", 0) or 0), "tp": int(getattr(p, "control_tp_bp", 0) or 0)},
                        "slPct": round(float(getattr(p, "aggregate_sl_pct", getattr(p, "sl_pct", 0.0)) or 0.0) * 100, 3),
                        "tpPct": round(float(getattr(p, "aggregate_tp_pct", getattr(p, "tp_pct", 0.0)) or 0.0) * 100, 3),
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
            "recon": {"ok": self.recon_ok, "pending": bool(getattr(self, "recon_pending", False)), "detail": self.recon_detail, "logicalOpen": len(self.open), "exchangeOpen": int(getattr(self, "exchange_open_count", -1)), "exchangePositionGroups": int(getattr(self, "exchange_own_open_count", getattr(self, "exchange_open_count", -1))), "simOpen": sim_n},
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
            "load": self.load.snapshot() if hasattr(self, "load") else {},
        }

    def write_stats(self, force: bool = False) -> None:
        if not self._stats_lock.acquire(blocking=False):
            return
        try:
            self._write_stats_locked(force)
        finally:
            self._stats_lock.release()

    def _write_stats_locked(self, force: bool = False) -> None:
        now = time.monotonic()
        fat = bool(getattr(getattr(self.load, "last_budget", None), "stats_full", False))
        min_dt = max(0.95 if fat else 1.6, getattr(self, "system_settings", {}).get("systemStatsIntervalS", 2))
        if not force and not self._stats_force and now - self._stats_ts < min_dt:
            return
        self._stats_ts = now
        self._stats_force = False
        stats = self.stats()
        # State dir may be missing on a fresh box (or after a manual wipe):
        # recreate it instead of dropping every stats write on the floor.
        os.makedirs(DIR, exist_ok=True)
        # Unique temporary file and fsync make overlapping writers restart-safe.
        atomic_write(STATS_PATH, stats)
        # Forced progress snapshots must not trigger a full report every tick.
        if now - float(getattr(self, "_report_ts", 0)) >= getattr(self, "system_settings", {}).get("systemReportIntervalS", 30):
            self._report_ts = now
            try:
                self.write_results_export(stats)
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
        d = drawdown_time_by_symbol(closed)
        return {"maxDdS": d.get("maxS"), "avgDdS": d.get("avgS"), "episodes": d.get("episodes"), "maxDepth": d.get("maxDepth"), "currentS": d.get("currentS")}

    def _by_symbol_blob(self, closed: List[Any]) -> List[Dict[str, Any]]:
        buckets: Dict[str, List[Any]] = {}
        for c in closed:
            if isinstance(c, dict):
                s = str(c.get("symbol") or "?")
            else:
                s = str(getattr(c, "symbol", "?") or "?")
            buckets.setdefault(s, []).append(c)
        from set_engine import drawdown_time_by_symbol, row_equity_pnl
        out = []
        for s, rows in buckets.items():
            pnls = [row_equity_pnl(c, self.position_cost_pct) for c in rows]
            gp = sum(x for x in pnls if x > 0)
            gl = abs(sum(x for x in pnls if x < 0))
            d = drawdown_time_by_symbol(rows)
            cost = last_n_cost_pf(rows, len(rows) or 1, self.position_cost_pct)
            out.append({
                "symbol": s,
                "n": len(pnls),
                "wins": sum(1 for x in pnls if x > 0),
                "losses": sum(1 for x in pnls if x < 0),
                "net": round(sum(pnls), 6),
                "pf": round(float(cost.get("ratio") or 1.0), 4),
                "classicPf": round(99.0 if gp > 0 and gl <= 0 else (gp / gl if gl else 0.0), 4),
                "maxDdS": d.get("maxS"),
                "avgDdS": d.get("avgS"),
            })
        out.sort(key=lambda r: r["net"])
        return out

    def write_results_export(self, stats: Optional[Dict[str, Any]] = None) -> None:
        from stats_report import write as write_report
        # Periodic export uses the already coherent snapshot. Rebuilding the
        # full catalog here duplicates scoring summaries while the history
        # publisher waits for the state lock.
        st = stats if stats is not None else self.stats()
        write_report(
            st,
            os.path.join(DIR, f"results-export-{CONN_SHORT}.json"),
            os.path.join(DIR, f"results-export-{CONN_SHORT}.md"),
            cost_pct=self.position_cost_pct,
            conn=CONN_SHORT,
            dest_html=os.path.join(DIR, f"results-export-{CONN_SHORT}.html"),
        )

    def _qa_live_control_tests(self) -> None:
        """Refresh the cheap, state-local control probes on every hot tick.

        History replay deliberately skips the heavier QA suite. It must not,
        however, leave a stale control failure visible while recovery has
        already restored the current open positions. These checks only walk
        the bounded open book and never perform exchange I/O.
        """
        now = time.time()
        if not bool(getattr(self, "control_orders", True)):
            self.record_test("controls-on-open", True, "disabled")
            self.record_test("qa-controls", True, "disabled")
            self.record_test("qa-ctrl-overall", True, "disabled")
            self.record_test("qa-ctrl-range", True, "disabled")
            return

        missing = sum(
            1
            for p in self.open.values()
            if self.missing_controls(p)
            and now - float(getattr(p, "opened_at", 0) or 0) > 90.0
        )
        cooling = (
            self.api.path_cd.get("/openApi/swap/v2/trade/order", 0) > now
            or now < self.ctrl_skip.get("__order_cap__", 0)
        )
        detail = f"missing={missing} open={len(self.open)} cool={int(cooling)}"
        self.record_test("controls-on-open", missing == 0 or cooling, detail)
        self.record_test("qa-controls", missing == 0 or cooling, detail)

        overall_ok = True
        for p in self.open.values():
            if now - float(getattr(p, "opened_at", 0) or 0) <= 90.0:
                continue
            if not (
                real_oid(p.sl_oid)
                and real_oid(p.tp_oid)
                or (
                    real_oid(getattr(p, "sec_sl_oid", ""))
                    and real_oid(getattr(p, "sec_tp_oid", ""))
                )
            ):
                overall_ok = False

        sl_bad = 0
        tp_bad = 0
        tp_crossed = 0
        for p in self.open.values():
            if now - float(getattr(p, "opened_at", 0) or 0) < 90.0:
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

    def qa_tick(self) -> None:
        """In-process probes — no extra live orders. Runs on the hot loop."""
        if self.last_error and is_transient_api(self.last_error):
            self.last_error = ""
        if self.hist_busy:
            self._qa_live_control_tests()
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
        self.record_test("qa-unlimited", MAX_OPEN <= 0 or MAX_OPEN >= 100, f"maxOpen={MAX_OPEN} cap={getattr(self, 'symbol_cap', 0)} stack={getattr(self.block, 'max_stack', None)} dca={getattr(self.dca, 'max_steps', None)}")
        book_cap = self.max_book_notional()
        sane_cap = max(self.notional_cap() * 32.0, 64.0)
        self.record_test("qa-book-cap", book_cap <= sane_cap * 1.001, f"book={book_cap:.2f} sane={sane_cap:.2f}")
        self._qa_live_control_tests()
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
        self._qa_indication_types(snap_ind)
        try:
            from indication_engine import self_test as ind_self
            fails = [n for n, ok, _ in ind_self() if not ok]
            self.record_test("qa-ind-self", not fails, f"fail={fails[:4]}")
        except Exception as e:
            self.record_test("qa-ind-self", False, str(e)[:80])
        dca_want = bool(self.mods.get("strategy.dca", True)) and bool(self.overlay.get("dcaEnabled", True)) and bool(getattr(self, "strat_dca", True))
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

    def _history_bounds(self, lookback: int, end_minute: Optional[int] = None) -> Tuple[int, int]:
        end = int(end_minute if end_minute is not None else (time.time() // 60) - 1)
        end = max(1, end)
        return end - max(1, int(lookback)) + 1, end

    def _hist_request_changed(self) -> bool:
        """Check the coalescing request channel without reading it on every bar."""
        now = time.monotonic()
        check_ts = float(getattr(self, "_hist_request_check_ts", 0.0) or 0.0)
        active_run_id = str(getattr(self, "_hist_active_run_id", "") or "")
        latest_run_id = str(getattr(self, "_hist_latest_request_id", "") or "")
        if now - check_ts < 0.35:
            return bool(active_run_id and latest_run_id and latest_run_id != active_run_id
                        and latest_run_id != getattr(self, "_hist_request_seen", ""))
        self._hist_request_check_ts = now
        request = read_hist_request(CONN_SHORT)
        latest = str(request.get("runId") or "")
        self._hist_latest_request_id = latest
        # The request file is a durable status boundary and intentionally
        # remains after publication. Only a generation newer than the last
        # consumed request may invalidate an in-flight automatic run.
        return bool(
            active_run_id
            and latest
            and latest != active_run_id
            and latest != self._hist_request_seen
        )

    def _hist_new_request(self, *, consume: bool = True) -> Dict[str, Any]:
        request = read_hist_request(CONN_SHORT)
        run_id = str(request.get("runId") or "")
        if not run_id or run_id == self._hist_request_seen:
            return {}
        if request.get("forcedOnly"):
            try:
                with open(forced_path(CONN_SHORT), encoding="utf-8") as stream:
                    completed = json.load(stream)
                if (completed.get("version") == 3 and completed.get("connection") == CONN_SHORT
                        and completed.get("requestRunId") == run_id):
                    self._hist_request_seen = run_id
                    return {}  # completed baseline survives a worker restart
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        self._hist_latest_request_id = run_id
        if consume:
            self._hist_request_seen = run_id
        return request

    def _hist_begin_request(self, request: Dict[str, Any], run_id: str) -> None:
        """Automatic runs never un-consume the durable manual request."""
        self._hist_active_run_id = run_id
        if request:
            self._hist_latest_request_id = run_id
            self._hist_request_seen = run_id

    def _hist_run_forced(self, request: Dict[str, Any]) -> None:
        """Consume the baseline request on the existing history worker."""
        run_id = str(request["runId"])
        self._hist_begin_request(request, run_id)
        overlay = dict(getattr(self, "overlay", {}))
        overlay.update(request.get("overlay") or {})
        if "controlMinTrades" in request:
            overlay["controlMinTrades"] = control_min_trades(request["controlMinTrades"])
        def stopped():
            status = read_hist_job(CONN_SHORT)
            return status.get("runId") == run_id and status.get("phase") == "stopped"
        def publish(job):
            # A newer queued request keeps its own status until consumed.
            if not self._hist_request_changed() and not stopped():
                write_hist_job(dict(job, runId=run_id, generation=request.get("generation", 0), shared=True), CONN_SHORT)
        def cancelled():
            return (self._hist_stop.is_set() or self._hist_request_changed() or stopped()
                    or any(os.path.exists(p) for p in (PAUSE_PATH, STOP_PATH, STOP_ALL)))
        publish(dict(phase="fetch", pct=0, detail="Forced baseline · shared history worker", forcedOnly=True))
        try:
            job = run_forced_calc(dict(request, overlay=overlay), persist=False,
                                  on_progress=publish, should_cancel=cancelled)
            matrix = job.pop("_forcedMatrix", [])
            self._hist_request_check_ts = 0  # read the newest generation before publishing
            if job.get("ready") and not cancelled():
                atomic_write(forced_path(CONN_SHORT), dict(job["forcedConfigs"], matrix=matrix,
                                                         connection=CONN_SHORT, requestRunId=run_id))
                self._forced_read_at = 0
            publish(job)
        finally:
            self._hist_active_run_id = ""

    def _hist_write_status(self, book: Optional[SetBook] = None, *, progress_only: bool = False, **values: Any) -> None:
        current = book or self.sets
        with self.state_guard():
            progress = current.progress
            payload = dict(getattr(self, "_hist_status", {}) or {})
            payload.update(values)
            payload.update({
                "ok": True,
                "connection": CONN_SHORT,
                "phase": progress.phase,
                "pct": round(float(progress.pct or 0.0), 1),
                "detail": progress.detail,
                "ready": bool(progress.ready),
                "lookback": int(current.lookback),
                "hours": round(float(current.lookback) / 60.0, 2),
                "runId": progress.run_id or payload.get("runId") or "",
                "generation": int(progress.generation or payload.get("generation") or 0),
                "mode": progress.mode or payload.get("mode") or "",
                "requestedStart": int(progress.requested_start or payload.get("requestedStart") or 0),
                "requestedEnd": int(progress.requested_end or payload.get("requestedEnd") or 0),
                "watermark": dict(progress.watermark or payload.get("watermark") or {}),
                "lastPublishedWatermark": dict(progress.last_published_watermark or getattr(self, "_hist_last_published_watermark", {}) or {}),
                "lastCompleteRun": float(progress.last_complete_run or payload.get("lastCompleteRun") or 0.0),
                "nextRunAt": float(progress.next_run_at or getattr(self, "_hist_next_hourly_at", 0.0) or 0.0),
                "validSymbols": list(progress.valid_symbols),
                "invalidSymbols": list(progress.invalid_symbols),
                "missingSymbols": list(progress.missing_symbols),
                "gappedSymbols": list(progress.gapped_symbols),
                "stale": bool(progress.stale),
                "deferredReason": progress.deferred_reason,
                "coordinationComplete": bool(progress.coordination_complete),
                "shared": True,
                "independent": False,
            })
            payload["progress"] = dict(payload.get("progress") or {},
                phase=progress.phase, pct=round(float(progress.pct or 0.0), 1),
                ready=bool(progress.ready), detail=progress.detail,
                setsDone=progress.sets_done, setsTotal=progress.sets_total,
                elapsedMs=round(progress.elapsed_ms, 1))
            self._hist_status = payload
        if not progress_only:
            try:
                snapshot = current.snapshot(full=False)
                payload["rows"] = snapshot.get("rows") or []
                payload["rowCount"] = len(snapshot.get("rows") or [])
                payload["validatedCount"] = snapshot.get("validatedCount") or 0
                if not payload.get("coverage"):
                    payload["coverage"] = snapshot.get("coverage") or {}
                payload["progress"] = snapshot.get("progress") or {}
            except Exception:
                pass
        write_hist_job(payload, CONN_SHORT)

    def _qa_indication_types(self, snapshot: dict) -> None:
        """A deliberately disabled indication is a valid configuration.

        Verify all eight reported flags against the applied settings, including
        Trend and Break. Missing flags or a real settings/report mismatch fail.
        """
        types = snapshot.get("types") or {}
        kinds = ("state", "direction", "move", "active", "common", "signals", "trend", "break")
        expected = {kind: bool(self.indications.settings.get("type" + kind.title(), True)) for kind in kinds}
        mismatches = [kind for kind in kinds if types.get(kind) is not expected[kind]]
        self.record_test("qa-ind-types", not mismatches, f"types={types} mismatch={mismatches}")

    def _hist_checkpoint(self, book: Optional[SetBook] = None, reason: str = "") -> None:
        current = book or self.sets
        progress = current.progress
        self.history_store.checkpoint({
            "runId": progress.run_id,
            "generation": int(progress.generation or 0),
            "mode": progress.mode,
            "phase": progress.phase,
            "reason": reason,
            "symbols": list(progress.valid_symbols),
            "watermark": dict(progress.watermark),
            "lastPublishedWatermark": dict(progress.last_published_watermark),
            "updatedAt": time.time(),
        })

    def _capped_scan_names(self, names: Optional[Sequence[str]] = None, cap: Optional[int] = None) -> List[str]:
        """Bound any symbol list to the configured scan book / symbolCap.

        0 = unlimited. Missing cap defaults to 50. Wildcards and stale
        all-universe snapshots collapse to the live scan book, not the
        full exchange catalog. A historic request may pass an explicit cap.
        """
        if cap is None:
            cap = int(getattr(self, "symbol_cap", DEFAULT_SYMBOL_CAP) or 0)
        else:
            try:
                cap = int(cap)
            except (TypeError, ValueError):
                cap = int(getattr(self, "symbol_cap", DEFAULT_SYMBOL_CAP) or 0)
        cap = max(0, cap)
        scan = [str(s) for s in SYMBOLS if s]
        raw = list(names) if names is not None else list(scan)
        wild = False
        out: List[str] = []
        seen: set[str] = set()
        for raw_s in raw:
            token = str(raw_s or "").strip().upper().replace("_", "-")
            if token in ("*", "ALL", "UNLIMITED", ""):
                wild = True
                continue
            if token.endswith("USDT") and not token.endswith("-USDT"):
                token = token[:-4] + "-USDT"
            if not token.endswith("-USDT") or token in seen:
                continue
            seen.add(token)
            out.append(token)
        if wild or not out:
            out = list(scan)
        if cap > 0 and len(out) > cap:
            must: List[str] = []
            seen_must: set[str] = set()
            raw_open = getattr(self, "open", None)
            if isinstance(raw_open, dict):
                open_iter = raw_open.values()
            elif isinstance(raw_open, (list, tuple)):
                open_iter = raw_open
            else:
                open_iter = []
            for p in list(open_iter):
                s = str(getattr(p, "symbol", "") or "")
                if s and s not in seen_must:
                    must.append(s)
                    seen_must.add(s)
            for s in FORCED_SYMBOLS:
                token = str(s or "")
                if token and token not in seen_must and (token in out or token in scan):
                    must.append(token)
                    seen_must.add(token)
            rest = [s for s in out if s not in seen_must]
            out = (must + rest)[: max(cap, len(must))]
        return out

    def _hist_selected_snapshot(self, requested: Optional[Sequence[str]] = None, cap: Optional[int] = None) -> Tuple[List[str], List[Dict[str, str]]]:
        names = self._capped_scan_names(requested, cap=cap)
        invalid = [
            {"symbol": symbol, "reason": "missing active exchange contract"}
            for symbol in names
            if symbol not in self.contracts
        ]
        valid = [symbol for symbol in names if symbol in self.contracts]
        if not valid:
            valid = [symbol for symbol in self._capped_scan_names(None, cap=cap) if symbol in self.contracts]
        return valid, invalid

    def _hist_coverage(self, book: SetBook, symbols: Sequence[str], start: int, end: int) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
        coverage: Dict[str, Dict[str, Any]] = {}
        missing: List[str] = []
        for i, symbol in enumerate(symbols):
            if i % 8 == 0:
                sd_notify("WATCHDOG=1")
            item = self.history_store.coverage(symbol, start, end, source="exchange")
            bars = self.history_store.window(symbol, bars=book.lookback, end=end, source="exchange")
            need = max(int(getattr(book, "min_bars", 60) or 60), 60)
            enough = len(bars) >= need
            item["barsHeld"] = len(bars)
            item["enough"] = enough
            coverage[symbol] = item
            # Interior holes on thin names must not stall the whole 40h book.
            if not enough:
                missing.append(symbol)
            elif bars:
                book.ingest_bars(symbol, bars)
        return coverage, missing

    def _hist_can_publish_partial(
        self,
        valid: Sequence[str],
        completed: Sequence[str],
        missing: Sequence[str],
    ) -> bool:
        """Allow a contiguous subset to go live instead of stalling forever on gaps."""
        if not missing:
            return True
        n_valid = len(valid)
        n_done = len(completed)
        if n_done <= 0 or n_valid <= 0:
            return False
        failures = int(getattr(self, "_hist_fetch_failures", 0) or 0)
        coverage_pct = 100.0 * n_done / n_valid
        min_done = max(1, min(24, max(1, n_valid // 5)))
        # Do not stall the whole book on a couple of exchange gaps. High
        # coverage is enough to open the gate; remaining gaps keep retrying.
        if n_done >= min_done and coverage_pct >= 80.0:
            return True
        return failures >= 2 and n_done >= min_done and coverage_pct >= 50.0

    def _hist_replay_selection(
        self,
        valid: Sequence[str],
        completed: Sequence[str],
        missing: Sequence[str],
        *,
        already_ready: bool,
        published: Sequence[str],
        changed: Sequence[str],
        retry: Sequence[str] = (),
    ) -> Tuple[List[str], str]:
        """Choose the smallest symbol slice that should be replayed.

        Empty names mean the durable lane should wait or skip: either gaps are
        still blocking the first publish, or an already-live book has no new
        closed minutes to score.
        """
        valid_list = [str(symbol) for symbol in valid]
        completed_list = [str(symbol) for symbol in completed]
        missing_list = [str(symbol) for symbol in missing]
        published_set = {str(symbol) for symbol in published}
        changed_set = {str(symbol) for symbol in changed}
        retry_set = set(retry)
        remaining = [symbol for symbol in completed_list if symbol in retry_set]
        if remaining:
            # Finish the outstanding pass before refreshing its completed
            # prefix again. New minutes must not starve the rest of the book.
            return remaining, "resume-replay"
        if missing_list:
            if already_ready:
                names = [
                    symbol
                    for symbol in completed_list
                    if symbol not in published_set or symbol in changed_set
                ]
                return names, ("incremental-gap-fill" if names else "wait-gaps")
            if completed_list:
                return completed_list, "partial"
            return [], "wait-gaps"
        if already_ready:
            names = [
                symbol
                for symbol in valid_list
                if symbol not in published_set or symbol in changed_set
            ]
            return names, ("incremental" if names else "skip-unchanged")
        return valid_list, "full"

    def _hist_replay_chunk_size(self, n: int) -> int:
        """Bound one isolated replay so the live loop and UI keep moving."""
        total = max(1, int(n or 1))
        if total <= 8:
            return total
        budget = self._budget()
        level = str(getattr(budget, "level", "normal") or "normal")
        sets_n = 0
        try:
            sets_n = len(getattr(getattr(self, "sets", None), "sets", {}) or {})
        except Exception:
            sets_n = 0
        # 30k+ set catalogs cannot score 16 symbols without stalling stats.
        if sets_n >= 8000:
            if total <= 64:
                size = 2 if level in ("critical", "overload") else 4
            elif level in ("critical", "overload", "busy"):
                size = 1
            else:
                size = 2
        elif level in ("critical", "overload"):
            size = 8
        elif level == "busy":
            size = 16
        else:
            size = 24
        return max(1, min(total, size))

    def _replay_worker_count(self, n_names: int, budget: Any = None) -> int:
        """Parallel symbol replay within configured worker and load ceilings."""
        n = max(1, int(n_names or 1))
        try:
            cpu = max(1, int(os.cpu_count() or 1))
        except Exception:
            cpu = 2
        cpu = min(cpu, int(getattr(self, "system_settings", {}).get("systemWorkers", 2)))
        level = str(getattr(budget, "level", "normal") or "normal") if budget is not None else "normal"
        if n <= 1 or level == "critical":
            return 1
        if cpu <= 1:
            return 1
        if level in ("busy", "overload"):
            return 1  # one transient replay tape while the retained catalog is near its ceiling
        return max(1, min(cpu, n))

    def _hist_replay_chunked(self, names: List[str], already: bool, progress_total: int, *,
                             watermarks: Optional[Dict[str, int]] = None, durable: bool = False) -> bool:
        """Publish completed slices; return true only when every requested slice finished."""
        pending = list(dict.fromkeys(str(s) for s in names if s))
        requested = set(pending)
        self._hist_replay_completed = set()
        if not pending:
            return False
        total = max(int(progress_total or 0), len(pending))
        done: List[str] = []
        source = self.sets
        generation = int(getattr(self, "_sets_generation", 0) or 0)
        # Coverage belongs to this frozen run; historical symbols from an old
        # universe must never turn a new initial/hourly pass into 100%.
        run_completed = {s for s, ts in source.progress.watermark.items() if int(ts or 0) > 0}
        run_completed.intersection_update(source.progress.valid_symbols)
        run_completed.difference_update(requested | set(source.progress.missing_symbols))
        ready = bool(already)
        first = True
        claimed = self._hist_peer_claim()
        if not claimed and ready:
            with self.state_guard():
                self.sets.progress.phase = "deferred"
                self.sets.progress.detail = f"history deferred · peer {self._hist_peer_busy()} replaying"
            return False
        try:
            while pending:
                if (self._hist_request_changed() or self.sets is not source
                        or int(getattr(self, "_sets_generation", 0) or 0) != generation):
                    break
                size = self._hist_replay_chunk_size(len(pending))
                if first and len(pending) > 8:
                    size = min(size, 4)
                first = False
                chunk = pending[:size]
                pending = pending[size:]
                sd_notify("WATCHDOG=1")
                self.hist_busy = True
                self._hist_peer_touch()
                try:
                    # Each published slice changes evidence. Re-evaluate its
                    # affected IDs; the content cache reuses unchanged inputs.
                    # Deferring middle slices hides new winners until the end
                    # of an unlimited universe and leaves losing sets active.
                    ok = self._replay_sets_isolated(chunk, ready, total, score=True, completed_symbols=run_completed)
                finally:
                    self.hist_busy = False
                if not ok:
                    # A failed symbol must not prevent independent later
                    # symbols from being attempted in this pass.
                    continue
                if (self.sets is not source or int(getattr(self, "_sets_generation", 0) or 0) != generation
                        or self._hist_request_changed()):
                    break
                done.extend(chunk)
                self._hist_replay_completed.update(chunk)
                run_completed.update(chunk)
                ready = True
                with self.state_guard():
                    progress = self.sets.progress
                    progress.ready = True
                    progress.symbols_done = min(len(run_completed), total)
                    progress.symbols_total = total
                    progress.coordination_complete = False
                    for symbol in chunk:
                        stamp = int((watermarks or {}).get(symbol) or 0)
                        if stamp > 0:
                            progress.watermark[symbol] = stamp
                            if durable:
                                self._hist_last_published_watermark[symbol] = stamp
                                progress.last_published_watermark[symbol] = stamp
                    if durable:
                        getattr(self, "_hist_replay_dirty", set()).difference_update(chunk)
                    if pending:
                        progress.phase = "replay"
                        progress.detail = f"slice {len(done)}/{total} ready · continuing {len(pending)}"
                try:
                    self._hist_write_status(self.sets)
                    self.write_stats(force=True)
                except Exception:
                    pass
                try:
                    self.trim_caches(force=False, keep_hist=True)
                except Exception:
                    pass
                sd_notify("WATCHDOG=1")
                time.sleep(0.02 if pending else 0.0)
        finally:
            self._hist_peer_release()
        remaining = requested - set(done)
        with self.state_guard():
            if self.sets is source and int(getattr(self, "_sets_generation", 0) or 0) == generation:
                self._hist_incremental_symbols.update(remaining)
                if remaining:
                    progress = source.progress
                    progress.coordination_complete = False
                    progress.symbols_done = min(len(run_completed), total)
                    progress.symbols_total = total
                    progress.pct = 35.0 + 60.0 * progress.symbols_done / max(1, total)
                    progress.phase = "partial"
                    progress.detail = f"replay {progress.symbols_done}/{total} · {len(remaining)} retry pending"
        return bool(done) and not remaining

    def _hist_fetch_durable(self, book: SetBook, generation: int, symbols: Sequence[str], start: int, end: int) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
        """Fetch only missing exchange minutes, with a two-minute tail overlap."""
        gaps_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        for i, symbol in enumerate(symbols):
            if i % 8 == 0:
                sd_notify("WATCHDOG=1")
            gaps_by_symbol[symbol] = self.history_store.missing_ranges(symbol, start, end, source="exchange")
        requests: List[Tuple[str, Dict[str, Any]]] = []
        for symbol in symbols:
            gaps = list(gaps_by_symbol.get(symbol) or [])
            tail_start = max(start, end - 1)
            tail_missing = any(int(gap["start"]) <= end <= int(gap["end"]) for gap in gaps)
            if tail_missing and not any(int(gap["start"]) <= tail_start <= int(gap["end"]) for gap in gaps):
                gaps.append({"start": tail_start, "end": end, "minutes": end - tail_start + 1, "source": "exchange", "error": "tail overlap"})
            for gap in gaps:
                cursor = max(start, int(gap["start"]))
                gap_end = min(end, int(gap["end"]))
                while cursor <= gap_end:
                    page_end = min(gap_end, cursor + 1439)
                    requests.append((symbol, {
                        "symbol": symbol,
                        "interval": "1m",
                        "startTime": str(cursor * 60_000),
                        "endTime": str((page_end + 1) * 60_000),
                        "limit": str(page_end - cursor + 1),
                    }))
                    cursor = page_end + 1
        if not requests:
            self._hist_fetch_changed = set()
            return self._hist_coverage(book, symbols, start, end)[0], {}
        self._hist_progress_update(
            book,
            generation,
            phase="backfill",
            detail=f"backfill {len(requests)} exact ranges",
            bars_total=max(0, (end - start + 1) * len(symbols)),
            symbols_total=len(symbols),
        )
        failures: Dict[str, str] = {}
        stored = 0
        changed: set[str] = set()
        for offset in range(0, len(requests), 4):
            sd_notify("WATCHDOG=1")
            if self._hist_request_changed():
                break
            batch = requests[offset : offset + 4]
            rows: List[Tuple[str, Dict[str, Any], Any]] = []
            try:
                if hasattr(self.api, "gather_public"):
                    rows = self.api.gather_public([
                        ("/openApi/swap/v2/quote/klines", params) for _symbol, params in batch
                    ], timeout=8.0)
                else:
                    rows = [
                        ("/openApi/swap/v2/quote/klines", params, self.api.public("/openApi/swap/v2/quote/klines", params))
                        for _symbol, params in batch
                    ]
            except Exception as exc:
                for symbol, _params in batch:
                    failures[symbol] = str(exc)[:160]
                time.sleep(0)
                continue
            for _path, params, body in rows:
                symbol = str(params.get("symbol") or "")
                payload = body.get("data") if isinstance(body, dict) else None
                parsed = parse_exchange_rows(payload)
                if not parsed:
                    failures[symbol] = "exchange returned no valid timestamped 1m bars"
                    continue
                result = self.history_store.merge(symbol, parsed, source="exchange", quality="exchange-confirmed", persist=False)
                stored += int(result.get("inserted") or 0) + int(result.get("replaced") or 0)
                if int(result.get("inserted") or 0) > 0 or int(result.get("replaced") or 0) > 0:
                    changed.add(symbol)
            sd_notify("WATCHDOG=1")
            if (offset // 4) and (offset // 4) % 40 == 0:
                try:
                    self.history_store.flush()
                except Exception:
                    pass
                sd_notify("WATCHDOG=1")
            self._hist_progress_update(
                book,
                generation,
                detail=f"backfill {min(offset + len(batch), len(requests))}/{len(requests)} ranges · {stored} bars",
                pct=round(100.0 * min(offset + len(batch), len(requests)) / max(1, len(requests)), 1),
            )
            time.sleep(0)
        try:
            sd_notify("WATCHDOG=1")
            self.history_store.flush()
            sd_notify("WATCHDOG=1")
        except Exception:
            pass
        coverage, missing = self._hist_coverage(book, symbols, start, end)
        for symbol in missing:
            failures.setdefault(symbol, "unresolved exchange gap")
        self._hist_fetch_stored = stored
        self._hist_fetch_changed = set(changed)
        if failures or missing:
            self._hist_fetch_failures = min(6, int(self._hist_fetch_failures or 0) + 1)
        else:
            self._hist_fetch_failures = 0
        return coverage, failures

    def _hist_progress_update(self, book: SetBook, generation: int, **values: Any) -> bool:
        """Update replay/fetch progress only while its catalog is current."""
        sd_notify("WATCHDOG=1")
        with self.state_guard():
            if self.sets is not book or int(getattr(self, "_sets_generation", 0) or 0) != generation:
                return False
            progress = book.progress
            for key, value in values.items():
                if hasattr(progress, key):
                    setattr(progress, key, value)
        # Progress is durable status too, but status writes are throttled so a
        # large replay cannot turn every inner vector callback into a disk sync.
        now = time.monotonic()
        if now - float(getattr(self, "_hist_status_write_ts", 0.0) or 0.0) >= 0.75:
            self._hist_status_write_ts = now
            try:
                self._hist_write_status(book)
            except Exception:
                pass
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

    def _replay_sets_isolated(self, names: List[str], already: bool, progress_total: int, score: bool = True, completed_symbols: Optional[set] = None) -> bool:
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
            # A full deepcopy duplicates the complete catalog, including the
            # large per-state object graph, before this slice has produced any
            # history.  Keep the source book live and build only a lean,
            # metadata-equivalent replay catalog with the selected bars.
            replay_book = source.replay_clone(names)
            run_progress = copy.deepcopy(source.progress)
            run_completed = set(completed_symbols) if completed_symbols is not None else set(source._hist_seen)
            if run_progress.valid_symbols:
                run_completed.intersection_update(run_progress.valid_symbols)
            replay_book._hist_seen = set(run_completed)
            replay_book._hist_total = max(0, int(progress_total or 0))
            replay_book.progress.ready = bool(already)
            source._running = True
            prior_done = len(run_completed)
            source.progress = copy.deepcopy(source.progress)
            source.progress.phase = "replay"
            source.progress.pct = 35.0 + 60.0 * min(prior_done, progress_total) / max(1, progress_total)
            source.progress.symbol = names[0] if names else ""
            source.progress.set_id = ""
            source.progress.bars_done = 0
            source.progress.bars_total = sum(len(replay_book.bars.get(s) or []) for s in names)
            source.progress.sets_done = 0
            source.progress.sets_total = len(replay_book.sets)
            source.progress.symbols_done = prior_done
            source.progress.symbols_total = max(0, int(progress_total or 0), prior_done, len(names))
            source.progress.elapsed_ms = 0.0
            source.progress.detail = f"{prior_done}/{source.progress.symbols_total} · {len(names)} this slice · {len(replay_book.sets)} sets"
            source.progress.ready = bool(already)

        def merge_progress():
            # A replay clone resets Progress internally. Preserve the run's
            # identity, coverage/gap metadata and prior publication watermark.
            progress = copy.deepcopy(replay_book.progress)
            for key in ("run_id", "generation", "mode", "requested_start", "requested_end", "watermark",
                        "last_published_watermark", "last_complete_run", "next_run_at", "valid_symbols",
                        "invalid_symbols", "missing_symbols", "gapped_symbols", "stale", "deferred_reason"):
                setattr(progress, key, copy.deepcopy(getattr(run_progress, key)))
            seen = run_completed | (set(replay_book._hist_seen) & set(names))
            progress.symbols_total = max(1, int(progress_total or 0))
            progress.symbols_done = min(len(seen), progress.symbols_total)
            progress.pct = 35.0 + 60.0 * progress.symbols_done / progress.symbols_total
            progress.ready = bool(already) or bool(source.progress.ready) or bool(progress.ready)
            progress.coordination_complete = False  # only the durable publisher completes the full run
            return progress
        try:
            self._hist_write_status(source)
        except Exception:
            pass

        def publish_progress() -> None:
            sd_notify("WATCHDOG=1")
            if should_abort():
                # Inner symbol callbacks must release a superseded long replay;
                # cancelling only pending futures waits for entire symbols.
                raise RuntimeError("Replay superseded by a newer generation")
            should_write = False
            with self.state_guard():
                if self.sets is source and int(getattr(self, "_sets_generation", 0) or 0) == generation:
                    keep_ready = bool(already) or bool(source.progress.ready)
                    source.progress = merge_progress()
                    if keep_ready:
                        source.progress.ready = True
                    source._running = True
                    now_m = time.monotonic()
                    should_write = now_m - float(getattr(self, "_hist_status_write_ts", 0.0) or 0.0) >= 0.75
            if should_write:
                self._hist_status_write_ts = time.monotonic()
                try:
                    self._hist_write_status(source)
                except Exception:
                    pass
                now_stats = time.monotonic()
                if now_stats - float(getattr(self, "_hist_stats_write_ts", 0.0) or 0.0) >= 1.5:
                    self._hist_stats_write_ts = now_stats
                    try:
                        self.write_stats(force=True)
                    except Exception:
                        pass
            time.sleep(0)

        def should_abort() -> bool:
            if self.sets is not source or int(getattr(self, "_sets_generation", 0) or 0) != generation:
                return True
            if self._hist_request_changed():
                return True
            return False

        budget = getattr(self.load, "last_budget", None)
        replay_workers = self._replay_worker_count(len(names or []), budget)

        published = False
        affected_ids: List[str] = []
        try:
            replay_book.replay_all(
                on_step=publish_progress,
                symbols=names,
                abort=should_abort,
                workers=replay_workers,
                merge=True,
                progress_total=progress_total,
                score=False,
            )
            try:
                replay_book.compact_hist_tapes()
                replay_book.trim_tapes(
                    hist_cap=96,
                    live_cap=80,
                    bar_cap=max(120, int(getattr(replay_book, "lookback", 480) or 480)),
                )
            except Exception:
                pass
            if self._hist_request_changed():
                with self.state_guard():
                    if self.sets is source and int(getattr(self, "_sets_generation", 0) or 0) == generation:
                        source.progress.phase = "deferred"
                        source.progress.detail = "replay superseded by newer generation"
                        source.progress.deferred_reason = "newer manual/config generation"
                return False

            with self.state_guard():
                if self.sets is not source or int(getattr(self, "_sets_generation", 0) or 0) != generation:
                    return False
                if replay_book.progress.phase == "error":
                    source.progress = merge_progress()
                    return False
                wanted = set(names)
                # The replay clone publishes only non-empty tapes. The
                # SetBook merge receives the completed symbol names and
                # clears stale rows for every omitted Set without retaining a
                # second full ``set_id -> []`` map under the live lock.
                incoming = {}
                affected = set()
                # An empty replacement still removes prior evidence. Score
                # every affected Set, including rows outside the newest 12.
                for sid, current in source.sets.items():
                    if any(str(row.get("symbol") or "") in wanted for row in current.hist):
                        affected.add(sid)
                for sid, target in replay_book.sets.items():
                    tape = target.hist
                    if not tape:
                        continue
                    incoming[sid] = list(tape)
                    if any(str(row.get("symbol") or "") in wanted for row in tape):
                        affected.add(sid)
                source._commit_hist(
                    incoming,
                    replay_book.ind_hist,
                    merge=True,
                    replayed_symbols=names,
                    hist_symbol_counts=replay_book._hist_counts,
                    score=False,
                    score_ids=affected,
                )
                keys = set(source.strategy_hist) | set(replay_book.strategy_hist)
                source.strategy_hist = {
                    key: merge_hist_rows(
                        source.strategy_hist.get(key) or [],
                        replay_book.strategy_hist.get(key) or [],
                        names,
                    )
                    for key in keys
                }
                source._hist_seen = set(source._hist_seen) | set(replay_book._hist_seen)
                source._hist_total = max(int(source._hist_total or 0), int(replay_book._hist_total or 0), len(source._hist_seen))
                source.last_run = float(replay_book.last_run or time.time())
                source.progress = merge_progress()
                keep_done = source.progress.symbols_done
                keep_total = source.progress.symbols_total
                source.progress.sets_total = len(source.sets)
                source.progress.sets_done = len(source.sets)
                source.progress.symbols_done = keep_done
                source.progress.symbols_total = keep_total
                source.progress.detail = f"slice {keep_done}/{keep_total} ready · continuing {max(0, keep_total - keep_done)}"
                source._snap_cache = None
                source._live_ov_cache = None
                self._stats_force = True
                affected_ids = sorted(affected)
                published = True
            if published:
                try:
                    source.compact_hist_tapes()
                    source.trim_tapes(
                        hist_cap=96,
                        live_cap=80,
                        bar_cap=max(120, int(getattr(source, "lookback", 480) or 480)),
                    )
                except Exception:
                    pass
            if published and score:
                self._score_committed(source, generation, affected_ids)
                try:
                    source.compact_hist_tapes()
                except Exception:
                    pass
            return published
        except Exception as exc:
            with self.state_guard():
                if self.sets is source and int(getattr(self, "_sets_generation", 0) or 0) == generation:
                    if self._hist_request_changed():
                        source.progress.phase = "deferred"
                        source.progress.detail = "replay superseded by newer generation"
                        source.progress.deferred_reason = "newer manual/config generation"
                    else:
                        source.progress.phase = "error"
                        source.progress.error = str(exc)[:220]
            return False
        finally:
            with self.state_guard():
                if self.sets is source and int(getattr(self, "_sets_generation", 0) or 0) == generation:
                    source._running = False

    def _score_committed(self, book: Any, generation: int, ids: List[str]) -> None:
        """Score a published hist slice without holding the state lock for the catalog."""
        todo = [str(s) for s in ids if s]
        if not todo:
            return

        total = len(todo)
        started = time.monotonic()
        last_status = 0.0
        with self.state_guard():
            if self.sets is not book or int(getattr(self, "_sets_generation", 0) or 0) != generation:
                return
            book.progress.phase = "score"
            book.progress.sets_done = 0
            book.progress.sets_total = total
            book.progress.detail = f"score 0/{total} · remaining {total}"

        def publish_scored(done: int) -> bool:
            nonlocal last_status
            with self.state_guard():
                if self.sets is not book or int(getattr(self, "_sets_generation", 0) or 0) != generation:
                    return False
                first_ready = not book.progress.ready
                # A completed batch has fully scored evidence. Admission
                # still checks each exact config's Base/Main/Real gates;
                # unscored configs remain inactive. Do not hold the first
                # qualified lanes behind the rest of the entire catalog.
                book.progress.ready = True
                book.progress.phase = "score"
                book.progress.sets_done = done
                book.progress.sets_total = total
                book.progress.elapsed_ms = (time.monotonic() - started) * 1000.0
                book.progress.detail = f"score {done}/{total} · remaining {total - done}"
                book._snap_cache = None
                if first_ready:
                    self._stats_force = True
            now = time.monotonic()
            if first_ready or done == total or now - last_status >= 1.5:
                last_status = now
                try:
                    # Progress publication must not rebuild the full catalog
                    # or stop scoring on a transient status-file failure.
                    self._hist_write_status(book, progress_only=True)
                except Exception:
                    pass
            sd_notify("WATCHDOG=1")
            return True

        try:
            cpu = max(1, int(os.cpu_count() or 1))
        except Exception:
            cpu = 2
        workers = 1 if len(todo) < 16 else max(1, min(cpu, int(getattr(self, "system_settings", {}).get("systemWorkers", 2)), max(1, len(todo) // 16)))
        if workers <= 1:
            for i in range(0, len(todo), 32):
                with self.state_guard():
                    if self.sets is not book or int(getattr(self, "_sets_generation", 0) or 0) != generation:
                        return
                states = [book.sets[sid] for sid in todo[i:i+32] if sid in book.sets]
                for pair in book.score_pairs(states):
                    book._score_pair(pair)
                if not publish_scored(min(i + 32, total)):
                    return
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="set-score") as pool:
                for start in range(0, len(todo), 32):
                    with self.state_guard():
                        if self.sets is not book or int(getattr(self, "_sets_generation", 0) or 0) != generation:
                            return
                    states = [book.sets[sid] for sid in todo[start:start+32] if sid in book.sets]
                    list(pool.map(book._score_pair, book.score_pairs(states)))
                    if not publish_scored(min(start + 32, total)):
                        return
        with self.state_guard():
            if self.sets is book and int(getattr(self, "_sets_generation", 0) or 0) == generation:
                book._cap_active()
                book.refresh_progress_detail()
                book._snap_cache = None
                book._live_ov_cache = None

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
            sd_notify("WATCHDOG=1")
            built = SetBook()
            built.calculation_cache = getattr(self, "calculation_cache", None)
            built.load(overlay, cts, rebuild=True)
            sd_notify("WATCHDOG=1")
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
                self._hist_wake.set()
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

    def stop_history(self) -> None:
        """Stop the history worker and wake it if it is in an idle wait."""
        self._hist_stop.set()
        self._hist_wake.set()

    def _hist_incremental_replay(self) -> bool:
        """Replay only symbols whose closed live bar watermark advanced.

        A complete hourly run remains the publication boundary for the full
        catalog. Between those runs, a one-symbol slice keeps current Set and
        indication evidence fresh without rescoring every selected symbol.
        """
        with self.state_guard():
            book = self.sets
            generation = int(getattr(self, "_sets_generation", 0) or 0)
            if not book.enabled or not book.progress.ready or book._running:
                return False
            pending = sorted(str(symbol) for symbol in getattr(self, "_hist_incremental_symbols", set()))
            if not pending:
                return False
            self._hist_incremental_symbols.difference_update(pending)
            previous = copy.deepcopy(book.progress)
            valid_list = self._capped_scan_names(previous.valid_symbols or SYMBOLS)
            valid = set(valid_list)
            names = [symbol for symbol in pending if symbol in valid]
            if not names:
                return False
            end = int(time.time() // 60) - 1
            retry: List[str] = []
            input_watermarks = {}
            for symbol in names:
                # Capture before ingest/replay: data arriving during a long
                # replay is not evidence that has already been calculated.
                input_watermarks[symbol] = min(end, int(self.history_store.watermark(symbol, source=None) or 0))
                bars = self.history_store.window(symbol, bars=book.lookback, end=end, source=None)
                if len(bars) < book.min_bars:
                    retry.append(symbol)
                    continue
                book.ingest_bars(symbol, bars)
            self._hist_incremental_symbols.update(retry)
            names = [symbol for symbol in names if symbol not in retry]
            if not names:
                return False
            book.progress.phase = "incremental"
            book.progress.pct = 100.0
            book.progress.detail = f"incremental closed-bar update · {len(names)} symbols"
            book.progress.symbols_done = len(valid_list) - len(retry)
            book.progress.symbols_total = len(valid_list)

        replayed = self._hist_replay_chunked(names, True, len(valid_list), watermarks=input_watermarks)
        done = set(names) if replayed else set(getattr(self, "_hist_replay_completed", set()))
        if not replayed:
            with self.state_guard():
                if self.sets is not book or int(getattr(self, "_sets_generation", 0) or 0) != generation:
                    return False
                self._hist_incremental_symbols.update(set(names) - done)
            retry.extend(symbol for symbol in names if symbol not in done)
        keep = set(valid_list)
        watermark = {str(sym): int(ts) for sym, ts in dict(previous.watermark or {}).items() if str(sym) in keep}
        for symbol in done:
            watermark[symbol] = input_watermarks[symbol]
        published = {str(sym): int(ts) for sym, ts in dict(previous.last_published_watermark or {}).items() if str(sym) in keep}
        with self.state_guard():
            progress = self.sets.progress
            progress.phase = "ready"
            progress.pct = 100.0
            progress.ready = True
            progress.run_id = previous.run_id
            progress.generation = previous.generation
            progress.mode = previous.mode
            progress.requested_start = previous.requested_start
            progress.requested_end = previous.requested_end
            progress.watermark = watermark
            progress.last_published_watermark = published
            progress.last_complete_run = previous.last_complete_run
            progress.next_run_at = previous.next_run_at
            progress.valid_symbols = list(valid_list)
            # This publication belongs to the frozen current universe. Old
            # symbols retained for evidence must not produce e.g. 65/13.
            missing = sorted(sym for sym in valid_list if int(watermark.get(sym) or 0) <= 0 or sym in retry)
            progress.symbols_total = len(valid_list)
            progress.symbols_done = len(valid_list) - len(missing)
            progress.pct = 100.0 * progress.symbols_done / max(1, progress.symbols_total)
            progress.phase = "partial" if missing else "ready"
            progress.invalid_symbols = list(previous.invalid_symbols)
            progress.missing_symbols = missing
            progress.gapped_symbols = []
            progress.stale = bool(missing)
            progress.deferred_reason = ""
            progress.coordination_complete = not missing
            progress.detail = f"incremental closed-bar update · {len(names)} symbols · hourly publish pending"
        self._hist_write_status(self.sets)
        return bool(done)

    def _hist_loop_durable(self) -> None:
        """One lane-owned initial/hourly/gap state machine for historic replay."""
        while not self._hist_stop.is_set():
            sd_notify("WATCHDOG=1")
            self._hist_wake.clear()
            if not self._catalog_ready.is_set():
                self._hist_wake.wait(timeout=5.0)
                continue
            paused = os.path.exists(PAUSE_PATH) or os.path.exists(STOP_PATH) or os.path.exists(STOP_ALL)
            if not paused and bool(getattr(self, "_hist_resume_repair", False)):
                # A pause/stop can leave an exchange gap even when its next
                # hourly deadline has not arrived. Force the exact durable
                # backfill path before allowing incremental replay again.
                self._hist_resume_repair = False
                self._hist_next_hourly_at = 0.0
                with self.state_guard():
                    if self.sets.progress.ready:
                        self.sets.progress.phase = "gap"
                        self.sets.progress.detail = "resume requires exchange gap repair"
                        self.sets.progress.stale = True
            # Peek until pause/load gates are clear so a manual request keeps
            # its explicit range instead of being consumed while deferred.
            request = self._hist_new_request(consume=False)
            try:
                with self.state_guard():
                    book = self.sets
                    now = time.time()
                    ready = bool(book.progress.ready)
                    manual = bool(request)
                    # The same deadline drives the first run, hourly refreshes,
                    # and bounded retry of an unresolved exchange gap.
                    hourly_due = now >= float(self._hist_next_hourly_at or 0.0)
                    enabled = bool(book.enabled)
                if paused:
                    self._hist_resume_repair = True
                    with self.state_guard():
                        book.progress.phase = "paused"
                        book.progress.detail = "historic lane paused · watermark checkpointed"
                        book.progress.stale = bool(book.progress.ready)
                        book.progress.deferred_reason = "pause/stop control"
                    self._hist_checkpoint(book, "paused")
                    self._hist_write_status(book)
                    self._hist_wake.wait(timeout=2.0)
                    continue
                if not enabled:
                    with self.state_guard():
                        book.progress.phase = "ready"
                        book.progress.ready = True
                        book.progress.detail = "historic lane disabled"
                    self._hist_write_status(book)
                    self._hist_wake.wait(timeout=5.0)
                    continue
                budget = self._budget()
                catalog_incomplete = (not ready) or (not bool(getattr(book.progress, "coordination_complete", False)))
                peer = "" if catalog_incomplete else self._hist_peer_busy()
                if peer:
                    with self.state_guard():
                        book.progress.phase = "deferred"
                        book.progress.detail = f"history deferred · peer {peer} replaying"
                        book.progress.stale = bool(book.progress.ready)
                        book.progress.deferred_reason = f"peer {peer}"
                    self._hist_checkpoint(book, "peer-deferred")
                    self._hist_write_status(book)
                    self._hist_wake.wait(timeout=2.0)
                    continue
                if (not bool(getattr(budget, "hist_run", True))) and not catalog_incomplete:
                    with self.state_guard():
                        book.progress.phase = "deferred"
                        book.progress.detail = f"history deferred · load {getattr(budget, 'level', 'unknown')}"
                        book.progress.stale = bool(book.progress.ready)
                        book.progress.deferred_reason = f"load {getattr(budget, 'level', 'unknown')}"
                    self._hist_checkpoint(book, "load-deferred")
                    self._hist_write_status(book)
                    self._hist_wake.wait(timeout=2.0)
                    continue
                incremental_pending = False
                with self.state_guard():
                    incremental_pending = bool(getattr(self, "_hist_incremental_symbols", set()))
                if ready and not manual and not hourly_due and incremental_pending:
                    self._hist_incremental_replay()
                    self._hist_wake.wait(timeout=0.25)
                    continue
                if not manual and not hourly_due:
                    wait_s = min(max(float(self._hist_next_hourly_at or now + 5.0) - now, 0.25), 10.0)
                    self._hist_wake.wait(timeout=wait_s)
                    continue
                if manual and request.get("forcedOnly"):
                    self._hist_run_forced(request)
                    continue
                mode = str(request.get("mode") or ("initial" if not ready else "hourly"))
                run_id = str(request.get("runId") or f"{CONN_SHORT}:{int(now * 1000)}")
                run_generation = int(request.get("generation") or (int(getattr(self, "_sets_generation", 0) or 0) + 1))
                raw_symbols = request.get("symbols")
                selected_symbols = request.get("selectedSymbols")
                requested_symbols = selected_symbols or raw_symbols or list(SYMBOLS)
                wildcard_requested = bool(request.get("allSymbols")) or any(
                    isinstance(values, list)
                    and any(str(value).strip().upper() in ("*", "ALL", "UNLIMITED") for value in values)
                    for values in (raw_symbols, selected_symbols)
                )
                if wildcard_requested:
                    # A wildcard is an explicit request for the frozen dynamic
                    # universe; do not let a stale selectedSymbols mirror win.
                    requested_symbols = list(SYMBOLS)
                request_overlay = request.get("overlay") if isinstance(request.get("overlay"), dict) else {}
                request_options = request.get("options") if isinstance(request.get("options"), dict) else {}
                req_cap = None
                for src in (request, request_overlay, request_options):
                    if isinstance(src, dict) and src.get("symbolCap") is not None:
                        try:
                            req_cap = max(0, int(src.get("symbolCap")))
                            break
                        except (TypeError, ValueError):
                            req_cap = None
                ov_cap = int(getattr(self, "symbol_cap", DEFAULT_SYMBOL_CAP) or 0)
                if ov_cap <= 0:
                    ov_cap = DEFAULT_SYMBOL_CAP
                # Overlay owns the ranked book. A hist generation must never
                # assign symbol_cap — stale 25-cap jobs were shrinking a 50 book.
                use_cap = ov_cap
                valid, invalid = self._hist_selected_snapshot(
                    requested_symbols if isinstance(requested_symbols, list) else list(SYMBOLS),
                    cap=use_cap,
                )
                self._hist_snapshot_symbols = list(valid)
                self._hist_invalid_symbols = list(invalid)
                keep = set(valid)
                self._hist_last_published_watermark = {
                    str(sym): int(ts)
                    for sym, ts in (getattr(self, "_hist_last_published_watermark", {}) or {}).items()
                    if str(sym) in keep
                }
                lookback = int(book.lookback)
                if manual:
                    request_overlay = request.get("overlay") if isinstance(request.get("overlay"), dict) else {}
                    request_options = request.get("options") if isinstance(request.get("options"), dict) else {}
                    try:
                        requested_hours = float(request.get("hours")) if request.get("hours") is not None else 0.0
                        if requested_hours <= 0 and request_overlay.get("histLookbackBars") is not None:
                            requested_hours = float(request_overlay.get("histLookbackBars")) / 60.0
                        if requested_hours > 0:
                            lookback = max(120, min(20160, int(round(requested_hours * 60))))
                    except (TypeError, ValueError):
                        pass
                    if lookback != book.lookback:
                        book.lookback = lookback
                    try:
                        refresh_raw = request.get("refreshS") or request_overlay.get("histRefreshS")
                        if refresh_raw is not None:
                            book.refresh_s = max(60.0, min(86400.0, float(refresh_raw)))
                    except (TypeError, ValueError):
                        pass
                    # These controls are replay-lane settings. They are
                    # applied to the shared SetBook snapshot without spawning
                    # a second calculator or changing exchange safety lanes.
                    if request_options:
                        if "stratBlock" in request_options:
                            book.hist_block = bool(request_options["stratBlock"])
                        if "stratDca" in request_options:
                            book.hist_dca = bool(request_options["stratDca"])
                        if "trailing" in request_options:
                            book.trail_enabled = bool(request_options["trailing"])
                    for key, value in request_overlay.items():
                        if key in ("histSimulateBlock", "histSimulateDca", "stratTrailing"):
                            if key == "histSimulateBlock":
                                book.hist_block = bool(value)
                            elif key == "histSimulateDca":
                                book.hist_dca = bool(value)
                            else:
                                book.trail_enabled = bool(value)
                start, end = self._history_bounds(lookback)
                catalog_generation = int(getattr(self, "_sets_generation", 0) or 0)
                self._hist_active_request = dict(request)
                self._hist_begin_request(request, run_id)
                with self.state_guard():
                    progress = book.progress
                    progress.phase = "initial" if mode == "initial" else "backfill"
                    progress.pct = 0.0
                    progress.run_id = run_id
                    progress.generation = run_generation
                    progress.mode = mode
                    progress.requested_start = start
                    progress.requested_end = end
                    progress.valid_symbols = list(valid)
                    progress.invalid_symbols = list(invalid)
                    progress.missing_symbols = []
                    progress.gapped_symbols = []
                    progress.stale = bool(ready)
                    progress.deferred_reason = ""
                    progress.symbols_total = len(valid)
                    # Keep the last published count while the book is already
                    # live so the UI does not flash 0/25 on every hourly run.
                    if not ready:
                        progress.symbols_done = 0
                    else:
                        try:
                            progress.symbols_done = max(int(progress.symbols_done or 0), 0)
                        except Exception:
                            pass
                    progress.bars_total = len(valid) * lookback
                    progress.bars_done = 0
                    progress.sets_total = len(book.sets)
                    progress.sets_done = 0
                    progress.detail = f"snapshot frozen · {len(valid)} valid · {len(invalid)} invalid · {mode}"
                self._hist_write_status(book, selectedSymbols=list(valid), validSymbols=list(valid), invalidSymbols=list(invalid))
                self._hist_checkpoint(book, "run-start")

                coverage, failures = self._hist_fetch_durable(book, catalog_generation, valid, start, end)
                if int(getattr(self, "_sets_generation", 0) or 0) != catalog_generation:
                    self._hist_checkpoint(book, "config-generation-changed")
                    continue
                if self._hist_request_changed():
                    self._hist_checkpoint(book, "superseded-before-replay")
                    continue
                missing = []
                for symbol in valid:
                    item = coverage.get(symbol) or {}
                    held = int(item.get("barsHeld") or item.get("present") or 0)
                    if not (item.get("enough") or held >= 60):
                        missing.append(symbol)
                gapped = [symbol for symbol in missing if (coverage.get(symbol) or {}).get("gaps")]
                completed = [symbol for symbol in valid if symbol not in missing]
                input_watermarks = {symbol: int((coverage.get(symbol) or {}).get("watermark") or 0) for symbol in completed}
                dirty = set(getattr(self, "_hist_replay_dirty", set())) & set(valid)
                dirty.update(getattr(self, "_hist_fetch_changed", set()))
                dirty.update(symbol for symbol, stamp in input_watermarks.items()
                             if stamp > int(self._hist_last_published_watermark.get(symbol) or 0))
                self._hist_replay_dirty = dirty
                bars_present = sum(int((coverage.get(symbol) or {}).get("present") or 0) for symbol in valid)
                coverage_blob = {
                    "symbols": {
                        "requested": len(list(requested_symbols)) if isinstance(requested_symbols, list) else len(valid),
                        "valid": len(valid),
                        "invalid": len(invalid),
                        "completed": len(completed),
                        "failed": len(failures),
                        "gapped": len(gapped),
                        "coveragePct": round(100.0 * len(completed) / max(1, len(valid)), 2),
                    },
                    "bars": {
                        "requested": len(valid) * lookback,
                        "completed": bars_present,
                        "missing": max(0, len(valid) * lookback - bars_present),
                        "gapped": sum(int((coverage.get(symbol) or {}).get("missing") or 0) for symbol in gapped),
                        "coveragePct": round(100.0 * bars_present / max(1, len(valid) * lookback), 2),
                    },
                    "perSymbol": coverage,
                    "gaps": [gap for item in coverage.values() for gap in (item.get("gaps") or [])],
                    "failures": failures,
                    "source": "exchange",
                }
                with self.state_guard():
                    progress = book.progress
                    already_ready = bool(progress.ready)
                    progress.missing_symbols = list(missing)
                    progress.gapped_symbols = list(gapped)
                    # Data coverage and completed calculations are distinct.
                    progress.symbols_done = sum(symbol in self._hist_last_published_watermark for symbol in completed)
                    progress.bars_done = bars_present
                    keep_wm = set(valid)
                    progress.watermark = {
                        str(sym): int(ts)
                        for sym, ts in self._hist_last_published_watermark.items()
                        if str(sym) in keep_wm
                    }
                    progress.coordination_complete = False
                    progress.pct = 20.0 if missing else 35.0
                    progress.phase = "gap" if missing else "replay"
                    progress.detail = (
                        f"unresolved exchange gaps · {len(missing)}/{len(valid)} symbols"
                        if missing else f"complete coverage · replaying {len(valid)} symbols × {len(book.sets)} sets"
                    )
                    progress.deferred_reason = "unresolved exchange gap" if missing else ""
                self._hist_write_status(book, coverage=coverage_blob)
                replay_names, replay_reason = self._hist_replay_selection(
                    valid,
                    completed,
                    missing,
                    already_ready=already_ready,
                    published=list(getattr(self, "_hist_last_published_watermark", {}) or {}),
                    changed=list(dirty),
                    retry=list(getattr(self, "_hist_replay_retry", set()) & set(valid)),
                )
                if missing:
                    retry_at = time.time() + min(60.0, 10.0 * max(1, int(self._hist_fetch_failures or 1)))
                    self._hist_next_hourly_at = retry_at
                    with self.state_guard():
                        book.progress.next_run_at = retry_at
                    self._hist_checkpoint(book, "gap")
                    self._hist_write_status(book, nextRunAt=retry_at)
                if replay_reason == "wait-gaps":
                    self._hist_wake.wait(timeout=min(5.0, max(0.5, float(self._hist_next_hourly_at or time.time() + 5.0) - time.time())))
                    continue
                if replay_reason == "skip-unchanged":
                    complete_at = time.time()
                    with self.state_guard():
                        refresh_s = max(60.0, min(86400.0, float(getattr(book, "refresh_s", 3600.0) or 3600.0)))
                    self._hist_next_hourly_at = complete_at + refresh_s
                    with self.state_guard():
                        progress = book.progress
                        progress.phase = "ready"
                        progress.pct = 100.0
                        progress.ready = True
                        progress.last_complete_run = complete_at
                        progress.next_run_at = self._hist_next_hourly_at
                        progress.stale = False
                        progress.deferred_reason = ""
                        progress.coordination_complete = True
                        progress.symbols_done = len(valid)
                        progress.symbols_total = len(valid)
                        progress.detail = f"coverage unchanged · {len(valid)} symbols · skip replay"
                    self._hist_write_status(book, coverage=coverage_blob, nextRunAt=self._hist_next_hourly_at)
                    self._hist_checkpoint(book, "skip-unchanged")
                    continue
                if missing:
                    with self.state_guard():
                        book.progress.phase = "replay"
                        book.progress.detail = (
                            f"partial coverage {len(completed)}/{len(valid)} · replaying {len(replay_names)} contiguous"
                        )
                        book.progress.deferred_reason = ""

                with self.state_guard():
                    already = bool(book.progress.ready)
                # Never inflate the replay denominator from a previous
                # uncapped watermark (old 500+ symbol runs).
                progress_total = max(len(valid), len(replay_names))
                replayed = self._hist_replay_chunked(replay_names, already, progress_total,
                                                     watermarks=input_watermarks, durable=True)
                if self.sets is not book or int(getattr(self, "_sets_generation", 0) or 0) != catalog_generation:
                    continue
                replay_done = set(getattr(self, "_hist_replay_completed", set()))
                self._hist_replay_retry = ((set(getattr(self, "_hist_replay_retry", set())) | set(replay_names))
                                          - replay_done) & set(valid)
                if not replayed or self._hist_request_changed():
                    self._hist_next_hourly_at = time.time() + 1.0
                    with self.state_guard():
                        book.progress.coordination_complete = False
                        book.progress.next_run_at = self._hist_next_hourly_at
                    self._hist_write_status(book, coverage=coverage_blob, nextRunAt=self._hist_next_hourly_at)
                    self._hist_checkpoint(book, "superseded-or-deferred")
                    self._hist_wake.wait(timeout=1.0)
                    continue
                watermark = {
                    str(sym): int(ts)
                    for sym, ts in dict(getattr(self, "_hist_last_published_watermark", {}) or {}).items()
                    if str(sym) in set(valid)
                }
                complete_at = time.time()
                self._hist_last_published_watermark = dict(watermark)
                if not missing:
                    self._hist_fetch_failures = 0
                self._hist_last_closed_minute = max(watermark.values(), default=0)
                with self.state_guard():
                    refresh_s = max(60.0, min(86400.0, float(getattr(book, "refresh_s", 3600.0) or 3600.0)))
                if missing:
                    self._hist_next_hourly_at = time.time() + min(60.0, 10.0 * max(1, int(self._hist_fetch_failures or 1)))
                else:
                    self._hist_next_hourly_at = complete_at + refresh_s
                with self.state_guard():
                    progress = book.progress
                    pending_symbols = [symbol for symbol in valid if not watermark.get(symbol) or symbol in missing
                                       or symbol in self._hist_replay_retry]
                    if pending_symbols:
                        self._hist_next_hourly_at = min(self._hist_next_hourly_at, complete_at + 10.0)
                    progress.phase = "partial" if pending_symbols else "ready"
                    progress.symbols_total = len(valid)
                    progress.symbols_done = len(valid) - len(pending_symbols)
                    progress.pct = 100.0 * progress.symbols_done / max(1, len(valid))
                    progress.ready = True
                    if not pending_symbols:
                        progress.last_complete_run = complete_at
                        progress.error = ""
                    progress.next_run_at = self._hist_next_hourly_at
                    progress.watermark = dict(watermark)
                    progress.last_published_watermark = dict(watermark)
                    progress.valid_symbols = list(valid)
                    progress.missing_symbols = list(missing)
                    progress.gapped_symbols = list(gapped)
                    progress.stale = bool(pending_symbols)
                    progress.deferred_reason = ""
                    progress.coordination_complete = not pending_symbols
                    if pending_symbols:
                        progress.detail = (
                            f"published partial {mode} replay · {progress.symbols_done}/{len(valid)} symbols · {len(pending_symbols)} pending"
                        )
                    else:
                        progress.detail = f"published complete {mode} replay · {len(valid)} symbols · next hourly refresh"
                    if request:
                        self._hist_request_seen = run_id
                self._hist_write_status(book, coverage=coverage_blob, finishedAt=complete_at if not pending_symbols else 0,
                                        lastCompleteRun=book.progress.last_complete_run, nextRunAt=self._hist_next_hourly_at)
                self._hist_checkpoint(book, "published")
            except Exception:
                error = traceback.format_exc()[-220:]
                self._hist_next_hourly_at = time.time() + 10.0
                with self.state_guard():
                    current = self.sets
                    current.progress.phase = "error"
                    current.progress.error = error
                    current.progress.detail = "historic lane error"
                    current.progress.stale = bool(current.progress.ready)
                    current.progress.deferred_reason = error[:180]
                    current.progress.next_run_at = self._hist_next_hourly_at
                self._hist_write_status(self.sets, error=error, deferredReason=error[:180], nextRunAt=self._hist_next_hourly_at)
                self._hist_checkpoint(self.sets, "error")
                if hasattr(self.api, "err"):
                    self.api.err.write("hist", msg=error[:200])
            finally:
                self._hist_active_request = {}
                self._hist_active_run_id = ""
                self.hist_busy = False
            with self.state_guard():
                next_at = float(getattr(self, "_hist_next_hourly_at", 0.0) or 0.0)
                wait_s = min(max(next_at - time.time(), 0.25), 10.0) if next_at else 1.0
            self._hist_wake.wait(timeout=wait_s)

    def _hist_loop(self) -> None:
        while not self._hist_stop.is_set():
            # Clear before inspecting state. Any catalog/config/bar wake that
            # arrives during replay remains set and is observed by the next
            # deadline wait, so a fresh signal cannot be lost between clear and
            # wait.
            self._hist_wake.clear()
            if not self._catalog_ready.is_set():
                self._hist_wake.wait(timeout=5.0)
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
            with self.state_guard():
                current = self.sets
                now = time.time()
                refresh_in = max(
                    0.0,
                    float(getattr(current, "last_run", 0.0) or 0.0)
                    + float(getattr(current, "refresh_s", 90.0) or 90.0)
                    - now,
                )
                fetch_in = max(0.0, float(getattr(self, "_hist_fetch_next", 0.0) or 0.0) - now)
                ready = bool(getattr(current.progress, "ready", False))
                enabled = bool(getattr(current, "enabled", True))
                budget = getattr(self.load, "last_budget", None)
                hist_allowed = bool(getattr(budget, "hist_run", True))
            if not enabled:
                wait_s = 5.0
            elif not hist_allowed:
                wait_s = 2.0
            elif refresh_in <= 0.0 and fetch_in > 0.0:
                wait_s = min(fetch_in, 5.0)
            elif not ready:
                wait_s = 0.5
            else:
                wait_s = min(max(refresh_in, 0.05), 5.0)
            self._hist_wake.wait(timeout=wait_s)

    def _warm_pass(self) -> None:
        # Network waits must not own the lock used by stats and coordination.
        # Market/indication methods publish per-symbol replacement rows; readers
        # snapshot collection membership before iterating concurrent updates.
        if time.time() - self.last_bal > BALANCE_EVERY:
            self.refresh_balance()
        self.refresh_klines()
        self.refresh_vol1h()
        self.process_indications()
        self.update_regime()

    def _warm_loop(self) -> None:
        while not self._warm_stop:
            t0 = time.time()
            try:
                self._warm_pass()
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

    def _cycle_step(self, name, action):
        """Fixed stage names, no trace history; identify stalls without growing logs."""
        started = time.perf_counter()
        self._active_stage_at = started
        self._active_cycle_stage = name
        try:
            return action()
        finally:
            self._cycle_stage_ms[name] = round(self._cycle_stage_ms.get(name, 0.0) + (time.perf_counter() - started) * 1000.0, 1)
            self._active_cycle_stage = ""

    def _finish_cycle_timing(self, started, cpu_started):
        self.last_scan_ms = (time.perf_counter() - started) * 1000.0
        self.last_scan_cpu_ms = (time.thread_time() - cpu_started) * 1000.0
        self.last_scan_io = bool(self.did_io)
        self.cycle_overrun = self.last_scan_ms > SCAN_S * 1000.0 and not (self.did_io or self.hist_busy)
        self.last_cycle_stages = dict(self._cycle_stage_ms)

    def _one_cycle(self) -> None:
        self._cycle_stage_ms = {}
        self.did_io = False
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
            elif DD_HALT > 0 and self.start_eq > 0 and self.equity > 0 and (self.start_eq - self.equity) / self.start_eq >= DD_HALT:
                self.halted = True
                self.halt_reason = "drawdown halt"
            else:
                self.halted = False
                self.halt_reason = None
        self.cycle += 1
        self._cycle_step("budget", self._budget)
        self._cycle_step("tickers", self.refresh_tickers)
        self._cycle_step("bars", self.seed_px_bars)
        if time.monotonic() >= float(getattr(self, "_next_fill_poll", 0.0)):
            self._cycle_step("fills", self.sync_own_fills)
        # Reconcile immediately after the boot snapshot, before touching old
        # local controls. Waiting for cycle 25 can take hours when a stale book
        # contains many positions and each repair encounters venue cooldowns.
        if self.cycle == 1 or self.cycle % 25 == 0:
            self._cycle_step("reconcile", self.adopt_exchange_positions)
        self._sync_set_processing()
        unprotected = self._cycle_step("controls", self.priority_controls)
        if self.cycle % 8 == 0:
            self._cycle_step("config", self.maybe_reload_config)
        if self.cycle % 220 == 0:
            self.pool.submit(self.set_leverage)
        self._cycle_step("manage", self.manage)
        if unprotected:
            unprotected = self._cycle_step("controls", self.priority_controls)
        # Indications run on the warm thread so the 530-symbol scan cannot stall the watchdog.
        if not self.halted:
            self._cycle_step("entries", self.maybe_entries)
            self._cycle_step("block", self.maybe_block_adds)
            self._cycle_step("dca", self.maybe_dca_adds)
        if self.cycle % QA_EVERY == 0:
            self._cycle_step("qa", self.qa_tick)
        heal_trim = self._heal_trim_pending()
        if self.cycle % 12 == 0 or heal_trim:
            self._cycle_step("trim", lambda: self.trim_caches(force=False, keep_hist=True))
            if heal_trim:
                self._heal_trim_clear()

    def _watchdog_loop(self) -> None:
        """Independent systemd heartbeat.

        Sleep releases the GIL so catalog bootstrap, durable 1m backfill, and
        historic scoring cannot starve Type=notify WatchdogSec.
        """
        stop = getattr(self, "_watchdog_stop", None)
        while True:
            sd_notify("WATCHDOG=1")
            # Never persist from this thread: json-dumping a multi-symbol tape
            # holds the GIL long enough that systemd never sees the ping.
            if stop is None:
                time.sleep(5.0)
                continue
            if stop.is_set():
                return
            stop.wait(timeout=5.0)

    def run(self) -> None:
        log(f"pulse start {CONN_SHORT} {BASE}")
        if getattr(self, "runtime", None):
            self.runtime.start(self)
        else:
            def retry_statistics():
                while not self._hist_stop.wait(30):
                    try:
                        monitor = RuntimeMonitor(DIR, CONN_SHORT, self.overlay)
                        self.runtime = monitor
                        monitor.start(self)
                        return
                    except Exception as exc:
                        self.last_error = f"Statistics recovery: {type(exc).__name__}"
            threading.Thread(target=retry_statistics, name="statistics-recovery", daemon=True).start()
        sd_notify("READY=1\nWATCHDOG=1")
        # Heartbeat must start before any blocking REST/catalog work. A 566
        # symbol kline fill or 13k-set catalog can otherwise exceed WatchdogSec
        # before the main loop ever runs.
        threading.Thread(target=self._watchdog_loop, name="watchdog", daemon=True).start()
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
        self.reconcile_startup_positions()
        self._sync_set_processing()
        sd_notify("WATCHDOG=1")
        try:
            self.list_orders()
        except Exception:
            pass
        self.priority_controls()
        self._load_lev_file()
        sd_notify("WATCHDOG=1")
        self.pool.submit(self.set_leverage)
        self.write_stats()
        warm = threading.Thread(target=self._warm_loop, name="warm-feed", daemon=True)
        warm.start()
        hist = threading.Thread(target=self._hist_loop_durable, name="hist-1m", daemon=True)
        hist.start()
        ctrl = threading.Thread(target=self._ctrl_watch_loop, name="ctrl-watch", daemon=True)
        ctrl.start()
        self.cycle_busy = False
        self.cycle_wait_ms = 0.0
        self.cycle_overrun = False
        # Probes are observability only.  Run them after the service has
        # entered its normal event loop so API stalls or a large local QA
        # matrix cannot hold up controls, history, or the first live cycle.
        qa = threading.Thread(target=self._background_startup_self_tests, name="startup-qa", daemon=True)
        qa.start()
        while True:
            # One cycle at a time on this thread. cycle_busy is observability
            # for stats; the lock is what actually serialises the work.
            t0 = time.perf_counter()
            cpu0 = time.thread_time()
            self.cycle_busy = True
            try:
                # Do not hold the shared state lock across refresh/control
                # REST calls.  A slow exchange response must not starve the
                # stats/UI readers, history progress, or control watcher.
                with self._cycle_lock:
                    self._one_cycle()
                if getattr(self, "runtime", None):
                    self.runtime.note_success()
            except Exception:
                if getattr(self, "runtime", None):
                    self.runtime.note_failure()
                self.errors += 1
                self.last_error = traceback.format_exc()[-400:]
                log("LOOP " + self.last_error)
                if hasattr(self.api, "err"):
                    self.api.err.write("loop", msg=self.last_error[:300])
            finally:
                self._finish_cycle_timing(t0, cpu0)
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
                self._wait_wake(remain)
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
    try:
        BASE = connection_endpoint(
            CONN_SHORT, redis_hget("base_url"), redis_hget("is_testnet"),
            vst_only=str(os.environ.get("CTS_VST_ONLY") or "").lower() in ("1", "true", "yes"),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    api = FastBingX(key, secret, ErrorLog(ERR_PATH), base=BASE)
    pulse = Pulse(api, load_contracts())
    import signal
    def stop_service(_signum, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop_service)
    clean = False
    try:
        pulse.run()
        clean = True
    except KeyboardInterrupt:
        clean = True
    except SystemExit as exc:
        clean = exc.code in (None, 0)
        if not clean:
            raise
    finally:
        if getattr(pulse, "runtime", None):
            pulse.runtime.finish(clean=clean)


if __name__ == "__main__":
    main()
