import api from './api';
import type { Experiment, ExperimentMetrics, EquityPoint, ModelArtifact, ParameterPreset, Trade } from '../types/experiment';
import type { ApiResponse, PaginatedResponse } from '../types/api';
import type { StrategyMetadata } from '../types/strategy';

export type ExperimentSortKey =
  | 'created_at'
  | 'annual_return'
  | 'sharpe_ratio'
  | 'max_drawdown'
  | 'strategy_id'
  | 'status';

export type ExperimentSortOrder = 'asc' | 'desc';
export type ResearchEvidenceFormat = 'json' | 'csv';

export interface ExperimentFilters {
  status?: string;
  strategy_id?: string;
  strategy_category?: StrategyMetadata['category'];
  is_starred?: boolean;
  search?: string;
  sort_by?: ExperimentSortKey;
  sort_order?: ExperimentSortOrder;
  page?: number;
  limit?: number;
}

export interface ExperimentListResponse extends PaginatedResponse<Experiment> {
  sort_by: ExperimentSortKey;
  sort_order: ExperimentSortOrder;
}

export interface CreateExperimentData {
  name: string;
  strategy_id: string;
  pool_preset: string;
  pool_custom_codes?: string[] | null;
  pool_industries?: string[];
  train_start?: string;
  train_end?: string;
  test_start: string;
  test_end: string;
  params: Record<string, unknown>;
  mode: string;
  data_access_policy?: 'allow_fetch' | 'cache_only';
  research_trust_profile?: 'governed_production_pit' | 'tushare_research_trusted';
  source_experiment_id?: number;
}

export interface CreateParameterPresetData {
  name: string;
  strategy_id: string;
  params: Record<string, unknown>;
  mode: string;
  pool_preset: string;
  pool_custom_codes?: string[] | null;
  pool_industries?: string[];
  source_experiment_id?: number;
  metrics_snapshot?: Record<string, unknown>;
  notes?: string;
  labels?: string[];
  is_default?: boolean;
}

export interface ExperimentPickerParams {
  strategy_id?: string;
  status?: string;
  limit?: number;
}

export interface SweepData {
  strategy_id: string;
  name?: string;
  param_grid: Record<string, unknown[]>;
  pool_preset?: string | null;
  pool_custom_codes?: string;
  pool_industries?: string;
  train_start?: string | null;
  train_end?: string | null;
  selection_start: string;
  selection_end: string;
  locked_test_start: string;
  locked_test_end: string;
  base_params?: Record<string, unknown>;
  mode?: string;
  data_access_policy?: 'allow_fetch' | 'cache_only';
  research_trust_profile?: 'governed_production_pit' | 'tushare_research_trusted';
  source_experiment_id?: number;
}

export interface CreateSweepResponse {
  sweep_id: number;
  total_experiments: number;
  experiment_ids: number[];
  job_ids: string[];
  failed_experiment_ids?: number[];
  selection_window: {
    start: string;
    end: string;
  };
  locked_test_window: {
    start: string;
    end: string;
  };
  research_trust: 'locked_test' | 'legacy_unlocked';
  data_access_policy: 'allow_fetch' | 'cache_only';
}

export async function listExperiments(filters: ExperimentFilters = {}): Promise<ExperimentListResponse> {
  const response = await api.get<ApiResponse<ExperimentListResponse>>('/api/experiments/', { params: filters });
  const data = response.data.data;
  return {
    items: data?.items ?? [],
    total: data?.total ?? 0,
    page: data?.page ?? 1,
    limit: data?.limit ?? 20,
    sort_by: data?.sort_by ?? filters.sort_by ?? 'created_at',
    sort_order: data?.sort_order ?? filters.sort_order ?? 'desc',
  };
}

export async function createExperiment(data: CreateExperimentData): Promise<{ experiment_id: number; job_id: string }> {
  const response = await api.post<ApiResponse<{ experiment_id: number; job_id: string }>>('/api/experiments/', data);
  if (!response.data.data) {
    throw new Error('创建实验失败');
  }
  return response.data.data;
}

export async function getExperiment(id: number): Promise<Experiment> {
  const response = await api.get<ApiResponse<Experiment>>(`/api/experiments/${id}`);
  if (!response.data.data) {
    throw new Error('实验不存在');
  }
  return response.data.data;
}

