#!/usr/bin/env node

import { createHash } from 'node:crypto';
import {
  chmod,
  mkdir,
  open,
  readFile,
  rename,
  unlink,
  writeFile,
} from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import {
  REPORT_SCHEMA,
  STATE_SCHEMA,
  cartesianGrid,
  choosePromotionCandidate,
  codesDigest,
  combinationsInGrid,
  formatGridForFrontend,
  isAllowedBrowserOrigin,
  isExpectedSemanticRead,
  isExpectedWrite,
  loadTuningConfig,
  sanitizeForArtifact,
  validateConfigAgainstApi,
  validateLiveOrigins,
} from './frontend_tuning/tuning_contract.mjs';
import {
  isTransientBrowserReadError,
} from './frontend_tuning/transient_failures.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const DEFAULT_CONFIG = join(ROOT, 'scripts/frontend_tuning/non_ml_tuning.v1.json');
const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

export function parseMode(argv) {
  const allowed = new Set(['--dry-run', '--live-preflight', '--execute']);
  const unknown = argv.filter((item) => !allowed.has(item));
  if (unknown.length) throw new Error(`Unknown arguments: ${unknown.join(', ')}`);
  const selected = [...new Set(argv)];
  if (selected.length > 1) {
    throw new Error('Choose exactly one of --dry-run, --live-preflight, or --execute');
  }
  return selected[0] === '--execute'
    ? 'execute'
    : selected[0] === '--live-preflight'
      ? 'live-preflight'
      : 'dry-run';
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export function exactLabel(page, label) {
  // Playwright's getByLabel(..., { exact: true }) matches the rendered label
  // text, including a visual required marker even when that marker is
  // aria-hidden. Keep the match anchored so similarly named controls and the
  // step indicator cannot collide, while accepting the UI's optional "*".
  return page.getByLabel(new RegExp(`^${escapeRegExp(label)}(?:\\s*\\*)?$`, 'u'));
}

const mode = parseMode(process.argv.slice(2));
const configPath = resolve(process.env.QUANT_TUNING_CONFIG || DEFAULT_CONFIG);
const frontendUrl = (
  process.env.QUANT_TUNING_FRONTEND_URL || 'http://localhost:5173'
).replace(/\/+$/, '');
const backendUrl = (
  process.env.QUANT_TUNING_BACKEND_URL || 'http://localhost:8000'
).replace(/\/+$/, '');
const stepTimeoutMs = parsePositiveInt('QUANT_TUNING_STEP_TIMEOUT_MS', 45_000);
const jobTimeoutMs = parsePositiveInt('QUANT_TUNING_JOB_TIMEOUT_MS', 1_200_000);
const readinessTimeoutMs = parsePositiveInt(
  'QUANT_TUNING_READINESS_TIMEOUT_MS',
  300_000,
);
const browserReadAttempts = parsePositiveInt('QUANT_TUNING_READ_ATTEMPTS', 6);
const browserReadRetryBaseMs = parsePositiveInt(
  'QUANT_TUNING_READ_RETRY_BASE_MS',
  500,
);
const TRANSIENT_READ_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);

function parsePositiveInt(name, fallback) {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number(raw);
  if (!Number.isInteger(parsed) || parsed <= 0) throw new Error(`${name} must be a positive integer`);
  return parsed;
}

function nowIso() {
  return new Date().toISOString();
}

export function clearResolvedStageError(item, stage) {
  if (item?.last_error?.stage !== stage) return false;
  item.last_error = null;
  return true;
}

export async function runStrategiesSequentially(strategies, execute) {
  for (const strategy of strategies) {
    await execute(strategy);
  }
}

function configFingerprint(config) {
  return createHash('sha256').update(JSON.stringify({
    config,
    frontend_origin: frontendUrl,
    backend_origin: backendUrl,
  })).digest('hex');
}

function canonicalJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(',')}]`;
  }
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(
      (key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`,
    ).join(',')}}`;
  }
  return JSON.stringify(value);
}

function submissionIntent(kind, identity) {
  return {
    schema_version: 'quant-platform/frontend-tuning-submission-intent/v1',
    kind,
    identity_sha256: createHash('sha256')
      .update(canonicalJson(identity))
      .digest('hex'),
    identity,
    prepared_at: nowIso(),
  };
}

function configuredPoolCodes(config) {
  return Array.isArray(config.dataset.codes) ? config.dataset.codes : [];
}

function baselineSubmissionIntent(strategy, config) {
  return submissionIntent('baseline', {
    name: `${config.campaign}-baseline-${strategy.strategy_id}`,
    strategy_id: strategy.strategy_id,
    pool_preset: config.dataset.pool_preset,
    pool_custom_codes: configuredPoolCodes(config),
    pool_industries: [],
    train_start: null,
    train_end: null,
    test_start: config.windows.selection_start,
    test_end: config.windows.selection_end,
    params: strategy.base_params,
    mode: 'batch',
    data_access_policy: 'cache_only',
    source_experiment_id: null,
  });
}

function sweepSubmissionIntent(strategy, baselineId, config) {
  return submissionIntent('sweep', {
    name: (
      `${config.campaign}-baseline-${strategy.strategy_id}`
      + `-${Object.keys(strategy.grid).join('-')}`
    ),
    strategy_id: strategy.strategy_id,
    baseline_experiment_id: baselineId,
    param_grid: strategy.grid,
    base_params: strategy.base_params,
    pool_preset: config.dataset.pool_preset,
    pool_custom_codes: configuredPoolCodes(config).join(','),
    pool_industries: '',
    train_start: null,
    train_end: null,
    selection_start: config.windows.selection_start,
    selection_end: config.windows.selection_end,
    locked_test_start: config.windows.locked_test_start,
    locked_test_end: config.windows.locked_test_end,
    mode: 'batch',
    data_access_policy: 'cache_only',
    source_experiment_id: baselineId,
  });
}

function ensureStoredIntent(stored, expected, label) {
  if (stored && stored.identity_sha256 !== expected.identity_sha256) {
    throw new Error(`${label} checkpoint submission intent differs from protocol`);
  }
  return stored ?? expected;
}

export function recoverBaselineCandidate(candidates, intent) {
  if (!Array.isArray(candidates)) {
    throw new Error('Baseline recovery lookup did not return a list');
  }
  if (candidates.length > 1) {
    throw new Error(
      `${intent.identity.strategy_id} baseline recovery is ambiguous (${candidates.length} exact-name rows)`,
    );
  }
  if (candidates.length === 0) return null;
  const candidate = candidates[0];
  const identity = intent.identity;
  const poolCustomCodes = normalizePoolCodes(
    candidate.pool_custom_codes,
    identity.pool_preset,
    `${identity.strategy_id} recovered baseline codes`,
  );
  const observed = {
    name: candidate.name,
    strategy_id: candidate.strategy_id,
    pool_preset: candidate.pool_preset,
    pool_custom_codes: poolCustomCodes,
    pool_industries: candidate.pool_industries,
    train_start: candidate.train_start,
    train_end: candidate.train_end,
    test_start: candidate.test_start,
    test_end: candidate.test_end,
    params: candidate.params,
    mode: candidate.mode,
    data_access_policy: candidate.data_access_policy,
    source_experiment_id: candidate.source_experiment_id,
  };
  if (canonicalJson(observed) !== canonicalJson(identity)) {
    throw new Error(
      `${identity.strategy_id} exact-name baseline recovery row differs from intent`,
    );
  }
  const experimentId = Number(candidate.id);
  if (!Number.isInteger(experimentId) || experimentId <= 0) {
    throw new Error(`${identity.strategy_id} baseline recovery ID is invalid`);
  }
  return {
    experiment_id: experimentId,
    job_id: null,
    url: `${frontendUrl}/experiment/${experimentId}`,
    submitted_at: candidate.created_at ?? null,
    status: candidate.status,
    recovered_after_ambiguous_submit: true,
  };
}

export function recoverSweepCandidate(candidates, intent) {
  if (!Array.isArray(candidates)) {
    throw new Error('Sweep recovery lookup did not return a list');
  }
  if (candidates.length > 1) {
    throw new Error(
      `${intent.identity.strategy_id} sweep recovery is ambiguous (${candidates.length} exact-name rows)`,
    );
  }
  if (candidates.length === 0) return null;
  const candidate = candidates[0];
  const identity = intent.identity;
  const observed = {
    name: candidate.name,
    strategy_id: candidate.strategy_id,
    param_grid: candidate.sweep_config,
    selection_start: candidate.selection_start,
    selection_end: candidate.selection_end,
    locked_test_start: candidate.locked_test_start,
    locked_test_end: candidate.locked_test_end,
    data_access_policy: candidate.data_access_policy,
    source_experiment_id: Number(candidate.source_experiment_id),
  };
  const expected = {
    name: identity.name,
    strategy_id: identity.strategy_id,
    param_grid: identity.param_grid,
    selection_start: identity.selection_start,
    selection_end: identity.selection_end,
    locked_test_start: identity.locked_test_start,
    locked_test_end: identity.locked_test_end,
    data_access_policy: identity.data_access_policy,
    source_experiment_id: identity.source_experiment_id,
  };
  if (
    canonicalJson(observed) !== canonicalJson(expected)
    || candidate.research_trust !== 'locked_test'
    || Number(candidate.total_experiments) !== combinationsInGrid(identity.param_grid)
  ) {
    throw new Error(
      `${identity.strategy_id} exact-name sweep recovery row differs from intent`,
    );
  }
  const sweepId = Number(candidate.id);
  if (!Number.isInteger(sweepId) || sweepId <= 0) {
    throw new Error(`${identity.strategy_id} sweep recovery ID is invalid`);
  }
  return {
    sweep_id: sweepId,
    url: `${frontendUrl}/experiment/sweep?sweep_id=${sweepId}`,
    total_experiments: Number(candidate.total_experiments),
    submitted_at: candidate.created_at ?? null,
    status: candidate.status,
    recovered_after_ambiguous_submit: true,
  };
}

function summarizeConfig(config) {
  return {
    schema_version: config.schema_version,
    campaign: config.campaign,
    dataset: {
      kind: config.dataset.kind,
      evidence_level: config.dataset.evidence_level,
      provider: config.dataset.provider,
      providers: config.dataset.providers,
      source_trust: config.dataset.source_trust,
      pool_preset: config.dataset.pool_preset,
      cache_key: config.dataset.cache_key,
      frame_digest: config.dataset.frame_digest,
      price_adjustment: config.dataset.price_adjustment,
      required_fields: config.dataset.required_fields,
      n_dates: config.dataset.n_dates,
      minimum_dates: config.dataset.minimum_dates,
      code_count: configuredPoolCodes(config).length,
      codes_sha256: codesDigest(configuredPoolCodes(config)),
      available_start: config.dataset.available_start,
      available_end: config.dataset.available_end,
    },
    windows: config.windows,
    expected: config.expected,
    ranking: config.ranking,
    strategies: config.strategies.map((strategy) => ({
      strategy_id: strategy.strategy_id,
      expected_version: strategy.expected_version,
      selection_combinations: combinationsInGrid(strategy.grid),
      grid: strategy.grid,
    })),
  };
}

async function ensurePrivateDirectory(path) {
  await mkdir(path, { recursive: true, mode: 0o700 });
  await chmod(path, 0o700);
}

export async function atomicJson(path, value) {
  await ensurePrivateDirectory(dirname(path));
  const temporary = `${path}.tmp`;
  const serialized = `${JSON.stringify(sanitizeForArtifact(value), null, 2)}\n`;
  await writeFile(temporary, serialized, { encoding: 'utf8', mode: 0o600 });
  await chmod(temporary, 0o600);
  await rename(temporary, path);
}

export async function readState(path, campaign, fingerprint, config) {
  try {
    const parsed = JSON.parse(await readFile(path, 'utf8'));
    if (
      parsed.schema_version !== STATE_SCHEMA
      || parsed.campaign !== campaign
      || parsed.config_sha256 !== fingerprint
    ) {
      throw new Error('Checkpoint belongs to a different campaign or config');
    }
    return parsed;
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
    return {
      schema_version: STATE_SCHEMA,
      campaign,
      config_sha256: fingerprint,
      created_at: nowIso(),
      updated_at: nowIso(),
      mode,
      protocol: summarizeConfig(config),
      preflight: null,
      strategies: Object.fromEntries(
        config.strategies.map((strategy) => [
          strategy.strategy_id,
          {
            selection_combinations: combinationsInGrid(strategy.grid),
            baseline_intent: null,
            baseline: null,
            sweep_intent: null,
            sweep: null,
            promotion: null,
            locked_test: null,
            last_error: null,
          },
        ]),
      ),
    };
  }
}

function processIsRunning(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === 'EPERM';
  }
}

export async function acquireCampaignLock(artifactDir, campaign, fingerprint) {
  await ensurePrivateDirectory(artifactDir);
  const lockPath = join(artifactDir, 'runner.lock');
  for (let attempt = 0; attempt < 2; attempt += 1) {
    let handle;
    try {
      handle = await open(lockPath, 'wx', 0o600);
      await handle.writeFile(`${JSON.stringify({
        schema_version: 'quant-platform/frontend-non-ml-tuning-lock/v1',
        campaign,
        config_sha256: fingerprint,
        pid: process.pid,
        created_at: nowIso(),
      })}\n`, 'utf8');
      await handle.sync();
      await handle.close();
      return async () => {
        await unlink(lockPath).catch((error) => {
          if (error?.code !== 'ENOENT') throw error;
        });
      };
    } catch (error) {
      await handle?.close().catch(() => undefined);
      if (error?.code !== 'EEXIST') throw error;
      let existing = null;
      try {
        existing = JSON.parse(await readFile(lockPath, 'utf8'));
      } catch {
        existing = null;
      }
      if (processIsRunning(Number(existing?.pid))) {
        throw new Error(
          `Campaign is already running in PID ${Number(existing.pid)}; refusing duplicate submissions`,
        );
      }
      const stalePath = join(
        artifactDir,
        `runner.lock.stale-${Date.now()}-${process.pid}`,
      );
      try {
        await rename(lockPath, stalePath);
        await chmod(stalePath, 0o600);
      } catch (renameError) {
        if (renameError?.code !== 'ENOENT') throw renameError;
      }
    }
  }
  throw new Error('Could not acquire the campaign execution lock');
}

async function persistState(statePath, state) {
  state.updated_at = nowIso();
  await atomicJson(statePath, state);
}

async function loadPlaywright() {
  const requested = process.env.QUANT_TUNING_PLAYWRIGHT_MODULE;
  if (!requested) {
    throw new Error(
      'QUANT_TUNING_PLAYWRIGHT_MODULE is required for live modes; point it to an external Playwright module',
    );
  }
  const specifier = requested.startsWith('/') ? pathToFileURL(requested).href : requested;
  const module = await import(specifier);
  const playwright = module.default ?? module;
  if (!playwright.chromium) throw new Error('Configured module does not export Playwright chromium');
  return playwright;
}

async function installWriteGuard(page) {
  const state = { expected: null, seen: [], semanticReads: [], violations: [] };
  const recordViolation = (message) => {
    state.violations.push(message);
    state.expected = null;
  };
  await page.route('**/*', async (route) => {
    const request = route.request();
    const method = request.method().toUpperCase();
    const requestUrl = new URL(request.url());
    const pathname = requestUrl.pathname;
    if (!isAllowedBrowserOrigin(request.url(), frontendUrl, backendUrl)) {
      recordViolation(`unexpected origin ${method} ${requestUrl.origin}${pathname}`);
      await route.abort('blockedbyclient');
      return;
    }
    if (['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      await route.continue();
      return;
    }
    let requestPayload;
    try {
      requestPayload = request.postDataJSON();
    } catch {
      requestPayload = undefined;
    }
    if (
      requestUrl.origin === backendUrl
      && isExpectedSemanticRead(method, pathname, requestPayload)
    ) {
      state.semanticReads.push({ kind: 'industry-readiness', method, pathname });
      await route.continue();
      return;
    }
    if (
      requestUrl.origin === backendUrl
      && method === 'POST'
      && (pathname === '/api/auth/login' || pathname === '/api/auth/refresh')
    ) {
      if (pathname === '/api/auth/login' && state.expected !== 'login') {
        recordViolation(`unarmed ${method} ${pathname}`);
        await route.abort('blockedbyclient');
        return;
      }
      state.seen.push({ kind: pathname === '/api/auth/login' ? 'login' : 'refresh', method, pathname });
      await route.continue();
      return;
    }
    if (
      requestUrl.origin === backendUrl
      && method === 'POST'
      && pathname === '/api/data/experiment-readiness'
      && ['baseline', 'sweep'].includes(state.expected)
    ) {
      state.seen.push({ kind: 'readiness', method, pathname });
      await route.continue();
      return;
    }
    const kind = state.expected;
    if (
      requestUrl.origin !== backendUrl
      || !kind
      || !isExpectedWrite(method, pathname, kind)
    ) {
      recordViolation(`unexpected ${method} ${pathname}`);
      await route.abort('blockedbyclient');
      return;
    }
    state.seen.push({ kind, method, pathname });
    await route.continue();
  });
  return {
    arm(kind) {
      this.assertClean();
      if (state.expected) throw new Error(`Write guard already armed for ${state.expected}`);
      state.expected = kind;
    },
    disarm(kind) {
      if (state.expected !== kind) throw new Error(`Write guard was not armed for ${kind}`);
      state.expected = null;
      this.assertClean();
    },
    assertClean() {
      if (state.violations.length) {
        throw new Error(`Browser write guard violations: ${state.violations.join('; ')}`);
      }
    },
    count(kind) {
      return state.seen.filter((item) => item.kind === kind).length;
    },
    summary() {
      return {
        writes_seen: [...state.seen],
        semantic_reads: [...state.semanticReads],
        violations: [...state.violations],
      };
    },
  };
}

export async function parsePageWriteResponse(response, kind) {
  if (!response.ok()) {
    throw new Error(`${kind} page request failed with HTTP ${response.status()}`);
  }
  const contentType = String(await response.headerValue('content-type') ?? '');
  if (!/^application\/json(?:\s*;|$)/i.test(contentType)) {
    throw new Error(`${kind} page request returned non-JSON content type`);
  }
  try {
    return await response.json();
  } catch {
    throw new Error(`${kind} page request returned invalid JSON`);
  }
}

export class AmbiguousPageWriteError extends Error {
  constructor(kind, cause) {
    super(
      `page write outcome ambiguous after exactly one ${kind} POST: ${cause.message}`,
      { cause },
    );
    this.name = 'AmbiguousPageWriteError';
    this.kind = kind;
    this.writeOutcomeAmbiguous = true;
  }
}

async function withTimeout(promise, timeoutMs, message) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(message)), timeoutMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export async function observePageWrite(
  page,
  guard,
  kind,
  action,
  {
    readinessTimeout = readinessTimeoutMs,
    writeTimeout = stepTimeoutMs,
  } = {},
) {
  const beforeCount = guard.count(kind);
  const beforeReadinessCount = guard.count('readiness');
  const requiresReadiness = ['baseline', 'sweep'].includes(kind);
  let writeResponseObserved = false;
  guard.arm(kind);
  try {
    const responsePromise = page.waitForResponse((response) => {
      const request = response.request();
      const requestUrl = new URL(request.url());
      return (
        requestUrl.origin === backendUrl
        && isExpectedWrite(request.method(), requestUrl.pathname, kind)
      );
    }, {
      timeout: requiresReadiness
        ? readinessTimeout + writeTimeout
        : writeTimeout,
    });
    // If the UI action itself fails, Playwright's outstanding response waiter
    // may reject later. Attach a handler immediately so that failure does not
    // become an unhandled rejection while the write guard is being unwound.
    void responsePromise.catch(() => undefined);
    let readinessPromise = null;
    if (requiresReadiness) {
      readinessPromise = page.waitForResponse((response) => {
        const request = response.request();
        const requestUrl = new URL(request.url());
        return (
          requestUrl.origin === backendUrl
          && request.method().toUpperCase() === 'POST'
          && requestUrl.pathname === '/api/data/experiment-readiness'
        );
      }, { timeout: readinessTimeout });
      void readinessPromise.catch(() => undefined);
    }
    await action();
    if (readinessPromise) {
      const readinessResponse = await readinessPromise;
      await parsePageWriteResponse(readinessResponse, 'readiness');
      if (guard.count('readiness') !== beforeReadinessCount + 1) {
        throw new Error(
          `${kind} page action did not perform exactly one cache-only readiness check`,
        );
      }
    }
    let response;
    try {
      response = await withTimeout(
        responsePromise,
        writeTimeout,
        `${kind} page write response timed out after readiness completed`,
      );
      writeResponseObserved = true;
    } catch (error) {
      if (guard.count(kind) === beforeCount + 1) {
        throw new AmbiguousPageWriteError(kind, error);
      }
      throw error;
    }
    let requestPayload;
    try {
      requestPayload = response.request().postDataJSON();
    } catch {
      requestPayload = undefined;
    }
    const payload = await parsePageWriteResponse(response, kind);
    if (guard.count(kind) !== beforeCount + 1) {
      throw new Error(`${kind} page action did not produce exactly one allowed write`);
    }
    return { request: requestPayload, response: payload?.data };
  } catch (error) {
    if (
      !writeResponseObserved
      && guard.count(kind) === beforeCount + 1
      && !(error instanceof AmbiguousPageWriteError)
    ) {
      throw new AmbiguousPageWriteError(kind, error);
    }
    throw error;
  } finally {
    guard.disarm(kind);
  }
}

function retryDelayMs(attempt, baseMs) {
  return Math.min(baseMs * (2 ** (attempt - 1)), 5_000);
}

function waitMs(delayMs) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, delayMs));
}

export function browserProcessEnv(source = process.env) {
  const environment = { ...source };
  delete environment.QUANT_TUNING_USERNAME;
  delete environment.QUANT_TUNING_PASSWORD;
  return environment;
}

export async function browserReadJson(
  page,
  pathname,
  {
    attempts = browserReadAttempts,
    retryBaseMs = browserReadRetryBaseMs,
    sleep = waitMs,
  } = {},
) {
  const totalAttempts = Math.max(Number(attempts) || 1, 1);
  for (let attempt = 1; attempt <= totalAttempts; attempt += 1) {
    let result;
    try {
      result = await page.evaluate(async ({ base, path }) => {
        const accessToken = window.localStorage.getItem('auth_token');
        const response = await window.fetch(`${base}${path}`, {
          method: 'GET',
          headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
        });
        let payload = null;
        try {
          payload = await response.json();
        } catch {
          payload = null;
        }
        return { ok: response.ok, status: response.status, payload };
      }, { base: backendUrl, path: pathname });
    } catch (error) {
      if (!isTransientBrowserReadError(error)) throw error;
      if (attempt >= totalAttempts) {
        throw new Error(
          `Read-only request ${pathname} remained unavailable after ${totalAttempts} attempts: ${error.message}`,
          { cause: error },
        );
      }
      await sleep(retryDelayMs(attempt, retryBaseMs));
      continue;
    }

    if (result.ok) {
      if (
        !result.payload
        || typeof result.payload !== 'object'
        || Array.isArray(result.payload)
        || !Object.hasOwn(result.payload, 'data')
      ) {
        throw new Error(
          `Read-only preflight ${pathname} returned an invalid JSON data envelope`,
        );
      }
      return result.payload.data;
    }
    if (
      attempt < totalAttempts
      && TRANSIENT_READ_STATUSES.has(Number(result.status))
    ) {
      await sleep(retryDelayMs(attempt, retryBaseMs));
      continue;
    }
    throw new Error(
      `Read-only preflight ${pathname} failed with HTTP ${result.status}`,
    );
  }
  throw new Error(`Read-only request ${pathname} exhausted its retry budget`);
}

async function browserPostReadJson(page, guard, pathname, body) {
  const beforeCount = guard.count('readiness');
  guard.arm('readiness');
  try {
    const result = await page.evaluate(async ({ base, path, requestBody }) => {
      const accessToken = window.localStorage.getItem('auth_token');
      const response = await window.fetch(`${base}${path}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
        },
        body: JSON.stringify(requestBody),
      });
      let payload = null;
      try {
        payload = await response.json();
      } catch {
        payload = null;
      }
      return { ok: response.ok, status: response.status, payload };
    }, { base: backendUrl, path: pathname, requestBody: body });
    if (!result.ok) {
      throw new Error(`Read-only preflight ${pathname} failed with HTTP ${result.status}`);
    }
    if (guard.count('readiness') !== beforeCount + 1) {
      throw new Error('Cache-only preflight did not produce exactly one readiness request');
    }
    return result.payload?.data;
  } finally {
    guard.disarm('readiness');
  }
}

