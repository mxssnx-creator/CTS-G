#!/usr/bin/env python3
"""Bybit V5 linear (USDT perpetual) client with the interface of FastBingX.

The live trader (pulse_trader.py) speaks BingX: BingX paths, BingX symbols (BTC-USDT), BingX order bodies and BingX
response shapes. This client takes those calls, sends the equivalent Bybit V5 requests, and hands back responses in the
BingX shape the trader reads. Nothing in the trader's order logic changes: a venue is selected by the connection name
(PULSE_CONN=bybit-*), see pulse_trader.main.

Mapping (BingX call -> Bybit V5):
  user/balance (v2, v3)           -> GET  /v5/account/wallet-balance (UNIFIED, CONTRACT as a fallback)
  user/positions                  -> GET  /v5/position/list (settleCoin USDT, paged); LONG +size, SHORT -size
  quote/ticker                    -> GET  /v5/market/tickers (linear, USDT perpetuals only)
  quote/klines (v2, v3)           -> GET  /v5/market/kline (newest first on the wire, oldest first here)
  trade/order POST                -> POST /v5/order/create (hedge mode positionIdx 1 / 2)
        MARKET entry              -> Market order, then the fill (avg price, executed qty) from order history / executions
        MARKET close              -> Market, reduceOnly (closePosition: qty 0 + closeOnTrigger)
        STOP_MARKET, TAKE_PROFIT_MARKET (stopPrice) -> conditional Market order: triggerPrice, triggerDirection, triggerBy,
                                     reduceOnly + closeOnTrigger (closePosition: qty 0)
        STOP, TAKE_PROFIT         -> the same with a Limit order at price
  trade/order GET                 -> GET  /v5/order/realtime, then /v5/order/history
  trade/order DELETE              -> POST /v5/order/cancel
  trade/openOrders                -> GET  /v5/order/realtime (settleCoin USDT, paged, conditional orders included)
  trade/allOrders                 -> GET  /v5/order/history + /v5/position/closed-pnl (profit per closing order)
  trade/allFillOrders             -> GET  /v5/execution/list
  trade/batchOrders (batch_place) -> POST /v5/order/create-batch (per item result from retExtInfo)
  trade/order/cancelReplace       -> POST /v5/order/amend (triggerPrice of the stop)
  trade/leverage GET / POST       -> GET /v5/position/list + instruments-info / POST /v5/position/set-leverage (both sides)
  trade/marginType POST           -> once: account margin mode REGULAR (cross); never repeated
The position mode is hedge (switch-mode, mode 3): the trader's lanes hold a LONG and a SHORT of one symbol independently.

Bybit errors are returned with BingX-style words in `msg` where the trader reads them ("insufficient margin", "position not
exist", "order not exist", "already exists", "trigger price", "exceeded ... limit", "order size", "rate limit"), with the
original Bybit text and retCode kept after them. Rate-limit replies set the same path cooldowns FastBingX sets.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.parse
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Tuple

from bingx_fast import ErrorLog, dumps, loads
from bybit_limits import AUTH_WORDS, IP_BAN_S, WORDS, Limiter, Verdict, classify

try:
    import websocket as _ws
except Exception:
    _ws = None

BASE = "https://api.bybit.com"
BASE_DEMO = "https://api-demo.bybit.com"
BASE_TESTNET = "https://api-testnet.bybit.com"
WS_URL = "wss://stream.bybit.com/v5/public/linear"
RECV = 5000
UA = "grok-x01-pulse/2.0-bybit"
CATEGORY = "linear"
SETTLE = "USDT"
WS_ARGS_PER_CONN = 90          # Bybit limits a futures connection to 100 topics
WS_PING_S = 20.0

BATCH_MAX = 10                  # orders per batch call (the rate-limit page allows 10, the endpoint pages up to 20 for linear)
BREAKER_FAILS = 6               # consecutive transient failures that open the circuit breaker
BREAKER_S = 15.0                # how long it stays open for new entries (closing orders and cancels always go)
RETRY_PAUSE_S = 0.25
OK_ALREADY = {110043, 110025, 110026, 34040}      # leverage / position mode / margin mode already as asked

# the BingX paths the trader uses (also the keys of path_cd)
P_BAL3 = "/openApi/swap/v3/user/balance"
P_BAL2 = "/openApi/swap/v2/user/balance"
P_POS = "/openApi/swap/v2/user/positions"
P_TICK = "/openApi/swap/v2/quote/ticker"
P_KL2 = "/openApi/swap/v2/quote/klines"
P_KL3 = "/openApi/swap/v3/quote/klines"
P_CONTRACTS = "/openApi/swap/v2/quote/contracts"
P_ORDER = "/openApi/swap/v2/trade/order"
P_OPEN = "/openApi/swap/v2/trade/openOrders"
P_ALL = "/openApi/swap/v2/trade/allOrders"
P_FILLS = "/openApi/swap/v1/trade/allFillOrders"
P_BATCH = "/openApi/swap/v2/trade/batchOrders"
P_REPLACE = "/openApi/swap/v2/trade/order/cancelReplace"
P_LEV = "/openApi/swap/v2/trade/leverage"
P_MARGIN = "/openApi/swap/v2/trade/marginType"

INTERVALS = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "2h": "120", "4h": "240",
             "6h": "360", "12h": "720", "1d": "D", "1w": "W", "1M": "M"}
STATUS = {"New": "NEW", "Created": "NEW", "Untriggered": "NEW", "PartiallyFilled": "PARTIALLY_FILLED", "Filled": "FILLED",
          "Cancelled": "CANCELED", "PartiallyFilledCanceled": "CANCELED", "Deactivated": "CANCELED", "Rejected": "REJECTED",
          "Triggered": "TRIGGERED"}


# ---- symbols and numbers -----------------------------------------------------------------------------------------------

def to_bx(symbol: str) -> str:
    """BTCUSDT -> BTC-USDT (the trader's form); anything that is not a plain USDT perpetual name is returned unchanged."""
    s = str(symbol or "")
    if s.endswith(SETTLE) and len(s) > len(SETTLE) and "-" not in s:
        return s[: -len(SETTLE)] + "-" + SETTLE
    return s


def to_by(symbol: str) -> str:
    """BTC-USDT -> BTCUSDT."""
    return str(symbol or "").replace("-", "").upper()


def is_usdt_perp(symbol: str) -> bool:
    s = str(symbol or "")
    return s.endswith(SETTLE) and len(s) > len(SETTLE) and "-" not in s


def fnum(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def decimals(step: Any) -> int:
    """Digits after the point of a step such as "0.001" (3) or "5" (0)."""
    try:
        d = Decimal(str(step)).normalize()
    except (InvalidOperation, ValueError):
        return 0
    exp = d.as_tuple().exponent
    return max(0, -int(exp)) if isinstance(exp, int) else 0


def fmt_dec(d: Decimal) -> str:
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def on_step(value: Any, step: Any) -> str:
    """The value on the nearest multiple of step, as a plain decimal string (never scientific notation)."""
    try:
        v, st = Decimal(str(value)), Decimal(str(step))
    except (InvalidOperation, ValueError):
        return str(value)
    if st <= 0:
        return fmt_dec(v)
    return fmt_dec((v / st).to_integral_value(rounding=ROUND_HALF_UP) * st)


# ---- instruments (contracts) -------------------------------------------------------------------------------------------

def parse_instrument(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One instruments-info row as the trader's contract fields, or None for a symbol the trader does not trade."""
    sym = str(row.get("symbol") or "")
    if not is_usdt_perp(sym):
        return None
    if str(row.get("contractType") or "LinearPerpetual") != "LinearPerpetual":
        return None
    if str(row.get("status") or "Trading") != "Trading":
        return None
    lot = row.get("lotSizeFilter") or {}
    pf = row.get("priceFilter") or {}
    lev = row.get("leverageFilter") or {}
    step = str(lot.get("qtyStep") or "1")
    tick = str(pf.get("tickSize") or "0.0001")
    return {
        "symbol": to_bx(sym),
        "bybit": sym,
        "min_qty": fnum(lot.get("minOrderQty")),
        "step": fnum(step) or 1.0,
        "qty_step": step,
        "qprec": decimals(step),
        "pprec": decimals(tick),
        "tick": tick,
        "min_usdt": fnum(lot.get("minNotionalValue")) or 5.0,
        "max_lev": int(fnum(lev.get("maxLeverage")) or 0) or 50,
        "max_mkt_qty": fnum(lot.get("maxMktOrderQty")),
    }


def contract_rows(rows: List[Dict[str, Any]], want: Optional[set] = None) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        c = parse_instrument(r)
        if c is None or (want is not None and c["symbol"] not in want):
            continue
        out.append(c)
    return out


# ---- price hub ---------------------------------------------------------------------------------------------------------

def apply_ticker_message(state: Dict[str, Dict[str, Any]], msg: Any) -> Optional[Tuple[str, float, float]]:
    """One tickers.<SYMBOL> push (snapshot or delta) merged into state. Returns (bingx symbol, last price, 24h change in
    percent) once a price is known, else None. A field missing from a delta has not changed."""
    if not isinstance(msg, dict):
        return None
    topic = str(msg.get("topic") or "")
    data = msg.get("data")
    if not topic.startswith("tickers.") or not isinstance(data, dict):
        return None
    sym = str(data.get("symbol") or topic.split(".", 1)[1])
    cur = state.setdefault(sym, {})
    if msg.get("type") == "snapshot":
        cur.clear()
    for k, v in data.items():
        if v is not None and v != "":
            cur[k] = v
    px = fnum(cur.get("lastPrice")) or fnum(cur.get("markPrice"))
    if px <= 0 or not is_usdt_perp(sym):
        return None
    return to_bx(sym), px, fnum(cur.get("price24hPcnt")) * 100.0


class BybitPriceHub:
    """Public linear ticker stream, sharded over connections of at most WS_ARGS_PER_CONN topics. Has the surface of
    bingx_fast.PriceHub: start, set_symbols, ok, last_msg, n, stop."""

    def __init__(self, on_px: Callable[[str, float, float], None], err: ErrorLog, ws_url: str = WS_URL) -> None:
        self.on_px = on_px
        self.err = err
        self.ws_url = ws_url
        self.symbols: List[str] = []
        self.stop = False
        self.ok = False
        self.last_msg = 0.0
        self.n = 0
        self.threads: Dict[int, threading.Thread] = {}
        self._live: Dict[int, bool] = {}
        self._lock = threading.Lock()

    def shards(self) -> List[List[str]]:
        syms = sorted({to_by(s) for s in self.symbols})
        return [syms[i:i + WS_ARGS_PER_CONN] for i in range(0, len(syms), WS_ARGS_PER_CONN)]

    def start(self, symbols: List[str]) -> None:
        self.symbols = list(symbols)
        if _ws is None:
            self.err.write("ws", msg="websocket-client missing")
            return
        self.stop = False
        self._spawn()

    def set_symbols(self, symbols: List[str]) -> None:
        self.symbols = list(symbols)
        if not self.stop and _ws is not None and self.threads:
            self._spawn()

    def _spawn(self) -> None:
        with self._lock:
            for i in range(len(self.shards())):
                t = self.threads.get(i)
                if t is None or not t.is_alive():
                    t = threading.Thread(target=self._run, args=(i,), name=f"by-ws{i}", daemon=True)
                    self.threads[i] = t
                    t.start()

    def _run(self, idx: int) -> None:
        while not self.stop:
            try:
                self._session(idx)
            except Exception as e:
                self._live[idx] = False
                self.ok = any(self._live.values())
                self.err.write("ws-session", msg=str(e)[:240])
            time.sleep(1.2)

    def _session(self, idx: int) -> None:
        ws = _ws.create_connection(self.ws_url, timeout=8, header=[f"User-Agent: {UA}"], enable_multithread=True)
        state: Dict[str, Dict[str, Any]] = {}
        subscribed: set = set()
        self._live[idx] = True
        self.ok = True
        last_ping = 0.0
        ws.settimeout(WS_PING_S + 5.0)
        try:
            while not self.stop:
                now = time.time()
                chunks = self.shards()
                want = set(chunks[idx]) if idx < len(chunks) else set()
                add, drop = sorted(want - subscribed), sorted(subscribed - want)
                for i in range(0, len(add), 10):
                    ws.send(dumps({"op": "subscribe", "args": [f"tickers.{s}" for s in add[i:i + 10]]}))
                for i in range(0, len(drop), 10):
                    ws.send(dumps({"op": "unsubscribe", "args": [f"tickers.{s}" for s in drop[i:i + 10]]}))
                subscribed = want
                if now - last_ping >= WS_PING_S:
                    ws.send(dumps({"op": "ping"}))
                    last_ping = now
                try:
                    raw = ws.recv()
                except Exception as e:
                    if "timed out" in str(e).lower():
                        continue
                    raise
                if raw is None:
                    break
                self.last_msg = time.time()
                try:
                    msg = loads(raw)
                except Exception:
                    continue
                hit = apply_ticker_message(state, msg)
                if hit:
                    self.n += 1
                    self.on_px(hit[0], hit[1], hit[2])
        finally:
            self._live[idx] = False
            self.ok = any(self._live.values())
            try:
                ws.close()
            except Exception:
                pass


# ---- client ------------------------------------------------------------------------------------------------------------

Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, Dict[str, str], bytes]]


def _urllib_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, method=method, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()


ORDER_PATHS = ("/v5/order/create", "/v5/order/create-batch", "/v5/order/cancel", "/v5/order/amend", "/v5/order/amend-batch", "/v5/order/cancel-batch")


class FastBybit:
    def __init__(self, key: str, secret: str, err: ErrorLog, base: str = BASE, ws_url: str = WS_URL,
                 public_base: str = "", transport: Optional[Transport] = None, hedge: bool = True,
                 trigger_by: str = "MarkPrice", sleep: Optional[Callable[[float], None]] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self.key = key
        self.secret = secret
        self.err = err
        self.base = (base or BASE).rstrip("/")
        # market data is the same on every host; the demo host is for signed trading, so its public reads go to mainnet
        self.public_base = (public_base or (BASE if "demo" in self.base else self.base)).rstrip("/")
        self.transport: Transport = transport or _urllib_transport
        self.sleep = sleep or time.sleep
        self.clock = clock or time.time
        self.hedge = hedge
        self.trigger_by = trigger_by
        self.limiter = Limiter()
        self.buckets = self.limiter.eps                    # what the trader's self-test looks for: gates exist
        self.cooldown_until = 0.0
        self.path_cd: Dict[str, float] = {}
        self.px: Dict[str, float] = {}
        self.chg: Dict[str, float] = {}
        self.hub = BybitPriceHub(self._on_px, err, ws_url=ws_url)
        self.on_event = None
        self.bridge = None
        self.stats = {"rest": 0, "ws": 0, "wait": 0.0, "rl": 0, "err": 0, "asyncN": 0, "asyncP50": 0.0, "retry": 0, "resync": 0, "breaker": 0}
        self.inst: Dict[str, Dict[str, Any]] = {}          # bingx symbol -> parse_instrument
        self._inst_ts = 0.0
        self._time_offset = 0
        self._ts_lock = threading.Lock()
        self._last_ts = 0
        self._mode_done = False
        self._margin_done = False
        self.fail_streak = 0
        self.fail_ts = 0.0
        self.breaker_until = 0.0
        self._pub_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None

    # -- plumbing ------------------------------------------------------------------------------------------------------

    def _next_ts(self) -> int:
        with self._ts_lock:
            t = int(self.clock() * 1000) + self._time_offset
            if t <= self._last_ts:
                t = self._last_ts + 1
            self._last_ts = t
            return t

    def start_ws(self, symbols: List[str]) -> None:
        self.hub.start(symbols)

    def _on_px(self, symbol: str, px: float, chg: float = 0.0) -> None:
        self.px[symbol] = px
        self.chg[symbol] = chg
        self.stats["ws"] += 1
        cb = getattr(self, "on_event", None)
        if cb:
            try:
                cb("tick")
            except Exception:
                pass

    def sign(self, ts: str, payload: str) -> str:
        """HMAC-SHA256 over timestamp + api key + recv window + (query string | JSON body), lowercase hex."""
        raw = f"{ts}{self.key}{RECV}{payload}"
        return hmac.new(self.secret.encode(), raw.encode(), hashlib.sha256).hexdigest()

    def _lane(self, v5: str) -> str:
        if v5.startswith("/v5/market"):
            return "public"
        return "order" if v5 in ORDER_PATHS else "private"

    def _cooled(self, why: str) -> Dict[str, Any]:
        return {"retCode": 101209, "retMsg": "cooling " + why, "_cooled": True, "_why": why, "_status": 0, "_headers": {}}

    def _breaker_open(self, v5: str, params: Dict[str, Any]) -> bool:
        """While the breaker is open a call that would add risk (a create that is not reduce-only) is refused; closing orders,
        cancels and reads always go."""
        if self.clock() >= self.breaker_until or v5 not in ("/v5/order/create", "/v5/order/create-batch"):
            return False
        reqs = params.get("request") if v5.endswith("batch") else [params]
        return any(not r.get("reduceOnly") for r in (reqs or []))

    def _note_result(self, v: Verdict, v5: str, write: bool) -> None:
        """The breaker counts transient failures (a timeout, a server error) in a row. Only a write that succeeded clears the
        count: a read that works says nothing about the order path, and old failures age out after 30 s."""
        now = self.clock()
        if v.cls == "transient":
            if now - self.fail_ts > 30.0:
                self.fail_streak = 0
            self.fail_ts = now
            self.fail_streak += 1
            if self.fail_streak >= BREAKER_FAILS and now >= self.breaker_until:
                self.breaker_until = now + BREAKER_S
                self.stats["breaker"] += 1
                self.err.write("breaker", msg=f"{self.fail_streak} transient failures in a row; new entries held {BREAKER_S:.0f}s", path=v5)
        elif write and v.cls == "ok":
            self.fail_streak = 0
            self.breaker_until = 0.0

    def _send(self, method: str, v5: str, params: Dict[str, Any], key: str, signed: bool, cost: int = 1,
              retry_time: bool = True) -> Dict[str, Any]:
        """One Bybit call through the gates. Returns the decoded envelope with "_status" and "_headers" added, a transport
        error, or a cooled marker when a gate (IP ban, endpoint reset, breaker) says the request must not go."""
        if self._breaker_open(v5, params):
            return self._cooled("circuit-breaker")
        wait, why = self.limiter.acquire(v5, cost, now=self.clock())
        if wait is None:
            return self._cooled(why)
        if wait > 0:
            self.stats["wait"] += wait
            self.sleep(wait)
        base = self.base if signed else self.public_base
        headers = {"User-Agent": UA, "Accept": "application/json"}
        body: Optional[bytes] = None
        url = base + v5
        payload = ""
        if method == "GET":
            payload = urllib.parse.urlencode([(k, str(v)) for k, v in params.items() if v is not None])
            if payload:
                url += "?" + payload
        else:
            payload = json.dumps(params, separators=(",", ":"))
            body = payload.encode()
            headers["Content-Type"] = "application/json"
        if signed:
            ts = str(self._next_ts())
            headers.update({"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": str(RECV),
                            "X-BAPI-SIGN": self.sign(ts, payload)})
        self.stats["rest"] += 1
        try:
            status, rh, raw = self.transport(method, url, headers, body)
        except Exception as e:
            self.stats["err"] += 1
            self.err.write("http", method=method, path=v5, msg=str(e)[:220])
            self._note_result(Verdict("transient", True), v5, method != "GET")
            return {"retCode": -1, "retMsg": str(e)[:400], "_transport": True, "_class": "transient", "_status": 0, "_headers": {}}
        rh = {str(k).lower(): v for k, v in (rh or {}).items()}
        try:
            env = loads(raw) if raw else {}
        except Exception:
            env = {}
        if not isinstance(env, dict):
            env = {}
        env["_status"], env["_headers"] = status, rh
        if "retCode" not in env:                       # not a V5 envelope: an HTTP error page, a gateway reply, a bad body
            env["retCode"] = status if status != 200 else -1
            env.setdefault("retMsg", (raw[:200].decode("utf-8", "ignore") if raw else "empty response"))
            env["_badbody"] = True
        code, msg = env.get("retCode"), str(env.get("retMsg") or "")
        self.limiter.observe(v5, rh, now=self.clock())
        v = classify(code, status, msg)
        env["_class"] = v.cls
        if v.cls == "time" and retry_time:
            self._sync_time()
            return self._send(method, v5, params, key, signed, cost, retry_time=False)
        if v.cls == "ip-ban":
            pause = self.limiter.ban_ip(self.clock())
            self.path_cd[key] = self.clock() + pause
            self.cooldown_until = max(self.cooldown_until, self.clock() + min(pause, 12.0))
            self.stats["rl"] += 1
            self.err.write("rate-limit", path=key, code=code, msg=f"IP rate limit: no requests for {pause:.0f}s", wait=round(pause, 1))
        elif v.cls == "rate":
            pause = self.limiter.trip(v5, rh, now=self.clock())
            self.path_cd[key] = max(self.path_cd.get(key, 0.0), self.clock() + pause)
            self.cooldown_until = max(self.cooldown_until, self.clock() + min(pause, 12.0))
            self.stats["rl"] += 1
            self.err.write("rate-limit", path=key, code=code, msg=msg[:160], wait=round(pause, 2))
        elif v.cls not in ("ok", "not-found", "unchanged", "duplicate", "trigger"):
            self.err.write("api", method=method, path=v5, code=code, msg=msg[:220], cls=v.cls)
        self._note_result(v, v5, method != "GET")
        return env

    def _sync_time(self) -> None:
        """retCode 10002: the local clock is outside the receive window. The offset to the venue's clock is applied to every
        later timestamp (checked against Bybit's own server time, never guessed)."""
        env = self._send("GET", "/v5/market/time", {}, "time", False, retry_time=False)
        t = fnum(env.get("time")) or fnum((env.get("result") or {}).get("timeNano")) / 1e6
        if t > 0:
            self._time_offset = int(t - self.clock() * 1000)
            self.stats["resync"] += 1
            self.err.write("time-sync", msg=f"clock offset {self._time_offset} ms")

    def call(self, key: str, method: str, v5: str, params: Dict[str, Any], signed: bool = True, cost: int = 1,
             retries: int = 0) -> Dict[str, Any]:
        """A gated call. `key` is the BingX path the trader watches in path_cd (its cooldown gates the call first). A transient
        failure is repeated `retries` times after a pause: only reads and idempotent writes pass retries > 0."""
        if self.clock() < self.path_cd.get(key, 0.0) or (v5 in ORDER_PATHS and self.clock() < self.cooldown_until):
            return self._cooled("path-cooling")
        env = self._send(method, v5, params, key, signed, cost)
        n = 0
        while n < retries and env.get("_class") == "transient" and not env.get("_cooled"):
            n += 1
            self.stats["retry"] += 1
            self.sleep(RETRY_PAUSE_S * n)
            env = self._send(method, v5, params, key, signed, cost)
        return env

    # -- error words ---------------------------------------------------------------------------------------------------

    def fail(self, env: Dict[str, Any]) -> Dict[str, Any]:
        """A failed Bybit envelope as the BingX-shaped error the trader reads."""
        if env.get("_cooled"):
            return {"code": 101209, "msg": "cooling" + (" (" + str(env.get("_why")) + ")" if env.get("_why") else ""), "error": True, "cooled": True}
        if env.get("_transport"):
            return {"code": -1, "msg": str(env.get("retMsg") or "")[:400], "error": True}
        code = env.get("retCode")
        msg = str(env.get("retMsg") or "")
        status = int(env.get("_status") or 0)
        v = classify(code, status, msg)
        icode = int(code) if isinstance(code, (int, float)) else -1
        if v.cls in ("rate", "ip-ban"):
            tail = " the IP is held for ten minutes" if v.cls == "ip-ban" else ""
            return {"code": 100410, "msg": f"rate limit (bybit {code}){tail} {msg}"[:260]}
        if v.cls == "auth":
            word = AUTH_WORDS.get(icode, WORDS["auth"])
        elif v.cls == "leverage":
            sym = str(env.get("_symbol") or "")
            mx = int((self.inst.get(sym) or {}).get("max_lev") or 0)
            word = WORDS["param"] if not mx else f"maximum leverage {mx}"
        elif v.cls == "permanent":
            word = ""
        else:
            word = WORDS.get(v.cls, "")
        text = f"{word} (bybit {code}) {msg}".strip() if word else f"{msg} (bybit {code})"
        out: Dict[str, Any] = {"code": icode, "msg": text[:260]}
        if v.cls == "transient":
            out["transient"] = True
        return out

    @staticmethod
    def ok(env: Dict[str, Any]) -> bool:
        return env.get("retCode") == 0 and not env.get("_transport") and not env.get("_cooled")

    @staticmethod
    def verdict(env: Dict[str, Any]) -> Verdict:
        return classify(env.get("retCode"), int(env.get("_status") or 0), str(env.get("retMsg") or ""))

    # -- instruments ---------------------------------------------------------------------------------------------------

    def load_instruments(self, force: bool = False) -> Dict[str, Dict[str, Any]]:
        if self.inst and not force and self.clock() - self._inst_ts < 3600:
            return self.inst
        rows: List[Dict[str, Any]] = []
        cursor = ""
        for _ in range(8):
            params = {"category": CATEGORY, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            env = self.call("contracts", "GET", "/v5/market/instruments-info", params, signed=False, retries=1)
            if not self.ok(env):
                break
            res = env.get("result") or {}
            rows.extend(res.get("list") or [])
            cursor = str(res.get("nextPageCursor") or "")
            if not cursor:
                break
        found = {c["symbol"]: c for c in contract_rows(rows)}
        if found:
            self.inst, self._inst_ts = found, self.clock()
        return self.inst

    def _qty(self, symbol: str, q: Any) -> str:
        c = self.inst.get(symbol)
        if c is None:
            c = self.load_instruments().get(symbol)
        return on_step(q, c["qty_step"]) if c else fmt_dec(Decimal(str(q)))

    def _px(self, symbol: str, p: Any) -> str:
        c = self.inst.get(symbol)
        if c is None:
            c = self.load_instruments().get(symbol)
        return on_step(p, c["tick"]) if c else fmt_dec(Decimal(str(p)))

    # -- account setup -------------------------------------------------------------------------------------------------

    def _setup_account(self) -> None:
        """Hedge position mode for every USDT perpetual, once the venue has answered (an 'already set' reply, retCode 110025,
        counts). A rate limit, a transport error or a cooling gate does not mark it done: the next order asks again."""
        if self._mode_done or not self.hedge:
            return
        env = self.call("setup", "POST", "/v5/position/switch-mode", {"category": CATEGORY, "coin": SETTLE, "mode": 3})
        cls = env.get("_class")
        if self.ok(env) or cls in ("unchanged",):
            self._mode_done = True
        elif not env.get("_cooled") and cls not in ("rate", "ip-ban", "transient", "time"):
            self._mode_done = True                       # a definite refusal (open positions, unsupported account): do not nag
            self.err.write("setup", msg=f"hedge mode: {env.get('retCode')} {str(env.get('retMsg'))[:120]}")

    def _pos_idx(self, position_side: str) -> int:
        if not self.hedge:
            return 0
        return 1 if str(position_side).upper() == "LONG" else 2

    # -- order translation ---------------------------------------------------------------------------------------------

    def order_request(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """A BingX order body (trade/order, batchOrders item) as a Bybit create-order request."""
        symbol = str(body.get("symbol") or "")
        otype = str(body.get("type") or "MARKET").upper()
        side = "Buy" if str(body.get("side") or "").upper() == "BUY" else "Sell"
        pos_side = str(body.get("positionSide") or ("LONG" if side == "Buy" else "SHORT")).upper()
        closing = (pos_side == "LONG" and side == "Sell") or (pos_side == "SHORT" and side == "Buy")
        close_all = str(body.get("closePosition") or "").lower() in ("true", "1")
        req: Dict[str, Any] = {"category": CATEGORY, "symbol": to_by(symbol), "side": side, "positionIdx": self._pos_idx(pos_side)}
        qty = body.get("quantity")
        trigger = otype in ("STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT", "TRIGGER_MARKET", "TP_MARKET")
        if trigger:
            is_sl = otype in ("STOP_MARKET", "STOP", "TRIGGER_MARKET")
            sp = body.get("stopPrice")
            req["orderType"] = "Limit" if otype in ("STOP", "TAKE_PROFIT") else "Market"
            req["triggerPrice"] = self._px(symbol, sp)
            req["triggerBy"] = "LastPrice" if str(body.get("workingType") or "").upper() == "CONTRACT_PRICE" else self.trigger_by
            # a stop sells below the market for a long and buys above it for a short; a take profit is the reverse
            falls = (pos_side == "LONG") == is_sl
            req["triggerDirection"] = 2 if falls else 1
            req["reduceOnly"] = True
            req["closeOnTrigger"] = True
            if req["orderType"] == "Limit":
                req["price"] = self._px(symbol, body.get("price") or sp)
                req["timeInForce"] = "GTC"
        else:
            req["orderType"] = "Limit" if otype == "LIMIT" else "Market"
            if req["orderType"] == "Limit":
                req["price"] = self._px(symbol, body.get("price"))
                req["timeInForce"] = "GTC"
            if closing:
                req["reduceOnly"] = True
                if close_all:
                    req["closeOnTrigger"] = True
        if close_all and (trigger or closing):
            req["qty"] = "0"                                   # qty 0 with reduceOnly + closeOnTrigger closes the position
            req["reduceOnly"], req["closeOnTrigger"] = True, True
        else:
            req["qty"] = self._qty(symbol, qty)
        cid = str(body.get("clientOrderID") or body.get("clientOrderId") or "")
        if cid:
            req["orderLinkId"] = cid[:36]
        return req

    def _bx_order(self, o: Dict[str, Any]) -> Dict[str, Any]:
        """A Bybit order (realtime / history row) as the BingX order row."""
        idx = int(fnum(o.get("positionIdx")))
        side = str(o.get("side") or "")
        reduce = bool(o.get("reduceOnly")) or bool(o.get("closeOnTrigger"))
        if idx == 1:
            pos_side = "LONG"
        elif idx == 2:
            pos_side = "SHORT"
        elif reduce:
            pos_side = "LONG" if side == "Sell" else "SHORT"
        else:
            pos_side = "LONG" if side == "Buy" else "SHORT"
        trig = fnum(o.get("triggerPrice"))
        stop_type = str(o.get("stopOrderType") or "")
        market = str(o.get("orderType") or "Market") == "Market"
        if trig > 0 or stop_type in ("StopLoss", "TakeProfit", "PartialStopLoss", "PartialTakeProfit"):
            direction = int(fnum(o.get("triggerDirection")))
            if stop_type in ("StopLoss", "PartialStopLoss"):
                is_sl = True
            elif stop_type in ("TakeProfit", "PartialTakeProfit"):
                is_sl = False
            else:
                is_sl = (pos_side == "LONG" and direction == 2) or (pos_side == "SHORT" and direction == 1)
            typ = ("STOP_MARKET" if is_sl else "TAKE_PROFIT_MARKET") if market else ("STOP" if is_sl else "TAKE_PROFIT")
        else:
            typ = "MARKET" if market else "LIMIT"
        qty = str(o.get("qty") or "")
        status = STATUS.get(str(o.get("orderStatus") or ""), str(o.get("orderStatus") or "").upper())
        row = {
            "symbol": to_bx(o.get("symbol")),
            "orderId": str(o.get("orderId") or ""),
            "clientOrderID": str(o.get("orderLinkId") or ""),
            "type": typ,
            "side": "BUY" if side == "Buy" else "SELL",
            "positionSide": pos_side,
            "stopPrice": str(o.get("triggerPrice") or "") if trig > 0 else "",
            "price": str(o.get("price") or ""),
            "avgPrice": str(o.get("avgPrice") or ""),
            "origQty": qty,
            "quantity": qty,
            "executedQty": str(o.get("cumExecQty") or "0"),
            "status": status,
            "closePosition": "true" if reduce and fnum(qty) == 0 and trig > 0 else "false",
            "workingType": "CONTRACT_PRICE" if str(o.get("triggerBy") or "") == "LastPrice" else "MARK_PRICE",
            "time": int(fnum(o.get("createdTime"))),
            "updateTime": int(fnum(o.get("updatedTime"))),
        }
        if o.get("cumRealisedPnl") not in (None, ""):
            row["profit"] = str(o.get("cumRealisedPnl"))
        return row

    # -- BingX-shaped reads --------------------------------------------------------------------------------------------

    def _balance(self, path: str) -> Dict[str, Any]:
        env = self.call(path, "GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": SETTLE}, retries=1)
        if not self.ok(env) and env.get("_class") in ("param", "permanent"):
            env2 = self.call(path, "GET", "/v5/account/wallet-balance", {"accountType": "CONTRACT", "coin": SETTLE}, retries=1)
            if self.ok(env2):
                env = env2
        if not self.ok(env):
            return self.fail(env)
        rows = (env.get("result") or {}).get("list") or []
        acct = rows[0] if rows else {}
        coin = next((c for c in (acct.get("coin") or []) if str(c.get("coin") or SETTLE) == SETTLE), {})
        equity = fnum(coin.get("equity")) or fnum(acct.get("totalEquity"))
        wallet = fnum(coin.get("walletBalance")) or fnum(acct.get("totalWalletBalance")) or equity
        avail = fnum(acct.get("totalAvailableBalance"))
        if not avail:
            avail = fnum(coin.get("availableToWithdraw")) or fnum(coin.get("availableBalance"))
        used = fnum(acct.get("totalInitialMargin")) or fnum(coin.get("totalPositionIM"))
        upl = fnum(coin.get("unrealisedPnl")) or fnum(acct.get("totalPerpUPL"))
        return {"code": 0, "data": {"balance": {"asset": SETTLE, "balance": str(wallet), "equity": str(equity),
                                                "availableMargin": str(avail), "usedMargin": str(used), "unrealizedProfit": str(upl)}}}

    def _positions(self, path: str, symbol: str = "") -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        cursor = ""
        for _ in range(10):
            params: Dict[str, Any] = {"category": CATEGORY, "limit": 200}
            if symbol:
                params["symbol"] = to_by(symbol)
            else:
                params["settleCoin"] = SETTLE
            if cursor:
                params["cursor"] = cursor
            env = self.call(path, "GET", "/v5/position/list", params, retries=1)
            if not self.ok(env):
                return self.fail(env)
            res = env.get("result") or {}
            rows.extend(res.get("list") or [])
            cursor = str(res.get("nextPageCursor") or "")
            if not cursor:
                break
        out = []
        for p in rows:
            size = fnum(p.get("size"))
            side = str(p.get("side") or "")
            if size <= 0 or side not in ("Buy", "Sell"):
                continue
            idx = int(fnum(p.get("positionIdx")))
            long_ = idx == 1 or (idx == 0 and side == "Buy")
            sym = to_bx(p.get("symbol"))
            out.append({
                "symbol": sym,
                "positionSide": "LONG" if long_ else "SHORT",
                "positionAmt": fmt_dec(Decimal(str(size))) if long_ else "-" + fmt_dec(Decimal(str(size))),
                "availableAmt": fmt_dec(Decimal(str(size))),
                "avgPrice": str(p.get("avgPrice") or "0"),
                "entryPrice": str(p.get("avgPrice") or "0"),
                "leverage": str(p.get("leverage") or "0"),
                "liquidationPrice": str(p.get("liqPrice") or "0"),
                "markPrice": str(p.get("markPrice") or "0"),
                "unrealizedProfit": str(p.get("unrealisedPnl") or "0"),
                "positionId": f"{p.get('symbol')}:{idx}",
                "createTime": int(fnum(p.get("createdTime"))),
                "updateTime": int(fnum(p.get("updatedTime"))),
            })
        return {"code": 0, "data": out}

    def _tickers(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        params: Dict[str, Any] = {"category": CATEGORY}            # no symbol: one call answers every symbol
        if extra.get("symbol"):
            params["symbol"] = to_by(extra["symbol"])
        env = self.call(P_TICK, "GET", "/v5/market/tickers", params, signed=False, retries=1)
        if not self.ok(env):
            return self.fail(env)
        out = []
        for t in (env.get("result") or {}).get("list") or []:
            sym = str(t.get("symbol") or "")
            if not is_usdt_perp(sym):
                continue
            out.append({
                "symbol": to_bx(sym),
                "lastPrice": str(t.get("lastPrice") or "0"),
                "markPrice": str(t.get("markPrice") or t.get("lastPrice") or "0"),
                "priceChangePercent": str(fnum(t.get("price24hPcnt")) * 100.0),
                "quoteVolume": str(t.get("turnover24h") or "0"),
                "volume": str(t.get("volume24h") or "0"),
                "highPrice": str(t.get("highPrice24h") or "0"),
                "lowPrice": str(t.get("lowPrice24h") or "0"),
                "bidPrice": str(t.get("bid1Price") or "0"),
                "askPrice": str(t.get("ask1Price") or "0"),
            })
        return {"code": 0, "data": out}

    def _klines(self, path: str, extra: Dict[str, Any]) -> Dict[str, Any]:
        iv = INTERVALS.get(str(extra.get("interval") or "1m"), str(extra.get("interval") or "1"))
        params: Dict[str, Any] = {"category": CATEGORY, "symbol": to_by(extra.get("symbol")), "interval": iv,
                                  "limit": min(1000, max(1, int(fnum(extra.get("limit"), 200))))}
        if extra.get("endTime"):
            params["end"] = int(fnum(extra["endTime"]))
        if extra.get("startTime"):
            params["start"] = int(fnum(extra["startTime"]))
        env = self.call(path, "GET", "/v5/market/kline", params, signed=False, retries=1)
        if not self.ok(env):
            return self.fail(env)
        rows = []
        for k in (env.get("result") or {}).get("list") or []:
            if isinstance(k, (list, tuple)) and len(k) >= 6:
                rows.append({"time": int(fnum(k[0])), "open": str(k[1]), "high": str(k[2]), "low": str(k[3]), "close": str(k[4]), "volume": str(k[5])})
        rows.sort(key=lambda r: r["time"])                      # Bybit answers newest first
        return {"code": 0, "data": rows}

    def _order_pages(self, path: str, v5: str, base_params: Dict[str, Any], pages: int = 10) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        rows: List[Dict[str, Any]] = []
        cursor = ""
        for _ in range(pages):
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            env = self.call(path, "GET", v5, params, retries=1)
            if not self.ok(env):
                return rows, env
            res = env.get("result") or {}
            rows.extend(res.get("list") or [])
            cursor = str(res.get("nextPageCursor") or "")
            if not cursor:
                break
        return rows, None

    def _open_orders(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        params: Dict[str, Any] = {"category": CATEGORY, "limit": 50}
        if extra.get("symbol"):
            params["symbol"] = to_by(extra["symbol"])
        else:
            params["settleCoin"] = SETTLE
        rows, bad = self._order_pages(P_OPEN, "/v5/order/realtime", params)
        if bad is not None:
            return self.fail(bad)
        return {"code": 0, "data": {"orders": [self._bx_order(o) for o in rows]}}

    def _order_get(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        sym, oid = str(extra.get("symbol") or ""), str(extra.get("orderId") or "")
        row = self._find_order(P_ORDER, sym, oid)
        if row is None:
            return {"code": 80016, "msg": f"order not exist (bybit {oid})"}
        return {"code": 0, "data": {"order": self._bx_order(row)}}

    def _find_order(self, path: str, symbol: str, order_id: str, link_id: str = "") -> Optional[Dict[str, Any]]:
        base: Dict[str, Any] = {"category": CATEGORY, "limit": 1}
        if symbol:
            base["symbol"] = to_by(symbol)
        if order_id:
            base["orderId"] = order_id
        elif link_id:
            base["orderLinkId"] = link_id
        for v5, extra in (("/v5/order/realtime", {"openOnly": 0}), ("/v5/order/history", {})):
            env = self.call(path, "GET", v5, {**base, **extra}, retries=1)
            if self.ok(env):
                lst = (env.get("result") or {}).get("list") or []
                if lst:
                    return lst[0]
        return None

    def _executions(self, path: str, symbol: str, order_id: str) -> List[Dict[str, Any]]:
        env = self.call(path, "GET", "/v5/execution/list", {"category": CATEGORY, "symbol": to_by(symbol), "orderId": order_id, "limit": 100}, retries=1)
        return ((env.get("result") or {}).get("list") or []) if self.ok(env) else []

    def _all_orders(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        limit = min(50, max(1, int(fnum(extra.get("limit"), 50))))
        env = self.call(P_ALL, "GET", "/v5/order/history", {"category": CATEGORY, "limit": limit}, retries=1)
        if not self.ok(env):
            return self.fail(env)
        pnl: Dict[str, float] = {}
        cp = self.call(P_ALL, "GET", "/v5/position/closed-pnl", {"category": CATEGORY, "limit": 100}, retries=1)
        if self.ok(cp):
            for r in (cp.get("result") or {}).get("list") or []:
                pnl[str(r.get("orderId") or "")] = pnl.get(str(r.get("orderId") or ""), 0.0) + fnum(r.get("closedPnl"))
        out = []
        for o in (env.get("result") or {}).get("list") or []:
            row = self._bx_order(o)
            if row["orderId"] in pnl:
                row["profit"] = str(pnl[row["orderId"]])
            out.append(row)
        return {"code": 0, "data": {"orders": out}}

    def _fills(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        env = self.call(P_FILLS, "GET", "/v5/execution/list", {"category": CATEGORY, "limit": min(100, max(1, int(fnum(extra.get("pageSize"), 50))))}, retries=1)
        if not self.ok(env):
            return self.fail(env)
        out = []
        for e in (env.get("result") or {}).get("list") or []:
            if str(e.get("execType") or "Trade") not in ("Trade", "BustTrade", "AdlTrade"):
                continue
            out.append({"symbol": to_bx(e.get("symbol")), "orderId": str(e.get("orderId") or ""), "clientOrderID": str(e.get("orderLinkId") or ""),
                        "executedQty": str(e.get("execQty") or "0"), "avgPrice": str(e.get("execPrice") or "0"), "status": "FILLED",
                        "side": "BUY" if str(e.get("side")) == "Buy" else "SELL", "profit": "0", "time": int(fnum(e.get("execTime")))})
        return {"code": 0, "data": {"fill_orders": out}}

    def _leverage_get(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        sym = str(extra.get("symbol") or "")
        env = self.call(P_LEV, "GET", "/v5/position/list", {"category": CATEGORY, "symbol": to_by(sym)}, retries=1)
        if not self.ok(env):
            return self.fail(env)
        long_l = short_l = 0
        for p in (env.get("result") or {}).get("list") or []:
            lv = int(fnum(p.get("leverage")))
            if int(fnum(p.get("positionIdx"))) == 2:
                short_l = lv
            else:
                long_l = lv
                if not self.hedge:
                    short_l = lv
        c = self.inst.get(sym) or self.load_instruments().get(sym) or {}
        mx = int(c.get("max_lev") or 0)
        return {"code": 0, "data": {"maxLongLeverage": mx, "maxShortLeverage": mx, "longLeverage": long_l, "shortLeverage": short_l}}

    def _leverage_set(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        self._setup_account()
        lev = str(int(fnum(extra.get("leverage"), 1)))
        env = self.call(P_LEV, "POST", "/v5/position/set-leverage",
                        {"category": CATEGORY, "symbol": to_by(extra.get("symbol")), "buyLeverage": lev, "sellLeverage": lev}, retries=1)
        if self.ok(env) or env.get("_class") == "unchanged":
            return {"code": 0, "data": {"leverage": int(lev)}}
        env["_symbol"] = str(extra.get("symbol") or "")
        return self.fail(env)

    def _margin_type(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        if self._margin_done:
            return {"code": 0, "data": {}}
        env = self.call("setup", "POST", "/v5/account/set-margin-mode", {"setMarginMode": "REGULAR_MARGIN"})
        cls = env.get("_class")
        if self.ok(env) or cls == "unchanged" or (not env.get("_cooled") and cls not in ("rate", "ip-ban", "transient", "time")):
            self._margin_done = True
            if not self.ok(env) and cls != "unchanged":
                self.err.write("setup", msg=f"margin mode: {env.get('retCode')} {str(env.get('retMsg'))[:120]}")
        return {"code": 0, "data": {}}

    # -- BingX-shaped writes -------------------------------------------------------------------------------------------

    def _fill(self, path: str, symbol: str, order_id: str, requested: str) -> Dict[str, Any]:
        """Average price and executed quantity of a market order (the create reply carries only its ids)."""
        for wait in (0.0, 0.08, 0.2):
            if wait:
                self.sleep(wait)
            fills = self._executions(path, symbol, order_id)
            qty = sum(fnum(f.get("execQty")) for f in fills)
            if qty > 0:
                val = sum(fnum(f.get("execQty")) * fnum(f.get("execPrice")) for f in fills)
                return {"avgPrice": fmt_dec(Decimal(str(val / qty))), "executedQty": fmt_dec(Decimal(str(qty))), "status": "FILLED"}
            row = self._find_order(path, symbol, order_id)
            if row is not None and fnum(row.get("cumExecQty")) > 0 and fnum(row.get("avgPrice")) > 0:
                return {"avgPrice": str(row.get("avgPrice")), "executedQty": str(row.get("cumExecQty")), "status": STATUS.get(str(row.get("orderStatus")), "")}
        return {}

    def _landed(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Did this create reach the venue? Looked up by its orderLinkId, which is what makes a retry safe."""
        link = str(req.get("orderLinkId") or "")
        if not link:
            return None
        return self._find_order("landed", to_bx(req.get("symbol")), "", link)       # its own cooldown key: a create's rate limit must not block the lookup

    def _create(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """POST /v5/order/create, idempotent: a transport error, a timeout or a server error may hide a placed order, so the
        order is looked up by its orderLinkId before it is sent again, and a duplicate-link refusal on the resend resolves to
        the order that exists. A retry never opens a second position."""
        env = self.call(P_ORDER, "POST", "/v5/order/create", req)
        for attempt in range(2):
            cls = env.get("_class")
            if self.ok(env) or env.get("_cooled") or cls not in ("transient", "duplicate", "rate"):
                break
            row = self._landed(req)
            if row is not None:
                return env_ok({"orderId": row.get("orderId"), "orderLinkId": row.get("orderLinkId") or req.get("orderLinkId")})
            if cls == "duplicate":
                break
            self.stats["retry"] += 1
            self.sleep(self._pause(env))
            env = self.call(P_ORDER, "POST", "/v5/order/create", req)
        if env.get("_class") == "duplicate":
            row = self._landed(req)
            if row is not None:
                return env_ok({"orderId": row.get("orderId"), "orderLinkId": row.get("orderLinkId") or req.get("orderLinkId")})
        return env

    def _pause(self, env: Dict[str, Any]) -> float:
        """How long to wait before sending again: to the endpoint's reset time after a rate limit, else a short step."""
        rst = fnum((env.get("_headers") or {}).get("x-bapi-limit-reset-timestamp")) / 1000.0
        if env.get("_class") == "rate" and rst > self.clock():
            return min(3.0, max(RETRY_PAUSE_S, rst - self.clock() + 0.2))
        return RETRY_PAUSE_S

    def _order_post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        self._setup_account()
        req = self.order_request(body)
        env = self._create(req)
        if not self.ok(env):
            env["_symbol"] = str(body.get("symbol") or "")
            return self.fail(env)
        res = env.get("result") or {}
        oid = str(res.get("orderId") or "")
        sym = str(body.get("symbol") or "")
        order: Dict[str, Any] = {"orderId": oid, "clientOrderID": str(res.get("orderLinkId") or req.get("orderLinkId") or ""),
                                 "symbol": sym, "type": str(body.get("type") or "MARKET").upper(), "side": str(body.get("side") or ""),
                                 "positionSide": str(body.get("positionSide") or ""), "quantity": req.get("qty"), "origQty": req.get("qty")}
        if "triggerPrice" not in req and oid:
            order.update(self._fill(P_ORDER, sym, oid, str(req.get("qty"))))
        else:
            order["stopPrice"] = req.get("triggerPrice", "")
        return {"code": 0, "data": {"order": order, "orderId": oid}}

    def _order_cancel(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        req: Dict[str, Any] = {"category": CATEGORY, "symbol": to_by(extra.get("symbol"))}
        if extra.get("orderId"):
            req["orderId"] = str(extra["orderId"])
        elif extra.get("clientOrderID"):
            req["orderLinkId"] = str(extra["clientOrderID"])
        env = self.call(P_ORDER, "POST", "/v5/order/cancel", req, retries=1)       # a cancel is idempotent: the order is gone or it is not
        if not self.ok(env):
            return self.fail(env)
        res = env.get("result") or {}
        return {"code": 0, "data": {"order": {"orderId": str(res.get("orderId") or req.get("orderId") or "")}}}

    # -- batches -------------------------------------------------------------------------------------------------------

    def _run_batch(self, v5: str, items: List[Dict[str, Any]], key: str, landed: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
                   max_rounds: int = 3) -> List[Dict[str, Any]]:
        """One batch endpoint over any number of items, BATCH_MAX per call. Each item ends as {"ok", "orderId", "orderLinkId",
        "code", "msg", "cls"}. An item that failed for a transient reason or a rate limit is sent again on its own (only the
        failed items, not the batch); a duplicate or a transport error is resolved by looking the item up by its
        orderLinkId first (`landed`), so nothing is placed twice. The rest of the batch is never resent."""
        results: List[Optional[Dict[str, Any]]] = [None] * len(items)
        pending = list(range(len(items)))
        rounds = 0
        while pending and rounds < max_rounds:
            rounds += 1
            retry: List[int] = []
            pause = RETRY_PAUSE_S
            for lo in range(0, len(pending), BATCH_MAX):
                idxs = pending[lo:lo + BATCH_MAX]
                sub = [items[i] for i in idxs]
                env = self.call(key, "POST", v5, {"category": CATEGORY, "request": sub}, cost=len(sub))
                if env.get("_cooled"):
                    for i in idxs:
                        results[i] = {"ok": False, "orderId": "", "orderLinkId": str(items[i].get("orderLinkId") or ""), "code": 101209, "msg": "cooling", "cls": "cooled", "cooled": True}
                    continue
                if not self.ok(env):                      # the whole call failed: every item is unknown, not necessarily failed
                    cls = env.get("_class")
                    for i in idxs:
                        row = landed(items[i]) if (landed is not None and cls in ("transient", "rate")) else None
                        if row is not None:
                            results[i] = {"ok": True, "orderId": str(row.get("orderId") or ""), "orderLinkId": str(row.get("orderLinkId") or items[i].get("orderLinkId") or ""), "code": 0, "msg": "", "cls": "ok"}
                        elif cls in ("transient", "rate"):
                            retry.append(i)
                            results[i] = self._item_fail(env.get("retCode"), env.get("retMsg"), cls, items[i])
                            pause = max(pause, self._pause(env))
                        else:
                            results[i] = self._item_fail(env.get("retCode"), env.get("retMsg"), cls, items[i], int(env.get("_status") or 0))
                    continue
                made = (env.get("result") or {}).get("list") or []
                info = (env.get("retExtInfo") or {}).get("list") or []
                for j, i in enumerate(idxs):
                    m = made[j] if j < len(made) and isinstance(made[j], dict) else {}
                    inf = info[j] if j < len(info) and isinstance(info[j], dict) else {}
                    code = inf.get("code", 0 if m.get("orderId") else -1)
                    if code in (0, None, "0") and (m.get("orderId") or v5.endswith("amend-batch") or v5.endswith("cancel-batch")):
                        results[i] = {"ok": True, "orderId": str(m.get("orderId") or items[i].get("orderId") or ""), "orderLinkId": str(m.get("orderLinkId") or items[i].get("orderLinkId") or ""), "code": 0, "msg": "", "cls": "ok"}
                        continue
                    v = classify(code, 200, str(inf.get("msg") or ""))
                    if v.cls == "duplicate" and landed is not None:
                        row = landed(items[i])
                        if row is not None:
                            results[i] = {"ok": True, "orderId": str(row.get("orderId") or ""), "orderLinkId": str(row.get("orderLinkId") or ""), "code": 0, "msg": "", "cls": "ok"}
                            continue
                    results[i] = self._item_fail(code, inf.get("msg"), v.cls, items[i])
                    if v.retry and v.cls in ("transient", "rate"):
                        retry.append(i)
            pending = retry
            if pending and rounds < max_rounds:
                self.stats["retry"] += len(pending)
                self.sleep(pause)
        return [r if r is not None else {"ok": False, "orderId": "", "orderLinkId": "", "code": -1, "msg": "no result", "cls": "transient"} for r in results]

    def _item_fail(self, code: Any, msg: Any, cls: Any, item: Dict[str, Any], status: int = 200) -> Dict[str, Any]:
        bad = self.fail({"retCode": code, "retMsg": msg, "_status": status, "_symbol": to_bx(item.get("symbol"))})
        return {"ok": False, "orderId": "", "orderLinkId": str(item.get("orderLinkId") or ""), "code": bad["code"], "msg": bad["msg"], "cls": str(cls or "permanent")}

    def batch_place(self, orders: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Any number of BingX order bodies (several symbols, SL and TP together) as create-batch calls of BATCH_MAX. The reply
        has one row per order, in order, whether or not others in the same call failed."""
        if not orders:
            return {"code": 0, "data": {"orders": []}}
        self._setup_account()
        reqs = []
        for o in orders:
            r = self.order_request(o)
            r.pop("category", None)
            reqs.append(r)
        results = self._run_batch("/v5/order/create-batch", reqs, P_BATCH, landed=self._landed)
        rows = []
        for o, r in zip(orders, results):
            row: Dict[str, Any] = {"symbol": str(o.get("symbol") or ""), "type": str(o.get("type") or "").upper(),
                                   "positionSide": str(o.get("positionSide") or ""), "side": str(o.get("side") or ""),
                                   "clientOrderID": r["orderLinkId"] or str(o.get("clientOrderID") or "")}
            if r["ok"]:
                row.update({"code": 0, "orderId": r["orderId"], "msg": ""})
            else:
                row.update({"code": r["code"], "msg": r["msg"], "orderId": ""})
                if r.get("cooled"):
                    row["cooled"] = True
            rows.append(row)
        if rows and all(r.get("cooled") for r in rows):
            return {"code": 101209, "msg": "cooling", "error": True, "cooled": True, "data": {"orders": rows}}
        return {"code": 0, "data": {"orders": rows}}

    def batch_cancel(self, orders: List[Dict[str, Any]]) -> Dict[str, Any]:
        """orders: [{"symbol": "BTC-USDT", "orderId": "..."}]. One row each: an order that is already gone counts as cancelled."""
        if not orders:
            return {"code": 0, "data": {"orders": []}}
        items = [{"symbol": to_by(o.get("symbol")), **({"orderId": str(o["orderId"])} if o.get("orderId") else {"orderLinkId": str(o.get("clientOrderID") or "")})} for o in orders]
        results = self._run_batch("/v5/order/cancel-batch", items, P_ORDER)
        rows = []
        for o, r in zip(orders, results):
            gone = r["cls"] in ("not-found",)
            rows.append({"symbol": str(o.get("symbol") or ""), "orderId": r["orderId"] or str(o.get("orderId") or ""), "code": 0 if (r["ok"] or gone) else r["code"],
                         "msg": "" if (r["ok"] or gone) else r["msg"]})
        return {"code": 0, "data": {"orders": rows}}

    def batch_amend(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """items: [{"symbol", "orderId", "stopPrice"?, "quantity"?}]: trigger prices (and quantities) of existing orders."""
        if not items:
            return {"code": 0, "data": {"orders": []}}
        reqs = []
        for it in items:
            sym = str(it.get("symbol") or "")
            r: Dict[str, Any] = {"symbol": to_by(sym), "orderId": str(it.get("orderId") or "")}
            if it.get("stopPrice") not in (None, ""):
                r["triggerPrice"] = self._px(sym, it["stopPrice"])
            if it.get("quantity") not in (None, ""):
                r["qty"] = self._qty(sym, it["quantity"])
            reqs.append(r)
        results = self._run_batch("/v5/order/amend-batch", reqs, P_REPLACE)
        return {"code": 0, "data": {"orders": [{"symbol": str(i.get("symbol") or ""), "orderId": r["orderId"], "code": 0 if r["ok"] else r["code"], "msg": r["msg"]} for i, r in zip(items, results)]}}

    def cancel_replace(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Move a stop: amend the conditional order's trigger price. BingX cancels the old order and places a new one; Bybit
        changes the order in place, so its id stays."""
        body = dict(extra or {})
        old = str(body.get("cancelOrderId") or "")
        sym = str(body.get("symbol") or "")
        if not old or not sym:
            return {"code": -1, "msg": "cancelReplace needs symbol and cancelOrderId", "error": True}
        req: Dict[str, Any] = {"category": CATEGORY, "symbol": to_by(sym), "orderId": old,
                               "triggerPrice": self._px(sym, body.get("stopPrice"))}
        if str(body.get("quantity") or "") and str(body.get("closePosition") or "").lower() not in ("true", "1"):
            req["qty"] = self._qty(sym, body.get("quantity"))
        env = self.call(P_REPLACE, "POST", "/v5/order/amend", req, retries=1)      # setting the same trigger twice is harmless
        if not self.ok(env):
            return self.fail(env)
        oid = str((env.get("result") or {}).get("orderId") or old)
        return {"code": 0, "data": {"order": {"orderId": oid}, "orderId": oid}}

    # -- public surface (the FastBingX interface) ----------------------------------------------------------------------

    def _route(self, method: str, path: str, extra: Dict[str, Any]) -> Dict[str, Any]:
        if method == "GET":
            if path in (P_BAL3, P_BAL2):
                return self._balance(path)
            if path == P_POS:
                return self._positions(path)
            if path == P_OPEN:
                return self._open_orders(extra)
            if path == P_ORDER:
                return self._order_get(extra)
            if path == P_ALL:
                return self._all_orders(extra)
            if path == P_FILLS:
                return self._fills(extra)
            if path == P_LEV:
                return self._leverage_get(extra)
        elif method == "POST":
            if path == P_ORDER:
                return self._order_post(extra)
            if path == P_LEV:
                return self._leverage_set(extra)
            if path == P_MARGIN:
                return self._margin_type(extra)
            if path == P_REPLACE:
                return self.cancel_replace(extra)
        elif method == "DELETE":
            if path == P_ORDER:
                return self._order_cancel(extra)
        self.err.write("unsupported", method=method, path=path)
        return {"code": -1, "msg": f"bybit: unsupported call {method} {path}", "error": True}

    def get(self, path: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._route("GET", path, dict(extra or {}))

    def post(self, path: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._route("POST", path, dict(extra or {}))

    def delete(self, path: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._route("DELETE", path, dict(extra or {}))

    def reserve_public(self, path: str) -> Optional[float]:
        if self.clock() < self.path_cd.get(path, 0.0):
            return None
        wait, _why = self.limiter.acquire("/v5/market/tickers" if path == P_TICK else "/v5/market/kline", 1, now=self.clock())
        return wait

    def public(self, path: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.clock() < self.path_cd.get(path, 0.0) or self.clock() < self.limiter.ip_ban_until:
            return {"code": 101209, "msg": "cooling", "error": True, "cooled": True}
        extra = dict(extra or {})
        if path == P_TICK:
            return self._tickers(extra)
        if path in (P_KL2, P_KL3):
            return self._klines(path, extra)
        if path == P_CONTRACTS:
            return {"code": 0, "data": list(self.load_instruments().values())}
        return {"code": -1, "msg": f"bybit: unsupported public call {path}", "error": True}

    def gather_public(self, reqs: List[Tuple[str, Dict[str, Any]]], timeout: float = 4.2) -> List[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
        """Public GETs (klines of several symbols) in parallel on a small pool; every request still passes the IP and the
        endpoint gate, so the parallelism never exceeds the limits."""
        if not reqs:
            return []
        pool = self._pub_pool
        if pool is None:
            pool = self._pub_pool = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="by-pub")
        futs = [(p, e, pool.submit(self.public, p, e)) for p, e in reqs]
        out: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
        end = time.monotonic() + max(0.5, float(timeout)) + 5.0
        for p, e, f in futs:
            try:
                out.append((p, e, f.result(timeout=max(0.05, end - time.monotonic()))))
            except Exception as exc:
                out.append((p, e, {"error": True, "msg": f"thread-gather: {str(exc)[:80]}"}))
        return out

    def snapshot(self) -> Dict[str, Any]:
        return {
            "rest": self.stats["rest"],
            "ws": self.stats["ws"],
            "wsOk": bool(self.hub.ok and time.time() - self.hub.last_msg < 8),
            "wsAgeMs": round((time.time() - self.hub.last_msg) * 1000) if self.hub.last_msg else None,
            "rateWaits": round(self.stats["wait"], 3),
            "rateTrips": self.stats["rl"],
            "httpErr": self.stats["err"],
            "asyncN": 0,
            "asyncP50": 0.0,
            "retries": self.stats["retry"],
            "breakerOpen": self.clock() < self.breaker_until,
            "ipBanSeconds": max(0, round(self.limiter.ip_ban_until - self.clock())),
            "errors": self.err.recent(8),
            "errorN": self.err.n,
        }


def env_ok(result: Dict[str, Any]) -> Dict[str, Any]:
    """A success envelope built locally (an order found by its link id after an unclear create)."""
    return {"retCode": 0, "retMsg": "OK", "result": result, "retExtInfo": {}, "_status": 200, "_headers": {}, "_class": "ok", "_resolved": True}


def fetch_contract_rows(base: str = BASE, want: Optional[set] = None, transport: Optional[Transport] = None) -> List[Dict[str, Any]]:
    """The trader's contract fields for every USDT perpetual (pulse_trader.load_contracts for a bybit connection)."""
    tr = transport or _urllib_transport
    rows: List[Dict[str, Any]] = []
    cursor = ""
    for _ in range(8):
        qs = {"category": CATEGORY, "limit": 1000}
        if cursor:
            qs["cursor"] = cursor
        status, _h, raw = tr("GET", f"{base.rstrip('/')}/v5/market/instruments-info?{urllib.parse.urlencode(qs)}", {"User-Agent": UA}, None)
        env = loads(raw) if raw else {}
        if status != 200 or env.get("retCode") != 0:
            raise RuntimeError(f"bybit instruments-info {status} {env.get('retCode')} {str(env.get('retMsg'))[:80]}")
        res = env.get("result") or {}
        rows.extend(res.get("list") or [])
        cursor = str(res.get("nextPageCursor") or "")
        if not cursor:
            break
    return contract_rows(rows, want)
