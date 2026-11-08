# Signals dynamic-symbol research update — 8 November 2026

`SignalProgress` is an offline research engine. It is not connected to Pulse's
live entry loop and does not submit exchange orders. This update does not
change the live symbol selection, live configuration, or order handling.

Previously, registration stopped as soon as any symbol's progression started.
A changing research universe therefore required rebuilding the book, risking
loss of existing per-lane history or an incomplete history for the new
symbol. `register_new_symbol` now installs one complete, nonempty batch of
profiles for a never-registered and never-observed symbol. The batch is
validated before it is installed. It cannot add profiles later to a symbol
whose earlier events they would have missed. Each new root starts with its own
empty histories; existing roots are untouched. The symbol registry is rebuilt
from roots when restoring an existing checkpoint.

`prefill_new_symbol` consumes up to 4,320 contiguous completed one-minute
bars and their caller-supplied causal events. It requires the last bar to end
at the supplied next decision minute and validates the entire batch before
progression. Historical candidates are counted and discarded rather than
returned for execution. Shadow progression and open simulated positions remain
in the research state so the next minute follows the same history as an
independent replay. A caller must provide complete profiles and causal source
events, control memory and universe size, and ensure any future execution
adapter distinguishes simulated state from venue positions.

Tests compare new-symbol history and candidate IDs with independently replayed
books, verify existing lanes remain unchanged, exercise checkpoint restoration,
and reject duplicate profiles, previously observed symbols, gaps, invalid
events, an incorrect cutoff, an invalid late bar, and oversized prehistory.
These checks establish the tested behavior, not live integration or profit.

The latest separate 24-hour Signals research run produced two modeled settled
trades, both losses. The 200-symbol parent replay used for the accompanying
audit does not include this Signals engine or Block/DCA add-on legs. A live
source feed, lane-to-position ownership and netting, order reconciliation,
additional-strategy integration, and forward performance remain unverified.
