import api from './api';
import { normalizeDeploymentSubmissionError } from './apiError';
import type {
  Deployment,
  ModelLifecycle,
  Order,
  Portfolio,
  PortfolioAllocation,
  PortfolioNavPoint,
  Position,
  Signal,
  StrategyAnalytics,
  StrategyAnalyticsItem,
  StrategyAnalyticsMetrics,
  StrategyAnalyticsPoint,
} from '../types/trading';
import type { ApiResponse, PaginatedResponse } from '../types/api';

export interface DeploymentFilters {
  status?: string;
  strategy_id?: string;
}

export interface CreateDeploymentData {
  strategy_id: string;
  display_name: string;
  params: Record<string, unknown>;
  mode: string;
  source_experiment_id?: number;
  research_promotion_id?: number;
  portfolio_id?: number;
  target_weight_bps?: number;
}

export interface CreatePortfolioData {
  name: string;
  total_capital: number;
  rebalance_frequency: string;
  allocations: PortfolioAllocation[];
}

export function toAllocationPayload(
  allocation: PortfolioAllocation,
): PortfolioAllocation {
  return {
    deployment_id: allocation.deployment_id,
    target_weight_bps: allocation.target_weight_bps,
    min_weight_bps: allocation.min_weight_bps,
    max_weight_bps: allocation.max_weight_bps,
    locked: Boolean(allocation.locked),
    risk_budget_bps: allocation.risk_budget_bps ?? null,
  };
}

export interface AllocationValidation {
  valid: boolean;
  errors: string[];
  warnings: string[];
  strategy_weight_bps: number;
  cash_weight_bps: number;
  total_weight_bps: number;
  cash_capital: number;
}

export interface RebalancePreviewRow extends PortfolioAllocation {
  current_capital: number;
  target_capital: number;
  capital_delta: number;
  direction: 'BUY' | 'SELL' | 'HOLD';
  estimated_cost: number;
}

export interface RebalancePreview {
  validation: AllocationValidation;
  rows: RebalancePreviewRow[];
  one_way_turnover: number;
  turnover_rate: number;
  estimated_cost: number;
}

export interface SimulationStatus {
  status: 'not_started' | 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  progress?: number;
  error?: string | null;
  result?: Record<string, unknown>;
  created_at?: string;
  completed_at?: string | null;
}

export interface SimulationRun {
  id: string;
  portfolio_id?: number | null;
  trade_date: string;
  status: SimulationStatus['status'];
  summary: Record<string, unknown>;
  error?: string | null;
  created_at: string;
  completed_at?: string | null;
}

export interface SimulationSchedule {
  enabled: boolean;
  run_time: string;
  timezone: string;
  refresh_data?: boolean;
  scope: string;
}

export interface SimulationCalendar {
  pool_id: string;
  pool_ids?: string[];
  generation_ids?: string[];
  min_date: string;
  max_date: string;
  suggested_start: string;
  trading_days: number;
  trust_tier?: string;
  warnings?: string[];
  warning_severity?: string;
  live_eligible?: boolean;
}

export interface PortfolioStrategyOverview {
  deployment_id: number;
  display_name: string;
  strategy_id: string;
  status: string;
  source_experiment_id?: number | null;
  params: Record<string, unknown>;
  target_weight_bps: number;
  target_capital: number;
  current_market_value: number;
  unrealized_pnl: number;
  actual_weight_pct: number;
  position_count: number;
  filled_orders: number;
}

export interface PortfolioOverview {
  portfolio_id: number;
  name: string;
  status: string;
  current_revision: number;
  rebalance_frequency: string;
  start_date: string | null;
  latest_date: string | null;
  trading_days: number;
  initial_capital: number;
  current_equity: number;
  cash_balance: number;
  daily_pnl: number;
  daily_return: number;
  cumulative_return: number;
  max_drawdown: number;
  sharpe_ratio: number | null;
  strategies: PortfolioStrategyOverview[];
  recent_orders: Order[];
  positions: Position[];
}

export async function listDeployments(filters: DeploymentFilters = {}): Promise<Deployment[]> {
  const response = await api.get<ApiResponse<Deployment[]>>('/api/trading/deployments', { params: filters });
  return response.data.data ?? [];
}

export interface CreateDeploymentResult {
  deployment_id: number;
  portfolio_id?: number | null;
  revision?: number | null;
  research_risk_snapshot?: Deployment['research_risk_snapshot'];
  research_risk_snapshot_hash?: string | null;
}

