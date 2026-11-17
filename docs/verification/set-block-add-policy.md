# Set Block add counts

Set Block now qualifies each count within its Set, lane, direction and selected relations under the complete add policy. The policy includes the count steps and ratios, shared budget, relation selection and qualification settings. Those settings affect the earlier triggers and the budget available to later counts. The first observation captures the parent's policy and PositionCost. After an edit, that parent can close under its captured settings and the new policy starts its own evidence.

## Settings

In **Strategy → Block → Set Block add counts**, Add Auto Last-N defaults to **0**, which uses cumulative evidence. A value from **3 to 100** enables a bounded window per relation and count. Add Auto minimum observations defaults to **15**, capped at the selected window length. The cumulative minimum applies in cumulative mode.

Each enabled relation must qualify using its own count's evidence. A count qualifying on its recent price probes can collect a prospective shadow decision while held. Actual fills remain in the lifetime execution ledger. An unknown outcome occupies its place in a recent window and holds qualification until it ages out. A guard gets recovery credit only for shadow decisions made while that particular relation and count were held.

Carry and pause apply per count. A filled count can carry its own ratio after a loss. Recovery uses the parent's base result and that count's own cash contribution. The next parent uses the largest applicable relation scale. The engine's aggregate exposure limits still apply.

## State and verification

Version 4 retains earlier ledgers in the state as archived evidence. Earlier pooled deficits remain a hold when the deficit guard is enabled. Older executables cannot read version 4 and disable adds when the state file is unreadable. Preserve the pre-upgrade state file for an intentional rollback.

`scripts/test_set_block_scope.py` checks policy edits across restart, guard isolation, carry, unknown outcomes and native replay parity for cumulative and rolling qualification. These checks establish decision and accounting behavior. Add Auto remains opt-in pending profitable replay evidence across independent periods.
