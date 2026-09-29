#!/usr/bin/env node
// Browser check for Settings · Test Historic auto-assign of validated symbols
// and configs. The Test Historic and lane config endpoints are mocked so a
// finished result is guaranteed; nothing here touches a running engine.
//   TEST_BASE=http://127.0.0.1:8080 node scripts/hist-auto-assign-check.mjs
import { chromium } from "playwright";

const BASE = process.env.TEST_BASE || "http://127.0.0.1:8080";
const CONN_KEY = process.env.CTS_CONN_KEY || "pulse.connType";
const out = [];
const ok = (m) => out.push("OK " + m);
const bad = (m) => out.push("FAIL " + m);
const check = (cond, m) => (cond ? ok(m) : bad(m));

const READY = {
  phase: "ready",
  pct: 100,
  ready: true,
  detail: "",
  internSymbols: ["BTC-USDT", "ETH-USDT", "SOL-USDT", "NOTAMAJOR-USDT"],
  successfulConfigs: [
    { setId: "indications:1m:sl0.6:st8", validated: true, pf: 1.31, n: 41 },
    { setId: "general:1m:sl0.6:st4", validated: true, pf: 1.44, n: 28 },
    { setId: "indications:1m:sl1.0:st3", validated: false, pf: 2.5, n: 99 },
  ],
  validatedIds: ["indications:1m:sl0.6:st8", "general:1m:sl0.6:st4"],
};

