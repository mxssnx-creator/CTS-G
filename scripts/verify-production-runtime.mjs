/** Offline production HTTP/load/isolation check. No exchange/real Redis calls. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { PGlite } from "@electric-sql/pglite";

const root = resolve(import.meta.dirname, "..");
const state = await mkdtemp(join(tmpdir(), "cts-production-qa-"));
const children = [];
let checks = 0;
async function freePort() {
  const socket = createServer();
  socket.listen(0, "127.0.0.1");
  await once(socket, "listening");
  const port = socket.address().port;
  await new Promise((r) => socket.close(r));
  return port;
}
function start(command, args, env) {
  const child = spawn(command, args, { cwd: root, env: { PATH: process.env.PATH, ...env }, stdio: ["ignore", "pipe", "pipe"] });
  let errors = "";
  child.stdout.on("data", () => {});
  child.stderr.on("data", (b) => { errors = (errors + b).slice(-4000); });
  child.getErrors = () => errors;
  children.push(child);
  return child;
}
async function get(url, options) {
  return fetch(url, { ...options, signal: AbortSignal.timeout(12000) });
}
async function ready(url, child) {
  for (let n = 0; n < 80; n++) {
    if (child.exitCode !== null) throw new Error(`server exited: ${child.getErrors()}`);
    try { if ((await get(url)).ok) return; } catch { /* starting */ }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`server not ready: ${child.getErrors()}`);
}
const python = String.raw`
import os
import pulse_http as p
db = {"connection:bingx-x02": {"api_key": "offline-key", "api_secret": "offline-secret", "base_url": "https://open-api-vst.bingx.com"}}
p.redis_hgetall = lambda key: dict(db.get(key, {}))
def put(key, values):
    db.setdefault(key, {}).update(values)
    return True
p.redis_hset = put
p.unit_state = lambda *args, **kwargs: "active"
p._sysctl = lambda *args, **kwargs: (0, "active")
p.BoundedHTTPServer(("127.0.0.1", int(os.environ["PULSE_PORT"])), p.Handler).serve_forever()
`;
try {
  const apps = [];
  for (const [index, name] of ["blue", "green"].entries()) {
    const instance = join(state, name), data = join(instance, "data");
    await mkdir(data, { recursive: true });
    await writeFile(join(data, "overlay-bingx-x02.json"), JSON.stringify({ marker: name, slToTpRatio: 0.6 }));
    await writeFile(join(data, "stats-bingx-x02.json"), JSON.stringify({ t: Date.now() / 1000, running: true, cycle: 3 + index, mode: "VST_DEMO", open: [], closed: [], openCount: 0 }));
    await writeFile(join(data, "private-state.json"), '{"private":"do-not-serve"}');
    const pulsePort = await freePort(), deskPort = await freePort();
    const env = { CTS_G_NAME: `cts-qa-${name}`, CTS_STATE_DIR: instance, CTS_DATA_DIR: data, CTS_LOG_DIR: join(instance, "logs"), CTS_REDIS_DB: String(14 + index), PULSE_PORT: String(pulsePort), PYTHONPATH: join(root, "server/pulse"), PYTHONDONTWRITEBYTECODE: "1" };
    const pulse = start("python3", ["-u", "-c", python], env);
    await ready(`http://127.0.0.1:${pulsePort}/stats.json?conn=vst`, pulse);
    const url = `http://127.0.0.1:${deskPort}`;
    const desk = start("node", [".output/server/index.mjs"], { ...env, NODE_ENV: "production", HOST: "127.0.0.1", PORT: String(deskPort), PULSE_URL: `http://127.0.0.1:${pulsePort}` });
    await ready(url, desk);
    apps.push({ name, url, pulsePort, instance });
  }
  for (const app of apps) {
    const pulseCfg = await get(`http://127.0.0.1:${app.pulsePort}/config.json?conn=vst`);
    assert.equal(pulseCfg.status, 200, "fixture Pulse must still be alive");
    for (const path of ["/", "/settings", "/results", "/system"]) {
      const response = await get(app.url + path);
      assert.equal(response.status, 200, path);
      assert.ok((await response.text()).length > 1000); checks++;
    }
    const cfg = await (await get(app.url + "/config.json?conn=vst")).json();
    assert.equal(cfg.overlay?.marker, app.name, JSON.stringify(cfg)); checks++;
    const saved = await (await get(app.url + "/config.json?conn=vst", { method: "POST", headers: { "Content-Type": "application/json", Origin: app.url }, body: '{"slToTpRatio":0.9}' })).json();
    assert.equal(saved.ok, true); assert.equal(saved.overlay.marker, app.name); checks++;
    const conn = await (await get(app.url + "/connection.json?conn=vst", { method: "POST", headers: { "Content-Type": "application/json" }, body: '{"connectionMethod":"rest"}' })).json();
    assert.equal(conn.ok, true); assert.equal(conn.apiKeySet, true);
    assert.equal(JSON.stringify(conn).includes("offline-secret"), false); checks++;
    const stopped = await (await get(app.url + "/control.json?conn=vst", { method: "POST", headers: { "Content-Type": "application/json" }, body: '{"action":"stop"}' })).json();
    assert.equal(stopped.ok, true); checks++;
    assert.equal((await get(app.url + "/config.json?conn=vst", { method: "POST", headers: { Origin: "https://foreign.invalid", "Content-Type": "application/json" }, body: "{}" })).status, 403); checks++;
    assert.equal((await get(`http://127.0.0.1:${app.pulsePort}/private-state.json`)).status, 404); checks++;
    assert.equal((await get(`http://127.0.0.1:${app.pulsePort}/control.json`, { method: "POST", body: "[]" })).status, 400); checks++;
    assert.equal((await get(app.url + "/config.json", { method: "POST", body: "x".repeat(1_048_577) })).status, 413); checks++;
    const started = performance.now();
    for (let batch = 0; batch < 5; batch++) {
      await Promise.all(Array.from({ length: 8 }, async () => {
        const response = await get(app.url + "/stats.json?conn=vst");
        assert.equal(response.status, 200);
        const stats = await response.json();
        assert.equal(stats.connection, "bingx-x02");
        assert.equal(stats.running, false); checks++;
      }));
    }
    console.log(`${app.name}: 40 concurrent-batch reads, ${(performance.now() - started).toFixed(0)} ms, isolated state`);
  }
  const pgdir = join(state, "database");
  let pg = new PGlite({ dataDir: pgdir });
  await pg.exec("create table retained (n int); insert into retained values (42)");
  await pg.close();
  pg = new PGlite({ dataDir: pgdir });
  assert.equal((await pg.query("select n from retained")).rows[0].n, 42); checks++;
  await pg.close();
  console.log(`Production runtime: ${checks} checks passed; no exchange writes`);
} catch (error) {
  console.error(error);
  for (const child of children) if (child.getErrors()) console.error(child.getErrors());
  process.exitCode = 1;
} finally {
  for (const child of children.reverse()) {
    if (child.exitCode === null) {
      child.kill("SIGTERM");
      await Promise.race([once(child, "exit"), new Promise((r) => setTimeout(r, 3000))]);
      if (child.exitCode === null) child.kill("SIGKILL");
    }
  }
  await rm(state, { recursive: true, force: true });
}