export function evidenceDownloadFilename(
  id: number,
  format: ResearchEvidenceFormat,
  contentDisposition?: string,
): string {
  const fallback = `research-evidence-experiment-${id}.${format === 'csv' ? 'zip' : 'json'}`;
  const match = contentDisposition?.match(/filename="([^"]+)"/i);
  if (!match?.[1]) return fallback;
  const basename = match[1].split(/[\\/]/).pop() ?? '';
  const safe = basename
    .replace(/[^A-Za-z0-9._-]/g, '_')
    .replace(/^\.+/, '');
  const expectedExtension = format === 'csv' ? '.zip' : '.json';
  return safe && safe.endsWith(expectedExtension) ? safe : fallback;
}

export async function downloadExperimentEvidence(
  id: number,
  format: ResearchEvidenceFormat,
): Promise<string> {
  const response = await api.get<Blob>(
    `/api/experiments/${id}/export`,
    {
      params: { format },
      responseType: 'blob',
      timeout: 120_000,
    },
  );
  const filename = evidenceDownloadFilename(
    id,
    format,
    response.headers['content-disposition'],
  );
  const href = URL.createObjectURL(response.data);
  try {
    const anchor = document.createElement('a');
    anchor.href = href;
    anchor.download = filename;
    anchor.rel = 'noopener';
    anchor.click();
  } finally {
    URL.revokeObjectURL(href);
  }
  return filename;
}

export async function getExperimentMetrics(id: number): Promise<ExperimentMetrics> {
  const response = await api.get<ApiResponse<ExperimentMetrics>>(`/api/experiments/${id}/metrics`);
  if (!response.data.data) {
    throw new Error('无法获取指标');
  }
  return response.data.data;
}

export async function getEquityCurve(id: number, resolution: string = 'daily'): Promise<EquityPoint[]> {
  const response = await api.get<ApiResponse<EquityPoint[]>>(`/api/experiments/${id}/equity`, {
    params: { resolution },
  });
  return response.data.data ?? [];
}

export async function getTradeLog(id: number, page: number = 1, limit: number = 50): Promise<PaginatedResponse<Trade>> {
  const response = await api.get<ApiResponse<PaginatedResponse<Trade>>>(`/api/experiments/${id}/trades`, {
    params: { page, limit },
  });
  return response.data.data ?? { items: [], total: 0, page: 1, limit: 50 };
}

export async function getExperimentModels(id: number): Promise<ModelArtifact[]> {
  const response = await api.get<ApiResponse<ModelArtifact[]>>(`/api/experiments/${id}/models`);
  return response.data.data ?? [];
}

export async function toggleStar(id: number, isStarred: boolean): Promise<void> {
  await api.put(`/api/experiments/${id}/star`, { is_starred: isStarred });
}

export async function setLabels(id: number, labels: string[]): Promise<void> {
  await api.put(`/api/experiments/${id}/labels`, { labels });
}

export async function getExperimentPicker(params: ExperimentPickerParams = {}): Promise<Experiment[]> {
  const response = await api.get<ApiResponse<Experiment[]>>('/api/experiments/picker', { params });
  return response.data.data ?? [];
}

export async function createSweep(data: SweepData): Promise<CreateSweepResponse> {
  const response = await api.post<ApiResponse<CreateSweepResponse>>('/api/experiments/sweep', data);
  if (!response.data.data) {
    throw new Error('创建参数扫描失败');
  }
  return response.data.data;
}

export interface SweepExperimentResult {
  id: number;
  name: string;
  params: Record<string, unknown>;
  status: string;
  repairable?: boolean;
  repair_mode?: 'reset' | 'replace' | null;
  selection_metrics: {
    sharpe_ratio: number | null;
    annual_return: number | null;
    max_drawdown: number | null;
    win_rate: number | null;
  };
}

export interface SweepResultResponse {
  sweep: {
    id: number;
    status: string;
    sweep_config: Record<string, unknown[]>;
    total_experiments: number;
    completed_experiments: number;
    selection_start: string | null;
    selection_end: string | null;
    locked_test_start: string | null;
    locked_test_end: string | null;
    research_trust: 'locked_test' | 'legacy_unlocked';
    data_access_policy: 'allow_fetch' | 'cache_only';
    promoted_experiment_id: number | null;
    promotion_source_experiment_id: number | null;
  };
  experiments: SweepExperimentResult[];
  repairable_experiment_ids?: number[];
}

export async function getSweepResult(sweepId: number): Promise<SweepResultResponse> {
  const response = await api.get<ApiResponse<SweepResultResponse>>(`/api/experiments/sweep/${sweepId}`);
  if (!response.data.data) {
    throw new Error('参数扫描不存在');
  }
  return response.data.data;
}

