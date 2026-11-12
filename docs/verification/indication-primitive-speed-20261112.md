# Exact indication calculation reuse

An eight-pack replay profile found repeated true-range walks, EMA/RSI vectors,
weighted-average generators and Kaufman path sums inside each candle decision.
The profile collected 488,877 calls for one representative decision. This
calculation was consuming a core during native warmup.

ATR now reads its requested tail and one preceding close, preserving the gap
on the first retained bar. Weighted averages keep the same multiplication and
summation order using a built-in iterator. Kaufman path sums reuse the original
adjacent differences in the same order. EMA and RSI reuse exact float vectors
across families, with fresh result lists for callers. IEEE byte keys retain
signed zeros and NaN payloads; integer inputs retain the original arithmetic.

The cache is private to each thread, limited to 128 entries and a 2 MiB
conservative payload estimate. Vectors longer than 512 use the original path.
Fee, gate, stop, history and lane semantics are preserved.

Five focused checks pass: bit-level primitive oracles, all library names over
complete timeframe history in both mirrored directions, interior mutation and
result isolation, signed-zero/integer cases, and bounded independent threads.

Five alternating paired runs over 12 completed SOL windows and all eight
ordinary packs compared 960 direction/confidence/reason outputs. Every output
matched. Median raw-signal speedup was **1.918x**, with paired ratios 2.371,
1.681, 1.545, 2.343 and 1.918. The benchmark records market and library hashes.
This measures the signal calculation, excluding replay scoring, network,
execution and startup imports. Whole-system speed and profitability remain
separate outcomes.

Reproduce with `scripts/benchmark_indication_primitives.py`, providing the
frozen SOL OHLCV JSON and frozen overlay from the evidence bundle. Run
`python3 scripts/test_indication_primitives.py` for the independent arithmetic
and library checks.
