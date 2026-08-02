import Card from '../shared/Card';
import Button from '../shared/Button';
import type { AiMarketInsightResult } from '../../types/ai';
import { AiErrorNotice, AiResultMeta } from './AiShared';
import { useAiAction, type AiScopeKey } from './aiAction';

interface AiMarketInsightCardProps {
  onRequestInsight: () => Promise<AiMarketInsightResult>;
  initialResult?: AiMarketInsightResult;
  disabled?: boolean;
  scopeKey?: AiScopeKey;
}

export default function AiMarketInsightCard({
  onRequestInsight,
  initialResult,
  disabled = false,
  scopeKey,
}: AiMarketInsightCardProps) {
  const action = useAiAction(initialResult, scopeKey);

  return (
    <Card>
      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h3 className="font-semibold text-gray-800">AI 市场洞察</h3>
            <p className="mt-1 text-xs text-gray-500">洞察仅供研究参考，不构成投资建议。</p>
          </div>
          <Button
            size="sm"
            loading={action.loading}
            disabled={disabled}
            onClick={() => void action.run(onRequestInsight)}
          >
            {action.result ? '刷新洞察' : '获取洞察'}
          </Button>
        </div>
        <AiErrorNotice message={action.error} />
        {action.result && (
          <div className="space-y-3">
            <AiResultMeta result={action.result} />
            <div className="whitespace-pre-wrap break-words rounded-lg bg-gray-50 p-3 text-sm leading-6 text-gray-700">
              {action.result.insight || '模型未返回市场洞察。'}
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}
