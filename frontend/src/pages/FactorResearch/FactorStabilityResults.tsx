import Banner from '../../components/shared/Banner';
import Card from '../../components/shared/Card';
import StatusTag from '../../components/shared/StatusTag';
import type { FactorStabilityResult } from '../../services/factorResearch';

interface Props {
  stability: FactorStabilityResult | null | undefined;
  configured: boolean;
}

const roleLabel = {
  train: '训练',
  validation: '验证',
  locked: '锁定',
};

function metric(value: number | null | undefined, digits = 4): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toFixed(digits)
    : '-';
}

function ratio(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${(value * 100).toFixed(1)}%`
    : '-';
}

export default function FactorStabilityResults({
  stability,
  configured,
}: Props) {
  if (!stability) {
    return (
      <Card
        className="mt-4"
        title="样本外稳定性"
        description="训练、验证与锁定窗口的防泄漏证据"
      >
        <p role="status" className="text-sm text-ink-500">
          {configured
            ? '该运行未包含样本外结果，可能来自旧版服务或计算尚未完成。'
            : '本次运行未启用预注册样本外评估。重新运行并声明三个窗口后可生成分窗证据。'}
        </p>
      </Card>
    );
  }

  const primary = String(stability.stability_summary.primary_horizon);
  return (
    <Card
      className="mt-4"
      title="预注册样本外稳定性"
      description={`主评估周期 ${primary} 个交易日；日度观测不跨窗汇总`}
    >
      <Banner variant="warning" title="解释与泄漏边界">
        {stability.warnings.join(' ')}
      </Banner>

      <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <div className="rounded border border-ink-200 p-3">
          <p className="text-xs text-ink-500">RankIC 同号</p>
          <p className="mt-1 font-semibold">
            {stability.stability_summary.rank_ic_sign_consistent ? '是' : '否'}
          </p>
        </div>
        <div className="rounded border border-ink-200 p-3">
          <p className="text-xs text-ink-500">RankIC 均值极差</p>
          <p className="mt-1 font-semibold tnum">
            {metric(stability.stability_summary.rank_ic_mean_range)}
          </p>
        </div>
        <div className="rounded border border-ink-200 p-3">
          <p className="text-xs text-ink-500">锁定减验证 RankIC</p>
          <p className="mt-1 font-semibold tnum">
            {metric(stability.stability_summary.locked_minus_validation_rank_ic)}
          </p>
        </div>
        <div className="rounded border border-ink-200 p-3">
          <p className="text-xs text-ink-500">可评估窗口</p>
          <p className="mt-1 font-semibold tnum">
            {stability.stability_summary.windows_with_evaluable_primary_ic} / 3
          </p>
        </div>
      </div>

      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-ink-200 text-ink-500">
              <th className="p-2">窗口</th>
              <th className="p-2">交易日</th>
              <th className="p-2">IC</th>
              <th className="p-2">RankIC</th>
              <th className="p-2">ICIR</th>
              <th className="p-2">胜率</th>
              <th className="p-2">分层多空</th>
              <th className="p-2">覆盖</th>
              <th className="p-2">校正 p</th>
            </tr>
          </thead>
          <tbody>
            {stability.windows.map((window) => {
              const horizon = window.horizons[primary];
              const rank = horizon.ic.summary.rank_ic;
              return (
                <tr key={window.role} className="border-b border-ink-100">
                  <td className="p-2">
                    <div className="flex items-center gap-2">
                      <span>{roleLabel[window.role]}</span>
                      {window.role === 'locked' && (
                        <StatusTag variant="warning">预先锁定</StatusTag>
                      )}
                    </div>
                    <p className="mt-0.5 whitespace-nowrap text-xs text-ink-500">
                      {window.actual_start} 至 {window.actual_end}
                    </p>
                  </td>
                  <td className="p-2 tnum">{window.sessions}</td>
                  <td className="p-2 tnum">
                    {metric(horizon.ic.summary.pearson_ic.mean)}
                  </td>
                  <td className="p-2 tnum">{metric(rank.mean)}</td>
                  <td className="p-2 tnum">{metric(rank.icir)}</td>
                  <td className="p-2 tnum">{ratio(rank.positive_ratio)}</td>
                  <td className="p-2 tnum">
                    {metric(window.quantile_returns.long_short.mean)}
                  </td>
                  <td className="p-2 tnum">
                    {ratio(window.coverage.primary_evaluation_ratio)}
                  </td>
                  <td className="p-2 tnum">
                    {metric(horizon.multiple_testing.adjusted_p_value)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="mt-4 overflow-x-auto">
        <h4 className="mb-2 text-sm font-medium text-ink-700">RankIC 衰减分窗</h4>
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-ink-200 text-ink-500">
              <th className="p-2">窗口</th>
              {stability.windows[0]?.decay.points.map((point) => (
                <th key={point.horizon} className="p-2">{point.horizon} 日</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {stability.windows.map((window) => (
              <tr key={window.role} className="border-b border-ink-100">
                <td className="p-2">{roleLabel[window.role]}</td>
                {window.decay.points.map((point) => (
                  <td key={point.horizon} className="p-2 tnum">
                    {metric(point.rank_ic.mean)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-4 rounded border border-ink-200 p-3 text-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="font-medium text-ink-700">多重检验控制</p>
          <StatusTag variant="neutral">
            Bonferroni · {stability.multiple_testing.hypotheses_tested} 项
          </StatusTag>
        </div>
        <p className="mt-2 text-ink-600">
          α {metric(stability.multiple_testing.alpha, 3)}，校正阈值{' '}
          {metric(stability.multiple_testing.adjusted_alpha, 6)}。
          {stability.multiple_testing.interpretation}
        </p>
      </div>
    </Card>
  );
}
