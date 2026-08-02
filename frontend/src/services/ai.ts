import api from './api';
import type {
  AiAnalysisResult,
  AiDiagnosisCategory,
  AiDiagnosisResult,
  AiMarketInsightResult,
  AiParamSuggestion,
  AiParamSuggestionResult,
  AiResponseMeta,
  AiSignalExplanationResult,
  AiUsage,
} from '../types/ai';

type UnknownRecord = Record<string, unknown>;

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function unwrapPayload(value: unknown): UnknownRecord {
  if (!isRecord(value)) return {};
  return isRecord(value.data) ? value.data : value;
}

function optionalString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined;
}

function optionalNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function stringList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.filter((item): item is string => typeof item === 'string' && Boolean(item.trim()));
  }
  const item = optionalString(value);
  return item ? [item] : [];
}

function diagnosisCategory(value: unknown): AiDiagnosisCategory | undefined {
  const category = optionalString(value);
  if (!category) return undefined;
  if ([
    'strategy_interface',
    'strategy_code',
    'data',
    'params',
    'environment',
    'unknown',
  ].includes(category)) {
    return category as AiDiagnosisCategory;
  }
  return 'unknown';
}

function responseMeta(payload: UnknownRecord): AiResponseMeta {
  const nestedMeta = isRecord(payload.meta) ? payload.meta : {};
  const cached = payload.cached ?? nestedMeta.cached;
  const model = optionalString(payload.model) ?? optionalString(nestedMeta.model);
  const usageValue = payload.usage ?? nestedMeta.usage;
  const rawUsage = isRecord(usageValue) ? usageValue : undefined;
  const usage = rawUsage
    ? Object.fromEntries(
        Object.entries(rawUsage).filter(([, value]) => (
          value === null
          || ['number', 'string', 'boolean', 'undefined'].includes(typeof value)
        ))
      ) as AiUsage
    : undefined;

  return {
    cached: cached === true,
    ...(model ? { model } : {}),
    ...(usage ? { usage } : {}),
  };
}

function normalizeError(error: unknown): Error {
  const message = error instanceof Error ? error.message : String(error);
  if (/api[\s_-]*key|deepseek.*(?:not configured|未配置)|(?:not configured|未配置).*deepseek|密钥未配置/i.test(message)) {
    return new Error('AI 服务尚未配置 API Key，请联系管理员配置后重试。');
  }
  if (/503|service unavailable|temporarily unavailable|服务暂不可用/i.test(message)) {
    return new Error('AI 服务暂时不可用，请稍后重试。');
  }
  return error instanceof Error ? error : new Error('AI 请求失败，请稍后重试。');
}

async function postAi(path: string, body: UnknownRecord): Promise<UnknownRecord> {
  try {
    const response = await api.post<unknown>(path, body);
    return unwrapPayload(response.data);
  } catch (error) {
    throw normalizeError(error);
  }
}

function normalizeSuggestion(value: unknown): AiParamSuggestion | null {
  if (!isRecord(value)) return null;
  const paramName = optionalString(value.param_name)
    ?? optionalString(value.parameter)
    ?? optionalString(value.name);
  const hasSuggestedValue = Object.hasOwn(value, 'suggested_value')
    || Object.hasOwn(value, 'recommended_value')
    || Object.hasOwn(value, 'suggested');
  if (!paramName || !hasSuggestedValue) return null;

  const suggestedValue = Object.hasOwn(value, 'suggested_value')
    ? value.suggested_value
    : Object.hasOwn(value, 'recommended_value')
      ? value.recommended_value
      : value.suggested;

  return {
    param_name: paramName,
    current_value: Object.hasOwn(value, 'current_value') ? value.current_value : value.current,
    suggested_value: suggestedValue,
    reason: optionalString(value.reason) ?? optionalString(value.rationale) ?? 'AI 参数建议',
    ...(typeof value.valid === 'boolean' ? { valid: value.valid } : {}),
    ...(optionalNumber(value.confidence) !== undefined ? { confidence: optionalNumber(value.confidence) } : {}),
  };
}

export async function analyzeBacktest(experimentId: number): Promise<AiAnalysisResult> {
  const payload = await postAi('/api/ai/analyze-backtest', { experiment_id: experimentId });
  return {
    ...responseMeta(payload),
    experiment_id: optionalNumber(payload.experiment_id) ?? experimentId,
    analysis: optionalString(payload.analysis) ?? '',
  };
}

