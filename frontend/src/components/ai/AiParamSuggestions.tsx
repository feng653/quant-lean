import { useState } from 'react';
import Card from '../shared/Card';
import Button from '../shared/Button';
import Badge from '../shared/Badge';
import type { AiParamSuggestion, AiParamSuggestionResult } from '../../types/ai';
import { AiErrorNotice, AiResultMeta } from './AiShared';
import { useAiAction, type AiScopeKey } from './aiAction';
import {
  applySelectedSuggestions,
  suggestionValidation,
  type SuggestionStrategy,
} from './paramSuggestionUtils';

interface AiParamSuggestionsProps {
  strategy: SuggestionStrategy;
  currentParams: Record<string, unknown>;
  onSuggest: (
    strategyId: string,
    currentParams: Record<string, unknown>
  ) => Promise<AiParamSuggestionResult>;
  onApply: (nextParams: Record<string, unknown>, applied: AiParamSuggestion[]) => void;
  initialResult?: AiParamSuggestionResult;
  disabled?: boolean;
  scopeKey?: AiScopeKey;
}

export default function AiParamSuggestions({
  strategy,
  currentParams,
  onSuggest,
  onApply,
  initialResult,
  disabled = false,
  scopeKey,
}: AiParamSuggestionsProps) {
  const action = useAiAction(initialResult, scopeKey);
  const [selection, setSelection] = useState<{
    scopeKey: AiScopeKey;
    values: Set<string>;
  }>({ scopeKey, values: new Set() });
  const selected = Object.is(selection.scopeKey, scopeKey)
    ? selection.values
    : new Set<string>();
  const suggestions = action.result?.suggestions ?? [];

  const toggleSelected = (paramName: string) => {
    setSelection((previous) => {
      const previousValues = Object.is(previous.scopeKey, scopeKey)
        ? previous.values
        : new Set<string>();
      const next = new Set(previousValues);
      if (next.has(paramName)) next.delete(paramName);
      else next.add(paramName);
      return { scopeKey, values: next };
    });
  };

  const handleApply = () => {
    const next = applySelectedSuggestions(strategy, currentParams, suggestions, selected);
    if (next.applied.length) onApply(next.params, next.applied);
  };

  const selectedCount = suggestions.filter((suggestion) => (
    selected.has(suggestion.param_name)
    && suggestionValidation(strategy, suggestion).valid
  )).length;

  return (
    <Card>
      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h3 className="font-semibold text-gray-800">AI 参数建议</h3>
            <p className="mt-1 text-xs text-gray-500">
              {strategy.display_name} · 建议不会自动写入，请逐项确认。
            </p>
          </div>
          <Button
            size="sm"
            loading={action.loading}
            disabled={disabled}
            onClick={() => {
              setSelection({ scopeKey, values: new Set() });
              void action.run(() => onSuggest(strategy.strategy_id, currentParams));
            }}
          >
            {action.result ? '重新生成' : '生成建议'}
          </Button>
        </div>
        <AiErrorNotice message={action.error} />
        {action.result && <AiResultMeta result={action.result} />}
        {action.result?.raw_text && !suggestions.length && (
          <div className="space-y-2 rounded-lg border border-yellow-200 bg-yellow-50 p-3">
            <p className="text-xs font-medium text-yellow-800">旧版文本建议（仅供阅读，不能直接应用）</p>
            <p className="whitespace-pre-wrap break-words text-sm leading-6 text-yellow-900">
              {action.result.raw_text}
            </p>
          </div>
        )}
        {suggestions.length > 0 && (
          <div className="space-y-2">
            {suggestions.map((suggestion, index) => {
              const validation = suggestionValidation(strategy, suggestion);
              const checked = selected.has(suggestion.param_name);
              return (
                <label
                  key={`${suggestion.param_name}-${index}`}
                  className={`flex items-start gap-3 rounded-lg border p-3 ${
                    validation.valid ? 'border-gray-200 hover:bg-gray-50' : 'border-gray-200 bg-gray-50 opacity-70'
                  }`}
                >
                  <input
                    type="checkbox"
                    className="mt-1 h-4 w-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500"
                    checked={checked}
                    disabled={!validation.valid}
                    onChange={() => toggleSelected(suggestion.param_name)}
                  />
                  <span className="min-w-0 flex-1 space-y-1">
                    <span className="flex flex-wrap items-center gap-2 text-sm font-medium text-gray-800">
                      {suggestion.param_name}
                      {!validation.valid && <Badge variant="warning" size="sm">不可应用</Badge>}
                    </span>
                    <span className="block break-words text-sm text-gray-600">
                      {formatValue(suggestion.current_value ?? currentParams[suggestion.param_name])}
                      {' → '}
                      <span className="font-medium text-primary-700">
                        {formatValue(suggestion.suggested_value)}
                      </span>
                    </span>
                    <span className="block text-xs leading-5 text-gray-500">
                      {validation.reason ?? suggestion.reason}
                    </span>
                  </span>
                </label>
              );
            })}
            <div className="flex flex-col gap-2 pt-1 sm:flex-row sm:items-center sm:justify-between">
              <span className="text-xs text-gray-500">已选择 {selectedCount} 项合法建议</span>
              <Button size="sm" disabled={selectedCount === 0 || disabled} onClick={handleApply}>
                应用所选建议
              </Button>
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}

function formatValue(value: unknown): string {
  if (value === undefined) return '未设置';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}
