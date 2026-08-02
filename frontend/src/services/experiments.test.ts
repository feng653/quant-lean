import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  buildPortfolioCandidates,
  createExperiment,
  downloadExperimentEvidence,
  evidenceDownloadFilename,
  getStrategyCorrelation,
  listExperiments,
  type CreateExperimentData,
} from './experiments';

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock('./api', () => ({
  default: { get, post },
}));

afterEach(() => {
  get.mockReset();
  post.mockReset();
  vi.unstubAllGlobals();
});

describe('experiment collection API paths', () => {
  it('uses the canonical collection path for listing', async () => {
    get.mockResolvedValueOnce({
      data: {
        data: {
          items: [],
          total: 0,
          page: 1,
          limit: 20,
          sort_by: 'annual_return',
          sort_order: 'asc',
        },
      },
    });

    await expect(listExperiments({
      status: 'completed',
      sort_by: 'annual_return',
      sort_order: 'asc',
    })).resolves.toEqual({
      items: [],
      total: 0,
      page: 1,
      limit: 20,
      sort_by: 'annual_return',
      sort_order: 'asc',
    });
    expect(get).toHaveBeenCalledWith('/api/experiments/', {
      params: {
        status: 'completed',
        sort_by: 'annual_return',
        sort_order: 'asc',
      },
    });
  });

  it('keeps deterministic defaults for an empty legacy response', async () => {
    get.mockResolvedValueOnce({ data: {} });

    await expect(listExperiments()).resolves.toMatchObject({
      items: [],
      sort_by: 'created_at',
      sort_order: 'desc',
    });
  });

  it('posts experiment creation directly to the canonical collection path', async () => {
    const request: CreateExperimentData = {
      name: 'canonical-path',
      strategy_id: 'ma_cross_v1',
      pool_preset: 'custom',
      pool_custom_codes: ['000001'],
      pool_industries: [],
      test_start: '2023-07-31',
      test_end: '2023-12-29',
      params: { fast_period: 10, slow_period: 60 },
      mode: 'batch',
      data_access_policy: 'cache_only',
    };
    post.mockResolvedValueOnce({
      data: { data: { experiment_id: 11, job_id: '20' } },
    });

    await expect(createExperiment(request)).resolves.toEqual({
      experiment_id: 11,
      job_id: '20',
    });
    expect(post).toHaveBeenCalledWith('/api/experiments/', request);
  });

  it('requests correlation as a read-only GET with repeated experiment IDs', async () => {
    const report = {
      analysis_role: 'post_hoc_diversification_diagnostic',
      method: 'spearman',
      min_observations: 120,
      return_definition: 'adjacent_persisted_equity_pct_change',
      thresholds: {
        near_duplicate: 0.95,
        high_positive: 0.8,
        negative_diversifier: -0.25,
        low_absolute: 0.2,
      },
      experiments: [],
      matrix: { experiment_ids: [8, 13], values: [], overlap_counts: [] },
      pairs: [],
      warnings: [],
      summary: {
        total_pairs: 0,
        available_pairs: 0,
        unavailable_pairs: 0,
        high_correlation_pairs: 0,
        negative_diversifier_pairs: 0,
      },
    };
    get.mockResolvedValueOnce({ data: { data: report } });

    await expect(getStrategyCorrelation([8, 13], 'spearman', 120)).resolves.toBe(report);
    expect(get).toHaveBeenCalledOnce();
    const [path, config] = get.mock.calls[0] as [string, { params: URLSearchParams }];
    expect(path).toBe('/api/research/strategy-correlation');
    expect(config.params.getAll('experiment_ids')).toEqual(['8', '13']);
    expect(config.params.get('method')).toBe('spearman');
    expect(config.params.get('min_observations')).toBe('120');
  });

  it('sends portfolio weights and tail fraction without write requests', async () => {
    get.mockResolvedValueOnce({ data: { data: { pairs: [] } } });
    await getStrategyCorrelation([8, 13], 'pearson', 60, [0.7, 0.3], 0.1);
    const [, config] = get.mock.calls[0] as [string, { params: URLSearchParams }];
    expect(config.params.getAll('weights')).toEqual(['0.7', '0.3']);
    expect(config.params.get('tail_fraction')).toBe('0.1');
    expect(post).not.toHaveBeenCalled();
  });

  it('requests five review-only portfolio drafts with explicit PIT inputs', async () => {
    const candidates = {
      schema_version: 'portfolio-candidate-set/v1',
      candidate_count: 5,
      candidates: [],
    };
    post.mockResolvedValueOnce({ data: { data: candidates } });

    await expect(
      buildPortfolioCandidates([3, 5, 8], 'spearman', 120, 0.05),
    ).resolves.toBe(candidates);
    expect(post).toHaveBeenCalledWith(
      '/api/research/strategy-correlation/portfolio-candidates',
      {
        experiment_ids: [3, 5, 8],
        method: 'spearman',
        min_observations: 120,
        tail_fraction: 0.05,
      },
    );
  });

  it('uses a safe deterministic research evidence filename', () => {
    expect(
      evidenceDownloadFilename(
        42,
        'json',
        'attachment; filename="../../unsafe.json"',
      ),
    ).toBe('unsafe.json');
    expect(
      evidenceDownloadFilename(
        42,
        'csv',
        'attachment; filename="wrong.exe"',
      ),
    ).toBe('research-evidence-experiment-42.zip');
  });

  it('downloads CSV evidence as a ZIP blob with a finite request timeout', async () => {
    const click = vi.fn();
    const createObjectURL = vi.fn(() => 'blob:evidence');
    const revokeObjectURL = vi.fn();
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL });
    vi.stubGlobal('document', {
      createElement: vi.fn(() => ({
        href: '',
        download: '',
        rel: '',
        click,
      })),
    });
    const evidence = new Blob(['evidence']);
    get.mockResolvedValueOnce({
      data: evidence,
      headers: {
        'content-disposition':
          'attachment; filename="research-evidence-experiment-42.zip"',
      },
    });

    await expect(downloadExperimentEvidence(42, 'csv')).resolves.toBe(
      'research-evidence-experiment-42.zip',
    );
    expect(get).toHaveBeenCalledWith(
      '/api/experiments/42/export',
      {
        params: { format: 'csv' },
        responseType: 'blob',
        timeout: 120_000,
      },
    );
    expect(createObjectURL).toHaveBeenCalledWith(evidence);
    expect(click).toHaveBeenCalledOnce();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:evidence');
  });
});
