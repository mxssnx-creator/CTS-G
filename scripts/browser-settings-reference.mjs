#!/usr/bin/env node
// Local browser exercise with mocked endpoints: no real account or engine is written.
import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { chromium } from "playwright";

const origin = "http://127.0.0.1:3176";
const browser = await chromium.launch({ headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
let saved = null;
const pageErrors = [];
page.on("pageerror", (e) => pageErrors.push(e.message));
await page.route("**/*", async (route) => {
  const req = route.request();
  const url = new URL(req.url());
  if (url.origin !== origin) return route.abort();
  if (url.pathname === "/config.json") {
    if (req.method() === "POST") {
      assert.equal(url.searchParams.get("conn"), "vst");
      saved = req.postDataJSON().overlay;
      return route.fulfill({ json: { ok: true, conn: "vst", overlay: saved } });
    }
    return route.fulfill({ json: { cts: {}, overlay: saved || {}, conn: "vst" } });
  }
  if (url.pathname === "/stats.json" || url.pathname === "/live-stats.json") {
    return route.fulfill({ json: { running: false, lanes: [], slots: [] } });
  }
  if (url.pathname === "/connections.json") {
    return route.fulfill({ json: { selectedDefault: "overall", types: [{ type: "vst", label: "VST" }], slots: [], lanes: [] } });
  }
  if (req.method() !== "GET") return route.abort();
  return route.continue();
});

const input = (label) => page.locator("label").filter({ hasText: label }).locator('input[type="number"]');
try {
  await page.goto(`${origin}/settings`, { waitUntil: "domcontentloaded" });
  // The server renders the buttons before React attaches their handlers.
  await page.waitForTimeout(1500);
  await page.getByTestId("conn-vst").click();
  await page.locator('[data-testid="desk-root"][data-conn="vst"]').waitFor();
  await page.getByTestId("section-sets").click();
  await page.getByTestId("save-overlay").waitFor({ state: "visible" });
  await input("Lookback bars").fill("4320");
  await input("Pre-history · max bars").fill("4320");
  await input("Pre-history · target samples per Set").fill("24");
  await page.getByTestId("section-pulse").click();
  await input("Scratch min %").fill("0.30");
  await page.getByTestId("save-overlay").click();
  await page.waitForFunction(() => document.querySelector('[data-testid="save-status"]')?.textContent?.includes("Saved vst · engine reload"));
  assert.equal(saved.histLookbackBars, 4320);
  assert.equal(saved.histLookbackMax, 4320);
  assert.equal(saved.histLookbackTargetN, 24);
  assert.equal(saved.scratchMinPct, 0.3);
  // Apply the exact captured payload to a fresh SetBook, without starting
  // Pulse or connecting to an exchange. This checks the frontend-to-engine
  // value mapping, while the HTTP transport above remains a mock.
  const loadBook = `import json,sys
sys.path.insert(0, 'server/pulse')
from set_engine import SetBook
b=SetBook(); b.load(json.load(sys.stdin))
print(json.dumps({'lookback':b.lookback,'maximum':b.lookback_max,'target':b.lookback_sample_target,'scratch':b.scratch_min}))`;
  const processResult = spawnSync("python3", ["-c", loadBook], { input: JSON.stringify(saved), encoding: "utf8", timeout: 15000 });
  assert.equal(processResult.status, 0, processResult.stderr);
  const applied = JSON.parse(processResult.stdout);
  assert.equal(applied.lookback, 4320);
  assert.equal(applied.maximum, 4320);
  assert.equal(applied.target, 24);
  assert.ok(Math.abs(applied.scratch - 0.003) < 1e-12);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1500);
  await page.locator('[data-testid="desk-root"][data-conn="vst"]').waitFor();
  await page.getByTestId("section-sets").click();
  await page.waitForFunction(() => document.querySelector('[data-testid="save-overlay"]') && !document.querySelector('[data-testid="save-overlay"]').disabled);
  assert.equal(await input("Lookback bars").inputValue(), "4320");
  assert.equal(await input("Pre-history · max bars").inputValue(), "4320");
  assert.equal(await input("Pre-history · target samples per Set").inputValue(), "24");
  await page.getByTestId("section-pulse").click();
  assert.equal(Number(await input("Scratch min %").inputValue()), 0.3);
  assert.deepEqual(pageErrors, []);
  console.log(JSON.stringify({ ok: true, connection: "vst", lookbackBars: saved.histLookbackBars,
    maxBars: saved.histLookbackMax, targetSamples: saved.histLookbackTargetN, scratchMinPct: saved.scratchMinPct,
    reloaded: true, appliedToIsolatedSetBook: applied, pageErrors,
    note: "mocked browser transport and isolated SetBook; no live Pulse API or account written" }, null, 2));
} catch (error) {
  console.error(JSON.stringify({ error: String(error), pageErrors, desk: await page.getByTestId("desk-root").getAttribute("data-conn").catch(() => null) }));
  throw error;
} finally {
  await browser.close();
}
