# Forward-cohort ordering — 8 November 2026

The `trade_sim.py` next-8 and next-50 statistics are intended to describe the
next entries of each Base-validated Set. The stored Set tapes are ordered by
exit time. The old implementation skipped inherited positions but then took
the first N remaining closes in that exit order. When symbols overlapped, a
later entry that closed quickly could displace an earlier entry from the
cohort. This affects the displayed shadow forward PF and counts; it does not
alter the walk-forward entry decisions, closed-parent ledger, or its PF.

The simulator now builds a lazy, compact entry-time index for each Set. Ties
across symbols use stable symbol order, without sorting by a future close or
return. The original shadow tapes remain exit-ordered for last-N stage
evaluation. Only already-completed shadow trades with entries at or after the
validation are included; entry cohorts can overlap between hourly samples and
can contain fewer than N trades near the end of the available data.

Regression checks cover inherited positions, entry/exit order inversions,
simultaneous entries, fees deducted once, insufficient available trades,
and an independently sorted randomized example. These statistics remain
retrospective shadow measurements; they are not account returns or proof of
future profitability. The simulation does not place orders.
