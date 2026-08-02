import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  archiveFactorResearchRun,
  compareFactorResearchRuns,
  createFactorProtocol,
  createFactorProtocolVersion,
  exportFactorStrategy,
  getFactorStrategyVersions,
  getFactorResearchReadiness,
  getFactorResearchRun,
  listFactorResearchRuns,
  listFactorProtocols,
  lockFactorProtocol,
  rollbackFactorStrategy,
  setFactorLifecycle,
  submitFactorResearchJob,
} from './factorResearch';

const { get, post, deleteRequest } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  deleteRequest: vi.fn(),
}));

vi.mock('./api', () => ({
  default: { get, post, delete: deleteRequest },
}));

afterEach(() => {
  get.mockReset();
  post.mockReset();
  deleteRequest.mockReset();
});

describe('factor research v2 API contract', () => {
  it('loads readiness and user history from dedicated endpoints', async () => {
    const readiness = { ready: false, pools: [], limits: {} };
    get.mockResolvedValueOnce({ data: { data: readiness } });
    await expect(getFactorResearchReadiness()).resolves.toBe(readiness);
    expect(get).toHaveBeenLastCalledWith('/api/factor-research/readiness');

    const page = { items: [], total: 0, page: 2, page_size: 10 };
    get.mockResolvedValueOnce({ data: { data: page } });
    await expect(listFactorResearchRuns({
      factor_id: 'momentum_20',
      query: 'frun_',
      sort: 'oldest',
      page: 2,
      page_size: 10,
    })).resolves.toEqual(page);
    expect(get).toHaveBeenLastCalledWith('/api/factor-research/runs', {
      params: {
        factor_id: 'momentum_20',
        query: 'frun_',
        sort: 'oldest',
        page: 2,
        page_size: 10,
      },
    });
  });

  it('encodes run ids for detail and archive requests', async () => {
    const run = { run_id: 'frun_1' };
    get.mockResolvedValueOnce({ data: { data: run } });
    await expect(getFactorResearchRun('frun_/unsafe')).resolves.toBe(run);
    expect(get).toHaveBeenCalledWith('/api/factor-research/runs/frun_%2Funsafe');

    deleteRequest.mockResolvedValueOnce({ data: { data: { archived: true } } });
    await archiveFactorResearchRun('frun_/unsafe');
    expect(deleteRequest).toHaveBeenCalledWith('/api/factor-research/runs/frun_%2Funsafe');
  });

  it('passes selected immutable runs to comparison and export', async () => {
    post.mockResolvedValueOnce({
      data: { data: { schema_version: 'v1', dataset_consistent: true, runs: [] } },
    });
    await compareFactorResearchRuns(['frun_a', 'frun_b']);
    expect(post).toHaveBeenLastCalledWith('/api/factor-research/compare', {
      run_ids: ['frun_a', 'frun_b'],
    });

    post.mockResolvedValueOnce({
      data: {
        data: {
          strategy_id: 'factor_combo_123456789abc',
          definition_sha256: 'a'.repeat(64),
          research_evidence: [{ run_id: 'frun_a' }],
        },
      },
    });
    const body = {
      name: '证据组合',
      components: [{ factor_id: 'momentum_20', weight: 1 }],
      top_k_pct: 0.1,
      research_run_ids: ['frun_a'],
    };
    await exportFactorStrategy(body);
    expect(post).toHaveBeenLastCalledWith('/api/factor-research/export-strategy', body);
  });

  it('submits long-running analysis through the durable job endpoint', async () => {
    post.mockResolvedValueOnce({
      data: { data: { job_id: 'job-factor-1', status: 'pending' } },
    });
    const body = {
      factor_id: 'momentum_20',
      pool_preset: 'csi300',
      pool_custom_codes: [],
      start: '2024-01-01',
      end: '2024-12-31',
      horizons: [1, 5, 20],
      primary_horizon: 5,
      quantiles: 5,
      winsor_method: 'mad' as const,
    };

    await expect(submitFactorResearchJob(body)).resolves.toEqual({
      job_id: 'job-factor-1',
      status: 'pending',
    });
    expect(post).toHaveBeenLastCalledWith('/api/factor-research/jobs', body);
  });

  it('creates, versions and locks user factor protocols', async () => {
    const payload = {
      schema_version: 'factor-research-protocol/v1' as const,
      question: '该因子在成本后是否仍然有效？',
      hypothesis: 'RankIC 和多空收益达到预注册门槛。',
      factor_ids: ['momentum_20'],
      data: {
        pool_id: 'csi300',
        version_policy: 'latest_trusted_at_execution' as const,
      },
      window: { start: '2021-01-01', end: '2024-12-31' },
      implementation: {
        horizons: [1, 5, 20],
        primary_horizon: 5,
        quantiles: 5,
        rebalance_interval: 5,
        default_cost_bps: 10,
        cost_scenarios_bps: [0, 10, 20],
        neutralization: 'none' as const,
      },
      thresholds: {
        rank_ic_mean_min: 0.02,
        rank_ic_ir_min: 0.3,
        long_short_mean_min: 0,
      },
      export_rules: {
        allow_strategy_export: true,
        require_all_thresholds: true,
        require_dataset_consistency: true,
        minimum_evidence_runs: 1,
      },
    };
    const series = {
      protocol_id: `fproto_${'a'.repeat(32)}`,
      current_version: 1,
      versions: [],
    };
    get.mockResolvedValueOnce({ data: { data: [series] } });
    await expect(listFactorProtocols()).resolves.toEqual([series]);

    post.mockResolvedValueOnce({ data: { data: series } });
    await createFactorProtocol({ name: '预注册', payload });
    expect(post).toHaveBeenLastCalledWith('/api/factor-research/protocols', {
      name: '预注册',
      payload,
    });

    post.mockResolvedValueOnce({ data: { data: { ...series, current_version: 2 } } });
    await createFactorProtocolVersion({
      protocol_id: series.protocol_id,
      expected_current_version: 1,
      payload,
    });
    expect(post).toHaveBeenLastCalledWith(
      `/api/factor-research/protocols/${series.protocol_id}/versions`,
      { expected_current_version: 1, payload },
    );

    const reference = {
      protocol_id: series.protocol_id,
      version: 1,
      payload_digest: 'b'.repeat(64),
    };
    post.mockResolvedValueOnce({ data: { data: { ...reference, status: 'locked' } } });
    await lockFactorProtocol(reference);
    expect(post).toHaveBeenLastCalledWith(
      `/api/factor-research/protocols/${series.protocol_id}/versions/1/lock`,
      { payload_digest: reference.payload_digest },
    );
  });

  it('uses exact versions, revisions and idempotency for governance writes', async () => {
    post.mockResolvedValueOnce({
      data: { data: { factor_id: 'momentum_20', status: 'deprecated' } },
    });
    await setFactorLifecycle({
      factor_id: 'momentum_20',
      version: '1.0.0',
      definition_digest: 'a'.repeat(64),
      expected_revision: 3,
      status: 'deprecated',
      idempotency_key: 'factor-deprecate-1',
    });
    expect(post).toHaveBeenLastCalledWith(
      '/api/factor-research/catalog/momentum_20/versions/1.0.0/deprecate',
      {
        definition_digest: 'a'.repeat(64),
        expected_revision: 3,
        idempotency_key: 'factor-deprecate-1',
      },
    );

    get.mockResolvedValueOnce({
      data: {
        data: {
          strategy_id: 'factor_combo_123456789abc',
          current_version: 2,
          series_revision: 2,
          versions: [],
        },
      },
    });
    await getFactorStrategyVersions('factor_combo_/unsafe');
    expect(get).toHaveBeenLastCalledWith(
      '/api/factor-research/strategies/factor_combo_%2Funsafe/versions',
    );

    post.mockResolvedValueOnce({
      data: {
        data: {
          strategy_id: 'factor_combo_123456789abc',
          strategy_version: 1,
        },
      },
    });
    await rollbackFactorStrategy({
      strategy_id: 'factor_combo_123456789abc',
      target_version: 1,
      expected_version: 2,
      idempotency_key: 'strategy-rollback-1',
    });
    expect(post).toHaveBeenLastCalledWith(
      '/api/factor-research/strategies/factor_combo_123456789abc/rollback',
      {
        target_version: 1,
        expected_version: 2,
        idempotency_key: 'strategy-rollback-1',
      },
    );
  });
});
