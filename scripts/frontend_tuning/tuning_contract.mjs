import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';

export const CONTRACT_SCHEMA = 'quant-platform/frontend-non-ml-single-tuning/v2';
export const REAL_SINGLE_CONTRACT_SCHEMA = 'quant-platform/frontend-single-strategy-tuning/v2';
export const STATE_SCHEMA = 'quant-platform/frontend-non-ml-tuning-state/v1';
export const REPORT_SCHEMA = 'quant-platform/frontend-non-ml-tuning-report/v1';
export const WRITE_ENDPOINTS = Object.freeze({
  readiness: {
    method: 'POST',
    pathname: '/api/data/experiment-readiness',
  },
  baseline: { method: 'POST', pathname: '/api/experiments/' },
  sweep: { method: 'POST', pathname: '/api/experiments/sweep' },
  repair: {
    method: 'POST',
    pathname: /^\/api\/experiments\/sweep\/\d+\/repair$/,
  },
  promote: {
    method: 'POST',
    pathname: /^\/api\/experiments\/sweep\/\d+\/promote$/,
  },
});

const INDUSTRY_READINESS_PATH = '/api/data/industries/readiness';
const INDUSTRY_CODE = /^\d{6}(?:\.(?:SH|SZ|BJ))?$/i;
// Factor Research exports data-defined combinations into the registry at runtime.
// They are deliberately not part of a preregistered static single-strategy
// campaign.  Keep this deliberately narrow: an unrecognised non-training
// strategy must still make the contract fail closed rather than disappearing
// from the campaign by name alone.
const GENERATED_FACTOR_COMBINATION_ID = /^factor_combo_[0-9a-f]{12}$/;

const SECRET_KEY = /(?:authorization|cookie|password|passwd|secret|token|credential)/i;
const BEARER_VALUE = /Bearer\s+[A-Za-z0-9._~+/=-]+/gi;

export async function loadTuningConfig(path) {
  const raw = await readFile(path, 'utf8');
  const config = JSON.parse(raw);
  validateConfigStructure(config);
  return config;
}

export function combinationsInGrid(grid) {
  const lengths = Object.values(grid).map((values) => values.length);
  return lengths.reduce((total, length) => total * length, lengths.length ? 1 : 0);
}

export function cartesianGrid(grid) {
  return Object.entries(grid).reduce(
    (items, [name, values]) =>
      items.flatMap((item) => values.map((value) => ({ ...item, [name]: value }))),
    [{}],
  );
}

export function customCacheKey(codes) {
  const digest = createHash('sha256')
    .update([...codes].sort().join(','))
    .digest('hex')
    .slice(0, 16);
  return `custom_${digest}`;
}

export function codesDigest(codes) {
  return createHash('sha256').update([...codes].sort().join(',')).digest('hex');
}

export function validateLiveOrigins(frontendValue, backendValue) {
  const validate = (value, label) => {
    let url;
    try {
      url = new URL(value);
    } catch {
      throw new Error(`${label} must be an absolute URL`);
    }
    if (!['http:', 'https:'].includes(url.protocol)) {
      throw new Error(`${label} must use http or https`);
    }
    if (!['localhost', '127.0.0.1', '::1', '[::1]'].includes(url.hostname)) {
      throw new Error(`${label} must remain on the loopback interface`);
    }
    if (url.username || url.password) {
      throw new Error(`${label} must not embed credentials`);
    }
    if ((url.pathname && url.pathname !== '/') || url.search || url.hash) {
      throw new Error(`${label} must contain only an origin`);
    }
    return url.origin;
  };
  return {
    frontend: validate(frontendValue, 'Frontend URL'),
    backend: validate(backendValue, 'Backend URL'),
  };
}

export function isAllowedBrowserOrigin(urlValue, frontendOrigin, backendOrigin) {
  let origin;
  try {
    origin = new URL(urlValue).origin;
  } catch {
    return false;
  }
  return origin === frontendOrigin || origin === backendOrigin;
}

