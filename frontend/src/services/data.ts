import api from './api';
import type { ApiResponse } from '../types/api';
import { parseIndustryCatalog } from './industryCatalog';
import type { IndustryCatalogState } from './industryCatalog';

export interface PoolInfo {
  id: string;
  name: string;
  description: string;
  count: number;
  index_code: string | null;
  declared_count?: number | null;
  availability?: PoolAvailability;
  lineage?: PoolLineage;
  risk_warnings?: string[];
}

export interface PoolAvailability {
  ready: boolean;
  reason: string | null;
  requested_as_of: string;
  resolved_as_of: string | null;
  resolution: 'exact_activated_observation' | 'weekend_prior_activated_observation' | 'unavailable';
  staleness_calendar_days: number | null;
  network_accessed: false;
  source_batches: unknown[];
}

export interface PoolLineage {
  requested_as_of: string;
  resolved_as_of: string | null;
  resolution?: string;
  staleness_calendar_days?: number | null;
  source_as_of: string | null;
  point_in_time: boolean;
  snapshot_hash: string | null;
}

export interface PoolStocksResponse {
  pool_id: string;
  stocks: string[];
  count: number;
  availability: PoolAvailability;
  lineage: PoolLineage;
  quality: { ready: boolean; reason: string | null };
  risk_warnings: string[];
}

export interface PoolCacheInfo {
  pool_id: string;
  exists: boolean;
  date_start: string | null;
  date_end: string | null;
  n_dates: number;
  n_stocks: number;
  file_size_mb: number;
  last_updated: string | null;
  error?: string;
}

export interface DataUpdateStatus {
  broker_status: {
    job_uuid?: string;
    status?: string;
    progress?: number;
    error?: string | null;
    created_at?: string;
    completed_at?: string | null;
  };
  governance_refresh_status?: {
    job_uuid?: string;
    status?: string;
    progress?: number;
    error?: string | null;
    created_at?: string;
    completed_at?: string | null;
  };
  research_refresh_status?: {
    job_uuid?: string;
    status?: string;
    progress?: number;
    error?: string | null;
    created_at?: string;
    completed_at?: string | null;
    result?: {
      status?: string;
      import_warning?: string | null;
      continuation_required?: boolean;
      continuation_scheduled?: boolean;
      collection?: {
        run_id?: string;
        completed_tasks: number;
        planned_tasks: number;
        pending_tasks: number;
        calls_this_invocation?: number;
        completed_this_invocation?: number;
        reconciled_session_count?: number;
        complete?: boolean;
        failures?: Record<string, {
          diagnostic?: { code?: string; retryable?: boolean };
          task?: { dataset?: string; params?: Record<string, unknown> };
        }> | Array<{
          diagnostic?: { code?: string; retryable?: boolean };
          task?: { dataset?: string; params?: Record<string, unknown> };
        }>;
        optional_failures?: Array<{
          diagnostic?: { code?: string; retryable?: boolean };
          task?: { dataset?: string; params?: Record<string, unknown> };
        }>;
      };
    } | null;
  };
  market_data_update_contract?: {
    scope?: string;
    available?: boolean;
    reason?: string;
    requires?: string[];
  };
  research_data_contract?: {
    available: boolean;
    classification: 'vendor_research_trusted' | 'single_source_research';
    research_trust_profile: 'tushare_research_trusted' | 'single_source_research_warning_only';
    allowed_uses: string[];
    risk_policy: 'warning_only';
    live_eligible: false;
    generation_id?: string;
    date_start?: string;
    date_end?: string;
    warnings?: string[];
    market?: { available: boolean; date_start: string | null; date_end: string | null; row_count: number };
  };
  research_pools?: Array<{
    pool_id: string;
    available: boolean;
    record_count: number;
    requested_as_of: string;
    resolved_month: string | null;
    generation_id: string | null;
    classification: 'vendor_research_trusted' | 'single_source_research';
    warnings: string[];
    live_eligible: false;
  }>;
  pools_cache: PoolCacheInfo[];
}

export interface ResearchDataSource {
  source_id: 'tushare' | 'baostock' | 'activated_local';
  display_name: string;
  installed: boolean;
  configured: boolean;
  available: boolean;
  refreshable: boolean;
  classification: 'vendor_research_trusted' | 'single_source_research' | 'cross_check_only' | 'local_runtime';
  capabilities: string[];
  last_observation: string | null;
  row_count: number;
  generation_id: string | null;
  warnings: string[];
  datasets: Array<{
    dataset: string;
    status: string;
    record_count: number;
  }>;
  live_eligible: false;
}

