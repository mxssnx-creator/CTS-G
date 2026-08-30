# v1 acceptance criteria
1. scripts/engine-zest.py: 85/85 OK, fail=0 (fixes x01-block-multi, x01-dca-unlim, x02-unlim-stack, block-unlim-stack, dca-unlim-engine).
2. All server/pulse/*.py compile with py_compile.
3. node scripts/overall-zest.mjs passes.
4. npm run typecheck passes.
5. npm test passes.
6. universe.json empty file must not crash the engine.
