"""Bybit suite: FastBybit against a stub Bybit V5 venue, and the live trader on top of it.

The venue is a transport function (no socket): it verifies the X-BAPI signature of every private request, keeps orders,
positions and executions, applies the V5 rules the trader runs into (hedge positionIdx, reduce-only needs a position,
trigger price on the right side of the market, order link id length, qty on the step), and answers in the V5 envelope
(retCode, retMsg, result, retExtInfo). Bybit itself is not reachable from the build host (country block), so these
checks are the proof of the translation: BingX call in, Bybit V5 request out, BingX-shaped answer back.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
import types
import urllib.parse
from decimal import Decimal

from common import PULSE, Skip  # noqa: F401
import bybit_fast as bf
import pulse_trader as pt
from bingx_fast import ErrorLog

_STATE_DIR = tempfile.mkdtemp(prefix="ctsg-bybit-test-")
for _name in ("STATS_PATH", "TRADES_PATH", "STOP_PATH", "PAUSE_PATH", "LOG_PATH", "BLOCK_PATH", "OVERLAY_PATH",
              "OPEN_PATH", "CTS_PATH", "ERR_PATH", "LEV_PATH", "START_EQ_PATH", "RESET_EQ_PATH", "UNIVERSE_PATH"):
    if hasattr(pt, _name):
        setattr(pt, _name, os.path.join(_STATE_DIR, "by-" + os.path.basename(getattr(pt, _name))))

KEY, SECRET = "TESTKEY", "TESTSECRET"
_REAL_LOAD_CONTRACTS = pt.load_contracts     # the trader suite replaces it with an offline one when it is imported; this suite is imported first

INSTRUMENTS = [
    {"symbol": "BTCUSDT", "status": "Trading", "contractType": "LinearPerpetual", "baseCoin": "BTC", "quoteCoin": "USDT", "settleCoin": "USDT",
     "lotSizeFilter": {"minOrderQty": "0.001", "maxOrderQty": "100", "qtyStep": "0.001", "minNotionalValue": "5", "maxMktOrderQty": "50"},
     "priceFilter": {"tickSize": "0.5", "minPrice": "0.5", "maxPrice": "1999999.5"},
     "leverageFilter": {"minLeverage": "1", "maxLeverage": "100.00", "leverageStep": "0.01"}},
    {"symbol": "1000PEPEUSDT", "status": "Trading", "contractType": "LinearPerpetual", "baseCoin": "1000PEPE", "quoteCoin": "USDT", "settleCoin": "USDT",
     "lotSizeFilter": {"minOrderQty": "10", "maxOrderQty": "1000000", "qtyStep": "10", "minNotionalValue": "5", "maxMktOrderQty": "500000"},
     "priceFilter": {"tickSize": "0.0000001", "minPrice": "0.0000001", "maxPrice": "9.9999999"},
     "leverageFilter": {"minLeverage": "1", "maxLeverage": "25.00", "leverageStep": "0.01"}},
    {"symbol": "BTCPERP", "status": "Trading", "contractType": "LinearPerpetual", "baseCoin": "BTC", "quoteCoin": "USDC", "settleCoin": "USDC",
     "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001", "minNotionalValue": "5"}, "priceFilter": {"tickSize": "0.1"},
     "leverageFilter": {"maxLeverage": "100"}},
    {"symbol": "BTC-26DEC26", "status": "Trading", "contractType": "LinearFutures", "baseCoin": "BTC", "quoteCoin": "USDT", "settleCoin": "USDT",
     "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001", "minNotionalValue": "5"}, "priceFilter": {"tickSize": "0.5"},
     "leverageFilter": {"maxLeverage": "50"}},
    {"symbol": "ETHUSDT", "status": "Trading", "contractType": "LinearFutures", "baseCoin": "ETH", "quoteCoin": "USDT", "settleCoin": "USDT",
     "lotSizeFilter": {"minOrderQty": "0.01", "qtyStep": "0.01", "minNotionalValue": "5"}, "priceFilter": {"tickSize": "0.01"},
     "leverageFilter": {"maxLeverage": "50"}},
    {"symbol": "NEWUSDT", "status": "PreLaunch", "contractType": "LinearPerpetual", "baseCoin": "NEW", "quoteCoin": "USDT", "settleCoin": "USDT",
     "lotSizeFilter": {"minOrderQty": "1", "qtyStep": "1", "minNotionalValue": "5"}, "priceFilter": {"tickSize": "0.01"},
     "leverageFilter": {"maxLeverage": "10"}},
]


def env(code=0, msg="OK", result=None, ext=None):
    return {"retCode": code, "retMsg": msg, "result": result if result is not None else {}, "retExtInfo": ext if ext is not None else {}, "time": int(time.time() * 1000)}


class StubBybit:
    """A Bybit V5 linear venue in a function. Call it as the transport: (method, url, headers, body) -> (status, headers, bytes)."""

    def __init__(self, key=KEY, secret=SECRET, page=500):
        self.key, self.secret, self.page = key, secret, page
        self.calls = []                 # (method, path, params or body dict)
        self.mid = {"BTCUSDT": 100.0, "1000PEPEUSDT": 0.001}
        self.orders = {}                # orderId -> order dict
        self.pos = {}                   # (symbol, idx) -> {"size": Decimal, "avg": float, "lev": int}
        self.execs = []
        self.hedge = False
        self.lev = {}
        self.n = 0
        self.rate_limit_next = None     # (status, headers, envelope) returned once for the next private call
        self.fail = {}                  # path -> envelope returned once
        self.sig_failures = 0
        self.balance_unified_ok = True
        self.exec_lag = 0               # executions that answer empty before the fill shows (tests the retry)
        self.margin_calls = 0
        self.mode_calls = 0
        self.headers = {}               # response headers added to every private reply (rate-limit headers)
        self.lose = {}                  # path -> n: process the next n requests of that path, then lose the response (timeout)
        self.script = {}                # path -> [envelope, ...]: answered before any processing, one per request
        self.item_script = {}           # path -> [{index: (code, msg)}, ...]: per-item failures of the next batch calls (not processed)
        self.skew_ms = 0                # the venue's clock runs this far ahead of the caller's
        self.raw = {}                   # path -> [(status, bytes), ...]: raw answers before any processing
        self.clock = time.time          # the venue's clock (a test with a fake client clock points it at that clock)

    # -- transport -------------------------------------------------------------------------------------------------------
    def __call__(self, method, url, headers, body):
        u = urllib.parse.urlsplit(url)
        path, query = u.path, u.query
        params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
        text = body.decode() if body else ""
        data = json.loads(text) if text else {}
        self.calls.append((method, path, data if method == "POST" else params))
        if path in self.raw and self.raw[path]:
            st, bts = self.raw[path].pop(0)
            return st, {}, bts
        if path == "/v5/market/time":
            now_ms = int(self.clock() * 1000) + self.skew_ms
            return 200, {}, json.dumps({"retCode": 0, "retMsg": "OK", "result": {"timeSecond": str(now_ms // 1000), "timeNano": str(now_ms * 1_000_000)}, "time": now_ms}).encode()
        if not path.startswith("/v5/market"):
            ts, rw, sg = headers.get("X-BAPI-TIMESTAMP", ""), headers.get("X-BAPI-RECV-WINDOW", ""), headers.get("X-BAPI-SIGN", "")
            want = hmac.new(self.secret.encode(), f"{ts}{headers.get('X-BAPI-API-KEY', '')}{rw}{query if method == 'GET' else text}".encode(), hashlib.sha256).hexdigest()
            if headers.get("X-BAPI-API-KEY") != self.key or sg != want:
                self.sig_failures += 1
                return 200, {}, json.dumps(env(10004, "error sign! origin_string[" + ts + "...]")).encode()
            if abs(int(ts or 0) - (int(self.clock() * 1000) + self.skew_ms)) > int(rw or 5000):
                return 200, {}, json.dumps(env(10002, "invalid request, please check your server timestamp or recv_window param")).encode()
            if self.script.get(path):
                return 200, dict(self.headers), json.dumps(self.script[path].pop(0)).encode()
            if self.rate_limit_next is not None:
                st, hd, ev = self.rate_limit_next
                self.rate_limit_next = None
                return st, hd, json.dumps(ev).encode()
            if path in self.fail:
                return 200, {}, json.dumps(self.fail.pop(path)).encode()
        h = getattr(self, "h_" + path.strip("/").replace("v5/", "").replace("/", "_").replace("-", "_"), None)
        if h is None:
            return 404, {}, json.dumps(env(10001, "unknown path " + path)).encode()
        if path in self.item_script and self.item_script[path]:
            fails = self.item_script[path].pop(0)
            reqs = data.get("request", [])
            ok_reqs = [r for i, r in enumerate(reqs) if i not in fails]
            sub = h(method, {**data, "request": ok_reqs})
            made, info, k = [], [], 0
            for i, r in enumerate(reqs):
                if i in fails:
                    made.append({"category": "linear", "symbol": r["symbol"], "orderId": "", "orderLinkId": r.get("orderLinkId", ""), "createAt": ""})
                    info.append({"code": fails[i][0], "msg": fails[i][1]})
                else:
                    made.append(sub["result"]["list"][k])
                    info.append(sub["retExtInfo"]["list"][k])
                    k += 1
            out = env(result={"list": made}, ext={"list": info})
        else:
            out = h(method, params if method == "GET" else data)
        if self.lose.get(path):
            self.lose[path] -= 1
            raise OSError("timed out")
        return 200, dict(self.headers) if not path.startswith("/v5/market") else {}, json.dumps(out).encode()

    def paged(self, rows, p):
        start = int(p.get("cursor") or 0)
        lim = min(int(p.get("limit") or 50), self.page)
        chunk = rows[start:start + lim]
        nxt = str(start + lim) if start + lim < len(rows) else ""
        return env(result={"list": chunk, "nextPageCursor": nxt})

    # -- market ----------------------------------------------------------------------------------------------------------
    def h_market_instruments_info(self, m, p):
        return self.paged(INSTRUMENTS, p)

    def h_market_tickers(self, m, p):
        rows = [{"symbol": s, "lastPrice": str(px), "markPrice": str(px + 0.1), "indexPrice": str(px), "price24hPcnt": "0.05", "turnover24h": "123456.5",
                 "volume24h": "1000", "highPrice24h": str(px * 1.1), "lowPrice24h": str(px * 0.9), "bid1Price": str(px), "ask1Price": str(px)} for s, px in self.mid.items()]
        rows.append({"symbol": "BTCPERP", "lastPrice": "100", "markPrice": "100"})
        rows.append({"symbol": "BTC-26DEC26", "lastPrice": "100", "markPrice": "100"})
        if p.get("symbol"):
            rows = [r for r in rows if r["symbol"] == p["symbol"]]
        return env(result={"category": "linear", "list": rows})

    def h_market_kline(self, m, p):
        end = int(p.get("end") or 1_700_000_000_000 + 600_000)
        n = min(int(p.get("limit") or 200), 5)
        rows = []
        for i in range(n):                                   # newest first, as the venue answers
            t = end - i * 60_000
            rows.append([str(t), "100", "101", "99", "100.5", "7", "700"])
        return env(result={"category": "linear", "symbol": p.get("symbol"), "list": rows})

    def h_market_time(self, m, p):
        return env(result={"timeSecond": str(int(time.time())), "timeNano": str(int(time.time() * 1e9))})

    # -- account / position ----------------------------------------------------------------------------------------------
    def h_account_wallet_balance(self, m, p):
        if p.get("accountType") == "UNIFIED" and not self.balance_unified_ok:
            return env(10001, "accountType only support UNIFIED")
        acct = {"accountType": p.get("accountType"), "totalEquity": "101.5", "totalWalletBalance": "100", "totalAvailableBalance": "88.25",
                "totalPerpUPL": "1.5", "totalInitialMargin": "13.25",
                "coin": [{"coin": "USDT", "equity": "101.5", "walletBalance": "100", "unrealisedPnl": "1.5", "totalPositionIM": "13.25"}]}
        return env(result={"list": [acct]})

    def h_position_switch_mode(self, m, p):
        self.mode_calls += 1
        if self.hedge:
            return env(110025, "Position mode is not modified")
        self.hedge = True
        return env()

    def h_account_set_margin_mode(self, m, p):
        self.margin_calls += 1
        return env()

    def h_position_set_leverage(self, m, p):
        sym = p["symbol"]
        if self.lev.get(sym) == (p["buyLeverage"], p["sellLeverage"]):
            return env(110043, "leverage not modified")
        self.lev[sym] = (p["buyLeverage"], p["sellLeverage"])
        for (s, idx), row in self.pos.items():
            if s == sym:
                row["lev"] = int(p["buyLeverage"])
        return env()

    def h_position_list(self, m, p):
        rows = []
        for (s, idx), r in sorted(self.pos.items()):
            if p.get("symbol") and p["symbol"] != s:
                continue
            side = "" if r["size"] <= 0 else ("Buy" if idx in (0, 1) and (idx == 1 or r.get("long", True)) else "Sell")
            if idx == 2:
                side = "" if r["size"] <= 0 else "Sell"
            if r["size"] <= 0 and not p.get("symbol"):
                continue
            rows.append({"symbol": s, "side": side, "size": format(r["size"], "f") if r["size"] > 0 else "0", "avgPrice": str(r["avg"]), "positionIdx": idx,
                         "leverage": str(r.get("lev") or int(self.lev.get(s, ("10",))[0])), "liqPrice": "", "markPrice": str(self.mid.get(s, 0)),
                         "unrealisedPnl": "0.5", "createdTime": "1700000000000", "updatedTime": "1700000001000", "positionStatus": "Normal"})
        return self.paged(rows, p)

    def h_position_closed_pnl(self, m, p):
        rows = [{"symbol": e["symbol"], "orderId": e["orderId"], "side": e["side"], "closedPnl": e["pnl"], "closedSize": e["execQty"]} for e in self.execs if e.get("pnl")]
        return env(result={"list": rows[-100:], "nextPageCursor": ""})

    # -- orders ----------------------------------------------------------------------------------------------------------
    def _new_id(self):
        self.n += 1
        return f"oid{self.n:04d}"

    def _check(self, o):
        """The V5 rules the trader meets. Returns an error envelope, or None."""
        sym, idx = o["symbol"], int(o.get("positionIdx", 0))
        if o.get("category") != "linear" or sym not in self.mid:
            return env(10001, "params error: category/symbol")
        if self.hedge and idx not in (1, 2):
            return env(10001, "position idx not match position mode")
        if len(o.get("orderLinkId", "")) > 36:
            return env(10001, "orderLinkId is too long")
        if o.get("qty") not in ("0",):
            step = Decimal("0.001") if sym == "BTCUSDT" else Decimal("10")
            if Decimal(str(o["qty"])) % step != 0 or Decimal(str(o["qty"])) <= 0:
                return env(110017, "Order quantity does not meet the qty step")
        if o.get("orderLinkId") and any(x.get("orderLinkId") == o.get("orderLinkId") for x in self.orders.values()):
            return env(110072, "OrderLinkedID is duplicate")
        trig = o.get("triggerPrice")
        if trig:
            tp = float(trig)
            if o.get("triggerDirection") == 1 and tp <= self.mid[sym]:
                return env(110092, "expect Rising, but trigger_price[%s] <= current[%s]" % (trig, self.mid[sym]))
            if o.get("triggerDirection") == 2 and tp >= self.mid[sym]:
                return env(110093, "expect Falling, but trigger_price[%s] >= current[%s]" % (trig, self.mid[sym]))
        if o.get("reduceOnly"):
            held = self.pos.get((sym, idx), {}).get("size", Decimal(0))
            if held <= 0:
                return env(110017, "current position is zero, cannot fix reduce-only order qty")
        elif o.get("orderType") == "Market" and not trig:
            lev = self.lev.get(sym)
            if lev is None and self.pos.get((sym, idx)) is None:
                pass
        return None

    def _create(self, o):
        bad = self._check(o)
        if bad is not None:
            return None, bad
        oid = self._new_id()
        rec = dict(o)
        rec.update(orderId=oid, orderStatus="Untriggered" if o.get("triggerPrice") else "New", cumExecQty="0", avgPrice="", createdTime="1700000000000", updatedTime="1700000000000")
        self.orders[oid] = rec
        if not o.get("triggerPrice") and o.get("orderType") == "Market":
            self._fill(rec, self.mid[o["symbol"]])
        return {"orderId": oid, "orderLinkId": o.get("orderLinkId", "")}, None

    def _fill(self, rec, px):
        sym, idx = rec["symbol"], int(rec.get("positionIdx", 0))
        key = (sym, idx)
        row = self.pos.setdefault(key, {"size": Decimal(0), "avg": px, "lev": 10})
        qty = row["size"] if rec["qty"] == "0" else Decimal(str(rec["qty"]))
        pnl = ""
        if rec.get("reduceOnly"):
            qty = min(qty, row["size"])
            long_ = idx == 1
            pnl = str(round(float(qty) * ((px - row["avg"]) if long_ else (row["avg"] - px)), 6))
            row["size"] -= qty
        else:
            tot = row["size"] + qty
            row["avg"] = (float(row["size"]) * row["avg"] + float(qty) * px) / float(tot)
            row["size"] = tot
        rec.update(orderStatus="Filled", cumExecQty=format(qty, "f"), avgPrice=str(px), updatedTime="1700000002000")
        self.execs.append({"symbol": sym, "orderId": rec["orderId"], "orderLinkId": rec.get("orderLinkId", ""), "side": rec["side"], "execQty": format(qty, "f"),
                           "execPrice": str(px), "execType": "Trade", "execTime": "1700000002000", "pnl": pnl})

    def h_order_create(self, m, o):
        res, bad = self._create(o)
        return bad if bad is not None else env(result=res)

    def h_order_create_batch(self, m, p):
        made, info = [], []
        for o in p["request"]:
            res, bad = self._create({"category": p["category"], **o})
            if bad is not None:
                made.append({"category": "linear", "symbol": o["symbol"], "orderId": "", "orderLinkId": o.get("orderLinkId", ""), "createAt": ""})
                info.append({"code": bad["retCode"], "msg": bad["retMsg"]})
            else:
                made.append({"category": "linear", "symbol": o["symbol"], **res, "createAt": "1700000000000"})
                info.append({"code": 0, "msg": "OK"})
        return env(result={"list": made}, ext={"list": info})

    def h_order_cancel(self, m, p):
        o = self.orders.get(p.get("orderId", ""))
        if o is None:
            return env(110001, "order not exists or too late to cancel")
        if o["orderStatus"] not in ("New", "Untriggered"):
            return env(110008, "Order has been finished or canceled")
        o["orderStatus"] = "Cancelled"
        return env(result={"orderId": o["orderId"], "orderLinkId": o.get("orderLinkId", "")})

    def h_order_amend(self, m, p):
        o = self.orders.get(p.get("orderId", ""))
        if o is None or o["orderStatus"] not in ("New", "Untriggered"):
            return env(110001, "order not exists or too late to replace")
        tp = float(p["triggerPrice"])
        if o.get("triggerDirection") == 2 and tp >= self.mid[o["symbol"]]:
            return env(110093, "expect Falling, but trigger_price[%s] >= current[%s]" % (p["triggerPrice"], self.mid[o["symbol"]]))
        if o.get("triggerDirection") == 1 and tp <= self.mid[o["symbol"]]:
            return env(110092, "expect Rising, but trigger_price[%s] <= current[%s]" % (p["triggerPrice"], self.mid[o["symbol"]]))
        o["triggerPrice"] = p["triggerPrice"]
        return env(result={"orderId": o["orderId"], "orderLinkId": o.get("orderLinkId", "")})

    def h_order_amend_batch(self, m, p):
        made, info = [], []
        for r in p["request"]:
            out = self.h_order_amend("POST", {"category": p["category"], **r})
            if out["retCode"] == 0:
                made.append({"category": "linear", "symbol": r["symbol"], "orderId": r["orderId"], "orderLinkId": ""})
                info.append({"code": 0, "msg": "OK"})
            else:
                made.append({"category": "linear", "symbol": r["symbol"], "orderId": "", "orderLinkId": ""})
                info.append({"code": out["retCode"], "msg": out["retMsg"]})
        return env(result={"list": made}, ext={"list": info})

    def h_order_cancel_batch(self, m, p):
        made, info = [], []
        for r in p["request"]:
            out = self.h_order_cancel("POST", {"category": p["category"], **r})
            ok_ = out["retCode"] == 0
            made.append({"category": "linear", "symbol": r["symbol"], "orderId": r.get("orderId", "") if ok_ else "", "orderLinkId": ""})
            info.append({"code": out["retCode"], "msg": out["retMsg"]})
        return env(result={"list": made}, ext={"list": info})

    def _rows(self, statuses, p):
        rows = []
        for o in self.orders.values():
            if o["orderStatus"] not in statuses:
                continue
            if p.get("symbol") and p["symbol"] != o["symbol"]:
                continue
            if p.get("orderId") and p["orderId"] != o["orderId"]:
                continue
            if p.get("orderLinkId") and p["orderLinkId"] != o.get("orderLinkId"):
                continue
            rows.append(o)
        return rows

    def h_order_realtime(self, m, p):
        return self.paged(self._rows(("New", "Untriggered", "PartiallyFilled"), p), p)

    def h_order_history(self, m, p):
        return self.paged(self._rows(("Filled", "Cancelled", "Rejected", "Triggered"), p), p)

    def h_execution_list(self, m, p):
        if self.exec_lag > 0:
            self.exec_lag -= 1
            return env(result={"list": [], "nextPageCursor": ""})
        rows = [e for e in self.execs if (not p.get("orderId") or e["orderId"] == p["orderId"]) and (not p.get("symbol") or e["symbol"] == p["symbol"])]
        return env(result={"list": rows, "nextPageCursor": ""})

    # -- test helpers ----------------------------------------------------------------------------------------------------
    def move(self, sym, px):
        """The market moves to px: conditional orders whose trigger is crossed fire as market orders at their trigger price."""
        self.mid[sym] = px
        for o in list(self.orders.values()):
            if o["symbol"] != sym or o["orderStatus"] != "Untriggered":
                continue
            tp = float(o["triggerPrice"])
            if (o["triggerDirection"] == 1 and px >= tp) or (o["triggerDirection"] == 2 and px <= tp):
                if self.pos.get((sym, int(o["positionIdx"])), {}).get("size", Decimal(0)) > 0:
                    self._fill(o, tp)
                else:
                    o["orderStatus"] = "Deactivated"

    def sent(self, path, method="POST"):
        return [d for m_, p_, d in self.calls if p_ == path and m_ == method]


def _api(stub=None, **kw):
    stub = stub or StubBybit()
    return bf.FastBybit(KEY, SECRET, ErrorLog(os.path.join(_STATE_DIR, "by-err.jsonl")), transport=stub, **kw), stub


def _contracts():
    return {c["symbol"]: pt.Contract(symbol=c["symbol"], min_qty=c["min_qty"], step=c["step"], qprec=c["qprec"], pprec=c["pprec"], min_usdt=c["min_usdt"], max_lev=c["max_lev"])
            for c in bf.contract_rows(INSTRUMENTS)}


_CID = [0]


def _cid(kind="u"):
    _CID[0] += 1
    return f"Gx01{kind}g060308000{_CID[0]:05d}"[:32]


class Clock:
    """A fake clock: sleeping advances it, nothing really waits."""

    def __init__(self, t=1_800_000_000.0):
        self.t, self.slept = t, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += s


def _api_clock(stub=None, **kw):
    c = Clock(time.time())
    stub = stub or StubBybit()
    stub.clock = c.now
    return bf.FastBybit(KEY, SECRET, ErrorLog(os.path.join(_STATE_DIR, "by-err.jsonl")), transport=stub, clock=c.now, sleep=c.sleep, **kw), stub, c


# ---- checks ----------------------------------------------------------------------------------------------------------------

def requests_are_signed_over_the_exact_query_and_body():
    api, stub = _api()
    api.get(bf.P_POS)
    api.post(bf.P_ORDER, {"symbol": "BTC-USDT", "type": "MARKET", "side": "BUY", "positionSide": "LONG", "quantity": "0.1", "clientOrderID": _cid("o")})
    ts = api.sign("1700000000000", "category=linear&settleCoin=USDT&limit=200")
    want = hmac.new(SECRET.encode(), f"1700000000000{KEY}5000category=linear&settleCoin=USDT&limit=200".encode(), hashlib.sha256).hexdigest()
    bad_api, bad_stub = _api(stub=StubBybit(secret="OTHER"))
    r = bad_api.get(bf.P_POS)
    ok = stub.sig_failures == 0 and ts == want and len(ts) == 64 and bad_stub.sig_failures == 1 and r.get("code") == 10004 and "signature" in r.get("msg", "")
    return ok, f"verified requests={len(stub.calls)} sig_failures={stub.sig_failures} manual HMAC equal={ts == want} wrong secret -> {r.get('code')} {r.get('msg')[:40]}"


def symbols_and_tickers_map_both_ways():
    api, stub = _api()
    r = api.public(bf.P_TICK)
    rows = {x["symbol"]: x for x in r["data"]}
    one = api.public(bf.P_TICK, {"symbol": "BTC-USDT"})["data"]
    ok = (bf.to_bx("BTCUSDT") == "BTC-USDT" and bf.to_bx("1000PEPEUSDT") == "1000PEPE-USDT" and bf.to_by("1000PEPE-USDT") == "1000PEPEUSDT"
          and bf.to_bx("BTCPERP") == "BTCPERP" and bf.to_bx("BTC-26DEC26") == "BTC-26DEC26"
          and sorted(rows) == ["1000PEPE-USDT", "BTC-USDT"] and abs(float(rows["BTC-USDT"]["priceChangePercent"]) - 5.0) < 1e-9
          and rows["BTC-USDT"]["quoteVolume"] == "123456.5" and rows["BTC-USDT"]["markPrice"] == "100.1" and len(one) == 1
          and any(p.get("symbol") == "BTCUSDT" for m, pa, p in stub.calls if pa == "/v5/market/tickers"))
    return ok, f"tickers={sorted(rows)} change%={rows['BTC-USDT']['priceChangePercent']} (USDC perp and dated future dropped)"


def contracts_follow_the_instrument_filters():
    rows = bf.contract_rows(INSTRUMENTS)
    by = {c["symbol"]: c for c in rows}
    btc, pepe = by["BTC-USDT"], by["1000PEPE-USDT"]
    api, stub = _api(stub=StubBybit(page=2))
    paged = bf.fetch_contract_rows(bf.BASE, transport=stub)
    api.load_instruments()
    ok = (sorted(by) == ["1000PEPE-USDT", "BTC-USDT"] and btc["qprec"] == 3 and btc["pprec"] == 1 and btc["step"] == 0.001 and btc["min_usdt"] == 5.0
          and btc["max_lev"] == 100 and pepe["step"] == 10.0 and pepe["qprec"] == 0 and pepe["pprec"] == 7 and pepe["min_qty"] == 10.0
          and sorted(c["symbol"] for c in paged) == ["1000PEPE-USDT", "BTC-USDT"] and sorted(api.inst) == ["1000PEPE-USDT", "BTC-USDT"])
    return ok, f"BTC step={btc['step']} qprec={btc['qprec']} pprec={btc['pprec']} minNotional={btc['min_usdt']} maxLev={btc['max_lev']}; PEPE step={pepe['step']} (not 10^-qprec); paged load over {len(stub.sent('/v5/market/instruments-info', 'GET'))} pages"


def klines_come_oldest_first_in_the_shape_the_trader_parses():
    api, stub = _api()
    r = api.public(bf.P_KL2, {"symbol": "BTC-USDT", "interval": "5m", "limit": "3", "endTime": "1700000600000"})
    times = [x["time"] for x in r["data"]]
    p = pt.Pulse.__new__(pt.Pulse)
    bars = p._parse_klines(r["data"])
    q = [pa for m, path, pa in stub.calls if path == "/v5/market/kline"][-1]
    ok = times == sorted(times) and len(times) == 3 and bars[0] == [100.0, 101.0, 99.0, 100.5, 7.0] and q["interval"] == "5" and q["end"] == "1700000600000" and q["symbol"] == "BTCUSDT" and q["limit"] == "3"
    hr = api.public(bf.P_KL2, {"symbol": "ETH-USDT", "interval": "1h", "limit": "2"})
    ok = ok and [pa for m, path, pa in stub.calls if path == "/v5/market/kline"][-1]["interval"] == "60"
    return ok, f"times ascending={times == sorted(times)} parsed bar={bars[0]} interval 5m->{q['interval']} endTime->end"


def balance_and_positions_come_back_in_the_bingx_shape():
    api, stub = _api()
    b = api.get(bf.P_BAL3)["data"]["balance"]
    stub.balance_unified_ok = False
    b2 = api.get(bf.P_BAL2)["data"]["balance"]
    stub.hedge = True
    stub.pos[("BTCUSDT", 1)] = {"size": Decimal("0.4"), "avg": 99.5, "lev": 20}
    stub.pos[("BTCUSDT", 2)] = {"size": Decimal("0.2"), "avg": 100.5, "lev": 20}
    stub.pos[("1000PEPEUSDT", 1)] = {"size": Decimal(0), "avg": 0.0}
    rows = api.get(bf.P_POS)["data"]
    by = {(r["symbol"], r["positionSide"]): r for r in rows}
    ok = (b["equity"] == "101.5" and b["availableMargin"] == "88.25" and b["usedMargin"] == "13.25" and b["unrealizedProfit"] == "1.5"
          and b2["equity"] == "101.5" and len(rows) == 2 and float(by[("BTC-USDT", "LONG")]["positionAmt"]) == 0.4 and float(by[("BTC-USDT", "SHORT")]["positionAmt"]) == -0.2
          and by[("BTC-USDT", "SHORT")]["availableAmt"] == "0.2" and by[("BTC-USDT", "LONG")]["avgPrice"] == "99.5" and by[("BTC-USDT", "LONG")]["leverage"] == "20"
          and by[("BTC-USDT", "SHORT")]["positionId"] == "BTCUSDT:2")
    return ok, f"balance equity={b['equity']} available={b['availableMargin']} (CONTRACT fallback ok={b2['equity']}); positions={sorted(by)} short amt={by[('BTC-USDT', 'SHORT')]['positionAmt']}"


def entry_order_body_and_fill_report():
    api, stub = _api()
    cid = _cid("o")
    r = api.post(bf.P_ORDER, {"symbol": "BTC-USDT", "type": "MARKET", "side": "BUY", "positionSide": "LONG", "quantity": 0.30000000000000004, "clientOrderID": cid})
    o = r["data"]["order"]
    req = stub.sent("/v5/order/create")[-1]
    stub.exec_lag = 2
    r2 = api.post(bf.P_ORDER, {"symbol": "1000PEPE-USDT", "type": "MARKET", "side": "SELL", "positionSide": "SHORT", "quantity": 37, "clientOrderID": _cid("o")})
    req2 = stub.sent("/v5/order/create")[-1]
    real_sleep = bf.time.sleep
    ok = (r["code"] == 0 and req["positionIdx"] == 1 and req["orderType"] == "Market" and req["side"] == "Buy" and req["qty"] == "0.3" and req["symbol"] == "BTCUSDT"
          and "reduceOnly" not in req and req["orderLinkId"] == cid and float(o["avgPrice"]) == 100.0 and o["executedQty"] == "0.3" and o["orderId"].startswith("oid")
          and pt.filled_qty(o, 1.0) == 0.3 and r2["code"] == 0 and req2["positionIdx"] == 2 and req2["side"] == "Sell" and req2["qty"] == "40"
          and r2["data"]["order"]["executedQty"] == "40" and stub.hedge and stub.mode_calls == 1)
    return ok, f"entry qty {req['qty']} (float noise removed) idx={req['positionIdx']} avg={o['avgPrice']} executed={o['executedQty']}; PEPE qty 37 -> {req2['qty']} on step 10; late executions retried; hedge mode set once"


def protective_batch_is_conditional_reduce_only_orders():
    api, stub = _api()
    stub.hedge = True
    stub.pos[("BTCUSDT", 1)] = {"size": Decimal("0.4"), "avg": 100.0}
    stub.pos[("BTCUSDT", 2)] = {"size": Decimal("0.4"), "avg": 100.0}
    out = {}
    for side, slp, tpp in (("LONG", "98.26", "103.04"), ("SHORT", "102.04", "96.96")):
        sl = pt.ctrl_payload("BTC-USDT", side, "sl", slp, "0.4", _cid("u"), close_pos=True)
        tp = pt.ctrl_payload("BTC-USDT", side, "tp", tpp, "0.4", _cid("v"), close_pos=True)
        r = api.batch_place([sl, tp])
        reqs = stub.sent("/v5/order/create-batch")[-1]["request"]
        out[side] = (r, reqs)
    rl, ql = out["LONG"]
    rs, qs = out["SHORT"]
    rows = rl["data"]["orders"]
    ok = (rl["code"] == 0 and [x["type"] for x in rows] == ["STOP_MARKET", "TAKE_PROFIT_MARKET"] and all(x["code"] == 0 and x["orderId"] for x in rows)
          and ql[0]["triggerPrice"] == "98.5" and ql[1]["triggerPrice"] == "103" and ql[0]["triggerDirection"] == 2 and ql[1]["triggerDirection"] == 1
          and all(q["reduceOnly"] is True and q["closeOnTrigger"] is True and q["qty"] == "0" and q["orderType"] == "Market" and q["triggerBy"] == "MarkPrice"
                  and q["positionIdx"] == 1 and q["side"] == "Sell" for q in ql)
          and qs[0]["triggerDirection"] == 1 and qs[1]["triggerDirection"] == 2 and all(q["positionIdx"] == 2 and q["side"] == "Buy" for q in qs)
          and rows[0]["clientOrderID"].startswith("Gx01u") and rows[1]["clientOrderID"].startswith("Gx01v"))
    return ok, f"LONG SL dir={ql[0]['triggerDirection']} trig={ql[0]['triggerPrice']} (tick 0.5), TP dir={ql[1]['triggerDirection']}; SHORT SL dir={qs[0]['triggerDirection']} TP dir={qs[1]['triggerDirection']}; reduceOnly+closeOnTrigger qty 0 idx 1/2"


def batch_reports_each_item_and_single_forms_fall_back():
    api, stub = _api()
    stub.hedge = True
    stub.pos[("BTCUSDT", 1)] = {"size": Decimal("0.4"), "avg": 100.0}
    sl = pt.ctrl_payload("BTC-USDT", "LONG", "sl", "98.5", "0.4", _cid("u"), close_pos=True)
    tp = pt.ctrl_payload("BTC-USDT", "LONG", "tp", "99.0", "0.4", _cid("v"), close_pos=True)        # a take profit under the market: refused
    r = api.batch_place([sl, tp])
    rows = r["data"]["orders"]
    # the trader's single forms: explicit quantity instead of closePosition
    single = pt.ctrl_payload("BTC-USDT", "LONG", "tp", "103.0", "0.4", _cid("t"), close_pos=False, with_qty=True)
    one = api.post(bf.P_ORDER, single)
    req = stub.sent("/v5/order/create")[-1]
    ok = (r["code"] == 0 and rows[0]["code"] == 0 and rows[0]["orderId"] and rows[1]["code"] == 110092 and not rows[1]["orderId"]
          and pt.ctrl_err_kind(rows[1]["msg"]) == "px" and one["code"] == 0 and req["qty"] == "0.4" and req["reduceOnly"] is True and req["closeOnTrigger"] is True
          and pt.extract_oid(one) == one["data"]["order"]["orderId"])
    return ok, f"item codes={[x['code'] for x in rows]} ('{rows[1]['msg'][:60]}' -> ctrl_err_kind {pt.ctrl_err_kind(rows[1]['msg'])}); explicit-qty single form qty={req['qty']}"


def stop_moves_by_amending_the_trigger_in_place():
    api, stub = _api()
    stub.hedge = True
    stub.pos[("BTCUSDT", 1)] = {"size": Decimal("0.4"), "avg": 100.0}
    sl = pt.ctrl_payload("BTC-USDT", "LONG", "sl", "98.5", "0.4", _cid("u"), close_pos=True)
    oid = api.batch_place([sl])["data"]["orders"][0]["orderId"]
    body = pt.ctrl_payload("BTC-USDT", "LONG", "sl", "99.5", "0.4", _cid("u"), close_pos=True)
    body["cancelOrderId"] = oid
    r = api.cancel_replace(body)
    amended = stub.sent("/v5/order/amend")[-1]
    body["stopPrice"], body["cancelOrderId"] = "101", oid
    wrong = api.cancel_replace(body)                      # a stop above the market of a falling trigger
    body["stopPrice"], body["cancelOrderId"] = "99", "nope"
    gone = api.cancel_replace(body)
    ok = (r["code"] == 0 and pt.extract_oid(r) == oid and amended["orderId"] == oid and amended["triggerPrice"] == "99.5" and stub.orders[oid]["triggerPrice"] == "99.5"
          and wrong["code"] == 110093 and pt.ctrl_err_kind(wrong["msg"]) == "px" and gone["code"] == 110001 and "order not exist" in gone["msg"])
    return ok, f"amend keeps order id {oid} trigger->{stub.orders[oid]['triggerPrice']}; wrong side -> {wrong['code']} ({pt.ctrl_err_kind(wrong['msg'])}); unknown id -> {gone['code']}"


def open_orders_are_typed_by_trigger_direction_and_paged():
    api, stub = _api(stub=StubBybit(page=2))
    stub.hedge = True
    stub.pos[("BTCUSDT", 1)] = {"size": Decimal("0.4"), "avg": 100.0}
    stub.pos[("BTCUSDT", 2)] = {"size": Decimal("0.4"), "avg": 100.0}
    for side, slp, tpp in (("LONG", "98.5", "103"), ("SHORT", "102", "97")):
        api.batch_place([pt.ctrl_payload("BTC-USDT", side, "sl", slp, "0.4", _cid("u"), close_pos=True), pt.ctrl_payload("BTC-USDT", side, "tp", tpp, "0.4", _cid("v"), close_pos=True)])
    rows = api.get(bf.P_OPEN)["data"]["orders"]
    by = {(r["positionSide"], r["type"]): r for r in rows}
    pages = len(stub.sent("/v5/order/realtime", "GET"))
    one = api.get(bf.P_OPEN, {"symbol": "BTC-USDT"})["data"]["orders"]
    ok = (len(rows) == 4 and sorted(by) == [("LONG", "STOP_MARKET"), ("LONG", "TAKE_PROFIT_MARKET"), ("SHORT", "STOP_MARKET"), ("SHORT", "TAKE_PROFIT_MARKET")]
          and by[("LONG", "STOP_MARKET")]["stopPrice"] == "98.5" and by[("SHORT", "TAKE_PROFIT_MARKET")]["stopPrice"] == "97" and all(r["symbol"] == "BTC-USDT" and r["closePosition"] == "true" for r in rows)
          and all(r["clientOrderID"].startswith("Gx01") and r["status"] == "NEW" for r in rows) and pages >= 2 and len(one) == 4
          and stub.sent("/v5/order/realtime", "GET")[0].get("settleCoin") == "USDT")
    return ok, f"{len(rows)} conditional orders over {pages} pages typed {sorted(by)}"


def cancel_and_status_lookup_report_like_bingx():
    api, stub = _api()
    stub.hedge = True
    stub.pos[("BTCUSDT", 1)] = {"size": Decimal("0.4"), "avg": 100.0}
    ids = [x["orderId"] for x in api.batch_place([pt.ctrl_payload("BTC-USDT", "LONG", "sl", "98.5", "0.4", _cid("u"), close_pos=True), pt.ctrl_payload("BTC-USDT", "LONG", "tp", "103", "0.4", _cid("v"), close_pos=True)])["data"]["orders"]]
    live = api.get(bf.P_ORDER, {"symbol": "BTC-USDT", "orderId": ids[0]})["data"]["order"]
    c = api.delete(bf.P_ORDER, {"symbol": "BTC-USDT", "orderId": ids[0]})
    c2 = api.delete(bf.P_ORDER, {"symbol": "BTC-USDT", "orderId": ids[0]})
    c3 = api.delete(bf.P_ORDER, {"symbol": "BTC-USDT", "orderId": "ghost"})
    dead = api.get(bf.P_ORDER, {"symbol": "BTC-USDT", "orderId": ids[0]})["data"]["order"]
    miss = api.get(bf.P_ORDER, {"symbol": "BTC-USDT", "orderId": "ghost"})
    ok = (live["status"] == "NEW" and live["type"] == "STOP_MARKET" and c["code"] == 0 and pt.real_oid(pt.extract_oid(c)) == ids[0]
          and c2["code"] == 110008 and "order not exist" in c2["msg"] and c3["code"] == 110001 and dead["status"] == "CANCELED" and miss["code"] == 80016)
    return ok, f"open={live['status']} cancel={c['code']} again={c2['code']} unknown={c3['code']} then={dead['status']}; missing order -> {miss['code']}"


def bybit_errors_carry_the_words_the_trader_reads():
    api, stub = _api()
    cases = [
        ({"retCode": 110007, "retMsg": "ab not enough for new order"}, "insufficient margin", None),
        ({"retCode": 110012, "retMsg": "Insufficient available balance"}, "insufficient margin", None),
        ({"retCode": 110017, "retMsg": "current position is zero, cannot fix reduce-only order qty"}, "position not exist", "flat"),
        ({"retCode": 110092, "retMsg": "expect Rising, but trigger_price[1] <= current[2]"}, "trigger price", "px"),
        ({"retCode": 110020, "retMsg": "Active orders more than 500"}, "exceeded", "cap"),
        ({"retCode": 110072, "retMsg": "OrderLinkedID is duplicate"}, "already exists", "exists"),
        ({"retCode": 110094, "retMsg": "Order does not meet minimum order value 5USDT"}, "order size", None),
        ({"retCode": 110001, "retMsg": "order not exists or too late to cancel"}, "order not exist", None),
        ({"retCode": 10004, "retMsg": "error sign!"}, "signature", None),
        ({"retCode": 10006, "retMsg": "Too many visits. Exceeded the API Rate Limit."}, "rate limit", None),
    ]
    bad = []
    for e, word, kind in cases:
        f = api.fail({**e, "_status": 200})
        if word not in f["msg"].lower() or (kind and pt.ctrl_err_kind(f["msg"]) != kind):
            bad.append((e["retCode"], f["msg"][:60]))
    t = pt.is_transient_api(api.fail({"retCode": 110007, "retMsg": "x", "_status": 200})["msg"]) and pt.is_transient_api(api.fail({"retCode": 110017, "retMsg": "current position is zero", "_status": 200})["msg"])
    rl = api.fail({"retCode": 10006, "retMsg": "Too many visits", "_status": 200})
    cooled = api.fail({"_cooled": True})
    tr = api.fail({"_transport": True, "retMsg": "timed out"})
    ok = not bad and t and rl["code"] == 100410 and cooled.get("cooled") is True and cooled["code"] == 101209 and tr["code"] == -1 and tr.get("error") is True
    return ok, f"{len(cases)} codes mapped to trader words, mismatches={bad}; rate limit -> {rl['code']}; transport -> error"


def leverage_and_account_modes_are_set_once():
    api, stub = _api()
    api.load_instruments()
    g0 = api.get(bf.P_LEV, {"symbol": "BTC-USDT"})["data"]
    r1 = api.post(bf.P_LEV, {"symbol": "BTC-USDT", "side": "LONG", "leverage": 100})
    r2 = api.post(bf.P_LEV, {"symbol": "BTC-USDT", "side": "SHORT", "leverage": 100})       # the venue answers 110043: not modified
    m1 = api.post(bf.P_MARGIN, {"symbol": "BTC-USDT", "marginType": "CROSSED"})
    m2 = api.post(bf.P_MARGIN, {"symbol": "BTC-USDT", "marginType": "CROSSED"})
    stub.hedge = True
    stub.pos[("BTCUSDT", 1)] = {"size": Decimal("0.4"), "avg": 100.0, "lev": 100}
    g1 = api.get(bf.P_LEV, {"symbol": "BTC-USDT"})["data"]
    parsed = pt.Pulse.parse_lev_payload(None, g1)
    sets = stub.sent("/v5/position/set-leverage")
    ok = (g0["maxLongLeverage"] == 100 and r1["code"] == 0 and r2["code"] == 0 and len(sets) == 2 and sets[0] == {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "100", "sellLeverage": "100"}
          and m1["code"] == 0 and m2["code"] == 0 and stub.margin_calls == 1 and stub.mode_calls == 1 and parsed[0] == 100 and parsed[1] == 100)
    return ok, f"max={g0['maxLongLeverage']}, both sides set in one request each ({len(sets)} posts, 110043 = ok), margin mode set once ({stub.margin_calls}), hedge once ({stub.mode_calls}); read back {parsed}"


def price_stream_merges_snapshots_and_deltas():
    st = {}
    snap = {"topic": "tickers.BTCUSDT", "type": "snapshot", "data": {"symbol": "BTCUSDT", "lastPrice": "66666.6", "markPrice": "66660", "price24hPcnt": "-0.158315"}, "ts": 1}
    delta = {"topic": "tickers.BTCUSDT", "type": "delta", "data": {"symbol": "BTCUSDT", "markPrice": "66661"}, "ts": 2}
    delta2 = {"topic": "tickers.BTCUSDT", "type": "delta", "data": {"symbol": "BTCUSDT", "lastPrice": "66670"}, "ts": 3}
    markonly = {"topic": "tickers.ETHUSDT", "type": "snapshot", "data": {"symbol": "ETHUSDT", "markPrice": "3000"}, "ts": 4}
    a, b, c, d = (bf.apply_ticker_message(st, m) for m in (snap, delta, delta2, markonly))
    junk = [bf.apply_ticker_message(st, x) for x in ({"op": "pong"}, {"topic": "tickers.BTCPERP", "type": "snapshot", "data": {"symbol": "BTCPERP", "lastPrice": "1"}}, {"success": True}, None)]
    ok = (a[:2] == ("BTC-USDT", 66666.6) and abs(a[2] + 15.8315) < 1e-9 and b[1] == 66666.6 and c[1] == 66670.0 and abs(c[2] + 15.8315) < 1e-9 and d == ("ETH-USDT", 3000.0, 0.0) and junk == [None] * 4)
    return ok, f"snapshot={a} delta without lastPrice keeps {b[1]}; delta with it -> {c[1]}; mark-only ticker -> {d[1]}; control frames and USDC perps ignored"


class _FakeWS:
    def __init__(self, hub, frames):
        self.hub, self.frames, self.sent, self.closed = hub, list(frames), [], False

    def settimeout(self, t):
        pass

    def send(self, s):
        self.sent.append(json.loads(s))

    def recv(self):
        if not self.frames:
            self.hub.stop = True
            return None
        return json.dumps(self.frames.pop(0))

    def close(self):
        self.closed = True


def price_hub_subscribes_in_shards_and_pings():
    ticks = []
    hub = bf.BybitPriceHub(lambda s, px, ch: ticks.append((s, px, ch)), ErrorLog(os.path.join(_STATE_DIR, "by-ws.jsonl")))
    hub.symbols = [f"S{i:03d}-USDT" for i in range(200)]
    shards = hub.shards()
    hub.symbols = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    ws = _FakeWS(hub, [{"topic": "tickers.BTCUSDT", "type": "snapshot", "data": {"symbol": "BTCUSDT", "lastPrice": "100", "price24hPcnt": "0.01"}}, {"op": "pong", "ret_msg": "pong"}])
    saved = bf._ws
    bf._ws = types.SimpleNamespace(create_connection=lambda *a, **k: ws)
    try:
        hub._session(0)
    finally:
        bf._ws = saved
    subs = [m for m in ws.sent if m["op"] == "subscribe"]
    pings = [m for m in ws.sent if m["op"] == "ping"]
    topics = sorted(t for m in subs for t in m["args"])
    ok = (len(shards) == 3 and max(len(x) for x in shards) <= bf.WS_ARGS_PER_CONN and sum(len(x) for x in shards) == 200
          and topics == ["tickers.BTCUSDT", "tickers.ETHUSDT", "tickers.SOLUSDT"] and pings and ticks == [("BTC-USDT", 100.0, 1.0)] and ws.closed and hub.n == 1)
    return ok, f"200 symbols -> {[len(x) for x in shards]} per connection; subscribed {topics}; ping sent={bool(pings)}; tick {ticks}"


def positions_and_orders_lifecycle_through_the_live_trader():
    """One entry, its protective pair, a venue-side take profit and a market close, with the trader's own code on the stub."""
    sym = "BTC-USDT"
    saved = (pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH)
    pt.dump_cts_settings = lambda: {}
    try:
        api, stub = _api()
        p = pt.Pulse(api, _contracts())
        p.boot_ts = 0.0
        p.px[sym] = 100.0
        p.last_px[sym] = 100.0
        p.available = 50.0
        p.halted = False
        p.entry_sense = lambda *a, **k: None
        p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
        pos = p.open.get(sym)
        entry = stub.sent("/v5/order/create")[0]
        batch = stub.sent("/v5/order/create-batch")
        conds = [o for o in stub.orders.values() if o.get("triggerPrice")]
        held = stub.pos.get(("BTCUSDT", 1), {}).get("size", Decimal(0))
        ok_open = (pos is not None and float(pos.qty) == float(held) and entry["positionIdx"] == 1 and entry["side"] == "Buy" and entry["orderLinkId"].startswith(pt.TAG)
                   and len(batch) == 1 and len(conds) == 2 and pt.real_oid(pos.sl_oid) and pt.real_oid(pos.tp_oid) and pos.controls_ok
                   and stub.hedge and sorted(float(o["triggerPrice"]) for o in conds)[0] < 100.0 < sorted(float(o["triggerPrice"]) for o in conds)[1])
        flat_before = p._exchange_flat(pos)
        tp_px = max(float(o["triggerPrice"]) for o in conds)
        stub.move("BTCUSDT", tp_px + 0.5)                       # the take profit fires on the venue
        flat_after = p._exchange_flat(pos)
        ve = p.venue_exit(pos)
        ok_tp = (not flat_before) and flat_after and ve is not None and ve[0] == "tp" and abs(ve[1] - tp_px) < 1e-9
        # a second position closed by the trader itself
        stub.mid["BTCUSDT"] = 100.0
        p.cooldown.clear()
        p.open.clear()
        p.place(sym, 1, "ind:state:tf_combined:0.60:a2:src", 0.7)
        pos2 = p.open.get(sym)
        done, px = p.market_close(pos2)
        close_req = stub.sent("/v5/order/create")[-1]
        held2 = stub.pos[("BTCUSDT", 1)]["size"]
        ok_close = (pos2 is not None and done and close_req["side"] == "Sell" and close_req["reduceOnly"] is True and close_req["positionIdx"] == 1 and held2 == 0 and abs(px - 100.0) < 1e-9)
    finally:
        pt.BLOCK_PATH, pt.LEV_PATH, pt.OPEN_PATH, pt.dump_cts_settings, pt.TRADES_PATH = saved
    return ok_open and ok_tp and ok_close, (f"open: qty={getattr(pos, 'qty', None)} venue={held} sl/tp oids={getattr(pos, 'sl_oid', '')}/{getattr(pos, 'tp_oid', '')} ({ok_open}); "
                                            f"venue take profit -> venue_exit={ve} flat before/after={flat_before}/{flat_after} ({ok_tp}); market close reduceOnly idx {close_req['positionIdx']} -> venue flat ({ok_close})")


