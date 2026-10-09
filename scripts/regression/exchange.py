"""Exchange client suite: rate limits on the sync and async paths, with no network.

The client is built without its constructor (no websockets, no threads): each check sets the few attributes the
method reads, and replaces the HTTP call with a counting stub. Async behaviour runs on a real event loop.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

from common import Skip  # noqa: F401
from bingx_fast import AsyncBridge, ErrorLog, FastBingX, TokenBucket

PATH = "/openApi/swap/v2/quote/ticker"


def _client(err_path):
    c = FastBingX.__new__(FastBingX)
    c.buckets = {"public": TokenBucket(1000.0, 1000.0), "order": TokenBucket(1000.0, 1000.0),
                 "private": TokenBucket(1000.0, 1000.0)}
    c.path_cd = {}
    c.cooldown_until = 0.0
    c.stats = {"rest": 0, "err": 0, "wait": 0.0, "rl": 0}
    c.err = ErrorLog(err_path)
    c.base = "https://example.invalid"
    return c


def _rate_limited(minutes=1.0):
    until_ms = int((time.time() + 60 * minutes) * 1000)
    return {"code": 100410, "msg": f"rate limit, unblocked after {until_ms}"}


def public_cooldown_blocks_http():
    """A rate-limit reply on a public GET starts a path cooldown; the next call sends no request."""
    with tempfile.TemporaryDirectory() as td:
        c = _client(os.path.join(td, "err.log"))
        calls = []
        c._http = lambda method, url: (calls.append(url), _rate_limited())[1]
        first = c.public(PATH)
        second = c.public(PATH)
    ok = len(calls) == 1 and second.get("cooled") is True and first.get("code") == 100410
    return ok, f"http_calls={len(calls)} second_cooled={second.get('cooled')}"


def public_ok_path_is_not_blocked():
    """A healthy public GET is sent every time: the cooldown only follows a rate-limit reply."""
    with tempfile.TemporaryDirectory() as td:
        c = _client(os.path.join(td, "err.log"))
        calls = []
        c._http = lambda method, url: (calls.append(url), {"code": 0, "data": {}})[1]
        for _ in range(3):
            c.public(PATH)
    return len(calls) == 3, f"http_calls={len(calls)} of 3"


def token_bucket_reserve_never_sleeps():
    """reserve() returns the wait and does not block: the event loop depends on that."""
    bucket = TokenBucket(rate=1.0, burst=1.0)
    first = bucket.reserve()
    t0 = time.perf_counter()
    second = bucket.reserve()
    elapsed = time.perf_counter() - t0
    ok = first == 0.0 and second > 0.0 and elapsed < 0.05
    return ok, f"first={first} second={second:.3f} call_seconds={elapsed:.4f}"


class _Resp:
    def __init__(self, body, status=200):
        self.content = body
        self.status_code = status


class _StubClient:
    """Async client stub: every get() is counted and answered with a fixed body."""

    def __init__(self, body):
        self.body = body
        self.calls = []

    async def get(self, url):
        self.calls.append(url)
        return _Resp(self.body)


def _bridge(gate, client):
    b = AsyncBridge.__new__(AsyncBridge)
    b.base = "https://example.invalid"
    b.headers = {}
    b.err = gate.err
    b.gate = gate
    b.lat = __import__("collections").deque(maxlen=48)
    b.client = client
    b.ok = True
    b.loop = None
    return b


def async_bridge_honours_cooldown_and_trips():
    """On the event loop: a rate-limit reply trips the path, and the next gather sends nothing for that path."""
    import json
    with tempfile.TemporaryDirectory() as td:
        c = _client(os.path.join(td, "err.log"))
        limited = json.dumps(_rate_limited()).encode()
        client = _StubClient(limited)
        bridge = _bridge(c, client)
        first = asyncio.run(bridge._gather([(PATH, {})]))
        after_first = len(client.calls)
        second = asyncio.run(bridge._gather([(PATH, {})]))
    ok = (after_first == 1 and len(client.calls) == 1 and first[0][2].get("code") == 100410
          and second[0][2].get("cooled") is True and c.path_cd.get(PATH, 0) > time.time())
    return ok, f"requests={len(client.calls)} cooled_reply={second[0][2].get('cooled')}"


def async_bridge_waits_on_the_loop_for_the_bucket():
    """With an empty bucket the gather waits with asyncio.sleep: a concurrent task still runs during the wait."""
    import json
    with tempfile.TemporaryDirectory() as td:
        c = _client(os.path.join(td, "err.log"))
        c.buckets["public"] = TokenBucket(rate=20.0, burst=1.0)
        client = _StubClient(json.dumps({"code": 0, "data": {}}).encode())
        bridge = _bridge(c, client)

        async def run():
            ticks = []

            async def ticker():
                for _ in range(3):
                    await asyncio.sleep(0.01)
                    ticks.append(1)
            t = asyncio.create_task(ticker())
            out = await bridge._gather([(PATH, {}), (PATH, {"n": 2})])
            await t
            return out, len(ticks)

        out, ticks = asyncio.run(run())
    ok = len(out) == 2 and len(client.calls) == 2 and ticks == 3
    return ok, f"requests={len(client.calls)} ticks_during_gather={ticks}"


CHECKS = [
    ("exchange.public-cooldown-blocks-http", public_cooldown_blocks_http),
    ("exchange.public-ok-path-not-blocked", public_ok_path_is_not_blocked),
    ("exchange.token-bucket-reserve-never-sleeps", token_bucket_reserve_never_sleeps),
    ("exchange.async-bridge-honours-cooldown", async_bridge_honours_cooldown_and_trips),
    ("exchange.async-bridge-waits-on-loop", async_bridge_waits_on_the_loop_for_the_bucket),
]


if __name__ == "__main__":
    bad = 0
    for name, fn in CHECKS:
        ok, detail = fn()
        bad += 0 if ok else 1
        print(("PASS " if ok else "FAIL ") + name, detail)
    raise SystemExit(1 if bad else 0)
