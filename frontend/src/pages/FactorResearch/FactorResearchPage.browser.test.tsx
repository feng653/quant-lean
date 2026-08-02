// @vitest-environment jsdom

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  FactorResearchResult,
  FactorResearchRun,
  FactorResearchRunPage,
} from '../../services/factorResearch';
import type { Job, JobUpdateEvent } from '../../types/job';
import FactorResearchPage from './FactorResearchPage';

const RUN_A = `frun_${'a'.repeat(32)}`;
const RUN_B = `frun_${'b'.repeat(32)}`;
const JOB_A = 'factor-job-a';

const mocks = vi.hoisted(() => ({
  listResearchFactors: vi.fn(),
  getFactorResearchReadiness: vi.fn(),
  listFactorResearchRuns: vi.fn(),
  submitFactorResearchJob: vi.fn(),
  getFactorResearchRun: vi.fn(),
  compareFactorResearchRuns: vi.fn(),
  exportFactorStrategy: vi.fn(),
  archiveFactorResearchRun: vi.fn(),
  listJobs: vi.fn(),
  cancelJob: vi.fn(),
  retryJob: vi.fn(),
  downloadFactorEvidence: vi.fn(),
  eventHandler: null as ((event: JobUpdateEvent) => void) | null,
}));

vi.mock('../../services/factorResearch', async (importOriginal) => {
  const original = await importOriginal<typeof import('../../services/factorResearch')>();
  return {
    ...original,
    listResearchFactors: mocks.listResearchFactors,
    getFactorResearchReadiness: mocks.getFactorResearchReadiness,
    listFactorResearchRuns: mocks.listFactorResearchRuns,
    submitFactorResearchJob: mocks.submitFactorResearchJob,
    getFactorResearchRun: mocks.getFactorResearchRun,
    compareFactorResearchRuns: mocks.compareFactorResearchRuns,
    exportFactorStrategy: mocks.exportFactorStrategy,
    archiveFactorResearchRun: mocks.archiveFactorResearchRun,
  };
});

vi.mock('../../services/jobs', () => ({
  listJobs: mocks.listJobs,
  cancelJob: mocks.cancelJob,
  retryJob: mocks.retryJob,
}));

vi.mock('../../services/factorEvidenceExport', async (importOriginal) => {
  const original = await importOriginal<typeof import('../../services/factorEvidenceExport')>();
  return { ...original, downloadFactorEvidence: mocks.downloadFactorEvidence };
});

vi.mock('../../hooks/useWebSocket', () => ({
  useJobEvents: (handler: (event: JobUpdateEvent) => void) => {
    mocks.eventHandler = handler;
  },
}));

vi.mock('../../components/shared/EChart', () => ({
  default: () => <div data-testid="chart" />,
}));
vi.mock('./FactorCatalogGovernance', () => ({
  default: () => <div data-testid="factor-catalog" />,
}));
vi.mock('./FactorProtocolPanel', () => ({
  default: () => <div data-testid="factor-protocols" />,
}));
vi.mock('./PointInTimeReadiness', () => ({
  PointInTimeReadinessSummary: () => <span>点时摘要</span>,
  PointInTimeReadinessDetails: () => <span>点时详情</span>,
}));
vi.mock('./FactorStabilityConfig', () => ({
  default: () => <div data-testid="stability-config" />,
}));
vi.mock('./FactorStabilityResults', () => ({
  default: () => <div data-testid="stability-results" />,
}));
vi.mock('./NeutralizationConfig', () => ({
  default: () => <div data-testid="neutralization-config" />,
}));
vi.mock('./NeutralizationResult', () => ({
  default: () => <div data-testid="neutralization-result" />,
}));
vi.mock('./FactorExportEvidence', () => ({
  default: () => <div data-testid="factor-export-evidence" />,
}));

function runSummary(runId: string, factorId = 'momentum_20'): FactorResearchRun {
  return {
    run_id: runId,
    factor_id: factorId,
    request: {
      factor_id: factorId,
      pool_preset: 'csi300',
      start: '2024-01-01',
      end: '2024-12-31',
      horizons: [1, 5, 20],
      primary_horizon: 5,
      quantiles: 5,
    },
    request_digest: '1'.repeat(64),
    dataset_digest: '2'.repeat(64),
    result_digest: '3'.repeat(64),
    run_digest: '4'.repeat(64),
    schema_version: 'factor-research-run/v3',
    created_at: '2026-07-31T00:00:00Z',
    source_job_uuid: JOB_A,
    archived_at: null,
  };
}

