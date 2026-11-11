# Strategy, settings and statistics audit — 1 November 2026

Status: draft research branch, not a live release. No exchange orders were submitted, x01 was not started and x02 was not stopped. This work does not establish positive forward PF or complete independent runtime range lanes.

## Implemented

- Normal Set admission has its own `stratNormal` switch. Turning it off retains internal paper processing for additional strategy types. General off no longer forces Indications on when every pack is disabled.
- The trailing master and module switch consistently disable trailing Sets, peak-following and recalculation. Existing protective stops are retained and never deliberately loosened by switching trailing off.
- Settings expose effective Main short candidates and candidates excluded by the configured long/short ratio. The UI explicitly describes adaptive selection, not simultaneous independent execution lanes.
- Statistics expose Base/Main/Real populations, sample counts, PF evidence, configured Last-N windows, context coverage and per-symbol maker/taker rate provenance. Empty, flat, no-loss and finite PF states are distinct. Legacy 99 without evidence is ambiguous, not proof of no losses; a finite PF above 99 is not a no-loss sentinel.
- Both dashboard pages preserve recorded net returns and use the connection's reported cost allowance only for gross-only rows. The frontend and engine calculate mean net percentage directly from measured net results, including when the allowance is zero. Fill-price slippage is not subtracted again from recorded net results.
- Compact connection views are allowlisted from their own lane metadata; Overall's progress, account details, strategy blocks, costs, diagnostics and signals are not relabeled as one connection. Ownership is not inferred from USDT currency. Missing detailed evidence is explicitly marked.
- Three-day drawdown cards no longer substitute all-history metrics under a three-day label. Empty tape is not shown as successful performance.
- Paper-scope tails obey each Set's strict decision clock before Last-N truncation. The bounded merge avoids scanning every stored close.
- Failed service starts retain STOP/PAUSE state and return failure. Successful selected-lane starts preserve other lanes' global halt as per-lane flags. Automatic recovery respects disabled/masked units and requires complete configured credentials. Install/update reject a nonworking systemd manager before mutation. Active service state alone is not exchange readiness.
- Trader source pin and restore patch reproduce blob `6ca110bcc5aec16d938935172df8eeeb438f6d76`.

The React review kept the new panels stateless, with semantic table headers/captions, stable keys and no new UI dependencies. Syntax parsing is not a substitute for a TypeScript build or browser render test.

## Verification

| Check | Result |
| --- | --- |
| Initial engine baseline | 491 passed, 0 failed, 0 skipped |
| After timing/switch/evidence changes | 495 passed, 0 failed, 0 skipped; 460 s, 4 workers |
| Final engine run, including zero-allowance mean-net regression | 496 passed, 0 failed, 0 skipped; 441 s, 4 workers |
| Separate engine functional suite | 109/109 passed; its obsolete 50-symbol test expectation was corrected to the already requested/shipped 30-symbol overlay |
| Final restore/install/logistics checks | 27 passed, 0 failed, 0 skipped |
| Node script suites | 195 total: 191 passed, 4 expected optional skips, 0 failed |
| TypeScript test runner | 61 total: 59 passed, 2 dependency-loader failures (`@tanstack/react-start`, `jose`) |
| Startup controls | 13 unit methods passed |
| Strategy switches | 7 unit methods passed |
| Paper scopes | 6 unit methods passed, including 96 randomized comparisons |
| Independent stage arithmetic | 4 unit methods passed, including 80 randomized cases |
| Frontend build / typecheck / rendered UI | Not verified: missing installed Vite/TypeScript/app dependencies; dependency network access unavailable |

No dependency failure was converted to a passing test by disabling its suite. Test OPEN/CLOSE logs come from simulated venues, not exchange submissions.

The separate functional suite was first run unchanged: 108/109 passed, with only `x01-50-symbol-book` failing against the 30-symbol overlay. Its subsequent 109/109 pass changes the test, not the runtime symbol cap. No observed functional hang remained in these bounded runs.

## Scoped performance comparison

Same synthetic 1,654 Sets, 661,600 stored rows, Last-200, 12 scopes and 2,400 output rows:

| Metric | Before | After |
| --- | ---: | ---: |
| Median scope extraction | 371.429 ms | 31.213 ms |
| Stored-row reads | 661,600 | 54,122 |
| Peak RSS | 200,076 KiB | 204,176 KiB |

