# CTS-G project instructions

The latest priority is post-Base PF through execution and correct Block behavior.
Keep selected-window qualification separate from later settled results, including
all losses. See `docs/verification/block-postbase-20261109.md` for the Block
floor, partial-fill, fees, notional weighting, lane and fixed-target repairs.
The 52-case offline sensitivity did not establish a profitable Block preset.

The owner's latest requested x01 settings (2026-11-09) are **80 symbols**
(`symbolCap: 80`), the **lowest volume factor** (currently `volumeFactor: 0.05`),
and a **3 USD test account**, with Micro qualification required first. These
supersede the 2026-11-08 request for 120 symbols and factor 1. They record the
owner's preference, not an active account balance or a verified running instance.
Research tests must distinguish nominal sizing from exchange minimum lots;
minimum-volume settings alone do not cap notional after venue rounding.

The later request lowers Micro qualification while remaining positive: the
explicit Micro TP-range default is `minPfByRange.micro: 1.01`. It applies across
the stage windows when per-step qualification is enabled. This requested
threshold does not establish profitable subsequent executions.

Read `CLAUDE.md` for the project's execution, stage, accounting and history
contracts. Distinguish simulations and paper-stage figures from confirmed
exchange fills and actual executed profit factor.
