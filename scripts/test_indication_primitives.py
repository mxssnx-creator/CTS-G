"""Exact numerical oracles and isolation checks for shared indication work."""
from pathlib import Path
import math,random,struct,sys,threading,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server/pulse'))
import indication_lib as lib


def old_tr(b):
    out=[float(b[0][1])-float(b[0][2])]
    for i in range(1,len(b)):
        h,lo,pc=float(b[i][1]),float(b[i][2]),float(b[i-1][3])
        out.append(max(h-lo,abs(h-pc),abs(lo-pc)))
    return out


def old_atr(b,n):
    t=old_tr(b)[-n:]
    return sum(t)/len(t)if t else 0.


def old_ema(vals,n):
    k=2./(n+1.);e=float(vals[0]);out=[]
    for v in vals:
        e=float(v)*k+e*(1-k);out.append(e)
    return out


def old_wma(vals,n):
    out=[]
    for i in range(len(vals)):
        lo=max(0,i-n+1);w=vals[lo:i+1];den=len(w)*(len(w)+1)/2.
        out.append(sum((j+1)*v for j,v in enumerate(w))/den)
    return out


def old_rsi(c,n):
    out=[50.]*len(c)
    if len(c)<=n:return out
    g=l=0.
    for i in range(1,n+1):
        d=c[i]-c[i-1];g+=max(d,0.);l+=max(-d,0.)
    g/=n;l/=n
    out[n]=(50. if g==0 else 100.)if l==0 else 100.-100./(1+g/l)
    for i in range(n+1,len(c)):
        d=c[i]-c[i-1];g=(g*(n-1)+max(d,0.))/n;l=(l*(n-1)+max(-d,0.))/n
        out[i]=(50. if g==0 else 100.)if l==0 else 100.-100./(1+g/l)
    return out


def old_kama(b,n):
    if len(b)<n+4:return None
    c=[float(x[3])for x in b];fast,slow=2./3.,2./31.;k=c[0];ks=[]
    for i in range(len(c)):
        if i>=n:
            path=sum(abs(c[j]-c[j-1])for j in range(i-n+1,i+1))
            er=abs(c[i]-c[i-n])/path if path>0 else 0.;sc=(er*(fast-slow)+slow)**2
            k=k+sc*(c[i]-k)
        else:k=c[i]
        ks.append(k)
    atr=old_atr(b,n)
    if atr<=0:return None
    sl=ks[-1]-ks[-3]
    if c[-1]>ks[-1]and sl>.1*atr:return 1,lib._conf(.58,min(.25,sl/atr*.2))
    if c[-1]<ks[-1]and sl<-.1*atr:return -1,lib._conf(.58,min(.25,-sl/atr*.2))
    return None


def market(count,seed=43):
    rng=random.Random(seed);price=100.;rows=[]
    for i in range(count):
        opening=price;price=max(.01,price+rng.uniform(-1,1))
        rows.append([opening,max(opening,price)+rng.random(),min(opening,price)-rng.random(),price,10.+rng.random()*100])
    return rows


class Primitives(unittest.TestCase):
    def setUp(self):lib.clear_vector_memo()

    def assertBits(self,a,b):
        self.assertEqual([struct.pack('!d',x)for x in a],[struct.pack('!d',x)for x in b])

    def test_primitive_outputs_keep_the_original_order_and_bits(self):
        for length in [1,2,8,30,128,300,512,513]:
            rows=market(length);values=[r[3]for r in rows]
            for n in [1,5,8,14,21,34,55]:
                self.assertBits([lib._atr(rows,n)],[old_atr(rows,n)])
                self.assertBits(lib._ema(values,n),old_ema(values,n))
                self.assertBits(lib._rsi(values,n),old_rsi(values,n))
                self.assertBits(lib._wma(values,n),old_wma(values,n))
                self.assertEqual(lib.f_kama(rows,n),old_kama(rows,n))
        gap=[[10.,11.,9.,10.,1.],[30.,31.,29.,30.,1.],[2.,3.,1.,2.,1.]]
        for n in [0,1,2,3,8,-1]:self.assertBits([lib._atr(gap,n)],[old_atr(gap,n)])

    def test_value_keys_notice_interior_mutation_and_do_not_alias_results(self):
        values=[r[3]for r in market(128)]
        first=lib._ema(values,8);first[0]=99999.
        self.assertBits(lib._ema(values,8),old_ema(values,8))
        self.assertGreater(lib.vector_memo_info()['hits'],0)
        values[20]+=.75
        self.assertBits(lib._ema(values,8),old_ema(values,8))
        self.assertBits(lib._rsi(values,8),old_rsi(values,8))

    def test_ieee_keys_preserve_signed_zero_and_integer_arithmetic(self):
        for values in [[0.,0.],[-0.,-0.],[float('nan'),1.],[1.,float('nan')]]:
            self.assertBits(lib._ema(values,8),old_ema(values,8))
        values=[2**54,2**54+1,2**54-1,2**54+2,2**54]
        self.assertBits(lib._rsi(values,1),old_rsi(values,1))

    def test_cache_stays_bounded_and_thread_local(self):
        for i in range(500):lib._ema([float(i+j)for j in range(300)],8)
        info=lib.vector_memo_info()
        self.assertLessEqual(info['estimatedBytes'],info['maxBytes'])
        self.assertLessEqual(info['entries'],info['maxEntries'])
        results=[]
        def run():
            lib.clear_vector_memo();lib._rsi([float(i)for i in range(80)],8)
            results.append(lib.vector_memo_info())
        thread=threading.Thread(target=run);thread.start();thread.join()
        self.assertEqual(results[0]['entries'],1)
        self.assertEqual(lib.vector_memo_info(),info)

    def test_every_library_name_matches_plain_oracles_on_full_timeframe_history(self):
        rows=market(lib.HISTORY)
        cases=[rows,[[200.-r[0],200.-r[2],200.-r[1],200.-r[3],r[4]]for r in rows]]
        for data in cases:
            lib._tls.memo={};lib.clear_vector_memo()
            actual={name:fn(data)for name,fn in lib.build().items()}
            families=dict(lib.FAMILIES);families['kama']=(families['kama'][0],old_kama)
            with patch.multiple(lib,_atr=old_atr,_ema=old_ema,_rsi=old_rsi,_wma=old_wma,FAMILIES=families):
                lib._tls.memo={}
                expected={name:fn(data)for name,fn in lib.build().items()}
            self.assertEqual(actual,expected)


if __name__=='__main__':unittest.main(verbosity=2)