export async function suggestParams(
  strategyId: string,
  currentParams: Record<string, unknown>
): Promise<AiParamSuggestionResult> {
  const payload = await postAi('/api/ai/suggest-params', {
    strategy_id: strategyId,
    current_params: currentParams,
  });
  const suggestions = Array.isArray(payload.suggestions)
    ? payload.suggestions.map(normalizeSuggestion).filter((item): item is AiParamSuggestion => item !== null)
    : [];
  const rawText = optionalString(payload.suggestion) ?? optionalString(payload.raw_text);
  return {
    ...responseMeta(payload),
    strategy_id: optionalString(payload.strategy_id) ?? strategyId,
    suggestions,
    ...(rawText ? { raw_text: rawText } : {}),
  };
}

export async function marketInsight(portfolioId: number): Promise<AiMarketInsightResult> {
  const payload = await postAi('/api/ai/market-insight', { portfolio_id: portfolioId });
  return {
    ...responseMeta(payload),
    portfolio_id: optionalNumber(payload.portfolio_id) ?? portfolioId,
    insight: optionalString(payload.insight) ?? '',
  };
}

/** @deprecated Use marketInsight. */
export const getMarketInsight = marketInsight;

export async function diagnoseError(experimentId: number, errorLog: string): Promise<AiDiagnosisResult> {
  const payload = await postAi('/api/ai/diagnose-error', {
    experiment_id: experimentId,
    error_log: errorLog,
  });
  const diagnosisObject = isRecord(payload.diagnosis) ? payload.diagnosis : undefined;
  const structured = isRecord(payload.structured) ? payload.structured : undefined;
  const rootCause = optionalString(structured?.root_cause)
    ?? optionalString(diagnosisObject?.root_cause)
    ?? optionalString(payload.root_cause);
  const diagnosisText = optionalString(payload.diagnosis)
    ?? optionalString(diagnosisObject?.summary)
    ?? optionalString(payload.summary)
    ?? rootCause
    ?? '';
  const fixSuggestions = stringList(
    structured?.fix_suggestion
    ?? structured?.fix_suggestions
    ?? diagnosisObject?.fix_suggestion
    ?? diagnosisObject?.fix_suggestions
    ?? diagnosisObject?.suggestions
    ?? payload.fix_suggestion
    ?? payload.fix_suggestions
    ?? payload.suggestions
  );
  const fixSuggestion = optionalString(structured?.fix_suggestion)
    ?? optionalString(diagnosisObject?.fix_suggestion)
    ?? optionalString(payload.fix_suggestion)
    ?? fixSuggestions[0];
  const category = diagnosisCategory(
    structured?.category
    ?? diagnosisObject?.category
    ?? payload.category
  );
  const autoFixable = structured?.auto_fixable
    ?? diagnosisObject?.auto_fixable
    ?? payload.auto_fixable;

  return {
    ...responseMeta(payload),
    experiment_id: optionalNumber(payload.experiment_id) ?? experimentId,
    diagnosis: diagnosisText,
    evidence: stringList(structured?.evidence ?? diagnosisObject?.evidence ?? payload.evidence),
    fix_suggestions: fixSuggestions,
    ...(category ? { category } : {}),
    ...(optionalString(diagnosisObject?.severity ?? payload.severity)
      ? { severity: optionalString(diagnosisObject?.severity ?? payload.severity) }
      : {}),
    ...(rootCause ? { root_cause: rootCause } : {}),
    ...(fixSuggestion ? { fix_suggestion: fixSuggestion } : {}),
    ...(typeof autoFixable === 'boolean' ? { auto_fixable: autoFixable } : {}),
  };
}

export async function explainSignal(
  strategyId: string,
  signal: Record<string, unknown>,
  context?: Record<string, unknown>
): Promise<AiSignalExplanationResult> {
  const payload = await postAi('/api/ai/explain-signal', {
    strategy_id: strategyId,
    signal,
    ...(context ? { context } : {}),
  });
  return {
    ...responseMeta(payload),
    strategy_id: optionalString(payload.strategy_id) ?? strategyId,
    explanation: optionalString(payload.explanation) ?? '',
  };
}
