# Block Active and adjusted-only execution

Local implementation for the user's CTS-G request. Not installed remotely.

Defaults: `normalExecutionEnabled=false`, `blockActive=true`, `setMaxActive=50`.
General and Indications calculation remain enabled. The Normal setting controls
unadjusted exchange entries, including the forced-baseline entry path.

Block Active requires a selected, historically ready reference with at least
8 samples, Real PF, positive net expectation and bounded drawdown time. It
requires valid reconciliation, control orders, both Active Live/Real flags,
overall coordination and the existing live PF floor. A virtual reference must
show at least 45 seconds and 0.2% continuation. Its normal quantity is never
executed when Normal is disabled.

The order quantity is the Block increment of the reference less confirmed and
pending same-symbol/same-side quantities. Count alternatives are not summed.
The increment cannot exceed the reference quantity. Exchange minimums cannot
upsize an adjusted entry. Pending recovery preserves Block attribution and
the parent/count/ratio. Active positions cannot receive another Block or DCA
layer. Shadow observations restart after process restart or stale/reversed
signals; they are never persisted as exchange fills. A count with nonpositive
own retained adjusted results stays blocked; this conservative gate does not
promise automatic recovery or profitability.

The former no-op active-set limit is enforced while calculation continues for
the whole catalog. Previously unselected candidates can return after scoring
improves. Explicit zero retains unlimited selection. The default is a maximum,
not a forced count of profitable sets.

Validation: full engine suite 283/283; focused suite 76/76; frontend 228 passed
and four existing missing-OG-skill tests skipped; TypeScript and production
build passed; release contracts 11/11. The tests cover the actual order-call
boundary, normal opt-in/default-off, forced-path blocking, partial pending
recovery, all Block counts/ratios, minimum amounts, overall gates and selection.
No real exchange order was sent by these tests. No full browser acceptance is
claimed. The precise last execution logs accompany the HTML report.

Historical reruns: 2,502,720 finite matrix rows; 24,700 Top-25 Block/Axis rows;
900 additional adjusted-only variants over 50 training-ranked parents. The
new 900-row experiment has zero qualifying variants and zero modeled entries.
It uses historical shadow coordination and minute resolution, not live account
fills, exchange minima, funding, latency or a combined exchange portfolio.
Do not weaken gates or label zero blocked trades as positive results.

Remote still runs 87e99fd0454cd2dc3f053c4c11ef44fadbc2212c. Its fresh isolated
engine run is 282/283: the old hardcoded Gx02 named-install fixture remains.
Remote frontend 225 passed/four skipped; typecheck/build passed. X02 remains
inactive with STOP present; X01/Mainnet must remain masked. A separate
552-symbol historical job must not be cancelled casually.

Remote disk pressure recurred: read-only evidence showed about 10.28 GB free
and a roughly 23 GB Redis incremental AOF. Redis PONG and persistence writes
were still successful. The BGREWRITEAOF request was rejected by automatic
approval review because it mutates/loads shared persistence under disk pressure
without explicit maintenance approval. It was not executed. Prior automatic
review also blocks source transfer/installation, remote settings/control writes
and extra TLS listeners. Do not use alternative transfer routes to bypass it.
Finish reviewable artifacts before asking for the specific remaining approval.

Reproduction:

    python3 -m unittest discover -s scripts -p 'test_*.py' -v
    python3 scripts/engine-test.py
    python3 scripts/release-contract-test.py
    npm test
    npm run typecheck
    npm run build
    python3 scripts/replay_complete.py --data DATA --output RESULTS --workers 2
    python3 scripts/replay_top25.py --source RESULTS --settings SETTINGS --output ADDITIONAL
    python3 scripts/replay_block_active.py --source RESULTS --settings SETTINGS --output ACTIVE_RESULTS

The report supplement reads `block-active-results.json` and
`block-active-verification.json` from its evidence directory and renders plain
tables, parameter details, daily metrics and a training-PF diagram.
