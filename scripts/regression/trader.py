"""Trader suite: the pinned pulse_trader's pacing, leverage, readiness and watchdog, without an exchange.

The module imports with no network. Methods run on a bare instance (no __init__), so no request leaves the process.
These checks read the working file, so they cover the same code the pin covers.
"""
from __future__ import annotations

import ast
import json
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
    return p


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
        p.cancel_controls = lambda sym, **kw: None
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
    ok = lag == ["A-USDT"] and gone == [] and held == ["A-USDT"] and both == ["A-USDT"] and empty == [] and empty_lag == ["A-USDT"]
    return ok, (f"lagging read kept={lag}, two reads agree dropped={gone}, listed kept={held}, one-way short kept={both}, "
                f"two empty reads dropped={empty == []}, empty then listed kept={empty_lag}")


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
            p.available = 50.0
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


class _BookVenue(_StubExchange):
    """The stub venue with a ticker: best bid / ask and their quantities as given (None = no ticker row)."""

    def __init__(self, book):
        super().__init__(executed="1", requested="1")
        self.book = book

    def public(self, path, extra=None):
        if path.endswith("/quote/ticker") and self.book is not None:
            return {"code": 0, "data": {"symbol": (extra or {}).get("symbol"), "lastPrice": "100", **{k: str(v) for k, v in self.book.items()}}}
        return super().public(path, extra)


def thin_book_entry_is_never_sent():
    """Pre-trade book check (found live on x02 2026-11-03: 9 of 11 VST entries filled 0.7-24 % away on thin books and were closed at once as bad
    fills): an entry whose best price on its side sits beyond the bad-fill limit from the decision price, or whose top quantity cannot fill the order,
    sends no order and rests the symbol; a sane book sends it; a book that cannot be read lets it go (the post-fill guard still applies)."""
    sym = "BTC-USDT"
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH)
    out = {}
    with tempfile.TemporaryDirectory() as td:
        pt.BLOCK_PATH = os.path.join(td, "block.json")
        pt.LEV_PATH = os.path.join(td, "lev.json")
        pt.OPEN_PATH = os.path.join(td, "open.json")
        pt.TRADES_PATH = os.path.join(td, "trades.jsonl")
        pt.dump_cts_settings = lambda: {}
        try:
            for label, book in (("far", {"askPrice": 110.0, "askQty": 50, "bidPrice": 109.9, "bidQty": 50}),
                                ("thin", {"askPrice": 100.02, "askQty": 0.0001, "bidPrice": 99.98, "bidQty": 0.0001}),
                                ("sane", {"askPrice": 100.02, "askQty": 50, "bidPrice": 99.98, "bidQty": 50}),
                                ("unread", None)):
                api = _BookVenue(book)
                for name in ("BLOCK_PATH", "LEV_PATH", "OPEN_PATH", "TRADES_PATH"):     # each case its own state files: no position carries over
                    setattr(pt, name, os.path.join(td, f"{label}-{name.lower()}.json"))
                contracts = {sym: pt.Contract(symbol=sym, min_qty=0.001, step=0.001, qprec=3, pprec=2, min_usdt=5.0, max_lev=150)}
                p = pt.Pulse(api, contracts)
                p.boot_ts = 0.0
                p.px[sym] = 100.0
                p.available = 50.0
                p.halted = False
                p.entry_sense = lambda *a, **k: None
                p.sets.use_historic_gate = False
                p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
                sent = len([b for path, b in api.posts if path.endswith("/trade/order")])
                out[label] = (sent, int(p.__dict__.get("exec_n", {}).get("skippedThinBook", 0)), p.cooldown.get(sym, 0) > pt.time.time())
        finally:
            pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH = saved
    ok = (out["far"] == (0, 1, True) and out["thin"] == (0, 1, True) and out["sane"][0] == 1 and out["sane"][1] == 0
          and out["unread"][0] == 1 and out["unread"][1] == 0)
    return ok, f"(orders sent, thin-book skips, symbol resting): {out}"


def leverage_keeps_liquidation_beyond_the_stop():
    """At the venue's maximum leverage (150x) the liquidation can sit inside the Set's own stop: found live on x02 (2026-11-03), a short liquidated
    at +0.23 % with its stop at +0.45 %, positions vanished from the venue before their stop. An entry now sets the symbol's leverage to at most
    stop_leverage_cap(stop): 1 / leverage stays at least LIQ_SAFETY stops (plus the maintenance allowance) away; the reconciliation keeps that cap."""
    sym = "BTC-USDT"
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH)
    with tempfile.TemporaryDirectory() as td:
        for name in ("BLOCK_PATH", "LEV_PATH", "OPEN_PATH", "TRADES_PATH"):
            setattr(pt, name, os.path.join(td, f"{name.lower()}.json"))
        pt.dump_cts_settings = lambda: {}
        try:
            api = _BookVenue({"askPrice": 100.02, "askQty": 50, "bidPrice": 99.98, "bidQty": 50})
            contracts = {sym: pt.Contract(symbol=sym, min_qty=0.001, step=0.001, qprec=3, pprec=2, min_usdt=5.0, max_lev=150)}
            p = pt.Pulse(api, contracts)
            p.boot_ts = 0.0
            p.lev_stop_cap = True            # the stop cap is a setting (leverageStopCap, off by default since 2026-11-03)
            p.px[sym] = 100.0
            p.available = 50.0
            p.halted = False
            p.entry_sense = lambda *a, **k: None
            p.sets.use_historic_gate = False
            p.lev_max[sym], p.lev_map[sym] = 150, 150
            p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
            pos = p.open.get(sym)
            sl = float(pos.sl_pct) if pos else 0.0
            cap = pt.stop_leverage_cap(sl)
            lev_posts = [b for path, b in api.posts if path.endswith("/trade/leverage")]
            applied = int(p.lev_map.get(sym) or 0)
            target = p.target_leverage(150, sym)
        finally:
            pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH = saved
    math_ok = all((1.0 / pt.stop_leverage_cap(x)) >= pt.LIQ_SAFETY * x + pt.LIQ_MAINT - 1e-12 for x in (0.0024, 0.0045, 0.0102, 0.02, 0.08))
    ok = pos is not None and 0 < applied <= cap < 150 and target == applied and bool(lev_posts) and math_ok and pt.stop_leverage_cap(0) == 0
    return ok, f"stop {sl * 100:.2f}% -> cap {cap}x, applied {applied}x (target {target}x, leverage posts {len(lev_posts)}); cap math {math_ok}"


def leverage_is_always_the_exchange_maximum_by_default():
    """Owner 2026-11-03: every entry runs at its contract's exchange maximum leverage. leverageStopCap (default off) is the earlier safety that lowered it to
    1 / (2 x stop + 0.5 %). Off: a position at a 1 % stop opens at the contract's maximum (150x), a symbol left at a lowered leverage by an earlier capped entry
    is raised back, and the leverage posts name the maximum; on: the cap applies again. Every shipped overlay and preset has useMaxLeverage on and no cap."""
    import json as _j
    from common import ROOT
    sym = "BTC-USDT"
    out = {}
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH)
    for label, capped, start_lev in (("default", False, 150), ("lowered before", False, 40), ("cap on", True, 150)):
        with tempfile.TemporaryDirectory() as td:
            for name in ("BLOCK_PATH", "LEV_PATH", "OPEN_PATH", "TRADES_PATH"):
                setattr(pt, name, os.path.join(td, f"{label}-{name.lower()}.json"))
            pt.dump_cts_settings = lambda: {}
            try:
                api = _BookVenue({"askPrice": 100.02, "askQty": 50, "bidPrice": 99.98, "bidQty": 50})
                contracts = {sym: pt.Contract(symbol=sym, min_qty=0.001, step=0.001, qprec=3, pprec=2, min_usdt=5.0, max_lev=150)}
                p = pt.Pulse(api, contracts)
                p.boot_ts, p.halted, p.available = 0.0, False, 50.0
                p.px[sym] = 100.0
                p.entry_sense = lambda *a, **k: None
                p.sets.use_historic_gate = False
                p.lev_stop_cap = capped
                p.lev_max[sym], p.lev_map[sym] = 150, start_lev
                if start_lev < 150:
                    p.__dict__.setdefault("lev_cap", {})[sym] = start_lev
                p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
                out[label] = (bool(p.open.get(sym)), int(p.lev_map.get(sym) or 0), p.target_leverage(150, sym))
            finally:
                pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH = saved
    files = [os.path.join(PULSE, f"overlay-{v}-{c}.json") for v in ("bingx", "bybit") for c in ("x01", "x02")] + \
            [os.path.join(ROOT, "presets", f) for f in ("secure-minimal.json", "secure-minimal-expanded-research.json")]
    ship = {os.path.basename(f): (_j.load(open(f)).get("useMaxLeverage"), _j.load(open(f)).get("leverageStopCap")) for f in files}
    default_off = pt.Pulse.__init__ is not None and not getattr(pt.Pulse(_BookVenue({}), {}), "lev_stop_cap", True)
    ok = (out["default"] == (True, 150, 150) and out["lowered before"] == (True, 150, 150) and out["cap on"][0] and out["cap on"][1] < 150
          and default_off and all(v[0] is not False and not v[1] for k, v in ship.items() if k.startswith("overlay-bingx")))
    return ok, f"(opened, leverage applied, target) {out}; the engine default is no cap: {default_off}; shipped (useMaxLeverage, leverageStopCap) {sorted(set(map(str, ship.values())))}"


class _ExistsVenue(_StubExchange):
    """Every protective order is refused with 110406 "Position SL order already exists"; open orders listed as given."""

    def __init__(self, listed):
        super().__init__(executed="1", requested="1")
        self.listed = listed
        self.refused = 0

    def post(self, path, body):
        if path.endswith("/trade/order") and str(body.get("type") or "") in pt.SL_TYPES | pt.TP_TYPES:
            self.refused += 1
            return {"code": 110406, "msg": "Position SL order already exists"}
        return super().post(path, body)


def control_already_exists_never_loops():
    """A protective order refused as "already exists" must not be retried every few seconds: found live on x02 (2026-11-03), 110406 every ~10 s until
    BingX banned the order path. A foreign control is never adopted; the symbol backs off CTRL_EXISTS_BACKOFF_S
    and a second call inside it sends nothing."""
    out = {}
    for label, listed in (("foreign", [{"orderId": "777", "type": "STOP_MARKET", "positionSide": "LONG", "closePosition": "true", "stopPrice": "95",
                                       "clientOrderID": "foreign"}]),
                          ("prefix-only", [{"orderId": "779", "type": "STOP_MARKET", "positionSide": "LONG", "closePosition": "true", "stopPrice": "95",
                                            "clientOrderID": pt.TAG + "u-foreign"}]), ("none", [])):
        venue = _ExistsVenue(listed)
        saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH)
        with tempfile.TemporaryDirectory() as td:
            for name in ("BLOCK_PATH", "LEV_PATH", "OPEN_PATH", "TRADES_PATH"):
                setattr(pt, name, os.path.join(td, f"{name.lower()}.json"))
            pt.dump_cts_settings = lambda: {}
            try:
                p, sym = _enter(_StubExchange(executed="1", requested="1"))
                pos = p.open.get(sym)
                p.api = venue
                p.list_orders = lambda symbol=None: list(venue.listed)
                p.ctrl_skip = {}
                pos.sl_oid = pos.sec_sl_oid = ""
                got = p.place_ctrl(pos, "sl", 95.0)
                pos.sl_oid = got                       # the caller keeps what place_ctrl returns
                first = venue.refused
                got2 = p.place_ctrl(pos, "sl", 95.0)
                out[label] = (got, got2, first, venue.refused, round(p.ctrl_skip.get(sym, 0) - pt.time.time()))
            finally:
                pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH = saved
    a, b, n = out["foreign"], out["prefix-only"], out["none"]
    ok = all(result[0] == "" and result[1] == "" and result[2] == 1 and result[3] == 1
             and result[4] >= pt.CTRL_EXISTS_BACKOFF_S - 2 for result in (a, b, n))
    return ok, f"(returned id, second call, refusals after the first call, after the second, back-off s): {out}"


def cancellation_requires_a_recorded_order_id():
    p = _bare_pulse()
    pos = pt.Position(symbol='BTC-USDT', side='LONG', qty=1, entry=100, opened_at=1, sl=95, tp=105, peak=100)
    pos.sl_oid = '123'
    p.open = {pos.symbol: pos}
    p._oo_cache = {}
    p.list_orders = lambda _symbol=None: []
    calls = []
    p.api = types.SimpleNamespace(delete=lambda path, body: calls.append((path, body)) or {'code': 0})
    foreign = p.cancel_order('BTC-USDT', '999', pt.TAG + 'same-prefix')
    wrong_symbol = p.cancel_order('ETH-USDT', '123', pt.TAG + 'same-prefix')
    own = p.cancel_order('BTC-USDT', '123', pt.TAG + 'own')
    ok = not foreign and not wrong_symbol and own and [row[1]['orderId'] for row in calls] == ['123']
    return ok, f'foreign={foreign} wrong-symbol={wrong_symbol} own={own} deletes={len(calls)}'


def a_moved_stop_never_storms_the_order_path():
    """Found live on x02 (2026-11-03): the sync pass moved a stop by posting the new one first; BingX refused it (110406, the position's SL exists),
    place_ctrl returned our own old stop and the next pass, ~9 s later, posted again -- over 20 refusals in 480 s, the order path banned for ~7 minutes
    and a trailing Set's close held 90 s (stop 1.26 %, closed at -3.71 %). Now: (1) a refusal that ends in adopting OUR OWN stop backs that control off
    CTRL_EXISTS_BACKOFF_S, so a second call sends nothing; (2) from CTRL_EXISTS_BUDGET refusals within CTRL_EXISTS_WINDOW_S account-wide no control is
    posted at all (closes keep the path); (3) the sync pass moves an existing, covering stop with the venue's atomic cancel-replace."""
    own = [{"orderId": "778", "type": "STOP_MARKET", "positionSide": "LONG", "closePosition": "true", "stopPrice": "96", "clientOrderID": pt.TAG + "uLX"}]
    venue = _ExistsVenue(own)
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH)
    out = {}
    with tempfile.TemporaryDirectory() as td:
        for name in ("BLOCK_PATH", "LEV_PATH", "OPEN_PATH", "TRADES_PATH"):
            setattr(pt, name, os.path.join(td, f"{name.lower()}.json"))
        pt.dump_cts_settings = lambda: {}
        try:
            p, sym = _enter(_StubExchange(executed="1", requested="1"))
            pos = p.open.get(sym)
            p.api = venue
            p.list_orders = lambda symbol=None: list(venue.listed)
            p.ctrl_skip = {}
            p.ctrl_exists_at = []
            pos.sl_oid = pos.sec_sl_oid = "778"
            got = p.place_ctrl(pos, "sec-sl", 95.0)
            first = venue.refused
            got2 = p.place_ctrl(pos, "sec-sl", 94.0)
            out["own"] = (got, got2, first, venue.refused)
            # the account-wide budget: CTRL_EXISTS_BUDGET refusals -> a fresh symbol's stop is not posted either
            p.ctrl_exists_at = [pt.time.time()] * pt.CTRL_EXISTS_BUDGET
            p.ctrl_exists_until = {}
            before = venue.refused
            p.place_ctrl(pos, "tp", 120.0)
            out["budget"] = (p.ctrl_exists_budget_spent(), venue.refused - before)
            p.ctrl_exists_at = [pt.time.time() - pt.CTRL_EXISTS_WINDOW_S - 1] * pt.CTRL_EXISTS_BUDGET
            out["cleared"] = not p.ctrl_exists_budget_spent()
        finally:
            pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH = saved
    src = open(os.path.join(PULSE, "pulse_trader.py")).read()
    sync_ok = ("if replace and is_sl and old and covered and legal and moved and hasattr(self.api, \"cancel_replace\") and getattr(self.api, \"cancel_replace_ok\", True):" in src
               and "self.replace_sl(pos, want)" in src)
    ok = (out["own"] == ("778", "778", 1, 1) and out["budget"] == (True, 0) and out["cleared"] and sync_ok
          and pt.CTRL_EXISTS_BUDGET < 20 and pt.CTRL_EXISTS_WINDOW_S >= 480)
    return ok, f"own stop (id, second call, refusals after first, after second) {out['own']}; budget spent / posts {out['budget']}; window clears {out['cleared']}; sync moves by cancel-replace {sync_ok}"


def an_unsupported_cancel_replace_is_asked_once():
    """Found live on x01 (BingX mainnet, 2026-11-03): POST /trade/order/cancelReplace answered 100400 "this api is not exist" 76 times in 12 minutes (the demo
    accepts it). After the first such answer the client never sends it again (cancel_replace_ok False, answered locally), and replace_sl moves a stop the
    other way: the new stop placed, the old one cancelled."""
    import bingx_fast as bf
    posts = []

    class Fx(bf.FastBingX):
        def __init__(self):
            self.cancel_replace_ok = True

        def post(self, path, extra=None):
            posts.append(path)
            return {"code": 100400, "msg": "this api is not exist,please refer to the API docs https://bingx-api.github.io/docs", "error": True}
    c = Fx()
    first = c.cancel_replace({"symbol": "A-USDT"})
    second = c.cancel_replace({"symbol": "A-USDT"})
    other = Fx()
    other.post = lambda path, extra=None: {"code": 80012, "msg": "some other refusal", "error": True}
    other.cancel_replace({})
    src = open(os.path.join(PULSE, "pulse_trader.py")).read()
    guards = src.count('getattr(self.api, "cancel_replace_ok", True)')
    ok = len(posts) == 1 and first["code"] == 100400 and second.get("unsupported") is True and not c.cancel_replace_ok and other.cancel_replace_ok and guards >= 2
    return ok, f"posts sent {len(posts)} for two calls; second answered locally {second.get('unsupported')}; an unrelated refusal keeps it on {other.cancel_replace_ok}; guarded call sites {guards}"


