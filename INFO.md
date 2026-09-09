# CTS-G operations and continuity

This is the credential-free source of truth for operating CTS-G. It is written
so a new chat or operator can recover the workspace, connect to the VPS and
validate the deployment without relying on hidden conversation history.

## Canonical locations

| Purpose | Location |
|---|---|
| GitHub repository | `mxssnx-creator/CTS-G` |
| Persistent working checkout | `/workspace/CTS-G` |
| Installed VPS checkout | `/opt/cts-g` |
| Verified recovery checkpoints | `/workspace/backups/CTS-G/<UTC timestamp>-<label>` |
| Protected Chisel/SSH information | private `ssh-chisel.txt` plus the valid SSH identity; never Git |

`/opt/cts-g` is the running installation. `/workspace/CTS-G` is the canonical
maintenance checkout and must not be replaced by an unverified archive.

## Remote access: canonical solution

The VPS is `152.53.114.112`. Its Chisel listener is
`http://152.53.114.112:8090`. The verified tunnel is:

```text
local 127.0.0.1:2222 -> remote 127.0.0.1:22
```

The currently verified Chisel server SHA-256 fingerprint is:

```text
Q0MxL4WHKwM2JbRy6/6fAUee3600R7pPo1CKov8/EPc=
```

### Mandatory managed-workspace rule

Managed ChatGPT/Codex workers may be unable to open a raw socket to the public
IP even though the VPS ports are healthy. Chisel therefore **must use the same
HTTP/HTTPS network proxy already configured in the workspace**. Direct Chisel
repeatedly failed with `network is unreachable`; the proxied command and the
complete SSH round trip were revalidated on 2026-09-03.

The working architecture has four separate values/roles:

| Item | Role | Safe handling |
|---|---|---|
| Chisel server key (`ck-...`) | Verifies the server fingerprint | Never use it as an SSH login key |
| Chisel auth (`user:password`) | Authenticates the Chisel client | Read only from protected access info |
| SSH ED25519 identity | Authenticates `root` through the tunnel | `0600`, outside Git and logs |
| Pinned fingerprint | Detects a wrong Chisel server | Keep exact; never skip verification |

Load `CHISEL_AUTH` only from the protected access info. Never paste the value
into a command committed to Git, a workflow, a log, or this document. Prefer
the `AUTH` environment variable supported by Chisel so the credential is not
placed in the process argument list:

```bash
set -euo pipefail
test -n "${HTTPS_PROXY:-}"

PROTECTED_ACCESS_INFO=/secure/path/ssh-chisel.txt
CHISEL_AUTH="$(sed -n 's/.*--auth[[:space:]]\+\([^[:space:]]*\).*/\1/p' "$PROTECTED_ACCESS_INFO" | head -n 1)"
test -n "$CHISEL_AUTH"

AUTH="$CHISEL_AUTH" chisel client \
  --proxy "$HTTPS_PROXY" \
  --fingerprint 'Q0MxL4WHKwM2JbRy6/6fAUee3600R7pPo1CKov8/EPc=' \
  http://152.53.114.112:8090 \
  127.0.0.1:2222:127.0.0.1:22
```

If the protected file uses a plain `CHISEL_AUTH=...` entry instead, source it
only in a protected shell and keep the value out of command history and output.
The older `--auth "$CHISEL_AUTH"` form is equivalent, but less private because
the value can be visible in process arguments.

Keep the client running, then connect through the local endpoint:

```bash
chmod 600 /secure/path/snet-ln-deb01.txt
ssh \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  -i /secure/path/snet-ln-deb01.txt \
  -p 2222 root@127.0.0.1
```

In execution environments where background processes and subsequent commands
use isolated network namespaces, start Chisel and run SSH in the **same
execution session**, not merely in two visually adjacent terminals. A reliable
sequence is: start the client, wait until its local listener exists, run
`ssh-keyscan -p 2222 127.0.0.1` into a protected temporary `known_hosts` file,
then run SSH from that same session. On an ordinary host that can reach
`152.53.114.112:8090` directly, `--proxy "$HTTPS_PROXY"` may be omitted. It is
not optional in the managed workspace described above.

The mapping is always local port **2222** to remote port **22**.
`222` and `22222` are incorrect. Do not use a second Chisel client on the same
local port; stop the stale client or document a deliberate temporary port.

### Project-scoped service ownership

