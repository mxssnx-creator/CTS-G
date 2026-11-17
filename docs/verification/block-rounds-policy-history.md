# Block rounds policy history

Editing one count’s sizing settings used to inherit that count’s former recovery balance and Auto PF. A count now starts or resumes the history for its own policy. Editing one count leaves sibling histories intact when their settings still match. Long and short remain separate even when a parent Set supports both. Results for an in-flight position settle against the policy recorded at entry, including after a restart.

Each count computes recovery at the size it asked for on its own, including its own ceiling and Auto verdict. This keeps a sibling’s sizing, venue lot rounding and the combined volume cap from clearing another count’s deficit. Actual entry sizing still adds all eligible requests under the combined cap. Actual executed PF remains a separate measure.

The version-3 file retains older state for inspection. Files from version 1 or 2 lack an exact policy and individual entry size. Their open memberships are retired as positions leave and their history cannot qualify a new procedure. Version-2 software cannot use a version-3 file. Deploy with a copy of the current state available for rollback.
