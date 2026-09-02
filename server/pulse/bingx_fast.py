#!/usr/bin/env python3
"""Fast BingX swap client: pooled HTTP, WS prices, token-bucket limits, batch orders."""
from __future__ import annotations

import gzip
import hmac
import hashlib
import json
import re
import threading
import time
import traceback
import urllib.parse
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

try:
    import orjson

    def dumps(obj: Any) -> str:
        return orjson.dumps(obj).decode()

    def loads(raw: Any) -> Any:
        if isinstance(raw, (bytes, bytearray)):
            return orjson.loads(raw)
        return orjson.loads(raw)
except Exception:
    def dumps(obj: Any) -> str:  # type: ignore
        return json.dumps(obj, separators=(",", ":"))

    def loads(raw: Any) -> Any:  # type: ignore
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        return json.loads(raw)

try:
    import asyncio
except Exception:
    asyncio = None  # type: ignore

try:
    import httpx
except Exception:
    httpx = None  # type: ignore

try:
    import websocket as _ws
except Exception:
    _ws = None

BASE = "https://open-api.bingx.com"
WS_URL = "wss://open-api-swap.bingx.com/swap-market"
RECV = 5000
UA = "grok-x01-pulse/2.0"

# CTS connector numbers (UID / IP)
LIMITS = {
    "public": (12.0, 20.0),
    "private": (5.0, 10.0),
    "order": (2.4, 5.0),
}

RATE_CODES = {100410, 100421, 109421, 109429, 100429, 101209}
SKIP_API_LOG = {110424, 101204, 100421, 101209, 109429}


class TokenBucket:
    def __init__(self, rate: float, burst: float) -> None:
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.ts = time.monotonic()
        self.lock = threading.Lock()

    def take(self) -> float:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.ts) * self.rate)
            self.ts = now
            if self.tokens >= 1:
                self.tokens -= 1
                return 0.0
            wait = (1.0 - self.tokens) / self.rate
            self.tokens = 0.0
            self.ts = now + wait
        if wait > 0:
            time.sleep(wait)
        return wait


class ErrorLog:
    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.ring: Deque[Dict[str, Any]] = deque(maxlen=80)
        self.n = 0
        self._last: Dict[str, float] = {}

    def write(self, kind: str, **kw: Any) -> None:
        rec = {"t": round(time.time(), 3), "kind": kind}
        rec.update(kw)
        self.ring.appendleft(rec)
        self.n += 1
        if kind in ("api", "rate-limit", "ws-session"):
            key = kind + str(kw.get("code") or kw.get("msg") or "")[:48]
            now = time.time()
            if now - self._last.get(key, 0.0) < 8.0:
                return
            self._last[key] = now
        line = dumps(rec) + "\n"
        with self.lock:
            try:
                with open(self.path, "a") as f:
                    f.write(line)
                if self.n % 80 == 0:
                    self._rotate()
            except Exception:
                pass

    def _rotate(self) -> None:
        try:
            import os
            if os.path.getsize(self.path) < 120_000:
                return
            with open(self.path, "rb") as f:
                f.seek(-min(160_000, os.path.getsize(self.path)), 2)
                f.readline()
                tail = f.read()
            tmp = self.path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(tail)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def recent(self, n: int = 12) -> List[Dict[str, Any]]:
        return list(self.ring)[:n]


