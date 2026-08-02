import { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { getEquityCurve, getExperiment, getExperimentMetrics } from '../../services/experiments';
import type { EquityPoint, Experiment, ExperimentMetrics } from '../../types/experiment';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import EChart from '../../components/shared/EChart';
import EmptyState from '../../components/shared/EmptyState';
import Icon from '../../components/shared/Icon';
import PageHeader from '../../components/shared/PageHeader';
import Skeleton from '../../components/shared/Skeleton';
import {
  baseGrid,
  baseLegend,
  baseTooltip,
  baseXAxis,
  baseYAxis,
  formatPct,
  SERIES_PALETTE,
} from '../../components/shared/chartTheme';

interface CompareItem {
  experiment: Experiment;
  metrics: ExperimentMetrics | null;
  equity: EquityPoint[];
}

const COMPARE_METRICS = [
  { key: 'annual_return', label: '年化收益', pct: true, higherBetter: true },
  { key: 'sharpe_ratio', label: 'Sharpe', pct: false, higherBetter: true },
  { key: 'max_drawdown', label: '最大回撤', pct: true, higherBetter: false },
  { key: 'win_rate', label: '胜率', pct: true, higherBetter: true },
  { key: 'total_trades', label: '总交易数', pct: false, higherBetter: false },
] as const;

function readMetric(metrics: ExperimentMetrics | null, key: string): number | null {
  if (!metrics) return null;
  const value = metrics[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export default function ComparePage() {
  const location = useLocation();
  const navigate = useNavigate();
  const ids = useMemo(
    () => (location.state as { ids?: number[] } | null)?.ids ?? [],
    [location.state],
  );

  const [items, setItems] = useState<CompareItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [normalized, setNormalized] = useState(true);

  const load = useCallback(async () => {
    if (ids.length === 0) return;
    setLoading(true);
    setError(null);
    try {
      const results = await Promise.all(
        ids.map(async (id) => {
          const experiment = await getExperiment(id);
          const metrics = await getExperimentMetrics(id).catch(() => null);
          const equity = await getEquityCurve(id).catch(() => [] as EquityPoint[]);
          return { experiment, metrics, equity };
        }),
      );
      setItems(results);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '加载对比数据失败');
    } finally {
      setLoading(false);
    }
  }, [ids]);

  useEffect(() => {
    void load();
  }, [load]);

  const chartOption = useMemo(() => {
    const allDates = Array.from(
      new Set(items.flatMap((item) => item.equity.map((point) => point.date))),
    ).sort();
    return {
      color: [...SERIES_PALETTE],
      grid: baseGrid(),
      legend: baseLegend(),
      tooltip: baseTooltip(),
      xAxis: baseXAxis({ data: allDates }),
      yAxis: baseYAxis(),
      series: items.map((item) => {
        const byDate = new Map(item.equity.map((point) => [point.date, point.equity]));
        const first = item.equity[0]?.equity ?? 1;
        return {
          name: `#${item.experiment.id} ${item.experiment.name}`,
          type: 'line' as const,
          showSymbol: false,
          lineStyle: { width: 1.8 },
          data: allDates.map((date) => {
            const value = byDate.get(date);
            if (value === undefined) return null;
            return normalized && first > 0 ? value / first : value;
          }),
        };
      }),
    };
  }, [items, normalized]);

  if (ids.length === 0) {
    return (
      <div>
        <PageHeader title="实验对比" breadcrumb={[{ label: '研究' }, { label: '实验中心', to: '/experiment' }, { label: '对比' }]} />
        <div className="rounded-md border border-ink-200 bg-surface">
          <EmptyState
            icon="compare"
            title="请选择要对比的实验"
            description="在实验列表中勾选两个或以上实验，然后发起对比。"
            action={
              <Button onClick={() => navigate('/experiment')}>前往实验列表</Button>
            }
          />
        </div>
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        title="实验对比"
        description={`对比 ${ids.length} 个实验。高亮仅为所选实验内的相对比较，不代表实盘有效性。`}
        breadcrumb={[{ label: '研究' }, { label: '实验中心', to: '/experiment' }, { label: '对比' }]}
        actions={
          <Button variant="secondary" size="sm" onClick={() => navigate('/experiment')}>
            <Icon name="arrowLeft" className="h-4 w-4" />
            返回列表
          </Button>
        }
      />

      {error && (
        <Banner
          variant="danger"
          className="mb-4"
          action={
            <Button variant="secondary" size="sm" onClick={() => void load()}>
              重试
            </Button>
          }
        >
          {error}
        </Banner>
      )}

      {loading ? (
        <div className="space-y-4">
          <Skeleton className="h-56 w-full" />
          <Skeleton className="h-72 w-full" />
        </div>
      ) : (
        <>
          {/* Metrics table */}
          <Card title="指标对比" className="mb-4" padding="none">
            <div className="overflow-x-auto scrollbar-thin">
              <table className="w-full text-sm" style={{ minWidth: 720 }}>
                <caption className="sr-only">实验指标对比</caption>
                <thead>
                  <tr className="border-b border-ink-200 bg-ink-50">
                    <th scope="col" className="px-4 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-ink-500">
                      指标
                    </th>
                    {items.map((item) => (
                      <th key={item.experiment.id} scope="col" className="tnum px-4 py-2.5 text-right text-xs font-semibold text-ink-700">
                        #{item.experiment.id} {item.experiment.name}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-ink-100">
                  {COMPARE_METRICS.map((metric) => {
                    const values = items.map((item) => readMetric(item.metrics, metric.key));
                    const present = values.filter((value): value is number => value !== null);
                    const best = present.length > 0
                      ? (metric.higherBetter ? Math.max(...present) : Math.min(...present))
                      : null;
                    return (
                      <tr key={metric.key}>
                        <th scope="row" className="px-4 py-2.5 text-left text-xs font-medium text-ink-500">
                          {metric.label}
                          {metric.higherBetter ? '' : '（越小越优）'}
                        </th>
                        {items.map((item, index) => {
                          const value = values[index];
                          const isBest = value !== null && best !== null && value === best && present.length > 1;
                          return (
                            <td
                              key={item.experiment.id}
                              className={`tnum px-4 py-2.5 text-right ${
                                isBest ? 'font-semibold text-accent-800 underline decoration-accent-400 underline-offset-4' : 'text-ink-700'
                              }`}
                            >
                              {value === null ? '-' : metric.pct ? formatPct(value) : value.toFixed(2)}
                            </td>
                          );
                        })}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>

          {/* Equity overlay */}
          <Card
            title="净值叠加"
            padding="sm"
            actions={
              <div className="flex items-center gap-1 rounded border border-ink-200 p-0.5">
                {[
                  { key: true, label: '归一化' },
                  { key: false, label: '原始净值' },
                ].map((option) => (
                  <button
                    key={String(option.key)}
                    type="button"
                    aria-pressed={normalized === option.key}
                    onClick={() => setNormalized(option.key)}
                    className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
                      normalized === option.key
                        ? 'bg-accent-700 text-white'
                        : 'text-ink-600 hover:bg-ink-100'
                    }`}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
            }
          >
            <EChart option={chartOption} style={{ height: 340 }} notMerge />
          </Card>
        </>
      )}
    </div>
  );
}
