import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, readFileSync, writeFileSync, rmSync, mkdirSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import ts from 'typescript';

const root = fileURLToPath(new URL('../', import.meta.url));
const require = createRequire(import.meta.url);

// Execute the real DB bootstrap in separate processes. Only Vite's migration
// glob is replaced with a deterministic fixture; PGLite and all DB code are real.
function fixture(dir) {
  let source = readFileSync(join(root, 'src/lib/db.ts'), 'utf8');
  source = source.replace('"../../scripts/migration-plan.mjs"', JSON.stringify(pathToFileURL(join(root, 'scripts/migration-plan.mjs')).href));
  source = source.replace('"../../scripts/embedded-db-path.mjs"', JSON.stringify(pathToFileURL(join(root, 'scripts/embedded-db-path.mjs')).href));
  source = source.replaceAll('import("@electric-sql/pglite")', `import(${JSON.stringify(pathToFileURL(require.resolve('@electric-sql/pglite')).href)})`);
  const pattern = /import\.meta\.glob\("\/migrations\/\*\.sql",\s*\{[\s\S]*?\}\)/;
  assert.match(source, pattern);
  source = source.replace(pattern, JSON.stringify({ '/migrations/0002_restart.sql': 'create table restart_evidence (value text not null)' }));
  const target = join(dir, 'db.mjs');
  writeFileSync(target, ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText);
  return target;
}
function run(target, env = {}, value = '', expected = 0) {
  const script = `import {getSql,getPglite} from ${JSON.stringify(pathToFileURL(target).href)};
    const [sql,again] = await Promise.all([getSql(),getSql()]);
    if(sql !== again) throw new Error('bootstrap was not shared');
    const before=await sql.query('select value from restart_evidence order by value');
    if(${JSON.stringify(value)}) await sql.query('insert into restart_evidence(value) values ($1)', [${JSON.stringify(value)}]);
    const migrations=await sql.query('select name from _migrations');
    await (await getPglite()).close();
    console.log(JSON.stringify({before,migrations}));`;
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', script], {
    env: { PATH: process.env.PATH, ...env }, encoding: 'utf8', timeout: 30000,
  });
  assert.equal(result.status, expected, result.stderr || result.error?.message);
  return expected === 0 ? JSON.parse(result.stdout.trim()) : result;
}

