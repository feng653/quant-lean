#!/usr/bin/env node

import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

function argumentsFrom(argv) {
  const result = { all: false };
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (item === '--all') result.all = true;
    else if (item === '--base-url') result.baseUrl = argv[++index];
    else if (item === '--api-url') result.apiUrl = argv[++index];
    else if (item === '--report') result.report = argv[++index];
    else throw new Error(`Unknown argument: ${item}`);
  }
  if (!result.baseUrl || !result.apiUrl || !result.report) {
    throw new Error('--base-url, --api-url and --report are required');
  }
  return result;
}

async function loadPlaywright() {
  const configured = process.env.PLAYWRIGHT_MODULE || 'playwright';
  const target = configured.startsWith('/')
    ? pathToFileURL(configured).href
    : configured;
  return import(target);
}

async function authenticatedJson(page, apiUrl, pathname) {
  return page.evaluate(async ({ requestBase, requestPath }) => {
    const token = localStorage.getItem('auth_token');
    const response = await fetch(`${requestBase}${requestPath}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    const text = await response.text();
    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { raw: text };
    }
    return { status: response.status, payload };
  }, { requestBase: apiUrl, requestPath: pathname });
}

function nonMlSingleStrategies(strategies) {
  const excludedCategories = new Set(['ml', 'portfolio', 'composite']);
  return strategies
    .filter((item) => (
      item.requires_training === false
      && !excludedCategories.has(item.category)
      && Array.isArray(item.sub_strategies)
      && item.sub_strategies.length === 0
    ))
    .sort((left, right) => left.strategy_id.localeCompare(right.strategy_id));
}

function representativeStrategies(candidates) {
  const factor = candidates.find((item) => item.category === 'factor');
  const technical = candidates.filter((item) => item.category === 'technical').slice(0, 2);
  if (!factor || technical.length < 2) {
    throw new Error('Registry does not expose one factor and two technical single strategies');
  }
  return [factor, ...technical];
}

async function advanceWizard(page, nextHeading) {
  const next = page.getByRole('button', { name: '下一步', exact: true });
  const deadline = Date.now() + 30_000;
  while (await next.isDisabled()) {
    if (Date.now() >= deadline) {
      throw new Error(`Wizard cannot advance to ${nextHeading}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  await next.click();
  await page.getByRole('heading', { name: nextHeading, exact: true }).waitFor();
}

async function createFromFrontend(page, strategy, index) {
  await page.goto(`/experiment/new?strategy_id=${encodeURIComponent(strategy.strategy_id)}`);
  await page.getByRole('radio', { name: new RegExp(strategy.display_name) }).waitFor();
  await advanceWizard(page, '配置参数');
  await advanceWizard(page, '选择股票池');
  await page.getByLabel('股票池', { exact: true }).selectOption('csi300');
  await page.waitForFunction(() => {
    const select = document.querySelector('select');
    return select?.value === 'csi300';
  });
  await advanceWizard(page, '选择时间');
  const dateInputs = page.locator('input[type="date"]');
  if (await dateInputs.count() !== 2) {
    throw new Error('Non-training strategy must expose exactly two test date inputs');
  }
  await dateInputs.nth(0).fill('2024-01-02');
  await dateInputs.nth(1).fill('2024-03-29');
  await advanceWizard(page, '确认运行');
  await page.getByLabel(/实验名称/).fill(
    `PIT QA ${index + 1} · ${strategy.strategy_id}`,
  );
  await page.getByRole('button', { name: '提交运行' }).click();
  await page.waitForURL(/\/experiment\/\d+$/, { timeout: 120_000 });
  const match = page.url().match(/\/experiment\/(\d+)$/);
  if (!match) throw new Error(`Experiment detail navigation missing: ${page.url()}`);
  return Number(match[1]);
}

async function waitForCompleted(page, apiUrl, experimentId) {
  const deadline = Date.now() + 12 * 60 * 1000;
  while (Date.now() < deadline) {
    const response = await authenticatedJson(
      page,
      apiUrl,
      `/api/experiments/${experimentId}`,
    );
    if (response.status !== 200) {
      throw new Error(`Experiment ${experimentId} read failed: HTTP ${response.status}`);
    }
    const experiment = response.payload.data;
    if (experiment?.status === 'completed') return experiment;
    if (['failed', 'cancelled'].includes(experiment?.status)) {
      throw new Error(
        `Experiment ${experimentId} ended as ${experiment.status}: ${experiment.error_log || ''}`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(`Experiment ${experimentId} did not complete before timeout`);
}

async function main() {
  const options = argumentsFrom(process.argv.slice(2));
  const playwrightModule = await loadPlaywright();
  const chromium = playwrightModule.chromium || playwrightModule.default?.chromium;
  if (!chromium) throw new Error('Configured Playwright module has no chromium export');
  const launchOptions = { headless: true };
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) {
    launchOptions.executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  }
  const browser = await chromium.launch(launchOptions);
  const page = await browser.newPage({ baseURL: options.baseUrl });
  const browserErrors = [];
  page.on('pageerror', (error) => browserErrors.push(String(error)));
  const report = {
    schema_version: 'pit-qa-browser-report/v1',
    production_eligible: false,
    mode: options.all ? 'all_non_ml_single_strategies' : 'representative_three',
    experiments: [],
  };
  try {
    await page.goto('/login');
    await page.getByLabel('用户名', { exact: true }).fill(
      process.env.PIT_QA_USERNAME || 'qa_admin',
    );
    await page.getByLabel('密码', { exact: true }).fill(
      process.env.PIT_QA_PASSWORD || 'qa-admin-123',
    );
    await page.getByRole('button', { name: '登录' }).click();
    await page.waitForURL((url) => url.pathname === '/', { timeout: 30_000 });
    const registryResponse = await authenticatedJson(
      page,
      options.apiUrl,
      '/api/strategies/',
    );
    if (registryResponse.status !== 200 || !Array.isArray(registryResponse.payload.data)) {
      throw new Error(`Strategy registry unavailable: HTTP ${registryResponse.status}`);
    }
    const candidates = nonMlSingleStrategies(registryResponse.payload.data);
    const selected = options.all ? candidates : representativeStrategies(candidates);
    report.registry_selection = {
      candidate_count: candidates.length,
      strategy_ids: selected.map((item) => item.strategy_id),
      rule: 'requires_training=false; category not ml/portfolio/composite; sub_strategies empty',
    };
    for (let index = 0; index < selected.length; index += 1) {
      const strategy = selected[index];
      const experimentId = await createFromFrontend(page, strategy, index);
      const experiment = await waitForCompleted(page, options.apiUrl, experimentId);
      const evidenceResponse = await authenticatedJson(
        page,
        options.apiUrl,
        `/api/experiments/${experimentId}/export?format=json`,
      );
      if (evidenceResponse.status !== 200) {
        throw new Error(`Experiment ${experimentId} evidence export failed`);
      }
      const evidence = evidenceResponse.payload;
      const manifest = evidence.research_manifest?.manifest;
      const manifestHash = evidence.research_manifest?.manifest_hash;
      if (
        !manifestHash
        || manifest?.pit_runtime?.verified !== true
        || manifest?.pit_runtime?.production_eligible !== false
        || manifest?.pit_runtime?.qa_runtime_attestation?.non_production !== true
      ) {
        throw new Error(`Experiment ${experimentId} has no isolated PIT QA manifest`);
      }
      report.experiments.push({
        experiment_id: experimentId,
        strategy_id: strategy.strategy_id,
        category: strategy.category,
        status: experiment.status,
        manifest_hash: manifestHash,
        timeline_hash: manifest.pit_runtime.timeline_hash,
        membership_batch_ids: manifest.universe.timeline_identity.source_batches.map(
          (item) => item.batch_id,
        ),
        canonical_price_binding_id: manifest.pit_runtime.canonical_price_binding_id,
        canonical_price_binding_digest: manifest.pit_runtime.canonical_price_binding_digest,
        data_version: experiment.data_version,
        frontend_path: `/experiment/${experimentId}`,
      });
    }
    report.status = 'browser-completed';
    report.browser_errors = browserErrors;
  } finally {
    await browser.close();
    await fs.mkdir(path.dirname(options.report), { recursive: true });
    await fs.writeFile(options.report, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  }
  process.stdout.write(`${JSON.stringify(report)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
