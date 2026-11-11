"""Exact causal prefix queries for immutable replay tapes.

A replay accelerator, not a different trading rule. Native stage_chain
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
from collections.abc import Sequence


class LaneValues(Sequence):
    """Read-only indexed values: last-N slices visit only the requested rows."""
    def __init__(self, values, indices):
        self.values, self.indices = values, indices

    def __len__(self):
        return len(self.indices)

    def __getitem__(self, key):
        if isinstance(key, slice):
            return LaneValues(self.values, self.indices[key])
        return self.values[self.indices[key]]

    def __iter__(self):
        return (self.values[k] for k in self.indices)


class IndexedStages:
    def __init__(self, sg):
        self.sg = sg
        self.native = {name: getattr(sg, name) for name in
                       ('chain_at', 'lane_stream', 'lane_stream_entries', 'stage_lane_stats', 'other_lanes')}
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
        # Keep the causal prefix read-only and bounded at this query's clock.
        # Copying every historic float here dominated allocation in large
        # replay grids, even though callers only read a few indexed windows.
        return chain, memoryview(row['nets'])[:end].toreadonly()

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

    def _stage_view(self, book, tapes, st, now):
        got = self._chain(book, tapes, st, now)
        if got is None:
            return None
        row, chain = got
        values = {lane: LaneValues(row['nets'], chain[lane]) for lane in ('main', 'real', 'realneg')}
        verdict = self.sg.se.stage_verdicts(values['main'], values['real'], values['realneg'], chain['short_n'], row['params'])
        return values, verdict

    def stage_lane_stats(self, book, tapes, st, now):
        got = self._stage_view(book, tapes, st, now)
        if got is None:
            return self.native['stage_lane_stats'](book, tapes, st, now)
        values, verdict = got
        lane = next((lane for lane in getattr(book, 'lane_order', ('pos', 'neg'))
                     if verdict['pos' if lane == 'pos' else 'negl']), '')
        if not lane and (verdict['pos'] or verdict['negl']):
            lane = 'pos' if verdict['pos'] else 'neg'
        return lane, self.sg.se.lane_rank_stats(values['realneg' if lane == 'neg' else 'real'], tapes.cf)

    def other_lanes(self, book, tapes, st, now, lane):
        if not (book.main_on or book.neg_on or book.realst_on):
            return []
        got = self._stage_view(book, tapes, st, now)
        if got is None:
            return self.native['other_lanes'](book, tapes, st, now, lane)
        values, verdict = got
        result, base_rank = [], None
        for candidate in getattr(book, 'lane_order', ('pos', 'neg')):
            if candidate == lane or not verdict['pos' if candidate == 'pos' else 'negl']:
                continue
            if base_rank is None:
                base_rank = self.sg.gate_base(book, tapes, st, now)[1]
            rank = base_rank
            if getattr(book, 'rank_by_lane', False):
                windows = book.win(st)
                stats = self.sg.se.lane_rank_stats(values['realneg' if candidate == 'neg' else 'real'], tapes.cf)
                rank = self.sg.se.lane_rank_of(stats, book.floor_for(windows.real_neg_n if candidate == 'neg' else windows.real_n, st.pack, st), -base_rank[4])
            result.append((candidate, rank))
        return result

    def install(self):
        for name in self.native:
            setattr(self.sg, name, getattr(self, name))
        return self

    def provenance(self):
        return dict(implementation='causal immutable-tape index; native fallback for context expiry',
                    sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    builds=self.builds, queries=self.queries, fallbacks=self.fallbacks)
