# Signals offline distance sweep, 2026-11-14

Offline only (`server/pulse/signal_replay.py`, `scripts/run_signal_research.py`; no venue access, no order). 8 liquid USDT perpetuals (public 1m history, 72 h), three consecutive 24 h windows
(12 h prehistory + 12 h trading each, windows W0 / W1 / W2, 24 h apart), sources ema-cross / macd-cross / rsi-reversal, short + medium range, timeframes 1 / 5 / 15 / 30, two Last-N lanes, both
directions, 12 protection profiles (config: `config.json` here; fees taker 0.05 % each leg + 0.01 % slippage per leg, `$25` per order, the preset's risk book). 9,216 independent contexts per window.
Retrospective diagnostic: the same data selects and reads, so none of this is an untouched holdout.

## Paper Base-stage PF by protection (every context's Base closes pooled; in-sample for the selection, n = closes)
| protection | kind | TP | SL | trail arm / gap | hold | W0 PF (n) | W1 PF (n) | W2 PF (n) |
|---|---|---|---|---|---|---|---|---|
| n-10-5 | normal | 1% | 0.5% | 0% / 0% | 60 m | 0.61 (4950) | 0.61 (4878) | 0.73 (5078) |
| n-3-3 | normal | 0.3% | 0.3% | 0% / 0% | 30 m | 0.38 (6966) | 0.39 (6938) | 0.42 (6850) |
| n-4-4 | normal | 0.4% | 0.4% | 0% / 0% | 30 m | 0.48 (6378) | 0.47 (6416) | 0.51 (6458) |
| n-6-4 | normal | 0.6% | 0.4% | 0% / 0% | 30 m | 0.53 (6172) | 0.51 (6246) | 0.59 (6304) |
| n-6-6 | normal | 0.6% | 0.6% | 0% / 0% | 60 m | 0.59 (4976) | 0.61 (4970) | 0.70 (5140) |
| o-4 | trailing-only | 0% | 0.4% | 0.3% / 0.2% | 30 m | 0.53 (6504) | 0.52 (6496) | 0.59 (6506) |
| o-5a | trailing-only | 0% | 0.5% | 0.4% / 0.2% | 30 m | 0.60 (6068) | 0.59 (6066) | 0.63 (6174) |
| o-5b | trailing-only | 0% | 0.5% | 0.5% / 0.3% | 60 m | 0.60 (5364) | 0.60 (5292) | 0.74 (5436) |
| o-8 | trailing-only | 0% | 0.8% | 0.6% / 0.3% | 60 m | 0.65 (4524) | 0.70 (4612) | 0.86 (4758) |
| t-10-5 | trailing | 1% | 0.5% | 0.5% / 0.3% | 60 m | 0.59 (5376) | 0.57 (5296) | 0.70 (5444) |
| t-6-4 | trailing | 0.6% | 0.4% | 0.3% / 0.2% | 30 m | 0.49 (6514) | 0.47 (6504) | 0.52 (6518) |
| t-8-5 | trailing | 0.8% | 0.5% | 0.4% / 0.2% | 30 m | 0.58 (6072) | 0.54 (6066) | 0.60 (6176) |

Reading: the ranking is the same in all three windows. Tight distances lose most (3 / 3 % PF about 0.4) and PF rises with wider stop and target; the best profile in every window is **o-8**
(trailing-only, stop 0.8 %, trail arm 0.6 %, gap 0.3 %, hold 60 min): 0.65 / 0.70 / 0.86. That is the direction the cost arithmetic predicts (a 0.10 % round trip is a large share of a 0.3 % target) and the one finding that
repeats; **no profile reaches PF 1**.

## Funded model book (the capital-limited book fed by the Real verdict)
| window | opened | settled | PF | net (return units) | max equity drawdown | hours |
|---|---|---|---|---|---|---|
| W0 | 30 | 28 | 0.36 | -0.04210 | 0.150% | {'flat': 3, 'negative': 7, 'positive': 2} |
| W1 | 12 | 12 | 0.56 | -0.01863 | 0.099% | {'flat': 9, 'negative': 2, 'positive': 1} |
| W2 | 7 | 5 | 0.98 | -0.00025 | 0.035% | {'flat': 9, 'negative': 3} |

Chain-qualified contexts (Base, Main and Real all passing at PF 2.2): 0 in every window; 3 Base-stage qualified. Drawdown stays tiny (0.04 - 0.15 %) only because the Real gate lets almost nothing through
(5 - 28 orders a window); every window is net negative and no window has every hour positive. The Signals lane is **not wired into live execution** (by design of the module, see its header) and nothing here
supports wiring it: the funded PF is 0.36 / 0.56 / 0.98 on 28 / 12 / 5 orders. Candidate to test next (not done): trailing-only with stops of 0.8 % and wider, in a window the selection did not see.
