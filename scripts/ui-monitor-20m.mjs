#!/usr/bin/env node
/** 20-minute continuous desk monitor: API + hydrated UI + controls. Does not start Live/mainnet. */
import { chromium } from "playwright";
import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";

const BASE = process.env.TEST_BASE || "http://127.0.0.1:8080";
const MINUTES = Number(process.env.MONITOR_MINUTES || 20);
const INTERVAL_MS = Number(process.env.MONITOR_INTERVAL_MS || 90_000);
const outDir = "/workspace/screenshots";
const logPath = "/tmp/ui-monitor-20m.log";
mkdirSync(outDir, { recursive: true });

const log = (line) => {
  const row = `[${new Date().toISOString()}] ${line}`;
  console.log(row);
  appendFileSync(logPath, row + "\n");
};

async function jsonGet(path) {
  const r = await fetch(BASE + path, { cache: "no-store" });
  const text = await r.text();
  let body = null;
  try { body = JSON.parse(text); } catch { body = text.slice(0, 200); }
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
  else if (!cfg.body?.overlay && !Array.isArray(cfg.body?.lanes)) fails.push("config overlay");
  const hist = await jsonGet("/hist-test.json");
  if (hist.status !== 200) fails.push(`hist-test ${hist.status}`);
  else {
    const pct = Number(hist.body?.pct || 0);
    if (hist.body?.phase === "ready" && pct < 99) fails.push(`hist ready pct ${pct}`);
  }
  const live = await jsonGet("/live-stats.json");
  if (live.status !== 200) fails.push(`live-stats ${live.status}`);
  const pages = ["/", "/results", "/settings", "/step-sweep", "/system"];
  for (const p of pages) {
    const r = await fetch(BASE + p, { cache: "no-store" });
    if (r.status !== 200) fails.push(`page ${p} ${r.status}`);
  }
  // Engine stop is safe when already halted. Never start live/mainnet.
  const stop = await fetch(BASE + "/control.json?conn=overall", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "stop" }),
  });
  if (![200, 400].includes(stop.status)) {
    const t = await stop.text().catch(() => "");
    if (!/sidecar offline|legacy|disabled/i.test(t)) fails.push(`control stop ${stop.status}`);
  }
  log(`pass ${pass} api ${fails.length ? "FAIL " + fails.join("; ") : "ok"} stats=${stats.status} hist=${hist.body?.phase}/${hist.body?.pct} pf=${stats.body?.pfCost?.ratio ?? stats.body?.profitFactor ?? stats.body?.pf}`);
  return fails;
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
    await page.waitForFunction(() => !window.$_TSR || window.$_TSR.hydrated === true, null, { timeout: 25000 });
    await page.waitForSelector("[data-testid=desk-root]", { timeout: 15000 });
    await page.waitForTimeout(900);
    for (const id of ["engine-start", "engine-pause", "engine-stop"]) {
      const btn = page.getByTestId(id);
      if (!(await btn.count())) fails.push(`missing ${id}`);
      else if (await btn.isDisabled()) fails.push(`disabled ${id}`);
    }
    const desk = await page.locator("main").innerText();
    if (/NaN|undefined|\[object Object\]/.test(desk)) fails.push("desk NaN");
    if (/CONNECTING/.test(desk) && /HALTED|OFFLINE|PAUSE/.test(desk) === false) {
      // Connecting is only a fail if it never resolved to halt/live after hydrate.
      if (pass > 1) fails.push("stuck CONNECTING");
    }
    await page.goto(BASE + "/settings", { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => !window.$_TSR || window.$_TSR.hydrated === true, null, { timeout: 25000 });
    await page.waitForTimeout(800);
    for (const id of ["hist-test-start", "hist-test-pause", "hist-test-stop"]) {
      const btn = page.getByTestId(id);
      if (!(await btn.count())) fails.push(`missing ${id}`);
      else if (await btn.isDisabled()) fails.push(`disabled ${id}`);
    }
    const status = await page.getByTestId("hist-test-status").innerText().catch(() => "");
    if (/ready 0%/i.test(status)) fails.push("hist 0%");
    const applied = await page.getByTestId("effective-settings-summary").innerText().catch(() => "");
    if (/waiting for applied snapshot/i.test(applied)) fails.push("settings waiting snapshot");
    await page.goto(BASE + "/step-sweep", { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => !window.$_TSR || window.$_TSR.hydrated === true, null, { timeout: 25000 });
    await page.waitForTimeout(800);
    const sweep = await page.locator("main").innerText();
    if (!/Historic test/i.test(sweep)) fails.push("sweep title");
    if (!/Start|Refresh now|Resume/i.test(sweep) || !/Pause/i.test(sweep) || !/Stop/i.test(sweep)) fails.push("sweep controls");
    await page.screenshot({ path: `${outDir}/monitor-pass-${pass}.png`, timeout: 6000 }).catch(() => {});
    const fatal = errors.filter((e) => !/502|Hydration|Failed to fetch|AbortError/i.test(e));
    if (fatal.length) fails.push("pageerror " + fatal[0].slice(0, 80));
  } catch (e) {
    fails.push("throw " + e.message);
  } finally {
    await page.close();
  }
  log(`pass ${pass} ui ${fails.length ? "FAIL " + fails.join("; ") : "ok"}`);
  return fails;
}

const started = Date.now();
const end = started + MINUTES * 60_000;
const report = [];
const browser = await chromium.launch({
  executablePath: process.env.CTS_CHROMIUM_PATH || undefined,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
log(`monitor start ${MINUTES}m interval ${INTERVAL_MS}ms base ${BASE}`);
let pass = 0;
try {
  while (Date.now() < end) {
    pass += 1;
    const apiFails = await apiPass(pass);
    const uiFails = await uiPass(browser, pass);
    const all = [...apiFails, ...uiFails];
    report.push({ pass, t: Date.now() - started, ok: all.length === 0, fails: all });
    const remain = end - Date.now();
    if (remain <= 0) break;
    await new Promise((r) => setTimeout(r, Math.min(INTERVAL_MS, remain)));
  }
} finally {
  await browser.close();
  const fails = report.filter((r) => !r.ok);
  writeFileSync("/tmp/ui-monitor-20m.json", JSON.stringify({ report, fails: fails.length, passes: report.length }, null, 2));
  log(`monitor done passes=${report.length} fail=${fails.length}`);
  process.exit(fails.length ? 1 : 0);
}
