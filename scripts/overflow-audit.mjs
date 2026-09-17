import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";

const pages = ["/", "/results", "/settings", "/system", "/step-sweep"];
const viewports = [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 390, height: 844 },
];
mkdirSync("/workspace/screenshots", { recursive: true });
const browser = await chromium.launch({ args: ["--no-sandbox"] });
const findings = [];
for (const vp of viewports) {
  const context = await browser.newContext({ viewport: { width: vp.width, height: vp.height } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (msg) => { if (msg.type() === "error") errors.push(msg.text()); });
  for (const path of pages) {
    errors.length = 0;
    const url = `http://127.0.0.1:8080${path}`;
    await page.goto(url, { waitUntil: "networkidle", timeout: 45000 });
    await page.waitForTimeout(700);
    const metrics = await page.evaluate(() => {
      const doc = document.documentElement;
      const overflowing = [...document.querySelectorAll("body *")].filter((el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.right > window.innerWidth + 2;
      }).slice(0, 8).map((el) => ({
        tag: el.tagName.toLowerCase(),
        cls: (el.className || "").toString().slice(0, 80),
        right: Math.round(el.getBoundingClientRect().right),
        text: (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 60),
      }));
      return {
        scrollWidth: doc.scrollWidth,
        innerWidth: window.innerWidth,
        overflow: doc.scrollWidth > window.innerWidth + 2,
        overflowing,
      };
    });
    const shot = `/workspace/screenshots/style-${vp.name}${path === "/" ? "-desk" : path.replace("/", "-")}.png`;
    await page.screenshot({ path: shot, fullPage: false });
    findings.push({ vp: vp.name, path, errors: [...errors], ...metrics, shot });
  }
  await context.close();
}
await browser.close();
writeFileSync("/workspace/screenshots/style-overflow.json", JSON.stringify(findings, null, 2));
const bad = findings.filter((f) => f.overflow || f.errors.length);
console.log(JSON.stringify({ ok: bad.length === 0, bad, pages: findings.map((f) => ({ vp: f.vp, path: f.path, overflow: f.overflow, errors: f.errors.length, scrollWidth: f.scrollWidth, innerWidth: f.innerWidth })) }, null, 2));
process.exit(bad.length ? 1 : 0);
