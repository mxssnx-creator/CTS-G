"""Independent native oracles for causal replay prefix indexing."""
from array import array
from dataclasses import replace
import random
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'server/pulse'), str(ROOT/'scripts/regression')]
import stage_eval as sg
from indexed_stage_query import IndexedStages
from common import overlay


class IndexedQuery(unittest.TestCase):
    def fixture(self, seed=17, n=320):
        rng=random.Random(seed)
        tx=sorted(float(1000+30*rng.randrange(n)) for _ in range(n))
        te=[t-30*rng.randrange(1,20) for t in tx]
        pp=[rng.choice([-.013,-.007,.003,.009,.021])+.00176 for _ in tx]
        book=sg.se.SetBook();book.load(overlay(mainGateEnabled=True, mainShortEnabled=True,
            mainShortWindows=[3,5,8],setGateWindow=8,setGateShortN=4,mainGateN=8,realGateN=5))
        st=book.by_idx[0]
        params=replace(book.stage_params(st.pack,st),lookback_s=2100,base_long=8,base_short=4,
                       gate_min=3, main_n=8,short_windows=(3,5,8),short_min=2,short_margin=1.05,
                       real_n=5,neg_windows=(3,5),max_dd_s=9000)
        fake=SimpleNamespace(history_bars=lambda:100000, evidence_bars=lambda:35,
                             stage_params=lambda *args:params)
        tapes=sg.Tapes.__new__(sg.Tapes);tapes.cf=.00176
        tapes.by_set={st.idx:tuple(array('d',x)for x in (tx,te,pp))}
        return fake,tapes,st,params

    def assert_queries(self,book,tapes,st,adapter):
        tx=tapes.by_set[st.idx][0]
        for now in [tx[0]-1,*tx[::11],tx[-1],tx[-1]+.01,tx[-1]+2500]:
            native,nets=adapter.native['chain_at'](book,tapes,st,now)
            actual,used=adapter.chain_at(book,tapes,st,now)
            self.assertEqual(actual,native,(now,native,actual))
            self.assertEqual(used,nets)
            for lane in ['pos','neg']:
                for name in ['lane_stream','lane_stream_entries']:
                    self.assertEqual(getattr(adapter,name)(book,tapes,st,now,lane),
                                     adapter.native[name](book,tapes,st,now,lane))

    def test_native_prefix_parity_with_ties_signs_floors_and_negative_veto(self):
        for seed in [17,31]:
            book,tapes,st,params=self.fixture(seed)
            for strict in [False,True]:
                for minimal in [0,2]:
                    for short in [False,True]:
                        cfg=replace(params,full_window=strict,base_min_n=minimal,main_min_n=minimal,
                            short_on=short,base_min_both=True,main_min_avg=True,
                            negative_filter={'windows':[3,5],'mode':'any','maxPf':.8} if seed==31 else None,
                            size_floors=((4,1.01),(10,1.1)),long_floor=1.2)
                        book.stage_params=lambda *args:cfg
                        adapter=IndexedStages(sg)
                        self.assert_queries(book,tapes,st,adapter)
                        self.assertEqual(adapter.builds,1)

    def test_dynamic_sources_are_never_cached_as_immutable(self):
        book,tapes,st,_=self.fixture()
        source=SimpleNamespace(by_set=tapes.by_set)
        tapes.tape_source=lambda *args:source
        adapter=IndexedStages(sg)
        self.assert_queries(book,tapes,st,adapter)
        self.assertEqual(adapter.builds,0)
        self.assertGreater(adapter.fallbacks,0)

    def test_changed_parameters_and_cost_rebuild_and_expired_context_falls_back(self):
        book,tapes,st,params=self.fixture()
        adapter=IndexedStages(sg);now=tapes.by_set[st.idx][0][-1]
        adapter.chain_at(book,tapes,st,now)
        book.stage_params=lambda *args:replace(params,floor=params.floor+1)
        self.assert_queries(book,tapes,st,adapter)
        self.assertEqual(adapter.builds,2)
        tapes.cf=.002
        self.assert_queries(book,tapes,st,adapter)
        self.assertEqual(adapter.builds,3)
        book.history_bars=lambda:50
        self.assert_queries(book,tapes,st,adapter)
        self.assertGreater(adapter.fallbacks,0)

    def test_future_outcomes_cannot_change_an_earlier_query(self):
        book,tapes,st,_=self.fixture();tx,te,pp=tapes.by_set[st.idx]
        now=tx[len(tx)//2]
        before=IndexedStages(sg).chain_at(book,tapes,st,now)
        for i,t in enumerate(tx):
            if t>=now:pp[i]=-1000 if i%2 else 1000
        after=IndexedStages(sg).chain_at(book,tapes,st,now)
        self.assertEqual(before,after)

    def test_native_and_indexed_portfolio_orders_are_identical(self):
        book=sg.se.SetBook();book.load(overlay(histLookbackBars=120,histLookbackAuto=False,
            setMinStep=2,setStepMax=3,setSlRatios=[.5,1],stratTrailing=False,exitIgnoreTp=False,
            mainGateEnabled=True,mainShortEnabled=True,setGateWindow=8,setGateShortN=4,
            mainGateN=5,mainShortWindows=[2,3],mainShortMinOrders=2,realGateN=3,
            setGateMaxDdH=9,holdEnabled=False,setGateMinTrades=1,setMinPf=.01,
            setGateMinN=0,mainMinN=0,minPfStepAuto=False,stageFullWindow=False,
            setGateConsistencyPf=0))
        names=['A-USDT','B-USDT'];bars={s:[[50*(1.003**i),50*(1.003**(i+1))*1.0001,
            50*(1.003**i)*.9999,50*(1.003**(i+1)),1]for i in range(360)]for s in names}
        times={s:[9960+60*i for i in range(360)]for s in names};shadow={}
        cfg=sg.lane_cfg(book)
        for s in names:
            dirs={p:[1]*360 for p in book.packs}
            confs={p:[1.0]*360 for p in book.packs};out={}
            for st in book.by_idx:
                rows=sg.se.SetLane(st,s).scan(0,360,bars[s],dirs[st.pack],confs[st.pack],True,cfg)
                out[st.idx]=tuple(array(code,(r[key]for r in rows))for code,key in
                                  [('i','entry_i'),('i','exit_i'),('d','pnl_pct')])
            shadow[s]=(16,dirs,confs,out)
        tapes=sg.Tapes(book,names,times,shadow);hours=[21600.0,25200.0,28800.0]
        native=sg.execute(book,names,bars,times,shadow,tapes,hours,refresh_s=0,include_open=True)
        adapter=IndexedStages(sg).install()
        from replay_storage import compact_shadow
        before=[(list(v),list(shadow[s][2][p]))for s in names for p,v in shadow[s][1].items()]
        compact_shadow(shadow)
        self.assertEqual(before,[(list(v),list(shadow[s][2][p]))for s in names for p,v in shadow[s][1].items()])
        try:
            indexed=sg.execute(book,names,bars,times,shadow,tapes,hours,refresh_s=0,include_open=True)
        finally:
            for name,fn in adapter.native.items():setattr(sg,name,fn)
        self.assertEqual(indexed,native)
        self.assertTrue(any('net'in row for row in indexed))
        self.assertGreater(adapter.queries,0)


if __name__=='__main__':unittest.main(verbosity=2)