function requireIsoDate(value, name) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value ?? '')) {
    throw new Error(`${name} must be an ISO date`);
  }
}

export function validateConfigStructure(config) {
  if (![CONTRACT_SCHEMA, REAL_SINGLE_CONTRACT_SCHEMA].includes(config?.schema_version)) {
    throw new Error(`Unsupported tuning config schema: ${config?.schema_version ?? 'missing'}`);
  }
  const { dataset, windows, strategies, expected, ranking } = config;
  const realSingle = config.schema_version === REAL_SINGLE_CONTRACT_SCHEMA;
  if (realSingle) {
    if (
      dataset?.kind !== 'cross_validated_market_research'
      || dataset?.evidence_level !== 'public_cross_validated'
      || !['csi300', 'csi500', 'csi800', 'csi1000'].includes(dataset?.pool_preset)
      || dataset?.cache_key !== dataset?.pool_preset
    ) {
      throw new Error('Real single-strategy campaigns require one cross-validated index cache');
    }
    if (!Array.isArray(dataset.codes) || dataset.codes.length !== 0) {
      throw new Error('Index-pool campaigns must not freeze a custom-code list');
    }
    if (dataset.price_adjustment !== 'hfq') {
      throw new Error('Cross-validated return research requires the hfq contract');
    }
    if (
      dataset.source_trust !== 'public_cross_validated_research_only'
      || JSON.stringify(dataset.providers) !== JSON.stringify(['baostock:official'])
    ) {
      throw new Error('Real campaign source identity must match the reviewed cross-validation adapter');
    }
    if (!Number.isInteger(dataset.minimum_dates) || dataset.minimum_dates < 1000) {
      throw new Error('Real market campaigns require at least 1000 trading dates');
    }
    if (
      !Array.isArray(config.scope?.excluded_categories)
      || JSON.stringify([...config.scope.excluded_categories].sort())
        !== JSON.stringify(['composite', 'portfolio'])
      || config.scope?.requires_training !== false
    ) {
      throw new Error('Real campaign scope must exclude composite, portfolio and training strategies');
    }
    if (
      typeof dataset.disclosure !== 'string'
      || dataset.disclosure.length < 40
      || !dataset.disclosure.includes('幸存者偏差')
    ) {
      throw new Error('Real campaign must disclose current-constituent survivorship bias');
    }
  } else {
    if (
      dataset?.kind !== 'deterministic_synthetic'
      || dataset?.evidence_level !== 'declared'
      || dataset?.pool_preset !== 'custom'
    ) {
      throw new Error('This campaign must remain explicitly declared deterministic synthetic data');
    }
    if (!Array.isArray(dataset.codes) || dataset.codes.length !== 30) {
      throw new Error('The campaign requires exactly 30 custom stock codes');
    }
    if (new Set(dataset.codes).size !== dataset.codes.length) {
      throw new Error('Custom stock codes must be unique');
    }
    if (dataset.cache_key !== customCacheKey(dataset.codes)) {
      throw new Error('dataset.cache_key does not match the sorted custom-code digest');
    }
    if (
      typeof dataset.frame_digest !== 'string'
      || !/^dv2\|.+\|sha256:[0-9a-f]{64}$/.test(dataset.frame_digest)
    ) {
      throw new Error('The campaign requires a canonical declared-cache frame digest');
    }
    if (dataset.price_adjustment !== 'qfq') {
      throw new Error('The declared cache must use the qfq adjustment contract');
    }
    if (
      !Array.isArray(config.scope?.excluded_categories)
      || JSON.stringify([...config.scope.excluded_categories].sort())
        !== JSON.stringify(['composite', 'ml'])
      || config.scope?.requires_training !== false
    ) {
      throw new Error('Synthetic single-strategy scope must exclude composite, ML and training strategies');
    }
  }
  if (
    JSON.stringify(dataset.required_fields)
    !== JSON.stringify(['amount', 'close', 'high', 'low', 'open', 'volume'])
  ) {
    throw new Error('The declared cache field contract has drifted');
  }
  if (!realSingle && (!Number.isInteger(dataset.n_dates) || dataset.n_dates <= 0)) {
    throw new Error('The declared cache requires a positive date count');
  }
  for (const [name, value] of Object.entries(windows ?? {})) {
    requireIsoDate(value, `windows.${name}`);
  }
  if (
    windows.selection_start >= windows.selection_end
    || windows.selection_end >= windows.locked_test_start
    || windows.locked_test_start >= windows.locked_test_end
  ) {
    throw new Error('Selection and locked-test windows must be strictly ordered and disjoint');
  }
  if (!realSingle && (
    windows.selection_start !== '2023-07-31'
    || windows.selection_end !== '2023-12-29'
    || windows.locked_test_start !== '2024-01-02'
    || windows.locked_test_end !== '2024-03-29'
  )) {
    throw new Error('Campaign windows differ from the preregistered protocol');
  }
  if (!Array.isArray(strategies) || strategies.length !== expected?.strategy_count) {
    throw new Error('Configured strategies differ from the declared campaign count');
  }
  const strategyIds = strategies.map((item) => item.strategy_id);
  if (new Set(strategyIds).size !== strategyIds.length) {
    throw new Error('Strategy IDs must be unique');
  }
  let selectionTotal = 0;
  for (const strategy of strategies) {
    if (!strategy.strategy_id || !strategy.expected_version) {
      throw new Error('Every strategy requires an ID and expected version');
    }
    if (!strategy.grid || Object.keys(strategy.grid).length === 0) {
      throw new Error(`${strategy.strategy_id} has an empty parameter grid`);
    }
    for (const [name, values] of Object.entries(strategy.grid)) {
      if (!name || !Array.isArray(values) || values.length === 0) {
        throw new Error(`${strategy.strategy_id}.${name || '<empty>'} has no candidates`);
      }
    }
    selectionTotal += combinationsInGrid(strategy.grid);
  }
  if (
    !Number.isInteger(expected?.strategy_count)
    || expected.strategy_count <= 0
    || expected.baseline_experiments !== expected.strategy_count
    || expected.selection_experiments !== selectionTotal
    || expected.locked_test_experiments !== expected.strategy_count
    || expected.total_experiments !== (
      expected.baseline_experiments
      + expected.selection_experiments
      + expected.locked_test_experiments
    )
    || expected.persistent_sweep_tabs !== expected.strategy_count
    || (!realSingle && (
      expected.strategy_count !== 11
      || selectionTotal !== 114
      || expected.total_experiments !== 136
    ))
  ) {
    throw new Error(`Campaign cardinality mismatch; selection grid currently totals ${selectionTotal}`);
  }
  if (
    ranking?.primary !== 'sharpe_ratio'
    || ranking?.near_tie_tolerance !== 0.02
    || JSON.stringify(ranking?.tie_breakers) !== JSON.stringify([
      'max_drawdown_closest_to_zero',
      'annual_return_desc',
      'win_rate_desc',
      'default_parameter_distance_asc',
      'experiment_id_asc',
    ])
  ) {
    throw new Error('Ranking protocol differs from the preregistered Sharpe tie-break contract');
  }
  return { strategy_count: strategies.length, selection_experiments: selectionTotal };
}