async function lookupSubmissionRecovery(page, intent) {
  const query = new URLSearchParams({
    name: intent.identity.name,
    strategy_id: intent.identity.strategy_id,
  });
  return browserReadJson(page, `/api/experiments/recovery?${query.toString()}`);
}

export async function recoverSweepAfterAmbiguousSubmit(
  page,
  intent,
  {
    attempts = browserReadAttempts,
    retryBaseMs = browserReadRetryBaseMs,
    sleep = waitMs,
    validate = validateRecoveredSweepMembers,
  } = {},
) {
  const totalAttempts = Math.max(Number(attempts) || 1, 1);
  for (let attempt = 1; attempt <= totalAttempts; attempt += 1) {
    const recovery = await lookupSubmissionRecovery(page, intent);
    const sweep = recoverSweepCandidate(recovery?.sweeps, intent);
    if (sweep) {
      await validate(page, sweep, intent);
      return sweep;
    }
    if (attempt < totalAttempts) {
      await sleep(retryDelayMs(attempt, retryBaseMs));
    }
  }
  return null;
}

async function validateRecoveredSweepMembers(page, sweep, intent) {
  const result = await browserReadJson(
    page,
    `/api/experiments/sweep/${sweep.sweep_id}`,
  );
  if (
    !result
    || Number(result.sweep?.id) !== sweep.sweep_id
    || result.sweep?.name !== intent.identity.name
    || result.sweep?.strategy_id !== intent.identity.strategy_id
    || result.sweep?.data_access_policy !== intent.identity.data_access_policy
    || !Array.isArray(result.experiments)
    || result.experiments.length !== sweep.total_experiments
  ) {
    throw new Error(
      `${intent.identity.strategy_id} recovered sweep membership is incomplete`,
    );
  }
  const expectedParams = cartesianGrid(intent.identity.param_grid)
    .map((params) => canonicalJson({ ...intent.identity.base_params, ...params }))
    .sort();
  const actualParams = result.experiments
    .map((experiment) => canonicalJson(experiment.params))
    .sort();
  if (canonicalJson(actualParams) !== canonicalJson(expectedParams)) {
    throw new Error(
      `${intent.identity.strategy_id} recovered sweep parameter members differ from intent`,
    );
  }
  const details = await Promise.all(
    result.experiments.map(
      (experiment) => browserReadJson(page, `/api/experiments/${Number(experiment.id)}`),
    ),
  );
  for (const detail of details) {
    const poolCustomCodes = normalizePoolCodes(
      detail.pool_custom_codes,
      intent.identity.pool_preset,
      `${intent.identity.strategy_id} recovered sweep child codes`,
    );
    const observed = {
      strategy_id: detail.strategy_id,
      pool_preset: detail.pool_preset,
      pool_custom_codes: poolCustomCodes,
      pool_industries: detail.pool_industries,
      train_start: detail.train_start,
      train_end: detail.train_end,
      test_start: detail.test_start,
      test_end: detail.test_end,
      mode: detail.mode,
      data_access_policy: detail.data_access_policy,
      source_experiment_id: Number(detail.source_experiment_id),
    };
    const expected = {
      strategy_id: intent.identity.strategy_id,
      pool_preset: intent.identity.pool_preset,
      pool_custom_codes: intent.identity.pool_custom_codes
        ? intent.identity.pool_custom_codes.split(',')
        : [],
      pool_industries: [],
      train_start: intent.identity.train_start,
      train_end: intent.identity.train_end,
      test_start: intent.identity.selection_start,
      test_end: intent.identity.selection_end,
      mode: intent.identity.mode,
      data_access_policy: intent.identity.data_access_policy,
      source_experiment_id: intent.identity.source_experiment_id,
    };
    if (canonicalJson(observed) !== canonicalJson(expected)) {
      throw new Error(
        `${intent.identity.strategy_id} recovered sweep child differs from intent`,
      );
    }
  }
}

