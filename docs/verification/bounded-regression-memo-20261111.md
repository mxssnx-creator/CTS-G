# Bounded regression memo

Repeated full-grid fixtures span four symbols. The memo retains immutable,
compressed raw replay snapshots, up to 32 entries and 64 MiB of payload.
Every hit decodes fresh rows and input arrays. Oversized or unpickleable
snapshots run through the native replay without retention. Native scoring,
stage admission and Last-N evaluations still execute for each check.

The integrated tests retain the four-symbol native comparison, cost/candle/
distance invalidation, copied-input isolation and oversized-cache checks from
the earlier compact-column memo. Dependent peer frames and fixed pause
settings are now represented in the key. Automatic pause closures that cannot
be serialized take the native path. Global legacy stop-step settings no
longer affect a book's raw replay: distance inputs are explicit per book.
Admission-only window changes may reuse raw rows and must rerun their own
scoring. The cache is used only by the regression harness.

See [performance and native verification](performance-native-x02-20261111.md)
for measured timings, frozen economic parity and execution limitations.
