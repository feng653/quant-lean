import assert from 'node:assert/strict';
import {
  mkdtemp,
  readFile,
  readdir,
  rm,
  stat,
  writeFile,
} from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { test } from 'node:test';
import {
  AmbiguousPageWriteError,
  atomicJson,
  acquireCampaignLock,
  assertPoolCodesMatch,
  browserProcessEnv,
  browserReadJson,
  clearResolvedStageError,
  exactLabel,
  observePageWrite,
  parseMode,
  parsePageWriteResponse,
  readState,
  recoverBaselineCandidate,
  recoverSweepAfterAmbiguousSubmit,
  recoverSweepCandidate,
  runStrategiesSequentially,
} from '../run_frontend_non_ml_tuning.mjs';
import { loadTuningConfig } from './tuning_contract.mjs';
import {
  isTransientRunnerReportError,
} from './transient_failures.mjs';

const configPath = resolve(
  new URL('.', import.meta.url).pathname,
  'non_ml_tuning.v1.json',
);

test('runner mode parsing is explicit and rejects ambiguous flags', () => {
  assert.equal(parseMode([]), 'dry-run');
  assert.equal(parseMode(['--dry-run']), 'dry-run');
  assert.equal(parseMode(['--live-preflight']), 'live-preflight');
  assert.equal(parseMode(['--execute']), 'execute');
  assert.throws(
    () => parseMode(['--execute', '--live-preflight']),
    /exactly one/,
  );
  assert.throws(() => parseMode(['--unsafe']), /Unknown arguments/);
});

test('page write response rejects redirects and non-JSON before parsing', async () => {
  let redirectJsonCalled = false;
  await assert.rejects(
    parsePageWriteResponse({
      ok: () => false,
      status: () => 307,
      headerValue: async () => null,
      json: async () => {
        redirectJsonCalled = true;
        return {};
      },
    }, 'baseline'),
    /HTTP 307/,
  );
  assert.equal(redirectJsonCalled, false);

  let htmlJsonCalled = false;
  await assert.rejects(
    parsePageWriteResponse({
      ok: () => true,
      status: () => 200,
      headerValue: async () => 'text/html; charset=utf-8',
      json: async () => {
        htmlJsonCalled = true;
        return {};
      },
    }, 'baseline'),
    /non-JSON/,
  );
  assert.equal(htmlJsonCalled, false);

  const payload = { data: { experiment_id: 11, job_id: '20' } };
  assert.deepEqual(
    await parsePageWriteResponse({
      ok: () => true,
      status: () => 200,
      headerValue: async () => 'application/json; charset=utf-8',
      json: async () => payload,
    }, 'baseline'),
    payload,
  );

  await assert.rejects(
    parsePageWriteResponse({
      ok: () => true,
      status: () => 200,
      headerValue: async () => 'application/json',
      json: async () => {
        throw new Error('malformed');
      },
    }, 'baseline'),
    /invalid JSON/,
  );
});

function writeGuardFixture() {
  const counts = { readiness: 0, sweep: 0 };
  let expected = null;
  return {
    arm(kind) {
      assert.equal(expected, null);
      expected = kind;
    },
    disarm(kind) {
      assert.equal(expected, kind);
      expected = null;
    },
    count(kind) {
      return counts[kind] ?? 0;
    },
    record(kind) {
      counts[kind] = (counts[kind] ?? 0) + 1;
    },
  };
}

function responseFixture(pathname, data, requestBody = undefined) {
  return {
    ok: () => true,
    status: () => 200,
    headerValue: async () => 'application/json; charset=utf-8',
    json: async () => ({ data }),
    request: () => ({
      method: () => 'POST',
      url: () => `http://localhost:8000${pathname}`,
      postDataJSON: () => requestBody,
    }),
  };
}

test('sweep write observation gives readiness and final POST separate bounded stages', async () => {
  const guard = writeGuardFixture();
  const readiness = responseFixture(
    '/api/data/experiment-readiness',
    { ready: true },
  );
  const sweep = responseFixture(
    '/api/experiments/sweep',
    { sweep_id: 16 },
    { strategy_id: 'liquidity_factor_v1' },
  );
  const observedTimeouts = [];
  const page = {
    waitForResponse(predicate, options) {
      observedTimeouts.push(options.timeout);
      if (predicate(sweep)) return Promise.resolve(sweep);
      assert.equal(predicate(readiness), true);
      return new Promise((resolvePromise) => {
        setTimeout(() => resolvePromise(readiness), 20);
      });
    },
  };

  const observed = await observePageWrite(
    page,
    guard,
    'sweep',
    async () => {
      guard.record('readiness');
      guard.record('sweep');
    },
    { readinessTimeout: 100, writeTimeout: 5 },
  );
  assert.deepEqual(observed, {
    request: { strategy_id: 'liquidity_factor_v1' },
    response: { sweep_id: 16 },
  });
  assert.deepEqual(observedTimeouts, [105, 100]);
});

