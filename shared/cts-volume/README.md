# Relational volume weights

Neutral execution sizing with independent scope maps. See `ratios.ts` for validation and multiplication, `sliders.tsx` for settings controls, and `ratios.test.ts` for numerical contracts. No qualification result or previously filled quantity is changed. Additional parent-sized legs apply `strategyVolumeWeight` only when that strategy is new to the parent.

The policy is persisted by the host project in `relationalVolumeRatios`. Missing policy/weights means 1.0; explicit false disables the policy. Keys are case-normalized and duplicate case variants, dangerous keys, non-finite values and out-of-range/non-step ratios are rejected in strict settings validation.