async function login(page, guard) {
  let username = process.env.QUANT_TUNING_USERNAME;
  let password = process.env.QUANT_TUNING_PASSWORD;
  if (!username || !password) {
    throw new Error('QUANT_TUNING_USERNAME and QUANT_TUNING_PASSWORD are required for live modes');
  }
  await page.goto(`${frontendUrl}/login`, { waitUntil: 'domcontentloaded', timeout: stepTimeoutMs });
  await exactLabel(page, '用户名').fill(username);
  await exactLabel(page, '密码').fill(password);
  const beforeCount = guard.count('login');
  guard.arm('login');
  try {
    const loginResponse = page.waitForResponse(
      (response) => (
        response.request().method() === 'POST'
        && new URL(response.url()).origin === backendUrl
        && new URL(response.url()).pathname === '/api/auth/login'
      ),
      { timeout: stepTimeoutMs },
    );
    await page.getByRole('button', { name: '登录' }).click();
    const response = await loginResponse;
    if (!response.ok()) throw new Error(`Frontend login failed with HTTP ${response.status()}`);
    if (guard.count('login') !== beforeCount + 1) {
      throw new Error('Frontend login did not produce exactly one allowed write');
    }
    await page.waitForURL((url) => url.pathname !== '/login', { timeout: stepTimeoutMs });
    // The login response intentionally contains no permission list. Reload so
    // App.checkAuth hydrates the current RBAC grants before protected UI routes
    // are exercised by a non-admin campaign account.
    const identityResponse = page.waitForResponse(
      (candidate) => (
        candidate.request().method() === 'GET'
        && new URL(candidate.url()).pathname === '/api/auth/me'
      ),
      { timeout: stepTimeoutMs },
    );
    await page.reload({ waitUntil: 'domcontentloaded', timeout: stepTimeoutMs });
    const identity = await identityResponse;
    if (!identity.ok()) {
      throw new Error(`Frontend identity hydration failed with HTTP ${identity.status()}`);
    }
  } finally {
    guard.disarm('login');
    username = null;
    password = null;
  }
}

