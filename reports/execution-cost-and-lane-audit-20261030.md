# Execution cost, Main PF and lane audit — 2026-10-30

Branch: `Stable-02-01`. This report supersedes the operational limitations in `main-live-validation-audit-20261030.md`.

## Result and scope

Several accounting and ownership bugs were reproduced and corrected. The production `SetBook.replay_all` path reproduces higher selected Main PF than selected Base PF in all three saved market snapshots. Paper stages and actual fills now have separate evidence and counts. The execution tests use deterministic venues inside the process; no real-money engine or order was started.

This establishes specific calculation and processing invariants. It does **not** establish that future Main/executed PF must always exceed Base PF, or that Block sizing is optimal. Base-selected windows, causally admitted Main windows, and actual exchange fills contain different trades. Their PFs must be reported separately, with sample counts. The code never copies or clamps one PF to another.

## Reproduced faults and changes

| Fault | Effect | Correction and evidence |
| --- | --- | --- |
| Paper and actual closes shared stage evidence | Overlapping entries were counted twice; live closes changed Main short selection without replaying its causal chain | Paper-only Base/Main/Real; separate bounded actual windows per Set and positive/negative lane. `correctness.paper-actual-isolation` verifies actual losses leave all paper streams and statistics unchanged. |
| Axis selected at pack/lane scope and positive preference | A stronger rejected Set, or one lane of a Set, could hide a valid alternative | Check every candidate's own Set/lane paper stream and continuation capacity; try both lanes of the same Set. Optional DCA Axis admission also checks its parent's exact scope. |
| Replay caches survived changes to signal, cost or exit/hold rules | Old evidence could validate newly configured behavior | Fingerprint relevant settings, clear paper evidence and hold selections, reject entries until a fresh complete pass; discard stale-generation chunks. |
| An add changed the aggregate entry/quantity | Parent stop/hold or Set return could be evaluated on the add-on's average | Persist original base entry/quantity and chosen hold; keep parent rules fixed; allocate base, Block and DCA returns separately. DCA adds use notional-weighted returns. |
| Fixed round-trip allowance reused on actual fills | Exchange fees were not represented consistently; reports could charge recorded net a second time | Account/contract fee lookup, fill-level commission precedence, entry and exit fees on their own notionals; immutable explicit net shared by Axis, Exit, DCA, variants, exports and UI. |
| Partial close followed by a failed remainder/restart | First fill's value/quantity could disappear from the completed result | Persist partial quantity, value and fees. The oracle closes 40% at 101 and 60% at 102: final price 101.6, original quantity, exact cash net. |
| Accepted order or explicit zero treated as a completed fill | Requested volume/reference price could become fictitious execution | Resolve uncertain entry/Set Block acknowledgements with a read. Uncertain closes persist their order identity and wait for that same order; no duplicate close is sent. Only confirmed fills reach PF. |
| Position refresh overwrote quantity while a close was unconfirmed | A 1-unit pending close became 0.6 locally; restart could discard the eventual result as a ghost | Resolve the same pending order before reconciliation, retain the original quantity/fees/reason, settle once, and continue only the confirmed remainder. New regressions reproduce the pre-fix 0.6 bug and verify exact final net after restart. |
| Historical close filters depended on current size limits | Shrinking volume settings removed old trades; large Block positions could disappear after restart | Keep all verified owned/connection-matching outcomes regardless of today's cap. Verified emergency closes retain their losses. Unknown exit prices are excluded and counted explicitly. |
| Restart seeded Set closes into default Exit/SL/trailing books | Cross-strategy scores changed only after restart | Ownership checks inside both consumers reject Set records, including history seeding. |
| Only total execution counts restored | Positive/negative, Block-add and exact-Set counts reset | Restore the same scope counters as the live close path; reject foreign connections; zero net is not a loss. |
| Block pause count was reduced to a boolean; zero loaded as one | A two-observation pause could resume early; disabling it had no effect | Preserve zero and check the last N own group observations. Regression covers two observations, recovery and zero. |
| Flat RSI was directional; new price-window features lacked an independent oracle | Flat data could add biased indication votes | Flat RSI is 50. Rolling drawdown/Ulcer/last-part features match a plain calculation to below 1e-12 in the regression oracle. |

The previous stage fixes remain: completed candles only; per-symbol event times; collision-free Set types; Main short selection using information closed before each entry; long last N starting at 10; minimal N 1–3 as sign coordination. Negative minimal never receives a positive-PF threshold. All eight packs and base/Exit/trailing types are included in bounded coverage.

