#!/usr/bin/env node

import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const frontendUrl = (
  process.env.QUANT_TUNING_SMOKE_FRONTEND_URL || 'http://127.0.0.1:5173'
).replace(/\/+$/, '');
const backendUrl = new URL(frontendUrl);
backendUrl.port = '8000';
const playwrightPath = process.env.QUANT_TUNING_PLAYWRIGHT_MODULE;
const browserExecutable = process.env.QUANT_TUNING_BROWSER_EXECUTABLE;

if (!playwrightPath) {
  throw new Error('QUANT_TUNING_PLAYWRIGHT_MODULE is required');
}

process.env.QUANT_TUNING_FRONTEND_URL = frontendUrl;
process.env.QUANT_TUNING_BACKEND_URL = backendUrl.origin;

const playwrightModule = await import(
  playwrightPath.startsWith('/') ? pathToFileURL(playwrightPath).href : playwrightPath
);
const playwright = playwrightModule.default ?? playwrightModule;
const { configureBaselineForm } = await import('../run_frontend_non_ml_tuning.mjs');

const strategy = {
  strategy_id: 'liquidity_factor_v1',
  base_params: {
    lookback_days: 21,
    method: 'amihud',
    top_k_pct: 0.1,
  },
};
const metadata = {
  strategy_id: strategy.strategy_id,
  display_name: '流动性因子',
  version: '1.0.0',
  category: 'factor',
  description: '浏览器表单契约烟测策略',
  supported_modes: ['batch'],
  requires_training: false,
  retrain_frequency: 'never',
  training_mode: 'none',
  portfolio_signal_mode: 'target_weights',
  execution_config: {
    param_key: '_execution',
    defaults: {
      initial_capital: 1_000_000,
      max_positions: 10,
      lot_size: 100,
      volume_participation: null,
      commission_rate: 0.0003,
      slippage_rate: 0.001,
      stamp_duty_rate: 0.0005,
      min_commission: 5,
    },
  },
  params: [
    {
      name: 'lookback_days',
      type: 'int',
      default: 21,
      description: '流动性回看交易日',
      required: true,
      min: 5,
      max: 126,
    },
    {
      name: 'method',
      type: 'choice',
      default: 'amihud',
      description: '流动性口径',
      required: true,
      choices: ['amihud', 'amount'],
    },
    {
      name: 'top_k_pct',
      type: 'float',
      default: 0.1,
      description: '买入截面排名比例',
      required: true,
      min: 0.01,
      max: 1,
    },
  ],
  sub_strategies: [],
  integration_method: '',
  tags: ['smoke'],
};
const codes = [
  '000001.SZ',
  '000002.SZ',
  '000063.SZ',
  '000100.SZ',
  '000333.SZ',
  '000651.SZ',
  '000725.SZ',
  '000858.SZ',
  '002230.SZ',
  '002415.SZ',
  '002594.SZ',
  '300014.SZ',
  '300059.SZ',
  '300750.SZ',
  '600000.SH',
  '600009.SH',
  '600028.SH',
  '600030.SH',
  '600036.SH',
  '600050.SH',
  '600104.SH',
  '600276.SH',
  '600309.SH',
  '600519.SH',
  '600690.SH',
  '600887.SH',
  '601012.SH',
  '601318.SH',
  '601398.SH',
  '601888.SH',
];
const config = {
  dataset: { codes, pool_preset: 'custom' },
  windows: {
    selection_start: '2023-07-31',
    selection_end: '2023-12-29',
  },
};
const intent = {
  identity: {
    name: 'current-ui-smoke-baseline-liquidity_factor_v1',
  },
};

const browser = await playwright.chromium.launch({
  headless: true,
  ...(browserExecutable ? { executablePath: browserExecutable } : {}),
});
const context = await browser.newContext({ serviceWorkers: 'block' });
await context.addInitScript(() => {
  window.localStorage.setItem('auth_token', 'smoke-only-token');
});

let readinessPayload = null;
let experimentPayload = null;
await context.route(`${backendUrl.origin}/**`, async (route) => {
  const request = route.request();
  const url = new URL(request.url());
  const json = (data, status = 200) => route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
  });
  if (request.method() === 'GET' && url.pathname === '/api/auth/me') {
    return json({
      id: 1,
      username: 'smoke',
      display_name: 'Smoke',
      is_admin: true,
      permissions: [],
    });
  }
  if (request.method() === 'GET' && url.pathname === '/api/strategies') {
    return json([metadata]);
  }
  if (request.method() === 'GET' && url.pathname === '/api/data/pools') {
    return json([]);
  }
  if (
    request.method() === 'GET'
    && url.pathname === '/api/experiments/parameter-presets'
  ) {
    return json({ items: [], total: 0, page: 1, limit: 20 });
  }
  if (
    request.method() === 'GET'
    && url.pathname === '/api/data/industries'
  ) {
    return json({
      schema_version: 'industry-catalog/v2',
      classification: 'cninfo_008001',
      filterable: false,
      reason: 'smoke-no-scope',
      industries: [],
    });
  }
  if (
    request.method() === 'POST'
    && url.pathname === '/api/data/industries/readiness'
  ) {
    return json({
      schema_version: 'industry-catalog/v2',
      classification: 'cninfo_008001',
      filterable: false,
      reason: 'smoke-no-industry-selection',
      industries: [],
    });
  }
  if (
    request.method() === 'POST'
    && url.pathname === '/api/data/experiment-readiness'
  ) {
    readinessPayload = request.postDataJSON();
    return json({
      ready: true,
      data_access_policy: 'cache_only',
      network_accessed: false,
      market_data: { ready: true, issues: [] },
      benchmark: { ready: true, issues: [] },
    });
  }
  if (request.method() === 'POST' && url.pathname === '/api/experiments/') {
    experimentPayload = request.postDataJSON();
    return json({ experiment_id: 999_999, job_id: 'smoke-not-created' });
  }
  return json(null, 404);
});

const page = await context.newPage();
try {
  await configureBaselineForm(page, strategy, config, intent);
  await page.getByRole('button', { name: '提交运行' }).click();
  await page.waitForURL(/\/experiment\/999999$/, { timeout: 10_000 });

  assert.deepEqual(readinessPayload, {
    data_access_policy: 'cache_only',
    pool_preset: 'custom',
    pool_custom_codes: codes,
    test_start: config.windows.selection_start,
    test_end: config.windows.selection_end,
  });
  assert.equal(experimentPayload.name, intent.identity.name);
  assert.equal(experimentPayload.strategy_id, strategy.strategy_id);
  assert.equal(experimentPayload.pool_preset, 'custom');
  assert.deepEqual(experimentPayload.pool_custom_codes, codes);
  assert.deepEqual(experimentPayload.pool_industries, []);
  assert.equal(experimentPayload.data_access_policy, 'cache_only');
  assert.equal(experimentPayload.test_start, config.windows.selection_start);
  assert.equal(experimentPayload.test_end, config.windows.selection_end);
  assert.deepEqual(experimentPayload.params, strategy.base_params);
  process.stdout.write('current frontend baseline form smoke passed; POST was mocked\n');
} finally {
  await context.close();
  await browser.close();
}
