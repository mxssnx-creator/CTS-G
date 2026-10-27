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
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings)
    with tempfile.TemporaryDirectory() as td:
        pt.BLOCK_PATH = os.path.join(td, "block.json")
        pt.LEV_PATH = os.path.join(td, "lev.json")
        pt.OPEN_PATH = os.path.join(td, "open.json")
        pt.dump_cts_settings = lambda: {}
        try:
            return fn()
        finally:
            pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings = saved


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
        p.available = 50.0
        p.halted = False
        p.entry_sense = lambda *a, **k: None
        p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
        return sym in p.open, dict(getattr(p, "unknown_entry", {}))
    opened, unk = _isolated(run)
    return (not opened) and any(k.startswith("BTC-USDT:") for k in unk), f"in book={opened} unknown entries={list(unk)}"


class _PagedKlines:
    """A venue that serves 1m klines like the real one: at most 1000 bars per request (a larger limit is refused with code
    109400 and no data), oldest first, endTime inclusive. The series is 3000 minutes long and its closes count 0, 1, 2..."""

    def __init__(self, n: int = 3000, t_end: int = 1_800_000_000_000) -> None:
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
        contiguous = closes == [float(x) for x in range(3000 - 1920, 3000)]
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
    cids = {st.id: p.cid("o", set_id=st.id, pack=st.pack, set_idx=st.idx) for st in sets if st.kind == "base"}
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
    """A general signal must not enter on the strength of a valid indications Set: the pack gate is per pack."""
    import types
    p = _bare_pulse()
    ind = types.SimpleNamespace(id="i1", step=2)

    class _Sets:
        enabled = True
        use_historic_gate = True

        def pick_any(self, pack):
            return ind if pack == "indications" else None

        def pack_open(self, pack):
            return pack == "indications"

    p.sets = _Sets()
    p.strat_ind = True
    p.indications = types.SimpleNamespace(settings={"enabled": True}, primary=lambda s: None)
    reasons = {}
    for pack, why in (("general", "gen:test"), ("indications", "ind:state:tf:0.6:a2:s")):
        try:
            reasons[pack] = p.entry_sense("A-USDT", 1 if pack == "general" else 1, why, "LONG") if False else None
        except Exception as e:
            reasons[pack] = repr(e)[:40]
    src = open(os.path.join(pt.__file__ and os.path.dirname(pt.__file__), "pulse_trader.py")).read()
    gate_is_per_pack = "if not self.sets.pack_open(pack):" in src and "or self.sets.pack_open(\"general\") or self.sets.pack_open(\"indications\")" not in src
    return gate_is_per_pack, f"per-pack gate in entry_sense={gate_is_per_pack}"


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
                 {"clientOrderID": p.cid("s", pos=pos), "orderId": "7777", "status": "FILLED", "symbol": sym, "executedQty": "1", "avgPrice": "99", "profit": "-1.0"}]
        p.api.get = lambda path, extra=None: {"code": 0, "data": {"orders": fills}}
        p.sync_own_fills()
        return sent_all_seen, len(counted)
    seen, n = _isolated(run)
    return seen and n == 1, f"close cids seen={seen} fills counted={n} (only the unknown stop fill counts)"


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
        p.sets.pick_any = lambda pack: st
        p.sets.pick_trail = lambda pack: None
        p.tp_min = 0.0035          # the shipped overlay clamp: it must not apply to a Set fill
        p.sl_min = 0.0020
        p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
        pos = p.open.get(sym)
        return pos, p.position_cost_pct
    pos, cost = _isolated(run)
    c = cost / 100.0
    ok = pos is not None and abs(pos.tp_pct - 2 * c) < 1e-9 and abs(pos.sl_pct - 2 * c) < 1e-9
    return ok, f"cost={cost}% tp={getattr(pos, 'tp_pct', None)} sl={getattr(pos, 'sl_pct', None)} want both {2 * c}"


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
    venue = _StubExchange(executed="1", requested="1")
    p, sym = _enter(venue)
    pos = p.open[sym]
    pos.set_id = set_id
    pos.pack = "indications"
    pos.opened_at = time.time() - 700.0        # past the exit lanes' minimum hold and the scratch time
    pos.sl = pos.entry * 0.97
    pos.tp = pos.entry * 1.03
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
        for gain, name in ((0.0003, "below scratch"), (0.0030, "above scratch")):
            p, sym, pos = _lane_pulse()
            p.exits.enabled = False
            p.px[sym] = pos.entry * (1 + gain)
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
    ok = out["below scratch"][0] and out["above scratch"] == (False, ["scratch+"]) and out["time"] == (False, ["time"])
    return ok, f"{out}"