def own_closing_fills_are_synced_with_their_pnl():
    api, stub = _api()
    stub.hedge = True
    stub.pos[("BTCUSDT", 1)] = {"size": Decimal("0.4"), "avg": 100.0}
    stub.mid["BTCUSDT"] = 103.0
    r = api.post(bf.P_ORDER, {"symbol": "BTC-USDT", "type": "MARKET", "side": "SELL", "positionSide": "LONG", "closePosition": "true", "clientOrderID": _cid("c")})
    rows = api.get(bf.P_ALL, {"limit": 50})["data"]["orders"]
    fills = api.get(bf.P_FILLS, {"pageIndex": 1, "pageSize": 50})["data"]["fill_orders"]
    closed = [x for x in rows if x["clientOrderID"].startswith("Gx01c")]
    req = stub.sent("/v5/order/create")[-1]
    ok = (r["code"] == 0 and req["qty"] == "0" and req["reduceOnly"] is True and req["closeOnTrigger"] is True and len(closed) == 1 and closed[0]["status"] == "FILLED"
          and abs(float(closed[0]["profit"]) - 1.2) < 1e-6 and float(closed[0]["executedQty"]) == 0.4 and float(closed[0]["avgPrice"]) == 103.0 and fills and fills[0]["status"] == "FILLED")
    return ok, f"closePosition market -> qty {req['qty']} reduceOnly; allOrders row profit={closed[0]['profit'] if closed else None} executed={closed[0]['executedQty'] if closed else None}; fills={len(fills)}"