test('a seen sweep POST without a response is explicitly ambiguous', async () => {
  const guard = writeGuardFixture();
  const readiness = responseFixture(
    '/api/data/experiment-readiness',
    { ready: true },
  );
  const sweep = responseFixture('/api/experiments/sweep', { sweep_id: 16 });
  const page = {
    waitForResponse(predicate) {
      if (predicate(sweep)) return new Promise(() => {});
      assert.equal(predicate(readiness), true);
      return Promise.resolve(readiness);
    },
  };

  await assert.rejects(
    observePageWrite(
      page,
      guard,
      'sweep',
      async () => {
        guard.record('readiness');
        guard.record('sweep');
      },
      { readinessTimeout: 20, writeTimeout: 1 },
    ),
    (error) => (
      error instanceof AmbiguousPageWriteError
      && error.kind === 'sweep'
      && error.writeOutcomeAmbiguous === true
    ),
  );
});

test('browser GET retries bounded transient fetch failures and then succeeds', async () => {
  let calls = 0;
  const delays = [];
  const page = {
    async evaluate() {
      calls += 1;
      if (calls < 3) throw new TypeError('Failed to fetch');
      return {
        ok: true,
        status: 200,
        payload: { data: { id: 24, status: 'completed' } },
      };
    },
  };

  assert.deepEqual(
    await browserReadJson(page, '/api/experiments/24', {
      attempts: 3,
      retryBaseMs: 10,
      sleep: async (delay) => delays.push(delay),
    }),
    { id: 24, status: 'completed' },
  );
  assert.equal(calls, 3);
  assert.deepEqual(delays, [10, 20]);
});

test('browser child environment excludes interactive tuning credentials', () => {
  assert.deepEqual(
    browserProcessEnv({
      PATH: '/usr/bin:/bin',
      QUANT_TUNING_USERNAME: 'admin',
      QUANT_TUNING_PASSWORD: 'secret',
      SAFE_SETTING: 'retained',
    }),
    {
      PATH: '/usr/bin:/bin',
      SAFE_SETTING: 'retained',
    },
  );
});

test('successful stage clears only its own resolved error evidence', () => {
  const sweepItem = {
    last_error: {
      stage: 'sweep-submit-or-restore',
      message: 'old readiness timeout',
      at: '2026-07-31T01:53:42Z',
    },
  };
  assert.equal(
    clearResolvedStageError(sweepItem, 'sweep-submit-or-restore'),
    true,
  );
  assert.equal(sweepItem.last_error, null);

  const otherStage = {
    last_error: {
      stage: 'sweep-rank-promote',
      message: 'promotion failed',
      at: '2026-07-31T02:00:00Z',
    },
  };
  assert.equal(
    clearResolvedStageError(otherStage, 'sweep-submit-or-restore'),
    false,
  );
  assert.deepEqual(otherStage.last_error, {
    stage: 'sweep-rank-promote',
    message: 'promotion failed',
    at: '2026-07-31T02:00:00Z',
  });
  assert.equal(clearResolvedStageError({ last_error: null }, 'baseline'), false);
});

test('strategy pipeline does not submit the next sweep before the current lifecycle is terminal', async () => {
  const events = [];
  let releaseFirstSweep;
  const firstSweepTerminal = new Promise((resolvePromise) => {
    releaseFirstSweep = resolvePromise;
  });
  const running = runStrategiesSequentially(
    [{ strategy_id: 'first' }, { strategy_id: 'second' }],
    async (strategy) => {
      events.push(`submit:${strategy.strategy_id}`);
      if (strategy.strategy_id === 'first') await firstSweepTerminal;
      events.push(`terminal:${strategy.strategy_id}`);
    },
  );

  await new Promise((resolvePromise) => setImmediate(resolvePromise));
  assert.deepEqual(events, ['submit:first']);
  releaseFirstSweep();
  await running;
  assert.deepEqual(events, [
    'submit:first',
    'terminal:first',
    'submit:second',
    'terminal:second',
  ]);
});

