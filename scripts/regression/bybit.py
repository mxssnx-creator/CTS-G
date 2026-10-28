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

    # -- transport -------------------------------------------------------------------------------------------------------
    def __call__(self, method, url, headers, body):
        u = urllib.parse.urlsplit(url)
        path, query = u.path, u.query
        params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
        text = body.decode() if body else ""
        data = json.loads(text) if text else {}
        self.calls.append((method, path, data if method == "POST" else params))
        if not path.startswith("/v5/market"):
            ts, rw, sg = headers.get("X-BAPI-TIMESTAMP", ""), headers.get("X-BAPI-RECV-WINDOW", ""), headers.get("X-BAPI-SIGN", "")
            want = hmac.new(self.secret.encode(), f"{ts}{headers.get('X-BAPI-API-KEY', '')}{rw}{query if method == 'GET' else text}".encode(), hashlib.sha256).hexdigest()
            if headers.get("X-BAPI-API-KEY") != self.key or sg != want:
                self.sig_failures += 1
                return 200, {}, json.dumps(env(10004, "error sign! origin_string[" + ts + "...]")).encode()
            if self.rate_limit_next is not None:
                st, hd, ev = self.rate_limit_next
                self.rate_limit_next = None
                return st, hd, json.dumps(ev).encode()
            if path in self.fail:
                return 200, {}, json.dumps(self.fail.pop(path)).encode()
        h = getattr(self, "h_" + path.strip("/").replace("v5/", "").replace("/", "_").replace("-", "_"), None)
        if h is None:
            return 404, {}, json.dumps(env(10001, "unknown path " + path)).encode()
        out = h(method, params if method == "GET" else data)
        return 200, {}, json.dumps(out).encode()

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
        if any(x.get("orderLinkId") == o.get("orderLinkId") and o.get("orderLinkId") and x["orderStatus"] in ("New", "Untriggered") for x in self.orders.values()):
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

    def _rows(self, statuses, p):
        rows = []
        for o in self.orders.values():
            if o["orderStatus"] not in statuses:
                continue
            if p.get("symbol") and p["symbol"] != o["symbol"]:
                continue
            if p.get("orderId") and p["orderId"] != o["orderId"]:
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


def _cid(kind="u"):
    return f"Gx01{kind}g060308000{int(time.time() * 1000) % 100000:05d}"[:32]


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
          and "reduceOnly" not in req and req["orderLinkId"] == cid and o["avgPrice"] == "100.0" and o["executedQty"] == "0.3" and o["orderId"].startswith("oid")
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


def rate_limits_start_the_path_cooldown_without_more_requests():
    api, stub = _api()
    stub.rate_limit_next = (200, {"x-bapi-limit-reset-timestamp": str(int((time.time() + 3) * 1000))}, env(10006, "Too many visits. Exceeded the API Rate Limit."))
    r = api.post(bf.P_ORDER, {"symbol": "BTC-USDT", "type": "MARKET", "side": "BUY", "positionSide": "LONG", "quantity": "0.1", "clientOrderID": _cid("o")})
    n = len(stub.calls)
    r2 = api.post(bf.P_ORDER, {"symbol": "BTC-USDT", "type": "MARKET", "side": "BUY", "positionSide": "LONG", "quantity": "0.1", "clientOrderID": _cid("o")})
    cd = api.path_cd.get(bf.P_ORDER, 0) - time.time()
    other = api.get(bf.P_POS)
    ip_api, ip_stub = _api()
    ip_stub.rate_limit_next = (403, {}, {})
    ip_api.get(bf.P_POS)
    ip_cd = ip_api.path_cd.get(bf.P_POS, 0) - time.time()
    ok = (r["code"] == 100410 and 1.5 < cd < 4.0 and r2.get("cooled") is True and len(stub.calls) == n and other["code"] == 0 and ip_cd > 500 and api.cooldown_until > time.time())
    return ok, f"first order -> {r['code']}, path cooldown {cd:.1f}s from the reset header, next order cooled without a request ({len(stub.calls) == n}); HTTP 403 -> {ip_cd:.0f}s"


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
    ok = (a == ("BTC-USDT", 66666.6, -15.8315) and b[1] == 66666.6 and c[1] == 66670.0 and abs(c[2] + 15.8315) < 1e-9 and d == ("ETH-USDT", 3000.0, 0.0) and junk == [None] * 4)
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
    ("bybit.rate-limit-cooldown", rate_limits_start_the_path_cooldown_without_more_requests),
    ("bybit.leverage-and-modes-set-once", leverage_and_account_modes_are_set_once),
    ("bybit.price-stream-merges-snapshot-and-delta", price_stream_merges_snapshots_and_deltas),
    ("bybit.price-hub-shards-subscribes-pings", price_hub_subscribes_in_shards_and_pings),
    ("bybit.trader-lifecycle-entry-controls-venue-tp-close", positions_and_orders_lifecycle_through_the_live_trader),
    ("bybit.own-closing-fills-synced-with-pnl", own_closing_fills_are_synced_with_their_pnl),
    ("bybit.connection-name-picks-the-venue", connection_names_pick_the_venue),
]
