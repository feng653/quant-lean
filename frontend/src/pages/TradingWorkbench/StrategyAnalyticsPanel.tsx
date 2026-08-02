import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import ReactECharts from '../../components/shared/EChart';
import Input from '../../components/shared/Input';
import Spinner from '../../components/shared/Spinner';
import { createParameterPreset, getExperiment } from '../../services/experiments';
import { getStrategyAnalytics } from '../../services/trading';
import type {
  Deployment,
  StrategyAnalytics,
  StrategyAnalyticsItem,
  StrategyAnalyticsMetrics,
} from '../../types/trading';

interface StrategyAnalyticsPanelProps {
  portfolioId: number;
  deployments: Deployment[];
}

const colors = ['#2563eb', '#16a34a', '#dc2626', '#9333ea', '#ea580c', '#0891b2'];

const metricColumns: Array<{
  key: keyof StrategyAnalyticsMetrics;
  label: string;
  format: 'percent' | 'number' | 'currency';
}> = [
  { key: 'cumulative_return', label: '累计收益', format: 'percent' },
  { key: 'annualized_return', label: '年化收益', format: 'percent' },
  { key: 'annualized_volatility', label: '年化波动', format: 'percent' },
  { key: 'sharpe_ratio', label: 'Sharpe', format: 'number' },
  { key: 'sortino_ratio', label: 'Sortino', format: 'number' },
  { key: 'calmar_ratio', label: 'Calmar', format: 'number' },
  { key: 'max_drawdown', label: '最大回撤', format: 'percent' },
  { key: 'positive_day_ratio', label: '正收益天数', format: 'percent' },
  { key: 'win_rate', label: '交易胜率', format: 'percent' },
  { key: 'profit_loss_ratio', label: '盈亏比', format: 'number' },
  { key: 'profit_factor', label: 'Profit Factor', format: 'number' },
  { key: 'turnover_rate', label: '换手率', format: 'percent' },
  { key: 'transaction_cost', label: '交易成本', format: 'currency' },
  { key: 'capital_utilization', label: '资金利用率', format: 'percent' },
  { key: 'target_weight_deviation', label: '目标权重偏离', format: 'percent' },
  { key: 'contribution_pnl', label: '收益贡献', format: 'currency' },
  { key: 'risk_contribution', label: '风险贡献', format: 'percent' },
];

