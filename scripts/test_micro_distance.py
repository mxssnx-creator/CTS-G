import sys,unittest,math
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"server/pulse"))
from set_engine import SetBook,SetLane

class MicroDistances(unittest.TestCase):
 def make(self,**kw):
  b=SetBook();b.load({"stratGeneral":True,"stratIndications":False,"setMinStep":2,"setStepMax":8,"setSlRatios":[.5,1],"histHonorTp":False,**kw});return b
 def test_own_floor_live_replay_and_independent_books(self):
  old=self.make(minSlDistancePct=.9)
  own=self.make(minSlDistancePct=.9,microMinSlDistancePct=.07,microMinTrailDistancePct=.05,trailMinStep=2)
  cfg={"cost_pct":.11,"min_sl_distance_pct":.9,"entry_conf":0,"time_bars":100,"scratch_bars":100,"scratch_min":0,"cooldown":0,"honor_tp":False}
  st=next(s for s in own.by_idx if s.kind=="base" and s.step==2 and s.sl_ratio==.5 and s.side==1)
  expected=max(.0007,st.tp_pct*.5);self.assertAlmostEqual(own.lane_fracs(st)[0],expected)
  scalar=SetLane(st,"A");scalar.step(0,[100,100,100,100],1,1,True,cfg)
  self.assertAlmostEqual(scalar.open["sl"],100*(1-expected))
  scan=SetLane(st,"A");scan.scan(0,1,[[100,100,100,100]],[1],[1],True,cfg,[0])
  self.assertEqual(scan.open,scalar.open)
  global_st=next(s for s in own.by_idx if s.kind=="base" and s.step==8)
  self.assertGreater(own.lane_fracs(global_st)[0],.009)
  self.assertGreater(old.lane_fracs(old.sets[st.id])[0],expected)
  trails=[s for s in own.by_idx if s.kind=="trail" and s.trail_arm<=.3+1e-9]
  self.assertTrue(trails);self.assertTrue(all(s.trail_give>=.05 for s in trails));self.assertTrue(any(abs(s.trail_give-.05)<1e-9 for s in trails))
 def test_invalid_floors_rejected(self):
  for x in [-1,float("nan"),float("inf"),11]:
   with self.assertRaises(ValueError):self.make(microMinSlDistancePct=x)

if __name__=="__main__":unittest.main()
