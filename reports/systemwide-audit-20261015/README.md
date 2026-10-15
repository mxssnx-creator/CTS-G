# CTS-G six-hour preparation and execution audit

The market snapshot was frozen at **2026-10-14 23:08 UTC**. Preparation covers
11:08–17:08 UTC; simulated execution covers 17:08–23:08 UTC. Earlier candles
warm indicators only and do not supply PF qualification samples. Both profiles
start with a separate simulated **1,000 USDT** shared account.

The baseline retains the frozen production settings. The separately labelled
short-history experiment uses five completed own samples for Base, Main and
Real. That horizon-based experiment was chosen before inspecting its execution
outcome. It keeps the same configured 1.10 PF floor, strict PF > 1.02 boundary
and 0.18% roundtrip PositionCost. It does not change the production defaults.

Return PF is `1 + 0.10 * mean(net return / PositionCost reference)`. Net returns
deduct configured fees once and include the applicable funding payments.
Empty or unavailable evidence is shown as unavailable. Open positions affect
equity but never closed-trade PF. Historical stage cohorts, sized account
executions and physical/control order counts are separate populations.

Every requested symbol, indication configuration, Normal/Trailing exit,
direction and Axis/Block/DCA child retains its own identity and evidence.
Identical numerical calculations can share immutable tapes; actual live
results remain independent. Descendants whose own parent never clears Base
are recorded as upstream-ineligible rather than receiving invented evidence.
The HTML reports include hourly accounts, stage and strategy calculations,
equity contributions and drawdowns, entries, closes, open positions, fees,
funding and request counts, with downloadable tables.

The frozen overlay enables both Overall and PerConfig controls. Native Overall
controls take precedence for the physical non-HTF symbol/side protection pair;
internal configuration lots and their gates remain independent. Long outer
protection uses the lowest member stop and highest member target; short uses
the highest stop and lowest target. HTF retains its own pair. Logical orders
and simulated serviced API requests are reported separately.

The frozen universe contains 50 symbols. Public data is complete for 48;
EOS-USDT and MKR-USDT were unavailable in the instrument/candle endpoints and
are explicitly excluded. No substitutes or synthetic candles are used. The
input archive is `cts-g-six-hour-market-input-20261014T2308Z.zip`; the source
manifest records hashes for each public response. `raw-cache-parity.json`
records exact current-source parity across all 2,958,047 BTC replay rows,
catalog identities, lane coverage and bars; reuse for other symbols also
requires their unchanged input/overlay hashes and configured geometry.

This is a public-candle paper simulation. Its report records the assumptions
for candle ordering, fills, order-rate capacity and protection. Account-specific
liquidation tiers, measured spread, slippage, latency and exchange partial-fill
behaviour are unavailable. Profitability is a measured result of each profile,
not a completion criterion that can be forced by changing the evidence.

Reproduction uses the checked-in fetcher, `scripts/run_six_hour_audit.py`,
`scripts/six_hour_account.py` and `scripts/render_six_hour_audit.py`. Keep the
frozen input archive and overlay; do not fetch today's candles as a substitute
for this window. Generated raw replay caches are outside Git and can be rebuilt.