async function livePreflight(page, guard, config) {
  const user = await browserReadJson(page, '/api/auth/me');
  const permissions = new Set(user?.permissions ?? []);
  const required = [
    'data:read',
    'strategies:read',
    'experiments:read',
    'experiments:create',
    'experiments:sweep',
  ];
  if (!user?.is_admin && required.some((permission) => !permissions.has(permission))) {
    throw new Error(`The existing account lacks required permissions: ${required.join(', ')}`);
  }
  const strategies = await browserReadJson(page, '/api/strategies');
  const metadata = validateConfigAgainstApi(config, strategies);
  const readiness = await browserPostReadJson(
    page,
    guard,
    '/api/data/experiment-readiness',
    {
      data_access_policy: 'cache_only',
      pool_preset: config.dataset.pool_preset,
      pool_custom_codes: configuredPoolCodes(config),
      test_start: config.windows.selection_start,
      test_end: config.windows.locked_test_end,
    },
  );
  const cache = readiness?.market_data;
  const benchmark = readiness?.benchmark;
  const requiredFields = config.dataset.required_fields;
  const cacheFailures = [];
  if (readiness?.ready !== true) cacheFailures.push('combined readiness is false');
  if (readiness?.data_access_policy !== 'cache_only') cacheFailures.push('policy is not cache_only');
  if (readiness?.network_accessed !== false) cacheFailures.push('readiness accessed the network');
  if (cache?.ready !== true) cacheFailures.push('market cache is not ready');
  if (cache?.pool_id !== config.dataset.pool_preset) cacheFailures.push('pool differs');
  if (cache?.cache_key !== config.dataset.cache_key) cacheFailures.push('cache key differs');
  if (cache?.schema_version !== 4) cacheFailures.push('schema is not v4');
  if (config.dataset.kind === 'deterministic_synthetic') {
    if (cache?.requested_code_count !== 30 || cache?.available_code_count !== 30) {
      cacheFailures.push('stock count is not 30');
    }
    if (cache?.n_dates !== config.dataset.n_dates) cacheFailures.push('date count differs');
    if (cache?.date_start !== config.dataset.available_start) cacheFailures.push('start date differs');
    if (cache?.date_end !== config.dataset.available_end) cacheFailures.push('end date differs');
  } else {
    if (!Number.isInteger(cache?.available_code_count) || cache.available_code_count < 250) {
      cacheFailures.push('index stock coverage is below 250');
    }
    if (!Number.isInteger(cache?.n_dates) || cache.n_dates < config.dataset.minimum_dates) {
      cacheFailures.push('trading-date coverage is too short');
    }
    if (!cache?.date_start || cache.date_start > config.windows.selection_start) {
      cacheFailures.push('cache starts after the selection window');
    }
    if (!cache?.date_end || cache.date_end < config.windows.locked_test_end) {
      cacheFailures.push('cache ends before the locked-test window');
    }
  }
  if (cache?.required_start !== config.windows.selection_start) cacheFailures.push('required start differs');
  if (cache?.required_end !== config.windows.locked_test_end) cacheFailures.push('required end differs');
  if (JSON.stringify(cache?.fields) !== JSON.stringify(requiredFields)) cacheFailures.push('OHLCV fields differ');
  if (cache?.price_adjustment !== config.dataset.price_adjustment) cacheFailures.push('adjustment differs');
  if (cache?.source_trust !== (config.dataset.source_trust ?? config.dataset.evidence_level)) {
    cacheFailures.push('source trust differs');
  }
  for (const provider of config.dataset.providers ?? [config.dataset.provider]) {
    if (!(cache?.source_providers ?? []).includes(provider)) cacheFailures.push(`provider differs: ${provider}`);
  }
  if (config.dataset.kind === 'deterministic_synthetic') {
    if (!(cache?.source_evidence_levels ?? []).includes('declared')) cacheFailures.push('evidence differs');
    if (cache?.source_frame_digest !== config.dataset.frame_digest) cacheFailures.push('frame digest differs');
  } else if (cache?.source_all_batches_cross_validated !== true) {
    cacheFailures.push('independent cross-validation evidence is missing');
  }
  if (cache?.source_identity_consistent !== true) cacheFailures.push('source identity is mixed');
  if (cache?.source_complete_code_coverage !== true) cacheFailures.push('source code coverage is incomplete');
  if (config.dataset.kind === 'deterministic_synthetic') {
    if (cache?.codes_sha256 !== codesDigest(configuredPoolCodes(config))) cacheFailures.push('custom-code digest differs');
    if ((cache?.missing_codes ?? []).length) cacheFailures.push('custom codes are missing');
  }
  if (Object.keys(cache?.missing_fields ?? {}).length) cacheFailures.push('OHLCV fields are missing');
  if ((cache?.issues ?? []).length) cacheFailures.push('market readiness reported issues');
  const benchmarkStart = new Date(`${config.windows.selection_start}T00:00:00Z`);
  benchmarkStart.setUTCDate(benchmarkStart.getUTCDate() - 10);
  const requiredBenchmarkStart = benchmarkStart.toISOString().slice(0, 10);
  if (benchmark?.ready !== true) cacheFailures.push('benchmark cache is not ready');
  if (benchmark?.index_code !== '000300') cacheFailures.push('benchmark is not 000300');
  if (benchmark?.required_start !== requiredBenchmarkStart) cacheFailures.push('benchmark required start differs');
  if (benchmark?.required_end !== config.windows.locked_test_end) cacheFailures.push('benchmark required end differs');
  if (
    !benchmark?.date_start
    || benchmark.date_start > requiredBenchmarkStart
    || !benchmark?.date_end
    || benchmark.date_end < config.windows.locked_test_end
  ) {
    cacheFailures.push('benchmark date coverage differs');
  }
  if (!Number.isInteger(benchmark?.observations) || benchmark.observations <= 0) {
    cacheFailures.push('benchmark has no observations');
  }
  if ((benchmark?.issues ?? []).length) cacheFailures.push('benchmark readiness reported issues');
  if (cacheFailures.length) {
    throw new Error(`Atomic local cache readiness preflight failed: ${cacheFailures.join('; ')}`);
  }
  return {
    checked_at: nowIso(),
    account: {
      user_id: user.id,
      is_admin: Boolean(user.is_admin),
      required_permissions_satisfied: true,
    },
    frontend_url: frontendUrl,
    backend_url: backendUrl,
    strategy_count: Object.keys(metadata).length,
    cache: {
      pool_id: cache.pool_id,
      cache_key: cache.cache_key,
      schema_version: cache.schema_version,
      source_trust: cache.source_trust,
      source_providers: cache.source_providers,
      source_frame_digest: cache.source_frame_digest,
      source_identity_consistent: cache.source_identity_consistent,
      source_complete_code_coverage: cache.source_complete_code_coverage,
      price_adjustment: cache.price_adjustment,
      date_start: cache.date_start,
      date_end: cache.date_end,
      n_dates: cache.n_dates,
      n_stocks: cache.available_code_count,
      fields: cache.fields,
      codes_sha256: cache.codes_sha256,
      disclosure: config.dataset.disclosure
        ?? 'Declared deterministic synthetic data; not real or verified market data.',
    },
    benchmark: {
      index_code: benchmark.index_code,
      required_start: benchmark.required_start,
      required_end: benchmark.required_end,
      date_start: benchmark.date_start,
      date_end: benchmark.date_end,
      observations: benchmark.observations,
      network_accessed: readiness.network_accessed,
    },
  };
}

