#!/usr/bin/env node
/**
 * Capture documentation screenshots from a running local frontend.
 *
 * Usage:
 *   QUANT_DOC_USERNAME=... QUANT_DOC_PASSWORD=... node scripts/capture_readme_screenshots.mjs
 *
 * The credentials are intentionally read only from the process environment and
 * are never printed or persisted.  The script writes only public UI captures.
 */
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
const playwrightModule = process.env.QUANT_DOC_PLAYWRIGHT_MODULE ?? 'playwright';
const { default: playwright } = await import(playwrightModule);
const { chromium } = playwright;

const baseUrl = process.env.QUANT_DOC_BASE_URL ?? 'http://127.0.0.1:5173';
const username = process.env.QUANT_DOC_USERNAME;
const password = process.env.QUANT_DOC_PASSWORD;
const outputDir = resolve('docs/assets/readme');
const executablePath = process.env.QUANT_DOC_CHROMIUM_PATH;

if (!username || !password) {
  throw new Error('QUANT_DOC_USERNAME and QUANT_DOC_PASSWORD are required.');
}

const captures = [
  ['02-dashboard.png', '/'],
  ['03-data-governance.png', '/data'],
  ['04-experiment-center.png', '/experiment'],
  ['04a-strategy-library.png', '/strategies'],
  ['05-new-experiment.png', '/experiment/new'],
  ['06-factor-research.png', '/factor-research'],
  ['07-strategy-correlation.png', '/experiment/correlation'],
  ['08-job-center.png', '/jobs'],
  ['09-paper-trading.png', '/trading'],
  ['09a-execution-safety.png', '/trading/brokers'],
  ['10-model-lifecycle.png', '/trading/models'],
  ['11-administration.png', '/admin'],
];

await mkdir(outputDir, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  ...(executablePath ? { executablePath } : {}),
});
const context = await browser.newContext({ viewport: { width: 1440, height: 1050 }, deviceScaleFactor: 1 });
const page = await context.newPage();

try {
  await page.goto(`${baseUrl}/login`, { waitUntil: 'networkidle' });
  await page.screenshot({ path: resolve(outputDir, '01-login.png'), fullPage: true });
  await page.getByLabel('用户名', { exact: true }).fill(username);
  await page.getByLabel('密码', { exact: true }).fill(password);
  await Promise.all([
    page.waitForURL((url) => !url.pathname.endsWith('/login'), { timeout: 20_000 }),
    page.getByRole('button', { name: '登录' }).click(),
  ]);

  for (const [filename, pathname] of captures) {
    await page.goto(`${baseUrl}${pathname}`, { waitUntil: 'networkidle', timeout: 30_000 });
    await page.screenshot({ path: resolve(outputDir, filename), fullPage: true });
  }
} finally {
  await browser.close();
}
