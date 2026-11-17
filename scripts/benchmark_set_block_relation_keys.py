#!/usr/bin/env python3
"""Microbenchmark relation-key creation; this is not an engine-cycle benchmark."""
import argparse
from pathlib import Path
from statistics import median
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server' / 'pulse'))
import block_rules as br

parser = argparse.ArgumentParser()
parser.add_argument('--iterations', type=int, default=100000)
parser.add_argument('--trials', type=int, default=3)
parser.add_argument('--cold', action='store_true', help='vary Set identity to exercise cache misses')
args = parser.parse_args()
if args.iterations <= 0 or args.trials <= 0:
    parser.error('iterations and trials must be positive')
cfg = br.clean_cfg({'blockRelations': ['symbol', 'step']})
br._scoped_relation_key.cache_clear()
times = []
for _ in range(args.trials):
    started = perf_counter()
    for i in range(args.iterations):
        sid = f'pack:L:1m:sl1.0:st20:{i}' if args.cold else 'pack:L:1m:sl1.0:st20'
        br.relation_keys(cfg, sid, 'pos', 'LONG', 'BTC-USDT', 'pack', 20)
    times.append(perf_counter() - started)
print({'iterations': args.iterations, 'mode': 'cold' if args.cold else 'hot', 'seconds': times,
       'medianSeconds': median(times), 'cache': str(br._scoped_relation_key.cache_info())})