test('browser GET retry is bounded and non-transient responses fail immediately', async () => {
  let fetchCalls = 0;
  const failingPage = {
    async evaluate() {
      fetchCalls += 1;
      throw new TypeError('Failed to fetch');
    },
  };
  await assert.rejects(
    browserReadJson(failingPage, '/api/experiments/24', {
      attempts: 3,
      retryBaseMs: 1,
      sleep: async () => {},
    }),
    /remained unavailable after 3 attempts/,
  );
  assert.equal(fetchCalls, 3);

  let authCalls = 0;
  const unauthorizedPage = {
    async evaluate() {
      authCalls += 1;
      return { ok: false, status: 401, payload: null };
    },
  };
  await assert.rejects(
    browserReadJson(unauthorizedPage, '/api/auth/me', {
      attempts: 6,
      retryBaseMs: 1,
      sleep: async () => {},
    }),
    /HTTP 401/,
  );
  assert.equal(authCalls, 1);
});

test('browser GET does not retry code defects and validates the JSON data envelope', async () => {
  let defectCalls = 0;
  const defectivePage = {
    async evaluate() {
      defectCalls += 1;
      throw new ReferenceError('unexpected runner defect');
    },
  };
  await assert.rejects(
    browserReadJson(defectivePage, '/api/experiments/24', {
      attempts: 6,
      retryBaseMs: 1,
      sleep: async () => {},
    }),
    /unexpected runner defect/,
  );
  assert.equal(defectCalls, 1);

  for (const payload of [null, {}, [], { result: {} }]) {
    let envelopeCalls = 0;
    const invalidEnvelopePage = {
      async evaluate() {
        envelopeCalls += 1;
        return { ok: true, status: 200, payload };
      },
    };
    await assert.rejects(
      browserReadJson(invalidEnvelopePage, '/api/experiments/24', {
        attempts: 6,
        retryBaseMs: 1,
        sleep: async () => {},
      }),
      /invalid JSON data envelope/,
    );
    assert.equal(envelopeCalls, 1);
  }
});

test('parent launcher resumes only explicit transient report failures', () => {
  assert.equal(
    isTransientRunnerReportError({
      status: 'failed',
      error: { message: 'page.evaluate: TypeError: Failed to fetch' },
    }),
    true,
  );
  assert.equal(
    isTransientRunnerReportError({
      status: 'failed',
      error: {
        message: 'Read-only preflight /api/experiments/24 failed with HTTP 503',
      },
    }),
    true,
  );
  assert.equal(
    isTransientRunnerReportError({
      status: 'failed',
      error: {
        message: 'page write outcome ambiguous after exactly one sweep POST: page.waitForResponse timeout',
      },
    }),
    true,
  );
  for (const message of [
    'locator.fill: Timeout 45000ms exceeded',
    'Browser write guard violations: unexpected POST /api/example',
    'Read-only preflight /api/auth/me failed with HTTP 401',
    'ReferenceError: unexpected runner defect',
  ]) {
    assert.equal(
      isTransientRunnerReportError({
        status: 'failed',
        error: { message },
      }),
      false,
    );
  }
});

test('form locators are anchored but accept an aria-hidden required marker', () => {
  const observed = [];
  const locator = { marker: 'locator' };
  const page = {
    getByLabel(label) {
      observed.push(label);
      return locator;
    },
  };

  assert.equal(exactLabel(page, '股票池'), locator);
  assert.equal(observed.length, 1);
  assert.equal(observed[0] instanceof RegExp, true);
  assert.equal(observed[0].test('股票池'), true);
  assert.equal(observed[0].test('股票池*'), true);
  assert.equal(observed[0].test('股票池 *'), true);
  assert.equal(observed[0].test('步骤 3：选择股票池，当前步骤'), false);
  assert.equal(observed[0].test('自定义股票池'), false);
});

test('pool-code contract accepts only empty preset representations and exact custom codes', () => {
  for (const emptyRepresentation of [null, '', []]) {
    assert.doesNotThrow(() => assertPoolCodesMatch(
      'preset codes', emptyRepresentation, [], 'csi300',
    ));
  }
  assert.throws(
    () => assertPoolCodesMatch('preset codes', ['000001'], [], 'csi300'),
    /must be empty for preset pool csi300/,
  );

  assert.doesNotThrow(() => assertPoolCodesMatch(
    'custom codes', '000001,600000', ['000001', '600000'], 'custom',
  ));
  assert.throws(
    () => assertPoolCodesMatch(
      'custom codes', '000001,600001', ['000001', '600000'], 'custom',
    ),
    /differs from preregistered config/,
  );
  assert.throws(
    () => assertPoolCodesMatch('custom codes', null, ['000001'], 'custom'),
    /must contain an explicit custom-code list/,
  );
});

