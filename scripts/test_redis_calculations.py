"""Redis protocol + Lua tests in an isolated in-memory server; no exchange I/O."""
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server/pulse'))
import fakeredis
from calculation_cache import CalculationCache, PUT
from redis_coordination import RedisCoordinator, RedisUnavailable, READ_HASH, WRITE_HASH
from set_engine import SetBook, SetState


class RedisCalculationTests(unittest.TestCase):
    def setUp(self):
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.cache = CalculationCache('bingx-x02', client=self.redis)

    def put(self, index, stamp=None, sid='set-A', payload='{}', cache=None):
        cache = cache or self.cache
        key = cache.key(sid)
        token = hashlib.sha256(str(index).encode()).hexdigest()
        cfg = cache.settings
        result = self.redis.eval(PUT, 5, key, key+':t', cache.prefix+'sets', cache.prefix+'sizes', cache.prefix+'total',
                                 token, payload, index if stamp is None else stamp, 20000,
                                 cfg['systemSetMaxEntries'],cfg['systemTrimTargetPct'],cfg['systemRedisCalcMaxMb']*1048576,
                                 cfg['systemRedisCalcMaxSets'],cfg['systemRedisCalcTtlS'],cache.prefix,cfg['systemRedisCalcSetMaxKb']*1024)
        return key, token, result

    def test_full_350_prunes_to_newest_280_and_repeated_event_is_idempotent(self):
        tokens = {}
        for i in range(1, 351):
            key, token, result = self.put(i)
            tokens[i] = token
        self.assertEqual(self.redis.hlen(key), 280)
        self.assertEqual(set(self.redis.hkeys(key)), {tokens[i] for i in range(71,351)})
        self.assertEqual(result[2], 70)
        self.put(350)
        self.assertEqual(self.redis.hlen(key),280)
        self.assertGreater(self.redis.ttl(key),0)

    def test_out_of_order_arrivals_preserve_newest_source_timestamps(self):
        sequence = list(range(350)); random.Random(9).shuffle(sequence)
        for i in sequence: key, _, _ = self.put(i)
        kept = [int(x) for _,x in self.redis.zrange(key+':t',0,-1,withscores=True)]
        self.assertEqual(kept, list(range(70,350)))
        for i in range(-70,0): self.put(i)
        self.assertEqual([int(x) for _,x in self.redis.zrange(key+':t',0,-1,withscores=True)], kept)

    def test_equal_timestamps_keep_latest_insertions_with_stable_duplicate_order(self):
        tokens={}
        for i in range(350):
            key,token,_=self.put(i,stamp=1000)
            tokens[i]=token
        self.assertEqual(set(self.redis.hkeys(key)),{tokens[i] for i in range(70,350)})
        before=self.redis.zrange(key+':t',0,-1)
        self.put(70,stamp=1000)
        self.assertEqual(self.redis.zrange(key+':t',0,-1),before)

    def test_global_byte_and_set_pressure_remain_bounded_and_foreign_data_survives(self):
        self.redis.hset('connection:bingx-x01',mapping={'sentinel':'keep'})
        self.redis.set('live_positions:foreign','keep')
        self.cache.configure({'systemRedisCalcMaxMb':8,'systemRedisCalcMaxSets':1000})
        saw_prune = False
        for i in range(1300):
            _, _, result = self.put(i,sid=f'set-{i}',payload='v'*32000)
            self.assertLess(result[0],8*1048576)
            self.assertLess(result[1],350)
            if result[2]:
                saw_prune = True
                self.assertLessEqual(result[0],8*1048576*.8)
        self.assertTrue(saw_prune)
        self.assertEqual(self.redis.get('live_positions:foreign'),'keep')
        self.assertEqual(self.redis.hget('connection:bingx-x01','sentinel'),'keep')
        self.assertEqual(self.redis.ttl('connection:bingx-x01'),-1)

    def test_single_set_byte_ceiling_keeps_recent_results_and_exact_accounting(self):
        self.cache.configure({'systemRedisCalcSetMaxKb':64})
        for i in range(100):
            key,_,_ = self.put(i,payload='x'*16000)
            used=int(self.redis.hget(self.cache.prefix+'sizes',key))
            self.assertLess(used,65536)
            expected=1024+sum(len(k)+len(v)+256 for k,v in self.redis.hgetall(key).items())
            self.assertEqual(used,expected)
            times=[score for _,score in self.redis.zrange(key+':t',0,-1,withscores=True)]
            self.assertEqual(times,list(range(i-len(times)+1,i+1)))

    def test_set_pressure_100_to_80_and_connections_are_independent(self):
        self.cache.configure({'systemRedisCalcMaxSets':100})
        other = CalculationCache('bingx-x01',client=self.redis)
        self.put(1, sid='set-0',cache=other)
        for i in range(100): _,_,result = self.put(i,sid=f'set-{i}')
        self.assertEqual(result[1],80)
        self.assertEqual(self.redis.zcard(other.prefix+'sets'),1)
        self.assertFalse(self.redis.exists(self.cache.key('set-0')))
        self.assertTrue(self.redis.exists(self.cache.key('set-99')))

    @staticmethod
    def book(count=64):
        book = SetBook()
        for i in range(count):
            pack='general' if i%2==0 else 'indications'
            trailing=bool(i%4>=2)
            st = SetState(id=f'{pack}:config-{i}',pack=pack,tf='1m',sl_ratio=.6,trail_key='0.3:0.1' if trailing else '',
                          trail_arm=.003 if trailing else 0,trail_give=.001 if trailing else 0,idx=i,kind='trail' if trailing else 'base',
                          indication_kind=('state','direction','move','active','common','signals','trend','break')[i%8])
            st.hist = [dict(t=1700000000+j*60,symbol=f'S-{j%3}',side='LONG' if j%2 else 'SHORT',
                            pnl_pct=-.002 if j%4==0 else .004+(i%5)*.0001,hold_s=60,reason='tp') for j in range(80)]
            book.sets[st.id] = st; book.by_idx.append(st)
        return book

    def test_cached_pf_ddt_and_all_qualification_fields_equal_uncached_calculation(self):
        book = self.book()
        book._score_all()
        expected = [asdict(st) for st in book.by_idx]
        book.calculation_cache = self.cache
        book._score_all()
        self.assertEqual(self.cache.status()['misses'],64)
        self.assertEqual([asdict(st) for st in book.by_idx], expected)
        with patch.object(book,'_fast_historic_bundle',side_effect=AssertionError('cache miss')):
            book._score_all()
        self.assertEqual(self.cache.status()['hits'],64)
        self.assertEqual([asdict(st) for st in book.by_idx], expected)
        book.cost_pct *= 2
        book._score_all()
        self.assertEqual(self.cache.status()['misses'],128)
        self.assertNotEqual([asdict(st) for st in book.by_idx], expected)
        pure = copy.deepcopy(book)
        pure._score_all()
        self.assertEqual([asdict(st) for st in book.by_idx], [asdict(st) for st in pure.by_idx])

    def test_current_qualification_settings_reapply_on_cache_hit(self):
        book = self.book(1); book.calculation_cache = self.cache
        book._score_all()
        book.auto_deact = True; book.strict_gate = True; book.min_pf = 99
        book._score_all()
        self.assertEqual(self.cache.status()['hits'],1)
        pure=copy.deepcopy(book); pure._score_all()
        self.assertEqual(asdict(book.by_idx[0]),asdict(pure.by_idx[0]))

    def test_low_reuse_limits_redis_churn_without_skipping_calculations(self):
        book=self.book(96); book._score_all(); expected=[asdict(st) for st in book.by_idx]
        self.cache.configure({'systemRedisProbeEntries':64})
        book.calculation_cache=self.cache; book._score_all()
        self.assertEqual(self.cache.status()['misses'],64)
        self.assertEqual(self.cache.status()['bypassed'],32)
        self.assertTrue(self.cache.status()['bypassReason'])
        self.assertEqual([asdict(st) for st in book.by_idx],expected)
        self.cache.pause_until=0
        for start in (0,32):
            for pair in book.score_pairs(book.by_idx[start:start+32]): book._score_pair(pair)
        self.assertEqual(self.cache.status()['hits'],64)
        self.assertEqual(self.cache.status()['bypassReason'],'')

    def test_concurrent_input_change_cannot_reuse_a_previous_prepared_score(self):
        book=self.book(1); st=book.by_idx[0]; book.calculation_cache=self.cache
        prepared=self.cache.prepare(book,[st])[0]
        st.hist=[dict(row,pnl_pct=-.02) for row in st.hist]
        book.cost_pct=.2
        book._score_pair(prepared)
        pure=copy.deepcopy(book); pure._score_all()
        self.assertEqual(asdict(st),asdict(pure.by_idx[0]))

    def test_outage_falls_back_without_skipping_any_set_or_repeated_timeout(self):
        book=self.book(80); book._score_all(); expected=[asdict(st) for st in book.by_idx]
        book.calculation_cache=self.cache
        with patch.object(self.redis,'pipeline',side_effect=ConnectionError('offline')) as failed:
            book._score_all(); book._score_all()
        self.assertEqual(failed.call_count,1)
        self.assertEqual([asdict(st) for st in book.by_idx],expected)
        self.cache.retry_at=0; book._score_all()
        self.assertEqual(self.cache.status()['error'],'')
        self.assertEqual(self.cache.status()['misses'],80)

    def test_live_rows_never_reuse_historical_only_bundle(self):
        book=self.book(1); st=book.by_idx[0]
        st.live=[dict(st.hist[-1],t=1800000000,pnl_pct=-.1)]
        self.assertEqual(self.cache.prepare(book,[st]),[(st,None)])
        with self.assertRaises(ValueError): self.cache.prepare(book,[st]*33)

    def test_partial_closes_of_one_parent_all_count_and_retry_does_not_duplicate(self):
        book=self.book(1); st=book.by_idx[0]
        for i in range(3):
            row=dict(set_id=st.id,client_id='one-parent',close_fill_id=f'partial-{i}',t=1800000000+i,
                     symbol='S-1',side='LONG',qty=.2,pnl=1,pnl_pct=.002,ind_kind='trend',exchange_confirmed=True)
            book.on_live_close(row); book.on_live_close(row)
        self.assertEqual(len(st.live),3)
        self.assertEqual(len(book.ind_live['trend']),3)
        self.assertAlmostEqual(sum(r['qty'] for r in st.live),.6)

    def test_corrupt_cached_metrics_recompute_instead_of_stopping_calculation(self):
        book=self.book(1); st=book.by_idx[0]; book.calculation_cache=self.cache
        book._score_all(); expected=asdict(st)
        token=self.cache.signature(book,st)
        self.redis.hset(self.cache.key(st.id),token,json.dumps({'signature':token,'bundle':[{}, {'LONG':{},'SHORT':{}}]}))
        book._score_all()
        self.assertEqual(asdict(st),expected)
        self.assertEqual(self.cache.status()['misses'],2)

    def test_configuration_hash_cache_coalesces_readers_and_write_invalidates(self):
        # fakeredis implements Lua/hash operations but not MEMORY USAGE. Replace
        # only that metadata probe; length and atomic write guards run unchanged.
        def evaluate(script,*args):
            return self.redis.eval(script.replace("redis.call('MEMORY','USAGE',k,'SAMPLES',0)", '0'),*args)
        client=Mock(); client.eval.side_effect=evaluate
        config=RedisCoordinator(client)
        self.assertTrue(config.write_hash('connection:bingx-x02',{'api_key':'test-only','note':'a\nÜ'}))
        with ThreadPoolExecutor(max_workers=16) as pool:
            rows=list(pool.map(lambda _:config.read_hash('connection:bingx-x02'),range(250)))
        self.assertTrue(all(row['note']=='a\nÜ' for row in rows))
        self.assertEqual(config.calls,2)
        self.assertTrue(config.write_hash('connection:bingx-x02',{'note':'changed'}))
        self.assertEqual(config.read_hash('connection:bingx-x02')['note'],'changed')
        before=self.redis.hgetall('connection:bingx-x02')
        self.assertFalse(config.write_hash('connection:bingx-x02',{'too-big':'x'*17000}))
        self.assertEqual(self.redis.hgetall('connection:bingx-x02'),before)
        with self.assertRaises(ValueError): config.write_hash('live_positions:foreign',{'x':'x'})

    def test_existing_oversized_hash_rejects_atomically_and_does_not_cache_errors(self):
        def evaluate(script,*args):
            return self.redis.eval(script.replace("redis.call('MEMORY','USAGE',k,'SAMPLES',0)", '0'),*args)
        client=Mock(); client.eval.side_effect=evaluate
        config=RedisCoordinator(client)
        self.redis.hset('connection:bingx-x02',mapping={str(i):'a' for i in range(513)})
        self.assertFalse(config.write_hash('connection:bingx-x02',{'valid':'attempt'}))
        self.assertFalse(self.redis.hexists('connection:bingx-x02','valid'))
        with self.assertRaises(RedisUnavailable):config.read_hash('connection:bingx-x02')
        self.assertEqual(len(config.cache),0)
        self.assertEqual(config.calls,1)


if __name__=='__main__':unittest.main()
