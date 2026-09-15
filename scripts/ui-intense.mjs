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
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
page.setDefaultTimeout(20000);
await page.route("**/grok-app-builder/extensions.js", (route) => route.abort());
await page.route("https://fonts.googleapis.com/**", (route) => route.abort());

const errors = [];
const consoleErr = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => {
  if (m.type() === "error") consoleErr.push(m.text());
});

async function waitHydrated() {
  await page.waitForFunction(() => !window.$_TSR || window.$_TSR.hydrated === true, null, { timeout: 30000 });
}

async function dumpButtons(label) {
  return page.evaluate((lab) => {
    const btns = [...document.querySelectorAll("button")];
    const interesting = btns
      .filter((b) => /start|pause|stop|resume|replay/i.test(`${b.innerText} ${b.getAttribute("data-testid") || ""} ${b.getAttribute("aria-label") || ""}`))
      .map((b) => ({
        testid: b.getAttribute("data-testid"),
        text: (b.innerText || "").replace(/\s+/g, " ").trim().slice(0, 40),
        disabled: b.disabled,
        aria: b.getAttribute("aria-label"),
      }));
    const hist = document.querySelector("[data-testid=test-historic]");
    const status = document.querySelector("[data-testid=hist-test-status]");
    const sweep = document.querySelector("[data-testid=sweep-progress]");
    return {
      label: lab,
      path: location.pathname,
      title: document.querySelector("h1")?.textContent?.trim(),
      interesting,
      histPresent: Boolean(hist),
      histText: hist ? hist.innerText.slice(0, 400) : "",
      status: status?.innerText || "",
      sweep: sweep?.innerText?.slice(0, 200) || "",
      bodyHasStart: /Start|Resume|Pause|Stop/.test(document.body.innerText),
    };
  }, label);
}

const report = [];
function rec(ok, msg, extra) {
  report.push({ ok, msg, extra: extra || null });
  console.log((ok ? "OK  " : "FAIL") + " " + msg);
}

