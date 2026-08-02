import api from './api';
import type { ApiResponse } from '../types/api';

export interface FactorDefinition {
  factor_id: string;
  version: string;
  definition_digest: string;
  name: string;
  description: string;
  direction: 'high' | 'low';
  lookback: number;
  required_fields: string[];
  category: string;
  parameters: Record<string, unknown>;
  parameter_schema: Record<string, unknown>;
  dependencies: Array<{ factor_id: string; version: string }>;
  supersedes: string | null;
  status: 'published' | 'deprecated';
  deprecated: boolean;
  current: boolean;
  revision: number;
  published_at: string;
  deprecated_at: string | null;
}

export interface FactorCacheCapability {
  pool_id: string;
  label: string;
  ready: boolean;
  disabled_reason: string | null;
  date_start: string | null;
  date_end: string | null;
  n_dates: number;
  n_stocks: number;
  fields: string[];
  available_factor_ids: string[];
  schema_version: number | null;
  source_trust: string;
  source_providers: string[];
  source_evidence_levels: string[];
  ready_for_unbiased_return_research?: boolean;
  ready_for_unbiased_research: boolean;
  neutralization_ready: boolean;
  neutralization?: NeutralizationReadiness;
  point_in_time: PointInTimeReadiness;
}

export type NeutralizationMode = 'none' | 'industry' | 'size' | 'industry+size';

export interface NeutralizationModeReadiness {
  ready: boolean;
  reason: string | null;
}

export interface NeutralizationReadiness {
  schema_version: string;
  modes: Record<NeutralizationMode, NeutralizationModeReadiness>;
  industry: {
    ready: boolean;
    reason: string | null;
    scope_id?: string | null;
    query_semantics: string;
  };
  size: {
    schema_version: string;
    ready: boolean;
    reason: string | null;
    selected_field: 'float_market_cap' | 'market_cap' | null;
    available_fields: string[];
    required_provenance_schema: string;
    evidence?: Record<string, unknown> | null;
  };
}

export interface PointInTimeDomainReadiness {
  ready: boolean;
  reason: string | null;
  scope_id?: string;
  neutralization_ready?: boolean;
  missing_security_code_count?: number;
  missing_price_code_count?: number;
}

export interface PointInTimeReadiness {
  schema_version: string;
  ready: boolean;
  universe: PointInTimeDomainReadiness;
  security_master: PointInTimeDomainReadiness;
  industry: PointInTimeDomainReadiness;
  limitations: string[];
}

export interface FactorResearchReadiness {
  schema_version: string;
  ready: boolean;
  pools: FactorCacheCapability[];
  limits: {
    max_horizons: number;
    max_horizon: number;
    max_window_days: number;
    quantiles: { min: number; max: number };
  };
}

export interface StatisticSummary {
  count: number;
  mean: number | null;
  std: number | null;
  icir: number | null;
  positive_ratio: number | null;
  t_stat: number | null;
}

export interface FactorRunEvidence {
  run_id: string;
  created_at: string;
  request_digest?: string;
  dataset_digest: string;
  result_digest: string;
  run_digest?: string;
  source_job_uuid?: string | null;
  archived_at: string | null;
}

export interface FactorResearchWindow {
  start: string;
  end: string;
}

export interface FactorStabilityConfig {
  mode: 'fixed_three_way';
  train: FactorResearchWindow;
  validation: FactorResearchWindow;
  locked: FactorResearchWindow;
  locked_declared: boolean;
  hypotheses_tested: number;
  correction: 'bonferroni';
  alpha: number;
}