def _order(sym="BTC-USDT", side="BUY", ps="LONG", qty="0.1", cid=None, **kw):
    return {"symbol": sym, "type": "MARKET", "side": side, "positionSide": ps, "quantity": qty, "clientOrderID": cid or _cid("o"), **kw}


def _ctrl(kind, price, ps="LONG", sym="BTC-USDT"):
    return pt.ctrl_payload(sym, ps, "sl" if kind == "sl" else "tp", price, "0.4", _cid("u" if kind == "sl" else "v"), close_pos=True)


def _held(stub, size="0.4"):
    stub.hedge = True
    stub.pos[("BTCUSDT", 1)] = {"size": Decimal(size), "avg": 100.0}
    stub.pos[("BTCUSDT", 2)] = {"size": Decimal(size), "avg": 100.0}
    stub.pos[("1000PEPEUSDT", 1)] = {"size": Decimal("100"), "avg": 0.001}


def rate_limit_headers_drive_the_endpoint_gate():
    api, stub, clk = _api_clock()
    api.get(bf.P_POS)                                                    # warm: nothing limited yet
    n0 = len(clk.slept)
    reset = clk.now() + 0.8
    stub.headers = {"x-bapi-limit": "10", "x-bapi-limit-status": "0", "x-bapi-limit-reset-timestamp": str(int(reset * 1000))}
    api.get(bf.P_POS)                                                    # this reply says: nothing left in the window
    stub.headers = {}
    ep = api.limiter.endpoint("/v5/position/list")
    api.get(bf.P_POS)                                                    # must wait for the reset before it goes
    waited = clk.slept[n0:]
    other = len(clk.slept)
    api.get(bf.P_BAL3)                                                   # another endpoint is not held back
    free = len(clk.slept) == other
    stub.headers = {"x-bapi-limit": "20", "x-bapi-limit-status": "19", "x-bapi-limit-reset-timestamp": str(int(clk.now() * 1000))}
    api.get(bf.P_POS)
    ok = bool(waited) and 0.75 <= max(waited) <= 1.6 and free and ep.limit == 20.0 and ep.bucket.rate == 20.0
    return ok, f"after X-Bapi-Limit-Status 0 the next call on that endpoint waited {max(waited) if waited else 0}s (reset in 0.8s); another endpoint waited: {not free}; the venue's limit 20 replaced the local guess ({ep.bucket.rate})"


