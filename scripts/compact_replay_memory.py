"""Lossless binary64 storage for replay consumers; raw replay is unchanged."""
from array import array
import bisect
import hashlib
from pathlib import Path


class Triples:
    def __init__(self):self.times,self.pfs,self.nets=array('d'),array('d'),array('d')
    def append(self,row):
        self.times.append(row[0]);self.pfs.append(row[1]);self.nets.append(row[2])
    def __iter__(self):return zip(self.times,self.pfs,self.nets)
    def __len__(self):return len(self.times)


class CompactMemory:
    def __init__(self,sg):
        self.sg=sg;self.original_floors=sg.AutoFloors;self.original_prefix=sg.Tapes.prefix

    def install(self):
        sg=self.sg;native=self.original_floors
        class PackedFloors(native):
            def __init__(self,book,tapes):
                self.on=bool(getattr(book,'pf_by_step',False)and getattr(book,'pf_step_auto',False))
                self.sets=[];self.last=None
                if not self.on:return
                n_win,cf=int(book.pf_step_auto_n),tapes.cf
                for st in book.by_idx:
                    tx,te,pp=tapes.by_set[st.idx]
                    if len(tx)<=n_win:continue
                    nets=array('d',(value-cf for value in pp));g,l=sg.se.prefix_sums(nets)
                    pairs=Triples()
                    for k in range(len(tx)):
                        end=bisect.bisect_left(tx,te[k])
                        if end<n_win:continue
                        gp,gl=g[end]-g[end-n_win],l[end]-l[end-n_win]
                        if gl>0:pairs.append((tx[k],gp/gl,nets[k]))
                        elif gp>0:pairs.append((tx[k],99.0,nets[k]))
                    sig,run=array('q'),0
                    symbols=getattr(tapes,'symbols_by_set',{}).get(st.idx)
                    if symbols is None:symbols=(f'unknown:{st.id}',)*len(tx)
                    for t,e,x,symbol in zip(tx,te,pp,symbols):
                        run=hash((run,sg.se.auto_evidence_key(t,symbol,x,e,st.side)));sig.append(run)
                    self.sets.append(((book.auto_step_bucket(st),st.side),tx,sig,pairs))
        def packed_prefix(tapes,idx):
            cache=tapes.__dict__.setdefault('_prefix',{})
            if idx not in cache:
                nets=array('d',(x-tapes.cf for x in tapes.by_set[idx][2]))
                g,l=sg.se.prefix_sums(nets)
                cache[idx]=(nets,array('d',g),array('d',l))
            return cache[idx]
        sg.AutoFloors=PackedFloors;sg.Tapes.prefix=packed_prefix
        return self

    def restore(self):
        self.sg.AutoFloors=self.original_floors;self.sg.Tapes.prefix=self.original_prefix

    def provenance(self):
        return dict(implementation='binary64 columns; native learning arithmetic and observation order',
                    sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),rawReplayChanged=False)
