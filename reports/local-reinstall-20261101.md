# Local reinstall checkpoint — 1 November 2026

Checked at 15:44 CET (Europe/Vienna). This is a partial, offline installation,
not a successful x01 startup or a verified persistent trading service.

## Installed

- App files were installed at `/opt/cts-g` from the staged, tested research tree
  `eb6511a007bdaf48c2154cfc94fe2083775fff28`, corresponding to research commit
  `ea0489bd0b78605ad3412b638175bc11209ab181`. Git worktree metadata was not copied.
- The existing deployment helper installed the pulse tree at
  `/opt/grok-x01-pulse`. Both destination directories were absent before this
  attempt; no previous account state or credential file was overwritten.
- Trader Git blob: `d0049a8bf9de29f49e1eb9e03baf019e5674b60c`, matching the
  installer pin. App and runtime pulse files compared byte-for-byte equal.
- All 27 Python modules compiled. Installed-code checks passed for the deploy
  pin, connection guard (17 internal cases), x01's 30-symbol bootstrap, and
  18 execution-accounting cases. These checks use offline fixtures, not a venue.
- The shipped x01 overlay has `symbolCap=30`, `volumeFactor=0.05`, and
  `setUseHistoricGate=true`. No gate or account-risk protection was disabled.

## Not installed or started

- Desktop dependencies: `npm ci --offline --no-audit --no-fund` failed with
  `ENOTCACHED` for `zustand-5.0.15.tgz`. No `node_modules` directory remained.
- Redis binaries are unavailable and systemd is not running here. The engine's
  private-file credential fallback does not itself require Redis, but that does
  not replace a fully installed, persistent stack or working venue access.
- A standard, unauthenticated BingX HTTPS probe failed through the configured
  connection with `Proxy CONNECT aborted due to timeout` / HTTP `000`.
- No credential was read, copied into the install, printed, or committed.
- No x01 trading process, sidecar healer, or x02 process was started. No exchange
  order, leverage, position, protection, or remote service was changed. Current
  account state and whether another x01 instance is active remain unknown.

The local files can disappear if this execution environment is replaced. The
repository checkpoint is durable; the local installation is not certified as
persistent. The separate 490-check research regression is documented in
`reports/progress-rebuild-20261101.md`; it is not a live-readiness proof.

## Safe resumption

Use an approved persistent Linux runtime with working BingX REST/WebSocket and
dependency access. Install dependencies and a supported supervisor. Before any
live startup, back up actual state, determine whether another x01 instance is
active, and reconcile authenticated account ownership, pending orders, positions,
and protective controls. Use the existing private credential file only through
the supported credential mechanism. Do not substitute old stats, offline
fixtures, empty local files, or a merely alive process for exchange health.

Latest model results and their post-Base denominators are unchanged in the
progress report. Live PF, live order counts, and present Signal readiness are
unavailable; no profitability claim follows from this installation.