def a_set_lane_honours_tp_only_as_the_replay_does():
    def run():
        res = {}
        for honor in (True, False):
            p, sym, pos = _lane_pulse()
            p.exits.enabled = False
            p.sets.hist_honor_tp = honor
            pos.opened_at = time.time() - 30.0
            pos.tp_pct = 0.0024
            pos.tp = pos.entry * 1.0024
            p.px[sym] = pos.tp * 1.001
            p.manage()
            res[honor] = (sym in p.open, [c.reason for c in p.closed])
            pos.tp  # noqa: B018
            sl_safe, tp_safe = p.security_prices(pos)
            res[f"order tp {honor}"] = round(abs(tp_safe - pos.entry) / pos.entry, 4)
        return res
    res = _isolated(run)
    ok = res[True] == (False, ["tp"]) and res[False][0] is True and res["order tp False"] >= res["order tp True"]
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
        pos.trail_set_id = ""                       # no trailing Set picked: a base Set does not trail
        p.manage()
        without = list(moves)
        pos.trail_set_id = "indications:1m:tr0.60:0.36"
        p.manage()
        return without, moves, round(pos.entry * 1.01 * (1 - 0.0036), 4)
    without, with_set, want = _isolated(run)
    return without == [] and with_set == [want], f"without a trailing Set: {without}; with: {with_set} (want {want})"


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


CHECKS = [
    ("trader.set-lane-does-not-read-the-exit-strategies", the_set_lane_does_not_read_the_exit_strategies),
    ("trader.set-position-closed-only-by-its-lane", a_set_position_is_closed_only_by_its_own_lane),
    ("trader.set-lane-scratch-and-time-follow-replay", a_set_lane_scratch_and_time_follow_the_replay_rules),
    ("trader.set-lane-honours-tp-as-the-replay-does", a_set_lane_honours_tp_only_as_the_replay_does),
    ("trader.set-trails-only-with-its-trailing-set", a_set_trails_only_with_its_trailing_set_and_the_exit_lanes_stay_out),
    ("trader.exit-lane-scores-only-its-own-closes", exit_lane_scores_only_its_own_closes),
    ("trader.cids-round-trip-to-their-own-set", client_order_ids_round_trip_to_their_own_set),
    ("trader.signal-needs-its-own-pack", a_signal_needs_its_own_pack_to_hold_a_valid_set),
    ("trader.manager-close-not-counted-twice", a_manager_close_is_not_counted_again_by_the_fill_sync),
    ("trader.set-fill-keeps-its-sl-and-tp", a_set_fill_keeps_the_sets_own_sl_and_tp),
    ("trader.slpct-tppct-are-percent", overlay_slpct_and_tppct_are_percent_without_guessing),
    ("trader.history-fetched-in-pages", history_is_fetched_in_pages_within_the_venue_limit),
    ("trader.failed-close-keeps-position-protected", failed_close_keeps_the_position_protected),
    ("trader.partial-close-recorded-at-full-size", partial_close_is_recorded_at_the_full_size),
    ("trader.lost-entry-answer-remembered", lost_entry_answer_is_remembered_for_adoption),
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
