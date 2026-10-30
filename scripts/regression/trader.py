"""Trader suite: the pinned pulse_trader's pacing, leverage, readiness and watchdog, without an exchange.

The module imports with no network. Methods run on a bare instance (no __init__), so no request leaves the process.
These checks read the working file, so they cover the same code the pin covers.
"""
from __future__ import annotations

import ast
import os
import tempfile
import types

from common import PULSE, Skip  # noqa: F401
import pulse_trader as pt

# Every state file the trader writes or reads goes to a private directory: a check must never touch (or read back)
# the live engine's state in the pulse directory.
_STATE_DIR = tempfile.mkdtemp(prefix="ctsg-trader-test-")
for _name in ("STATS_PATH", "TRADES_PATH", "STOP_PATH", "PAUSE_PATH", "LOG_PATH", "BLOCK_PATH", "OVERLAY_PATH",
              "OPEN_PATH", "CTS_PATH", "ERR_PATH", "LEV_PATH", "START_EQ_PATH", "RESET_EQ_PATH", "UNIVERSE_PATH"):
    if hasattr(pt, _name):
        setattr(pt, _name, os.path.join(_STATE_DIR, os.path.basename(getattr(pt, _name))))


def _offline_contracts(want=None):
    """Tests never reach the venue: the contract list is a fixed, offline one (the requested names, else two)."""
    names = sorted(want) if want else ["BTC-USDT", "ETH-USDT"]
    return {s: pt.Contract(symbol=s, min_qty=0.001, step=0.001, qprec=3, pprec=6, min_usdt=5.0, max_lev=50) for s in names}


pt.load_contracts = _offline_contracts

SRC = os.path.join(PULSE, "pulse_trader.py")


def _bare_pulse():
    """A Pulse with only the attributes the checks read. Methods are unbound from __init__, so nothing connects."""
    p = pt.Pulse.__new__(pt.Pulse)
    p.use_max_leverage = True
    p.lev_max = {}
    p.lev_map = {}
    p.klines_tf = {"1m": {}, "5m": {}, "15m": {}}
    p.klines = p.klines_tf["1m"]
    p.bar_min = {}
    p.real_1m = {}
    p.price_ts = {}
    p.last_bal = pt.time.time()
    return p


def stale_or_failed_balance_blocks_entries():
    """Old, invalid and failed balance reads cannot fund entries; a valid zero remains zero."""
    p = _bare_pulse()
    p.ctrl_skip = {}
    p.boot_ts = 0
    p.control_orders = False
    p.open = {}
    p.errors = 0
    p.available = 5.0
    p.start_eq = p.session_eq = 10.0
    p.dd_halt = 0.0
    p.halted = False
    p.halt_reason = None
    p.last_bal = pt.time.time() - pt.BALANCE_STALE_S - 1
    stale = p.entries_blocked()
    p.last_bal = pt.time.time()
    fresh = not p.entries_blocked()
    p.recon_ok = False
    bad_recon = p.entries_blocked()
    p.recon_ok = True
    class Api:
        def __init__(self, reply): self.reply = reply
        def get(self, path): return self.reply
    p.api = Api({"code": -1, "msg": "timeout", "data": {"availableMargin": 5}})
    p.refresh_balance()
    failed_zero = p.available == 0
    p.available = 5.0
    p.api = Api({"code": 0, "data": {"balance": {"equity": "10", "availableMargin": "NaN"}}})
    p.refresh_balance()
    invalid_zero = p.available == 0
    p.api = Api({"code": 0, "data": {"balance": {"equity": "10", "availableMargin": 0, "availableBalance": 9}}})
    p.refresh_balance()
    explicit_zero = p.available == 0
    p.api = Api({"code": 0, "data": {"balance": {"equity": "10", "availableMargin": "3"}}})
    p.refresh_balance()
    recovered = p.available == 3 and not p.entries_blocked()
    ok = stale and fresh and bad_recon and failed_zero and invalid_zero and explicit_zero and recovered
    return ok, f"stale={stale} fresh={fresh} failed reconciliation={bad_recon} failed={failed_zero} invalid={invalid_zero} zero={explicit_zero} recovered={recovered}"


def websocket_last_price_follows_declines():
    """An old high must not make a legal short stop appear illegal after the market falls."""
    p = _bare_pulse()
    p.api = types.SimpleNamespace(px={"A-USDT": 100.0}, px_ts={"A-USDT": pt.time.time()})
    p.px, p.last_px = {}, {}
    old = pt.SYMBOLS
    try:
        pt.SYMBOLS = ["A-USDT"]
        p.ingest_ws_px()
        p.api.px["A-USDT"] = 90.0
        p.api.px_ts["A-USDT"] = pt.time.time()
        p.ingest_ws_px()
        p.api.px["A-USDT"] = float("inf")
        p.api.px_ts["A-USDT"] = pt.time.time()
        p.ingest_ws_px()
        band = p.px_band("A-USDT")
        pos = pt.Position(symbol="A-USDT", side="SHORT", qty=1, entry=90, opened_at=1, sl=91, tp=89, peak=90)
        legal = p.sl_legal(pos, 91.0)
    finally:
        pt.SYMBOLS = old
    return band == (90.0, 90.0, 90.0, 90.0) and legal, f"price band={band}; short stop legal={legal}"


def ticker_fallback_is_nonblocking_and_preserves_newer_tick():
    """A slow REST ticker cannot hold the cycle or overwrite a newer WebSocket price."""
    import concurrent.futures
    import threading
    p = _bare_pulse()
    started, release = threading.Event(), threading.Event()
    class Api:
        def __init__(self):
            self.px, self.px_ts = {}, {}
            self.hub = types.SimpleNamespace(ok=False, last_msg=0)
            self.calls = 0
        def public(self, path):
            self.calls += 1
            started.set()
            release.wait(2)
            return {"code": 0, "data": [{"symbol": "A-USDT", "lastPrice": "90"}]}
    p.api = Api()
    p.px, p.last_px = {}, {}
    p.last_uni, p.symbol_sort = pt.time.time(), "vol1h"
    p._ticker_future, p._ticker_started = None, 0
    p._ticker_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    old = pt.SYMBOLS
    try:
        pt.SYMBOLS = ["A-USDT"]
        t = pt.time.monotonic()
        p.refresh_tickers(background=True)
        elapsed = pt.time.monotonic() - t
        started.wait(1)
        p.refresh_tickers(background=True)
        p.api.px["A-USDT"] = 100
        p.api.px_ts["A-USDT"] = pt.time.time()
        p.ingest_ws_px()
        release.set()
        p._ticker_future.result(timeout=2)
        p.refresh_tickers(background=True)
        latest = p.px.get("A-USDT")
        fresh = p.market_price_fresh("A-USDT")
        p.price_ts["A-USDT"] = pt.time.time() - pt.PRICE_STALE_S - 1
        stale = not p.market_price_fresh("A-USDT")
        calls = p.api.calls
    finally:
        release.set()
        p._ticker_pool.shutdown(wait=True)
        pt.SYMBOLS = old
    ok = elapsed < 0.1 and calls == 1 and latest == 100 and fresh and stale
    return ok, f"cycle wait={elapsed*1000:.2f}ms requests={calls} newer price={latest} fresh={fresh} stale={stale}"


def entry_rechecks_price_after_network_delay():
    """A price or balance that becomes invalid during a network check cannot create an entry order."""
    def run(kind):
        sym = "BTC-USDT"
        venue = _StubExchange(executed="1")
        contracts = {sym: pt.Contract(symbol=sym, min_qty=0.001, step=0.001, qprec=3, pprec=2, min_usdt=5, max_lev=50)}
        p = pt.Pulse(venue, contracts)
        p.boot_ts = 0
        p.last_bal = pt.time.time()
        p.available = 50
        p.px[sym] = 100
        p.price_ts[sym] = pt.time.time()
        p.halted = False
        p.entry_sense = lambda *a, **k: None
        p.sets.use_historic_gate = False
        def slow_leverage(_sym, force=False):
            if kind == "price":
                p.price_ts[sym] = pt.time.time() - pt.PRICE_STALE_S - 1
            else:
                p.available = 0
            return p.leverage_for(contracts[sym])
        p.ensure_max_leverage = slow_leverage
        p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
        return len([x for path, x in venue.posts if path.endswith("/trade/order")]), len(p.open)
    price = _isolated(lambda: run("price"))
    balance = _isolated(lambda: run("balance"))
    return price == (0, 0) and balance == (0, 0), f"(orders, positions) after stale price={price}, failed balance={balance}"


def cycles_keep_managing_when_ticker_read_is_slow():
    """A blocked fallback read must not hold subsequent cycles or suppress active-position management."""
    import concurrent.futures
    import threading
    p = _bare_pulse()
    release = threading.Event()
    class Api:
        px, px_ts = {}, {}
        hub = types.SimpleNamespace(ok=False, last_msg=0)
        def public(self, path):
            release.wait(2)
            return {"code": 0, "data": []}
    p.api, p.px, p.last_px = Api(), {}, {}
    p._ticker_future, p._ticker_started = None, 0
    p._ticker_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    p.last_uni, p.halt_reason, p.halted, p.cycle = pt.time.time(), None, False, 0
    p._budget = p.seed_px_bars = p.maybe_reload_config = lambda: None
    p.load = types.SimpleNamespace(last_budget=types.SimpleNamespace(level="normal"))
    p.adopt_exchange_positions = p.sync_own_fills = p.qa_tick = lambda: None
    p.maybe_entries = p.maybe_block_adds = p.maybe_dca_adds = lambda: None
    p.trim_caches = lambda **kw: None
    p.priority_controls = lambda: 0
    calls = []
    p.manage = lambda: calls.append(1)
    old = pt.SYMBOLS
    try:
        pt.SYMBOLS = [f"S{i}-USDT" for i in range(30)]
        times = []
        for _ in range(40):
            start = pt.time.perf_counter()
            p._one_cycle()
            times.append((pt.time.perf_counter() - start) * 1000)
    finally:
        release.set()
        p._ticker_pool.shutdown(wait=True)
        pt.SYMBOLS = old
    ok = len(calls) == 40 and max(times) < 100
    return ok, f"management calls={len(calls)} max cycle={max(times):.2f}ms with blocked ticker"


def faster_scan_keeps_periodic_work_frequency():
    """A 100 ms scan must not double reconciliation, fill, or configuration polling."""
    periods = (1.0, 1.6, 2.4, 5.0, 30.0, 44.0)
    pairs = [(pt.cycle_stride(s, 0.1) * 0.1, pt.cycle_stride(s, 0.2) * 0.2) for s in periods]
    return all(abs(a - b) < 1e-9 for a, b in pairs), f"period seconds at 100/200ms={pairs}"


def slow_warm_feed_does_not_block_balance():
    """A blocked market-data worker must not stop balance refresh or cause rapid retry storms."""
    import threading
    p = _bare_pulse()
    entered, release = threading.Event(), threading.Event()
    p._warm_stop, p._balance_next_at, p.last_bal = False, 0, 0
    p.available, p.errors, p.last_error = 5.0, 0, ""
    p.api = types.SimpleNamespace()
    p.load = types.SimpleNamespace(last_budget=types.SimpleNamespace(warm_s=.01))
    def slow_fees():
        entered.set()
        release.wait(2)
    p.refresh_fees = slow_fees
    p.refresh_klines = p.refresh_vol1h = p.process_indications = p.update_regime = lambda: None
    calls = []
    def balance():
        calls.append(1)
        p.last_bal = pt.time.time()
        p.available = 3
    p.refresh_balance = balance
    t = threading.Thread(target=p._warm_loop)
    t.start()
    try:
        entered.wait(1)
        start = pt.time.monotonic()
        p._balance_tick()
        elapsed = pt.time.monotonic() - start
        p._balance_tick()
        once = len(calls) == 1 and p.available == 3
        p.last_bal, p._balance_next_at, p.available = 0, 0, 9
        def fail(): raise RuntimeError("offline")
        p.refresh_balance = fail
        p._balance_tick()
        failed_safe = p.available == 0 and p.errors == 1
    finally:
        p._warm_stop = True
        release.set()
        t.join(2)
    ok = elapsed < .1 and once and failed_safe and not t.is_alive()
    return ok, f"balance tick={elapsed*1000:.2f}ms; no duplicate={once}; failed-safe={failed_safe}"


def extra_source_switch_controls_all_external_reads():
    """Turning off extra sources prevents both prefetch and per-symbol external reads."""
    from indication_engine import EXTRA, IndicationBook
    p = _bare_pulse()
    p.indications = IndicationBook()
    p._budget = lambda: types.SimpleNamespace(extra_n=1, extra_sources=True, scan_chunk=30)
    p.open, p.universe, p.px = {}, [], {"A-USDT": 100.0}
    p.load = types.SimpleNamespace(cursor_ind=0, scan_window=lambda *a: (["A-USDT"], 0))
    bars = [[100.0, 101.0, 99.0, 100.0, 1.0] for _ in range(30)]
    p.real_bars_1m = lambda s: bars
    p.ready_1m = lambda s: len(bars)
    p.score = lambda s: (0, "", 0.0)
    p.tf_on = {"1m": True, "5m": False, "15m": False}
    p.klines_tf = {"1m": {"A-USDT": bars}}
    calls = []
    old_prefetch, old_get, old_symbols = EXTRA.prefetch, EXTRA.get, pt.SYMBOLS
    EXTRA.prefetch = lambda pairs: calls.append(("prefetch", list(pairs)))
    EXTRA.get = lambda src, sym: (calls.append(("get", src, sym)) or [])
    try:
        pt.SYMBOLS = ["A-USDT"]
        p.indications.load({"indExtraSources": False})
        p.process_indications()
        disabled = list(calls)
        p.indications.load({"indExtraSources": True})
        p.process_indications()
        enabled = list(calls)
    finally:
        EXTRA.prefetch, EXTRA.get, pt.SYMBOLS = old_prefetch, old_get, old_symbols
    ok = not disabled and any(x[0] == "prefetch" for x in enabled) and sum(x[0] == "get" for x in enabled) == 2
    return ok, f"disabled external reads={len(disabled)}; enabled mocked operations={len(enabled)}"


def restored_book_drops_a_position_only_when_two_reads_agree():
    """A position in the saved book that the venue does not list at start is dropped only when a second read agrees (a lagging
    positions list must not unprotect a live position, empty or not); one the venue still lists, in either mode, stays."""
    def pos(sym, side):
        return pt.Position(symbol=sym, side=side, qty=1.0, entry=10.0, opened_at=1.0, sl=9.0, tp=11.0, peak=10.0)

    def run(first, second, saved):
        p = _bare_pulse()
        p.open = {s_: pos(s_, sd) for s_, sd in saved}
        reads = [first, second]

        class Api:
            def get(self, path, extra=None):
                rows = reads.pop(0) if reads else second
                return {"code": 0, "data": rows}
        p.api = Api()
        p.cooldown = {}
        p.close_from_venue = lambda pos_, why: False
        p.cancel_controls = lambda sym: None
        p.save_open_book = lambda: None
        real_sleep = pt.time.sleep
        pt.time.sleep = lambda s_: None
        try:
            p.reconcile_restored_book()
        finally:
            pt.time.sleep = real_sleep
        return sorted(p.open)
    other = {"symbol": "OTHER-USDT", "positionSide": "LONG", "positionAmt": "1"}
    mine = {"symbol": "A-USDT", "positionSide": "LONG", "positionAmt": "1"}
    oneway = {"symbol": "A-USDT", "positionSide": "BOTH", "positionAmt": "-2"}
    lag = run([other], [other, mine], [("A-USDT", "LONG")])                  # the second read lists it: kept
    gone = run([other], [other], [("A-USDT", "LONG")])                       # two reads agree: dropped
    held = run([other, mine], [other, mine], [("A-USDT", "LONG")])           # listed: kept
    both = run([oneway], [oneway], [("A-USDT", "SHORT")])                    # one-way mode: the sign of the amount is the side
    empty = run([], [], [("A-USDT", "LONG")])                                # two empty reads: the account is flat, the ghost is dropped
    empty_lag = run([], [mine], [("A-USDT", "LONG")])                        # an empty first read that the second read contradicts: kept
    short_lag = run([], [{"symbol": "A-USDT", "positionSide": "SHORT", "positionAmt": "1"}], [("A-USDT", "SHORT")])
    ok = lag == ["A-USDT"] and gone == [] and held == ["A-USDT"] and both == ["A-USDT"] and empty == [] and empty_lag == ["A-USDT"] and short_lag == ["A-USDT"]
    return ok, (f"lagging read kept={lag}, two reads agree dropped={gone}, listed kept={held}, one-way short kept={both}, "
                f"two empty reads dropped={empty == []}, empty then listed kept={empty_lag}, positive-qty short={short_lag}")


