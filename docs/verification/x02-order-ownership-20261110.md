# x02 order ownership follow-up — 10 November 2026

The previous control recovery path could treat an unrelated same-symbol stop as
the engine's own after a venue "already exists" response. It now backs off
unless the exact venue order ID is recorded on the relevant open position.
Cancellation also requires that exact recorded ID and the matching symbol; a
matching client-ID prefix by itself is insufficient. A replaced stop retains
its recorded old ID until its cancellation is dispatched. If an order cannot
be attributed, it is left in place and protection is not claimed.

The bounded x02 session journal rejects account-setting mutations, bulk or
unknown cancellations, previously submitted client IDs and aggregate-closing
orders. It binds a venue ID to a journaled client identity from its own POST
response or a matching order query. Session-only leverage configuration is
frozen and sizing estimates use conservative 1x margin. Its state and journal
stay separate from existing installations.

An authenticated, read-only x02 preflight observed zero positions and zero
open orders at the time of the check. No CTS-G order was submitted by this
follow-up. Earlier CTS-C diagnostic trades belong to a different project and
are not CTS-G acceptance evidence.

Native CTS-G overall protective orders still use `closePosition`, and the
exchange aggregates same-symbol/same-direction positions. Another process can
join that aggregate after an empty preflight; such a control could close more
than this session owns. The native reconciliation path also needs fill-level
attribution before accepting aggregate position growth. The bounded session
therefore refuses execution before connecting until quantity-bounded protection
and ownership attribution have been independently verified. Existing live
installations are not changed or restarted by this follow-up. No venue
strategy PF, successful 30-minute acceptance, or profitability is claimed.
