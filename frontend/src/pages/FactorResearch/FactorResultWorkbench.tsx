import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import StatusTag from '../../components/shared/StatusTag';
import type { FactorResearchResult } from '../../services/factorResearch';
import {
  diagnoseFactorResult,
  type FactorResultCheckStatus,
} from './factorResultDiagnosis';

const statusPresentation: Record<
  FactorResultCheckStatus,
  { label: string; variant: 'verified' | 'warning' | 'blocked' }
> = {
  passed: { label: '证据齐全', variant: 'verified' },
  attention: { label: '待补证据', variant: 'warning' },
  blocked: { label: '阻断晋级', variant: 'blocked' },
};

interface Props {
  result: FactorResearchResult;
  onNavigate: (target: string) => void;
}

export default function FactorResultWorkbench({ result, onNavigate }: Props) {
  const diagnosis = diagnoseFactorResult(result);
  const bannerVariant = diagnosis.decision === 'blocked'
    ? 'danger'
    : diagnosis.decision === 'incomplete'
      ? 'warning'
      : 'ok';

  return (
    <Card
      className="mt-4"
      title="运行摘要与研究诊断"
      description="把协议、样本外、实施质量和证据完整性汇总为保守的研究工作流建议"
    >
      <Banner variant={bannerVariant} title={diagnosis.title}>
        {diagnosis.summary}
      </Banner>

      <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <div className="rounded border border-ink-200 p-3">
          <p className="text-xs text-ink-500">因子 / 主周期</p>
          <p className="mt-1 font-semibold">
            {result.factor.name} · {result.request.primary_horizon} 日
          </p>
        </div>
        <div className="rounded border border-ink-200 p-3">
          <p className="text-xs text-ink-500">研究窗口</p>
          <p className="mt-1 text-sm font-semibold tnum">
            {result.request.start} 至 {result.request.end}
          </p>
        </div>
        <div className="rounded border border-ink-200 p-3">
          <p className="text-xs text-ink-500">数据覆盖</p>
          <p className="mt-1 font-semibold tnum">
            {result.dataset.codes} 只 · {result.dataset.rows} 日
          </p>
        </div>
        <div className="rounded border border-ink-200 p-3">
          <p className="text-xs text-ink-500">运行 ID</p>
          <p className="mt-1 truncate font-mono text-xs" title={result.run?.run_id}>
            {result.run?.run_id ?? '旧版同步结果'}
          </p>
        </div>
      </div>

      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-ink-200 text-xs text-ink-500">
              <th className="p-2">证据维度</th>
              <th className="p-2">状态</th>
              <th className="p-2">诊断</th>
              <th className="p-2">定位</th>
            </tr>
          </thead>
          <tbody>
            {diagnosis.checks.map((check) => {
              const presentation = statusPresentation[check.status];
              return (
                <tr key={check.id} className="border-b border-ink-100">
                  <th className="p-2 font-medium text-ink-800">{check.label}</th>
                  <td className="p-2">
                    <StatusTag variant={presentation.variant}>{presentation.label}</StatusTag>
                  </td>
                  <td className="p-2 text-ink-600">{check.summary}</td>
                  <td className="p-2">
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={`查看${check.label}`}
                      onClick={() => onNavigate(check.target)}
                    >
                      查看
                    </Button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="mt-4 rounded border border-ink-200 bg-ink-50/40 p-3">
        <h3 className="text-sm font-semibold text-ink-800">建议的下一步</h3>
        <ol className="mt-2 space-y-2">
          {diagnosis.nextSteps.map((step, index) => (
            <li key={`${step.target}-${step.text}`} className="flex items-center gap-3 text-sm">
              <span className="tnum text-ink-500">{index + 1}.</span>
              <button
                type="button"
                className="text-left text-accent-700 underline-offset-2 hover:underline"
                onClick={() => onNavigate(step.target)}
              >
                {step.text}
              </button>
            </li>
          ))}
        </ol>
      </div>
    </Card>
  );
}