try {
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.evaluate(() => {
    localStorage.removeItem("x01-pulse-overlay");
    localStorage.setItem("pulse.connType", "vst");
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitHydrated();
  await page.waitForSelector("[data-testid=desk-root]", { timeout: 15000 });
  await page.waitForTimeout(1500);

  const pages = ["/", "/results", "/settings", "/step-sweep", "/system"];
  for (const p of pages) {
    await page.goto(BASE + p, { waitUntil: "domcontentloaded" });
    await waitHydrated();
    await page.waitForSelector("[data-testid=desk-root]", { timeout: 15000 });
    await page.waitForTimeout(1200);
    const pageErrBefore = errors.length;
    const dump = await dumpButtons(p);
    const shot = `${outDir}/intense${p.replace("/", "-") || "-desk"}.png`;
    await page.screenshot({ path: shot, fullPage: false, timeout: 8000 }).catch(() => {});
    rec(Boolean(dump.title), `${p} title ${dump.title}`);
    rec(dump.interesting.some((b) => b.testid === "engine-start"), `${p} engine-start`);
    rec(dump.interesting.some((b) => b.testid === "engine-pause"), `${p} engine-pause`);
    rec(dump.interesting.some((b) => b.testid === "engine-stop"), `${p} engine-stop`);
    const eng = dump.interesting.filter((b) => String(b.testid || "").startsWith("engine-"));
    rec(eng.every((b) => b.disabled === false), `${p} engine buttons enabled`, eng);
    if (p === "/settings") {
      rec(dump.histPresent, "settings test-historic present");
      rec(dump.interesting.some((b) => b.testid === "hist-test-start"), "settings hist-test-start");
      rec(dump.interesting.some((b) => b.testid === "hist-test-pause"), "settings hist-test-pause");
      rec(dump.interesting.some((b) => b.testid === "hist-test-stop"), "settings hist-test-stop");
      const ht = dump.interesting.filter((b) => String(b.testid || "").startsWith("hist-test-"));
      rec(ht.filter((b) => /start|pause|stop/.test(b.testid || "")).every((b) => b.disabled === false), "settings hist-test buttons enabled", ht);
    }
    if (p === "/step-sweep") {
      rec(dump.interesting.some((b) => b.testid === "hist-test-start"), "sweep hist-test-start");
      rec(dump.interesting.some((b) => b.testid === "hist-test-pause"), "sweep hist-test-pause");
      rec(dump.interesting.some((b) => b.testid === "hist-test-stop"), "sweep hist-test-stop");
    }
    rec(errors.length === pageErrBefore, `${p} no new pageerror`, errors.slice(pageErrBefore));
    report.push({ ok: true, msg: `${p} dump`, extra: dump });
  }

  // Settings: click through sections
  await page.goto(BASE + "/settings", { waitUntil: "domcontentloaded" });
  await waitHydrated();
  await page.waitForTimeout(800);
  const sections = await page.locator("[data-testid^=section-]").all();
  rec(sections.length >= 10, `settings sections ${sections.length}`);
  for (const s of sections) {
    const id = await s.getAttribute("data-testid");
    await s.click();
    await page.waitForTimeout(80);
  }
  rec(errors.length === 0 || errors.every((e) => /502|Hydration/.test(e)), "sections no crash", errors);

  // Historic test page body
  await page.goto(BASE + "/step-sweep", { waitUntil: "domcontentloaded" });
  await waitHydrated();
  await page.waitForTimeout(1500);
  const sweepTxt = await page.locator("main").innerText();
  rec(/Historic test/i.test(sweepTxt), "sweep has Historic test title");
  rec(/Start/i.test(sweepTxt), "sweep has Start text");
  rec(/Pause/i.test(sweepTxt), "sweep has Pause text");
  rec(/Stop/i.test(sweepTxt), "sweep has Stop text");

  // Click hist-test start on settings and watch state
  await page.goto(BASE + "/settings", { waitUntil: "domcontentloaded" });
  await waitHydrated();
  await page.waitForTimeout(1000);
  const start = page.getByTestId("hist-test-start");
  const pause = page.getByTestId("hist-test-pause");
  const stop = page.getByTestId("hist-test-stop");
  rec(await start.count(), "can find hist-test-start");
  rec(!(await start.isDisabled()), "start enabled idle/ready");
  rec(!(await pause.isDisabled()), "pause enabled idle/ready");
  rec(!(await stop.isDisabled()), "stop enabled idle/ready");
  const startLabel0 = (await start.innerText()).replace(/\s+/g, " ").trim();
  rec(/Start|Resume/i.test(startLabel0), "start label " + startLabel0);

  // Click start (no confirm unlike engine)
  await start.click();
  await page.waitForTimeout(800);
  const afterStart = await dumpButtons("after-start");
  rec(true, "after start dump", afterStart);
  const status1 = await page.getByTestId("hist-test-status").innerText().catch(() => "");
  rec(true, "status after start: " + status1.slice(0, 120));
  await page.screenshot({ path: `${outDir}/intense-after-start.png`, timeout: 6000 }).catch(() => {});

  rec(!(await pause.isDisabled()), "pause still enabled after start");
  rec(!(await stop.isDisabled()), "stop still enabled after start");
  rec(!(await start.isDisabled()), "start still enabled after start");

  await pause.click();
  await page.waitForTimeout(900);
  const startLabelP = (await start.innerText()).replace(/\s+/g, " ").trim();
  const statusP = await page.getByTestId("hist-test-status").innerText().catch(() => "");
  rec(/Resume/i.test(startLabelP) || /paused/i.test(statusP), "pause -> Resume/paused " + startLabelP + " | " + statusP.slice(0, 80));
  await page.screenshot({ path: `${outDir}/intense-after-pause.png`, timeout: 6000 }).catch(() => {});

  await start.click();
  await page.waitForTimeout(900);
  const startLabelR = (await start.innerText()).replace(/\s+/g, " ").trim();
  const statusR = await page.getByTestId("hist-test-status").innerText().catch(() => "");
  rec(!/Resume/i.test(startLabelR) || /resum|evaluat|queued|rank|replay|score/i.test(statusR), "resume worked " + startLabelR + " | " + statusR.slice(0, 80));

  await stop.click();
  await page.waitForFunction(
    () => /stopped|idle|ready|Ready/i.test(document.querySelector("[data-testid=hist-test-status]")?.textContent || ""),
    null,
    { timeout: 8000 },
  ).catch(() => null);
  const statusS = await page.getByTestId("hist-test-status").innerText().catch(() => "");
  const startLabelS = (await start.innerText()).replace(/\s+/g, " ").trim();
  rec(/stop|idle|ready|Ready/i.test(statusS), "stop status " + statusS.slice(0, 100));
  rec(/Start/i.test(startLabelS), "after stop label Start: " + startLabelS);
  await page.screenshot({ path: `${outDir}/intense-after-stop.png`, timeout: 6000 }).catch(() => {});

  // Engine confirm dialogs exist
  page.once("dialog", async (d) => { await d.dismiss(); });
  await page.getByTestId("engine-stop").click();
  rec(true, "engine stop dialog handled");

  rec(errors.filter((e) => !/502|Hydration|Failed to fetch/.test(e)).length === 0, "no fatal pageerror", errors);
} catch (e) {
  rec(false, "throw " + e.message + "\n" + e.stack);
} finally {
  await browser.close();
  const fails = report.filter((r) => !r.ok);
  writeFileSync("/tmp/ui-intense.json", JSON.stringify({ report, errors, consoleErr: consoleErr.slice(0, 40) }, null, 2));
  console.log(`\n${report.filter((r) => r.ok).length}/${report.length} ok  fail=${fails.length}`);
  if (fails.length) {
    for (const f of fails) console.log("FAIL DETAIL", f.msg, f.extra ? JSON.stringify(f.extra).slice(0, 400) : "");
  }
  process.exit(fails.length ? 1 : 0);
}