export async function createDeployment(data: CreateDeploymentData): Promise<CreateDeploymentResult> {
  try {
    const response = await api.post<ApiResponse<CreateDeploymentResult>>('/api/trading/deployments', data);
    if (!response.data.data) {
      throw new Error('服务未返回部署记录');
    }
    return response.data.data;
  } catch (error: unknown) {
    throw normalizeDeploymentSubmissionError(error);
  }
}

export async function updateDeployment(
  deploymentId: number,
  data: {
    status?: 'active' | 'paused' | 'stopped';
    display_name?: string;
    user_notes?: string;
    research_promotion_id?: number;
  },
): Promise<{ updated: boolean; deployment_id: number }> {
  const response = await api.put<ApiResponse<{ updated: boolean; deployment_id: number }>>(
    `/api/trading/deployments/${deploymentId}`,
    data,
  );
  if (!response.data.data) throw new Error('更新部署失败');
  return response.data.data;
}

export async function getModelLifecycle(deploymentId: number): Promise<ModelLifecycle> {
  const response = await api.get<ApiResponse<ModelLifecycle>>(
    `/api/trading/deployments/${deploymentId}/model-lifecycle`,
  );
  if (!response.data.data) throw new Error('无法加载模型生命周期');
  return response.data.data;
}

export async function triggerModelRetrain(
  deploymentId: number,
): Promise<{ deployment_id: number; job_id: string }> {
  const response = await api.put<ApiResponse<{ deployment_id: number; job_id: string }>>(
    `/api/trading/deployments/${deploymentId}/retrain`,
  );
  if (!response.data.data) throw new Error('提交重训练失败');
  return response.data.data;
}

export async function getPositions(portfolioId?: number): Promise<Position[]> {
  const params = portfolioId ? { portfolio_id: portfolioId } : {};
  const response = await api.get<ApiResponse<Position[]>>('/api/trading/positions', { params });
  return response.data.data ?? [];
}

export async function getSignals(deploymentId?: number, date?: string): Promise<Signal[]> {
  const params: Record<string, unknown> = {};
  if (deploymentId) params.deployment_id = deploymentId;
  if (date) params.date = date;
  const response = await api.get<ApiResponse<Signal[]>>('/api/trading/signals', { params });
  return response.data.data ?? [];
}

export async function getOrders(
  deploymentId?: number,
  page: number = 1,
  limit: number = 50
): Promise<PaginatedResponse<Order>> {
  const params: Record<string, unknown> = { page, limit };
  if (deploymentId) params.deployment_id = deploymentId;
  const response = await api.get<ApiResponse<PaginatedResponse<Order>>>('/api/trading/orders', { params });
  return response.data.data ?? { items: [], total: 0, page: 1, limit: 50 };
}

export async function listPortfolios(): Promise<Portfolio[]> {
  const response = await api.get<ApiResponse<Portfolio[]>>('/api/trading/portfolios');
  return response.data.data ?? [];
}

export async function createPortfolio(data: CreatePortfolioData): Promise<{ portfolio_id: number }> {
  const response = await api.post<ApiResponse<{ portfolio_id: number }>>(
    '/api/trading/portfolios',
    { ...data, allocations: data.allocations.map(toAllocationPayload) },
  );
  if (!response.data.data) {
    throw new Error('创建组合失败');
  }
  return response.data.data;
}

export async function validateAllocations(
  portfolioId: number,
  allocations: PortfolioAllocation[],
): Promise<{ allocations: PortfolioAllocation[]; validation: AllocationValidation }> {
  const response = await api.post<ApiResponse<{ allocations: PortfolioAllocation[]; validation: AllocationValidation }>>(
    `/api/trading/portfolios/${portfolioId}/validate`,
    { allocations: allocations.map(toAllocationPayload) },
  );
  if (!response.data.data) throw new Error('组合校验失败');
  return response.data.data;
}

export async function previewRebalance(
  portfolioId: number,
  allocations: PortfolioAllocation[],
): Promise<RebalancePreview> {
  const response = await api.post<ApiResponse<RebalancePreview>>(
    `/api/trading/portfolios/${portfolioId}/preview`,
    { allocations: allocations.map(toAllocationPayload) },
  );
  if (!response.data.data) throw new Error('调仓预览失败');
  return response.data.data;
}