def exchange_flat_requires_a_valid_direction_aware_snapshot():
    p = _bare_pulse()
    pos = pt.Position(symbol="A-USDT", side="SHORT", qty=1, entry=10, opened_at=1, sl=11, tp=9, peak=10)
    cases = [
        ({}, False),
        ([{"symbol": "A-USDT", "positionSide": "BOTH", "positionAmt": "-1"}], False),
        ([{"symbol": "A-USDT", "positionSide": "SHORT", "positionAmt": "1"}], False),
        ([{"symbol": "A-USDT", "positionSide": "SHORT", "positionAmt": "NaN"}], False),
        ([{"symbol": "A-USDT", "positionSide": "SHORT", "positionAmt": "0", "availableAmt": "1"}], False),
        ([{"symbol": "A-USDT", "positionSide": "LONG", "positionAmt": "1"}], True),
        ([], True),
    ]
    results = []
    for rows, want in cases:
        p.api = types.SimpleNamespace(get=lambda path, rows=rows: {"code": 0, "data": rows})
        results.append((p._exchange_flat(pos), want))
    return all(got == want for got, want in results), f"(flat, expected)={results}"


def cycles_start_at_least_scan_apart():
    """Ticks are not an input to the pacing: cycles of 10 ms each, scan 0.2 s. Every start is 0.2 s after the last."""
    scan, cost = 0.2, 0.01
    t, starts = 0.0, []
    for _ in range(50):
        starts.append(t)
        t += cost
        t += pt.pace_gap_s(starts[-1], t, scan)
    gaps = [round(b - a, 9) for a, b in zip(starts, starts[1:])]
    ok = all(g == scan for g in gaps)
    return ok, f"gaps min={min(gaps)} max={max(gaps)} over {len(gaps)} cycles"


def slow_cycle_is_not_padded():
    """A cycle longer than the scan period gets no extra wait: the next cycle starts when this one ends."""
    scan = 0.2
    gap = pt.pace_gap_s(0.0, 0.35, scan)
    ok = gap == 0.0 and pt.pace_gap_s(0.0, 0.05, scan) > 0.149
    return ok, f"gap after 0.35 s cycle={gap}; gap 0.05 s into a 0.2 s period={pt.pace_gap_s(0.0, 0.05, scan):.3f}"


def main_loop_does_not_wait_on_the_tick_event():
    """The loop sleeps the pacing gap. It must not wait on wake_ev, which a tick sets, or a tick would start a cycle early."""
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run")
    waits = [n for n in ast.walk(run) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "wait" and "wake_ev" in ast.unparse(n.func.value)]
    uses_gap = any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "pace_gap_s" for n in ast.walk(run))
    return not waits and uses_gap, f"wake_ev waits in run()={len(waits)} paced by pace_gap_s={uses_gap}"


def overlay_reads_leverage_settings():
    """Overlay keys decide the mode and the pulse leverage. Absent or zero values fall back; the range is 1 to 500."""
    cases = [
        ({"leverage": 150, "useMaxLeverage": False}, (False, 150)),
        ({"leverage": 20, "useMaxLeverage": True}, (True, 20)),
        ({}, (True, 150)),
        ({"leverage": 0}, (True, 150)),
        ({"leverage": "x"}, (True, 150)),
        ({"leverage": 9999}, (True, 500)),
    ]
    got = [(c, pt.leverage_settings(c)) for c, _ in cases]
    bad = [(c, g, w) for (c, g), (_, w) in zip(got, cases) if g != w]
    return not bad, f"mismatches={bad or 'none'}"


def max_toggle_decides_the_target():
    """Max on: each contract runs at its exchange max. Off: the pulse leverage, capped by the contract's max."""
    saved = pt.LEVERAGE
    try:
        pt.LEVERAGE = 20
        p = _bare_pulse()
        on = [p.target_leverage(150), p.target_leverage(10), p.target_leverage(0)]
        p.use_max_leverage = False
        off = [p.target_leverage(150), p.target_leverage(10), p.target_leverage(0)]
    finally:
        pt.LEVERAGE = saved
    ok = on == [150, 10, 20] and off == [20, 10, 20]
    return ok, f"max on -> {on} (max, max, pulse fallback); max off -> {off} (pulse capped by max, pulse fallback)"


def order_leverage_follows_the_target():
    """leverage_for is the target for the contract's own max, so new orders use the overlay when max is off."""
    saved = pt.LEVERAGE
    try:
        pt.LEVERAGE = 25
        p = _bare_pulse()
        c = types.SimpleNamespace(symbol="BTC-USDT", max_lev=150)
        p.use_max_leverage = True
        on = p.leverage_for(c)
        p.use_max_leverage = False
        off = p.leverage_for(c)
        p.lev_max["BTC-USDT"] = 10
        capped = p.leverage_for(c)
    finally:
        pt.LEVERAGE = saved
    ok = (on, off, capped) == (150, 25, 10)
    return ok, f"orders: max on={on} max off={off} max off with a known max of 10={capped}"


def leverage_requires_account_confirmation():
    """Contract maxima cannot masquerade as applied leverage, and partial updates block the entry."""
    p = _bare_pulse()
    p.contracts = {"BTC-USDT": pt.Contract(symbol="BTC-USDT", min_qty=.001, step=.001, qprec=3, pprec=2, min_usdt=5, max_lev=150)}
    p.seed_lev_from_contracts()
    seeded_only_max = p.lev_max.get("BTC-USDT") == 150 and "BTC-USDT" not in p.lev_map

    class RefuseSecond(_StubExchange):
        def get(self, path, extra=None):
            return {"code": 0, "data": {"maxLongLeverage": "150", "maxShortLeverage": "150", "longLeverage": "10", "shortLeverage": "10"}}
        def post(self, path, body):
            if path.endswith("/trade/leverage"):
                self.posts.append((path, body))
                return {"code": 0} if body.get("side") == "LONG" else {"code": 1, "msg": "refused"}
            return super().post(path, body)
    def failed():
        venue = RefuseSecond()
        pulse, sym = _enter(venue)
        return len([1 for path, _ in venue.posts if path.endswith("/trade/order")]), sym in pulse.lev_map
    orders, recorded = _isolated(failed)
    return seeded_only_max and orders == 0 and not recorded, f"contract only max={seeded_only_max}; rejected leverage -> orders={orders}, recorded={recorded}"


def x01_max_leverage_does_not_change_margin_type():
    class ChangeLeverage(_StubExchange):
        def get(self, path, extra=None):
            return {"code": 0, "data": {"maxLongLeverage": "150", "maxShortLeverage": "150", "longLeverage": "10", "shortLeverage": "10"}}
    def run():
        venue = ChangeLeverage()
        pulse, sym = _enter(venue)
        return ([path for path, _ in venue.posts if path.endswith("/trade/leverage")],
                [path for path, _ in venue.posts if path.endswith("/trade/marginType")], sym in pulse.open)
    saved = pt.CONN_SHORT
    try:
        pt.CONN_SHORT = "bingx-x01"
        lever, margin, opened = _isolated(run)
    finally:
        pt.CONN_SHORT = saved
    return len(lever) == 2 and not margin and opened, f"leverage changes={len(lever)}, margin-type changes={len(margin)}, stub entry={opened}"


def minimum_order_rejection_cannot_bypass_entry_limits():
    class OversizedMinimum(_StubExchange):
        def post(self, path, body):
            if path.endswith("/trade/order"):
                self.posts.append((path, body))
                return {"code": 100400, "msg": "minimum order amount is 1000"}
            return super().post(path, body)
    def run():
        venue = OversizedMinimum()
        p, sym = _enter(venue)
        return len([1 for path, _ in venue.posts if path.endswith("/trade/order")]), sym in p.open
    orders, opened = _isolated(run)
    return orders == 1 and not opened, f"order attempts={orders}; opened={opened}"


def retried_minimum_order_keeps_its_actual_client_id():
    class RetryVenue(_StubExchange):
        def post(self, path, body):
            if path.endswith("/trade/order"):
                self.posts.append((path, dict(body)))
                attempts = [p for p, _ in self.posts if p.endswith("/trade/order")]
                if len(attempts) == 1:
                    return {"code": 100400, "msg": "minimum order amount is 0.06"}
                return {"code": 0, "data": {"order": {"orderId": "1", "avgPrice": "100", "executedQty": "0.06", "status": "FILLED"}}}
            return super().post(path, body)
    def run():
        sym = "BTC-USDT"
        venue = RetryVenue()
        contracts = {sym: pt.Contract(symbol=sym, min_qty=.001, step=.001, qprec=3, pprec=2, min_usdt=5, max_lev=150)}
        p = pt.Pulse(venue, contracts)
        p.boot_ts, p.last_bal, p.available, p.halted = 0, pt.time.time(), 50, False
        p.px[sym], p.price_ts[sym] = 100.0, pt.time.time()
        p.control_orders = False
        p.entry_sense = lambda *a, **k: None
        p.max_book_notional = lambda: 100.0
        p.sets.use_historic_gate = False
        p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", .7)
        ids = [b['clientOrderID'] for path, b in venue.posts if path.endswith('/trade/order')]
        return ids, p.open.get(sym).client_id if sym in p.open else None
    ids, recorded = _isolated(run)
    return len(ids) == 2 and ids[0] != ids[1] and recorded == ids[1], f"attempts={len(ids)} distinct IDs={len(set(ids))}; recorded retry ID={bool(ids and recorded == ids[-1])}"


def uncertain_entry_blocks_retries_for_the_symbol():
    """A lost answer cannot lead to a second order after the short cooldown, even on the other side."""
    class CountLost(_StubExchange):
        def post(self, path, body):
            if path.endswith("/trade/order"):
                self.posts.append((path, body))
                return {"code": -1, "msg": "timed out", "error": True}
            return super().post(path, body)
    def run():
        venue = CountLost()
        p, sym = _enter(venue)
        before = len([1 for path, _ in venue.posts if path.endswith("/trade/order")])
        p.cooldown[sym] = 0
        p.place(sym, -1, "ind:state:tf_combined:0.60:a2:src", 0.7)
        after = len([1 for path, _ in venue.posts if path.endswith("/trade/order")])
        return before, after, f"{sym}:LONG" in p.unknown_entry
    before, after, unknown = _isolated(run)
    return before == after == 1 and unknown, f"order attempts before={before}, after={after}, uncertain={unknown}"


def uncertain_entry_survives_restart():
    import json
    def run():
        venue = _LostAnswerVenue()
        p, sym = _enter(venue)
        key = f"{sym}:LONG"
        saved = json.load(open(pt.entry_intent_path()))
        p2 = pt.Pulse(_StubExchange(), _offline_contracts())
        persisted = key in saved and p2.entry_intent_ids.get(key) == saved[key]["cid"] and key in p2.unknown_entry
        private = os.stat(pt.entry_intent_path()).st_mode & 0o077 == 0
        return persisted, private, p2.intent_state_bad
    persisted, private, bad = _isolated(run)
    return persisted and private and not bad, f"restored={persisted}; private={private}; invalid state={bad}"


def failed_intent_persistence_blocks_order():
    def run():
        pt.OPEN_PATH = os.path.join(os.path.dirname(pt.OPEN_PATH), "missing", "open.json")
        venue = _StubExchange()
        p, sym = _enter(venue)
        attempts = len([1 for path, _ in venue.posts if path.endswith("/trade/order")])
        return attempts, p.intent_state_bad, sym in p.open
    attempts, bad, opened = _isolated(run)
    return attempts == 0 and bad and not opened, f"order attempts={attempts}; persistence failure={bad}; opened={opened}"


def uncertain_adoption_requires_matching_fill():
    p = _bare_pulse()
    key, cid = "A-USDT:LONG", pt.TAG + "o-test"
    p.entry_intent_ids = {key: cid}
    rows = [
        {"status": "FILLED", "executedQty": "1", "avgPrice": "100", "side": "BUY", "positionSide": "LONG", "clientOrderId": cid},
        {"status": "FILLED", "executedQty": "2", "avgPrice": "100", "side": "BUY", "positionSide": "LONG", "clientOrderId": cid},
        {"status": "FILLED", "executedQty": "1", "avgPrice": "100", "side": "SELL", "positionSide": "LONG", "clientOrderId": cid},
        {"status": "FILLED", "executedQty": "1", "avgPrice": "100", "side": "BUY", "positionSide": "LONG", "clientOrderId": "foreign"},
        {"fillUnconfirmed": True},
    ]
    out = []
    for row in rows:
        p.confirmed_order = lambda data, symbol, cid, row=row: row
        out.append(p._verified_entry_intent(key, "A-USDT", "LONG", 1.0) is not None)
    return out == [True, False, False, False, False], f"(matched, bad quantity, bad side, bad ID, unconfirmed)={out}"


def uncertain_entry_recovery_uses_its_own_fill():
    def run(match):
        p, sym = _enter(_LostAnswerVenue())
        key = f"{sym}:LONG"
        cid = p.entry_intent_ids[key]
        fill_at = pt.time.time() - 1
        class Venue(_StubExchange):
            def get(self, path, extra=None):
                if path.endswith('/user/positions'):
                    return {"code": 0, "data": [{"symbol": sym, "positionSide": "LONG", "positionAmt": "0.05", "avgPrice": "100"}]}
                if path.endswith('/trade/order'):
                    return {"code": 0, "data": {"order": {"symbol": sym, "status": "FILLED", "executedQty": "0.05", "avgPrice": "100", "side": "BUY", "positionSide": "LONG", "clientOrderId": cid if match else "foreign", "updateTime": int(fill_at * 1000)}}}
                return super().get(path, extra)
        p.api = Venue()
        p.control_orders = False
        p.our_orders = lambda *a, **k: []
        p.adopt_exchange_positions()
        pos = p.open.get(sym)
        return bool(pos), (pos.client_id if pos else ""), key in p.unknown_entry, cid, (pos.opened_at if pos else 0), fill_at
    yes = _isolated(lambda: run(True))
    no = _isolated(lambda: run(False))
    ok = yes[0] and yes[1] == yes[3] and not yes[2] and abs(yes[4] - yes[5]) < .01 and not no[0] and no[2]
    return ok, f"matching fill adopted={yes[0]} with original ID={yes[1] == yes[3]} intent cleared={not yes[2]}; foreign fill adopted={no[0]} intent retained={no[2]}"


def padding_does_not_count_as_readiness():
    """seed_px_bars pads a WebSocket-only symbol with flat bars. They are not history: readiness counts only real
    minutes, so the 20-bar gate opens after 20 real minutes, not on the padding."""
    p = _bare_pulse()
    p.px = {"A-USDT": 100.0}
    p.seed_px_bars()
    after_seed = p.ready_1m("A-USDT")
    padded_len = len(p.klines_tf["1m"]["A-USDT"])
    for _ in range(20):
        p.bar_min["A-USDT"][0] -= 1.0
        p.seed_px_bars()
    ready = p.ready_1m("A-USDT")
    untouched = p.ready_1m("B-USDT")
    ok = after_seed == 0 and padded_len == 24 and ready == 20 and untouched == 0
    return ok, f"after seed={after_seed} padded len={padded_len} after 20 real minutes={ready} untouched={untouched}"


def only_the_main_loop_pings_the_watchdog():
    """A background thread that pings the watchdog would mask a stuck main loop. Pings come from run() and
    _one_cycle() only, which run on the main thread."""
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    owners = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "sd_notify" and n.args \
                    and isinstance(n.args[0], ast.Constant) and "WATCHDOG" in str(n.args[0].value):
                owners.add(fn.name)
    ok = owners <= {"run", "_one_cycle"} and owners
    return bool(ok), f"functions that ping the watchdog: {sorted(owners)}"


def partial_fill_is_sized_from_the_executed_quantity():
    """A partial fill reports executedQty below the request. The position is sized from what filled; the request is
    only the fallback for legacy replies with no execution field. Explicit zero never creates a fill."""
    cases = [
        ({"executedQty": "0.4", "quantity": "1"}, 1.0, 0.4),
        ({"quantity": "1"}, 2.0, 1.0),
        ({"executedQty": "0", "origQty": "5"}, 9.0, 0.0),
        ({}, 3.0, 3.0),
    ]
    got = [pt.filled_qty(d, req) for d, req, _ in cases]
    ok = all(abs(g - w) < 1e-12 for g, (_, _, w) in zip(got, cases))
    return ok, f"filled={got} want={[w for _, _, w in cases]}"


