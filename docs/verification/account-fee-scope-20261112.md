# Fee scope and final integration

Integrate the later fee-scope and explicit Micro-default changes from #171 with the already merged #172 cleanup/scheduling source. The running twelve-symbol observation retains its original #172 source and captured overlay; it is not retagged as a test of this later head.

BingX documents its swap commission endpoint as the current user's maker/taker rates with no symbol parameter: https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-account/api-reference.md. One measured response can populate the account-bound selected-symbol cache. Bybit and unknown adapters retain symbol scope. A failed symbol receives its own cooldown and cannot starve another symbol; account failures use a shared cooldown. Future-dated cache entries require a fresh measurement.

The explicit native profile now supplies independent Micro SL/trailing defaults of 0.1% / 0.05%, preserves custom values and rejects malformed inputs. This is distinct from the already running captured profile. No new profitable result or cost discount is inferred from this code change. Actual fill prices retain slippage and reported commissions enter once.

Scoped tests cover both venue scopes, partial failures, cooldown persistence, cache freshness, exact commissions, native wrapper admission and cleanup. Full hosted checks are required on the combined source before publication.