test('checkpoint initialization covers all strategies and rejects config drift', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'quant-tuning-runner-test-'));
  try {
    const path = join(directory, 'checkpoint.json');
    const config = await loadTuningConfig(configPath);
    const state = await readState(
      path,
      config.campaign,
      'a'.repeat(64),
      config,
    );
    assert.equal(Object.keys(state.strategies).length, 11);
    assert.equal(
      Object.values(state.strategies).reduce(
        (total, item) => total + item.selection_combinations,
        0,
      ),
      114,
    );

    await atomicJson(path, state);
    const resumed = await readState(
      path,
      config.campaign,
      'a'.repeat(64),
      config,
    );
    assert.deepEqual(resumed.strategies, state.strategies);
    await assert.rejects(
      readState(path, config.campaign, 'b'.repeat(64), config),
      /different campaign or config/,
    );
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('artifacts are private and recursively redact credentials', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'quant-tuning-artifact-test-'));
  try {
    const path = join(directory, 'nested', 'report.json');
    await atomicJson(path, {
      password: 'do-not-store',
      message: 'Authorization: Bearer abc.def.ghi',
    });
    const payload = JSON.parse(await readFile(path, 'utf8'));
    assert.deepEqual(payload, {
      password: '[REDACTED]',
      message: 'Authorization: Bearer [REDACTED]',
    });
    assert.equal((await stat(path)).mode & 0o777, 0o600);
    assert.equal((await stat(join(directory, 'nested'))).mode & 0o777, 0o700);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('campaign lock blocks a live duplicate and preserves a stale lock', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'quant-tuning-lock-test-'));
  try {
    const release = await acquireCampaignLock(
      directory,
      'campaign',
      'a'.repeat(64),
    );
    await assert.rejects(
      acquireCampaignLock(directory, 'campaign', 'a'.repeat(64)),
      /already running/,
    );
    await release();

    await writeFile(
      join(directory, 'runner.lock'),
      `${JSON.stringify({ pid: 2_147_483_647 })}\n`,
      { encoding: 'utf8', mode: 0o600 },
    );
    const releaseAfterStale = await acquireCampaignLock(
      directory,
      'campaign',
      'a'.repeat(64),
    );
    const names = await readdir(directory);
    assert.equal(
      names.some((name) => name.startsWith('runner.lock.stale-')),
      true,
    );
    await releaseAfterStale();
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('committed POSTs are recovered from deterministic intents before resubmission', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'quant-tuning-crash-recovery-test-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const baselineIntent = {
    identity: {
      name: 'campaign-baseline-ma_cross_v1',
      strategy_id: 'ma_cross_v1',
      pool_preset: 'custom',
      pool_custom_codes: ['000001', '600000'],
      pool_industries: [],
      train_start: null,
      train_end: null,
      test_start: '2023-07-31',
      test_end: '2023-12-29',
      params: { fast_period: 10, slow_period: 60 },
      mode: 'batch',
      data_access_policy: 'cache_only',
      source_experiment_id: null,
    },
  };
  const checkpointPath = join(directory, 'checkpoint.json');
  await atomicJson(checkpointPath, {
    baseline_intent: baselineIntent,
    baseline: null,
  });
  const afterCrash = JSON.parse(await readFile(checkpointPath, 'utf8'));
  const baseline = recoverBaselineCandidate([
    {
      id: 11,
      ...baselineIntent.identity,
      status: 'completed',
      created_at: '2026-07-30T00:00:00Z',
    },
  ], afterCrash.baseline_intent);
  assert.equal(baseline.experiment_id, 11);
  assert.equal(baseline.status, 'completed');
  assert.equal(baseline.recovered_after_ambiguous_submit, true);

  const sweepIntent = {
    identity: {
      name: 'campaign-baseline-ma_cross_v1-fast_period-slow_period',
      strategy_id: 'ma_cross_v1',
      param_grid: {
        fast_period: [5, 10, 20],
        slow_period: [30, 60, 120],
      },
      selection_start: '2023-07-31',
      selection_end: '2023-12-29',
      locked_test_start: '2024-01-02',
      locked_test_end: '2024-03-29',
      data_access_policy: 'cache_only',
      source_experiment_id: 42,
    },
  };
  const sweep = recoverSweepCandidate([
    {
      id: 7,
      name: sweepIntent.identity.name,
      strategy_id: sweepIntent.identity.strategy_id,
      sweep_config: sweepIntent.identity.param_grid,
      selection_start: sweepIntent.identity.selection_start,
      selection_end: sweepIntent.identity.selection_end,
      locked_test_start: sweepIntent.identity.locked_test_start,
      locked_test_end: sweepIntent.identity.locked_test_end,
      data_access_policy: 'cache_only',
      source_experiment_id: 42,
      research_trust: 'locked_test',
      total_experiments: 9,
      status: 'running',
      created_at: '2026-07-30T00:00:01Z',
    },
  ], sweepIntent);
  assert.equal(sweep.sweep_id, 7);
  assert.equal(sweep.recovered_after_ambiguous_submit, true);

  assert.throws(
    () => recoverBaselineCandidate([
      { id: 42, ...baselineIntent.identity },
      { id: 43, ...baselineIntent.identity },
    ], baselineIntent),
    /ambiguous/,
  );
  assert.throws(
    () => recoverBaselineCandidate([
      {
        id: 42,
        ...baselineIntent.identity,
        data_access_policy: 'allow_fetch',
      },
    ], baselineIntent),
    /differs from intent/,
  );
  assert.throws(
    () => recoverSweepCandidate([
      {
        id: 7,
        name: sweepIntent.identity.name,
        strategy_id: sweepIntent.identity.strategy_id,
        sweep_config: sweepIntent.identity.param_grid,
        selection_start: sweepIntent.identity.selection_start,
        selection_end: sweepIntent.identity.selection_end,
        locked_test_start: sweepIntent.identity.locked_test_start,
        locked_test_end: sweepIntent.identity.locked_test_end,
        data_access_policy: 'allow_fetch',
        source_experiment_id: 42,
        research_trust: 'locked_test',
        total_experiments: 9,
      },
    ], sweepIntent),
    /differs from intent/,
  );
});

