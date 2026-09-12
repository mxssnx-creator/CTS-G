import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { writeFileSync } from 'node:fs';

const root = '/workspace/CTS-G/reports/continuous-7d-20260912';
const browser = await chromium.launch({
  executablePath: process.env.BROWSER_EXECUTABLE_PATH || undefined,
  headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [], saved = [];
  page.on('pageerror', error => errors.push(error.message));
  // These optional workspace resources are outside this settings-contract
  // check. The separate raw browser smoke retains their network failures.
  await page.route('**/css2?*', r => r.fulfill({ contentType: 'text/css', body: '' }));
  await page.route('**/grok-app-builder/extensions.js', r => r.fulfill({ contentType: 'text/javascript', body: '' }));
  await page.route('**/config.json**', async route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON(); saved.push(body);
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, ...body }) });
    } else await route.continue();
  });
  await Promise.all([page.waitForRequest(r => r.url().includes('/connections.json')), page.goto(process.argv[2] || 'http://127.0.0.1:8081/settings')]);
  await page.getByTestId('section-profit').click();
  const pf = page.locator('label').filter({ hasText: 'Overall minimum PF' }).locator('input[type=number]');
  assert.equal(await pf.inputValue(), '1.05');
  await pf.fill('1.2');
  await page.screenshot({ path: root + '/overall-pf-settings.png' });
  await page.getByTestId('section-sets').click();
  const base = page.locator('label').filter({ hasText: 'Base evaluation · last positions' }).locator('input[type=number]');
  assert.equal(await base.inputValue(), '30');
  await base.fill('75');
  const response = page.waitForResponse(r => r.url().includes('/config.json') && r.request().method() === 'POST');
  await page.getByRole('button', { name: 'Save to VST', exact: true }).click();
  await response;
  const payload = saved.find(p => (p.overlay || p).baseEvalPosCount === 75);
  assert.ok(payload, 'The saved VST payload includes Base last75');
  const overlay = payload.overlay || payload;
  for (const key of ['minPf','baseMinPf','mainMinPf','realMinPf','setMinPf','dcaMinPf','exitMinPf']) assert.equal(overlay[key], 1.2, key);
  assert.equal(overlay.setPfWindow, 75);
  assert.equal(overlay.setMinSamples, 75);
  assert.deepEqual(errors, []);
  await page.screenshot({ path: root + '/base-evaluation-settings.png' });
  const result = { ok: true, checked: 'single PF control, all stage aliases, Base default30 and saved75',
                   exchangeAccess: false, pageErrors: errors, optionalExternalResourcesStubbed: true };
  writeFileSync(root + '/settings-contract.json', JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
} finally { await browser.close(); }
