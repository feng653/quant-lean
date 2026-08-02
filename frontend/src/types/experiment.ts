export interface Experiment {
  id: number;
  user_id: number;
  name: string;
  strategy_id: string;
  strategy_category: string;
  is_starred: boolean;
  labels: string[];
  pool_preset: string | null;
  pool_custom_codes: string[];
  pool_industries: string[];
  train_start?: string | null;
  train_end?: string | null;
  test_start: string;
  test_end: string;
  params: Record<string, unknown>;
  params_hash: string;
  mode: string;
  data_access_policy: 'allow_fetch' | 'cache_only';
  research_trust?: {
    profile: 'governed_production_pit' | 'tushare_research_trusted';
    trust_tier: string;
    known_limitations: string[];
    warning_severity?: 'none' | 'high';
    eligible_for_paper_trading: boolean;
    eligible_for_live_trading: false;
  };
  requires_training: boolean;
  retrain_frequency: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  error_log?: string | null;
  ai_diagnosis?: string | null;
  progress_pct: number;
  progress_message: string;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  source_experiment_id?: number | null;
  sharpe_ratio?: number | null;
  annual_return?: number | null;
  max_drawdown?: number | null;
  win_rate?: number | null;
}

export interface ParameterPreset {
  id: number;
  user_id: number;
  name: string;
  strategy_id: string;
  params: Record<string, unknown>;
  mode: string;
  pool_preset: string;
  pool_custom_codes: string[];
  pool_industries: string[];
  source_experiment_id?: number | null;
  metrics_snapshot: Record<string, unknown>;
  notes: string;
  labels: string[];
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface ExperimentMetrics {
  cumulative_return: number;
  annualized_return: number;
  annual_return?: number;      // 后端返回的字段名
  sharpe_ratio: number;
  max_drawdown: number;
  win_rate: number;
  total_trades: number;
  [key: string]: unknown;      // 兼容其他后端字段
}

export interface EquityPoint {
  date: string;
  equity: number;
  benchmark_equity: number | null;
  benchmark?: number;   // 后端返回的字段名
  drawdown: number | null;
}

export interface Trade {
  id: number;
  experiment_id: number;
  date: string;
  signal_date?: string | null;
  code: string;
  action: 'BUY' | 'SELL';
  price: number;
  shares: number;
  amount: number;
  cost: number;
  signal_strategy?: string;
  signal_score?: number;
}

export interface ModelArtifact {
  id: number;
  experiment_id: number;
  strategy_id: string;
  model_version: number;
  model_storage_key: string | null;
  metadata_storage_key: string | null;
  params_hash: string;
  train_window_start?: string | null;
  train_window_end?: string | null;
  feature_count?: number | null;
  train_samples?: number | null;
  train_metrics: {
    training_mode?: 'train_once' | 'periodic';
    retrain_frequency?: string;
    retrain_count?: number;
    total_fit_samples?: number | null;
    elapsed_seconds?: number | null;
    summary?: string | null;
    last_training_window?: [string, string] | null;
    last_validation_window?: [string, string] | null;
    cycles?: Array<{
      pred_month: string;
      train_start?: string | null;
      train_end?: string | null;
      validation_start?: string | null;
      validation_end?: string | null;
      retrained: boolean;
      fit_seconds: number;
      n_train_samples?: number | null;
      n_validation_samples?: number | null;
      n_train_features?: number | null;
      error?: string | null;
    }>;
    [key: string]: unknown;
  };
  feature_importance: Record<string, number>;
  is_latest: boolean;
  created_at?: string | null;
}
