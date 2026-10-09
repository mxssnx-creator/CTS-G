import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { createPoller } from "./poll.ts";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * The desk poll store against a real HTTP stub. The stub answers with a good value, a 500, a body that is not
 * JSON, and good values again, each after a delay so requests could overlap if the store let them. Only good values
 * reach the store, failures are counted, no two requests are in flight at once, and nothing lands after stop().
 */
test("the poll store against an HTTP stub: failures never reach the store, requests never overlap", async () => {
  let served = 0;
  let active = 0;
  let maxActive = 0;
  const server = createServer((_req, res) => {
    const k = served++;
    active += 1;
    maxActive = Math.max(maxActive, active);
    setTimeout(() => {
      active -= 1;
      if (k === 1) {
        res.writeHead(500, { "content-type": "text/plain" });
        res.end("boom");
        return;
      }
      if (k === 3) {
        res.writeHead(200, { "content-type": "text/plain" });
        res.end("not json");
        return;
      }
      const n = k === 0 ? 1 : k === 2 ? 3 : k;
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ n }));
    }, 15);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const { port } = server.address() as { port: number };
  const url = `http://127.0.0.1:${port}/stats`;

  const stored: number[] = [];
  const failures: number[] = [];
  const poller = createPoller<{ n: number }>({
    fetch: async (signal) => {
      const r = await fetch(url, { signal });
      if (!r.ok) return null;
      try {
        const body = await r.json();
        return typeof body?.n === "number" ? body : null;
      } catch {
        return null;
      }
    },
    onData: (v) => stored.push(v.n),
    onFailure: (count) => failures.push(count),
    intervalMs: 5,
  });

  poller.start();
  const deadline = Date.now() + 5000;
  while (served < 5 && Date.now() < deadline) await sleep(5);
  poller.stop();
  await sleep(60);                       // a request still in flight at stop() must not publish
  const atStop = stored.length;
  await sleep(80);
  server.close();

  assert.ok(served >= 5, `the stub served ${served} requests`);
  assert.equal(maxActive, 1, "two requests were in flight at once");
  assert.deepEqual(stored.slice(0, 2), [1, 3], "the 500 must not reach the store");
  assert.ok(stored.slice(2).every((n) => n >= 4), "the non-JSON body (answer 3) must not reach the store");
  assert.equal(stored.length, atStop, "a value landed after stop()");
  assert.deepEqual(failures.slice(0, 2), [1, 1], "each failed response is counted once, and the count resets on a value");
});
