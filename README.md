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
| `deploy/install-linux.sh` | First-time Linux install (packages, systemd, Redis, desk + engines) |
| `deploy/update-linux.sh` | In-place Linux update (keeps overlays and open positions) |
| `deploy/grok-pulse@.service` | systemd unit for a connection slot |
| `scripts/` | test / smoke / PWA / preview helpers |

Exchange API keys stay in Redis (`api_key` / `api_secret` per connection). They are never stored in this repo.

## Linux install

On the VPS (`/opt/cts-g`, desk **:3102**). Unattended — no prompts. `--port` and `--name` are optional. Packages are installed only if missing.

```bash
sudo /opt/cts-g/deploy/install-linux.sh
sudo /opt/cts-g/deploy/install-linux.sh --port 3102 --name cts-g
```

```bash
git clone https://github.com/mxssnx-creator/CTS-G.git /opt/cts-g
sudo /opt/cts-g/deploy/install-linux.sh --from-dir /opt/cts-g
```

From this tree, if SSH works:

```bash
./deploy/remote-install.sh --host 152.53.114.112 --user root
```

Installs Node 22, Python 3, Redis, pulse engine at `/opt/grok-x01-pulse`, desk at `/opt/cts-g`. Git origin is `https://github.com/mxssnx-creator/CTS-G.git` (`xssnet <mxssnx@gmail.com>`).

| Unit | Role |
|---|---|
| `grok-pulse-http` | Stats/control sidecar on :3015 |
| `grok-desk` | Desk UI on :3102 |
| `grok-pulse@bingx-x02` | VST engine |
| `grok-pulse@bingx-x01` | Live engine (starts when Redis keys exist, or pass `--start-live`) |

```bash
sudo /opt/cts-g/deploy/update-linux.sh          # git pull, restart, keep overlays + opens
sudo /opt/cts-g/deploy/update-linux.sh --force  # match origin/main exactly
```

Keys stay in Redis, never in git:

```bash
redis-cli HSET connection:bingx-x01 api_key '…' api_secret '…'
redis-cli HSET connection:bingx-x02 api_key '…' api_secret '…'
sudo systemctl restart grok-pulse@bingx-x01
```

`update-linux.sh` does not flatten exchange positions.

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
python3 scripts/engine-test.py   # sets, indications, rank, overlays, SL:TP
node scripts/overall-test.mjs    # desk / results / system / settings
npm run typecheck
npm test
```
