"""A smaller-than-cap foreign change cannot be closed as this QA run's size."""
import unittest
from types import SimpleNamespace
from pathlib import Path
from x02_demo_acceptance import Acceptance, Client, VenueReject


class Ownership(unittest.TestCase):
    def test_explicit_rejection_never_queries_or_counts_a_fill(self):
        run,closed,case=self.subject(2)
        run.log.append(dict(method='POST',result='unconfirmed-or-rejected',error='VST API 101400: The minimum order amount is 30 DOGE.',
            body=dict(symbol='X-USDT',positionSide='LONG',type='MARKET',side='BUY',clientOrderID='rejected')))
        run.api.order=lambda *a,**k:self.fail('a rejected order must not be queried as an uncertain fill')
        run.cleanup(case);self.assertEqual(closed,[2])

    def test_validation_rejection_is_journaled_distinctly(self):
        import tempfile,json
        with tempfile.TemporaryDirectory() as folder:
            run=Acceptance.__new__(Acceptance);run.out=Path(folder);run.log=[]
            run.save=lambda:None
            def reject(*a,**k):raise VenueReject('VST API 101400: minimum order')
            run.api=SimpleNamespace(call=reject,order=lambda *a:self.fail('no query after explicit rejection'))
            run.prefix='test';run.seq=0
            with self.assertRaises(VenueReject):run.market('X-USDT','LONG','BUY',1)
            self.assertEqual(run.log[0]['result'],'rejected')
    def test_recovery_looks_up_the_exact_original_client_identity(self):
        client=Client.__new__(Client);seen=[]
        client.call=lambda method,path,params:seen.append(params)or dict(data=dict(order=dict(orderId='1')))
        client.order('X-USDT','OriginalMixedId')
        self.assertEqual(seen[0]['clientOrderID'],'OriginalMixedId')
        run=Acceptance.__new__(Acceptance);run.prefix='MixedPrefix';run.seq=0
        self.assertEqual(run.cid(),'mixedprefix1')
    def subject(self,qty):
        run=Acceptance.__new__(Acceptance);run.save=lambda:None;run.check=lambda *a,**k:None
        run.log=[];closed=[]
        for n,side,fill in [(1,'BUY',2),(2,'BUY',1),(3,'SELL',1)]:
            run.log.append(dict(method='POST',result='accepted',
                body=dict(symbol='X-USDT',positionSide='LONG',type='MARKET',side=side,clientOrderID='own'+str(n)),
                response=dict(data=dict(order=dict(orderId=str(n),status='FILLED',executedQty=fill)))))
        quantities=iter([qty,0])
        run.api=SimpleNamespace(position=lambda *a:(next(quantities),[]),rows=lambda *a,**k:[])
        run.market=lambda symbol,position_side,side,amount:closed.append(amount)
        return run,closed,dict(symbol='X-USDT',side='LONG',maxQty=3,controls=[])

    def test_foreign_increase_below_original_cap_is_never_closed(self):
        run,closed,case=self.subject(2.5)
        with self.assertRaisesRegex(RuntimeError,'Concurrent or unaccounted'):run.cleanup(case)
        self.assertEqual(closed,[])

    def test_unaccounted_reduction_is_not_assumed_owned(self):
        run,closed,case=self.subject(1.5)
        with self.assertRaisesRegex(RuntimeError,'Concurrent or unaccounted'):run.cleanup(case)
        self.assertEqual(closed,[])

    def test_exact_owned_remainder_and_duplicate_receipt(self):
        run,closed,case=self.subject(2)
        run.log.append(dict(run.log[0]))
        run.cleanup(case)
        self.assertEqual(closed,[2]);self.assertTrue(case['cleaned'])


if __name__=='__main__':unittest.main(verbosity=2)