def rate_limit_error_backs_off_to_the_reset_and_retries_once():
    api, stub, clk = _api_clock()
    api.post(bf.P_ORDER, _order())                                       # account set up
    reset = clk.now() + 2.0
    stub.script["/v5/order/create"] = [env(10006, "Too many visits. Exceeded the API Rate Limit.")]
    stub.headers = {}
    stub.rate_limit_next = (200, {"x-bapi-limit-reset-timestamp": str(int(reset * 1000))}, env(10006, "Too many visits. Exceeded the API Rate Limit."))
    stub.script.clear()
    n_pos = len(stub.sent("/v5/order/create"))
    t0 = clk.now()
    r = api.post(bf.P_ORDER, _order())                                   # 10006, then the pause to the reset, then the resend
    paused = clk.now() - t0
    # a second consecutive refusal is returned as the BingX rate-limit code and the path rests
    stub.rate_limit_next = (200, {"x-bapi-limit-reset-timestamp": str(int((clk.now() + 5) * 1000))}, env(10006, "Too many visits."))
    r2 = api.post(bf.P_ORDER, _order())
    calls = len(stub.calls)
    r3 = api.post(bf.P_ORDER, _order())                                  # during the cooldown nothing is sent
    sent_during = len(stub.calls) - calls
    cd = api.path_cd.get(bf.P_ORDER, 0) - clk.now()
    clk.t += 6.0
    r4 = api.post(bf.P_ORDER, _order())
    ok = (r["code"] == 0 and 2.0 <= paused <= 2.6 and r2["code"] in (100410, 101209) and r3.get("cooled") is True and sent_during == 0 and cd > 1.0 and r4["code"] == 0)
    return ok, f"10006 -> paused {paused:.2f}s to the reset timestamp and the order went through ({r['code']}); a second refusal -> {r2['code']}, path rests {cd:.1f}s with {sent_during} requests sent; after the reset -> {r4['code']}"


