import { chromium } from "playwright";
import { writeFileSync } from "node:fs";

const BASE = "http://127.0.0.1:8080";
const out = [];
const ok = (m) => out.push("OK " + m);
const fail = (m) => { out.push("FAIL " + m); };

const browser = await chromium.launch({ args: ["--no-sandbox", "--disable-dev-shm-usage"] });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
page.setDefaultTimeout(16000);
const shot = (path) => page.screenshot({ path, timeout: 6000 }).catch((e) => out.push("WARN shot " + path + " " + e.message));
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => { if (m.type() === "error") errors.push("console " + m.text()); });

try {
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.evaluate(() => {
    localStorage.removeItem("x01-pulse-overlay");
    localStorage.removeItem("x01-pulse-overlay-vst");
    localStorage.removeItem("x01-pulse-overlay-live");
    localStorage.setItem("pulse.connType", "vst");
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2000);
  await page.getByTestId("conn-vst").click();
  await page.waitForFunction(
    () => /coverage · px/i.test(document.body.innerText) && /controls ·/i.test(document.body.innerText),
    null,
    { timeout: 15000 },
  ).catch(() => null);
  await page.waitForTimeout(300);
  ok("desk " + (await page.locator("h1").innerText()));
  const desk = await page.locator("main").innerText();
  if (/VST|X02|demo/i.test(desk)) ok("switched VST"); else fail("desk not VST");
  if (/SL:TP/i.test(desk) && /1m/.test(desk) && /15m/.test(desk)) ok("desk strips"); else fail("missing strips");
  if (/work · cycle|in-process tests/i.test(desk)) ok("work strip"); else out.push("WARN no work strip");
  if (/Open book|Scanning/i.test(desk)) ok("open book"); else fail("no open book");
  if (/coverage · px/i.test(desk) && /controls ·/i.test(desk)) ok("coverage+controls"); else fail("missing coverage/controls");
  if (/recon /i.test(desk)) ok("recon in coverage"); else out.push("WARN no recon");
  if (/load /i.test(desk)) ok("load strip"); else out.push("WARN no load strip");
  await shot("/workspace/screenshots/desk-vst-live.png");

  await page.goto(BASE + "/settings", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1200);
  await page.getByTestId("conn-vst").click();
  await page.waitForFunction(
    () => /cycle \d+/i.test(document.body.innerText),
    null,
    { timeout: 12000 },
  ).catch(() => null);
  await page.waitForTimeout(400);
  const applied = page.locator("[data-testid=live-applied]");
  if (await applied.count()) ok("live-applied " + (await applied.innerText()).replace(/\s+/g, " ").slice(0, 120));
  else fail("no live-applied");

  const sections = [
    "overview", "connection", "profit", "risk", "trailing", "timeframes",
    "packs", "sets", "exits", "stages", "block", "dca", "axes", "volume",
    "controls", "indication", "pulse", "symbols",
  ];
  const expectH2 = {
    overview: "coverage",
    connection: "connection",
    profit: "profit",
    risk: "stop loss",
    trailing: "trailing",
    timeframes: "timeframe",
    packs: "strategy",
    sets: "independent sets",
    exits: "best exits",
    stages: "stage",
    block: "block",
    dca: "dca",
    axes: "coordination",
    volume: "volume",
    controls: "control orders",
    indication: "indication",
    pulse: "pulse",
    symbols: "symbol",
  };
  for (const id of sections) {
    const btn = page.getByTestId(`section-${id}`);
    if (!(await btn.count())) { fail("no nav " + id); continue; }
    await btn.click({ timeout: 4000 }).catch(() => null);
    await page.waitForFunction(
      (t) => (document.querySelector("h2")?.textContent || "").toLowerCase().includes(t),
      expectH2[id],
      { timeout: 2500 },
    ).catch(() => null);
    const h = (await page.locator("h2").first().innerText().catch(() => "")).toLowerCase();
    if (h.includes(expectH2[id])) ok(`section ${id} h2=${h || "?"}`);
    else out.push("WARN section " + id + " h2=" + (h || "?"));
  }
  await page.getByTestId("section-symbols").click();
  await page.waitForFunction(
    () => /Volatility 1H/i.test(document.body.innerText) && /Max leverage/i.test(document.body.innerText),
    null,
    { timeout: 8000 },
  ).catch(() => null);
  await page.waitForTimeout(300);
  const symTxt = await page.locator("main").innerText();
  if (/Volatility 1H/i.test(symTxt) && /exchange max leverage/i.test(symTxt)) ok("symbols rank chips"); else fail("symbols missing rank");
  if (await page.getByTestId("symbol-sort-vol1h").count()) ok("vol1h sort chip"); else fail("no vol1h chip");
  if (await page.getByTestId("symbols-dynamic").count()) ok("dynamic book toggle"); else fail("no dynamic toggle");
  await shot("/workspace/screenshots/ui-symbols-rank.png");

  await page.getByTestId("section-overview").click();
  await page.waitForFunction(
    () => /Scan universe/i.test(document.body.innerText),
    null,
    { timeout: 8000 },
  ).catch(() => null);
  await page.waitForTimeout(300);
  const ov = await page.locator("main").innerText();
  if (/Scan universe/i.test(ov) || /coverage · px/i.test(ov)) ok("overview coverage panel"); else fail("overview missing coverage");
  await shot("/workspace/screenshots/ui-connection.png");

  await page.getByTestId("section-risk").click();
  await page.waitForSelector("[data-ratio='1.5']", { timeout: 8000 });
  await page.waitForTimeout(200);
  await shot("/workspace/screenshots/ui-sl-tp-ratios.png");
  const chip = page.locator("[data-ratio='1.5']");
  if (await chip.count()) {
    await chip.first().click();
    await page.getByTestId("save-status").waitFor({ timeout: 4000 }).catch(() => null);
    await page.waitForTimeout(300);
    const txt = await page.locator("[data-testid=save-status]").innerText();
    if (/Unsaved overlay/i.test(txt)) ok("unsaved after 1.5"); else out.push("WARN no unsaved label: " + txt);
    await page.locator("[data-testid=save-overlay]").click();
    await page.waitForTimeout(1800);
    const r = await page.evaluate(async () => (await fetch("/config.json?conn=vst", { cache: "no-store" })).json());
    const ratio = Number(r?.overlay?.slToTpRatio);
    if (ratio === 1.5) ok("saved slToTpRatio 1.5"); else fail("save 1.5 got " + ratio);
    const mods = r?.overlay?.modules || {};
    if (mods["risk.slTpRatios"] === true) ok("saved modules synced"); else out.push("WARN modules " + JSON.stringify(mods).slice(0, 80));
  } else fail("no 1.5 chip");

  await page.getByTestId("section-dca").click();
  await page.waitForFunction(() => /Distance #1/i.test(document.body.innerText), null, { timeout: 8000 });
  await page.waitForTimeout(200);
  const dcaTxt = await page.locator("main").innerText();
  if (/Distance #1/i.test(dcaTxt) && /Take profit mode/i.test(dcaTxt) && /Vol × #1/i.test(dcaTxt)) ok("dca step controls");
  else fail("dca missing step controls");

  await page.getByTestId("section-trailing").click();
  await page.waitForTimeout(400);
  await shot("/workspace/screenshots/ui-trailing-recals.png");
  const tr = await page.locator("main").innerText();
  if (/independent recals|Arm min|trail/i.test(tr)) ok("trailing page"); else fail("trailing missing");

  await page.getByTestId("section-timeframes").click();
  await page.waitForFunction(() => /Combined consensus/i.test(document.body.innerText), null, { timeout: 8000 }).catch(() => null);
  await page.waitForTimeout(200);
  await shot("/workspace/screenshots/ui-timeframes.png");
  const tf = await page.locator("main").innerText();
  if (/1 minute/i.test(tf) && /Combined consensus/i.test(tf)) ok("timeframes page"); else fail("timeframes missing");

  await page.getByTestId("section-controls").click();
  await page.waitForTimeout(400);
  const ctl = await page.locator("[data-testid=controls-live]").innerText().catch(() => "");
  if (/live controls/i.test(ctl)) ok("controls live " + ctl.replace(/\s+/g, " ").slice(0, 80)); else fail("no controls live");

  await page.waitForTimeout(400);
  const r2 = await page.evaluate(async () => (await fetch("/config.json?conn=vst", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ overlay: { slToTpRatio: 0.6, dcaTakeProfitMode: "average" } }),
  })).json()).catch((e) => ({ overlay: { slToTpRatio: "err " + e.message } }));
  if (Number(r2?.overlay?.slToTpRatio) === 0.6) ok("restored 0.6"); else fail("restore got " + r2?.overlay?.slToTpRatio);
  await shot("/workspace/screenshots/ui-saved.png");

  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1200);
  await page.getByTestId("conn-overall").click();
  await page.waitForTimeout(800);
  ok("overall");
  await page.getByTestId("conn-live").click();
  await page.waitForTimeout(800);
  const liveTxt = await page.locator("main").innerText();
  if (/halt|X01|LIVE_MAINNET|mainnet|equity/i.test(liveTxt)) ok("live view"); else fail("live empty");
  await shot("/workspace/screenshots/desk.png");
  await page.getByTestId("conn-vst").click();
  await page.waitForTimeout(1000);
  await shot("/workspace/screenshots/desk-vst.png");

  await page.goto(BASE + "/results", { waitUntil: "domcontentloaded" });
  await page.getByTestId("conn-vst").click();
  await page.waitForSelector("[data-testid=coverage-panel]", { timeout: 12000 }).catch(() => null);
  await page.waitForTimeout(400);
  ok("results " + (await page.locator("h1").innerText().catch(() => "?")));
  const res = await page.locator("main").innerText();
  if (/Scan universe/i.test(res) || /coverage · px/i.test(res)) ok("results coverage panel"); else fail("results missing coverage");
  await shot("/workspace/screenshots/desk-overall.png");

  await page.goto(BASE + "/system", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(900);
  ok("system " + (await page.locator("h1").innerText().catch(() => "?")));
  const sys = await page.locator("main").innerText();
  if (/Block strategy/i.test(sys) && /Coordination axes/i.test(sys) && /Independent Sets/i.test(sys)) ok("system missing modules present");
  else fail("system missing new modules");

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(BASE + "/settings", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(900);
  await shot("/workspace/screenshots/settings-mobile.png");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 2);
  if (!overflow) ok("settings mobile no overflow"); else fail("settings mobile overflow");
} catch (e) {
  fail("throw " + e.message);
} finally {
  if (errors.length) out.push("PAGEERRORS " + errors.slice(0, 5).join(" | "));
  console.log(out.join("\n"));
  writeFileSync("/tmp/ui-check.json", JSON.stringify({ out, errors }, null, 2));
  await browser.close();
}
process.exit(out.some((l) => l.startsWith("FAIL")) ? 1 : 0);
