# CTS-A-O portable indication calculations

The `origin/` graph is copied from CTS-A-O revision `819b6621f25393e3fe66da4a0fa0a52c4baee709` plus additive DCA Tactic and its registry entry. `source-manifest.json` records the exact files and SHA-256 hashes. Inverse mathematics and original source IDs are preserved. `catalog.ts` and `catalog.json` are metadata; importing those does not eagerly evaluate thousands of candidates.

`portedStates` / `entryOnsets` evaluate selected source IDs against genuine closed OHLCV bars. `alignedSourceStates` uses only complete UTC-aligned higher candles. Pair/market variants require matched, distinct peer references; absent inputs yield unavailable/zero, not a substitute direction. The streaming cache assumes append-only immutable closed bars; reset it when history is replaced, shortened, or corrected.

Run `node --experimental-strip-types --test shared/cts-indications/ported.test.ts`. All 2,918 source-state hashes come from the native CTS-A-O oracle on the checked-in analytical fixture. This proves parity and causal contracts, not profitable trading. New source selection must still pass the host strategy pipeline.
