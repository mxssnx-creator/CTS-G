# Signals and independent strategy evidence — 22 November 2026

This is offline research and a scoped accounting repair. It does not establish
profitable live execution. The experimental independent-window progression
and source export are not connected to the Pulse live entry loop. Its fixed PF
settings are not the ordinary engine's per-Set Auto PF implementation. The
new volume-momentum source is available as an explicit experimental Signal
pack; it is absent from the shipped defaults and was not used in the x02 run.

## Changes and accounting contract

- A Signals window can identify Short as positive or negative independently of
  the positive/negative Last window. Negative windows compare loss to gain at
  the configured PF floor; returns themselves are not inverted or removed. At
  Base and Main the full long-plus-Last window must also pass a positive floor.
- Optional Real positive/negative rules use the exact Real stream's own closes
  and have distinct candidate IDs. Negative Real rules additionally require a
  disjoint positive Real history and a positive aggregate Real window, each at
  the configured floor. When `realLongN` is set, the positive mode uses the
  same context and aggregate checks; the candidate uses eight prior closes.
  The older coordinated mode remains available
  and identifies its evidence as Main. The offline research presets now use
  their own Real history. The low-distance candidate enables both Short and Real
  signs. Its existing PF floor of 2.2 is unchanged.
- The research minimum is configurable above 1, allowing sensitivity tests;
  no lower threshold is promoted to a live preset. Equal-clock or future closes
  remain excluded. The volume-momentum source uses only completed bars and is
  exported by `scripts/export_signal_python_events.py` for offline testing.
- The companion parent-accounting change prevents Block rounds and Relate from
  learning an aggregate position result as the
  parent's result when an add exists but the parent's separate fees are unknown.
  That observation is unknown; it is not turned into an artificial win or loss.

## Chronological simulations

Three liquid Binance futures one-minute series, BTC/SOL/XRP, span 4–13 October
2026. The first evaluation uses 4–7 October as prehistory and 7–10 October for
trading; the later comparison uses 7–10 October as prehistory and 10–13 October
for trading. These tapes were used in earlier work: neither period is an
untouched holdout. All numbers are simulated, with OHLC stop-first assumptions,
not venue fills. Funding, margin liquidation and queue fills are not modeled.

The earlier independent-window experiment used twelve source families, 1/5/15-minute
events, both source ranges and directions, four normal/trailing Signals/Exit
profiles, independent positive and negative Short and Last conditions, and
positive and negative Real gates. The exploratory PF floor was 1.3; the fee
scenario used 0.05% taker per leg plus 0.01% slippage per leg. Each source ran
in its own $1,000 model book with $10 nominal per order and an eight-position
cap. Source portfolios must not be added as though they shared capital. It was
run before the subsequent Base/Main aggregate-window correction, so it does
not characterize the final evaluator; corrected tests are reported separately.

With eight prior Real closes plus the independent recent window required, all
twelve source books had zero opened positions in both trading periods; Exec PF
is therefore unavailable, not zero. Across the complete first 72+72 hour tape,
122 Real streams were allocated and the longest accumulated five closes; the
later tape allocated 38 Real streams, the longest with three closes. None had
the minimum nine closes. This is a sample scarcity finding, not evidence of
profitable execution. An earlier version that omitted the extra Real context
generated trades and is superseded by this test.

The updated low-distance candidate was also replayed with 100 hours of
prehistory and 44 hours of trading for VWAP, SAR and `r-clv-thrust`: none
opened a position. The candidate kept its PF 2.2 floor.

In a separate fixed-N research model, the exploratory volume-momentum source
used 4/4/4 and 12/8/4 Base/Main/Real close windows and a 1.3 floor. The fee
scenario was 0.05% per taker leg plus 0.005% slippage per leg. The table
includes open positions at the final mark, with modeled exit slippage and fees.

| Source / geometry | First: closed / open | First: closed PF / marked PF | Later: closed / open | Later: closed PF / marked PF |
| --- | ---: | ---: | ---: | ---: |
| Volume momentum, default | 90 / 0 | 0.666 / 0.666 | 60 / 4 | 1.157 / 1.212 |
| Volume momentum, low | 108 / 0 | 0.895 / 0.895 | 22 / 7 | 0.671 / 0.673 |
| EMA cross, default | 36 / 0 | 0.675 / 0.675 | 9 / 0 | 1.412 / 1.412 |
| OBV, low | 51 / 0 | 0.533 / 0.533 | 27 / 5 | 5.154 / 2.762 |

The high later closed-only OBV PF drops after the five open positions are
marked, and its earlier period lost. The earlier signed-window volume-momentum
replay with eight Real context closes opened no positions in either period.

### Re-test after the Base/Main aggregate fix

The same two 72+72 hour tapes were replayed again after adding the full
long-plus-Last positive PF check. VWAP and SAR were run at two Real context
lengths; the new source was run with two prior Real closes. The extra recent
Real window remains independent and is required in addition to the context.

| Source and Real context | First: closed / open | First closed PF | Later: closed / open | Later closed PF |
| --- | ---: | ---: | ---: | ---: |
| VWAP, 2 prior | 1 / 0 | no losses; not finite | 0 / 0 | unavailable |
| SAR, 2 prior | 0 / 0 | unavailable | 0 / 0 | unavailable |
| VWAP or SAR, 8 prior | 0 / 0 | unavailable | 0 / 0 | unavailable |
| Volume momentum, 2 prior | 2 / 0 | 0.000 | 0 / 0 | unavailable |

