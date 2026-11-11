"""Replay cache isolation, bounded storage and independent native comparison."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts/regression'), str(ROOT/'server/pulse')]
from common import SMALL, T0, overlay, synth_bars
import memo
from set_engine import SetBook


class ReplayMemo(unittest.TestCase):
    def setUp(self):
        memo.clear()

    def tearDown(self):
        memo.clear()

    def book(self):
        b = SetBook()
        b.load(overlay(**SMALL, holdAuto=False))
        b.bars = {f'S{i}-USDT': synth_bars(i,100) for i in range(4)}
        return b

    def test_four_symbol_reuse_keeps_fresh_inputs_and_matches_native(self):
        b=self.book()
        with memo.using():
            first=[]
            for s in b.bars:
                rows={};inputs=b._replay_symbol(s,rows,T0)
                first.append((s,rows,inputs))
            self.assertEqual(memo.STATS['miss'],4)
            for s,expected,inputs in first:
                rows={};again=b._replay_symbol(s,rows,T0)
                self.assertEqual(rows,expected)
                self.assertEqual(again['cand'],inputs['cand'])
                self.assertIsNot(again['dirs'],inputs['dirs'])
            self.assertEqual(memo.STATS['hit'],4)
            # A caller mutating returned evidence cannot poison another book.
            first[0][2]['dirs'][b.packs[0]][0]=12345
            rows={};clean=b._replay_symbol(first[0][0],rows,T0)
            self.assertNotEqual(clean['dirs'][b.packs[0]][0],12345)
            with patch.object(memo,'MODE','verify'):
                b._replay_symbol(first[0][0],{},T0)
            self.assertEqual(memo.STATS['verified'],1)

    def test_policy_changes_do_not_borrow_old_snapshots(self):
        b=self.book();s=next(iter(b.bars))
        with memo.using():
            b._replay_symbol(s,{},T0)
            b.pause_mode='fixed';b.pause_fixed=5
            b._replay_symbol(s,{},T0)
            self.assertEqual(memo.STATS['miss'],2)
            self.assertEqual(memo.STATS['hit'],0)

    def test_admission_windows_reuse_raw_rows_but_run_their_own_native_scoring(self):
        b=self.book();s=next(iter(b.bars))
        with memo.using():
            b._replay_symbol(s,{},T0)
            b.load(overlay(**SMALL,holdAuto=False,setGateWindow=20,mainGateN=12))
            with patch.object(memo,'MODE','verify'):
                b._replay_symbol(s,{},T0)
            self.assertEqual(memo.STATS['hit'],1)
            self.assertEqual(memo.STATS['verified'],1)
            self.assertEqual(b.gate_window,20)

    def test_byte_limit_and_clear_account_for_all_retained_snapshots(self):
        b=self.book()
        with patch.object(memo,'MAX_CACHE_BYTES',100000),memo.using():
            for s in b.bars:b._replay_symbol(s,{},T0)
            self.assertLessEqual(memo._CACHE_BYTES,100000)
            self.assertEqual(memo._CACHE_BYTES,sum(map(len,memo._CACHE.values())))
        memo.clear()
        self.assertEqual(memo._CACHE_BYTES,0)


if __name__=='__main__':unittest.main(verbosity=2)