function normalizeParamType(type) {
  return String(type).toLowerCase();
}

function assertCandidateMatchesField(strategyId, field, candidate) {
  const type = normalizeParamType(field.type);
  if (['int', 'integer'].includes(type)) {
    if (typeof candidate !== 'number' || !Number.isInteger(candidate)) {
      throw new Error(`${strategyId}.${field.name} requires integer candidates`);
    }
  } else if (['float', 'number'].includes(type)) {
    if (typeof candidate !== 'number' || !Number.isFinite(candidate)) {
      throw new Error(`${strategyId}.${field.name} requires finite numeric candidates`);
    }
  } else if (['bool', 'boolean'].includes(type)) {
    if (typeof candidate !== 'boolean') {
      throw new Error(`${strategyId}.${field.name} requires boolean candidates`);
    }
  } else if (['choice', 'str', 'string', 'text'].includes(type)) {
    if (typeof candidate !== 'string') {
      throw new Error(`${strategyId}.${field.name} requires string candidates`);
    }
  }
  if (typeof candidate === 'number') {
    if (field.min != null && candidate < field.min) {
      throw new Error(`${strategyId}.${field.name} candidate is below API minimum`);
    }
    if (field.max != null && candidate > field.max) {
      throw new Error(`${strategyId}.${field.name} candidate is above API maximum`);
    }
  }
  if (Array.isArray(field.choices) && !field.choices.includes(candidate)) {
    throw new Error(`${strategyId}.${field.name} candidate is outside API choices`);
  }
}