export interface ResearchDataSourcesReport {
  schema_version: 'research-data-sources/v1';
  mode: 'research_and_paper_warning_only';
  live_trading_policy: 'hard_locked';
  sources: ResearchDataSource[];
}

export interface ResearchDataConflict {
  left_source: string;
  right_source: string;
  pool_id: string;
  as_of: string;
  status: 'match' | 'match_not_independent' | 'conflict' | 'right_source_unavailable';
  left_count: number;
  right_count: number;
  only_left_count?: number;
  only_right_count?: number;
  only_left_sample?: string[];
  only_right_sample?: string[];
  independent?: boolean;
  lineage_status?: string;
  weight_conflict_count?: number;
  weight_conflict_sample?: Array<{
    security_code: string;
    field: 'weight';
    left_value: number;
    right_value: number;
    absolute_delta: number;
    tolerance: number;
  }>;
}

export interface ResearchDataConflictReport {
  schema_version: 'research-data-conflicts/v1';
  status: string;
  comparisons: ResearchDataConflict[];
  conflicts: ResearchDataConflict[];
  conflict_count: number;
  cross_validated?: boolean;
  uncompared?: Array<{
    left_source: string;
    right_source: string;
    pool_id?: string;
    as_of?: string;
    reason: string;
    fields?: string[];
  }>;
  interpretation?: string;
}

export interface IndustryInfo {
  code: string;
  name: string;
}

export interface ExperimentDataReadinessRequest {
  data_access_policy: 'cache_only';
  research_trust_profile?: 'governed_production_pit' | 'tushare_research_trusted';
  price_purpose?: 'compatibility_research' | 'return_research' | 'real_tuning' | 'execution_simulation';
  pool_preset: string;
  pool_custom_codes: string[];
  train_start?: string;
  test_start: string;
  test_end: string;
}

export interface CacheReadinessSummary {
  ready: boolean;
  required_start: string;
  required_end: string;
  date_start: string | null;
  date_end: string | null;
  issues: string[];
}

export interface ExperimentDataReadiness {
  schema_version: 'experiment-readiness/v3';
  ready: boolean;
  data_access_policy: 'pit_cache_only';
  price_purpose: 'compatibility_research' | 'return_research' | 'real_tuning' | 'execution_simulation';
  requested_purpose: 'compatibility_research' | 'return_research' | 'real_tuning' | 'execution_simulation';
  effective_gate: string;
  network_accessed: false;
  writes_performed: false;
  legacy_or_static_fallback_allowed: false;
  checks: Array<{ code: string; passed: boolean; source: string }>;
  blockers: Array<{ code: string; source: string }>;
  production_blockers: Array<{ code: string; source: string }>;
  research_trust: {
    profile: 'tushare_research_trusted';
    trust_tier: 'conditional_personal_research';
    eligible: boolean;
    blockers: string[];
    warnings: string[];
    warning_severity: 'none' | 'high';
    known_limitations: string[];
  } | null;
  evidence: {
    pool_id: string | null;
    timeline_hash: string | null;
    canonical_price_binding_id: string | null;
    canonical_price_binding_digest: string | null;
    isolated_test_fixture: boolean;
    evidence_class: 'isolated_test_fixture' | 'governed_runtime' | 'tushare_research_trusted' | 'incomplete';
    trust_tier: string;
    known_limitations: string[];
    eligible_for_research_experiment: boolean;
    eligible_for_formal_experiment: boolean;
    eligible_for_real_tuning: boolean;
    eligible_for_paper_trading: boolean;
    eligible_for_live_trading: false;
    qa_attestation_sha256: string | null;
  };
  market_data: CacheReadinessSummary & {
    pool_id: string;
    cache_key: string;
    schema_version: number | null;
    requested_code_count: number;
    available_code_count: number;
    missing_codes: string[];
    missing_fields: Record<string, string[]>;
  };
  benchmark: CacheReadinessSummary & {
    index_code: string;
    observations: number;
  };
}

