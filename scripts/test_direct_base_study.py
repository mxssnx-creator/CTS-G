"""Scalar gate oracle and production Axis/RoundsBook parity for the direct simulation."""
import base64
import bisect
import gzip
import re
from collections import defaultdict
import json
import math
import os
from pathlib import Path
import random
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'server/pulse'))
import axis_sets
import block_rounds
import direct_base_grid as direct
import stage_grid


def signed_ok(values, sign, floor):
    g = math.fsum(v for v in values if v > 0)
    loss = -math.fsum(v for v in values if v < 0)
    if sign == 'negative':
        g, loss = loss, g
    return g > 0 and g >= floor*loss and g-loss > stage_grid.NEG_EPS


def base_ok(rule, seen, floor):
    if not rule.native:
        size, offset, sign = rule.blocks[0]
        return len(seen) >= size and signed_ok(seen[-size:], sign, floor)
    w = rule.native
    if len(seen) < w.need:
        return False
    recent = seen[-w.need:]
    return (signed_ok(recent[:w.long], 'positive', floor)
            and signed_ok(recent[w.long:-w.last], w.short_mode, floor)
            and signed_ok(recent[-w.last:], w.last_mode, floor)
            and signed_ok(recent, 'positive', floor))


class GateTests(unittest.TestCase):
    def test_exact_matrix_and_membership_against_entry_time_oracle(self):
        rng = random.Random(121)
        exits = [2*(i//3)+10 for i in range(170)]
        entries = [t-rng.choice([2,4,12,18]) for t in exits]
        nets = [rng.choice([.03125,.0625,-.015625,-.03125,0]) for _ in exits]
        grid = stage_grid.StageGrid.from_settings({'enabled': True})
        native = stage_grid.StageHistory(grid, exits, entries, nets, 85, lambda _: 1.0)
        bases, ends = direct.base_rules(grid), direct.end_rules()
        self.assertEqual([len(bases), len(ends)], [423,76])
        self.assertEqual(len({r.name for r in bases}),423)
        self.assertEqual(len({r.name for r in ends}),76)
        self.assertFalse(any('S16-' in r.name or 'S20-' in r.name for r in ends))
        streams, table, child = direct.memberships(native)
        past = lambda ids, at: [nets[i] for i in ids if at-85 <= exits[i] < at]
        all_ids = range(len(nets))
        prior = [past(all_ids, e) for e in entries]
        for fi, floor in enumerate(direct.BASE_FLOORS):
            for b, base in enumerate(bases):
                pid = table[fi][b]
                expected = tuple(i for i in all_ids if base_ok(base, prior[i], floor))
                self.assertEqual(streams[pid],expected, (base, floor))
            # Test each sign/size and both end floors on dense and sparse parents.
            for b in [0,77,198,399,400,409,414,422]:
                pid = table[fi][b]
                parent = streams[pid]
                windows = {i:past(parent, entries[i]) for i in parent}
                for ei, end_floor in enumerate(direct.END_FLOORS):
                    for e, end in enumerate(ends):
                        admitted = []
                        for i, recent in windows.items():
                            passed = True
                            for size, offset, sign in end.blocks:
                                right = len(recent)-offset
                                if right < size or not signed_ok(recent[right-size:right],sign,end_floor):
                                    passed = False
                                    break
                            if passed: admitted.append(i)
                        self.assertEqual(streams[child[pid][ei][e]],tuple(admitted),(b, e, floor,end_floor))

    def test_future_outcomes_unsupported_ties_and_negative_floor(self):
        grid = stage_grid.StageGrid.from_settings({})
        closes = [10,20,30,40,50]
        entries = [5,10,25,35,45]
        def observed(values):
            h = stage_grid.StageHistory(grid, closes, entries, values, 1000, lambda _: 2)
            return [direct.members(r.mask(h.root),range(5)) for r in direct.end_rules()]
        gates = observed([-.125,.0625,-.0625,.25,.125])
        t_minus = [r.name for r in direct.end_rules()].index('T1-')
        self.assertNotIn(1, gates[t_minus]) # earlier exit shares entry clock
        self.assertIn(3,gates[t_minus])
        self.assertEqual([tuple(i for i in s if i < 4) for s in gates],
                         [tuple(i for i in s if i < 4) for s in observed([-.125,.0625,-.0625,.25,-10])])
        self.assertFalse(gates[0]) # S8+ needs eight complete members
        self.assertTrue(all(not s for s in observed([0]*5)))


def native_overlay(rows, warm, finish, axis, block, floor):
    count, ratio = [(0,0),(1,.1),(1,.2),(2,.1),(2,.2)][block]
    cfg = block_rounds.config_of({'enabled': bool(count), 'relations':['set'], 'maxMult':1.4,
        'auto':False, 'counts': {str(count):{'on':True,'ratio':ratio,'step':True,'maxLevel':2}}})
    book = block_rounds.RoundsBook(cfg=cfg)
    events = []
    for id_,(en,ex,sym,_) in enumerate(rows):
        if warm <= en < finish:
            events.append((en,0,sym,id_))
        if ex is not None:
            events.append((ex,1,sym,id_))
    seen, opened, trades = [], {}, []
    amounts, per_hour = [], defaultdict(list)
    for t,event,sym,id_ in sorted(events):
        en,ex,_,result = rows[id_]
        if event:
            seen.append(result)
            if id_ in opened:
                book.closed(str(id_),result)
                weighted = opened[id_]*result
                amounts.append(weighted)
                per_hour[math.ceil((t-warm)/60)-1].append(weighted)
        elif not axis or axis_sets.rule_ok('pause' if axis<3 else 'prev', axis if axis<3 else 4, seen, floor):
            keys = block_rounds.keys_of(str(sym),1,'test','base',3,'owned','exact')
            weight = book.ask(keys)[0]
            book.opened(str(id_),keys,weight)
            opened[id_] = weight
            trades.append([id_,weight])
    return trades,amounts,per_hour


class OverlayTests(unittest.TestCase):
    def test_all_treatments_match_native_fills_sizes_and_settlement_hours(self):
        rng=random.Random(212)
        rows = [[i, i+rng.randrange(2,36),rng.randrange(8),rng.choice([-.01,-.006,-.002,.003,.009])] for i in range(1,325,3)]
        rows += [[354,None,1,None],[360,None,6,None],[338,360,2,.012]]
        rows.sort(key=lambda r:(r[1] or 361,r[2],r[0]))
        opts={'orders':rows,'stream':list(range(len(rows))),'period':{'warm':60,'minutes':360}}
        node = """import fs from 'node:fs';
import {prepare,axisPlan,measure} from './scripts/direct_base/metrics.mjs';
const d=JSON.parse(fs.readFileSync(0)); const es=prepare(d,d.stream,d.period), all=[];
for (const floor of [2,2.5]) for(let a=0;a<4;a++) for(let b=0;b<5;b++)
 all.push(measure(d,axisPlan(d,d.stream,es,a,floor),d.period,b,true));
console.log(JSON.stringify(all));"""
        result=json.loads(subprocess.run(['node','--input-type=module','-e',node],cwd=ROOT,
            text=True,input=json.dumps(opts),capture_output=True,check=True).stdout)
        for floor in (2,2.5):
            for a in range(4):
                for b in range(5):
                    m=result.pop(0)
                    trades,vals,hourly=native_overlay(rows,60,360,a,b,floor)
                    self.assertEqual(m['trades'],trades,(a,b,floor))
                    self.assertEqual(m['n'],len(vals))
                    self.assertEqual(m['open'],len(trades)-len(vals))
                    self.assertAlmostEqual(m['gain'],sum(max(v,0) for v in vals))
                    self.assertAlmostEqual(m['loss'],sum(max(-v,0) for v in vals))
                    self.assertEqual(m['hourN'],[len(hourly[h]) for h in range(5)])
                    for h in range(5):
                        self.assertAlmostEqual(m['hourNet'][h],sum(hourly[h]))
                    self.assertEqual(m['positive'],sum(sum(hourly[h])>0 for h in range(5)))


class PublishedStudyTests(unittest.TestCase):
    def test_later_frozen_ledgers_and_hour_denominators(self):
        folder=ROOT/'reports/direct-base-20261127'
        choices=json.loads((folder/'forward-selection.json').read_text())
        result=json.loads((folder/'forward-results.json').read_text())
        self.assertEqual(set(choices),set(result['results']))
        p=result['period']
        self.assertEqual((p['minutes']-p['warm'])//60,8)
        for name,case in result['results'].items():
            self.assertEqual(case['settings'],choices[name])
            m=case['execution']
            hourly=[[] for _ in range(m['hours'])]
            opens=0
            for en,ex,sym,net,size in m['trades']:
                self.assertTrue(p['warm']<=en<p['minutes'])
                self.assertLess(sym,len(p['symbols']))
                if ex is None:
                    opens+=1
                    self.assertIsNone(net)
                else:
                    self.assertTrue(en<ex<=p['minutes'])
                    hourly[math.ceil((ex-p['warm'])/60)-1].append(net*size)
            vals=[v for group in hourly for v in group]
            self.assertEqual((len(vals),opens),(m['n'],m['open']))
            self.assertAlmostEqual(sum(max(0,v) for v in vals),m['gain'])
            self.assertAlmostEqual(sum(max(0,-v) for v in vals),m['loss'])
            self.assertEqual([len(group) for group in hourly],m['hourN'])
            for hour,group in enumerate(hourly):
                self.assertAlmostEqual(sum(group),m['hourNet'][hour])
            self.assertEqual(sum(sum(group)>0 for group in hourly),m['positive'])

    def test_frozen_selections_match_scalar_admission_and_native_execution(self):
        from direct_base import study
        report = ROOT / 'reports/direct-base-20261127/CTS-G-direct-post-Base.html'
        html = report.read_text()
        meta = json.loads(re.search(r'<script id="model" type="application/json">(.*?)</script>', html, re.S)[1])
        proxy_keys = ("all_proxy", "https_proxy", "http_proxy")
        with patch.dict(os.environ, {k: "" for n in proxy_keys for k in (n,n.upper())}):
            book, _ = study.make_book()
        bases, ends = direct.base_rules(book.stage_grid), direct.end_rules()
        for window, label in enumerate(('dev', 'test')):
            period = meta['periods'][window]
            unique = {tuple(w['conf'].values()): w for w in meta['winners'].values()}
            for si, key in enumerate(meta['sets']):
                choices = [w for w in unique.values() if w['conf']['set'] == si]
                if not choices:
                    continue
                blob = re.search(fr'id="tape-{window}-{si}">([^<]*)', html)[1]
                data = json.loads(gzip.decompress(base64.b64decode(blob)))
                offsets = base64.b64decode(data['index64'])
                deltas = base64.b64decode(data['delta64'])
                def order_ids(index):
                    lo,hi = struct.unpack_from('<II', offsets,4*index)
                    output = []
                    previous=delta=0
                    factor=1
                    for byte in deltas[lo:hi]:
                        delta+=(byte & 127)*factor
                        if byte>=128:
                            factor*=128
                        else:
                            previous+=delta
                            output.append(previous)
                            delta,factor=0,1
                    return output
                rows = data['orders']
                closes = [60*(r[1] if r[1] is not None else period['minutes']+1) for r in rows]
                entries = [60*r[0] for r in rows]
                nets = [r[3] or 0 for r in rows]
                native = book.grid_history(book.sets[key], closes=closes, entries=entries, nets=nets)
                parents = {}
                for w in choices:
                    c = w['conf']
                    pair = c['bf'],c['base']
                    if pair not in parents:
                        h = direct.fixed_history(native,meta['baseFloors'][c['bf']])
                        rule = bases[c['base']]
                        admitted = []
                        for i, at in enumerate(entries):
                            if rule.native:
                                ok = h.root.passes(rule.native,0,at)
                            else:
                                lo, hi = h.root.indices(at)
                                ok = all(hi-lo >= size+offset and h.root.test(size,hi-offset,sign,at,offset)
                                         for size,offset,sign in rule.blocks)
                            if ok:
                                admitted.append(i)
                        parents[pair] = admitted
                    parent = parents[pair]
                    parent_id = data['base'][c['bf']][c['base']]
                    self.assertEqual(parent,order_ids(parent_id))
                    source_times = [closes[i] for i in parent]
                    source_nets = [nets[i] for i in parent]
                    expected = []
                    for i in parent:
                        lo = bisect.bisect_left(source_times,entries[i]-native.lookback)
                        hi = bisect.bisect_left(source_times,entries[i])
                        if all(hi-lo >= size+offset and signed_ok(source_nets[hi-offset-size:hi-offset],sign,c['floor'])
                               for size,offset,sign in ends[c['end']].blocks):
                            expected.append(i)
                    actual_id = data['ends'][str(parent_id)][c['ef']][c['end']]
                    self.assertEqual(expected,order_ids(actual_id))
                    filtered = [rows[i] for i in expected]
                    trades, vals, hourly = native_overlay(filtered,period['warm'],period['minutes'],
                                                          c['axis'],c['block'],c['floor'])
                    result = w[label]
                    self.assertEqual(result['n'],len(vals))
                    self.assertEqual(result['open'],len(trades)-len(vals))
                    self.assertEqual(result['positive'],sum(sum(v)>0 for v in hourly.values()))
                    self.assertEqual(result['active'],len(hourly))
                    self.assertEqual(result['raised'],sum(weight>1 for _,weight in trades))
                    self.assertAlmostEqual(result['gain'],sum(v for v in vals if v>0))
                    self.assertAlmostEqual(result['loss'],-sum(v for v in vals if v<0))
                    ticks = defaultdict(list)
                    for i, weight in trades:
                        _, close, _, net = filtered[i]
                        if close is not None:
                            ticks[close].append(weight*net)
                    equity=peak=drawdown=0.
                    for tick in sorted(ticks):
                        equity+=sum(ticks[tick])
                        peak=max(peak,equity)
                        drawdown=max(drawdown,peak-equity)
                    self.assertAlmostEqual(result['drawdown'],drawdown)


if __name__ == '__main__':
    unittest.main()
