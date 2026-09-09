"""Redis memoization for pure historical metrics, isolated by lane and Set.

Only calculation workers use this cache. Qualification and order ownership are
always evaluated against the current settings. A miss/outage computes locally.
Bounded pipelines, expiring compact keys and atomic oldest-first pruning keep
cache pressure independent of the complete calculation catalog.
"""
from __future__ import annotations

import hashlib
import json
import copy
from dataclasses import dataclass
from pathlib import Path
import threading
import time

from redis_coordination import redis_client
from runtime_scope import redis_key
from system_settings import normalize_system_settings

REVISION = hashlib.sha256(Path(__file__).with_name('set_engine.py').read_bytes()).hexdigest()[:16]


@dataclass(frozen=True)
class PreparedBundle:
    signature: str
    value: tuple

PUT = """
local data,times,sets,sizes,totalkey=unpack(KEYS)
local token,payload=ARGV[1],ARGV[2]
local stamp,now,cap,target,budget,maxsets,ttl=tonumber(ARGV[3]),tonumber(ARGV[4]),tonumber(ARGV[5]),tonumber(ARGV[6]),tonumber(ARGV[7]),tonumber(ARGV[8]),tonumber(ARGV[9])
local prefix=ARGV[10]
local setbudget=tonumber(ARGV[11])
if #payload>32768 or #token~=64 or cap>350 then return redis.error_reply('CTS_CACHE_LIMIT') end
local total=tonumber(redis.call('GET',totalkey) or '0')
local used=tonumber(redis.call('HGET',sizes,data) or '0')
-- Expired objects can leave bounded registry entries; repair accounting here.
if redis.call('EXISTS',data)==0 then total=math.max(0,total-used); used=0; redis.call('DEL',times) end
if used==0 then used=1024; total=total+1024 end
local previous=redis.call('HGET',data,token)
local old=previous and #previous or 0
local member
if previous then
  local ok,decoded=pcall(cjson.decode,previous)
  if ok then member=decoded.member end
end
if not member then
  if previous then
    for _,oldMember in ipairs(redis.call('ZRANGE',times,0,349)) do
      if string.sub(oldMember,22)==token then redis.call('ZREM',times,oldMember) end
    end
  end
  local sequence=redis.call('HINCRBY',sizes,'_sequence',1)
  member=string.format('%020d',sequence)..':'..token
end
payload=cjson.encode({body=payload,member=member})
local delta=#payload-old
if redis.call('HEXISTS',data,token)==0 then delta=delta+#token+256 end
redis.call('HSET',data,token,payload)
redis.call('ZADD',times,stamp,member)
used=used+delta; total=total+delta
local count=redis.call('ZCARD',times)
local removed=0
if count>=cap or used>=setbudget then
  local keep=count>=cap and math.max(1,math.floor(cap*target/100)) or count
  local byteTarget=used>=setbudget and math.floor(setbudget*target/100) or used
  for _,orderedId in ipairs(redis.call('ZRANGE',times,0,-1)) do
    if count<=keep and used<=byteTarget then break end
    local id=string.sub(orderedId,22)
    local size=redis.call('HSTRLEN',data,id)+#id+256
    redis.call('HDEL',data,id); redis.call('ZREM',times,orderedId)
    used=used-size; total=total-size; removed=removed+1; count=count-1
  end
end
redis.call('HSET',sizes,data,used)
local newest=redis.call('ZREVRANGE',times,0,0,'WITHSCORES')
redis.call('ZADD',sets,tonumber(newest[2] or stamp),data)
redis.call('EXPIRE',data,ttl); redis.call('EXPIRE',times,ttl)
local n=redis.call('ZCARD',sets)
if total>=budget or n>=maxsets or redis.call('HGET',sizes,'_pressure')=='1' then
  local byteTarget=math.floor(budget*target/100)
  local setTarget=math.floor(maxsets*target/100)
  -- Only this versioned cache namespace is eligible for removal.
  for _,key in ipairs(redis.call('ZRANGE',sets,0,127)) do
    if total<=byteTarget and n<=setTarget then break end
    if string.sub(key,1,#prefix)~=prefix then return redis.error_reply('CTS_CACHE_SCOPE') end
    total=math.max(0,total-tonumber(redis.call('HGET',sizes,key) or '0'))
    redis.call('UNLINK',key,key..':t')
    redis.call('HDEL',sizes,key); redis.call('ZREM',sets,key)
    n=n-1; removed=removed+1
  end
  if total<=byteTarget and n<=setTarget then redis.call('HDEL',sizes,'_pressure')
  else redis.call('HSET',sizes,'_pressure','1') end
end
redis.call('SET',totalkey,total,'EX',ttl)
redis.call('EXPIRE',sets,ttl); redis.call('EXPIRE',sizes,ttl)
return {total,n,removed}
"""


