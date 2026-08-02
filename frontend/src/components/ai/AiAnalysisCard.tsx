import Card from '../shared/Card';
import Button from '../shared/Button';
import type { AiAnalysisResult } from '../../types/ai';
import { AiErrorNotice, AiResultMeta } from './AiShared';
import { useAiAction, type AiScopeKey } from './aiAction';

interface AiAnalysisCardProps {
  onAnalyze: () => Promise<AiAnalysisResult>;
  initialResult?: AiAnalysisResult;
  title?: string;
  disabled?: boolean;
  scopeKey?: AiScopeKey;
}

export default function AiAnalysisCard({
  onAnalyze,
  initialResult,
  title = 'AI 回测分析',
  disabled = false,
  scopeKey,
}: AiAnalysisCardProps) {
  const action = useAiAction(initialResult, scopeKey);

  return (
    <Card>
      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h3 className="font-semibold text-gray-800">{title}</h3>
            <p className="mt-1 text-xs text-gray-500">仅在点击后请求分析，不会自动消耗模型额度。</p>
          </div>
          <Button
            size="sm"
            loading={action.loading}
            disabled={disabled}
            onClick={() => void action.run(onAnalyze)}
          >
            {action.result ? '重新分析' : '开始分析'}
          </Button>
        </div>
        <AiErrorNotice message={action.error} />
        {action.result && (
          <div className="space-y-3">
            <AiResultMeta result={action.result} />
            <div className="whitespace-pre-wrap break-words rounded-lg bg-gray-50 p-3 text-sm leading-6 text-gray-700">
              {action.result.analysis || '模型未返回分析内容。'}
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}