def indicator_sees_only_real_bars():
    """seed_px_bars pads a new symbol with flat rows so the indicator can start. The indicator path must get only the
    real minutes: 20 real bars after 20 real minutes, not the 44 stored rows."""
    import types
    p = _bare_pulse()
    p.px = {}
    sym = "A-USDT"
    for k in range(21):                       # the first call opens the minute, each later call rolls one real bar
        p.px[sym] = 100.0 + k
        p.seed_px_bars()
        if sym in p.bar_min:
            p.bar_min[sym][0] -= 1.0
    captured = {}

    class _Ind:
        settings = {"enabled": True, "extraSources": False}
        extra_cursor = 0
        last = {}

        def process(self, symbol, bars, **kw):
            captured["bars"] = list(bars)
            captured["by_tf"] = dict(kw.get("bars_by_tf") or {})

    p.indications = _Ind()
    p.load = types.SimpleNamespace(cursor_ind=0,
                                   scan_window=lambda syms, open_syms, chunk, cursor, ranked: ([sym], 0))
    p._budget = lambda: types.SimpleNamespace(scan_chunk=10, extra_n=0, extra_sources=False)
    p.open = {}
    p.universe = []
    p.tf_on = {}
    p.score = lambda s: (0, 0.0, 0.0)
    p.process_indications()
    stored = len(p.klines_tf["1m"][sym])
    got = len(captured.get("bars") or [])
    tf1 = len((captured.get("by_tf") or {}).get("1m") or [])
    ok = p.ready_1m(sym) == 20 and got == 20 and tf1 == 20 and stored == 44
    return ok, f"ready={p.ready_1m(sym)} indicator bars={got} 1m bars={tf1} stored={stored}"


class _StubExchange:
    """The venue's answers for one entry: the order reports a partial fill; the protective batch is accepted.
    Nothing leaves the process."""

    path_cd: dict = {}

    def __init__(self, executed: str = "0.4", requested: str = "1") -> None:
        self.posts: list = []
        self.batches: list = []
        self.executed = executed
        self.requested = requested

    def get(self, path, extra=None):
        return {"code": 0, "data": {"balance": {"equity": "100", "availableMargin": "50"}, "maxLongLeverage": "150",
                                    "maxShortLeverage": "150", "longLeverage": "150", "shortLeverage": "150"}}

    def post(self, path, body):
        self.posts.append((path, body))
        if path.endswith("/trade/order"):
            return {"code": 0, "data": {"order": {"orderId": "1", "avgPrice": "100", "executedQty": self.executed,
                                                  "quantity": self.requested,
                                                  "clientOrderID": body.get("clientOrderID", "")}}}
        return {"code": 0, "data": {}}

    def batch_place(self, orders):
        self.batches.append(list(orders))
        return {"code": 0, "data": {"orders": [{"orderId": "9", "type": o.get("type"),
                                                "clientOrderID": o.get("clientOrderID", ""), "code": 0}
                                               for o in orders]}}

    def public(self, path, extra=None):
        return {"code": 0, "data": []}

    def delete(self, path, extra=None):
        return {"code": 0}

    def gather_public(self, *a, **k):
        return []


def entry_lifecycle_with_a_stub_exchange():
    """One entry through place() against the stub venue, which reports a partial fill. The order is posted at the
    requested size, the position is recorded at the executed size, and one protective batch goes out. The state files
    are redirected to a temporary directory, and the settings dump and contract source are replaced."""
    sym = "BTC-USDT"
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH)
    orders: list = []
    pos = None
    api = None
    with tempfile.TemporaryDirectory() as td:
        pt.BLOCK_PATH = os.path.join(td, "block.json")
        pt.LEV_PATH = os.path.join(td, "lev.json")
        pt.OPEN_PATH = os.path.join(td, "open.json")
        pt.dump_cts_settings = lambda: {}
        try:
            api = _StubExchange(executed="0.4", requested="1")
            contracts = {sym: pt.Contract(symbol=sym, min_qty=0.001, step=0.001, qprec=3, pprec=2, min_usdt=5.0,
                                          max_lev=150)}
            p = pt.Pulse(api, contracts)
            p.boot_ts = 0.0
            p.px[sym] = 100.0
            p.price_ts[sym] = pt.time.time()
            p.available = 50.0
            p.last_bal = pt.time.time()
            p.halted = False
            p.entry_sense = lambda *a, **k: None
            p.sets.use_historic_gate = False  # this fixture exercises an untagged venue entry
            p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
            pos = p.open.get(sym)
            orders = [b for path, b in api.posts if path.endswith("/trade/order")]
            ok = (len(orders) == 1 and pos is not None and abs(float(pos.qty) - 0.4) < 1e-12
                  and len(api.batches) == 1 and len(api.batches[0]) == 2)
        finally:
            pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH = saved
    return ok, (f"entry orders={len(orders)} position qty={getattr(pos, 'qty', None)} "
                f"protective batches={len(api.batches) if api else 0}")


class _PartialCloseVenue(_StubExchange):
    """Entry fills fully; the first close order fills 40% of its quantity, the next close is accepted in full unless
    fail_rest is set, in which case it is refused."""

    def __init__(self, fail_rest: bool = False) -> None:
        super().__init__(executed="1", requested="1")
        self.fail_rest = fail_rest
        self.closes: list = []

    def post(self, path, body):
        if path.endswith("/trade/order") and body.get("side") == "SELL":
            self.closes.append(dict(body))
            if len(self.closes) == 1 and "quantity" in body:
                q = float(body["quantity"])
                return {"code": 0, "data": {"order": {"orderId": "c1", "avgPrice": "101",
                                                      "executedQty": str(q * 0.4), "quantity": str(q)}}}
            if self.fail_rest:
                return {"code": 100400, "msg": "temporary refusal"}
            return {"code": 0, "data": {"order": {"orderId": "c2", "avgPrice": "102", "executedQty": str(float(self.closes[0]["quantity"]) * .6)}}}
        return super().post(path, body)


def _enter(venue):
    sym = "BTC-USDT"
    contracts = {sym: pt.Contract(symbol=sym, min_qty=0.001, step=0.001, qprec=3, pprec=2, min_usdt=5.0, max_lev=150)}
    p = pt.Pulse(venue, contracts)
    p.boot_ts = 0.0
    p.px[sym] = 100.0
    p.price_ts[sym] = pt.time.time()
    p.available = 50.0
    p.last_bal = pt.time.time()
    p.halted = False
    p.entry_sense = lambda *a, **k: None
    p.sets.use_historic_gate = False  # the isolated venue fixture opens a non-Set position
    p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
    return p, sym


def partial_close_fill_closes_the_remainder():
    """A close that fills only part of its quantity must not be reported as a full close. The remainder is closed with
    the closePosition form, the returned price blends the two fills, and if the remainder is refused the position stays
    open at the size that is left."""
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH)
    with tempfile.TemporaryDirectory() as td:
        pt.BLOCK_PATH = os.path.join(td, "block.json")
        pt.LEV_PATH = os.path.join(td, "lev.json")
        pt.OPEN_PATH = os.path.join(td, "open.json")
        pt.TRADES_PATH = os.path.join(td, "trades.jsonl")
        pt.dump_cts_settings = lambda: {}
        try:
            venue = _PartialCloseVenue()
            p, sym = _enter(venue)
            pos = p.open[sym]
            q0 = float(pos.qty)
            ok1, px1 = p.market_close(pos)
            second_is_close_position = len(venue.closes) == 2 and venue.closes[1].get("closePosition") == "true" \
                and "quantity" not in venue.closes[1]
            blended = 101.0 * 0.4 * q0 + 102.0 * 0.6 * q0
            blended_px = blended / q0
            venue2 = _PartialCloseVenue(fail_rest=True)
            os.unlink(pt.OPEN_PATH)    # independent second account, not a restart of the first partial
            p2, sym2 = _enter(venue2)
            pos2 = p2.open[sym2]
            q2 = float(pos2.qty)
            ok2, _ = p2.market_close(pos2)
            kept = abs(float(pos2.qty) - q2 * 0.6) < 1e-9
        finally:
            pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH = saved
    ok = ok1 and second_is_close_position and abs(px1 - blended_px) < 1e-6 and (not ok2) and kept
    return ok, (f"full close={ok1} remainder via closePosition={second_is_close_position} price={px1:.4f} "
                f"(want {blended_px:.4f}); refused remainder -> closed={ok2} qty kept={kept}")


def symbol_cap_applies_under_a_wildcard():
    """symbolCap cuts the ranked universe, also when the overlay is a wildcard ("*"). Cap 0 keeps every name. A symbol
    with an open position stays in the book above the cap."""
    saved_syms = list(pt.SYMBOLS)
    p = _bare_pulse()
    p.symbol_sort = "vol1h"
    p.symbols_dynamic = True
    p.overlay_wild = True
    p.open = {}
    p.universe = [{"symbol": f"S{i:02d}-USDT", "vol1h": float(100 - i), "maxLeverage": 50, "quoteVolume": 1.0,
                   "vol24h": 0.0, "changePct": 0.0} for i in range(60)]
    p.last_dyn_sel = 0.0
    p.contracts = {}
    p.sets = types.SimpleNamespace(set_symbols=lambda *a, **k: None, enabled=False)
    out = {}
    try:
        p.symbol_cap = 30
        p.apply_dynamic_symbols(force=True)
        out["capped"] = len(pt.SYMBOLS)
        p.symbol_cap = 0
        p.apply_dynamic_symbols(force=True)
        out["uncapped"] = len(pt.SYMBOLS)
        p.symbol_cap = 30
        p.open = {"S59-USDT": types.SimpleNamespace(symbol="S59-USDT")}
        p.apply_dynamic_symbols(force=True)
        out["with_open"] = (len(pt.SYMBOLS), "S59-USDT" in pt.SYMBOLS)
    except Exception as exc:  # noqa: BLE001
        return False, f"raised {type(exc).__name__}: {exc}"
    finally:
        pt.SYMBOLS[:] = saved_syms
    ok = out.get("capped") == 30 and out.get("uncapped") == 60 and out.get("with_open") == (30, True)
    return ok, f"wildcard cap 30 -> {out.get('capped')}; cap 0 -> {out.get('uncapped')}; cap 30 + one open -> {out.get('with_open')}"


def symbol_rotation_respects_configured_interval():
    p = _bare_pulse()
    p.symbol_sort, p.symbols_dynamic, p.symbol_cap = "vol1h", True, 2
    p.symbol_rotate_s, p.last_dyn_sel, p.overlay_wild = 300.0, 0.0, True
    p.open, p.contracts = {}, {}
    p.ensure_contracts = lambda: None
    p.universe = [{"symbol": f"{s}-USDT", "maxLeverage": 50, "vol1h": v} for s, v in (("A", 3), ("B", 2), ("C", 1))]
    old = list(pt.SYMBOLS)
    try:
        p.apply_dynamic_symbols(force=True)
        first = list(pt.SYMBOLS)
        p.universe[2]["vol1h"] = 10
        p.apply_dynamic_symbols()
        early = list(pt.SYMBOLS)
        p.last_dyn_sel -= 301
        p.apply_dynamic_symbols()
        later = list(pt.SYMBOLS)
    finally:
        pt.SYMBOLS[:] = old
    return first == early == ["A-USDT", "B-USDT"] and later == ["C-USDT", "A-USDT"], f"initial={first}; before interval={early}; after interval={later}"


class _RefusingCloseVenue(_StubExchange):
    """Entry fills fully; every closing order is refused."""

    def post(self, path, body):
        if path.endswith("/trade/order") and body.get("side") == "SELL":
            return {"code": 100400, "msg": "temporary refusal"}
        return super().post(path, body)


class _LostAnswerVenue(_StubExchange):
    """The entry request fails on the wire: the caller cannot know whether the order filled."""

    def post(self, path, body):
        if path.endswith("/trade/order"):
            return {"code": -1, "msg": "timed out", "error": True}
        return super().post(path, body)


def _isolated(fn):
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.TRADES_PATH, pt.dump_cts_settings)
    with tempfile.TemporaryDirectory() as td:
        pt.BLOCK_PATH = os.path.join(td, "block.json")
        pt.LEV_PATH = os.path.join(td, "lev.json")
        pt.OPEN_PATH = os.path.join(td, "open.json")
        pt.TRADES_PATH = os.path.join(td, "trades.jsonl")      # no closes carried over from another check
        pt.dump_cts_settings = lambda: {}
        try:
            return fn()
        finally:
            pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.TRADES_PATH, pt.dump_cts_settings = saved


def requested_local_configuration_reaches_engine():
    """The local 30-symbol, 0.05-volume, 100 ms settings survive the engine's configuration clamp."""
    import json
    saved = (pt.OVERLAY_PATH, pt.SCAN_S, list(pt.SYMBOLS))
    with tempfile.TemporaryDirectory() as td:
        pt.OVERLAY_PATH = os.path.join(td, "overlay.json")
        with open(pt.OVERLAY_PATH, "w") as f:
            json.dump({"symbolCap": 30, "symbolsAll": True, "symbolsDynamic": True,
                       "symbols": ["*"], "volumeFactor": 0.05, "scanS": 0.1}, f)
        try:
            def run():
                p = pt.Pulse(_StubExchange(), _offline_contracts())
                return (p.symbol_cap, p.volume_factor, pt.SCAN_S)
            got = _isolated(run)
        finally:
            pt.OVERLAY_PATH, pt.SCAN_S = saved[0], saved[1]
            pt.SYMBOLS[:] = saved[2]
    return got == (30, 0.05, 0.1), f"symbol cap, factor, scan interval = {got}"


def stale_price_cannot_trigger_a_software_exit():
    """A stale price is ignored; the same fresh price executes the Set's own stop."""
    def run():
        venue = _StubExchange(executed="1")
        p, sym = _enter(venue)
        p.control_orders = False
        pos = p.open[sym]
        p.px[sym] = pos.sl * 0.98
        p.price_ts[sym] = pt.time.time() - pt.PRICE_STALE_S - 1
        before = len(venue.posts)
        p.manage()
        held = sym in p.open and len(venue.posts) == before
        p.price_ts[sym] = pt.time.time()
        p.manage()
        closed = sym not in p.open and len(venue.posts) > before
        return held, closed
    held, closed = _isolated(run)
    return held and closed, f"stale held={held}; fresh stop closed={closed}"


def stale_price_does_not_replace_exchange_controls():
    """Existing exchange controls stay untouched when the current market price is stale."""
    p = _bare_pulse()
    pos = pt.Position(symbol="A-USDT", side="LONG", qty=1, entry=100, opened_at=1, sl=99, tp=101, peak=100)
    p.open, p.px, p.ctrl_skip, p.control_orders = {pos.symbol: pos}, {pos.symbol: 100}, {}, True
    p.missing_controls = lambda pos: False
    p.controls_illegal = lambda pos: True
    replaced = []
    p.ensure_controls = lambda pos: replaced.append(pos.symbol)
    p.price_ts[pos.symbol] = pt.time.time() - pt.PRICE_STALE_S - 1
    p.priority_controls()
    stale_writes = len(replaced)
    p.price_ts[pos.symbol] = pt.time.time()
    p.priority_controls()
    return stale_writes == 0 and replaced == [pos.symbol], f"stale writes={stale_writes}; fresh checks={len(replaced)}"


def slow_cycles_do_not_force_stats_every_cycle():
    """A CPU-bound overrun is reported as such and does not defeat the stats write throttle."""
    class NoThread:
        def __init__(self, *a, **kw): pass
        def start(self): pass
    def run():
        p = pt.Pulse(_StubExchange(), _offline_contracts())
        p.pool = types.SimpleNamespace(submit=lambda *a, **k: None)
        for name in ("refresh_balance", "refresh_fees", "refresh_tickers", "refresh_klines", "process_indications",
                     "update_regime", "sync_own_fills", "reconcile_restored_book", "adopt_exchange_positions",
                     "list_orders", "priority_controls", "_load_lev_file", "run_self_tests"):
            setattr(p, name, lambda *a, **k: None)
        writes, cycles = [], []
        p.stats = lambda: (writes.append(1) or {})
        p.write_results_export = lambda: None
        p._stats_force, p._stats_ts = False, 0.0
        def one():
            if len(cycles) == 3:
                raise SystemExit()
            cycles.append(1)
            p.did_io = False
            pt.time.sleep(.12)
        p._one_cycle = one
        old_threads, old_scan, old_notify = pt.threading, pt.SCAN_S, pt.sd_notify
        pt.threading, pt.SCAN_S, pt.sd_notify = types.SimpleNamespace(Thread=NoThread), .1, lambda *a: None
        try:
            try: p.run()
            except SystemExit: pass
        finally:
            pt.threading, pt.SCAN_S, pt.sd_notify = old_threads, old_scan, old_notify
        return len(writes), p.last_scan_io, p.cycle_overrun, p.last_cycle_ms
    writes, io, overrun, elapsed = _isolated(run)
    ok = writes == 1 and not io and overrun and elapsed >= 100
    return ok, f"stats writes={writes}, mislabelled I/O={io}, overrun={overrun}, total={elapsed:.1f}ms"


def failed_close_keeps_the_position_protected():
    """A close the venue refuses must leave the position in the book with its stops re-armable: the stops were
    cancelled before the close, so their ids are dropped and missing_controls is true. A symbol banned for a no-ctrl
    close is blocked for new entries, not erased from the book."""
    def run():
        p, sym = _enter(_RefusingCloseVenue(executed="1", requested="1"))
        pos = p.open[sym]
        had = not p.missing_controls(pos)
        p.close_pos(pos, 100.0, "no-ctrl-test")
        return had, sym in p.open, p.missing_controls(pos), sym in p.ignore_syms
    had, kept, missing, banned = _isolated(run)
    return had and kept and missing and banned, f"stops before={had} kept in book={kept} stops re-armable={missing} banned={banned}"


