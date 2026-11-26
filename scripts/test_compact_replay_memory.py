"""Packed evidence must preserve every value and native learned floor."""
from array import array
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts/regression'),str(ROOT/'server/pulse')]
from common import overlay,SMALL,T0
import stage_eval as sg
from compact_replay_memory import CompactMemory
from replay_storage import release_shadow_trades


class PackedEvidence(unittest.TestCase):
    def test_merged_tapes_keep_stable_symbol_order_with_packed_provenance(self):
        book=SimpleNamespace(cost_pct=0.0,by_idx=[SimpleNamespace(idx=0)])
        names=['B-USDT','A-USDT']
        times={'A-USDT':[100,160,220],'B-USDT':[100,160,220]}
        shadow={
            'A-USDT':(0,{}, {}, {0:(array('i',[0,1]),array('i',[1,2]),array('d',[.01,.03]))}),
            'B-USDT':(0,{}, {}, {0:(array('i',[0,1]),array('i',[1,2]),array('d',[.02,.04]))}),
        }
        tapes=sg.Tapes(book,names,times,shadow)
        closes,entries,pnl=tapes.by_set[0]
        self.assertEqual(list(closes),[220,220,280,280])
        self.assertEqual(list(entries),[160,160,220,220])
        self.assertEqual(list(pnl),[.01,.02,.03,.04])
        symbols=tapes.symbols_by_set[0]
        self.assertEqual(list(symbols),['A-USDT','B-USDT','A-USDT','B-USDT'])
        self.assertEqual(symbols[1:3],('B-USDT','A-USDT'))
        self.assertIsInstance(symbols.ids,array)
        self.assertEqual(symbols.ids.typecode,'H')

    def test_execution_pruning_keeps_signals_and_pause_plan_only(self):
        directions={'p':array('i',[1,-1,0])};confidences={'p':array('d',[.9,.8,0.0])}
        pauses={('p',1):array('H',[0,5,10])}
        shadow={'A-USDT':(12,directions,confidences,{0:(array('i',[0]),array('i',[1]),array('d',[.01])),
                                                        -1:pauses})}
        release_shadow_trades(shadow)
        warm,got_directions,got_confidences,rows=shadow['A-USDT']
        self.assertEqual(warm,12)
        self.assertIs(got_directions,directions);self.assertIs(got_confidences,confidences)
        self.assertEqual(rows,{-1:pauses});self.assertIs(rows[-1],pauses)

    def test_prefixes_and_hourly_learning_equal_native_without_object_copies(self):
        book=sg.se.SetBook();book.load(overlay(**{**SMALL,'holdAuto':False,'minPfByStep':True,'minPfStepAuto':True,'minPfStepAutoN':10}))
        book.by_idx=book.by_idx[:8];book.lookback=120
        tx=array('d',(T0-100000+i*60 for i in range(1200)))
        by={st.idx:(array('d',tx),array('d',(t-120 for t in tx)),array('d',((i%7-2)*.0012+st.idx*.00001 for i in range(len(tx)))))for st in book.by_idx}
        def tapes():
            t=sg.Tapes.__new__(sg.Tapes);t.by_set=by;t.cf=.0011;t.symbols_by_set={i:tuple('A'if k%2 else'B'for k in range(len(tx)))for i in by};return t
        native_tapes=tapes();reference=sg.AutoFloors(book,native_tapes)
        native_prefix=tuple(list(x)for x in native_tapes.prefix(book.by_idx[0].idx))
        adapter=CompactMemory(sg).install()
        try:
            other=sg.se.SetBook();other.load(overlay(**{**SMALL,'holdAuto':False,'minPfByStep':True,'minPfStepAuto':True,'minPfStepAutoN':10}))
            other.by_idx=other.by_idx[:8];other.lookback=120
            packed_tapes=tapes();packed=sg.AutoFloors(other,packed_tapes)
            self.assertEqual(tuple(list(x)for x in packed_tapes.prefix(book.by_idx[0].idx)),native_prefix)
            self.assertTrue(all(isinstance(x,array)for x in packed_tapes.prefix(book.by_idx[0].idx)))
            self.assertEqual(len(reference.sets),len(packed.sets))
            for st,raw,compact in zip(book.by_idx,reference.sets,packed.sets):
                self.assertEqual(raw[0],compact[0]);self.assertEqual(list(raw[1]),list(compact[1]))
                self.assertEqual(list(raw[2]),list(compact[2]));self.assertEqual(list(raw[3]),list(compact[3]))
                self.assertIs(compact[1],by[st.idx][0])
            for now in range(int(T0-36000),int(T0),3600):
                self.assertEqual(reference.apply(book,now),packed.apply(other,now))
                self.assertEqual(book.step_floor_auto,other.step_floor_auto)
        finally:adapter.restore()


if __name__=='__main__':unittest.main(verbosity=2)
