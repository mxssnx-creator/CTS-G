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

2026-11-10 / 11: relation lanes (`relationLanes`: overall / indication / type / range / step lanes of the
post-Base orders, volume ratios for passing lanes) exist and ship **off**; the raise did not help on the two
development windows. Axis has no stable sign across four windows and is unchanged. The shipped Block lowered
the replay on four of four windows and is left on by the owner's choice. The statistics now show executed
parents with against without Block adds and relation raises. See
`docs/verification/relation-lanes-axis-block-20261110.md`. None of this is evidence from an account.