def partial_close_is_recorded_at_the_full_size():
    def run():
        venue = _PartialCloseVenue()
        p, sym = _enter(venue)
        pos = p.open[sym]
        q0 = float(pos.qty)
        p.close_pos(pos, 100.0, "test-close")
        return q0, [float(c.qty) for c in p.closed]
    q0, qs = _isolated(run)
    return qs == [q0], f"entry qty={q0} recorded close qty={qs}"


def lost_entry_answer_is_remembered_for_adoption():
    def run():
        sym = "BTC-USDT"
        contracts = {sym: pt.Contract(symbol=sym, min_qty=0.001, step=0.001, qprec=3, pprec=2, min_usdt=5.0,
                                      max_lev=150)}
        p = pt.Pulse(_LostAnswerVenue(), contracts)
        p.boot_ts = 0.0
        p.px[sym] = 100.0
        p.price_ts[sym] = pt.time.time()
        p.available = 50.0
        p.last_bal = pt.time.time()
        p.halted = False
        p.entry_sense = lambda *a, **k: None
        p.sets.use_historic_gate = False  # exercise venue recovery independently of Set validation
        p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
        return sym in p.open, dict(getattr(p, "unknown_entry", {}))
    opened, unk = _isolated(run)
    return (not opened) and any(k.startswith("BTC-USDT:") for k in unk), f"in book={opened} unknown entries={list(unk)}"