function assertSameJson(label, actual, expected) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${label} differs from preregistered config`);
  }
}

/**
 * Normalize the deliberately different empty-code representations used by the
 * current UI/API contract for a preset pool.  Experiment creation sends null,
 * sweep creation sends an empty string, and persisted experiment details may
 * return an empty array.  They all mean "use the named preset", never a
 * custom universe.
 *
 * A custom pool is intentionally stricter: list order and every code are part
 * of the submission identity.  A comma-delimited string is only accepted for
 * the sweep transport, then compared byte-for-byte at the code level.
 */
export function normalizePoolCodes(value, poolPreset, label = 'pool custom codes') {
  if (poolPreset !== 'custom') {
    if (value === null || value === '' || (Array.isArray(value) && value.length === 0)) {
      return [];
    }
    throw new Error(`${label} must be empty for preset pool ${poolPreset}`);
  }
  if (Array.isArray(value)) return value;
  if (typeof value === 'string') return value === '' ? [] : value.split(',');
  throw new Error(`${label} must contain an explicit custom-code list`);
}

export function assertPoolCodesMatch(label, actual, expectedCodes, poolPreset) {
  const expected = normalizePoolCodes(expectedCodes, poolPreset, `${label} expected codes`);
  const observed = normalizePoolCodes(actual, poolPreset, label);
  assertSameJson(label, observed, expected);
}

export async function configureBaselineForm(page, strategy, config, intent) {
  await page.goto(`${frontendUrl}/experiment/new`, { waitUntil: 'domcontentloaded', timeout: stepTimeoutMs });
  await page.getByRole('radio').filter({ hasText: strategy.strategy_id }).click();
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByRole('button', { name: '下一步' }).click();
  await exactLabel(page, '股票池').selectOption(config.dataset.pool_preset);
  if (config.dataset.pool_preset === 'custom') {
    const customCodes = exactLabel(page, '自定义股票代码');
    await customCodes.waitFor({ state: 'visible', timeout: stepTimeoutMs });
    await customCodes.fill(configuredPoolCodes(config).join(','));
  }
  await page.getByRole('button', { name: '下一步' }).click();
  await exactLabel(page, '测试开始').fill(config.windows.selection_start);
  await exactLabel(page, '测试结束').fill(config.windows.selection_end);
  await exactLabel(page, '数据访问策略').selectOption('cache_only');
  await page.getByRole('button', { name: '下一步' }).click();
  const { name } = intent.identity;
  await exactLabel(page, '实验名称').fill(name);
}

async function createBaseline(page, guard, strategy, config, intent) {
  await configureBaselineForm(page, strategy, config, intent);
  const { name } = intent.identity;
  const observed = await observePageWrite(page, guard, 'baseline', async () => {
    await page.getByRole('button', { name: '提交运行' }).click();
  });
  const expectedParams = strategy.base_params;
  assertPoolCodesMatch(
    'Baseline codes',
    observed.request.pool_custom_codes,
    configuredPoolCodes(config),
    config.dataset.pool_preset,
  );
  assertSameJson('Baseline industries', observed.request.pool_industries, []);
  assertSameJson('Baseline params', observed.request.params, expectedParams);
  if (
    observed.request.name !== name
    || observed.request.strategy_id !== strategy.strategy_id
    || observed.request.pool_preset !== config.dataset.pool_preset
    || observed.request.test_start !== config.windows.selection_start
    || observed.request.test_end !== config.windows.selection_end
    || observed.request.mode !== 'batch'
    || observed.request.data_access_policy !== 'cache_only'
    || observed.request.source_experiment_id != null
  ) {
    throw new Error(`${strategy.strategy_id} baseline page payload differs from protocol`);
  }
  const experimentId = Number(observed.response?.experiment_id);
  const jobId = String(observed.response?.job_id ?? '');
  if (!Number.isInteger(experimentId) || experimentId <= 0 || !jobId) {
    throw new Error(`${strategy.strategy_id} baseline response identity is invalid`);
  }
  return {
    experiment_id: experimentId,
    job_id: jobId,
    url: `${frontendUrl}/experiment/${experimentId}`,
    submitted_at: nowIso(),
    status: 'submitted',
  };
}

async function waitForExperiment(page, experimentId) {
  const deadline = Date.now() + jobTimeoutMs;
  while (Date.now() < deadline) {
    const experiment = await browserReadJson(page, `/api/experiments/${experimentId}`);
    if (TERMINAL.has(experiment.status)) return experiment;
    await page.waitForTimeout(2500);
  }
  throw new Error(`experiment ${experimentId} timed out after ${jobTimeoutMs}ms`);
}

async function createSweepTab(
  context,
  guardFactory,
  strategy,
  baselineId,
  config,
  intent,
) {
  const page = await context.newPage();
  const guard = await guardFactory(page);
  await page.goto(
    `${frontendUrl}/experiment/sweep?baseline_id=${baselineId}`,
    { waitUntil: 'domcontentloaded', timeout: stepTimeoutMs },
  );
  await exactLabel(page, '选择基准实验').selectOption(String(baselineId));
  await exactLabel(page, '参数').first().waitFor({ timeout: stepTimeoutMs });
  await exactLabel(page, '选模开始').fill(config.windows.selection_start);
  await exactLabel(page, '选模结束').fill(config.windows.selection_end);
  await exactLabel(page, '锁定测试开始').fill(config.windows.locked_test_start);
  await exactLabel(page, '锁定测试结束').fill(config.windows.locked_test_end);

  const entries = Object.entries(strategy.grid);
  for (let index = 0; index < entries.length; index += 1) {
    if (index > 0) await page.getByRole('button', { name: '添加参数' }).click();
    const [name, values] = entries[index];
    await exactLabel(page, '参数').nth(index).selectOption(name);
    const modeSelect = exactLabel(page, '取值方式').nth(index);
    if (!(await modeSelect.isDisabled())) await modeSelect.selectOption('custom');
    await exactLabel(page, '自定义取值').nth(index).fill(formatGridForFrontend(values));
  }
  const count = combinationsInGrid(strategy.grid);
  await page.getByText(`预计生成 ${count} 个实验`, { exact: false }).waitFor({ timeout: stepTimeoutMs });
  const submitButton = page.getByRole(
    'button',
    { name: `提交扫描（${count} 个组合）` },
  );
  await submitButton.waitFor({ state: 'visible', timeout: stepTimeoutMs });
  await submitButton.click({ trial: true, timeout: stepTimeoutMs });
  let observed;
  try {
    observed = await observePageWrite(page, guard, 'sweep', async () => {
      await submitButton.click();
    });
  } catch (error) {
    if (!(error instanceof AmbiguousPageWriteError)) throw error;
    const recovered = await recoverSweepAfterAmbiguousSubmit(page, intent);
    if (!recovered) throw error;
    await page.goto(recovered.url, {
      waitUntil: 'domcontentloaded',
      timeout: stepTimeoutMs,
    });
    return { page, guard, record: recovered };
  }
  if (!observed.response || typeof observed.response !== 'object') {
    throw new Error(`${strategy.strategy_id} sweep response is missing`);
  }
  const sweepId = Number(observed.response?.sweep_id);
  assertSameJson('Sweep grid', observed.request.param_grid, strategy.grid);
  assertSameJson('Sweep base params', observed.request.base_params, strategy.base_params);
  assertSameJson('Sweep industries', observed.request.pool_industries, '');
  assertPoolCodesMatch(
    'Sweep codes',
    observed.request.pool_custom_codes,
    configuredPoolCodes(config),
    config.dataset.pool_preset,
  );
  if (
    !Number.isInteger(sweepId)
    || sweepId <= 0
    || observed.request.strategy_id !== strategy.strategy_id
    || observed.request.name !== intent.identity.name
    || observed.request.pool_preset !== config.dataset.pool_preset
    || observed.request.mode !== 'batch'
    || observed.request.data_access_policy !== 'cache_only'
    || Number(observed.request.source_experiment_id) !== baselineId
    || observed.request.selection_start !== config.windows.selection_start
    || observed.request.selection_end !== config.windows.selection_end
    || observed.request.locked_test_start !== config.windows.locked_test_start
    || observed.request.locked_test_end !== config.windows.locked_test_end
    || observed.response.total_experiments !== count
    || observed.response.research_trust !== 'locked_test'
    || observed.response.data_access_policy !== 'cache_only'
    || !Array.isArray(observed.response.experiment_ids)
    || observed.response.experiment_ids.length !== count
  ) {
    throw new Error(`${strategy.strategy_id} sweep page payload differs from protocol`);
  }
  const persistentUrl = `${frontendUrl}/experiment/sweep?sweep_id=${sweepId}`;
  await page.waitForURL(
    (url) => (
      url.pathname === '/experiment/sweep'
      && url.searchParams.get('sweep_id') === String(sweepId)
    ),
    { timeout: stepTimeoutMs },
  );
  return {
    page,
    guard,
    record: {
      sweep_id: sweepId,
      url: persistentUrl,
      total_experiments: count,
      submitted_at: nowIso(),
      status: 'submitted',
    },
  };
}

async function restoreSweepTab(context, guardFactory, sweep) {
  const page = await context.newPage();
  const guard = await guardFactory(page);
  await page.goto(sweep.url, { waitUntil: 'domcontentloaded', timeout: stepTimeoutMs });
  await page.getByRole('heading', { name: `参数扫描 #${sweep.sweep_id}` }).waitFor({
    timeout: stepTimeoutMs,
  });
  return { page, guard };
}

async function waitForSweep(page, sweepId, expectedTotal) {
  const timeoutMs = jobTimeoutMs * Math.max(1, expectedTotal);
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const result = await browserReadJson(page, `/api/experiments/sweep/${sweepId}`);
    const terminal = result.experiments.filter((item) => TERMINAL.has(item.status)).length;
    if (
      result.experiments.length === expectedTotal
      && terminal === expectedTotal
      && TERMINAL.has(result.sweep.status)
    ) {
      return result;
    }
    await page.waitForTimeout(2500);
  }
  throw new Error(`sweep ${sweepId} timed out after ${timeoutMs}ms`);
}

async function repairSweepIfNeeded(page, guard, sweepId) {
  const current = await browserReadJson(page, `/api/experiments/sweep/${sweepId}`);
  const repairableIds = current.repairable_experiment_ids ?? [];
  if (repairableIds.length === 0) return null;
  await page.getByRole('button', {
    name: `重试可恢复成员（${repairableIds.length}）`,
  }).waitFor({ timeout: stepTimeoutMs });
  const observed = await observePageWrite(page, guard, 'repair', async () => {
    await page.getByRole('button', {
      name: `重试可恢复成员（${repairableIds.length}）`,
    }).click();
  });
  const repairedIds = observed.response?.repaired_experiment_ids;
  if (
    Number(observed.response?.sweep_id) !== sweepId
    || !Array.isArray(repairedIds)
    || JSON.stringify([...repairedIds].sort((a, b) => a - b))
      !== JSON.stringify([...repairableIds].sort((a, b) => a - b))
  ) {
    throw new Error(`Sweep ${sweepId} repair response differs from recoverable members`);
  }
  return repairedIds;
}

async function promoteFromSweep(page, guard, sweepId, experimentId) {
  const idCell = page.getByText(`#${experimentId}`, { exact: true }).last();
  const row = idCell.locator('xpath=ancestor::tr');
  await row.getByRole('button', { name: '选择此组合' }).click();
  const observed = await observePageWrite(page, guard, 'promote', async () => {
    await page.getByRole('button', { name: '确认并创建锁定测试' }).click();
  });
  if (!observed.response || typeof observed.response !== 'object') {
    throw new Error(`Sweep ${sweepId} promotion response is missing`);
  }
  if (
    Number(observed.request.experiment_id) !== experimentId
    || Number(observed.response.sweep_id) !== sweepId
    || Number(observed.response.source_experiment_id) !== experimentId
    || observed.response.research_trust !== 'locked_test'
  ) {
    throw new Error(`Sweep ${sweepId} promotion page payload differs from selected candidate`);
  }
  return {
    source_experiment_id: experimentId,
    experiment_id: Number(observed.response.experiment_id),
    job_id: observed.response.job_id ? String(observed.response.job_id) : null,
    created: Boolean(observed.response.created),
    submitted_at: nowIso(),
  };
}

