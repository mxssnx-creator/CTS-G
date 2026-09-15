#!/usr/bin/env node
/** 2-hour continuous desk monitor. Never starts Live/mainnet. Survives per-pass errors. */
import { chromium } from "playwright";
import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";

const BASE = process.env.TEST_BASE || "http://127.0.0.1:8080";
const MINUTES = Number(process.env.MONITOR_MINUTES || 120);
const INTERVAL_MS = Number(process.env.MONITOR_INTERVAL_MS || 180_000);
const outDir = "/workspace/screenshots";
const logPath = "/tmp/ui-monitor-2h.log";
const jsonPath = "/tmp/ui-monitor-2h.json";
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
  else {
    if (typeof stats.body?.running !== "boolean") fails.push("stats missing running");
    if (stats.body?.profitFactor == null && stats.body?.pf == null && !stats.body?.pfCost) fails.push("stats missing pf");
  }
  const conns = await jsonGet("/connections.json");
  if (conns.status !== 200) fails.push(`connections ${conns.status}`);
  else if (!Array.isArray(conns.body?.types) || conns.body.types.length < 3) fails.push("connections types");
  const cfg = await jsonGet("/config.json?conn=vst");
  if (cfg.status !== 200) fails.push(`config ${cfg.status}`);
  else {
    const ov = cfg.body?.overlay || {};
    if (!cfg.body?.overlay && !Array.isArray(cfg.body?.lanes)) fails.push("config overlay");
    if (ov.blockEvalPosCount != null && Number(ov.blockEvalPosCount) < 5) fails.push("blockEvalPosCount");
  }
  const hist = await jsonGet("/hist-test.json");
  if (hist.status !== 200) fails.push(`hist-test ${hist.status}`);
  else if (hist.body?.phase === "ready" && Number(hist.body?.pct || 0) < 99) fails.push(`hist ready pct ${hist.body.pct}`);
  const live = await jsonGet("/live-stats.json");
  if (live.status !== 200) fails.push(`live-stats ${live.status}`);
  for (const p of ["/", "/results", "/settings", "/step-sweep", "/system"]) {
    const r = await fetch(BASE + p, { cache: "no-store" });
    if (r.status !== 200) fails.push(`page ${p} ${r.status}`);
  }
  // Do not POST stop/start — a proxied control can halt the remote VST desk.
  log(`pass ${pass} api ${fails.length ? "FAIL " + fails.join("; ") : "ok"} stats=${stats.status} hist=${hist.body?.phase}/${hist.body?.pct} halted=${stats.body?.halted}`);
  return { fails, hist: hist.body, stats: { halted: stats.body?.halted, pf: stats.body?.pfCost?.ratio ?? stats.body?.profitFactor } };
}

async function uiPass(browser, pass) {
  const fails = [];
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  page.setDefaultTimeout(16000);
  await page.route("**/grok-app-builder/extensions.js", (route) => route.abort());
  await page.route("https://fonts.googleapis.com/**", (route) => route.abort());
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  try {
    await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => !window.$_TSR || window.$_TSR.hydrated === true, null, { timeout: 25000 });
    await page.waitForSelector("[data-testid=desk-root]", { timeout: 15000 });
    await page.waitForTimeout(700);
    for (const id of ["engine-start", "engine-pause", "engine-stop"]) {
      const btn = page.getByTestId(id).first();
      if (!(await btn.count())) fails.push(`missing ${id}`);
      else if (await btn.isDisabled()) fails.push(`disabled ${id}`);
    }
    const desk = await page.locator("main").innerText();
    if (/NaN|undefined|\[object Object\]/.test(desk)) fails.push("desk NaN");
    if (pass > 1 && /CONNECTING/.test(desk) && !/HALTED|OFFLINE|PAUSE/.test(desk)) fails.push("stuck CONNECTING");
    await page.goto(BASE + "/settings", { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => !window.$_TSR || window.$_TSR.hydrated === true, null, { timeout: 25000 });
    await page.waitForTimeout(600);
    for (const id of ["hist-test-start", "hist-test-pause", "hist-test-stop"]) {
      const btn = page.getByTestId(id).first();
      if (!(await btn.count())) fails.push(`missing ${id}`);
      else if (await btn.isDisabled()) fails.push(`disabled ${id}`);
    }
    const status = await page.getByTestId("hist-test-status").innerText().catch(() => "");
    if (/ready 0%/i.test(status)) fails.push("hist 0%");
    const applied = await page.getByTestId("effective-settings-summary").innerText().catch(() => "");
    if (/waiting for applied snapshot/i.test(applied)) fails.push("settings waiting snapshot");
    await page.getByTestId("section-block").click().catch(() => {});
    await page.waitForTimeout(250);
    const block = await page.locator("main").innerText();
    if (!/Main eval last positions/i.test(block)) fails.push("block eval control");
    await page.goto(BASE + "/step-sweep", { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => !window.$_TSR || window.$_TSR.hydrated === true, null, { timeout: 25000 });
    await page.waitForTimeout(500);
    const sweep = await page.locator("main").innerText();
    if (!/Historic test/i.test(sweep)) fails.push("sweep title");
    if (!/Start|Refresh now|Resume/i.test(sweep) || !/Pause/i.test(sweep) || !/Stop/i.test(sweep)) fails.push("sweep controls");
    if (pass === 1 || pass % 10 === 0) {
      await page.screenshot({ path: `${outDir}/monitor-2h-pass-${pass}.png`, timeout: 6000 }).catch(() => {});
    }
    const fatal = errors.filter((e) => !/502|Hydration|Failed to fetch|AbortError/i.test(e));
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
log(`monitor-2h start ${MINUTES}m interval ${INTERVAL_MS}ms base ${BASE}`);
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
      try {
        await browser.close();
      } catch { /* ignore */ }
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
    report,
  }, null, 2));
  log(`monitor-2h done passes=${report.length} fail=${fails.length}`);
  process.exit(fails.length ? 1 : 0);
}
