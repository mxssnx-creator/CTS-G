# Acknowledgements and confirmed fills

An accepted or malformed order response without an executed quantity was
previously converted to the entire requested quantity. That could create a
local position and change results even when the exchange had not confirmed a
fill. An explicit zero or malformed execution quantity now stays zero. A
nonterminal or status-less acknowledgement and its requested quantity remain
pending; explicit partial executions or a terminal filled status are handled
as progress. Unknown identities are reconciled using the existing client ID.
No unconfirmed fill is added to the position book, and a pending fill triggers
an immediate polling opportunity. String and numeric zero response codes are
both recognized as success.

Unfilled Block and DCA adds, as well as accepted closes, now request prompt
fill polling too; an acknowledgement without a venue order ID keeps its original
client ID fenced for reconciliation. A Block level is not increased merely
because its request was accepted, and a pending add blocks a duplicate add.
Offline exchange fixtures that intend to simulate fills provide explicit
executed quantities rather than relying on the former inferred-fill behavior.

This change does not prove actual realized profit factor. Execution prices,
fees and exchange fill histories require separate confirmed evidence; existing
provisional price fallbacks still need an audit. No live account or running
service was changed by this fix.