class PriceHub:
    def __init__(self, on_px: Callable[[str, float], None], err: ErrorLog, ws_url: str = WS_URL) -> None:
        self.on_px = on_px
        self.err = err
        self.ws_url = ws_url
        self.symbols: List[str] = []
        self.stop = False
        self.ok = False
        self.last_msg = 0.0
        self.n = 0
        self.thread: Optional[threading.Thread] = None

    def start(self, symbols: List[str]) -> None:
        self.symbols = list(symbols)
        if _ws is None:
            self.err.write("ws", msg="websocket-client missing")
            return
        self.stop = False
        self.thread = threading.Thread(target=self._run, name="bx-ws", daemon=True)
        self.thread.start()

    def set_symbols(self, symbols: List[str]) -> None:
        self.symbols = list(symbols)

    def _run(self) -> None:
        while not self.stop:
            try:
                self._session()
            except Exception as e:
                self.ok = False
                self.err.write("ws-session", msg=str(e)[:240])
            time.sleep(1.2)

    def _session(self) -> None:
        ws = _ws.create_connection(
            self.ws_url,
            timeout=8,
            header=[f"User-Agent: {UA}"],
            enable_multithread=True,
        )
        self.ok = True
        for i, s in enumerate(self.symbols):
            ws.send(dumps({"id": f"{i}-t", "reqType": "sub", "dataType": f"{s}@ticker"}))
            if i and i % 80 == 0:
                time.sleep(0.04)
        ws.settimeout(15)
        while not self.stop:
            raw = ws.recv()
            if raw is None:
                break
            if isinstance(raw, (bytes, bytearray)) and raw[:2] != b"\x1f\x8b":
                try:
                    txt = raw.decode("utf-8", "ignore")
                except Exception:
                    txt = ""
            elif isinstance(raw, str):
                txt = raw
            else:
                txt = ""
            if txt.lower() in ("ping", "pong"):
                try:
                    ws.send("Pong")
                except Exception:
                    break
                self.last_msg = time.time()
                continue
            data = _decode_ws(raw)
            if data is None:
                continue
            if isinstance(data, dict) and data.get("ping") is not None:
                try:
                    ping = data.get("ping")
                    ws.send(dumps({"pong": ping if ping not in (True, False) else int(time.time() * 1000)}))
                except Exception:
                    break
                self.last_msg = time.time()
                continue
            self.last_msg = time.time()
            self.n += 1
            _apply_px(data, self.on_px)
        try:
            ws.close()
        except Exception:
            pass
        self.ok = False


def _decode_ws(raw: Any) -> Optional[Any]:
    try:
        if isinstance(raw, bytes):
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            raw = raw.decode("utf-8", "ignore")
        if not raw or raw == "Ping" or raw == "ping":
            return {"ping": True}
        return loads(raw)
    except Exception:
        return None


def _apply_px(data: Any, on_px: Callable[[str, float], None]) -> None:
    if not isinstance(data, dict):
        return
    if data.get("ping") or data.get("ping") is True:
        return
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(payload, dict):
        if isinstance(data.get("data"), list) and data["data"]:
            payload = data["data"][0] if isinstance(data["data"][0], dict) else {}
        else:
            payload = {}
    sym = payload.get("s") or payload.get("symbol") or data.get("s") or ""
    dtype = str(data.get("dataType") or data.get("e") or "")
    if not sym and "@" in dtype:
        sym = dtype.split("@", 1)[0]
    px = payload.get("c") or payload.get("p") or payload.get("lastPrice") or payload.get("markPrice") or payload.get("lp")
    try:
        f = float(px or 0)
    except Exception:
        f = 0.0
    if sym and f > 0:
        on_px(str(sym), f)


