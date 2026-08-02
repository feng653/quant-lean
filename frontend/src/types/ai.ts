export interface AiUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  latency_ms?: number;
  [key: string]: number | string | boolean | null | undefined;
}

export interface AiResponseMeta {
  cached: boolean;
  model?: string;
  usage?: AiUsage;
}

export interface AiAnalysisResult extends AiResponseMeta {
  experiment_id?: number;
  analysis: string;
}

export interface AiParamSuggestion {
  param_name: string;
  current_value?: unknown;
  suggested_value: unknown;
  reason: string;
  valid?: boolean;
  confidence?: number;
}

export interface AiParamSuggestionResult extends AiResponseMeta {
  strategy_id?: string;
  suggestions: AiParamSuggestion[];
  raw_text?: string;
}

export type AiDiagnosisCategory =
  | 'strategy_interface'
  | 'strategy_code'
  | 'data'
  | 'params'
  | 'environment'
  | 'unknown';

export interface AiDiagnosisResult extends AiResponseMeta {
  experiment_id?: number;
  diagnosis: string;
  category?: AiDiagnosisCategory;
  severity?: string;
  root_cause?: string;
  evidence: string[];
  fix_suggestion?: string;
  fix_suggestions: string[];
  auto_fixable?: boolean;
}

export interface AiMarketInsightResult extends AiResponseMeta {
  portfolio_id?: number;
  insight: string;
}

export interface AiSignalExplanationResult extends AiResponseMeta {
  strategy_id?: string;
  explanation: string;
}

export interface AnalyzeBacktestRequest {
  experiment_id: number;
}

export interface SuggestParamsRequest {
  strategy_id: string;
  current_params: Record<string, unknown>;
}

export interface DiagnoseErrorRequest {
  experiment_id: number;
  error_log: string;
}

export interface ExplainSignalRequest {
  strategy_id: string;
  signal: Record<string, unknown>;
  context?: Record<string, unknown>;
}

export interface MarketInsightRequest {
  portfolio_id: number;
}