export async function createPortfolioDraft(
  portfolioId: number,
  allocations: PortfolioAllocation[],
  effectiveDate?: string,
): Promise<{ portfolio_id: number; revision: number; validation: AllocationValidation }> {
  const response = await api.post<ApiResponse<{ portfolio_id: number; revision: number; validation: AllocationValidation }>>(
    `/api/trading/portfolios/${portfolioId}/drafts`,
    {
      allocations: allocations.map(toAllocationPayload),
      effective_date: effectiveDate,
    },
  );
  if (!response.data.data) throw new Error('保存草稿失败');
  return response.data.data;
}

export async function publishPortfolioDraft(
  portfolioId: number,
  revision: number,
): Promise<{ portfolio_id: number; revision: number; published: boolean }> {
  const response = await api.post<ApiResponse<{ portfolio_id: number; revision: number; published: boolean }>>(
    `/api/trading/portfolios/${portfolioId}/drafts/${revision}/publish`,
  );
  if (!response.data.data) throw new Error('发布组合失败');
  return response.data.data;
}

export async function getPortfolioNav(portfolioId: number): Promise<PortfolioNavPoint[]> {
  const response = await api.get<ApiResponse<PortfolioNavPoint[]>>(
    `/api/trading/portfolios/${portfolioId}/nav`,
  );
  return response.data.data ?? [];
}

export async function getPortfolioOverview(portfolioId: number): Promise<PortfolioOverview> {
  const response = await api.get<ApiResponse<PortfolioOverview>>(`/api/trading/portfolios/${portfolioId}/overview`);
  if (!response.data.data) throw new Error('无法加载模拟盘总览');
  return response.data.data;
}

type UnknownRecord = Record<string, unknown>;

const nullableNumber = (value: unknown): number | null => (
  typeof value === 'number' && Number.isFinite(value) ? value : null
);

const recordValue = (value: unknown): UnknownRecord => (
  value && typeof value === 'object' ? value as UnknownRecord : {}
);

const analyticsMetricKeys: Array<keyof StrategyAnalyticsMetrics> = [
  'cumulative_return', 'annualized_return', 'annualized_volatility',
  'sharpe_ratio', 'sortino_ratio', 'calmar_ratio', 'max_drawdown',
  'positive_day_ratio', 'win_rate', 'profit_loss_ratio', 'profit_factor',
  'turnover_rate', 'transaction_cost', 'capital_utilization',
  'target_weight_deviation', 'contribution_pnl', 'contribution_return',
  'risk_contribution',
];

const normalizeStrategyPoint = (date: string, value: unknown): StrategyAnalyticsPoint => {
  const point = recordValue(value);
  return {
    date: String(point.date ?? date),
    nav: nullableNumber(point.nav),
    equity: nullableNumber(point.equity ?? point.total_equity),
    daily_pnl: nullableNumber(point.daily_pnl),
    daily_return: nullableNumber(point.daily_return),
    cumulative_return: nullableNumber(point.cumulative_return),
    drawdown: nullableNumber(point.drawdown),
    contribution_pnl: nullableNumber(point.contribution_pnl),
    contribution_return: nullableNumber(point.contribution_return),
    target_weight_pct: nullableNumber(point.target_weight_pct),
    actual_weight_pct: nullableNumber(point.actual_weight_pct),
  };
};