const formatMetric = (
  value: number | null,
  format: 'percent' | 'number' | 'currency',
) => {
  if (value == null) return '数据不足';
  if (format === 'percent') return `${(value * 100).toFixed(2)}%`;
  if (format === 'currency') return `¥${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
  return value.toFixed(2);
};

const cumulativeValues = (values: Array<number | null>) => {
  let total = 0;
  return values.map((value) => {
    total += value ?? 0;
    return Number(total.toFixed(2));
  });
};

export default function StrategyAnalyticsPanel({
  portfolioId,
  deployments,
}: StrategyAnalyticsPanelProps) {
  const [analytics, setAnalytics] = useState<StrategyAnalytics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [hidden, setHidden] = useState<Set<number>>(new Set());
  const [savingId, setSavingId] = useState<number | null>(null);
  const [sortKey, setSortKey] = useState<keyof StrategyAnalyticsMetrics>('sharpe_ratio');
  const [sortDescending, setSortDescending] = useState(true);

  const load = useCallback(async (rangeStart?: string, rangeEnd?: string) => {
    setLoading(true);
    setError(null);
    try {
      setAnalytics(await getStrategyAnalytics(
        portfolioId,
        rangeStart,
        rangeEnd,
      ));
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载策略分析失败');
    } finally {
      setLoading(false);
    }
  }, [portfolioId]);

  useEffect(() => {
    void load();
  }, [load]);

  const visibleStrategies = useMemo(
    () => (analytics?.strategies ?? []).filter((item) => !hidden.has(item.deployment_id)),
    [analytics, hidden],
  );
  const sortedStrategies = useMemo(
    () => [...(analytics?.strategies ?? [])].sort((left, right) => {
      const leftValue = left[sortKey];
      const rightValue = right[sortKey];
      if (leftValue == null && rightValue == null) return 0;
      if (leftValue == null) return 1;
      if (rightValue == null) return -1;
      return sortDescending ? rightValue - leftValue : leftValue - rightValue;
    }),
    [analytics, sortDescending, sortKey],
  );

  const portfolioSeries = analytics?.portfolio_series ?? [];
  const dates = visibleStrategies[0]?.series.map((point) => point.date)
    ?? portfolioSeries.map((point) => point.date);

  const navOption = {
    tooltip: { trigger: 'axis' },
    legend: { type: 'scroll', top: 0 },
    grid: { left: '3%', right: '4%', bottom: '14%', containLabel: true },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value', name: '标准化净值', scale: true },
    dataZoom: dates.length > 30 ? [{ type: 'inside' }, { type: 'slider', height: 18 }] : [],
    series: [
      ...(portfolioSeries.length > 0 ? [{
        name: '组合',
        type: 'line',
        symbol: 'none',
        lineStyle: { width: 3, color: '#111827' },
        data: portfolioSeries.map((point) => {
          const base = portfolioSeries.find((item) => item.nav != null)?.nav;
          return point.nav != null && base ? Number((point.nav / base).toFixed(6)) : null;
        }),
      }] : []),
      ...visibleStrategies.map((strategy, index) => ({
        name: strategy.display_name,
        type: 'line',
        symbol: 'none',
        lineStyle: { width: 2, color: colors[index % colors.length] },
        data: strategy.series.map((point) => (
          point.cumulative_return == null
            ? null
            : Number((1 + point.cumulative_return).toFixed(6))
        )),
      })),
    ],
  };

  const pnlOption = {
    tooltip: { trigger: 'axis' },
    legend: { type: 'scroll' },
    grid: { left: '3%', right: '4%', bottom: '12%', containLabel: true },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value', name: '累计盈亏（元）' },
    series: visibleStrategies.map((strategy, index) => ({
      name: strategy.display_name,
      type: 'line',
      symbol: 'none',
      lineStyle: { color: colors[index % colors.length] },
      data: cumulativeValues(strategy.series.map((point) => point.daily_pnl)),
    })),
  };

  const contributionOption = {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { type: 'scroll' },
    grid: { left: '3%', right: '4%', bottom: '12%', containLabel: true },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value', name: '每日收益贡献（元）' },
    series: visibleStrategies.map((strategy, index) => ({
      name: strategy.display_name,
      type: 'bar',
      stack: 'contribution',
      itemStyle: { color: colors[index % colors.length] },
      data: strategy.series.map((point) => point.contribution_pnl),
    })),
  };

  const drawdownOption = {
    tooltip: { trigger: 'axis' },
    legend: { type: 'scroll' },
    grid: { left: '3%', right: '4%', bottom: '12%', containLabel: true },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value', name: '回撤', max: 0, axisLabel: { formatter: '{value}%' } },
    series: visibleStrategies.map((strategy, index) => ({
      name: strategy.display_name,
      type: 'line',
      symbol: 'none',
      lineStyle: { color: colors[index % colors.length] },
      areaStyle: { opacity: 0.05 },
      data: strategy.series.map((point) => (
        point.drawdown == null ? null : Number((point.drawdown * 100).toFixed(4))
      )),
    })),
  };

  const weightOption = {
    tooltip: { trigger: 'axis' },
    legend: { type: 'scroll' },
    grid: { left: '3%', right: '4%', bottom: '12%', containLabel: true },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value', name: '实际仓位', axisLabel: { formatter: '{value}%' } },
    series: visibleStrategies.map((strategy, index) => ({
      name: strategy.display_name,
      type: 'line',
      symbol: 'none',
      lineStyle: { color: colors[index % colors.length] },
      data: strategy.series.map((point) => point.actual_weight_pct),
      markLine: {
        silent: true,
        symbol: 'none',
        lineStyle: { type: 'dashed', color: colors[index % colors.length], opacity: 0.45 },
        data: strategy.series[0]?.target_weight_pct == null
          ? []
          : [{ yAxis: strategy.series[0].target_weight_pct }],
      },
    })),
  };

  const toggleStrategy = (deploymentId: number) => {
    setHidden((current) => {
      const next = new Set(current);
      if (next.has(deploymentId)) next.delete(deploymentId);
      else next.add(deploymentId);
      return next;
    });
  };

  const changeSort = (key: keyof StrategyAnalyticsMetrics) => {
    if (key === sortKey) setSortDescending((current) => !current);
    else {
      setSortKey(key);
      setSortDescending(true);
    }
  };

  const savePreset = async (strategy: StrategyAnalyticsItem) => {
    const defaultName = `${strategy.display_name} - ${new Date().toISOString().slice(0, 10)}`;
    const name = window.prompt('参数方案名称', defaultName)?.trim();
    if (!name) return;
    setSavingId(strategy.deployment_id);
    setFeedback(null);
    setError(null);
    try {
      const deployment = deployments.find((item) => item.id === strategy.deployment_id);
      const source = strategy.source_experiment_id
        ? await getExperiment(strategy.source_experiment_id).catch(() => null)
        : null;
      await createParameterPreset({
        name,
        strategy_id: strategy.strategy_id,
        params: strategy.params,
        mode: source?.mode ?? deployment?.mode ?? 'batch',
        pool_preset: source?.pool_preset ?? 'csi500',
        pool_custom_codes: source?.pool_custom_codes ?? [],
        pool_industries: source?.pool_industries ?? [],
        source_experiment_id: strategy.source_experiment_id ?? undefined,
        metrics_snapshot: Object.fromEntries(
          metricColumns.map(({ key }) => [key, strategy[key]]),
        ),
        notes: `从组合 #${portfolioId} 的策略分析保存`,
        labels: ['交易工作台'],
      });
      setFeedback(`已保存参数方案“${name}”`);
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存参数方案失败');
    } finally {
      setSavingId(null);
    }
  };

  if (loading && !analytics) {
    return <Card><div className="flex justify-center py-16"><Spinner size="lg" /></div></Card>;
  }

  return (
    <div className="space-y-4">
      <Card padding="sm">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="font-semibold text-gray-800">策略有效性分析</h2>
            <p className="mt-1 text-sm text-gray-500">
              采用剔除组合内部资金调拨后的策略收益率，并将各策略贡献与组合收益对账。
            </p>
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <div className="w-40"><Input label="开始日期" type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} /></div>
            <div className="w-40"><Input label="结束日期" type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} /></div>
            <Button variant="secondary" loading={loading} onClick={() => void load(startDate || undefined, endDate || undefined)}>应用范围</Button>
          </div>
        </div>
        {analytics && (
          <p className="mt-3 text-xs text-gray-500">
            数据区间：{analytics.start_date ?? '-'} 至 {analytics.end_date ?? '-'}
          </p>
        )}
      </Card>

      {error && <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}
      {feedback && <div className="rounded-lg border border-green-200 bg-green-50 p-3 text-sm text-green-700">{feedback}</div>}

      {!analytics || analytics.strategies.length === 0 ? (
        <Card><div className="py-12 text-center text-sm text-gray-500">暂无策略级历史。请先在“模拟运行”中执行一次历史回放。</div></Card>
      ) : (
        <>
          <Card padding="sm">
            <div className="flex flex-wrap gap-2">
              {analytics.strategies.map((strategy) => (
                <button
                  key={strategy.deployment_id}
                  type="button"
                  className={`rounded-full border px-3 py-1 text-xs ${hidden.has(strategy.deployment_id) ? 'border-gray-200 bg-gray-50 text-gray-400' : 'border-primary-200 bg-primary-50 text-primary-700'}`}
                  onClick={() => toggleStrategy(strategy.deployment_id)}
                >
                  {hidden.has(strategy.deployment_id) ? '显示' : '隐藏'} {strategy.display_name}
                </button>
              ))}
            </div>
          </Card>

          <Card>
            <h3 className="mb-1 font-semibold">标准化净值对比</h3>
            <p className="mb-3 text-xs text-gray-500">各策略从 1.0 起步，黑色粗线为组合基准。</p>
            <ReactECharts option={navOption} style={{ height: 360 }} />
          </Card>

          <div className="grid gap-4 xl:grid-cols-2">
            <Card>
              <h3 className="mb-1 font-semibold">策略累计盈亏</h3>
              <p className="mb-3 text-xs text-gray-500">金额口径，内部资金调拨不计作策略盈亏。</p>
              <ReactECharts option={pnlOption} style={{ height: 320 }} />
            </Card>
            <Card>
              <h3 className="mb-1 font-semibold">每日收益贡献</h3>
              <p className="mb-3 text-xs text-gray-500">堆叠结果用于解释组合当日盈亏来源。</p>
              <ReactECharts option={contributionOption} style={{ height: 320 }} />
            </Card>
            <Card>
              <h3 className="mb-1 font-semibold">策略回撤对比</h3>
              <p className="mb-3 text-xs text-gray-500">按策略自身历史权益高点计算。</p>
              <ReactECharts option={drawdownOption} style={{ height: 320 }} />
            </Card>
            <Card>
              <h3 className="mb-1 font-semibold">目标权重与实际仓位</h3>
              <p className="mb-3 text-xs text-gray-500">实线为逐日实际仓位，虚线为当前目标权重。</p>
              <ReactECharts option={weightOption} style={{ height: 320 }} />
            </Card>
          </div>

          <Card padding="none">
            <div className="border-b px-4 py-3">
              <h3 className="font-semibold">策略指标对比</h3>
              <p className="mt-1 text-xs text-gray-500">无法由现有历史可靠计算的指标明确显示“数据不足”，不会以 0 替代。</p>
            </div>
            <div className="overflow-x-auto">
              <table className="min-w-[1800px] text-sm">
                <thead className="bg-gray-50 text-xs text-gray-500">
                  <tr>
                    <th className="sticky left-0 z-10 bg-gray-50 px-3 py-3 text-left">策略</th>
                    {metricColumns.map((column) => (
                      <th key={column.key} className="px-3 py-3 text-right">
                        <button
                          type="button"
                          className="whitespace-nowrap hover:text-primary-700"
                          onClick={() => changeSort(column.key)}
                        >
                          {column.label}
                          {sortKey === column.key ? (sortDescending ? ' ↓' : ' ↑') : ''}
                        </button>
                      </th>
                    ))}
                    <th className="px-3 py-3 text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {sortedStrategies.map((strategy) => (
                    <tr key={strategy.deployment_id}>
                      <td className="sticky left-0 z-10 bg-white px-3 py-3">
                        <p className="font-medium">{strategy.display_name}</p>
                        <p className="text-xs text-gray-400">{strategy.data_points} 个交易日</p>
                      </td>
                      {metricColumns.map((column) => (
                        <td key={column.key} className="px-3 py-3 text-right">
                          {formatMetric(strategy[column.key], column.format)}
                        </td>
                      ))}
                      <td className="px-3 py-3 text-right">
                        <div className="flex justify-end gap-2">
                          {strategy.source_experiment_id && (
                            <Link className="rounded px-2 py-1 text-xs text-primary-700 hover:bg-primary-50" to={`/experiment/${strategy.source_experiment_id}`}>来源实验</Link>
                          )}
                          <Button size="sm" variant="secondary" loading={savingId === strategy.deployment_id} onClick={() => void savePreset(strategy)}>保存参数</Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}
