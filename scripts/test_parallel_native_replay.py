"""Separate raw calculation processes must reproduce native tapes and scoring."""
from pathlib import Path
import sys
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts/regression'),str(ROOT/'server/pulse')]
from common import SMALL,T0,overlay,synth_bars
from set_engine import SetBook
from parallel_native_replay import ParallelReplay


class NativeWorkers(unittest.TestCase):
    def make(self,**extra):
        b=SetBook();b.load(overlay(**{**SMALL,'holdAuto':False,**extra}))
        b.bars={f'S{i}-USDT':synth_bars(i+1,180)for i in range(4)}
        b.bar_t={s:T0-60 for s in b.bars}
        return b

    def signature(self,b):
        return ({s:{k:list(v)for k,v in tapes.items()}for s,tapes in b._hist_rows.items()},
                [(st.id,st.gate_n,st.gate_pf,st.proc,st.exe,st.deact_reason)for st in b.by_idx])

    def test_two_workers_equal_native_with_bounded_submissions_and_live_clock(self):
        for mode in ['off','fixed','auto','hold']:
            extra=dict(pauseMode='off' if mode=='hold' else mode,holdAuto=mode=='hold',
                       pauseFixedMin=3,pauseCandidates=[2,3],pauseAutoMin=2,pauseAutoMax=8)
            reference,actual=self.make(**extra),self.make(**extra)
            reference.replay_all(now=T0)
            workers=ParallelReplay(actual,2)
            try:
                actual.replay_all(now=T0)
                self.assertEqual(actual.progress.errors,0,actual.progress.error)
                self.assertEqual(workers.fallbacks,0,workers.last_error)
                self.assertEqual(workers.completed,4)
                self.assertLessEqual(len(workers.pending),2)
                self.assertEqual(self.signature(actual),self.signature(reference),mode)
                self.assertEqual(actual.pause_now,reference.pause_now)
                # A later pass has its own immutable frame and still runs its
                # own scoring; no qualification result is reused from workers.
                actual.replay_all(now=T0+60,symbols=['S0-USDT'])
                reference.replay_all(now=T0+60,symbols=['S0-USDT'])
                self.assertEqual(self.signature(actual),self.signature(reference))
            finally:workers.close()

    def test_worker_failure_retains_native_results_and_never_marks_missing_rows_qualified(self):
        def submit(*args):
            future=Future();future.set_exception(RuntimeError('deliberate calculation-worker failure'));return future
        fake=SimpleNamespace(submit=submit,_processes={},shutdown=lambda **kwargs:None)
        reference,actual=self.make(),self.make()
        reference.replay_all(now=T0)
        with patch('parallel_native_replay.ProcessPoolExecutor',return_value=fake):
            workers=ParallelReplay(actual,2)
            try:
                actual.replay_all(now=T0)
                self.assertEqual(workers.fallbacks,4)
                self.assertEqual(workers.completed,0)
                self.assertEqual(actual.progress.errors,0)
                self.assertEqual(self.signature(actual),self.signature(reference))
            finally:workers.close()


if __name__=='__main__':unittest.main(verbosity=2)