def leverage_maximum_follows_the_venue_dynamically():
    """The contract list is read again every LEV_MAX_REFRESH_S and a symbol whose maximum leverage changed gets the new one (owner 2026-11-03: always the maximum,
    dynamically): a raised maximum (50 -> 100) is adopted and the applied leverage dropped so the next entry sets it, a lowered one (150 -> 75) too, an unchanged
    symbol and a symbol never seen keep their state, an unreadable contract list changes nothing; the call is made from the REST tick when the interval is up."""
    p = _bare_pulse()
    p.contracts = {s_: pt.Contract(s_, 0.001, 0.001, 3, 4, 2.0, mx) for s_, mx in (("A-USDT", 50), ("B-USDT", 150), ("C-USDT", 20))}
    p.lev_max = {"A-USDT": 50, "B-USDT": 150, "C-USDT": 20}
    p.lev_map = {"A-USDT": 50, "B-USDT": 150, "C-USDT": 20}
    p._persist_lev = lambda: None
    fresh = {"A-USDT": pt.Contract("A-USDT", 0.001, 0.001, 3, 4, 2.0, 100), "B-USDT": pt.Contract("B-USDT", 0.001, 0.001, 3, 4, 2.0, 75),
             "C-USDT": pt.Contract("C-USDT", 0.001, 0.001, 3, 4, 2.0, 20), "NEW-USDT": pt.Contract("NEW-USDT", 0.001, 0.001, 3, 4, 2.0, 50)}
    changed = p.refresh_leverage_limits(fresh)
    state = ({k: p.lev_max[k] for k in ("A-USDT", "B-USDT", "C-USDT")}, {k: p.lev_map[k] for k in ("A-USDT", "B-USDT", "C-USDT")}, "NEW-USDT" in p.lev_max,
             p.contracts["A-USDT"].max_lev, p.target_leverage(p.lev_max["A-USDT"], "A-USDT"), p.target_leverage(p.lev_max["B-USDT"], "B-USDT"))
    pt_load = pt.load_contracts
    try:
        def boom(*a, **k):
            raise OSError("down")
        pt.load_contracts = boom
        keep = p.refresh_leverage_limits()
    finally:
        pt.load_contracts = pt_load
    src = open(os.path.join(PULSE, "pulse_trader.py")).read()
    wired = "self.refresh_leverage_limits()" in src and pt.LEV_MAX_REFRESH_S <= 3600
    ok = (sorted(changed) == ["A-USDT", "B-USDT"] and state == ({"A-USDT": 100, "B-USDT": 75, "C-USDT": 20}, {"A-USDT": 0, "B-USDT": 0, "C-USDT": 20}, False, 100, 100, 75)
          and keep == [] and wired)
    return ok, f"changed {sorted(changed)}; maxima / applied / unseen symbol added / A contract max / targets {state}; a failed list read changes {keep}; wired into the REST tick {wired}"


def unanswered_close_that_never_existed_is_sent_again():
    """A close whose POST got no answer stays pending while the venue might still fill it, but a close the venue keeps reporting as "order not exist"
    was never accepted: found live on x02 (2026-11-03), polled for minutes while the position went unmanaged. After CLOSE_MISS_N such reads over
    CLOSE_MISS_S the pending close is cleared (the next close goes out); a read that is merely unknown keeps waiting."""
    clock = [1000.0]
    saved_time = pt.time.time
    out = {}
    try:
        pt.time.time = lambda: clock[0]
        for label, reply in (("not-exist", {"code": 109421, "msg": "order not exist"}), ("unknown", {"code": -1, "error": True, "msg": "timeout"})):
            p = _bare_pulse()
            p.seen_fill_cids = set()
            p.px = {"A-USDT": 10.0}
            p.save_open_book = lambda: True

            class Api:
                def get(self, path, extra=None, _r=reply):
                    return dict(_r)
            p.api = Api()
            pos = pt.Position(symbol="A-USDT", side="LONG", qty=1.0, entry=10.0, opened_at=1.0, sl=9.0, tp=11.0, peak=10.0)
            pos.close_pending = {"client_id": "Gx02cX"}
            seen = []
            for k in range(4):
                clock[0] = 1000.0 + 25.0 * k
                p.market_close(pos)
                seen.append(bool(pos.close_pending))
            out[label] = seen
    finally:
        pt.time.time = saved_time
    ok = out["not-exist"] == [True, True, True, False] and out["unknown"] == [True, True, True, True]
    return ok, f"pending after each read (25 s apart): {out}"


def indications_trailing_volume_is_not_multiplied():
    """volumeByPackKind: the x 3 of 2026-11-03 for indications trailing Sets is reverted (owner 2026-11-04, live PF 0.24 - 0.74): the engine default is empty,
    no shipped file multiplies indications trailing (the presets carry {}; x01 sizes general Exit Sets 1.5 x, owner 2026-11-05), an overlay without the key gets no multiple; the setting still works when asked for (3 x on top of the pack /
    type / range multiples, a value over the bound is capped at 5)."""
    import types, json as _j
    from common import ROOT
    tr = types.SimpleNamespace(pack="indications", kind="trail", step=0)
    base = types.SimpleNamespace(pack="indications", kind="base", step=15)
    gtr = types.SimpleNamespace(pack="general", kind="trail", step=0)
    pk = pt.volume_mults_of(pt.VOLUME_BY_PACK_KIND_DEFAULT)
    got = {n: pt.set_volume_mult(x, {}, {}, {}, pk) for n, x in (("ind trail", tr), ("ind base", base), ("gen trail", gtr))}
    asked = pt.volume_mults_of({"indications:trail": 3})
    stacked = pt.set_volume_mult(tr, {"indications": 1.5}, {"trail": 1.0}, {"trail": 1.0}, asked)
    capped = pt.volume_mults_of({"indications:trail": 9})["indications:trail"]
    files = [os.path.join(PULSE, f"overlay-{v}-{c}.json") for v in ("bingx", "bybit") for c in ("x01", "x02")] + \
            [os.path.join(ROOT, "presets", f) for f in ("secure-minimal.json", "secure-minimal-expanded-research.json")]
    shipped = {os.path.basename(f): _j.load(open(f)).get("volumeByPackKind") for f in files}
    ok = (got == {"ind trail": 1.0, "ind base": 1.0, "gen trail": 1.0} and abs(stacked - 4.5) < 1e-12 and capped == pt.VOLUME_MULT_MAX
          and all(v == {} or (isinstance(v, dict) and "indications:trail" not in v) for v in shipped.values()) and pt.VOLUME_BY_PACK_KIND_DEFAULT == {}
          and all(v == {} for k, v in shipped.items() if k.endswith(".json") and not k.startswith("overlay-")))     # owner 2026-11-05: x01 sizes general Exit Sets 1.5 x; no shipped file multiplies indications trailing
    return ok, f"default multiples {got}, asked for 3 with pack 1.5 {stacked}, 9 capped to {capped}; shipped {set(map(str, shipped.values()))}"


def set_volume_multiples_size_by_pack_type_and_range():
    """volumeByPack / volumeByKind / volumeByRange (owner 2026-11-03): a Set position's size is the volume factor's size x its pack's x its type's x
    its TP range's multiple (each 0.1 .. 5, missing = 1), never over sizeMaxNotional; with no multiple set the size is unchanged."""
    import types
    sym = "BTC-USDT"
    st = types.SimpleNamespace(id="indications:L:1m:sl1.00:st15", pack="indications", kind="base", step=15, idx=0, sl_ratio=1.0, trail_key="",
                               trail_arm=0.0, trail_give=0.0, hold_bars=0)
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH)
    out = {}
    with tempfile.TemporaryDirectory() as td:
        pt.dump_cts_settings = lambda: {}
        try:
            for label, mults in (("plain", ({}, {}, {})), ("x3", ({"indications": 1.5}, {"base": 2.0}, {"normal": 1.0})),
                                 ("over", ({"indications": 5}, {"base": 5}, {"normal": 5})), ("other range", ({}, {}, {"long": 3.0}))):
                for name in ("BLOCK_PATH", "LEV_PATH", "OPEN_PATH", "TRADES_PATH"):
                    setattr(pt, name, os.path.join(td, f"{label}-{name.lower()}.json"))
                api = _BookVenue({"askPrice": 100.0, "askQty": 1e6, "bidPrice": 99.99, "bidQty": 1e6})
                contracts = {sym: pt.Contract(symbol=sym, min_qty=0.001, step=0.001, qprec=3, pprec=2, min_usdt=0.1, max_lev=150)}
                p = pt.Pulse(api, contracts)
                p.boot_ts, p.halted, p.available = 0.0, False, 1e6
                p.px[sym] = 100.0
                p.entry_sense = lambda *a, **k: None
                p.volume_factor, p.size_pct, p.size_max = 1.0, 0.0, 25.0
                p.volume_by_pack, p.volume_by_kind, p.volume_by_range = (pt.volume_mults_of(m) for m in mults)
                p._scope_choice = lambda pack, side=0, symbol="": (st, "pos", "")
                p.sets.lane_fracs = lambda s_: (0.01, 0.015)
                p.place(sym, 1, "set:indications:x", 0.9)
                orders = [b for path, b in api.posts if path.endswith("/trade/order") and str(b.get("type")) == "MARKET"]
                out[label] = round(float(orders[0]["quantity"]) * 100.0, 4) if orders else None
        finally:
            pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH = saved
    base = out["plain"]
    ok = (base is not None and out["x3"] is not None and abs(out["x3"] - 3 * base) < 0.11 and out["over"] is not None and out["over"] <= 25.0 + 1e-9
          and out["other range"] == base and pt.volume_mults_of({"a": 99, "b": "x", "c": 0.01}) == {"a": 5.0, "c": 0.1})
    return ok, f"entry notional (USDT) by setting: {out}"


def relation_lanes_raise_a_set_entry_and_never_cancel_it():
    """relationLanes: the multiple SetBook.relation_mult answers for the chosen Set and lane raises that entry's quantity (rounded DOWN to the lot step), on top of
    the size it had; the position and its close record carry what was really raised. A raise that does not fit under the hard size ceiling opens the entry at its
    base size (it never cancels an entry); the lane handed to relation_mult is the lane the entry opens on; a failing lookup or a nonsense answer raises nothing;
    with the strict size cap the budget scales by the same ratio and no further."""
    import types
    sym = "BTC-USDT"
    st = types.SimpleNamespace(id="indications:L:1m:sl1.00:st15", pack="indications", kind="base", step=15, idx=0, sl_ratio=1.0, trail_key="",
                               trail_arm=0.0, trail_give=0.0, hold_bars=0)
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH)
    out, lanes_seen = {}, []

    def boom(_st, _lane):
        raise RuntimeError("lanes unavailable")
    cases = (("plain", lambda s_, l_: (1.0, []), {}), ("raised", lambda s_, l_: (1.5, ["overall", "type"]), {}),
             ("ceiling", lambda s_, l_: (1.5, ["overall"]), {"size_max": "base"}), ("error", boom, {}), ("nonsense", lambda s_, l_: (float("nan"), ["x"]), {}),
             ("bound", lambda s_, l_: (50.0, ["overall"]), {"size_max": 1e9}),
             ("strict", lambda s_, l_: (1.5, ["indication"]), {"strict": True}), ("strict plain", lambda s_, l_: (1.0, []), {"strict": True}))
    with tempfile.TemporaryDirectory() as td:
        pt.dump_cts_settings = lambda: {}
        try:
            for label, fn, opt in cases:
                for name in ("BLOCK_PATH", "LEV_PATH", "OPEN_PATH", "TRADES_PATH"):
                    setattr(pt, name, os.path.join(td, f"{label.replace(' ', '-')}-{name.lower()}.json"))
                api = _BookVenue({"askPrice": 100.0, "askQty": 1e6, "bidPrice": 99.99, "bidQty": 1e6})
                contracts = {sym: pt.Contract(symbol=sym, min_qty=0.001, step=0.001, qprec=3, pprec=2, min_usdt=0.1, max_lev=150)}
                p = pt.Pulse(api, contracts)
                p.boot_ts, p.halted, p.available = 0.0, False, 1e6
                p.px[sym] = 100.0
                p.entry_sense = lambda *a, **k: None
                p.volume_factor, p.size_pct, p.size_max = 1.0, 0.0, 25.0
                if opt.get("strict"):
                    p.strict_size_cap, p.equity, p.size_pct, p.size_max = True, 1000.0, 1.0, 25.0
                if "size_max" in opt:
                    p.size_max = out["plain"]["notional"] if opt["size_max"] == "base" else opt["size_max"]
                lane = "neg" if label == "nonsense" else "pos"
                p._scope_choice = lambda pack, side=0, symbol="", _lane=lane: (st, _lane, "")
                p.sets.lane_fracs = lambda s_: (0.01, 0.015)

                def mult(s_, l_, _fn=fn):
                    lanes_seen.append(l_)
                    return _fn(s_, l_)
                p.sets.relation_mult = mult
                p.place(sym, 1, "set:indications:x", 0.9)
                orders = [b for path, b in api.posts if path.endswith("/trade/order") and str(b.get("type")) == "MARKET"]
                pos = p.open.get(sym)
                out[label] = {"notional": round(float(orders[0]["quantity"]) * 100.0, 4) if orders else None,
                              "mult": round(float(pos.rel_mult), 4) if pos else None, "why": pos.rel_why if pos else None,
                              "asked": int(p.exec_n.get("relationAsked", 0)), "raised": int(p.exec_n.get("relationRaised", 0))}
                if label == "raised" and pos is not None:
                    p.close_pos(pos, 101.0, "test")
                    rec = p.closed[-1] if getattr(p, "closed", None) else None
                    out["raised"]["closed"] = (round(float(rec.rel_mult), 4), rec.rel_why) if rec is not None else None
        finally:
            pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH = saved
    base = out["plain"]["notional"]
    sbase = out["strict plain"]["notional"]
    ok = (base is not None and out["plain"]["mult"] == 1.0 and out["plain"]["asked"] == 0
          and out["raised"]["notional"] is not None and abs(out["raised"]["notional"] - 1.5 * base) < 0.11 and out["raised"]["notional"] <= 1.5 * base + 1e-9
          and abs(out["raised"]["mult"] - out["raised"]["notional"] / base) < 1e-3 and out["raised"]["why"] == "overall+type" and out["raised"]["raised"] == 1
          and out["raised"].get("closed") == (out["raised"]["mult"], "overall+type")
          and out["ceiling"]["notional"] == base and out["ceiling"]["mult"] == 1.0 and out["ceiling"]["why"] == "" and out["ceiling"]["asked"] == 1 and out["ceiling"]["raised"] == 0
          and out["error"]["notional"] == base and out["error"]["mult"] == 1.0 and out["nonsense"]["notional"] == base and out["nonsense"]["mult"] == 1.0
          and abs(out["bound"]["notional"] - pt.VOLUME_MULT_MAX * base) < 0.11
          and sbase is not None and abs(out["strict"]["notional"] - 1.5 * sbase) < 0.11 and out["strict"]["notional"] <= 1.5 * sbase + 1e-9
          and lanes_seen == ["pos", "pos", "pos", "pos", "neg", "pos", "pos", "pos"])
    return ok, f"entry notional and recorded multiple by case: {out}; lanes handed over {lanes_seen}"


