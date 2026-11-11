"""Bounded process workers for native raw replay; scoring stays in SetBook.

Only candles and numeric engine configuration reach workers. Orders, fees,
positions, qualification and the exchange client remain in the parent process.
"""
from array import array
from concurrent.futures import ProcessPoolExecutor, TimeoutError
import hashlib
import json
import multiprocessing
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server/pulse'))
import set_engine as se

_BOOK=None
_POLICY=None
_SOURCE=hashlib.sha256(Path(se.__file__).read_bytes()).hexdigest()
_RAW=('entry_conf','hist_time_bars','scratch_s','scratch_min','cooldown_bars','cost_pct','ind_settings',
      'min_sl_distance_pct','hist_honor_tp','warmup','hold_auto','direction_split','lookback',
      'pause_mode','pause_fixed','pause_candidates','pause_auto_min','pause_auto_max','pause_pack')
_LANE=('id','idx','pack','kind','side','step','tp_pct','sl_ratio','trail_arm','trail_give',
       'trail_sl','hold_bars','pause_bars')


class PauseLookup:
    def __init__(self,values):self.values=values
    def __call__(self,index):return self.values[index]


def _calculate(payload):
    global _BOOK,_POLICY
    overlay,raw,lanes,market,clocks,symbol,now,source=payload
    if source!=_SOURCE:raise ValueError('worker engine source differs from parent')
    policy=json.dumps(overlay,sort_keys=True,separators=(',',':'))
    if _BOOK is None or policy!=_POLICY:
        _BOOK=se.SetBook();_BOOK.load(overlay);_POLICY=policy
    book=_BOOK
    if [st.id for st in book.by_idx]!=[row['id'] for row in lanes]:
        raise ValueError('worker grid differs from native parent')
    for key,value in raw.items():setattr(book,key,value)
    for st,row in zip(book.by_idx,lanes):
        for key,value in row.items():setattr(st,key,value)
    book.bars,book.bar_t=market,clocks
    histories={};inputs=book._replay_symbol(symbol,histories,now)
    # Closures are evaluated over exactly the immutable replay frame, including
    # its final pause decision. No future candle or qualification is supplied.
    count=len(inputs['bars'])
    inputs['pause']={key:(fixed,PauseLookup(array('i',(fn(i) for i in range(count+1)))) if fn else None)
                     for key,(fixed,fn) in inputs['pause'].items()}
    aliases=inputs.get('tape_alias',{})
    packed={sid:se.RowCols(rows)for sid,rows in histories.items()if rows and sid not in aliases}
    for sid,first in aliases.items():
        if histories.get(sid) and first in packed:
            packed[sid]=packed[first].share(histories[sid][0]['set_id'])
    # RowCols uses a process-local word table. Transfer it explicitly so side
    # and exit reason codes can never be decoded using another worker's table.
    codes=list(se._CODES)
    pauses={key:value for key,value in book.pause_now.items()if key[1]==symbol}
    while len(book._sig_cache)>4:book._sig_cache.pop(next(iter(book._sig_cache)))
    return packed,inputs,codes,pauses


class ParallelReplay:
    def __init__(self,book,workers=3,heartbeat=None):
        if not 1<=workers<=4:raise ValueError('native replay workers must be between one and four')
        self.book,self.workers,self.heartbeat=book,workers,heartbeat
        self.original=book._replay_symbol
        self.pool=ProcessPoolExecutor(max_workers=workers,mp_context=multiprocessing.get_context('spawn'))
        self.pending={};self.todo=iter(());self.closed=threading.Event()
        self.completed=self.fallbacks=0
        self.last_error=''
        book._prefetch_replay=self.prefetch
        book._replay_symbol=self.replay

    def prefetch(self,view,names,now):
        if self.closed.is_set():raise RuntimeError('native replay workers stopped')
        for future in self.pending.values():future.cancel()
        self.pending={};self.view=view;self.now=now
        self.started=time.monotonic()
        self.overlay=dict(view.processing_overlay)
        self.raw={key:getattr(view,key) for key in _RAW}
        self.lanes=[{key:getattr(st,key,0) for key in _LANE}for st in view.by_idx]
        self.todo=iter(names)
        for _ in range(self.workers):self._submit_next()

    def _submit_next(self):
        symbol=next(self.todo,None)
        if symbol is None or self.closed.is_set():return
        peers=se._ported_peer_symbols(self.view.packs) if se._ported_pack_sources(self.view.packs) else set()
        names=[s for s in self.view.bars if s==symbol or peers is None or s in peers]
        market={s:self.view.bars[s]for s in names}
        clocks={s:self.view.bar_t[s]for s in names if s in self.view.bar_t}
        payload=(self.overlay,self.raw,self.lanes,market,clocks,symbol,self.now,_SOURCE)
        self.pending[symbol]=self.pool.submit(_calculate,payload)

    def replay(self,symbol,hist,now,on_step=None):
        future=self.pending.pop(symbol,None)
        if future is None:raise RuntimeError('missing prefetched native replay frame')
        if now!=self.now:raise ValueError('native replay clock differs from prefetched frame')
        try:
            while True:
                if self.closed.is_set():raise RuntimeError('native replay workers stopped')
                try:
                    packed,inputs,codes,pauses=future.result(timeout=1)
                    break
                except TimeoutError:
                    self.book.progress.elapsed_ms=(time.monotonic()-self.started)*1000
                    if on_step:on_step()
                    if self.heartbeat:self.heartbeat(self.completed,len(self.pending)+1)
            for sid,cols in packed.items():
                hist.setdefault(sid,[]).extend({'t':cols.t[i],'symbol':cols.symbol,
                    'side':codes[cols.side[i]],'pnl':cols.pnl[i],'pnl_pct':cols.pct[i],
                    'hold_s':cols.hold[i],'reason':codes[cols.reason[i]],'set_id':cols.set_id}
                    for i in range(len(cols)))
            self.book.pause_now.update(pauses)
            self.completed+=1
            return inputs
        except Exception as exc:
            # Unsupported dynamic grids and worker failures retain the exact
            # native implementation. Nothing is invented or marked qualified.
            if self.closed.is_set():raise
            self.fallbacks+=1
            self.last_error=f'{type(exc).__name__}: {exc}'[:200]
            return self.original.__func__(self.view,symbol,hist,now,on_step=on_step)
        finally:
            self._submit_next()

    def close(self):
        self.closed.set()
        self.book.__dict__.pop('_prefetch_replay',None)
        self.book._replay_symbol=self.original
        processes=list((self.pool._processes or {}).values())
        self.pool.shutdown(wait=False,cancel_futures=True)
        # These owned children perform pure calculations, never exchange I/O.
        for process in processes:
            if process.is_alive():process.terminate()
        for process in processes:process.join(timeout=2)
