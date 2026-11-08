"""Offline transport regression: no exchange calls or credentials."""
import json
import unittest
from x02_low_steps_session import Session, batch_payload, crossed_protection_error


class BatchEncoding(unittest.TestCase):
    def test_both_trigger_directions_use_scoped_recovery(self):
        self.assertTrue(crossed_protection_error('VST API 110413: Take Profit price should be greater than the current price'))
        self.assertTrue(crossed_protection_error('VST API 110414: Take Profit price should be lower than the current price'))
        self.assertTrue(crossed_protection_error('VST API 110411: Stop Loss price should be lower than the current price'))
        self.assertTrue(crossed_protection_error('VST API 110412: Stop Loss price should be greater than the current price'))
        self.assertFalse(crossed_protection_error('VST API 109500: unknown status'))
        self.assertFalse(crossed_protection_error('transport timeout'))

    def test_market_and_protection_json_numbers_preserve_journal_values(self):
        rows=[dict(symbol='ETH-USDT',type='MARKET',quantity='0.004',clientOrderID='Own-123'),
              dict(symbol='ETH-USDT',type='STOP_MARKET',quantity='0.004',stopPrice='2894.27',closePosition='true')]
        before=json.dumps(rows)
        parsed=json.loads(batch_payload(rows))
        self.assertEqual(parsed[0]['quantity'],.004)
        self.assertEqual(parsed[1]['stopPrice'],2894.27)
        self.assertEqual(parsed[0]['clientOrderID'],'Own-123')
        self.assertEqual(parsed[1]['closePosition'],'true')
        self.assertEqual(json.dumps(rows),before)

    def test_rejects_invalid_batch_before_dispatch(self):
        for rows in ([],[{}]*6,[{'quantity':'nan'}],[{'stopPrice':'0'}],[{'quantity':'-1'}]):
            with self.assertRaises(ValueError):batch_payload(rows)

    def test_missing_settlement_cannot_be_reported_as_profit(self):
        class API:
            def order(self,*args):raise RuntimeError('missing terminal close')
            def rows(self,*args):return []
            def call(self,*args):return {'data':[]}
        s=Session.__new__(Session);s.api=API();s.save=lambda:None
        entry={'symbol':'X-USDT','positionSide':'LONG','side':'BUY','clientOrderID':'entry','type':'MARKET'}
        close={**entry,'side':'SELL','clientOrderID':'close'}
        s.log=[{'method':'POST','body':entry,'response':{'data':{'order':{'status':'FILLED','side':'BUY','executedQty':'2','avgPrice':'100'}}}},
               {'method':'POST','body':close}]
        s.report={'startedUtcMs':1,'feePolicy':{'takerFeeRate':.0005},'cases':[
            {'symbol':'X-USDT','side':'LONG','step':2,'ratio':.25,'cleaned':True,'simExit':None}]}
        s.finish_stats()
        self.assertFalse(s.report['executionProbeStats']['accountingComplete'])
        self.assertIsNone(s.report['executionProbeStats']['netEstimate'])
        self.assertIsNone(s.report['executionProbeStats']['returnPf'])
        self.assertEqual(len(s.report['unresolvedAccountingOrders']),1)

    def test_crossed_protection_recovers_only_unprotected_probe(self):
        def case(sym):return {'symbol':sym,'side':'LONG','controls':[{'cid':sym+'sl'},{'cid':sym+'tp'}]}
        a,b=case('A'),case('B')
        class API:
            def rows(self,*args,**kwargs):return [dict(clientOrderID=x['cid'],symbol='B',positionSide='LONG',closePosition='true',workingType='MARK_PRICE') for x in b['controls']]
        s=Session.__new__(Session);s.api=API();s.save=lambda:None;closed=[];s.cleanup=lambda c:closed.append(c['symbol'])
        s.recover_crossed_protection([a,b],'VST API 110414: crossed TP')
        self.assertEqual(closed,['A'])
        self.assertEqual(a['protectionDispatchFailure']['action'],'flatten-owned-probe')
        self.assertNotIn('protectionDispatchFailure',b)


if __name__=='__main__':unittest.main()
