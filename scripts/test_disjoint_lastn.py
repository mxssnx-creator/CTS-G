"""Native prefix/replay regression for consecutive signed position blocks."""
import sys,unittest,random,itertools
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"server/pulse"))
from set_engine import StageParams,stage_chain,stage_verdicts

def policy(**kw):
    p=dict(base_long=20,base_short=10,gate_min=20,floor=1.1,max_dd_s=1e12,lookback_s=1e12,consistency=0,main_on=True,main_n=20,short_on=True,short_windows=(10,),short_min=1,short_margin=1,neg_on=False,neg_windows=(10,),real_on=True,real_n=8,disjoint=True,base_min_n=3,main_min_n=3,base_short_positive=False,main_short_positive=False,base_min_positive=False,main_min_positive=False,base_min_avg=True,main_min_avg=True)
    p.update(kw);return StageParams(**p)

class NativeBlocks(unittest.TestCase):
    def test_causal_base_to_main(self):
        values=[2]*20+[-1]*13+[2]
        result=stage_chain([2*i+1 for i in range(34)],[2*i for i in range(34)],values,policy())
        self.assertNotIn(32,result["main"]);self.assertIn(33,result["main"])
        self.assertEqual(result["real"],[],"new Main cannot borrow Base closes")
    def test_native_score_uses_same_signed_slices_without_second_cost(self):
        sys.path.insert(0,str(Path(__file__).resolve().parent/"regression"))
        from gates import _book,_set,O
        b=_book(stagesEnabled=True,realGateEnabled=False,mainNegEnabled=False,lastNDisjoint=True,setGateWindow=20,setGateShortN=10,setGateMinTrades=20,setMinPf=1.1,setGateMinN=3,setGateMinMode="avg-negative",setGateShortMode="negative")
        b.base_min_n=3;b.base_min_positive=False;b.base_min_avg=True
        st=_set(b);cf=b.cost_pct/100
        vals=[.02]*20+[-.001]*13
        st.hist=[{"t":O+60*i,"pnl":v,"pnl_pct":v+cf,"hold_s":30,"symbol":"X","side":"LONG"} for i,v in enumerate(vals)]
        b._score_one(st,now=O+60*len(vals))
        self.assertEqual((st.gate_n,st.base_short_n,st.base_min_n),(20,10,3))
        self.assertAlmostEqual(st.gate_gp,.4);self.assertAlmostEqual(st.base_short_gl,.01)
        self.assertTrue(st.base_min_ok);self.assertEqual(b.gate_reason(st),"")
        st.hist.pop(0);b._score_one(st,now=O+60*len(vals))
        self.assertEqual(b.gate_reason(st),"n<min")
    def test_all_signed_ranges_against_scalar(self):
        rng=random.Random(38)
        for a,b,c,sp,mp in itertools.product(range(10,71,5),range(5,21,3),range(1,6),(True,False),(True,False)):
            values=[rng.choice([-2,-1,1,3]) for _ in range(a+b+c)]
            p=policy(main_n=a,short_windows=(b,),main_min_n=c,main_short_positive=sp,main_min_positive=mp)
            verdict=stage_verdicts(values,[],[],b,p)
            def positive(x):
                gp=sum(v for v in x if v>0);gl=-sum(v for v in x if v<0)
                return (round(gp/gl,4)>=1.1 if gl else gp>0)
            self.assertEqual(verdict["main"],positive(values[:a]))
            self.assertEqual(verdict["short"],positive(values[a:a+b]) if sp else sum(values[a:a+b])<0)
            self.assertEqual(verdict["minimal"],sum(values[-c:])>0 if mp else sum(values[-c:])<0)
            self.assertFalse(stage_verdicts(values[1:],[],[],b,p)["main"])

if __name__=="__main__":unittest.main()
