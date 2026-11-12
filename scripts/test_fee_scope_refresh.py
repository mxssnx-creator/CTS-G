"""Venue fee scope, failure isolation and freshness; no live transport."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server/pulse'))
from execution_cost import FeeSchedule


class FeeScope(unittest.TestCase):
    def test_bad_symbol_never_starves_other_symbol_or_loses_its_own_cooldown(self):
        for raises in [False, True]:
            with self.subTest(raises=raises):
                calls=[]
                def get(path, params):
                    calls.append(params)
                    if params['symbol']=='A':
                        if raises:raise TimeoutError()
                        return {'code':-1}
                    return {'code':0,'data':{'makerCommissionRate':0,'takerCommissionRate':.0002}}
                schedule=FeeSchedule();api=SimpleNamespace(get=get,commission_scope='symbol')
                self.assertFalse(schedule.refresh(api,['A','B'],10_000))
                self.assertTrue(schedule.refresh(api,['A','B'],10_002))
                self.assertFalse(schedule.refresh(api,['A','B'],10_004))
                self.assertEqual(calls,[{'symbol':'A'},{'symbol':'B'}])
                self.assertEqual(schedule.rates('B')[:2],(0,.0002))
                self.assertFalse(schedule.refresh(api,['A','B'],10_061))
                self.assertEqual(calls[-1],{'symbol':'A'})

    def test_account_failure_has_one_shared_cooldown_then_one_measured_response(self):
        calls=[]
        def get(path, params):
            calls.append(params)
            return {'code':-1}if len(calls)==1 else {'code':0,'data':{'commission':{'makerCommissionRate':.0002,'takerCommissionRate':.0005}}}
        schedule=FeeSchedule();api=SimpleNamespace(get=get,commission_scope='account')
        self.assertFalse(schedule.refresh(api,['A','B'],10_000))
        self.assertFalse(schedule.refresh(api,['A','B'],10_002))
        self.assertEqual(calls,[{}])
        self.assertTrue(schedule.refresh(api,['A','B'],10_061))
        self.assertEqual(calls,[{},{}])
        self.assertEqual(schedule.rates('A'),schedule.rates('B'))
        self.assertEqual(schedule.rates('B')[:2],(.0002,.0005))

    def test_future_dated_cache_does_not_block_fresh_measurement(self):
        schedule=FeeSchedule();schedule.account['A']={'t':100_000,'maker':.001,'taker':.002}
        calls=[];api=SimpleNamespace(commission_scope='account',get=lambda *a:calls.append(a)or
            {'code':0,'data':{'makerCommissionRate':.0002,'takerCommissionRate':.0005}})
        self.assertTrue(schedule.refresh(api,['A'],10_000))
        self.assertEqual(len(calls),1)
        self.assertEqual(schedule.account['A']['t'],10_000)


if __name__=='__main__':unittest.main()
