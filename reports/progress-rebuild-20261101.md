# Independent progress and Signals sensitivity — 1 November 2026

Research only. No exchange was contacted, no order was sent, and x01/x02
account state and protective orders were not verified. Historical qualification,
subsequent modeled execution and venue-owned fills are different cohorts. PF is
gross positive divided by absolute gross negative unit returns; one is neutral.

## CTS-G history snapshot

The opt-in `histKeepLastComplete` prototype retains the last fresh, complete,
same-universe Set score while expanded history is assembled. It fails closed
when source data, topology, freshness, or the symbol universe no longer supports
that snapshot. It does not relax PF, coordination or execution-loss gates, and
it remains **off by default**. The engine and restore/install pin are updated
together. A complete offline regression initially found one minimal-book
compatibility failure; after its fix, the **484-check full rerun passed**.
Four separate admission-oracle tests and two Signal study-preparation tests
also passed. None is a live performance or profitability test.

## Follow-up: first candidate batch and service continuity

The ordinary entry scanner formerly computed at most 16 previously unseen
symbol signals per cycle. With 30 ready symbols, later signals could not
compete for the first opening. Its bounded batch is now 30; larger universes
still continue on later cycles. A synthetic independent fixture confirms that
all 30 ready symbols are included and a 31st is deferred to the next cycle.
An entry-loop fixture also confirms that the strongest same-direction ready
signal at symbol 30 receives the first available slot in that controlled case.
The existing signal confidence ranking, directional preference, availability,
Set qualification, scope checks and other risk rules remain unchanged. This
does not establish that the first eligible order will be profitable.

The engine's systemd `StartLimitIntervalSec` was misplaced in `[Service]` and
was ignored. Engine, HTTP and desk units now declare a bounded retry window in
`[Unit]` and a 15-second restart delay, so repeated automatic retries alone
cannot exhaust the configured 20-start/120-second limit. This is not proof of
a running or recoverable remote installation. An explicit installer/updater
`--no-vst` disables x02 auto-start and stops its manager when services are
started. It does not close positions or cancel orders; protection must be
verified before stopping. Later updates preserve an already-disabled x02.
Mocked systemd checks exercise these paths without modifying real services.
Existing-checkout installs now use the same non-forcing, fast-forward-only Git
update as the updater instead of an unconditional checkout/reset. A fake-SSH
integration test covers spaced paths, flag forwarding and preservation of
dirty and diverged work; no remote server was contacted.

An independent reconstruction of the previously studied ordinary CTS-G tapes
reproduced all 25 selected Base Set/checkpoints and all 1,116,351 shadow
closes, with zero same-Set/symbol overlaps. The archived eight-symbol study
used 72h prehistory and 12h trading; it is retrospective and is not a Signals
study or an untouched holdout. Next-eight shadow returns after selection were
200 overlapping observations at PF **0.253050**, assuming 0.12% round-trip
cost. Removing that assumed cost from the same fixed returns gives gross PF
approximately **0.676781**; it does not reselect entries or holds.

The new `scripts/audit_forward_admission.py` additionally reapplies causal
Base/Main/Real shadow-stage decisions at each subsequent entry, instead of
assuming that the checkpoint verdict remains true. Nine of the 25 selected
checkpoints had eight subsequent Base-passing shadow candidates: 72 returns,
PF **0.382644**. Across all 25 checkpoints, up to eight available candidates
per checkpoint gave 109 returns, PF **0.233527**. These overlapping cohorts
have different sizes. No Main/Real candidates were admitted in this selected
shadow study; execution PF is unavailable. The candidates are not funded
orders: an unqualified shadow position can suppress another opportunity.
Twenty-four deterministic plain-oracle cases and future-leakage tests passed.

## Actual CTS-A-O Signals: frozen four-arm diagnostic