class FastBingX:
    def __init__(self, key: str, secret: str, err: ErrorLog, base: str = BASE, ws_url: str = WS_URL) -> None:
        self.key = key
        self.secret = secret
        self.err = err
        self.base = (base or BASE).rstrip("/")
        self.buckets = {k: TokenBucket(*v) for k, v in LIMITS.items()}
        self.cooldown_until = 0.0
        self.path_cd: Dict[str, float] = {}
        self.http = None
        import urllib.request
        self._opener = urllib.request.build_opener(urllib.request.HTTPHandler())
        if httpx is not None:
            self.http = httpx.Client(
                base_url=self.base,
                timeout=httpx.Timeout(2.0, connect=1.0),
                headers={"User-Agent": UA, "X-BX-APIKEY": key},
                http2=False,
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16, keepalive_expiry=30),
            )
        self.px: Dict[str, float] = {}
        self.chg: Dict[str, float] = {}
        self.hub = PriceHub(self._on_px, err, ws_url=ws_url)
        self.on_event = None
        self.stats = {"rest": 0, "ws": 0, "wait": 0.0, "rl": 0, "err": 0, "asyncN": 0, "asyncP50": 0.0}
        self.bridge = AsyncBridge(self.base, {"User-Agent": UA}, err)
        self._ts_lock = threading.Lock()
        self._last_ts = 0

    def _next_ts(self) -> int:
        """BingX rejects bursts that reuse the same millisecond timestamp."""
        with self._ts_lock:
            t = int(time.time() * 1000)
            if t <= self._last_ts:
                t = self._last_ts + 1
            self._last_ts = t
            return t

    def start_ws(self, symbols: List[str]) -> None:
        self.hub.start(symbols)

    def _on_px(self, symbol: str, px: float) -> None:
        self.px[symbol] = px
        self.stats["ws"] += 1
        cb = getattr(self, "on_event", None)
        if cb:
            try:
                cb("tick")
            except Exception:
                pass

    def _lane(self, path: str, method: str) -> str:
        if "/trade/order" in path or "/trade/batchOrders" in path or "/trade/closePosition" in path:
            return "order"
        if path.startswith("/openApi/swap") and method != "PUBLIC":
            if "/quote/" in path:
                return "public"
            return "private"
        return "public"

    def _sign(self, params: Dict[str, Any]) -> str:
        items = sorted((k, params[k]) for k in params)
        # BingX verifies the HMAC over the RAW (unencoded) query string.
        # For plain alnum values raw == encoded, but the batchOrders JSON
        # payload differs once percent-encoded and used to fail with 100001.
        raw = "&".join(f"{k}={v}" for k, v in items)
        qs = urllib.parse.urlencode(items, quote_via=urllib.parse.quote)
        sig = hmac.new(self.secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        return qs + "&signature=" + sig

    def _take(self, lane: str, path: str = "") -> bool:
        now = time.time()
        until = self.path_cd.get(path, 0.0)
        gate = until
        if lane == "order":
            gate = max(self.cooldown_until, until)
        if now < gate:
            return False
        w = self.buckets[lane].take()
        self.stats["wait"] += w
        return True

    def _trip(self, path: str, body: Dict[str, Any]) -> None:
        code = body.get("code")
        msg = str(body.get("msg") or "")
        if code not in RATE_CODES and "rate limit" not in msg.lower() and "100410" not in msg and "frequency limit" not in msg.lower():
            return
        self.stats["rl"] += 1
        wait = 1.2
        m = re.search(r"(?:unblocked after|retry after time:\s*)(\d{10,})", msg)
        if m:
            raw = int(m.group(1))
            until = raw / 1000.0 if raw > 10_000_000_000 else float(raw)
            wait = max(0.8, min(900.0, until - time.time() + 0.4))
            self.path_cd[path] = time.time() + wait
        else:
            self.path_cd[path] = time.time() + 8.0
        self.cooldown_until = max(self.cooldown_until, time.time() + min(wait, 12.0))
        self.err.write("rate-limit", path=path, code=code, msg=msg[:180], wait=round(wait, 2))

    def _req(self, method: str, path: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        lane = self._lane(path, method)
        if not self._take(lane, path):
            return {"code": 101209, "msg": "cooling", "error": True, "cooled": True}
        params: Dict[str, Any] = {"timestamp": str(self._next_ts()), "recvWindow": str(RECV)}
        if extra:
            for k, v in extra.items():
                if v is None:
                    continue
                params[k] = v if isinstance(v, str) else str(v)
        qs = self._sign(params)
        url = f"{path}?{qs}"
        self.stats["rest"] += 1
        try:
            body = self._http(method, url)
        except Exception as e:
            self.stats["err"] += 1
            self.err.write("http", method=method, path=path, msg=str(e)[:220])
            return {"code": -1, "msg": str(e)[:400], "error": True}
        if isinstance(body, dict) and body.get("code") not in (0, None):
            if body.get("code") not in (100404, 109400, 100001, *SKIP_API_LOG) or "signature" in str(body.get("msg") or "").lower():
                if body.get("code") not in (109400, 100404, *SKIP_API_LOG):
                    self.err.write("api", method=method, path=path, code=body.get("code"), msg=str(body.get("msg"))[:220])
            self._trip(path, body)
        return body if isinstance(body, dict) else {"code": -1, "msg": "bad-json", "error": True}

    def _http(self, method: str, url: str) -> Dict[str, Any]:
        # Always urllib for signed query strings. httpx re-encodes `?` params and
        # BingX then reports "signature mismatch" on burst entries with attach JSON.
        import urllib.request
        import urllib.error
        full = url if url.startswith("http") else self.base + url
        req = urllib.request.Request(
            full,
            method=method,
            data=None if method in ("GET", "DELETE") else b"",
            headers={"X-BX-APIKEY": self.key, "User-Agent": UA},
        )
        try:
            with self._opener.open(req, timeout=5) as resp:
                return loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                return loads(e.read())
            except Exception:
                return {"code": e.code, "msg": str(e)[:400], "error": True}
        except Exception as e:
            return {"code": -1, "msg": str(e)[:400], "error": True}

    def get(self, path: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._req("GET", path, extra)

    def post(self, path: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._req("POST", path, extra)

    def delete(self, path: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._req("DELETE", path, extra)

    def public(self, path: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._take("public", path)
        qs = urllib.parse.urlencode(extra or {})
        url = path + (f"?{qs}" if qs else "")
        self.stats["rest"] += 1
        try:
            body = self._http("GET", url)
        except Exception as e:
            self.stats["err"] += 1
            self.err.write("public", path=path, msg=str(e)[:220])
            return {"code": -1, "msg": str(e)[:400], "error": True}
        return body if isinstance(body, dict) else {"code": -1, "msg": "bad-json", "error": True}

    def batch_place(self, orders: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not orders:
            return {"code": 0, "data": {"orders": []}}
        chunk = orders[:5]
        return self.post("/openApi/swap/v2/trade/batchOrders", {"batchOrders": dumps(chunk)})

    def cancel_replace(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.post("/openApi/swap/v2/trade/order/cancelReplace", extra or {})

    def gather_public(self, reqs: List[Tuple[str, Dict[str, Any]]], timeout: float = 4.2) -> List[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
        self.stats["rest"] += len(reqs)
        self.stats["asyncN"] += len(reqs)
        rows = self.bridge.gather(reqs, timeout=timeout)
        snap = self.bridge.latency()
        if snap:
            self.stats["asyncP50"] = snap
        return rows

    def snapshot(self) -> Dict[str, Any]:
        return {
            "rest": self.stats["rest"],
            "ws": self.stats["ws"],
            "wsOk": bool(self.hub.ok and time.time() - self.hub.last_msg < 8),
            "wsAgeMs": round((time.time() - self.hub.last_msg) * 1000) if self.hub.last_msg else None,
            "rateWaits": round(self.stats["wait"], 3),
            "rateTrips": self.stats["rl"],
            "httpErr": self.stats["err"],
            "asyncN": self.stats.get("asyncN", 0),
            "asyncP50": round(self.stats.get("asyncP50", 0.0), 1),
            "errors": self.err.recent(8),
            "errorN": self.err.n,
        }


class AsyncBridge:
    """Dedicated asyncio loop for parallel public GETs. Orders stay on the sync client."""

    def __init__(self, base: str, headers: Dict[str, str], err: ErrorLog) -> None:
        self.base = base.rstrip("/")
        self.headers = headers
        self.err = err
        self.lat: Deque[float] = deque(maxlen=48)
        self.ok = False
        self.loop = None
        self.client = None
        if asyncio is None or httpx is None:
            return
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        t = threading.Thread(target=self._run, name="bx-async", daemon=True)
        t.start()
        self.ready.wait(2.5)

    def _run(self) -> None:
        assert asyncio is not None and httpx is not None and self.loop is not None
        asyncio.set_event_loop(self.loop)
        self.client = httpx.AsyncClient(
            base_url=self.base,
            timeout=httpx.Timeout(3.0, connect=1.4),
            headers=self.headers,
            http2=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=20),
        )
        self.ok = True
        self.ready.set()
        self.loop.run_forever()

    def latency(self) -> float:
        if not self.lat:
            return 0.0
        xs = sorted(self.lat)
        return xs[len(xs) // 2]

    def gather(self, reqs: List[Tuple[str, Dict[str, Any]]], timeout: float = 4.2) -> List[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
        if not reqs:
            return []
        if not self.ok or self.loop is None:
            return [(p, e, {"error": True, "msg": "async-offline"}) for p, e in reqs]
        fut = asyncio.run_coroutine_threadsafe(self._gather(reqs), self.loop)
        try:
            return fut.result(timeout)
        except Exception as e:
            self.err.write("async-gather", msg=str(e)[:200])
            return [(p, e2, {"error": True, "msg": str(e)[:180]}) for p, e2 in reqs]

    async def _gather(self, reqs: List[Tuple[str, Dict[str, Any]]]):
        sem = asyncio.Semaphore(8)

        async def one(path: str, extra: Dict[str, Any]):
            async with sem:
                qs = urllib.parse.urlencode(extra or {})
                url = path + (("?" + qs) if qs else "")
                t0 = time.perf_counter()
                try:
                    r = await self.client.get(url)
                    body = loads(r.content) if r.content else {"code": r.status_code, "error": True}
                except Exception as e:
                    return path, extra, {"error": True, "msg": str(e)[:180]}
                self.lat.append((time.perf_counter() - t0) * 1000)
                return path, extra, body if isinstance(body, dict) else {"error": True, "msg": "bad-json"}

        return await asyncio.gather(*[one(p, e) for p, e in reqs])
