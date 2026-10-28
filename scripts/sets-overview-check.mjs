// Drives the desk's Sets overview in a real browser against the live sidecar and checks every figure it shows against
// an independent computation from the raw stats. usage: node scripts/sets-overview-check.mjs [conn=vst] [shotsDir]
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const BASE = process.env.DESK_URL || "http://127.0.0.1:8080";
const CONN = process.argv[2] || "vst";
const SHOTS = process.argv[3] || "";
if (SHOTS) mkdirSync(SHOTS, { recursive: true });
const out = [];
let failed = 0;
const ok = (m) => out.push("OK   " + m);
const fail = (m) => {
  failed += 1;
  out.push("FAIL " + m);
};
const expect = (cond, m, detail = "") => (cond ? ok(m) : fail(m + (detail ? " :: " + detail : "")));

const STEP_RANGES = { all: [0, Infinity], "2-6": [2, 6], "7-12": [7, 12], "13-20": [13, 20], "21-30": [21, 30] };
const COST = 0.12;

// independent re-implementation, straight from the engine's definitions (_pf_split)
function expected(index, type, range) {
  const kind = type === "normal" ? "base" : "trail";
  const [lo, hi] = STEP_RANGES[range];
  const rows = index.filter((r) => {
    if (r.kind !== kind) return false;
    if (range === "all") return true;
    const step = kind === "trail" ? Math.round(parseFloat(String(r.tr).split(":")[0]) / COST) : r.st;
    return Number.isFinite(step) && step >= lo && step <= hi;
  });
  const valid = rows.filter((r) => r.on === 1);
  const sum = (a, k) => a.reduce((x, r) => x + Number(r[k] ?? 0), 0);
  const gp = sum(valid, "gp");
  const gl = sum(valid, "gl");
  const pf = valid.length ? Math.round((gl > 0 ? gp / gl : gp > 0 ? 99 : 0) * 1e4) / 1e4 : null;
  const pctVal = rows.length ? Math.round((10000 * valid.length) / rows.length) / 100 : 0;
  return { total: rows.length, valid: valid.length, pct: pctVal, pf, orders: sum(valid, "gn") };
}

// PW_CHROMIUM: a Chromium binary to use when the one this Playwright version expects is not installed
const browser = await chromium.launch({
  ...(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {}),
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
page.setDefaultTimeout(20000);
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror " + e));
page.on("console", (m) => {
  if (m.type() === "error") errors.push("console " + m.text());
});
const shot = async (name) => {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true }).catch(() => null);
};

