# Signals coordination and x02 acceptance — 1 November 2026

This is verified research and order-handling work, **not a production or
profitability approval**. It supersedes the validation status in the earlier
`signals-research-checkpoint-20261101.md`, which remains an historical record.
The independent Signals engine is not connected to Pulse's live entry loop.
No mainnet orders or live service deployment were performed.

## Exact Signals scope

`SignalProgress` rejects every strategy range except `signals` and `exit`.
Micro, Minimal, Short, General, Wide and Long cannot enter this strategy grid.
Source variants called `short` and `medium` are separate identities, not
ordinary strategy ranges. All 103 pinned CTS-A-O sources and 15 dominating
coordinations produce causal events on complete 1/5/15/30-minute candles.

| Control | Base | Main | Real admission |
|---|---|---|---|
| Long windows | 5, 10, 15, 20 | 5, **8**, 10, 15, 20 | Exact Main coordination by default |
| Short windows | 4, 6, 8, 10 | 4, 6, 8, 10 | Optional lane-minimal: its own last 1, 2 or 3 |
| Short/long maximum | 50% | 50% | Optional explicit fixed N, including 8 or 12 |
| Minimal | 1, 2, 3; positive and negative | 1, 2, 3; positive and negative | No extra fixed-N gate in coordinated mode |
| Minimum positive PF | 2.2 | 2.2 | 2.2 on the named validation evidence |

Nine original long/short pairs are impossible at a 50% ratio, including all
long-5 pairs with short >=4. They are listed as excluded, never rounded into
another rule. The original grid has 42 legal rules per stage; Main's added
8/4 option gives 48. Each protection/source identity therefore has 2,016
possible Base/Main paths. Rule listings include stable IDs.

Long and short context windows end before the minimal window in both modes.
The negative minimal is a condition, not an inverted strategy. Its losses
remain in cumulative and executed returns. Only closes strictly earlier than
the entry decision qualify. Expired, missing and equal-clock evidence does not
pass. Each Main path owns its history; each Base/Main pair owns its Real
history. Sharing occurs only for mathematically identical Base simulations.

`realLastN="coordinated"` uses that exact lane's current complete Main proof.
It does not pretend that the Real stream itself has a high PF: summary
`realValidationStage` and candidate `validationStage` identify the evidence,
while Real cumulative gains/losses/PF remain separately measured. The
`lane-minimal` mode and fixed integer mode test the Real lane's own closes.
The ordinary Pulse coordinator does not acquire an extra Signals gate.

## Replay findings

Frozen eight-symbol candles, 100 hours of prehistory and 12 hours of trading
on 9 October 2026, 00:00–12:00 UTC. Each source has a separate modeled capital
book; these portfolios must not be summed as one funded account. Results are
exploratory and net of configured taker fees (0.05% each leg) and adverse
slippage (0.01% each leg). No untouched broad holdout or hourly profit promise.

| Study | Sources | Main passes at end | Main qualifying-window PF | Settled executions | Positive source books |
|---|---:|---:|---:|---:|---:|
| Original 42/42 windows, Real 12 | 118 | 154 | 3.5027 | 0 | 0 |
| Main 8/4 added, coordinated Real | 118 | 890 | 3.8420 | 373 | 5 |
| Lane-minimal Real, preselected subset | 12 | 487 | 3.9807 | 30 | 1 |
| Smaller-distance profiles, 1% context DD | 118 | See machine report | See machine report | 3 | 0 |

The original baseline predates the numerical source correction; corrected
source events changed 15 of 7,072 streams. The coordinated run uses all corrected
events. Main's full-chain passes rose from 7 to 34; Real had 28 full-chain
admissible paths at the final snapshot. There is no fixed last-12 Main gate in
this engine. The fixed last-12 restriction was in Real.

The high gate PF is **not forward execution PF**. Across all hypothetical
coordinated-run histories, cumulative Base/Main/Real PF was approximately
0.5575 / 0.6151 / 0.3797. No losses were removed to increase these numbers.

Two first-window candidates met net-positive, >=10 settled, PF>=2.2 and
drawdown<=0.2% requirements. Their source selection and configuration were
frozen before running the next 12 hours:

| Source | First window net / PF / closes | Next window net / PF / closes |
|---|---|---|
| r-clv-thrust | +0.511338 / 8.5441 / 19 | -0.347123 / 0 / 11 |
| s2-block-scale | +0.337945 / 4.0375 / 15 | -0.205629 / 0.2346 / 9 |

Both failed the adjacent-window check. The one positive dynamic-Real source
had only one close and is insufficient evidence for a performing default.
Earlier Real 1/3/6 studies also remain archived, including negative outcomes.

## Additional smaller-distance candidates