CTS-G renders systemd units from the deployment templates with the install
name as prefix: the default units are `cts-g-desk.service`,
`cts-g-pulse-http.service`, `cts-g-pulse@<slot>.service` and
`cts-g-pulse.target`. This prefix is required on a shared VPS. Never overwrite
or restart an unscoped `grok-desk`/`grok-pulse*` unit because it may belong to
another checkout such as CTS-GX; a port or service-name collision can create
the exact restart race that the scoped units prevent. The installer preserves
foreign units and only controls the units rendered for `CTS_G_NAME`.

### Independent server-key check

The attached `ck-...` value is an inline Chisel ECDSA private key. It is not a
Root SSH identity. When that protected server-key file is available, verify its
fingerprint locally without contacting the VPS:

```bash
chisel server --host 127.0.0.1 --port 0 \
  --keyfile /secure/path/chisel-server-key.txt 2>&1 \
  | sed -n '/Fingerprint/p'
```

The expected fingerprint is the pinned value above. Stop on any mismatch; do
not replace the pin based on a single untrusted route.

### Read-only validation

After the tunnel is established, use the separate SSH identity for a safe
round-trip check:

```bash
ssh -i /secure/path/snet-ln-deb01.txt -p 2222 root@127.0.0.1 \
  'set -eu; hostname; id -u; git -C /opt/cts-g rev-parse HEAD; \
   systemctl is-active redis-server cts-g-desk cts-g-pulse-http \
   cts-g-pulse@bingx-x02 cts-g-pulse@bingx-x01'
```

The 2026-09-03 revalidation confirmed the pinned fingerprint, the proxied
Chisel session, SSH authentication as `root`, and the SSH service behind the
forward. The previous deployment reached hostname
`v2202607384858486523`; its exact SHA and checkpoint are retained in the
private continuity record. Always compare the new GitHub-approved SHA with
the remote checkout before restarting services. Desk, pulse HTTP and Redis
were healthy; the two pulse instances were then intentionally stopped after
the remote resolver returned `Temporary failure in name resolution` for the
exchange endpoint, so no retry loop or order activity was created. The
read-only public overview recovered to `OPERATIONAL` and reported CTS-G `up`
with HTTP 200. The earlier verified post-deploy checkpoint was
`/workspace/backups/CTS-G/20260903T070101Z-post-deploy-signals-block`.

### Failure interpretation

| Observation | Meaning / action |
|---|---|
| Raw IP reports `network is unreachable`, but proxy is configured | Use `--proxy "$HTTPS_PROXY"`; do not change the forwarding ports. |
| Chisel reports fingerprint mismatch | Stop. Independently verify the new fingerprint from a trusted route before changing the pinned value. |
| Chisel returns unauthorized | The protected auth value is stale; do not remove authentication or print the value. |
| Tunnel listener exists but Chisel later says authentication failed | A local listener can appear before server authentication completes; wait for the authenticated connection log, then test SSH. |
| SSH says `Permission denied (publickey,password)` | The Chisel `ck-...` server key is the wrong key for SSH. Use the separate authorized ED25519 identity, its `0600` permission, and `IdentitiesOnly=yes`. |
| Tunnel connects but SSH handshake fails | Confirm the same-session rule, `2222:127.0.0.1:22` mapping, and that the remote SSH service is listening on `127.0.0.1:22`. |
| Local port 2222 is occupied | Stop the stale local client or deliberately select another local port and document that temporary deviation. |
| Remote `git fetch` hangs | Do not depend on VPS-to-GitHub egress. Transfer a locally verified Git bundle through the SSH tunnel and fetch from that local bundle after creating a remote checkpoint. |

### Remote update path when VPS GitHub egress is blocked

The VPS may have a healthy application network while outbound GitHub fetches
stall. After the read-only check and a verified backup, transfer the already
verified local bundle through the same tunnel:

```bash
scp -P 2222 -i /secure/path/snet-ln-deb01.txt \
  /secure/local/cts-g-main-<sha>.bundle root@127.0.0.1:/tmp/cts-g-main.bundle

ssh -i /secure/path/snet-ln-deb01.txt -p 2222 root@127.0.0.1 \
  'set -eu; git bundle verify /tmp/cts-g-main.bundle; \
   git -C /workspace/CTS-G fetch /tmp/cts-g-main.bundle \
   refs/remotes/github/main:refs/remotes/github/main'
```

Compare the fetched commit to the GitHub-approved SHA before deploying. Keep
the bundle outside Git and remove it through the approved cleanup procedure
after the post-deployment checkpoint; never copy credentials with it.

## Access priority