export interface FactorStabilityResult {
  schema_version: string;
  design: {
    mode: 'fixed_three_way';
    pre_registered: true;
    locked_declared_before_run: true;
    parameter_policy: string;
    factor_data_policy: string;
    fit_policy: string;
    forward_return_policy: string;
    aggregation_policy: string;
  };
  windows: Array<{
    role: 'train' | 'validation' | 'locked';
    requested_start: string;
    requested_end: string;
    actual_start: string;
    actual_end: string;
    sessions: number;
    minimum_sessions: number;
    horizons: Record<string, {
      ic: {
        series: Array<{
          date: string;
          sample_count: number;
          pearson_ic: number | null;
          rank_ic: number | null;
        }>;
        summary: {
          pearson_ic: StatisticSummary;
          rank_ic: StatisticSummary;
        };
      };
      multiple_testing: {
        raw_approx_p_value: number | null;
        adjusted_p_value: number | null;
        passes_adjusted_alpha: boolean;
      };
    }>;
    quantile_returns: {
      mean_group_returns: Record<string, number | null>;
      long_short: StatisticSummary;
      monotonicity: number | null;
    };
    decay: {
      points: Array<{
        horizon: number;
        pearson_ic: StatisticSummary;
        rank_ic: StatisticSummary;
      }>;
    };
    coverage: {
      factor_dates: number;
      valid_factor_dates: number;
      evaluable_primary_dates: number;
      minimum_evaluable_primary_dates: number;
      primary_evaluation_ratio: number | null;
    };
  }>;
  stability_summary: {
    primary_horizon: number;
    rank_ic_means: Array<number | null>;
    rank_ic_irs: Array<number | null>;
    long_short_means: Array<number | null>;
    rank_ic_mean_range: number | null;
    rank_ic_sign_consistent: boolean;
    locked_minus_validation_rank_ic: number | null;
    windows_with_evaluable_primary_ic: number;
  };
  multiple_testing: {
    hypotheses_tested: number;
    correction: 'bonferroni';
    alpha: number;
    adjusted_alpha: number;
    p_value_method: string;
    interpretation: string;
  };
  warnings: string[];
}

