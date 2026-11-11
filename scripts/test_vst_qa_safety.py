#!/usr/bin/env python3
"""Offline safeguards for the x02 exchange QA helper."""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "pulse"))
import vst_live_qa as qa  # noqa: E402


class FakeApi:
    def __init__(self, positions=None, orders=None, position_response=None, order_response=None):
        self.positions = positions or []
        self.orders = orders or []
        self.position_response = position_response
        self.order_response = order_response
        self.deleted = []

    def get(self, path, extra=None):
        if path.endswith("/positions"):
            return self.position_response or {"code": 0, "data": self.positions}
        if path.endswith("/openOrders"):
            return self.order_response or {"code": 0, "data": {"orders": self.orders}}
        raise AssertionError(path)

    def delete(self, path, extra=None):
        self.deleted.append((path, extra))
        return {"code": 0}


class VstQaSafety(unittest.TestCase):
    def test_uncertain_market_is_queried_by_same_id_and_never_resubmitted(self):
        body={'symbol':'DOGE-USDT','positionSide':'LONG','clientOrderID':'Gx02-test','type':'MARKET'}
        class Api:
            posts=0
            def post(self,path,payload):
                self.posts+=1;raise TimeoutError()
            def get(self,path,params):
                self.params=params
                return {'code':0,'data':{'order':{**body,'orderId':'123','status':'FILLED'}}}
        api=Api();got=qa.market_once(api,'/order',body)
        self.assertEqual(got['code'],0);self.assertEqual(api.posts,1)
        self.assertEqual(api.params['clientOrderID'],body['clientOrderID'])

    def test_unknown_or_other_order_never_becomes_confirmed(self):
        body={'symbol':'DOGE-USDT','positionSide':'LONG','clientOrderID':'Gx02-test','type':'MARKET'}
        class Api:
            posts=0
            def post(self,path,payload):self.posts+=1;return {'code':-1,'error':True}
            def get(self,path,params):return {'code':0,'data':{'order':{**body,'clientOrderID':'other','orderId':'456','status':'FILLED'}}}
        api=Api();got=qa.market_once(api,'/order',body)
        self.assertTrue(got['error']);self.assertEqual(api.posts,1)

    def test_symbol_preflight_requires_verified_flat_symbol_and_no_orders(self):
        clear = FakeApi()
        self.assertTrue(qa.symbol_is_clear(clear, "DOGE-USDT", [], "test"))

        exposed = FakeApi(positions=[{"symbol": "DOGE-USDT", "positionAmt": "1"}])
        self.assertFalse(qa.symbol_is_clear(exposed, "DOGE-USDT", [], "test"))

        ordered = FakeApi(orders=[{"symbol": "DOGE-USDT", "type": "LIMIT"}])
        self.assertFalse(qa.symbol_is_clear(ordered, "DOGE-USDT", [], "test"))

        unknown = FakeApi(position_response={"code": -1, "error": True})
        self.assertFalse(qa.symbol_is_clear(unknown, "DOGE-USDT", [], "test"))
        for value in ('NaN','Infinity','unreadable'):
            bad = FakeApi(positions=[{"symbol":"DOGE-USDT","positionAmt":value}])
            self.assertFalse(qa.symbol_is_clear(bad,"DOGE-USDT",[],"test"))

    def test_control_cleanup_only_cancels_exact_test_ids_for_side_and_symbol(self):
        api = FakeApi(orders=[
            {"symbol": "DOGE-USDT", "positionSide": "LONG", "clientOrderID": "Gx02-test-sl", "orderId": 1},
            {"symbol": "DOGE-USDT", "positionSide": "LONG", "clientOrderID": "FOREIGN", "orderId": 2},
            {"symbol": "DOGE-USDT", "positionSide": "SHORT", "clientOrderID": "Gx02-test-tp", "orderId": 3},
            {"symbol": "ADA-USDT", "positionSide": "LONG", "clientOrderID": "Gx02-test-tp", "orderId": 4},
        ])
        self.assertTrue(qa.cancel_owned_controls(api, "DOGE-USDT", "LONG", {"Gx02-test-sl", "Gx02-test-tp"}))
        self.assertEqual(api.deleted, [
            ("/openApi/swap/v2/trade/order", {"symbol": "DOGE-USDT", "orderId": 1}),
        ])

    def test_main_refuses_x01_before_reading_credentials_or_making_requests(self):
        old_conn, old_tag, old_redis = qa.CONN_SHORT, qa.TAG, qa.redis
        try:
            qa.CONN_SHORT, qa.TAG = "bingx-x01", "Gx01"
            qa.redis = lambda _field: self.fail("must refuse before credential reads")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(qa.main(), 2)
        finally:
            qa.CONN_SHORT, qa.TAG, qa.redis = old_conn, old_tag, old_redis

    def test_demo_guard_rejects_mainnet_base(self):
        with self.assertRaises(qa.ConnGuardError):
            qa.resolve_base("bingx-x02", "https://open-api.bingx.com", "0")


if __name__ == "__main__":
    unittest.main()