def block_rounds_raise_the_group_after_a_losing_block_until_it_is_recovered():
    """blockRounds (owner definition 2026-11-11): the positions of a group form blocks of N; when a block's net is negative the group's next entries open at
    1 + ratio x level of the original volume (rounded down to the lot step), and stay raised until the volume-weighted net since that block is back at zero.
    Driven through Pulse.place and close_pos: nothing is raised before a block lost, both positions of the next block are raised, a recovered group is back at
    the base volume, the position and close record carry the multiple, the state survives a restart, a count that is off raises nothing but keeps its record,
    and with blockRounds off nothing is raised or tracked."""
    import types
    import block_rounds
    syms = [f"S{i}-USDT" for i in range(8)]
    st = types.SimpleNamespace(id="indications:L:1m:sl1.00:st15", pack="indications", kind="base", step=15, idx=0, sl_ratio=1.0, trail_key="",
                               trail_arm=0.0, trail_give=0.0, hold_bars=0)
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH)
    raw = {"enabled": True, "relations": ["step"], "maxMult": 3, "counts": {"2": {"on": True, "ratio": 0.5}, "3": {"on": False, "ratio": 1.0}}}
    out = {}

    def make(td, label):
        for name in ("BLOCK_PATH", "LEV_PATH", "OPEN_PATH", "TRADES_PATH"):
            setattr(pt, name, os.path.join(td, f"{label}-{name.lower()}.json"))
        api = _BookVenue({"askPrice": 100.0, "askQty": 1e6, "bidPrice": 99.99, "bidQty": 1e6})
        contracts = {sym: pt.Contract(symbol=sym, min_qty=0.001, step=0.001, qprec=3, pprec=2, min_usdt=0.1, max_lev=150) for sym in syms}
        p = pt.Pulse(api, contracts)
        p.boot_ts, p.halted, p.available = 0.0, False, 1e6
        p.entry_sense = lambda *a, **k: None
        p.volume_factor, p.size_pct, p.size_max = 1.0, 0.0, 25.0
        p._scope_choice = lambda pack, side=0, symbol="": (st, "pos", "")
        p.sets.lane_fracs = lambda s_: (0.01, 0.015)
        for sym in syms:
            p.px[sym] = 100.0
        return p, api

    def enter(p, api, sym):
        n0 = len(api.posts)
        p.place(sym, 1, "set:indications:x", 0.9)
        orders = [b for path, b in api.posts[n0:] if path.endswith("/trade/order") and str(b.get("type")) == "MARKET"]
        pos = p.open.get(sym)
        return (round(float(orders[0]["quantity"]) * 100.0, 4) if orders else None), pos

    with tempfile.TemporaryDirectory() as td:
        pt.dump_cts_settings = lambda: {}
        try:
            p, api = make(td, "on")
            p.rounds.cfg = block_rounds.config_of(raw)
            seq = []
            for sym, exit_px in ((syms[0], 99.0), (syms[1], 99.0)):            # block 1 of the count-2 procedure: two losses
                notional, pos = enter(p, api, sym)
                seq.append((notional, round(float(pos.rounds_mult), 4)))
                p.close_pos(pos, exit_px, "test")
            base = seq[0][0]
            n3, pos3 = enter(p, api, syms[2])                                   # block 2: both positions raised by 0.5
            n4, pos4 = enter(p, api, syms[3])
            out["raised"] = (n3, n4, round(float(pos3.rounds_mult), 4), pos3.rounds_why)
            p2, api2 = make(td, "on")                                           # a restart on the same files: the level and the membership are read back
            p2.rounds.cfg = block_rounds.config_of(raw)
            out["restart"] = (round(p2.rounds.ask(block_rounds.keys_of(syms[4], 1, "indications", "base", 15, "pos", st.id))[0], 4), len(p2.rounds.members))
            for w in (pos3, pos4):                                              # the stub venue fills every close at 100: a win is an entry below it
                w.entry = w.base_entry = 95.0
            p.close_pos(pos3, 103.0, "test")
            rec = p.closed[-1]
            out["closed"] = (round(float(rec.rounds_mult), 4), rec.rounds_why)
            p.close_pos(pos4, 103.0, "test")                                    # block 2 won more than block 1 lost: recovered
            n5, pos5 = enter(p, api, syms[4])
            out["recovered"] = (n5, round(float(pos5.rounds_mult), 4))
            snap = p.rounds.snapshot()
            out["counts"] = {r["count"]: (r["groups"], r["blocks"], r["raises"], r["recovered"]) for r in snap["byCount"]}
            p.close_pos(pos5, 99.0, "test")
            g3 = p.rounds.groups.get(p.rounds.gid("step", "indications|15|pos|L", 3)) or {}
            out["count 3 (off, its own blocks) level"] = g3.get("level")
            u, uapi = make(td, "unscored")                                     # a close made while the engine was down is recorded and teaches nothing
            u.rounds.cfg = block_rounds.config_of({"enabled": True, "relations": ["step"], "maxMult": 3, "counts": {"1": {"on": True, "ratio": 1.0}}})
            ukeys = block_rounds.keys_of(syms[5], 1, "indications", "base", 15, "pos", st.id)
            _n, upos = enter(u, uapi, syms[0])
            upos.downtime_close = True
            u.close_pos(upos, 99.0, "test")
            after_unscored = (u.rounds.ask(ukeys)[0], u.rounds.groups[u.rounds.gid("step", ukeys["step"], 1)]["blocks"], len(u.rounds.members))
            _n, upos = enter(u, uapi, syms[1])
            u.close_pos(upos, 99.0, "test")
            out["unscored then scored loss"] = (after_unscored, u.rounds.ask(ukeys)[0])
            _n, gpos = enter(u, uapi, syms[2])                                  # a position that leaves the book with no close record ...
            held = len(u.rounds.members)
            u.ban_sym(syms[2])
            _n, gpos2 = enter(u, uapi, syms[3])
            u.close_pos(gpos2, 99.0, "test")                                    # ... must not stall its group: the next block settles
            g1 = u.rounds.groups[u.rounds.gid("step", ukeys["step"], 1)]
            out["released"] = (held, len(u.rounds.members), len(g1["open"]), g1["blocks"])
            _n, gpos3 = enter(u, uapi, syms[4])
            os.remove(pt.OPEN_PATH)                                             # a restart whose book no longer holds the position
            u2, _a2 = make(td, "unscored")
            out["restart without the position"] = (len(u.rounds.members), len(u2.rounds.members), len(u2.rounds.groups[u2.rounds.gid("step", ukeys["step"], 1)]["open"]))
            q, _api = make(td, "off")
            notional, pos = enter(q, _api, syms[0])
            q.close_pos(pos, 99.0, "test")
            out["off"] = (notional, len(q.rounds.groups), q.rounds.ask(block_rounds.keys_of(syms[1], 1, "indications", "base", 15, "pos", st.id)))
        finally:
            pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH = saved
    ok = (base is not None and seq == [(base, 1.0), (base, 1.0)]
          and out["raised"][0] is not None and abs(out["raised"][0] - 1.5 * base) < 0.11 and out["raised"][0] <= 1.5 * base + 1e-9 and out["raised"][1] == out["raised"][0]
          and abs(out["raised"][2] - out["raised"][0] / base) < 1e-3 and out["raised"][3] == "step:N2:L1"
          and out["restart"] == (1.5, 2) and out["closed"] == (out["raised"][2], "step:N2:L1")
          and out["recovered"] == (base, 1.0)
          and out["counts"][2] == (1, 2, 1, 1) and out["counts"][1][1] == 4 and out["counts"][3][1] == 1 and out["counts"][4][1] == 1
          and out["count 3 (off, its own blocks) level"] == 0
          and out["unscored then scored loss"] == ((1.0, 0, 0), 2.0)
          and out["released"] == (1, 0, 0, 2) and out["restart without the position"] == (1, 0, 0)
          and out["off"] == (base, 0, (1.0, [])))
    return ok, f"entries and multiples: first block {seq}; {out}"


def lift_figures_compare_the_same_parents_with_and_without():
    """execution.lift ("PF related to without"): the settled Set parents once without and once with each stack type, on the parent's original base notional.
    Block: a parent without an add is its base result on both sides; a parent with adds is its base result against ALL its cash over the base notional, so
    losing adds lower the "with" side and are never dropped. Relation lanes: every parent at volume 1 against its recorded multiple, and the unit result of
    the raised parents next to the others. A parent with an unknown base result, or with adds and unknown cash, enters no sum and is counted as unknown.
    The figures survive a restart: they are rebuilt from the trades file."""
    C = pt.Closed
    def rec(base, pnl=None, fills=(), mult=1.0, base_qty=2.0, base_entry=50.0):
        return C(1.7e9, "A-USDT", "LONG", base_qty, base_entry, 0.0, pnl if pnl is not None else float("nan"), 0.0, "tp", 60.0, set_id="general:L:1m:sl1.00:st20", lane="pos",
                 base_entry=base_entry, base_qty=base_qty, block_fills=list(fills), base_net_pnl_pct=base, rel_mult=mult)
    add = {"group": 2, "qty": 1.0, "price": 51.0}
    rows = [rec(0.010), rec(-0.004), rec(0.020, pnl=3.0, fills=[add]),          # base +2 % of 100 = 2.0; the add made +1.0 more: with = 3 %
            rec(0.010, pnl=-0.5, fills=[add, add]),                             # base +1 %, the adds lost 1.5: with = -0.5 %
            rec(-0.010, mult=1.5), rec(0.006, mult=2.0),                        # raised parents: a loss at 1.5 x, a win at 2 x
            rec(None), rec(0.010, pnl=None, fills=[add])]                       # unknown base result; adds with unknown cash
    agg = pt.new_lift()
    for r in rows:
        pt.lift_note(agg, r)
    rep = pt.lift_report(agg)
    b, r_ = rep["block"], rep["relation"]
    def near(x, y):
        return x is not None and abs(x - y) < 1e-6
    block_ok = (rep["parents"] == 6 and rep["unknown"] == 2 and b["parentsWithAdds"] == 2 and b["adds"] == 3
                and near(b["without"]["pf"], round(0.046 / 0.014, 4)) and near(b["without"]["netPct"], 3.2)
                and near(b["with"]["pf"], round(0.046 / 0.019, 4)) and near(b["with"]["netPct"], 2.7)
                and b["addParentsWithout"]["pf"] == 99.0 and near(b["addParentsWithout"]["netPct"], 3.0)
                and near(b["addParentsWith"]["pf"], 6.0) and near(b["addParentsWith"]["netPct"], 2.5))
    rel_ok = (r_["raised"] == 2 and near(r_["volume"], 7.5) and near(r_["with"]["pf"], round(0.052 / 0.019, 4)) and near(r_["with"]["netPct"], 3.3)
              and near(r_["raisedUnit"]["pf"], 0.6) and near(r_["notRaisedUnit"]["pf"], 10.0)
              and near(r_["netPerVolumeWithoutPct"], round(3.2 / 6, 5)) and near(r_["netPerVolumeWithPct"], round(3.3 / 7.5, 5)))
    empty = pt.lift_report(pt.new_lift())
    empty_ok = empty["parents"] == 0 and empty["block"]["with"]["pf"] is None and empty["relation"]["netPerVolumeWithPct"] is None
    # restart: the same figures from the trades file
    saved = (pt.TRADES_PATH,)
    with tempfile.TemporaryDirectory() as td:
        pt.TRADES_PATH = os.path.join(td, "trades.jsonl")
        try:
            with open(pt.TRADES_PATH, "w") as f:
                for r in rows:
                    d = dict(r.__dict__)
                    d["pnl"] = None if d["pnl"] != d["pnl"] else d["pnl"]
                    d["client_id"] = ""
                    f.write(json.dumps(d) + "\n")
            p = _bare_pulse()
            p.cid_ours = lambda cid: True
            p._exec_boot()
            again = pt.lift_report(p.exec_lift)
        finally:
            pt.TRADES_PATH = saved[0]
    return block_ok and rel_ok and empty_ok and again == rep, f"block {b}; relation {r_}; unknown {rep['unknown']}; rebuilt from the trades file equal: {again == rep}"


def start_records_whether_the_previous_run_crashed():
    """The desk shows how the previous run ended: a start that finds the run-state file still "running" reports no clean stop (crash, kill or
    machine stop; found after the 2026-11-03 container pause, which left 9 positions unmanaged), a clean stop marks "stopped". system_info carries
    it with CPU, memory, request rates, the restore events and the recent errors."""
    import signal as _signal
    saved = (pt.DIR, pt.STATS_PATH, _signal.getsignal(_signal.SIGTERM))
    out = []
    with tempfile.TemporaryDirectory() as td:
        pt.DIR, pt.STATS_PATH = td, os.path.join(td, "stats.json")
        try:
            for _ in range(3):
                p = _bare_pulse()
                p.started, p.errors, p.last_error, p.open = pt.time.time(), 0, "", {}
                p.api = type("A", (), {"snapshot": lambda self: {"rest": 5, "ws": 9}})()
                p.mark_run_start()
                out.append(dict(p.last_run))
                if len(out) == 2:
                    p._write_run_state({"state": "stopped", "startedAt": p.started, "stoppedAt": pt.time.time(), "reason": "SIGTERM"})
            info = p.system_info()
        finally:
            pt.DIR, pt.STATS_PATH = saved[0], saved[1]
            _signal.signal(_signal.SIGTERM, saved[2])
    ok = (out[0]["reason"] == "first start" and out[1]["clean"] is False and "crash" in out[1]["reason"] and out[2]["clean"] is True
          and info["restTotal"] == 5 and info["wsTotal"] == 9 and info["machineMb"] and "recentErrors" in info and "lastRun" in info)
    return ok, f"starts: {[(o['clean'], o['reason']) for o in out]}; system keys {sorted(info)[:8]}"


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
    p.available = 50.0
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
    x01_cap = int(json.load(open(os.path.join(PULSE, "overlay-bingx-x01.json"), encoding="utf-8"))["symbolCap"])
    n_names = max(x01_cap, 120) + 40
    p.universe = [{"symbol": f"S{i:03d}-USDT", "vol1h": float(1000 - i), "maxLeverage": 50, "quoteVolume": 1.0,
                   "vol24h": 0.0, "changePct": 0.0} for i in range(n_names)]
    p.last_dyn_sel = 0.0
    p.contracts = {}
    p.sets = types.SimpleNamespace(set_symbols=lambda *a, **k: None, enabled=False)
    out = {}
    try:
        p.symbol_cap = 50
        p.apply_dynamic_symbols(force=True)
        out["capped"] = len(pt.SYMBOLS)
        p.symbol_cap = 0
        p.apply_dynamic_symbols(force=True)
        out["uncapped"] = len(pt.SYMBOLS)
        p.symbol_cap = 50
        last = f"S{n_names - 1:03d}-USDT"
        p.open = {last: types.SimpleNamespace(symbol=last)}
        p.apply_dynamic_symbols(force=True)
        out["with_open"] = (len(pt.SYMBOLS), last in pt.SYMBOLS)
        p.symbol_cap = 120
        p.apply_dynamic_symbols(force=True)
        out["requested_120"] = (len(pt.SYMBOLS), last in pt.SYMBOLS)
        p.symbol_cap = x01_cap
        p.apply_dynamic_symbols(force=True)
        out["x01"] = (len(pt.SYMBOLS), last in pt.SYMBOLS)
        p.open = {f"S{i:03d}-USDT": types.SimpleNamespace(symbol=f"S{i:03d}-USDT") for i in range(x01_cap + 1)}
        p.apply_dynamic_symbols(force=True)
        out["protect_more_than_cap"] = (len(pt.SYMBOLS), all(s in pt.SYMBOLS for s in p.open))
    except Exception as exc:  # noqa: BLE001
        return False, f"raised {type(exc).__name__}: {exc}"
    finally:
        pt.SYMBOLS[:] = saved_syms
    ok = (out.get("capped") == 50 and out.get("uncapped") == n_names and out.get("with_open") == (50, True)
          and out.get("requested_120") == (120, True)
          and out.get("x01") == (x01_cap, True) and out.get("protect_more_than_cap") == (x01_cap + 1, True))
    return ok, (f"wildcard cap 50 -> {out.get('capped')}; cap 0 -> {out.get('uncapped')}; "
                f"cap 50 + one open -> {out.get('with_open')}; requested cap 120 -> {out.get('requested_120')}; "
                f"x01 -> {out.get('x01')}; {x01_cap + 1} open positions -> {out.get('protect_more_than_cap')}")


def x01_bootstrap_honors_cap_before_ranked_tickers():
    """A real Pulse initialization with an offline fake cannot begin with an unbounded wildcard."""
    saved_syms, saved_loader, saved_overlay = list(pt.SYMBOLS), pt.load_contracts, pt.OVERLAY_PATH
    saved_open_loader = pt.Pulse._load_open_book
    cap = int(json.load(open(os.path.join(PULSE, "overlay-bingx-x01.json"), encoding="utf-8"))["symbolCap"])
    names = [f"S{i:03d}-USDT" for i in range(cap + 30)]
    contracts = {s: pt.Contract(symbol=s, min_qty=0.001, step=0.001, qprec=3, pprec=6,
                                min_usdt=5.0, max_lev=50) for s in names}

    def run():
        pt.load_contracts = lambda wanted=None: contracts if wanted is None else {s: contracts[s] for s in wanted if s in contracts}
        pt.OVERLAY_PATH = os.path.join(PULSE, "overlay-bingx-x01.json")
        p = pt.Pulse(_StubExchange(), dict(contracts))
        try:
            initial = list(pt.SYMBOLS)
            no_rank = not p.universe
            p.universe = [{"symbol": s, "vol1h": float(i), "maxLeverage": 50, "quoteVolume": 1.0,
                           "vol24h": 0.0, "changePct": 0.0} for i, s in enumerate(names)]
            p.apply_dynamic_symbols(force=True)
            ranked = list(pt.SYMBOLS)
        finally:
            p.pool.shutdown(wait=True)
        pt.Pulse._load_open_book = lambda self: self.open.update(
            {s: types.SimpleNamespace(symbol=s) for s in names[:cap + 1]})
        q = pt.Pulse(_StubExchange(), dict(contracts))
        try:
            restored = list(pt.SYMBOLS)
        finally:
            q.pool.shutdown(wait=True)
        return initial, no_rank, ranked, restored

    try:
        initial, no_rank, ranked, restored = _isolated(run)
        held = pt.bootstrap_symbols(names, [names[-1]], cap)
        many_held = pt.bootstrap_symbols(names, names[:cap + 1], cap)
        unlimited = pt.bootstrap_symbols(names, ["OUTSIDE-USDT"], 0)
    finally:
        pt.SYMBOLS[:] = saved_syms
        pt.load_contracts, pt.OVERLAY_PATH = saved_loader, saved_overlay
        pt.Pulse._load_open_book = saved_open_loader
    ok = (no_rank and len(initial) == cap and initial == names[:cap]
          and len(ranked) == cap and ranked[0] == names[-1]
          and len(held) == cap and names[-1] in held
          and len(many_held) == cap + 1 and len(restored) == cap + 1 and set(restored) == set(names[:cap + 1])
          and len(unlimited) == len(names) + 1 and "OUTSIDE-USDT" in unlimited)
    return ok, (f"before ticker={len(initial)}; ranked={len(ranked)} first={ranked[0] if ranked else None}; "
                f"held={len(held)}; 31 restored={len(restored)}; unlimited={len(unlimited)}")


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