export interface FactorResearchResult {
  schema_version: string;
  factor: FactorDefinition;
  request: {
    factor_id: string;
    pool_preset: string;
    start: string;
    end: string;
    horizons: number[];
    primary_horizon: number;
    quantiles: number;
    related_factor_ids?: string[];
    rebalance_interval?: number;
    default_cost_bps?: number;
    cost_scenarios_bps?: number[];
    capacity_participation_rates?: number[];
    orthogonalize?: boolean;
    combination_weights?: Record<string, number>;
    stability?: FactorStabilityConfig | null;
    neutralization?: NeutralizationMode;
    industry_scope?: string;
    size_field?: 'auto' | 'float_market_cap' | 'market_cap';
    protocol?: FactorProtocolReference | null;
  };
  dataset: {
    cache_key: string;
    rows: number;
    codes: number;
    date_start: string;
    date_end: string;
    content_sha256: string;
    source_provenance: Record<string, unknown>;
  };
  preprocessing: {
    config: Record<string, unknown>;
    diagnostics: Array<{ status: string }>;
  };
  ic: Record<string, {
    series: Array<{
      date: string;
      sample_count: number;
      pearson_ic: number | null;
      rank_ic: number | null;
    }>;
    summary: {
      pearson_ic: StatisticSummary;
      rank_ic: StatisticSummary;
    };
  }>;
  decay: {
    points: Array<{
      horizon: number;
      pearson_ic: StatisticSummary;
      rank_ic: StatisticSummary;
    }>;
  };
  quantile_returns: {
    mean_group_returns: Record<string, number | null>;
    long_short: StatisticSummary;
    monotonicity: number | null;
  };
  implementation?: {
    schema_version: string;
    status: 'available' | 'insufficient_samples';
    assumptions: {
      rebalance_interval_sessions: number;
      return_horizon_sessions: number | null;
      default_cost_bps: number;
      cost_scenarios_bps: number[];
      cost_convention: string;
      capacity_participation_rates: number[];
      capacity_currency: string;
    };
    coverage: {
      sampled_rebalance_dates: number;
      evaluated_rebalance_dates: number;
      evaluable_observations: number;
      possible_observations: number;
      evaluation_ratio: number | null;
      tradable: {
        status: 'available' | 'partial' | 'unavailable';
        reason: string | null;
        positive_amount_observations: number;
        amount_observations: number;
        ratio: number | null;
      };
    };
    gross: {
      mean_group_returns: Record<string, number | null>;
      long_short: { count: number; mean: number | null; min: number | null; max: number | null };
    };
    net_default: {
      cost_bps: number;
      mean_group_returns: Record<string, number | null>;
      long_short: { count: number; mean: number | null; min: number | null; max: number | null };
    };
    cost_sensitivity: Array<{
      cost_bps: number;
      mean_group_returns: Record<string, number | null>;
      long_short: { count: number; mean: number | null; min: number | null; max: number | null };
    }>;
    turnover: {
      series: Array<{
        date: string;
        group_turnover: Record<string, number | null>;
        long_short_turnover: number | null;
      }>;
      long_short: { count: number; mean: number | null; min: number | null; max: number | null };
      mean_group_turnover: Record<string, number | null>;
    };
    capacity: {
      status: 'available' | 'partial' | 'unavailable';
      reason: string | null;
      amount_field: string | null;
      available_rebalance_dates: number;
      total_rebalance_dates: number;
      scenarios: Record<string, {
        count: number;
        mean: number | null;
        min: number | null;
        max: number | null;
      }>;
    };
  };
  multi_factor?: {
    schema_version: string;
    status: 'available' | 'single_factor';
    input_digest: string;
    correlation: {
      alignment: string;
      pearson: FactorCorrelationMatrix;
      spearman: FactorCorrelationMatrix;
    };
    orthogonalization: {
      enabled: boolean;
      order: string[];
      order_rule: string;
      fit_window: string;
      method: string;
      input_digest: string;
      steps: Array<{
        factor_id: string;
        regressed_on: string[];
        method: string;
        successful_dates: number;
        insufficient_dates: number;
      }>;
    };
    combination: {
      weights: Record<string, number>;
      constraints: {
        lower_bound: number;
        upper_bound: number;
        sum: number;
        shorting: boolean;
      };
      score_digest: string;
      ic: FactorResearchResult['ic'][string];
      quantile_returns: FactorResearchResult['quantile_returns'];
    };
    publication: {
      status: 'not_published';
      automatic_publish: false;
      message: string;
    };
  };
  stability?: FactorStabilityResult | null;
  neutralization?: {
    schema_version: string;
    mode: NeutralizationMode;
    status: 'not_requested' | 'completed';
    fit_window: 'not_applicable' | 'same_trading_date_only';
    inputs: {
      industry: Record<string, unknown> | null;
      size: Record<string, unknown> | null;
    };
    primary_factor: {
      schema_version: string;
      mode: Exclude<NeutralizationMode, 'none'>;
      method: string;
      fit_window: string;
      summary: NeutralizationSummary;
      daily: NeutralizationDailyDiagnostic[];
    } | null;
    factor_summaries: Record<string, NeutralizationSummary>;
  };
  protocol_review?: {
    schema_version: 'factor-research-protocol-review/v1';
    protocol_id: string;
    version: number;
    payload_digest: string;
    question: string;
    hypothesis: string;
    passed: boolean;
    checks: Array<{
      metric: string;
      operator: '>=';
      threshold: number;
      actual: number | null;
      passed: boolean;
    }>;
    export_rules: FactorProtocolPayload['export_rules'];
    read_only: true;
  };
  limitations: string[];
  run?: FactorRunEvidence;
}

export interface NeutralizationExposureSnapshot {
  r_squared: number | null;
  intercept: number | null;
  baseline_industry: string | null;
  industry_coefficients: Record<string, number | null>;
  log_market_cap: number | null;
}