async function safeFailureScreenshot(page, artifactDir, strategyId, stage) {
  if (!page || page.isClosed()) return null;
  const safeStage = stage.replace(/[^A-Za-z0-9_-]/g, '_');
  const path = join(artifactDir, 'failures', `${strategyId}-${safeStage}-${Date.now()}.png`);
  await ensurePrivateDirectory(dirname(path));
  try {
    await page.locator('input[type="password"], input[autocomplete="username"]').fill('');
  } catch {
    // The current page may not contain login inputs.
  }
  await page.addStyleTag({
    content: 'header { visibility: hidden !important; } input[type="password"] { visibility: hidden !important; }',
  }).catch(() => undefined);
  await page.screenshot({ path, fullPage: true });
  await chmod(path, 0o600);
  return path;
}

async function runLive(config, state, statePath, artifactDir, runtime) {
  const executionConfirmation = `${config.expected.total_experiments}_FRONTEND_EXPERIMENTS`;
  if (mode === 'execute' && process.env.QUANT_TUNING_EXECUTE_CONFIRM !== executionConfirmation) {
    throw new Error(
      `Execution is locked; set QUANT_TUNING_EXECUTE_CONFIRM=${executionConfirmation} after reviewing the protocol`,
    );
  }
  const playwright = await loadPlaywright();
  const executablePath = process.env.QUANT_TUNING_BROWSER_EXECUTABLE || undefined;
  const browser = await playwright.chromium.launch({
    headless: process.env.QUANT_TUNING_HEADLESS !== '0',
    env: browserProcessEnv(),
    ...(executablePath ? { executablePath } : {}),
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    serviceWorkers: 'block',
  });
  runtime.browser = browser;
  runtime.context = context;
  const guards = runtime.guards;
  const guardFactory = async (page) => {
    page.setDefaultTimeout(stepTimeoutMs);
    const guard = await installWriteGuard(page);
    guards.push(guard);
    return guard;
  };
  const controlPage = await context.newPage();
  const controlGuard = await guardFactory(controlPage);
  await login(controlPage, controlGuard);
  const preflight = await livePreflight(controlPage, controlGuard, config);
  const checkpointUserId = state.preflight?.account?.user_id;
  if (
    checkpointUserId != null
    && String(checkpointUserId) !== String(preflight.account.user_id)
  ) {
    throw new Error(
      `Checkpoint belongs to account ${checkpointUserId}; refusing resume as another account`,
    );
  }
  state.preflight = preflight;
  await persistState(statePath, state);
  if (mode === 'live-preflight') return runtime;

  for (const strategy of config.strategies) {
    const item = state.strategies[strategy.strategy_id];
    try {
      const expectedIntent = baselineSubmissionIntent(strategy, config);
      const hadIntent = Boolean(item.baseline_intent);
      item.baseline_intent = ensureStoredIntent(
        item.baseline_intent,
        expectedIntent,
        `${strategy.strategy_id} baseline`,
      );
      if (!hadIntent) await persistState(statePath, state);
      if (!item.baseline) {
        const recovery = await lookupSubmissionRecovery(
          controlPage,
          item.baseline_intent,
        );
        item.baseline = recoverBaselineCandidate(
          recovery?.experiments,
          item.baseline_intent,
        );
        if (!item.baseline) {
          item.baseline = await createBaseline(
            controlPage,
            controlGuard,
            strategy,
            config,
            item.baseline_intent,
          );
        }
        await persistState(statePath, state);
      }
      const experiment = await waitForExperiment(controlPage, item.baseline.experiment_id);
      item.baseline.status = experiment.status;
      item.baseline.completed_at = experiment.completed_at ?? null;
      item.baseline.metrics = {
        sharpe_ratio: experiment.sharpe_ratio ?? null,
        annual_return: experiment.annual_return ?? null,
        max_drawdown: experiment.max_drawdown ?? null,
        win_rate: experiment.win_rate ?? null,
      };
      if (
        experiment.data_access_policy !== 'cache_only'
        || experiment.source_experiment_id != null
      ) {
        throw new Error(
          `Baseline ${item.baseline.experiment_id} did not persist the cache_only root identity`,
        );
      }
      await controlPage.goto(item.baseline.url, { waitUntil: 'domcontentloaded', timeout: stepTimeoutMs });
      await controlPage.getByText(`#${item.baseline.experiment_id}`, { exact: false }).first().waitFor({
        timeout: stepTimeoutMs,
      });
      if (experiment.status !== 'completed') {
        throw new Error(`Baseline ${item.baseline.experiment_id} ended as ${experiment.status}`);
      }
      clearResolvedStageError(item, 'baseline');
      await persistState(statePath, state);
    } catch (error) {
      item.last_error = { stage: 'baseline', message: error.message, at: nowIso() };
      item.failure_screenshot = await safeFailureScreenshot(
        controlPage, artifactDir, strategy.strategy_id, 'baseline',
      );
      await persistState(statePath, state);
      throw error;
    }
  }

  // This host has one experiment worker slot and limited memory. Keep the
  // entire sweep lifecycle serial: submitting later sweeps while an earlier
  // one is running can starve the cache-only readiness request in the web
  // process. A strategy does not release its page or yield to the next
  // strategy until its sweep and promoted locked test are terminal.
  await runStrategiesSequentially(config.strategies, async (strategy) => {
    const item = state.strategies[strategy.strategy_id];
    let page = null;
    let guard = null;
    try {
      try {
        const expectedIntent = sweepSubmissionIntent(
          strategy,
          item.baseline.experiment_id,
          config,
        );
        const hadIntent = Boolean(item.sweep_intent);
        item.sweep_intent = ensureStoredIntent(
          item.sweep_intent,
          expectedIntent,
          `${strategy.strategy_id} sweep`,
        );
        if (!hadIntent) await persistState(statePath, state);
        if (!item.sweep) {
          const ambiguousAttempt = item.sweep_submission_ambiguous;
          if (
            ambiguousAttempt
            && ambiguousAttempt.identity_sha256 !== item.sweep_intent.identity_sha256
          ) {
            throw new Error(
              `${strategy.strategy_id} ambiguous sweep marker differs from protocol`,
            );
          }
          const recovery = await lookupSubmissionRecovery(
            controlPage,
            item.sweep_intent,
          );
          const recovered = recoverSweepCandidate(
            recovery?.sweeps,
            item.sweep_intent,
          );
          if (recovered) {
            await validateRecoveredSweepMembers(
              controlPage,
              recovered,
              item.sweep_intent,
            );
            item.sweep = recovered;
            delete item.sweep_submission_ambiguous;
            await persistState(statePath, state);
            ({ page, guard } = await restoreSweepTab(
              context,
              guardFactory,
              item.sweep,
            ));
          } else if (ambiguousAttempt) {
            throw new Error(
              `${strategy.strategy_id} previous sweep POST outcome remains ambiguous; `
              + 'refusing a duplicate POST until the persisted intent becomes recoverable',
            );
          } else {
            const created = await createSweepTab(
              context,
              guardFactory,
              strategy,
              item.baseline.experiment_id,
              config,
              item.sweep_intent,
            );
            item.sweep = created.record;
            await persistState(statePath, state);
            if (created.page.url() !== created.record.url) {
              await created.page.close();
              throw new Error(
                `Frontend did not persist sweep ${created.record.sweep_id} in the URL; merge sweep URL recovery first`,
              );
            }
            ({ page, guard } = created);
          }
        } else {
          ({ page, guard } = await restoreSweepTab(
            context,
            guardFactory,
            item.sweep,
          ));
        }
        if (clearResolvedStageError(item, 'sweep-submit-or-restore')) {
          await persistState(statePath, state);
        }
      } catch (error) {
        if (
          error instanceof AmbiguousPageWriteError
          && error.kind === 'sweep'
        ) {
          item.sweep_submission_ambiguous = {
            identity_sha256: item.sweep_intent.identity_sha256,
            observed_at: nowIso(),
            message: error.message,
          };
        }
        item.last_error = {
          stage: 'sweep-submit-or-restore',
          message: error.message,
          at: nowIso(),
        };
        await persistState(statePath, state);
        throw error;
      }
      try {
        const repairedIds = await repairSweepIfNeeded(
          page,
          guard,
          item.sweep.sweep_id,
        );
        if (repairedIds) {
          item.sweep.repaired_experiment_ids = repairedIds;
          item.sweep.repaired_at = nowIso();
          await persistState(statePath, state);
        }
        const result = await waitForSweep(
          page,
          item.sweep.sweep_id,
          item.sweep.total_experiments,
        );
        const nonCompleted = result.experiments.filter(
          (experiment) => experiment.status !== 'completed',
        );
        if (nonCompleted.length) {
          throw new Error(
            `Sweep ${item.sweep.sweep_id} has ${nonCompleted.length} failed or cancelled members`,
          );
        }
        if (
          result.sweep.status !== 'completed'
          || result.sweep.research_trust !== 'locked_test'
          || result.sweep.data_access_policy !== 'cache_only'
          || result.sweep.selection_start !== config.windows.selection_start
          || result.sweep.selection_end !== config.windows.selection_end
          || result.sweep.locked_test_start !== config.windows.locked_test_start
          || result.sweep.locked_test_end !== config.windows.locked_test_end
        ) {
          throw new Error(`Sweep ${item.sweep.sweep_id} protocol drifted`);
        }
        await validateRecoveredSweepMembers(
          page,
          item.sweep,
          item.sweep_intent,
        );
        item.sweep.status = result.sweep.status;
        item.sweep.completed_experiments = result.experiments.length;
        const ranked = choosePromotionCandidate(
          result.experiments,
          strategy.api_metadata,
          config.ranking.near_tie_tolerance,
        );
        item.selection = {
          selected_experiment_id: Number(ranked.candidate.id),
          best_sharpe: ranked.best_sharpe,
          selected_metrics: ranked.candidate.selection_metrics,
          selected_params: ranked.candidate.params,
          near_tie_ids: ranked.near_tie_ids,
          protocol: config.ranking,
        };
        const recoveredPromotionId = Number(result.sweep.promoted_experiment_id);
        const recoveredSourceId = Number(
          result.sweep.promotion_source_experiment_id,
        );
        if (
          !item.promotion
          && Number.isInteger(recoveredPromotionId)
          && recoveredPromotionId > 0
          && Number.isInteger(recoveredSourceId)
          && recoveredSourceId > 0
        ) {
          item.promotion = {
            source_experiment_id: recoveredSourceId,
            experiment_id: recoveredPromotionId,
            job_id: null,
            created: false,
            recovered_from_sweep: true,
            submitted_at: null,
          };
          await persistState(statePath, state);
        }
        if (!item.promotion) {
          item.promotion = await promoteFromSweep(
            page,
            guard,
            item.sweep.sweep_id,
            item.selection.selected_experiment_id,
          );
          await persistState(statePath, state);
        } else if (
          item.promotion.source_experiment_id
          !== item.selection.selected_experiment_id
        ) {
          throw new Error(
            `Sweep ${item.sweep.sweep_id} checkpoint promotion conflicts with ranking`,
          );
        }
        const locked = await waitForExperiment(
          page,
          item.promotion.experiment_id,
        );
        item.locked_test = {
          experiment_id: item.promotion.experiment_id,
          url: `${frontendUrl}/experiment/${item.promotion.experiment_id}`,
          status: locked.status,
          completed_at: locked.completed_at ?? null,
          metrics: {
            sharpe_ratio: locked.sharpe_ratio ?? null,
            annual_return: locked.annual_return ?? null,
            max_drawdown: locked.max_drawdown ?? null,
            win_rate: locked.win_rate ?? null,
          },
        };
        const detailPage = await context.newPage();
        await guardFactory(detailPage);
        try {
          await detailPage.goto(item.locked_test.url, {
            waitUntil: 'domcontentloaded',
            timeout: stepTimeoutMs,
          });
          await detailPage.getByText(
            `#${item.locked_test.experiment_id}`,
            { exact: false },
          ).first().waitFor({ timeout: stepTimeoutMs });
        } finally {
          await detailPage.close();
        }
        if (locked.status !== 'completed') {
          throw new Error(`Locked test ${locked.id} ended as ${locked.status}`);
        }
        if (
          locked.data_access_policy !== 'cache_only'
          || Number(locked.source_experiment_id)
            !== item.selection.selected_experiment_id
        ) {
          throw new Error(
            `Locked test ${locked.id} did not inherit the selected cache_only member identity`,
          );
        }
        clearResolvedStageError(item, 'sweep-rank-promote');
        await persistState(statePath, state);
      } catch (error) {
        item.last_error = {
          stage: 'sweep-rank-promote',
          message: error.message,
          at: nowIso(),
        };
        item.failure_screenshot = await safeFailureScreenshot(
          page,
          artifactDir,
          strategy.strategy_id,
          'sweep-rank-promote',
        );
        await persistState(statePath, state);
        throw error;
      }
    } finally {
      if (page && !page.isClosed()) await page.close();
    }
  });
  return runtime;
}

