# Production source integration, 2026-09-08

The user authorized capturing, committing, pushing and merging the server work.
The running `/opt/cts-g` worktree was captured with a separate Git index. Its
24 changed source files exactly matched the existing commit
`19893db2192f02a978f18993b8b943ac9f34514e` (tree
`a828ed8204452325fc5347a00425d2b10666e99c`). The snapshot was preserved as
`codex/server-snapshot-20260908`; production HEAD, real index, files and services
were not replaced. No exchange orders were sent by this integration work.

The snapshot is merged with GitHub main
`0ac9c27254f49c0b169b616769dd666dc89c8035`, retaining its six newer commits,
including the 50-symbol/100-slot defaults and historic scheduling improvements.
Integration includes the server's diagnostic API fields, flat-position error
handling and tape compaction. An empty completed replay now removes obsolete
symbol evidence and rescores affected Sets, including older tape rows.

The frontend default symbol helper now agrees with the 50-symbol overlay. Its
default PF agrees with the engine's 1.10; explicit saved settings still win.
The research adapter supplies DDT to stage qualification. Outdated tests were
updated for explicit axis activation, historic readiness and CPU/load scheduling.

Verified locally:

- Engine suite: 396/396 checks.
- Python discovery: 111 tests, including new empty-replay and flat-error regressions.
- Release contract: 12 tests; forced/replay contract: 16 tests.
- Node scripts: 191 passed, four skips for absent optional skill files.
- TypeScript tests: 41 passed; typecheck, lint and production build passed.
- Build database migration was skipped by unsetting DATABASE_URL.
- Source secret scan and Git whitespace checks passed.

Protected server backups are in
`/var/backups/cts-g-release/20260908-server-commit.IgKdpv`: Git bundles, binary
worktree patch, untracked source archive and verified checksums. A separate
pre-work Git bundle is in `/workspace/backups/CTS-G/20260908-before-independent-RbwvFI`.

These checks verify source integration. They are not a 12-hour market replay,
an unlimited-duration availability test or proof of profitable live execution.
The subsequent implementation and current release status are documented in
`validation-20260908/verification.md`.
