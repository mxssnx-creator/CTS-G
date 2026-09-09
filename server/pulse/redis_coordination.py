"""Bounded, coalesced configuration access; no per-order/history Redis keys.

Only the two known configuration hash families are writable. A bounded native
connection pool serves cache misses. Lua checks size limits before
reading values or atomically writing. No key-space scans or shared eviction.
"""
from collections import OrderedDict
import re
import os
import threading
import time

from runtime_scope import redis_key
from system_settings import normalize_system_settings

try:
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
except ImportError:
    redis = None


def redis_client():
    if redis is None:
        raise RedisUnavailable('Redis runtime dependency unavailable')
    return redis.Redis(host=os.environ.get('REDISCLI_HOST', '127.0.0.1'),
                       port=int(os.environ.get('REDISCLI_PORT', '6379')),
                       password=os.environ.get('REDISCLI_AUTH') or None,
                       decode_responses=True, protocol=2, max_connections=4,
                       socket_connect_timeout=0.4, socket_timeout=0.75,
                       retry=Retry(NoBackoff(), 0), health_check_interval=30)

# Constant-time HLEN and sampled memory checks precede the bounded field scan.
HASH_GUARD = """
local k=KEYS[1]
local cap=tonumber(ARGV[1])
local fieldcap=tonumber(ARGV[2])
local n=redis.call('HLEN',k)
if n>512 then return redis.error_reply('CTS_HASH_FIELD_LIMIT') end
if (redis.call('MEMORY','USAGE',k,'SAMPLES',0) or 0)>cap*4+65536 then
  return redis.error_reply('CTS_HASH_MEMORY_LIMIT') end
local names=redis.call('HKEYS',k)
local sizes={}
local total=0
for _,f in ipairs(names) do
  local size=redis.call('HSTRLEN',k,f)
  if #f>128 or size>fieldcap then return redis.error_reply('CTS_FIELD_LIMIT') end
  sizes[f]=size
  total=total+#f+size
end
if total>cap then return redis.error_reply('CTS_HASH_BYTE_LIMIT') end
"""
READ_HASH = HASH_GUARD + "return redis.call('HGETALL',k)"
WRITE_HASH = HASH_GUARD + """
for i=3,#ARGV,2 do
  local f=ARGV[i]
  local v=ARGV[i+1]
  if not v or #f>128 or #v>fieldcap then return redis.error_reply('CTS_FIELD_LIMIT') end
  if not sizes[f] then n=n+1; total=total+#f end
  total=total+#v-(sizes[f] or 0)
  sizes[f]=#v
end
if n>512 or total>cap then return redis.error_reply('CTS_HASH_BYTE_LIMIT') end
return redis.call('HSET',k,unpack(ARGV,3))
"""


class RedisUnavailable(RuntimeError):
    pass


class RedisCoordinator:
    def __init__(self, client=None):
        self.lock = threading.RLock()
        self.cache = OrderedDict()
        self.settings = normalize_system_settings({})
        self.retry_at = 0.0
        self.calls = self.hits = self.errors = 0
        self.client = client

    def configure(self, settings):
        limits = normalize_system_settings(settings)
        with self.lock:
            if any(limits[k] != self.settings[k] for k in limits if k.startswith('systemRedis')):
                self.cache.clear()
            self.settings = limits

    def _key(self, key):
        if not re.fullmatch(r'(?:connection:|settings:connection_settings:)bingx-x0[12]', key):
            raise ValueError('unsupported configuration hash')
        result = redis_key(key)
        if len(result.encode()) > 192:
            raise ValueError('configuration key too long')
        return result

    def _call(self, script, key, values=()):
        if time.monotonic() < self.retry_at:
            raise RedisUnavailable('Redis recovery backoff')
        cap = int(self.settings['systemRedisMaxHashKb'] * 1024)
        fieldcap = int(self.settings['systemRedisMaxFieldKb'] * 1024)
        try:
            self.calls += 1
            if self.client is None:
                self.client = redis_client()
            result = self.client.eval(script, 1, key, cap, fieldcap, *values)
            self.retry_at = 0
            return result
        except Exception:
            self.errors += 1
            self.retry_at = time.monotonic() + 1
            raise RedisUnavailable('Redis configuration unavailable') from None

    def read_hash(self, key, fresh=False):
        key = self._key(key)
        with self.lock:
            cached = self.cache.get(key)
            if cached and not fresh and time.monotonic() - cached[0] < self.settings['systemRedisCacheS']:
                self.hits += 1
                self.cache.move_to_end(key)
                return dict(cached[1])
            self.cache.pop(key, None)
            result = self._call(READ_HASH, key)
            if not isinstance(result, list) or len(result) % 2 or len(result) > 1024:
                raise RedisUnavailable('Invalid configuration response')
            data = dict(zip(result[::2], result[1::2]))
            if any(not isinstance(k, str) or not isinstance(v, str) for k, v in data.items()):
                raise RedisUnavailable('Invalid configuration fields')
            self.cache[key] = (time.monotonic(), data)
            while len(self.cache) > 4:
                self.cache.popitem(last=False)
            return dict(data)

    def write_hash(self, key, mapping):
        key = self._key(key)
        data = {str(k): str(v) for k, v in mapping.items() if v is not None}
        with self.lock:
            if not data or len(data) > 512 or any(len(k.encode()) > 128 or len(v.encode()) > self.settings['systemRedisMaxFieldKb']*1024 for k,v in data.items()):
                return False
            if sum(len(k.encode())+len(v.encode()) for k,v in data.items()) > self.settings['systemRedisMaxHashKb']*1024:
                return False
            self.cache.pop(key, None)
            try:
                result = self._call(WRITE_HASH, key, [v for pair in data.items() for v in pair])
                return isinstance(result, int) and not isinstance(result, bool) and result >= 0
            except RedisUnavailable:
                return False  # Never retry an ambiguous write automatically.

    def status(self):
        with self.lock:
            return {'calls': self.calls, 'cacheHits': self.hits, 'errors': self.errors,
                    'cachedHashes': len(self.cache), 'maxCachedHashes': 4,
                    'maxHashBytes': int(self.settings['systemRedisMaxHashKb']*1024),
                    'maxFieldBytes': int(self.settings['systemRedisMaxFieldKb']*1024)}


coordinator = RedisCoordinator()
