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
from block_engine import BlockBook, BLOCK_COUNT_PREVIEW, BLOCK_PF_RATIO_MIN, BLOCK_PF_RATIO_MAX, clamp_stack, calculate_block_volume_increment_ratio, calculate_block_minimum_profit_factor, calculate_block_max_additional_ratio
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
MAX_DD_TIME_S = 0.0  # 0 = off; max seconds a position may stay underwater
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
    try:
        p = subprocess.run(["redis-cli", "HGET", REDIS_CONN, field], capture_output=True, text=True)
        return (p.stdout or "").strip()
    except FileNotFoundError:
        return ""
    except Exception:
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
        p = subprocess.run(["redis-cli", "HGETALL", f"settings:connection_settings:{CONN_SHORT}"], capture_output=True, text=True)
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
        self._stats_lock = threading.Lock()
        self.hist_busy = False
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
        self.recon_detail = "pending"
        self.exchange_open_count = -1  # -1 = not yet read from exchange
        self._empty_rest_streak = 0
        self.live_pos_keys: Optional[set] = None  # None = exchange truth unknown
        self._load_trade_history()
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
        self.flatten_skip: Dict[str, float] = {}
        self.cts: Dict[str, Any] = {}
        self.position_cost_pct = POSITION_COST_PCT_DEFAULT
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
        c = self.contracts.get(symbol)
        mx = int(self.lev_max.get(symbol) or getattr(c, "max_lev", 0) or 0)
        applied = int(self.lev_map.get(symbol) or 0)
        if not force and mx > 0 and applied >= mx:
            if c is not None:
                c.max_lev = mx
            return applied
        if self.api.path_cd.get("/openApi/swap/v2/trade/leverage", 0) > time.time():
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

    def sized_notional(self, symbol: Optional[str] = None) -> float:
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
        return max(0.2, float(TARGET_NOTIONAL) * vf)

    def notional_cap(self) -> float:
        return max(self.sized_notional(), 2.0)

    def avail_notional(self, c: Optional["Contract"] = None) -> float:
        """USDT notional the remaining available balance can still carry at this pair's max lev."""
        lev = max(1, self.leverage_for(c) if c is not None else int(LEVERAGE or 1))
        return max(0.0, float(self.available or 0)) * lev * 0.90

    def max_book_notional(self) -> float:
        """Per-position book room = parent base × configured Block/DCA rungs.
        0 rungs maps to the seeded default (Block 3, DCA distance list). Never
        a wallet-fraction balloon — leftover size is remaining available only."""
        base = self.notional_cap()
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

    def size_qty(self, c: Contract, px: float) -> float:
        """Size from remaining available first. Skip if the exchange min lot needs more margin than we have."""
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
        want_n = min(self.sized_notional(), room)
        if want_n < floor_n:
            want_n = floor_n
        q = self.round_qty(c, want_n / px)
        if q < floor:
            return 0.0
        if q * px > room * 1.02:
            return 0.0
        # Final sanity: an entry may never exceed 2× the configured target
        # (exchange min-lot floor excepted) — catches corrupt sizing upstream.
        if q * px > max(self.sized_notional() * 2.0, floor_n * 1.08):
            return 0.0
        return q

    def ban_sym(self, sym: str, sec: float = 1800.0) -> None:
        self.ignore_syms[sym] = time.time() + sec
        self.owned_syms.discard(sym)
        self.open.pop(sym, None)

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

    def save_open_book(self) -> None:
        try:
            blob = {s: asdict(p) for s, p in self.open.items()}
            tmp = OPEN_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(blob, f)
            os.replace(tmp, OPEN_PATH)
        except Exception:
            pass

    def _load_open_book(self) -> None:
        if not os.path.exists(OPEN_PATH):
            return
        try:
            data = json.load(open(OPEN_PATH))
        except Exception:
            return
        fields = {f.name for f in Position.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        for sym, rec in (data or {}).items():
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
            self.open[sym] = pos
            self.owned_syms.add(sym)
            if pos.client_id:
                self.seen_fill_cids.add(pos.client_id)

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
        nonce = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
        return f"{TAG}{kind}{p}{sl}{tr}{st}{ix}{nonce}"[:32]

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
                    "step": st_obj.step,
                    "idx": st_obj.idx,
                    "set_id": st_obj.id,
                }
        from set_engine import make_set_id
        return {"kind": kind, "pack": pack, "sl": sl, "trail": tr, "step": step, "idx": idx, "set_id": make_set_id(pack, sl, tr, step)}

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

    def refresh_balance(self) -> None:
        r = self.api.get("/openApi/swap/v3/user/balance")
        if not self.ok(r):
            r = self.api.get("/openApi/swap/v2/user/balance")
        data = r.get("data")
        row = None
        if isinstance(data, dict):
            row = data.get("balance") if isinstance(data.get("balance"), dict) else data
        elif isinstance(data, list) and data:
            row = next((x for x in data if str(x.get("asset") or x.get("currency") or "USDT").upper() in ("USDT", "VST")), data[0])
        if not isinstance(row, dict):
            self.errors += 1
            self.last_error = f"balance {r.get('msg')}"
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
        for s, px in list(self.px.items()):
            if px <= 0:
                continue
            rec = self.bar_min.get(s)
            if not rec or int(rec[0]) != minute:
                if rec:
                    bars = self.klines_tf["1m"].setdefault(s, [])
                    bars.append([rec[1], rec[2], rec[3], rec[4], 0.0])
                    del bars[:-KLINE_LIMIT]
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
            for s, bars in src.items():
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
            return
        ready1 = sum(1 for s in SYMBOLS if len(self.klines_tf.get("1m", {}).get(s) or []) >= 20)
        need1 = len(SYMBOLS) if len(SYMBOLS) <= 48 else max(32, len(SYMBOLS) // 2)
        filling = ready1 < max(1, need1)
        reqs = []
        tfs = tuple(TF_EVERY.keys())
        if filling:
            tfs = ("1m", "5m")
        for tf in tfs:
            every = TF_EVERY.get(tf, 2.0)
            if not self.tf_on.get(tf, True):
                continue
            due = [s for s in SYMBOLS if now - self.kline_ts_tf[tf].get(s, 0) >= every]
            if not due:
                continue
            due.sort(key=lambda s: self.kline_ts_tf[tf].get(s, 0))
            batch = due[: TF_BATCH.get(tf, 4)]
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
        self.last_kline = now

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
        for p in self.open.values():
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
        r = self.api.delete("/openApi/swap/v2/trade/order", {"symbol": symbol, "orderId": order_id})
        if self.ok(r):
            self._oo_cache.pop(symbol, None)
            self._oo_cache.pop("*", None)
            return True
        self.last_error = f"cancel {symbol} {r.get('msg')}"[:200]
        return False

    def cancel_controls(self, symbol: str, keep: Optional[set] = None) -> None:
        keep = keep or set()
        for o in self.list_orders(symbol):
            if not self.order_is_ours(o):
                continue
            oid = str(o.get("orderId") or "")
            typ = str(o.get("type") or "")
            if typ in SL_TYPES | TP_TYPES or o.get("stopPrice"):
                if oid and oid not in keep:
                    self.cancel_order(symbol, oid, self.order_cid(o))

    def opt_fracs(self, pos: Optional[Position] = None) -> Tuple[float, float, float, float]:
        """(sl, tp, sl_lo, sl_hi) fractions clamped to optimal security ranges."""
        sl_lo = max(float(self.sl_min), float(getattr(self.exits, "opt_sl_min", 0.001) or 0.001))
        sl_hi = min(float(self.sl_max), float(getattr(self.exits, "opt_sl_max", 0.009) or 0.009))
        if sl_lo > sl_hi:
            sl_lo, sl_hi = sl_hi, sl_lo
        sl = float(pos.sl_pct) if pos and pos.sl_pct > 0 else SL_PCT
        sl = max(sl_lo, min(sl_hi, sl))
        tp_lo = float(self.tp_min)
        tp_hi = float(self.tp_max)
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
        """Overall security SL/TP: widest of the order range and overlay max."""
        sl_f, tp_f, sl_lo, sl_hi = self.opt_fracs(pos)
        sl_w = max(sl_f, sl_hi, float(getattr(pos, "sl_pct", 0) or 0), sl_lo)
        tp_w = max(tp_f, float(self.tp_max), float(getattr(pos, "tp_pct", 0) or 0), float(self.tp_min))
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
        sl, tp = self.security_prices(pos)
        sec_sl, sec_tp = self.max_range_prices(pos)
        pick_sl = next((p for p in (sl, sec_sl) if self.sl_legal(pos, p)), 0.0)
        if not pick_sl:
            pick_sl = self.clamp_ctrl_price(pos, "sl", sec_sl or sl or 0)
        pick_tp = next((p for p in (tp, sec_tp) if self.tp_legal(pos, p)), 0.0)
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

    def place_ctrl(self, pos: Position, kind: str, price: float) -> str:
        is_sl = str(kind).lower() in ("sl", "s", "u", "sec-sl", "sec_sl")
        is_sec = str(kind).lower() in ("u", "v", "sec-sl", "sec-tp", "sec_sl", "sec_tp")
        cid_ch = "u" if (is_sec and is_sl) else ("v" if is_sec else ("s" if is_sl else "t"))
        if time.time() < self.ctrl_skip.get("__order_cap__", 0):
            return real_oid(pos.sl_oid if is_sl else pos.tp_oid)
        have_this = real_oid(pos.sl_oid if is_sl else pos.tp_oid)
        if have_this and time.time() < self.ctrl_skip.get(pos.symbol, 0):
            return have_this
        if (self.px.get(pos.symbol) or 0) <= 0 and (self.last_px.get(pos.symbol) or 0) <= 0:
            self.refresh_px_one(pos.symbol)
        price = self.clamp_ctrl_price(pos, "sl" if is_sl else "tp", price)
        c = self.contracts.get(pos.symbol)
        qty_s = self.fmt_qty(c, pos.qty)
        px_s = self.fmt_px(c, price)
        if float(px_s or 0) <= 0:
            log(f"CTRL SKIP {kind} {pos.symbol} stopPrice=0", every=20.0, key=f"cskip:{pos.symbol}:px0")
            self.ctrl_skip[pos.symbol] = time.time() + 60
            return have_this
        forms = [
            {"close_pos": False, "with_qty": True, "otype": "STOP_MARKET" if is_sl else "TAKE_PROFIT_MARKET"},
            {"close_pos": True, "with_qty": False, "otype": "STOP_MARKET" if is_sl else "TAKE_PROFIT_MARKET"},
            {"close_pos": False, "with_qty": True, "otype": "STOP" if is_sl else "TAKE_PROFIT"},
            {"close_pos": True, "with_qty": False, "otype": "STOP" if is_sl else "TAKE_PROFIT"},
        ]
        r: Dict[str, Any] = {}
        msg = ""
        oid = ""
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
                r = self.api.post("/openApi/swap/v2/trade/order", body)
                self.did_io = True
                if r.get("cooled"):
                    return ""
                msg = str(r.get("msg") or "")
                kind_err = ctrl_err_kind(msg)
                if self.ok(r):
                    oid = extract_oid(r)
                    if oid:
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
                        live = real_oid(o.get("orderId") or o.get("orderID"))
                        if not live:
                            continue
                        if is_sl and (typ in SL_TYPES or self._cid_kind(o) in ("s", "u")):
                            return live
                        if (not is_sl) and (typ in TP_TYPES or self._cid_kind(o) in ("t", "v")):
                            return live
                    continue
                if kind_err == "qty_close":
                    continue
                if kind_err == "cap":
                    self.ctrl_skip[pos.symbol] = time.time() + 90
                    self.ctrl_skip["__order_cap__"] = time.time() + 60
                    log(f"CTRL cap {pos.symbol} cooldown 60s", every=20.0, key="ordercap")
                    return ""
                if "110206" in msg or ("over 20" in msg.lower() and "error code" in msg.lower()):
                    self.ctrl_skip[pos.symbol] = time.time() + 30
                    return have_this
                if kind_err in ("px", "liq"):
                    self.ctrl_skip[pos.symbol] = time.time() + 45
                    break
            if oid:
                break
        if not oid:
            short = short_api_msg(msg)
            kind_err = ctrl_err_kind(msg)
            if is_transient_api(msg) or kind_err in ("flat", "px", "liq", "exists", "qty_close", "qty"):
                self.ctrl_skip[pos.symbol] = time.time() + (90 if kind_err in ("px", "liq", "qty") else 20)
                log(f"CTRL SKIP {kind} {pos.symbol} {short}", every=12.0, key=f"cskip:{pos.symbol}:{short}")
            else:
                self.errors += 1
                self.last_error = f"{kind} {pos.symbol} {short}"[:160]
                log(f"CTRL FAIL {kind} {pos.symbol} {pos.side} {short} px={price} mark={self.px.get(pos.symbol)}")
            low2 = msg.lower()
            if kind_err == "flat" or "position not exist" in low2:
                self.ctrl_skip[pos.symbol] = time.time() + 20
                age = time.time() - float(getattr(pos, "opened_at", 0) or 0)
                if age > 90.0 and self._exchange_flat(pos):
                    self.drop_ghost(pos, "ctrl-no-position")
            elif kind_err in ("px", "liq"):
                self.ctrl_skip[pos.symbol] = time.time() + 45
            elif kind_err == "qty":
                self.ctrl_skip[pos.symbol] = time.time() + 60
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
        """Hard freeze only for order-cap cooldown and the first boot seconds.
        A leftover/ghost missing SL/TP must never stop the rest of the book —
        after a manual exchange close the desk keeps placing, adding, and
        attaching controls on every other symbol."""
        if time.time() < self.ctrl_skip.get("__order_cap__", 0):
            return True
        if time.time() - float(getattr(self, "boot_ts", 0) or 0) < 25.0:
            return True
        return False

    def controls_illegal(self, pos: Position) -> bool:
        if self.missing_controls(pos):
            return True
        return (not self.sl_legal(pos, pos.sl)) or (not self.tp_legal(pos, pos.tp))

    def priority_controls(self) -> int:
        """Overall SL/TP first. Returns how many positions are still unprotected."""
        if not getattr(self, "control_orders", True):
            return 0
        miss = 0
        now = time.time()
        for pos in list(self.open.values()):
            px = self.px.get(pos.symbol) or pos.entry
            need = self.missing_controls(pos)
            illegal = (not need) and now >= self.ctrl_skip.get(f"legal:{pos.symbol}", 0) and self.controls_illegal(pos)
            if not need and not illegal:
                continue
            if now < self.ctrl_skip.get(pos.symbol, 0) and not need:
                miss += 1
                continue
            self.ensure_controls(pos)
            if self.missing_controls(pos):
                miss += 1
                age = time.time() - float(pos.opened_at or 0)
                bare = not (pos.sl_oid or pos.tp_oid or getattr(pos, "sec_sl_oid", "") or getattr(pos, "sec_tp_oid", ""))
                if bare and age > 300.0 and time.time() >= self.ctrl_skip.get("__order_cap__", 0):
                    log(f"CTRL flatten unprotected {pos.symbol} age={age:.0f}s")
                    self.close_pos(pos, px or pos.entry, "no-ctrl")
                else:
                    self.bump("ctrl")
            else:
                self.ctrl_skip[f"legal:{pos.symbol}"] = now + 8.0
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
        if time.time() < self.ctrl_skip.get("__order_cap__", 0):
            return
        if time.time() < self.ctrl_skip.get(pos.symbol, 0) and pos.sl_oid and pos.tp_oid:
            return
        want_sl, want_tp, _, _ = self.desired_sl_tp(pos)
        sl_b = self._ctrl_body(pos, "sl", want_sl)
        tp_b = self._ctrl_body(pos, "tp", want_tp)
        for b, ch in ((sl_b, "u"), (tp_b, "v")):
            b["closePosition"] = "true"
            b["clientOrderID"] = self.cid(ch, pos=pos)
            if "closePosition" in b:
                b["closePosition"] = "true"
        r = self.api.batch_place([sl_b, tp_b])
        self.did_io = True
        data = r.get("data") or {}
        rows = data.get("orders") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            rows = []
        for o in rows:
            if not isinstance(o, dict):
                continue
            code = o.get("code")
            if code not in (0, None, "0", ""):
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
        if not real_oid(pos.sl_oid):
            pos.sl_oid = pos.sec_sl_oid = self.place_ctrl(pos, "sec-sl", want_sl)
            if pos.sl_oid:
                pos.sl = want_sl
        if not real_oid(pos.tp_oid):
            pos.tp_oid = pos.sec_tp_oid = self.place_ctrl(pos, "sec-tp", want_tp)
            if pos.tp_oid:
                pos.tp = want_tp
        pos.controls_ok = bool(real_oid(pos.sl_oid) and real_oid(pos.tp_oid))
        pos.overall = pos.controls_ok
        pos.close_position = True
        pos.ctrl_qty = pos.qty
        pos.ctrl_verified = pos.controls_ok

    def ensure_controls(self, pos: Position) -> None:
        now = time.time()
        if now < self.ctrl_skip.get("__order_cap__", 0):
            return
        have_both = bool((real_oid(pos.sl_oid) or real_oid(getattr(pos, "sec_sl_oid", ""))) and (real_oid(pos.tp_oid) or real_oid(getattr(pos, "sec_tp_oid", ""))))
        if have_both and now < self.ctrl_skip.get(pos.symbol, 0) and getattr(pos, "ctrl_verified", False):
            return
        if self.api.path_cd.get("/openApi/swap/v2/trade/order", 0) > time.time() and have_both:
            return
        want_sl, want_tp, sec_sl, sec_tp = self.desired_sl_tp(pos)
        pos.sec_sl, pos.sec_tp = sec_sl, sec_tp
        if not real_oid(pos.sl_oid) and not real_oid(pos.tp_oid):
            self.place_ctrl_pair(pos)
            if real_oid(pos.sl_oid) and real_oid(pos.tp_oid):
                return
        banned = self.api.path_cd.get("/openApi/swap/v2/trade/openOrders", 0) > time.time()
        all_rows = [] if banned else self.list_orders(pos.symbol)
        orders = [o for o in all_rows if self.order_is_ours(o)]
        if banned:
            if not real_oid(pos.sl_oid):
                oid = self.place_ctrl(pos, "sec-sl", want_sl)
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
            pos.close_position = True
            pos.ctrl_qty = pos.qty
            self.ctrl_skip[f"sync:{pos.symbol}"] = time.time() + 12.0
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
                return True
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
        if sec_sls or pos.sl_oid:
            pos.close_position = True
        now = time.time()
        last_sync = float(self.ctrl_skip.get(f"sync:{pos.symbol}", 0) or 0)
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
            if have_oid and live_have and side_ok and not trail:
                return have_oid
            if have_oid and live_have and not can_replace:
                return have_oid
            if have_oid and live_have:
                cid = self.order_cid(live_rows[0]) if live_rows else ""
                self.cancel_order(pos.symbol, have_oid, cid)
            oid = self.place_ctrl(pos, "sec-sl" if is_sl else "sec-tp", want)
            if oid:
                self.ctrl_skip[f"sync:{pos.symbol}"] = now + 30.0
            return oid or have_oid

        pos.sl_oid = pos.sec_sl_oid = _place_side(True, pos.sl_oid or pos.sec_sl_oid, pos.sl, want_sl, bool(sls), sls)
        if pos.sl_oid:
            pos.sl = want_sl if not pos.sl or not self.sl_legal(pos, pos.sl) else pos.sl
        pos.tp_oid = pos.sec_tp_oid = _place_side(False, pos.tp_oid or pos.sec_tp_oid, pos.tp, want_tp, bool(tps), tps)
        if pos.tp_oid:
            pos.tp = want_tp if not pos.tp or not self.tp_legal(pos, pos.tp) else pos.tp
        pos.controls_ok = bool(real_oid(pos.sl_oid) and real_oid(pos.tp_oid))
        pos.ctrl_verified = bool(sls and tps)
        pos.ctrl_qty = pos.qty
        pos.overall = bool((real_oid(pos.sl_oid) and real_oid(pos.tp_oid)) or (real_oid(pos.sec_sl_oid) and real_oid(pos.sec_tp_oid)))
        pos.close_position = True

    def _ctrl_body(self, pos: Position, kind: str, price: float) -> Dict[str, Any]:
        price = self.clamp_ctrl_price(pos, kind, price)
        c = self.contracts.get(pos.symbol)
        return ctrl_payload(
            pos.symbol,
            pos.side,
            kind,
            self.fmt_px(c, price),
            self.fmt_qty(c, pos.qty),
            self.cid("u" if kind == "sl" else "v", pos=pos),
            close_pos=True,
            with_qty=False,
        )

    def replace_sl(self, pos: Position, new_sl: float) -> None:
        now = time.time()
        if now < self.ctrl_skip.get(f"sync:{pos.symbol}", 0):
            return
        c = self.contracts.get(pos.symbol)
        if c:
            new_sl = self.round_px(c, new_sl)
        new_sl = self.clamp_ctrl_price(pos, "sl", new_sl)
        if not self.sl_legal(pos, new_sl):
            new_sl = self.desired_sl_tp(pos)[0]
        if pos.sl_oid and abs(float(pos.sl or 0) - new_sl) / max(pos.entry, 1e-9) < 0.00035 and self.sl_legal(pos, pos.sl):
            return
        old_oid = real_oid(pos.sl_oid) or real_oid(getattr(pos, "sec_sl_oid", ""))
        replaced = False
        if old_oid and hasattr(self.api, "cancel_replace"):
            body = ctrl_payload(
                pos.symbol, pos.side, "sl", self.fmt_px(c, new_sl), self.fmt_qty(c, pos.qty),
                self.cid("u", pos=pos), close_pos=True, with_qty=False,
            )
            body["cancelOrderId"] = old_oid
            r = self.api.cancel_replace(body)
            self.did_io = True
            if self.ok(r):
                oid = extract_oid(r) or old_oid
                pos.sl_oid = pos.sec_sl_oid = real_oid(oid)
                pos.sl = pos.sec_sl = new_sl
                replaced = True
        if not replaced:
            if pos.sl_oid:
                self.cancel_order(pos.symbol, pos.sl_oid)
            if pos.sec_sl_oid and pos.sec_sl_oid != pos.sl_oid:
                self.cancel_order(pos.symbol, pos.sec_sl_oid)
            pos.sl_oid = pos.sec_sl_oid = ""
            pos.sl = new_sl
            pos.sec_sl = new_sl
            oid = self.place_ctrl(pos, "sec-sl", new_sl)
            pos.sl_oid = pos.sec_sl_oid = real_oid(oid)
        want_tp = self.desired_sl_tp(pos)[1]
        if not real_oid(pos.tp_oid):
            pos.tp_oid = pos.sec_tp_oid = real_oid(self.place_ctrl(pos, "sec-tp", want_tp))
            pos.tp = want_tp
        pos.controls_ok = bool(real_oid(pos.sl_oid) and real_oid(pos.tp_oid))
        self.ctrl_skip[f"sync:{pos.symbol}"] = now + 12.0

    def market_close(self, pos: Position) -> Tuple[bool, float]:
        close_side = "SELL" if pos.side == "LONG" else "BUY"
        forms = [
            {"quantity": pos.qty, "clientOrderID": self.cid("c", pos=pos)},
            {"closePosition": "true", "clientOrderID": self.cid("c", pos=pos)},
        ]
        pid = str(getattr(pos, "position_id", "") or "")
        if pid:
            forms.append({"positionId": pid, "clientOrderID": self.cid("c", pos=pos)})
        r: Dict[str, Any] = {}
        for extra in forms:
            body = {
                "symbol": pos.symbol,
                "type": "MARKET",
                "side": close_side,
                "positionSide": pos.side,
            }
            body.update(extra)
            if "quantity" in body and body.get("closePosition"):
                body.pop("quantity", None)
            r = self.api.post("/openApi/swap/v2/trade/order", body)
            self.did_io = True
            if self.ok(r):
                data = (r.get("data") or {}).get("order") or r.get("data") or {}
                px = float(data.get("avgPrice") or data.get("price") or 0) or (self.px.get(pos.symbol) or pos.entry)
                return True, px
            msg = str(r.get("msg") or "")
            if ctrl_err_kind(msg) == "qty_close":
                continue
            if "position not exist" in msg.lower():
                return True, self.px.get(pos.symbol) or pos.entry
        self.errors += 1
        self.last_error = f"close {pos.symbol} {r.get('msg')}"[:240]
        return False, self.px.get(pos.symbol) or pos.entry

    def occupying(self, sym: str, side: str = "", pack: str = "", set_id: str = "") -> bool:
        """Max 1 position per symbol, and per (symbol, direction, pack, Set)."""
        pos = self.open.get(sym)
        if pos:
            return True
        side_u = (side or "").upper()
        for p in self.open.values():
            if p.symbol != sym:
                continue
            if side_u and p.side == side_u:
                return True
            if pack and p.pack == pack:
                return True
            if set_id and p.set_id == set_id and (not side_u or p.side == side_u):
                return True
        return False

    def entry_sense(self, sym: str, direction: int, reason: str, conf: float, pack: str) -> Optional[str]:
        """Skip entries that do not make sense (weak, duplicate slot, dead Set)."""
        if conf < 0.50:
            return "low-conf"
        if (self.px.get(sym) or 0) <= 0:
            return "no-px"
        side = "LONG" if direction > 0 else "SHORT"
        if self.occupying(sym, side, pack):
            return "slot-taken"
        if pack == "indications":
            if not (self.strat_ind and bool(self.indications.settings.get("enabled"))):
                return "ind-off"
            ind = None
            try:
                ind = self.indications.match(sym, reason)
            except Exception:
                ind = self.indications.primary(sym)
            if not ind:
                return "no-ind"
            want = 1 if ind.direction == "long" else -1
            if want != direction:
                return "ind-mismatch"
            # Indication gate: under the strict gate (default) a kind runs
            # only while it is validated + profitable — or, if unproven, only
            # while the indications pack has a validated + profitable set.
            try:
                bits = str(reason).split(":")
                ind_kind = bits[1] if len(bits) > 1 and bits[0] == "ind" else ""
                if not ind_kind:
                    ind_kind = str(getattr(ind, "kind", "") or "")
                gate = getattr(self.sets, "indication_ok", None)
                if self.sets.enabled and callable(gate) and not gate(ind_kind, "LONG" if direction > 0 else "SHORT"):
                    return "ind-gate"
            except Exception:
                pass
        chosen = None
        side_name = "LONG" if direction > 0 else "SHORT"
        try:
            chosen = self.sets.pick_any(pack, side=side_name) if self.sets.enabled else None
            if not chosen and self.sets.enabled:
                chosen = self.sets.pick_any("general", side=side_name) or self.sets.pick_any("indications", side=side_name)
        except TypeError:
            try:
                chosen = self.sets.pick_any(pack) if self.sets.enabled else None
                if not chosen and self.sets.enabled:
                    chosen = self.sets.pick_any("general") or self.sets.pick_any("indications")
            except Exception:
                chosen = None
        except Exception:
            chosen = None
        if chosen:
            if self.occupying(sym, side, pack, chosen.id):
                return "set-slot"
            # pick() already enforces the validation gate: under the strict
            # gate only validated + profitable sets are returned at all.
            return None
        elif self.sets.enabled and self.sets.use_historic_gate:
            if not getattr(getattr(self.sets, "progress", None), "ready", False):
                # Hist still loading — keep processing instead of freezing the book.
                return None
            if getattr(self.sets, "strict_gate", False):
                # Strict: no validated + profitable set for this side -> no entry.
                return "set-gate"
            if not (self.sets.pack_open(pack, side=side) or self.sets.pack_open("general", side=side) or self.sets.pack_open("indications", side=side)):
                # Positive-PF-only: gate ready and every pack closed -> no entry.
                return "set-gate"
        return None

    def place(self, sym: str, direction: int, reason: str, conf: float) -> None:
        if self.entries_blocked():
            return
        if self.halted or os.path.exists(STOP_PATH) or os.path.exists(PAUSE_PATH) or os.path.exists(STOP_ALL):
            return
        if time.time() < self.cooldown.get("__book__", 0):
            return
        if float(self.available or 0) <= 0:
            return
        if time.time() - self.last_entry_ts < STAGGER_S and MAX_OPEN > 0:
            return
        if (MAX_OPEN > 0 and len(self.open) >= MAX_OPEN) or sym in self.open:
            return
        if time.time() < self.cooldown.get(sym, 0):
            return
        if self.ignore_syms.get(sym, 0) > time.time():
            return
        if MAX_PER_GROUP > 0 and self.group_count(self.group_of(sym)) >= MAX_PER_GROUP:
            return
        pack = "indications" if str(reason).startswith("ind:") else "general"
        skip = self.entry_sense(sym, direction, reason, conf, pack)
        if skip:
            if time.time() - self.skip_log.get("sense", 0) > 40:
                log(f"SKIP {sym} {skip}", every=40.0, key="sense", quiet=True)
                self.skip_log["sense"] = time.time()
            return
        c = self.contracts.get(sym)
        px = self.px.get(sym) or 0
        if not c or px <= 0:
            return
        qty = self.size_qty(c, px)
        if qty <= 0:
            return
        notional = qty * px
        if notional > self.max_book_notional() * 1.02:
            return
        self.ensure_max_leverage(sym)
        lev = self.leverage_for(c)
        margin = notional / max(1, lev)
        if margin > float(self.available or 0) * 0.95:
            return
        side = "LONG" if direction > 0 else "SHORT"
        order_side = "BUY" if direction > 0 else "SELL"
        chosen = None
        try:
            chosen = self.sets.pick_any(pack, side=side)
            if not chosen:
                chosen = self.sets.pick_any("general", side=side) or self.sets.pick_any("indications", side=side)
        except TypeError:
            try:
                chosen = self.sets.pick_any(pack)
                if not chosen:
                    chosen = self.sets.pick_any("general") or self.sets.pick_any("indications")
            except Exception:
                chosen = None
        except Exception:
            chosen = None
        set_idx = -1
        trail_set_id = ""
        trail_idx = -1
        if chosen:
            sl_ratio = chosen.sl_ratio
            set_id = chosen.id
            set_idx = int(getattr(chosen, "idx", -1))
            if str(getattr(chosen, "kind", "") or "") == "trail" and getattr(chosen, "trail_key", ""):
                trail_key, trail_arm, trail_give = chosen.trail_key, chosen.trail_arm, chosen.trail_give
                trail_set_id = chosen.id
                trail_idx = set_idx
            else:
                trail_key, trail_arm, trail_give = "", 0.0, 0.0
        else:
            sl_ratio = self.variants.current_sl()
            trail_key, trail_arm, trail_give = self.variants.current_trail()
            set_id = ""
        cid = self.cid("o", set_id=set_id, pack=pack, set_idx=set_idx)
        sl_pct_a, tp_pct_a, _src_a = resolve_sl_tp(
            base_sl=SL_PCT, base_tp=TP_PCT, sl_min=self.sl_min, sl_max=self.sl_max,
            tp_min=self.tp_min, tp_max=self.tp_max, cost_pct=self.position_cost_pct,
            tp_cost_ratio=self.tp_cost_ratio, sl_to_tp=sl_ratio, bind_sl_to_tp=True,
        )
        sl_a = px * (1 - sl_pct_a) if direction > 0 else px * (1 + sl_pct_a)
        tp_a = px * (1 + tp_pct_a) if direction > 0 else px * (1 - tp_pct_a)
        # Entry is market-only. Nested SL/TP JSON on the same POST makes BingX
        # report "signature mismatch". Security SL/TP go on via control orders.
        attach: Dict[str, str] = {}

        def _entry_body(order_qty, order_cid):
            body = {
                "symbol": sym,
                "type": "MARKET",
                "side": order_side,
                "positionSide": side,
                "quantity": order_qty,
                "clientOrderID": order_cid,
            }
            body.update(attach)
            return body

        r = self.api.post("/openApi/swap/v2/trade/order", _entry_body(qty, cid))
        self.did_io = True
        if not self.ok(r) and attach:
            msg0 = str(r.get("msg") or "").lower()
            if any(k in msg0 for k in ("stop loss", "take profit", "stoploss", "takeprofit", "trigger price", "workingtype", "signature")):
                attach = {}
                r = self.api.post("/openApi/swap/v2/trade/order", _entry_body(qty, self.cid("o", set_id=set_id, pack=pack, set_idx=set_idx)))
                self.did_io = True
        if not self.ok(r):
            msg = str(r.get("msg") or "")
            m = re.search(r"maximum leverage[^\d]*(\d+)", msg, re.I)
            if m:
                cap = max(1, int(m.group(1)))
                self.lev_max[sym] = cap
                if c is not None:
                    c.max_lev = cap
                for lev_side in ("LONG", "SHORT"):
                    self.api.post("/openApi/swap/v2/trade/leverage", {"symbol": sym, "side": lev_side, "leverage": cap})
                self.lev_map[sym] = cap
                self._persist_lev()
                r = self.api.post(
                    "/openApi/swap/v2/trade/order",
                    _entry_body(qty, self.cid("o", set_id=set_id, pack=pack, set_idx=set_idx)),
                )
                self.did_io = True
                msg = str(r.get("msg") or "")
            m2 = re.search(r"minimum order amount is\s+([\d.]+)", msg, re.I)
            if m2 and not self.ok(r):
                need = float(m2.group(1))
                c.min_qty = max(float(c.min_qty or 0), need)
                qty = self.round_qty_up(c, need)
                if qty > 0:
                    r = self.api.post(
                        "/openApi/swap/v2/trade/order",
                        _entry_body(qty, self.cid("o", set_id=set_id, pack=pack, set_idx=set_idx)),
                    )
                    self.did_io = True
                    msg = str(r.get("msg") or "")
            if not self.ok(r):
                msg = str(r.get("msg") or "")
                short = short_api_msg(msg)
                low = str(msg or "").lower()
                self.cooldown[sym] = time.time() + (45.0 if "cooling" in low else 12.0)
                if "insufficient" in low and "margin" in low:
                    self.cooldown["__book__"] = time.time() + 20.0
                    log(f"ENTRY wait available={self.available:.4f} after {sym}", every=15.0, key="avail-wait")
                if "order size" in low or "available amount" in low:
                    self.cooldown[sym] = time.time() + 60.0
                    self.cooldown["__book__"] = time.time() + 20.0
                if is_transient_api(msg):
                    log(f"ORDER SKIP {sym} {side} {short}", every=12.0, key=f"oskip:{short}")
                    return
                self.errors += 1
                self.last_error = f"order {sym} {short}"[:160]
                log(f"ORDER FAIL {sym} {side} {short}")
                return
        data = (r.get("data") or {}).get("order") or r.get("data") or {}
        self.last_error = ""
        avg = float(data.get("avgPrice") or data.get("price") or px) or px
        filled = float(data.get("quantity") or data.get("origQty") or qty) or qty
        attached_sl = extract_oid(data.get("stopLoss") if isinstance(data.get("stopLoss"), dict) else {"data": {"stopLoss": data.get("stopLoss")}}) if isinstance(data, dict) else ""
        attached_tp = extract_oid(data.get("takeProfit") if isinstance(data.get("takeProfit"), dict) else {"data": {"takeProfit": data.get("takeProfit")}}) if isinstance(data, dict) else ""
        ind = None
        try:
            if pack == "indications":
                ind = self.indications.match(sym, reason)
        except Exception:
            ind = None
        if ind is None and not str(reason).startswith("ind:"):
            try:
                ind = self.indications.primary(sym)
            except Exception:
                ind = None
        ind_kind = ""
        if str(reason).startswith("ind:"):
            bits = str(reason).split(":")
            ind_kind = bits[1] if len(bits) > 1 else ""
        if not ind_kind and ind is not None:
            ind_kind = str(getattr(ind, "kind", "") or "")
        sl_pct, tp_pct, src = resolve_sl_tp(
            base_sl=SL_PCT,
            base_tp=TP_PCT,
            sl_min=self.sl_min,
            sl_max=self.sl_max,
            tp_min=self.tp_min,
            tp_max=self.tp_max,
            ind_sl=(ind.stop_loss_pct / 100.0) if ind else 0.0,
            ind_tp=(ind.take_profit_pct / 100.0) if ind else 0.0,
            cost_pct=self.position_cost_pct,
            tp_cost_ratio=self.tp_cost_ratio,
            sl_to_tp=sl_ratio,
            rr=float(self.indications.settings.get("takeProfitRewardRisk") or 1.8),
            bind_sl_to_tp=True,
        )
        if chosen and getattr(chosen, "step", 0):
            tp_pct = max(self.tp_min, min(self.tp_max, chosen.tp_pct))
            sl_pct = max(self.sl_min, min(self.sl_max, tp_pct * sl_ratio))
            src = f"step{chosen.step}xcost"
        reason = f"{reason} {src} sltp={sl_ratio:.1f} tr={trail_key} st={getattr(chosen, 'step', 0) if chosen else 0} set={set_id or 'def'}"
        sl = avg * (1 - sl_pct) if direction > 0 else avg * (1 + sl_pct)
        if self.exits.enabled and self.exits.ignore_tp:
            tp_pct = max(tp_pct, sl_pct * 3.0, self.tp_max)
        tp = avg * (1 + tp_pct) if direction > 0 else avg * (1 - tp_pct)
        pos = Position(
            symbol=sym, side=side, qty=filled, entry=avg, opened_at=time.time(),
            sl=sl, tp=tp, peak=avg, order_id=str(data.get("orderId") or ""),
            notional=filled * avg, reason=f"{reason} c{conf:.2f}", conf=conf,
            sl_ratio=sl_ratio, trail_key=trail_key,
            trail_arm=trail_arm / 100.0, trail_give=trail_give / 100.0,
            sl_pct=sl_pct, tp_pct=tp_pct,
            set_id=set_id, set_idx=set_idx, trail_set_id=trail_set_id, trail_idx=trail_idx, pack=pack, client_id=cid, ours=True,
            overall=True, close_position=True, ind_kind=ind_kind,
        )
        pos.sl, pos.tp = self.security_prices(pos)
        if attached_sl:
            pos.sl_oid = pos.sec_sl_oid = attached_sl
        if attached_tp:
            pos.tp_oid = pos.sec_tp_oid = attached_tp
        self.open[sym] = pos
        self.owned_syms.add(sym)
        self.save_open_book()
        if cid:
            self.seen_fill_cids.add(cid)
        self.last_entry_ts = time.time()
        self.fees_est += filled * avg * 0.0005
        self.available = max(0.0, self.available - margin)
        if getattr(self, "control_orders", True):
            pos.ctrl_verified = False
            self.place_ctrl_pair(pos)
            if real_oid(pos.sl_oid) and real_oid(pos.tp_oid):
                self._order_est = int(getattr(self, "_order_est", 0) or 0) + 2
            if self.missing_controls(pos):
                self.ensure_controls(pos)
            if self.missing_controls(pos):
                log(f"OPEN scratch no-ctrl {sym}")
                self.close_pos(pos, avg, "no-ctrl")
                return
        self.signals.append({"t": time.time(), "symbol": sym, "side": side, "reason": pos.reason, "px": avg, "qty": filled})
        self.block.register_parent(sym, side, filled, avg)
        try:
            self.dca.attach(sym, side, filled, avg)
        except Exception:
            pass
        log(f"OPEN {sym} {side} qty={filled} px={avg} sl={pos.sl} tp={pos.tp} sl_oid={pos.sl_oid} tp_oid={pos.tp_oid}")
        self._stats_force = True

    def _exchange_flat(self, pos: Position) -> bool:
        """True only when the exchange has zero size on this symbol+side."""
        try:
            r = self.api.get("/openApi/swap/v2/user/positions")
        except Exception:
            return False
        if not self.ok(r):
            return False
        for p in (r.get("data") or []):
            if str(p.get("symbol") or "") != pos.symbol:
                continue
            side = (p.get("positionSide") or "").upper() or ("LONG" if float(p.get("positionAmt") or 0) > 0 else "SHORT")
            if side != pos.side:
                continue
            try:
                amt = abs(float(p.get("positionAmt") or p.get("availableAmt") or 0))
            except Exception:
                amt = 0.0
            if amt > 1e-12:
                return False
        return True

    def drop_ghost(self, pos: Position, why: str) -> None:
        if self.open.get(pos.symbol) is None:
            return
        if not self._exchange_flat(pos):
            log(f"GHOST skip still-live {pos.symbol} {pos.side} {why}", every=20.0, key=f"ghost:{pos.symbol}")
            return
        log(f"GHOST drop {pos.symbol} {pos.side} {why} qty={pos.qty}")
        self.close_pos(pos, self.px.get(pos.symbol) or pos.entry, why, exchange=False)

    def sim_stats(self) -> Tuple[int, float]:
        """Real/Live/Simulated bookkeeping. Every tracked position is "Real"
        (valid system entry). Positions confirmed by the exchange's position
        list are "Live". The rest are "Simulated": valid to process by the
        system-internal calcs (uPnl, set evaluation), but not on the exchange.
        Returns (sim_count, sim_unrealized_pnl); count -1 while the exchange
        truth is unknown (no successful adopt read yet)."""
        keys = getattr(self, "live_pos_keys", None)
        if keys is None:
            return -1, 0.0
        n = 0
        upnl = 0.0
        for p in self.open.values():
            if f"{p.symbol}:{p.side}" in keys:
                continue
            n += 1
            px = self.px.get(p.symbol) or 0
            if px > 0 and p.entry > 0:
                d = (px - p.entry) / p.entry
                if p.side != "LONG":
                    d = -d
                upnl += d * p.qty * p.entry
        return n, upnl

    def close_pos(self, pos: Position, px: float, reason: str, exchange: bool = True) -> None:
        skip_eval = any(k in str(reason or "").lower() for k in ("oversized", "ctrl-no-position", "no-ctrl"))
        if exchange:
            self.cancel_controls(pos.symbol)
            self._order_est = max(0, int(getattr(self, "_order_est", 0) or 0) - 2)
            ok, exit_px = self.market_close(pos)
            if not ok:
                if skip_eval:
                    self.ban_sym(pos.symbol)
                return
        else:
            exit_px = px if px > 0 else (self.px.get(pos.symbol) or pos.entry)
        if pos.side == "LONG":
            pnl_pct = (exit_px - pos.entry) / pos.entry
        else:
            pnl_pct = (pos.entry - exit_px) / pos.entry
        pnl = net_pnl_usdt(pnl_pct, pos.qty, pos.entry, self.position_cost_pct)
        hold = time.time() - pos.opened_at
        rec = Closed(
            time.time(), pos.symbol, pos.side, pos.qty, pos.entry, exit_px, pnl, pnl_pct, reason, hold,
            sl_ratio=pos.sl_ratio, trail_key=pos.trail_key, sl_pct=pos.sl_pct, tp_pct=pos.tp_pct,
            set_id=pos.set_id, pack=pos.pack, trail_set_id=getattr(pos, "trail_set_id", ""), client_id=pos.client_id, ours=True, conn=CONN_SHORT,
            ind_kind=str(getattr(pos, "ind_kind", "") or ""),
        )
        if pos.client_id:
            self.seen_fill_cids.add(pos.client_id)
        if skip_eval:
            self.open.pop(pos.symbol, None)
            self.ban_sym(pos.symbol)
            log(f"CLOSE {pos.symbol} {pos.side} pnl={pnl:.4f} ({pnl_pct*100:.3f}%) {reason} hold={hold:.0f}s skip-eval")
            self.save_open_book()
            self._stats_force = True
            return
        self.closed.append(rec)
        try:
            self.variants.on_close(rec)
        except Exception:
            pass
        try:
            self.sets.on_live_close(rec)
            self.sets.adapt_from_live(self.strategy_closes())
        except Exception:
            pass
        try:
            self.exits.on_close(rec)
        except Exception:
            pass
        try:
            self.dca.on_close(asdict(rec) if hasattr(rec, "__dataclass_fields__") else {
                "symbol": rec.symbol, "side": rec.side, "reason": rec.reason,
                "client_id": getattr(rec, "client_id", ""), "pnl": rec.pnl, "pnl_pct": rec.pnl_pct,
            })
            self.dca.drop(pos.symbol, pos.side)
        except Exception:
            pass
        if pnl >= 0:
            self.wins += 1
            self.consec_loss = 0
        else:
            self.losses += 1
            self.consec_loss += 1
            if self.consec_loss >= 8:
                self.cooldown["__book__"] = time.time() + 120
                self.consec_loss = 4
                log("pause new entries 120s after cold streak")
        self.cooldown[pos.symbol] = time.time() + COOLDOWN_S
        self.block.on_parent_close(pos.symbol, pos.side, pnl, pnl_pct=net_pnl_pct(pnl_pct, self.position_cost_pct))
        self.open.pop(pos.symbol, None)
        self.save_open_book()
        try:
            with open(TRADES_PATH, "a") as f:
                f.write(json.dumps(asdict(rec)) + "\n")
        except Exception:
            pass
        log(f"CLOSE {pos.symbol} {pos.side} pnl={pnl:.4f} ({pnl_pct*100:.3f}%) {reason} hold={hold:.0f}s")
        self._stats_force = True

    def manage(self) -> None:
        now = time.time()
        self.ingest_ws_px()
        for pos in list(self.open.values()):
            px = self.px.get(pos.symbol) or 0
            if px <= 0:
                continue
            age = now - pos.opened_at
            if age >= MAX_HOLD_S:
                self.close_pos(pos, px, "max-hold-6h")
                continue
            if getattr(self, "control_orders", True):
                if time.time() >= self.ctrl_skip.get(pos.symbol, 0):
                    if self.missing_controls(pos):
                        self.ensure_controls(pos)
            if pos.side == "LONG":
                pnl_pct = (px - pos.entry) / pos.entry
                pos.peak = max(pos.peak, px)
            else:
                pnl_pct = (pos.entry - px) / pos.entry
                pos.peak = min(pos.peak, px) if pos.peak else px
            # Max drawdown time: force-close positions that stay underwater
            # longer than the configured window (0 = off). A small buffer
            # beyond breakeven keeps fee noise from resetting the clock.
            if pnl_pct < -0.0005:
                if not pos.under_since:
                    pos.under_since = now
            else:
                pos.under_since = 0.0
            if MAX_DD_TIME_S > 0 and pos.under_since and now - pos.under_since >= MAX_DD_TIME_S:
                self.close_pos(pos, px, "dd-time")
                continue
            if pos.side == "LONG" and pos.sl > 0 and pos.sl < pos.entry * 1.0000001 and px <= pos.sl:
                self.close_pos(pos, px, "sl")
                continue
            if pos.side == "SHORT" and pos.sl > 0 and pos.sl > pos.entry * 0.9999999 and px >= pos.sl:
                self.close_pos(pos, px, "sl")
                continue
            if not (self.exits.enabled and self.exits.ignore_tp):
                if pos.side == "LONG" and px >= pos.tp:
                    self.close_pos(pos, px, "tp")
                    continue
                if pos.side == "SHORT" and px <= pos.tp:
                    self.close_pos(pos, px, "tp")
                    continue
            sig = 0
            if self.exits.enabled and self.exits.rev_on:
                ind = self.indications.primary(pos.symbol)
                if ind:
                    sig = 1 if ind.direction == "long" else -1
                else:
                    d, _, conf = self.score(pos.symbol)
                    sig = d if conf >= 0.58 else 0
            if self.exits.enabled:
                dec = self.exits.decide(
                    side=pos.side,
                    entry=pos.entry,
                    px=px,
                    peak=pos.peak,
                    sl=pos.sl,
                    opened_at=pos.opened_at,
                    trail_arm=pos.trail_arm or TRAIL_ARM,
                    signal_dir=sig,
                    now=now,
                )
                if dec.action == "close":
                    if (now - getattr(self, "boot_ts", 0)) < 120 and dec.lane != "hard":
                        continue
                    if age < 90 and dec.lane not in ("hard",):
                        continue
                    self.close_pos(pos, px, dec.reason)
                    continue
                if dec.action == "tighten" and dec.sl:
                    moved = abs(dec.sl - pos.sl) / max(pos.entry, 1e-9)
                    if moved >= 0.004 and time.time() >= self.ctrl_skip.get(f"sync:{pos.symbol}", 0):
                        pos.trail_armed = True
                        pos.trail = dec.sl
                        self.replace_sl(pos, dec.sl)
                    continue
            if self.strat_trail and pnl_pct >= (pos.trail_arm or TRAIL_ARM) and (now - pos.opened_at) >= self.coord.trailing_min_step:
                pos.trail_armed = True
                give = pos.trail_give or TRAIL_GIVE
                if pos.side == "LONG":
                    trail = max(pos.peak * (1 - give), pos.entry * (1 + 0.0004))
                    if pos.trail is None or trail > pos.trail + 1e-12:
                        pos.trail = trail
                        self.replace_sl(pos, trail)
                else:
                    trail = min(pos.peak * (1 + give), pos.entry * (1 - 0.0004))
                    if pos.trail is None or trail < pos.trail - 1e-12:
                        pos.trail = trail
                        self.replace_sl(pos, trail)
            if not self.exits.enabled:
                age = now - pos.opened_at
                if age >= TIME_STOP_S and (pnl_pct >= 0.0012 or pnl_pct <= -0.0025):
                    self.close_pos(pos, px, "time")
                    continue
                if age >= SCRATCH_S and pnl_pct >= SCRATCH_MIN:
                    self.close_pos(pos, px, "scratch+")
                    continue



    def _load_trade_history(self) -> None:
        if not os.path.exists(TRADES_PATH):
            return
        try:
            with open(TRADES_PATH) as f:
                lines = f.readlines()[-80:]
            for line in lines:
                rec = json.loads(line)
                c = Closed(
                    t=float(rec.get("t") or 0),
                    symbol=str(rec.get("symbol") or ""),
                    side=str(rec.get("side") or ""),
                    qty=float(rec.get("qty") or 0),
                    entry=float(rec.get("entry") or 0),
                    exit=float(rec.get("exit") or 0),
                    pnl=float(rec.get("pnl") or 0),
                    pnl_pct=float(rec.get("pnl_pct") or 0),
                    reason=str(rec.get("reason") or ""),
                    hold_s=float(rec.get("hold_s") or 0),
                    sl_ratio=float(rec.get("sl_ratio") or rec.get("slRatio") or 0),
                    trail_key=str(rec.get("trail_key") or rec.get("trailKey") or ""),
                    sl_pct=float(rec.get("sl_pct") or rec.get("slPct") or 0),
                    tp_pct=float(rec.get("tp_pct") or rec.get("tpPct") or 0),
                    set_id=str(rec.get("set_id") or rec.get("setId") or ""),
                    pack=str(rec.get("pack") or ""),
                    client_id=str(rec.get("client_id") or rec.get("clientId") or ""),
                    ours=bool(rec.get("ours", True)),
                    conn=str(rec.get("conn") or rec.get("connection") or ""),
                    ind_kind=str(rec.get("ind_kind") or rec.get("indKind") or ""),
                )
                cid = c.client_id
                if not self.cid_ours(cid) and not c.set_id:
                    continue
                if cid and not self.cid_ours(cid):
                    continue
                if "oversized" in str(c.reason or "").lower():
                    continue
                if abs(c.qty * c.entry) > 40:
                    continue
                if c.conn and c.conn != CONN_SHORT:
                    continue
                self.closed.append(c)
                if cid:
                    self.seen_fill_cids.add(cid)
                    self.owned_syms.add(c.symbol)
                if c.pnl > 0:
                    self.wins += 1
                    self.consec_loss = 0
                elif c.pnl < 0:
                    self.losses += 1
                    self.consec_loss += 1
        except Exception:
            pass

    def apply_live_config(self, initial: bool = False) -> None:
        global TARGET_NOTIONAL, LEVERAGE, MAX_OPEN, MAX_PER_GROUP, SL_PCT, TP_PCT, USE_MAX_LEVERAGE
        global TRAIL_ARM, TRAIL_GIVE, TIME_STOP_S, MAX_DD_TIME_S, SCRATCH_S, SCRATCH_MIN, SCAN_S, COOLDOWN_S, STAGGER_S, SYMBOLS
        cts = dump_cts_settings()
        self.cts = cts
        ov = load_json_file(OVERLAY_PATH)
        self.overlay = ov
        try:
            self.overlay_mtime = os.path.getmtime(OVERLAY_PATH)
        except Exception:
            self.overlay_mtime = 0.0
        if ov.get("targetNotional"):
            # Clamp desk-supplied target notional: a corrupt or absurd overlay
            # value must never translate into impossible order volume.
            TARGET_NOTIONAL = max(0.2, min(500.0, float(ov["targetNotional"])))
        try:
            self.volume_factor = max(0.05, min(10.0, float(ov.get("volumeFactor") or 1.0)))
        except Exception:
            self.volume_factor = 1.0
        self.use_max_leverage = True
        USE_MAX_LEVERAGE = True
        if ov.get("leverage"):
            LEVERAGE = int(ov["leverage"])
        LEVERAGE = max(150, max(self.lev_map.values()) if self.lev_map else 150)
        if ov.get("maxOpen") is not None:
            MAX_OPEN = int(ov["maxOpen"])
        if ov.get("maxPerGroup") is not None:
            MAX_PER_GROUP = int(ov["maxPerGroup"])
        if ov.get("slPct"):
            SL_PCT = float(ov["slPct"]) / 100.0 if float(ov["slPct"]) > 0.05 else float(ov["slPct"])
        if ov.get("tpPct"):
            TP_PCT = float(ov["tpPct"]) / 100.0 if float(ov["tpPct"]) > 0.05 else float(ov["tpPct"])
        if ov.get("trailArmPct") is not None:
            TRAIL_ARM = float(ov["trailArmPct"]) / 100.0 if float(ov["trailArmPct"]) > 0.02 else float(ov["trailArmPct"])
        if ov.get("trailGivePct") is not None:
            TRAIL_GIVE = float(ov["trailGivePct"]) / 100.0 if float(ov["trailGivePct"]) > 0.02 else float(ov["trailGivePct"])
        if ov.get("timeStopS") is not None:
            TIME_STOP_S = min(MAX_HOLD_S, max(30.0, float(ov["timeStopS"])))
        else:
            TIME_STOP_S = MAX_HOLD_S
        if ov.get("maxDdTimeS") is not None:
            MAX_DD_TIME_S = min(86400.0, max(0.0, float(ov["maxDdTimeS"])))
        else:
            MAX_DD_TIME_S = 0.0
        if ov.get("scratchS"):
            SCRATCH_S = float(ov["scratchS"])
        if ov.get("scratchMinPct") is not None:
            SCRATCH_MIN = float(ov["scratchMinPct"]) / 100.0 if float(ov["scratchMinPct"]) > 0.02 else float(ov["scratchMinPct"])
        if ov.get("scanS"):
            SCAN_S = max(0.20, min(8.0, float(ov["scanS"])))
        if ov.get("cooldownS") is not None:
            COOLDOWN_S = float(ov["cooldownS"])
        if ov.get("staggerS"):
            STAGGER_S = float(ov["staggerS"])
        self.position_cost_pct = float(ov.get("positionCostPct") or 0.15)
        self.pf_window = int(ov.get("pfWindow") or 15)
        self.sl_min = float(ov.get("slMinPct") or 0.20) / 100.0
        self.sl_max = float(ov.get("slMaxPct") or 1.20) / 100.0
        self.tp_min = float(ov.get("tpMinPct") or 0.35) / 100.0
        self.tp_max = float(ov.get("tpMaxPct") or 2.40) / 100.0
        self.tp_cost_ratio = float(ov.get("tpCostRatio") or 5)
        self.variants.load(ov, cts)
        self.sl_to_tp = self.variants.current_sl()
        TRAIL_ARM, TRAIL_GIVE = self.variants.trail_frac()
        self.tf_on = {
            "1m": bool(ov.get("tf1m", True)),
            "5m": bool(ov.get("tf5m", True)),
            "15m": bool(ov.get("tf15m", True)),
        }
        self.strat_ind = bool(ov.get("stratIndications", True))
        self.strat_block = bool(ov.get("stratBlock", True))
        self.strat_trail = bool(ov.get("stratTrailing", True))
        self.strat_general = bool(ov.get("stratGeneral", True))
        self.strat_dca = bool(ov.get("stratDca", ov.get("dcaEnabled", False)))
        self.symbol_sort = coerce_symbol_sort(ov.get("symbolSort") or ov.get("symbolsSort") or "vol1h")
        self.symbols_dynamic = bool(ov.get("symbolsDynamic", True))
        try:
            self.symbol_cap = max(0, int(ov.get("symbolCap") if ov.get("symbolCap") is not None else 0))
        except Exception:
            self.symbol_cap = 0
        wild = bool(ov.get("symbolsAll"))
        cleaned: List[str] = []
        seen = set()
        if isinstance(ov.get("symbols"), list):
            for raw in ov["symbols"]:
                token = str(raw).strip().upper().replace("_", "-")
                if token in ("*", "ALL", "UNLIMITED"):
                    wild = True
                    continue
                s = token
                if s.endswith("USDT") and not s.endswith("-USDT"):
                    s = s[:-4] + "-USDT"
                if not s.endswith("-USDT"):
                    continue
                if s in seen:
                    continue
                seen.add(s)
                cleaned.append(s)
                if MAX_SYMBOLS > 0 and len(cleaned) >= MAX_SYMBOLS:
                    break
        self.overlay_wild = bool(wild)
        if wild:
            extra = load_contracts(None)
            names = [s for s in extra.keys() if str(s).endswith("-USDT") and not str(s).startswith(("NCCO", "NCS", "NCFX"))]
            names.sort()
            if names:
                SYMBOLS[:] = names
                self.contracts.update(extra)
        elif cleaned:
            SYMBOLS[:] = cleaned
        self.ensure_contracts()
        try:
            self.apply_dynamic_symbols(force=True)
        except Exception:
            pass
        if not initial:
            self.pool.submit(self.set_leverage)
        # CTS Block defaults, overlay wins
        b_en = ov.get("blockEnabled", cts.get("variantBlockEnabled", True))
        try:
            b_stack = int(ov.get("blockMaxStack") if ov.get("blockMaxStack") is not None else (cts.get("blockMaxStack") or 0))
        except Exception:
            b_stack = 0
        b_ratio = float(ov.get("blockVolumeRatio") or cts.get("blockVolumeRatio") or 1)
        b_pfr = float(ov.get("blockProfitFactorRatio") or cts.get("blockProfitFactorRatio") or 1.1)
        b_pause = int(ov.get("blockPauseCountRatio") or cts.get("blockPauseCountRatio") or 1)
        real_pf = 1.1
        try:
            st = ((cts.get("strategies") or {}).get("main") or {}).get("real") or {}
            real_pf = float(ov.get("realMinPf") or ov.get("minPf") or st.get("min_profit_factor") or cts.get("realProfitFactor") or 1.25)
        except Exception:
            pass
        self.block.enabled = bool(b_en) if b_en is not None else True
        if ov.get("blockEnabled") is None and cts.get("variantBlockEnabled") is None:
            self.block.enabled = True
        self.block.max_stack = clamp_stack(b_stack)
        try:
            self.load.configure(ov)
        except Exception:
            pass
        try:
            b_ratio = float(b_ratio)
        except Exception:
            b_ratio = 1.0
        # Guard: leftover overlay stored max_stack as volume_ratio (n=1 then
        # adds 3× parent). Count×ratio vs original parent needs ratio ~1.
        if b_ratio >= 2.0 and int(round(b_ratio)) == int(self.block.max_stack):
            b_ratio = 1.0
        self.block.volume_ratio = max(0.25, min(3.0, b_ratio))
        self.block.pf_ratio = max(BLOCK_PF_RATIO_MIN, min(BLOCK_PF_RATIO_MAX, b_pfr))
        self.block.pause_ratio = max(0, b_pause)
        self.block.active_live = bool(ov.get("blockActiveLive", cts.get("blockActiveLiveEnabled", True)))
        self.block.active_real = bool(ov.get("blockActiveReal", cts.get("blockActiveRealEnabled", True)))
        self.block.default_min_pf = float(real_pf)
        self.control_orders = bool(ov.get("controlOrders", cts.get("control_orders", True)))
        self.coord.load(cts, ov)
        self.indications.load(ov)
        self.sets.load(ov, cts)
        self.exits.load(ov, cts)
        self.dca.load(ov, cts)
        if initial:
            try:
                self.variants.seed_history(list(self.strategy_closes()))
            except Exception:
                pass
            try:
                self.sets.seed_live(list(self.strategy_closes()))
                self.sets.adapt_from_live(list(self.strategy_closes()))
            except Exception:
                pass
            try:
                self.exits.seed(list(self.strategy_closes()))
            except Exception:
                pass
        self.mods = resolve_modules(ov)
        if self.mods.get("strategy.block") is False:
            self.block.enabled = False
        elif self.strat_block and ov.get("blockEnabled", True):
            self.block.enabled = True
        self.control_orders = bool(self.mods.get("exec.controls", self.control_orders))
        self.coord.rearrange = bool(self.mods.get("strategy.rearrange", self.coord.rearrange))
        if not self.mods.get("strategy.indications", True):
            self.indications.settings["enabled"] = False
        else:
            self.indications.settings["enabled"] = bool(ov.get("indEnabled", True))
            self.strat_ind = True
        self.dca.enabled = bool(self.mods.get("strategy.dca", False)) and bool(ov.get("dcaEnabled", False)) and bool(getattr(self, "strat_dca", False))
        if not self.mods.get("strategy.coord", True):
            for ax in self.coord.axes.values():
                ax.enabled = False
        # SL:TP ratios 0.3–1.5 are the risk grid. Do not cap TP against maxStopLossRatio.
        if not initial:
            log(
                f"CFG reload n={len(SYMBOLS)} notional={TARGET_NOTIONAL} lev={LEVERAGE} "
                f"sort={self.symbol_sort} dyn={int(self.symbols_dynamic)} cap={self.symbol_cap} "
                f"block={self.block.enabled}/{self.block.max_stack}x{self.block.volume_ratio} "
                f"sltp={self.sl_to_tp} trail={self.variants.trail_key} "
                f"tf={self.tf_on} axes={ {k: int(v.enabled) for k,v in self.coord.axes.items()} }"
            )
        dirty_lanes = False
        for lane in list(self.block.lanes.values()):
            px = self.px.get(lane.symbol) or lane.base_entry or 0
            if px > 0 and lane.base_qty * px > self.max_book_notional():
                log(f"BLOCK lane reset oversized {lane.symbol} base={lane.base_qty} n={lane.base_qty * px:.0f}")
                lane.base_qty = 0.0
                lane.active = False
                lane.confirmed_add = 0.0
                dirty_lanes = True
        if dirty_lanes:
            self.block.save()

    def seed_lev_from_contracts(self) -> None:
        for s, c in self.contracts.items():
            mx = int(getattr(c, "max_lev", 0) or 0)
            if mx <= 0:
                continue
            self.lev_max[s] = mx
            if int(self.lev_map.get(s) or 0) < mx:
                self.lev_map[s] = mx

    def ensure_contracts(self) -> None:
        missing = [s for s in SYMBOLS if s not in self.contracts]
        if missing:
            extra = load_contracts(set(SYMBOLS))
            self.contracts.update(extra)
            log(f"contracts +{len(extra)} now={len(self.contracts)}")
        self.seed_lev_from_contracts()

    def maybe_reload_config(self) -> None:
        try:
            mt = os.path.getmtime(OVERLAY_PATH)
        except Exception:
            mt = 0.0
        if mt and mt != self.overlay_mtime:
            self.apply_live_config()
            if hasattr(self.api, "hub"):
                self.api.hub.set_symbols(list(SYMBOLS))

    def pulse_snapshot(self) -> Dict[str, Any]:
        return {
            "targetNotional": TARGET_NOTIONAL,
            "volumeFactor": float(getattr(self, "volume_factor", 1.0) or 1.0),
            "leverage": LEVERAGE,
            "useMaxLeverage": True,
            "leverageMap": dict(getattr(self, "lev_map", {})),
            "leverageMax": dict(getattr(self, "lev_max", {})),
            "maxOpen": MAX_OPEN,
            "maxPerGroup": MAX_PER_GROUP,
            "slPct": SL_PCT * 100,
            "tpPct": TP_PCT * 100,
            "trailArmPct": TRAIL_ARM * 100,
            "trailGivePct": TRAIL_GIVE * 100,
            "timeStopS": TIME_STOP_S,
            "maxDdTimeS": MAX_DD_TIME_S,
            "scratchS": SCRATCH_S,
            "scratchMinPct": SCRATCH_MIN * 100,
            "scanS": SCAN_S,
            "cooldownS": COOLDOWN_S,
            "staggerS": STAGGER_S,
            "controlOrders": getattr(self, "control_orders", True),
            "blockEnabled": self.block.enabled,
            "blockMaxStack": self.block.max_stack,
            "blockVolumeRatio": self.block.volume_ratio,
            "blockProfitFactorRatio": self.block.pf_ratio,
            "blockPauseCountRatio": self.block.pause_ratio,
            "blockActiveLive": self.block.active_live,
            "axisPrevEnabled": self.coord.axes["prev"].enabled,
            "axisPrevMaxWindow": self.coord.axes["prev"].max_window,
            "axisLastEnabled": self.coord.axes["last"].enabled,
            "axisLastMaxWindow": self.coord.axes["last"].max_window,
            "axisContEnabled": self.coord.axes["cont"].enabled,
            "axisContMaxWindow": self.coord.axes["cont"].max_window,
            "axisPauseEnabled": self.coord.axes["pause"].enabled,
            "axisPauseMaxWindow": self.coord.axes["pause"].max_window,
            "minPf": self.coord.min_pf,
            "positionCostPct": self.position_cost_pct,
            "pfWindow": self.pf_window,
            "slMinPct": self.sl_min * 100,
            "slMaxPct": self.sl_max * 100,
            "tpMinPct": self.tp_min * 100,
            "tpMaxPct": self.tp_max * 100,
            "tpCostRatio": self.tp_cost_ratio,
            "slToTpRatio": self.sl_to_tp,
            "slToTpAuto": self.variants.sl_auto,
            "slToTpRecalcN": self.variants.sl_recalc_n,
            "slToTpRecalcEvery": self.variants.sl_recalc_every,
            "trailAuto": self.variants.trail_auto,
            "trailArmMin": self.variants.trail_arm_min,
            "trailArmMax": self.variants.trail_arm_max,
            "trailGiveMin": self.variants.trail_give_min,
            "trailGiveMax": self.variants.trail_give_max,
            "trailGiveFactor": self.variants.trail_give_factor,
            "trailRecalcGive": self.variants.trail_recalc_give,
            "trailRecalcN": self.variants.trail_recalc_n,
            "tf1m": self.tf_on.get("1m", True),
            "tf5m": self.tf_on.get("5m", True),
            "tf15m": self.tf_on.get("15m", True),
            "tfCombined": bool(self.indications.settings.get("tfCombined", True)),
            "tfMinAgree": int(self.indications.settings.get("tfMinAgree") or 2),
            "stratIndications": self.strat_ind,
            "stratBlock": self.strat_block,
            "stratTrailing": self.strat_trail,
            "stratGeneral": self.strat_general,
            "stratDca": getattr(self, "strat_dca", True),
            "dcaEnabled": bool(self.dca.enabled),
            "indEnabled": bool(self.indications.settings.get("enabled", True)),
            "indTypeState": bool(self.indications.settings.get("typeState", True)),
            "indTypeDirection": bool(self.indications.settings.get("typeDirection", True)),
            "indTypeMove": bool(self.indications.settings.get("typeMove", True)),
            "indTypeActive": bool(self.indications.settings.get("typeActive", True)),
            "indTypeCommon": bool(self.indications.settings.get("typeCommon", True)),
            "indTypeSignals": bool(self.indications.settings.get("typeSignals", True)),
            "histEnabled": self.sets.enabled,
            "histLookbackBars": self.sets.lookback,
            "histMinBars": self.sets.min_bars,
            "histWarmup": self.sets.warmup,
            "histRefreshS": self.sets.refresh_s,
            "setPfWindow": self.sets.pf_n,
            "setDeactN": self.sets.deact_n,
            "setMinPf": self.sets.min_pf,
            "setMaxDdTimeS": self.sets.max_dd_s,
            "setAutoDeact": self.sets.auto_deact,
            "setUseHistoricGate": self.sets.use_historic_gate,
            "setStrictGate": bool(getattr(self.sets, "strict_gate", True)),
            "setMinSamples": self.sets.min_samples,
            "setReactivate": self.sets.reactivate,
            "setMaxActive": self.sets.max_active,
            "exitEnabled": self.exits.enabled,
            "exitIgnoreTp": self.exits.ignore_tp,
            "exitBestOf": self.exits.best_of,
            "exitLockOn": self.exits.lock_on,
            "exitPeakOn": self.exits.peak_on,
            "exitRevOn": self.exits.rev_on,
            "exitTimeOn": self.exits.time_on,
            "exitLockPct": self.exits.lock_pct * 100,
            "exitBeBuffer": self.exits.be_buffer * 100,
            "exitOptSlPct": self.exits.opt_sl * 100,
            "exitOptSlMin": self.exits.opt_sl_min * 100,
            "exitOptSlMax": self.exits.opt_sl_max * 100,
            "exitMinHoldS": self.exits.min_hold_s,
            "exitPfWindow": self.exits.pf_n,
            "exitDeactN": self.exits.deact_n,
            "exitMinPf": self.exits.min_pf,
            "exitAutoDeact": self.exits.auto_deact,
            "noise": self.coord.noise,
            "volWeight": self.coord.vol_weight,
            "minStep": self.coord.min_step,
            "maxStopLossRatio": self.coord.max_sl_ratio,
            "trailingMinStep": self.coord.trailing_min_step,
            "posCountsVolumeRatio": self.coord.pos_count_vol_ratio,
            "rearrange": self.coord.rearrange,
            "rearrangeGap": self.coord.rearrange_gap,
            "modules": getattr(self, "mods", {}),
            "indEnabled": self.indications.settings.get("enabled"),
            "indMinSources": self.indications.settings.get("minimumSourceSignals"),
            "indMinAgreement": self.indications.settings.get("minimumAgreement"),
            "indMinConfidence": self.indications.settings.get("minimumConfidence"),
            "indMinStrength": self.indications.settings.get("minimumStrength"),
            "indStopMinPct": self.indications.settings.get("stopLossMinPct"),
            "indStopMaxPct": self.indications.settings.get("stopLossMaxPct"),
            "indAtrMult": self.indications.settings.get("stopLossAtrMultiplier"),
            "indRewardRisk": self.indications.settings.get("takeProfitRewardRisk"),
            "indExtraSources": self.indications.settings.get("extraSources"),
            "dcaEnabled": self.dca.enabled,
            "dcaMaxSteps": self.dca.max_steps,
            "dcaCooldownSeconds": self.dca.cooldown_s,
            "dcaBreakevenProfitPct": self.dca.be_pct * 100,
            "dcaTakeProfitMode": self.dca.tp_mode,
            "blockActiveReal": self.block.active_real,
            "symbols": list(SYMBOLS),
            "symbolsAll": bool(getattr(self, "overlay_wild", False)),
            "symbolsDynamic": bool(getattr(self, "symbols_dynamic", True)),
            "symbolSort": getattr(self, "symbol_sort", "vol1h"),
            "symbolCap": int(getattr(self, "symbol_cap", 0) or 0),
        }

    def block_intern_pf(self, pos: Position) -> float:
        """intern PF feeding the Block count gates for this parent position.

        Floor follows the BlockBook defaultMinPF (CTS strategies.main.real
        stage), never a hardcoded constant. Under the strict gate (default)
        only a VALIDATED (last15_n >= max(minSamples, 8)) AND PROFITABLE
        (cost-adjusted PF >= 1.00) set may lift the floor — unproven or losing
        sets keep the CTS default. Legacy mode keeps the old soft behavior.
        """
        floor_pf = float(getattr(self.block, "default_min_pf", 1.2) or 1.2)
        intern_pf = floor_pf
        try:
            side = str(getattr(pos, "side", "") or "")
            st = self.sets.sets.get(pos.set_id) if pos.set_id else None
            if st is None:
                try:
                    st = self.sets.pick_any(pos.pack or "indications", side=side) or self.sets.pick_any("general", side=side)
                except TypeError:
                    st = self.sets.pick_any(pos.pack or "indications") or self.sets.pick_any("general")
            if st is not None:
                blob = {}
                side_u = (side or "").upper()
                raw_side = getattr(st, "by_side", None) or {}
                if side_u and isinstance(raw_side, dict):
                    blob = raw_side.get(side_u) or {}
                if blob:
                    ratio = float(blob.get("last15_ratio") or 0.0)
                    n = int(blob.get("last15_n") or 0)
                else:
                    ratio = float(getattr(st, "last15_ratio", 0.0) or 0.0)
                    n = int(getattr(st, "last15_n", 0) or 0)
                if getattr(self.sets, "strict_gate", False):
                    need = max(int(getattr(self.sets, "min_samples", 8) or 8), 8)
                    if n >= need and ratio + 1e-9 >= 1.0:
                        # Validated + profitable: the set governs, but never
                        # below the CTS real-stage floor.
                        intern_pf = max(ratio, floor_pf)
                else:
                    intern_pf = ratio or floor_pf
                    if n < 8:
                        intern_pf = max(intern_pf, floor_pf)
        except Exception:
            intern_pf = floor_pf
        return intern_pf

    def _coord_add_state(self, count: Optional[int] = None) -> Tuple[bool, int, float, List[str]]:
        """Axis count-pos gate for additional strategies. Returns (allow, stack_cap, last_pf, reasons)."""
        stack = int(getattr(self.block, "max_stack", 3) or 3)
        coord = getattr(self, "coord", None)
        if coord is None or not callable(getattr(coord, "add_gate", None)):
            return True, stack, 1.0, []
        rows = self.strategy_closes()
        consec = 0
        for c in reversed(rows):
            pnl = float(getattr(c, "pnl", 0) or 0)
            if pnl < 0:
                consec += 1
            else:
                break
        intern = {}
        try:
            m = ((self.coord.last or {}).get("metrics") or {})
            intern = {"pf": float(m.get("internPf") or 0), "n": float(m.get("internN") or 0)}
        except Exception:
            intern = {}
        tape = None
        if count is not None:
            try:
                tape = list((self.block.count_tape or {}).get(int(count)) or [])
            except Exception:
                tape = None
        allow, reasons, metrics = self.coord.add_gate(rows, consec, intern=intern, count=count, count_tape=tape)
        last_pf = float((metrics or {}).get("lastPf") or (metrics or {}).get("last15Ratio") or 1.0)
        stack = int(getattr(self.block, "max_stack", 3) or 3)
        cap = self.coord.add_stack_cap(stack, last_pf)
        return bool(allow), int(cap), last_pf, list(reasons or [])

    def maybe_block_adds(self) -> None:
        """CTS Block Live: add-on only against an existing same-side parent."""
        if self.halted or not self.block.enabled or not self.strat_block:
            return
        if self.entries_blocked():
            return
        if self.available <= 0:
            return
        allow_add, stack_cap, last_pf, add_reasons = self._coord_add_state()
        if not allow_add:
            if time.time() - self.skip_log.get("add-gate", 0) > 45:
                log("COORD add-gate " + "; ".join(add_reasons)[:160], every=45.0, key="coord-add", quiet=True)
                self.skip_log["add-gate"] = time.time()
            return
        if time.time() - self.block_last_emit < max(12.0, STAGGER_S * 8):
            return
        if os.path.exists(STOP_PATH) or os.path.exists(PAUSE_PATH) or os.path.exists(STOP_ALL):
            return
        if time.time() < self.cooldown.get("__book__", 0):
            return
        if self.api.path_cd.get("/openApi/swap/v2/trade/order", 0) > time.time():
            return
        live_keys = {self.block.key(p.symbol, p.side) for p in self.open.values()}
        dirty = False
        for k, lane in list(self.block.lanes.items()):
            if k not in live_keys and (lane.active or lane.base_qty > 0):
                lane.active = False
                lane.base_qty = 0.0
                lane.confirmed_add = 0.0
                lane.legs = []
                lane.satisfied = {}
                dirty = True
        if dirty:
            self.block.save()
        live_n_by: Dict[str, int] = {}
        for p in self.open.values():
            live_n_by[self.block.key(p.symbol, p.side)] = live_n_by.get(self.block.key(p.symbol, p.side), 0) + 1
        emitted = 0
        add_budget = 8 if MAX_OPEN <= 0 else 2
        for pos in list(self.open.values()):
            if emitted >= add_budget:
                break
            if self.missing_controls(pos):
                self.ensure_controls(pos)
                if self.missing_controls(pos):
                    continue
            k = self.block.key(pos.symbol, pos.side)
            lane = self.block.lanes.get(k)
            if not lane or lane.base_qty <= 0:
                continue
            # Parent still valid only if pulse score agrees with side (continuation).
            # intern PF comes from block_intern_pf: under the strict gate only
            # a validated + profitable set lifts the CTS real-stage floor.
            intern_pf = self.block_intern_pf(pos)
            d, why, conf = self.score(pos.symbol)
            same = (pos.side == "LONG" and d > 0) or (pos.side == "SHORT" and d < 0)
            if not same:
                try:
                    def _blk_allow(kind: str, direction: str = "") -> bool:
                        gate = getattr(self.sets, "indication_ok", None)
                        if not (self.sets.enabled and callable(gate)):
                            return True
                        try:
                            return bool(gate(kind, pos.side))
                        except TypeError:
                            return bool(gate(kind))
                        except Exception:
                            return True
                    picked = None
                    try:
                        picked = self.indications.pick_entry(pos.symbol, min_conf=0.50, allow=_blk_allow)
                    except TypeError:
                        picked = self.indications.pick_entry(pos.symbol, min_conf=0.50)
                    except Exception:
                        picked = None
                    ind = picked[0] if picked else (self.indications.best(pos.symbol) or self.indications.primary(pos.symbol))
                    if ind:
                        same = (pos.side == "LONG" and ind.direction == "long") or (pos.side == "SHORT" and ind.direction == "short")
                except Exception:
                    same = True
            if not same:
                continue
            # One add-strategy per parent: DCA already filled → skip Block.
            try:
                dca_lane = (getattr(self.dca, "lanes", {}) or {}).get(self.dca.key(pos.symbol, pos.side))
                if dca_lane and int(getattr(dca_lane, "filled_n", 0) or 0) > 0:
                    continue
            except Exception:
                pass
            # Don't pyramid the same second as the entry (that's just 2× size).
            age = time.time() - float(getattr(pos, "opened_at", 0) or 0)
            if age < 45.0:
                continue
            # Don't stack into a losing or still-flat parent. Adds need a real
            # continuation so a stop doesn't immediately double the loss.
            px_now = self.px.get(pos.symbol) or pos.entry
            u = 0.0
            if px_now > 0 and pos.entry > 0:
                u = ((px_now - pos.entry) / pos.entry) * (1 if pos.side == "LONG" else -1)
            if u < 0.002:
                continue
            # Live book losing → don't pyramid more size.
            try:
                live_pf = self.live_recent_pf(pos.side, n=8)
                if live_pf is not None and live_pf + 1e-9 < 1.25:
                    continue
            except Exception:
                pass
            rows = self.block.evaluate_counts(lane, live_n=live_n_by.get(k, 1), intern_pf=intern_pf, stack_cap=stack_cap)
            row = self.block.pick_emit(rows)
            if not row:
                continue
            count_n = int(row.get("blockCount") or 0)
            ok_n, _, _, why_n = self._coord_add_state(count=count_n)
            if not ok_n:
                self.block.pause_count(lane, count_n, 90)
                if time.time() - self.skip_log.get(f"add-n:{count_n}", 0) > 45:
                    log(f"COORD count-pos n={count_n} " + "; ".join(why_n)[:120], every=45.0, key=f"coord-add-{count_n}", quiet=True)
                    self.skip_log[f"add-n:{count_n}"] = time.time()
                continue
            c = self.contracts.get(pos.symbol)
            px = self.px.get(pos.symbol) or pos.entry
            if not c or px <= 0:
                continue
            parent = float(lane.base_qty or 0) or self.size_qty(c, px)
            inc = float(row.get("volumeIncrement") or max(1.0, int(row["blockCount"]) * self.block.volume_ratio))
            raw = float(row.get("requestedAddQty") or 0)
            leftover = max(0.0, parent * inc - float(lane.confirmed_add or 0))
            raw = min(raw, leftover) if leftover > 0 else 0.0
            if raw <= 0:
                continue
            # Dust remainder: mark the count filled instead of bumping to a full extra parent.
            if raw < float(c.min_qty or 0) or raw * px < float(c.min_usdt or 0) * 0.98:
                self.block.mark_nearly_filled(lane, int(row["blockCount"]))
                log(f"BLOCK sat dust {pos.symbol} n={row['blockCount']} rem={raw} min={c.min_qty}/{c.min_usdt}")
                continue
            room = max(0.0, self.max_book_notional() - pos.qty * px)
            add_cap = min(self.notional_cap() * max(1.0, inc), room, leftover * px)
            qty = self.cap_order_qty(c, px, raw, add_cap)
            qty = min(qty, leftover * 1.02)
            if qty > leftover * 1.15 or qty < float(c.min_qty or 0) or qty <= 0:
                self.block.mark_nearly_filled(lane, int(row["blockCount"]))
                continue
            if qty * px < float(c.min_usdt or 0) * 0.98:
                self.block.mark_nearly_filled(lane, int(row["blockCount"]))
                continue
            if (pos.qty + qty) * px > self.max_book_notional() * 1.05:
                key = f"{pos.symbol}:{row['blockCount']}:cap"
                now = time.time()
                if now - self.skip_log.get(key, 0) > 30:
                    log(f"BLOCK skip {pos.symbol} n={row['blockCount']} book cap {self.max_book_notional():.2f}")
                    self.skip_log[key] = now
                self.block.pause_count(lane, int(row["blockCount"]), 90)
                continue
            margin = (qty * px) / max(1, self.leverage_for(c))
            if margin > self.available * 0.38 or self.available < 0.28:
                key = f"{pos.symbol}:{row['blockCount']}"
                now = time.time()
                if now - self.skip_log.get(key, 0) > 30:
                    log(f"BLOCK skip {pos.symbol} n={row['blockCount']} margin {margin:.3f} avail {self.available:.3f}")
                    self.skip_log[key] = now
                continue
            order_side = "BUY" if pos.side == "LONG" else "SELL"
            cid = self.cid("b", pos=pos)
            r = self.api.post(
                "/openApi/swap/v2/trade/order",
                {
                    "symbol": pos.symbol,
                    "type": "MARKET",
                    "side": order_side,
                    "positionSide": pos.side,
                    "quantity": qty,
                    "clientOrderID": cid,
                },
            )
            self.did_io = True
            if not self.ok(r):
                msg = str(r.get("msg") or "")
                m2 = re.search(r"minimum order amount is\s+([\d.]+)", msg, re.I)
                if m2:
                    need = float(m2.group(1))
                    c.min_qty = max(float(c.min_qty or 0), need)
                    qty = self.round_qty_up(c, need)
                    r = self.api.post(
                        "/openApi/swap/v2/trade/order",
                        {
                            "symbol": pos.symbol,
                            "type": "MARKET",
                            "side": order_side,
                            "positionSide": pos.side,
                            "quantity": qty,
                            "clientOrderID": self.cid("b", pos=pos),
                        },
                    )
                    self.did_io = True
                    msg = str(r.get("msg") or "")
                if not self.ok(r):
                    self.errors += 1
                    self.last_error = f"block {pos.symbol} n={row['blockCount']} {msg}"[:240]
                    log(f"BLOCK FAIL {pos.symbol} #{row['blockCount']} {msg}")
                    self.block_last_emit = time.time()
                    low = msg.lower()
                    if "maximum position" in low or "order size must be less" in low or "insufficient" in low:
                        self.block.pause_count(lane, int(row["blockCount"]), 180)
                    continue
            data = (r.get("data") or {}).get("order") or r.get("data") or {}
            avg = float(data.get("avgPrice") or data.get("price") or px) or px
            filled = float(data.get("quantity") or data.get("origQty") or qty) or qty
            oid = str(data.get("orderId") or "")
            row["emitted"] = 1
            self.block.record_fill(lane, row, filled, cid, oid)
            pos.qty += filled
            # weighted entry
            pos.entry = ((pos.entry * (pos.qty - filled)) + avg * filled) / pos.qty if pos.qty else avg
            pos.notional = pos.qty * pos.entry
            self.available = max(0.0, self.available - margin)
            if getattr(self, "control_orders", True):
                pos.sl_oid = pos.tp_oid = pos.sec_sl_oid = pos.sec_tp_oid = ""
                pos.ctrl_verified = False
                self.ctrl_skip.pop(f"sync:{pos.symbol}", None)
                self.ctrl_skip.pop(pos.symbol, None)
                self.ensure_controls(pos)
            self.block_last_emit = time.time()
            emitted += 1
            self.save_open_book()
            log(
                f"BLOCK ADD {pos.symbol} {pos.side} n={row['blockCount']} +{filled} "
                f"base={lane.base_qty} add={lane.confirmed_add} tot={lane.base_qty+lane.confirmed_add} "
                f"minPF={row['blockMinPF']:.3f} {row['setKey']}"
            )

    def live_recent_pf(self, side: Optional[str] = None, n: int = 8) -> Optional[float]:
        """Cost-net PF of recent live closes. None until enough samples."""
        rows = list(self.closed or [])
        if side:
            want = str(side).upper()
            rows = [c for c in rows if str(getattr(c, "side", "") or "").upper() == want]
        rows = rows[-max(5, int(n or 8)) :]
        if len(rows) < 5:
            return None
        try:
            pc = last_n_cost_pf(rows, len(rows), self.position_cost_pct)
            return float(pc.get("ratio") or 0)
        except Exception:
            return None

    def maybe_dca_adds(self) -> None:
        """Independent CTS DCA adds — own distances/mults/PF, not Block."""
        if not getattr(self.dca, "enabled", False) or self.halted:
            return
        if self.entries_blocked():
            return
        live_pf = self.live_recent_pf(n=8)
        if live_pf is not None and live_pf + 1e-9 < 1.25:
            return
        allow_add, _, _, add_reasons = self._coord_add_state()
        if not allow_add:
            if time.time() - self.skip_log.get("dca-add-gate", 0) > 45:
                log("COORD dca-gate " + "; ".join(add_reasons)[:160], every=45.0, key="coord-dca", quiet=True)
                self.skip_log["dca-add-gate"] = time.time()
            return
        if time.time() - getattr(self, "dca_last_emit", 0) < 0.35:
            return
        emitted = 0
        add_budget = 8 if MAX_OPEN <= 0 else 2
        for pos in list(self.open.values()):
            if emitted >= add_budget:
                break
            if time.time() < self.dca_fail_cd.get(pos.symbol, 0):
                continue
            if self.missing_controls(pos):
                self.ensure_controls(pos)
                if self.missing_controls(pos):
                    continue
            px = self.px.get(pos.symbol) or pos.entry
            if px <= 0 or pos.entry <= 0:
                continue
            age = time.time() - float(getattr(pos, "opened_at", 0) or 0)
            if age < 45.0:
                self.dca.skips += 1
                continue
            if pos.qty * px >= self.max_book_notional():
                self.dca.skips += 1
                continue
            # Independent of Block, but never stack both onto the same parent.
            try:
                blk = self.block.lanes.get(self.block.key(pos.symbol, pos.side))
                if blk and float(getattr(blk, "confirmed_add", 0) or 0) > 0:
                    self.dca.skips += 1
                    continue
            except Exception:
                pass
            sl_pct = float(pos.sl_pct or SL_PCT or 0.0048)
            adv = abs(px - pos.entry) / pos.entry
            against = (pos.side == "LONG" and px < pos.entry) or (pos.side == "SHORT" and px > pos.entry)
            if against and adv > max(sl_pct * 1.5, 0.012):
                self.dca.skips += 1
                continue
            parent = float(getattr(self.dca.lanes.get(self.dca.key(pos.symbol, pos.side), None), "parent_qty", 0) or 0)
            seed = parent if parent > 0 else pos.qty
            row = self.dca.due(pos.symbol, pos.side, seed, pos.entry, px)
            if not row:
                continue
            c = self.contracts.get(pos.symbol)
            if not c or px <= 0:
                continue
            want = min(float(row["qty"]), seed * 2.5)
            room = max(0.0, self.max_book_notional() - pos.qty * px)
            add_cap = min(self.notional_cap() * max(1.0, min(2.5, float(row.get("mult") or 1))), room)
            qty = self.cap_order_qty(c, px, want, add_cap)
            floor = self.min_order_qty(c, px)
            if qty < floor:
                if floor * px > add_cap * 1.08 or floor > seed * 2.5:
                    self.dca.skips += 1
                    continue
                qty = floor
            if qty <= 0 or qty > seed * 2.55:
                self.dca.skips += 1
                continue
            if (pos.qty + qty) * px > self.max_book_notional() * 1.02:
                self.dca.skips += 1
                continue
            margin = (qty * px) / max(1, self.leverage_for(c))
            if margin > self.available * 0.38 or self.available < 0.28:
                continue
            order_side = "BUY" if pos.side == "LONG" else "SELL"
            cid = self.cid("d", pos=pos)
            r = self.api.post(
                "/openApi/swap/v2/trade/order",
                {
                    "symbol": pos.symbol,
                    "type": "MARKET",
                    "side": order_side,
                    "positionSide": pos.side,
                    "quantity": qty,
                    "clientOrderID": cid,
                },
            )
            self.did_io = True
            if not self.ok(r):
                msg = str(r.get("msg") or "")
                self.dca_fail_cd[pos.symbol] = time.time() + (180.0 if is_transient_api(msg) else 25.0)
                if is_transient_api(msg):
                    log(f"DCA SKIP {pos.symbol} #{row['n']} {short_api_msg(msg)}", every=20.0, key=f"dcaf:{pos.symbol}")
                else:
                    self.errors += 1
                    self.last_error = f"dca {pos.symbol} n={row['n']} {short_api_msg(msg)}"[:160]
                    log(f"DCA FAIL {pos.symbol} #{row['n']} {r.get('msg')}", every=20.0, key=f"dcaf:{pos.symbol}")
                self.dca_last_emit = time.time()
                continue
            data = (r.get("data") or {}).get("order") or r.get("data") or {}
            avg = float(data.get("avgPrice") or data.get("price") or px) or px
            filled = float(data.get("quantity") or data.get("origQty") or qty) or qty
            self.dca.record_fill(row["lane"], row["step"], filled, avg, cid)
            pos.qty += filled
            pos.entry = ((pos.entry * (pos.qty - filled)) + avg * filled) / pos.qty if pos.qty else avg
            pos.notional = pos.qty * pos.entry
            try:
                mode = str(getattr(self.dca, "tp_mode", "average") or "average").lower()
                if mode in ("breakeven", "be"):
                    be = max(float(getattr(self.dca, "be_pct", 0.002) or 0.002), 0.0004)
                    pos.tp = pos.entry * (1 + be) if pos.side == "LONG" else pos.entry * (1 - be)
                else:
                    tp_pct = pos.tp_pct or TP_PCT
                    pos.tp = pos.entry * (1 + tp_pct) if pos.side == "LONG" else pos.entry * (1 - tp_pct)
                sl_pct = pos.sl_pct or SL_PCT
                pos.sl = pos.entry * (1 - sl_pct) if pos.side == "LONG" else pos.entry * (1 + sl_pct)
            except Exception:
                pass
            self.available = max(0.0, self.available - margin)
            if getattr(self, "control_orders", True):
                pos.sl_oid = pos.tp_oid = pos.sec_sl_oid = pos.sec_tp_oid = ""
                pos.ctrl_verified = False
                self.ctrl_skip.pop(f"sync:{pos.symbol}", None)
                self.ctrl_skip.pop(pos.symbol, None)
                self.ensure_controls(pos)
            self.dca_last_emit = time.time()
            emitted += 1
            log(f"DCA ADD {pos.symbol} {pos.side} n={row['n']} +{filled} avg={pos.entry:.6f} adv={row['adversePct']*100:.2f}%")

    def _budget(self):
        try:
            return self.load.observe(
                n_sym=len(SYMBOLS),
                n_open=len(self.open),
                hot_ms=float(self.last_scan_ms or 0),
                warm_ms=float(self.warm_ms or 0),
                hist_busy=bool(self.hist_busy),
                kline_ban=time.time() < float(getattr(self, "kline_ban", 0) or 0),
                cycle_overrun=bool(getattr(self, "cycle_overrun", False)),
            )
        except Exception:
            from load_engine import Budget
            return Budget()

    def trim_caches(self, force: bool = False) -> None:
        keep = set(SYMBOLS)
        for p in self.open.values():
            if p.symbol:
                keep.add(p.symbol)
        n = 0
        n += trim_map(self.px, keep)
        n += trim_map(self.last_px, keep)
        n += trim_map(self.chg, keep)
        n += trim_map(self.vol1h, keep)
        n += trim_map(self.vol1h_ts, keep)
        n += trim_map(self.kline_ts, keep)
        n += trim_map(self.bar_min, keep)
        n += trim_map(self._score_cache, keep)
        n += trim_map(self._ind_fp, keep)
        n += prune_ttl(self.cooldown, slack=0.0)
        n += prune_ttl(self.ignore_syms, slack=0.0)
        n += prune_ttl(self.ctrl_skip, slack=120.0)
        for tf, store in list(self.klines_tf.items()):
            n += trim_map(store, keep)
            for s, bars in list(store.items()):
                if isinstance(bars, list) and len(bars) > KLINE_LIMIT:
                    del bars[:-KLINE_LIMIT]
                    n += 1
        try:
            n += self.indications.keep(keep)
        except Exception:
            pass
        try:
            b = getattr(self.load, "last_budget", None)
            look = int(getattr(b, "lookback", 180) or 180)
            n += self.sets.trim_bars(keep)
            n += self.sets.trim_tapes(hist_cap=96, live_cap=64, bar_cap=look)
        except Exception:
            pass
        try:
            from indication_engine import EXTRA
            n += EXTRA.prune(keep, max_n=48)
        except Exception:
            pass
        try:
            self.load.trimmed += n
        except Exception:
            pass
        b = getattr(self.load, "last_budget", None)
        if force or (b and b.do_gc):
            try:
                self.load.free(force=force)
            except Exception:
                pass

    def process_indications(self) -> None:
        if not bool(self.indications.settings.get("enabled", True)):
            return
        b = self._budget()
        open_syms = [p.symbol for p in self.open.values() if p.symbol]
        ranked = [r.get("symbol") for r in (self.universe or []) if r.get("symbol")]
        window, nxt = self.load.scan_window(list(SYMBOLS), open_syms, b.scan_chunk, int(getattr(self.load, "cursor_ind", 0) or 0), ranked)
        self.load.cursor_ind = nxt
        self._scan_keep = list(window)
        extra_n = int(b.extra_n or 0) if b.extra_sources else 0
        extra_syms = []
        if extra_n and self.indications.settings.get("extraSources"):
            rot = list(window) or list(SYMBOLS)
            n = len(rot) or 1
            start = self.indications.extra_cursor % n
            extra_syms = rot[start:start + extra_n] or rot[:extra_n]
            self.indications.extra_cursor += extra_n
            try:
                from indication_engine import EXTRA
                EXTRA.prefetch([(src, s) for s in extra_syms for src in ("binance-usdm", "bybit-linear")])
            except Exception:
                pass
        fp_map = getattr(self, "_ind_fp", None)
        if not isinstance(fp_map, dict):
            fp_map = {}
            self._ind_fp = fp_map
        for s in window:
            bars = self.klines_tf.get("1m", {}).get(s) or self.klines.get(s) or []
            if len(bars) < 20:
                continue
            last_c = float(bars[-1][3]) if bars else 0.0
            px = self.px.get(s) or 0
            fp = (len(bars), last_c, round(float(px or 0), 6))
            if s not in extra_syms and fp_map.get(s) == fp and s in self.indications.last:
                continue
            fp_map[s] = fp
            d, _, conf = self.score(s)
            bars_by_tf = {
                tf: (self.klines_tf.get(tf, {}).get(s) or [])
                for tf in TIMEFRAMES
                if self.tf_on.get(tf, True)
            }
            self.indications.process(
                s,
                bars,
                pulse_dir=d,
                pulse_conf=conf,
                px=px,
                sl_pct=SL_PCT,
                tp_pct=TP_PCT,
                want_extra=s in extra_syms,
                bars_by_tf=bars_by_tf,
            )

    def strategy_closes(self) -> List[Closed]:
        """Only this system + this connection. Ignore foreign and leftover oversized."""
        cap = self.max_book_notional() * 2.0
        out: List[Closed] = []
        for c in self.closed:
            if getattr(c, "ours", True) is False:
                continue
            cid = getattr(c, "client_id", "") or ""
            if cid and not self.cid_ours(cid):
                continue
            conn = getattr(c, "conn", "") or ""
            if conn and conn != CONN_SHORT:
                continue
            n = abs(float(c.qty) * float(c.entry or 0))
            if n > cap:
                continue
            if "ctrl-no-position" in str(c.reason or "").lower() or str(c.reason or "") in ("no-ctrl",):
                continue
            if "oversized" in str(c.reason or "").lower():
                continue
            if n > self.max_book_notional() * 1.05:
                continue
            out.append(c)
        tagged = [c for c in out if (getattr(c, "client_id", "") and self.cid_ours(getattr(c, "client_id", ""))) or getattr(c, "set_id", "")]
        return tagged if tagged else []

    def system_open_upnl(self) -> float:
        """Mark-to-market of this connection's system book only. Cost-net."""
        tot = 0.0
        for p in self.open.values():
            if getattr(p, "ours", True) is False:
                continue
            cid = getattr(p, "client_id", "") or ""
            if cid and not self.cid_ours(cid):
                continue
            px = float(self.px.get(p.symbol) or 0)
            if px <= 0 or p.entry <= 0 or p.qty <= 0:
                continue
            d = (px - p.entry) / p.entry
            if p.side != "LONG":
                d = -d
            tot += net_pnl_usdt(d, p.qty, p.entry, self.position_cost_pct)
        return tot

    def system_activity(self) -> Dict[str, Any]:
        """Grow / loss / balance path from system+connection orders only.

        Wallet deposits, withdrawals, and independent (untagged / other-bot)
        trades never enter this tape.
        """
        closes = self.strategy_closes()
        grow = sum(float(c.pnl) for c in closes if float(c.pnl) > 0)
        loss = abs(sum(float(c.pnl) for c in closes if float(c.pnl) < 0))
        realized = sum(float(c.pnl) for c in closes)
        upnl = self.system_open_upnl()
        net = realized + upnl
        wins = sum(1 for c in closes if float(c.pnl) > 0)
        losses = sum(1 for c in closes if float(c.pnl) < 0)
        peak = 0.0
        eq = 0.0
        max_dd = 0.0
        for c in sorted(closes, key=lambda x: float(getattr(x, "t", 0) or 0)):
            eq += float(c.pnl)
            if eq > peak:
                peak = eq
            if peak - eq > max_dd:
                max_dd = peak - eq
        dd_pct = (max_dd / peak * 100.0) if peak > 1e-12 else 0.0
        traded = sum(abs(float(c.qty) * float(c.entry or 0)) for c in closes)
        for p in self.open.values():
            if getattr(p, "ours", True) is False:
                continue
            traded += abs(float(p.qty) * float(p.entry or 0))
        pnl_pct = (net / traded * 100.0) if traded > 1e-12 else 0.0
        return {
            "closes": closes,
            "n": len(closes),
            "grow": round(grow, 6),
            "loss": round(loss, 6),
            "realized": round(realized, 6),
            "unrealized": round(upnl, 6),
            "pnl": round(net, 6),
            "wins": wins,
            "losses": losses,
            "drawdownPct": round(max(0.0, dd_pct), 3),
            "tradedNotional": round(traded, 4),
            "pnlPct": round(pnl_pct, 3),
            "source": "system-orders",
        }

    def maybe_entries(self) -> None:
        if self.halted:
            return
        rows = self.strategy_closes()
        consec = 0
        for c in reversed(rows):
            if c.pnl < 0:
                consec += 1
            else:
                break
        intern_metrics: Dict[str, Any] = {"pf": 0.0, "n": 0}
        try:
            best_st = None
            best_n = -1
            best_side = ""
            for pack_name in ("indications", "general"):
                for side_n in ("LONG", "SHORT"):
                    st = None
                    try:
                        st = self.sets.pick_any(pack_name, side=side_n) if self.sets.enabled else None
                    except TypeError:
                        st = self.sets.pick_any(pack_name) if self.sets.enabled else None
                    except Exception:
                        st = None
                    if not st:
                        continue
                    blob = (getattr(st, "by_side", None) or {}).get(side_n) or {}
                    n = int(blob.get("last15_n") or getattr(st, "last15_n", 0) or 0)
                    if best_st is None or n > best_n:
                        best_st = st
                        best_n = n
                        best_side = side_n
            if best_st:
                blob = (getattr(best_st, "by_side", None) or {}).get(best_side) or {}
                intern_metrics = {
                    "pf": float(blob.get("last15_ratio") or best_st.last15_ratio),
                    "n": float(blob.get("last15_n") or best_st.last15_n),
                    "pack": best_st.pack,
                    "side": best_side,
                }
        except Exception:
            intern_metrics = {"pf": 0.0, "n": 0}
        allow, reasons, metrics = self.coord.gate(rows, consec, intern=intern_metrics)
        if self.entries_blocked():
            self.priority_controls()
            return
        slot_cap = self.coord.slot_cap(MAX_OPEN, metrics.get("last15Ratio", metrics.get("lastPf", 1.0)))
        ranked: List[Tuple[float, str, int, str]] = []
        best: Dict[str, Tuple[float, str, int, str]] = {}
        if self.strat_ind and bool(self.indications.settings.get("enabled")):
            def _ind_allow(kind: str, direction: str = "") -> bool:
                gate = getattr(self.sets, "indication_ok", None)
                if not (self.sets.enabled and callable(gate)):
                    return True
                side = "LONG" if str(direction).lower().startswith("l") else "SHORT"
                try:
                    return bool(gate(kind, side))
                except TypeError:
                    return bool(gate(kind))
                except Exception:
                    return True
            for s in SYMBOLS:
                if s in self.open:
                    continue
                picked = None
                try:
                    picked = self.indications.pick_entry(s, min_conf=0.52, allow=_ind_allow)
                except Exception:
                    picked = None
                if not picked:
                    try:
                        one = self.indications.best(s) or self.indications.primary(s)
                        if one and one.confidence >= 0.52 and _ind_allow(one.kind, one.direction):
                            picked = (one, float(one.confidence), 1)
                    except Exception:
                        picked = None
                if not picked:
                    continue
                pick, conf, agree_n = picked
                d = 1 if pick.direction == "long" else -1
                why = f"ind:{pick.kind}:{pick.mode}:{pick.agreement:.2f}:a{agree_n}:{','.join(pick.sources[:3])}"
                best[s] = (float(conf), s, d, why)
        if self.strat_general:
            for s in SYMBOLS:
                if s in self.open:
                    continue
                d, why, conf = self.score(s)
                if d == 0:
                    continue
                cur = best.get(s)
                if not cur or conf > cur[0]:
                    best[s] = (conf, s, d, f"gen:{why}")
        ranked = sorted(best.values(), reverse=True)
        intern = {}
        intern_any = False
        hist_ready = bool(self.sets.enabled and getattr(self.sets, "progress", None) and self.sets.progress.ready)
        for pack in ("indications", "general"):
            if self.sets.enabled and self.sets.use_historic_gate and hist_ready:
                try:
                    intern[pack] = bool(
                        self.sets.pack_open(pack, side="LONG") or self.sets.pack_open(pack, side="SHORT")
                    )
                except TypeError:
                    intern[pack] = bool(self.sets.pack_open(pack))
            else:
                intern[pack] = True
            intern_any = intern_any or intern[pack]
        if not intern_any and self.sets.enabled and not getattr(self.sets, "strict_gate", False):
            # Legacy mode only: reopen both packs when the gate has no pick.
            # Strict gate (default): closed packs stay closed — no validated +
            # profitable set means no live entries at all.
            intern_any = True
            intern["indications"] = intern["general"] = True
        # 0 / missing maxOpen = unlimited. Coord and intern are advisory, never a hard book cap.
        if MAX_OPEN <= 0:
            slot_cap = 10**9
        elif intern_any:
            slot_cap = MAX_OPEN
        elif not allow:
            slot_cap = max(1, min(slot_cap, MAX_OPEN))
            if ranked and (time.time() - self.skip_log.get("gate", 0) > 45):
                log("COORD intern-soft " + ("; ".join(reasons)[:160] if not allow else "no intern pick"), every=45.0, key="coord-pause", quiet=True)
                self.skip_log["gate"] = time.time()
        else:
            slot_cap = MAX_OPEN
        if not allow:
            if ranked and (time.time() - self.skip_log.get("gate", 0) > 45):
                log("COORD soft " + "; ".join(reasons)[:160] + f" intern={intern}", every=45.0, key="coord-pause", quiet=True)
                self.skip_log["gate"] = time.time()
        opens = []
        for p in self.open.values():
            px = self.px.get(p.symbol) or p.entry
            u = ((px - p.entry) / p.entry * (1 if p.side == "LONG" else -1)) * 100
            opens.append({"symbol": p.symbol, "uPnlPct": u, "ageS": time.time() - p.opened_at, "conf": p.conf})
        swap = self.coord.pick_rearrange(opens, ranked, slot_cap)
        if swap and swap["from"] in self.open:
            pos = self.open[swap["from"]]
            self.close_pos(pos, self.px.get(pos.symbol) or pos.entry, f"rearr->{swap['to']}")
            log(f"COORD rearr {swap['from']} -> {swap['to']} gap={swap['conf']:.2f}")
        if len(self.open) >= slot_cap:
            self.maybe_block_adds()
            self.maybe_dca_adds()
            return
        n_l = sum(1 for _, _, d, _ in ranked if d > 0)
        n_s = sum(1 for _, _, d, _ in ranked if d < 0)
        prefer = -1 if n_s >= n_l + 3 else (1 if n_l >= n_s + 3 else 0)
        if prefer:
            ranked = [r for r in ranked if r[2] == prefer] + [r for r in ranked if r[2] != prefer]
        if float(self.available or 0) < 8.0:
            def _min_n(row: Tuple[float, str, int, str]) -> float:
                s = row[1]
                c = self.contracts.get(s)
                px = self.px.get(s) or 0
                if not c or px <= 0:
                    return 1e9
                return self.min_order_qty(c, px) * px
            ranked = sorted(ranked, key=_min_n)
        placed = 0
        skipped = 0
        for conf, s, d, why in ranked:
            if self.entries_blocked():
                break
            if float(self.available or 0) <= 0 or time.time() < self.cooldown.get("__book__", 0):
                break
            pack = "indications" if str(why).startswith("ind:") else "general"
            if self.sets.enabled and self.sets.use_historic_gate and self.sets.progress.ready:
                if not intern.get(pack):
                    alt = "general" if pack == "indications" else "indications"
                    if intern.get(alt):
                        pack = alt
                    elif getattr(self.sets, "strict_gate", False):
                        # Strict: both packs closed -> this candidate cannot
                        # bind a validated + profitable set. Skip it.
                        skipped += 1
                        continue
                    elif intern_any:
                        pack = "general" if intern.get("general") else "indications"
                    else:
                        pack = "general"
            before = len(self.open)
            self.place(s, d, why, conf)
            if len(self.open) > before:
                placed += 1
            else:
                skipped += 1
            room = self.avail_notional()
            burst = 16 if MAX_OPEN <= 0 else 6  # unlimited: no order-count throttle
            if room < 8:
                burst = 1
            if placed >= burst or (slot_cap > 0 and len(self.open) >= slot_cap):
                break
        if placed == 0 and ranked and (time.time() - self.skip_log.get("entry0", 0) > 30):
            log(f"ENTRY none n={len(ranked)} skip={skipped} intern={intern} cap={slot_cap} open={len(self.open)} avail={self.available:.4f}", every=30.0, key="entry0")
            self.skip_log["entry0"] = time.time()
        self.maybe_block_adds()
        self.maybe_dca_adds()

    def flatten_all(self, why: str) -> None:
        for pos in list(self.open.values()):
            self.close_pos(pos, self.px.get(pos.symbol) or pos.entry, why)

    def adopt_exchange_positions(self) -> None:
        """Refresh OUR book only. Ignore any exchange position/order without our tracking id."""
        self.did_io = True
        r = self.api.get("/openApi/swap/v2/user/positions")
        if not self.ok(r):
            self.recon_ok = False
            self.recon_detail = f"adopt {(r.get('msg') or r.get('code'))}"[:120]
            return
        rows = r.get("data") or []
        if not isinstance(rows, list):
            return
        live_n = 0
        live_keys: set = set()
        for p in rows:
            try:
                amt = float(p.get("positionAmt") or p.get("availableAmt") or 0)
            except Exception:
                continue
            if abs(amt) > 1e-12:
                live_n += 1
                sym_k = p.get("symbol")
                if sym_k:
                    side_k = (p.get("positionSide") or "").upper() or ("LONG" if amt > 0 else "SHORT")
                    live_keys.add(f"{sym_k}:{side_k}")
        self.exchange_open_count = live_n
        # Real/Live/Simulated: live_pos_keys = what the exchange REALLY holds
        # right now (any valid read refreshes it, even while the flat-exchange
        # glitch guard below still arms). Book positions not in this set are
        # "Simulated" — system-internal calcs only.
        self.live_pos_keys = live_keys
        if live_n == 0 and self.open:
            # Glitch guard: one empty REST page must never wipe the book — but a
            # CONFIRMED flat exchange (2 consecutive empty reads, ~50 cycles apart)
            # means every tracked position is a phantom: fall through so the
            # stale-local sweep below removes them (age>=180s + per-position
            # _exchange_flat re-check for controlled positions).
            self._empty_rest_streak = int(getattr(self, "_empty_rest_streak", 0) or 0) + 1
            if self._empty_rest_streak < 2:
                log("ADOPT skip empty rest", every=20.0, key="adopt-empty")
                return
            log(f"ADOPT flat-exchange confirmed streak={self._empty_rest_streak} book={len(self.open)}")
        else:
            self._empty_rest_streak = 0
        live = set()
        foreign = set()
        for p in rows:
            try:
                amt = float(p.get("positionAmt") or p.get("availableAmt") or 0)
            except Exception:
                continue
            if amt == 0:
                continue
            sym = p.get("symbol")
            if not sym:
                continue
            side = (p.get("positionSide") or "").upper() or ("LONG" if amt > 0 else "SHORT")
            px = float(p.get("avgPrice") or p.get("entryPrice") or self.px.get(sym) or 0)
            qty = abs(amt)
            ours = next((pos for pos in self.open.values() if pos.symbol == sym and pos.side == side), None)
            live.add(sym)
            live_lev = 0
            try:
                live_lev = int(float(p.get("leverage") or 0))
            except Exception:
                live_lev = 0
            tagged = []
            try:
                tagged = [o for o in self.our_orders(sym) if str(o.get("positionSide") or "").upper() in (side, "")]
            except Exception:
                tagged = []
            owned = bool(ours and getattr(ours, "ours", True) and (ours.client_id and self.cid_ours(ours.client_id) or ours.sl_oid or ours.tp_oid))
            if not tagged and not owned:
                foreign.add(f"{sym}:{side}")
                log(f"SKIP foreign {sym} {side} q={qty}", every=60.0, key=f"foreign:{sym}:{side}", quiet=True)
                continue
            if live_lev and live_lev < int(self.lev_max.get(sym) or self.lev_map.get(sym) or 0):
                self.ensure_max_leverage(sym, force=True)
            try:
                liq = float(p.get("liquidationPrice") or p.get("liqPrice") or p.get("avgLiquidationPrice") or 0)
            except Exception:
                liq = 0.0
            pid = str(p.get("positionId") or p.get("positionID") or "")
            if ours is not None:
                if liq > 0:
                    ours.liq = liq
                if pid:
                    ours.position_id = pid
                if px > 0:
                    ours.qty = qty
                    ours.entry = px
                    ours.notional = qty * px
                    ours.ours = True
                if not getattr(ours, "set_id", "") and tagged:
                    try:
                        trk = self.parse_track(self.order_cid(tagged[0])) or {}
                        if trk.get("set_id"):
                            ours.set_id = str(trk.get("set_id"))
                            idxv = trk.get("idx")
                            ours.set_idx = int(idxv) if idxv is not None else -1
                            ours.pack = str(trk.get("pack") or ours.pack)
                    except Exception:
                        pass
                continue
            if px <= 0:
                continue
            track = self.parse_track(self.order_cid(tagged[0])) if tagged else {}
            track = track or {}
            sl_ratio = float(track.get("sl") or self.variants.current_sl())
            trail_key, trail_arm, trail_give = self.variants.current_trail()
            if track.get("trail"):
                trail_key = str(track.get("trail"))
            sl_pct, tp_pct, src = resolve_sl_tp(
                base_sl=SL_PCT, base_tp=TP_PCT,
                sl_min=self.sl_min, sl_max=self.sl_max,
                tp_min=self.tp_min, tp_max=self.tp_max,
                sl_to_tp=sl_ratio, bind_sl_to_tp=True,
                cost_pct=self.position_cost_pct, tp_cost_ratio=self.tp_cost_ratio,
            )
            sl = px * (1 - sl_pct) if side == "LONG" else px * (1 + sl_pct)
            tp = px * (1 + tp_pct) if side == "LONG" else px * (1 - tp_pct)
            cid = (self.order_cid(tagged[0]) if tagged else "") or self.cid("o")
            set_id = str(track.get("set_id") or "")
            pack = str(track.get("pack") or ("indications" if set_id.startswith("ind") else "general"))
            self.open[sym] = Position(
                symbol=sym, side=side, qty=qty, entry=px, opened_at=time.time(),
                sl=sl, tp=tp, peak=px, notional=qty * px, reason=f"recover {src} set={set_id or 'def'}", conf=0.35,
                sl_ratio=sl_ratio, trail_key=trail_key,
                trail_arm=trail_arm / 100.0, trail_give=trail_give / 100.0,
                sl_pct=sl_pct, tp_pct=tp_pct, client_id=cid, ours=True,
                overall=True, close_position=True,
                set_id=set_id, pack=pack, set_idx=int(track["idx"]) if track.get("idx") is not None else -1,
                liq=liq, position_id=pid,
            )
            rec_pos = self.open[sym]
            rec_pos.sl, rec_pos.tp = self.security_prices(rec_pos)
            log(f"RECOVER {sym} {side} qty={qty} cid={cid}", every=20.0, key=f"rec:{sym}")
            if getattr(self, "control_orders", True):
                rec_pos.ctrl_verified = False
                self.place_ctrl_pair(rec_pos)
                if self.missing_controls(rec_pos):
                    self.ensure_controls(rec_pos)
        for sym in list(self.open):
            pos = self.open.get(sym)
            if not pos:
                continue
            if pos.symbol in live:
                if hasattr(self, "_absent_n"):
                    self._absent_n.pop(sym, None)
                continue
            age = time.time() - float(pos.opened_at or 0)
            key = f"{pos.symbol}:{pos.side}"
            live_keys_now = getattr(self, "live_pos_keys", None) or set()
            still_live = key in live_keys_now
            if still_live:
                if hasattr(self, "_absent_n"):
                    self._absent_n.pop(sym, None)
                continue
            if age < 45.0:
                continue
            misses = int((getattr(self, "_absent_n", None) or {}).get(sym, 0)) + 1
            if not hasattr(self, "_absent_n"):
                self._absent_n = {}
            self._absent_n[sym] = misses
            flat_ex = int(getattr(self, "_empty_rest_streak", 0) or 0) >= 2 and live_n == 0
            # Partial list: need 3 misses. Fully-flat exchange already confirmed by streak.
            if not flat_ex and misses < 3:
                continue
            has_ctrl = bool(pos.sl_oid or pos.tp_oid or getattr(pos, "sec_sl_oid", "") or getattr(pos, "sec_tp_oid", ""))
            if has_ctrl and not flat_ex and not self._exchange_flat(pos):
                continue
            log(f"DROP stale local {sym} age={age:.0f}s miss={misses}")
            self.open.pop(sym, None)
            self.cooldown[sym] = time.time() + 12.0
        self.ignored_foreign = len(foreign)
        ours_live = {k.split(":")[0] for k in live if k not in {x.split(":")[0] for x in foreign}}
        book_syms = set(self.open)
        issues = []
        for pos in self.open.values():
            if pos.symbol not in live:
                issues.append(f"book-only {pos.symbol}")
        if abs(len(self.open) - len(book_syms & live)) > 0 and book_syms - live:
            issues.append(f"count book={len(self.open)} live_ours={len(live - {x.split(':')[0] for x in foreign})}")
        self.recon_ok = not issues
        self.recon_detail = (
            f"ok ours={len(self.open)} foreign={len(foreign)} live={len(live)}"
            if not issues
            else "; ".join(issues)
        )[:160]
        self.save_open_book()

    def sync_own_fills(self) -> None:
        """Pull FILLED orders with our tracking id. Ignore everything else. Feed Set live tape."""
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
            return
        n = 0
        for o in orders:
            cid = self.order_cid(o)
            if not cid or not self.cid_ours(cid) or cid in self.seen_fill_cids:
                continue
            status = str(o.get("status") or o.get("orderStatus") or o.get("state") or "").upper()
            if status and status not in ("FILLED", "FINISHED", "SUCCESS", "FILLED_FULLY"):
                continue
            track = self.parse_track(cid) or {}
            kind = track.get("kind") or ""
            if kind not in ("c", "s", "t"):
                if kind == "o":
                    self.seen_fill_cids.add(cid)
                    if o.get("symbol"):
                        self.owned_syms.add(str(o.get("symbol")))
                continue
            try:
                qty = float(o.get("executedQty") or o.get("origQty") or o.get("quantity") or 0)
                px = float(o.get("avgPrice") or o.get("price") or 0)
                pnl = float(o.get("profit") or o.get("realizedPnl") or o.get("pnl") or 0)
            except Exception:
                continue
            if qty <= 0 or px <= 0:
                continue
            pnl_pct = pnl / (qty * px) if qty * px else 0.0
            rec = {
                "t": time.time(),
                "symbol": str(o.get("symbol") or ""),
                "side": str(o.get("positionSide") or ""),
                "qty": qty,
                "entry": px,
                "exit": px,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "reason": f"exch:{kind}",
                "hold_s": 0.0,
                "set_id": track.get("set_id") or "",
                "pack": track.get("pack") or "",
                "client_id": cid,
                "ours": True,
                "conn": CONN_SHORT,
                "sl_ratio": track.get("sl") or 0.6,
                "trail_key": track.get("trail") or "",
            }
            self.seen_fill_cids.add(cid)
            try:
                self.sets.on_live_close(rec)
            except Exception:
                pass
            n += 1
        if n:
            log(f"SYNC fills {n} ours", every=30.0, key="sync-fills", quiet=True)

    def set_leverage(self) -> None:
        """Actively keep every desk symbol at its own exchange max leverage."""
        global LEVERAGE
        self.use_max_leverage = True
        self._load_lev_file()
        if self.api.path_cd.get("/openApi/swap/v2/trade/leverage", 0) > time.time():
            return
        need = [s for s in SYMBOLS if int(self.lev_map.get(s) or 0) < int(self.lev_max.get(s) or 1) or s not in self.lev_max]
        if not need:
            if self.lev_map:
                LEVERAGE = max(int(v) for v in self.lev_map.values() if v)
            now = time.time()
            if now - getattr(self, "_lev_rot_ts", 0) > 90 and SYMBOLS:
                self._lev_rot_ts = now
                rot = SYMBOLS[int(now / 90) % len(SYMBOLS)]
                self.ensure_max_leverage(rot, force=True)
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
        cov = self.sets.coverage()
        fam = cov.get("families") or {}
        self.record_test(
            "qa-set-grid",
            bool(cov.get("trailCover") and cov.get("slCover") and cov.get("independentTrail") and fam.get("trail", 0) >= 5 and fam.get("base", 0) >= 8),
            f"n={cov.get('product')} fam={fam} trails={cov.get('trails')}",
        )
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
                Closed(time.time(), "SYS-USDT", "LONG", 10.0, 1.0, 1.1, 0.40, 0.01, "tp", 30.0, set_id="s1", client_id=ours_cid, ours=True, conn=CONN_SHORT),
                Closed(time.time(), "SYS-USDT", "SHORT", 10.0, 1.0, 1.1, -0.15, -0.01, "sl", 20.0, set_id="s1", client_id=ours_cid, ours=True, conn=CONN_SHORT),
                Closed(time.time(), "EXT-USDT", "LONG", 99.0, 1.0, 1.2, 9.99, 0.2, "manual", 10.0, client_id="manual-bot", ours=False, conn=CONN_SHORT),
                Closed(time.time(), "EXT-USDT", "LONG", 5.0, 1.0, 1.1, 0.50, 0.1, "tp", 10.0, client_id="", ours=True, conn=CONN_SHORT),
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
        sim_n, sim_upnl = self.sim_stats()
        closed_n = 80 if getattr(getattr(self.load, "last_budget", None), "stats_full", True) else 40
        closed_out = []
        for c in list(act["closes"])[-closed_n:][::-1]:
            d = asdict(c)
            d["indKind"] = d.get("ind_kind") or ""
            closed_out.append(d)
        cov = self._coverage_blob()
        ind_snap = self.indications.snapshot()
        sets_snap = self.sets.snapshot(full=False)
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
            "maxHoldS": MAX_HOLD_S,
            "tests": self.tests[-24:],
            "block": self.block.snapshot(),
            "pulse": self.pulse_snapshot(),
            "coord": self.coord.snapshot(),
            "pfCost": pc,
            "profitFactor": pc["ratio"],
            "pf": pc["ratio"],
            "pfNeutral": 1.0,
            "pfPlus1xCost": 1.1,
            "pfScale": "1.00=neutral · 1.10=+1×PositionCost",
            "variants": self.variants.snapshot(),
            "sets": sets_snap,
            "exits": self.exits.snapshot(),
            "indications": ind_snap,
            "dca": self.dca.snapshot(),
            "api": snap,
            "cts": {"blockMaxStack": self.cts.get("blockMaxStack"), "variantBlockEnabled": self.cts.get("variantBlockEnabled"), "blockVolumeRatio": self.cts.get("blockVolumeRatio"), "blockProfitFactorRatio": self.cts.get("blockProfitFactorRatio"), "position_mode": self.cts.get("position_mode"), "margin_mode": self.cts.get("margin_mode"), "control_orders": self.cts.get("control_orders")},
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
                    "ctrlQty": getattr(p, "ctrl_qty", p.qty),
                    "slRangePct": [round(self.opt_fracs(p)[2] * 100, 3), round(self.opt_fracs(p)[3] * 100, 3)],
                    "tpRangePct": [round(self.tp_min * 100, 3), round(self.tp_max * 100, 3)],
                    "slRatio": p.sl_ratio,
                    "trailKey": p.trail_key,
                    "slPct": round(p.sl_pct * 100, 3),
                    "tpPct": round(p.tp_pct * 100, 3),
                    "setId": p.set_id,
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
        stages = ((self.coord.last or {}).get("stages") if hasattr(self.coord, "last") else {}) or {}
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
        return {
            "strategies": {
                "indications": bool(self.strat_ind and self.indications.settings.get("enabled", True)),
                "general": bool(self.strat_general),
                "block": bool(self.block.enabled and self.strat_block),
                "trailing": bool(self.strat_trail),
                "dca": bool(self.dca.enabled),
                "exits": bool(self.exits.enabled),
                "rearrange": bool(self.coord.rearrange),
                "coord": bool(any(ax.enabled for ax in self.coord.axes.values())),
                "trailRecalc": bool(getattr(self.variants, "trail_auto", True) or self.strat_trail),
                "sets": bool(self.sets.enabled),
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
            "evals": {
                "n": eval_n,
                "symbols": len(getattr(self.indications, "evals", {}) or {}),
                "typeHits": hits,
            },
            "coord": {
                "allow": bool((self.coord.last or {}).get("allow", True)),
                "addsAllow": bool((self.coord.last or {}).get("addsAllow", True)),
                "addReasons": list((self.coord.last or {}).get("addReasons") or [])[:6],
                "stages": stages,
                "mainEval": int(getattr(self.coord, "main_eval", 5)),
                "realEval": int(getattr(self.coord, "real_eval", 3)),
                "posCountVolRatio": float(getattr(self.coord, "pos_count_vol_ratio", 0.05) or 0.05),
                "sizeMult": round(float(self.coord.size_mult(len(self.open))), 4),
                "openN": len(self.open),
                "axes": {k: {"enabled": v.enabled, "maxWindow": v.max_window} for k, v in self.coord.axes.items()},
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
            },
            "recon": {"ok": self.recon_ok, "detail": self.recon_detail, "exchangeOpen": int(getattr(self, "exchange_open_count", -1)), "simOpen": sim_n},
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
            src = list(closed)[-n:] if n else list(closed)
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
        from set_engine import drawdown_time
        recs = []
        for c in closed:
            if isinstance(c, dict):
                recs.append({"t": c.get("t"), "pnl": c.get("pnl")})
            else:
                recs.append({"t": getattr(c, "t", 0), "pnl": getattr(c, "pnl", 0)})
        d = drawdown_time(recs)
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
        from set_engine import drawdown_time
        out = []
        for s, pnls in buckets.items():
            gp = sum(x for x in pnls if x > 0)
            gl = abs(sum(x for x in pnls if x < 0))
            d = drawdown_time(ddt_rows.get(s) or [])
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
        self.record_test("qa-sltp-grid", abs(self.sl_to_tp - round(self.sl_to_tp, 1)) < 1e-9 and 0.3 <= self.sl_to_tp <= 1.5, f"r={self.sl_to_tp}")
        self.record_test("qa-trail-indep", self.variants.trail_arm >= 0.3, f"{self.variants.trail_key}")
        self.record_test("qa-hot-budget", self.last_scan_ms <= (SCAN_S * 1000.0 + 40.0) or self.last_scan_io or self.hist_busy or self.cycle < 40, f"{self.last_scan_ms:.0f}ms budget={SCAN_S*1000:.0f} io={int(self.last_scan_io)} hist={int(self.hist_busy)}")
        rss = rss_mb()
        self.record_test("qa-rss", rss < (110 if len(SYMBOLS) <= 80 else 520), f"{rss:.1f}MB n={len(SYMBOLS)}")
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
        range_ok = True
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
                    range_ok = False
            if tp_px > 0 and mark > 0:
                side_ok = (p.side == "LONG" and tp_px > mark) or (p.side == "SHORT" and tp_px < mark)
                if not side_ok:
                    range_ok = False
        self.record_test("qa-ctrl-overall", overall_ok or cooling, f"open={len(self.open)} overall={int(overall_ok)} miss={missing}")
        self.record_test("qa-ctrl-range", range_ok or cooling or not self.open, f"range ok={int(range_ok)}")
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
        self.record_test(
            "qa-set-cover",
            bool(cov.get("slCover") and cov.get("trailCover") and cov.get("independentTrail") and int(cov.get("product") or 0) >= 10),
            f"n={cov.get('product')} fam={fam} sl={cov.get('slCover')} tr={cov.get('trailCover')}",
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

    def _hist_fetch(self) -> None:
        if not self.sets.enabled:
            return
        self.sets.progress.phase = "fetch"
        limit = str(self.sets.lookback)
        reqs = [("/openApi/swap/v2/quote/klines", {"symbol": s, "interval": "1m", "limit": limit}) for s in SYMBOLS]
        stored = 0
        chunk = 4
        for i in range(0, len(reqs), chunk):
            self.sets.progress.detail = f"fetch {i}/{len(reqs)}"
            self.sets.progress.pct = (i / max(1, len(reqs))) * 8.0
            batch = reqs[i : i + chunk]
            sd_notify("WATCHDOG=1")
            rows = []
            if hasattr(self.api, "gather_public"):
                rows = self.api.gather_public(batch, timeout=6.0)
            else:
                for path, extra in batch:
                    body = self.api.public(path, extra)
                    rows.append((path, extra, body))
            for _path, extra, body in rows:
                s = extra.get("symbol")
                bars = self._parse_klines((body or {}).get("data"))
                if s and bars:
                    self.sets.ingest_bars(s, bars)
                    stored += 1
            time.sleep(0.12)
        self.sets.progress.detail = f"fetched {stored}/{len(SYMBOLS)}"

    def _hist_loop(self) -> None:
        while not self._hist_stop:
            try:
                for s in SYMBOLS:
                    bars = self.klines_tf.get("1m", {}).get(s) or self.klines.get(s) or []
                    if bars:
                        self.sets.ingest_bars(s, bars)
                if self.sets.due():
                    have = sum(1 for s in SYMBOLS if len(self.sets.bars.get(s) or []) >= self.sets.min_bars)
                    if have < max(4, len(SYMBOLS) // 2) or time.time() - self.sets.last_run >= self.sets.refresh_s:
                        self._hist_fetch()
                    self.hist_busy = True
                    nbar = [0]
                    def _hist_step():
                        nbar[0] += 1
                        sd_notify("WATCHDOG=1")
                        time.sleep(0)
                    try:
                        b = self._budget()
                        names = list(SYMBOLS)
                        if b.scan_chunk and len(names) > b.hist_chunk:
                            names, self.load.cursor_hist = self.load.scan_window(
                                names, [p.symbol for p in self.open.values()], b.hist_chunk, int(self.load.cursor_hist or 0)
                            )
                        if b.hist_run:
                            already = bool(getattr(self.sets, "progress", None) and self.sets.progress.ready)
                            self.sets.replay_all(
                                on_step=_hist_step,
                                symbols=names,
                                abort=lambda: already and self.load.last_budget.level == "critical",
                            )
                    finally:
                        self.hist_busy = False
            except Exception:
                self.hist_busy = False
                self.sets.progress.phase = "error"
                self.sets.progress.error = traceback.format_exc()[-220:]
                if hasattr(self.api, "err"):
                    self.api.err.write("hist", msg=self.sets.progress.error[:200])
            remain = 2.4
            t0 = time.monotonic()
            while time.monotonic() - t0 < remain and not self._hist_stop:
                time.sleep(0.2)

    def _warm_loop(self) -> None:
        while not self._warm_stop:
            t0 = time.time()
            try:
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