def ip_ban_stops_every_request_for_ten_minutes():
    api, stub, clk = _api_clock()
    api.get(bf.P_POS)
    stub.raw["/v5/position/list"] = [(403, b"<html>access too frequent</html>")]
    r = api.get(bf.P_POS)
    n = len(stub.calls)
    outs = [api.get(bf.P_BAL3), api.public(bf.P_TICK), api.post(bf.P_ORDER, _order()), api.public(bf.P_KL2, {"symbol": "BTC-USDT", "interval": "1m", "limit": "5"})]
    held = len(stub.calls) - n
    clk.t += 599
    still = api.get(bf.P_POS).get("cooled") is True
    clk.t += 3
    back = api.get(bf.P_POS)
    ok = (r["code"] == 100410 and "ten minutes" in r["msg"] and all(o.get("cooled") for o in outs) and held == 0 and still and back["code"] == 0 and 599 < api.limiter.ip_ban_until - (clk.now() - 602) <= 601)
    return ok, f"HTTP 403 -> {r['code']} ({'ten minutes' in r['msg']}); {held} requests sent during the ban (reads, public and orders all cooled); at 599 s still cooled={still}; after 602 s -> {back['code']}; pace {bf.__dict__.get('IP_RATE', None) or 'bybit_limits.IP_RATE'}"


