#!/usr/bin/env node
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";

const BASE = process.env.TEST_BASE || "http://127.0.0.1:8080";
const outDir = "/workspace/screenshots";
mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch({
  executablePath: process.env.CTS_CHROMIUM_PATH || undefined,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
page.setDefaultTimeout(20000);
await page.route("**/grok-app-builder/extensions.js", (route) => route.abort());
await page.route("https://fonts.googleapis.com/**", (route) => route.abort());

const errors = [];
const consoleErr = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => {
  if (m.type() !== "error") return;
  if (/Failed to load resource: net::ERR_FAILED/.test(m.text())) return;
  if (/status of 503/.test(m.text())) return;
  consoleErr.push(m.text());
});

async function waitHydrated() {
  await page.waitForFunction(() => !window.$_TSR || window.$_TSR.hydrated === true, null, { timeout: 30000 });
}

const report = [];
const rec = (ok, msg, extra) => {
  report.push({ ok: Boolean(ok), msg, extra: extra || null });
  console.log((ok ? "OK  " : "FAIL") + " " + msg);
};

async function snapshot(path) {
  await page.goto(BASE + path, { waitUntil: "domcontentloaded" });
  await waitHydrated();
  await page.waitForSelector("[data-testid=desk-root]", { timeout: 15000 });
  await page.waitForTimeout(1400);
  const body = await page.locator("main").innerText();
  const title = (await page.locator("h1").innerText().catch(() => "")).trim();
  const shotName = path === "/" ? "-desk" : path.replaceAll("/", "-");
  const shot = `${outDir}/complete${shotName}.png`;
  await page.screenshot({ path: shot, fullPage: false, timeout: 8000 }).catch(() => {});
  return { body, title, shot, path };
}

try {
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.evaluate(() => {
    localStorage.removeItem("x01-pulse-overlay");
    localStorage.setItem("pulse.connType", "overall");
  });

  const desk = await snapshot("/");
  rec(Boolean(desk.title), `desk title ${desk.title}`);
  rec(/coverage · px/i.test(desk.body), "desk coverage strip");
  rec(/Open book|Scanning/i.test(desk.body), "desk open book");
  rec(/Last 15 cost PF|Profit factor/i.test(desk.body), "desk stats overview");
  rec(/indications|general|block/i.test(desk.body), "desk strategy packs");
  rec(/engine-start/.test(await page.content()) || Boolean(await page.getByTestId("engine-start").count()), "desk engine start");
  rec(await page.getByTestId("engine-pause").count(), "desk engine pause");
  rec(await page.getByTestId("engine-stop").count(), "desk engine stop");
  rec(await page.getByTestId("conn-overall").count() && await page.getByTestId("conn-live").count() && await page.getByTestId("conn-vst").count(), "desk conn switch");
  rec(!/NaN|undefined|\[object Object\]/.test(desk.body), "desk no NaN/undefined");

  await page.getByTestId("conn-live").click();
  await page.waitForTimeout(900);
  const liveBody = await page.locator("main").innerText();
  rec(/Live|USDT|bingx-x01|HALTED|PAUSED|RUNNING/i.test(liveBody), "desk live view");
  await page.getByTestId("conn-vst").click();
  await page.waitForTimeout(900);
  const vstBody = await page.locator("main").innerText();
  rec(/VST|demo|X02/i.test(vstBody), "desk vst view");
  await page.getByTestId("conn-overall").click();
  await page.waitForTimeout(600);

  const results = await snapshot("/results");
  rec(/Results|Closed|Profit/i.test(results.title + results.body), "results heading");
  rec(await page.getByTestId("results-identity").count(), "results identity");
  rec(/Last 15 cost PF/i.test(results.body), "results PF hero");
  rec(/Evaluation windows/i.test(results.body), "results evaluation windows");
  rec(await page.getByTestId("results-tab-overview").count(), "results overview tab");
  rec(await page.getByTestId("results-tab-coverage").count(), "results coverage tab");
  rec(await page.getByTestId("results-tab-strategies").count(), "results strategies tab");
  rec(await page.getByTestId("results-tab-indications").count(), "results indications tab");
  rec(/Trades/i.test(results.body), "results trades hero");
  rec(!/NaN|undefined|\[object Object\]/.test(results.body), "results no NaN");

  for (const tab of ["coverage", "indications", "strategies", "sets", "tests", "controls", "errors"]) {
    const before = errors.length;
    await page.getByTestId(`results-tab-${tab}`).click();
    await page.waitForTimeout(500);
    rec(errors.length === before, `results tab ${tab} no pageerror`, errors.slice(before));
  }
  await page.getByTestId("results-tab-strategies").click();
  await page.waitForTimeout(700);
  const stratTxt = await page.locator("main").innerText();
  rec(/Block|DCA|Strategy|PF families|trailing/i.test(stratTxt), "results strategies content");

  const settings = await snapshot("/settings");
  rec(/Settings/i.test(settings.title), "settings title");
  rec(await page.getByTestId("section-overview").count(), "settings overview section");
  rec(await page.getByTestId("section-historic").count(), "settings historic section");
  rec(await page.getByTestId("test-historic").count() || /Test Historic/i.test(settings.body), "settings test historic present");
  rec(await page.getByTestId("hist-test-start").count(), "settings hist start");
  rec(await page.getByTestId("hist-test-pause").count(), "settings hist pause");
  rec(await page.getByTestId("hist-test-stop").count(), "settings hist stop");
  const histStatus = await page.getByTestId("hist-test-status").innerText().catch(() => "");
  rec(!/ready 0%/i.test(histStatus), "settings hist status not 0% when ready", histStatus.slice(0, 160));

  const sections = await page.locator("[data-testid^=section-]").all();
  rec(sections.length >= 8, `settings sections ${sections.length}`);
  for (const s of sections) {
    const before = errors.length;
    await s.click();
    await page.waitForTimeout(80);
    rec(errors.length === before, `settings ${(await s.getAttribute("data-testid"))} no crash`);
  }

  const sweep = await snapshot("/step-sweep");
  rec(/Historic test/i.test(sweep.body), "sweep historic title");
  rec(await page.getByTestId("hist-test-start").count(), "sweep hist start");
  rec(await page.getByTestId("hist-test-pause").count(), "sweep hist pause");
  rec(await page.getByTestId("hist-test-stop").count(), "sweep hist stop");
  rec(await page.getByTestId("sweep-coverage").count(), "sweep coverage cards");
  rec(/Symbols|Sets|Evals/i.test(sweep.body), "sweep coverage labels");
  rec(!/ready 0%/i.test(sweep.body), "sweep not stuck at 0%", (await page.getByTestId("hist-test-status").innerText().catch(() => "")).slice(0, 160));
  rec(/100%|ready|positive/i.test(sweep.body), "sweep shows completed progress");
  rec(!/NaN|undefined|\[object Object\]/.test(sweep.body), "sweep no NaN");

  const system = await snapshot("/system");
  rec(/System|module/i.test(system.title + system.body), "system page");
  rec(/Risk|Execution|Desk/i.test(system.body), "system module groups");
  rec(!/NaN|undefined|\[object Object\]/.test(system.body), "system no NaN");

  const fatal = errors.filter((e) => !/502|Hydration|Failed to fetch|AbortError/i.test(e));
  rec(fatal.length === 0, "no fatal pageerror", fatal.slice(0, 8));
  const consoleFatal = consoleErr.filter((e) => !/502|Hydration|Failed to fetch|AbortError|net::ERR/i.test(e));
  rec(consoleFatal.length === 0, "no fatal console error", consoleFatal.slice(0, 8));
} catch (e) {
  rec(false, "throw " + e.message);
} finally {
  await browser.close();
  const fails = report.filter((r) => !r.ok);
  writeFileSync("/tmp/ui-complete-site.json", JSON.stringify({ report, errors, consoleErr: consoleErr.slice(0, 40) }, null, 2));
  console.log(`\n${report.filter((r) => r.ok).length}/${report.length} ok  fail=${fails.length}`);
  if (fails.length) {
    for (const f of fails) console.log("FAIL DETAIL", f.msg, f.extra ? JSON.stringify(f.extra).slice(0, 400) : "");
  }
  process.exit(fails.length ? 1 : 0);
}