def failed_close_keeps_the_position_protected():
    """A refused close retains the position and its existing protection until a full close is confirmed."""
    def run():
        p, sym = _enter(_RefusingCloseVenue(executed="1", requested="1"))
        pos = p.open[sym]
        had = not p.missing_controls(pos)
        p.close_pos(pos, 100.0, "no-ctrl-test")
        return had, sym in p.open, p.missing_controls(pos), sym in p.ignore_syms
    had, kept, missing, banned = _isolated(run)
    return had and kept and not missing and banned, f"stops before={had} kept in book={kept} protection retained={not missing} banned={banned}"


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
        p.available = 50.0
        p.halted = False
        p.entry_sense = lambda *a, **k: None
        p.sets.use_historic_gate = False  # exercise venue recovery independently of Set validation
        p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
        return sym in p.open, dict(getattr(p, "unknown_entry", {}))
    opened, unk = _isolated(run)
    return (not opened) and any(k.startswith("BTC-USDT:") for k in unk), f"in book={opened} unknown entries={list(unk)}"


def _frozen_clock(fn):
    """Run a check with ONE clock for the fake venue and the code under test. _PagedKlines stamps its candles from the wall clock when it is built and the
    history code compares them with its own clock: a minute boundary in between (likely under load) made a fresh history look a minute stale and re-paged it,
    which failed these checks about one run in a dozen (seen on the already-pushed 58af012 and again in a full run)."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **k):
        clock, now = pt.time.time, pt.time.time()
        pt.time.time = lambda: now
        try:
            return fn(*a, **k)
        finally:
            pt.time.time = clock
    return wrapper


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


@_frozen_clock
def history_retries_only_uncertain_reads_without_resending_successes():
    import set_engine as se
    class SlowPage(_PagedKlines):
        def __init__(self):
            super().__init__(n=10000)
            self.retries = []
        def gather_public(self, reqs, timeout=4.2):
            return [(path, extra, {'error':True,'msg':'read timed out'} if extra['symbol']=='A-USDT'
                     else row[2]) for (path, extra), row in zip(reqs, super().gather_public(reqs, timeout))]
        def public(self, path, extra):
            self.retries.append(dict(extra))
            return super().gather_public([(path, extra)])[0][2]
    saved, sleep = list(pt.SYMBOLS), pt.time.sleep
    try:
        pt.SYMBOLS[:] = ['A-USDT', 'B-USDT']
        pt.time.sleep = lambda _s: None
        p = _bare_pulse()
        p.api, p.sets = SlowPage(), se.SetBook()
        p.sets.lookback = 1080
        refreshed = p._hist_fetch()
        return (refreshed == set(pt.SYMBOLS) and p.api.retries
                and all(r['symbol']=='A-USDT' for r in p.api.retries)
                and all(len(p.sets.bars[s])==p.sets.history_bars() for s in pt.SYMBOLS)), f'complete={sorted(refreshed)}; read-only retries={len(p.api.retries)}; successful B pages not resent'
    finally:
        pt.SYMBOLS[:], pt.time.sleep = saved, sleep


@_frozen_clock
def history_page_overlap_cannot_shorten_stage_context():
    import set_engine as se
    class Overlap(_PagedKlines):
        def gather_public(self, reqs, timeout=4.2):
            # VST rounds an endTime one millisecond before the old candle up
            # to that same candle, so consecutive pages overlap by one row.
            adjusted = [(path, {**extra, **({'endTime': str(int(extra['endTime'])+1)}
                         if 'endTime' in extra else {})}) for path, extra in reqs]
            result = super().gather_public(adjusted, timeout)
            return [(path, original, row[2]) for (path, original), row in zip(reqs, result)]
    saved, sleep = list(pt.SYMBOLS), pt.time.sleep
    try:
        pt.SYMBOLS[:] = ['A-USDT']
        pt.time.sleep = lambda _s: None
        p = _bare_pulse()
        p.api, p.sets = Overlap(n=10000), se.SetBook()
        p.sets.lookback = 1080
        target = p.sets.history_bars()
        refreshed = p._hist_fetch()
        closes = [b[3] for b in p.sets.bars['A-USDT']]
        wanted = [float(i) for i in range(9999-target, 9999)]
        return closes == wanted and refreshed == {'A-USDT'}, f'{len(closes)}/{target} completed distinct minutes; contiguous={closes == wanted}'
    finally:
        pt.SYMBOLS[:], pt.time.sleep = saved, sleep


@_frozen_clock
def history_is_fetched_in_pages_within_the_venue_limit():
    """The Set history asks for 1920 bars, which the venue refuses in one request. The fetch pages backwards with
    endTime, so the series arrives whole, in order and without a gap; once a symbol holds its history, the next refresh
    asks only for the newest minutes."""
    import set_engine as se
    saved = list(pt.SYMBOLS)
    try:
        pt.SYMBOLS[:] = ["A-USDT", "B-USDT"]
        p = _bare_pulse()
        venue = _PagedKlines(n=8000)
        p.api = venue
        p.sets = se.SetBook()
        p.sets.lookback = 1920
        target = p.sets.history_bars()
        p._hist_fetch()
        first_requests = list(venue.requests)
        have = {s: len(p.sets.bars.get(s) or []) for s in pt.SYMBOLS}
        closes = [b[3] for b in (p.sets.bars.get("A-USDT") or [])]
        contiguous = closes == [float(x) for x in range(8000 - 1 - target, 8000 - 1)]     # the forming last minute is not a bar
        venue.requests.clear()
        p._hist_fetch()
        refresh_limits = sorted({int(r["limit"]) for r in venue.requests})
    finally:
        pt.SYMBOLS[:] = saved
    ok = (all(n == target for n in have.values()) and contiguous and all(int(r["limit"]) <= 1000 for r in first_requests)
          and any("endTime" in r for r in first_requests) and refresh_limits == [120])
    return ok, f"bars={have} contiguous={contiguous} first requests={len(first_requests)} refresh limits={refresh_limits}"


@_frozen_clock
def grown_history_fetches_the_full_target_up_to_three_days():
    """A growth step must fetch older bars even if the old history is 90% of the new target."""
    import set_engine as se
    saved = list(pt.SYMBOLS)
    sleep = pt.time.sleep
    try:
        pt.SYMBOLS[:] = ["A-USDT"]
        pt.time.sleep = lambda _s: None
        p = _bare_pulse()
        # Stage reconstruction now needs older entry-time context in addition
        # to the visible lookback.  Supply enough venue history to verify the
        # entire context target at every growth step.
        venue = _PagedKlines(n=20001)
        p.api, p.sets = venue, se.SetBook()
        got = []
        for target in (1920, 2160, 2400, 4320):
            p.sets.lookback = target
            context_target = p.sets.history_bars()
            venue.requests.clear()
            p._hist_fetch()
            closes = [row[3] for row in p.sets.bars.get("A-USDT", [])]
            expected = [float(x) for x in range(20000 - context_target, 20000)]
            got.append((context_target, len(closes), closes == expected, max(int(r["limit"]) for r in venue.requests)))
        venue.requests.clear()
        p._hist_fetch()
        refresh = [int(r["limit"]) for r in venue.requests]
    finally:
        pt.SYMBOLS[:] = saved
        pt.time.sleep = sleep
    ok = all(target == n and ordered and request_max <= 1000 for target, n, ordered, request_max in got) and refresh == [120]
    return ok, f"growth {got}; full-history refresh limits={refresh}"


@_frozen_clock
def history_growth_retries_failed_or_missing_pages():
    """A failed historical page cannot certify an old window as an expanded one."""
    import set_engine as se

    class Venue(_PagedKlines):
        def __init__(self, failure):
            super().__init__(n=20001)
            self.failure = failure

        def gather_public(self, reqs, timeout=4.2):
            rows = super().gather_public(reqs, timeout)
            out = []
            for path, extra, body in rows:
                if self.failure and extra["symbol"] == "B-USDT" and (self.failure == "first" or "endTime" in extra):
                    if self.failure == "missing":
                        continue
                    body = ({"code": 109400, "data": []} if self.failure == "coded"
                            else {"error": True, "msg": "page failed"})
                out.append((path, extra, body))
            return out

    def run(failure):
        p = _bare_pulse()
        p.api, p.sets = Venue(failure), se.SetBook()
        p.sets.load({"histLookbackBars": 2400, "histLookbackMax": 2400,
                     "histLookbackAuto": True, "histMinBars": 60, "holdEnabled": False,
                     "setMinStep": 2, "setStepMax": 2, "setSlRatios": [0.5],
                     "trailVariants": ["0.3:0.1"], "stratGeneral": True, "stratIndications": False})
        old, last_t = p._closed_klines(p.api.rows[-1921:])
        for sym in pt.SYMBOLS:
            p.sets.ingest_bars(sym, old, last_t=last_t)
            p.real_1m[sym] = 0
        p.sets._lookback_pending = True
        replayed = []
        p.sets._replay_symbol = lambda sym, *_args, **_kwargs: replayed.append(sym)
        p.open = {}
        p._budget = lambda: types.SimpleNamespace(scan_chunk=0, hist_chunk=6, hist_run=True)
        p.load = types.SimpleNamespace(last_budget=types.SimpleNamespace(level="normal"), cursor_hist=0)
        p.hist_busy = False
        p._hist_tick()
        first = (list(replayed), p.sets._lookback_pending,
                 dict(p.sets._hist_replay_key), len(p.sets.bars["B-USDT"]))
        p.api.failure = None
        p.sets.last_run = 0.0
        replayed.clear()
        p._hist_tick()
        second = (list(replayed), p.sets._lookback_pending,
                  dict(p.sets._hist_replay_key), len(p.sets.bars["B-USDT"]))
        return first, second, p.sets.history_bars()

    saved, sleep = list(pt.SYMBOLS), pt.time.sleep
    try:
        pt.SYMBOLS[:] = ["A-USDT", "B-USDT"]
        pt.time.sleep = lambda _s: None
        cases = [run(mode) for mode in ("first", "error", "coded", "missing")]
        # A legitimately new listing can have fewer bars than the target without
        # being a failed page; it should not block the rest of the universe.
        short = _bare_pulse()
        short.api, short.sets = _PagedKlines(n=501), se.SetBook()
        short.sets.lookback = 2400
        available = short._hist_fetch(["A-USDT"])
        short_n = len(short.sets.bars["A-USDT"])
        # A previously full history whose recent refresh no longer overlaps
        # must fetch the missing older bars on the next pass.
        gap = _bare_pulse()
        gap.api, gap.sets = _PagedKlines(n=20001), se.SetBook()
        gap.sets.lookback = 2400
        gap_target = gap.sets.history_bars()
        old, old_t = gap._closed_klines(gap.api.rows[-gap_target - 600:-600])
        gap.sets.ingest_bars("A-USDT", old, last_t=old_t)
        gap_first = gap._hist_fetch(["A-USDT"]), len(gap.sets.bars["A-USDT"])
        gap_second = gap._hist_fetch(["A-USDT"]), len(gap.sets.bars["A-USDT"])
    finally:
        pt.SYMBOLS[:], pt.time.sleep = saved, sleep
    ok = (all(first[0] == ["A-USDT"] and first[1] and "B-USDT" not in first[2] and first[3] < target
              and set(second[0]) == {"A-USDT", "B-USDT"} and not second[1]
              and set(second[2]) == {"A-USDT", "B-USDT"} and second[3] == target
              for first, second, target in cases)
          and available == {"A-USDT"} and short_n == 499
          and gap_first == (set(), 119) and gap_second == ({"A-USDT"}, gap_target))
    return ok, f"failure/retry={cases}; valid short listing={available} {short_n} bars; stale gap={gap_first} -> {gap_second}"


@_frozen_clock
def short_history_backfill_is_temporarily_bounded():
    """An exhausted listing is refreshed cheaply, but gaps and expiry retry backfill."""
    import set_engine as se
    saved, sleep = list(pt.SYMBOLS), pt.time.sleep
    try:
        pt.SYMBOLS[:] = ["A-USDT"]
        pt.time.sleep = lambda _s: None
        p = _bare_pulse()
        p.api, p.sets = _PagedKlines(n=2501), se.SetBook()
        p.api.rows[0].update(open="0.1", high="0.2", low="0.05", close="0.1")
        p.sets.lookback = 2400
        first = p._hist_fetch(["A-USDT"])
        first_pages = len(p.api.requests)
        p.api.requests.clear()
        second = p._hist_fetch(["A-USDT"])
        second_limits = [int(row["limit"]) for row in p.api.requests]
        cached_n = len(p.sets.bars["A-USDT"])
        p._hist_short_cache["A-USDT"]["expires"] = 0
        p.api.requests.clear()
        expired = p._hist_fetch(["A-USDT"])
        expired_pages = len(p.api.requests)
        # A refresh that no longer overlaps must invalidate the cache; it
        # must not certify the truncated series until backfill succeeds.
        for row in p.api.rows:
            for key in ("open", "high", "low", "close"):
                row[key] = str(float(row[key]) + 10000)
        p.api.requests.clear()
        gap = p._hist_fetch(["A-USDT"])
        gap_limits = [int(row["limit"]) for row in p.api.requests]
        p.api.requests.clear()
        recovered = p._hist_fetch(["A-USDT"])
        recovered_pages = len(p.api.requests)
        p.sets.lookback = 2500
        p.api.requests.clear()
        grown = p._hist_fetch(["A-USDT"])
        grown_pages = len(p.api.requests)
        return (first == second == expired == recovered == grown == {"A-USDT"}
                and first_pages > 1 and second_limits == [120] and cached_n == 2500
                and expired_pages > 1 and gap == set() and gap_limits == [120]
                and recovered_pages > 1 and grown_pages > 1), f"pages first/refresh/expired/gap/recover/growth={first_pages}/{second_limits}/{expired_pages}/{gap_limits}/{recovered_pages}/{grown_pages}; gap={gap}"
    finally:
        pt.SYMBOLS[:], pt.time.sleep = saved, sleep


@_frozen_clock
def short_history_cache_rejects_failed_or_invalid_pages():
    import set_engine as se

    class Failing(_PagedKlines):
        def gather_public(self, reqs, timeout=4.2):
            rows = super().gather_public(reqs, timeout)
            return [(path, extra, {"error": True, "msg": "retry"} if "endTime" in extra else body)
                    for path, extra, body in rows]

    class Duplicating(_PagedKlines):
        def gather_public(self, reqs, timeout=4.2):
            rows = super().gather_public(reqs, timeout)
            for _path, _extra, body in rows:
                if body.get("data"):
                    body["data"].append(dict(body["data"][-1]))
            return rows

    saved, sleep = list(pt.SYMBOLS), pt.time.sleep
    try:
        pt.SYMBOLS[:] = ["A-USDT"]
        pt.time.sleep = lambda _s: None
        invalid = _bare_pulse()
        invalid.api, invalid.sets = _PagedKlines(n=2501), se.SetBook()
        invalid.sets.lookback = 2400
        invalid._hist_fetch(["A-USDT"])
        invalid_cached = "A-USDT" in invalid._hist_short_cache
        invalid.api.requests.clear()
        invalid._hist_fetch(["A-USDT"])
        invalid_retry = len(invalid.api.requests) > 1
        failed = _bare_pulse()
        failed.api, failed.sets = Failing(n=2501), se.SetBook()
        failed.sets.lookback = 2400
        first = failed._hist_fetch(["A-USDT"])
        failed_cached = "A-USDT" in failed._hist_short_cache
        failed.api.requests.clear()
        failed._hist_fetch(["A-USDT"])
        failed_retry = len(failed.api.requests) > 1
        duplicate = _bare_pulse()
        duplicate.api, duplicate.sets = Duplicating(n=2501), se.SetBook()
        duplicate.api.rows[0].update(open="0.1", high="0.2", low="0.05", close="0.1")
        duplicate.sets.lookback = 2400
        duplicate._hist_fetch(["A-USDT"])
        duplicate_cached = "A-USDT" in duplicate._hist_short_cache
        return (not invalid_cached and invalid_retry and not first and not failed_cached and failed_retry
                and not duplicate_cached), f"invalid cached/retried={invalid_cached}/{invalid_retry}; failed cached/retried={failed_cached}/{failed_retry}; duplicate cached={duplicate_cached}"
    finally:
        pt.SYMBOLS[:], pt.time.sleep = saved, sleep


@_frozen_clock
def flat_candle_gap_cannot_preserve_short_history_cache():
    """Identical candle values must not hide a real timestamp gap."""
    import set_engine as se
    saved, sleep = list(pt.SYMBOLS), pt.time.sleep
    try:
        pt.SYMBOLS[:] = ["A-USDT"]
        pt.time.sleep = lambda _s: None
        p = _bare_pulse()
        p.api, p.sets = _PagedKlines(n=2501), se.SetBook()
        for row in p.api.rows:
            row.update(open="100", high="100", low="100", close="100")
        p.sets.lookback = 2400
        p._hist_fetch(["A-USDT"])
        p.sets.bar_t["A-USDT"] -= 300 * 60
        p.api.requests.clear()
        gap = p._hist_fetch(["A-USDT"])
        gap_limits, remaining = [int(row["limit"]) for row in p.api.requests], len(p.sets.bars["A-USDT"])
        p.api.requests.clear()
        recovered = p._hist_fetch(["A-USDT"])
        return (gap == set() and gap_limits == [120] and remaining == 119 and recovered == {"A-USDT"}
                and len(p.api.requests) > 1), f"gap={gap}; remaining={remaining}; recovery pages={len(p.api.requests)}"
    finally:
        pt.SYMBOLS[:], pt.time.sleep = saved, sleep


def fifty_short_histories_refresh_without_full_repaging():
    """Bound request volume across a synthetic 50-symbol short-history universe."""
    import set_engine as se
    saved, sleep, clock = list(pt.SYMBOLS), pt.time.sleep, pt.time.time
    try:
        pt.SYMBOLS[:] = [f"S{i:02d}-USDT" for i in range(50)]
        pt.time.sleep = lambda _s: None
        # one frozen clock for the fake venue and the code under test: the venue stamps its candles from the clock, and a minute boundary between
        # the two 50-symbol fetches (easy under load) made the stored history look a minute stale and re-paged it (found on the pushed 58af012 too)
        now = clock()
        pt.time.time = lambda: now
        p = _bare_pulse()
        p.api, p.sets = _PagedKlines(n=2501, t_end=int(now // 60) * 60_000), se.SetBook()
        p.api.rows[0].update(open="0.1", high="0.2", low="0.05", close="0.1")
        p.sets.lookback = 2400
        first = p._hist_fetch()
        first_requests = len(p.api.requests)
        p.api.requests.clear()
        second = p._hist_fetch()
        limits = [int(row["limit"]) for row in p.api.requests]
        return (len(first) == len(second) == 50 and first_requests == 150
                and len(limits) == 50 and set(limits) == {120}), f"symbols={len(second)}; requests backfill/refresh={first_requests}/{len(limits)}; refresh limits={set(limits)}"
    finally:
        pt.SYMBOLS[:], pt.time.sleep, pt.time.time = saved, sleep, clock


def history_chunks_rotate_even_when_many_positions_are_open():
    """Every history symbol must eventually refresh; live open-position priority cannot starve it."""
    from load_engine import LoadGovernor
    g = LoadGovernor()
    names = [f"S{i:02d}" for i in range(50)]
    cursor, seen, sizes = 0, set(), []
    for _ in range(9):
        chunk, cursor = g.history_window(names, 6, cursor)
        seen.update(chunk)
        sizes.append(len(set(chunk)))
    g.partial = False
    full, reset = g.history_window(names, 6, cursor)
    ok = len(seen) == 50 and sizes == [6] * 9 and full == names and reset == 0
    return ok, f"covered={len(seen)}/50 after {len(sizes)} passes; unique chunk sizes={sizes}; partial off returns all={full == names}"


def load_governor_keeps_the_set_replay_running_at_the_books_footprint():
    """The Set replay must run at the memory the Set book itself needs. Found live on x02 (2026-11-03): 30 symbols x 3 days of 1m history x 3,176 Sets
    held about 250 MB before the first replay; the fixed limits (hard 156 MB at 30 symbols, critical 236 MB) made the governor critical, it shed the replay
    for good and no Set was ever validated or traded. The limits now take a share of the machine's memory; with no machine figure they keep the formula."""
    import load_engine as le
    saved = dict(le._MACHINE)
    try:
        le._MACHINE["mb"] = 1024.0                         # a small 1 GB host
        g = le.LoadGovernor()
        b = g.observe(n_sym=30, n_open=0, rss_mb=250.0)
        small_ok = b.hist_run and b.level in ("idle", "normal", "busy")
        b_big = g.observe(n_sym=30, n_open=0, rss_mb=900.0)        # far above the machine share: still sheds
        le._MACHINE["mb"] = 0.0                            # unknown machine: the per-symbol formula as before
        g2 = le.LoadGovernor()
        b0 = g2.observe(n_sym=30, n_open=0, rss_mb=250.0)
        g3 = le.LoadGovernor()
        g3.configure({"rssHardMb": 120})                   # an explicit setting still wins
        explicit = g3.hard_limit(30) == 120.0
    finally:
        le._MACHINE.clear()
        le._MACHINE.update(saved)
    ok = small_ok and not b_big.hist_run and b0.level == "critical" and not b0.hist_run and explicit
    return ok, (f"1 GB host at 250 MB: level={b.level} replay={b.hist_run}; at 900 MB: {b_big.level} replay={b_big.hist_run}; unknown host at 250 MB: "
                f"{b0.level} replay={b0.hist_run}; rssHardMb wins={explicit}")


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


