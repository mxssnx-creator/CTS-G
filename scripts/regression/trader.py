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
    only the fallback when the venue reports no fill yet."""
    cases = [
        ({"executedQty": "0.4", "quantity": "1"}, 1.0, 0.4),
        ({"quantity": "1"}, 2.0, 1.0),
        ({"executedQty": "0", "origQty": "5"}, 9.0, 5.0),
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
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings)
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
            p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
            pos = p.open.get(sym)
            orders = [b for path, b in api.posts if path.endswith("/trade/order")]
            ok = (len(orders) == 1 and pos is not None and abs(float(pos.qty) - 0.4) < 1e-12
                  and len(api.batches) == 1 and len(api.batches[0]) == 2)
        finally:
            pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings = saved
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
            return {"code": 0, "data": {"order": {"orderId": "c2", "avgPrice": "102"}}}
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
    p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
    return p, sym


def partial_close_fill_closes_the_remainder():
    """A close that fills only part of its quantity must not be reported as a full close. The remainder is closed with
    the closePosition form, the returned price blends the two fills, and if the remainder is refused the position stays
    open at the size that is left."""
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings)
    with tempfile.TemporaryDirectory() as td:
        pt.BLOCK_PATH = os.path.join(td, "block.json")
        pt.LEV_PATH = os.path.join(td, "lev.json")
        pt.OPEN_PATH = os.path.join(td, "open.json")
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
            p2, sym2 = _enter(venue2)
            pos2 = p2.open[sym2]
            q2 = float(pos2.qty)
            ok2, _ = p2.market_close(pos2)
            kept = abs(float(pos2.qty) - q2 * 0.6) < 1e-9
        finally:
            pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings = saved
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


CHECKS = [
    ("trader.symbol-cap-applies-under-a-wildcard", symbol_cap_applies_under_a_wildcard),
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