def local_pace_stays_far_below_the_ip_limit():
    import bybit_limits as bl
    ok = bl.IP_RATE[0] * 5 <= 600 * 0.7 and bl.ENDPOINTS["/v5/order/create"][0] <= 10 and bl.ENDPOINTS["/v5/order/create-batch"][0] <= 10 and bl.ENDPOINTS["/v5/position/set-leverage"][0] <= 10 and bl.ENDPOINTS["/v5/position/list"][0] <= 50 and bl.IP_BAN_S == 600.0
    return ok, f"IP pace {bl.IP_RATE[0]:.0f}/s = {bl.IP_RATE[0] * 5:.0f} per 5 s (venue 600); order endpoints {bl.ENDPOINTS['/v5/order/create'][0]:.0f}/s, reads {bl.ENDPOINTS['/v5/position/list'][0]:.0f}/s"


def batches_are_chunked_and_retry_only_the_failed_items():
    api, stub, clk = _api_clock()
    _held(stub)
    api.post(bf.P_ORDER, _order())                                       # account set up
    orders = [_ctrl("sl", "98.5"), _ctrl("tp", "103"), _ctrl("sl", "98.0", sym="1000PEPE-USDT"), _ctrl("tp", "103", ps="SHORT"), ]
    orders[2] = pt.ctrl_payload("1000PEPE-USDT", "LONG", "sl", "0.0009", "10", _cid("u"), close_pos=True)
    orders[3] = pt.ctrl_payload("BTC-USDT", "SHORT", "tp", "97", "0.4", _cid("v"), close_pos=True)
    # item 1: a rate limit inside the batch, item 2: a refusal that must not be retried, item 3: a transient server error
    stub.item_script["/v5/order/create-batch"] = [{1: (10006, "Too many visits"), 2: (110092, "expect Rising, but trigger_price[1] <= current[2]"), 3: (10016, "Server error")}]
    n = len(stub.sent("/v5/order/create-batch"))
    r = api.batch_place(orders)
    calls = stub.sent("/v5/order/create-batch")[n:]
    rows = r["data"]["orders"]
    sizes = [len(c["request"]) for c in calls]
    retried = [x["symbol"] + ":" + x.get("triggerDirection", "").__str__() for x in calls[1]["request"]] if len(calls) > 1 else []
    # 25 orders go out in calls of 10, 10 and 5
    stub.mid["BTCUSDT"] = 100.0
    many = [pt.ctrl_payload("BTC-USDT", "LONG", "sl", "98.5", "0.4", _cid("u"), close_pos=True) for _ in range(25)]
    stub.hedge = True
    n2 = len(stub.sent("/v5/order/create-batch"))
    r2 = api.batch_place(many)
    sizes2 = [len(c["request"]) for c in stub.sent("/v5/order/create-batch")[n2:]]
    ok = (sizes == [4, 2] and [x["code"] for x in rows] == [0, 0, 110092, 0] and rows[2]["orderId"] == "" and pt.ctrl_err_kind(rows[2]["msg"]) == "px"
          and all(x["orderId"] for i, x in enumerate(rows) if i != 2) and len(retried) == 2 and sizes2 == [10, 10, 5] and len(r2["data"]["orders"]) == 25)
    return ok, f"4 orders -> calls of {sizes}: only the rate-limited and the server-error items were sent again; the refused item stays refused ({rows[2]['code']}); codes {[x['code'] for x in rows]}; 25 orders -> calls of {sizes2}"


def batch_whole_call_failure_resolves_items_by_link_id():
    api, stub, clk = _api_clock()
    _held(stub)
    api.post(bf.P_ORDER, _order())
    orders = [_ctrl("sl", "98.5"), _ctrl("tp", "103")]
    stub.lose["/v5/order/create-batch"] = 1                              # the venue places both, the reply is lost
    n = len(stub.sent("/v5/order/create-batch"))
    r = api.batch_place(orders)
    calls = len(stub.sent("/v5/order/create-batch")) - n
    conds = [o for o in stub.orders.values() if o.get("triggerPrice") and o["orderStatus"] == "Untriggered"]
    rows = r["data"]["orders"]
    ok = calls == 1 and len(conds) == 2 and all(x["code"] == 0 and x["orderId"] for x in rows) and sorted(x["orderId"] for x in rows) == sorted(o["orderId"] for o in conds)
    return ok, f"reply lost after both orders were placed: {calls} create-batch call, venue holds {len(conds)} orders (no second pair), both items resolved to their order ids by orderLinkId"


def retry_after_a_lost_reply_never_opens_a_second_position():
    api, stub, clk = _api_clock()
    api.post(bf.P_ORDER, _order(qty="0.1"))                              # account set up, position 0.1
    base = stub.pos[("BTCUSDT", 1)]["size"]
    cid = _cid("o")
    stub.lose["/v5/order/create"] = 1                                    # placed and filled on the venue, the answer never arrives
    r = api.post(bf.P_ORDER, _order(qty="0.2", cid=cid))
    after_lost = stub.pos[("BTCUSDT", 1)]["size"]
    creates = [d for d in stub.sent("/v5/order/create") if d.get("orderLinkId") == cid]
    # a server error that did not place anything: the retry places it once
    cid2 = _cid("o")
    stub.script["/v5/order/create"] = [env(10016, "Server error")]
    r2 = api.post(bf.P_ORDER, _order(qty="0.3", cid=cid2))
    after_retry = stub.pos[("BTCUSDT", 1)]["size"]
    # a duplicate-link refusal on a resend resolves to the order that exists
    cid3 = _cid("o")
    r3 = api.post(bf.P_ORDER, _order(qty="0.1", cid=cid3))
    stub.script["/v5/order/create"] = []
    dup = api._create({"category": "linear", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "qty": "0.1", "positionIdx": 1, "orderLinkId": cid3})
    after_dup = stub.pos[("BTCUSDT", 1)]["size"]
    ok = (r["code"] == 0 and after_lost - base == Decimal("0.2") and len(creates) == 1 and float(r["data"]["order"]["executedQty"]) == 0.2
          and r2["code"] == 0 and after_retry - after_lost == Decimal("0.3") and dup.get("retCode") == 0 and after_dup - after_retry == Decimal("0.1"))
    return ok, f"lost reply: position +{after_lost - base} (one order, {len(creates)} create request, reported executed {r['data']['order']['executedQty']}); server error then retry: +{after_retry - after_lost}; duplicate orderLinkId resolved to the existing order, position +{after_dup - after_retry}"


