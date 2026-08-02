import Card from '../../components/shared/Card';
import StatusTag from '../../components/shared/StatusTag';
import type { FactorResearchResult } from '../../services/factorResearch';

function metric(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toFixed(4)
    : '-';
}

export default function NeutralizationResult({
  result,
}: {
  result: FactorResearchResult['neutralization'];
}) {
  if (!result || result.status === 'not_requested') return null;
  const primary = result.primary_factor;
  if (!primary) return null;
  const summary = primary.summary;
  return (
    <Card
      className="mt-4"
      title="行业与规模中性化诊断"
      description="每个交易日独立 OLS；被排除日期不会进入后续 IC、分层收益或组合分析"
    >
      <div className="flex flex-wrap items-center gap-2">
        <StatusTag variant="verified">{result.mode}</StatusTag>
        <StatusTag variant={summary.dates_excluded === 0 ? 'verified' : 'warning'}>
          有效 {summary.dates_neutralized}/{summary.dates_total} 日
        </StatusTag>
        <span className="text-xs text-ink-500">拟合窗口：仅同一交易日</span>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[
          ['观测覆盖', summary.coverage_ratio == null ? '-' : `${(summary.coverage_ratio * 100).toFixed(1)}%`],
          ['中性化前平均 R²', metric(summary.mean_r_squared_before)],
          ['中性化后平均 R²', metric(summary.mean_r_squared_after)],
          ['排除日期', String(summary.dates_excluded)],
        ].map(([label, value]) => (
          <div key={label} className="rounded border border-ink-200 p-3">
            <p className="text-xs text-ink-500">{label}</p>
            <p className="mt-1 font-semibold tnum">{value}</p>
          </div>
        ))}
      </div>
      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-ink-200 text-ink-500">
              <th className="p-2">日期</th>
              <th className="p-2">状态</th>
              <th className="p-2">样本</th>
              <th className="p-2">覆盖</th>
              <th className="p-2">R² 前 / 后</th>
              <th className="p-2">规模暴露 前 / 后</th>
              <th className="p-2">排除原因</th>
            </tr>
          </thead>
          <tbody>
            {primary.daily.slice(-30).map((item) => (
              <tr key={item.date} className="border-b border-ink-100">
                <td className="p-2 tnum">{item.date}</td>
                <td className="p-2">{item.status}</td>
                <td className="p-2 tnum">{item.sample_count}/{item.candidate_count}</td>
                <td className="p-2 tnum">
                  {item.coverage_ratio == null ? '-' : `${(item.coverage_ratio * 100).toFixed(1)}%`}
                </td>
                <td className="p-2 tnum">
                  {metric(item.before?.r_squared)} / {metric(item.after?.r_squared)}
                </td>
                <td className="p-2 tnum">
                  {metric(item.before?.log_market_cap)} / {metric(item.after?.log_market_cap)}
                </td>
                <td className="p-2 text-xs">
                  {Object.entries(item.dropped_by_reason)
                    .filter(([, count]) => count > 0)
                    .map(([reason, count]) => `${reason}: ${count}`)
                    .join(' · ') || '-'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {primary.daily.length > 30 && (
        <p className="mt-2 text-xs text-ink-500">
          页面展示最近 30 个交易日；完整逐日诊断可通过研究证据导出。
        </p>
      )}
    </Card>
  );
}
