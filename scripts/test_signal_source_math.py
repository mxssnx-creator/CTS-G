"""Independent high-precision checks for the new web-researched source math."""
from decimal import Decimal, localcontext
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"server/pulse"))
from signal_sources import states


class SourceMath(unittest.TestCase):
    def test_bollinger_matches_decimal_population_variance(self):
        fixtures=[[100+scale*(1+math.sin(i/11)) for i in range(500)] for scale in (1e-10,1,1e5)]
        fixtures.append([100+(i//20)%6 for i in range(1200)])
        for values in fixtures:
            for label,p in (("short",20),("medium",50)):
                expected=[0]*len(values)
                with localcontext() as ctx:
                    ctx.prec=60
                    for i in range(p-1,len(values)):
                        window=list(map(Decimal.from_float,values[i-p+1:i+1]))
                        mean=sum(window)/p
                        sd=(sum((x-mean)**2 for x in window)/p).sqrt()
                        expected[i]=1 if window[-1]<mean-2*sd else -1 if window[-1]>mean+2*sd else 0
                self.assertEqual(states(values,"bb-bounce",label),expected)

    def test_cci_tiny_moves_match_decimal_mean_deviation(self):
        values=[100+1e-10*math.sin(i/11) for i in range(1200)]
        for label,p in (("short",14),("medium",20)):
            typical=[(x+x+x)/3 for x in values]
            expected=[0]*len(values)
            with localcontext() as ctx:
                ctx.prec=60
                for i in range(p-1,len(values)):
                    window=list(map(Decimal.from_float,typical[i-p+1:i+1]))
                    mean=sum(window)/p
                    mad=sum(abs(x-mean) for x in window)/p
                    expected[i]=1 if window[-1]-mean < -Decimal("1.5")*mad else -1 if window[-1]-mean > Decimal("1.5")*mad else 0
            self.assertEqual(states(values,"cci-reversal",label,values,values),expected)

    def test_flat_sources_neutral_and_volume_required(self):
        flat=[100.]*100
        for source in ("bb-bounce","cci-reversal","obv-trend"):
            self.assertEqual(states(flat,source,"short",flat,flat,[0]*100),[0]*100)
        with self.assertRaises(ValueError):states(flat,"obv-trend","short")
        with self.assertRaises(ValueError):states(flat,"obv-trend","short",volumes=[-1]*100)

    def test_cci_scalar_deviation_and_obv_signed_flow(self):
        values=[100+(i%17)**2/10 for i in range(100)]
        high=[x+1 for x in values];low=[x-1 for x in values];volume=[1+i%7 for i in range(100)]
        for label,p in (("short",14),("medium",20)):
            expected=[0]*len(values)
            for i in range(p-1,len(values)):
                window=values[i-p+1:i+1];mean=sum(window)/p
                mad=sum(abs(x-mean) for x in window)/p
                v=(window[-1]-mean)/(.015*mad) if mad else 0
                expected[i]=1 if v < -100 else -1 if v > 100 else 0
            self.assertEqual(states(values,"cci-reversal",label,high,low),expected)
        flow=[sum(volume[j]*(1 if values[j]>values[j-1] else -1 if values[j]<values[j-1] else 0)
                  for j in range(1,i+1)) for i in range(len(values))]
        for label,p in (("short",20),("medium",50)):
            expected=[0]*len(values);average=sum(flow[:p])/p
            for i in range(p-1,len(values)):
                if i>=p:average+=(flow[i]-average)*2/(p+1)
                expected[i]=1 if flow[i]>average else -1 if flow[i]<average else 0
            self.assertEqual(states(values,"obv-trend",label,high,low,volume),expected)


if __name__=="__main__":unittest.main()