export function validateConfigAgainstApi(config, apiStrategies) {
  validateConfigStructure(config);
  if (!Array.isArray(apiStrategies)) throw new Error('Strategy API did not return a list');
  const excluded = new Set(config.scope?.excluded_categories ?? []);
  const nonTraining = apiStrategies.filter(
    (item) => item.requires_training === false
      && !excluded.has(item.category)
      && !isGeneratedFactorCombination(item),
  );
  const configuredIds = config.strategies.map((item) => item.strategy_id).sort();
  const apiIds = nonTraining.map((item) => item.strategy_id).sort();
  if (JSON.stringify(configuredIds) !== JSON.stringify(apiIds)) {
    throw new Error(
      `Configured non-training strategies differ from API: config=${configuredIds.join(',')} api=${apiIds.join(',')}`,
    );
  }
  const metadataById = Object.fromEntries(nonTraining.map((item) => [item.strategy_id, item]));
  for (const strategy of config.strategies) {
    const metadata = metadataById[strategy.strategy_id];
    if (!metadata || metadata.requires_training !== false) {
      throw new Error(`${strategy.strategy_id} is missing or requires training`);
    }
    if (metadata.version !== strategy.expected_version) {
      throw new Error(
        `${strategy.strategy_id} version drift: expected ${strategy.expected_version}, got ${metadata.version}`,
      );
    }
    if (!Array.isArray(metadata.params)) {
      throw new Error(`${strategy.strategy_id} API parameter schema is missing`);
    }
    const fields = Object.fromEntries(metadata.params.map((field) => [field.name, field]));
    for (const [name, candidates] of Object.entries(strategy.grid)) {
      const field = fields[name];
      if (!field) throw new Error(`${strategy.strategy_id}.${name} is absent from the API schema`);
      for (const candidate of candidates) {
        assertCandidateMatchesField(strategy.strategy_id, field, candidate);
      }
    }
    strategy.base_params = Object.fromEntries(
      metadata.params.map((field) => [field.name, field.default]),
    );
    strategy.api_metadata = metadata;
  }
  return metadataById;
}

export function isGeneratedFactorCombination(metadata) {
  return (
    metadata?.category === 'factor'
    && GENERATED_FACTOR_COMBINATION_ID.test(String(metadata?.strategy_id ?? ''))
  );
}