const READINESS_REASON_LABELS: Record<string, string> = {
  effective_dated_history_missing: '缺少覆盖所选窗口的 PIT 历史成分时间线',
  point_in_time_universe_missing: '缺少已激活的 PIT 股票池证据',
  canonical_runtime_binding_missing: '缺少与 PIT 时间线精确匹配的双价格运行绑定',
  ledger_unavailable: '生产双价格账本尚不可用',
  pit_trading_calendar_binding_missing: '缺少权威交易日历绑定',
  pit_benchmark_binding_missing: '缺少点时基准绑定',
  daily_cache_missing: '所选股票池的本地行情缓存不存在',
  daily_cache_start_not_covered: '行情数据未覆盖实验所需起始日期',
  daily_cache_end_not_covered: '行情数据未覆盖实验所需结束日期',
  benchmark_cache_missing: '本地基准行情不存在',
  benchmark_start_not_covered: '基准行情未覆盖实验所需起始日期',
  benchmark_end_not_covered: '基准行情未覆盖实验所需结束日期',
  market_data_not_ready: '行情数据未通过完整性检查',
  benchmark_data_not_ready: '基准数据未通过完整性检查',
  four_index_monthly_manifest_coverage_complete: 'Tushare 四指数 2016-01 至 2026-06 月度历史成分证据尚未完整',
  window_within_declared_coverage: '研究窗口超出 Tushare 条件信任范围（最晚 2026-06-30）',
  research_purpose_only: 'Tushare 条件信任只允许探索性研究，不允许调优或模拟执行',
  candidate_collection_valid: 'Tushare 候选采集尚未完成全部一致性检查',
  runtime_cache_not_exclusively_tushare: '所选窗口尚未绑定仅来自 Tushare 的本地研究行情；不会静默混用其他来源',
};

/** Convert v3 blockers (or a legacy response) into non-empty browser diagnostics. */
export function describeExperimentReadinessBlockers(
  readiness: ExperimentDataReadiness,
): string[] {
  const codes = readiness.blockers?.map((item) => item.code) ?? [
    ...(readiness.market_data?.issues ?? []),
    ...(readiness.benchmark?.issues ?? []),
  ];
  const labels = codes.map((code) => {
    if (code.startsWith('purpose_evidence_incomplete:')) {
      return '当前用途所需的严格 PIT 数据证据不完整';
    }
    return READINESS_REASON_LABELS[code] ?? code;
  });
  const unique = [...new Set(labels.filter(Boolean))];
  if (!readiness.ready && unique.length === 0) {
    return ['PIT 实验就绪检查未通过，服务未返回具体阻断原因'];
  }
  return unique;
}

/**
 * Backward-compatible industry list payload. The backend may additionally
 * return industry-catalog/v2 fields (schema_version, filterable, reason,
 * mapped_stocks, map_coverage, minimum_coverage); they are optional so old
 * responses keep parsing.
 */
export interface IndustryList {
  classification: string;
  industries: IndustryInfo[];
  count?: number;
  source?: string | null;
  schema_version?: string;
  filterable?: boolean;
  reason?: string | null;
  mapped_stocks?: number;
  requested_stocks?: number;
  requested_mapped_stocks?: number;
  invalid_requested_codes?: string[];
  map_coverage?: number;
  coverage_scope?: string;
  minimum_coverage?: number;
}

export async function listPools(): Promise<PoolInfo[]> {
  const response = await api.get<ApiResponse<PoolInfo[]>>('/api/data/pools');
  return response.data.data ?? [];
}

export async function getPoolStocks(poolId: string, industry?: string): Promise<PoolStocksResponse> {
  const response = await api.get<ApiResponse<PoolStocksResponse>>(
    `/api/data/pools/${poolId}/stocks`,
    { params: industry ? { industry } : {} },
  );
  return response.data.data ?? {
    pool_id: poolId,
    stocks: [],
    count: 0,
    availability: {
      ready: false,
      reason: 'point_in_time_response_missing',
      requested_as_of: '',
      resolved_as_of: null,
      resolution: 'unavailable',
      staleness_calendar_days: null,
      network_accessed: false,
      source_batches: [],
    },
    lineage: {
      requested_as_of: '',
      resolved_as_of: null,
      source_as_of: null,
      point_in_time: false,
      snapshot_hash: null,
    },
    quality: { ready: false, reason: 'point_in_time_response_missing' },
    risk_warnings: ['point_in_time_response_missing'],
  };
}

export async function listIndustries(poolId?: string): Promise<IndustryList> {
  const response = await api.get<ApiResponse<IndustryList>>('/api/data/industries', {
    params: poolId ? { pool_id: poolId } : {},
  });
  return response.data.data ?? { classification: 'unknown', industries: [] };
}

