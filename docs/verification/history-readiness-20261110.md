# History and sample readiness — 10 November 2026

CTS-G's existing default is 4,320 completed one-minute bars (72 hours); it is
preserved. The controlled comparison uses explicit 24-hour and 48-hour evaluated
history on the same following 24 hours, within the 72-hour research limit.
Live stage-context collection additionally retains upstream entry evidence and
warmup; collection duration and the current evaluation window are different.

Automatic history growth now checks each Set's own policy. A complete Last-1
lane no longer appears short because a sibling requests Last-30; strict Last-200
continues collecting beyond the collection target of 12. An independent minimal
window reads the full eligible Base history instead of the truncated long-window
count. Small configured populations can also request growth. Partial-window
admission, explicit history limits, failed/locked exclusions and atomic complete
history promotion remain in force. No PF admission threshold is relaxed.

The controlled regression covers strict and partial windows, Last-1/200,
independent minimal Last-3, and two-Set populations. Existing history-growth
and sample-coverage regressions are included in the full verification run.

Sample readiness is distinct from profitability. Complete losing windows count
as fully sampled. Current Base, Main and Real windows use their own causal
close streams; downstream percentages state their predecessor-qualified
population. Missing or disabled windows are not reported as passing.

Market comparison results, test source hashes, hourly counts, post-Base PF,
settled PF, and execution limitations are written by
`scripts/history_readiness_study.py` into its selected evidence directory.
No economic preset is promoted automatically.

## Completed paired market study

The 72 cases use the same frozen 12-symbol market series and final 24-hour trading interval. There are 36 paired policies, with 24h versus 48h evaluated history, ten positive Base/Main/Real window triples and two recent-loss Last-1 veto variants per range. All 576 native/replay checks and 2,304 independent sample-count checks match. Source hashes remained unchanged throughout.

For the illustrative strict Base-30/Main-15/Real-12 policy, Base readiness at trading start changes from 75.00% to 96.875% for Micro, 71.18% to 96.875% for Minimal, and 69.62% to 96.875% for Short. Real readiness remains zero for that policy. Under 10/5/3, all three ranges reach 100% Base readiness with 48h; downstream own processed histories remain much thinner. Every full losing window stays in the readiness denominator.

None of the 72 tested configurations passes the predefined two-half profitability screen. The controlled catalogue tests normal TP/SL and four indications, not every strategy combination. The 3,115 settled trade occurrences are summed across separate experiments and are not unique venue trades. No PF/order-count economic preset is promoted. Detailed HTML, hourly rows, plots and paired JSON are produced by the two `reports/*history-readiness.py` scripts.

Final verification on this source: 679/679 Python regressions; 108 TypeScript tests; 191 Node passes with four unavailable authoring-document checks skipped; 109/109 engine checks; typecheck and development build pass. The native bounded VST wrapper has its own offline transport/ownership/readiness checks and does not certify a running venue session by itself.
