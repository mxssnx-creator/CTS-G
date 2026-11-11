"""Exact causal prefix queries for immutable replay tapes.

An opt-in replay accelerator, not a different trading rule. Native stage_chain
forms membership once per Set and parameter snapshot. A query exposes only
closes strictly before its clock and recomputes the current short-window vote
from that prefix. Expiring context or dynamic tape adapters use the native
implementation. No indexed membership is reused across parameter changes.
"""
from array import array
import bisect
import hashlib
import json
from pathlib import Path


class IndexedStages:
    def __init__(self, sg):
        self.sg = sg
        self.native = {name: getattr(sg, name) for name in
                       ('chain_at', 'lane_stream', 'lane_stream_entries')}
        self.cache = {}
        self.builds = self.queries = self.fallbacks = 0

    def _index(self, book, tapes, st, now):
        sg = self.sg
        lb = sg.evidence_s(book)
        source = sg.tape_source(tapes, st.idx, now, lb)
        tx, te, pp = source.by_set[st.idx]
        # Re-scoring a retained suffix changes its initial context. Such a
        # query deliberately falls back instead of assuming an eternal prefix.
        if source is not tapes or bisect.bisect_left(tx, now-book.history_bars()*60.0):
            self.fallbacks += 1
            return None
        params = book.stage_params(st.pack, st)
        key = (id(tapes), id(tx), tapes.cf, json.dumps(vars(params), sort_keys=True))
        indexed = self.cache.get(st.idx)
        if indexed is None or indexed['key'] != key:
            se = sg.se
            if any(entry > close for close, entry in zip(tx, te)):
                self.fallbacks += 1
                return None
            nets = array('d', (x-tapes.cf for x in pp))
            chain = se.stage_chain(tx, te, nets, params)
            main = chain['main']
            mt = array('d', (tx[k] for k in main))
            mn = array('d', (nets[k] for k in main))
            votes = {}
            if params.main_on and params.short_on:
                _, _, passes = se._main_windows(mt, mn, [te[k] for k in main], params)
                for w in dict.fromkeys((params.main_n, *params.short_windows)):
                    verdict = passes(w)
                    gains, losses, counts = array('d', [0.0]), array('d', [0.0]), array('i', [0])
                    for include, value in zip(verdict, mn):
                        gains.append(gains[-1] + (value if include and value > 0 else 0.0))
                        losses.append(losses[-1] + (-value if include and value < 0 else 0.0))
                        counts.append(counts[-1] + int(include))
                    votes[w] = (gains, losses, counts)
            indexed = dict(key=key, tx=tx, te=te, nets=nets, params=params,
                           chain=chain, mt=mt, votes=votes,
                           lane_times={k: array('d', (tx[i] for i in chain[k]))
                                       for k in ('main', 'real', 'realneg')})
            self.cache[st.idx] = indexed
            self.builds += 1
        return indexed

    def _chain(self, book, tapes, st, now):
        self.queries += 1
        row = self._index(book, tapes, st, now)
        if row is None:
            return None
        lo = now-self.sg.evidence_s(book)
        chain, params = row['chain'], row['params']
        out = {}
        for lane in ('main', 'real', 'realneg'):
            clocks = row['lane_times'][lane]
            a, b = bisect.bisect_left(clocks, lo), bisect.bisect_left(clocks, now)
            out[lane] = chain[lane][a:b]
            if lane == 'main':
                out['main_context'] = chain['main'][:b]
                out['short_by_order'] = chain['short_by_order'][a:b]
                main_start, main_end = a, b
        selected = params.main_n
        if row['votes']:
            g, l, _ = row['votes'][params.main_n]
            ref = self.sg.se.pf_of(g[main_end]-g[main_start], l[main_end]-l[main_start])
            best = ref
            for w in params.short_windows:
                g, l, count = row['votes'][w]
                if count[main_end]-count[main_start] < params.short_min:
                    continue
                value = self.sg.se.pf_of(g[main_end]-g[main_start], l[main_end]-l[main_start])
                if value > best+self.sg.se.SHORT_EPS and value >= ref*params.short_margin-self.sg.se.SHORT_EPS:
                    best, selected = value, w
        out['short_n'] = selected
        return row, out

    def chain_at(self, book, tapes, st, now):
        got = self._chain(book, tapes, st, now)
        if got is None:
            return self.native['chain_at'](book, tapes, st, now)
        row, chain = got
        end = bisect.bisect_left(row['tx'], now)
        return chain, list(row['nets'][:end])

    def lane_stream(self, book, tapes, st, now, lane):
        got = self._chain(book, tapes, st, now)
        if got is None:
            return self.native['lane_stream'](book, tapes, st, now, lane)
        row, chain = got
        return [(row['tx'][k], row['nets'][k]) for k in chain['realneg' if lane == 'neg' else 'real']]

    def lane_stream_entries(self, book, tapes, st, now, lane):
        got = self._chain(book, tapes, st, now)
        if got is None:
            return self.native['lane_stream_entries'](book, tapes, st, now, lane)
        row, chain = got
        return [(row['tx'][k], row['te'][k], row['nets'][k]) for k in chain['realneg' if lane == 'neg' else 'real']]

    def install(self):
        for name in self.native:
            setattr(self.sg, name, getattr(self, name))
        return self

    def provenance(self):
        return dict(implementation='causal immutable-tape index; native fallback for context expiry',
                    sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    builds=self.builds, queries=self.queries, fallbacks=self.fallbacks)