/**
 * Fetch the raw industry payload (v1 or v2) without discarding provenance
 * fields. Throws on transport/HTTP errors so callers can offer retry.
 */
export async function fetchIndustryCatalogPayload(
  classification?: string,
  poolId?: string,
  codes?: string[],
): Promise<unknown> {
  if (codes && codes.length > 0) {
    const response = await api.post<ApiResponse<unknown>>(
      '/api/data/industries/readiness',
      { codes },
      {
        params: classification ? { classification } : {},
      },
    );
    return response.data.data;
  }
  const response = await api.get<ApiResponse<unknown>>('/api/data/industries', {
    params: {
      ...(classification ? { classification } : {}),
      ...(poolId ? { pool_id: poolId } : {}),
    },
  });
  return response.data.data;
}

/**
 * Fetch and validate the industry catalog. The result is fail-closed:
 * unreadable names (e.g. BK codes) are never exposed as selectable
 * industries, and any structural problem degrades to `unavailable`.
 */
export async function getIndustryCatalog(
  classification?: string,
  poolId?: string,
  codes?: string[],
): Promise<IndustryCatalogState> {
  const payload = await fetchIndustryCatalogPayload(classification, poolId, codes);
  return parseIndustryCatalog(payload);
}

export async function refreshIndustryCatalog(
  poolId?: string,
  classification?: string,
  codes?: string[],
): Promise<void> {
  await api.post('/api/data/industries/refresh', codes?.length ? { codes } : undefined, {
    params: {
      ...(poolId ? { pool_id: poolId } : {}),
      ...(classification ? { classification } : {}),
    },
  });
}

export async function getDataUpdateStatus(): Promise<DataUpdateStatus> {
  const response = await api.get<ApiResponse<DataUpdateStatus>>('/api/data/update/status');
  return response.data.data ?? { broker_status: { status: 'unknown' }, pools_cache: [] };
}

export async function getResearchDataSources(): Promise<ResearchDataSourcesReport> {
  const response = await api.get<ApiResponse<ResearchDataSourcesReport>>(
    '/api/data/research-sources',
  );
  if (!response.data.data) throw new Error('研究数据源状态加载失败');
  return response.data.data;
}

export async function getResearchDataConflicts(): Promise<ResearchDataConflictReport> {
  const response = await api.get<ApiResponse<ResearchDataConflictReport>>(
    '/api/data/research-sources/conflicts',
  );
  if (!response.data.data) throw new Error('研究数据源冲突报告加载失败');
  return response.data.data;
}

export async function triggerResearchDataRefresh(request: {
  source_id: ResearchDataSource['source_id'];
  from_month: string;
  to_month?: string;
  max_calls: number;
}): Promise<{ job_id: string; message: string; mode: string }> {
  const response = await api.post<ApiResponse<{
    job_id: string;
    message: string;
    mode: string;
  }>>('/api/data/research-sources/refresh', request);
  if (!response.data.data) throw new Error('研究数据刷新提交失败');
  return response.data.data;
}

export async function inspectExperimentDataReadiness(
  request: ExperimentDataReadinessRequest,
): Promise<ExperimentDataReadiness> {
  const response = await api.post<ApiResponse<ExperimentDataReadiness>>(
    '/api/data/experiment-readiness',
    request,
  );
  if (!response.data.data) throw new Error('本地数据就绪检查失败');
  return response.data.data;
}

export async function triggerDataUpdate(poolId?: string): Promise<{
  job_id?: string;
  message: string;
  mode: string;
}> {
  const response = await api.post<ApiResponse<{
    job_id?: string;
    message: string;
    mode: string;
  }>>('/api/data/update', undefined, { params: poolId ? { pool_id: poolId } : {} });
  if (!response.data.data) throw new Error('提交数据更新失败');
  return response.data.data;
}

/** Refresh quarantined constituent evidence only; never price/cache data. */
export async function triggerPitGovernanceRefresh(poolId?: string): Promise<{
  job_id?: string;
  message: string;
  mode: string;
}> {
  const response = await api.post<ApiResponse<{
    job_id?: string;
    message: string;
    mode: string;
  }>>('/api/data/pit-governance/refresh', undefined, { params: poolId ? { pool_id: poolId } : {} });
  if (!response.data.data) throw new Error('提交 PIT 治理证据刷新失败');
  return response.data.data;
}

export async function invalidatePoolCache(poolId: string): Promise<void> {
  await api.post('/api/data/cache/invalidate', undefined, { params: { pool_id: poolId } });
}