export interface RepairSweepResponse {
  sweep_id: number;
  repaired_experiment_ids: number[];
  replacement_experiment_ids: Record<string, number>;
  job_ids: string[];
  status: 'running';
}

export async function repairSweep(sweepId: number): Promise<RepairSweepResponse> {
  const response = await api.post<ApiResponse<RepairSweepResponse>>(
    `/api/experiments/sweep/${sweepId}/repair`,
  );
  if (!response.data.data) {
    throw new Error('恢复参数扫描失败');
  }
  return response.data.data;
}

export interface PromoteSweepResponse {
  sweep_id: number;
  source_experiment_id: number;
  experiment_id: number;
  job_id?: string;
  created: boolean;
  research_trust: 'locked_test';
}

export async function promoteSweepExperiment(
  sweepId: number,
  experimentId: number,
): Promise<PromoteSweepResponse> {
  const response = await api.post<ApiResponse<PromoteSweepResponse>>(
    `/api/experiments/sweep/${sweepId}/promote`,
    { experiment_id: experimentId },
  );
  if (!response.data.data) {
    throw new Error('创建锁定最终测试失败');
  }
  return response.data.data;
}

export async function compareExperiments(ids: number[]): Promise<Record<string, unknown>> {
  const response = await api.post<ApiResponse<Record<string, unknown>>>('/api/experiments/compare', { experiment_ids: ids });
  return response.data.data ?? {};
}

export type StrategyCorrelationMethod = 'pearson' | 'spearman';

export interface StrategyCorrelationExperiment {
  id: number;
  name: string;
  strategy_id: string;
  test_start: string | null;
  test_end: string | null;
  quality: {
    equity_observations: number;
    return_observations: number;
    invalid_equity_points: number;
    duplicate_dates: number;
    invalid_returns: number;
    return_start: string | null;
    return_end: string | null;
  };
}

export interface StrategyCorrelationPair {
  left_experiment_id: number;
  right_experiment_id: number;
  correlation: number | null;
  overlap: number;
  overlap_start: string | null;
  overlap_end: string | null;
  interval_mismatch_exclusions: number;
  classification:
    | 'near_duplicate'
    | 'high_positive'
    | 'high_negative'
    | 'negative'
    | 'low'
    | 'moderate'
    | 'unavailable';
  unavailable_reason: 'insufficient_overlap' | 'constant_series' | null;
  tail_correlation?: {
    fraction: number;
    observations: number;
    correlation: number | null;
    unavailable_reason: 'insufficient_tail_overlap' | null;
  };
  holding_overlap?: {
    method: 'daily_code_jaccard';
    observations: number;
    mean: number | null;
    latest: number | null;
    maximum: number | null;
    unavailable_reason: 'trade_inventory_unavailable' | null;
  };
}

export interface StrategyCorrelationWarning {
  level: 'warning' | 'danger';
  code: string;
  experiment_ids: number[];
  message: string;
}

export interface StrategyCorrelationReport {
  analysis_role: 'post_hoc_diversification_diagnostic';
  method: StrategyCorrelationMethod;
  min_observations: number;
  return_definition: 'adjacent_persisted_equity_pct_change';
  thresholds: {
    near_duplicate: number;
    high_positive: number;
    negative_diversifier: number;
    low_absolute: number;
  };
  experiments: StrategyCorrelationExperiment[];
  matrix: {
    experiment_ids: number[];
    values: Array<Array<number | null>>;
    overlap_counts: number[][];
  };
  pairs: StrategyCorrelationPair[];
  portfolio_contribution?: {
    available: boolean;
    unavailable_reason?: 'insufficient_common_overlap';
    common_observations: number;
    common_start?: string;
    common_end?: string;
    annualized_return?: number;
    annualized_volatility?: number;
    tail_fraction?: number;
    tail_cutoff?: number;
    tail_observations?: number;
    contributions?: Array<{
      experiment_id: number;
      weight: number;
      annual_return_contribution: number;
      annual_risk_contribution: number | null;
      risk_contribution_share: number | null;
      tail_return_contribution: number | null;
    }>;
    read_only: true;
  };
  constraint_suggestions?: Array<{
    experiment_id: number;
    suggested_max_weight: number;
    reasons: string[];
    action: 'review_only';
  }>;
  automation?: {
    mutates_portfolio: false;
    message: string;
  };
  warnings: StrategyCorrelationWarning[];
  summary: {
    total_pairs: number;
    available_pairs: number;
    unavailable_pairs: number;
    high_correlation_pairs: number;
    negative_diversifier_pairs: number;
  };
  pit_evidence?: {
    verified: true;
    source_run_manifest_hashes: string[];
  };
}

