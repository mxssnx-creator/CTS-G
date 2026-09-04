# Validation handoff

The complete row-level HTML artifact is `cts-g-validation.html`.

## Evidence and promotion

- Recalculated XRP/BCH/SOL for 72h: 4,320 historical 1m bars per symbol,
  3,888 independent baseline combinations, 100% of the defined TP/SL grid.
- TP 0.40–0.80%, SL 0.10–0.50%, 0.05 percentage-point increments; eight
  indication types and both sides. One identified settings hash per symbol.
- No candidate clears all existing gates. Do not lower minimum PF/sample
  requirements, inflate partial fills into additional trades, or transfer a
  synthetic/historical result into the exchange-confirmed evidence pool.
- The displayed PF8/25/75 distinguishes full from partial windows. DDT here
  is realized backtest drawdown duration, not live account or intratrade DDT.
- No authenticated VST lifecycle test or new Mainnet order was performed.
  Mainnet promotion and a stable remote rollout remain unconfirmed.

## Release repair

Remote read-only inspection of commit `7f51a6f` showed both CTS-G engines
failed with missing `forced_configs.valid_candidate`. That release also
imports `completed_roundtrips` and `runtime_scope`; publish the complete
dependency set, never only `pulse_trader.py`.

The repair preserves canonical legacy Redis keys until an explicit installer
namespace migration. Named instances use separate namespaces and CID prefixes.
An independent replay copy recreates the non-copyable pick lock, preserves
Set aliases, and omits disposable UI caches. A same-group fill no longer
references undefined `pos` while extending its aggregate close quantity.

## Acceptance blocker

Typecheck, lint, production build and JS/TS tests passed. Local browser
acceptance could not run: Vite exited at `uv_interface_addresses` with errno 1,
and the browser daemon exited during startup. Do not claim a visual or end-to-end
acceptance, full reinstall, remote soak, or successful live profitability.
No permission restriction was bypassed.

## Separate work preserved

- `codex/connection-session-margin-call`: CTS-G persistent 30%-remaining-equity
  guard, ownership-scoped close intents, 16 targeted tests and 283/283 engine
  checks passed on its recorded base. UI, reset/rearm flow, newer-main integration
  and remote activation remain open.
- CTS-K-N has concurrent margin-call work. The unintegrated
  `session-equity-policy.ts` / `session-equity-guard.ts` draft is not a deployed
  feature and must not be combined blindly with the concurrent margin-call API.
- Original installer/multiinstance/dashboard changes remain preserved in the
  original worktree. Installer manifest/remove/reinstall and privileged service
  controls are not verified complete.

The 30% floor means more than 70% loss from the active session baseline, not
30% loss. Never describe this as guaranteed moderate risk or guaranteed fills.