function buildReport(config, state, guardSummaries, status, error = null) {
  const strategies = Object.entries(state.strategies).map(([strategyId, item]) => ({
    strategy_id: strategyId,
    selection_combinations: item.selection_combinations,
    baseline: item.baseline,
    sweep: item.sweep,
    selection: item.selection ?? null,
    promotion: item.promotion,
    locked_test: item.locked_test,
    last_error: item.last_error,
    failure_screenshot: item.failure_screenshot ?? null,
  }));
  return {
    schema_version: REPORT_SCHEMA,
    generated_at: nowIso(),
    status,
    mode,
    campaign: config.campaign,
    data_disclosure: config.dataset.disclosure ?? (
      'All reported research results use declared deterministic synthetic data. '
      + 'They are software acceptance evidence, not real-market performance or deployment evidence.'
    ),
    protocol: summarizeConfig(config),
    preflight: state.preflight,
    progress: {
      baseline_completed: strategies.filter((item) => item.baseline?.status === 'completed').length,
      sweep_submitted: strategies.filter((item) => item.sweep?.sweep_id).length,
      sweep_completed: strategies.filter((item) => item.sweep?.status === 'completed').length,
      locked_test_completed: strategies.filter((item) => item.locked_test?.status === 'completed').length,
    },
    browser_write_guard: guardSummaries,
    strategies,
    error: error ? { name: error.name, message: error.message } : null,
  };
}

export async function main() {
  const config = await loadTuningConfig(configPath);
  const fingerprint = configFingerprint(config);
  const artifactDir = resolve(
    process.env.QUANT_TUNING_ARTIFACT_DIR
      || join(tmpdir(), `quant-platform-${config.campaign}`),
  );
  const statePath = join(artifactDir, 'checkpoint.json');
  const reportPath = join(artifactDir, 'report.json');
  const state = await readState(statePath, config.campaign, fingerprint, config);
  state.mode = mode;
  if (mode === 'dry-run') {
    const report = buildReport(config, state, [], 'dry-run-valid');
    await atomicJson(reportPath, report);
    process.stdout.write(`${JSON.stringify({
      status: 'dry-run-valid',
      campaign: config.campaign,
      strategies: config.expected.strategy_count,
      baseline_experiments: config.expected.baseline_experiments,
      selection_experiments: config.expected.selection_experiments,
      locked_test_experiments: config.expected.locked_test_experiments,
      total_experiments: config.expected.total_experiments,
      report: reportPath,
    })}\n`);
    return;
  }

  const runtime = { guards: [], browser: null, context: null };
  let releaseLock = null;
  try {
    validateLiveOrigins(frontendUrl, backendUrl);
    if (mode === 'execute') {
      releaseLock = await acquireCampaignLock(
        artifactDir,
        config.campaign,
        fingerprint,
      );
    }
    await runLive(config, state, statePath, artifactDir, runtime);
    const summaries = runtime.guards.map((guard) => guard.summary());
    for (const guard of runtime.guards) guard.assertClean();
    const status = mode === 'live-preflight' ? 'live-preflight-valid' : 'completed';
    await atomicJson(reportPath, buildReport(config, state, summaries, status));
    process.stdout.write(`${JSON.stringify({ status, report: reportPath, checkpoint: statePath })}\n`);
  } catch (error) {
    const summaries = runtime?.guards?.map((guard) => guard.summary()) ?? [];
    await atomicJson(reportPath, buildReport(config, state, summaries, 'failed', error));
    process.stderr.write(`${JSON.stringify({ status: 'failed', error: error.message, report: reportPath })}\n`);
    process.exitCode = 1;
  } finally {
    await runtime?.browser?.close();
    await releaseLock?.();
  }
}

const invokedPath = process.argv[1] ? pathToFileURL(resolve(process.argv[1])).href : null;
if (invokedPath === import.meta.url) {
  await main();
}
