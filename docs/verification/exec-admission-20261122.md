# Exec admission and settlement

`StageHistory` evaluates disjoint Long, Short and Last blocks on the closed
orders of each parent Set. A derived Set contains only its parent's orders
that qualified at entry time. An order needs a complete window and can use
only settlements strictly earlier than its decision. The configured
positive and negative Short and Last windows are evaluated independently.
Real and Exec include positive Short windows through 30. Live takes the
selected Exec path.

The live book and the replay use configuration order as the final rank key
when profit-factor evidence ties. The replay applies the live rules for
enabled Set types and uses the Exec kind order even if a saved legacy
setting prefers the Negative lane. A Set with insufficient samples remains
ineligible. Per-Set Auto PF refits when a late symbol supplies an earlier
training close. Each Last-N block learns from its own parent Set, sample
count, block offset and sign. With the default sample bounds a fit reads up to
the latest 256 candidate outcomes in the prior 48 hours and requires support
in both chronological halves. A window with too few outcomes for training
uses its fallback threshold. Positive blocks compare gain to loss. Negative
blocks compare loss to gain and retain their own evaluations. The hourly
boundary excludes the close at that exact hour.
The complete disjoint window also has to meet its own positive PF threshold.
This test runs both at the live decision and at the historical entry used
to place a close in the next related Set.

A completed live position rests its own symbol and direction. The observed
net return determines whether the winning-trade pause applies. The engine
keeps its symbol-wide liquidity and rapid stop-out limits. Venue checks use
the stated direction for hedge positions and the quantity sign for one-way
positions. A second position snapshot cannot drop a still-held hedge leg
merely because its size is positive.

The Stage Grid reports post-stage paper PF alongside settled Exec results.
Exec position-return PF uses the equal-weighted net returns of the executed
positions. Exec cash PF uses their USDT PnL after fees and includes signed
funding where the venue reported it. The stored fees and funding remain
separate. Active positions, filled orders and live control orders have
separate counts.

## Regression coverage

- `python3 scripts/test_stage_grid.py` compares the indexed grid with a causal
  reference and exercises stale training prefixes, deterministic ranking,
  settlement, funding, directional cooldown and the 200-symbol entry scan.
- `python3 scripts/test_indexed_stage_query.py` compares replay queries and
  accepted orders with the native engine.
- `python3 scripts/test_position_settlement.py` verifies exact ownership,
  partially filled exits and fee attribution.
- `python3 scripts/test_x02_native_session.py` exercises the VST order journal,
  positions and controls against offline exchange responses.

Profitability acceptance uses forward positions after trading costs. The
historical PF of a qualifying Last-N window records which orders passed the
window's rules. Its sample ends before the next entry and its PF measures
those past orders.