Source checkpoint `2599b65`, economic source SHA-256
`627ce026ab9f0b7398ddecdba7905bafb21cac158db90ff065c7999aebdd2c6f`, feed
SHA-256 `bd4b66e09c65b990082ccc56dfd2624fd18815d01767a315dc002dfa0f6f02b0`.
Eight symbols, 8 October 12:00–9 October 00:00 UTC prehistory and 9 October
00:00–12:00 UTC trading, with modeled 0.15% round-trip cost. This is a reused
market window, not a forward holdout. The original-stage adapter remains off.
All four configurations were frozen and normalized before any arm was scored.

| Arm | Signal Last-N | Signal minimum trail | Signal TP values | Signal tapes | Active Signal units | Signal settled orders / PF |
| --- | ---: | ---: | --- | ---: | ---: | --- |
| Control | 12 | 0.20% | 1.0% | 504 | 0 | 0 / unavailable |
| Last-8 | 8 | 0.20% | 1.0% | 504 | 0 | 0 / unavailable |
| Distance | 12 | 0.30% | 1.0%, 1.5% | 630 | 0 | 0 / unavailable |
| Last-8 + distance | 8 | 0.30% | 1.0%, 1.5% | 630 | 0 | 0 / unavailable |

Each arm had 0/42 Signal representative pairs passing the diagnostic gate; this
is **not** the exact-context post-Base validation percentage, which is
unavailable. All 42 pairs lacked the 30 historical entries needed for the
configured MFE fitting. Every run returned **exit 1**, with 74/76 checks:
requested Base catalog and indication-kind coverage were incomplete. The 12h
input did not provide the 120 completed 15m/30m bars required by those paths,
and Low-Range was not evaluated. No arm proves Signal profitability or a
profitable parameter choice. An initial wrapper attempt failed before starting
because `/usr/bin/time` was absent; the four recorded engine runs proceeded
without that wrapper. Timing is not a controlled comparison.

The ordinary funded **model** was identical across all four arms: 132/34,374
Base pairs passed (0.3840%); at the trading start, 8/251 post-Base engine
configs passed (3.1873%; four finite historical PFs have mean 33.181, and
four no-loss sets are separate). There were 47/42/5 positions and 354/336/18
orders opened/settled/held. The 336 settled modeled orders had return PF
**3.696049**, after costs. An independent sum of unit returns reproduced gross
gain 1.5518340683 and loss 0.4198629735; the separately sized cash-PF
diagnostic was 5.062258. Exact-cohort comparisons found all 336 identities and
unit returns unchanged in each arm. This ordinary cohort is not a Signals
result, exchange-fill result, complete-coverage run, or stable forward proof.

An older, different CTS-A-O Signals model window (17 October 09:00–21:00 UTC,
12 symbols, 150h fitting) ended with **11,416/3,030,696** exact Signal contexts
passing (0.3767%; endpoint qualified-history PF 4.6037). Its separately
settled Signal cohort had **271** modeled closes at PF **0.0480**, with
852/271/581 orders opened/settled/held and 1/0/1 positions opened/closed/held.
These endpoint and execution samples must not be pooled. The archived
half-gap arm produced account PF 8.3815 in the earlier window and 0.0525 in
the later window; it was not promoted.

## Remaining release blockers

- The CTS-A-O controlled ordinary-path reproducer still has one genuinely
  failing expected TODO: raw candidate wins can qualify a next order despite
  the exact processed predecessor's Last-12 being losses. A focused run gave
  62 passes and that one expected failure. The persistent stage adapter, inverse
  strategy and durable ownership integration remain unfinished.
- CTS-G does not yet integrate the actual CTS-A-O Signal-source/range identities.
  The prototype snapshot is not enabled in the x01 overlay and has not been
  evaluated against a live venue or sustained real-time load.
- Current x01/exchange positions, protective orders, actual maker/taker fees,
  and x02 status are unknown. This environment has no permitted exchange route
  or configured remote host; its earlier local installation was absent.
  Restore an approved route, reconcile ownership and protection, and verify the
  actual system before a live start. No parameter was promoted from these
  retrospective tests.

The offline evidence archive has SHA-256
`1dd1c78e53b9003c162f5d5c101d0d780727f5569d67fdffab8b1f1de64979dc`.