1. Chisel over the configured HTTP/HTTPS proxy is the primary recovery path.
2. Tailscale is the preferred secondary private-network path after its device,
   ACL and SSH state are verified.
3. NetBird is a fallback, not a second mesh client to enable blindly in
   parallel.

Tailscale and NetBird are already installed on the VPS. Do not reinstall or
re-enrol either client merely because direct public sockets are blocked in a
managed worker; that restriction is on the worker side and is solved by the
Chisel proxy rule above.

## Persistent workspace and backups

Before modifying or updating the VPS:

1. Ensure `/workspace/CTS-G` is a Git checkout of the canonical GitHub repo.
2. Fetch the target branch and record repository, branch, HEAD, upstream and
   worktree status.
3. Create an owner-only checkpoint under `/workspace/backups/CTS-G/`.
4. Include a complete Git bundle, a `SHA256SUMS` manifest, tracked diff and an
   untracked-file archive when applicable.
5. Run `git bundle verify` and `sha256sum -c SHA256SUMS` before calling the
   checkpoint valid.
6. Never put access credentials, private keys, Redis exchange secrets or raw
   production settings dumps into Git or a portable backup artifact.

A continuity note for the next chat must record the repository, branch, exact
HEAD, clean/synchronised status, checkpoint directory, bundle and manifest
hashes, validation results, deployment status, and any publication restriction.

## Change, push and merge gate

Use a feature branch and pull request. Before merge, require at minimum:

```bash
npm ci
npm run lint
npm run typecheck
npm test
python3 scripts/engine-test.py
npm run build
```

Also run the repository smoke/evaluation scripts and browser checks described
in `AGENTS.md`. Verify dev and production-rendered output, both connection
lanes, configuration persistence, calculation/statistics paths, Redis-backed
state and the Linux deployment. Merge only intended files; the branch and PR
must contain no bootstrap transport artifacts or credentials.

Historic validation accepts 2–336 hours (1m bars, up to fourteen days); the
three-day stress run is `hours=72` / `lookback=4320`, and a full fourteen-day
run is `hours=336` / `lookback=20160`. It processes every enabled pack, SL:TP ratio,
trailing arm/give pair, step, direction and indication kind, plus independent
Block and DCA tapes. Signals additionally publish a separate `block:signals`
historic ledger by symbol and direction; this is evaluation evidence for the
same-parent live Block add-on, not a standalone order. `validated #/#` means cost-adjusted PF >= 1.0 with the
required evaluation sample; `active #/#` is intentionally stricter and also
requires the configured enable PF and drawdown-time limit. Historic row counts
may exceed the catalog count because LONG, SHORT and BOTH are reported as
separate views.

## Processing and accounting contract

The live and historic paths use the same Set identity, indication, strategy,
volume and cost conventions. `volumeRatio` defaults to `1.0`; every configured
ratio, relation, step, SL:TP range and independent trailing pair remains a
separate Set. Order-level fills and logical-position quantities are calculated
independently, then aggregated only inside the owning symbol/direction/control
group. `executedQty` (including zero) is authoritative when the exchange sends
it; a requested quantity is only a fallback when no execution field exists.

Close order responses and exchange order history are cumulative. The local
book applies only the new cumulative delta, records each confirmed execution
leg once, keeps the remaining quantity and scoped controls after a partial
fill, and removes the logical position only after the final fill. A repeated
snapshot is idempotent. A rejected or no-fill control request remains visible
as a pending/recovery state and immediately re-establishes only that
position's protection pair. Fallback close forms reuse one client ID, so a
retry cannot create an untraceable duplicate close order.

Exchange reconciliation classifies ownership by the configured connection and
CTS client-ID namespace before adopting anything. Foreign orders and
positions remain diagnostic-only; they are excluded from CTS PnL, equity,
balance, PF, DDT and open-position counts and are never cancelled or merged.
The overview reports both `exchangeOwnOpenCount` and the diagnostic
`exchangeTotalOpenCount` so a discrepancy cannot be hidden in a single count.
Financial totals use authoritative `close` events; fill callbacks are
operational evidence and cannot double-count realized PnL or fees.

Historic replay performs network fetches outside the shared state lock. It
runs against a deep-copied SetBook and commits only when the SetBook pointer
and configuration generation still match. Live tapes, current bars and
configuration changes therefore cannot be overwritten by a stale replay.
Catalog-wide scoring and direction/strategy rollups happen once after all
symbol/chunk workers finish, preventing quadratic progress stalls. Drawdown
time is calculated per symbol and then aggregated; timestamps are normalized
between seconds and milliseconds and stale tails do not create cross-symbol
DDT episodes.

