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
failed in that environment; the proxied command and the complete SSH round
trip were verified on 2026-09-02.

Load `CHISEL_AUTH` only from the protected access info. Never paste it into a
command committed to Git, a workflow, a log, or this document.

```bash
test -n "$HTTPS_PROXY"

chisel client \
  --proxy "$HTTPS_PROXY" \
  --fingerprint 'Q0MxL4WHKwM2JbRy6/6fAUee3600R7pPo1CKov8/EPc=' \
  --auth "$CHISEL_AUTH" \
  http://152.53.114.112:8090 \
  127.0.0.1:2222:127.0.0.1:22
```

Keep that client running, then connect through the local endpoint in another
terminal:

```bash
ssh \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  -i /secure/path/snet-ln-deb01.txt \
  -p 2222 root@127.0.0.1
```

In execution environments where background processes and subsequent commands
use isolated network namespaces, start Chisel and run SSH in the same execution
session. On an ordinary host that can reach `152.53.114.112:8090` directly,
`--proxy "$HTTPS_PROXY"` may be omitted. It is not optional in the managed
workspace described above.

The mapping is always local port **2222** to remote port **22**. `222` and
`22222` are incorrect.

### Failure interpretation

| Observation | Meaning / action |
|---|---|
| Raw IP reports `network is unreachable`, but proxy is configured | Use `--proxy "$HTTPS_PROXY"`; do not change the forwarding ports. |
| Chisel reports fingerprint mismatch | Stop. Independently verify the new fingerprint from a trusted route before changing the pinned value. |
| Chisel returns unauthorized | The protected auth value is stale; do not remove authentication or print the value. |
| Tunnel connects but SSH fails | Confirm the identity, its `0600` permission, `IdentitiesOnly=yes`, and that the mapping is `2222:127.0.0.1:22`. |
| Local port 2222 is occupied | Stop the stale local client or deliberately select another local port and document that temporary deviation. |

### Read-only validation

After the tunnel is established, a safe validation is:

```bash
ssh -i /secure/path/snet-ln-deb01.txt -p 2222 root@127.0.0.1 \
  'hostname; git -C /opt/cts-g rev-parse HEAD; systemctl is-active redis-server grok-desk grok-pulse-http grok-pulse@bingx-x02 grok-pulse@bingx-x01'
```

The 2026-09-02 validation reached hostname `v2202607384858486523`, found
`/opt/cts-g` at commit `bf8afab66251e6170f3d7297cd0003676222e02e`, and returned
`active` for the desk, sidecar, both pulse engines and Redis.

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