def timestamp_outside_the_window_is_resynced_with_the_server_time():
    stub = StubBybit()
    stub.skew_ms = 60_000                                                # the venue's clock is a minute ahead
    api = bf.FastBybit(KEY, SECRET, ErrorLog(os.path.join(_STATE_DIR, "by-err.jsonl")), transport=stub)
    r = api.get(bf.P_POS)
    first = len(stub.calls)
    r2 = api.get(bf.P_POS)
    second = len(stub.calls) - first
    ok = r["code"] == 0 and r2["code"] == 0 and 59_000 <= api._time_offset <= 61_000 and api.stats["resync"] == 1 and second == 1 and any(p == "/v5/market/time" for m, p, d in stub.calls)
    return ok, f"retCode 10002 -> clock offset {api._time_offset} ms from /v5/market/time, request repeated ({first} calls), the next request needs {second} call"


def circuit_breaker_holds_entries_but_never_closes_or_cancels():
    api, stub, clk = _api_clock()
    _held(stub)
    api.post(bf.P_ORDER, _order())
    oid = api.batch_place([_ctrl("sl", "98.5")])["data"]["orders"][0]["orderId"]
    stub.script["/v5/order/create"] = [env(10016, "Server error")] * 40
    for _ in range(4):
        api.post(bf.P_ORDER, _order())
    opened = clk.now() < api.breaker_until
    n = len(stub.calls)
    entry = api.post(bf.P_ORDER, _order())
    held = len(stub.calls) == n and entry.get("cooled") is True
    stub.script["/v5/order/create"] = []
    close = api.post(bf.P_ORDER, {"symbol": "BTC-USDT", "type": "MARKET", "side": "SELL", "positionSide": "LONG", "quantity": "0.1", "clientOrderID": _cid("c")})
    cancel = api.delete(bf.P_ORDER, {"symbol": "BTC-USDT", "orderId": oid})
    clk.t += 16
    back = api.post(bf.P_ORDER, _order())
    ok = opened and held and close["code"] == 0 and cancel["code"] == 0 and back["code"] == 0 and api.stats["breaker"] >= 1 and api.breaker_until == 0.0
    return ok, f"{api.stats['breaker']} breaker trip(s) after repeated server errors; entry held without a request={held}; reduce-only close -> {close['code']}, cancel -> {cancel['code']}; entry after 15 s -> {back['code']}"


def error_classes_cover_rate_auth_time_balance_and_filters():
    import bybit_limits as bl
    table = [
        ((10006, 200, "Too many visits"), "rate"), ((0, 429, ""), "rate"), ((10018, 200, ""), "ip-ban"), ((-1, 403, "access too frequent"), "ip-ban"),
        ((10002, 200, "invalid request, please check your server timestamp or recv_window param"), "time"),
        ((10002, 200, ""), "time"), ((10003, 200, ""), "auth"), ((10004, 200, ""), "auth"), ((10005, 200, ""), "auth"), ((10010, 200, ""), "auth"), ((-1, 401, ""), "auth"),
        ((10009, 200, ""), "restricted"), ((10029, 200, ""), "restricted"),
        ((10000, 200, ""), "transient"), ((10016, 200, ""), "transient"), ((-1, 0, "timed out"), "transient"), ((502, 502, "<html>bad gateway"), "transient"), ((110079, 200, ""), "transient"),
        ((10001, 200, "params error"), "param"),
        ((110007, 200, ""), "insufficient"), ((110012, 200, ""), "insufficient"), ((110045, 200, ""), "insufficient"), ((110044, 200, ""), "insufficient"),
        ((110017, 200, "current position is zero, cannot fix reduce-only order qty"), "flat"), ((110034, 200, ""), "flat"), ((110023, 200, ""), "flat"),
        ((110017, 200, "Order quantity truncates to zero"), "qty-filter"), ((110094, 200, "notional"), "price-filter"), ((110003, 200, ""), "price-filter"),
        ((110001, 200, ""), "not-found"), ((110008, 200, ""), "not-found"), ((110010, 200, ""), "not-found"),
        ((110072, 200, ""), "duplicate"), ((110092, 200, ""), "trigger"), ((110093, 200, ""), "trigger"), ((110020, 200, ""), "cap"),
        ((110043, 200, ""), "unchanged"), ((110025, 200, ""), "unchanged"), ((110013, 200, ""), "leverage"), ((110040, 200, ""), "liquidation"),
        ((110016, 200, ""), "limit"), ((110999, 200, ""), "permanent"),
    ]
    bad = [(a, want, bl.classify(*a).cls) for a, want in table if bl.classify(*a).cls != want]
    retry_ok = all(bl.classify(*a).retry == (want in ("rate", "transient", "time")) for a, want in table if want not in ("ip-ban",))
    api, stub = _api()
    stub.raw["/v5/position/list"] = [(200, b"not json at all")]
    r = api.get(bf.P_POS)                                                # a bad body is a transient read error, retried once
    stub.raw["/v5/position/list"] = [(502, b"<html>502 Bad Gateway</html>"), (200, json.dumps(env(result={"list": [], "nextPageCursor": ""})).encode())]
    r2 = api.get(bf.P_POS)
    ok = not bad and retry_ok and r["code"] == 0 and r2["code"] == 0
    return ok, f"{len(table)} (code, status, text) cases classified, mismatches={bad[:3]}; retry flag only for rate/transient/time={retry_ok}; bad JSON and HTTP 502 on a read are retried once -> {r['code']}/{r2['code']}"


def reads_retry_once_on_a_server_error_and_writes_do_not_repeat_blindly():
    api, stub, clk = _api_clock()
    stub.script["/v5/position/list"] = [env(10016, "Server error")]
    r = api.get(bf.P_POS)
    reads = len(stub.sent("/v5/position/list", "GET"))
    stub.script["/v5/position/set-leverage"] = [env(10016, "Server error")] * 3
    lv = api.post(bf.P_LEV, {"symbol": "BTC-USDT", "side": "LONG", "leverage": 10})
    ok = r["code"] == 0 and reads == 2 and lv["code"] == 10016 and lv.get("transient") is True and "retry" in lv["msg"]
    return ok, f"position read: server error then ok after {reads} requests; leverage write gave up after its retry -> {lv['code']} marked transient"


def hedge_mode_is_asked_again_after_a_rate_limit():
    api, stub, clk = _api_clock()
    stub.script["/v5/position/switch-mode"] = [env(10006, "Too many visits")]
    api.post(bf.P_ORDER, _order())
    first = api._mode_done
    clk.t += 2.0
    api.post(bf.P_ORDER, _order())
    ok = (not first or stub.hedge) and api._mode_done and stub.hedge
    return ok, f"switch-mode refused by a rate limit did not mark the account set up (done={first} after the first order); the next order asked again -> hedge={stub.hedge}"


def batch_cancel_and_amend_work_item_by_item():
    api, stub, clk = _api_clock()
    _held(stub)
    api.post(bf.P_ORDER, _order())
    ids = [x["orderId"] for x in api.batch_place([_ctrl("sl", "98.5"), _ctrl("tp", "103"), _ctrl("sl", "98.0")])["data"]["orders"]]
    am = api.batch_amend([{"symbol": "BTC-USDT", "orderId": ids[0], "stopPrice": "99.5"}, {"symbol": "BTC-USDT", "orderId": ids[1], "stopPrice": "99"}, {"symbol": "BTC-USDT", "orderId": "ghost", "stopPrice": "99.5"}])
    cc = api.batch_cancel([{"symbol": "BTC-USDT", "orderId": ids[0]}, {"symbol": "BTC-USDT", "orderId": ids[0]}, {"symbol": "BTC-USDT", "orderId": "ghost"}])
    amended = stub.sent("/v5/order/amend-batch")
    ok = ([x["code"] for x in am["data"]["orders"]] == [0, 110092, 110001] and stub.orders[ids[0]]["orderStatus"] == "Cancelled"
          and [x["code"] for x in cc["data"]["orders"]] == [0, 0, 0] and len(amended) == 1 and len(amended[0]["request"]) == 3)
    return ok, f"amend-batch item codes {[x['code'] for x in am['data']['orders']]} (one call of 3); cancel-batch {[x['code'] for x in cc['data']['orders']]} (an order already gone counts as cancelled)"


def demo_trading_reads_market_data_from_mainnet():
    seen = []

    def tr(method, url, headers, body):
        seen.append(url)
        return 200, {}, json.dumps(env(result={"list": [], "category": "linear"})).encode()
    demo = bf.FastBybit(KEY, SECRET, ErrorLog(os.path.join(_STATE_DIR, "by-err.jsonl")), base=bf.BASE_DEMO, transport=tr)
    demo.public(bf.P_TICK)
    demo.get(bf.P_POS)
    live = bf.FastBybit(KEY, SECRET, ErrorLog(os.path.join(_STATE_DIR, "by-err.jsonl")), base=bf.BASE, transport=tr)
    live.public(bf.P_TICK)
    ok = seen[0].startswith("https://api.bybit.com/v5/market/tickers") and seen[1].startswith("https://api-demo.bybit.com/v5/position/list") and seen[2].startswith("https://api.bybit.com/v5/market")
    return ok, f"demo: public -> {seen[0].split('/v5')[0]}, signed -> {seen[1].split('/v5')[0]}; live: public -> {seen[2].split('/v5')[0]}"


