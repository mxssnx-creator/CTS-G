import sys, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parent/"regression"),str(Path(__file__).resolve().parents[1]/"server/pulse")]
import pulse_trader as mod

class TrailingPacing(unittest.TestCase):
    def trader(self):
        p=mod.Pulse.__new__(mod.Pulse);p.ctrl_skip={};p.contracts={};p._oo_cache={};p.overlay={"ownedQuantityControls":True}
        p.clamp_ctrl_price=lambda pos,kind,price:price;p.sl_legal=lambda pos,price:True
        p.fmt_px=lambda c,v:str(v);p.fmt_qty=lambda c,v:str(v);p.cid=lambda *a,**kw:mod.TAG+"owned"
        p.desired_sl_tp=lambda pos:(pos.sl,pos.tp);p.ok=lambda r:r.get("code")==0
        calls=[]
        p.api=SimpleNamespace(cancel_replace_ok=True,cancel_replace=lambda body:(calls.append(body) or {"code":0,"data":{"orderId":str(200+len(calls))}}))
        p.place_ctrl=lambda *a:""
        return p,calls
    def pos(self,identity):
        return mod.Position("BTC-USDT","LONG",1,100,1,95,110,100,sl_oid="101",tp_oid="102",set_id=identity,client_id=identity)
    def test_bursts_are_bounded_and_same_symbol_lanes_do_not_share_success_delay(self):
        p,calls=self.trader();a,b=self.pos("a"),self.pos("b")
        with patch.object(mod.time,"time",return_value=100):
            self.assertTrue(p.replace_sl(a,96))
            for _ in range(100):self.assertFalse(p.replace_sl(a,97))
            self.assertFalse(p.replace_sl(b,96))
        self.assertEqual(len(calls),1);self.assertEqual(a.ctrl_sync_after,100.5);self.assertEqual(b.ctrl_sync_after,0)
        with patch.object(mod.time,"time",return_value=100.51):self.assertTrue(p.replace_sl(b,97))
        self.assertEqual(len(calls),2);self.assertNotIn("sync:BTC-USDT",p.ctrl_skip)
        self.assertEqual(calls[0]["quantity"],"1");self.assertNotIn("closePosition",calls[0])
    def test_unknown_native_receipt_keeps_failure_backoff_and_unverified_protection(self):
        p,calls=self.trader();a=self.pos("a")
        p.api.cancel_replace=lambda body:{"code":0,"data":{}}
        with patch.object(mod.time,"time",return_value=100):self.assertFalse(p.replace_sl(a,96))
        self.assertEqual(a.ctrl_sync_after,112);self.assertTrue(a.ctrl_undercovered);self.assertFalse(a.ctrl_verified)

if __name__=="__main__":unittest.main()