## Costs and units

`positionCostPct` and the slippage allowance are percentages; exchange rates are fractions. A read-only account query for BTC-USDT returned maker `0.0002` (0.02%) and taker `0.0005` (0.05%). Other symbols are refreshed independently at runtime; this read does not verify every symbol/account rate.

Paper allowance = max(configured PositionCost, 2 × highest known contract/account taker rate + slippage allowance), with unit conversion. The configured 0.12% floor and 0.02% slippage allowance produce 0.12% with those taker fees. This is one allowance, not an extra fee added to 0.12%.

For a long position, actual net cash is `quantity × (exit − entry) − entry fee − exit fee`; the price difference reverses for a short. Each fee estimate uses its own fill notional. Actual fill prices already contain execution slippage, so no paper slippage allowance is subtracted again. A LIMIT order is not assumed to be maker; explicit maker evidence is required. Reported zero commission is preserved. Known net remains fixed when a later paper-cost setting changes.

Provenance distinguishes reported fees, exchange-rate estimates and configured estimates. Funding fees and non-USDT fee conversion are not yet attributed to individual positions. Historical records without original fill/commission data cannot be reconstructed exactly from this patch alone.

Primary protocol references:

- [BingX account commission API](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-account/api-reference.md)
- [BingX contract market fields](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-market/api-reference.md)
- [BingX order status and execution fields](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-trade/api-reference.md)
- [Bybit fee-rate endpoint](https://bybit-exchange.github.io/docs/v5/account/fee-rate)
- [Bybit asynchronous order acknowledgement](https://bybit-exchange.github.io/docs/v5/order/create-order)

## Block and volume

Set Block has a separate persisted book keyed by `(Set, lane, symbol, side, group)`. Group n observes the first price n PositionCosts in profit and measures that hypothetical add until the parent's exit. Actual adds form a different sample. Rejected groups continue gathering observations so they can recover without opening losing proof trades. Parent PF, another group, or another lane cannot supply missing proof.

Default qualification is eight own observations, a 25-result window, and the configured group PF floor. Each qualifying group adds at most `blockVolumeRatio × original base quantity`; volume does not compound on earlier adds or carry to another Set parent. Six groups at ratio 1 allow at most 7× base before other limits, not the former triangular 22× calculation.

The total planned loss at the existing parent stop, with a fee allowance, is capped at `blockRiskPctOfBalance` (default 0.25% of equity). Available margin, book limits and exchange lot steps can reduce or reject an add. Minimum lot size cannot override this budget. A gap/slippage can exceed a planned-stop budget. DCA and Set Block cannot both add to the same parent; neither adds while a partial/uncertain close is pending.

The bounded market run produced zero qualified Block adds. It therefore proves neither improved Block PF nor an optimal volume ratio. The deterministic integration tests exercise qualified adds, rejected lanes, accounting, protection, restart and independent evidence.

## Bounded production replay and modeled executions

Input: saved BTC, ETH, SOL and DOGE one-minute candles, 2026-10-14, 660 bars per symbol (11 hours). Three six-hour rolling snapshots, reduced grid of 438 Sets, all eight packs, automatic hold selection, positive minimal N=1, 0.12% paper cost. These are selected **in-sample paper windows** from the production engine.

| Snapshot UTC | Base-valid / configured | Base selected PF | Base window orders | Main-valid Sets | Main selected PF | Main window orders | Final eligible Sets |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 18:08 | 178 / 438 | 20.0207 | 3,336 | 174 | 28.7416 | 2,592 | 174 |
| 20:38 | 159 / 438 | 37.4792 | 2,295 | 120 | 54.1170 | 1,688 | 121 |
| 23:08 | 325 / 438 | 9.3053 | 4,208 | 287 | 34.4693 | 2,523 | 287 |

[Machine-readable snapshots](market-snapshot-20261030.json) include every strategy/type/step-range group. The earlier negative-minimal snapshots had zero final eligible Sets; correct support for negative coordination does not imply profitable examples exist in this sample.

A separate causal execution model starts after six hours of prehistory, limits each symbol to one open position, and then advances five hours. It uses the production lane exits, stage chain, Coordinator and Set Block book. The Axis-off, Axis-on and Axis-plus-Block variants each close 13 modeled positions, leave four open, and observe no net loss in those 13 closes. The engine's PF=99 sentinel means **no observed losses**, not a finite estimated PF. Total normalized net return is 0.0177696; this sum is not account ROI. No Block group meets its proof requirement.

| Strategy/type/step/lane | Modeled closes | Observed PF |
| --- | ---: | --- |
| indications / base / 15 / positive | 2 | no losses |
| flow / base / 25 / positive | 2 | no losses |
| trend / base / 14–15 / positive | 3 | no losses |
| multi / base / 16, 19, 25 / positive | 5 | no losses |
| general / base / 17 / positive | 1 | no losses |

[Modeled execution details](bounded-execution-20261030.json). Runtime was 60.8 seconds; parent-process peak RSS was about 31.8 MB (not a summed worker peak). The model uses fixed hold and completed-candle prices; it does not model intrabar prices, order latency or actual venue fills. Short Range signal generation is exercised, but this sample did not produce a Short Range execution.

## Verification

[Machine-readable check results](regression-verification-20261030.json) record every Python check and the verified source blobs.

All new tests/replays use at most three days of data. The expanded grid is 1,654 x01 Sets (819 base, 405 Exit, 430 trailing). The 50-symbol six-hour test covers 103 reduced-grid configurations × 50 symbol replays; configuration Sets pool their own symbol evidence, so this is not 5,150 independent global configuration objects.

Regression coverage includes independent stage oracles and process-debug membership, prefix causality, all indication/strategy packs, grid identities, hold/exit equivalence, fee units, partial fills, acknowledgement handling, restart, lane ownership, statistics exports and bounded processing. The final full Python engine run passed **408/408** checks (no failures or skips, 473 seconds, four workers), including the new pending-close reconciliation regressions. The read-only monitoring suite subsequently passed **6/6** targeted checks; it is included in the regression registry, now 414 checks. Old mutation-suite results are not claimed as a new mutation run.

| Verification | Result |
| --- | --- |
| `python scripts/regression/run.py` | 408 pass, 0 fail, 0 skip (before the separate monitor suite was registered) |
| `python scripts/regression/run.py --only monitoring --jobs 1` | 6 pass, 0 fail |
| `node scripts/run-ts-tests.mjs` | 72 pass, 0 fail |
| `node --test scripts/*.test.mjs` | 191 pass, 0 fail, 4 skips for absent optional OG-skill documentation |
| `npm run typecheck` | Pass |
| `npm run build` | Pass; external database migration skipped because no database URL is configured |
| Python compilation and `git diff --check` | Pass |
| Browser settings, desktop 1280×800 and mobile 390×844 | Slippage and Block-risk edits persist across tab changes; no internal HTTP errors, uncaught exceptions or horizontal overflow; development and built preview agree |

The browser check used a local read-only Pulse HTTP fixture, without engine startup, credentials or saved settings. It exposed a preview routing fault: Nitro handled requests before Vite's Pulse proxy, returning 404 for stats/config. Preview now registers Pulse routes before Nitro; the Linux desk already uses the development route. External Google Fonts and Grok branding requests are blocked by this test environment, so the raw smoke command still reports those two network errors. They were not removed or hidden. The agent-browser daemon could not initialize here; the installed Playwright browser provided the verified desktop/mobile checks instead.

The settings view now distinguishes Set Block adds from the separate non-Set overlay formula, preserves volume 0.05 in its displayed precision, and labels a stopped desk as offline/halted.

No x01 live trading was launched. A read-only account check reported no available margin at that time. This is not an exchange execution acceptance test or evidence of sustainable profitability.


## Read-only runtime monitoring

The x01 overlay requests all eligible symbols with `symbolCap: 50`. This verifies the checked-in configuration, not the running server's active universe. Current remote checks returned HTTP 503 or timeout for the Pulse status endpoint; the desk and CTS status endpoints also timed out. Runtime health and continuous stability remain **unverified**. No remote deployment, engine start, settings save or exchange mutation was performed.

`scripts/monitor_readonly.py` performs only bounded GETs of `/stats.json`. It checks connection identity, timestamps, cycle/replay progress, 50-symbol coverage, halted/paused state and increased error counts; it reports the selected paper Base/Main windows separately. It never uses the existing `monitor_engines.py` auto-restart behavior. `--once` returns a failing exit status for unavailable/unhealthy status; repeated mode prints bounded health records for external supervision.

An hourly read-only status watch was enabled in ChatGPT at the user's request. Hourly alerts are not continuous process supervision, do not keep a trading engine running, and cannot verify inaccessible runtime data.