export interface NeutralizationDailyDiagnostic {
  date: string;
  status: 'ok' | 'insufficient_samples' | 'rank_deficient';
  sample_count: number;
  candidate_count: number;
  coverage_ratio: number | null;
  dropped_by_reason: Record<string, number>;
  rank: number | null;
  feature_count: number | null;
  before: NeutralizationExposureSnapshot | null;
  after: NeutralizationExposureSnapshot | null;
}

export interface NeutralizationSummary {
  dates_total: number;
  dates_neutralized: number;
  dates_excluded: number;
  observations_neutralized: number;
  possible_observations: number;
  coverage_ratio: number | null;
  dropped_by_reason: Record<string, number>;
  mean_r_squared_before: number | null;
  mean_r_squared_after: number | null;
}

export interface FactorCorrelationMatrix {
  factors: string[];
  matrix: Array<Array<number | null>>;
  valid_date_counts: number[][];
  method: 'pearson' | 'spearman';
  min_samples: number;
}

export interface FactorResearchRun {
  run_id: string;
  factor_id: string;
  request: FactorResearchResult['request'];
  request_digest: string;
  dataset_digest: string;
  result_digest: string;
  run_digest: string;
  schema_version: string;
  created_at: string;
  source_job_uuid?: string | null;
  archived_at: string | null;
  result?: FactorResearchResult;
  factor_version?: string;
  factor_definition_digest?: string;
  factor_definition?: FactorDefinition;
}

export interface FactorResearchRunPage {
  items: FactorResearchRun[];
  total: number;
  page: number;
  page_size: number;
}

export interface FactorResearchRunFilters {
  include_archived?: boolean;
  factor_id?: string;
  query?: string;
  sort?: 'newest' | 'oldest' | 'factor' | 'horizon';
  page?: number;
  page_size?: number;
}

export interface FactorRunComparison {
  schema_version: string;
  dataset_consistent: boolean;
  runs: Array<{
    run_id: string;
    factor_id: string;
    created_at: string;
    dataset_digest: string;
    primary_horizon: number;
    rank_ic_mean: number | null;
    rank_ic_ir: number | null;
    rank_ic_positive_ratio: number | null;
    long_short_mean: number | null;
    monotonicity: number | null;
  }>;
}

export interface FactorResearchRequest {
  factor_id: string;
  pool_preset: string;
  pool_custom_codes: string[];
  start: string;
  end: string;
  horizons: number[];
  primary_horizon: number;
  quantiles: number;
  winsor_method: 'mad' | 'quantile' | 'none';
  related_factor_ids?: string[];
  rebalance_interval?: number;
  default_cost_bps?: number;
  cost_scenarios_bps?: number[];
  capacity_participation_rates?: number[];
  orthogonalize?: boolean;
  combination_weights?: Record<string, number>;
  stability?: FactorStabilityConfig | null;
  neutralization?: NeutralizationMode;
  industry_scope?: string;
  size_field?: 'auto' | 'float_market_cap' | 'market_cap';
  protocol?: FactorProtocolReference | null;
}

export interface FactorProtocolReference {
  protocol_id: string;
  version: number;
  payload_digest: string;
}

export interface FactorProtocolPayload {
  schema_version: 'factor-research-protocol/v1';
  question: string;
  hypothesis: string;
  factor_ids: string[];
  data: {
    pool_id: string;
    version_policy: 'latest_trusted_at_execution' | 'pinned_dataset_digest';
    expected_dataset_digest?: string | null;
  };
  window: { start: string; end: string };
  implementation: {
    horizons: number[];
    primary_horizon: number;
    quantiles: number;
    rebalance_interval: number;
    default_cost_bps: number;
    cost_scenarios_bps: number[];
    neutralization: NeutralizationMode;
  };
  thresholds: {
    rank_ic_mean_min: number;
    rank_ic_ir_min: number;
    long_short_mean_min: number;
  };
  export_rules: {
    allow_strategy_export: boolean;
    require_all_thresholds: boolean;
    require_dataset_consistency: boolean;
    minimum_evidence_runs: number;
  };
}

