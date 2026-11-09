"""Offline endpoint identity checks: demo prices and orders belong to one venue."""
import os
import tempfile
import asyncio
import threading
from types import SimpleNamespace
import time
from unittest.mock import patch

import common  # noqa: F401
import bingx_fast
import pulse_trader
from conn_guard import ConnGuardError

LIVE = "https://open-api.bingx.com"
DEMO = "https://open-api-vst.bingx.com"
LIVE_WS = "wss://open-api-swap.bingx.com/swap-market"
DEMO_WS = "wss://vst-open-api-ws.bingx.com/swap-market"


def constructor_selects_matching_prices():
    observed = []
    with tempfile.TemporaryDirectory() as td, patch.object(bingx_fast, "httpx", None):
        for base, ws in ((DEMO, DEMO_WS), (LIVE, LIVE_WS)):
            client = bingx_fast.FastBingX("fixture", "fixture", bingx_fast.ErrorLog(os.path.join(td, "error")), base=base)
            observed.append((client.base, client.hub.ws_url))
            if client.base != base or client.hub.ws_url != ws:
                return False, str(observed)
    return True, str(observed)


def constructor_refuses_mismatched_price_feed():
    rejected = 0
    with tempfile.TemporaryDirectory() as td, patch.object(bingx_fast, "httpx", None):
        for base, ws in ((DEMO, LIVE_WS), (LIVE, DEMO_WS), (DEMO, "wss://example.invalid/swap-market")):
            try:
                bingx_fast.FastBingX("fixture", "fixture", bingx_fast.ErrorLog(os.path.join(td, "error")), base=base, ws_url=ws)
            except ConnGuardError:
                rejected += 1
    return rejected == 3, f"refused={rejected}/3"


def factory_checks_connection_before_constructing_client():
    rejected = 0
    with patch.object(pulse_trader, "FastBingX") as factory:
        for conn, base in (("bingx-x02", LIVE), ("bingx-x01", DEMO), ("bingx-x09", DEMO)):
            try:
                pulse_trader.make_api(conn, "fixture", "fixture", base)
            except ConnGuardError:
                rejected += 1
        made = factory.call_count
    return rejected == 3 and made == 0, f"refused={rejected}/3 constructed={made}"


def expired_gather_cancels_its_background_work():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    cancelled = threading.Event()
    bridge = bingx_fast.AsyncBridge.__new__(bingx_fast.AsyncBridge)
    bridge.ok, bridge.loop = True, loop
    bridge.err = bingx_fast.ErrorLog(os.devnull)
    async def pending(_reqs):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()
    bridge._gather = pending
    thread.start()
    try:
        result = bridge.gather([('/quote', {})], timeout=.02)
        stopped = cancelled.wait(.3)
        return stopped and result[0][2].get('error') is True, f"cancelled={stopped}"
    finally:
        async def cleanup():
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(cleanup(), loop).result(1)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(1)
        loop.close()


def fresh_prices_do_not_prevent_universe_refresh():
    symbols = [f"S{i}-USDT" for i in range(12)]
    calls = []
    trader = pulse_trader.Pulse.__new__(pulse_trader.Pulse)
    trader.api = SimpleNamespace(hub=SimpleNamespace(ok=True, last_msg=time.time()),
        public=lambda path: (calls.append(path), {'code':0,'data':[]})[1])
    trader.px = dict.fromkeys(symbols, 100.)
    trader.ingest_ws_px = lambda: len(symbols)
    trader.universe, trader.last_uni = [], 0
    with patch.object(pulse_trader, "SYMBOLS", symbols):
        trader.refresh_tickers()
        first = len(calls)
        trader.universe, trader.last_uni = [{'symbol': s} for s in symbols], time.time()
        trader.refresh_tickers()
    return first == 1 and len(calls) == 1, f"bootstrap={first}, fresh-ranked={len(calls)-first} requests"


CHECKS = [
    ("exchange.demo-client-prices-match-rest-environment", constructor_selects_matching_prices),
    ("exchange.demo-client-rejects-mismatched-websocket", constructor_refuses_mismatched_price_feed),
    ("exchange.connection-factory-rejects-cross-environment", factory_checks_connection_before_constructing_client),
    ("exchange.expired-gather-cancels-background-requests", expired_gather_cancels_its_background_work),
    ("exchange.fresh-prices-still-initialize-ranked-universe", fresh_prices_do_not_prevent_universe_refresh),
]