The Stats/Overview controls expose separate groups for catalog coverage,
indication kinds (including `signals` and `break`), directions, strategies,
Block, DCA, trailing, exits and per-symbol results. Cost PF, classic PF, net
PnL, wins/losses, fills, DDT and `valid #/#` / `active #/#` are sourced from
the same full snapshot. The Settings table is not a top-N substitute for the
catalog counters.

The reproducible offline VST evidence is
`reports/cts-g-72h-report.html`, generated with:

```bash
python3 scripts/generate-72h-report.py --symbols 12 --workers 4
```

The previous complete run covered 72 hours × 12 symbols, 13,520 catalog Sets
per symbol, 40,560 expanded Set/direction views and 39,163 validated rows.
After the full-range update, the same run is regenerated with 30 SL:TP ratios
and the resulting catalog dimensions are recorded in the HTML report. All
eight indication kinds, both directions, the full trailing product and
Block/DCA lanes are included. This is synthetic/offline evidence only and does
not claim live exchange profitability or submit orders.

The current full-range verification completed before publication with the
following reproducible runs (all synthetic/offline, no exchange orders):

| Run | Result |
|---|---|
| 72h × 1 symbol, one worker | `31,200` catalog Sets, `68,094` rows, `4,718` validated, `504.1s`, ready |
| 72h × 4 symbols, four workers | `31,200` catalog Sets per symbol, `68,848` aggregate rows, `65,590` validated, `826.3s`, ready |
| 4h active matrix, extra coordination on/off | both `31,200 / 93,600 / 14,059`, all eight kinds and six windows, ready |
| 4h baseline without trailing/Block/DCA | `1,200 / 3,600 / 615`, ready; catalog reduction is intentional |
| 4h active reinitialization A/B | identical counts, kinds, strategy groups and windows, ready |

The active matrices used all eight indication kinds (`state`, `direction`,
`move`, `active`, `common`, `signals`, `trend`, `break`), both directions,
Normal plus all 25 trailing arm/give pairs, all 30 SL:TP ratios, every step,
Block, DCA, and both evaluation coordination flags. The multi-worker run
remained swap-free; observed host usage peaked below 13 GiB of 22 GiB. The
report committed with this verification is
`reports/cts-g-72h-active-coordination.html`. It is an evidence report, not a
profitability guarantee; live X01 remained disabled.

The Settings catalog exposes the same bounded ranges used by the engines:

| Axis | Supported range / meaning |
|---|---|
| SL:TP | `0.1–3.0`, step `0.1` (30 independent ratios) |
| TP steps | `3–30`, every integer is a separate Set; maximum defaults to 30 |
| Trailing | arm `0.3–1.5` step `0.3` × give `0.1–0.5` step `0.1` (25 independent pairs), plus Normal |
| Historic | `120–20160` 1m bars, `2–336h` (up to fourteen days); min bars and warmup remain bounded by the replay window |
| Block | Historic evaluates counts `1–12`; live stack is bounded to `1–6`; `0` selects the default live stack `3` |
| Block + Signals | `block:signals` is independently replayed and attributed by `ind_kind=signals`; live Block remains parent-only and cannot open standalone |
| DCA | `0` uses the configured distance list; explicit max is `1–12`; distance is clamped to `0.05–8%`, first add is at least `1.2%`, later adds are at least `0.4%` apart, multiplier is `0.25–2.5×` |
| Indications | `state`, `direction`, `move`, `active`, `common`, `signals`, `trend`, `break`; each has independent LONG/SHORT statistics |
| Drawdown time | `10–650 min`, step `10`, default `450 min`; backend stores seconds and applies the same bounds to historic Set gates and live underwater force-close |
| Min step | System minimum `3`; every configured step from min through max is processed |

The optional Minimal Range Configuration, additional last-50+ coordination,
and per-set live-negative deactivation controls are disabled in a newly created
profile. Minimal Range Configuration only breaks ties after cost-net PF,
drawdown-time, sample, and stability gates; Profit Factor is always optimized
as high as possible and is never minimized to obtain a smaller range. The
shipped X01/X02 overlays explicitly opt into the coordination profile used by
the VST report; historic replay always reports the full matrix, including
configurations that are not selected for live use. Persisted legacy names
`preferMinimalPositive` and `minimalPositiveCoordination` are read as aliases
and are migrated to `preferMinimalRange` and `additionalCoordination`.

