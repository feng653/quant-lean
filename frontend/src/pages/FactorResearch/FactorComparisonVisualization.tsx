import EChart from '../../components/shared/EChart';
import Banner from '../../components/shared/Banner';
import type { FactorRunComparison } from '../../services/factorResearch';

type ComparisonRun = FactorRunComparison['runs'][number];

function finiteOrNull(value: number | null): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function metric(value: number | null, digits = 4): string {
  const finite = finiteOrNull(value);
  return finite == null ? '缺失' : finite.toFixed(digits);
}

function percentage(value: number | null): string {
  const finite = finiteOrNull(value);
  return finite == null ? '缺失' : `${(finite * 100).toFixed(1)}%`;
}

function runLabel(item: ComparisonRun, factorNames: Record<string, string>): string {
  return `${factorNames[item.factor_id] ?? item.factor_id} · ${item.primary_horizon}日`;
}

/**
 * A descriptive view of immutable run comparison data.  It intentionally keeps
 * API order: this is evidence inspection, not an automated factor ranking.
 */
export default function FactorComparisonVisualization({
  comparison,
  factorNames,
}: {
  comparison: FactorRunComparison;
  factorNames: Record<string, string>;
}) {
  const option = {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: Array<{ dataIndex: number }>) => {
        const item = comparison.runs[params[0]?.dataIndex];
        if (!item) return '';
        return [
          runLabel(item, factorNames),
          `RankIC 均值：${metric(item.rank_ic_mean)}`,
          `多空均值：${metric(item.long_short_mean)}`,
          `RankIC IR：${metric(item.rank_ic_ir)}`,
          `RankIC 胜率：${percentage(item.rank_ic_positive_ratio)}`,
        ].join('<br/>');
      },
    },
    legend: { data: ['RankIC 均值', '多空均值'] },
    grid: { left: 56, right: 22, top: 44, bottom: 72 },
    xAxis: {
      type: 'category',
      data: comparison.runs.map((item) => runLabel(item, factorNames)),
      axisLabel: { interval: 0, rotate: 28, hideOverlap: true },
    },
    yAxis: {
      type: 'value',
      name: '原始指标值',
      axisLabel: { formatter: (value: number) => value.toFixed(3) },
    },
    series: [
      {
        name: 'RankIC 均值',
        type: 'bar',
        data: comparison.runs.map((item) => finiteOrNull(item.rank_ic_mean)),
        itemStyle: { color: '#2563eb' },
      },
      {
        name: '多空均值',
        type: 'bar',
        data: comparison.runs.map((item) => finiteOrNull(item.long_short_mean)),
        itemStyle: { color: '#0f766e' },
      },
    ],
  };

  return (
    <section className="mt-4" aria-label="因子比较可视化">
      {!comparison.dataset_consistent && (
        <Banner variant="warning" title="不可直接横比">
          所选运行的数据摘要不一致。图表仅保留各自不可变证据的描述性展示，不能据此比较高低、选优或形成交易建议。
        </Banner>
      )}
      <div className="mt-3 rounded border border-ink-200 bg-surface p-3">
        <div className="mb-1 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <h3 className="text-sm font-medium text-ink-800">RankIC 与多空均值</h3>
          <p className="text-xs text-ink-500">保留 API 返回顺序；空值与非有限值以缺失显示，不按 0 处理。</p>
        </div>
        <p className="mb-3 text-xs text-ink-500">
          两项指标量纲不同，柱高只用于查看每条运行自身的量级；悬停可查看 RankIC IR 与胜率。
        </p>
        <EChart option={option} style={{ height: 320 }} />
      </div>
    </section>
  );
}
