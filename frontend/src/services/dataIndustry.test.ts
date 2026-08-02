import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  fetchIndustryCatalogPayload,
  describeExperimentReadinessBlockers,
  getIndustryCatalog,
  inspectExperimentDataReadiness,
  listIndustries,
  refreshIndustryCatalog,
} from './data';

const getMock = vi.hoisted(() => vi.fn());
const postMock = vi.hoisted(() => vi.fn());

vi.mock('./api', () => ({
  default: {
    get: getMock,
    post: postMock,
  },
}));

const READY_PAYLOAD = {
  schema_version: 'industry-catalog/v2',
  classification: 'eastmoney',
  industries: [{ code: 'BK0477', name: '银行' }],
  count: 1,
  filterable: true,
  source: 'akshare:eastmoney',
  requested_stocks: 300,
  requested_mapped_stocks: 294,
  map_coverage: 0.98,
  coverage_scope: 'requested_codes',
  minimum_coverage: 0.95,
};

describe('pool-scoped industry catalog requests', () => {
  beforeEach(() => {
    getMock.mockReset();
    postMock.mockReset();
  });

  it('passes pool_id when the list is requested for an experiment pool', async () => {
    getMock.mockResolvedValueOnce({ data: { data: READY_PAYLOAD } });

    await listIndustries('csi300');

    expect(getMock).toHaveBeenCalledWith('/api/data/industries', {
      params: { pool_id: 'csi300' },
    });
  });

  it('combines classification and pool_id without dropping either scope', async () => {
    getMock.mockResolvedValueOnce({ data: { data: READY_PAYLOAD } });

    await fetchIndustryCatalogPayload('eastmoney', 'csi500');

    expect(getMock).toHaveBeenCalledWith('/api/data/industries', {
      params: { classification: 'eastmoney', pool_id: 'csi500' },
    });
  });

  it('uses a read-only POST body for an exact custom-code scope', async () => {
    postMock.mockResolvedValueOnce({ data: { data: READY_PAYLOAD } });

    await fetchIndustryCatalogPayload(
      'cninfo_008001',
      undefined,
      ['000001.SZ', '600000.SH'],
    );

    expect(postMock).toHaveBeenCalledWith(
      '/api/data/industries/readiness',
      { codes: ['000001.SZ', '600000.SH'] },
      { params: { classification: 'cninfo_008001' } },
    );
    expect(getMock).not.toHaveBeenCalled();
  });

  it('keeps custom-code refresh explicit and separate from readiness', async () => {
    postMock.mockResolvedValueOnce({ data: { data: READY_PAYLOAD } });

    await refreshIndustryCatalog(
      undefined,
      'cninfo_008001',
      ['000001', '600000'],
    );

    expect(postMock).toHaveBeenCalledWith(
      '/api/data/industries/refresh',
      { codes: ['000001', '600000'] },
      { params: { classification: 'cninfo_008001' } },
    );
  });

  it('keeps an unscoped global catalog fail-closed', async () => {
    getMock.mockResolvedValueOnce({
      data: {
        data: {
          ...READY_PAYLOAD,
          filterable: false,
          reason: 'coverage_not_evaluated',
          requested_stocks: 0,
          requested_mapped_stocks: 0,
          map_coverage: null,
          coverage_scope: 'not_evaluated',
        },
      },
    });

    const catalog = await getIndustryCatalog();

    expect(getMock).toHaveBeenCalledWith('/api/data/industries', { params: {} });
    expect(catalog.status).toBe('unavailable');
    if (catalog.status !== 'unavailable') return;
    expect(catalog.reason).toBe('coverage_not_evaluated');
    expect(catalog.meta.coverageScope).toBe('not_evaluated');
  });
});

describe('cache-only experiment readiness contract', () => {
  beforeEach(() => {
    postMock.mockReset();
  });

  it('sends the exact stock and benchmark window to the read-only endpoint', async () => {
    const request = {
      data_access_policy: 'cache_only' as const,
      price_purpose: 'return_research' as const,
      pool_preset: 'custom',
      pool_custom_codes: ['000001', '000002'],
      test_start: '2025-01-01',
      test_end: '2025-12-31',
    };
    const response = {
      schema_version: 'experiment-readiness/v3' as const,
      ready: true,
      data_access_policy: 'pit_cache_only' as const,
      price_purpose: 'return_research' as const,
      requested_purpose: 'return_research' as const,
      effective_gate: 'ready_for_unbiased_return_research',
      network_accessed: false as const,
      writes_performed: false as const,
      legacy_or_static_fallback_allowed: false as const,
      checks: [],
      blockers: [],
      production_blockers: [],
      research_trust: null,
      evidence: {
        pool_id: 'custom',
        timeline_hash: null,
        canonical_price_binding_id: null,
        canonical_price_binding_digest: null,
        isolated_test_fixture: false,
        evidence_class: 'governed_runtime' as const,
        trust_tier: 'governed_production_pit',
        known_limitations: [],
        eligible_for_research_experiment: true,
        eligible_for_formal_experiment: true,
        eligible_for_real_tuning: true,
        eligible_for_paper_trading: true,
        eligible_for_live_trading: false as const,
        qa_attestation_sha256: null,
      },
      market_data: {
        ready: true,
        pool_id: 'custom',
        cache_key: 'custom_hash',
        schema_version: 4,
        required_start: request.test_start,
        required_end: request.test_end,
        date_start: request.test_start,
        date_end: request.test_end,
        requested_code_count: 2,
        available_code_count: 2,
        missing_codes: [],
        missing_fields: {},
        issues: [],
      },
      benchmark: {
        ready: true,
        index_code: '000300',
        required_start: '2024-12-22',
        required_end: request.test_end,
        date_start: '2024-12-20',
        date_end: request.test_end,
        observations: 260,
        issues: [],
      },
    };
    postMock.mockResolvedValueOnce({ data: { data: response } });

    await expect(inspectExperimentDataReadiness(request)).resolves.toEqual(response);
    expect(postMock).toHaveBeenCalledWith('/api/data/experiment-readiness', request);
  });

  it('renders strict PIT blockers even when legacy cache issues are empty', () => {
    const readiness = {
      schema_version: 'experiment-readiness/v3',
      ready: false,
      blockers: [
        { code: 'canonical_runtime_binding_missing', source: 'market_data.price_ledger' },
        { code: 'purpose_evidence_incomplete:ready_for_real_tuning', source: 'market_data' },
      ],
      market_data: { issues: [] },
      benchmark: { issues: [] },
    } as unknown as Parameters<typeof describeExperimentReadinessBlockers>[0];

    expect(describeExperimentReadinessBlockers(readiness)).toEqual([
      '缺少与 PIT 时间线精确匹配的双价格运行绑定',
      '当前用途所需的严格 PIT 数据证据不完整',
    ]);
  });
});
