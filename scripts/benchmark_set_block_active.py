"""Repeat in-memory Set Block candidate checks for synthetic active parents; no I/O or exchange."""
import argparse
from pathlib import Path
from statistics import median
import sys
import tempfile
from time import perf_counter
import types

p = argparse.ArgumentParser()
p.add_argument('worktree', nargs='?', default=str(Path(__file__).resolve().parents[1]))
p.add_argument('--parents', type=int, default=300)
p.add_argument('--cycles', type=int, default=100)
p.add_argument('--trials', type=int, default=3)
a = p.parse_args()
sys.path.insert(0, str(Path(a.worktree) / 'server/pulse'))
import block_rules as br
from set_block import SetBlockBook
block = types.SimpleNamespace(max_stack=2, volume_ratio=.5, min_samples=1, pf_ratio=.8, default_min_pf=1.1)
cfg = br.clean_cfg({'blockCounts':[{'step':1,'ratio':.5}, {'step':1,'ratio':.5}],
                    'blockSkipFirst':0,'blockMinObservations':1,'blockRelations':['symbol','step'],'blockGuard':True})
with tempfile.TemporaryDirectory() as d:
    book=SetBlockBook(str(Path(d)/'book.json'));book.cfg=cfg
    book.save=lambda:None
    parents=[]
    for i in range(a.parents):
        pos=types.SimpleNamespace(set_id=f'pack:L:1m:sl1.00:st20:{i}',lane='pos',symbol=f'SYM{i%50}-USDT',side='LONG',client_id=f'p{i}',
            opened_at=1.,base_entry=100.,base_qty=1.,qty=1.,entry=100.,tp=102.,block_fills=[],exit_fee_qty=0.,exit_fee_usdt=0.,base_entry_fee_usdt=None)
        book.observe(pos,100.12,10.,block,.1,3600.)
        for key in book.rel_keys(cfg,pos):
            book.ledger(key,create=True)['paper']['1']=[2,.02,.001]
        parents.append(pos)
    seconds=[]; decisions=[]
    for trial in range(a.trials):
        start=perf_counter(); approved=0
        for cycle in range(a.cycles):
            for pos in parents:
                approved+=book.candidate(pos,100.12,100.+cycle,block,.1,3600.,floor=1.1) is not None
        seconds.append(perf_counter()-start);decisions.append(approved)
    assert all(x == a.parents*a.cycles for x in decisions)
    print({'parents':a.parents,'symbols':50,'cycles':a.cycles,'checks':a.parents*a.cycles,'seconds':seconds,
           'medianSeconds':median(seconds),'persistence':'disabled','decisions':decisions})
