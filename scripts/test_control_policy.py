"""Zero-control admission, independent selection and cache provenance."""
import copy
import pathlib
import sys
import unittest
import numpy as np
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server/pulse'))
from control_policy import ControlRule, METRICS, training_winner
from sweep_seven_days import reusable_group
from validation_policy import control_min_trades
from system_settings import calculation_overlay


def row(**kw):
    m={k:0. for k in METRICS}
    m.update(trainN=10,trainNet=.02,trainCostRSum=10,testN=5,testNet=.005,testCostRSum=5)
    m.update(kw)
    return [m[k] for k in METRICS]


class ControlTests(unittest.TestCase):
    def test_default_off_keeps_actual_negative_and_missing_control(self):
        m=np.array([row(testN=0,testNet=0,testCostRSum=0),row(testNet=-1,testCostRSum=-1000),row()])
        before=m.copy();r=ControlRule().masks(m)
        self.assertTrue(r['qualified'].all())
        self.assertFalse(r['controlChecked'])
        self.assertTrue(r['states']['control-disabled'].all())
        np.testing.assert_array_equal(m,before)
        self.assertLess(r['controlPf'][1],0)
        self.assertFalse(ControlRule().masks([row(trainNet=-1)])['qualified'][0])

    def test_optional_control_five_is_honest_about_missing_and_negative(self):
        r=ControlRule(control_n=5).masks([row(testN=4),row(testNet=-1),row(testCostRSum=0),row()])
        np.testing.assert_array_equal(r['qualified'],[False,False,False,True])
        self.assertTrue(r['controlChecked'])
        for i,key in enumerate(('control-insufficient','control-nonpositive','control-pf','qualified')):
            self.assertTrue(r['states'][key][i])

    def test_every_result_has_exactly_one_status_for_both_modes(self):
        m=[row(trainN=0),row(trainNet=-1),row(trainCostRSum=0),row(testN=0),row(testNet=-1),row(testCostRSum=0),row()]
        for n in (0,5,8):
            states=ControlRule(control_n=n).masks(m)['states']
            np.testing.assert_array_equal(np.sum(list(states.values()),axis=0),np.ones(len(m)))

    def test_selection_never_reads_holdout_or_admits_negative_training(self):
        m=np.array([row(trainNet=-5),row(trainNet=1),row(trainNet=2)])
        self.assertEqual(training_winner(m,ControlRule()),2)
        for col in (10,11,17,18,19):m[:,col]=float('nan')
        self.assertEqual(training_winner(m,ControlRule(control_n=8)),2)
        self.assertIsNone(training_winner([row(trainNet=-1)],ControlRule()))

    def test_zero_preserved_across_runtime_settings(self):
        for n in (0,5,25,100):
            self.assertEqual(calculation_overlay({'controlMinTrades':n})['controlMinTrades'],n)
            self.assertEqual(control_min_trades(str(n)),n)
        self.assertEqual(calculation_overlay({})['controlMinTrades'],0)
        for bad in (None,float('nan'),float('inf'),-1,True):self.assertEqual(control_min_trades(bad),0)
        for bad in (-1,False,1.5):
            with self.assertRaises(ValueError):ControlRule(control_n=bad)

    def test_cache_rejects_different_data_period_symbol_and_control_rule(self):
        source=dict(sha256='candles-v1',start=1,end=2,symbol='SOL-USDT',source='exchange')
        saved=dict(signature='code-v1',source=copy.deepcopy(source),controlRule=ControlRule().settings())
        self.assertTrue(reusable_group(saved,source,'code-v1',ControlRule()))
        for field,value in (('sha256','new-prices'),('start',0),('end',3),('symbol','BCH-USDT'),('source','synthetic')):
            self.assertFalse(reusable_group(saved,dict(source,**{field:value}),'code-v1',ControlRule()))
        self.assertFalse(reusable_group(saved,source,'code-v2',ControlRule()))
        self.assertFalse(reusable_group(saved,source,'code-v1',ControlRule(control_n=5)))


if __name__=='__main__':unittest.main()