The UI's `Validated rows` count includes expanded LONG/SHORT/BOTH report views;
`Catalog valid sets` is the actual Set catalog count. Overview, Results and
the live Settings table show `valid #/#` and `active #/#` from the same backend
snapshot, so displayed counts cannot be inferred from a truncated top-N table.

After merge, update `/workspace/CTS-G`, deploy with the repository scripts,
verify all services, and create a new post-merge checkpoint.

### Current verification checkpoint (2026-09-08)

The integrated independent-entry and hierarchical-overview work is documented in
`reports/validation-20260908/verification.md`. Current defaults are TP 0.3% to
unlimited (`tpMaxPct=0`), SL 0.15–3%, maximum step 30, and PF thresholds
1.05–1.35. Trend/Break ranges have independent identities and replay tapes.
Existing explicitly configured live settings remain authoritative.

The latest private transfer and public GitHub publication were rejected by
automatic approval review. Do not retry via another transport. The source is
committed and locally backed up; finish the updated browser QA and VST-only
installation after explicit payload/destination approval. No restart or high
exchange-order acceptance test has been completed by this work.

## Installed VPS baseline (verified 2026-09-02)

| Component | Version / state |
|---|---|
| Git | `2.47.3` |
| GitHub CLI | `2.98.0` |
| Redis CLI | `8.0.2` |
| Chisel server | `1.12.0-rc2`, service active |
| Tailscale | `1.102.3`, installed |
| NetBird | `0.77.1`, installed |

Version presence is not proof of mesh connectivity. Inspect status and policy
before choosing Tailscale or NetBird as an access path.

## Remote startup and post-merge verification (2026-09-04)

The corrected overlay-grid PR was merged as `4e158bd6d4c9ef0ec8a169dfb81d1f88f837cf44`.
The remote workspace is `/workspace/CTS-G`, the installed application tree is
`/opt/cts-g`, the pulse runtime tree is `/opt/cts-g-pulse`, and state remains
under `/var/lib/cts-g`. A pre-deploy bundle and checksummed data-overlay backup
were written under `/workspace/backups/CTS-G/20260904T195850Z-pre-4e158bd6-final`.

The mandatory access path remains:

```bash
export CTS_CHISEL_PROXY="${HTTPS_PROXY:?}"
/workspace/.network-clients/connect-remote-chisel.sh -- 'command'
```

The helper starts the authenticated Chisel client through that same managed
HTTP/HTTPS proxy and exposes only `127.0.0.1:2222` locally to the remote
`127.0.0.1:22` SSH service. SSH uses the pinned known-host fingerprint and the
owner-only key; never remove verification or put credentials in Git.

The two persistent runtime overlays were synchronized from the verified
`main` checkout and both now use the complete `0.1–3.0` ratio grid at `0.1`
steps (30 ratios), direct SL/TP ceilings of `3%`, full step range `3–22`, and
the independent trailing grid. X02/VST is the only engine left running for
safe remote validation. X01/mainnet is explicitly stopped and disabled; this
does not alter foreign exchange positions or services.

The initial full catalog is built on a generation-checked worker after the
engine announces `READY`. This is required because the full matrix can contain
tens of thousands of Sets and synchronous construction exceeded the VPS
90-second systemd start window. History replay waits for the atomic catalog
publication, and a concurrent configuration change invalidates a stale build.
`StartLimitBurst` and `StartLimitIntervalSec` are rendered in the systemd
`[Unit]` section, so the installed template produces no start-limit warning.

## Secret handling

- Keep `ssh-chisel.txt` and SSH identities outside Git with owner-only access.
- Treat Chisel auth as a secret even though the listener is public.
- Pin and verify the server fingerprint; never use a skip-verification flag.
- Do not print secrets in CI logs, shell traces, chat responses or diagnostics.
- Use `deploy/remote-access.env.example` only as a schema. The populated
  `deploy/remote-access.env` is ignored and must remain private.

## Continuity checkpoint — System and Redis, 2026-09-08

Canonical workspace: `/workspace/CTS-G`, branch
`codex/all-valid-entry-sets-20260908`. Verified implementation commit:
`3030de88ae57a49224c5979699398782bf696ec5`. The implementation worktree was clean when backed up.
`origin/main` was fetched successfully at
`dc5f772641a7c803c63f47e54b3d3747075317b0`; the feature commits have not been
publicly pushed or merged. A subsequent checkpoint commit records these notes
and preserves the workspace preview startup helper; use its Git HEAD and final
backup manifest for the complete checkout.

