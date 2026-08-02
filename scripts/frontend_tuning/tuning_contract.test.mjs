import assert from 'node:assert/strict';
import { test } from 'node:test';
import { resolve } from 'node:path';
import {
  choosePromotionCandidate,
  codesDigest,
  formatGridForFrontend,
  isAllowedBrowserOrigin,
  isGeneratedFactorCombination,
  isExpectedSemanticRead,
  isExpectedWrite,
  loadTuningConfig,
  sanitizeForArtifact,
  validateConfigAgainstApi,
  validateConfigStructure,
  validateLiveOrigins,
} from './tuning_contract.mjs';

const configPath = resolve(
  new URL('.', import.meta.url).pathname,
  'non_ml_tuning.v1.json',
);
const realConfigPath = resolve(
  new URL('.', import.meta.url).pathname,
  'non_ml_single_market_tuning.v2.json',
);

test('preregistered config has 11 non-ML single strategies and 114 selection members', async () => {
  const config = await loadTuningConfig(configPath);
  assert.equal(config.strategies.length, 11);
  assert.equal(config.expected.selection_experiments, 114);
  assert.equal(config.expected.total_experiments, 136);
  assert.deepEqual(config.scope.excluded_categories, ['composite', 'ml']);
  assert.equal(config.scope.requires_training, false);
  const unsafe = structuredClone(config);
  unsafe.scope.excluded_categories = ['ml'];
  assert.throws(
    () => validateConfigStructure(unsafe),
    /exclude composite, ML/,
  );
  assert.equal(
    codesDigest(config.dataset.codes),
    '2ee693c36bca7e34a05f1624d84b90ddeb729e81cdf79c6f40a9cfb6fa91eac6',
  );
  assert.equal(
    config.dataset.frame_digest,
    'dv2|r2412|c180|start2015-01-01|end2024-03-29|sha256:14693f29c18c9d6bdde63cbc3fac935d44af6d8fe143ac7159284f07080bb5e0',
  );
});

test('real-market protocol excludes composite and portfolio strategies', async () => {
  const config = await loadTuningConfig(realConfigPath);
  assert.equal(config.strategies.length, 10);
  assert.equal(config.expected.selection_experiments, 102);
  assert.equal(config.expected.total_experiments, 122);
  assert.deepEqual(config.scope.excluded_categories, ['composite', 'portfolio']);
  assert.equal(config.dataset.pool_preset, 'csi300');
  assert.equal(config.dataset.price_adjustment, 'hfq');
  assert.match(config.dataset.disclosure, /幸存者偏差/);

  const unsafe = structuredClone(config);
  unsafe.scope.excluded_categories = ['composite'];
  assert.throws(
    () => validateConfigStructure(unsafe),
    /exclude composite, portfolio/,
  );

  const metadata = config.strategies.map((strategy, index) => ({
    strategy_id: strategy.strategy_id,
    version: strategy.expected_version,
    category: index < 5 ? 'factor' : 'technical',
    requires_training: false,
    params: Object.entries(strategy.grid).map(([name, candidates]) => ({
      name,
      type: typeof candidates[0] === 'number'
        ? (Number.isInteger(candidates[0]) ? 'int' : 'float')
        : 'choice',
      default: candidates[0],
      choices: typeof candidates[0] === 'string' ? candidates : undefined,
    })),
  }));
  const selected = validateConfigAgainstApi(config, [
    ...metadata,
    {
      strategy_id: 'composite_equal_v1',
      version: '1.0.0',
      category: 'composite',
      requires_training: false,
      params: [],
    },
    {
      strategy_id: 'risk_parity_v1',
      version: '1.0.0',
      category: 'portfolio',
      requires_training: false,
      params: [],
    },
    {
      strategy_id: 'alpha158_lgb_v1',
      version: '1.0.0',
      category: 'ml',
      requires_training: true,
      params: [],
    },
  ]);
  assert.deepEqual(Object.keys(selected).sort(), config.strategies
    .map((strategy) => strategy.strategy_id)
    .sort());
});