`presets/signals-low-distance-candidates.json` adds each profile below for both
Signals and Exit. Every distance exceeds the computed two-leg break-even
requirement. Its context drawdown ceiling is 1% at all positive stage gates;
the separate modeled book limits drawdown admissions to 0.5%, hourly loss
admissions to 0.2%, four open orders and 40 gross exposure per 1,000 capital.
These controls pause admissions; they do not guarantee a hard loss bound.

| Type | SL | TP | Trail arm | Trail gap | Max hold |
|---|---:|---:|---:|---:|---:|
| Normal tight | 0.30% | 0.45% | — | — | 45 min |
| Normal balanced | 0.45% | 0.70% | — | — | 60 min |
| Trailing tight | 0.35% | 0.90% | 0.45% | 0.20% | 60 min |
| Trailing only tight | 0.45% | — | 0.60% | 0.25% | 90 min |

All 118 sources were tested. Only three executions occurred, all losing.
These profiles are deliberately labeled **candidates**, not proven profitable
or enabled live defaults. The independent preset retains the previously
tested normal/trailing/trailing-only low and middle profiles.

## Order correctness and actual x02 VST tests

An actual VST rejection (109400) established that BingX trigger-market
protection requires quantity even with `closePosition=true`. Payloads and
functional expectations now match the observed exchange contract. Overall
controls retain their full-position flag as stacks grow; quantity-limited
controls require the full remaining amount, not a 95% tolerance. Replacements
are confirmed before old orders are cancelled. Cleanup is scoped to owned
IDs, symbol and hedge direction. Failed closes keep protective coverage.

Cumulative protective partial fills are indexed by order ID and book only new
quantity/value/fee deltas. Smaller position snapshots reconcile those fills
before changing quantity. Sub-0.1% remainders are retained. Fully filled saved
journals finalize correctly after restart; unknown exits persist in the audit
and remain excluded from PF. QA market timeouts query the original client ID
and never automatically resubmit under a new ID.

Bounded x02 VST acceptance passed **30/30 checks** with eight market fills:
DOGE long 54 + 27, partial close 27, final close 54; ADA short 22 + 11,
partial close 11, final close 22. Both sides finished flat, with no test orders
left. Overall SL/TP presence, values and close-position coverage were read
back after stacking. Protective orders were not deliberately triggered.

An independent Decimal fill ledger matched the engine's net cash to <1e-10:
DOGE -0.007702 after 0.006082 fees; ADA -0.016221 after 0.006321 fees. These
are order-plumbing tests, not profitable strategy executions. Identifiers in
the public acceptance evidence are hashed; credentials are excluded.

## Verification and remaining integration work

- Full Python regression: 505 passed, zero failed or skipped. After the final
  policy-listing change: all Signals checks and 20 progression tests passed.
- Full controlled 1,764 original rule-pair reconstruction plus all 252 new
  Main-8 pairs. Independent real-feed scalar oracle: 24 roots / 1,435 paths
  for coordinated mode, and 12 roots / 648 paths for dynamic Real; all matched.
- Engine functional checks: 109/109. Node: 191 passed, four environment skips.
  TypeScript: 96 passed. Typecheck and production build passed.
- Protective coverage: 22 tests. Demo safeguards: six tests. Catalogue tests
  cover all sources, strict prefix causality, missing data, independence and
  stable variance/CCI calculations.
- Desktop/mobile dev and built output rendered with no uncaught page errors
  or horizontal overflow; final snapshots agree. **Full browser smoke did not
  pass**: this workspace lacks the live sidecar/connection service
  (`connections.json` returns 500); external font/platform requests fail.
  Offline display zeros are not an account-equity verification.
- Event lookup, bounded history, cached windows, active-position iteration and
  lazy child allocation avoid materializing the full configuration product.
  Source generation took about 3.6 seconds for the 112-hour eight-symbol input.
  Worst per-source p95 minute step was 24.35 ms in the coordinated study.

The recovery patch and deployment pin reproduce trader blob
`6c6aa8bc42856371f86dcb0773f6faf4d994175f`. Code and evidence were isolated
from the other chat's ongoing checkout before this change was prepared.

Still required before a live Signals release: the live event adapter,
multi-lane execution ownership/netting and DCA/Block/Relate stack integration,
full frontend-to-engine acceptance, and prospective performance evidence.
Replay does not model funding, liquidation, liquidity or exchange partial
fills. None of the tested configurations establishes positive net every hour.
The integration target is the existing research branch, not an automatic
production deployment or a merge of the other chat's unreviewed edits.

Machine evidence is under `reports/signals-verified-20261101/`; the frozen raw
replays, source events, test logs and browser images are preserved separately.