try {
  const res = await fetch(`http://127.0.0.1:3015/stats.json?conn=${CONN}`);
  const stats = await res.json();
  const index = stats.sets?.index ?? [];
  expect(index.length > 0, `stats carry the Set index (${index.length} Sets)`);
  expect(index.length === 0 || ["n", "gn", "gp", "gl", "rk"].every((k) => k in index[0]), "index rows carry the pooled-PF fields");

  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.evaluate((c) => {
    localStorage.removeItem("pulse.setsView");
    localStorage.setItem("pulse.connType", c);
  }, CONN);
  await page.goto(BASE + "/results", { waitUntil: "domcontentloaded" });
  await page.getByTestId("sets-overview").waitFor();
  await page.getByTestId("sets-overview-body").waitFor();

  // defaults
  expect((await page.getByTestId("sets-range-all").getAttribute("aria-pressed")) === "true", "Step range defaults to All");
  expect((await page.getByTestId("sets-type-normal").getAttribute("aria-pressed")) === "true", "Type defaults to Normal");
  const typeLabels = await page.locator('[data-testid="sets-type"] button').allInnerTexts();
  expect(JSON.stringify(typeLabels) === JSON.stringify(["Normal", "Trailing", "Axis", "Block", "DCA"]), "types are Normal, Trailing, Axis, Block, DCA", JSON.stringify(typeLabels));
  const rangeLabels = await page.locator('[data-testid="sets-range"] button').allInnerTexts();
  expect(rangeLabels[0] === "All", "the first range is All", JSON.stringify(rangeLabels));
  await shot("results-default");

  const readKpis = async () => {
    const t = await page.getByTestId("sets-overview-body").innerText();
    const grab = (label, re) => {
      const m = t.match(new RegExp(label + "\\s*\\n\\s*([^\\n]+)", "i"));
      return m ? m[1].trim() : null;
    };
    return { shown: grab("Sets shown"), validated: grab("Validated"), pf: grab("Effective PF"), orders: grab("Orders"), text: t };
  };

  for (const type of ["normal", "trailing"]) {
    await page.getByTestId(`sets-type-${type}`).click();
    for (const range of Object.keys(STEP_RANGES)) {
      await page.getByTestId(`sets-range-${range}`).click();
      await page.waitForTimeout(120);
      const want = expected(index, type, range);
      const got = await readKpis();
      const tag = `${type}/${range}`;
      expect(Number(got.shown) === want.total, `${tag}: Sets shown ${want.total}`, `got ${got.shown}`);
      expect(parseFloat(got.validated) === Math.round(want.pct * 10) / 10 || Math.abs(parseFloat(got.validated) - want.pct) < 0.06, `${tag}: validated ${want.pct}%`, `got ${got.validated}`);
      const gotPf = got.pf === "—" ? null : parseFloat(got.pf);
      expect(want.pf === null ? gotPf === null : Math.abs(gotPf - want.pf) < 0.006, `${tag}: effective PF ${want.pf}`, `got ${got.pf}`);
      expect(Number(got.orders) === want.orders, `${tag}: orders ${want.orders}`, `got ${got.orders}`);
      const rows = await page.locator('[data-testid="sets-overview-table"] tbody tr').count();
      expect(rows === Math.min(60, want.total) || (want.total === 0 && rows === 1), `${tag}: table rows ${Math.min(60, want.total)}`, `got ${rows}`);
      if (want.total > 0 && type === "normal" && range !== "all") {
        const steps = await page.locator('[data-testid="sets-overview-table"] tbody tr td:nth-child(3)').allInnerTexts();
        const [lo, hi] = STEP_RANGES[range];
        expect(steps.every((s) => Number(s) >= lo && Number(s) <= hi), `${tag}: every row step within ${lo}-${hi}`, steps.slice(0, 8).join(","));
      }
    }
  }
  await page.getByTestId("sets-type-normal").click();
  await page.getByTestId("sets-range-all").click();

  // all = the engine's own post-Base figures
  const all = expected(index, "normal", "all");
  const trAll = expected(index, "trailing", "all");
  const pb = stats.sets?.pfBase;
  const both = {
    valid: all.valid + trAll.valid,
    total: all.total + trAll.total,
  };
  expect(both.valid === pb?.base?.sets, `Normal + Trailing valid Sets equal the engine's pfBase (${both.valid})`, `engine ${pb?.base?.sets}`);
  expect(both.total === (pb?.total ?? pb?.unfiltered?.sets), `Normal + Trailing total equal the engine's (${both.total})`, `engine ${pb?.total}`);

  // the selection survives a reload
  await page.getByTestId("sets-type-trailing").click();
  await page.getByTestId("sets-range-2-6").click();
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByTestId("sets-overview").waitFor();
  await page.waitForTimeout(400);
  expect((await page.getByTestId("sets-type-trailing").getAttribute("aria-pressed")) === "true", "the type selection survives a reload");
  expect((await page.getByTestId("sets-range-2-6").getAttribute("aria-pressed")) === "true", "the range selection survives a reload");
  await shot("results-trailing-2-6");

  // the other types
  for (const type of ["axis", "block", "dca"]) {
    await page.getByTestId(`sets-type-${type}`).click();
    await page.getByTestId(`sets-overview-${type}`).waitFor();
    const text = await page.getByTestId(`sets-overview-${type}`).innerText();
    expect(text.length > 20 && !/NaN|undefined|\[object/.test(text), `${type} panel renders without NaN/undefined`, text.slice(0, 120));
    const disabled = await page.getByTestId("sets-range-all").isDisabled();
    expect(disabled, `${type}: the step range is disabled (it filters Sets only)`);
    await shot(`results-${type}`);
  }
  // the Axis panel agrees with the stats
  await page.getByTestId("sets-type-axis").click();
  const axText = await page.getByTestId("sets-overview-axis").innerText();
  for (const name of Object.keys(stats.coord?.axes ?? {})) expect(axText.includes(name), `axis panel lists axis ${name}`);

  // charts drawn and no stray values anywhere on the page
  await page.getByTestId("sets-type-normal").click();
  await page.getByTestId("sets-range-all").click();
  await page.waitForTimeout(500);
  const svgs = await page.locator("svg.recharts-surface").count();
  expect(svgs >= 7, `charts are drawn (${svgs} recharts surfaces)`);
  const body = await page.locator("main").innerText();
  expect(!/NaN|undefined|\[object Object\]|Infinity/.test(body), "the Results page shows no NaN, undefined or Infinity", (body.match(/.{0,30}(NaN|undefined|Infinity).{0,30}/) || [""])[0]);

  // the Settings Sets section
  await page.goto(BASE + "/settings", { waitUntil: "domcontentloaded" });
  // a click before hydration is lost: click until the section opens
  let opened = false;
  for (let i = 0; i < 8 && !opened; i++) {
    await page.waitForTimeout(700);
    await page.getByTestId("section-sets").click();
    opened = (await page.getByTestId("sets-overview").count()) > 0;
  }
  expect(opened, "Settings > Sets shows the Sets overview");
  if (opened) {
    await page.getByTestId("sets-overview-body").waitFor();
    expect((await page.getByTestId("sets-range-all").getAttribute("aria-pressed")) !== null, "Settings > Sets has the step-range and type selectors");
    await page.getByTestId("sets-type-trailing").click();
    expect((await page.getByTestId("sets-type-trailing").getAttribute("aria-pressed")) === "true", "Settings > Sets: a type can be chosen");
  }
  await shot("settings-sets");
} catch (e) {
  fail("script error: " + String(e).slice(0, 300));
}
const real = errors.filter((e) => !/favicon|Failed to load resource.*(404|401)|ResizeObserver/i.test(e));
expect(real.length === 0, "no page or console errors", real.slice(0, 4).join(" | ").slice(0, 400));
await browser.close();
console.log(out.join("\n"));
console.log(failed === 0 ? `\nall ${out.length} checks OK` : `\n${failed} FAILED of ${out.length}`);
process.exit(failed === 0 ? 0 : 1);