class _PagedKlines:
    """A venue that serves 1m klines like the real one: at most 1000 bars per request (a larger limit is refused with code
    109400 and no data), oldest first, endTime inclusive. The series is 3000 minutes long and its closes count 0, 1, 2...
    Its last row is the minute still forming (as on the venue): the Set history takes the completed minutes only."""

    def __init__(self, n: int = 3000, t_end: int = 0) -> None:
        import time as _t
        t_end = t_end or int(_t.time() // 60) * 60_000
        self.rows = [{"open": str(i + 0.0), "high": str(i + 0.5), "low": str(i - 0.5), "close": str(i + 0.0), "volume": "1",
                      "time": t_end - (n - 1 - i) * 60_000} for i in range(n)]
        self.requests: list = []

    def gather_public(self, reqs, timeout=4.2):
        out = []
        for path, extra in reqs:
            self.requests.append(dict(extra))
            lim = int(extra.get("limit") or 500)
            if lim > 1000:
                out.append((path, extra, {"code": 109400, "msg": "Invalid parameters, err:limit: This field must be less than"}))
                continue
            end = int(extra.get("endTime") or 10 ** 18)
            rows = [r for r in self.rows if r["time"] <= end][-lim:]
            out.append((path, extra, {"code": 0, "data": rows}))
        return out


def history_is_fetched_in_pages_within_the_venue_limit():
    """The Set history asks for 1920 bars, which the venue refuses in one request. The fetch pages backwards with
    endTime, so the series arrives whole, in order and without a gap; once a symbol holds its history, the next refresh
    asks only for the newest minutes."""
    import set_engine as se
    saved = list(pt.SYMBOLS)
    try:
        pt.SYMBOLS[:] = ["A-USDT", "B-USDT"]
        p = _bare_pulse()
        venue = _PagedKlines()
        p.api = venue
        p.sets = se.SetBook()
        p.sets.lookback = 1920
        p._hist_fetch()
        first_requests = list(venue.requests)
        have = {s: len(p.sets.bars.get(s) or []) for s in pt.SYMBOLS}
        closes = [b[3] for b in (p.sets.bars.get("A-USDT") or [])]
        contiguous = closes == [float(x) for x in range(3000 - 1 - 1920, 3000 - 1)]     # the forming last minute is not a bar
        venue.requests.clear()
        p._hist_fetch()
        refresh_limits = sorted({int(r["limit"]) for r in venue.requests})
    finally:
        pt.SYMBOLS[:] = saved
    ok = (all(n == 1920 for n in have.values()) and contiguous and all(int(r["limit"]) <= 1000 for r in first_requests)
          and any("endTime" in r for r in first_requests) and refresh_limits == [120])
    return ok, f"bars={have} contiguous={contiguous} first requests={len(first_requests)} refresh limits={refresh_limits}"


def _book_with_three_packs(**extra):
    import set_engine as se
    from common import overlay
    b = se.SetBook()
    b.load(overlay(stratShortRange=True, **extra))
    return b


def client_order_ids_round_trip_to_their_own_set():
    """Every Set of the default grid on three packs (1449 Sets, indices above 1000) must decode from its own client order
    id to itself. After the grid moves (min step raised, as step-adapt does) a base Set must still decode to itself or to
    no Set at all, never to another Set."""
    b = _book_with_three_packs()
    p = _bare_pulse()
    p.sets = b
    sets = list(b.by_idx)
    wrong = [st.id for st in sets if (p.parse_track(p.cid("o", set_id=st.id, pack=st.pack, set_idx=st.idx)) or {}).get("set_id") != st.id]
    cids = {st.id: p.cid("o", set_id=st.id, pack=st.pack, set_idx=st.idx) for st in sets if st.kind != "trail"}
    b.min_step = 6
    b._rebuild_sets()
    moved = 0
    misattributed = []
    for sid, cid in cids.items():
        got = (p.parse_track(cid) or {}).get("set_id")
        if got and got != sid:
            misattributed.append((sid, got))
        moved += 1
    letters = {st.pack: p.cid("o", set_id=st.id, pack=st.pack, set_idx=st.idx)[len(pt.TAG) + 1] for st in sets}
    ok = not wrong and not misattributed and letters == {"indications": "i", "general": "g", "shortrange": "s"}
    return ok, f"Sets={len(sets)} wrong round trips={wrong[:3]} after the grid moved: {moved} ids, misattributed={misattributed[:3]} letters={letters}"


def a_signal_needs_its_own_pack_to_hold_a_valid_set():
    """Under the Base gate an entry opens only under a Set picked for its own pack (pick_any). A pack with a valid Set that is not on a
    lane must take no entry either: the gate answers "set-gate" rather than letting the no-Set fallback open the position."""
    import types
    p = _bare_pulse()
    p.px = {"A-USDT": 1.0}
    p.open = {}
    ind = types.SimpleNamespace(id="i1", step=2, idx=0, pack="indications", kind="base")

    class _Sets:
        enabled = True
        use_historic_gate = True
        entry_conf = 0.0

        def symbol_ok(self, sym):
            return True

        def pick_any(self, pack):
            return ind if pack == "indications" else None

        def pack_open(self, pack):          # both packs hold a valid Set somewhere; only indications has one on a lane
            return True

        def lane_of(self, st):
            return "pos"

    p.sets = _Sets()
    p.strat_ind = True
    p.indications = types.SimpleNamespace(settings={"enabled": True}, primary=lambda s: None)
    general = p.entry_sense("A-USDT", 1, "gen:test", 0.9, "general")
    own = p.entry_sense("A-USDT", 1, "set:indications", 0.9, "indications")
    ok = general == "set-gate" and own is None
    return ok, f"general without its own Set: {general!r} (want 'set-gate'); indications with its Set: {own!r} (want None)"


def a_venue_order_of_another_symbol_is_not_this_exit():
    """The venue answers an order id under any symbol. A stop or TP fill of another symbol must never be recorded as this
    position's exit (x02 recorded ZCAT closed at WLD's take-profit, +20 %); a fill of the position's own symbol still is."""
    import types
    p = _bare_pulse()
    pos = types.SimpleNamespace(symbol="ZCAT-USDT", sl_oid="", sec_sl_oid="", tp_oid="2115874311641849856", sec_tp_oid="")

    class _Api:
        def __init__(self, sym, px):
            self.sym, self.px = sym, px

        def get(self, path, params=None):
            return {"code": 0, "data": {"order": {"symbol": self.sym, "status": "FILLED", "avgPrice": self.px}}}

    p.api = _Api("WLD-USDT", "0.3985")
    other = pt.Pulse.venue_exit(p, pos)
    p.api = _Api("ZCAT-USDT", "0.0422")
    own = pt.Pulse.venue_exit(p, pos)
    ok = other is None and own == ("tp", 0.0422)
    return ok, f"another symbol's fill -> {other}; own symbol's fill -> {own}"


def a_manager_close_is_not_counted_again_by_the_fill_sync():
    """The manager records its own close; the fill sync must skip the close order it sent and the stop orders of that
    position, or the Set tape gets the same close twice."""
    def run():
        venue = _PartialCloseVenue()
        venue.posts.clear()
        p, sym = _enter(venue)
        pos = p.open[sym]
        pos.sl_oid, pos.tp_oid = "9001", "9002"
        p.close_pos(pos, 100.0, "test-close")
        close_cids = [f["clientOrderID"] for f in venue.closes]
        sent_all_seen = all(c in p.seen_fill_cids for c in close_cids)
        counted = []
        p.sets.on_live_close = lambda rec: counted.append(rec)
        fills = [{"clientOrderID": close_cids[0], "status": "FILLED", "symbol": sym, "executedQty": "1", "avgPrice": "101", "profit": "1.0"},
                 {"clientOrderID": p.cid("s", pos=pos), "orderId": "9001", "status": "FILLED", "symbol": sym, "executedQty": "1", "avgPrice": "99", "profit": "-1.0"},
                 {"clientOrderID": p.cid("s", pos=pos), "orderId": "7777", "status": "FILLED", "symbol": sym, "executedQty": "1", "avgPrice": "99", "profit": "-1.0"},
                 {"clientOrderID": p.cid("t", pos=pos), "orderId": "7778", "status": "FILLED", "symbol": sym, "executedQty": "1", "avgPrice": "101"}]
        p.api.get = lambda path, extra=None: {"code": 0, "data": {"orders": fills}}
        before = p.__dict__.get("exec_n", {}).get("skipped", 0)
        p.sync_own_fills()
        p.sync_own_fills()
        return sent_all_seen, len(counted), p.__dict__.get("exec_n", {}).get("skipped", 0) - before
    seen, n, skipped = _isolated(run)
    return seen and n == 0 and skipped == 2, f"close cids seen={seen}; scored orphan fills={n}; skipped once each={skipped} (including a fill without profit)"


def a_set_fill_keeps_the_sets_own_sl_and_tp():
    """A Set fill is placed with the Set's grid, as the replay that validated it: TP = step x PositionCost (2 x at the
    least, no overlay tpMin clamp), SL = ratio x TP and never under PositionCost + one step."""
    import types
    def run():
        venue = _StubExchange(executed="1", requested="1")
        sym = "BTC-USDT"
        contracts = {sym: pt.Contract(symbol=sym, min_qty=0.001, step=0.001, qprec=3, pprec=2, min_usdt=5.0, max_lev=150)}
        p = pt.Pulse(venue, contracts)
        p.boot_ts = 0.0
        p.px[sym] = 100.0
        p.price_ts[sym] = pt.time.time()
        p.available = 50.0
        p.last_bal = pt.time.time()
        p.halted = False
        p.entry_sense = lambda *a, **k: None
        st = types.SimpleNamespace(id="indications:1m:sl0.50:st2", idx=3, pack="indications", kind="base", step=2, sl_ratio=0.5,
                                   tp_pct=2 * p.position_cost_pct / 100.0, trail_key="", trail_arm=0.0, trail_give=0.0)
        p.sets.pick_any = lambda pack, **kw: st
        p.sets.pick_trail = lambda pack: None
        p.tp_min = 0.0035          # the shipped overlay clamp: it must not apply to a Set fill
        p.sl_min = 0.0020
        p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
        pos = p.open.get(sym)
        return pos, p.position_cost_pct
    pos, cost = _isolated(run)
    c = cost / 100.0
    ok = (pos is not None and abs(pos.tp_pct - 2 * c) < 1e-9 and abs(pos.sl_pct - 2 * c) < 1e-9
          and pos.trail_arm == 0.0 and pos.trail_give == 0.0 and not pos.trail_set_id)     # a base Set does not trail
    return ok, (f"cost={cost}% tp={getattr(pos, 'tp_pct', None)} sl={getattr(pos, 'sl_pct', None)} want both {2 * c}; "
                f"trailing arm={getattr(pos, 'trail_arm', None)} give={getattr(pos, 'trail_give', None)} (none for a base Set)")


def overlay_slpct_and_tppct_are_percent_without_guessing():
    saved = (pt.SL_PCT, pt.TP_PCT)
    p = _bare_pulse()
    src = open(pt.__file__).read()
    guessed = "> 0.05 else float(ov[\"slPct\"])" in src or "> 0.05 else float(ov[\"tpPct\"])" in src
    pt.SL_PCT, pt.TP_PCT = saved
    return not guessed, f"magnitude guess removed={not guessed}"


def _lane_pulse(set_id="indications:1m:sl1.00:st4"):
    """A Pulse holding one open position on a stub venue, with the Exit strategies switched on and a reversal signal
    against the position, so any Exit-lane involvement shows."""
    import types
    # Each case is a fresh account fixture, not a restart of the prior case.
    for path in (pt.TRADES_PATH, pt.OPEN_PATH):
        if os.path.exists(path):
            os.unlink(path)
    venue = _StubExchange(executed="1", requested="1")
    p, sym = _enter(venue)
    pos = p.open[sym]
    pos.set_id = set_id
    pos.pack = "indications"
    pos.opened_at = time.time() - 700.0        # past the exit lanes' minimum hold and the scratch time
    pos.sl = pos.entry * 0.97
    pos.tp = pos.entry * 1.03
    pos.sl_pct = pos.tp_pct = 0.03              # the lane's own levels (manage_set_lane reads these)
    pos.sl_oid = pos.tp_oid = "1"
    p.exits.enabled = True
    p.exits.rev_on = True
    p.exits.time_on = True
    p.exits.ignore_tp = True
    p.boot_ts = 0.0
    p.indications = types.SimpleNamespace(primary=lambda s: types.SimpleNamespace(direction="short"),
                                          settings={"enabled": True}, last={})
    p.control_orders = False
    return p, sym, pos


import time  # noqa: E402


def a_set_position_is_closed_only_by_its_own_lane():
    """With the Exit strategies on and a reversal signal against the position, a Set position stays open (the reversal
    lane belongs to the Exit strategies); the same position without a Set is closed by that lane."""
    def run():
        p, sym, pos = _lane_pulse()
        p.px[sym] = pos.entry * 1.0005
        p.manage()
        set_stays = sym in p.open
        pos.set_id = ""
        p.manage()
        exit_closes = sym not in p.open
        return set_stays, exit_closes, [c.reason for c in p.closed]
    set_stays, exit_closes, reasons = _isolated(run)
    return set_stays and exit_closes, f"Set position stayed open={set_stays}; the same position without a Set closed by the Exit lane={exit_closes} reasons={reasons}"


def a_set_lane_scratch_and_time_follow_the_replay_rules():
    def run():
        out = {}
        # the replay scratches on a bar close: live judges each completed 1m bar's close once, never an intrabar tick
        for tick, close, name in ((0.0003, 0.0003, "below scratch"), (0.0030, 0.0030, "above scratch"),
                                  (0.0030, 0.0003, "tick above, close below")):
            p, sym, pos = _lane_pulse()
            p.exits.enabled = False
            p.px[sym] = pos.entry * (1 + tick)
            p.last_closed_1m = {sym: (int(time.time() // 60) - 1, pos.entry * (1 + close))}
            p.manage()
            out[name] = (sym in p.open, [c.reason for c in p.closed])
        p, sym, pos = _lane_pulse()
        pos.opened_at = time.time() - (p.sets.hist_time_bars * 60.0 + 5.0)
        pos.entry = 100.0
        p.px[sym] = 100.0 * 0.9995
        p.manage()
        out["time"] = (sym in p.open, [c.reason for c in p.closed])
        return out
    out = _isolated(run)
    ok = out["below scratch"][0] and out["above scratch"] == (False, ["scratch+"]) and out["tick above, close below"][0] \
        and out["time"] == (False, ["time"])
    return ok, f"{out}"


def a_set_lane_honours_tp_only_as_the_replay_does():
    """TP by the Set's kind, as the replay: a base Set leaves at its TP whatever exitIgnoreTp says, an Exit Set (exitIgnoreTp's
    own type) never does and its venue TP sits out at the safety distance, a trailing Set follows the book's setting."""
    def run():
        res = {}
        for name, sid in (("base", "indications:1m:sl1.00:st4"), ("exit", "indications:1m:sl1.00:st4:xt"),
                          ("trail", "indications:1m:tr0.48:0.36")):
            for honor in (True, False):
                p, sym, pos = _lane_pulse(sid)
                p.exits.enabled = False
                p.sets.hist_honor_tp = honor
                pos.opened_at = time.time() - 30.0
                pos.tp_pct = 0.0024
                pos.tp = pos.entry * 1.0024
                if name == "trail":
                    pos.trail_set_id = sid
                p.px[sym] = pos.tp * 1.001
                p.manage()
                res[(name, honor)] = [c.reason for c in p.closed]
                sl_safe, tp_safe = p.security_prices(pos)
                res[(name, honor, "order tp")] = round(abs(tp_safe - pos.entry) / pos.entry, 4)
        return res
    res = _isolated(run)
    ok = (res[("base", True)] == ["tp"] and res[("base", False)] == ["tp"]
          and res[("exit", True)] == [] and res[("exit", False)] == []
          and res[("trail", True)] == ["tp"] and res[("trail", False)] == []
          and res[("exit", True, "order tp")] > res[("base", True, "order tp")])
    return ok, f"{res}"


def a_set_trails_only_with_its_trailing_set_and_the_exit_lanes_stay_out():
    def run():
        p, sym, pos = _lane_pulse()
        p.exits.enabled = True
        moves = []
        p.replace_sl = lambda pos_, price: (moves.append(round(price, 4)) or True)
        pos.opened_at = time.time() - 30.0
        pos.trail_arm, pos.trail_give = 0.006, 0.0036
        pos.peak = pos.entry * 1.01
        p.px[sym] = pos.entry * 1.009
        pos.trail_set_id = ""                       # a base Set does not trail
        p.manage()
        without = list(moves)
        pos.trail_set_id = "indications:1m:tr0.60:0.36"   # a foreign trailing Set on a base Set position: still no trailing
        p.manage()
        foreign = list(moves)
        pos.set_id = pos.trail_set_id                       # the position's own Set is the trailing Set: it trails
        p.manage()
        return without, foreign, moves, round(pos.entry * 1.01 * (1 - 0.0036), 4)
    without, foreign, with_set, want = _isolated(run)
    ok = without == [] and foreign == [] and with_set == [want]
    return ok, f"base Set: {without}; base Set with a foreign trailing Set: {foreign}; own trailing Set: {with_set} (want {want})"


def exit_lane_scores_only_its_own_closes():
    def run():
        p, sym, pos = _lane_pulse()
        before = {k: v.n for k, v in p.exits.lanes.items()}
        vbefore = (p.variants.skipped, sum(len(r) for r in p.variants.sl_rows.values()))
        p.exits.enabled = False
        p.px[sym] = pos.entry * 1.003
        p.close_pos(pos, p.px[sym], "scratch+")
        after_set = {k: v.n for k, v in p.exits.lanes.items()}
        vafter = (p.variants.skipped, sum(len(r) for r in p.variants.sl_rows.values()))
        p2, sym2, pos2 = _lane_pulse()
        pos2.set_id = ""
        p2.px[sym2] = pos2.entry * 1.003
        p2.close_pos(pos2, p2.px[sym2], "exit:time")
        after_exit = {k: v.n for k, v in p2.exits.lanes.items()}
        return before, after_set, after_exit, vbefore, vafter
    before, after_set, after_exit, vb, va = _isolated(run)
    ok = before == after_set and sum(after_exit.values()) == sum(before.values()) + 1 and vb == va
    return ok, f"exit lane orders before={sum(before.values())} after a Set close={sum(after_set.values())} after an exit close={sum(after_exit.values())}; variants unchanged={vb == va}"


def the_set_lane_does_not_read_the_exit_strategies():
    """Independence, checked on the code: the functions that close, trail or price a Set position never reach the Exit
    strategies, the coordinator or the default variants."""
    import ast
    src = open(pt.__file__).read()
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Pulse")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "manage_set_lane")
    names = {f"{a.value.id}.{a.attr}" for a in ast.walk(fn) if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id == "self"}
    touched = sorted(n for n in names if n.split(".")[1] in ("exits", "variants", "coord", "dca", "block"))
    seg = ast.get_source_segment(src, next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "security_prices")) or ""
    exit_sl_guarded = seg.count("not set_lane") >= 2
    return not touched and exit_sl_guarded, f"manage_set_lane touches {touched or 'none of exits/variants/coord/dca/block'}; security_prices guards both exit-lane SL tightenings={exit_sl_guarded}"


def credentials_come_from_a_private_file_without_redis():
    """With a private credential file in place the engine needs no Redis: no redis-cli process is started. A file that
    others can read is ignored."""
    import json as _json
    import subprocess as _sp
    saved_run, saved_env = _sp.run, os.environ.get("PULSE_CRED_FILE")
    calls = []
    def no_redis(*a, **k):
        calls.append(a)
        raise FileNotFoundError("redis-cli")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "cred.json")
        with open(path, "w") as f:
            _json.dump({"api_key": "K1", "api_secret": "S1", "base_url": "https://open-api-vst.bingx.com"}, f)
        try:
            pt.subprocess.run = no_redis
            os.environ["PULSE_CRED_FILE"] = path
            os.chmod(path, 0o600)
            private = (pt.connection_field("api_key"), pt.connection_field("api_secret"), pt.connection_field("base_url"))
            spawned_private = len(calls)
            os.chmod(path, 0o644)
            open_file = pt.connection_field("api_key")
        finally:
            pt.subprocess.run = saved_run
            if saved_env is None:
                os.environ.pop("PULSE_CRED_FILE", None)
            else:
                os.environ["PULSE_CRED_FILE"] = saved_env
    ok = private == ("K1", "S1", "https://open-api-vst.bingx.com") and spawned_private == 0 and open_file == ""
    return ok, f"private file read={private[0] == 'K1'} redis processes={spawned_private}; world-readable file ignored={open_file == ''}"


def history_thread_fetches_at_start_and_never_replays_padding():
    """At start every symbol holds only padded live rows (flat seed rows, no real minutes). One pass of the history
    thread must fetch the real history at once (no wait for live minutes), keep the padding out of the Set bars, and
    replay the symbols."""
    import types
    import set_engine as se
    saved = list(pt.SYMBOLS)
    try:
        pt.SYMBOLS[:] = ["A-USDT", "B-USDT", "C-USDT", "D-USDT"]
        p = _bare_pulse()
        p.api = _PagedKlines()
        p.sets = se.SetBook()
        from common import overlay, SMALL
        p.sets.load(overlay(**SMALL))
        p.sets.lookback = 1920
        for s_ in pt.SYMBOLS:
            p.klines_tf["1m"][s_] = [[100.0, 100.0, 100.0, 100.0, 0.0]] * 60       # the seed padding
            p.real_1m[s_] = 0
        p.open = {}
        p._budget = lambda: types.SimpleNamespace(scan_chunk=0, hist_chunk=6, hist_run=True)
        p.load = types.SimpleNamespace(last_budget=types.SimpleNamespace(level="normal"), cursor_hist=0)
        p.hist_busy = False
        p._hist_tick()
        sizes = {s_: len(p.sets.bars.get(s_) or []) for s_ in pt.SYMBOLS}
        flat = sum(1 for s_ in pt.SYMBOLS for b in (p.sets.bars.get(s_) or []) if b[0] == b[1] == b[2] == b[3] == 100.0)
        replayed = p.sets.progress.symbols_total
    finally:
        pt.SYMBOLS[:] = saved
    ok = all(n == 1920 for n in sizes.values()) and flat == 0 and replayed == 4
    return ok, f"bars={sizes} padded rows in the Set history={flat} symbols replayed={replayed}"


def a_symbol_that_left_the_universe_leaves_the_set_history():
    """The universe rotates. A symbol that left it keeps no Set bars, so after the next scoring pass its trades are in no
    Set's tape (the simulator and a fresh start see the current universe only) and its memory is freed. A symbol with an
    open position stays."""
    import types
    import set_engine as se
    from common import overlay, SMALL, synth_bars
    saved = list(pt.SYMBOLS)
    try:
        pt.SYMBOLS[:] = ["A-USDT", "B-USDT"]
        p = _bare_pulse()
        p.api = types.SimpleNamespace(gather_public=lambda reqs, timeout=4.2: [(x, y, {"code": 0, "data": []}) for x, y in reqs])
        p.sets = se.SetBook()
        p.sets.load(overlay(**SMALL))
        for k, s_ in enumerate(("A-USDT", "B-USDT", "GONE-USDT", "HELD-USDT")):
            p.sets.bars[s_] = synth_bars(70 + k, 1920, start=50.0 + k, vol=0.003)
            p.real_1m[s_] = 0
        p.sets.replay_all()
        before = sorted(p.sets._hist_rows)
        p.open = {"HELD-USDT": types.SimpleNamespace(symbol="HELD-USDT")}
        p.sets.last_run = 0.0                      # a scoring pass is due
        p._budget = lambda: types.SimpleNamespace(scan_chunk=0, hist_chunk=6, hist_run=True)
        p.load = types.SimpleNamespace(last_budget=types.SimpleNamespace(level="normal"), cursor_hist=0)
        p.hist_busy = False
        p._hist_tick()
        after_bars = sorted(p.sets.bars)
        after_rows = sorted(p.sets._hist_rows)
    finally:
        pt.SYMBOLS[:] = saved
    ok = "GONE-USDT" in before and "GONE-USDT" not in after_bars and "GONE-USDT" not in after_rows \
        and "HELD-USDT" in after_bars and {"A-USDT", "B-USDT"} <= set(after_bars)
    return ok, f"rows before={before}; bars after={after_bars}; rows after={after_rows}"


def history_thread_keeps_real_history_and_tops_up_a_partial_one():
    """Two states the start-up check does not reach. (a) Every symbol holds its full real history and the venue is
    offline: the seed padding must not replace it (no fetch can repair it). (b) Every symbol holds a partial real
    history (above min_bars, under 90 % of the lookback): the thread must fetch the rest, not take it as complete."""
    import types
    import set_engine as se
    from common import overlay, SMALL

    class _Offline:
        def gather_public(self, reqs, timeout=4.2):
            return [(path, extra, {"code": 0, "data": []}) for path, extra in reqs]

    def run(api, real_n, live=False):
        p = _bare_pulse()
        p.api = api
        p.sets = se.SetBook()
        p.sets.load(overlay(**SMALL))
        p.sets.lookback = 1920
        p.sets.refresh_s = 10 ** 6
        p.sets.last_run = time.time() - p.sets.retry_s - 1     # due on the retry clock, not on the refresh clock
        for k, s_ in enumerate(pt.SYMBOLS):
            p.sets.bars[s_] = [[50.0 + k + i * 0.01, 50.2 + k + i * 0.01, 49.8 + k + i * 0.01, 50.1 + k + i * 0.01, 1.0]
                               for i in range(real_n)]
            p.klines_tf["1m"][s_] = [[100.0, 100.0, 100.0, 100.0, 0.0]] * 60       # the seed padding
            p.real_1m[s_] = 0
            if live:      # rows built from live ticks since start: zero volume, never equal to the exchange's rows
                p.klines_tf["1m"][s_] = [[100.0, 100.0, 100.0, 100.0, 0.0]] * 20 + [[70.0 + i * 0.003, 70.2, 69.9, 70.1, 0.0] for i in range(40)]
                p.real_1m[s_] = 40
        p.open = {}
        p._budget = lambda: types.SimpleNamespace(scan_chunk=0, hist_chunk=6, hist_run=False)
        p.load = types.SimpleNamespace(last_budget=types.SimpleNamespace(level="normal"), cursor_hist=0)
        p.hist_busy = False
        p._hist_tick()
        sizes = [len(p.sets.bars.get(s_) or []) for s_ in pt.SYMBOLS]
        flat = sum(1 for s_ in pt.SYMBOLS for b in (p.sets.bars.get(s_) or []) if b[0] == b[1] == b[2] == b[3] == 100.0)
        return sizes, flat

    saved = list(pt.SYMBOLS)
    try:
        pt.SYMBOLS[:] = ["A-USDT", "B-USDT", "C-USDT", "D-USDT"]
        kept, kept_flat = run(_Offline(), 1920)
        topped, topped_flat = run(_PagedKlines(), 500)
        livetail, _ = run(_Offline(), 1920, live=True)
        boot_live, boot_flat = run(_Offline(), 0, live=True)      # no history yet: the live-built real rows bootstrap it, never the padding
        boot_none, _ = run(_Offline(), 0)                          # no history and no real rows: nothing is ingested
    finally:
        pt.SYMBOLS[:] = saved
    ok = all(n == 1920 for n in kept) and kept_flat == 0 and all(n == 1920 for n in topped) and topped_flat == 0 and all(n == 1920 for n in livetail) \
         and all(n == 40 for n in boot_live) and boot_flat == 0 and all(n == 0 for n in boot_none)
    return ok, f"bootstrap from live-built rows {boot_live} (no real rows {boot_none}); live-built tail on a full history -> {livetail}; offline full history -> {kept} (padded rows {kept_flat}); partial 500 -> {topped} (padded rows {topped_flat})"


def no_other_lane_touches_a_set_position():
    """Independent lanes: the coordinator's rearrange may not swap out a Set position, the Block and DCA add-ons may not
    add to it (an add-on re-anchors the entry, stop and target the Set validated), and the global 6 h hold cap leaves it
    to its own time exit. The same position without a Set stays open to all of them."""
    import types

    def run():
        out = {}
        p, sym, pos = _lane_pulse()
        p.px[sym] = pos.entry * 1.002         # one step and more in profit: Block may stack on a position without a Set
        out["rearrange"] = (len(p.rearrange_opens()), None)
        # Block: an add-on candidate reaches evaluate_counts; a Set position must never get there
        seen = {"block": 0, "dca": 0}
        lane = types.SimpleNamespace(active=True, base_qty=1.0, confirmed_add=0.0, legs=[], satisfied={})
        p.block = types.SimpleNamespace(
            enabled=True, lanes={f"{sym}:{pos.side}": lane}, key=lambda s_, side: f"{s_}:{side}", save=lambda: None,
            evaluate_counts=lambda *a, **k: seen.__setitem__("block", seen["block"] + 1) or [], pick_emit=lambda rows: None,
            on_parent_close=lambda *a, **k: None, register_parent=lambda *a, **k: None)
        p.strat_block = True
        p.halted = False
        p.block_last_emit = 0.0
        p.cooldown = {}
        p.entries_blocked = lambda: False
        p.missing_controls = lambda _p: False
        p.score = lambda s_: (1 if pos.side == "LONG" else -1, "x", 0.9)
        p.dca = types.SimpleNamespace(
            enabled=True, lanes={}, key=lambda s_, side: f"{s_}:{side}", skips=0,
            due=lambda *a, **k: seen.__setitem__("dca", seen["dca"] + 1) or None,
            on_close=lambda *a, **k: None, drop=lambda *a, **k: None, attach=lambda *a, **k: None)
        p.dca_last_emit = 0.0
        p.dca_fail_cd = {}
        p.max_book_notional = lambda: 1e9
        p.maybe_block_adds()
        p.maybe_dca_adds()
        set_adds = dict(seen)
        # hold cap: a Set position past 6 h with a 12 h time exit stays open
        p.sets.hist_time_bars = 720
        p.sets.scratch_min = 0.05
        pos.opened_at = time.time() - pt.MAX_HOLD_S - 60
        p.manage()
        set_held = sym in p.open
        # the same position without a Set: every lane may act
        pos.set_id = ""
        out["rearrange_free"] = len(p.rearrange_opens())
        seen["block"] = seen["dca"] = 0
        p.maybe_block_adds()
        p.maybe_dca_adds()
        free_adds = dict(seen)
        return out["rearrange"][0], out["rearrange_free"], set_adds, free_adds, set_held

    set_swap, free_swap, set_adds, free_adds, set_held = _isolated(run)
    ok = set_swap == 0 and free_swap == 1 and set_adds == {"block": 0, "dca": 0} \
        and free_adds["block"] == 1 and free_adds["dca"] == 1 and set_held
    return ok, (f"Set position: rearrange candidates={set_swap} add-on checks={set_adds} held past 6 h={set_held}; "
                f"without a Set: rearrange candidates={free_swap} add-on checks={free_adds}")


def a_set_position_gets_no_add_on_lane():
    """Opening a Set position registers no Block parent and no DCA lane: the add-on lanes belong to positions without a
    Set. A position without a Set still gets its Block parent."""
    def run():
        sym = "BTC-USDT"
        contracts = {sym: pt.Contract(symbol=sym, min_qty=0.001, step=0.001, qprec=3, pprec=2, min_usdt=5.0, max_lev=150)}
        res = []
        for with_set in (True, False):
            if os.path.exists(pt.OPEN_PATH):
                os.remove(pt.OPEN_PATH)          # each run starts with an empty book
            p = pt.Pulse(_StubExchange(executed="1", requested="1"), contracts)
            p.boot_ts = 0.0
            p.px[sym] = 100.0
            p.price_ts[sym] = pt.time.time()
            p.available = 50.0
            p.last_bal = pt.time.time()
            p.halted = False
            p.entry_sense = lambda *a, **k: None
            st = p.sets.by_idx[0] if (with_set and p.sets.by_idx) else None
            if not with_set:
                p.sets.use_historic_gate = False  # test Block's independent non-Set parent
            p.sets.pick_any = lambda pack, _st=st, **kw: _st
            p.sets.pick_trail = lambda pack: None
            parents = []
            p.block.register_parent = lambda *a: parents.append(a)
            p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
            pos = p.open.get(sym)
            res.append((bool(pos and pos.set_id), len(parents)))
        return res
    (set_open, set_parents), (free_open, free_parents) = _isolated(run)
    ok = set_open and set_parents == 0 and not free_open and free_parents == 1
    return ok, f"Set position opened={set_open} Block parents={set_parents}; position without a Set parents={free_parents}"


def a_venue_ban_pauses_the_flatten_and_a_refused_close_is_logged():
    """While the venue bans the order path (path_cd), the unprotected-position flatten waits instead of cancelling stops
    and calling the close every cycle. Once the ban is over the flatten runs, and a close the venue refuses is logged with
    the venue's reason."""
    def run():
        p, sym = _enter(_StubExchange(executed="1", requested="1"))
        pos = p.open[sym]
        pos.sl_oid = pos.tp_oid = pos.sec_sl_oid = pos.sec_tp_oid = ""
        pos.opened_at = time.time() - 900.0
        p.ensure_controls = lambda _p: None
        p.ctrl_skip = {}
        calls = []

        def refused(_p):
            calls.append(_p.symbol)
            p.last_error = f"close {_p.symbol} over 20 error code:109420 requests"
            return False, _p.entry
        p.market_close = refused
        p.cancel_controls = lambda s_: None
        lines = []
        saved_log = pt.log
        pt.log = lambda msg, *a, **k: lines.append(msg)
        try:
            if not hasattr(p.api, "path_cd"):
                p.api.path_cd = {}
            p.api.path_cd["/openApi/swap/v2/trade/order"] = time.time() + 300.0
            p.priority_controls()
            banned = len(calls)
            p.api.path_cd["/openApi/swap/v2/trade/order"] = 0.0
            p.priority_controls()
            free = len(calls)
        finally:
            pt.log = saved_log
        logged = any(l.startswith(f"CLOSE FAIL {sym}") and "109420" in l for l in lines)
        return banned, free, logged, sym in p.open
    banned, free, logged, kept = _isolated(run)
    ok = banned == 0 and free == 1 and logged and kept
    return ok, f"close calls during the ban={banned} after={free} refusal logged={logged} position kept={kept}"


def position_size_follows_the_balance_and_the_volume_factor():
    """Position notional = balance x sizePctOfBalance % x volumeFactor: it doubles with the balance and with the factor,
    falls back to targetNotional x volumeFactor before a balance is read (or with the percentage at 0), and size_qty raises
    it to the venue's minimum order on a small account."""
    p = _bare_pulse()
    p.volume_factor, p.size_pct = 1.5, 1.0
    p.size_max = 1e9                              # the ceiling has its own check
    out = {}
    for bal in (0.0, 8.92, 1000.0, 2000.0):
        p.equity = bal
        out[bal] = round(p.sized_notional(), 6)
    p.equity, p.volume_factor = 1000.0, 3.0
    doubled_factor = round(p.sized_notional(), 6)
    p.size_pct = 0.0
    fixed = round(p.sized_notional(), 6)
    # a 9 USDT account: 1 % x 1.5 = 0.13 USDT, under a 5 USDT minimum order -> the minimum
    p.size_pct, p.volume_factor, p.equity, p.available = 1.0, 1.5, 8.92, 8.92
    c = pt.Contract(symbol="X-USDT", min_qty=0.001, step=0.001, qprec=3, pprec=4, min_usdt=5.0, max_lev=50)
    p.leverage_for = lambda c_: 50
    q = p.size_qty(c, 100.0)
    ok = (out[0.0] == round(pt.TARGET_NOTIONAL * 1.5, 6) and abs(out[1000.0] - 15.0) < 1e-9
          and abs(out[2000.0] - 30.0) < 1e-9 and abs(doubled_factor - 30.0) < 1e-9
          and fixed == round(pt.TARGET_NOTIONAL * 3.0, 6) and abs(q * 100.0 - 5.0) < 1e-6)
    return ok, (f"notional by balance={out}; factor 3 at 1000={doubled_factor}; pct 0={fixed}; "
                f"9 USDT account order={round(q * 100.0, 4)} USDT (venue minimum 5)")


def drawdown_halt_follows_its_setting():
    """ddHaltPct (percent of the active session's start equity, default 80) halts entries on a drawdown; 0 switches the halt off, and a
    drawdown halt already latched clears on the next balance read. The minimum-equity guard stays on either way."""
    def run():
        out = {}
        for pct, eq in ((None, 15.0), (0.0, 30.0), (0.0, 15.0), (0.0, 0.1)):
            p, sym = _enter(_StubExchange(executed="1", requested="1"))
            p.start_eq = p.session_eq = 100.0
            p.dd_halt = pt.DD_HALT if pct is None else pct / 100.0
            p.halted, p.halt_reason = (True, "drawdown halt") if pct == 0.0 else (False, None)   # latched from before
            p.api.get = lambda path, extra=None, _e=eq: {"code": 0, "data": {"balance": {"equity": str(_e), "availableMargin": str(_e)}}}
            p.refresh_balance()
            out[f"{pct}:{eq}"] = (p.halted, p.halt_reason)
        return out
    out = _isolated(run)
    ok = out["None:15.0"] == (True, "drawdown halt") and out["0.0:30.0"] == (False, None) and out["0.0:15.0"] == (False, None) and out["0.0:0.1"][0] is True \
        and "below min" in str(out["0.0:0.1"][1])
    return ok, f"(halted, reason) by (ddHaltPct, equity from start 100): {out}"


def drawdown_halt_measures_from_the_active_session():
    """The halt is 80 % (ddHaltPct) of the equity the active session started with, not of an older stored start equity: a stored start of 100 with a
    session that began at 10 does not halt at equity 5 (50 % below the session), halts at 1.5 (85 % below), and the stored basis ("start") halts at
    5. A new session (explicit Start) re-baselines."""
    def run(basis, eq, start=100.0, session=10.0):
        p, sym = _enter(_StubExchange(executed="1", requested="1"))
        p.start_eq, p.session_eq, p.dd_basis, p.dd_halt = start, session, basis, 0.80
        p.halted, p.halt_reason = False, None
        p.api.get = lambda path, extra=None, _e=eq: {"code": 0, "data": {"balance": {"equity": str(_e), "availableMargin": str(_e)}}}
        p.refresh_balance()
        return (p.halted, p.halt_reason, round(p.session_eq, 4))
    out = _isolated(lambda: {"session eq 5": run("session", 5.0), "session eq 1.5": run("session", 1.5), "stored start eq 5": run("start", 5.0),
                             "first read sets the session": run("session", 7.0, session=0.0)})
    ok = (out["session eq 5"][0] is False and out["session eq 1.5"] == (True, "drawdown halt", 10.0)
          and out["stored start eq 5"] == (True, "drawdown halt", 10.0) and out["first read sets the session"] == (False, None, 7.0))
    return ok, f"(halted, reason, session start) {out}"


def no_entry_on_a_symbol_that_failed_the_data_check():
    import types
    p = _bare_pulse()
    p.sets = types.SimpleNamespace(enabled=True, use_historic_gate=True, symbol_ok=lambda s: s != "BAD-USDT", entry_conf=0.5,
                                   pick_any=lambda pack: types.SimpleNamespace(id="x"), pack_open=lambda pack: True)
    p.strat_ind = False
    p.px = {"BAD-USDT": 1.0, "OK-USDT": 1.0}
    p.occupying = lambda *a, **k: False
    bad = p.entry_sense("BAD-USDT", 1, "gen:x", 0.9, "general")
    good = p.entry_sense("OK-USDT", 1, "gen:x", 0.9, "general")
    return bad == "data-quality" and good != "data-quality", f"failed symbol -> {bad}; clean symbol -> {good}"


def short_history_cannot_pass_symbol_quality():
    """A symbol absent from the quality map can still be missing the minimum history."""
    b = pt.SetBook()
    b.min_bars = 20
    b.bars["TEST-USDT"] = [[100.0, 101.0, 99.0, 100.0, 1.0] for _ in range(19)]
    short = not b.symbol_ok("TEST-USDT")
    b.bars["TEST-USDT"].append([100.0, 101.0, 99.0, 100.0, 1.0])
    enough = b.symbol_ok("TEST-USDT")
    b.bars["TEST-USDT"][-1] = [100.0, 300.0, 99.0, 300.0, 1.0]
    spike = not b.symbol_ok("TEST-USDT") and b.quality.get("TEST-USDT") == "spike"
    return short and enough and spike, f"short blocked={short}; sufficient bars={enough}; spike blocked={spike}"


def automatic_memory_budget_responds_to_headroom():
    """The adaptive budget leaves system reserve, shrinks under pressure and honors its cap."""
    import load_engine as le
    governor = le.LoadGovernor()
    governor.configure({"rssAutoLimit": True, "rssSoftMb": 768, "rssHardMb": 1536})
    old = le.system_memory_mb
    try:
        le.system_memory_mb = lambda: (8192.0, 4000.0)
        governor.observe(n_sym=30, rss_mb=500.0)
        rich = (governor.soft_limit(30), governor.hard_limit(30), governor.level)
        governor._auto_memory_at = 0
        le.system_memory_mb = lambda: (8192.0, 256.0)
        governor.observe(n_sym=30, rss_mb=500.0)
        tight = (governor.soft_limit(30), governor.hard_limit(30), governor.level)
        governor._auto_memory_at = 0
        le.system_memory_mb = lambda: None
        governor.observe(n_sym=30, rss_mb=500.0)
        fallback = (governor.soft_limit(30), governor.hard_limit(30))
    finally:
        le.system_memory_mb = old
    ok = (500 < rich[1] <= 8192 * .4 and tight[1] == 500 and tight[0] < tight[1]
          and tight[2] in ("overload", "critical") and fallback == (768, 1536))
    return ok, f"ample headroom={rich}; constrained headroom={tight}; no telemetry fallback={fallback}"


def set_control_fallback_never_moves_inside_lane():
    """A rejected Set control cannot fall back to a closer stop or target."""
    p = _bare_pulse()
    sym = "T-USDT"
    p.px, p.last_px = {sym: 100.0}, {sym: 100.0}
    p.contracts = {sym: pt.Contract(symbol=sym, min_qty=.001, step=.001, qprec=3, pprec=2, min_usdt=1, max_lev=50)}
    p.cid = lambda *a, **k: "test-cid"
    class Refuse:
        path_cd = {}
        def __init__(self): self.prices = []
        def post(self, path, body):
            self.prices.append(float(body['stopPrice']))
            return {"code": 100400, "msg": "invalid trigger price"}
    results = []
    for side, kind, intended in (("LONG", "sl", 95.0), ("LONG", "tp", 105.0),
                                 ("SHORT", "sl", 105.0), ("SHORT", "tp", 95.0)):
        p.api, p.ctrl_skip = Refuse(), {}
        pos = pt.Position(symbol=sym, side=side, qty=1, entry=100, opened_at=1,
                          sl=95 if side == "LONG" else 105, tp=105 if side == "LONG" else 95,
                          peak=100, set_id="general:1m:sl1.00:st8")
        oid = p.place_ctrl(pos, "sec-" + kind, intended)
        prices = p.api.prices
        safe = all((v <= intended if side == "LONG" else v >= intended) if kind == "sl"
                   else (v >= intended if side == "LONG" else v <= intended) for v in prices)
        results.append((side, kind, prices, not oid and bool(prices) and safe))
    return all(r[-1] for r in results), f"fallbacks={results}"


def control_pair_records_the_accepted_fallback_price():
    p = _bare_pulse()
    p.ctrl_skip, p._oo_cache = {}, {}
    p.api = types.SimpleNamespace(batch_place=lambda orders: {"code": 1}, path_cd={})
    p.desired_sl_tp = lambda pos: (95.0, 105.0, 95.0, 105.0)
    p._ctrl_body = lambda pos, kind, price: {"type": kind, "stopPrice": price}
    p.cid = lambda *a, **k: "test-cid"
    def accepted(pos, kind, price):
        if kind == "sec-sl": pos.sec_sl = 94.0
        else: pos.sec_tp = 106.0
        return "123" if kind == "sec-sl" else "456"
    p.place_ctrl = accepted
    pos = pt.Position(symbol="T-USDT", side="LONG", qty=1, entry=100, opened_at=1, sl=95, tp=105, peak=100)
    p.place_ctrl_pair(pos)
    ok = (pos.sl, pos.tp, pos.sec_sl, pos.sec_tp) == (94.0, 106.0, 94.0, 106.0) and pos.controls_ok
    return ok, f"recorded SL/TP=({pos.sl}, {pos.tp}); controls={pos.controls_ok}"


def positions_read_is_nonblocking_and_rejects_stale_book():
    import concurrent.futures
    import threading
    p = _bare_pulse()
    entered, release = threading.Event(), threading.Event()
    class Slow:
        def __init__(self): self.calls = 0
        def get(self, path):
            self.calls += 1
            entered.set()
            release.wait(2)
            return {"code": 0, "data": []}
    p.api, p.open = Slow(), {}
    p._recon_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    p._recon_future = None
    applied = []
    p.adopt_exchange_positions = lambda reply: applied.append(reply)
    try:
        p.schedule_position_read()
        entered.wait(1)
        t0 = pt.time.perf_counter()
        for _ in range(20):
            p.schedule_position_read()
            p.poll_position_read()
        elapsed = pt.time.perf_counter() - t0
        calls = p.api.calls
        release.set()
        p._recon_future.result(timeout=2)
        fresh = p.poll_position_read() and len(applied) == 1
        p.schedule_position_read()
        p._recon_future.result(timeout=2)
        p.open["T-USDT"] = pt.Position(symbol="T-USDT", side="LONG", qty=1, entry=100,
                                      opened_at=1, sl=95, tp=105, peak=100)
        discarded = not p.poll_position_read() and len(applied) == 1
    finally:
        release.set()
        p._recon_pool.shutdown(wait=True)
    ok = elapsed < 0.1 and calls == 1 and fresh and discarded
    return ok, f"blocked read poll={elapsed*1000:.2f}ms calls={calls}; fresh applied={fresh}; changed-book snapshot discarded={discarded}"


def _signal_book():
    """A SetBook on the shipped x01 overlay with one symbol's history ending on a known completed minute."""
    import json
    import set_engine as se
    b = se.SetBook()
    b.load(json.load(open(os.path.join(PULSE, "overlay-bingx-x01.json"))))
    return b, se


def live_entries_are_the_replays_signal_on_the_completed_minute():
    """Under the Base gate a live entry is the replay's: the signal pack_signals gives the last completed bar of the Set
    history (highest confidence at or above setEntryConf), taken within ENTRY_FRESH_S of that minute's close, decided once
    per symbol and minute. A history that does not end on that minute, or a stale minute, gives no entry."""
    b, se = _signal_book()
    sym = "A-USDT"
    t_open = 1_790_000_040.0              # open time of the newest completed minute
    full = se.synth_trend(600, step=0.09, noise=0.05)
    hits, misses, wrong = 0, 0, []
    old_syms = pt.SYMBOLS
    try:
        pt.SYMBOLS = [sym]
        for n in range(320, 600, 7):
            bars = full[:n]
            b.bars.clear()
            b.bar_t.clear()
            b.ingest_bars(sym, bars[:-1], last_t=t_open - 60.0)        # the minute has closed, the venue has not served it yet
            p = _bare_pulse()
            p.sets = b
            p.open = {}
            early = p.set_entry_candidates(now=t_open + 60.0 + 2.0)
            b.ingest_bars(sym, bars, tail=True, last_t=t_open)
            got = p.set_entry_candidates(now=t_open + 60.0 + 5.0).get(sym)
            if early:
                wrong.append((n, "entry before the minute reached the history", early))
            sig = se.pack_signals(bars, b.packs, b.ind_settings, 0.0, len(bars) - 1)
            want = None
            for pk in b.packs:
                d, c, _w = sig[pk][-1]
                if d != 0 and c >= b.entry_conf and (want is None or c > want[1]):
                    want = (pk, c, d)
            if want is None:
                misses += 1
                if got is not None:
                    wrong.append((n, "entry without a signal", got))
                continue
            hits += 1
            if got is None or got[2] != want[2] or abs(got[0] - want[1]) > 1e-12 or not got[3].startswith(f"set:{want[0]}:"):
                wrong.append((n, want, got))
            late = p.set_entry_candidates(now=t_open + 60.0 + pt.ENTRY_FRESH_S + 1.0)
            if late:
                wrong.append((n, "late entry", late))
            q = _bare_pulse()
            q.sets = b
            q.open = {}
            if q.set_entry_candidates(now=t_open + 120.0 + 5.0):
                wrong.append((n, "entry on a minute the history does not hold", None))
            if pt.pack_of_reason(got[3]) != want[0]:
                wrong.append((n, "pack", pt.pack_of_reason(got[3])))
    finally:
        pt.SYMBOLS = old_syms
    return hits >= 3 and misses >= 1 and not wrong, f"signal bars={hits} quiet bars={misses} mismatches={wrong[:3]}"


def completed_minutes_only_reach_the_set_history():
    """The venue's last kline row is the minute still forming: it is not a bar. The v3 answer (newest first) is ordered."""
    p = _bare_pulse()
    minute = 1_790_000_100.0
    rows = [{"open": "2", "high": "3", "low": "1", "close": "2.5", "volume": "1", "time": int((minute - 60 * k) * 1000)} for k in range(0, 5)]
    bars, last_t = p._closed_klines(rows, now=minute + 20.0)          # newest first, the first row still forming
    ordered = p._parse_klines(list(reversed(rows))) == p._parse_klines(rows)
    ok = len(bars) == 4 and last_t == minute - 60.0 and ordered
    return ok, f"bars={len(bars)} last={last_t} (want {minute - 60.0}) v3 order fixed={ordered}"


def completed_minutes_do_not_bridge_gaps_or_bad_rows():
    """The timestamp-free history can represent only a contiguous series of completed minutes."""
    p = _bare_pulse()
    minute = 1_790_000_100.0
    def bar(k):
        return {"open": "2", "high": "3", "low": "1", "close": str(2 + k / 100),
                "volume": "1", "time": int((minute - 60 * k) * 1000)}
    rows = [bar(k) for k in (0, 1, 2, 4, 5, 6)]
    bars, last_t = p._closed_klines(rows + [dict(rows[1])], now=minute + 20)
    bad = [dict(r) for r in rows]
    del bad[1]["close"]
    rejected = p._closed_klines(bad, now=minute + 20)
    ok = len(bars) == 2 and [b[3] for b in bars] == [2.02, 2.01] and last_t == minute - 60 and rejected == ([], None)
    return ok, f"contiguous closes={[b[3] for b in bars]}, duplicate removed, malformed series rejected={rejected == ([], None)}"


def entry_selection_keeps_packs_and_axis_lanes_independent():
    """An invalid stronger pack, or a blocked positive lane, must not hide an independently valid entry."""
    from coord_engine import Coordinator
    sym = "X-USDT"
    minute = 1_790_000_100.0
    choices = {"strong": None, "valid": types.SimpleNamespace(lane="pos")}
    class Sets:
        packs = ["strong", "valid"]
        bar_t = {sym: minute - 60}
        def entry_signals(self, _sym, _t):
            return [("strong", 1, .95, "strong-signal"), ("valid", -1, .8, "valid-signal")]
        def pick_any(self, pack, allowed_lanes=None):
            row = choices[pack]
            return row if row is not None and (allowed_lanes is None or row.lane in allowed_lanes) else None
        def lane_of(self, row):
            return row.lane
    p = _bare_pulse(); p.sets = Sets(); p.open = {}; p.coord = Coordinator()
    p.coord.scopes = {"lane:pos": {"allow": False}, "pack:valid:pos": {"allow": True},
                      "pack:strong:pos": {"allow": False}, "pack:strong:neg": {"allow": True}}
    old = pt.SYMBOLS
    try:
        pt.SYMBOLS = [sym]
        first = p.set_entry_candidates(now=minute + 5).get(sym)
        choices["strong"] = types.SimpleNamespace(lane="pos")
        second = p.set_entry_candidates(now=minute + 6).get(sym)  # same signal memo, newly changed validity
        choices["valid"] = None
        choices["strong"] = types.SimpleNamespace(lane="neg")
        third = p.set_entry_candidates(now=minute + 7).get(sym)
    finally:
        pt.SYMBOLS = old
    # One pack's losing pooled lane is advisory to another pack with its own winning evidence.
    co = Coordinator()
    co.load({}, {"axisLastEnabled": True, "axisPrevEnabled": False, "axisPauseEnabled": False})
    winners = [{"pnl_pct": .01, "pnl": .01} for _ in range(4)]
    losses = [{"pnl_pct": -.01, "pnl": -.01} for _ in range(4)]
    co.judge_scopes({"lane:pos": losses, "pack:valid:pos": winners, "pack:strong:pos": losses})
    scope = (co.scope_allows("valid", "pos"), co.scope_allows("strong", "pos"))
    alternative = types.SimpleNamespace(lane="neg")
    class TwoLanes(Sets):
        def pick_any(self, _pack, allowed_lanes=None):
            if allowed_lanes is None or "pos" in allowed_lanes:
                return types.SimpleNamespace(lane="pos")
            return alternative if "neg" in allowed_lanes else None
    p.sets = TwoLanes()
    picked, blocked = p._scope_pick("strong")
    ok = (first and first[3].startswith("set:valid:") and second and second[3].startswith("set:valid:")
          and third and third[3].startswith("set:strong:") and picked is alternative and not blocked
          and scope == ((True, ""), (False, "pack:strong:pos")))
    return bool(ok), f"selected={first}, {second}, {third}; scopes={scope}; negative alternative={picked is alternative}"


def entry_does_not_open_when_the_set_disappears_after_sense():
    """A validation change between entry_sense and placement cannot create an untagged live position."""
    def run():
        sym = "BTC-USDT"
        venue = _StubExchange(executed="1", requested="1")
        contract = pt.Contract(symbol=sym, min_qty=.001, step=.001, qprec=3, pprec=2, min_usdt=5, max_lev=50)
        p = pt.Pulse(venue, {sym: contract})
        p.boot_ts, p.available, p.halted = 0.0, 50.0, False
        p.px[sym] = 100.0
        p.price_ts[sym] = pt.time.time()
        p.entry_sense = lambda *args, **kwargs: None
        p.sets.pick_any = lambda _pack, **kw: None
        p.place(sym, 1, "set:indications:signal", .8)
        orders = [body for path, body in venue.posts if path.endswith("/trade/order")]
        return bool(p.sets.enabled and p.sets.use_historic_gate), sym in p.open, len(orders)
    gate, opened, orders = _isolated(run)
    return gate and not opened and orders == 0, f"gate on={gate}; opened={opened}; venue orders={orders}"


def an_exit_set_position_never_takes_its_tp_and_a_base_set_always_does():
    """exitIgnoreTp is its own Set type: a base Set's lane exits at its TP, an Exit Set's lane never does (its venue TP
    order sits out at the safety distance), a trailing Set follows the book's setting; the client id tells the twins apart."""
    b, se = _signal_book()
    p = _bare_pulse()
    p.sets = b
    base = next(x for x in b.by_idx if x.kind == "base")
    twin = next(x for x in b.by_idx if x.kind == "exit")
    trail = next(x for x in b.by_idx if x.kind == "trail")
    honor = {k: p.lane_honors_tp(types.SimpleNamespace(set_id=st.id)) for k, st in (("base", base), ("exit", twin), ("trail", trail))}
    round_trip = all((p.parse_track(p.cid("o", set_id=st.id, pack=st.pack, set_idx=st.idx)) or {}).get("set_id") == st.id
                     for st in (base, twin, trail))
    same = next(x for x in b.by_idx if x.kind == "exit" and x.pack == base.pack and x.step == base.step and abs(x.sl_ratio - base.sl_ratio) < 1e-9)

    def by_content(st_):
        """Decode with a grid index that points at another Set (as after a grid change): only the content can decide."""
        cid = p.cid("o", set_id=st_.id, pack=st_.pack, set_idx=st_.idx)
        k = len(pt.TAG) + 8
        cid = cid[:k] + f"{trail.idx:04d}" + cid[k + 4:]
        return (p.parse_track(cid) or {}).get("set_id")
    twins = {"base": by_content(base), "exit": by_content(same)}
    ok = honor == {"base": True, "exit": False, "trail": not b.exit_sets} and round_trip and twins == {"base": base.id, "exit": same.id}
    return ok, f"honours TP {honor} cid round trip={round_trip} twins by content={twins}"


def the_entry_loop_places_the_set_signal_and_not_the_live_score():
    """maybe_entries under the Base gate: the order placed is the Set history's signal of the completed minute (its pack,
    direction and confidence), whatever the live general score says (here the opposite direction at a higher confidence)."""
    from coord_engine import Coordinator
    b, se = _signal_book()
    sym = "A-USDT"
    t_open = float(int(time.time() // 60) * 60 - 60)
    full = se.synth_trend(600, step=0.09, noise=0.05)
    bars = sig = None
    for n in range(320, 600, 7):
        b.bars.clear()
        b.bar_t.clear()
        b.ingest_bars(sym, full[:n], last_t=t_open)
        sig = b.entry_signal(sym, t_open)
        if sig:
            bars = full[:n]
            break
    if not bars:
        return False, "no signal bar in the fixture"
    p = _bare_pulse()
    p.sets = b
    p.open = {}
    p.halted = False
    p.closed = []
    p.strategy_closes = lambda: []
    p.coord = Coordinator()
    p.coord.load({}, {})
    p.strat_ind = p.strat_general = True
    p.indications = types.SimpleNamespace(settings={"enabled": True}, pick_entry=lambda *a, **k: None, best=lambda s: None, primary=lambda s: None)
    p.score = lambda s: (-sig[1], "live", 0.99)            # the live score: opposite direction, higher confidence
    p.entries_blocked = lambda: False
    p.rearrange_opens = lambda: []
    p.maybe_block_adds = p.maybe_dca_adds = lambda: None
    p.available = 100.0
    p.cooldown, p.skip_log, p.contracts, p.px = {}, {}, {}, {}
    p.avail_notional = lambda *a: 100.0
    placed = []
    p.place = lambda s_, d, why, conf: placed.append((s_, d, why, round(conf, 6)))
    old_syms, old_fresh = pt.SYMBOLS, pt.ENTRY_FRESH_S
    try:
        pt.SYMBOLS = [sym]
        pt.ENTRY_FRESH_S = 61.0                              # the check must not depend on the second of the minute it runs in
        p.maybe_entries()
    finally:
        pt.SYMBOLS, pt.ENTRY_FRESH_S = old_syms, old_fresh
    want = [(sym, sig[1], None, round(sig[2], 6))]
    ok = len(placed) == 1 and placed[0][1] == sig[1] and placed[0][3] == want[0][3] and placed[0][2].startswith(f"set:{sig[0]}:")
    return ok, f"placed={placed} set signal={sig[:3]}"


def the_entry_loop_mirrors_the_set_signal_without_any_last_n_validation():
    """Once a Set passes its gates the entry is mirrored to the exchange as the replay's signal: no last-N validation sits between them. Thirty
    straight losing closes, every Axis (last, previous, continuation, pause) switched on, the coordinator's last-N PF far under its minimum:
    the signal is still placed, in the same direction and pack, and the coordinator only advises (it records the failing evidence, it never
    blocks)."""
    from coord_engine import Coordinator
    b, se = _signal_book()
    sym = "A-USDT"
    t_open = float(int(time.time() // 60) * 60 - 60)
    full = se.synth_trend(600, step=0.09, noise=0.05)
    sig = None
    for n in range(320, 600, 7):
        b.bars.clear()
        b.bar_t.clear()
        b.ingest_bars(sym, full[:n], last_t=t_open)
        sig = b.entry_signal(sym, t_open)
        if sig:
            break
    if not sig:
        return False, "no signal bar in the fixture"
    losses = [pt.Closed(t=time.time() - 60 * (40 - k), symbol="X-USDT", side="LONG", qty=1.0, entry=100.0, exit=99.0, pnl=-0.01, pnl_pct=-0.01,
                        reason="sl", hold_s=60.0, set_id="s", pack="indications") for k in range(30)]
    p = _bare_pulse()
    p.sets = b
    p.open = {}
    p.halted = False
    p.closed = losses
    p.strategy_closes = lambda: list(losses)
    p.coord = Coordinator()
    p.coord.load({}, {"axisPrevEnabled": True, "axisLastEnabled": True, "axisContEnabled": True, "axisPauseEnabled": True, "windowsLinked": False, "pfWindow": 8})
    p.strat_ind = p.strat_general = True
    p.indications = types.SimpleNamespace(settings={"enabled": True}, pick_entry=lambda *a, **k: None, best=lambda s: None, primary=lambda s: None)
    p.score = lambda s: (0, "live", 0.0)
    p.entries_blocked = lambda: False
    p.rearrange_opens = lambda: []
    p.maybe_block_adds = p.maybe_dca_adds = lambda: None
    p.available = 100.0
    p.cooldown, p.skip_log, p.contracts, p.px = {}, {}, {}, {}
    p.avail_notional = lambda *a: 100.0
    placed = []
    p.place = lambda s_, d, why, conf: placed.append((s_, d, why))
    old_syms, old_fresh, old_max = pt.SYMBOLS, pt.ENTRY_FRESH_S, pt.MAX_OPEN
    try:
        pt.SYMBOLS = [sym]
        pt.ENTRY_FRESH_S = 61.0
        pt.MAX_OPEN = 5
        p.maybe_entries()
    finally:
        pt.SYMBOLS, pt.ENTRY_FRESH_S, pt.MAX_OPEN = old_syms, old_fresh, old_max
    advice = dict(p.coord.last or {})
    ok = len(placed) == 1 and placed[0][1] == sig[1] and placed[0][2].startswith(f"set:{sig[0]}:") and advice.get("allow") is False
    return ok, f"placed={placed}; the coordinator advised allow={advice.get('allow')} reasons={(advice.get('reasons') or [])[:2]}"


def a_set_close_waits_only_its_own_lane_and_never_pauses_other_lanes():
    """No cross-lane wait on a Set: after a Set close the symbol waits the Set lane's own cooldown (setCooldownBars, as the
    replay), however many losses came before, and a run of Set losses never pauses the book. The Exit lane keeps its
    escalating backoff and its cold-streak pause. A stop-out within 30 s bans the symbol on either lane (venue safety)."""
    p = pt.Pulse.__new__(pt.Pulse)
    p.position_cost_pct = 0.12
    p.ignore_syms = {}
    p.sets = types.SimpleNamespace(cooldown_bars=2)
    set_waits = [p.symbol_backoff("A-USDT", -0.01, 300.0, "sl", set_lane=True) for _ in range(5)]
    exit_waits = [p.symbol_backoff("B-USDT", -0.01, 300.0, "sl") for _ in range(3)]
    p.symbol_backoff("C-USDT", -0.1, 3.0, "sl", set_lane=True)
    fast_ban = p.ignore_syms.get("C-USDT", 0.0) - time.time() > 1700

    def run(set_id):
        q, sym, pos = _lane_pulse(set_id)
        q.consec_loss = 7
        q.cooldown.pop("__book__", None)
        q.close_pos(pos, pos.entry * 0.99, "sl")
        return q.cooldown.get("__book__", 0.0) > time.time()
    paused_by_set = _isolated(lambda: run("indications:1m:sl1.00:st4"))
    paused_by_exit_lane = _isolated(lambda: run(""))
    ok = set_waits == [120.0] * 5 and exit_waits == [9.0, 36.0, 144.0] and fast_ban and not paused_by_set and paused_by_exit_lane
    return ok, (f"Set lane waits={set_waits}; Exit lane waits={exit_waits}; fast stop-out ban={fast_ban}; "
                f"8th loss pauses the book: Set close={paused_by_set}, Exit-lane close={paused_by_exit_lane}")


def every_stage_runs_on_its_own_lane_from_entry_to_close_and_stats():
    """The stage chain end to end in the trader: pick_any takes the positive lane (Base -> Main -> Real) first and the Real Negative
    lane only when no positive Set is valid; the client id carries the lane (a recovered or fill-synced order keeps it); a live close
    joins the Real Set of its own lane only; the execution stats count Sets per stage and orders per lane; the overall strategies
    read the Real Sets' paper stream."""
    b, se = _signal_book()
    p = _bare_pulse()
    p.sets = b
    base = [x for x in b.by_idx if x.kind == "base" and x.pack == b.packs[0]][:2]
    pos_st, neg_st = base
    for x in b.by_idx:
        x.active, x.gate_n, x.gate_pf, x.max_dd_s, x.n = False, 0, 0.0, 0.0, 0
    for x in base:
        x.active, x.gate_n, x.gate_pf, x.gate_pf_old, x.gate_pf_new, x.n = True, 50, 2.0, 2.0, 2.0, 100
        x.base_short_n, x.base_short_pf = max(b.base_short, 1), 2.0     # the Base short window full (its default)
    pos_st.stage = {"pos": True, "negl": False, "main": True, "short": True, "neg": False}
    neg_st.stage = {"pos": False, "negl": True, "main": False, "short": True, "neg": True}
    lanes = (b.lane_of(pos_st), b.lane_of(neg_st))
    first = b.pick_any(pos_st.pack)
    pos_st.stage = {"pos": False, "negl": False, "main": False, "short": False, "neg": False}
    second = b.pick_any(pos_st.pack)
    cid_neg = p.cid("o", set_id=neg_st.id, pack=neg_st.pack, set_idx=neg_st.idx, lane="neg")
    cid_pos = p.cid("o", set_id=pos_st.id, pack=pos_st.pack, set_idx=pos_st.idx, lane="pos")
    rt = ((p.parse_track(cid_neg) or {}).get("lane"), (p.parse_track(cid_pos) or {}).get("lane"))
    close_pos = p.cid("c", pos=types.SimpleNamespace(set_id=neg_st.id, pack=neg_st.pack, set_idx=neg_st.idx, lane="neg"))
    close_lane = (p.parse_track(close_pos) or {}).get("lane")
    # a live close joins the Real Set of its lane only
    neg_st.proc_replayed, neg_st.proc, neg_st.exe, neg_st.neg, neg_st.live = True, [], [], [], []
    neg_st.live.append({"t": 1.0, "pnl_pct": 0.01, "lane": "neg"})
    m_, r_, q_, _n = b._streams(neg_st)
    joins = (len(m_), len(r_), len(q_))
    # execution stats per lane
    p.open = {"A": types.SimpleNamespace(set_id=neg_st.id, lane="neg", pack=neg_st.pack), "B": types.SimpleNamespace(set_id=pos_st.id, lane="pos", pack=pos_st.pack)}
    p.exec_n = {}
    p._exec_note(pt.Closed(1.0, "A", "LONG", 1, 1, 1, 0.5, 0.01, "tp", 60, set_id=neg_st.id, lane="neg"))
    p._exec_note(pt.Closed(1.0, "B", "LONG", 1, 1, 1, -0.5, -0.01, "sl", 60, set_id=pos_st.id, lane="pos"))
    ex = p.execution_snapshot()
    paper = b.real_paper(10)
    ok = (lanes == ("pos", "neg") and first is pos_st and second is neg_st and rt == ("neg", "pos") and close_lane == "neg"
          and joins == (0, 0, 0) and b.execution_stats(neg_st, "neg")["n"] == 1
          and ex["lanes"]["openPos"] == 1 and ex["lanes"]["openNeg"] == 1 and ex["lanes"]["closedNeg"] == 1
          and ex["lanes"]["winsNeg"] == 1 and ex["lanes"]["closedPos"] == 1 and ex["lanes"]["lossesPos"] == 1
          and set(ex["sets"]["stages"]) == {"base", "main", "negative", "real", "realNeg"} and isinstance(paper, list))
    return ok, (f"lanes {lanes}; pick {getattr(first, 'id', None) == pos_st.id}/{getattr(second, 'id', None) == neg_st.id}; cid lanes {rt} close {close_lane}; "
                f"live close joins (Main, Real, Real Neg) {joins}; stats {ex['lanes']} stages {ex['sets']['stages']}")


def real_stage_overall_strategies_judge_every_scope_on_its_own_stream():
    """The Real stage's overall strategies per scope: real_paper_scopes splits the Real paper stream into "overall", each lane and
    each pack (every row of a scope comes from that scope's Sets only, each scope at most n rows, oldest first); judge_scopes gives
    each its own verdict (with the last axis on, a losing pack/lane blocks only that pack/lane; pooled scopes are figures);
    place() holds an entry whose scope blocks and counts it; the execution stats list every scope with its executed orders."""
    from common import SMALL, overlay
    from set_engine import SetBook
    b = SetBook()
    b.load(overlay(stratTrend=True, **SMALL))
    packs = list(b.packs)
    sts = [x for x in b.by_idx if x.kind == "base"]
    by_pack = {}
    for st in sts:
        by_pack.setdefault(st.pack, st)
    t = 1000.0
    for k, st in enumerate(by_pack.values()):
        st.exe = [(t + 60 * j + k, 0.004) for j in range(12)]              # winning Real Sets
        st.neg = [(t + 60 * j + k + 1, -0.004) for j in range(12)]         # losing Real Negative Sets
    streams = b.real_paper_scopes(40)
    lane_ok = all(r["lane"] == "neg" for r in streams["lane:neg"]) and all(r["lane"] == "pos" for r in streams["lane:pos"])
    pack_rows = {p_: len(streams.get(f"pack:{p_}", [])) for p_ in packs}
    sizes_ok = all(len(v) <= 40 for v in streams.values()) and all(v == sorted(v, key=lambda r: r["t"]) for v in streams.values())
    co = __import__("coord_engine").Coordinator()
    co.load({}, {"axisLastEnabled": True, "axisLastMaxWindow": 6, "axisPrevEnabled": False, "axisPauseEnabled": False, "axisContEnabled": False})
    sc = co.judge_scopes({k: streams[k] for k in ("lane:pos", "lane:neg", f"pack:{packs[0]}", f"pack:{packs[0]}:pos", f"pack:{packs[0]}:neg")})
    verdicts = {k: v["allow"] for k, v in sc.items()}
    allows = (co.scope_allows(packs[0], "pos"), co.scope_allows(packs[0], "neg"))
    # place(): a Set whose lane scope blocks is held, counted, and nothing is ordered
    def run():
        sym = "BTC-USDT"
        contracts = {sym: pt.Contract(symbol=sym, min_qty=0.001, step=0.001, qprec=3, pprec=2, min_usdt=5.0, max_lev=150)}
        out = []
        for lane in ("neg", "pos"):
            if os.path.exists(pt.OPEN_PATH):
                os.remove(pt.OPEN_PATH)
            p = pt.Pulse(_StubExchange(executed="1", requested="1"), contracts)
            p.boot_ts, p.available, p.halted = 0.0, 50.0, False
            p.last_bal = pt.time.time()
            p.px[sym] = 100.0
            p.price_ts[sym] = pt.time.time()
            p.entry_sense = lambda *a, **k: None
            st = p.sets.by_idx[0]
            p.sets.pick_any = lambda pack, _st=st, **kw: _st
            p.sets.pick_trail = lambda pack: None
            p.sets.lane_of = lambda _st, _l=lane: _l
            st.score_at = time.time()
            st.exe = [(st.score_at - 60 * j, .004) for j in range(12, 0, -1)]
            st.neg = [(st.score_at - 60 * j, -.004) for j in range(12, 0, -1)]
            p.sets.pick_any = lambda pack, _st=st, predicate=None, allowed_lanes=None: _st if (
                (allowed_lanes is None or p.sets.lane_of(_st) in allowed_lanes)
                and (predicate is None or predicate(_st))) else None
            p.coord.scopes = {"lane:neg": {"allow": False}, "lane:pos": {"allow": True},
                              f"pack:{st.pack}:neg": {"allow": False}, f"pack:{st.pack}:pos": {"allow": True}}
            p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
            out.append((lane, sym in p.open, int(p.exec_n.get("scopeBlocked", 0))))
            p._exec_note(pt.Closed(2000.0, sym, "LONG", 1, 1, 1, 0.5, 0.01, "tp", 60, set_id=st.id, pack=st.pack, lane=lane))
            ex = p.execution_snapshot()
            out.append(sorted(ex["overall"]["scopes"]))
        return out
    placed = _isolated(run)
    pk_lane_ok = all(r["lane"] == "neg" for r in streams[f"pack:{packs[0]}:neg"]) and all(r["lane"] == "pos" for r in streams[f"pack:{packs[0]}:pos"])
    ok = (lane_ok and pk_lane_ok and sizes_ok and all(v > 0 for v in pack_rows.values()) and verdicts["lane:neg"] is False and verdicts["lane:pos"] is True
          and verdicts[f"pack:{packs[0]}:pos"] is True and verdicts[f"pack:{packs[0]}:neg"] is False
          and allows[0] == (True, "") and allows[1] == (False, f"pack:{packs[0]}:neg")
          and placed[0] == ("neg", False, 1) and placed[2] == ("pos", True, 0) and "lane:neg" in placed[1] and "overall" in placed[3])
    return ok, f"rows per pack {pack_rows}; verdicts {verdicts}; scope_allows pos/neg {allows}; place {placed}"


CHECKS = [
    ("trader.stale-or-failed-balance-blocks-entries", stale_or_failed_balance_blocks_entries),
    ("trader.websocket-last-price-follows-declines", websocket_last_price_follows_declines),
    ("trader.ticker-fallback-nonblocking", ticker_fallback_is_nonblocking_and_preserves_newer_tick),
    ("trader.entry-rechecks-price-after-delay", entry_rechecks_price_after_network_delay),
    ("trader.cycle-continues-during-ticker-read", cycles_keep_managing_when_ticker_read_is_slow),
    ("trader.faster-scan-keeps-poll-frequency", faster_scan_keeps_periodic_work_frequency),
    ("trader.slow-warm-feed-does-not-block-balance", slow_warm_feed_does_not_block_balance),
    ("trader.requested-local-configuration-reaches-engine", requested_local_configuration_reaches_engine),
    ("trader.extra-source-switch-controls-external-reads", extra_source_switch_controls_all_external_reads),
    ("trader.stale-price-cannot-trigger-software-exit", stale_price_cannot_trigger_a_software_exit),
    ("trader.stale-price-does-not-replace-controls", stale_price_does_not_replace_exchange_controls),
    ("trader.slow-cycle-does-not-force-stats", slow_cycles_do_not_force_stats_every_cycle),
    ("trader.no-untagged-order-when-set-disappears", entry_does_not_open_when_the_set_disappears_after_sense),
    ("trader.entry-selection-keeps-packs-and-axis-lanes-independent", entry_selection_keeps_packs_and_axis_lanes_independent),
    ("trader.completed-minutes-do-not-bridge-gaps-or-bad-rows", completed_minutes_do_not_bridge_gaps_or_bad_rows),
    ("trader.live-entry-is-the-replays-signal", live_entries_are_the_replays_signal_on_the_completed_minute),
    ("trader.lanes-every-stage-on-its-own-lane", every_stage_runs_on_its_own_lane_from_entry_to_close_and_stats),
    ("trader.real-overall-strategies-per-scope", real_stage_overall_strategies_judge_every_scope_on_its_own_stream),
    ("trader.lanes-set-close-waits-only-its-own-cooldown", a_set_close_waits_only_its_own_lane_and_never_pauses_other_lanes),
    ("trader.entry-loop-places-the-set-signal", the_entry_loop_places_the_set_signal_and_not_the_live_score),
    ("trader.entry-mirrors-set-signal-without-last-n-validation", the_entry_loop_mirrors_the_set_signal_without_any_last_n_validation),
    ("trader.only-completed-minutes-reach-set-history", completed_minutes_only_reach_the_set_history),
    ("trader.exit-set-never-takes-tp-base-always", an_exit_set_position_never_takes_its_tp_and_a_base_set_always_does),
    ("trader.drawdown-halt-follows-its-setting", drawdown_halt_follows_its_setting),
    ("trader.drawdown-halt-measures-from-the-active-session", drawdown_halt_measures_from_the_active_session),
    ("trader.size-follows-balance-and-volume-factor", position_size_follows_the_balance_and_the_volume_factor),
    ("trader.venue-ban-pauses-flatten-and-refusal-logged", a_venue_ban_pauses_the_flatten_and_a_refused_close_is_logged),
    ("trader.lanes-no-other-lane-touches-a-set-position", no_other_lane_touches_a_set_position),
    ("trader.lanes-set-position-gets-no-add-on-lane", a_set_position_gets_no_add_on_lane),
    ("trader.no-entry-on-a-failed-data-check", no_entry_on_a_symbol_that_failed_the_data_check),
    ("trader.history-thread-fetches-at-start", history_thread_fetches_at_start_and_never_replays_padding),
    ("trader.restored-book-drop-needs-two-reads", restored_book_drops_a_position_only_when_two_reads_agree),
    ("trader.flat-read-is-direction-aware", exchange_flat_requires_a_valid_direction_aware_snapshot),
    ("trader.history-kept-offline-and-topped-up", history_thread_keeps_real_history_and_tops_up_a_partial_one),
    ("trader.departed-symbol-leaves-set-history", a_symbol_that_left_the_universe_leaves_the_set_history),
    ("trader.credentials-from-private-file", credentials_come_from_a_private_file_without_redis),
    ("trader.set-lane-does-not-read-the-exit-strategies", the_set_lane_does_not_read_the_exit_strategies),
    ("trader.set-position-closed-only-by-its-lane", a_set_position_is_closed_only_by_its_own_lane),
    ("trader.set-lane-scratch-and-time-follow-replay", a_set_lane_scratch_and_time_follow_the_replay_rules),
    ("trader.set-lane-honours-tp-as-the-replay-does", a_set_lane_honours_tp_only_as_the_replay_does),
    ("trader.set-trails-only-with-its-trailing-set", a_set_trails_only_with_its_trailing_set_and_the_exit_lanes_stay_out),
    ("trader.exit-lane-scores-only-its-own-closes", exit_lane_scores_only_its_own_closes),
    ("trader.cids-round-trip-to-their-own-set", client_order_ids_round_trip_to_their_own_set),
    ("trader.signal-needs-its-own-pack", a_signal_needs_its_own_pack_to_hold_a_valid_set),
    ("trader.venue-order-of-another-symbol-is-not-the-exit", a_venue_order_of_another_symbol_is_not_this_exit),
    ("trader.manager-close-not-counted-twice", a_manager_close_is_not_counted_again_by_the_fill_sync),
    ("trader.set-fill-keeps-its-sl-and-tp", a_set_fill_keeps_the_sets_own_sl_and_tp),
    ("trader.slpct-tppct-are-percent", overlay_slpct_and_tppct_are_percent_without_guessing),
    ("trader.history-fetched-in-pages", history_is_fetched_in_pages_within_the_venue_limit),
    ("trader.failed-close-keeps-position-protected", failed_close_keeps_the_position_protected),
    ("trader.partial-close-recorded-at-full-size", partial_close_is_recorded_at_the_full_size),
    ("trader.lost-entry-answer-remembered", lost_entry_answer_is_remembered_for_adoption),
    ("trader.symbol-cap-applies-under-a-wildcard", symbol_cap_applies_under_a_wildcard),
    ("trader.symbol-rotation-interval", symbol_rotation_respects_configured_interval),
    ("trader.partial-close-closes-the-remainder", partial_close_fill_closes_the_remainder),
    ("trader.entry-lifecycle-stub-exchange", entry_lifecycle_with_a_stub_exchange),
    ("trader.partial-fill-sized-from-executed", partial_fill_is_sized_from_the_executed_quantity),
    ("trader.indicator-sees-only-real-bars", indicator_sees_only_real_bars),
    ("trader.cycles-start-at-least-scan-apart", cycles_start_at_least_scan_apart),
    ("trader.slow-cycle-is-not-padded", slow_cycle_is_not_padded),
    ("trader.main-loop-does-not-wait-on-ticks", main_loop_does_not_wait_on_the_tick_event),
    ("trader.overlay-reads-leverage-settings", overlay_reads_leverage_settings),
    ("trader.max-toggle-decides-the-target", max_toggle_decides_the_target),
    ("trader.order-leverage-follows-the-target", order_leverage_follows_the_target),
    ("trader.leverage-requires-account-confirmation", leverage_requires_account_confirmation),
    ("trader.x01-leverage-preserves-margin-type", x01_max_leverage_does_not_change_margin_type),
    ("trader.minimum-order-retry-respects-limits", minimum_order_rejection_cannot_bypass_entry_limits),
    ("trader.minimum-order-retry-keeps-client-id", retried_minimum_order_keeps_its_actual_client_id),
    ("trader.short-history-blocks-symbol", short_history_cannot_pass_symbol_quality),
    ("trader.uncertain-entry-blocks-retry", uncertain_entry_blocks_retries_for_the_symbol),
    ("trader.uncertain-entry-survives-restart", uncertain_entry_survives_restart),
    ("trader.intent-write-failure-blocks-order", failed_intent_persistence_blocks_order),
    ("trader.uncertain-adoption-requires-matched-fill", uncertain_adoption_requires_matching_fill),
    ("trader.uncertain-entry-recovery", uncertain_entry_recovery_uses_its_own_fill),
    ("trader.automatic-memory-budget", automatic_memory_budget_responds_to_headroom),
    ("trader.set-control-fallback-stays-outside", set_control_fallback_never_moves_inside_lane),
    ("trader.control-pair-records-accepted-price", control_pair_records_the_accepted_fallback_price),
    ("trader.positions-read-nonblocking", positions_read_is_nonblocking_and_rejects_stale_book),
    ("trader.padding-is-not-readiness", padding_does_not_count_as_readiness),
    ("trader.only-main-loop-pings-watchdog", only_the_main_loop_pings_the_watchdog),
]
