# Overall exchange controls — 2026-09-13

`controlOrdersOverall=true` shares quantity-matched protection by symbol and
hedge direction. `controlOrdersPerConfig=true` preserves independent internal
Set lots, target/trailing prices, evaluation evidence and realized results.
No closePosition order includes unrelated same-side exposure.

Each order is bound to its original client IDs and quantities. Cumulative
exchange quantities, prices and fees are allocated to those lots only.
Late fills cannot acquire newly opened members. Interrupted callbacks retain
per-member progress. Final cleanup survives an empty book and restart.
Unresolved exchange acknowledgements retain their client ID and are queried
before resubmission. This is not a claim of a transaction spanning the venue,
trade journal, open book and pending-intent files.

The official cancel/replace endpoint replaces existing protection without
requiring two spare order slots. A confirmed first leg is retained if the
second fails. Old per-Set orders are retired cooperatively. Venue deadlines,
order-size constraints, hedge ownership and protection remain enforced.

Batch JSON carries numeric quantity, price and stopPrice. Signed HTTP uses
the existing connection pool without rebuilding query parameters. Transport
timeouts do not trigger a second submission through another client.
`api.requestLatencyMs` reports up to 256 transport samples per endpoint,
including median, p95 and maximum; rate waits and fill confirmation latency
are separate. These metrics include unsuccessful HTTP requests.

No official Python SDK was found in the BingX-API organization. This change
uses the native API client and the official API reference, not an unofficial
package presented as an official SDK:

- [BingX swap reference, pinned](https://github.com/BingX-API/api-ai-skills/blob/5fb44d121b7e10ef3493bb4de21fedf7e5c98ac6/skills/swap-trade/api-reference.md)
- [HTTPX connection pooling](https://www.python-httpx.org/advanced/clients/)

Offline verification: `scripts/test_overall_controls.py` covers independent
lots/directions, late retired fills, cumulative partial fills, interrupted
callbacks, mode transitions, failed replacements, lost acknowledgements,
changed boundaries after restart, controls-off and final cleanup retry.
`scripts/test_vst_scheduling.py` checks numeric batch serialization, raw
signed URL preservation, no timeout replay and shared rate deadlines.

Run the synthetic 20-symbol, 500-position benchmark with:

```
python scripts/benchmark_overall_execution.py /tmp/offline-execution-speed.json
```

Its fake-exchange throughput is not live execution speed. Mainnet remains
stopped; runtime activation and latency verification target VST only.
The prior seven-day strategy report remains valid because this change does
not change historical strategy calculations or their input data.

Measured offline: 500 confirmed logical positions across 20 synthetic symbols,
20 shared protection pairs, 10.3835 seconds (48.15 positions/second), per-position
median 20.155 ms and p95 39.886 ms. Includes local book persistence and simulated
exchange acknowledgements; excludes network and venue rate waiting. An earlier
run with deep-copy book serialization took 86.9036 seconds. These sequential
measurements are diagnostic, not a controlled hardware or venue benchmark.

Verification before VST rollout: 394 Python regression tests passed; 12 focused
overall-control tests and 22 scheduling/transport tests passed. Typecheck and
production build passed. Desktop/mobile dev and production browser checks had
no console/page errors or horizontal overflow.
