#!/usr/bin/env node
// Browser check: the number boxes in Settings can be typed into.
// They used to clamp on every keystroke, so "24" became 44, "1.2" became 1.02
// and "3600" became 21600. The lane config endpoint is mocked; nothing is saved.
//   TEST_BASE=http://127.0.0.1:8080 node scripts/settings-typing-check.mjs
import { chromium } from "playwright";

const BASE = process.env.TEST_BASE || "http://127.0.0.1:8080";
const CONN_KEY = process.env.CTS_CONN_KEY || "pulse.connType";
const out = [];
const check = (cond, m) => out.push((cond ? "OK " : "FAIL ") + m);

const browser = await chromium.launch({
  executablePath: process.env.CTS_CHROMIUM_PATH || undefined,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
try {
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 1000 } });
  await ctx.addInitScript(([key]) => {
    try { localStorage.setItem(key, "live"); } catch { /* ignore */ }
  }, [CONN_KEY]);
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);
  await page.route("**/grok-app-builder/extensions.js", (r) => r.abort());
  await page.route("https://fonts.googleapis.com/**", (r) => r.abort());
  await page.route("**/config.json?conn=*", (route) => {
    if (route.request().method() === "POST") return route.fulfill({ json: { ok: true, detail: "saved", conn: "live" } });
    return route.fulfill({ json: { cts: {}, overlay: {}, conn: "live" } });
  });
  await page.goto(`${BASE}/settings`, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => {
      const save = document.querySelector("[data-testid=save-overlay]");
      return save && !save.disabled;
    },
    null,
    { timeout: 30000 },
  );
  await page.waitForTimeout(800);

  const typeInto = async (section, label, text, expected) => {
    await page.locator(`[data-testid=section-${section}]`).click();
    await page.waitForTimeout(300);
    const input = page.locator("label", { hasText: label }).first().locator("input[type=number]");
    await input.click({ clickCount: 3 });
    await page.keyboard.press("Control+A");
    await page.keyboard.type(text, { delay: 80 });
    check((await input.inputValue()) === expected, `${label} keeps "${text}" while typing`);
    await page.keyboard.press("Enter");
  };
  await typeInto("overview", /^Historic time range/, "24", "24");
  await typeInto("overview", /^Min PF · test results/, "1.2", "1.2");
  await typeInto("overview", /^Step range · minimum/, "12", "12");
  await typeInto("overview", /^Set DDT maximum/, "600", "600");
  await typeInto("pulse", /^Max hold s/, "3600", "3600");

  // Out-of-range input is clamped once the box is left, not while typing.
  const hours = page.locator("label", { hasText: /^Historic time range/ }).first().locator("input[type=number]");
  await page.locator("[data-testid=section-overview]").click();
  await hours.click({ clickCount: 3 });
  await page.keyboard.press("Control+A");
  await page.keyboard.type("999", { delay: 80 });
  await hours.blur();
  await page.waitForTimeout(200);
  check((await hours.inputValue()) === "64", "an out-of-range value is clamped to the maximum on blur");
  await ctx.close();
} finally {
  await browser.close();
}
console.log(out.join("\n"));
process.exit(out.some((l) => l.startsWith("FAIL")) ? 1 : 0);
