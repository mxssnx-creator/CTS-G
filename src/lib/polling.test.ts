import assert from "node:assert/strict";
import { test } from "node:test";
import { setImmediate as flush } from "node:timers/promises";
import { startPolling } from "./polling.ts";

test("250 manual refreshes produce one in-flight request and one queued refresh", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const calls: { done: () => void; signal: AbortSignal }[] = [];
  const poll = startPolling((signal) => new Promise<void>((done) => calls.push({ done, signal })), () => 3500);
  for (let i = 0; i < 250; i++) poll.refresh();
  assert.equal(calls.length, 1);
  calls[0].done(); await flush();
  t.mock.timers.tick(0);
  assert.equal(calls.length, 2);
  calls[1].done(); await flush();
  t.mock.timers.tick(3499);
  assert.equal(calls.length, 2);
  t.mock.timers.tick(1);
  assert.equal(calls.length, 3);
  poll.stop(); calls[2].done(); await flush();
  t.mock.timers.tick(100000);
  assert.equal(calls.length, 3);
  assert.ok(calls.every((call) => call.signal.aborted));
});

test("transient rejection retries, hidden polling slows down, and stop clears its timer", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let calls = 0;
  let hidden = false;
  const poll = startPolling(async () => { calls++; if (calls === 1) throw new Error("temporary"); }, () => hidden ? 8000 : 3500);
  await flush();
  hidden = true;
  t.mock.timers.tick(3500); await flush();
  assert.equal(calls, 2);
  t.mock.timers.tick(7999); await flush();
  assert.equal(calls, 2);
  t.mock.timers.tick(1); await flush();
  assert.equal(calls, 3);
  poll.stop(); t.mock.timers.tick(80000);
  assert.equal(calls, 3);
});

test("a stalled statistics poll never holds up an independent config poll", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let configCalls = 0;
  let statsCalls = 0;
  const stats = startPolling(() => { statsCalls++; return new Promise(() => {}); }, () => 4000);
  const config = startPolling(async () => { configCalls++; }, () => 4000);
  await flush();
  for (let i = 0; i < 250; i++) { t.mock.timers.tick(4000); await flush(); }
  assert.equal(statsCalls, 1);
  assert.equal(configCalls, 251);
  stats.stop(); config.stop();
});
