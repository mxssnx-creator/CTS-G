import { test } from "node:test";
import assert from "node:assert/strict";
import { createPoller } from "./poll.ts";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** A fetch that takes `ms` and records how many calls overlap. */
function slowFetch(ms: number, answer: (n: number) => number | null) {
  const state = { calls: 0, active: 0, maxActive: 0, signals: [] as AbortSignal[] };
  const fetch = async (signal: AbortSignal) => {
    state.calls += 1;
    const n = state.calls;
    state.signals.push(signal);
    state.active += 1;
    state.maxActive = Math.max(state.maxActive, state.active);
    await sleep(ms);
    state.active -= 1;
    return answer(n);
  };
  return { fetch, state };
}

test("a kick during a request never runs two requests in parallel", async () => {
  const { fetch, state } = slowFetch(30, (n) => n);
  const p = createPoller<number>({ fetch, onData: () => {}, intervalMs: 5 });
  p.start();
  await sleep(5);
  p.kick();
  p.kick();
  p.kick();
  await sleep(200);
  p.stop();
  assert.equal(state.maxActive, 1, "at most one request in flight");
  assert.ok(state.calls >= 2, "the kick produced a fresh request");
});

test("a failed response is not published: the last good value stays", async () => {
  const values: number[] = [];
  let failures = 0;
  const { fetch } = slowFetch(1, (n) => (n === 2 ? null : n * 10));
  const p = createPoller<number>({
    fetch,
    onData: (v) => values.push(v),
    intervalMs: 2,
    onFailure: (count) => {
      failures = count;
    },
  });
  p.start();
  await sleep(60);
  p.stop();
  assert.ok(values.length >= 2, "good responses were published");
  assert.ok(!values.includes(0), "a null response never reached the view");
  assert.equal(failures, 1, "the failure was counted once");
});

test("stop aborts the request in flight and drops its late result", async () => {
  const published: number[] = [];
  const { fetch, state } = slowFetch(40, (n) => n);
  const p = createPoller<number>({ fetch, onData: (v) => published.push(v), intervalMs: 2 });
  p.start();
  await sleep(5);
  p.stop();
  await sleep(80);
  assert.equal(published.length, 0, "no value is published after stop");
  assert.equal(state.signals[0]?.aborted, true, "the request in flight was aborted");
  const before = state.calls;
  await sleep(40);
  assert.equal(state.calls, before, "no new request starts after stop");
});

test("start twice does not start a second chain", async () => {
  const { fetch, state } = slowFetch(10, (n) => n);
  const p = createPoller<number>({ fetch, onData: () => {}, intervalMs: 5 });
  p.start();
  p.start();
  await sleep(25);
  p.stop();
  assert.equal(state.maxActive, 1, "one chain, one request in flight");
});

test("a request that throws counts as a failure, not as a value", async () => {
  const values: number[] = [];
  const p = createPoller<number>({
    fetch: async () => {
      throw new Error("network down");
    },
    onData: (v) => values.push(v),
    intervalMs: 2,
  });
  p.start();
  await sleep(20);
  p.stop();
  assert.equal(values.length, 0);
});

test("a poll that started before a save may not overwrite it", async () => {
  const { createSaveGate } = await import("./poll.ts");
  const gate = createSaveGate();
  const beforeSave = gate.snapshot();
  gate.mark();                           // the save starts
  assert.equal(gate.fresh(beforeSave), false, "the stale poll is discarded");
  const afterSave = gate.snapshot();
  assert.equal(gate.fresh(afterSave), true, "a poll that starts after the save may apply");
});