export interface FactorProtocolVersion extends FactorProtocolReference {
  status: 'draft' | 'locked';
  payload: FactorProtocolPayload;
  created_at: string;
  locked_at: string | null;
  used_run_count: number;
}

export interface FactorProtocolSeries {
  protocol_id: string;
  name: string;
  current_version: number;
  created_at: string;
  updated_at: string;
  versions: FactorProtocolVersion[];
}

export interface FactorResearchJobSubmission {
  job_id: string;
  status: 'pending';
}

export async function listResearchFactors(): Promise<FactorDefinition[]> {
  const response = await api.get<ApiResponse<FactorDefinition[]>>('/api/factor-research/catalog');
  return response.data.data ?? [];
}

export async function getFactorResearchReadiness(): Promise<FactorResearchReadiness> {
  const response = await api.get<ApiResponse<FactorResearchReadiness>>(
    '/api/factor-research/readiness',
  );
  if (!response.data.data) throw new Error('因子研究就绪信息为空');
  return response.data.data;
}

export async function analyzeResearchFactor(
  body: FactorResearchRequest,
): Promise<FactorResearchResult> {
  const response = await api.post<ApiResponse<FactorResearchResult>>(
    '/api/factor-research/analyze',
    body,
  );
  if (!response.data.data) throw new Error('因子研究响应为空');
  return response.data.data;
}

export async function submitFactorResearchJob(
  body: FactorResearchRequest,
): Promise<FactorResearchJobSubmission> {
  const response = await api.post<ApiResponse<FactorResearchJobSubmission>>(
    '/api/factor-research/jobs',
    body,
  );
  if (!response.data.data) throw new Error('因子研究任务响应为空');
  return response.data.data;
}

export async function listFactorResearchRuns(
  filters: FactorResearchRunFilters = {},
): Promise<FactorResearchRunPage> {
  const response = await api.get<ApiResponse<FactorResearchRunPage>>(
    '/api/factor-research/runs',
    { params: filters },
  );
  return response.data.data ?? {
    items: [],
    total: 0,
    page: filters.page ?? 1,
    page_size: filters.page_size ?? 20,
  };
}

export async function listFactorProtocols(): Promise<FactorProtocolSeries[]> {
  const response = await api.get<ApiResponse<FactorProtocolSeries[]>>(
    '/api/factor-research/protocols',
  );
  return response.data.data ?? [];
}

export async function createFactorProtocol(body: {
  name: string;
  payload: FactorProtocolPayload;
}): Promise<FactorProtocolSeries> {
  const response = await api.post<ApiResponse<FactorProtocolSeries>>(
    '/api/factor-research/protocols',
    body,
  );
  if (!response.data.data) throw new Error('研究协议创建响应为空');
  return response.data.data;
}

export async function createFactorProtocolVersion(body: {
  protocol_id: string;
  expected_current_version: number;
  payload: FactorProtocolPayload;
}): Promise<FactorProtocolSeries> {
  const response = await api.post<ApiResponse<FactorProtocolSeries>>(
    `/api/factor-research/protocols/${encodeURIComponent(body.protocol_id)}/versions`,
    {
      expected_current_version: body.expected_current_version,
      payload: body.payload,
    },
  );
  if (!response.data.data) throw new Error('研究协议版本响应为空');
  return response.data.data;
}

export async function lockFactorProtocol(
  reference: FactorProtocolReference,
): Promise<FactorProtocolVersion> {
  const response = await api.post<ApiResponse<FactorProtocolVersion>>(
    `/api/factor-research/protocols/${encodeURIComponent(reference.protocol_id)}`
      + `/versions/${reference.version}/lock`,
    { payload_digest: reference.payload_digest },
  );
  if (!response.data.data) throw new Error('研究协议锁定响应为空');
  return response.data.data;
}

export async function getFactorResearchRun(runId: string): Promise<FactorResearchRun> {
  const response = await api.get<ApiResponse<FactorResearchRun>>(
    `/api/factor-research/runs/${encodeURIComponent(runId)}`,
  );
  if (!response.data.data) throw new Error('研究运行详情为空');
  return response.data.data;
}

