import type { StrategyMetadata } from '../../types/strategy';
import type { AiParamSuggestion } from '../../types/ai';

export type SuggestionStrategy = Pick<StrategyMetadata, 'strategy_id' | 'display_name' | 'params'>;

export interface AppliedParamSuggestions {
  params: Record<string, unknown>;
  applied: AiParamSuggestion[];
}

export function suggestionValidation(
  strategy: SuggestionStrategy,
  suggestion: AiParamSuggestion
): { valid: boolean; reason?: string } {
  if (suggestion.valid === false) return { valid: false, reason: '服务端标记为不可应用' };
  const field = strategy.params.find((param) => param.name === suggestion.param_name);
  if (!field) return { valid: false, reason: '参数不在当前策略定义中' };

  const value = suggestion.suggested_value;
  const type = field.type.toLowerCase();
  if (['int', 'integer'].includes(type) && (typeof value !== 'number' || !Number.isInteger(value))) {
    return { valid: false, reason: '建议值不是整数' };
  }
  if (['float', 'number'].includes(type) && (typeof value !== 'number' || !Number.isFinite(value))) {
    return { valid: false, reason: '建议值不是有效数字' };
  }
  if (['bool', 'boolean'].includes(type) && typeof value !== 'boolean') {
    return { valid: false, reason: '建议值不是布尔值' };
  }
  if (['str', 'string', 'text', 'choice'].includes(type) && typeof value !== 'string') {
    return { valid: false, reason: '建议值不是文本' };
  }
  if (['list', 'array'].includes(type) && !Array.isArray(value)) {
    return { valid: false, reason: '建议值不是列表' };
  }
  if (field.choices?.length && (
    typeof value !== 'string'
    || !field.choices.includes(value)
  )) {
    return { valid: false, reason: '建议值不在允许选项中' };
  }
  if (typeof value === 'number' && field.min != null && value < field.min) {
    return { valid: false, reason: `建议值低于最小值 ${field.min}` };
  }
  if (typeof value === 'number' && field.max != null && value > field.max) {
    return { valid: false, reason: `建议值高于最大值 ${field.max}` };
  }
  return { valid: true };
}

export function applySelectedSuggestions(
  strategy: SuggestionStrategy,
  currentParams: Record<string, unknown>,
  suggestions: AiParamSuggestion[],
  selectedParamNames: ReadonlySet<string>
): AppliedParamSuggestions {
  const applied = suggestions.filter((suggestion) => (
    selectedParamNames.has(suggestion.param_name)
    && suggestionValidation(strategy, suggestion).valid
  ));
  const params = { ...currentParams };
  applied.forEach((suggestion) => {
    params[suggestion.param_name] = suggestion.suggested_value;
  });
  return { params, applied };
}
