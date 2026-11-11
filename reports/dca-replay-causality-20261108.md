# DCA research replay causality — 8 November 2026

The offline `stack_eval.py` DCA comparisons previously processed entire
parents in entry order. A later-entered parent could have an add close before
an earlier-entered parent's add was considered, but that close was missing
from the latter's deactivation evidence. Conversely, evidence retained from
other parents was ordered by their entry rather than by actual close time.
Equal-clock closes were also available to a decision at that same clock.

The one-rung and multi-rung studies now schedule candidate adds in hit-time
order. Only already-admitted adds whose parent closed strictly before the next
candidate enter the DCA last-N evidence, sorted by close time. Simultaneous
candidate ordering is deterministic and does not use future returns. A
multi-rung parent stopped by the self-off rule cannot resume a later rung.
The sign of the last-25 average is checked directly, including when the
configured cost is zero.

An indication-gated multi-rung add also uses the completed candle before its
simulated intrabar hit, rather than reading the hit candle's close before
filling inside it. Regression tests cover out-of-order parent entries and
closes, the last-15 ordering, equal-clock evidence, parent stop behavior and
the prior-candle rule.

This changes only the offline comparison script. It does not enable DCA,
change live trading, or establish profitable operation. One-minute OHLCV does
not reveal the exact path inside a candle; simulated add prices, fills, fees,
and the parent exit must not be treated as confirmed exchange results.
