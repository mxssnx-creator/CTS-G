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
   systemctl is-active redis-server grok-desk grok-pulse-http \
   grok-pulse@bingx-x02 grok-pulse@bingx-x01'
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

Historic validation accepts 2–72 hours (1m bars); the three-day stress run is
`hours=72` / `lookback=4320`. It processes every enabled pack, SL:TP ratio,
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

The latest complete run covered 72 hours × 12 symbols, 13,520 catalog Sets
per symbol, 40,560 expanded Set/direction views and 39,163 validated rows;
all eight indication kinds, both directions, the full trailing product and
Block/DCA lanes were included. This is synthetic/offline evidence only and
does not claim live exchange profitability or submit orders.

The Settings catalog exposes the same bounded ranges used by the engines:

| Axis | Supported range / meaning |
|---|---|
| SL:TP | `0.2–2.6`, step `0.2` (13 independent ratios) |
| TP steps | `3–22`, every integer is a separate Set |
| Trailing | arm `0.3–1.5` step `0.3` × give `0.1–0.5` step `0.1` (25 independent pairs), plus Normal |
| Historic | `120–4320` 1m bars, `2–72h`; min bars and warmup remain bounded by the replay window |
| Block | Historic evaluates counts `1–12`; live stack is bounded to `1–6`; `0` selects the default live stack `3` |
| Block + Signals | `block:signals` is independently replayed and attributed by `ind_kind=signals`; live Block remains parent-only and cannot open standalone |
| DCA | `0` uses the configured distance list; explicit max is `1–12`; distance is clamped to `0.05–8%`, first add is at least `1.2%`, later adds are at least `0.4%` apart, multiplier is `0.25–2.5×` |
| Indications | `state`, `direction`, `move`, `active`, `common`, `signals`, `trend`, `break`; each has independent LONG/SHORT statistics |
| Drawdown time | `10–650 min`, step `10`, default `450 min`; backend stores seconds and applies the same bounds to historic Set gates and live underwater force-close |
| Min step | System minimum `3`; every configured step from min through max is processed |

The UI's `Validated rows` count includes expanded LONG/SHORT/BOTH report views;
`Catalog valid sets` is the actual Set catalog count. Overview, Results and
the live Settings table show `valid #/#` and `active #/#` from the same backend
snapshot, so displayed counts cannot be inferred from a truncated top-N table.

After merge, update `/workspace/CTS-G`, deploy with the repository scripts,
verify all services, and create a new post-merge checkpoint.

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

## Secret handling

- Keep `ssh-chisel.txt` and SSH identities outside Git with owner-only access.
- Treat Chisel auth as a secret even though the listener is public.
- Pin and verify the server fingerprint; never use a skip-verification flag.
- Do not print secrets in CI logs, shell traces, chat responses or diagnostics.
- Use `deploy/remote-access.env.example` only as a schema. The populated
  `deploy/remote-access.env` is ignored and must remain private.