function resultPayload(): FactorResearchResult {
  return {
    schema_version: 'factor-research/v4',
    factor: {
      factor_id: 'momentum_20',
      name: '20日动量',
      category: 'momentum',
      required_fields: ['close'],
      current: true,
      deprecated: false,
    },
    request: runSummary(RUN_A).request,
    dataset: {
      cache_key: 'csi300',
      rows: 120,
      codes: 300,
      date_start: '2024-01-01',
      date_end: '2024-12-31',
      content_sha256: '2'.repeat(64),
      source_provenance: { content_sha256: '5'.repeat(64) },
    },
    preprocessing: { config: {}, diagnostics: [] },
    ic: {
      '5': {
        series: [],
        summary: {
          pearson_ic: {
            count: 10,
            mean: 0.03,
            std: 0.1,
            icir: 0.3,
            positive_ratio: 0.6,
            t_stat: 1,
          },
          rank_ic: {
            count: 10,
            mean: 0.04,
            std: 0.1,
            icir: 0.4,
            positive_ratio: 0.7,
            t_stat: 1.2,
          },
        },
      },
    },
    decay: { points: [] },
    quantile_returns: {
      mean_group_returns: { '1': -0.01, '5': 0.01 },
      long_short: {
        count: 10,
        mean: 0.02,
        std: 0.1,
        icir: 0.2,
        positive_ratio: 0.6,
        t_stat: 1,
      },
      monotonicity: 0.8,
    },
    stability: null,
    neutralization: null,
    limitations: ['仅用于研究。'],
  } as unknown as FactorResearchResult;
}

function job(status: Job['status'], result?: Record<string, unknown>): Job {
  return {
    id: 1,
    job_uuid: JOB_A,
    job_type: 'factor_research',
    params: { factor_id: 'momentum_20' },
    status,
    progress: status === 'completed' || status === 'failed' ? 1 : 0.2,
    result,
    error: status === 'failed' ? 'SECRET traceback /Users/example' : null,
    resource_type: 'factor_research',
    resource_id: 'momentum_20',
    attempt: 1,
    created_at: '2026-07-31T00:00:00Z',
  };
}

