// Sweeps the whole desk in a real browser against the live sidecar: every route and every Settings section on every
// connection view. Fails on any page or console error, on NaN / undefined / [object Object] / Infinity in the page text,
// and on any figure that disagrees with an independent computation from the raw stats.
// usage: PW_CHROMIUM=/path/to/chromium node scripts/desk-sweep.mjs [shotsDir]
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const BASE = process.env.DESK_URL || "http://127.0.0.1:8080";
const SIDECAR = process.env.PULSE_URL || "http://127.0.0.1:3015";
const SHOTS = process.argv[2] || "";
if (SHOTS) mkdirSync(SHOTS, { recursive: true });
const out = [];
let failed = 0;
const ok = (m) => out.push("OK   " + m);
const fail = (m) => {
  failed += 1;
  out.push("FAIL " + m);
};
const expect = (c, m, d = "") => (c ? ok(m) : fail(m + (d ? " :: " + d : "")));

const BAD = /\bNaN\b|\bundefined\b|\[object Object\]|\bInfinity\b/;
const VIEWS = [
  { conn: "overall", sidecar: "overall", label: "Overall" },
  { conn: "live", sidecar: "live", label: "Live (x01)" },
  { conn: "vst", sidecar: "vst", label: "VST (x02)" },
];
const ROUTES = ["/", "/results", "/system", "/settings"];
const COST = 0.12;

function lastN(closed, n) {
  const usable = closed.filter((r) => r.pnl_pct !== null && r.pnl_pct !== undefined && Number.isFinite(Number(r.pnl_pct)));
  const newest = [...usable].sort((a, b) => Number(b.t) - Number(a.t)).slice(0, n);
  let gp = 0;
  let gl = 0;
  for (const r of newest) {
    const net = Number(r.pnl_pct) - COST / 100;
    if (net > 0) gp += net;
    else if (net < 0) gl += -net;
  }
  const pf = gl > 0 ? gp / gl : gp > 0 ? 99 : 0;
  return { count: newest.length, pf: Math.round(pf * 1e4) / 1e4 };
}

const browser = await chromium.launch({
  ...(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {}),
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
page.setDefaultTimeout(20000);
let errors = [];
page.on("pageerror", (e) => errors.push("pageerror " + e));
page.on("console", (m) => {
  if (m.type() === "error") errors.push("console " + m.text().slice(0, 240));
});
// the payloads the page itself received, newest last: a live engine closes positions between two reads, so a tile is
// compared with the last few payloads the page had, not with a later read of the sidecar
const received = [];
page.on("response", async (r) => {
  if (!/\/stats\.json\?conn=/.test(r.url())) return;
  try {
    received.push(await r.json());
    if (received.length > 4) received.shift();
  } catch {
    /* not JSON: ignore */
  }
});
const noise = (e) => /favicon|Failed to load resource.*(404|401)|ResizeObserver/i.test(e);
const settle = async (ms = 1800) => page.waitForTimeout(ms);
const shot = async (name) => {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true }).catch(() => null);
};

async function visit(route, view) {
  errors = [];
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.evaluate((c) => localStorage.setItem("pulse.connType", c), view.conn);
  await page.goto(BASE + route, { waitUntil: "domcontentloaded" });
  await settle(2200);
  const text = await page.locator("body").innerText();
  const tag = `${view.label} ${route}`;
  const real = errors.filter((e) => !noise(e));
  expect(real.length === 0, `${tag}: no page or console errors`, real.slice(0, 3).join(" | "));
  const bad = text.match(new RegExp(".{0,40}(" + BAD.source + ").{0,40}"));
  expect(!bad, `${tag}: no NaN / undefined / [object Object] / Infinity in the text`, bad ? bad[0] : "");
  expect(text.length > 200, `${tag}: the page has content`, `${text.length} chars`);
  return text;
}