def extra_set_client_ids_never_guess_the_owner():
    """Additional packs retain identity across grid changes; a bad digest cannot become a General Set."""
    b = _book_with_three_packs(stratTrend=True, extraIndicators=["donchian_break", "rsi2_reversion"])
    p = _bare_pulse()
    p.sets = b
    extras = [s for s in b.by_idx if s.pack.startswith("extra.") or s.pack == "trend"]
    samples = [next(s for s in extras if s.pack == pack and s.kind == kind and (kind == "trail" or s.step >= 6))
               for pack in ("extra.donchian_break", "extra.rsi2_reversion", "trend") for kind in ("base", "exit", "trail")]
    ids = [(s.id, p.cid("o", set_id=s.id, pack=s.pack, set_idx=s.idx, lane="neg")) for s in samples]
    roundtrip = all(len(cid) <= 32 and (p.parse_track(cid) or {}).get("set_id") == sid
                    and (p.parse_track(cid) or {}).get("lane") == "neg" for sid, cid in ids)
    st = samples[0]
    high = p.cid("o", set_id=st.id, pack=st.pack, set_idx=10025)
    high_ok = (p.parse_track(high) or {}).get("set_id") == st.id
    at = len(pt.TAG) + 13
    bad = ids[0][1]
    bad = bad[:at] + ("0" if bad[at] != "0" else "1") + bad[at + 1:]
    rejected = (p.parse_track(bad) or {}).get("set_id") == ""
    b.min_step = 6
    b._rebuild_sets()
    after = [(sid, (p.parse_track(cid) or {}).get("set_id")) for sid, cid in ids]
    moved_ok = all(got in ("", sid) for sid, got in after)
    return roundtrip and high_ok and rejected and moved_ok, f"roundtrip={roundtrip} high index={high_ok} bad digest rejected={rejected} grid change={moved_ok}"


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

        def pick_any(self, pack, side=0):
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
    pos = pt.Position("ZCAT-USDT", "LONG", 1, .04, 1, .03, .05, .04, tp_oid="2115874311641849856")
    p.save_open_book = lambda: True
    p.fill_cost = lambda *args: (0, "reported-fee")

    class _Api:
        def __init__(self, sym, px):
            self.sym, self.px = sym, px

        def get(self, path, params=None):
            return {"code": 0, "data": {"order": {"symbol": self.sym, "positionSide": "LONG", "orderId": "2115874311641849856", "executedQty": "1", "status": "FILLED", "avgPrice": self.px}}}

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
        p.available = 50.0
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
    ok = (pos is not None and abs(pos.tp_pct - 2 * c) < 1e-9 and abs(pos.sl_pct - (c + .001)) < 1e-9
          and pos.trail_arm == 0.0 and pos.trail_give == 0.0 and not pos.trail_set_id)     # a base Set does not trail
    return ok, (f"cost={cost}% tp={getattr(pos, 'tp_pct', None)} sl={getattr(pos, 'sl_pct', None)} want TP {2 * c}, SL {c + .001}; "
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
            p.sets.set_scratch = True                      # the scratch exit is a Set-lane option (setScratchEnabled); this check reads it on
            p.exits.enabled = False
            p.px[sym] = pos.entry * (1 + tick)
            p.last_closed_1m = {sym: (int(time.time() // 60) - 1, pos.entry * (1 + close))}
            p.manage()
            out[name] = (sym in p.open, [c.reason for c in p.closed])
        # off (the default): the same close above the scratch level leaves the Set position open; only its own stop, target and time close it
        p, sym, pos = _lane_pulse()
        p.sets.set_scratch = False
        p.exits.enabled = False
        p.px[sym] = pos.entry * 1.003
        p.last_closed_1m = {sym: (int(time.time() // 60) - 1, pos.entry * 1.003)}
        p.manage()
        out["above scratch, scratch off"] = (sym in p.open, [c.reason for c in p.closed])
        p, sym, pos = _lane_pulse()
        pos.opened_at = time.time() - (p.sets.hist_time_bars * 60.0 + 5.0)
        pos.entry = 100.0
        p.px[sym] = 100.0 * 0.9995
        p.manage()
        out["time"] = (sym in p.open, [c.reason for c in p.closed])
        return out
    out = _isolated(run)
    ok = out["below scratch"][0] and out["above scratch"] == (False, ["scratch+"]) and out["tick above, close below"][0] \
        and out["time"] == (False, ["time"]) and out["above scratch, scratch off"] == (True, [])
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


@_frozen_clock
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
        p.api = _PagedKlines(n=8000)
        p.sets = se.SetBook()
        from common import overlay, SMALL
        p.sets.load(overlay(**{**SMALL, "holdEnabled": False}))
        p.sets.lookback = 1920
        target = p.sets.history_bars()
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
    ok = all(n == target for n in sizes.values()) and flat == 0 and replayed == 4
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
        requested = []
        def gather(reqs, timeout=4.2):
            requested.extend(y.get("symbol") for _, y in reqs)
            return [(x, y, {"code": 0, "data": []}) for x, y in reqs]
        p.api = types.SimpleNamespace(gather_public=gather)
        p.sets = se.SetBook()
        p.sets.load(overlay(**SMALL))
        for k, s_ in enumerate(("A-USDT", "B-USDT", "GONE-USDT", "HELD-USDT")):
            p.sets.bars[s_] = synth_bars(70 + k, 1920, start=50.0 + k, vol=0.003)
            p.real_1m[s_] = 0
        p.sets.replay_all()
        before = sorted(p.sets._hist_rows)
        replayed = []
        replay_symbol = p.sets._replay_symbol
        def observe(symbol, *args, **kwargs):
            replayed.append(symbol)
            return replay_symbol(symbol, *args, **kwargs)
        p.sets._replay_symbol = observe
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
        and "HELD-USDT" in after_bars and "HELD-USDT" in replayed and "HELD-USDT" in requested \
        and {"A-USDT", "B-USDT"} <= set(after_bars)
    return ok, f"rows before={before}; bars after={after_bars}; rows after={after_rows}; replayed={replayed}; fetched={requested}"


@_frozen_clock
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
        probe = se.SetBook()
        probe.load(overlay(**SMALL))
        probe.lookback = 1920
        target = probe.history_bars()
        kept, kept_flat = run(_Offline(), target)
        topped, topped_flat = run(_PagedKlines(n=8000), 500)
        livetail, _ = run(_Offline(), target, live=True)
        boot_live, boot_flat = run(_Offline(), 0, live=True)      # no history yet: the live-built real rows bootstrap it, never the padding
        boot_none, _ = run(_Offline(), 0)                          # no history and no real rows: nothing is ingested
    finally:
        pt.SYMBOLS[:] = saved
    ok = all(n == target for n in kept) and kept_flat == 0 and all(n == target for n in topped) and topped_flat == 0 and all(n == target for n in livetail) \
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
            p.available = 50.0
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
        p.cancel_controls = lambda s_, **kw: None
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


class _EntryBudgetVenue(_BookVenue):
    """In-memory venue for entry budgets and retry IDs; no network or real orders."""

    def __init__(self, required_qty=None, reconcile_by_cid=False, first_error=None, top_qty=100, fail_leverage=False):
        super().__init__({"askPrice": 100.0, "askQty": top_qty, "bidPrice": 99.99, "bidQty": top_qty})
        self.required_qty = required_qty
        self.reconcile_by_cid = reconcile_by_cid
        self.first_error = first_error
        self.fail_leverage = fail_leverage
        self.entry_posts = []
        self.order_reads = []
        self.leverage_posts = []

    def post(self, path, body):
        if path.endswith("/trade/leverage"):
            self.leverage_posts.append(dict(body))
            if self.fail_leverage:
                return {"code": 100400, "msg": "leverage update refused"}
        if path.endswith("/trade/order") and body.get("type") == "MARKET":
            row = dict(body)
            self.posts.append((path, row))
            self.entry_posts.append(row)
            if len(self.entry_posts) == 1 and self.first_error is not None:
                return {"code": 109400, "msg": self.first_error}
            if len(self.entry_posts) == 1 and self.required_qty is not None:
                return {"code": 109400, "msg": f"minimum order amount is {self.required_qty}"}
            if self.reconcile_by_cid:
                return {"code": 0, "data": {"order": {"status": "NEW"}}}
            return {"code": 0, "data": {"order": {"orderId": "991", "status": "FILLED", "avgPrice": "100",
                                                     "executedQty": str(row["quantity"])}}}
        return super().post(path, body)

    def get(self, path, extra=None):
        if path.endswith("/trade/order") and self.reconcile_by_cid:
            params = dict(extra or {})
            self.order_reads.append(params)
            last = self.entry_posts[-1]
            if params.get("clientOrderId") == last["clientOrderID"]:
                return {"code": 0, "data": {"order": {"symbol": "BTC-USDT", "status": "FILLED",
                                                          "avgPrice": "100", "executedQty": str(last["quantity"])}}}
            return {"code": 109421, "msg": "order not exist"}
        return super().get(path, extra)


def _entry_budget_case(*, strict, multiple=1.0, carry=False, required_qty=None, reconcile=False,
                       available=100.0, first_error=None, top_qty=100, has_set=True, fail_leverage=False):
    def run():
        sym = "BTC-USDT"
        venue = _EntryBudgetVenue(required_qty, reconcile, first_error, top_qty, fail_leverage)
        contract = pt.Contract(sym, 0.001, 0.001, 3, 2, 0.1, 150)
        p = pt.Pulse(venue, {sym: contract})
        p.boot_ts, p.halted, p.available, p.equity = 0.0, False, available, 100.0
        p.size_pct, p.volume_factor, p.size_max, p.strict_size_cap = 1.0, 1.0, 25.0, strict
        p.px[sym] = 100.0
        p.entry_sense = lambda *a, **k: None
        p.ensure_max_leverage = lambda *a, **k: 150
        p.lev_max[sym] = p.lev_map[sym] = 150
        p.control_orders = False
        p.sets.use_historic_gate = False
        next_cid = iter(range(1, 20))
        p.cid = lambda *args, **kwargs: f"{pt.TAG}otest{next(next_cid)}"
        st = types.SimpleNamespace(id="indications:L:1m:sl1.00:st5", pack="indications", kind="base", step=5, idx=0,
                                   sl_ratio=1.0, trail_key="", trail_arm=0.0, trail_give=0.0, hold_bars=0)
        p._scope_choice = lambda pack, side=0, symbol="": (st, "pos", "") if has_set else (None, "", "")
        p.sets.lane_fracs = lambda _st: (0.01, 0.015)
        p.volume_by_pack, p.volume_by_kind, p.volume_by_range = {}, {}, {}
        p.volume_by_pack_kind = {"indications:base": multiple}
        if carry:
            p.block.enabled = p.strat_block = True
            if has_set:
                p.block_cfg.carry = True
                p.set_block = types.SimpleNamespace(entry_scale=lambda *args: 2.0)
            else:
                p.block.parent_scale = lambda *args: 2.0
        try:
            p.place(sym, 1, "set:indications:x", 0.9)
            pos = p.open.get(sym)
            return {"notionals": [round(float(row["quantity"]) * 100.0, 5) for row in venue.entry_posts],
                    "ids": [row["clientOrderID"] for row in venue.entry_posts],
                    "read_ids": [row.get("clientOrderId") for row in venue.order_reads],
                    "position_qty": None if pos is None else pos.qty,
                    "position_cid": None if pos is None else pos.client_id, "available_after": p.available,
                    "leverage_posts": len(venue.leverage_posts), "leverage_recorded": p.lev_map.get(sym)}
        finally:
            p.pool.shutdown(wait=True)
    return _isolated(run)


def strict_entry_budget_survives_set_multipliers_and_block_carry():
    fixed = _entry_budget_case(strict=True, multiple=1.5)
    carried = _entry_budget_case(strict=True, carry=True)
    carried_plain = _entry_budget_case(strict=True, carry=True, has_set=False)
    legacy = _entry_budget_case(strict=False, multiple=1.5)
    ok = (fixed["notionals"] == [1.0] and carried["notionals"] == [1.0]
          and carried_plain["notionals"] == [1.0] and legacy["notionals"] == [1.5])
    return ok, f"strict multiplier={fixed['notionals']} Set/plain carries={carried['notionals']}/{carried_plain['notionals']} legacy={legacy['notionals']}"


def minimum_order_retry_respects_budget_and_reconciles_its_own_id():
    strict = _entry_budget_case(strict=True, required_qty=0.02)
    margin = _entry_budget_case(strict=False, required_qty=0.1, available=0.02)
    allowed = _entry_budget_case(strict=True, multiple=0.5, required_qty=0.008, reconcile=True)
    thin = _entry_budget_case(strict=True, multiple=0.5, required_qty=0.008, top_qty=0.006)
    malformed = _entry_budget_case(strict=True, required_qty="1..2")
    ok = (strict["notionals"] == [1.0] and strict["position_qty"] is None
          and margin["notionals"] == [1.0] and margin["position_qty"] is None
          and allowed["notionals"] == [0.5, 0.8] and allowed["position_qty"] == 0.008
          and allowed["ids"][0] != allowed["ids"][1] and allowed["read_ids"] == [allowed["ids"][1]]
          and allowed["position_cid"] == allowed["ids"][1]
          and thin["notionals"] == [0.5] and thin["position_qty"] is None
          and malformed["notionals"] == [1.0] and malformed["position_qty"] is None)
    return ok, f"strict={strict}, low margin={margin}, allowed={allowed}, thin={thin['notionals']}, malformed={malformed['notionals']}"


def leverage_retry_checks_margin_and_reconciles_its_own_id():
    error = "maximum leverage is 30"
    low = _entry_budget_case(strict=True, first_error=error, available=0.02)
    allowed = _entry_budget_case(strict=True, first_error=error, available=0.1, reconcile=True)
    rejected = _entry_budget_case(strict=True, first_error=error, available=0.1, fail_leverage=True)
    ok = (low["notionals"] == [1.0] and low["position_qty"] is None
          and low["leverage_posts"] == 0
          and allowed["notionals"] == [1.0, 1.0] and allowed["position_qty"] == 0.01
          and allowed["ids"][0] != allowed["ids"][1] and allowed["read_ids"] == [allowed["ids"][1]]
          and allowed["position_cid"] == allowed["ids"][1]
          and abs(allowed["available_after"] - (0.1 - 1.0 / 30.0)) < 1e-9
          and allowed["leverage_posts"] == 2 and allowed["leverage_recorded"] == 30
          and rejected["notionals"] == [1.0] and rejected["position_qty"] is None
          and rejected["leverage_posts"] > 0 and rejected["leverage_recorded"] != 30)
    return ok, f"low margin={low}, allowed retry={allowed}, leverage refused={rejected}"


def size_budget_preserves_exact_venue_minimum():
    """A min-lot notional/price round trip must not lose a quantity step."""
    import pulse_trader as pt
    p = pt.Pulse.__new__(pt.Pulse)
    p.equity = p.available = 3.0
    p.size_pct, p.volume_factor, p.size_max = 5.0, .05, 25.0
    p.leverage_for = lambda _c: 1
    c = pt.Contract("STRK-USDT", 53.996, .001, 3, 5, 2.0, 1)
    px = .03735
    minimum = p.min_order_qty(c, px)
    p.strict_size_cap = False
    legacy = p.size_qty(c, px)
    p.strict_size_cap = True
    small = p.size_qty(c, px)
    p.size_pct, p.volume_factor, p.size_max = 100.0, 1.0, minimum * px
    exact = p.size_qty(c, px)
    p.size_max = (minimum - c.step) * px
    below = p.size_qty(c, px)
    p.strict_size_cap, p.available = False, 1.0
    no_margin = p.size_qty(c, px)
    return (minimum == 53.996 and legacy == minimum and small == 0
            and exact == minimum and below == 0 and no_margin == 0), (
        f"minimum={minimum} legacy={legacy} strictSmall={small} strictExact={exact} "
        f"strictBelow={below} noMargin={no_margin}")


def strict_size_budget_skips_oversized_venue_minimum():
    """The opt-in small-balance budget skips an oversized minimum; legacy sizing remains unchanged."""
    p = _bare_pulse()
    p.available, p.equity, p.size_pct, p.volume_factor, p.size_max = 100.0, 100.0, 2.0, 1.0, 5.0
    p.leverage_for = lambda _c: 1
    c = pt.Contract(symbol="TEST-USDT", min_qty=0.01, step=0.01, qprec=2, pprec=2, min_usdt=5.0, max_lev=50)
    p.strict_size_cap = True
    small = p.size_qty(c, 100.0)               # 2 USDT budget cannot buy the 5 USDT minimum
    p.equity = 500.0
    capped = p.size_qty(c, 100.0)              # budget is capped at 5 USDT
    p.equity = 0.0
    unknown = p.size_qty(c, 100.0)             # no fixed-notional fallback
    p.equity = float("inf")
    nonfinite = p.size_qty(c, 100.0)           # a corrupt balance cannot pass by hitting the size ceiling
    p.equity, p.strict_size_cap = 100.0, False
    legacy = p.size_qty(c, 100.0)              # older overlays keep their existing behavior
    ok = small == 0 and abs(capped * 100.0 - 5.0) < 1e-9 and unknown == 0 and nonfinite == 0 and legacy * 100.0 >= 5.0
    return ok, f"small={small} capped_notional={capped * 100.0} unknown={unknown} nonfinite={nonfinite} legacy_notional={legacy * 100.0}"


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
                                   pick_any=lambda pack, side=0: types.SimpleNamespace(id="x"), pack_open=lambda pack: True)
    p.strat_ind = False
    p.px = {"BAD-USDT": 1.0, "OK-USDT": 1.0}
    p.occupying = lambda *a, **k: False
    bad = p.entry_sense("BAD-USDT", 1, "gen:x", 0.9, "general")
    good = p.entry_sense("OK-USDT", 1, "gen:x", 0.9, "general")
    return bad == "data-quality" and good != "data-quality", f"failed symbol -> {bad}; clean symbol -> {good}"


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
            p._scope_choice = lambda pack, side=0, symbol="": (object(), "pos", "")  # declared admitted stage; signal transport only
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


def thirty_ready_symbols_are_ranked_in_one_bounded_signal_batch():
    """All 30 ready symbols can compete before an opening; larger universes remain bounded and finish next cycle."""
    minute = 1_790_000_100.0
    names = [f"S{i:02d}-USDT" for i in range(31)]

    class Sets:
        packs = ["general"]
        bar_t = {s: minute - 60 for s in names}
        def entry_signals(self, sym, _t):
            return [("general", 1, (names.index(sym) + 1) / 100, "signal")]

    def pulse():
        p = _bare_pulse()
        p.open = {}
        p.sets = Sets()
        p._scope_choice = lambda pack, side=0, symbol="": (object(), "pos", "")
        return p

    old_syms = pt.SYMBOLS
    try:
        p = pulse()
        pt.SYMBOLS = names[:30]
        first = p.set_entry_candidates(now=minute + 1)
        best = max(first.values()) if first else None
        q = pulse()
        pt.SYMBOLS = names
        bounded = q.set_entry_candidates(now=minute + 1)
        completed = q.set_entry_candidates(now=minute + 2)
    finally:
        pt.SYMBOLS = old_syms
    ok = len(first) == 30 and best and best[1] == names[29] and len(bounded) == 30 and len(completed) == 31
    return bool(ok), f"30 ready={len(first)}, highest={best and best[1]}, 31-symbol batches={len(bounded)}/{len(completed)}"


def strongest_ready_symbol_can_take_the_first_slot():
    """A stronger 30th ready signal competes at the real entry loop, not merely in the candidate helper."""
    from coord_engine import Coordinator
    minute = 1_790_000_100.0
    names = [f"S{i:02d}-USDT" for i in range(30)]
    class Sets:
        enabled = use_historic_gate = True
        packs = ["general"]
        bar_t = {s: minute - 60 for s in names}
        def entry_signals(self, sym, _t):
            return [("general", 1, (names.index(sym) + 1) / 100, "signal")]
        def pick_any(self, pack, side=0):
            return types.SimpleNamespace(last15_pf=2.0, last15_n=50, pack=pack)
        def pack_open(self, _pack):
            return True
    p = _bare_pulse()
    p.sets, p.open, p.halted = Sets(), {}, False
    p.strat_general = False
    p.coord = Coordinator()
    p.coord.load({}, {})
    p._scope_choice = lambda pack, side=0, symbol="": (object(), "pos", "")
    p.strategy_closes = lambda: []
    p.real_stage_paper = False
    p.entries_blocked = lambda: False
    p.rearrange_opens = lambda: []
    p.maybe_block_adds = p.maybe_dca_adds = lambda: None
    p.available, p.cooldown, p.skip_log, p.contracts, p.px = 100.0, {}, {}, {}, {}
    p.avail_notional = lambda *a: 100.0
    placed = []
    def place(sym, _direction, _why, _conf):
        placed.append(sym)
        p.open[sym] = object()
    p.place = place
    old_syms, old_max, old_time = pt.SYMBOLS, pt.MAX_OPEN, pt.time.time
    try:
        pt.SYMBOLS, pt.MAX_OPEN = names, 1
        pt.time.time = lambda: minute + 5
        p.maybe_entries()
    finally:
        pt.SYMBOLS, pt.MAX_OPEN, pt.time.time = old_syms, old_max, old_time
    return placed == [names[-1]], f"first opened={placed}; expected strongest ready symbol={names[-1]}"


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
    prices = [bar(k) for k in range(7)]
    prices[3]["close"] = "nan"
    valid_tail, valid_t = p._closed_klines(prices, now=minute + 20)
    prices[1]["open"] = "0"
    invalid_latest = p._closed_klines(prices, now=minute + 20)
    ok = (len(bars) == 2 and [b[3] for b in bars] == [2.02, 2.01] and last_t == minute - 60
          and rejected == ([], None) and [b[3] for b in valid_tail] == [2.02, 2.01]
          and valid_t == minute - 60 and invalid_latest == ([], None))
    return ok, (f"contiguous closes={[b[3] for b in bars]}, duplicate removed, malformed series rejected={rejected == ([], None)}; "
                f"invalid price retained suffix={[b[3] for b in valid_tail]}, invalid latest rejected={invalid_latest == ([], None)}")


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
        def pick_any(self, pack, allowed_lanes=None, side=0):
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
        def pick_any(self, _pack, allowed_lanes=None, side=0):
            if allowed_lanes is None or "pos" in allowed_lanes:
                return types.SimpleNamespace(lane="pos")
            return alternative if "neg" in allowed_lanes else None
    p.sets = TwoLanes()
    picked, blocked = p._scope_pick("strong")
    ok = (first and first[3].startswith("set:valid:") and second and second[3].startswith("set:valid:")
          and third and third[3].startswith("set:strong:") and picked is alternative and not blocked
          and scope == ((True, ""), (False, "pack:strong:pos")))
    return bool(ok), f"selected={first}, {second}, {third}; scopes={scope}; negative alternative={picked is alternative}"


def one_dynamic_set_axis_does_not_block_other_sets():
    """With real SetBook selection, a blocked high-ranked Set cannot mask another Set's lane."""
    from coord_engine import Coordinator
    from set_engine import SetBook
    import time

    book = SetBook()
    book.load({"stagesEnabled": False, "histLookbackAuto": False, "setGateMinTrades": 0,
               "minPfBySize": False, "minPfByStep": False, "directionMode": "both", "pauseMode": "off", "setMinPf": 1.1, "setMinStep": 2, "setStepMax": 3,
               "setSlRatios": [0.5, 1.0], "trailVariants": ["0.3:0.1"],
               "stratGeneral": True, "stratIndications": False})
    rows = [st for st in book.by_idx if st.pack == "general" and st.kind == "base"]
    first, second = rows[:2]
    book.by_idx = [first, second]
    book.sets = {st.id: st for st in book.by_idx}
    now = time.time()
    for st, score in ((first, 2.0), (second, 1.5)):
        st.active, st.locked, st.last_error = True, False, ""
        st.gate_n, st.gate_pf, st.n = 20, score, 20
        st.max_dd_s, st.score_at = 0, now
    coordinator = Coordinator()
    coordinator.load({}, {"axisLastEnabled": True, "axisPrevEnabled": False, "axisPauseEnabled": False,
                          "axisContEnabled": False, "minPfLinked": False, "minPf": 1.1})
    p = _bare_pulse()
    p.sets, p.coord, p.open, p.position_cost_pct = book, coordinator, {}, 0.12

    def evidence(st, result):
        st.exe = [(now - 60 * i, result) for i in range(4, 0, -1)]

    evidence(first, -0.004)
    evidence(second, 0.004)
    selected_after_first_fails = p._scope_choice("general")
    evidence(first, 0.004)
    evidence(second, -0.004)
    selected_after_second_fails = p._scope_choice("general")
    evidence(first, -0.004)
    none_when_both_fail = p._scope_choice("general")
    ok = (first.id != second.id and selected_after_first_fails[0] is second
          and selected_after_second_fails[0] is first and none_when_both_fail[0] is None)
    return ok, (f"independent Sets={first.id}, {second.id}; first blocked -> "
                f"{getattr(selected_after_first_fails[0], 'id', None)}; second blocked -> "
                f"{getattr(selected_after_second_fails[0], 'id', None)}; both blocked -> none={none_when_both_fail[0] is None}")


def entry_does_not_open_when_the_set_disappears_after_sense():
    """A validation change between entry_sense and placement cannot create an untagged live position."""
    def run():
        sym = "BTC-USDT"
        venue = _StubExchange(executed="1", requested="1")
        contract = pt.Contract(symbol=sym, min_qty=.001, step=.001, qprec=3, pprec=2, min_usdt=5, max_lev=50)
        p = pt.Pulse(venue, {sym: contract})
        p.boot_ts, p.available, p.halted = 0.0, 50.0, False
        p.px[sym] = 100.0
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
    exits = {(x.pack, x.step, round(x.sl_ratio, 9)) for x in b.by_idx if x.kind == "exit"}
    base = next(x for x in b.by_idx if x.kind == "base" and (x.pack, x.step, round(x.sl_ratio, 9)) in exits)   # a pack that runs Exit Sets (indications' are off)
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
    p._scope_choice = lambda pack, side=0, symbol="": (object(), "pos", "")  # declared admitted stage; signal transport only
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
    p._scope_choice = lambda pack, side=0, symbol="": (object(), "pos", "")  # declared admitted stage; signal transport only
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
    # the Exit lane's first wait is the module's COOLDOWN_S, which any check that applies a real overlay leaves at that overlay's value: state its own
    old_cooldown, pt.COOLDOWN_S = pt.COOLDOWN_S, 9.0
    try:
        exit_waits = [p.symbol_backoff("B-USDT", -0.01, 300.0, "sl") for _ in range(3)]
    finally:
        pt.COOLDOWN_S = old_cooldown
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
            p.px[sym] = 100.0
            p.entry_sense = lambda *a, **k: None
            st = p.sets.by_idx[0]
            p.sets.pick_any = lambda pack, _st=st, **kw: _st
            p.sets.pick_trail = lambda pack: None
            p.sets.lane_of = lambda _st, _l=lane: _l
            st.score_at = time.time()
            st.exe = [(st.score_at - 60 * j, .004) for j in range(12, 0, -1)]
            st.neg = [(st.score_at - 60 * j, -.004) for j in range(12, 0, -1)]
            p.sets.pick_any = lambda pack, _st=st, predicate=None, allowed_lanes=None, side=0: _st if (
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



def volume_factor_is_bounded_and_defaults_to_the_minimum():
    """volumeFactor (owner: default and minimum 0.05): an overlay without it, with 0, a negative or an unreadable value sizes at 0.05, never at 1.0 (the
    earlier fallback, 20 x the default); a valid value is kept inside 0.05 .. 10; the sized notional follows it."""
    got = {repr(raw): pt.volume_factor_of(raw) for raw in (None, 0, -1, "x", float("nan"), 0.05, 0.3, 1, 3, 99)}
    want = {"None": 0.05, "0": 0.05, "-1": 0.05, "'x'": 0.05, "nan": 0.05, "0.05": 0.05, "0.3": 0.3, "1": 1.0, "3": 3.0, "99": 10.0}
    p = _bare_pulse()
    p.equity, p.size_pct, p.size_max = 1000.0, 1.0, 1e9
    sizes = {}
    for raw in (None, 0, 1, 2):
        p.volume_factor = pt.volume_factor_of(raw)
        sizes[repr(raw)] = round(p.sized_notional(), 6)
    ok = got == want and sizes == {"None": 0.5, "0": 0.5, "1": 10.0, "2": 20.0} and pt.VOLUME_FACTOR_MIN == 0.05
    return ok, f"factors {got}; notional at 1 % of 1,000 USDT: {sizes}"

def a_venue_restricted_symbol_rests_thirty_minutes_after_a_refused_entry():
    """x01 2026-11-04: "Reminder: Due to the large market fluctuations, in order to reduce the risk of liquidation, API orders are temporarily disabled" on MAGMA /
    POWER / NES was retried every 2 - 3 minutes for hours. A symbol the venue restricts rests 30 minutes; cooling 45 s; any other refusal 12 s."""
    msg = "Reminder: Due to the large market fluctuations, in order to reduce the risk of liquidation, API orders are temporarily disabled for this symbol"
    got = [pt.entry_fail_cooldown_s(msg), pt.entry_fail_cooldown_s("venue is cooling"), pt.entry_fail_cooldown_s("The minimum order amount is 15"), pt.entry_fail_cooldown_s("")]
    src = open(os.path.join(PULSE, "pulse_trader.py"), encoding="utf-8").read()
    wired = "self.cooldown[sym] = time.time() + entry_fail_cooldown_s(low)" in src
    return got == [1800.0, 45.0, 12.0, 12.0] and wired, f"cooldowns {got}; entry path uses it: {wired}"


def a_manual_close_rests_only_that_symbol_and_direction():
    """Owner 2026-11-04: closing a position by hand must not make the engine reopen the exact same position, and must not stop trading. A position the venue no
    longer holds and that left no stop / target fill of ours (drop_ghost, a restored position gone) puts (symbol, side) to rest for manualCloseRestMin; the other
    direction of that symbol and every other symbol are untouched; the rest persists in a file, expires, and 0 switches it off; place() honours it."""
    import types, time as _t
    old = pt.MANUAL_REST_PATH
    with tempfile.TemporaryDirectory() as d:
        pt.MANUAL_REST_PATH = os.path.join(d, "manual-rest.json")
        try:
            fake = types.SimpleNamespace(manual_rest={}, manual_rest_s=240 * 60.0)
            pt.Pulse.note_manual_close(fake, types.SimpleNamespace(symbol="AAA-USDT", side="LONG"), "ctrl-no-position")
            now = _t.time()
            saved = json.load(open(pt.MANUAL_REST_PATH))
            active = [pt.manual_rest_active(fake.manual_rest, s, sd, now) for s, sd in (("AAA-USDT", "LONG"), ("AAA-USDT", "SHORT"), ("BBB-USDT", "LONG"))]
            later = pt.manual_rest_active(fake.manual_rest, "AAA-USDT", "LONG", now + 241 * 60)
            fake.manual_rest_s = 0.0
            pt.Pulse.note_manual_close(fake, types.SimpleNamespace(symbol="CCC-USDT", side="SHORT"), "ctrl-no-position")
            off = "CCC-USDT:SHORT" not in fake.manual_rest
            expired = {"X:LONG": now - 1, "Y:LONG": now + 60}
            pruned = pt.manual_rest_prune(expired, now)
        finally:
            pt.MANUAL_REST_PATH = old
    src = open(os.path.join(PULSE, "pulse_trader.py"), encoding="utf-8").read()
    wired = ("manual_rest_active(getattr(self, \"manual_rest\", {}), sym, \"LONG\" if direction > 0 else \"SHORT\", time.time())" in src
             and src.count("self.note_manual_close(pos,") >= 3
             and "DROP stale local {sym} age={age:.0f}s\")\n            self.note_manual_close(pos," in src)
    ok = active == [True, False, False] and not later and off and pruned == 1 and list(expired) == ["Y:LONG"] and list(saved) == ["AAA-USDT:LONG"] and wired
    return ok, f"rest active (AAA long, AAA short, BBB long) {active}, after 241 min {later}, 0 = off {off}, persisted {list(saved)}, pruned {pruned}, wired into place() and both drop paths {wired}"


def transfers_move_the_drawdown_basis_not_the_drawdown():
    """Owner 2026-11-04 ("on each session money got transferred"): a deposit or a withdrawal is neither a gain nor a loss. The wallet balance moves by realized PnL, fees and
    funding (the venue's income feed) and by transfers (not in the feed): a change the feed does not explain, confirmed on a second read (the feed can lag a fill), moves the
    session and start equity by it; PnL-explained changes, a lagging feed that catches up, a failed income read and transferAdjust off move nothing; a real trading loss
    still reads as drawdown."""
    import types, time as _t
    old = pt.START_EQ_PATH
    income = {"rows": [], "fail": False}

    def get(path, extra=None):
        if income["fail"]:
            raise RuntimeError("income unavailable")
        return {"code": 0, "data": list(income["rows"])}
    out = {}
    with tempfile.TemporaryDirectory() as d:
        pt.START_EQ_PATH = os.path.join(d, "start-eq.json")
        try:
            def fresh(adjust=True):
                return types.SimpleNamespace(api=types.SimpleNamespace(get=get), ok=lambda r: isinstance(r, dict) and r.get("code") == 0, session_eq=100.0, start_eq=100.0,
                                             transfer_adjust=adjust)
            t0 = _t.time()
            f = fresh()
            pt.Pulse.check_transfer(f, 100.0, t0)                                           # the reference
            income["rows"] = [{"incomeType": "REALIZED_PNL", "income": "0.2", "time": str(int((t0 + 19) * 1000))}]
            small = pt.Pulse.check_transfer(f, 100.2, t0 + 20)                               # PnL-sized drift, explained by the feed
            income["rows"] = []
            income["rows"] = [{"incomeType": "REALIZED_PNL", "income": "0.2", "time": str(int((t0 + 19) * 1000))}]
            first = pt.Pulse.check_transfer(f, 200.2, t0 + 40)                               # +100 not in the feed: first sighting
            dep = pt.Pulse.check_transfer(f, 200.2, t0 + 60)                                 # confirmed
            out["deposit"] = (small, first, dep, round(f.session_eq, 4), round(f.start_eq, 4), len(f.transfers), json.load(open(pt.START_EQ_PATH))["startEquity"])
            pt.Pulse.check_transfer(f, 100.2, t0 + 80)
            wd = pt.Pulse.check_transfer(f, 100.2, t0 + 100)
            out["withdrawal"] = (wd, round(f.session_eq, 4), round(f.start_eq, 4))
            # PnL, fees and funding explain a fall: no transfer
            g = fresh()
            pt.Pulse.check_transfer(g, 100.0, t0)
            now_ms = (t0 + 30) * 1000.0
            income["rows"] = [{"incomeType": "REALIZED_PNL", "income": "-3.5", "time": str(int(now_ms - 5000))}, {"incomeType": "TRADING_FEE", "income": "-0.5", "time": str(int(now_ms - 4000))}]
            a1 = pt.Pulse.check_transfer(g, 96.0, t0 + 30)
            a2 = pt.Pulse.check_transfer(g, 96.0, t0 + 60)
            out["pnl"] = (a1, a2, g.session_eq, getattr(g, "transfers", []))
            # a lagging feed: the first read sees -1 of the -4, the second the whole
            h = fresh()
            pt.Pulse.check_transfer(h, 100.0, t0)
            income["rows"] = [{"incomeType": "REALIZED_PNL", "income": "-1.0", "time": str(int((t0 + 29) * 1000))}]
            l1 = pt.Pulse.check_transfer(h, 96.0, t0 + 30)
            income["rows"] = [{"incomeType": "REALIZED_PNL", "income": "-1.0", "time": str(int((t0 + 29) * 1000))}, {"incomeType": "REALIZED_PNL", "income": "-3.0", "time": str(int((t0 + 31) * 1000))}]
            l2 = pt.Pulse.check_transfer(h, 96.0, t0 + 60)
            out["lag"] = (l1, l2, h.session_eq, getattr(h, "transfers", []))
            # the income read fails / the setting is off: nothing moves
            income["fail"] = True
            k = fresh()
            pt.Pulse.check_transfer(k, 100.0, t0)
            k1 = pt.Pulse.check_transfer(k, 300.0, t0 + 30)
            k2 = pt.Pulse.check_transfer(k, 300.0, t0 + 60)
            income["fail"] = False
            income["rows"] = []
            z = fresh(adjust=False)
            pt.Pulse.check_transfer(z, 100.0, t0)
            z1 = pt.Pulse.check_transfer(z, 300.0, t0 + 30)
            z2 = pt.Pulse.check_transfer(z, 300.0, t0 + 60)
            out["unknown"] = (k1, k2, k.session_eq, z1, z2, z.session_eq)
        finally:
            pt.START_EQ_PATH = old
    # the drawdown in money after a deposit is unchanged: (session + 100) - (equity + 100) = session - equity
    dd_before = 100.0 - 80.0
    dd_after = (100.0 + 100.0) - (80.0 + 100.0)
    src = open(os.path.join(PULSE, "pulse_trader.py"), encoding="utf-8").read()
    wired = "self.check_transfer(float(getattr(self, \"_wallet\", 0.0) or 0.0), time.time())" in src
    dep, wd, pnl, lag, unk = out["deposit"], out["withdrawal"], out["pnl"], out["lag"], out["unknown"]
    ok = (dep[0] == 0.0 and dep[1] == 0.0 and abs(dep[2] - 100.0) < 1e-9 and abs(dep[3] - 200.0) < 1e-9 and abs(dep[4] - 200.0) < 1e-9 and dep[5] == 1 and abs(dep[6] - 200.0) < 1e-9
          and abs(wd[0] + 100.0) < 1e-9 and abs(wd[1] - 100.0) < 1e-9 and abs(wd[2] - 100.0) < 1e-9 and pnl[0] == 0.0 and pnl[1] == 0.0 and pnl[2] == 100.0 and not pnl[3]
          and lag[0] == 0.0 and lag[1] == 0.0 and lag[2] == 100.0 and not lag[3] and unk == (0.0, 0.0, 100.0, 0.0, 0.0, 100.0)
          and abs(dd_before - dd_after) < 1e-12 and wired)
    return ok, f"deposit {dep}; withdrawal {wd}; PnL-explained {pnl}; lagging feed {lag}; unknown / off {unk}; drawdown in money before {dd_before:.2f} after {dd_after:.2f}; wired into refresh_balance {wired}"


def restart_credits_the_downtime_so_positions_keep_running():
    """Owner 2026-11-04 ("All positions got closed ... keep pos running and continue on crash, reloads"): after an 8 hour outage the engine came back and its 6 hour time exit closed
    27 positions at once although it had not been watching them. The hold clock now excludes the time the engine was down: a restart credits the gap since the previous run's
    last sign of life to every open position (`hold_credit_s`, at most a day per restart and 72 h in all), the time exit and the scratch exit read `position_age`, and the
    credit is made before the restored book is reconciled."""
    import types, time as _t
    now = _t.time()
    pos = types.SimpleNamespace(opened_at=now - 8 * 3600.0, hold_credit_s=0.0)
    before = pt.position_age(pos, now)
    fake = types.SimpleNamespace(open={"A-USDT": pos, "B-USDT": types.SimpleNamespace(opened_at=now - 1 * 3600.0, hold_credit_s=0.0)},
                                 last_run={"previousEnd": now - 8 * 3600.0}, saved=[])
    fake.save_open_book = lambda: fake.saved.append(1) or True
    saved_dt = pt.DOWNTIME_PATH
    with tempfile.TemporaryDirectory() as td:          # the restart also records the outage interval: never in the live state directory
        pt.DOWNTIME_PATH = os.path.join(td, "downtime.json")
        try:
            total = pt.Pulse.credit_restart_downtime(fake)
        finally:
            pt.DOWNTIME_PATH = saved_dt
    after = pt.position_age(pos, now)
    hold_s = 6 * 3600.0
    caps = (pt.downtime_credit(now - 40 * 3600.0, now), pt.downtime_credit(now - 8 * 3600.0, now, already=70 * 3600.0), pt.downtime_credit(None, now), pt.downtime_credit(now + 5, now))
    src = open(os.path.join(PULSE, "pulse_trader.py"), encoding="utf-8").read()
    wired = ("age = position_age(pos, now)\n            if age >= MAX_HOLD_S" in src and "self.credit_restart_downtime()" in src
             and src.index("self.credit_restart_downtime()") < src.index("self.reconcile_restored_book()", src.index("self.credit_restart_downtime()") - 400))
    ok = (before >= hold_s and after < 60.0 and abs(pos.hold_credit_s - 8 * 3600.0) < 5.0 and fake.open["B-USDT"].hold_credit_s > 7 * 3600.0 and fake.saved
          and caps[0] == pt.DOWNTIME_CREDIT_MAX_S and abs(caps[1] - 2 * 3600.0) < 1.0 and caps[2] == 0.0 and caps[3] == 0.0 and wired and total > 0)
    return ok, f"age after an 8 h outage: {before / 3600:.2f} h before the credit, {after:.0f} s after (time exit at {hold_s / 3600:.0f} h no longer fires); credit caps {tuple(round(c / 3600, 2) for c in caps)} h; wired into manage and the restart path {wired}"


def closes_made_while_the_engine_was_down_are_recorded_but_not_scored():
    """Owner 2026-11-06 ("ensure correctness and correct PF eval"): a position the venue closed on its own target / stop while the engine was down never ran under its Set's rules (the
    Set's stop, trailing and time exit did not run), so its result is real cash but not a Set-lane result. The close found at restart is written to the trades file with the reason
    suffix "-downtime", counted apart (`downtimeCloses`), scored in no Set / lane / pack PF and does not ban the symbol; the readers of the trades file (`_exec_boot`,
    `seed_set_executions`, `strategy_closes`) skip such rows and every row whose close time lies inside a recorded outage (state file `downtime-<conn>.json`, written by the restart's
    `credit_restart_downtime`, only for gaps of two minutes or more); a close of a running engine counts as before."""
    import types, time as _t
    sid = "general:L:1m:sl1.00:st4"

    def one(downtime):
        def run():
            p, sym = _enter(_RefusingCloseVenue(executed="1", requested="1"))
            pos = p.open[sym]
            pos.set_id, pos.pack, pos.lane = sid, "general", "pos"
            pos.downtime_close = downtime
            p.close_pos(pos, 101.0, "tp", exchange=False)
            rows = [json.loads(x) for x in open(pt.TRADES_PATH)] if os.path.exists(pt.TRADES_PATH) else []
            return (dict(p.exec_scope), dict(p.exec_n), [r["reason"] for r in rows], sym in p.ignore_syms, sym in p.open)
        return _isolated(run)
    live, down = one(False), one(True)
    with tempfile.TemporaryDirectory() as td:
        saved = (pt.TRADES_PATH, pt.DOWNTIME_PATH)
        pt.TRADES_PATH, pt.DOWNTIME_PATH = os.path.join(td, "trades.jsonl"), os.path.join(td, "downtime.json")
        try:
            now = _t.time()
            short_ok = not pt.record_downtime(now - 60.0, now)            # a one-minute gap is a restart, not an outage
            kept = pt.record_downtime(now - 3 * 3600.0, now + 60.0)
            fake = types.SimpleNamespace(open={}, last_run={"previousEnd": now - 8 * 3600.0}, save_open_book=lambda: True)
            pt.Pulse.credit_restart_downtime(fake)
            iv = pt.downtime_intervals()
            rows = [
                {"t": now - 10 * 3600.0, "symbol": "A", "side": "LONG", "pnl": 0.1, "net_pnl_pct": 0.01, "reason": "tp", "set_id": sid, "pack": "general", "lane": "pos", "conn": pt.CONN_SHORT},
                {"t": now - 5 * 3600.0, "symbol": "B", "side": "LONG", "pnl": 0.1, "net_pnl_pct": 0.01, "reason": "tp", "set_id": sid, "pack": "general", "lane": "pos", "conn": pt.CONN_SHORT},      # inside the 3 h outage
                {"t": now - 1000.0, "symbol": "C", "side": "LONG", "pnl": 0.1, "net_pnl_pct": 0.01, "reason": "tp-downtime", "set_id": sid, "pack": "general", "lane": "pos", "conn": pt.CONN_SHORT},
                {"t": now - 900.0, "symbol": "D", "side": "LONG", "pnl": 0.1, "net_pnl_pct": 0.01, "reason": "tp-unverified-exit", "set_id": sid, "pack": "general", "lane": "pos", "conn": pt.CONN_SHORT},
                {"t": now + 200.0, "symbol": "E", "side": "LONG", "pnl": -0.1, "net_pnl_pct": -0.01, "reason": "sl", "set_id": sid, "pack": "general", "lane": "pos", "conn": pt.CONN_SHORT},
            ]
            with open(pt.TRADES_PATH, "w") as f:
                f.write("".join(json.dumps(r) + "\n" for r in rows))
            boot = pt.Pulse.__new__(pt.Pulse)
            boot._exec_boot()
            scope = boot.exec_scope.get("overall", {})
            tagged = [pt.not_scored_close(r, iv) for r in rows]
        finally:
            pt.TRADES_PATH, pt.DOWNTIME_PATH = saved
    src = open(os.path.join(PULSE, "pulse_trader.py"), encoding="utf-8").read()
    wired = src.index("pos.downtime_close = True") < src.index('self.close_from_venue(pos, "restart")', src.index("pos.downtime_close = True")) and "record_downtime(prev_end, now + 60.0)" in src
    ok = (live[0].get("overall", {}).get("orders") == 1 and live[2] == ["tp"] and not live[4]
          and not down[0] and down[1].get("downtime") == 1 and down[1].get("skipped") == 1 and down[2] == ["tp-downtime"] and not down[3] and not down[4]
          and short_ok and kept and len(iv) == 2 and tagged == [False, True, True, True, False]
          and scope.get("orders") == 2 and boot.exec_n.get("skipped") == 3 and boot.exec_n.get("downtime") == 2 and wired)
    return ok, (f"running close scored {live[0].get('overall', {}).get('orders')}x as {live[2]}; downtime close scored {len(down[0])} scopes, recorded as {down[2]}, symbol banned={down[3]}, "
                f"counted downtime={down[1].get('downtime')}; short gap recorded={not short_ok}, intervals {len(iv)}; boot reader scored {scope.get('orders')} of 5 rows, skipped {boot.exec_n.get('skipped')} "
                f"(downtime {boot.exec_n.get('downtime')}); wired {wired}")


def live_executed_pf_equals_the_plain_reading_of_the_trades_file():
    """Owner 2026-11-06 ("ensure correctness and correct PF eval"): the executed PF the engine publishes per scope (overall, lane, pack, Set:lane; `pf` on the net position return,
    `cashPf` in USDT) equals scripts/live_pf_audit.py's plain-loop recomputation from the trades file, on rows of every kind: a Set stop and target on both lanes, a Block-add close
    (cash covers the grown position, the return the base notional), a no-ctrl close (scored), an unverified exit, a downtime close, a row of another connection and one that is not ours
    (all skipped), and a row whose return is unknown (its scope reports pf None, never a partial sample)."""
    import importlib.util, time as _t
    from common import ROOT
    spec = importlib.util.spec_from_file_location("live_pf_audit", os.path.join(ROOT, "scripts", "live_pf_audit.py"))
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    now = _t.time()
    base = {"symbol": "A-USDT", "side": "LONG", "qty": 10.0, "entry": 1.0, "conn": pt.CONN_SHORT, "ours": True}
    def row(i, **kw):
        r = {**base, "t": now - 5000.0 + i, "client_id": "", "lane": "pos", "pack": "general", "set_id": "general:L:1m:sl1.00:st4", "reason": "tp", "pnl": 0.2, "net_pnl_pct": 0.02, "fee_source": "reported-fee"}
        r.update(kw)
        return r
    rows = [
        row(1, reason="sl", pnl=-0.35, net_pnl_pct=-0.035),
        row(2),
        row(3, lane="neg", pack="indications", set_id="indications:S:1m:sl1.00:st6", pnl=0.5, net_pnl_pct=0.05),
        row(4, lane="neg", pack="indications", set_id="indications:S:1m:sl1.00:st6", reason="sl", pnl=-0.4, net_pnl_pct=-0.04),
        row(5, qty=30.0, base_qty=10.0, pnl=0.9, net_pnl_pct=0.03, block_fills=[[1.01, 10.0], [1.02, 10.0]]),          # Block adds: cash on 30 units, the return on the base notional
        row(6, reason="no-ctrl", pnl=-0.01, net_pnl_pct=-0.001),
        row(7, reason="tp-unverified-exit", pnl=0.3, net_pnl_pct=0.03),
        row(8, reason="tp-downtime", pnl=0.4, net_pnl_pct=0.04),
        row(9, conn="bingx-other", pnl=0.7, net_pnl_pct=0.07),
        row(10, ours=False, pnl=0.8, net_pnl_pct=0.08),
        row(11, pack="trend", set_id="trend:L:1m:sl1.00:st5", pnl=0.1, net_pnl_pct=None),                                # unknown return: pf None for this Set's scopes
        row(12, set_id="", pack="", pnl=0.6, net_pnl_pct=0.06),                                                          # no Set: not a Set-lane close
    ]
    def run():
        p, _sym = _enter(_RefusingCloseVenue(executed="1", requested="1"))
        with open(pt.TRADES_PATH, "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
        p._exec_boot()
        scopes = p.execution_snapshot()["overall"]["scopes"]
        return {k: v.get("executed", {}) for k, v in scopes.items()}
    saved = pt.DOWNTIME_PATH
    with tempfile.TemporaryDirectory() as td:
        pt.DOWNTIME_PATH = os.path.join(td, "downtime.json")
        try:
            executed = _isolated(run)
        finally:
            pt.DOWNTIME_PATH = saved
    plain = audit.recompute(rows, pt.CONN_SHORT, None, [])
    bad = audit.diff(plain, {k: {"executed": v} for k, v in executed.items()})
    ov, neg, pos, trend = audit.figures(plain["overall"]), audit.figures(plain["lane:neg"]), audit.figures(plain["lane:pos"]), audit.figures(plain["pack:trend"])
    # by hand: scored rows 1-6 and 11 (7 closes on the Set lane); gains 0.02 + 0.05 + 0.03, losses 0.035 + 0.04 + 0.001 on returns
    want_pf = round((0.02 + 0.05 + 0.03) / (0.035 + 0.04 + 0.001), 3)
    ok = (not bad and ov["orders"] == 7 and ov["pf"] is None and trend["pf"] is None and plain["lane:neg"]["orders"] == 2 and neg["pf"] == round(0.05 / 0.04, 3)
          and pos["orders"] == 5 and executed["overall"]["orders"] == 7 and executed["pack:trend"]["pf"] is None and want_pf > 0)
    return ok, (f"{len(rows)} rows, scored {ov['orders']} Set-lane closes, differences {bad[:3]}; overall pf {ov['pf']} (unknown return in one row -> None), neg lane pf {neg['pf']} / cash {neg['cashPf']}, "
                f"engine {executed.get('lane:neg', {}).get('pf')} / {executed.get('lane:neg', {}).get('cashPf')}")


def the_group_gate_blocks_a_losing_group_and_only_that_group():
    """Owner 2026-11-06 ("make only profitable ones run, but also Signals"): with groupGate on, a group (pack x kind x TP range x lane, `set_group`) whose newest `groupGateN`
    executed results inside the last `groupGateHours` hours have a PF under `groupGatePf` opens no position; the other groups of the same pack keep opening, a never-traded
    group runs, the evidence ages out (the group re-tests itself), the gate is off by default, and the block is reported (`scopeBlocked`, stats `groups`). Checked through
    the real selection (`_scope_choice`) with the Axis on and with it off."""
    from coord_engine import Coordinator
    from set_engine import SetBook, group_verdict, set_group
    from position_cost import tp_range_of
    import time as _t
    sid_group = lambda st, lane="pos": set_group(st.id, lane)
    def build(gate, axes):
        book = SetBook()
        book.load({"stagesEnabled": False, "histLookbackAuto": False, "setGateMinTrades": 0, "minPfBySize": False, "minPfByStep": False, "directionMode": "both", "pauseMode": "off",
                   "setMinPf": 1.1, "setMinStep": 2, "setStepMax": 5, "setSlRatios": [0.5], "trailVariants": ["0.3:0.1"], "stratGeneral": True, "stratIndications": False,
                   "groupGate": gate, "groupGateN": 5, "groupGatePf": 1.0, "groupGateHours": 12})
        rows = [st for st in book.by_idx if st.pack == "general" and st.kind == "base"]
        micro = next(st for st in rows if tp_range_of(st.step) == "micro")
        minimal = next(st for st in rows if tp_range_of(st.step) == "minimal")
        book.by_idx = [micro, minimal]
        book.sets = {st.id: st for st in book.by_idx}
        now = _t.time()
        for st, score in ((micro, 2.0), (minimal, 1.5)):             # the micro group is the better-ranked Set
            st.active, st.locked, st.last_error = True, False, ""
            st.gate_n, st.gate_pf, st.n = 20, score, 20
            st.max_dd_s, st.score_at = 0, now
            st.exe = [(now - 60 * i, 0.004) for i in range(4, 0, -1)]   # the paper evidence is fine for both: only the executed group results differ
        coord = Coordinator()
        coord.load({}, {"axisLastEnabled": axes, "axisPrevEnabled": False, "axisPauseEnabled": False, "axisContEnabled": False, "minPfLinked": False, "minPf": 1.1})
        p = _bare_pulse()
        p.sets, p.coord, p.open, p.position_cost_pct = book, coord, {}, 0.12
        return p, micro, minimal, now
    def close(p, st, net, t_):
        p._exec_note(pt.Closed(t_, "A-USDT", "LONG", 1.0, 1.0, 1.0, net, 0.0, "sl" if net < 0 else "tp", 60.0, set_id=st.id, pack="general", lane="pos", net_pnl_pct=net))
    out = {}
    for axes in (True, False):
        p, micro, minimal, now = build(True, axes)
        first = p._scope_choice("general")[0]                           # no executed result yet: the best-ranked Set (micro group) runs
        for i in range(3):
            close(p, micro, -0.02, now - 600 + i)                       # the micro group loses three times in a row
        after_loss = p._scope_choice("general")
        for i in range(3):
            close(p, minimal, -0.02, now - 500 + i)                     # now the minimal group loses too
        both_lost = p._scope_choice("general")
        close(p, micro, 0.09, now - 100)                                 # one big win lifts the micro group: gains 0.09 against losses 0.06
        recovered = p._scope_choice("general")
        out[axes] = (getattr(first, "id", None) == micro.id, getattr(after_loss[0], "id", None) == minimal.id, both_lost[0] is None and both_lost[2].startswith("group:"),
                     getattr(recovered[0], "id", None) == micro.id)
    p, micro, minimal, now = build(True, True)
    for i in range(3):
        close(p, micro, -0.02, now - 20 * 3600 + i)                      # losses older than groupGateHours
    aged = p._scope_choice("general")[0]
    off, o_micro, _m, o_now = build(False, True)
    for i in range(3):
        close(off, o_micro, -0.02, o_now - 600 + i)
    gate_off = off._scope_choice("general")[0]
    snap_groups = p.execution_snapshot() if hasattr(p, "execution_snapshot") else {}
    pure = (group_verdict([], 100.0)[0] and group_verdict([(99.0, 0.05)], 100.0)[0] and not group_verdict([(99.0, -0.01), (98.0, 0.005)], 100.0, 10, 1.0, 12)[0]
            and group_verdict([(99.0, -0.01), (98.0, 0.011)], 100.0)[0] and group_verdict([(1.0, -0.5)], 100000.0)[0])
    ids = (set_group("indications:L:1m:sl0.50:st21", "pos"), set_group("trend:S:1m:sl1.00:st5:xt", "neg"), set_group("general:S:1m:tr0.70:0.60", "pos"))
    ok = (all(out[a] == (True, True, True, True) for a in (True, False)) and getattr(aged, "id", None) == micro.id and getattr(gate_off, "id", None) == o_micro.id and pure
          and ids == ("group:indications:base:long:pos", "group:trend:exit:minimal:neg", "group:general:trail:short:pos") and SetBook().group_gate is False)
    return ok, (f"axis on {out[True]}, axis off {out[False]} (first runs, loser blocked -> the other group, both blocked -> none by group key, one win -> back); aged evidence reopens="
                f"{getattr(aged, 'id', None) == micro.id}; gate off ignores losses={getattr(gate_off, 'id', None) == o_micro.id}; keys {ids}")


def alternate_lane_keeps_the_executed_group_gate():
    """Changing the approved lane cannot bypass the losing execution group's gate."""
    from set_engine import set_group
    now = pt.time.time()
    rows = [types.SimpleNamespace(id="general:L:1m:sl0.50:st2", lane="pos"),
            types.SimpleNamespace(id="general:L:1m:sl0.50:st2", lane="neg"),
            types.SimpleNamespace(id="general:L:1m:sl0.50:st5", lane="neg")]
    def pick(_pack, side=0, allowed_lanes=None, predicate=None):
        return next((st for st in rows if (allowed_lanes is None or st.lane in allowed_lanes)
                     and (predicate is None or predicate(st))), None)
    p = _bare_pulse()
    p.sets = types.SimpleNamespace(pick_any=pick, lane_of=lambda st: st.lane,
                                  group_gate=True, group_gate_n=5, group_gate_pf=1.05,
                                  group_gate_hours=12, group_gate_min=1)
    p.coord = types.SimpleNamespace(scope_allows=lambda pack, lane: (lane == "neg", "positive-scope-blocked"))
    p.group_exec = {set_group(rows[1].id, "neg"): [(now - 1, -0.01)]}
    allowed = p._scope_choice("general", 1)
    p.group_exec[set_group(rows[2].id, "neg")] = [(now - 1, -0.01)]
    denied = p._scope_choice("general", 1)
    p.sets.group_gate = False
    disabled = p._scope_choice("general", 1)
    ok = (allowed[0] is rows[2] and allowed[1] == "neg" and denied[0] is None
          and bool(denied[2]) and disabled[0] is rows[1])
    return ok, f"healthy alternative={allowed[0] is rows[2]}, all losing denied={denied[0] is None}, gate off={disabled[0] is rows[1]}"


CHECKS = [
    ("trader.alternate-lane-keeps-the-executed-group-gate", alternate_lane_keeps_the_executed_group_gate),
    ("trader.restart-credits-the-downtime-so-positions-keep-running", restart_credits_the_downtime_so_positions_keep_running),
    ("trader.closes-made-while-the-engine-was-down-are-recorded-but-not-scored", closes_made_while_the_engine_was_down_are_recorded_but_not_scored),
    ("trader.live-executed-pf-equals-the-plain-reading-of-the-trades-file", live_executed_pf_equals_the_plain_reading_of_the_trades_file),
    ("trader.group-gate-blocks-a-losing-group-and-only-that-group", the_group_gate_blocks_a_losing_group_and_only_that_group),
    ("trader.transfers-move-the-drawdown-basis-not-the-drawdown", transfers_move_the_drawdown_basis_not_the_drawdown),
    ("trader.a-manual-close-rests-only-that-symbol-and-direction", a_manual_close_rests_only_that_symbol_and_direction),
    ("trader.a-venue-restricted-symbol-rests-thirty-minutes-after-a-refused-entry", a_venue_restricted_symbol_rests_thirty_minutes_after_a_refused_entry),
    ("trader.volume-factor-is-bounded-and-defaults-to-the-minimum", volume_factor_is_bounded_and_defaults_to_the_minimum),
    ("trader.strongest-ready-symbol-can-take-first-slot", strongest_ready_symbol_can_take_the_first_slot),
    ("trader.thirty-ready-symbols-compete-in-one-bounded-batch", thirty_ready_symbols_are_ranked_in_one_bounded_signal_batch),
    ("trader.strict-size-budget", strict_size_budget_skips_oversized_venue_minimum),
    ("trader.minimum-lot-roundtrip", size_budget_preserves_exact_venue_minimum),
    ("trader.strict-entry-budget-survives-multipliers-and-carry", strict_entry_budget_survives_set_multipliers_and_block_carry),
    ("trader.minimum-retry-respects-budget-and-client-id", minimum_order_retry_respects_budget_and_reconciles_its_own_id),
    ("trader.leverage-retry-respects-margin-and-client-id", leverage_retry_checks_margin_and_reconciles_its_own_id),
    ("trader.no-untagged-order-when-set-disappears", entry_does_not_open_when_the_set_disappears_after_sense),
    ("trader.entry-selection-keeps-packs-and-axis-lanes-independent", entry_selection_keeps_packs_and_axis_lanes_independent),
    ("trader.one-dynamic-set-axis-does-not-block-other-sets", one_dynamic_set_axis_does_not_block_other_sets),
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
    ("trader.extra-cids-never-guess-owner", extra_set_client_ids_never_guess_the_owner),
    ("trader.signal-needs-its-own-pack", a_signal_needs_its_own_pack_to_hold_a_valid_set),
    ("trader.venue-order-of-another-symbol-is-not-the-exit", a_venue_order_of_another_symbol_is_not_this_exit),
    ("trader.manager-close-not-counted-twice", a_manager_close_is_not_counted_again_by_the_fill_sync),
    ("trader.set-fill-keeps-its-sl-and-tp", a_set_fill_keeps_the_sets_own_sl_and_tp),
    ("trader.slpct-tppct-are-percent", overlay_slpct_and_tppct_are_percent_without_guessing),
    ("trader.history-fetched-in-pages", history_is_fetched_in_pages_within_the_venue_limit),
    ("trader.history-overlap-counts-distinct-minutes", history_page_overlap_cannot_shorten_stage_context),
    ("trader.history-timeout-retries-only-uncertain-reads", history_retries_only_uncertain_reads_without_resending_successes),
    ("trader.history-growth-fetches-three-days", grown_history_fetches_the_full_target_up_to_three_days),
    ("trader.history-growth-retries-failed-pages", history_growth_retries_failed_or_missing_pages),
    ("trader.short-history-backfill-bounded", short_history_backfill_is_temporarily_bounded),
    ("trader.short-history-cache-rejects-bad-pages", short_history_cache_rejects_failed_or_invalid_pages),
    ("trader.flat-candle-gap-invalidates-short-cache", flat_candle_gap_cannot_preserve_short_history_cache),
    ("trader.fifty-short-histories-bound-refresh", fifty_short_histories_refresh_without_full_repaging),
    ("trader.history-chunks-rotate-fairly", history_chunks_rotate_even_when_many_positions_are_open),
    ("trader.load-governor-keeps-the-set-replay-running-at-the-books-footprint", load_governor_keeps_the_set_replay_running_at_the_books_footprint),
    ("trader.thin-book-entry-is-never-sent", thin_book_entry_is_never_sent),
    ("trader.leverage-keeps-liquidation-beyond-the-stop", leverage_keeps_liquidation_beyond_the_stop),
    ("trader.control-already-exists-never-loops", control_already_exists_never_loops),
    ("trader.cancellation-requires-recorded-order-id", cancellation_requires_a_recorded_order_id),
    ("trader.leverage-is-always-the-exchange-maximum-by-default", leverage_is_always_the_exchange_maximum_by_default),
    ("trader.a-moved-stop-never-storms-the-order-path", a_moved_stop_never_storms_the_order_path),
    ("trader.an-unsupported-cancel-replace-is-asked-once", an_unsupported_cancel_replace_is_asked_once),
    ("trader.leverage-maximum-follows-the-venue-dynamically", leverage_maximum_follows_the_venue_dynamically),
    ("trader.unanswered-close-that-never-existed-is-sent-again", unanswered_close_that_never_existed_is_sent_again),
    ("trader.set-volume-multiples-size-by-pack-type-and-range", set_volume_multiples_size_by_pack_type_and_range),
    ("trader.indications-trailing-volume-is-not-multiplied", indications_trailing_volume_is_not_multiplied),
    ("trader.relation-lanes-raise-a-set-entry-and-never-cancel-it", relation_lanes_raise_a_set_entry_and_never_cancel_it),
    ("trader.block-rounds-raise-the-group-after-a-losing-block-until-it-is-recovered", block_rounds_raise_the_group_after_a_losing_block_until_it_is_recovered),
    ("trader.lift-figures-compare-the-same-parents-with-and-without", lift_figures_compare_the_same_parents_with_and_without),
    ("trader.start-records-whether-the-previous-run-crashed", start_records_whether_the_previous_run_crashed),
    ("trader.failed-close-keeps-position-protected", failed_close_keeps_the_position_protected),
    ("trader.partial-close-recorded-at-full-size", partial_close_is_recorded_at_the_full_size),
    ("trader.lost-entry-answer-remembered", lost_entry_answer_is_remembered_for_adoption),
    ("trader.symbol-cap-applies-under-a-wildcard", symbol_cap_applies_under_a_wildcard),
    ("trader.x01-bootstrap-honors-cap-before-ranked-tickers", x01_bootstrap_honors_cap_before_ranked_tickers),
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
    ("trader.padding-is-not-readiness", padding_does_not_count_as_readiness),
    ("trader.only-main-loop-pings-watchdog", only_the_main_loop_pings_the_watchdog),
]
