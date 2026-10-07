# 24h account simulations, 2026-10-07

**Setup**
- 12 desk symbols; the BingX 1m tape was fetched fresh.
- Each run is 24h after 24h of engine pre-history.
- Engine: `scripts/sim_12h_account.py`, with the desk overlay now taking precedence over the profile (`8671da2`).
- Start equity 10 USDT. Max leverage, minimum lots.

## Account result per 24h

| Day | Fee (round trip) | Deployed gates | All signals, no gate |
|---|---|---|---|
| Oct 5 20:00 → Oct 6 20:00 | 0.18% | 0 orders | −7.83 USDT, 1,577 orders, PF 0.29 |
| | 0.06% | 0 orders | −4.49, PF 0.61 |
| | 0.04% | 0 orders | −3.56, PF 0.70 |
| Oct 6 19:00 → Oct 7 19:00 | 0.18% | −3.34, 198 orders, 1/24 hours positive | −6.99, 1,565 orders, PF 0.46 |
| | 0.06% | −2.79 | −1.00, 2,396 orders, PF 0.92 |
| | 0.04% | −2.61 | +0.29, 2,615 orders, PF 1.02 |

## Trade level: is the selection predictive?
`scripts/selection_audit.py` uses 300,000 sampled Set trades per day. Each trade's prior evidence is the same Set × side's closes before entry, pooled or per symbol, last 10/30/50/100. The trades are split into ten equal buckets by that evidence, and the table shows each bucket's average net on the next trade:

| Day | Gross per trade | Net per trade at 0.18% | Exit mix | Deployed gate's trades |
|---|---:|---:|---|---|
| Oct 5–6 | +0.011% | −0.169% | 95% time stop, 1% TP | none admitted |
| Oct 6–7 | +0.005% | −0.175% | 92% time stop, 2% TP | −0.370% (worse than all) |

- On both days, every bucket of every key is negative.
- The ordering of the buckets flips between the two days.
- So no Set-level evidence rule selects better trades.

## Conclusions
1. **The 1m signals carry no measurable edge** over a 30-minute hold: gross is +0.005% to +0.011% per trade. Lower fees reduce the loss but do not change its sign on both days.
2. **The PF stage gate selects noise** and, on the latest day, worse than random. A "better" selection rule could not be found.
3. **What was done:**
   - Live x01 trades the 1m lanes at probe size (`oneMinuteLanes: probe`, `9b947af`).
   - VST x02 stays on, so live evidence keeps accumulating.
   - The 1h lane is separate and unchanged.
4. **Maker execution** (post-only entries, limit TP) cuts cost to about 0.04–0.07%, but that is still several times the measured gross edge. It is worth building only together with a signal whose gross clears about 0.07%.
