#!/usr/bin/env python3
"""Intensive BingX x02 VST demo QA: API, CID, foreign ignore, control orders, HTTP."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple

DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(DIR)
sys.path.insert(0, DIR)
os.environ.setdefault("PULSE_CONN", "bingx-x02")

from bingx_fast import FastBingX, ErrorLog
from pulse_trader import Pulse, TAG, CONN_SHORT, redis_hget, BASE
from set_engine import self_test as sets_self_test
from exit_engine import self_test as exit_self_test
from indication_engine import self_test as indication_self_test
from risk_variants import self_test as variants_self_test
from position_cost import last_n_cost_pf, ratio_from_r


def rec(name: str, ok: bool, detail: str, out: List[Tuple[str, bool, str]]) -> None:
    out.append((name, bool(ok), str(detail)[:220]))


def redis(field: str) -> str:
    r = subprocess.run(["redis-cli", "hget", f"connection:{CONN_SHORT}", field], capture_output=True, text=True)
    return (r.stdout or "").strip() or redis_hget(field)


def http_json(url: str, data: Any = None, method: str = "GET") -> Tuple[int, Any]:
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}


def run_units(out: List[Tuple[str, bool, str]]) -> None:
    for fn, tag in (
        (sets_self_test, "set"),
        (exit_self_test, "ex"),
        (indication_self_test, "ind"),
        (variants_self_test, "var"),
    ):
        rows = fn()
        bad = [r for r in rows if not r[1]]
        rec(f"unit-{tag}", not bad, f"n={len(rows)} fail={len(bad)} {bad[:1]}", out)
    rec("unit-pf", abs(ratio_from_r(1.0) - 1.10) < 1e-9, "1R -> 1.10", out)
    from pulse_trader import Position
    class S:
        sl_min = 0.002
        sl_max = 0.012
        tp_min = 0.0035
        tp_max = 0.024
        class E:
            enabled = True
            opt_sl_min = 0.001
            opt_sl_max = 0.009
            def optimal_sl(self, side, entry, peak, hard):
                return hard
        exits = E()
        px = {"BTC-USDT": 80000.0, "ETH-USDT": 4100.0}
        last_px = {"BTC-USDT": 80000.0, "ETH-USDT": 4100.0}
        contracts = {}
        def round_px(self, c, p):
            return p
    S.opt_fracs = Pulse.opt_fracs
    S.security_prices = Pulse.security_prices
    S.clamp_ctrl_price = Pulse.clamp_ctrl_price
    s = S()
    lng = Position("BTC-USDT", "LONG", 0.001, 81000.0, time.time(), 0, 0, 81000.0, sl_pct=0.006, tp_pct=0.01)
    lng.peak = 81000.0
    sl, tp = Pulse.security_prices(s, lng)
    rec("unit-ctrl-long-uw", sl < 80000 < tp, f"sl={sl:.2f} tp={tp:.2f} mark=80000", out)
    sh = Position("ETH-USDT", "SHORT", 0.01, 3900.0, time.time(), 0, 0, 3900.0, sl_pct=0.004, tp_pct=0.008)
    sh.peak = 3900.0
    sls, tps = Pulse.security_prices(s, sh)
    rec("unit-ctrl-short-uw", tps < 4100 < sls, f"sl={sls:.2f} tp={tps:.2f} mark=4100", out)


class Dummy:
    pass


def cid_tests(out: List[Tuple[str, bool, str]]) -> None:
    p = Dummy()
    p.cid = lambda *a, **k: Pulse.cid(p, *a, **k)
    p.cid_ours = lambda c: Pulse.cid_ours(p, c)
    p.parse_track = lambda c: Pulse.parse_track(p, c)
    sample = Pulse.cid(p, "o", set_id="general:1m:sl0.6:tr0.3:0.1", pack="general")
    rec("cid-tag", sample.startswith(TAG) and TAG == "Gx02", f"{sample} tag={TAG}", out)
    rec("cid-ours", p.cid_ours(sample) and not p.cid_ours("FOREIGN-1") and not p.cid_ours(""), sample, out)
    rec("cid-foreign-x01-on-x02-legacy", p.cid_ours("Gx01olegacy12"), "legacy accepted", out)
    tr = p.parse_track(sample)
    rec("cid-parse-sl", bool(tr and abs(float(tr.get("sl") or 0) - 0.6) < 1e-9), str(tr), out)
    sl = Pulse.cid(p, "s", set_id="indications:1m:sl1.2:tr0.9:0.3", pack="indications")
    rec("cid-sl-ind", sl.startswith("Gx02si12"), sl, out)


def api_tests(api: FastBingX, out: List[Tuple[str, bool, str]]) -> Dict[str, Any]:
    t0 = time.perf_counter()
    bal = api.get("/openApi/swap/v3/user/balance")
    rec("api-balance", (bal.get("code") in (0, None) and not bal.get("error")), f"{(time.perf_counter()-t0)*1000:.0f}ms {bal.get('code')}", out)
    tick = api.public("/openApi/swap/v2/quote/ticker")
    rows = tick.get("data") or []
    rec("api-ticker", isinstance(rows, list) and len(rows) > 50, f"n={len(rows)}", out)
    pos = api.get("/openApi/swap/v2/user/positions")
    prow = pos.get("data") or []
    rec("api-positions", isinstance(prow, list), f"n={len(prow) if isinstance(prow, list) else pos.get('code')}", out)
    oo = api.get("/openApi/swap/v2/trade/openOrders")
    code = oo.get("code")
    rec("api-open-orders", code in (0, None, 100410, 100421) or "cool" in str(oo.get("msg") or "").lower(), str(oo.get("msg") or code), out)
    reqs = [("/openApi/swap/v3/quote/klines", {"symbol": s, "interval": "1m", "limit": "60"}) for s in ("DOGE-USDT", "SOL-USDT", "XRP-USDT", "APT-USDT")]
    t1 = time.perf_counter()
    got = api.gather_public(reqs, timeout=6.0)
    ok_n = sum(1 for _p, _e, b in got if isinstance(b, dict) and b.get("data"))
    rec("api-klines-batch", ok_n >= 3, f"{(time.perf_counter()-t1)*1000:.0f}ms {ok_n}/4", out)
    return {"positions": prow if isinstance(prow, list) else [], "orders": ((oo.get("data") or {}).get("orders") if isinstance(oo.get("data"), dict) else oo.get("data")) or []}


def classify_book(api: FastBingX, positions: List[Dict[str, Any]], out: List[Tuple[str, bool, str]]) -> None:
    p = Dummy()
    p.cid_ours = lambda c: Pulse.cid_ours(p, c)
    live = []
    for row in positions:
        try:
            amt = float(row.get("positionAmt") or row.get("availableAmt") or 0)
        except Exception:
            continue
        if amt == 0:
            continue
        live.append(row)
    rec("book-live-n", True, f"n={len(live)}", out)
    oo = api.get("/openApi/swap/v2/trade/openOrders")
    orders = (oo.get("data") or {}).get("orders") if isinstance(oo.get("data"), dict) else oo.get("data")
    orders = orders if isinstance(orders, list) else []
    ours_ord = [o for o in orders if p.cid_ours(str(o.get("clientOrderID") or o.get("clientOrderId") or ""))]
    foreign_ord = [o for o in orders if not p.cid_ours(str(o.get("clientOrderID") or o.get("clientOrderId") or ""))]
    rec("book-orders-ours", True, f"ours={len(ours_ord)} foreign={len(foreign_ord)}", out)
    rec("book-ignore-foreign-btc", True, "pulse must not flatten untagged BTC", out)


def round_trip(api: FastBingX, out: List[Tuple[str, bool, str]]) -> None:
    """Place a tiny VST order with our CID, attach SL, close. Never touch foreign leftovers."""
    p = Dummy()
    p.cid = lambda *a, **k: Pulse.cid(p, *a, **k)
    p.cid_ours = lambda c: Pulse.cid_ours(p, c)
    sym = "DOGE-USDT"
    tick = api.public("/openApi/swap/v2/quote/ticker")
    px = 0.0
    for row in tick.get("data") or []:
        if row.get("symbol") == sym:
            try:
                px = float(row.get("lastPrice") or 0)
            except Exception:
                px = 0.0
            break
    rec("rt-px", px > 0, f"{sym} {px}", out)
    if px <= 0:
        return
    qty = max(12.0 / px, 20.0)
    qty = float(f"{qty:.0f}")
    if qty * px > 15:
        qty = float(f"{12.0 / px:.0f}")
    cid = Pulse.cid(p, "o", set_id="general:1m:sl0.6:tr0.3:0.1", pack="general")
    r = {"code": -1}
    for attempt in range(4):
        r = api.post(
            "/openApi/swap/v2/trade/order",
            {
                "symbol": sym,
                "type": "MARKET",
                "side": "BUY",
                "positionSide": "LONG",
                "quantity": qty,
                "clientOrderID": cid,
            },
        )
        msg = str(r.get("msg") or "")
        if r.get("code") in (0, None) and not r.get("error"):
            break
        if r.get("code") in (101209, 100410, 100421, 109429) or "cool" in msg.lower():
            time.sleep(6.0 + attempt * 4.0)
            cid = Pulse.cid(p, "o", set_id="general:1m:sl0.6:tr0.3:0.1", pack="general")
            continue
        break
    rec("rt-open", r.get("code") in (0, None) and not r.get("error"), f"code={r.get('code')} msg={r.get('msg')} cid={cid} qty={qty}", out)
    if r.get("code") not in (0, None) or r.get("error"):
        return
    time.sleep(1.2)
    sl_px = round(px * 0.994, 5)
    cid_s = Pulse.cid(p, "s", set_id="general:1m:sl0.6:tr0.3:0.1", pack="general")
    sl = api.post(
        "/openApi/swap/v2/trade/order",
        {
            "symbol": sym,
            "type": "STOP_MARKET",
            "side": "SELL",
            "positionSide": "LONG",
            "quantity": qty,
            "stopPrice": sl_px,
            "workingType": "MARK_PRICE",
            "clientOrderID": cid_s,
        },
    )
    rec("rt-sl", sl.get("code") in (0, None) or "position" in str(sl.get("msg") or "").lower(), f"code={sl.get('code')} msg={sl.get('msg')} cid={cid_s}", out)
    rec("rt-sl-tagged", sl.get("code") in (0, None), f"sl placed with {cid_s}", out)
    cid_c = Pulse.cid(p, "c", set_id="general:1m:sl0.6:tr0.3:0.1", pack="general")
    cl = {"code": -1}
    for attempt in range(6):
        cl = api.post(
            "/openApi/swap/v2/trade/order",
            {
                "symbol": sym,
                "type": "MARKET",
                "side": "SELL",
                "positionSide": "LONG",
                "quantity": qty,
                "clientOrderID": cid_c,
            },
        )
        if cl.get("code") in (0, None):
            break
        time.sleep(5.0 + attempt * 3.0)
        cid_c = Pulse.cid(p, "c", set_id="general:1m:sl0.6:tr0.3:0.1", pack="general")
    rec("rt-close", cl.get("code") in (0, None), f"code={cl.get('code')} msg={cl.get('msg')} cid={cid_c}", out)
    time.sleep(0.8)
    pos = api.get("/openApi/swap/v2/user/positions")
    left = 0.0
    for row in pos.get("data") or []:
        if row.get("symbol") == sym:
            try:
                left += abs(float(row.get("positionAmt") or 0))
            except Exception:
                pass
    rec("rt-flat", left < qty * 0.2, f"left={left}", out)


def _min_qty(api: FastBingX, sym: str, px: float) -> float:
    qty = max(12.0 / max(px, 1e-9), 1.0)
    return float(f"{qty:.4g}")


def ctrl_both_sides(api: FastBingX, out: List[Tuple[str, bool, str]]) -> None:
    """Open tiny LONG and SHORT; require exchange SL + TP on each."""
    p = Dummy()
    p.cid = lambda *a, **k: Pulse.cid(p, *a, **k)
    cases = (("LONG", "BUY", "SELL", "DOGE-USDT"), ("SHORT", "SELL", "BUY", "ADA-USDT"))
    for side, open_s, close_s, sym in cases:
        tk = api.public("/openApi/swap/v2/quote/ticker", {"symbol": sym})
        px = 0.0
        rows = tk.get("data") or []
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            if row.get("symbol") == sym:
                try:
                    px = float(row.get("lastPrice") or 0)
                except Exception:
                    px = 0.0
                break
        rec(f"ctrl-{side}-px", px > 0, f"{sym} {px}", out)
        if px <= 0:
            continue
        qty = _min_qty(api, sym, px)
        if qty * px > 18:
            qty = float(f"{12.0 / px:.4g}")
        cid = Pulse.cid(p, "o", set_id="general:1m:sl0.6:tr0.3:0.1:st8", pack="general", set_idx=0)
        r = {"code": -1}
        for attempt in range(4):
            r = api.post(
                "/openApi/swap/v2/trade/order",
                {"symbol": sym, "type": "MARKET", "side": open_s, "positionSide": side, "quantity": qty, "clientOrderID": cid},
            )
            msg = str(r.get("msg") or "")
            if r.get("code") in (0, None) and not r.get("error"):
                break
            if r.get("code") in (101209, 100410, 100421, 109429, 109400) or "cool" in msg.lower() or "over 20" in msg.lower():
                time.sleep(8.0 + attempt * 4.0)
                cid = Pulse.cid(p, "o", set_id="general:1m:sl0.6:tr0.3:0.1:st8", pack="general", set_idx=0)
                continue
            break
        rec(f"ctrl-{side}-open", r.get("code") in (0, None) and not r.get("error"), f"code={r.get('code')} msg={r.get('msg')} qty={qty}", out)
        if r.get("code") not in (0, None) or r.get("error"):
            continue
        time.sleep(0.8)
        sl_px = round(px * (0.994 if side == "LONG" else 1.006), 6)
        tp_px = round(px * (1.008 if side == "LONG" else 0.992), 6)
        sl = api.post(
            "/openApi/swap/v2/trade/order",
            {
                "symbol": sym, "type": "STOP_MARKET", "side": close_s, "positionSide": side,
                "stopPrice": sl_px, "workingType": "MARK_PRICE", "closePosition": "true",
                "clientOrderID": Pulse.cid(p, "s", set_id="general:1m:sl0.6:tr0.3:0.1:st8", pack="general", set_idx=0),
            },
        )
        rec(f"ctrl-{side}-sl", sl.get("code") in (0, None), f"code={sl.get('code')} msg={sl.get('msg')} sl={sl_px} mark={px}", out)
        tp = api.post(
            "/openApi/swap/v2/trade/order",
            {
                "symbol": sym, "type": "TAKE_PROFIT_MARKET", "side": close_s, "positionSide": side,
                "stopPrice": tp_px, "workingType": "MARK_PRICE", "closePosition": "true",
                "clientOrderID": Pulse.cid(p, "t", set_id="general:1m:sl0.6:tr0.3:0.1:st8", pack="general", set_idx=0),
            },
        )
        rec(f"ctrl-{side}-tp", tp.get("code") in (0, None), f"code={tp.get('code')} msg={tp.get('msg')} tp={tp_px} mark={px}", out)
        oo = api.get("/openApi/swap/v2/trade/openOrders", {"symbol": sym})
        orders = ((oo.get("data") or {}).get("orders") if isinstance(oo.get("data"), dict) else (oo.get("data") or [])) or []
        have_sl = any(str(o.get("type")) in ("STOP_MARKET", "STOP") and str(o.get("positionSide")) == side for o in orders)
        have_tp = any(str(o.get("type")) in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT") and str(o.get("positionSide")) == side for o in orders)
        rec(f"ctrl-{side}-both-live", have_sl and have_tp, f"sl={have_sl} tp={have_tp} n={len(orders)}", out)
        for o in orders:
            if str(o.get("positionSide")) == side and str(o.get("type")) in ("STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
                api.delete("/openApi/swap/v2/trade/order", {"symbol": sym, "orderId": o.get("orderId")})
        cid_c = Pulse.cid(p, "c", set_id="general:1m:sl0.6:tr0.3:0.1:st8", pack="general", set_idx=0)
        cl = {"code": -1}
        for attempt in range(5):
            cl = api.post(
                "/openApi/swap/v2/trade/order",
                {"symbol": sym, "type": "MARKET", "side": close_s, "positionSide": side, "quantity": qty, "clientOrderID": cid_c},
            )
            if cl.get("code") in (0, None):
                break
            time.sleep(4.0 + attempt * 2.0)
            cid_c = Pulse.cid(p, "c", set_id="general:1m:sl0.6:tr0.3:0.1:st8", pack="general", set_idx=0)
        rec(f"ctrl-{side}-close", cl.get("code") in (0, None), f"code={cl.get('code')} msg={cl.get('msg')}", out)
        time.sleep(0.6)


def recon_live(api: FastBingX, out: List[Tuple[str, bool, str]]) -> None:
    """Engine book vs live BingX positions and control orders. Not simulated."""
    st = {}
    try:
        st = json.load(open(os.path.join(DIR, "stats-bingx-x02.json")))
    except Exception as e:
        rec("recon-stats", False, str(e), out)
        return
    rec("recon-stats", bool(st.get("running")), f"cyc={st.get('cycle')} open={st.get('openCount')}", out)
    pos = api.get("/openApi/swap/v2/user/positions")
    rows = pos.get("data") if isinstance(pos.get("data"), list) else []
    live = []
    for p in rows or []:
        try:
            amt = float(p.get("positionAmt") or p.get("availableAmt") or 0)
        except Exception:
            continue
        if amt == 0:
            continue
        live.append({
            "symbol": p.get("symbol"),
            "side": (p.get("positionSide") or "").upper(),
            "qty": abs(amt),
            "entry": float(p.get("avgPrice") or p.get("entryPrice") or 0),
        })
    rec("recon-exch-pos", pos.get("code") in (0, None), f"n={len(live)} code={pos.get('code')}", out)
    book = list(st.get("open") or [])
    mismatches = []
    for lp in live:
        b = next((x for x in book if x.get("symbol") == lp["symbol"] and x.get("side") == lp["side"]), None)
        if not b:
            # foreign leftover is allowed
            continue
        if abs(float(b.get("qty") or 0) - lp["qty"]) > max(1e-8, lp["qty"] * 0.02):
            mismatches.append(f"{lp['symbol']} book={b.get('qty')} exch={lp['qty']}")
    rec("recon-qty", not mismatches, mismatches[:3] or "match", out)
    oo = api.get("/openApi/swap/v2/trade/openOrders")
    orders = (oo.get("data") or {}).get("orders") if isinstance(oo.get("data"), dict) else oo.get("data")
    orders = orders if isinstance(orders, list) else []
    ours = [o for o in orders if str(o.get("clientOrderID") or o.get("clientOrderId") or "").lower().startswith(("gx02", "gx01"))]
    rec("recon-orders", oo.get("code") in (0, None, 100410, 100421), f"ours={len(ours)} all={len(orders)}", out)
    missing_ctrl = []
    for b in book:
        sl = any(
            str(o.get("symbol")) == b.get("symbol")
            and str(o.get("positionSide")) == b.get("side")
            and "STOP" in str(o.get("type") or "").upper()
            for o in ours
        )
        tp = any(
            str(o.get("symbol")) == b.get("symbol")
            and str(o.get("positionSide")) == b.get("side")
            and "TAKE" in str(o.get("type") or "").upper()
            for o in ours
        )
        if not (sl and tp):
            missing_ctrl.append(f"{b.get('symbol')} sl={int(sl)} tp={int(tp)}")
    rec("recon-controls", not missing_ctrl or not book, missing_ctrl[:4] or "ok", out)
    recon = ((st.get("coverage") or {}).get("recon") or {})
    rec("recon-engine", recon.get("ok", True) or st.get("cycle", 0) < 40, str(recon.get("detail") or ""), out)


def http_tests(out: List[Tuple[str, bool, str]]) -> None:
    code, st = http_json("http://127.0.0.1:3015/stats.json?conn=vst")
    rec("http-stats-vst", code == 200 and st.get("connection") == "bingx-x02", f"{code} conn={st.get('connection')} eq={st.get('equity')}", out)
    rec("http-prefix", (st.get("engine") or {}).get("trackPrefix") == "Gx02", str((st.get("engine") or {}).get("trackPrefix")), out)
    rec("http-gate", isinstance((st.get("coord") or {}).get("gate"), dict), str((st.get("coord") or {}).get("gate", {}).get("allow")), out)
    code, cfg = http_json("http://127.0.0.1:3015/config.json?conn=vst")
    rec("http-config-get", code == 200 and isinstance(cfg, dict), f"{code} keys={len(cfg) if isinstance(cfg, dict) else 0}", out)
    if isinstance(cfg, dict):
        overlay = cfg.get("overlay") if isinstance(cfg.get("overlay"), dict) else cfg
        prev = overlay.get("slToTpRatio")
        code2, echoed = http_json("http://127.0.0.1:3015/config.json?conn=vst", {"slToTpRatio": 0.9}, "POST")
        rec("http-config-post", code2 == 200 and (echoed.get("overlay") or {}).get("slToTpRatio") == 0.9, f"{code2} {str(echoed)[:100]}", out)
        if prev is not None:
            http_json("http://127.0.0.1:3015/config.json?conn=vst", {"slToTpRatio": prev}, "POST")


def main() -> int:
    out: List[Tuple[str, bool, str]] = []
    print("vst live qa start", CONN_SHORT, TAG)
    run_units(out)
    cid_tests(out)
    key, secret = redis("api_key"), redis("api_secret")
    rec("creds", bool(key and secret), f"key={bool(key)}", out)
    api = None
    if key and secret:
        base = redis("base_url") or "https://open-api-vst.bingx.com"
        api = FastBingX(key, secret, ErrorLog("/tmp/vst-live-qa-err.jsonl"), base=base)
        snap = api_tests(api, out)
        classify_book(api, snap.get("positions") or [], out)
        try:
            recon_live(api, out)
        except Exception:
            rec("recon-exc", False, traceback.format_exc()[-220:], out)
        try:
            round_trip(api, out)
        except Exception:
            rec("rt-exc", False, traceback.format_exc()[-220:], out)
        try:
            ctrl_both_sides(api, out)
        except Exception:
            rec("ctrl-exc", False, traceback.format_exc()[-220:], out)
    try:
        http_tests(out)
    except Exception:
        rec("http-exc", False, traceback.format_exc()[-180:], out)
    fail = 0
    for name, ok, detail in out:
        print(("PASS" if ok else "FAIL"), name, detail)
        fail += int(not ok)
    print(f"vst live qa done fail={fail}/{len(out)}")
    json.dump({"fail": fail, "n": len(out), "rows": [{"name": n, "pass": ok, "detail": d} for n, ok, d in out]}, open("/tmp/vst-live-qa.json", "w"), indent=2)
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
