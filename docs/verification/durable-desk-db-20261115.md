# Durable desk DB: current port of the DB part of PR #19

The common base `4e158bd6d4c9ef0ec8a169dfb81d1f88f837cf44` and the current
Stable-02-01 target `37b7f84c029acebd1ec1eca14d85bb6fa0d69bce` contain the
same `src/lib/db.ts` blob, `49777ade8f9d5613a25f7b5272a6bf88edbe709a`.
The persistent PGLite directory in old head
`fa9b801174d83dae9558edccd361c9d864b5b3e4` was therefore still missing.
This is a narrow port onto the current target, not a merge of old engine code.

## Behavior

Managed Linux installs record a desk database path outside the checkout:
`/var/lib/<install-name>/db`. Explicit `CTS_DB_DIR` takes precedence over
`CTS_STATE_DIR/db`; existing environment-file paths and unrelated lines survive
an idempotent update. Invalid install names fail instead of being silently
converted into another instance's name. Explicit shell path overrides survive
`--name` as well. Those overrides must designate a distinct path per instance.

PGLite creates missing parent directories with private permissions and reopens
the same database across process restarts. Bootstrap still shares a single
promise per process and preserves the transactional migration ledger. Invalid
or inaccessible configured paths fail; they do not silently create an empty
in-memory replacement. `CTS_REQUIRE_PERSISTENT_DB=1` additionally rejects a
missing directory configuration. Unconfigured local previews remain in memory;
an explicit `DATABASE_URL` continues to select the existing Postgres backend.

There is one desk writer per database directory. This change does not add
cross-process PGLite locking, automatic database backup/export, or migration of
data held only in an old running process's memory. Before the first upgrade of
such a process, its data must be exported and verified. No production install,
restart, environment change, account write or exposure cleanup was performed.

## Verification

`node --test scripts/durable-desk-db.test.mjs` executes the actual DB bootstrap
with real PGLite in separate Node processes. Only the Vite migration glob is
replaced with a deterministic migration fixture. It checks exact retained rows
and migration identity, separate directories, explicit path precedence, a
completed write followed by SIGKILL without a clean close, preview isolation,
invalid/unavailable paths, private directory creation, and fake-root installer
environment updates. The previous target fails the retained-row assertion and
installer path checks. An initial test-harness import-resolution failure and an
intermediate missing-parent-directory failure were retained in the overall
verification report; neither is reported as a successful baseline run.

Typecheck, production build, scoped lint and the focused tests passed locally.
Full project regression/test and required CI outcomes are recorded with the
exact published head in the overall verification report.

## Still open in #19

The two-name Pulse-directory failure remains reproduced: named installs still
share `/opt/grok-x01-pulse`, Redis keys and service-unit names. Complete engine,
HTTP, settings, port, unit and log isolation must be ported together before
installing another instance. PR #19 remains open for those parts and its other
unverified claims. This DB repair is not a multi-instance deployment acceptance,
an x02 forward test, or evidence of positive execution PF. Trading policies,
qualification gates, fees and execution code are unchanged.
