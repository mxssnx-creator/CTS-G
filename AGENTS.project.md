# CTS-G project preferences

- User instruction, 2026-09-12: use **20 symbols**, rather than the entire
  exchange universe, for CTS-G VST tests and future test runs. Include
  BCH-USDT, SOL-USDT and XRP-USDT. Record the selected symbols with results.
- This is a test-universe size. Config/Set and logical position counts remain
  unlimited; keep the shared strict PF > 1.02 gate, two-day lookback and
  last-30 Base evaluation default. Keep managing existing positions even if
  their symbol leaves the test selection.
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