export function normalizeStrategyAnalytics(payload: unknown, portfolioId: number): StrategyAnalytics {
  const root = recordValue(payload);
  const rawStrategies = Array.isArray(root.strategies) ? root.strategies.map(recordValue) : [];
  const rawSeries = Array.isArray(root.series) ? root.series.map(recordValue) : [];
  const dateRange = recordValue(root.date_range);

  const strategies: StrategyAnalyticsItem[] = rawStrategies.map((strategy) => {
    const deploymentId = Number(strategy.deployment_id ?? strategy.id);
    const metricsSource = { ...recordValue(strategy.metrics), ...strategy };
    const metrics = Object.fromEntries(
      analyticsMetricKeys.map((key) => [key, nullableNumber(metricsSource[key])]),
    ) as unknown as StrategyAnalyticsMetrics;
    const nestedSeries = Array.isArray(strategy.series)
      ? strategy.series.map((point) => normalizeStrategyPoint('', point))
      : rawSeries.flatMap((day) => {
        const dayStrategies = Array.isArray(day.strategies) ? day.strategies : [];
        const matching = dayStrategies.map(recordValue).find(
          (point) => Number(point.deployment_id ?? point.id) === deploymentId,
        );
        return matching ? [normalizeStrategyPoint(String(day.date ?? ''), matching)] : [];
      });
    return {
      deployment_id: deploymentId,
      display_name: String(strategy.display_name ?? strategy.name ?? `部署 #${deploymentId}`),
      strategy_id: String(strategy.strategy_id ?? ''),
      status: String(strategy.status ?? 'active'),
      source_experiment_id: nullableNumber(strategy.source_experiment_id),
      params: recordValue(strategy.params),
      data_points: Number(strategy.data_points ?? nestedSeries.length),
      ...metrics,
      series: nestedSeries,
    };
  });

  const portfolioSeries = rawSeries.map((point) => {
    const portfolio = { ...point, ...recordValue(point.portfolio) };
    return {
      date: String(point.date ?? portfolio.date ?? ''),
      nav: nullableNumber(
        portfolio.nav
        ?? portfolio.total_equity
        ?? portfolio.portfolio_total_equity
      ),
      daily_return: nullableNumber(
        portfolio.daily_return
        ?? portfolio.portfolio_daily_return
      ),
      cumulative_return: nullableNumber(portfolio.cumulative_return),
      drawdown: nullableNumber(portfolio.drawdown),
    };
  });

  return {
    portfolio_id: Number(root.portfolio_id ?? portfolioId),
    start_date: String(
      root.start_date
      ?? dateRange.start_date
      ?? dateRange.start
      ?? portfolioSeries[0]?.date
      ?? ''
    ) || null,
    end_date: String(
      root.end_date
      ?? dateRange.end_date
      ?? dateRange.end
      ?? portfolioSeries.at(-1)?.date
      ?? ''
    ) || null,
    portfolio_series: portfolioSeries,
    strategies,
  };
}

export async function getStrategyAnalytics(
  portfolioId: number,
  startDate?: string,
  endDate?: string,
): Promise<StrategyAnalytics> {
  const response = await api.get<ApiResponse<unknown>>(
    `/api/trading/portfolios/${portfolioId}/strategy-analytics`,
    {
      params: {
        ...(startDate ? { start_date: startDate } : {}),
        ...(endDate ? { end_date: endDate } : {}),
      },
    },
  );
  if (!response.data.data) throw new Error('无法加载策略分析');
  return normalizeStrategyAnalytics(response.data.data, portfolioId);
}

export async function triggerSimulation(date?: string, portfolioId?: number): Promise<{ job_id: string }> {
  const response = await api.post<ApiResponse<{ job_id: string }>>('/api/trading/simulate/run', {
    date,
    portfolio_id: portfolioId,
  });
  if (!response.data.data) {
    throw new Error('触发模拟失败');
  }
  return response.data.data;
}

export async function backfillSimulation(
  startDate: string,
  endDate: string,
  portfolioId: number,
  restart: boolean = false,
): Promise<{ job_id: string }> {
  const response = await api.post<ApiResponse<{ job_id: string }>>('/api/trading/simulate/backfill', {
    start_date: startDate,
    end_date: endDate,
    portfolio_id: portfolioId,
    restart,
  });
  if (!response.data.data) throw new Error('提交历史模拟失败');
  return response.data.data;
}

export async function getSimulationCalendar(portfolioId?: number): Promise<SimulationCalendar> {
  const response = await api.get<ApiResponse<SimulationCalendar>>('/api/trading/simulate/calendar', {
    params: portfolioId ? { portfolio_id: portfolioId } : {},
  });
  if (!response.data.data) throw new Error('无法读取模拟盘股票池数据范围');
  return response.data.data;
}

export async function getSimulationStatus(portfolioId?: number): Promise<SimulationStatus> {
  const response = await api.get<ApiResponse<SimulationStatus>>('/api/trading/simulate/status', {
    params: portfolioId ? { portfolio_id: portfolioId } : {},
  });
  return response.data.data ?? { status: 'not_started' };
}

export async function listSimulationRuns(limit: number = 10, portfolioId?: number): Promise<SimulationRun[]> {
  const response = await api.get<ApiResponse<SimulationRun[]>>('/api/trading/simulate/runs', {
    params: { limit, ...(portfolioId ? { portfolio_id: portfolioId } : {}) },
  });
  return response.data.data ?? [];
}

export async function getSimulationSchedule(): Promise<SimulationSchedule> {
  const response = await api.get<ApiResponse<SimulationSchedule>>('/api/trading/simulate/schedule');
  return response.data.data ?? { enabled: false, run_time: '', timezone: '', scope: '' };
}