export interface PortfolioCandidateComponent {
  experiment_id: number;
  strategy_id: string;
  weight: number;
  metrics: {
    annualized_return: number;
    annualized_volatility: number;
    return_to_risk: number;
    tail_mean: number;
    max_drawdown: number;
  };
}

export interface PortfolioCandidate {
  candidate_id: string;
  name: string;
  selection_policy: string;
  strategy_id: 'composite_research_weighted_v1';
  params: {
    component_specs: string;
    static_weights: string;
  };
  components: PortfolioCandidateComponent[];
  risk_constraints: {
    passed: boolean;
    violations: string[];
    holding_evidence_complete: boolean;
  };
  source_manifest: {
    schema_version: 'portfolio-candidate-manifest/v1';
    definition_sha256: string;
    source_digest: string;
    source_run_manifest_hashes: string[];
  };
  publication: {
    status: 'draft';
    automatic: false;
    eligible_for_experiment: boolean;
    message: string;
  };
}

export interface PortfolioCandidateSet {
  schema_version: 'portfolio-candidate-set/v1';
  analysis_role: 'pit_research_candidate_generation';
  source_digest: string;
  common_observations: number;
  common_start: string;
  common_end: string;
  candidate_count: 5;
  candidates: PortfolioCandidate[];
  automation: {
    mutates_strategy_registry: false;
    mutates_portfolio: false;
    submits_experiment: false;
  };
}

export async function getStrategyCorrelation(
  experimentIds: number[],
  method: StrategyCorrelationMethod = 'pearson',
  minObservations: number = 60,
  weights?: number[],
  tailFraction?: number,
): Promise<StrategyCorrelationReport> {
  const params = new URLSearchParams();
  experimentIds.forEach((id) => params.append('experiment_ids', String(id)));
  params.set('method', method);
  params.set('min_observations', String(minObservations));
  weights?.forEach((weight) => params.append('weights', String(weight)));
  if (tailFraction !== undefined) params.set('tail_fraction', String(tailFraction));
  const response = await api.get<ApiResponse<StrategyCorrelationReport>>(
    '/api/research/strategy-correlation',
    { params },
  );
  if (!response.data.data) {
    throw new Error('无法获取策略相关性分析');
  }
  return response.data.data;
}

export async function buildPortfolioCandidates(
  experimentIds: number[],
  method: StrategyCorrelationMethod = 'pearson',
  minObservations: number = 60,
  tailFraction: number = 0.1,
): Promise<PortfolioCandidateSet> {
  const response = await api.post<ApiResponse<PortfolioCandidateSet>>(
    '/api/research/strategy-correlation/portfolio-candidates',
    {
      experiment_ids: experimentIds,
      method,
      min_observations: minObservations,
      tail_fraction: tailFraction,
    },
  );
  if (!response.data.data) throw new Error('无法生成组合候选');
  return response.data.data;
}

export async function listParameterPresets(strategyId?: string): Promise<ParameterPreset[]> {
  const response = await api.get<ApiResponse<PaginatedResponse<ParameterPreset>>>('/api/experiments/parameter-presets', {
    params: { ...(strategyId ? { strategy_id: strategyId } : {}), limit: 100 },
  });
  return response.data.data?.items ?? [];
}

export async function getParameterPreset(id: number): Promise<ParameterPreset> {
  const response = await api.get<ApiResponse<ParameterPreset>>('/api/experiments/parameter-presets/' + id);
  if (!response.data.data) throw new Error('参数方案不存在');
  return response.data.data;
}

export async function createParameterPreset(data: CreateParameterPresetData): Promise<ParameterPreset> {
  const response = await api.post<ApiResponse<ParameterPreset>>('/api/experiments/parameter-presets', data);
  if (!response.data.data) throw new Error('保存参数方案失败');
  return response.data.data;
}

export async function updateParameterPreset(
  id: number,
  data: Partial<CreateParameterPresetData>,
): Promise<ParameterPreset> {
  const response = await api.put<ApiResponse<ParameterPreset>>('/api/experiments/parameter-presets/' + id, data);
  if (!response.data.data) throw new Error('更新参数方案失败');
  return response.data.data;
}

export async function deleteParameterPreset(id: number): Promise<void> {
  await api.delete('/api/experiments/parameter-presets/' + id);
}