Output SHA-256 is identical: `0f628091649b65c5c1b5e164de3d67463ede719294e055f473a994a78ed8c499`. This is about 11.9× faster scope extraction and 91.8% fewer reads, not a full-engine speedup or memory improvement. See [before](paper-scopes-before-20261101.json) and [after](paper-scopes-after-20261101.json).

## Current market-history comparison

Unchanged x01 overlay, eight archived symbols, 85 hours available. Requested complete stage context is 108.5 hours and is not covered. Configured round-trip paper allowance is 0.12%; it is not a fetched account fee. The selected packs are Indications, General and Short Range, not actual CTS-A-O Signals.

| Stage | Qualified Sets | Share of 1,654 | Selected window samples | Net-return PF |
| --- | ---: | ---: | ---: | ---: |
| Base | 1 | 0.0604595% | 50 | 1.73549195 |
| Main | 0 | 0% | 0 | unavailable |
| Real | 0 | 0% | 0 | unavailable |
| Real Negative | 0 | 0% | 0 | unavailable |

The only endpoint Base-qualified Set is `indications:1m:sl0.75:st6`. Its causal Main history is empty, so it does not open a new Real entry. Base rejects 1,632 Sets for PF and 21 for the short window. There are 446,710 modeled raw paper closes, not funded orders. The current scope's latest-200 pooled paper PF is 0.0705223; the isolated positive Base PF is not evidence that subsequent trading is profitable.

Before, timing-fixed and switch/evidence replay economics agree. Cold runs took 212.304 s, 214.299 s and 231.093 s; peak RSS was about 1.31 GiB. These ran alongside other verification and do not establish full-cycle acceleration. The fixed 400M/640M service memory limits remain a release blocker.

The independently implemented arithmetic matched the existing adaptive chain for all 1,654 Sets, with zero differences. Five fixed-range experiments per Set produced 8,270 rows. The checker imports no production calculation helpers, but candidate entries, strategy signals, exits and holds still originate from the engine. This is not a wholly independent funded execution simulation.

The replay reports pin the exact engine/data hashes they tested; the later zero-allowance mean-net display correction and final frontend changes are covered separately by regression checks, not silently attributed to the older replay snapshot.

- [Before replay](current-stages-before-20261101.json)
- [After timing replay](current-stages-after-20261101.json)
- [Final stage/range replay](current-stages-final-20261101.json)
- [Every Set/range, line by line](independent-stage-lines-20261101.md)
- [Compressed machine-readable rows](independent-ranges-20261101.jsonl.gz)

Base uses Last-50 plus Last-25. Main long is 25; effective short candidates are 5 and 10, with a long-reference 25 experiment. Configured short 15 is excluded by the 0.5 ratio (integer cap 12). Negative ranges are 5 and 10; Real is 15. Static minimal 1–3 windows are off; a result still needs at least one measurable sample. These experiments are not separate production execution lanes.

## Older positive cohort

The frozen CTS-A-O ordinary cohort independently recounts to 336 modeled closes, net gain 1.5518340683, net loss 0.4198629735 and PF 3.696048869 in all four saved arms. Zero rows were added, removed or changed. The documented normalization is stored return divided by the external execution multiplier; recorded costs are not subtracted again.

This is a different engine, window and cohort, not evidence of current CTS-G or Signals profitability. Modeled counts were 47 opened / 42 settled / 5 open positions and 354 opened / 336 settled / 18 open orders. Actual venue counts remain unknown. Fixed-close maker/mixed/taker sensitivity excludes multi-leg rows and does not rerun signal selection, fills or qualification. See [positive reference recount](positive-reference-recheck-20261101.json).

## Remaining release blockers

- Simultaneously independent dynamic Last-N runtime lanes are not implemented; production still selects a candidate per Set.
- Actual CTS-A-O Signals and requested `-Active` position-type semantics have not been integrated or established by this audit. Signal vote settings are not equivalent to those position types.
- Complete strategy-independent signals/entries/exits/holds and funded execution simulation remain unverified, as do continuous forward progress and complete stage-history coverage.
- Browser rendering, production frontend build, complete TypeScript tests and typecheck are blocked by missing dependencies.
- Host-aware service memory sizing and sustained resource behavior remain unfinished.
- Venue reconciliation, protective order completeness, partial fills and statistics have offline tests but no live 30-symbol acceptance here. Current account balances, orders, positions and actual costs are unknown.
- No remote reinstall, production merge or real-money activation was performed. Do not infer live readiness or guaranteed positive PF from these offline checks.
