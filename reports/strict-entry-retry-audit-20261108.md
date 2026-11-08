# Strict entry budget and retry audit

The owner most recently requested 120 symbols and volume factor 1. These values
are recorded in `CLAUDE.md` for later sessions. They do not establish the
settings of an already running instance; the checked-in x01 overlay at the
time of this audit still has `symbolCap: 200` and `volumeFactor: 5`.

## Findings and changes

- With `strictSizeCap` enabled, the engine enforced its balance-relative
  ceiling while first sizing a parent, but a Set volume multiple or carried
  Block scale could subsequently increase the requested parent above that
  ceiling. The same optional ceiling now applies after each parent scaling
  path. Existing non-strict sizing keeps its prior behavior.
- When the exchange rejected an entry and reported a larger minimum quantity,
  the retry could exceed the strict parent budget or available margin without
  another book check. A retry now rechecks the venue minimum, budget, remaining
  capacity, margin and order-book depth before submitting the revised request.
  Malformed or nonfinite minimum quantities are not retried.
- A leverage-correction retry formerly used the original leverage to check and
  debit available margin. It now checks the revised margin and book before
  attempting the leverage change, and retries only after both leverage updates
  succeed. The debit uses the corrected leverage.
- Entry retries used a new client order ID for submission but retained the
  first attempt's ID for fill reconciliation and position tracking. The engine
  now follows the ID of the submitted retry.

`strictSizeCap` remains opt-in. It caps a requested **parent entry at the
reference price**; it does not cap the total book after later Block/DCA adds,
fees, adverse fill-price movement, or losses. Without the strict cap, the
venue's minimum lot may increase an order above the amount implied by the
volume factor. The current x01 overlay also has Block enabled and a separate
`general:exit` volume multiple. Neither a volume factor nor a historical PF
establishes future profit or a maximum account loss.

## Validation

The 122-check trader suite passed against isolated fake exchanges, including
new cases for strict and non-strict Set multiples, Block carry with and without
a Set, a changed venue minimum, low margin, a thin order book, invalid venue
messages, retry IDs, and lower leverage. The requested 120-symbol cap was also
checked with an open position that has to remain in the universe. Factor 1
was checked alongside the bounds and default handling. Rejected leverage
updates do not trigger an entry retry. No account credentials or real exchange
orders were used.
