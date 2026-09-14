import assert from "node:assert/strict";
import { test } from "node:test";
import {
  HIST_TEST_HOURS_DEFAULT,
  HIST_TEST_HOURS_MAX,
  HIST_TEST_HOURS_MIN,
  HIST_TEST_TARGET_DEFAULT,
  clampHistTestHours,
  histTestIsPaused,
  histTestIsRunning,
  histTestLookbackBars,
  histTestStartLabel,
  histTestStatusLine,
  pauseHistTest,
  startHistTest,
  stopHistTest,
} from "./hist-test.ts";

test("historic test hours clamp 4–64 default 20", () => {
  assert.equal(clampHistTestHours(undefined), HIST_TEST_HOURS_DEFAULT);
  assert.equal(clampHistTestHours(20), 20);
  assert.equal(clampHistTestHours(3), HIST_TEST_HOURS_MIN);
  assert.equal(clampHistTestHours(80), HIST_TEST_HOURS_MAX);
  assert.equal(histTestLookbackBars(20), 1200);
  assert.equal(histTestLookbackBars(4), 240);
  assert.equal(histTestLookbackBars(64), 3840);
});

test("historic test running phases and idle copy", () => {
  for (const phase of ["queued", "rank", "evaluate", "fetch", "replay", "score", "paused"]) {
    assert.equal(histTestIsRunning(phase), true, phase);
  }
  for (const phase of ["idle", "ready", "error", "stopped", ""]) {
    assert.equal(histTestIsRunning(phase), false, phase);
  }
  assert.match(histTestStatusLine(null, 20, 1.1), /20h tape · min PF 1.10 · fill until positive/);
  assert.match(histTestStatusLine({ phase: "evaluate", pct: 22, detail: "SOL-USDT · 3/20 positive" }), /evaluate 22%/);
});

test("start pause stop match engine bar labels", () => {
  assert.equal(histTestStartLabel(null), "Start");
  assert.equal(histTestStartLabel({ phase: "idle", pct: 0, detail: "" }), "Start");
  assert.equal(histTestStartLabel({ phase: "evaluate", pct: 20, detail: "" }), "Start");
  assert.equal(histTestStartLabel({ phase: "paused", pct: 20, detail: "historic test paused", paused: true }), "Resume");
  assert.equal(histTestIsPaused({ phase: "paused", pct: 0, detail: "" }), true);
  assert.equal(histTestIsPaused({ phase: "evaluate", pct: 10, detail: "", paused: true }), true);
  assert.equal(histTestIsPaused({ phase: "evaluate", pct: 10, detail: "" }), false);
  assert.match(histTestStatusLine({ phase: "paused", pct: 40, detail: "historic test paused", paused: true }), /paused/);
});

test("start posts hours, min PF and selected count", async (t) => {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  t.mock.method(globalThis, "fetch", async (url: string | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init: init || {} });
    return new Response(JSON.stringify({ ok: true, phase: "queued", pct: 1, detail: "queued", hours: 20, minPf: 1.1, targetCount: 20 }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  const job = await startHistTest({ hours: 20, minPf: 1.1, symbolCap: 20 });
  assert.equal(job.phase, "queued");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/hist-test.json");
  const body = JSON.parse(String(calls[0].init.body));
  assert.equal(body.action, "start");
  assert.equal(body.hours, 20);
  assert.equal(body.minPf, 1.1);
  assert.equal(body.symbolCap, 20);
  assert.equal(HIST_TEST_TARGET_DEFAULT, 20);
});

test("pause and stop post the same control actions as the engine bar", async (t) => {
  const actions: string[] = [];
  t.mock.method(globalThis, "fetch", async (_url: string | URL, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body || "{}")) as { action?: string };
    actions.push(String(body.action || ""));
    const action = body.action || "start";
    const phase = action === "pause" ? "paused" : action === "stop" ? "stopped" : "queued";
    return new Response(JSON.stringify({ ok: true, phase, pct: action === "stop" ? 100 : 20, detail: action, paused: action === "pause", running: action !== "stop" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  const paused = await pauseHistTest();
  const stopped = await stopHistTest();
  const resumed = await startHistTest({ hours: 20, minPf: 1.1, symbolCap: 20, action: "resume" });
  assert.deepEqual(actions, ["pause", "stop", "resume"]);
  assert.equal(paused.phase, "paused");
  assert.equal(paused.paused, true);
  assert.equal(stopped.phase, "stopped");
  assert.equal(resumed.phase, "queued");
});

test("start treats a missing cap as the 20-symbol fill target", async (t) => {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  t.mock.method(globalThis, "fetch", async (url: string | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init: init || {} });
    return new Response(JSON.stringify({ ok: true, phase: "queued", pct: 1, detail: "queued" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  await startHistTest({ hours: 48, minPf: 1.15, symbolCap: 0 });
  const body = JSON.parse(String(calls[0].init.body));
  assert.equal(body.hours, 48);
  assert.equal(body.minPf, 1.15);
  assert.equal(body.symbolCap, HIST_TEST_TARGET_DEFAULT);
});
