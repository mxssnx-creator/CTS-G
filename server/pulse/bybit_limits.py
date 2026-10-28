#!/usr/bin/env python3
"""Bybit V5 rate limits and error classes (pure logic, no I/O), used by bybit_fast.FastBybit.

Limits (Bybit V5 "Rate Limit" page):
  * IP: 600 requests per 5 s per IP. A breach answers HTTP 403 and holds for ten minutes, so the client stays far below it
    (IP_RATE) and, once a 403 / retCode 10018 is seen, sends nothing at all until the ban has run out.
  * UID: a rolling one-second window per endpoint: order create / amend / cancel and the three batch endpoints 10 per second
    (a batch counts once per order item), reads 50 per second, set-leverage 10 per second. Every reply carries
    X-Bapi-Limit (the limit), X-Bapi-Limit-Status (what is left in the window) and X-Bapi-Limit-Reset-Timestamp (when the
    window frees, the current time unless the limit was exceeded). Those headers drive a per-endpoint gate here; the local
    token bucket only paces what the headers cannot see.
  * retCode 10006 (too many visits) pauses that endpoint until the reset timestamp.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

from bingx_fast import TokenBucket

IP_RATE = (80.0, 160.0)               # requests per second, burst: 400 per 5 s on average, headroom under 600
IP_BAN_S = 600.0                       # a breached IP limit holds for ten minutes
MAX_WAIT_S = 3.0                       # a wait longer than this is reported as cooling instead of blocking the caller

# (requests or order items per second, burst)
ENDPOINTS: Dict[str, Tuple[float, float]] = {
    "/v5/order/create": (10.0, 10.0),
    "/v5/order/amend": (10.0, 10.0),
    "/v5/order/cancel": (10.0, 10.0),
    "/v5/order/create-batch": (10.0, 10.0),
    "/v5/order/amend-batch": (10.0, 10.0),
    "/v5/order/cancel-batch": (10.0, 10.0),
    "/v5/position/set-leverage": (10.0, 10.0),
    "/v5/position/switch-mode": (5.0, 5.0),
    "/v5/account/set-margin-mode": (5.0, 5.0),
    "/v5/order/realtime": (40.0, 50.0),
    "/v5/order/history": (40.0, 50.0),
    "/v5/execution/list": (40.0, 50.0),
    "/v5/position/list": (40.0, 50.0),
    "/v5/position/closed-pnl": (40.0, 50.0),
    "/v5/account/wallet-balance": (40.0, 50.0),
}
DEFAULT_PRIVATE = (20.0, 20.0)
DEFAULT_PUBLIC = (60.0, 100.0)


class Endpoint:
    """One endpoint's gate: a token bucket plus what the last reply's headers said."""

    def __init__(self, rate: float, burst: float) -> None:
        self.bucket = TokenBucket(rate, burst)
        self.limit: Optional[float] = None         # X-Bapi-Limit
        self.remaining: Optional[float] = None     # X-Bapi-Limit-Status
        self.reset_at = 0.0                        # X-Bapi-Limit-Reset-Timestamp, seconds
        self.seen_at = 0.0                         # when those headers arrived
        self.cool_until = 0.0

    def observe(self, headers: Dict[str, str], now: float) -> None:
        def num(k: str) -> Optional[float]:
            try:
                return float(headers.get(k))      # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
        lim, rem, rst = num("x-bapi-limit"), num("x-bapi-limit-status"), num("x-bapi-limit-reset-timestamp")
        if lim is not None and lim > 0:
            self.limit = lim
            if abs(self.bucket.rate - lim) > 1e-9:          # the venue's own number replaces the guess (a raised limit counts)
                self.bucket.rate = lim
                self.bucket.burst = max(lim, 1.0)
        if rem is not None:
            self.remaining, self.seen_at = rem, now
        if rst is not None and rst > 1e11:
            self.reset_at = rst / 1000.0

    def wait_for(self, cost: int, now: float) -> float:
        """Seconds to wait before `cost` units may be sent (0.0 when free)."""
        w = 0.0
        if self.remaining is not None and self.remaining < cost:
            # the window said it was used up: the next one starts at the reset time, else one second after that reply
            frees = max(self.reset_at, self.seen_at + 1.0)
            w = max(0.0, frees - now)
            if w <= 0.0:
                self.remaining = None
        for _ in range(max(1, cost)):
            w = max(w, self.bucket.reserve())
        return w


class Limiter:
    """Every gate of one client: the IP ban, the IP pace, and one Endpoint per v5 path."""

    def __init__(self) -> None:
        self.eps: Dict[str, Endpoint] = {}
        self.ip = TokenBucket(*IP_RATE)
        self.ip_ban_until = 0.0
        self.lock = threading.Lock()

    def endpoint(self, v5: str) -> Endpoint:
        with self.lock:
            ep = self.eps.get(v5)
            if ep is None:
                rate = ENDPOINTS.get(v5) or (DEFAULT_PUBLIC if v5.startswith("/v5/market") else DEFAULT_PRIVATE)
                ep = self.eps[v5] = Endpoint(*rate)
            return ep

    def acquire(self, v5: str, cost: int = 1, now: Optional[float] = None) -> Tuple[Optional[float], str]:
        """(wait seconds, "") when the call may go after that wait; (None, reason) when it is cooling and must not be sent."""
        now = time.time() if now is None else now
        if now < self.ip_ban_until:
            return None, "ip-ban"
        ep = self.endpoint(v5)
        if now < ep.cool_until:
            return None, "endpoint-cooling"
        wait = max(ep.wait_for(cost, now), self.ip.reserve())
        if wait > MAX_WAIT_S:
            return None, "queue"
        return wait, ""

    def observe(self, v5: str, headers: Dict[str, str], now: Optional[float] = None) -> None:
        self.endpoint(v5).observe(headers, time.time() if now is None else now)

    def trip(self, v5: str, headers: Dict[str, str], now: Optional[float] = None) -> float:
        """A 10006 / 429: the endpoint rests until its reset timestamp (at least 0.25 s, at most a minute). Returns the pause."""
        now = time.time() if now is None else now
        ep = self.endpoint(v5)
        ep.observe(headers, now)
        pause = 1.2
        if ep.reset_at > now:
            pause = min(60.0, max(0.25, ep.reset_at - now + 0.05))
        ep.cool_until = max(ep.cool_until, now + pause)
        return pause

    def ban_ip(self, now: Optional[float] = None, seconds: float = IP_BAN_S) -> float:
        now = time.time() if now is None else now
        self.ip_ban_until = max(self.ip_ban_until, now + seconds)
        return self.ip_ban_until - now


# ---- error classes -----------------------------------------------------------------------------------------------------

AUTH = {10003, 10004, 10005, 10007, 10008, 10010}
RESTRICTED = {10009, 10024, 10027, 10028, 10029}
TRANSIENT = {10000, 10016, 10017, 10019, 110079, 10014, 3400214}
INSUFFICIENT = {110004, 110006, 110007, 110012, 110014, 110044, 110045, 110051, 110052, 110053}
NOT_FOUND = {110001, 110008, 110010, 110019}
FLAT = {110023, 110034, 110005}
PRICE_FILTER = {110003, 110094, 110108, 110109, 110120, 110121}
QTY_FILTER = {110017, 110022, 110064}
LIMITS_HIT = {110016, 110021, 110090}
TRIGGER = {110092, 110093}
CAP = {110009, 110020, 110061}
DUPLICATE = {110072, 110030}
UNCHANGED = {110043, 110025, 110026, 110027, 34040}
LEVERAGE = {110013, 110036, 110038}
LIQUIDATION = {110011, 110040, 110046}


class Verdict:
    """cls: ok | rate | ip-ban | time | auth | restricted | transient | insufficient | flat | not-found | price-filter | qty-filter |
    limit | trigger | cap | duplicate | unchanged | leverage | liquidation | param | permanent.
    retry: the same request may succeed when sent again (after the pause); never true for an auth or a filter error."""

    __slots__ = ("cls", "retry")

    def __init__(self, cls: str, retry: bool = False) -> None:
        self.cls, self.retry = cls, retry

    def __repr__(self) -> str:
        return f"Verdict({self.cls}, retry={self.retry})"


def classify(code: Any, status: int = 200, msg: str = "") -> Verdict:
    low = str(msg or "").lower()
    try:
        c = int(code)
    except (TypeError, ValueError):
        c = -1
    if status == 403 or c == 10018:
        return Verdict("ip-ban")
    if status == 429 or c == 10006 or "too many visits" in low or "rate limit" in low:
        return Verdict("rate", True)
    if c == 0:
        return Verdict("ok")
    if c == 10002 or "recv_window" in low or "timestamp" in low and "window" in low:
        return Verdict("time", True)
    if c in AUTH or status == 401:
        return Verdict("auth")
    if c in RESTRICTED:
        return Verdict("restricted")
    if c in TRANSIENT or status in (500, 502, 503, 504) or c == -1:
        return Verdict("transient", True)
    if c == 10001:
        return Verdict("param")
    if c in INSUFFICIENT or ("insufficient" in low and ("balance" in low or "margin" in low)):
        return Verdict("insufficient")
    if "position is zero" in low or "no position" in low or c in FLAT:
        return Verdict("flat")
    if c in NOT_FOUND or "order not exists" in low or "order does not exist" in low:
        return Verdict("not-found")
    if c in DUPLICATE or ("duplicate" in low and "link" in low):
        return Verdict("duplicate")
    if c in TRIGGER:
        return Verdict("trigger")
    if c in CAP:
        return Verdict("cap")
    if c in UNCHANGED:
        return Verdict("unchanged")
    if c in LEVERAGE:
        return Verdict("leverage")
    if c in LIQUIDATION:
        return Verdict("liquidation")
    if c in QTY_FILTER:
        return Verdict("qty-filter")
    if c in PRICE_FILTER or "notional" in low:
        return Verdict("price-filter")
    if c in LIMITS_HIT:
        return Verdict("limit")
    return Verdict("permanent")


# the words the trader reads in `msg` (pulse_trader.ctrl_err_kind, is_transient_api, the entry error branch)
WORDS = {
    "insufficient": "insufficient margin",
    "flat": "position not exist",
    "not-found": "order not exist",
    "trigger": "trigger price is on the wrong side of the current price",
    "cap": "exceeded the limit of active orders",
    "qty-filter": "order size below the minimum quantity step",
    "price-filter": "order size or price outside the allowed range (current price filter)",
    "duplicate": "order already exists",
    "liquidation": "order would trigger liquidation",
    "auth": "authentication or permission error",
    "restricted": "symbol or account restricted",
    "param": "parameter error",
    "limit": "position or risk limit exceeded (order size)",
    "transient": "temporary exchange error (request frequency or server), retry later",
    "time": "timestamp outside the receive window",
}
AUTH_WORDS = {10003: "api key invalid", 10004: "signature error", 10005: "api key permission denied", 10007: "user authentication failed",
              10008: "account banned", 10010: "ip not allowed for this api key"}
