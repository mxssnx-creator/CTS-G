import pathlib,sys,unittest
from copy import deepcopy
import numpy as np
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
from replay_top25 import variants,selection_key,causal_gates,AXES
from replay_five_days import replay

class Top25Research(unittest.TestCase):
    def test_holdout_cannot_change_selection_order(self):
        row={'identity':'fixed','selectionMetrics':{'trainPf':.9,'trainN':20,'trainDdPct':3},'originalMetrics':{'holdoutPf':0}}
        changed=deepcopy(row);changed['originalMetrics']={'holdoutPf':'∞','holdoutNetPct':99999}
        self.assertEqual(selection_key(row),selection_key(changed))

    def test_cartesian_additional_strategy_coverage(self):
        cfg=dict(strategy='base',levels=0,incrementPct=0,volumeRatio=0,tpPct=.5,slPct=.2)
        rows=variants(cfg)
        self.assertEqual(len(rows),494)
        self.assertEqual(sum(r['mode']=='Baseline' for r in rows),1)
        self.assertEqual(sum(r['mode']=='Block' for r in rows),18)
        self.assertEqual(sum(r['mode']=='Axis' for r in rows),25)
        self.assertEqual(sum(r['mode']=='Axis + Block' for r in rows),450)
        self.assertEqual(len({(r['axis'],r['axisCount'],r['levels'],r['volumeRatio']) for r in rows}),494)

    def test_scaled_axis_block_is_additive_and_capped_relative_to_child(self):
        bars=[[100.,100.,100.,100.,1.] for _ in range(10)]
        bars[1]=[100.,100.4,100.,100.3,1.]
        cfg=[dict(strategy='block',levels=6,incrementPct=.2,volumeRatio=1,tpPct=5,slPct=5,entryVolumeRatio=.12)]
        r=replay(bars,[(1,1)]+[(0,0)]*9,1,cfg,warmup=0,cost_pct=0)[0]
        self.assertAlmostEqual(r['maxVolume'],.24)
        self.assertEqual(r['additions'],1)

    def test_entry_filter_does_not_remove_shadow_observations(self):
        cfg=[dict(strategy='base',levels=0,incrementPct=0,volumeRatio=0,tpPct=.5,slPct=.2)]
        bars=[[100.,101.,99.,100.,1.] for _ in range(20)];signals=[(1,1)]*20
        tape=[]
        shadow=replay(bars,signals,1,cfg,warmup=0,on_close=tape.append)[0]
        blocked=replay(bars,signals,1,cfg,warmup=0,entry_filter=lambda i:[False])[0]
        self.assertGreater(shadow['n'],0);self.assertEqual(blocked['n'],0);self.assertEqual(len(tape),shadow['n'])

    def test_gate_is_effective_next_bar_and_future_rows_do_not_change_past(self):
        bars=[[100.,100.,100.,100.,1.] for _ in range(110)]
        tape=[dict(t=60+i,bar=60+i,pnl=1.,pnl_pct=.0115,qty=1,entry=100,position_cost_pct=.15) for i in range(8)]
        allowed,_,events,_,isolated=causal_gates(bars,tape,np.zeros(110),{})
        self.assertFalse(allowed[67].any());self.assertTrue(allowed[68].any())
        future=tape+[dict(t=100,bar=100,pnl=-20,pnl_pct=-.2,qty=1,entry=100,position_cost_pct=.15)]
        again,_,_,_,_=causal_gates(bars,future,np.zeros(110),{})
        np.testing.assert_array_equal(allowed[:101],again[:101])
        self.assertTrue(all(e['closedThroughBar']<e['effectiveBar'] for e in events))
        self.assertTrue(isolated[63].any());self.assertFalse(allowed[63].any())

if __name__=='__main__':unittest.main()