try {
  for (const view of VIEWS) {
    let stats = null;
    try {
      stats = await (await fetch(`${SIDECAR}/stats.json?conn=${view.sidecar}`)).json();
    } catch {
      fail(`${view.label}: the sidecar answers`);
      continue;
    }
    for (const route of ROUTES) {
      const text = await visit(route, view);
      if (route === "/results") {
        const cands = [...received, stats].filter(Boolean);
        const ident = ((await page.getByTestId("results-identity").innerText().catch(() => "")) || "").toLowerCase();
        const headerOk = cands.some((c) => {
          const o = c.openCount ?? (c.open ?? []).length;
          return ident.includes(`${o} open`) && ident.includes(`${(c.closed ?? []).length} closed`);
        });
        expect(headerOk, `${view.label} results: open and closed counts in the header match the stats the page received`, ident);
        // Last 15 PF tile against an independent computation from the raw closes
        const m = text.match(/Last 15 PF\s*\n\s*([^\n]+)/i);
        const got = m ? m[1].trim() : null;
        const shown = got === "—" ? null : got === "no loss" ? 99 : got !== null ? parseFloat(got) : undefined;
        const pfOk = cands.some((c) => {
          const w = lastN(c.closed ?? [], 15);
          return w.count === 0 ? got === "—" : shown !== undefined && shown !== null && Math.abs(shown - Math.min(w.pf, 99)) < 0.006;
        });
        expect(pfOk, `${view.label} results: Last 15 PF matches an independent computation from the closes`, `tile ${got}; candidates ${cands.map((c) => lastN(c.closed ?? [], 15).pf).join(",")}`);
        if (stats.sets?.pfBase?.base) {
          const m2 = text.match(/Effective PF . after Base\s*\n\s*([^\n]+)/i);
          const g2 = m2 ? m2[1].trim() : null;
          const pbOk = cands.some((c) => {
            const pb = c.sets?.pfBase;
            if (!pb?.base) return false;
            const wantPf = pb.base.sets ? pb.base.pf : null;
            return g2 !== null && (wantPf === null ? g2 === "—" : Math.abs(parseFloat(g2) - wantPf) < 0.006) &&
              text.includes(`${pb.base.sets} valid Sets`) && text.includes(`n ${pb.base.n}`);
          });
          expect(pbOk, `${view.label} results: effective PF after Base, valid Sets and orders match the engine's pfBase`, `tile ${g2}`);
        }
        await shot(`results-${view.conn}`);
      }
      if (route === "/") await shot(`desk-${view.conn}`);
      if (route === "/system") await shot(`system-${view.conn}`);
    }
    // every Settings section
    errors = [];
    await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
    await page.evaluate((c) => localStorage.setItem("pulse.connType", c), view.conn);
    await page.goto(BASE + "/settings", { waitUntil: "domcontentloaded" });
    await settle(2200);
    const ids = await page.locator('[data-testid^="section-"]').evaluateAll((els) => els.map((e) => e.getAttribute("data-testid").slice(8)));
    expect(ids.length >= 14, `${view.label} settings: ${ids.length} sections`);
    for (const id of ids) {
      errors = [];
      let opened = false;
      for (let i = 0; i < 4 && !opened; i++) {
        await page.getByTestId(`section-${id}`).click();
        await page.waitForTimeout(350);
        opened = (await page.getByTestId(`section-${id}`).getAttribute("aria-current")) !== null || true;
      }
      await page.waitForTimeout(250);
      const text = await page.locator("main").innerText();
      const real = errors.filter((e) => !noise(e));
      const bad = text.match(new RegExp(".{0,40}(" + BAD.source + ").{0,40}"));
      expect(real.length === 0 && !bad, `${view.label} settings/${id}: no errors, no NaN/undefined`, (real[0] || (bad && bad[0]) || "").slice(0, 200));
    }
    await shot(`settings-${view.conn}`);
  }
} catch (e) {
  fail("script error: " + String(e).slice(0, 300));
}
await browser.close();
console.log(out.join("\n"));
console.log(failed === 0 ? `\nall ${out.length} checks OK` : `\n${failed} FAILED of ${out.length}`);
process.exit(failed === 0 ? 0 : 1);
