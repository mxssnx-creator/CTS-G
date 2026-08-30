# Verifier index

- `v1/` (2026-08-30): baseline acceptance criteria.
  Measures: (1) `python3 scripts/engine-zest.py` → 85/85 pass; (2) `python3 -m py_compile` on all server/pulse/*.py → clean; (3) `node scripts/overall-zest.mjs` → pass; (4) `npm run typecheck` → clean; (5) `npm test` → pass; (6) universe.json is valid non-empty JSON or engine tolerates empty without crash.
  Initial baseline. Run records appended under `verifier/runs/`.
- `v2/` (2026-08-30): adds `max_symbols_smoke.py` — 530-symbol offline drive of the real Pulse engine (config apply, indications scan, entries, stats write). Extends v1 with full-universe scale + x01 unlimited-cap assertions; v1 criteria unchanged.
