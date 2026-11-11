#!/usr/bin/env python3
"""Prepare the native no-Axis admission control from a verified shadow cache."""
import argparse
import json
import os
from pathlib import Path
import pickle
import sys
import time


def main():
    parser=argparse.ArgumentParser()
    for name in ['data','cache','patch-file','out']:parser.add_argument('--'+name,required=True)
    parser.add_argument('--wait-cache',action='store_true')
    parser.add_argument('--indexed-stage',action='store_true')
    args=parser.parse_args()
    if args.wait_cache:
        while not Path(args.cache+'.meta.json').is_file():time.sleep(3)
    here=Path(__file__).resolve().parent
    sys.path[:0]=[str(here),str(here.parent/'server/pulse')]
    os.environ['STAGE_OVERLAY_PATCH']=Path(args.patch_file).read_text()
    import stage_eval as sg
    import replay_cache
    from stack_eval import summarize
    names,bars,times=sg.load_bars(args.data,50);book=sg.make_book()
    fp=replay_cache.fingerprint(args.data,names,sg.replay_overlay(),sg.PULSE,sg.__file__)
    from replay_storage import compact_shadow
    shadow=compact_shadow(replay_cache.load(args.cache,fp));tapes=sg.Tapes(book,names,times,shadow)
    index=None
    if args.indexed_stage:
        from indexed_stage_query import IndexedStages
        index=IndexedStages(sg).install()
    start=max(times[s][0]for s in names)+book.lookback*60
    end=min(times[s][-1]for s in names)
    hours=[float(h)for h in range(((int(start)+3599)//3600)*3600,int(end),3600)][-24:]
    assert len(hours)==24
    print('Axis control started',len(names),len(book.by_idx),flush=True)
    rows=sg.execute(book,names,bars,times,shadow,tapes,hours,refresh_s=0,include_open=True)
    out=Path(args.out);tmp=out.with_suffix(out.suffix+'.tmp')
    tmp.write_bytes(pickle.dumps(dict(fingerprint=fp,rows=rows,stage_query=index.provenance()if index else None)));tmp.replace(out)
    closed=[r for r in rows if 'net'in r]
    print(json.dumps(summarize([(r['t'],r['net'],1)for r in closed],len(closed),0,hours)),flush=True)


if __name__=='__main__':main()
