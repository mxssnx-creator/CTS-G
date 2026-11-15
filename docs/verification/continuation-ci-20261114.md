# Continuation checks, 14 November 2026

Source base: `7fb0e077bdacc7a4881e5551e99efc94e96609fd` on Stable-02-01.

The Settings version of Secure, Minimal had gained independent Set Auto-PF, Signal and range/Exit settings while its saved JSON had not. Regenerate the saved preset from `secureMinimalPreset` so loading the file and selecting it in Settings use the same policy. Conservative explicit sizing, leverage, sample, drawdown and strategy switches are retained. The Auto floor remains above one.

The bounded x02 wrapper's former 18-hour evaluation default now needs 4,710 total minute bars with the shipped 720-minute Exit hold: 3 stage windows plus 30 indicator bars plus two hold spans. It therefore refused its own default before any connection. Use an explicit 12-hour test default, which requires 3,630 bars (60.5 hours). User-supplied longer histories are not clipped: the existing 72-hour cap still rejects 18 and 48 hours for this overlay. The engine self-test now checks the owner's current 100-symbol x02 preference, without asserting that an account is running.

Local evidence: the original Settings test reproduced the saved-preset difference; all 19 config-model tests pass after correction. The 46 native session tests, all 109 engine checks, TypeScript and production build pass. Full offline regression and required hosted CI are checked separately before merge. A first full local run failed at Python client initialization because the environment's SOCKS transport lacked socksio; installing that environment dependency allowed the actual offline checks to run. No runtime dependency or production gate was changed to hide that failure.

No exchange writes, settings application, account start or service restart. The read-only x02 server health request timed out; health is unavailable. No new market cohort was evaluated, so post-Base PF, passed/evaluated validation and venue execution PF for this source remain unavailable. Earlier losses and incomplete twelve-symbol acceptance remain unchanged.
