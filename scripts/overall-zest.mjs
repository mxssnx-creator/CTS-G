#!/usr/bin/env node
import { chromium } from "playwright";
import { writeFileSync } from "node:fs";

const BASE = process.env.ZEST_BASE || "http://127.0.0.1:8080";
const out = [];
const ok = (m) => out.push("OK " + m);
const fail = (m) => out.push("FAIL " + m);

const browser = await chromium.launch({ args: ["--no-sandbox", "--disable-dev-shm-usage"] });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
// The platform's deferred extensions.js hangs whole page loads where grok.com
// is unreachable (CI, offline dev). The zest checks the desk, not the platform
// script, so abort it instead of letting it block DOMContentLoaded.
await page.route("**/grok-app-builder/extensions.js", (route) => route.abort());
page.setDefaultTimeout(14000);
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));

async function clickConn(id) {
  const root = page.locator("[data-testid=desk-root]");
  await root.waitFor({ timeout: 10000 });
  await page.waitForFunction(() => {
    const el = document.querySelector("[data-testid=desk-root]");
    const stored = localStorage.getItem("pulse.connType") || "overall";
    return Boolean(el) && el.getAttribute("data-conn") === stored;
  }, null, { timeout: 8000 }).catch(() => null);
  await page.waitForTimeout(150);
  const cur = await root.getAttribute("data-conn");
  if (cur === id) return;
  await page.locator(`[data-testid=conn-${id}]`).first().click({ force: true });
  await page.waitForFunction(
    (want) => document.querySelector("[data-testid=desk-root]")?.getAttribute("data-conn") === want,
    id,
    { timeout: 10000 },
  );
}

