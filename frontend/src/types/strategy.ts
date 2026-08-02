export interface StrategyMetadata {
  strategy_id: string;
  display_name: string;
  version: string;
  category: 'technical' | 'ml' | 'factor' | 'portfolio' | 'composite';
  description: string;
  supported_modes: string[];
  requires_training: boolean;
  retrain_frequency: string;
  training_mode: 'none' | 'train_once' | 'periodic';
  portfolio_signal_mode: 'event_orders' | 'target_weights';
  execution_config: {
    param_key: '_execution';
    defaults: {
      initial_capital: number;
      max_positions: number;
      lot_size: number;
      volume_participation: number | null;
      commission_rate: number;
      slippage_rate: number;
      stamp_duty_rate: number;
      min_commission: number;
    };
  };
  params: ParamField[];
  sub_strategies: SubStrategyRef[];
  integration_method: string;
  tags: string[];
}

export interface ParamField {
  name: string;
  type: string;
  default: unknown;
  description: string;
  required: boolean;
  min?: number | null;
  max?: number | null;
  step?: number | null;
  choices?: string[] | null;
}

export interface SubStrategyRef {
  strategy_id: string;
  role: string;
  params_override?: Record<string, unknown>;
}
