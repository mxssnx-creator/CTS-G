"""Independent cash examples for native fills, partials and fee units."""
import json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import audit_native_x02 as native_audit
from audit_native_x02 import cash_group,position_cash,record_difference


def fill(side,qty,price,commission,position='LONG'):
    return dict(side=side,positionSide=position,volume=str(qty),price=str(price),
                commission=str(commission),currency='USDT')


class NativeCash(unittest.TestCase):
    def test_opposing_parent_errors_cannot_cancel_into_a_passing_audit(self):
        position=dict(entryValue=100,entry=100,exit=102,gross=2,commission=-.202,net=1.798)
        record=dict(pnl=1.798,entry=100,exit=102,pnl_pct=.02,net_pnl_pct=.01798,fees_usdt=.202)
        self.assertTrue(record_difference(record,position)['passed'])
        checks=[record_difference(dict(record,pnl=record['pnl']+error),position)for error in(.01,-.01)]
        self.assertAlmostEqual(sum(r['cash']for r in checks),0)
        self.assertFalse(any(r['passed']for r in checks))
    def test_missing_order_never_proves_an_uncertain_submission_was_not_accepted(self):
        for reply,legacy_cache in [({'code':109421,'msg':'order not exist'},False),
                                   ({'code':-1,'error':True},False),
                                   ({'code':-1,'error':True},True)]:
            with self.subTest(reply=reply,legacy_cache=legacy_cache),tempfile.TemporaryDirectory()as tmp:
                root=Path(tmp);now=time.time()
                body={'symbol':'A-USDT','side':'BUY','positionSide':'LONG','type':'MARKET',
                      'quantity':'1','clientOrderID':'Gtestentry'}
                report={'host':native_audit.HOST,'connection':native_audit.CONN,'createdAt':now-120,
                        'activeEndedAt':now-100,'tag':'Gtest','symbols':['A-USDT'],'cleanup':{}}
                (root/'session.json').write_text(json.dumps(report))
                events=[{'index':0,'result':'pending','method':'POST','path':'/openApi/swap/v2/trade/order','body':body},
                        {'index':0,'result':'response','response':reply}]
                (root/'mutation-journal.jsonl').write_text('\n'.join(json.dumps(r)for r in events))
                credentials=root/'test.json';credentials.write_text(json.dumps({'api_key':'fixture','api_secret':'fixture','base_url':native_audit.HOST}))
                if legacy_cache:
                    (root/'native-order-receipts.json').write_text(json.dumps([{'request':body,'order':
                        {'status':'NOT_ACCEPTED','orderId':'missing:gtestentry'},'fills':[],'terminal':True}]))
                calls=[]
                def get(path,params):
                    calls.append(path)
                    if path.endswith('/order'):return {'code':109421,'msg':'order not exist'}
                    return {'code':0,'data':{'orders':[]}if path.endswith('/openOrders')else[]}
                api=SimpleNamespace(get=get,http=SimpleNamespace(close=lambda:None))
                with patch.object(native_audit,'FastBingX',return_value=api):
                    with self.assertRaisesRegex(ValueError,'exact submitted identity'):
                        native_audit.audit(root,credentials)
                self.assertIn('/openApi/swap/v2/trade/order',calls)
                self.assertFalse((root/'native-fill-audit.json').exists())

    def test_long_partial_exits_pay_each_reported_commission_once(self):
        result=cash_group([fill('BUY',1,100,-.1),fill('SELL',.4,110,-.044),fill('SELL',.6,90,-.054)])
        self.assertEqual(result['gross'],-2)
        self.assertEqual(result['commission'],-.198)
        self.assertEqual(result['net'],-2.198)
        self.assertEqual(result['quantity'],1)

    def test_short_partial_exits_use_the_same_quantity_and_fee_units(self):
        result=cash_group([fill('SELL',1,100,-.1,'SHORT'),fill('BUY',.4,110,-.044,'SHORT'),fill('BUY',.6,90,-.054,'SHORT')])
        self.assertEqual(result['gross'],2)
        self.assertEqual(result['net'],1.802)

    def test_open_inventory_cannot_be_reported_as_settled(self):
        with self.assertRaisesRegex(ValueError,'not flat'):
            cash_group([fill('BUY',1,100,-.1),fill('SELL',.4,110,-.044)])

    def test_nonfinite_values_and_unconverted_commission_currency_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'nonfinite'):cash_group([fill('BUY','NaN',100,-.1)])
        bad=fill('BUY',1,100,-.1);bad['currency']='BTC'
        with self.assertRaisesRegex(ValueError,'currency'):cash_group([bad])

    def test_external_exit_uses_exact_position_cash_and_full_precision_commission(self):
        entry=dict(positionID=202,symbol='CT-USDT',positionSide='SHORT',executedQty='18.90',
                   avgPrice='.10597',time=1000000,clientOrderId='gown')
        row=dict(positionId=202,symbol='CT-USDT',positionSide='SHORT',closeAllPositions=True,
                 positionAmt='18.90',closePositionAmt='18.90',avgPrice='.10597',avgClosePrice='.10662',
                 positionCommission='-.0020089755',realisedProfit='-.0123',netProfit='-.0143',
                 openTime=1000000,updateTime=1100000)
        result=position_cash(entry,[row])
        self.assertEqual(result['net'],-.0142939755)
        self.assertEqual(result['gross'],-.012285)
        self.assertEqual(result['client_id'],'gown')
        for key,value in [('positionAmt','20'),('positionId',999),('avgPrice','.11')]:
            with self.assertRaises(ValueError):position_cash(entry,[dict(row,**{key:value})])


if __name__=='__main__':unittest.main(verbosity=2)