export async function archiveFactorResearchRun(runId: string): Promise<void> {
  await api.delete(`/api/factor-research/runs/${encodeURIComponent(runId)}`);
}

export async function compareFactorResearchRuns(
  runIds: string[],
): Promise<FactorRunComparison> {
  const response = await api.post<ApiResponse<FactorRunComparison>>(
    '/api/factor-research/compare',
    { run_ids: runIds },
  );
  if (!response.data.data) throw new Error('因子比较响应为空');
  return response.data.data;
}

export async function exportFactorStrategy(body: {
  name: string;
  components: Array<{ factor_id: string; weight: number }>;
  top_k_pct: number;
  research_run_ids: string[];
  idempotency_key?: string;
  strategy_id?: string;
  expected_version?: number;
}): Promise<{
  strategy_id: string;
  definition_sha256: string;
  strategy_version: number;
  version: string;
  series_revision: number;
  legacy_unbound: false;
  research_evidence: Array<{
    run_id: string;
    factor_id: string;
    factor_version: string;
    factor_definition_digest: string;
    dataset_digest: string;
    result_digest: string;
  }>;
}> {
  const response = await api.post<ApiResponse<{
    strategy_id: string;
    definition_sha256: string;
    strategy_version: number;
    version: string;
    series_revision: number;
    legacy_unbound: false;
    research_evidence: Array<{
      run_id: string;
      factor_id: string;
      factor_version: string;
      factor_definition_digest: string;
      dataset_digest: string;
      result_digest: string;
    }>;
  }>>('/api/factor-research/export-strategy', body);
  if (!response.data.data) throw new Error('策略导出响应为空');
  return response.data.data;
}

export async function setFactorLifecycle(body: {
  factor_id: string;
  version: string;
  definition_digest: string;
  expected_revision: number;
  status: 'published' | 'deprecated';
  idempotency_key: string;
}): Promise<FactorDefinition> {
  const response = await api.post<ApiResponse<FactorDefinition>>(
    `/api/factor-research/catalog/${encodeURIComponent(body.factor_id)}`
      + `/versions/${encodeURIComponent(body.version)}/${body.status === 'published' ? 'publish' : 'deprecate'}`,
    {
      definition_digest: body.definition_digest,
      expected_revision: body.expected_revision,
      idempotency_key: body.idempotency_key,
    },
  );
  if (!response.data.data) throw new Error('因子目录治理响应为空');
  return response.data.data;
}

export interface FactorStrategyVersionHistory {
  strategy_id: string;
  current_version: number;
  series_revision: number;
  versions: Array<{
    version: number;
    definition_sha256: string;
    created_at: string;
    research_evidence: Array<{
      run_id: string;
      dataset_digest: string;
      result_digest: string;
    }>;
  }>;
}

export async function getFactorStrategyVersions(
  strategyId: string,
): Promise<FactorStrategyVersionHistory> {
  const response = await api.get<ApiResponse<FactorStrategyVersionHistory>>(
    `/api/factor-research/strategies/${encodeURIComponent(strategyId)}/versions`,
  );
  if (!response.data.data) throw new Error('策略版本记录为空');
  return response.data.data;
}

export async function rollbackFactorStrategy(body: {
  strategy_id: string;
  target_version: number;
  expected_version: number;
  idempotency_key: string;
}): Promise<{ strategy_id: string; strategy_version: number }> {
  const response = await api.post<ApiResponse<{
    strategy_id: string;
    strategy_version: number;
  }>>(
    `/api/factor-research/strategies/${encodeURIComponent(body.strategy_id)}/rollback`,
    {
      target_version: body.target_version,
      expected_version: body.expected_version,
      idempotency_key: body.idempotency_key,
    },
  );
  if (!response.data.data) throw new Error('策略回滚响应为空');
  return response.data.data;
}
