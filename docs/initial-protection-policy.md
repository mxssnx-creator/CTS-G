# Initial protection and independent lanes — 8 November 2026

`minSlDistancePct` is the overall net initial-stop distance: 0.1–1.0% of entry price, default 0.1%. Requested SL/TP ratios are 0.25–2.5 in increments of 0.25. Legacy ratios normalize to that grid; duplicates are removed. Different requested ratios can produce the same effective stop floor while remaining distinct configurations.

For entry E, target distance T and ratio R:

    initial SL distance = max(T × R, E × (modeled round-trip cost + minSlDistancePct / 100), venue minimum)

Raising SL never widens the requested TP. Round initial stops outward to venue ticks. A ceiling below the minimum rejects the candidate; it must not tighten the stop below the floor. This applies to INITIAL protection: a trailing stop can tighten after favorable movement.

Fee policies are scoped to the exchange/account. Fallback maker/taker rates are 0.05%/0.08%; default slippage is 10% of modeled fees. Two taker legs therefore cost 0.176%, giving a default initial SL floor of 0.276%. The observed x02 VST rates were 0.02%/0.05%, yielding modeled cost 0.11% and floor 0.21%. Actual fills retain their fee provenance. Label estimates and never deduct costs twice.

Held orders retain captured protection and economics. New settings govern future admissions, not widening held stops. Additions and partial closes retain parent ownership, actual quantities/prices and remaining exposure. Count sets, admissions, fills, held positions, close slices and completed positions separately.

PF is positive net returns divided by absolute negative net returns; neutral is 1.0. Empty or unknown evidence is unavailable. Selection-window PF, processed-stage PF, settled execution PF and settled-plus-endpoint-mark PF describe different cohorts. Preserve losing outcomes. No threshold guarantees future PF, order count or low drawdown. Cost may consume almost the whole lowest target.

The new 12-symbol/24-hour CTS-G replay produced 58 closes, settled PF 1.206 and 12 held positions; including final marks lowers PF to 0.778. Minimal had 51 closes/PF 1.557; Short 7/PF 0.433; Micro none. These results do not justify a profitable-default claim. Extended coordination candidates remain research configurations. The expanded 8,426-set 12-symbol/24-hour run had 32 closes/PF 0.933 and marked PF 0.517; it was not promoted to a profitable default. The wider 24,770-set three-symbol/6-hour screen admitted no trades.

The x02 harness only supports VST. Geometry probes are deliberate admissions; native qualified signal decisions run in shadow. Probe PF is not strategy PF. Sampled REST marks cannot establish exact tick-level parity. Aborted sessions remain incomplete even after cleanup. Acceptance requires 50 accepted symbols, ten active minutes after admission, resolved order identities, flat owned exposure and no orphaned test controls.

CTS-A native complete-grid generation now emits all 20 Micro cells and 90 Minimal cells, including Trend/Break indication lanes. Partial fills match their original order before matching new configuration identities; queue rebalancing runs only when capacity is exhausted.

CTS-A-O processing and all 128 toggle combinations passed with two compute workers in 249 seconds. One-worker runs exceeded the fixture’s former 400-second wait; this is cold compute latency, not measured active-order latency.

Known remaining work: CTS-A-O's processed Main-to-Real predecessor book is not wired into all admission paths (`processed-main-conflict.test.ts`). A raw-candidate pass cannot establish that this gap is fixed. Exhaustive profitability and native exchange parity remain acceptance conditions, not inferred successes.

Research sources:

- [BingX official trade API](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-trade/api-reference.md)
- [BingX contract metadata](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-market/api-reference.md)
- [Svogun & Bazán-Palomino: costs and cryptocurrency technical rules](https://doi.org/10.1016/j.intfin.2022.101601)
- [Hudson & Urquhart: technical trading and cryptocurrencies](https://doi.org/10.1007/s10479-019-03357-1)
- [Bailey et al.: probability of backtest overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)

Donchian, Keltner and VWAP-z with independent Trend, Break or combined confirmation are hypotheses inferred for testing. These sources do not validate these exact configurations. Keep causal holdouts, record every tested configuration and include failures before promoting defaults.

Verification summary: CTS-G engine 109/109, Python regression 639/639, Node 191 passed/4 skipped, TypeScript 104/104 and Signals replay 22/22. CTS-A protection 19/19, ownership/partials 9/9 and stable regression 52/52; its broad legacy VST suite remains failing and its default script suite has unrelated missing-template/environment failures. CTS-A-O core run had 4,801 passes, a subsequently repaired migration fixture and processing cancellations; migration reruns and the six processing checks passed. Study/configuration fixtures 33/33 and bounded-window launch 1/1 pass; browser-dependent checks remain unavailable locally. All three projects passed type checks and production builds. These are scoped results, not an all-tests-green claim.

CTS-A generated-price 12-symbol/24-hour studies: Minimal closed 3 at return PF 0; Axis and Axis+Block each closed 9 at return PF 0.0652. They are lifecycle fixtures, not historical-market performance. No Block improvement was observed in this sample.
