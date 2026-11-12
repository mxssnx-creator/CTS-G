"""Packed evidence must preserve every value and native learned floor."""
from array import array
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts/regression'),str(ROOT/'server/pulse')]
from common import overlay,SMALL,T0
import stage_eval as sg
from compact_replay_memory import CompactMemory


class PackedEvidence(unittest.TestCase):
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
