# Twelve-symbol native VST settlement — 12 November 2026

The `CTS-G-12e` native session used twelve ordinary crypto contracts, selected by public liquidity, valid price increments and available contiguous observations. The cohort was selected before evaluating PF. It warmed 3,990 closed minute observations per symbol and observed the native engine for 300.2685 seconds. All twelve commission rates came from the authenticated account-wide fee response: maker 0.02%, taker 0.05% per fill. Shared-account test settings prevented leverage and margin mutations; maximum leverage was not established by this run.

Two short parents executed, each with an entry, two protective orders and a bounded-session cleanup close. The original report remains `complete: false` because its final lookup could not establish terminal receipts. An independent follow-up read bound the eight already acknowledged venue IDs: four market orders were FILLED and four controls CANCELLED. The two selected symbol sides and their open-order books were then flat. No unrelated account order was cancelled or position closed.

| Parent | Entry quantity | Entry | Cleanup exit | Reported gross VST | Signed commissions VST | Net VST |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| EVAA short | 1.928 | 1.0375 | 1.0414 | -0.007500 | -0.002004 | -0.009504 |
| PENGU short | 322.7 | 0.006201 | 0.006221 | -0.006400 | -0.002005 | -0.008405 |

The own-execution total was **-0.017909 VST; PF 0 from two closed parents**. Both exits were session cleanup, not natural strategy exits. The tiny difference from fill-price multiplication reflects the venue's reported gross rounding. Actual signed commissions were deducted once; execution prices already contain slippage. Modeled round-trip cost was 0.11%, compared with measured total taker commission 0.10%. These two forced exits do not establish sustained strategy profitability, Micro floor adequacy, or a representative sim/live PF difference.

An absent-order response was previously treated as a global cooldown and CID-only archive queries could not resolve batch control acknowledgements. Current Stable keeps the signed-byte transport, classifies explicit order absence separately from a true rate limit, and resolves only exact, previously bound journal identities with bounded terminal evidence. The earlier proof-of-concept acknowledgement helper was removed in favour of that shared implementation. Local native-session, signed-pool and cleanup-evidence checks cover this combined source; hosted checks must pass on the final published head.

The failed original report and the separate recovery proof retain distinct status. No negative fill, unavailable sample or preflight refusal is relabelled as a successful profitable run.