class CalculationCache:
    BATCH = 32

    def __init__(self, connection, settings=None, client=None):
        if connection not in ('bingx-x01', 'bingx-x02'):
            raise ValueError('unsupported calculation connection')
        self.prefix = redis_key('cts-calc:v2:'+connection+':')
        self.settings = normalize_system_settings(settings)
        self.client = client
        self.script = None
        self.retry_at = 0.0
        self.pause_until = 0.0
        self.window_hits = self.window_reads = 0
        self.bypass_reason = ''
        self.lock = threading.RLock()
        self.metrics = dict(hits=0, misses=0, bypassed=0, errors=0, pruned=0, accountedBytes=0, cachedSets=0)
        self.error = ''

    def configure(self, settings):
        self.settings = normalize_system_settings(settings)
        self.pause_until = 0
        self.window_hits = self.window_reads = 0

    def key(self, set_id):
        return self.prefix + 's:' + hashlib.sha256(set_id.encode()).hexdigest()[:24]

    @staticmethod
    def signature(book, state, rows=None):
        # Complete input to _fast_historic_bundle. Execution flags deliberately
        # remain outside this pure-metric cache and are reapplied by _score_one.
        params = [REVISION, book.cost_pct, book.pf_n, book.deact_n, book.eval_need(),
                  book.real_min_pf, book.max_dd_s, book._stage_window_ns()]
        source_rows = state.hist if rows is None else rows
        # Compact historic rows are Mapping-compatible but intentionally not
        # JSONEncoder objects.  Convert only the small cache-signature input;
        # the live Set tape remains slots-backed and memory bounded.
        json_rows = [dict(row) if not isinstance(row, dict) else row for row in source_rows]
        raw = json.dumps([params, json_rows], separators=(',', ':'), sort_keys=True, allow_nan=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _connect(self):
        if self.client is None:
            self.client = redis_client()
        if self.script is None:
            self.script = self.client.register_script(PUT)

    def _failed(self, exc):
        self.metrics['errors'] += 1
        self.retry_at = time.monotonic() + 30
        self.error = 'Calculation cache unavailable: '+type(exc).__name__

    def prepare(self, book, states):
        """One bounded batch. Returns bundles; every miss is computed exactly."""
        if len(states) > self.BATCH:
            raise ValueError('calculation pipeline exceeds batch limit')
        result = [(st, None) for st in states]
        if time.monotonic() < self.retry_at:
            return result
        if time.monotonic() < self.pause_until:
            self.metrics['bypassed'] += len(states)
            return result
        self.bypass_reason = ''
        candidates = []
        # A concurrent config/history publication cannot change the input after
        # its cache identity has been calculated.
        metrics_book = copy.copy(book)
        for index, st in enumerate(states):
            if not st.live and st.hist:
                try:
                    rows = [dict(row) for row in st.hist]
                    candidates.append((index, st, self.key(st.id), self.signature(metrics_book, st, rows), rows))
                except (ValueError, TypeError):
                    continue
        if not candidates:
            return result
        with self.lock:
            try:
                self._connect()
                pipe = self.client.pipeline(transaction=False)
                for _, _, key, token, _ in candidates:
                    pipe.hget(key, token)
                cached = pipe.execute()
            except Exception as exc:
                self._failed(exc)
                return result
            writes = []
            for (index, st, key, token, rows), payload in zip(candidates, cached):
                bundle = None
                if payload and len(payload.encode()) <= 65536:
                    try:
                        decoded = json.loads(json.loads(payload)['body'])
                        checksum = hashlib.sha256(json.dumps(decoded.get('bundle'),separators=(',', ':'),sort_keys=True,allow_nan=False).encode()).hexdigest()
                        if decoded.get('signature') == token and decoded.get('checksum') == checksum:
                            candidate = decoded['bundle']
                            if len(candidate) == 2 and isinstance(candidate[0], dict) and set(candidate[1]) == {'LONG','SHORT'}:
                                bundle = (candidate[0], candidate[1])
                    except (ValueError, KeyError, TypeError):
                        pass
                if bundle is not None:
                    self.metrics['hits'] += 1
                    self.window_hits += 1
                else:
                    self.metrics['misses'] += 1
                    bundle = metrics_book._fast_historic_bundle(rows, hist_n=len(rows), ordered=True)
                    checksum = hashlib.sha256(json.dumps(bundle,separators=(',', ':'),sort_keys=True,allow_nan=False).encode()).hexdigest()
                    encoded = json.dumps({'signature':token,'checksum':checksum,'bundle':bundle}, separators=(',', ':'), allow_nan=False)
                    if len(encoded.encode()) <= 32768:
                        stamp = max(float(r.get('t') or 0) for r in rows)
                        writes.append((key, token, encoded, stamp))
                result[index] = (st, PreparedBundle(token, bundle))
                self.window_reads += 1
            try:
                if writes:
                    pipe = self.client.pipeline(transaction=False)
                    settings = self.settings
                    for key, token, payload, stamp in writes:
                        self.script(keys=[key,key+':t',self.prefix+'sets',self.prefix+'sizes',self.prefix+'total'],
                                    args=[token,payload,stamp,time.time(),settings['systemSetMaxEntries'],
                                          settings['systemTrimTargetPct'],settings['systemRedisCalcMaxMb']*1048576,
                                          settings['systemRedisCalcMaxSets'],settings['systemRedisCalcTtlS'],self.prefix,
                                          settings['systemRedisCalcSetMaxKb']*1024], client=pipe)
                    for size, count, pruned in pipe.execute():
                        self.metrics.update(accountedBytes=size, cachedSets=count)
                        self.metrics['pruned'] += pruned
                self.error = ''
            except Exception as exc:
                self._failed(exc)
            if self.window_reads >= self.settings['systemRedisProbeEntries']:
                # A complete catalog can dwarf a 350-Set hot cache. Do not churn
                # its keys/AOF for the rest of a cold scan: every Set still scores
                # locally, and periodic probes resume reuse when it is useful.
                if self.window_hits * 100 / self.window_reads < self.settings['systemRedisMinHitPct']:
                    self.pause_until = time.monotonic() + self.settings['systemRedisRecheckS']
                    self.bypass_reason = 'Low cache reuse; local calculation avoids Redis/AOF churn'
                self.window_hits = self.window_reads = 0
        return result

    def status(self):
        return {**self.metrics, 'error': self.error, 'batch': self.BATCH,
                'bypassReason': self.bypass_reason,
                'entryLimit': self.settings['systemSetMaxEntries'], 'trimTargetPct': self.settings['systemTrimTargetPct'],
                'memoryLimitBytes': self.settings['systemRedisCalcMaxMb']*1048576,
                'source': 'Redis pure-metric cache; current qualification always reapplied'}