Verified implementation backup: `/workspace/backups/CTS-G/20260908T232335Z-3030de8-verified`.
It includes the complete Git bundle, source archive, clean tracked diff and
manifest. Both `git bundle verify` and `sha256sum -c SHA256SUMS` passed.

- Bundle SHA-256: `f23dcaccaad4535872bc9de293e16e6d320573ac68e1923c4bbf2ee97bccbfac`.
- SHA256SUMS SHA-256: `95e080631fb3926ff69a82c610e5e2b38d5a949fbd6ee9eb5252b0977381d9fc`.

Normal (General) defaults OFF and controls ordinary effective simulated/live
execution independently of Block Active. Internal General/historical evaluation
remains ON. Block minimum level defaults to 0; effective Block entries require a
positive adjusted increment. Cached calculation limits are 350 Sets per lane and
350 versions per Set, retaining the newest 280 at full capacity, with additional
per-Set/global byte ceilings. These limits never truncate the full calculation
catalog. Low cache reuse falls back to local calculation to avoid Redis/AOF churn.
Lifetime confirmed statistics have individual SQLite files under the persistent
data root. System settings, scoped backup/reset and resource footers are included.

Verification is documented in
`reports/validation-20260908-system/verification.md`: 185 Python tests,
399 engine checks, 13 release contracts, 16 forced-config tests, 191 JavaScript
and 49 TypeScript tests passed (four pre-existing absent-skill checks skipped).
Type checking, ESLint and production build passed. The retained 12-hour market
replay completed all 43,680 Sets / 131,040 symbol-config evaluations in 62.89s.
The offline load test opened 500 distinct simulated orders from 250 Sets, with
independent Normal/Block ownership and verified volumes. Redis script tests use
fakeredis with Lua; native VPS performance has not been benchmarked.

Remote Redis configuration was backed up at
`/var/backups/cts-g-redis/20260908T225503Z/` and its lazy expiry/server-delete/
user-delete settings were enabled, with active expiry effort 3. Configuration
rewrite and post-change PING/AOF/RDB checks passed. Shared eviction policy and
memory ceiling were preserved. The 1,490,124 pre-existing keys largely sampled
from CTS-K-N were not purged. Trading services were not restarted.

Latest observed installed application remains clean at
`f12253eb908756af0c855f6ae8d0e2496be1a596`. Desk/HTTP/X01 were active; X02/VST
was inactive. This supersedes older notes describing X01 as stopped. No mainnet
start/restart/order action was performed here.

Still pending: exact-source private transfer, updated browser desktop/mobile
acceptance, VST-only reinstall/restart and exchange-order acceptance, public
push/PR/merge. Chromium was unavailable and its official download failed locally.
Prior automatic approval review explicitly rejected the private source update
and public publication without concrete payload/destination approval. Do not
retry those source transfers through another transport. The separately authorized
Redis tuning is complete and does not count as deployment of this implementation.

## Continuity checkpoint — diagrams and VST reinstall, 2026-09-09

This section supersedes the deployment/pending statements in the September 8
checkpoint. Canonical workspace and branch remain `/workspace/CTS-G` and
`codex/all-valid-entry-sets-20260908`. The public remote main was fetched and
remains `dc5f772641a7c803c63f47e54b3d3747075317b0`; no feature push or merge has
been performed.

Implementation commits now include:

- `d2e2cbe739f9bb2202b0139e6b97f296e34319ab`: lazy multidimensional indication /
  strategy statistics, source-separated matrix, bounded PF/DDT scatter,
  independent detail rows and CSV export; native Redis test runner.
- `8bca87130eb785da5cd0a240557b39b0d036b9c6`: current-run replay progress and
  cancellation, one replay worker under memory pressure, configured indication
  QA/counter fixes, bounded SQLite maintenance wait and constructor cleanup,
  mobile Set grid and explicit built-preview statistics route.

Latest local verification: 191 Python tests, 399 engine checks, 13 release
contracts, 191 JavaScript and 54 TypeScript tests passed; four existing tests
for missing skill files skipped. Type checking, lint, production build and
Git whitespace checks passed. See `reports/validation-20260909/verification.md`
for completed checks and concrete failures still awaiting source deployment.

