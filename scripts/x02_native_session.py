#!/usr/bin/env python3
"""Bounded native Pulse VST session. Plan-only unless --execute is supplied.

The native strategy, PF gates, lifecycle and close accounting are unchanged.
This wrapper adds private state, a unique ownership tag, a mutation journal,
warmup/readiness admission lock, observation deadline and owned cleanup.
It never fabricates entries or treats geometry probes as strategy executions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
HOST = "https://open-api-vst.bingx.com"
CONN = "bingx-x02"
TERMINAL = {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}


class SessionEnd(BaseException):
    pass


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as stream:
        json.dump(value, stream, separators=(",", ":"), allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


def frozen_overlay(raw, symbols, history_hours=18):
    if len(symbols) != 50 or len(set(symbols)) != 50 or any(
        not isinstance(s, str) or not s.endswith("-USDT") or not s.replace("-", "").isalnum() for s in symbols
    ):
        raise ValueError("exactly 50 distinct explicit USDT symbols required")
    if not 1 <= history_hours <= 72:
        raise ValueError("history hours must be between 1 and 72")
    ov = json.loads(json.dumps(raw))
    ranges = {"micro": True, "minimal": True, "short": True, "normal": False, "long": False, "wide": False}
    ov.update(symbols=symbols, symbolsAll=False, symbolsDynamic=False, symbolCap=50,
              volumeFactor=.05, sizePctOfBalance=0, targetNotional=2.15, sizeMaxNotional=25,
              maxOpen=50, setMinStep=2, setStepMax=7, setStepAdapt=False,
              shortRangeStepMin=2, shortRangeStepMax=7, tpRanges=ranges, trailRanges=ranges,
              minSlDistancePct=.1, slippageFeeRatio=.1, histLookbackBars=int(history_hours * 60),
              histLookbackMax=int(history_hours * 60), histLookbackAuto=False, histKeepLastComplete=True,
              indExtraSources=False, ownedQuantityControls=True)
    # Preserve every configured PF/sample/Last-N policy and strategy toggle.
    return ov


def relocate_runtime(module, state, tag):
    """Relocate every native state global before constructing Pulse."""
    old = module.DIR
    for key, value in list(vars(module).items()):
        if isinstance(value, str) and (value == old or value.startswith(old + "/")):
            setattr(module, key, str(state) + value[len(old):])
    module.TAG = tag
    # This isolated session is configured only by its frozen overlay.
    module.dump_cts_settings = lambda: {}
    module.redis_hget = lambda _field: ""


def order_bodies(path, body):
    if path.endswith("/batchOrders"):
        return json.loads(body.get("batchOrders") or "[]")
    if path.endswith("/order") or path.endswith("/cancelReplace"):
        return [body]
    return []


def is_entry(body):
    return (body.get("positionSide"), body.get("side")) in (("LONG", "BUY"), ("SHORT", "SELL"))


class Journal:
    def __init__(self, api, out, tag):
        self.api, self.out, self.tag = api, Path(out), tag
        self.phase, self.deadline = "warming", None
        self.intents = []
        self.owned_clients = set()
        self.owned_orders = set()
        self.lock = threading.RLock()
        self.original = api._req
        api._req = self.request

    def append(self, row):
        with (self.out / "mutation-journal.jsonl").open("a") as stream:
            stream.write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def owns_order(self, symbol, order_id):
        with self.lock:
            return (str(symbol), str(order_id)) in self.owned_orders

    def owns_client(self, symbol, client_id):
        with self.lock:
            return (str(symbol), str(client_id).lower()) in self.owned_clients

    def remember_order(self, order, requested=None):
        """Bind a venue ID only to an exact journaled client ID and symbol."""
        if not isinstance(order, dict):
            return
        requested = requested or {}
        symbol = str(order.get('symbol') or requested.get('symbol') or '')
        cid = str(order.get('clientOrderID') or order.get('clientOrderId') or '')
        requested_cid = str(requested.get('clientOrderID') or requested.get('clientOrderId') or '')
        if cid and requested_cid and cid.lower() != requested_cid.lower():
            return
        cid = cid or requested_cid
        oid = str(order.get('orderId') or order.get('orderID') or '')
        if symbol and cid and oid.isdigit() and int(oid) > 0 and (symbol, cid.lower()) in self.owned_clients:
            self.owned_orders.add((symbol, oid))

    def observe_orders(self, path, extra, reply):
        if not isinstance(reply, dict) or reply.get('error') or str(reply.get('code')) != '0':
            return
        data = reply.get('data')
        if path.endswith('/trade/order') and isinstance(data, dict):
            order = data.get('order', data)
            self.remember_order(order, extra)
        elif path.endswith('/trade/openOrders'):
            orders = data.get('orders') if isinstance(data, dict) else data
            if isinstance(orders, list):
                for order in orders:
                    self.remember_order(order)

    def request(self, method, path, extra=None):
        if self.api.base.rstrip('/') != HOST or not path.startswith('/openApi/') or '://' in path:
            raise RuntimeError('bounded session permits only the fixed BingX VST transport')
        if method == "GET":
            reply = self.original(method, path, extra)
            with self.lock:
                self.observe_orders(path, extra or {}, reply)
            return reply
        body = dict(extra or {})
        if method == 'DELETE':
            if path != '/openApi/swap/v2/trade/order' or not self.owns_order(body.get('symbol'), body.get('orderId')):
                raise RuntimeError('cancellation requires this session\'s exact journaled venue order ID')
        elif method != 'POST' or path not in ('/openApi/swap/v2/trade/order', '/openApi/swap/v2/trade/batchOrders'):
            raise RuntimeError('session forbids account-setting and unrecognized mutations')
        bodies = order_bodies(path, body) if method == "POST" else []
        if method == 'POST' and not bodies:
            raise RuntimeError('an identified session order is required')
        for order in bodies:
            cid = str(order.get("clientOrderID") or order.get("clientOrderId") or "")
            if not order.get('symbol') or not cid.lower().startswith(self.tag.lower()):
                raise RuntimeError("native order is missing this session's ownership tag")
            if str(order.get('closePosition', '')).lower() in ('true', '1') or order.get('positionId'):
                raise RuntimeError('session forbids aggregate position-closing orders')
            if is_entry(order) and (self.phase != "active" or self.deadline is None or time.monotonic() >= self.deadline):
                return {"code": 101209, "error": True, "cooled": True, "msg": "session admission paused or deadline reached"}
        with self.lock:
            identities = [(str(order['symbol']), str(order.get('clientOrderID') or order.get('clientOrderId')).lower()) for order in bodies]
            if len(set(identities)) != len(identities) or any(identity in self.owned_clients for identity in identities):
                raise RuntimeError('client order ID was already journaled; reconcile it without resubmission')
            index = len(self.intents)
            row = {"index": index, "at": time.time(), "phase": self.phase, "method": method,
                   "path": path, "body": body, "result": "pending"}
            self.intents.append(row)
            self.append(row)
            self.owned_clients.update(identities)
            try:
                reply = self.original(method, path, body)
            except BaseException as exc:
                self.append({"index": index, "at": time.time(), "result": "unknown", "errorType": type(exc).__name__})
                raise
            row["response"] = reply
            self.append({"index": index, "at": time.time(), "result": "response", "response": reply})
            if method == 'POST' and len(bodies) == 1:
                self.observe_orders('/openApi/swap/v2/trade/order', bodies[0], reply)
            elif method == 'POST' and isinstance(reply, dict) and str(reply.get('code')) == '0':
                data = reply.get('data')
                orders = data.get('orders') if isinstance(data, dict) else data
                if isinstance(orders, list):
                    for order in orders:
                        self.remember_order(order)
            return reply


def rows(api, path, key=None):
    reply = api.get(path)
    if not isinstance(reply, dict) or reply.get("error") or str(reply.get("code")) != "0":
        raise RuntimeError("verified venue read unavailable: " + path)
    data = reply.get("data")
    if key and isinstance(data, dict):
        data = data.get(key)
    if not isinstance(data, list):
        raise RuntimeError("venue list shape unavailable: " + path)
    return data


def checked_positions(api):
    result = rows(api, "/openApi/swap/v2/user/positions")
    for row in result:
        qty = float(row.get("positionAmt", "nan"))
        if not math.isfinite(qty) or not row.get("symbol") or row.get("positionSide") not in ("LONG", "SHORT"):
            raise RuntimeError("invalid venue position evidence")
    return result


def ready(pulse, symbols, history_bars, now):
    book = pulse.sets
    usable = getattr(book, 'usable', None)
    expected = {s for s in symbols if not callable(usable) or usable(s)}
    age_of = getattr(book, '_snapshot_max_age', None)
    age = max(150.0, float(age_of())) if callable(age_of) else 150.0
    replay_keys = getattr(book, '_hist_replay_key', None)
    if replay_keys is not None:
        # Retained snapshot metadata deliberately requires an entirely usable
        # requested universe. It is not the completion marker for a normal
        # first replay when the native quality check excludes a symbol.
        scope_ready = all(replay_keys.get(s) == (book.lookback, book._topo_gen)
                          and 0 <= now-book._hist_replay_at.get(s, 0) <= age
                          for s in expected)
    else:
        scope_ready = set(getattr(book, '_history_snapshot_symbols', ())) == expected
    return (book.progress.ready and not book._running and not book._lookback_pending
            and bool(expected) and scope_ready
            and all(len(book.bars.get(s, ())) >= history_bars - 1
                    and 0 <= now - book.bar_t.get(s, 0) <= age
                    and pulse.fee_schedule.rates(s, pulse.contracts.get(s), now)[2] == "account-rate"
                    for s in symbols))


def native_admission_ready(pulse):
    """Mirror native qualification; no additional all-symbol sample gate."""
    book = pulse.sets
    return (book.progress.ready and not book._running and not book._lookback_pending
            and any(not book.gate_reason(st) for st in book.by_idx))


def context_requirement(overlay):
    sys.path.insert(0, str(ROOT / "server/pulse"))
    from set_engine import SetBook
    book = SetBook()
    book.load(overlay)
    count = book.history_bars()
    if count > 72 * 60:
        raise ValueError("test history including earlier stage context and warmup exceeds 72 hours")
    return count


def require_empty_account(api):
    if any(abs(float(p['positionAmt'])) > 1e-12 for p in checked_positions(api)) or rows(api, '/openApi/swap/v2/trade/openOrders', 'orders'):
        raise SessionEnd('account isolation lost before observation')


def freeze_account_settings(pulse):
    """The test never changes leverage or margin on a shared demo account.

    Estimate margin conservatively at 1x; actual exchange settings are left
    alone. This is a test-only execution constraint, not a changed preset.
    """
    pulse.set_leverage = lambda: None
    pulse.ensure_max_leverage = lambda _symbol, force=False: 1
    pulse.leverage_for = lambda _contract=None: 1
    # This venue does not promise atomic replacement support. Keep the native
    # place-before-cancel path; the journal allows only identified order writes.
    if getattr(pulse, "api", None) is not None:
        pulse.api.cancel_replace_ok = False


def shared_account_protection_verified():
    """Require the native quantity-only order paths, also enforced by Journal."""
    sys.path.insert(0, str(ROOT / "server/pulse"))
    from pulse_trader import Pulse
    return callable(getattr(Pulse, "quantity_controls_only", None))


def collect_account_fees(pulse, symbols, stopped, deadline, progress):
    """Bounded parallel GETs during paused startup, using the native limiter.

    Each worker uses the same native fee parser and owns its temporary book;
    only the main thread publishes validated rates to the runtime.
    """
    from execution_cost import FeeSchedule
    def fetch(symbol):
        schedule = FeeSchedule()
        schedule.refresh(pulse.api, [symbol], time.time())
        return symbol, schedule.account.get(symbol), schedule.error

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='x02-fees') as pool:
        while True:
            pending = [s for s in symbols if pulse.fee_schedule.rates(s, None, time.time())[2] != 'account-rate']
            if not pending:
                return
            if stopped():
                raise SessionEnd('requested session stop')
            if time.monotonic() >= deadline:
                raise SessionEnd('account fee readiness deadline')
            futures = [pool.submit(fetch, symbol) for symbol in pending]
            for future in as_completed(futures):
                symbol, row, error = future.result()
                if row is not None:
                    pulse.fee_schedule.account[symbol] = row
                progress(len(pulse.fee_schedule.account), len(symbols), error)
                if stopped() or time.monotonic() >= deadline:
                    for item in futures:
                        item.cancel()
                    raise SessionEnd('requested session stop' if stopped() else 'account fee readiness deadline')
            if any(s not in pulse.fee_schedule.account for s in pending):
                time.sleep(1)


def prepare_history_warmup(pulse, full_universe=True):
    """Publish measured costs before the startup-only full-universe policy."""
    pulse.apply_live_config()
    if full_universe:
        pulse.load.partial = False


def end_reason(phase, now, warm_deadline, active_deadline, last_cycle, heartbeat_seconds, stop_requested=False):
    if phase not in ('warming', 'active'):
        return None
    if stop_requested:
        return 'requested session stop'
    if phase == 'warming':
        return 'readiness deadline' if now >= warm_deadline else None
    # A stale loop must not become a successful observation merely because
    # wall time reached the requested deadline.
    if now - last_cycle > heartbeat_seconds:
        return 'main cycle heartbeat deadline'
    return 'observation complete' if active_deadline is not None and now >= active_deadline else None


def install_history_cancellation(pulse):
    """Let shutdown release a replay lock without interrupting live-close accounting."""
    book = pulse.sets
    original_replay, original_score = book.replay_all, book._score_one
    local = threading.local()
    def score(*args, **kwargs):
        if getattr(local, 'replaying', False) and pulse._hist_stop:
            raise SystemExit('session history stopped')
        return original_score(*args, **kwargs)
    def replay(*args, **kwargs):
        before = getattr(local, 'replaying', False)
        local.replaying = True
        source_step = kwargs.get('on_step')
        def step():
            if pulse._hist_stop:
                raise SystemExit('session history stopped')
            if source_step:
                source_step()
        kwargs['on_step'] = step
        try:
            return original_replay(*args, **kwargs)
        finally:
            local.replaying = before
    book._score_one, book.replay_all = score, replay


def capture(pulse, out, label, include_candles=True):
    pulse.write_stats(force=True)
    stats_path = Path(sys.modules[pulse.__class__.__module__].STATS_PATH)
    atomic_json(Path(out) / (label + "-stats.json"), json.loads(stats_path.read_text()))
    if not include_candles:
        return
    book = pulse.sets
    with book._lock:
        candles = {
            "capturedAt": time.time(), "barSeconds": 60,
            "symbols": {s: {"lastClosedBarAt": book.bar_t.get(s), "bars": list(book.bars.get(s, ()))}
                        for s in pulse.contracts},
        }
    # Disk serialization/fsync never holds the shared trading/replay lock.
    atomic_json(Path(out) / (label + "-candles.json"), candles)


def cleanup(pulse, journal, seconds):
    """Use native pending-safe closes only; never account-wide flatten/cancel."""
    journal.phase = "cleanup"
    end = time.monotonic() + seconds
    pulse._hist_stop = True
    pulse._warm_stop = True
    unresolved = []
    while time.monotonic() < end:
        # A dispatched entry can become visible after its original reply was
        # lost. Recover only this native tag before attempting owned closes.
        pulse.sync_own_fills()
        pulse.adopt_exchange_positions()
        for pos in list(pulse.open.values()):
            if not journal.owns_client(pos.symbol, pos.client_id):
                raise RuntimeError("foreign local position prevents owned cleanup")
            pulse.close_pos(pos, pulse.px.get(pos.symbol) or pos.entry, "bounded-session-cleanup")
        unresolved = []
        # An apparently flat snapshot is insufficient while an original write
        # may still fill. Resolve every submitted identity, including entries
        # not yet added to the native local book and delayed protective orders.
        with journal.lock:
            intents = list(journal.intents)
        for intent in intents:
            if intent["method"] != "POST":
                continue
            ack = intent.get("response") or {}
            if ack.get("cooled") is True:
                continue  # Native transport explicitly did not dispatch.
            if str(ack.get('code')) == '101400' and not ack.get('data'):
                continue  # Explicit invalid-parameter rejection, not a lost reply.
            for body in order_bodies(intent["path"], intent["body"]):
                cid = body["clientOrderID"]
                ack_data = ack.get("data") or {}
                ack_order = ack_data.get("order", ack_data) if isinstance(ack_data, dict) else {}
                oid = ack_order.get("orderId")
                query = {"symbol": body["symbol"], **({"orderId": str(oid)} if oid else {"clientOrderID": cid})}
                reply = pulse.api.get("/openApi/swap/v2/trade/order", query)
                data = reply.get("data") or {}
                order = data.get("order", data) if isinstance(data, dict) else {}
                found = str(order.get("clientOrderID") or order.get("clientOrderId") or "")
                own = found.lower() == cid.lower() or (oid and str(order.get("orderId")) == str(oid))
                terminal_or_control = order.get("status") in TERMINAL or (body.get("type") != "MARKET" and order.get("status") in ("NEW", "PENDING", "TRIGGERED"))
                if reply.get("error") or str(reply.get("code")) != "0" or not own or not terminal_or_control:
                    unresolved.append(cid)
        positions = checked_positions(pulse.api)
        exposed = [p for p in positions if abs(float(p["positionAmt"])) > 1e-12]
        orders = rows(pulse.api, "/openApi/swap/v2/trade/openOrders", "orders")
        own = [o for o in orders if journal.owns_order(o.get('symbol'), o.get('orderId') or o.get('orderID'))]
        if not exposed and not unresolved and not pulse.open:
            for order in own:
                pulse.api.delete("/openApi/swap/v2/trade/order", {"symbol": order["symbol"], "orderId": str(order["orderId"])})
            final = rows(pulse.api, "/openApi/swap/v2/trade/openOrders", "orders")
            if not final and not any(abs(float(p["positionAmt"])) > 1e-12 for p in checked_positions(pulse.api)):
                return {"confirmedFlat": True, "positions": 0, "orders": 0, "unresolvedOrderIntents": 0}
        time.sleep(1)
    return {"confirmedFlat": False, "localPositions": len(pulse.open), "unresolvedOrderIntents": len(unresolved),
            "reason": "cleanup deadline; unresolved exposure or order evidence; protections retained"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--overlay", type=Path, required=True)
    ap.add_argument("--symbols", type=Path, required=True, help="JSON array of exactly50 explicit symbols")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--credentials", type=Path)
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--warmup-minutes", type=float, default=30)
    ap.add_argument("--cleanup-seconds", type=float, default=180)
    ap.add_argument("--heartbeat-seconds", type=float, default=60)
    ap.add_argument("--history-hours", type=int, default=18,
                    help="evaluation hours; earlier stage context and warmup must fit the 72h test budget")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--admission", choices=('native', 'full-universe'), default='native',
                    help='native: start observation on native-qualified Sets; full-universe: additionally wait for complete frozen-universe history')
    args = ap.parse_args(argv)
    if not 0 < args.minutes <= 30 or not 0 < args.warmup_minutes <= 60 or not 0 < args.cleanup_seconds <= 600:
        ap.error("observation<=30min, warmup<=60min and cleanup<=600s must be positive")
    if not 15 <= args.heartbeat_seconds <= 300:
        ap.error('heartbeat must be between15 and300 seconds')
    if args.out.exists():
        ap.error("choose a new private output directory; sessions cannot overwrite prior evidence")
    symbols = json.loads(args.symbols.read_text())
    overlay = frozen_overlay(json.loads(args.overlay.read_text()), symbols, args.history_hours)
    required_history = context_requirement(overlay)
    os.umask(0o077)
    args.out.mkdir(parents=True, mode=0o700)
    state = args.out / "state"
    state.mkdir(mode=0o700)
    tag = "G" + secrets.token_hex(2)
    report = {"schema": "cts-g-native-x02-v1", "host": HOST, "connection": CONN, "tag": tag,
              "createdAt": time.time(), "requestedObservationSeconds": args.minutes * 60, "symbols": symbols,
              "complete": False, "executed": False, "overlay": overlay,
              "requiredHistoryBars": required_history,
              "admissionPolicy": args.admission,
              "limitations": ["minute OHLC captures cannot establish tick-identical simulation fills",
                              "external Binance/Bybit indication sources disabled; BingX VST data only",
                              "cleanup closes are reported separately from natural strategy closes",
                              "leverage and margin settings remain unchanged; margin estimates use 1x",
                              "native shared one-position-per-symbol constraint remains active",
                              "minimum venue lots may exceed nominal volume-factor sizing"],
              "sourceSha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in (Path(__file__), ROOT / "server/pulse/pulse_trader.py", ROOT / "server/pulse/set_engine.py")}}
    def save():
        atomic_json(args.out / "session.json", report)
    save()
    atomic_json(state / ("overlay-" + CONN + ".json"), overlay)
    if not args.execute:
        report["planOnly"] = True
        save()
        print("Plan saved; no credentials read and no connection started.")
        return 0
    # The native engine's overall protection uses closePosition, which can
    # close someone else's same-side aggregate if another process joins this
    # shared account. Do not open a lot that this session cannot protect under
    # the requested ownership boundary. Remove this block only after bounded
    # protection and fill-level isolation are independently verified.
    if not shared_account_protection_verified():
        report['stopReason'] = 'native aggregate-closing protection is not isolated on a shared account'
        save()
        print(json.dumps({'complete': False, 'executed': False, 'stopReason': report['stopReason']}))
        return 1
    if not args.credentials or args.credentials.stat().st_mode & 0o077:
        raise ValueError("execute requires a private0600 credential JSON file")
    credentials = json.loads(args.credentials.read_text())
    if not credentials.get("api_key") or not credentials.get("api_secret"):
        raise ValueError("credential JSON requires api_key and api_secret")
    if credentials.get("base_url", HOST).rstrip("/") != HOST or str(credentials.get("is_testnet", "true")).lower() not in ("true", "1"):
        raise ValueError("credentials are not explicitly scoped to VST")
    os.environ["PULSE_CONN"] = CONN
    os.environ["PULSE_BASE"] = HOST
    sys.path.insert(0, str(ROOT / "server/pulse"))
    import pulse_trader as pt
    if pt.CONN_SHORT != CONN or pt.IS_BYBIT or pt.BASE != HOST:
        raise RuntimeError('native module does not match the fixed VST identity')
    relocate_runtime(pt, state, tag)
    pt.connection_field = lambda field: str(credentials.get(field, ""))
    api = pt.make_api(CONN, credentials["api_key"], credentials["api_secret"], HOST)
    balances = rows(api, "/openApi/swap/v3/user/balance")
    if not any(r.get("asset") == "VST" for r in balances):
        raise RuntimeError("selected authenticated account is not confirmed VST")
    if any(abs(float(p["positionAmt"])) > 1e-12 for p in checked_positions(api)) or rows(api, "/openApi/swap/v2/trade/openOrders", "orders"):
        raise RuntimeError("fresh exclusive empty-account preflight failed")
    report["preflight"] = {"at": time.time(), "virtualCurrency": "VST", "positions": 0, "orders": 0}
    journal = Journal(api, args.out, tag)
    Path(pt.PAUSE_PATH).touch()
    contracts = pt.load_contracts(set(symbols))
    if set(contracts) != set(symbols):
        raise RuntimeError("selected symbol contract coverage is incomplete")
    pulse = pt.Pulse(api, contracts)
    freeze_account_settings(pulse)
    install_history_cancellation(pulse)
    original_cycle = pulse._one_cycle
    # No position exists during admission-locked warmup. Process the complete
    # frozen universe once instead of re-scoring its growing pool after every
    # tiny load-governor chunk. Restore the configured live policy at admission.
    warming_since = time.monotonic()
    last_cycle = [warming_since]
    watchdog_stop = threading.Event()
    watchdog_reason = [None]
    next_capture = [0.0]
    def cycle():
        now = time.monotonic()
        if (args.out / 'STOP_SESSION').exists():
            raise SessionEnd('requested session stop')
        if pulse.ignored_foreign:
            raise SessionEnd('foreign account position detected during session')
        if journal.phase == "warming":
            admission_ready = (native_admission_ready(pulse) if args.admission == 'native'
                               else ready(pulse, symbols, required_history, time.time()))
            if admission_ready:
                require_empty_account(api)
                capture(pulse, args.out, "ready")
                report["activeStartedAt"] = time.time()
                journal.deadline = time.monotonic() + args.minutes * 60
                journal.phase = "active"
                last_cycle[0] = time.monotonic()
                pulse.load.partial = bool(overlay.get('loadPartial', True))
                Path(pt.PAUSE_PATH).unlink(missing_ok=True)
                save()
            elif now - warming_since >= args.warmup_minutes * 60:
                raise SessionEnd("readiness deadline")
        elif now >= journal.deadline:
            raise SessionEnd("observation complete")
        original_cycle()
        last_cycle[0] = time.monotonic()
        report['lastCycleCompletedAt'] = time.time()
        if time.monotonic() >= next_capture[0]:
            elapsed = int(time.time() - report.get("activeStartedAt", report["createdAt"]))
            capture(pulse, args.out, f"{journal.phase}-{elapsed:05d}", include_candles=False)
            save()
            next_capture[0] = time.monotonic() + 60
    pulse._one_cycle = cycle
    def interrupt(_sig, _frame):
        reason = watchdog_reason[0] or end_reason(journal.phase, time.monotonic(),
            warming_since + args.warmup_minutes*60, journal.deadline, last_cycle[0],
            args.heartbeat_seconds, (args.out/'STOP_SESSION').exists())
        raise SessionEnd(reason or "operator interrupt")
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    def watch():
        while not watchdog_stop.wait(.25):
            reason = end_reason(journal.phase, time.monotonic(), warming_since + args.warmup_minutes*60,
                                journal.deadline, last_cycle[0], args.heartbeat_seconds,
                                (args.out/'STOP_SESSION').exists())
            if reason:
                watchdog_reason[0] = reason
                os.kill(os.getpid(), signal.SIGTERM)
                return
    threading.Thread(target=watch, name='session-watch', daemon=True).start()
    report["executed"] = True
    save()
    try:
        def fee_progress(measured, total, error):
            report['readiness'] = dict(phase='account-fees', measured=measured, total=total, error=error)
            save()
            if measured % 10 == 0 or error:
                print(json.dumps(report['readiness']), flush=True)
        collect_account_fees(pulse, symbols, lambda: (args.out / 'STOP_SESSION').exists(),
                             warming_since + args.warmup_minutes * 60, fee_progress)
        report['readiness'] = dict(phase='native-history', measured=len(symbols), total=len(symbols))
        save()
        # Paused native cycles intentionally skip hot reload. Publish the fully
        # measured account cost before building any history or unpausing entries.
        prepare_history_warmup(pulse, full_universe=args.admission == 'full-universe')
        require_empty_account(api)
        pulse.run()
        report["stopReason"] = "native stopped before observation deadline"
    except SessionEnd as exc:
        report["stopReason"] = str(exc)
    except BaseException as exc:
        report["stopReason"] = "runtime failure"
        report["errorType"] = type(exc).__name__
    finally:
        watchdog_stop.set()
        report["activeEndedAt"] = time.time()
        report['watchdogReason'] = watchdog_reason[0]
        pulse._hist_stop = pulse._warm_stop = True
        Path(pt.STOP_PATH).touch()
        try:
            report["cleanup"] = cleanup(pulse, journal, args.cleanup_seconds)
        except BaseException as exc:
            report["cleanup"] = {"confirmedFlat": False, "errorType": type(exc).__name__, "protectionsRetained": True}
        pulse.halted = True
        pulse.halt_reason = 'bounded session stopped: ' + str(report.get('stopReason') or 'unknown')
        try:
            capture(pulse, args.out, "final")
        except BaseException as exc:
            report['finalCaptureError'] = type(exc).__name__
        report["observationSeconds"] = max(0, report["activeEndedAt"] - report.get("activeStartedAt", report["activeEndedAt"]))
        report["complete"] = (report.get("stopReason") == "observation complete"
                              and report["observationSeconds"] >= args.minutes * 60
                              and report["cleanup"]["confirmedFlat"])
        save()
        pulse._hist_stop = pulse._warm_stop = True
        pulse.pool.shutdown(wait=False, cancel_futures=True)
    print(json.dumps({k: report.get(k) for k in ("complete", "stopReason", "observationSeconds", "cleanup")}))
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    code = main()
    # Native feed threads belong to this isolated process, not a persistent desk.
    # All completion/cleanup evidence above is durably saved before exit.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