test('ambiguous sweep submission polls deterministic intent and validates before reuse', async () => {
  const intent = {
    identity: {
      name: 'campaign-baseline-liquidity_factor_v1-lookback_days',
      strategy_id: 'liquidity_factor_v1',
      param_grid: { lookback_days: [5, 10] },
      selection_start: '2016-01-04',
      selection_end: '2022-12-30',
      locked_test_start: '2023-01-03',
      locked_test_end: '2026-06-30',
      data_access_policy: 'cache_only',
      source_experiment_id: 172,
    },
  };
  const candidate = {
    id: 16,
    name: intent.identity.name,
    strategy_id: intent.identity.strategy_id,
    sweep_config: intent.identity.param_grid,
    selection_start: intent.identity.selection_start,
    selection_end: intent.identity.selection_end,
    locked_test_start: intent.identity.locked_test_start,
    locked_test_end: intent.identity.locked_test_end,
    data_access_policy: intent.identity.data_access_policy,
    source_experiment_id: intent.identity.source_experiment_id,
    research_trust: 'locked_test',
    total_experiments: 2,
    status: 'completed',
    created_at: '2026-07-31T01:52:53Z',
  };
  let lookups = 0;
  let validations = 0;
  const delays = [];
  const page = {
    async evaluate() {
      lookups += 1;
      return {
        ok: true,
        status: 200,
        payload: {
          data: { sweeps: lookups === 1 ? [] : [candidate] },
        },
      };
    },
  };
  const recovered = await recoverSweepAfterAmbiguousSubmit(page, intent, {
    attempts: 2,
    retryBaseMs: 10,
    sleep: async (delay) => delays.push(delay),
    validate: async (_page, sweep, storedIntent) => {
      validations += 1;
      assert.equal(sweep.sweep_id, 16);
      assert.equal(storedIntent, intent);
    },
  });
  assert.equal(recovered.sweep_id, 16);
  assert.equal(recovered.status, 'completed');
  assert.equal(lookups, 2);
  assert.equal(validations, 1);
  assert.deepEqual(delays, [10]);
});

test('preset baseline recovery reuses a completed null-code row without resubmission', () => {
  const intent = {
    identity: {
      name: 'campaign-baseline-ma_cross_v1',
      strategy_id: 'ma_cross_v1',
      pool_preset: 'csi300',
      pool_custom_codes: [],
      pool_industries: [],
      train_start: null,
      train_end: null,
      test_start: '2023-07-31',
      test_end: '2023-12-29',
      params: { fast_period: 10, slow_period: 60 },
      mode: 'batch',
      data_access_policy: 'cache_only',
      source_experiment_id: null,
    },
  };
  const reused = recoverBaselineCandidate([{
    id: 172,
    ...intent.identity,
    pool_custom_codes: null,
    status: 'completed',
    created_at: '2026-07-31T00:00:00Z',
  }], intent);
  assert.equal(reused.experiment_id, 172);
  assert.equal(reused.status, 'completed');
  assert.throws(
    () => recoverBaselineCandidate([{
      id: 172,
      ...intent.identity,
      pool_custom_codes: ['000001'],
    }], intent),
    /must be empty for preset pool csi300/,
  );
});