describe('FactorResearchPage browser workflow', () => {
  let history: FactorResearchRunPage;
  let jobs: Job[];

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.eventHandler = null;
    history = {
      items: [runSummary(RUN_B, 'short_reversal_5')],
      total: 1,
      page: 1,
      page_size: 10,
    };
    jobs = [];
    mocks.listResearchFactors.mockResolvedValue([
      {
        factor_id: 'momentum_20',
        name: '20日动量',
        category: 'momentum',
        required_fields: ['close'],
        current: true,
        deprecated: false,
      },
      {
        factor_id: 'short_reversal_5',
        name: '5日反转',
        category: 'reversal',
        required_fields: ['close'],
        current: true,
        deprecated: false,
      },
    ]);
    mocks.getFactorResearchReadiness.mockResolvedValue({
      schema_version: 'factor-research-readiness/v1',
      ready: true,
      pools: [{
        pool_id: 'csi300',
        label: '沪深300',
        ready: true,
        date_start: '2020-01-01',
        date_end: '2026-07-30',
        n_dates: 1500,
        n_stocks: 300,
        fields: ['close'],
        available_factor_ids: ['momentum_20', 'short_reversal_5'],
        schema_version: 4,
        source_trust: 'public_cross_validated_research_only',
        source_providers: ['baostock'],
        source_evidence_levels: ['official'],
        neutralization: {
          modes: {
            none: { ready: true, reason: null },
            industry: { ready: false, reason: 'not_ready' },
            size: { ready: false, reason: 'not_ready' },
            'industry+size': { ready: false, reason: 'not_ready' },
          },
          industry: { ready: false, reason: 'not_ready', scope_id: 'cninfo_008001' },
        },
      }],
      limits: {},
    });
    mocks.listFactorResearchRuns.mockImplementation(async () => history);
    mocks.listJobs.mockImplementation(async () => ({
      items: jobs,
      total: jobs.length,
      page: 1,
      page_size: 8,
    }));
    mocks.submitFactorResearchJob.mockImplementation(async () => {
      jobs = [job('pending')];
      return { job_id: JOB_A, status: 'pending' };
    });
    mocks.getFactorResearchRun.mockResolvedValue({
      ...runSummary(RUN_A),
      result: resultPayload(),
    });
    mocks.compareFactorResearchRuns.mockResolvedValue({
      schema_version: 'factor-research-comparison/v1',
      dataset_consistent: true,
      runs: [{
        run_id: RUN_A,
        factor_id: 'momentum_20',
        created_at: '2026-07-31T00:00:00Z',
        dataset_digest: '2'.repeat(64),
        primary_horizon: 5,
        rank_ic_mean: 0.04,
        rank_ic_ir: 0.4,
        rank_ic_positive_ratio: 0.7,
        long_short_mean: 0.02,
        monotonicity: 0.8,
      }],
    });
    mocks.downloadFactorEvidence.mockResolvedValue('factor-evidence.json');
  });

  afterEach(() => cleanup());

  it('loads readiness, submits, recovers completion, opens evidence, compares, exports and redacts failures', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <FactorResearchPage />
      </MemoryRouter>,
    );

    expect((await screen.findByRole('button', { name: '提交后台研究' })) as HTMLButtonElement).toHaveProperty('disabled', false);
    expect(mocks.listJobs).toHaveBeenCalledWith(expect.objectContaining({
      job_type: 'factor_research',
      mine: true,
      page_size: 8,
    }));
    expect(mocks.listFactorResearchRuns).toHaveBeenCalledWith(expect.objectContaining({
      sort: 'newest',
      page: 1,
      page_size: 10,
    }));

    await user.click(screen.getByRole('button', { name: '提交后台研究' }));
    expect(mocks.submitFactorResearchJob).toHaveBeenCalledWith(expect.objectContaining({
      factor_id: 'momentum_20',
      pool_preset: 'csi300',
      pool_custom_codes: [],
      primary_horizon: 5,
    }));
    expect((await screen.findAllByText('排队中')).length).toBeGreaterThan(0);

    history = {
      items: [runSummary(RUN_A), runSummary(RUN_B, 'short_reversal_5')],
      total: 2,
      page: 1,
      page_size: 10,
    };
    jobs = [job('completed', {
      run_id: RUN_A,
      dataset_digest: '2'.repeat(64),
      result_digest: '3'.repeat(64),
    })];
    await act(async () => {
      mocks.eventHandler?.({
        type: 'job_updated',
        job_uuid: JOB_A,
        job_type: 'factor_research',
        status: 'completed',
        progress: 1,
      });
    });

    expect(await screen.findByText('后台研究完成，结果与数据摘要已作为不可变运行保存。'))
      .not.toBeNull();
    expect(screen.getByText('请求摘要')).not.toBeNull();
    expect(screen.getByText('来源任务')).not.toBeNull();
    expect(screen.getByText('数据版本 / 摘要')).not.toBeNull();
    expect(screen.getByText(JOB_A)).not.toBeNull();
    expect(mocks.getFactorResearchRun).toHaveBeenCalledWith(RUN_A);

    await user.click(screen.getByLabelText(`选择研究 ${RUN_B}`));
    await user.click(screen.getByRole('button', { name: '比较已选' }));
    expect(mocks.compareFactorResearchRuns).toHaveBeenCalledWith(
      expect.arrayContaining([RUN_A, RUN_B]),
    );
    expect(await screen.findByText('数据摘要一致，可直接横向比较')).not.toBeNull();

    await user.click(screen.getAllByRole('button', { name: '导出 JSON' })[0]);
    expect(mocks.downloadFactorEvidence).toHaveBeenCalledWith(RUN_A, 'json');

    jobs = [job('failed', {
      error_code: 'factor_cache_integrity_invalid',
      message: '缓存完整性校验失败',
      cache_key: 'csi300',
      action: 'refresh_in_data_center',
    })];
    await act(async () => {
      mocks.eventHandler?.({
        type: 'job_updated',
        job_uuid: JOB_A,
        job_type: 'factor_research',
        status: 'failed',
        progress: 1,
      });
    });

    expect(await screen.findByText(/factor_cache_integrity_invalid/)).not.toBeNull();
    expect(screen.getByText(/缓存完整性校验失败/)).not.toBeNull();
    expect(screen.queryByText(/SECRET traceback/)).toBeNull();
    expect(screen.getByRole('button', { name: '前往数据中心修复' })).not.toBeNull();
  });

  it('requests subsequent history and task pages without client-side truncation', async () => {
    history = {
      items: [runSummary(RUN_B)],
      total: 21,
      page: 1,
      page_size: 10,
    };
    jobs = Array.from({ length: 8 }, (_, index) => ({
      ...job('completed', { run_id: `${RUN_A}-${index}` }),
      id: index + 1,
      job_uuid: `${JOB_A}-${index}`,
    }));
    mocks.listJobs.mockImplementation(async (filters: { page?: number }) => ({
      items: jobs,
      total: 17,
      page: filters.page ?? 1,
      page_size: 8,
    }));
    render(
      <MemoryRouter>
        <FactorResearchPage />
      </MemoryRouter>,
    );
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: '下一页历史' }));
    await waitFor(() => expect(mocks.listFactorResearchRuns).toHaveBeenCalledWith(
      expect.objectContaining({ page: 2, page_size: 10 }),
    ));
    await user.click(screen.getByRole('button', { name: '下一页任务' }));
    await waitFor(() => expect(mocks.listJobs).toHaveBeenCalledWith(
      expect.objectContaining({ page: 2, page_size: 8 }),
    ));
  });
});