test('real-market protocol excludes only generated factor combinations and fails closed for static drift', async () => {
  const config = await loadTuningConfig(realConfigPath);
  const metadata = config.strategies.map((strategy, index) => ({
    strategy_id: strategy.strategy_id,
    version: strategy.expected_version,
    category: index < 5 ? 'factor' : 'technical',
    requires_training: false,
    params: Object.entries(strategy.grid).map(([name, candidates]) => ({
      name,
      type: typeof candidates[0] === 'number'
        ? (Number.isInteger(candidates[0]) ? 'int' : 'float')
        : 'choice',
      default: candidates[0],
      choices: typeof candidates[0] === 'string' ? candidates : undefined,
    })),
  }));
  const generated = {
    strategy_id: 'factor_combo_3d46b1c0d532',
    version: '1.0.0',
    category: 'factor',
    requires_training: false,
    params: [],
  };
  assert.equal(isGeneratedFactorCombination(generated), true);
  assert.equal(isGeneratedFactorCombination({ ...generated, category: 'technical' }), false);
  assert.equal(isGeneratedFactorCombination({ ...generated, strategy_id: 'factor_combo_not_a_digest' }), false);
  assert.deepEqual(
    Object.keys(validateConfigAgainstApi(config, [...metadata, generated])).sort(),
    config.strategies.map((strategy) => strategy.strategy_id).sort(),
  );

  assert.throws(
    () => validateConfigAgainstApi(config, [
      ...metadata,
      { ...generated, strategy_id: 'unexpected_static_rule_v1', category: 'technical' },
    ]),
    /unexpected_static_rule_v1/,
  );
});

test('ranking applies the 0.02 Sharpe near-tie protocol in declared order', () => {
  const metadata = {
    params: [
      { name: 'window', type: 'int', default: 20, min: 10, max: 30 },
    ],
  };
  const experiments = [
    {
      id: 30,
      status: 'completed',
      params: { window: 10 },
      selection_metrics: {
        sharpe_ratio: 1.0,
        max_drawdown: -0.12,
        annual_return: 0.3,
        win_rate: 0.6,
      },
    },
    {
      id: 20,
      status: 'completed',
      params: { window: 20 },
      selection_metrics: {
        sharpe_ratio: 0.985,
        max_drawdown: -0.08,
        annual_return: 0.2,
        win_rate: 0.55,
      },
    },
    {
      id: 10,
      status: 'completed',
      params: { window: 20 },
      selection_metrics: {
        sharpe_ratio: 0.979,
        max_drawdown: -0.01,
        annual_return: 0.8,
        win_rate: 0.9,
      },
    },
  ];
  const result = choosePromotionCandidate(experiments, metadata, 0.02);
  assert.equal(result.candidate.id, 20);
  assert.deepEqual(result.near_tie_ids, [20, 30]);
});

test('ranking continues through later tie-breakers when optional metrics are absent', () => {
  const metadata = {
    params: [
      { name: 'window', type: 'int', default: 20, min: 10, max: 30 },
    ],
  };
  const experiments = [
    {
      id: 30,
      status: 'completed',
      params: { window: 10 },
      selection_metrics: {
        sharpe_ratio: 1.0,
        max_drawdown: null,
        annual_return: null,
        win_rate: null,
      },
    },
    {
      id: 20,
      status: 'completed',
      params: { window: 20 },
      selection_metrics: {
        sharpe_ratio: 0.99,
        max_drawdown: null,
        annual_return: null,
        win_rate: null,
      },
    },
  ];
  assert.equal(choosePromotionCandidate(experiments, metadata, 0.02).candidate.id, 20);
});

test('comma-bearing composite candidates use safe JSON array input', () => {
  assert.equal(
    formatGridForFrontend(['a,b', 'c,d']),
    '["a,b","c,d"]',
  );
  assert.equal(formatGridForFrontend([5, 10, 20]), '5, 10, 20');
});