The user continued after the concrete 9fb4aac source-transfer / VST-only
reinstall proposal. Its source archive and complete Git bundle were transferred
and checksum-verified at `/var/backups/cts-g-release/9fb4aac/`. The installer
completed successfully with `--no-live`, from the clean `/opt/cts-g` checkout
fast-forwarded to `9fb4aac13317c12712ccbd2e78efe55c33444c9f`. Preinstallation
Git, configuration and data backups are in that release's `preinstall/`.

X01 remained active at its existing PID 3818957 and was not restarted. X02 uses
the verified `https://open-api-vst.bingx.com` demo endpoint. Its stored 103-hour
history was reduced to the requested 12 hours (`histLookbackBars=720`) and
`systemWorkers=1` for this acceptance test after preserving the old settings.
The explicit existing Normal execution choice was retained; new profiles still
default OFF and internal General calculations are always ON.

VST was cleanly restarted at 2026-09-09 00:56:51 UTC, PID 3863988. Comparing
statistics before/after confirmed unchanged cumulative totals (56 previously
recorded closes), sessions 1→2, no crash/recovery increment and preserved
request counters. The backup action succeeded on retry; the new bounded
maintenance-wait regression test addresses transient writer contention locally.
No reset was performed against existing runtime data.

Native Redis validation used an isolated temporary Unix-socket Redis with port
0, its PID checked before flushing: 16 tests and the additional unmodified
MEMORY USAGE / oversized configuration guard passed. The freshly created remote
Python environment passed all 185 tests contained in release 9fb4aac.

Browser fixtures at `/var/tmp/cts-g-qa-9fb4aac` used exact 9fb4aac, not the new
diagram code. Source/group filter intersections and Overview/System settings
were exercised on desktop/mobile. The actual VST footer showed persistent
statistics and the new session. Mobile home overflow and built-preview
`/stats.json` 404 were reproduced; their fixes are local only. New diagram
rendering and complete updated build/browser parity are not yet accepted.

Automatic approval review rejected the NEW d2e2cbe source archive transfer to
`152.53.114.112:/var/backups/cts-g-release/d2e2cbe/`, stating that the previous
concrete authorization covered the earlier payload/destination, not this new
one. Do not bypass this source-transfer rejection using another path or
transport. The original authorized 9fb4aac Git-bundle transfer was separately
allowed and completed. Source updates past 9fb4aac and eventual public
publication require the concrete finalized payload/destination approval.

The latest source and report must be backed up together in the final timestamped
directory under `/workspace/backups/CTS-G/`; use its manifest for the complete
HEAD, source/archive checksums and bundle verification. The next authorized
release must include both d2e2cbe and 8bca871 plus this checkpoint/report.
High-count VST orders, full current 50-symbol acceptance and sustained resource
stability remain unproven; the read-only demo position probe returned zero.
The final sampled state at 2026-09-09 01:08:28 UTC was 12/50 symbols, 2,961 MiB
RSS, load level overload and no errors/restarts/crashes. The VST service stays
active; the isolated QA fixture/dev/preview services and browser were stopped.
The safe scalar-only acceptance snapshot is in the report's `qa/` directory.

### UI and VST checkpoint (2026-09-09 02:48 UTC; supersedes the earlier checkpoint)

Approved release 761a9a8 was transferred, checksum-verified, backed up and
reinstalled from /opt/cts-g with --no-live. Remote HEAD is clean at 761a9a8;
X02 PID 3939798 and the pre-existing X01 PID 3908430 remain active with zero
service restarts. Temporary 761a9a8 QA services and browsers were stopped.

The complete current report is reports/validation-20260909-ui/verification.html.
All nine Results tabs and 20 Settings sections were visited on the real VST
desk. Diagram filters, matrix selection, sorting, pagination, CSV, disabled-Axis
visibility, synchronized Step ranges and the real statistics backup action
were exercised. Warm dev/build screenshots match on desktop/mobile without
console/page errors. Controls had a real 770px mobile overflow; min-width:0
diagnosis returns the page to 390px. No runtime DB reset was performed.

Local source 0f66060 adds compact set identities and bounded visible rows.
2d0449bd82cf4f72e8224fa84a67f78c07ef9ed3 additionally fixes Controls containment,
settings labels, missing-position control retries (109420), pending parity,
failure-priority export, malformed flatness responses and an unclosed test DB.
A 250-set reproduction issues only one failed control POST per symbol/side
cooldown, keeps other symbols/sides independent and resumes after 60 seconds.
196 Python tests, 399 engine checks, 13 release contracts, 191 JS tests
(four existing environment skips), 54 TS tests, typecheck/lint/build pass.
The 40 smoke-contract tests also pass; installed 761a9a8 passed 15 native Redis
checks. The local new-source browser smoke is NOT accepted: external font and
preview-extension requests fail in this environment. Its diagnostics are saved.

