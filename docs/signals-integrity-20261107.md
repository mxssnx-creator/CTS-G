# Signal integrity and complete catalogue study — 7 November 2026

Parent: merged Stable-02-01 `9093ac634b4f75b596098293b02bfccd4a76fe6c` (PR #146). Ordinary indication/execution results remain in `execution-integrity-20261107.md`; they are not Signal-engine results.

## Repairs

Signal Last-N memoization used the policy object's memory identity. Replacing rules in the same list could return a previous policy's passing windows; an object-id reuse could also collide. Cache by frozen rule values. The regression changes a passing positive policy to a negative policy and proves the old pass is not reused.

Checkpoint restore now requires integral history/open counts, rejects missing retained gains/losses and reconciles complete retained histories with cumulative totals. Relative-only summation tolerance preserves arbitrarily small losses. Open positions must retain positive entry and quantity. A valid incremental restart remains identical to uninterrupted processing. These are consistency checks, not authentication of an external checkpoint.

Research tools now accept an explicit symbol count and trading duration. Existing defaults remain eight symbols and 12h trading. The report preserves all scenarios/hours and distinguishes historical stream diagnostics, qualified windows and subsequent funded-model orders.

## Frozen 12-symbol, 24-hour Signal study

Trading: 6 November 19:00 to 7 November 19:00 UTC; prehistory: preceding 24h. Same twelve BTC/ETH/SOL/XRP/BNB/DOGE/ADA/AVAX/LINK/DOT/BCH/LTC public market tapes as the ordinary replay, with the earlier alignment hour excluded. No subsequent data or additional warmup is used.

103 pinned source indications plus 15 dominating coordinations; 10,608 event streams and 537,573 generated events; complete UTC 1/5/15/30m bars. Twelve protection profiles cover Normal, Trailing and Trailing-only in separate Signals/Exit lanes, short/medium source variants and both directions. All 42 Base × 48 Main policies remain enabled. PF floor 2.2 and maximum context drawdown 4% remain unchanged. Real uses the configured current Main proof; this mode intentionally has no duplicate fixed Real-close gate. Research history is explicitly 24h rather than the preset's 100h.

Each of 118 source scenarios owns a separate $1,000 capital book, $10/order, maximum eight open orders, 2% drawdown and 0.5% hourly-loss controls. Current public maker/taker estimates are 0.02%/0.05%; both legs are taker, with 0.005% per-leg slippage (10% of the fee). Charges use each modeled fill notional. Funding, liquidation, queue fills and account-specific fees are not modeled.

**Result: no profitable funded scenario.** `r-fisher` opened and closed two BCH short Trailing-only/Exit orders, both losing: return PF 0, equity change −$0.0151411492, maximum marked drawdown 0.0074594%. Its 24 hours contain one positive, one negative and 22 flat mark-to-market changes. The other 117 sources opened zero orders; their execution PF is unavailable, not zero or a win. No preset was promoted.

Final snapshot totals below sum configuration populations across separate source experiments; they are coverage counts, not one portfolio. Passing-window PF and percentages for every source appear in the HTML. All final Base/Main/Real streams include losses and overlap across policy paths.

| Stage | Passing / configured paths | Current chain passes | Allocated streams | Historical closes (48h) |
|---|---:|---:|---:|---:|
| Base | 939 / 10,692,864 | 939 | 254,592 | 3,470,283 |
| Main | 5 / 513,257,472 | 0 | 30,320 | 49,057 |
| Real | 0 / 513,257,472 | 0 | 2 | 2 |

Insufficient/unallocated paths remain in denominators. Main can pass its own history while its current Base predecessor no longer passes; that does not make the chain executable. The two trades were admitted earlier with their own then-current proof, and their later losses remain visible.

## Verification and resources

22 progression/window/restart tests pass, including full independent Base/Main path reconstruction and new regressions. Four standard Signal regression groups pass (source math, replay, progression, pinned catalogue); typecheck passes. The prior ordinary-engine suite passed 615 checks before this isolated Signal follow-up.

An independent scalar reconstruction matches 24 market roots, 848 policy paths and their archived streams. All 118 archive hashes, 2,832 hourly rows, opened/closed/held counts and every settled cash/return calculation reconcile; maximum cash discrepancy is zero. HTML data and embedded JavaScript syntax checks pass. Browser rendering could not be inspected because of environment restrictions.

Four local worker processes; sum of worker run times 835.65s (not wall time), maximum observed worker RSS 163.01 MiB. Worst minute progression step across a 12-symbol scenario: 282.27ms; largest per-scenario p95: 22.68ms. These are offline host measurements, not live-loop guarantees.

## Future release requirements and unresolved scope

Preserve source/config/type/range/side identities, exact predecessor history, losses and costs. Never manufacture PF by bonuses, omitted losing orders, future observations or an empty window. Check immutable rule keys, restart conservation, exit ownership and an independent scalar path before changing progression. Freeze any proposed new strategy using earlier data, retain a later evaluation period and publish open marks and every hour. High historical PF is not a guarantee of high future PF.

The full source/progression engine remains **offline only**; there is no connection to the ordinary Pulse live order adapter, no Signal DCA/Block/Relate fill integration and no authenticated x01 execution test. The ordinary x01-labelled 200-symbol check is public candle/indication readiness only. Micro and several ordinary Exit/Trailing scenarios also remain unprofitable. Do not describe this work as all-strategy profitability, complete live integration or a guarantee of future PF.

CTS-A-O independently merged #247 (delayed Signal settlement ordering), and CTS-A merged #11 (real losses, honest window PF, tiny/unknown evidence and legacy-preset labeling). Their results must not be substituted for CTS-G execution evidence. The CTS-A-O persistent stage adapter remains pending.