test('actual desk DB retains exact data and migration identity after restart; instances stay separate', () => {
  const dir = mkdtempSync(join(tmpdir(), 'cts-g-db-'));
  try {
    const target = fixture(dir), a = join(dir, 'instance a', 'db'), b = join(dir, 'instance b', 'db');
    const envA = { CTS_DB_DIR: a, CTS_REQUIRE_PERSISTENT_DB: '1' };
    assert.deepEqual(run(target, envA, 'loss:-0.0665167').before, []);
    assert.equal(statSync(a).mode & 0o777, 0o700);
    assert.deepEqual(run(target, { CTS_STATE_DIR: join(dir, 'instance b') }, 'other-instance').before, []);
    const reopened = run(target, envA);
    assert.deepEqual(reopened.before, [{ value: 'loss:-0.0665167' }]);
    assert.deepEqual(reopened.migrations, [{ name: '0002_restart.sql' }]);
    assert.deepEqual(run(target, { CTS_DB_DIR: b }).before, [{ value: 'other-instance' }]);
    assert.deepEqual(run(target, { CTS_DB_DIR: a, CTS_STATE_DIR: join(dir, 'unused') }).before, reopened.before);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('unconfigured preview stays in memory; invalid configured persistence never falls back', () => {
  const dir = mkdtempSync(join(tmpdir(), 'cts-g-db-invalid-'));
  try {
    const target = fixture(dir);
    run(target, {}, 'preview-only');
    assert.deepEqual(run(target).before, []);
    for (const env of [{ CTS_DB_DIR: 'relative/db' }, { CTS_DB_DIR: 'memory://' }, { CTS_REQUIRE_PERSISTENT_DB: '1' }]) run(target, env, '', 1);
    const blocked = join(dir, 'not-a-directory');
    writeFileSync(blocked, 'preserve');
    run(target, { CTS_DB_DIR: blocked }, '', 1);
    assert.equal(readFileSync(blocked, 'utf8'), 'preserve');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('an acknowledged DB write survives a killed writer without a clean close', () => {
  const dir = mkdtempSync(join(tmpdir(), 'cts-g-db-killed-'));
  try {
    const target = fixture(dir), env = { CTS_DB_DIR: join(dir, 'state', 'db') };
    const code = `import {getSql} from ${JSON.stringify(pathToFileURL(target).href)};
      const sql=await getSql();
      await sql.query('insert into restart_evidence(value) values ($1)', ['acknowledged-before-kill']);
      process.stdout.write('committed\\n', () => process.kill(process.pid, 'SIGKILL'));`;
    const killed = spawnSync(process.execPath, ['--input-type=module', '-e', code], {
      env: { PATH: process.env.PATH, ...env }, encoding: 'utf8', timeout: 30000,
    });
    assert.equal(killed.signal, 'SIGKILL', killed.stderr);
    assert.equal(killed.stdout, 'committed\n');
    assert.deepEqual(run(target, env).before, [{ value: 'acknowledged-before-kill' }]);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

function shell(script, env = {}) {
  return spawnSync('bash', ['--noprofile', '--norc', '-c', `source "$1/deploy/linux-common.sh"\n${script}`, 'test', root], {
    env: { PATH: process.env.PATH, ...env }, encoding: 'utf8', timeout: 5000,
  });
}

test('installer assigns distinct durable desk paths and preserves existing DB configuration', () => {
  const dir = mkdtempSync(join(tmpdir(), 'cts-g-db-env-'));
  try {
    const result = shell(`apply_name cts-g-a\nprintf '%s\\n' "$CTS_DB_DIR"\napply_name cts-g-b\nprintf '%s\\n' "$CTS_DB_DIR"`);
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(result.stdout.trim().split('\n'), ['/var/lib/cts-g-a/db', '/var/lib/cts-g-b/db']);
    const custom = shell('apply_name named\nprintf "%s" "$CTS_DB_DIR"', { CTS_DB_DIR: '/srv/custom db' });
    assert.equal(custom.status, 0, custom.stderr);
    assert.equal(custom.stdout, '/srv/custom db');
    const stateOverride = shell('apply_name named\nprintf "%s" "$CTS_DB_DIR"', { CTS_STATE_DIR: '/srv/state/' });
    assert.equal(stateOverride.status, 0, stateOverride.stderr);
    assert.equal(stateOverride.stdout, '/srv/state/db');
    for (const [name, initial, expected] of [
      ['fresh', '', 'CTS_DB_DIR=/var/lib/cts-g/db'],
      ['custom', 'CTS_DB_DIR=/srv/my db\nCUSTOM=keep\n', 'CTS_DB_DIR=/srv/my db'],
      ['state', 'CTS_STATE_DIR=/srv/state\nCUSTOM=keep\n', 'CTS_STATE_DIR=/srv/state'],
    ]) {
      const etc = join(dir, name); mkdirSync(etc);
      const file = join(etc, 'cts-g.env'); if (initial) writeFileSync(file, initial);
      const run = shell('seed_env\nseed_env', { ETC_DIR: etc, ENV_FILE: file });
      assert.equal(run.status, 0, run.stderr);
      const output = readFileSync(file, 'utf8');
      assert.ok(output.includes(expected), output);
      assert.equal(output.match(/^CTS_REQUIRE_PERSISTENT_DB=1$/gm)?.length, 1);
      if (initial) assert.ok(output.startsWith(initial));
      if (name === 'state') assert.doesNotMatch(output, /^CTS_DB_DIR=/m);
    }
    for (const name of ['..', '../bad', 'bad name', '-bad']) assert.equal(shell('apply_name "$NAME"', { NAME: name }).status, 1);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