async function session({ overlay, autoAssign = true, holdResult = false }) {
  const browser = await chromium.launch({
    executablePath: process.env.CTS_CHROMIUM_PATH || undefined,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  await ctx.addInitScript(([key]) => {
    try { localStorage.setItem(key, "live"); } catch { /* ignore */ }
  }, [CONN_KEY]);
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);
  await page.route("**/grok-app-builder/extensions.js", (r) => r.abort());
  await page.route("https://fonts.googleapis.com/**", (r) => r.abort());
  const saves = [];
  const state = { ready: !holdResult };
  await page.route("**/hist-test.json", (route) =>
    route.fulfill({ json: state.ready ? READY : { phase: "idle", pct: 0, detail: "" } }),
  );
  await page.route("**/config.json?conn=*", async (route) => {
    const req = route.request();
    if (req.method() === "POST") {
      const body = JSON.parse(req.postData() || "{}");
      saves.push(body.overlay || {});
      return route.fulfill({ json: { ok: true, detail: "saved", overlay: body.overlay, conn: "live" } });
    }
    return route.fulfill({ json: { cts: {}, overlay: { ...overlay, histTestAutoAssign: autoAssign }, conn: "live" } });
  });
  const configLoaded = page.waitForResponse(
    (r) => /\/config\.json\?conn=live/.test(r.url()) && r.request().method() === "GET",
    { timeout: 30000 },
  );
  await page.goto(`${BASE}/settings`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="test-historic"]');
  await page.waitForFunction(() => !window.$_TSR || window.$_TSR.hydrated === true, null, { timeout: 30000 });
  await configLoaded;
  await page.waitForTimeout(400); // let the loaded lane overlay reach the form
  return { browser, page, saves, state };
}

async function settle(page, ms = 2500) {
  await page.waitForTimeout(ms);
}

// 1) Explicit list + auto-assign ON: validated majors replace the list and it saves itself.
{
  const { browser, page, saves } = await session({ overlay: { symbols: ["XRP-USDT"], symbolsAll: false, symbolCap: 50 } });
  await page.waitForSelector('[data-testid="hist-test-assigned"]');
  await settle(page);
  const assigned = await page.textContent('[data-testid="hist-test-assigned"]');
  check(/Assigned symbols · 3/.test(assigned || ""), "assigned panel counts 3 validated majors (non-major dropped)");
  check(!/NOTAMAJOR/.test(assigned || ""), "non-major symbol is not assigned");
  check(/Assigned configs · 2/.test(assigned || ""), "assigned panel counts 2 validated configs (rejected one dropped)");
  const cfg = await page.textContent('[data-testid="hist-test-assigned-configs"]');
  check((cfg || "").indexOf("general:1m:sl0.6:st4") < (cfg || "").indexOf("indications:1m:sl0.6:st8"), "configs listed best PF first");
  check(saves.length === 1, `auto-saved exactly once (${saves.length})`);
  const saved = saves[0] || {};
  check(JSON.stringify(saved.symbols) === JSON.stringify(["BTC-USDT", "ETH-USDT", "SOL-USDT"]), "saved selection is the validated majors");
  check(saved.symbolsAll === false, "saved selection is not a wildcard");
  check(saved.histTestAutoAssign === true, "auto-assign flag persists ON");
  await settle(page, 3500);
  check(saves.length === 1, `no repeat save for the same result (${saves.length})`);
  await browser.close();
}

// 2) Auto-assign OFF: nothing is assigned or saved, the panel still shows the result.
{
  const { browser, page, saves } = await session({ overlay: { symbols: ["XRP-USDT"], symbolsAll: false, symbolCap: 50 }, autoAssign: false });
  await page.waitForSelector('[data-testid="hist-test-assigned"]');
  await settle(page);
  check(saves.length === 0, "auto-assign OFF never saves");
  const assigned = await page.textContent('[data-testid="hist-test-assigned"]');
  check(/selection differs · auto-assign off/.test(assigned || ""), "panel says the selection differs while OFF");
  await browser.close();
}

// 3) Wildcard book: the selection is left alone.
{
  const { browser, page, saves } = await session({ overlay: { symbols: ["*"], symbolsAll: true, symbolCap: 50 } });
  await page.waitForSelector('[data-testid="hist-test-assigned"]');
  await settle(page);
  check(saves.length === 0, "wildcard selection is never replaced");
  const assigned = await page.textContent('[data-testid="hist-test-assigned"]');
  check(/wildcard book/.test(assigned || ""), "panel explains the wildcard book");
  await browser.close();
}

// 5) Unsaved edits present when a result arrives: assign into the form, never auto-save them.
{
  const { browser, page, saves, state } = await session({
    overlay: { symbols: ["XRP-USDT"], symbolsAll: false, symbolCap: 50 },
    holdResult: true,
  });
  await page.locator('[data-testid="hist-test-hours"]').evaluate((el) => {
    const input = el.matches("input") ? el : el.querySelector("input");
    const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    set.call(input, "30");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await page.waitForFunction(
    () => /Unsaved overlay/.test(document.querySelector('[data-testid="save-status"]')?.textContent || ""),
    null,
    { timeout: 10000 },
  );
  state.ready = true;
  await page.waitForSelector('[data-testid="hist-test-assigned"]', { timeout: 20000 });
  await settle(page, 3500);
  check(saves.length === 0, `edits in progress are never auto-saved (${saves.length})`);
  const assigned = await page.textContent('[data-testid="hist-test-assigned"]');
  check(/in form · unsaved, save Live or VST to persist/.test(assigned || ""), "panel says the assignment waits for Save" + (/in form · unsaved/.test(assigned || "") ? "" : ` (panel read: ${(assigned || "").slice(0, 120)})`));
  check(/Unsaved overlay/.test((await page.textContent('[data-testid="save-status"]')) || ""), "status line still reads Unsaved overlay");
  await browser.close();
}

// 4) Toggle is present and reflects state.
{
  const { browser, page } = await session({ overlay: { symbols: ["BTC-USDT", "ETH-USDT", "SOL-USDT"], symbolsAll: false, symbolCap: 50 } });
  check((await page.locator('[data-testid="hist-test-auto-assign"]').count()) > 0, "auto-assign toggle is rendered");
  await settle(page);
  const assigned = await page.textContent('[data-testid="hist-test-assigned"]');
  check(/in selection/.test(assigned || ""), "panel reports the selection already matches");
  await browser.close();
}

console.log(out.join("\n"));
process.exit(out.some((l) => l.startsWith("FAIL")) ? 1 : 0);
