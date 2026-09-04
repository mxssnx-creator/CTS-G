import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import {
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

const ROOT = new URL("..", import.meta.url).pathname;

test("host retention keeps the newest 1000 diagnostics and never traverses state", () => {
  const root = mkdtempSync(join(tmpdir(), "cts-g-retention-"));
  const hostLog = join(root, "var/log/host.log");
  const runtimeLog = join(root, "var/lib/cts/instances/blue/logs/pulse.log");
  const stateLog = join(root, "var/lib/cts/instances/blue/data/trading.log");
  const linkedLog = join(root, "var/log/state-link.log");
  const lines = Array.from({ length: 1500 }, (_, index) => `line-${String(index + 1).padStart(4, "0")}`);
  const state = "authoritative-state\n";
  try {
    mkdirSync(join(root, "var/log"), { recursive: true });
    mkdirSync(join(root, "var/lib/cts/instances/blue/logs"), { recursive: true });
    mkdirSync(join(root, "var/lib/cts/instances/blue/data"), { recursive: true });
    writeFileSync(hostLog, `${lines.join("\n")}\n`);
    writeFileSync(runtimeLog, `${lines.join("\n")}\n`);
    writeFileSync(stateLog, state);
    symlinkSync(stateLog, linkedLog);

    execFileSync("bash", [join(ROOT, "deploy/limit-runtime-logs.sh")], {
      cwd: ROOT,
      env: {
        ...process.env,
        CTS_LOG_SCAN_ROOT: root,
        CTS_LOG_SKIP_JOURNAL: "1",
        CTS_LOG_MAX_LINES: "1000",
        CTS_LOG_MAX_BYTES: "1048576",
      },
    });

    for (const path of [hostLog, runtimeLog]) {
      const retained = readFileSync(path, "utf8").trimEnd().split("\n");
      assert.equal(retained.length, 1000);
      assert.equal(retained[0], "line-0501");
      assert.equal(retained.at(-1), "line-1500");
    }
    assert.equal(readFileSync(stateLog, "utf8"), state);
    assert.equal(lstatSync(linkedLog).isSymbolicLink(), true);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("Python logging and Redis commands are instance scoped and bounded", () => {
  const root = mkdtempSync(join(tmpdir(), "cts-g-python-retention-"));
  const data = join(root, "data");
  const logs = join(root, "logs");
  try {
    const program = String.raw`
import json
from storage_paths import append_log, log_path, redis_cli_args
p = log_path("runtime.log")
for i in range(1500):
    append_log(p, f"row-{i + 1:04d}")
with open(p, encoding="utf-8") as handle:
    rows = handle.read().splitlines()
print(json.dumps({"count": len(rows), "first": rows[0], "last": rows[-1], "redis": redis_cli_args("PING")}))
`;
    const result = spawnSync("python3", ["-c", program], {
      cwd: join(ROOT, "server/pulse"),
      encoding: "utf8",
      env: {
        ...process.env,
        CTS_G_NAME: "cts-g-blue",
        CTS_DATA_DIR: data,
        CTS_LOG_DIR: logs,
        CTS_REDIS_DB: "7",
        CTS_LOG_MAX_LINES: "1000",
        CTS_LOG_MAX_BYTES: "1048576",
      },
    });
    assert.equal(result.status, 0, result.stderr);
    const parsed = JSON.parse(result.stdout);
    assert.deepEqual(parsed, {
      count: 1000,
      first: "row-0501",
      last: "row-1500",
      redis: ["redis-cli", "-n", "7", "PING"],
    });
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("Linux installer uses independent names, state, ports, units and no-eviction Redis", () => {
  const common = readFileSync(join(ROOT, "deploy/linux-common.sh"), "utf8");
  const install = readFileSync(join(ROOT, "deploy/install-linux.sh"), "utf8");
  const update = readFileSync(join(ROOT, "deploy/update-linux.sh"), "utf8");
  const remote = readFileSync(join(ROOT, "deploy/remote-install.sh"), "utf8");
  const desk = readFileSync(join(ROOT, "deploy/cts-g-desk.sh"), "utf8");
  assert.match(common, /\/var\/lib\/cts\/instances\/\$\{CTS_G_NAME\}/);
  assert.match(common, /\/opt\/\$\{CTS_G_NAME\}-pulse/);
  assert.match(common, /pulse_template_unit/);
  assert.match(common, /CTS_REDIS_DB/);
  assert.match(common, /maxmemory-policy noeviction/);
  assert.doesNotMatch(common, /maxmemory-policy volatile-lru/);
  assert.match(common, /cts-log-retention\.timer/);
  assert.match(install, /--pulse-port/);
  assert.match(install, /--redis-db/);
  assert.match(update, /quiesce_instance/);
  assert.match(remote, /cts-g-stage/);
  assert.doesNotMatch(remote, /reset --hard/);
  assert.doesNotMatch(desk, /nohup/);
});

test("legacy env loading survives unset optional values and preserves routing", () => {
  const root = mkdtempSync(join(tmpdir(), "cts-g-env-test-"));
  try {
    const envFile = join(root, "cts-g.env");
    writeFileSync(envFile, "PORT=3302\nCUSTOM_SENTINEL=preserve-me\n");
    const script = `source deploy/linux-common.sh
load_existing_env_config
test "$DESK_PORT" = 3302
test "$PULSE_PORT" = 3015
test "$CTS_DATA_DIR" = "$STATE_DIR/data"
printf ok`;
    const result = spawnSync("bash", ["-c", script], {
      cwd: ROOT, encoding: "utf8", env: { ...process.env, ENV_FILE: envFile },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, "ok");
    const named = spawnSync("bash", ["-c", 'source deploy/linux-common.sh; STATE_DIR=/var/lib/cts/custom-blue; STATE_EXPLICIT=1; apply_name blue; test "$STATE_DIR" = /var/lib/cts/custom-blue; test "$CTS_DATA_DIR" = /var/lib/cts/custom-blue/data'], { cwd: ROOT });
    assert.equal(named.status, 0, named.stderr.toString());
    const invalid = spawnSync("bash", ["-c", 'source deploy/linux-common.sh; apply_name ".."'], { cwd: ROOT });
    assert.notEqual(invalid.status, 0);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("durable storage isolates instances and survives concurrent log/JSON writers", () => {
  const root = mkdtempSync(join(tmpdir(), "cts-g-race-test-"));
  try {
    const program = String.raw`
import concurrent.futures, json, os
from pathlib import Path
from storage_paths import DATA_DIR, LEGACY_DIRS, atomic_write, append_log, log_path, retain_last_lines
assert not LEGACY_DIRS
assert not list(DATA_DIR.glob("STOP*"))
p = Path(log_path("race.log"))
append_log(str(p), "start")
inode = p.stat().st_ino
def writer(i):
    append_log(str(p), f"row-{i}")
    atomic_write(str(DATA_DIR / "atomic.json"), {"n": i})
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    list(pool.map(writer, range(1600)))
assert p.stat().st_ino == inode
assert len(p.read_text().splitlines()) == 1000
assert isinstance(json.loads((DATA_DIR / "atomic.json").read_text())["n"], int)
with p.open("a") as open_writer:
    retain_last_lines(str(p), max_lines=10)
    open_writer.write("attached-writer\n")
assert p.read_text().endswith("attached-writer\n")
print("ok")
`;
    const result = spawnSync("python3", ["-c", program], {
      cwd: join(ROOT, "server/pulse"), encoding: "utf8",
      env: { ...process.env, CTS_DATA_DIR: join(root, "data"), CTS_LOG_DIR: join(root, "logs"), CTS_G_NAME: "cts-g-isolation" },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout.trim(), "ok");
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("stop intent is kept across installs and healing requires explicit run intent", () => {
  const common = readFileSync(join(ROOT, "deploy/linux-common.sh"), "utf8");
  const http = readFileSync(join(ROOT, "server/pulse/pulse_http.py"), "utf8");
  const install = readFileSync(join(ROOT, "deploy/install-linux.sh"), "utf8");
  assert.ok(install.indexOf("create_verified_backup\n") < install.indexOf("migrate_legacy_redis_state\n"));
  assert.match(common, /STOP-bingx-x01 STOP-bingx-x02/);
  assert.match(http, /if not os\.path\.exists\(os\.path\.join\(DIR, f"RUN-\{cid\}"\)\)/);
  assert.doesNotMatch(http, /return super\(\)\.do_GET/);
  assert.match(http, /n > 1048576/);
  assert.doesNotMatch(common, /redis\.call\("KEYS"/);
});

test("connection cards and engines recover scoped credentials without exposing secrets", () => {
  const root = mkdtempSync(join(tmpdir(), "cts-credentials-test-"));
  try {
    const code = String.raw`
import json, os, subprocess
from pathlib import Path
import pulse_http as h
def unavailable(*args, **kwargs):
    raise FileNotFoundError("offline fixture")
h.subprocess.run = unavailable
cid = "bingx-x02"
os.environ["CTS_BINGX_X02_API_KEY"] = "fixture-key"
os.environ["CTS_BINGX_X02_API_SECRET"] = "fixture-secret"
pub = h.connection_public(cid)
assert pub["apiKeySet"] and pub["apiSecretSet"]
assert "fixture-secret" not in json.dumps(pub)
assert not h.connection_public("bingx-x01")["apiKeySet"]
h.redis_hset = lambda *args, **kwargs: True
ok, detail, pub = h.save_connection(cid, {"connectionMethod": "rest"})
assert ok, detail
p = Path(h.path_for("credentials-bingx-x02.json"))
assert p.stat().st_mode & 0o777 == 0o600
del os.environ["CTS_BINGX_X02_API_KEY"]
del os.environ["CTS_BINGX_X02_API_SECRET"]
assert h.connection_public(cid)["apiSecretSet"]
os.environ["PULSE_CONN"] = cid
import pulse_trader as t
assert t.redis_hget("api_key") == "fixture-key"
assert t.redis_hget("api_secret") == "fixture-secret"
print("ok")
`;
    const result = spawnSync("python3", ["-c", code], {
      cwd: join(ROOT, "server/pulse"), encoding: "utf8",
      env: { ...process.env, CTS_DATA_DIR: join(root, "data"), CTS_LOG_DIR: join(root, "logs"), CTS_G_NAME: "cts-credential-fixture" },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout.trim(), "ok");
  } finally { rmSync(root, { recursive: true, force: true }); }
});
