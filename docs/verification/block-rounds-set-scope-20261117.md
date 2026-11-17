# Block rounds: independent Set and count scope

Block rounds now keep recovery state and automatic last-N evidence separately for each Set, lane, selected relation key and block count. An unscored position occupies its place in the evaluation window and makes its block ineligible to change the recovery state. A version-1 state file can still be loaded: old pooled groups and outstanding memberships are retained, but their pooled results cannot qualify a newly scoped Set. New groups accumulate their own evidence. Until the configured Auto minimum is reached, the existing manual-switch fallback remains in effect; Auto is not yet a profitability verdict in that period. Version-2 state cannot be read by older releases, which disable raises on an unreadable file.

This change concerns Block **rounds**. The separate Set Block add-on and its deficit guard require their own isolation review. These are not exchange execution results.

Local verification: eight focused scope, unknown-result and restart tests passed; the engine regression suite reported 753 passes, no failures and one skip, and the desk type check passed. The tests exercise simulated or stubbed venues, not an exchange account.

A retrospective sizing-overlay study on a frozen 20-symbol research ledger did not establish a profitable default. With Block off the modeled return-based PF was 1.197 in the first 12 hours and 0.924 in the next 12 hours. None of the 22 tested arms maintained PF above 1 in the second half. Most Set/count combinations had too few outcomes for a 15-result Auto verdict. The underlying candidates overlap and are not a shared-capital account simulation; no drawdown or live profitability conclusion follows. Existing defaults were not optimized by this experiment.