function finiteOr(value, fallback) {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

export function defaultParameterDistance(params, metadata) {
  let distance = 0;
  for (const field of metadata.params ?? []) {
    const current = params?.[field.name];
    const expected = field.default;
    if (typeof current === 'number' && typeof expected === 'number') {
      const range = (
        typeof field.min === 'number'
        && typeof field.max === 'number'
        && field.max > field.min
      )
        ? field.max - field.min
        : Math.max(Math.abs(expected), 1);
      distance += Math.abs(current - expected) / range;
    } else {
      distance += Object.is(current, expected) ? 0 : 1;
    }
  }
  return distance;
}

export function choosePromotionCandidate(experiments, metadata, tolerance = 0.02) {
  const completed = experiments.filter(
    (item) =>
      item.status === 'completed'
      && Number.isFinite(item.selection_metrics?.sharpe_ratio),
  );
  if (completed.length === 0) {
    throw new Error('No completed sweep member has a finite selection Sharpe');
  }
  const bestSharpe = Math.max(
    ...completed.map((item) => item.selection_metrics.sharpe_ratio),
  );
  const nearTies = completed.filter(
    (item) => bestSharpe - item.selection_metrics.sharpe_ratio <= tolerance + Number.EPSILON,
  );
  const compareAscending = (left, right) => {
    if (left === right) return 0;
    return left < right ? -1 : 1;
  };
  nearTies.sort((left, right) => {
    const leftMetrics = left.selection_metrics;
    const rightMetrics = right.selection_metrics;
    const drawdown = compareAscending(
      Math.abs(finiteOr(leftMetrics.max_drawdown, Number.POSITIVE_INFINITY)),
      Math.abs(finiteOr(rightMetrics.max_drawdown, Number.POSITIVE_INFINITY)),
    );
    if (drawdown !== 0) return drawdown;
    const annualReturn = compareAscending(
      finiteOr(rightMetrics.annual_return, Number.NEGATIVE_INFINITY),
      finiteOr(leftMetrics.annual_return, Number.NEGATIVE_INFINITY),
    );
    if (annualReturn !== 0) return annualReturn;
    const winRate = compareAscending(
      finiteOr(rightMetrics.win_rate, Number.NEGATIVE_INFINITY),
      finiteOr(leftMetrics.win_rate, Number.NEGATIVE_INFINITY),
    );
    if (winRate !== 0) return winRate;
    const parameterDistance = (
      defaultParameterDistance(left.params, metadata)
      - defaultParameterDistance(right.params, metadata)
    );
    if (parameterDistance !== 0) return parameterDistance;
    return Number(left.id) - Number(right.id);
  });
  return {
    candidate: nearTies[0],
    best_sharpe: bestSharpe,
    near_tie_ids: nearTies.map((item) => Number(item.id)),
  };
}

export function formatGridForFrontend(values) {
  if (values.some((value) => typeof value === 'string' && value.includes(','))) {
    return JSON.stringify(values);
  }
  return values
    .map((value) => (typeof value === 'string' ? value : JSON.stringify(value)))
    .join(', ');
}

export function sanitizeForArtifact(value) {
  if (Array.isArray(value)) return value.map(sanitizeForArtifact);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        SECRET_KEY.test(key) ? '[REDACTED]' : sanitizeForArtifact(item),
      ]),
    );
  }
  if (typeof value === 'string') {
    return value.replace(BEARER_VALUE, 'Bearer [REDACTED]');
  }
  return value;
}

export function isExpectedWrite(method, pathname, kind) {
  const expected = WRITE_ENDPOINTS[kind];
  if (!expected || method.toUpperCase() !== expected.method) return false;
  return typeof expected.pathname === 'string'
    ? pathname === expected.pathname
    : expected.pathname.test(pathname);
}

/**
 * The industry readiness endpoint is deliberately a POST because the custom
 * stock-code scope can be large.  It is nevertheless read-only on the server:
 * unlike /industries/refresh it neither fetches remote data nor writes cache.
 *
 * Keep this predicate intentionally narrower than a generic POST allowlist:
 * exact path, JSON-object shape, and only the validated stock-code scope are
 * accepted.  Any route or payload drift is fail-closed in the browser guard.
 */
export function isExpectedSemanticRead(method, pathname, payload) {
  if (method.toUpperCase() !== 'POST' || pathname !== INDUSTRY_READINESS_PATH) {
    return false;
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return false;
  const keys = Object.keys(payload);
  if (keys.length !== 1 || keys[0] !== 'codes' || !Array.isArray(payload.codes)) {
    return false;
  }
  return (
    payload.codes.length >= 1
    && payload.codes.length <= 5000
    && payload.codes.every(
      (code) => typeof code === 'string' && INDUSTRY_CODE.test(code.trim()),
    )
  );
}
