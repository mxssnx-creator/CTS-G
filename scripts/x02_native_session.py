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
              indExtraSources=False)
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
        self.lock = threading.RLock()
        self.original = api._req
        api._req = self.request

    def append(self, row):
        with (self.out / "mutation-journal.jsonl").open("a") as stream:
            stream.write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def request(self, method, path, extra=None):
        if self.api.base.rstrip('/') != HOST or not path.startswith('/openApi/') or '://' in path:
            raise RuntimeError('bounded session permits only the fixed BingX VST transport')
        if method == "GET":
            return self.original(method, path, extra)
        body = dict(extra or {})
        bodies = order_bodies(path, body) if method == "POST" else []
        for order in bodies:
            cid = str(order.get("clientOrderID") or order.get("clientOrderId") or "")
            if not cid.lower().startswith(self.tag.lower()):
                raise RuntimeError("native order is missing this session's ownership tag")
            if is_entry(order) and (self.phase != "active" or self.deadline is None or time.monotonic() >= self.deadline):
                return {"code": 101209, "error": True, "msg": "session admission paused or deadline reached"}
        with self.lock:
            index = len(self.intents)
            row = {"index": index, "at": time.time(), "phase": self.phase, "method": method,
                   "path": path, "body": body, "result": "pending"}
            self.intents.append(row)
            self.append(row)
            try:
                reply = self.original(method, path, body)
            except BaseException as exc:
                self.append({"index": index, "at": time.time(), "result": "unknown", "errorType": type(exc).__name__})
                raise
            row["response"] = reply
            self.append({"index": index, "at": time.time(), "result": "response", "response": reply})
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
    return (book.progress.ready and not book._running and not book._lookback_pending
            and set(getattr(book, "_history_snapshot_symbols", ())) == set(symbols)
            and all(len(book.bars.get(s, ())) >= history_bars - 1
                    and 0 <= now - book.bar_t.get(s, 0) <= 150
                    and pulse.fee_schedule.rates(s, pulse.contracts.get(s), now)[2] == "account-rate"
                    for s in symbols))


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


def capture(pulse, out, label):
    pulse.write_stats(force=True)
    stats_path = Path(sys.modules[pulse.__class__.__module__].STATS_PATH)
    atomic_json(Path(out) / (label + "-stats.json"), json.loads(stats_path.read_text()))
    book = pulse.sets
    with book._lock:
        atomic_json(Path(out) / (label + "-candles.json"), {
            "capturedAt": time.time(), "barSeconds": 60,
            "symbols": {s: {"lastClosedBarAt": book.bar_t.get(s), "bars": list(book.bars.get(s, ()))}
                        for s in pulse.contracts},
        })


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
            if not str(pos.client_id).lower().startswith(journal.tag.lower()):
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
        own = [o for o in orders if str(o.get("clientOrderID") or o.get("clientOrderId") or "").lower().startswith(journal.tag.lower())]
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
    ap.add_argument("--history-hours", type=int, default=18,
                    help="evaluation hours; earlier stage context and warmup must fit the 72h test budget")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args(argv)
    if not 0 < args.minutes <= 30 or not 0 < args.warmup_minutes <= 60 or not 0 < args.cleanup_seconds <= 600:
        ap.error("observation<=30min, warmup<=60min and cleanup<=600s must be positive")
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
              "limitations": ["minute OHLC captures cannot establish tick-identical simulation fills",
                              "external Binance/Bybit indication sources disabled; BingX VST data only",
                              "cleanup closes are reported separately from natural strategy closes",
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
    original_cycle = pulse._one_cycle
    warming_since = time.monotonic()
    next_capture = [0.0]
    def cycle():
        now = time.monotonic()
        if (args.out / 'STOP_SESSION').exists():
            raise SessionEnd('requested session stop')
        if journal.phase == "warming":
            if pulse.ignored_foreign:
                raise SessionEnd('foreign account position detected during warmup')
            if ready(pulse, symbols, required_history, time.time()):
                require_empty_account(api)
                capture(pulse, args.out, "ready")
                report["activeStartedAt"] = time.time()
                journal.deadline = time.monotonic() + args.minutes * 60
                journal.phase = "active"
                Path(pt.PAUSE_PATH).unlink(missing_ok=True)
                save()
            elif now - warming_since >= args.warmup_minutes * 60:
                raise SessionEnd("readiness deadline")
        elif now >= journal.deadline:
            raise SessionEnd("observation complete")
        original_cycle()
        if time.monotonic() >= next_capture[0]:
            elapsed = int(time.time() - report.get("activeStartedAt", report["createdAt"]))
            capture(pulse, args.out, f"{journal.phase}-{elapsed:05d}")
            next_capture[0] = time.monotonic() + 60
    pulse._one_cycle = cycle
    def interrupt(_sig, _frame):
        raise SessionEnd("operator interrupt")
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    report["executed"] = True
    save()
    try:
        # Native refresh reads one rate per invocation. During paused startup,
        # explicitly collect every selected account rate at its one-second pace,
        # rather than waiting 50 ordinary strategy cycles before the cost is known.
        while not all(pulse.fee_schedule.rates(s, contracts[s], time.time())[2] == "account-rate" for s in symbols):
            if (args.out / 'STOP_SESSION').exists():
                raise SessionEnd('requested session stop')
            if time.monotonic() - warming_since >= args.warmup_minutes * 60:
                raise SessionEnd("account fee readiness deadline")
            pulse.refresh_fees()
            time.sleep(1)
        # Paused native cycles intentionally skip hot reload. Publish the fully
        # measured account cost before building any history or unpausing entries.
        pulse.apply_live_config()
        require_empty_account(api)
        pulse.run()
        report["stopReason"] = "native stopped before observation deadline"
    except SessionEnd as exc:
        report["stopReason"] = str(exc)
    except BaseException as exc:
        report["stopReason"] = "runtime failure"
        report["errorType"] = type(exc).__name__
    finally:
        report["activeEndedAt"] = time.time()
        Path(pt.STOP_PATH).touch()
        try:
            capture(pulse, args.out, "before-cleanup")
            report["cleanup"] = cleanup(pulse, journal, args.cleanup_seconds)
            capture(pulse, args.out, "final")
        except BaseException as exc:
            report["cleanup"] = {"confirmedFlat": False, "errorType": type(exc).__name__, "protectionsRetained": True}
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