try {
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.evaluate(() => {
    localStorage.removeItem("x01-pulse-overlay");
    localStorage.setItem("pulse.connType", "overall");
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector("[data-testid=desk-root]", { timeout: 12000 });
  await page.waitForTimeout(2200);

  if (await page.locator("[data-testid=desk-root]").count()) ok("desk-root");
  else fail("no desk-root");
  if (await page.locator("[data-testid=conn-switch]").count()) ok("conn-switch");
  else fail("no conn-switch");

  await clickConn("overall");
  await page.waitForSelector("[data-testid=lane-board]", { timeout: 12000 }).catch(() => null);
  await page.waitForTimeout(400);
  const overallTxt = await page.locator("main").innerText();
  if (/Pulse desk/i.test(overallTxt)) ok("desk title");
  else fail("desk title missing");
  if (/Loading all desks/i.test(overallTxt) && !(await page.locator("[data-testid=lane-board]").count())) {
    fail("overall stuck loading");
  } else ok("overall not stuck");
  if (await page.locator("[data-testid=lane-board]").count()) ok("lane board");
  else fail("no lane board");
  if (await page.locator("[data-testid=lane-live]").count()) ok("live lane");
  else fail("no live lane");
  if (await page.locator("[data-testid=lane-vst]").count()) ok("vst lane");
  else fail("no vst lane");
  const identO = await page.locator("[data-testid=desk-identity]").innerText().catch(() => "");
  if (/overall/i.test(identO)) ok("overall identity " + identO.replace(/\s+/g, " ").slice(0, 80));
  else fail("overall identity " + identO);
  await page.screenshot({ path: "/workspace/screenshots/zest-overall.png", timeout: 5000 }).catch(() => {});

  await clickConn("vst");
  await page.waitForTimeout(900);
  const identV = await page.locator("[data-testid=desk-identity]").innerText().catch(() => "");
  if (/vst/i.test(identV) && /x02/i.test(identV)) ok("vst identity " + identV.replace(/\s+/g, " ").slice(0, 80));
  else fail("vst identity " + identV);
  const vstTxt = await page.locator("main").innerText();
  if (/Loading VST/i.test(vstTxt) && !/VST demo/i.test(identV)) fail("vst stuck loading");
  else ok("vst desk body");
  if (/Open book|Scanning|coverage/i.test(vstTxt)) ok("vst book/coverage");
  else out.push("WARN vst missing book: " + vstTxt.slice(0, 80));

  await clickConn("live");
  await page.waitForTimeout(900);
  const identL = await page.locator("[data-testid=desk-identity]").innerText().catch(() => "");
  if (/live/i.test(identL) && /x01/i.test(identL)) ok("live identity " + identL.replace(/\s+/g, " ").slice(0, 80));
  else fail("live identity " + identL);
  if (/vst/i.test(identL) && !/live/i.test(identL)) fail("live showing vst identity");
  else ok("live isolation identity");
  await page.screenshot({ path: "/workspace/screenshots/zest-live.png", timeout: 5000 }).catch(() => {});

  await page.goto(BASE + "/results", { waitUntil: "domcontentloaded" });
  await page.waitForSelector("[data-testid=desk-root]", { timeout: 10000 });
  await clickConn("vst");
  await page.waitForTimeout(700);
  const resId = await page.locator("[data-testid=results-identity]").innerText().catch(() => "");
  if (/vst|x02/i.test(resId)) ok("results vst " + resId.replace(/\s+/g, " ").slice(0, 80));
  else fail("results identity " + resId);
  const resTxt = await page.locator("main").innerText();
  if (/Download JSON/i.test(resTxt) && /PF after cost/i.test(resTxt)) ok("results export+pf");
  else fail("results missing export/pf");

  await page.goto(BASE + "/system", { waitUntil: "domcontentloaded" });
  await page.waitForSelector("[data-testid=desk-root]", { timeout: 10000 });
  await page.waitForTimeout(700);
  const sys = await page.locator("main").innerText();
  if (/Live packs/i.test(sys) && /Universe rank/i.test(sys)) ok("system universe rank module");
  else if (/Live packs/i.test(sys)) ok("system packs");
  else fail("system empty");
  if (/Indications/i.test(sys) && /Control orders/i.test(sys)) ok("system strategy/exec");
  else fail("system missing modules");

  await page.goto(BASE + "/settings", { waitUntil: "domcontentloaded" });
  await page.waitForSelector("[data-testid=desk-root]", { timeout: 10000 });
  await clickConn("vst");
  await page.waitForTimeout(800);
  const sections = [
    "overview", "connection", "profit", "risk", "trailing", "timeframes",
    "packs", "sets", "exits", "block", "dca", "axes", "volume",
    "controls", "indication", "pulse", "symbols",
  ];
  for (const id of sections) {
    const btn = page.getByTestId(`section-${id}`);
    if (!(await btn.count())) {
      fail("nav " + id);
      continue;
    }
    await btn.click();
    await page.waitForTimeout(120);
    ok("nav " + id);
  }

  await page.getByTestId("section-symbols").click();
  await page.waitForTimeout(400);
  if (await page.getByTestId("symbol-sort-vol1h").count()) ok("vol1h chip");
  else fail("no vol1h chip");
  const volOn = await page.getByTestId("symbol-sort-vol1h").getAttribute("class");
  if (/primary/.test(volOn || "")) ok("vol1h selected default");
  else fail("vol1h not selected");
  await page.getByTestId("symbol-sort-quoteVolume").click();
  await page.waitForTimeout(150);
  const qOn = await page.getByTestId("symbol-sort-quoteVolume").getAttribute("class");
  if (/primary/.test(qOn || "")) ok("quote volume selectable");
  else fail("cannot select quote volume");
  await page.getByTestId("symbol-sort-vol1h").click();
  if (await page.getByTestId("symbols-dynamic").count()) {
    const dyn = await page.getByTestId("symbols-dynamic").innerText();
    if (/ON/i.test(dyn)) ok("dynamic book on");
    else fail("dynamic off by default: " + dyn);
  } else fail("no dynamic toggle");
  const symTxt = await page.locator("main").innerText();
  if (/1H vol/i.test(symTxt) && /Max lev/i.test(symTxt) && /exchange max leverage/i.test(symTxt)) {
    ok("rank columns + hint");
  } else fail("rank table missing");
  await page.screenshot({ path: "/workspace/screenshots/zest-symbols.png", timeout: 5000 }).catch(() => {});

  await page.getByTestId("section-risk").click();
  await page.waitForSelector("[data-ratio='0.6']", { timeout: 6000 }).catch(() => null);
  if (await page.locator("[data-ratio='1.5']").count()) ok("sl:tp chips");
  else fail("no sl:tp chips");

  await page.getByTestId("section-overview").click();
  await page.waitForTimeout(300);
  const ov = await page.locator("main").innerText();
  if (/coverage/i.test(ov)) ok("overview coverage");
  else fail("overview no coverage");

  const snap = await page.evaluate(async () => {
    const r = await fetch("/live-stats.json", { cache: "no-store" });
    return { ok: r.ok, status: r.status, type: r.headers.get("content-type") };
  });
  if (snap.ok) ok("snapshot live-stats " + snap.status);
  else fail("snapshot live-stats " + snap.status);

  const page2 = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page2.route("**/grok-app-builder/extensions.js", (route) => route.abort());
  await page2.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page2.waitForTimeout(900);
  const mob = await page2.locator("main").innerText();
  const overflow = await page2.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 2);
  if (/Pulse desk|Settings|Overall/i.test(mob)) ok("mobile desk");
  else fail("mobile empty");
  if (!overflow) ok("mobile no overflow");
  else fail("mobile overflow");
  await page2.screenshot({ path: "/workspace/screenshots/zest-mobile.png", timeout: 5000 }).catch(() => {});
  await page2.close();

  const crashes = errors.filter((e) => !/502|Failed to load resource|Hydration failed/i.test(e));
  if (crashes.length) fail("pageerror " + crashes.slice(0, 2).join(" | "));
  else ok("no page crash");
} catch (e) {
  fail("throw " + e.message);
} finally {
  await browser.close();
  const fails = out.filter((l) => l.startsWith("FAIL"));
  console.log(out.join("\n"));
  console.log(`\n${out.filter((l) => l.startsWith("OK")).length}/${out.length} ok  fail=${fails.length}`);
  writeFileSync("/tmp/overall-zest.log", out.join("\n"));
  process.exit(fails.length ? 1 : 0);
}
