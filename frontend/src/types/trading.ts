export interface Deployment {
  id: number;
  strategy_id: string;
  strategy_category: string;
  display_name: string;
  params: Record<string, unknown>;
  mode: string;
  source_experiment_id?: number;
  source_model_artifact_id?: number;
  research_promotion_id?: number;
  promotion_report_id?: number;
  promotion_manifest_hash?: string;
  promotion_model_sha256?: string | null;
  promotion_binding_hash?: string;
  requires_retraining: boolean;
  retrain_frequency?: string | null;
  status: string;
  status_tags: string[];
  user_notes?: string | null;
  research_risk_snapshot?: {
    schema_version: 'paper-deployment-research-risk/v1';
    warnings: string[];
    warning_severity: 'none' | 'high';
    research_generation_id?: string | null;
    research_source_id?: string | null;
    window: { start?: string | null; end?: string | null };
    research_promotion_bound: boolean;
    paper_eligible: boolean;
    live_eligible: false;
  } | null;
  research_risk_snapshot_hash?: string | null;
  research_generation_id?: string | null;
  research_source_id?: string | null;
  research_window_start?: string | null;
  research_window_end?: string | null;
  current_model_version?: number;
  last_retrain_at?: string | null;
}

export interface ModelLifecycleFailure {
  code: string;
  message: string;
}

export interface ModelLifecycleVersion {
  id: number;
  deployment_id: number;
  model_version: number;
  model_storage_key?: string | null;
  metadata_storage_key?: string | null;
  train_metrics: Record<string, unknown>;
  feature_importance: Record<string, unknown>;
  validation_metrics: Record<string, unknown>;
  train_window_start?: string | null;
  train_window_end?: string | null;
  validation_window_start?: string | null;
  validation_window_end?: string | null;
  model_sha256?: string | null;
  model_size?: number | null;
  retrain_manifest_hash?: string | null;
  status: string;
  is_latest: boolean | number;
  manifest_verified: boolean;
  failure?: ModelLifecycleFailure | null;
  created_at: string;
}

export interface ModelRetrainAttempt {
  attempt_id: string;
  deployment_id: number;
  expected_model_version: number;
  candidate_model_version: number;
  status: string;
  train_window_start?: string | null;
  train_window_end?: string | null;
  validation_window_start?: string | null;
  validation_window_end?: string | null;
  validation_metrics: Record<string, unknown>;
  model_sha256?: string | null;
  model_size?: number | null;
  retrain_manifest_hash?: string | null;
  manifest_verified: boolean;
  failure?: ModelLifecycleFailure | null;
  created_at: string;
  completed_at?: string | null;
}

export interface ModelLifecycle {
  deployment: {
    id: number;
    display_name?: string | null;
    strategy_id: string;
    status: string;
    requires_retraining: boolean;
    retrain_frequency?: string | null;
    current_model_version: number;
    last_retrain_at?: string | null;
  };
  schedule: {
    enabled: boolean;
    eligible: boolean;
    next_retrain_at?: string | null;
    scan_minutes: number;
  };
  versions: ModelLifecycleVersion[];
  attempts: ModelRetrainAttempt[];
  safety: {
    automatic_live_publish: boolean;
    candidate_requires_validation: boolean;
    failed_candidate_preserves_champion: boolean;
  };
}

export interface Portfolio {
  id: number;
  name: string;
  total_capital: number;
  rebalance_frequency: string;
  allocations: PortfolioAllocation[];
  cash_balance?: number;
  current_revision?: number;
  status?: string;
}

export interface PortfolioAllocation {
  deployment_id: number;
  target_weight_bps: number;
  min_weight_bps: number;
  max_weight_bps: number;
  locked: boolean;
  risk_budget_bps?: number | null;
  capital?: number;
  display_name?: string;
  strategy_id?: string;
}

export interface Position {
  id?: number;
  portfolio_id?: number;
  deployment_id?: number;
  date?: string;
  code: string;
  name?: string;
  deployment_name?: string;
  shares: number;
  avg_cost: number;
  close_price: number;
  market_value: number;
  unrealized_pnl: number;
  weight_in_portfolio: number;
}

export interface Signal {
  id?: number;
  deployment_id?: number;
  date?: string;
  code: string;
  action: string;
  score: number;
  weight: number;
  confidence: number;
  reasoning: string;
  created_at?: string;
}

export interface Order {
  id: number;
  deployment_id: number;
  code: string;
  portfolio_id?: number;
  date: string;
  action: 'BUY' | 'SELL';
  order_type: string;
  price: number;
  shares: number;
  amount: number;
  status: string;
  cost?: number;
  reject_reason?: string | null;
  filled_at?: string | null;
  created_at?: string | null;
  deployment_name?: string;
}

export interface PortfolioNavPoint {
  date: string;
  nav: number;
  daily_return: number | null;
  cumulative_return: number | null;
  cash_balance: number | null;
}

export interface StrategyAnalyticsPoint {
  date: string;
  nav: number | null;
  equity: number | null;
  daily_pnl: number | null;
  daily_return: number | null;
  cumulative_return: number | null;
  drawdown: number | null;
  contribution_pnl: number | null;
  contribution_return: number | null;
  target_weight_pct: number | null;
  actual_weight_pct: number | null;
}

export interface StrategyAnalyticsMetrics {
  cumulative_return: number | null;
  annualized_return: number | null;
  annualized_volatility: number | null;
  sharpe_ratio: number | null;
  sortino_ratio: number | null;
  calmar_ratio: number | null;
  max_drawdown: number | null;
  positive_day_ratio: number | null;
  win_rate: number | null;
  profit_loss_ratio: number | null;
  profit_factor: number | null;
  turnover_rate: number | null;
  transaction_cost: number | null;
  capital_utilization: number | null;
  target_weight_deviation: number | null;
  contribution_pnl: number | null;
  contribution_return: number | null;
  risk_contribution: number | null;
}

export interface StrategyAnalyticsItem extends StrategyAnalyticsMetrics {
  deployment_id: number;
  display_name: string;
  strategy_id: string;
  status: string;
  source_experiment_id: number | null;
  params: Record<string, unknown>;
  data_points: number;
  series: StrategyAnalyticsPoint[];
}

export interface PortfolioAnalyticsPoint {
  date: string;
  nav: number | null;
  daily_return: number | null;
  cumulative_return: number | null;
  drawdown: number | null;
}

export interface StrategyAnalytics {
  portfolio_id: number;
  start_date: string | null;
  end_date: string | null;
  portfolio_series: PortfolioAnalyticsPoint[];
  strategies: StrategyAnalyticsItem[];
}
