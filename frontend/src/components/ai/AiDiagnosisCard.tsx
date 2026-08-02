import Card from '../shared/Card';
import Button from '../shared/Button';
import Badge from '../shared/Badge';
import type { AiDiagnosisResult } from '../../types/ai';
import { AiErrorNotice, AiResultMeta } from './AiShared';
import { useAiAction, type AiScopeKey } from './aiAction';

interface AiDiagnosisCardProps {
  onDiagnose: () => Promise<AiDiagnosisResult>;
  initialResult?: AiDiagnosisResult;
  disabled?: boolean;
  scopeKey?: AiScopeKey;
}

export default function AiDiagnosisCard({
  onDiagnose,
  initialResult,
  disabled = false,
  scopeKey,
}: AiDiagnosisCardProps) {
  const action = useAiAction(initialResult, scopeKey);

  return (
    <Card>
      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h3 className="font-semibold text-gray-800">AI 错误诊断</h3>
            <p className="mt-1 text-xs text-gray-500">
              诊断仅供参考；结果可能保存到实验记录，但不会修改策略参数或代码。
            </p>
          </div>
          <Button
            size="sm"
            loading={action.loading}
            disabled={disabled}
            onClick={() => void action.run(onDiagnose)}
          >
            {action.result ? '重新诊断' : '开始诊断'}
          </Button>
        </div>
        <AiErrorNotice message={action.error} />
        {action.result && (
          <div className="space-y-4">
            <AiResultMeta result={action.result} />
            <div className="flex flex-wrap gap-2">
              {action.result.category && <Badge variant="info">{action.result.category}</Badge>}
              {action.result.severity && <Badge variant="warning">{action.result.severity}</Badge>}
              {typeof action.result.auto_fixable === 'boolean' && (
                <Badge variant={action.result.auto_fixable ? 'success' : 'default'}>
                  {action.result.auto_fixable ? '具备自动处理条件' : '需人工处理'}
                </Badge>
              )}
            </div>
            {action.result.diagnosis && (
              <p className="whitespace-pre-wrap break-words text-sm leading-6 text-gray-700">
                {action.result.diagnosis}
              </p>
            )}
            {action.result.root_cause && (
              <section>
                <h4 className="mb-2 text-sm font-medium text-gray-800">根因</h4>
                <p className="rounded-lg bg-gray-50 px-3 py-2 text-sm text-gray-700">
                  {action.result.root_cause}
                </p>
              </section>
            )}
            <DiagnosisList title="诊断证据" items={action.result.evidence} />
            <DiagnosisList
              title="修复建议"
              items={action.result.fix_suggestions.length
                ? action.result.fix_suggestions
                : action.result.fix_suggestion
                  ? [action.result.fix_suggestion]
                  : []}
            />
          </div>
        )}
      </div>
    </Card>
  );
}

function DiagnosisList({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <section>
      <h4 className="mb-2 text-sm font-medium text-gray-800">{title}</h4>
      <ul className="space-y-2">
        {items.map((item, index) => (
          <li key={`${title}-${index}`} className="rounded-lg bg-gray-50 px-3 py-2 text-sm text-gray-700">
            {item}
          </li>
        ))}
      </ul>
    </section>
  );
}
