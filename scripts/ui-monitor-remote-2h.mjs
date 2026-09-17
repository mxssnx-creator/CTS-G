#!/usr/bin/env node
/** 2-hour remote desk monitor. Never starts/stops Live or VST. */
import { chromium } from "playwright";
import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";

const BASE = process.env.TEST_BASE || "http://152.53.114.112:3102";
const MINUTES = Number(process.env.MONITOR_MINUTES || 120);
const INTERVAL_MS = Number(process.env.MONITOR_INTERVAL_MS || 180_000);
const outDir = "/workspace/screenshots";
const logPath = "/tmp/ui-monitor-remote-2h.log";
const jsonPath = "/tmp/ui-monitor-remote-2h.json";
mkdirSync(outDir, { recursive: true });

const log = (line) => {
  const row = `[${new Date().toISOString()}] ${line}`;
  console.log(row);
  try { appendFileSync(logPath, row + "\n"); } catch { /* ignore */ }
};

process.on("uncaughtException", (e) => log("uncaught " + (e && e.stack ? e.stack : e)));
process.on("unhandledRejection", (e) => log("unhandled " + e));

async function jsonGet(path) {
  const r = await fetch(BASE + path, { cache: "no-store" });
  const text = await r.text();
  let body = null;
  try { body = JSON.parse(text); } catch { body = text.slice(0, 240); }
  return { status: r.status, body };
}

async function apiPass(pass) {
  const fails = [];
  const stats = await jsonGet("/stats.json?conn=overall");
  if (stats.status !== 200) fails.push(`stats ${stats.status}`);
  else if (typeof stats.body?.running !== "boolean") fails.push("stats missing running");
  const conns = await jsonGet("/connections.json");
  if (conns.status !== 200) fails.push(`connections ${conns.status}`);
  const cfg = await jsonGet("/config.json?conn=vst");
  if (cfg.status !== 200) fails.push(`config ${cfg.status}`);
  else {
    const ov = cfg.body?.overlay || {};
    if (ov.blockEvalPosCount != null && Number(ov.blockEvalPosCount) < 5) fails.push("blockEvalPosCount");
  }
  const hist = await jsonGet("/hist-test.json");
  if (hist.status !== 200 && hist.status !== 404) fails.push(`hist-test ${hist.status}`);
  for (const p of ["/", "/results", "/settings", "/step-sweep", "/system"]) {
    const r = await fetch(BASE + p, { cache: "no-store" });
    if (r.status !== 200) fails.push(`page ${p} ${r.status}`);
  }
  log(`pass ${pass} api ${fails.length ? "FAIL " + fails.join("; ") : "ok"} stats=${stats.status} running=${stats.body?.running} halted=${stats.body?.halted} pf=${stats.body?.profitFactor ?? stats.body?.pfCost?.ratio}`);
  return { fails, stats: stats.body };
}

async function uiPass(browser, pass) {
  const fails = [];
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  page.setDefaultTimeout(18000);
  await page.route("**/grok-app-builder/extensions.js", (route) => route.abort());
  await page.route("https://fonts.googleapis.com/**", (route) => route.abort());
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  try {
    await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => !window.$_TSR || window.$_TSR.hydrated === true, null, { timeout: 30000 });
    await page.waitForSelector("[data-testid=desk-root]", { timeout: 20000 });
    await page.waitForTimeout(900);
    for (const id of ["engine-start", "engine-pause", "engine-stop"]) {
      const btn = page.getByTestId(id).first();
      if (!(await btn.count())) fails.push(`missing ${id}`);
    }
    const desk = await page.locator("main").innerText();
    if (/NaN|undefined|\[object Object\]/.test(desk)) fails.push("desk NaN");
    if (pass > 1 && /CONNECTING/.test(desk) && !/HALT|LIVE|ON|PAUSE|OFFLINE/.test(desk)) fails.push("stuck CONNECTING");
    await page.goto(BASE + "/settings", { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => !window.$_TSR || window.$_TSR.hydrated === true, null, { timeout: 30000 });
    await page.waitForTimeout(700);
    for (const id of ["hist-test-start", "hist-test-pause", "hist-test-stop"]) {
      const btn = page.getByTestId(id).first();
      if (!(await btn.count())) fails.push(`missing ${id}`);
    }
    await page.getByTestId("section-block").click().catch(() => {});
    await page.waitForTimeout(250);
    const block = await page.locator("main").innerText();
    if (!/Main eval last positions/i.test(block)) fails.push("block eval control");
    if (pass === 1 || pass % 10 === 0) {
      await page.screenshot({ path: `${outDir}/remote-2h-pass-${pass}.png`, timeout: 8000 }).catch(() => {});
    }
    const fatal = errors.filter((e) => !/502|Hydration|Failed to fetch|AbortError|403/i.test(e));
    if (fatal.length) fails.push("pageerror " + fatal[0].slice(0, 80));
  } catch (e) {
    fails.push("throw " + (e && e.message ? e.message : e));
  } finally {
    await page.close().catch(() => {});
  }
  log(`pass ${pass} ui ${fails.length ? "FAIL " + fails.join("; ") : "ok"}`);
  return fails;
}

const started = Date.now();
const end = started + MINUTES * 60_000;
const report = [];
log(`remote-2h start ${MINUTES}m interval ${INTERVAL_MS}ms base ${BASE}`);
let browser;
try {
  browser = await chromium.launch({
    executablePath: process.env.CTS_CHROMIUM_PATH || undefined,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
} catch (e) {
  log("chromium launch failed " + e);
  process.exit(1);
}

let pass = 0;
try {
  while (Date.now() < end) {
    pass += 1;
    let apiFails = [];
    let uiFails = [];
    try {
      const api = await apiPass(pass);
      apiFails = api.fails;
    } catch (e) {
      apiFails = ["api throw " + (e && e.message ? e.message : e)];
      log(`pass ${pass} api THROW ${apiFails[0]}`);
    }
    try {
      uiFails = await uiPass(browser, pass);
    } catch (e) {
      uiFails = ["ui throw " + (e && e.message ? e.message : e)];
      log(`pass ${pass} ui THROW ${uiFails[0]}`);
      try { await browser.close(); } catch { /* ignore */ }
      try {
        browser = await chromium.launch({
          executablePath: process.env.CTS_CHROMIUM_PATH || undefined,
          args: ["--no-sandbox", "--disable-dev-shm-usage"],
        });
      } catch (err) {
        log("chromium relaunch failed " + err);
      }
    }
    const all = [...apiFails, ...uiFails];
    report.push({ pass, t: Date.now() - started, ok: all.length === 0, fails: all });
    writeFileSync(jsonPath, JSON.stringify({
      started: new Date(started).toISOString(),
      minutes: MINUTES,
      passes: report.length,
      fails: report.filter((r) => !r.ok).length,
      done: false,
      base: BASE,
      report,
    }, null, 2));
    const remain = end - Date.now();
    if (remain <= 0) break;
    await new Promise((r) => setTimeout(r, Math.min(INTERVAL_MS, remain)));
  }
} finally {
  try { await browser.close(); } catch { /* ignore */ }
  const fails = report.filter((r) => !r.ok);
  writeFileSync(jsonPath, JSON.stringify({
    started: new Date(started).toISOString(),
    ended: new Date().toISOString(),
    minutes: MINUTES,
    passes: report.length,
    fails: fails.length,
    done: true,
    base: BASE,
    report,
  }, null, 2));
  log(`remote-2h done passes=${report.length} fail=${fails.length}`);
  process.exit(fails.length ? 1 : 0);
}
