# CTS-G project preferences

- User instruction, 2026-09-15: defaults are **50 ranked symbols** and **100 effective positions** (unique symbol × LONG/SHORT groups). Independent intern, Block, DCA and control orders on an occupied group are unlimited (hundreds). Control orders stay overall: one SL/TP pair per symbol and direction using the widest member range. Direct user instruction of 50 symbols overrides the earlier 20-symbol test-universe note below.
- User instruction, 2026-09-12: earlier VST test-universe size of 20 symbols (include BCH-USDT, SOL-USDT and XRP-USDT) remains a recorded benchmark, not the live default. Config/Set counts remain unlimited; keep the shared strict PF > 1.02 gate, two-day lookback and last-30 Base evaluation default. Keep managing existing positions even if their symbol leaves the test selection.
- The completed exhaustive seven-day BCH/SOL/XRP report is a separate,
  reproducible benchmark. Reuse its evidence when its calculation sources
  and inputs are unchanged.
- The 1.02-PF window sweep did not support a better general default on the
  later control period. Retain Base Last-N 30 and deactivation Last-N 25;
  do not use the exploratory whole-period winner as an optimized default.
- User instruction, 2026-09-12: process work in batches and respect venue
  rate limits, including complete retry deadlines shared by batch and single
  order submission. Keep unfinished eligible lanes available for subsequent
  processing; count confirmed openings and never duplicate accepted orders.
- User instruction, 2026-09-12: Mainnet and VST use the same processing
  structures and shared profile. Keep endpoints, credentials, ownership and
  position/fill/pending state separate. The stopped Mainnet preparation must
  preserve STOP and must not start, enable or restart the trading service.
- User instruction, 2026-09-12: evaluate each strategy/configuration Set from
  its own evidence. Only Base-qualified Sets enter downstream calculations
  and system result statistics. Continue Base checks when evidence changes;
  retain rejected Sets and actual exchange accounting for ongoing tracking.
- User instruction, 2026-09-13: additional chronological control/holdout
  admission defaults to `controlMinTrades: 0` (off). Keep actual control
  results visible. Zero must not disable per-Set Base/Main/Real evaluation,
  cost accounting, TP/SL protection or venue rate limits.

- User instruction, 2026-09-13: additional fixed training minimum is zero.
  Baseline admission uses each Set's configured Base Last-N window, with
  actual available close counts reported explicitly; an empty tape is not
  positive evidence. Preserve venue rate limits and protective orders.

- User instruction, 2026-09-13: enable shared overall exchange protection by
  symbol and direction, while retaining independent internal Set lots and
  confirmed partial-fill accounting. `controlOrdersOverall` selects shared
  protection at the widest member SL/TP; `controlOrdersPerConfig` remains
  true for internal lot identity.