test('artifact sanitizer removes credential-like values recursively', () => {
  assert.deepEqual(
    sanitizeForArtifact({
      password: 'unsafe',
      nested: { authorization: 'Bearer unsafe', message: 'Bearer abc.def.ghi' },
    }),
    {
      password: '[REDACTED]',
      nested: { authorization: '[REDACTED]', message: 'Bearer [REDACTED]' },
    },
  );
});

test('write allowlist is exact and excludes direct lookalikes', () => {
  assert.equal(
    isExpectedWrite(
      'POST',
      '/api/data/experiment-readiness',
      'readiness',
    ),
    true,
  );
  assert.equal(
    isExpectedWrite(
      'POST',
      '/api/data/experiment-readiness/extra',
      'readiness',
    ),
    false,
  );
  assert.equal(isExpectedWrite('POST', '/api/experiments/', 'baseline'), true);
  assert.equal(isExpectedWrite('POST', '/api/experiments', 'baseline'), false);
  assert.equal(isExpectedWrite('POST', '/api/experiments//', 'baseline'), false);
  assert.equal(isExpectedWrite('POST', '/api/experiments/sweep', 'baseline'), false);
  assert.equal(isExpectedWrite('POST', '/api/experiments/sweep/42/promote', 'promote'), true);
  assert.equal(isExpectedWrite('GET', '/api/experiments/sweep/42/promote', 'promote'), false);
});

test('industry readiness POST is an exact, payload-validated semantic read', () => {
  const validPayload = { codes: ['000001', '600000.SH', '430047.bj'] };
  assert.equal(
    isExpectedSemanticRead('POST', '/api/data/industries/readiness', validPayload),
    true,
  );
  assert.equal(
    isExpectedSemanticRead('GET', '/api/data/industries/readiness', validPayload),
    false,
  );
  assert.equal(
    isExpectedSemanticRead('POST', '/api/data/industries/readiness/extra', validPayload),
    false,
  );
  assert.equal(
    isExpectedSemanticRead('POST', '/api/data/industries/refresh', validPayload),
    false,
  );
  assert.equal(
    isExpectedSemanticRead('POST', '/api/data/industries/readiness', { codes: [] }),
    false,
  );
  assert.equal(
    isExpectedSemanticRead(
      'POST',
      '/api/data/industries/readiness',
      { codes: ['000001'], force_refresh: true },
    ),
    false,
  );
  assert.equal(
    isExpectedSemanticRead('POST', '/api/data/industries/readiness', { codes: ['bad'] }),
    false,
  );
});

test('live credentials can only be sent to explicit loopback origins', () => {
  assert.deepEqual(
    validateLiveOrigins('http://localhost:5173/', 'http://127.0.0.1:8000'),
    {
      frontend: 'http://localhost:5173',
      backend: 'http://127.0.0.1:8000',
    },
  );
  assert.equal(
    validateLiveOrigins('http://[::1]:5173', 'http://[::1]:8000').frontend,
    'http://[::1]:5173',
  );
  assert.throws(
    () => validateLiveOrigins('https://example.com', 'http://localhost:8000'),
    /loopback/,
  );
  assert.throws(
    () => validateLiveOrigins('http://user:pass@localhost:5173', 'http://localhost:8000'),
    /credentials/,
  );
  assert.throws(
    () => validateLiveOrigins('http://localhost:5173/app', 'http://localhost:8000'),
    /only an origin/,
  );
  assert.equal(
    isAllowedBrowserOrigin(
      'http://localhost:8000/api/experiments',
      'http://localhost:5173',
      'http://localhost:8000',
    ),
    true,
  );
  assert.equal(
    isAllowedBrowserOrigin(
      'http://127.0.0.1:8000/api/experiments',
      'http://localhost:5173',
      'http://localhost:8000',
    ),
    false,
  );
  assert.equal(
    isAllowedBrowserOrigin(
      'https://example.com/api/experiments',
      'http://localhost:5173',
      'http://localhost:8000',
    ),
    false,
  );
});
