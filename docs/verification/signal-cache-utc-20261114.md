# UTC-aligned Signal memo — 14 November 2026

Base Stable-02-01: `724bfce9` (includes the reviewed preset/native-budget repair and the newer per-range Settings UI).

The Signal memo keyed a completed window by its settings and OHLCV values, but Signal packs additionally depend on the UTC close time to aggregate completed timeframe bars. Reusing equal values under a different clock phase therefore reused an incorrect onset. The deterministic 420-bar reproduction changes two onsets after a one-minute clock shift; the old warm cache disagrees with the cold calculation on both.

Include the exact close clock in both the ordinary memo lookup and the worker preflight/deduplication key. Bump the saved memo version to 2 so a restart rejects caches produced with the old identity. Matching settings, clock and values still reuse the cache; held trade history and configuration policies are unchanged.

The new regression compares cold, warmed, saved/restored and worker-backed calculations under the shifted UTC phase, proves unchanged windows remain cache hits, and rejects version-1 files. Eight Signal-pack checks, two worker/restart checks and the existing complete-window memo regression pass. The targeted mutation and complete regression result are recorded with the containing PR and all-project report. No golden fingerprint, gate, fee, overlay, service or exchange order changed.

This is signal causality/cache verification. New post-Base passed/evaluated counts and forward/live PF are unavailable; none is inferred from offline correctness.
