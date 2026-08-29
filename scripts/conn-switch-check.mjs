import { chromium } from "playwright";
import { writeFileSync } from "node:fs";

const BASE = "http://127.0.0.1:8080";
const out = [];
const ok = (m) => out.push("OK " + m);
const fail = (m) => out.push("FAIL " + m);

const browser = await chromium.launch({ args: ["--no-sandbox", "--disable-dev-shm-usage"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
page.setDefaultTimeout(16000);
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => {
  if (m.type() === "error") errors.push("console " + m.text());
});

async function waitConn(id) {
  await page.waitForFunction(
    (want) => document.querySelector("[data-testid=desk-root]")?.getAttribute("data-conn") === want,
    id,
    { timeout: 12000 },
  );
  await page.waitForFunction(
    (want) => {
      const el = document.querySelector("[data-testid=desk-root]");
      if (!el || el.getAttribute("data-conn") !== want) return false;
      const typ = el.getAttribute("data-stats-type") || "";
      return typ === want;
    },
    id,
    { timeout: 20000 },
  );
}

async function clickConn(id) {
  await page.locator(`[data-testid=conn-${id}]`).first().click({ force: true });
  await waitConn(id);
}

async function identity() {
  return page.locator("[data-testid=desk-identity]").innerText().catch(() => "");
}

function rootAttrs() {
  return page.evaluate(() => {
    const el = document.querySelector("[data-testid=desk-root]");
    return {
      conn: el?.getAttribute("data-conn") || "",
      type: el?.getAttribute("data-stats-type") || "",
      id: el?.getAttribute("data-stats-id") || "",
    };
  });
}

try {
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.evaluate(() => localStorage.setItem("pulse.connType", "overall"));
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1600);

  await clickConn("overall");
  const overall = await page.locator("main").innerText();
  if (/OVERALL|All desks|independent/i.test(overall)) ok("overall header");
  else fail("overall header missing");
  if (await page.locator("[data-testid=lane-board]").count()) ok("lane board");
  else fail("no lane board on overall");
  if (await page.locator("[data-testid=lane-live]").count()) ok("live lane card");
  else fail("no live lane");
  if (await page.locator("[data-testid=lane-vst]").count()) ok("vst lane card");
  else fail("no vst lane");
  const setsOverall = await page.locator("[data-testid=sets-strip]").innerText();
  if (/vst|live/i.test(setsOverall) && /sets/i.test(setsOverall)) ok("overall sets progress " + setsOverall.replace(/\s+/g, " ").slice(0, 140));
  else fail("overall sets empty: " + setsOverall.slice(0, 120));
  await page.screenshot({ path: "/workspace/screenshots/desk-overall.png", timeout: 4000 }).catch(() => out.push("WARN shot overall"));

  await clickConn("vst");
  const idVst = await identity();
  if (/vst/i.test(idVst) && /x02/i.test(idVst)) ok("vst identity " + idVst);
  else fail("vst identity wrong: " + idVst);
  const vstTxt = await page.locator("main").innerText();
  if (/X02|VST demo|Prod-VST|VST_DEMO/i.test(vstTxt)) ok("vst desk copy");
  else fail("vst desk copy missing");
  if (/LIVE_MAINNET|equity .* below min/i.test(vstTxt) && !/BingX X01 · live mainnet/i.test(await page.locator("header").innerText())) {
    /* live halt reason must not be the vst header */
  }
  if (await page.locator("[data-testid=lane-board]").count()) fail("lane board leaked onto vst");
  else ok("vst has no mixed lane board");
  if (/SL\+TP|Open book/i.test(vstTxt)) ok("vst open book");
  else fail("vst open book missing");
  const vstSets = await page.locator("[data-testid=sets-strip]").innerText();
  if (/sets/i.test(vstSets) && /[1-9]/i.test(vstSets)) ok("vst sets " + vstSets.replace(/\s+/g, " ").slice(0, 140));
  else fail("vst sets wrong: " + vstSets.slice(0, 120));
  await page.screenshot({ path: "/workspace/screenshots/desk-vst-switch.png", timeout: 4000 }).catch(() => out.push("WARN shot vst"));

  await clickConn("live");
  const idLive = await identity();
  if (/live/i.test(idLive) && /x01/i.test(idLive) && !/x02/i.test(idLive)) ok("live identity " + idLive);
  else fail("live identity wrong: " + idLive);
  const liveTxt = await page.locator("main").innerText();
  if (/LIVE_MAINNET|BingX X01|below min/i.test(liveTxt)) ok("live halt visible");
  else fail("live halt missing");
  if (/VST_DEMO/i.test(liveTxt)) fail("vst mode leaked onto live");
  else ok("live isolated from vst mode");
  const liveHeader = await page.locator("header").innerText();
  if (/live mainnet/i.test(liveHeader)) ok("live header");
  else fail("live header " + liveHeader.slice(0, 80));
  await page.screenshot({ path: "/workspace/screenshots/desk-live-switch.png", timeout: 4000 }).catch(() => out.push("WARN shot live"));

  await page.getByRole("link", { name: "Settings" }).click();
  await page.waitForSelector("[data-testid=conn-switch]");
  await page.waitForTimeout(800);
  await clickConn("vst");
  await page.locator('[data-section="connection"]').click({ force: true });
  const vstRoot = await rootAttrs();
  if (vstRoot.conn === "vst" && vstRoot.type === "vst") ok("settings vst root " + JSON.stringify(vstRoot));
  else fail("settings vst root " + JSON.stringify(vstRoot));
  const appliedVst = await page.locator("[data-testid=live-applied]").innerText();
  if (/\bvst\b/i.test(appliedVst)) ok("applied vst " + appliedVst.replace(/\s+/g, " ").slice(0, 100));
  else fail("applied vst " + appliedVst.replace(/\s+/g, " ").slice(0, 100));

  await clickConn("live");
  const liveRoot = await rootAttrs();
  if (liveRoot.conn === "live" && liveRoot.type === "live" && /x01/i.test(liveRoot.id)) ok("settings live root " + JSON.stringify(liveRoot));
  else fail("settings live root " + JSON.stringify(liveRoot));
  const appliedLive = await page.locator("[data-testid=live-applied]").innerText();
  if (/\blive\b/i.test(appliedLive) && !/x02/i.test(appliedLive)) ok("applied live isolated");
  else fail("applied live " + appliedLive.replace(/\s+/g, " ").slice(0, 120));

  await clickConn("overall");
  await page.waitForTimeout(600);
  const saveDisabled = await page.locator("[data-testid=save-overlay]").isDisabled();
  if (saveDisabled) ok("overall save disabled");
  else fail("overall save enabled");

  await page.getByRole("link", { name: "Results" }).click();
  await page.waitForSelector("[data-testid=results-identity]", { timeout: 15000 });
  await page.waitForTimeout(1000);
  await page.locator("[data-testid=conn-vst]").first().click({ force: true });
  await page.waitForTimeout(2000);
  const resVst = await page.locator("[data-testid=results-identity]").innerText();
  if (/vst/i.test(resVst) && /x02/i.test(resVst)) ok("results vst " + resVst);
  else fail("results vst " + resVst);
  await page.locator("[data-testid=conn-live]").first().click({ force: true });
  await page.waitForTimeout(2000);
  const resLive = await page.locator("[data-testid=results-identity]").innerText();
  if (/live/i.test(resLive) && /x01/i.test(resLive) && !/x02/i.test(resLive)) ok("results live " + resLive);
  else fail("results live " + resLive);

  await page.getByRole("link", { name: "Desk" }).click();
  await page.waitForSelector("[data-testid=lane-vst]", { timeout: 15000 });
  await page.locator("[data-testid=lane-vst]").click({ force: true });
  await page.waitForTimeout(1500);
  const afterLane = await rootAttrs();
  if (afterLane.conn === "vst") ok("lane card switches to vst");
  else fail("lane card " + JSON.stringify(afterLane));

  const apis = await page.evaluate(async () => {
    const grab = async (c) => {
      const r = await fetch("/stats.json?conn=" + c, { cache: "no-store" });
      const j = await r.json();
      return { conn: c, type: j.connType, id: j.connection, open: j.openCount, unit: j.unit, sets: Boolean(j.sets), engine: Boolean(j.engine), lanes: (j.sets?.lanes || []).map((x) => x.type) };
    };
    return { vst: await grab("vst"), live: await grab("live"), overall: await grab("overall") };
  });
  if (apis.vst.type === "vst" && apis.vst.id.includes("x02")) ok("api vst isolated open=" + apis.vst.open);
  else fail("api vst " + JSON.stringify(apis.vst));
  if (apis.live.type === "live" && apis.live.id.includes("x01")) ok("api live isolated open=" + apis.live.open);
  else fail("api live " + JSON.stringify(apis.live));
  if (apis.overall.type === "overall" && apis.overall.sets && apis.overall.engine) ok("api overall has progress lanes=" + apis.overall.lanes.join(","));
  else fail("api overall missing progress " + JSON.stringify(apis.overall));
} catch (e) {
  fail(String(e));
}

if (errors.length) out.push("WARN pageerrors " + errors.slice(0, 6).join(" | "));
const fails = out.filter((l) => l.startsWith("FAIL"));
writeFileSync("/tmp/conn-switch-check.txt", out.join("\n") + "\n");
console.log(out.join("\n"));
await browser.close();
process.exit(fails.length ? 1 : 0);