The one VWAP winner added 0.0490 model-book units; the two volume-momentum
losses totaled -0.1221. Each source is a separate model book. A single winner
or two losses is not enough to choose a production minimum. Neither the
source nor the shorter Real context is promoted into the shipped defaults.

An additional comparison used checksum-verified public BingX candles for the
same three symbols: 17–19 November as prehistory and 19–21 November for trading.
This is historical public market data, not x02 account execution. The same
fixed-N model and 0.11% cost scenario produced the following:

| Source / geometry | Closed / open | Closed / marked PF | Marked book change |
| --- | ---: | ---: | ---: |
| Volume momentum, default | 52 / 0 | 0.486 / 0.486 | -1.1076 |
| Volume momentum, low | 36 / 3 | 0.506 / 0.555 | -0.6191 |
| EMA cross, default | 41 / 7 | 2.999 / 3.052 | +1.5366 |
| EMA cross, low | 16 / 0 | 0.283 / 0.283 | -0.5208 |
| OBV, default | 16 / 1 | 0.462 / 0.521 | -0.3287 |
| OBV, low | 19 / 1 | 0.233 / 0.235 | -0.6077 |

EMA's wider profile passed this period but lost in the first Binance window.
None of these comparisons demonstrates persistent profitability.

Seventeen volume-momentum geometries were tested in the first period: nine
normal TP/SL pairs and eight trailing-only arm/gap pairs. Five were selected by
the lower of closed/marked PF, requiring at least 20 closes, for a later-period
comparison. A normal 1.2% TP / 0.8% SL case fell from 1.258 PF on 22 closes to
0.698 on 12; a 1.5% trail arm / 0.25% gap fell from 1.106 on 51 to 0.104 on 4.
None of the five had both at least 20 later closes and PF above 1. The later
period had already been inspected for other experiments, so this is not a
sealed confirmation. No geometry is promoted to live settings.

## Block count and sizing sensitivity

Three earlier sets of modeled execution parents (266, 417, and 1,038 closes)
were replayed with exact Exec lane, Set, direction and relation identity. The
36 preselected arms per window covered Set/symbol relations, counts 1–3,
ratios 0.2/0.5/1, and manual/Auto. This is unit-notional reweighting of the
original parents; it cannot model different venue fills, margin or market
impact. No arm achieved a PF above 1 in any of the three windows.

| Window | Baseline PF | Set N1 ratio 0.2 PF | Raised parents | Baseline / Block drawdown, return units |
| --- | ---: | ---: | ---: | ---: |
| 11–12 October | 0.906 | 0.915 | 34 | 0.333 / 0.327 |
| 17–18 October | 0.634 | 0.623 | 78 | 0.579 / 0.645 |
| 26–27 October | 0.659 | 0.673 | 243 | 1.465 / 1.531 |

Most exact Auto procedures did not reach their 15-observation minimum, so
manual fallback predominated. No profitability claim or default change follows.

## Bounded x02 observation

A separate 30-symbol BingX VST session used the existing EMA and OBV Signal
packs and the parent-return guard, with eight maximum open positions and a
30-entry submission cap. The experimental volume-momentum pack and the later
transport changes were not part of this session. It was stopped after 624
seconds because an entry write received a non-JSON response, leaving its exact
outcome unknown. The journal records 11 entry submissions. The engine recorded
10 closes: three natural and seven requested during session cleanup.

The three natural closes had recorded return PF 0.124 and net cash -0.0538
VST. The seven cleanup closes are excluded from that natural PF. These figures
come from native statistics; one natural close used an exchange-rate fee
estimate, and a full independent venue fill audit is not complete. Ten active
snapshots contained 50 position observations; each had verified controls and
none was flagged undercovered. This cannot rule out events between snapshots.

The final local position book was empty. The cleanup deadline expired with 26
order intents lacking terminal evidence, so the session did not claim confirmed
flat. A subsequent read showed zero open positions and zero open orders in the
selected 30-symbol scope. The one uncertain submission was absent from exact-ID
lookups and a later history read; absence is not a terminal receipt. Other
symbols and other sessions were outside this cleanup scope. The observed active
cycle work p95 was 499 ms, with a 7.65 s maximum; cycle spacing p95 was 634 ms.
This short run did not meet the requested one-hour verification or demonstrate
consistently high execution PF.

## Verification and interpretation

The corrected volume Signals archive was checked against an independent scalar
replay on 340 Base/Main paths across four roots; a VWAP catalogue audit checked
365 paths across four roots. These checks cover the sampled data, not all
possible paths.
The earlier signed 12-source experiments observed worst per-minute processing
steps of 427 ms and 323 ms under concurrent load (largest per-source p95
42.30/42.19 ms; worker peak RSS 70.6/68.0 MiB). This is not a live-loop
latency guarantee.

An hourly BTC study shows the importance of testing transaction costs and
walk-forward decisions, but its particular models and timeframe do not establish
an edge for these Signals ([Bysik and Ślepaczuk, 2026](https://arxiv.org/abs/2606.00060)).
Search intensity and look-ahead must be accounted for when selecting from many
strategies ([Gençay, 2026](https://arxiv.org/abs/2608.27734)). Dynamic trailing
and volatility regimes are hypotheses for testing, not proof that this short
window will be profitable ([Bui and Nguyen, 2026](https://arxiv.org/abs/2602.11708)).
The exchange's documented trade updates distinguish cumulative/last partial
fills and commissions ([BingX account-stream reference](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-ws-account/api-reference.md)).
