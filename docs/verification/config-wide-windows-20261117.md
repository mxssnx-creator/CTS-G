# Config-wide last-N windows — finding from CTS-A-O (17 November 2026)

## In one sentence

Judge every configuration on **its own last N results across all symbols**, not on each symbol separately. In CTS-A-O this
lifted the executed PF from **0.56 to 1.33** on the same market window. CTS-G already works this way: keep it.

## What CTS-A-O measured

CTS-A-O had moved to windows per configuration × symbol × direction (each lane needed its own 9–40 closes). On a recorded
BingX feed (6 symbols, 12 h pre-history, 12 h run, every other setting equal) it compared that with config-wide windows:

| | per symbol × side | config-wide |
|---|---:|---:|
| funded orders | 322 | 466 |
| executed PF | 0.56 | **1.33** |
| balance ($10 start) | $7.95 | **$13.40** |
| General range | 1 order | 40 @ PF 2.79 |
| Long range | 6 @ 0.50 | 96 @ PF 1.82 |
| Minimal range | 287 @ 0.60 | 292 @ PF 1.10 |
| Micro range | 13 @ **1.43** | 14 @ 0.21 |

Why: long-hold configurations rarely collect enough results on one symbol, so per-symbol windows stopped them executing,
while the busiest (and worse) configurations filled their windows and took the book. Micro was the only range better per
symbol, so CTS-A-O judges Micro on both (its own symbol × side window and its configuration's record must pass).
Real-money promotion stays per symbol × side there (a winning long never promotes an unproven short).
Full record: CTS-A-O `docs/config-wide-windows.md`, `docs/positive-coordinations.md`.

## Where CTS-G stands (branch Stable-02-01, checked in the code)

- A Set (pack × SL ratio × TP step, or trailing pair, or Exit Set) has **one tape with the orders of every symbol**
  (`SetBook._tape_at` reads `st.tape()` in time order, all symbols); every last-N window, stage and Auto floor reads that
  tape. So CTS-G's windows are already config-wide across symbols.
- With `directionMode` split (the default) each Set exists once per direction: windows are config-wide per direction —
  the same as CTS-A-O's "config" scope with the sides apart.

## For next work

1. **Keep config-wide Set windows.** Do not cut a Set's last-N window per symbol (a per-symbol tape, a per-symbol
   Base/Main/Real window or a per-symbol Auto floor) without a causal comparison on the same data that beats it — in
   CTS-A-O that change cost more than half the executed PF.
2. **Micro (and other very short-target ranges) may profit from both views**: the Set's record AND the symbol's own recent
   results. If tried, build it as an option, measure on development and sealed windows, executed PF next to the selection PF.
3. **Live / real-money lanes keep their exact scope** (a Set's lane, symbol and direction for the live gates).
4. Report executed PF and orders per range and type next to every selection PF (rule (e) of 2026-11-14 still holds:
   a higher gate is not a higher executed PF).
