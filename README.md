# CTS-G

Independent BingX pulse desks — **Live mainnet (X01)** and **VST demo (X02)** in parallel — with a full indication / Set / coordination stack and a desk UI.

This is the current CTS-G product. Previous CTS trees (keys, SSH dumps, old settings JSON) are not part of this repo.

## What it does

- **Overall / Live / VST** — three views, no identity bleed. Live and VST keep their own overlay, universe, tape, and control orders.
- **Indications** — state, direction, move, active, common, signals; independent 1m / 5m / 15m lanes plus combined eval.
- **Sets** — pack × SL:TP × trail × step product, historic replay, CID tracking (`Gx01` / `Gx02`).
- **Coordination stages** — intern / main / real / last / prev as advisory gates.
- **Block, DCA, trailing, exits, rearrange** — each pack independently enabled.
- **Universe rank** — max exchange leverage first, then Volatility 1H (or 24h / quote volume / abs change). Dynamic book keeps the top names that still fit margin.
- **Controls** — SL + TP (and security SL/TP) on every ours position. Foreign / leftover / other-desk positions are never flattened.

## Layout

| Path | Role |
|---|---|
| `src/` | Desk UI (desk, results, system, settings) |
| `server/pulse/` | Engine: trader, indications, sets, coord, block, DCA, exits |
| `server/pulse/overlay-bingx-x01.json` | Live overlay (capped liquid book, max leverage) |
| `server/pulse/overlay-bingx-x02.json` | VST overlay (full USDT-M universe) |
| `deploy/grok-pulse@.service` | systemd unit for a connection slot |
| `scripts/` | zest / smoke / PWA / preview helpers |

Exchange API keys stay in Redis (`api_key` / `api_secret` per connection). They are never stored in this repo.

## Desk

```bash
npm install
npm run dev
```

Pages: Desk · Results · System · Settings. Connection switch is Overall / Live / VST. Overlay save writes only the selected connection.

## Engine

Keys and connection id come from Redis (`connection:x01` / `connection:x02`). Run `pulse_trader.py` under the systemd unit, one process per slot. VST (`x02`) and Live (`x01`) must not share overlays or flatten each other.

## Tests

```bash
python3 scripts/engine-zest.py   # sets, indications, rank, overlays, SL:TP
node scripts/overall-zest.mjs    # desk / results / system / settings
npm run typecheck
npm test
```