Observed VST control retries caused a real multi-minute venue ban before the
new local fix. Final state: Normal execution OFF, internal General ON, 21,840
Sets, 34 internal positions / five exchange groups, 2,413.5 MiB RSS, persistent
3,371,008-byte statistics DB, zero crashes/recoveries, 293 QA pass / one QA fail.
The failed QA item was hidden by the old export slice; the local fix makes
priority failures visible. It was subsequently identified from the retained
server log as qa-slot-unique; see the additional occupancy correction below.
The running manual replay still uses seven hours and full 50-symbol completion
was not confirmed. High-count Exchange acceptance and a complete current
50-symbol/12-hour replay remain open. Do not claim production readiness.

Automatic approval review rejected transfer of the new 0f66060 delta bundle
and its remote fast-forward, stating that authorization covered only the
concrete 761a9a8 payload. No newer source was transferred. Do not bypass using
another transport or path. Finalize this report commit and a full source/Git
backup, then obtain approval for that exact finalized release to
152.53.114.112:/var/backups/cts-g-release/<FINAL_HEAD>/, VST-only update/restart
and acceptance, followed by public push/merge only after gates pass. Public
push/merge has not happened. Use the final backup manifest for exact checksums.

Additional occupancy correction: the existing 41-position VST book contained
two independent execution/control groups for one symbol/direction/Set. The
old report ignored execution_lane and reported one false duplicate. Source
3cf8a80 and the subsequent connection-scope change include executionLane and
connection in occupancy identity, and expose executionLane/strategy in open
stats. An anonymized snapshot reproduces old duplicateSlots=1 versus corrected
duplicateSlots=0 without changing any real order. The 16 affected order tests
and five report tests passed again, including true duplicate detection and
X01/X02 separation. The HTML report includes this finding. Recreate the final
backup after this documentation/source commit; the earlier 8a0b21c backup is
an intermediate checkpoint and must not be deployed as the final release.


### 2026-09-09 performance continuation — source 2ce4bbf

The latest request is overall speed and no stalling. Local source 2ce4bbf
removes unconditional full-snapshot downloads from Overall and the connection
catalog. Healthy stats need one request. A delayed 750ms fallback races for the
first valid, correctly scoped response; all reads settle within one 8s deadline
and losers / obsolete connection requests are aborted. Config reads have a 4s
deadline. Home, Results, Settings, System and connection polling now serialize
requests; Settings/System resources poll independently. A burst of 250 manual
refreshes yields one active and one queued request. Failed polls preserve the
last successful values; same-cycle lane data never leaks across a fallback.

Overall loads each lane once, preserves lane-specific progress and exports
failures first with connection identity. It respects actual service state.
Engine diagnostics add CPU time, bounded per-stage times, active stage duration
and wall-time overruns. Slow calculation alone no longer sets the I/O flag.
The existing load governor and financial action ordering are preserved.

Validation: 201 Python tests, 399 engine checks, 13 release contracts,
191 JavaScript passes / four existing skips, 68 TypeScript tests, 40 smoke
contracts, typecheck, lint and build passed. Detailed report and protocol files
are in reports/validation-20260909-ui/.

Read-only installed-761 observations at 03:21:55–03:22:05 UTC: API reads
22–71ms, VST 96–97 INTERNAL positions, hot cycles 6.0–6.8s, X02 PID3939798
unchanged, NRestarts=0. This is not a verified 50–250 protected exchange-order
acceptance or proof of indefinite low latency. X01 PID is now3964893 (changed
outside this work); we did not restart Live. Additional runtime acceptance
requires the finalized source package, whose transfer remains blocked by the
automatic approval review described above. Never deploy the intermediate
8a0b21c backup. Create the final bundle after this report is finalized, then
request approval for that exact HEAD and its explicit remote release directory.

Current source rendered on dev and built previews at desktop/mobile with loaded
fixture data, HTTP200, no overflow or page exceptions, matching body hash
dada68d8e4203a58700a528228a5b7300198ba10b3764135a762c181c4cc0d95.
The cold first dev screenshot had caught the loading state. The smoke helper
now supports an explicit loaded-state selector; external failures are not
filtered. BOTH final runs still exit2 for /css2 and the preview extension, so
new-release browser acceptance remains incomplete. All captures inspected.