def bybit_connections_get_their_own_tag_contracts_and_pins():
    import subprocess
    import sys
    out = {}
    for conn in ("bybit-x01", "bybit-x02", "bingx-x02", "bingx-x01"):
        r = subprocess.run([sys.executable, "-B", "-c", "import pulse_trader as p; print(p.TAG, p.IS_BYBIT, p.make_api.__name__)"], cwd=PULSE, capture_output=True, text=True,
                           env={**os.environ, "PULSE_CONN": conn}, timeout=120)
        out[conn] = (r.stdout.strip().split("\n") or [""])[-1]
    stub = StubBybit()
    saved = (bf._urllib_transport, pt.IS_BYBIT, pt.BASE)
    bf._urllib_transport = stub
    pt.IS_BYBIT, pt.BASE = True, "https://api.bybit.com"
    try:
        rows = _REAL_LOAD_CONTRACTS(None)
        only = _REAL_LOAD_CONTRACTS({"BTC-USDT"})
    finally:
        bf._urllib_transport, pt.IS_BYBIT, pt.BASE = saved
    c = rows.get("BTC-USDT")
    pep = rows.get("1000PEPE-USDT")
    import conn_guard
    mb, mm = conn_guard.resolve_base("bybit-x02")
    lb, lm = conn_guard.resolve_base("bybit-x01")
    hosts = (conn_guard.mode_of("https://api.bybit.com"), conn_guard.mode_of("https://api-demo.bybit.com/"))
    ok = (out["bybit-x01"] == "Gb01 True make_api" and out["bybit-x02"] == "Gb02 True make_api" and out["bingx-x02"] == "Gx02 False make_api" and out["bingx-x01"] == "Gx01 False make_api"
          and sorted(rows) == ["1000PEPE-USDT", "BTC-USDT"] and isinstance(c, pt.Contract) and c.step == 0.001 and c.qprec == 3 and c.min_usdt == 5.0 and c.max_lev == 100
          and pep.step == 10.0 and sorted(only) == ["BTC-USDT"] and mb == "https://api-demo.bybit.com"
          and lb == "https://api.bybit.com" and lm == conn_guard.LIVE_MAINNET and hosts == (conn_guard.LIVE_MAINNET, conn_guard.VST_DEMO))
    return ok, f"tags {out}; contracts {sorted(rows)} BTC step={getattr(c, 'step', None)} PEPE step={getattr(pep, 'step', None)}; want filter {sorted(only)}; bybit-x02 pinned to {mb}"


def the_sidecar_lists_bybit_lanes_apart_from_overall():
    import tempfile as tf
    import importlib
    saved_dir = os.environ.get("PULSE_DIR_TEST")
    import pulse_http as ph
    with tf.TemporaryDirectory() as td:
        open(os.path.join(td, "cred-bybit-x01.json"), "w").write("{}")
        old_dir = ph.DIR
        ph.DIR = td
        try:
            extra = ph._extra_lanes()
        finally:
            ph.DIR = old_dir
    none = ph._extra_lanes()                    # the real pulse directory has no Bybit file in the build host
    base_ids = [l["id"] for l in ph.LANES]
    ok = [l["id"] for l in extra] == ["bybit-x01"] and extra[0]["type"] == "bybit" and extra[0]["unit"] == "USDT" and base_ids == ["bingx-x01", "bingx-x02"] and all(l["id"].startswith("bybit") is False for l in ph.LANES) and isinstance(none, list)
    return ok, f"a credential file adds lane {[l['id'] for l in extra]}; Overall keeps {base_ids}"


def batch_item_with_a_used_link_id_resolves_to_the_existing_order():
    api, stub, clk = _api_clock()
    _held(stub)
    api.post(bf.P_ORDER, _order())
    first = _ctrl("sl", "98.5")
    oid = api.batch_place([first])["data"]["orders"][0]["orderId"]
    again = api.batch_place([dict(first), _ctrl("tp", "103")])           # the first item's link id exists: 110072 for that item only
    rows = again["data"]["orders"]
    conds = [o for o in stub.orders.values() if o.get("triggerPrice") and o["orderStatus"] == "Untriggered"]
    ok = rows[0]["code"] == 0 and rows[0]["orderId"] == oid and rows[1]["code"] == 0 and rows[1]["orderId"] and rows[1]["orderId"] != oid and len(conds) == 2
    return ok, f"a batch item whose orderLinkId exists -> the existing order {rows[0]['orderId']} (no second stop); the other item placed; venue holds {len(conds)} conditional orders"


def bring_up_script_writes_a_private_credential_file_and_never_prints_it():
    import subprocess
    # the repository's deploy script, not one next to the pulse copy under test (a fault-injection copy has no deploy/)
    sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "deploy", "bybit-up.sh")
    sh = os.path.abspath(sh)
    syntax = subprocess.run(["bash", "-n", sh], capture_output=True, text=True)
    with tempfile.TemporaryDirectory() as td:
        pdir = os.path.join(td, "pulse")
        env_ = {**os.environ, "BYBIT_API_KEY": "KEY-NEVER-PRINT", "BYBIT_API_SECRET": "SECRET-NEVER-PRINT", "PULSE_DIR": pdir, "LOG_DIR": os.path.join(td, "log"),
                "REPO": os.path.abspath(os.path.join(os.path.dirname(sh), ".."))}
        out = subprocess.run(["bash", sh, "x02"], capture_output=True, text=True, env=env_, timeout=120)
        cred = os.path.join(pdir, "cred-bybit-x02.json")
        data = json.load(open(cred)) if os.path.exists(cred) else {}
        mode = (os.stat(cred).st_mode & 0o777) if os.path.exists(cred) else 0
        said = out.stdout + out.stderr
        bad_arg = subprocess.run(["bash", sh, "x09"], capture_output=True, text=True, env=env_, timeout=30)
        started = any(f.endswith(".log") for f in os.listdir(os.path.join(td, "log"))) if os.path.isdir(os.path.join(td, "log")) else False
    ok = (syntax.returncode == 0 and out.returncode == 0 and data == {"api_key": "KEY-NEVER-PRINT", "api_secret": "SECRET-NEVER-PRINT", "is_testnet": "1"} and mode == 0o600
          and "KEY-NEVER-PRINT" not in said and "SECRET-NEVER-PRINT" not in said and bad_arg.returncode == 2 and not started)
    return ok, f"syntax ok={syntax.returncode == 0}; x02 file mode {oct(mode)} fields {sorted(data)} is_testnet={data.get('is_testnet')}; secrets in output: {'KEY-NEVER-PRINT' in said or 'SECRET-NEVER-PRINT' in said}; bad slot exit {bad_arg.returncode}; no engine started without START=1"


def connection_names_pick_the_venue():
    ok_by = ok_bx = False
    try:
        by = pt.make_api("bybit-x01", "k", "s", "https://api.bybit.com")
        bx = pt.make_api("bingx-x01", "k", "s", "https://open-api.bingx.com")
        ok_by = type(by).__name__ == "FastBybit" and by.base == "https://api.bybit.com" and by.hedge
        ok_bx = type(bx).__name__ == "FastBingX"
    except AttributeError:
        pass
    return ok_by and ok_bx, f"bybit-x01 -> FastBybit={ok_by}; bingx-x01 -> FastBingX={ok_bx}"


CHECKS = [
    ("bybit.requests-signed-over-exact-query-and-body", requests_are_signed_over_the_exact_query_and_body),
    ("bybit.symbols-and-tickers-map-both-ways", symbols_and_tickers_map_both_ways),
    ("bybit.contracts-follow-instrument-filters", contracts_follow_the_instrument_filters),
    ("bybit.klines-oldest-first-in-trader-shape", klines_come_oldest_first_in_the_shape_the_trader_parses),
    ("bybit.balance-and-positions-bingx-shape", balance_and_positions_come_back_in_the_bingx_shape),
    ("bybit.entry-body-and-fill-report", entry_order_body_and_fill_report),
    ("bybit.protective-batch-conditional-reduce-only", protective_batch_is_conditional_reduce_only_orders),
    ("bybit.batch-per-item-result-and-single-form-fallback", batch_reports_each_item_and_single_forms_fall_back),
    ("bybit.stop-moves-by-amend", stop_moves_by_amending_the_trigger_in_place),
    ("bybit.open-orders-typed-by-direction-and-paged", open_orders_are_typed_by_trigger_direction_and_paged),
    ("bybit.cancel-and-status-lookup", cancel_and_status_lookup_report_like_bingx),
    ("bybit.errors-carry-trader-words", bybit_errors_carry_the_words_the_trader_reads),
    ("bybit.leverage-and-modes-set-once", leverage_and_account_modes_are_set_once),
    ("bybit.rate-headers-drive-the-endpoint-gate", rate_limit_headers_drive_the_endpoint_gate),
    ("bybit.rate-limit-error-backs-off-to-the-reset", rate_limit_error_backs_off_to_the_reset_and_retries_once),
    ("bybit.ip-ban-stops-every-request-ten-minutes", ip_ban_stops_every_request_for_ten_minutes),
    ("bybit.local-pace-far-below-the-ip-limit", local_pace_stays_far_below_the_ip_limit),
    ("bybit.batches-chunked-and-only-failed-items-retried", batches_are_chunked_and_retry_only_the_failed_items),
    ("bybit.batch-lost-reply-resolved-by-link-id", batch_whole_call_failure_resolves_items_by_link_id),
    ("bybit.retry-never-opens-a-second-position", retry_after_a_lost_reply_never_opens_a_second_position),
    ("bybit.batch-item-with-a-used-link-id-resolves-to-the-existing-order", batch_item_with_a_used_link_id_resolves_to_the_existing_order),
    ("bybit.timestamp-resync", timestamp_outside_the_window_is_resynced_with_the_server_time),
    ("bybit.circuit-breaker-holds-entries-only", circuit_breaker_holds_entries_but_never_closes_or_cancels),
    ("bybit.error-classes", error_classes_cover_rate_auth_time_balance_and_filters),
    ("bybit.reads-retry-writes-do-not", reads_retry_once_on_a_server_error_and_writes_do_not_repeat_blindly),
    ("bybit.hedge-mode-asked-again-after-rate-limit", hedge_mode_is_asked_again_after_a_rate_limit),
    ("bybit.batch-cancel-and-amend-per-item", batch_cancel_and_amend_work_item_by_item),
    ("bybit.price-stream-merges-snapshot-and-delta", price_stream_merges_snapshots_and_deltas),
    ("bybit.price-hub-shards-subscribes-pings", price_hub_subscribes_in_shards_and_pings),
    ("bybit.trader-lifecycle-entry-controls-venue-tp-close", positions_and_orders_lifecycle_through_the_live_trader),
    ("bybit.own-closing-fills-synced-with-pnl", own_closing_fills_are_synced_with_their_pnl),
    ("bybit.demo-reads-market-data-from-mainnet", demo_trading_reads_market_data_from_mainnet),
    ("bybit.bring-up-script-private-credentials-no-secret-output", bring_up_script_writes_a_private_credential_file_and_never_prints_it),
    ("bybit.connection-name-picks-the-venue", connection_names_pick_the_venue),
    ("bybit.tag-contracts-and-pins-per-connection", bybit_connections_get_their_own_tag_contracts_and_pins),
    ("bybit.sidecar-lists-bybit-lanes-apart-from-overall", the_sidecar_lists_bybit_lanes_apart_from_overall),
]
