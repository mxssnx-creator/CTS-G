"""Bounded work, lossless numeric storage and trusted compressed checkpoints."""
from array import array
from concurrent.futures import Future
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'server/pulse'), str(ROOT/'scripts/regression')]
import stage_eval as sg
import replay_cache
from common import synth_bars


class ImmediatePool:
    """Reject prefetch beyond worker capacity, even when workers finish early."""
    maximum = 0
    outstanding = 0
    calls = 0

    def __init__(self, max_workers, **kwargs):
        self.capacity = max_workers
        type(self).maximum = type(self).outstanding = type(self).calls = 0

    def __enter__(self): return self
    def __exit__(self, *args): return False

    def submit(self, fn, args):
        owner = type(self)
        owner.outstanding += 1
        owner.calls += 1
        owner.maximum = max(owner.maximum, owner.outstanding)
        if owner.outstanding > self.capacity:
            raise AssertionError('unbounded completed results retained before consumption')
        class ConsumedFuture(Future):
            consumed = False
            def result(self, *a, **kw):
                if not self.consumed:
                    self.consumed = True
                    owner.outstanding -= 1
                return super().result(*a, **kw)
        future = ConsumedFuture()
        future.set_result(fn(args))
        return future


def fixture_job(args):
    s, bars, t0 = args
    return s, 1, {'general':[-1,0,1]}, {'general':[.12345678901234568,0.,-.0]}, {
        0:(array('i',[1]),array('i',[2]),array('d',[.00012345678901234568])), -1:{}}


class BoundedBuildTests(unittest.TestCase):
    def test_completed_work_never_accumulates_past_the_worker_budget(self):
        names = [f'S{i}' for i in range(9)]
        bars = {s:[[1,1,1,1,1]]*3 for s in names}
        times = {s:[0,60,120] for s in names}
        with patch.object(sg,'ProcessPoolExecutor',ImmediatePool), patch.object(sg,'_symbol_job',fixture_job), patch.dict(os.environ,{'STAGE_SYMBOL_CHECKPOINT_DIR':''}):
            result = sg.build(names,bars,times,2)
        self.assertEqual(ImmediatePool.maximum,2)
        self.assertEqual(ImmediatePool.calls,9)
        for s in names:
            self.assertEqual(list(result[s][1]['general']),[-1,0,1])
            self.assertEqual(result[s][2]['general'].tobytes(),array('d',[.12345678901234568,0.,-.0]).tobytes())

    def test_compressed_checkpoints_resume_without_running_the_producer(self):
        names=['A','B','C'];bars={s:[[1.,1.,1.,1.,1.]]*3 for s in names};times={s:[0,60,120] for s in names}
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,{'STAGE_SYMBOL_CHECKPOINT_DIR':td,'STAGE_CHECKPOINT_COMPRESS':'1'}), patch.object(sg,'replay_overlay',return_value={'fixture':True}), patch.object(sg,'ProcessPoolExecutor',ImmediatePool):
            with patch.object(sg,'_symbol_job',fixture_job): expected=sg.build(names,bars,times,2)
            with patch.object(sg,'_symbol_job',side_effect=AssertionError('checkpoint producer unexpectedly reran')):
                actual=sg.build(names,bars,times,2)
            self.assertEqual(expected,actual)
            self.assertEqual(ImmediatePool.calls,0)
            self.assertEqual(len(list(Path(td).glob('*.pkl.gz'))),3)
            changed={**bars,'A':[[2.,2.,2.,2.,1.]]*3}
            with self.assertRaisesRegex(ValueError,'provenance'):
                sg.build(names,changed,times,2)

    def test_native_small_replay_matches_direct_producer(self):
        from set_engine import SetBook
        def book():
            b=SetBook();b.load({'stratGeneral':True,'stratIndications':False,'stratShortRange':False,
                'setMinStep':2,'setStepMax':3,'setSlRatios':[1.0],'holdEnabled':False,
                'setStepAdapt':False,'pauseMode':'off','positionCostPct':.11})
            return b
        names=['A','B'];bars={s:synth_bars(i+1,150)for i,s in enumerate(names)};times={s:[60*i for i in range(150)]for s in names}
        with patch.object(sg,'make_book',side_effect=book),patch.dict(os.environ,{'STAGE_SYMBOL_CHECKPOINT_DIR':''}):
            expected={}
            for s in names:
                _,w,d,c,o=sg._symbol_job((s,bars[s],times[s][0]));expected[s]=(w,d,c,o)
            actual=sg.build(names,bars,times,2)
        for s in names:
            self.assertEqual(expected[s][0],actual[s][0])
            self.assertEqual(expected[s][1],{p:list(v)for p,v in actual[s][1].items()})
            self.assertEqual(expected[s][2],{p:list(v)for p,v in actual[s][2].items()})
            self.assertEqual(expected[s][3],actual[s][3])

    def test_gzip_is_lossless_smaller_and_integrity_checked(self):
        value={'n':array('d',[.12345678901234568,-.0]*10000),'side':array('i',[-1,0,1])}
        with tempfile.TemporaryDirectory() as td:
            raw=str(Path(td)/'cache.pkl');packed=raw+'.gz';identity={'fixture':'trusted'}
            replay_cache.save(raw,value,identity);replay_cache.save(packed,value,identity)
            actual=replay_cache.load(packed,identity)
            self.assertEqual(actual['n'].tobytes(),value['n'].tobytes())
            self.assertLess(Path(packed).stat().st_size,Path(raw).stat().st_size/10)
            self.assertEqual(replay_cache.load(raw,identity),value)
            content=Path(packed).read_bytes();Path(packed).write_bytes(content[:-1])
            with self.assertRaisesRegex(ValueError,'provenance'):
                replay_cache.load(packed,identity)


if __name__=='__main__': unittest.main()
