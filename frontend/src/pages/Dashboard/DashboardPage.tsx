import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router';
import {
  getPortfolioNav,
  getPositions,
  getSignals,
  listDeployments,
  listPortfolios,
} from '../../services/trading';
import type { Deployment, Portfolio, PortfolioNavPoint, Position, Signal } from '../../types/trading';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import EChart from '../../components/shared/EChart';
import EmptyState from '../../components/shared/EmptyState';
import Icon from '../../components/shared/Icon';
import PageHeader from '../../components/shared/PageHeader';
import Skeleton from '../../components/shared/Skeleton';
import StatusTag from '../../components/shared/StatusTag';
import {
  baseGrid,
  baseTooltip,
  baseXAxis,
  baseYAxis,
  CHART_COLORS,
  formatCny,
  formatSignedPct,
  signedToneClass,
} from '../../components/shared/chartTheme';

interface AggregatedNavPoint {
  date: string;
  equity: number;
}

/** Union of all dates; each portfolio contributes its last NAV at or before that date. */
function aggregateNav(series: PortfolioNavPoint[][]): AggregatedNavPoint[] {
  const dates = Array.from(
    new Set(series.flatMap((points) => points.map((point) => point.date))),
  ).sort();
  const sortedSeries = series.map((points) =>
    [...points].sort((a, b) => a.date.localeCompare(b.date)),
  );
  return dates.map((date) => {
    let equity = 0;
    for (const points of sortedSeries) {
      let last: number | null = null;
      for (const point of points) {
        if (point.date <= date) last = point.nav;
        else break;
      }
      if (last !== null) equity += last;
    }
    return { date, equity };
  });
}

export default function DashboardPage() {
  const [portfolios, setPortfolios] = useState<Portfolio[]>([]);
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [navSeries, setNavSeries] = useState<AggregatedNavPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [portfolioList, activeDeployments, positionList, signalList] = await Promise.all([
        listPortfolios(),
        listDeployments({ status: 'active' }),
        getPositions(),
        getSignals(),
      ]);
      const navs = await Promise.all(
        portfolioList.map((portfolio) =>
          getPortfolioNav(portfolio.id).catch(() => [] as PortfolioNavPoint[]),
        ),
      );
      setPortfolios(portfolioList);
      setDeployments(activeDeployments);
      setPositions(positionList);
      setSignals(signalList.slice(0, 8));
      setNavSeries(aggregateNav(navs));
      setLastUpdated(new Date().toLocaleString('zh-CN', { hour12: false }));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '加载总览失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const totalPositionValue = positions.reduce((sum, item) => sum + (item.market_value || 0), 0);
  const configuredCapital = portfolios.reduce((sum, item) => sum + (item.total_capital || 0), 0);
  const latestNav = navSeries.length > 0 ? navSeries[navSeries.length - 1].equity : null;
  const previousNav = navSeries.length > 1 ? navSeries[navSeries.length - 2].equity : null;
  const totalEquity = latestNav ?? (totalPositionValue || configuredCapital);
  const dailyReturn =
    latestNav !== null && previousNav !== null && previousNav > 0
      ? latestNav / previousNav - 1
      : null;

  const chartOption = {
    grid: baseGrid(),
    tooltip: baseTooltip({
      valueFormatter: (value: unknown) => formatCny(typeof value === 'number' ? value : null),
    }),
    xAxis: baseXAxis({ data: navSeries.map((point) => point.date) }),
    yAxis: baseYAxis({
      axisLabel: {
        color: CHART_COLORS.axisLabel,
        fontSize: 11,
        formatter: (value: number) => `${(value / 10_000).toFixed(0)}万`,
      },
    }),
    series: [
      {
        name: '组合总权益',
        type: 'line',
        data: navSeries.map((point) => point.equity),
        smooth: true,
        showSymbol: false,
        lineStyle: { color: CHART_COLORS.accent, width: 2 },
        itemStyle: { color: CHART_COLORS.accent },
        areaStyle: { color: CHART_COLORS.accent, opacity: 0.08 },
      },
    ],
  };

  return (
    <div>
      <PageHeader
        title="总览"
        description="模拟盘组合、持仓和策略运行状态。所有数字来自已落库的模拟净值，不代表真实资金收益。"
        breadcrumb={[{ label: '研究' }, { label: '总览' }]}
        actions={
          <Button variant="secondary" size="sm" onClick={() => void load()} loading={loading}>
            <Icon name="refresh" className="h-4 w-4" />
            刷新
          </Button>
        }
        tags={
          <>
            <StatusTag variant="paper">模拟盘环境</StatusTag>
            <StatusTag variant="live">实盘未认证</StatusTag>
            <span className="text-xs text-ink-400">
              数据更新：{lastUpdated ?? '尚未更新'}
            </span>
          </>
        }
      />

      {error && (
        <Banner variant="danger" className="mb-5" title="加载总览失败">
          {error}
        </Banner>
      )}

      {/* KPI row */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <KpiCard
          label="组合总权益"
          value={totalEquity ? formatCny(totalEquity) : '-'}
          caption={`${portfolios.length} 个活动组合`}
          loading={loading}
        />
        <KpiCard
          label="最近交易日收益"
          value={dailyReturn !== null ? formatSignedPct(dailyReturn) : '-'}
          valueClass={signedToneClass(dailyReturn)}
          caption="来自已落库净值"
          loading={loading}
        />
        <KpiCard
          label="当前持仓"
          value={String(positions.length)}
          caption={`市值 ${formatCny(totalPositionValue)}`}
          loading={loading}
        />
        <KpiCard
          label="活动策略部署"
          value={String(deployments.length)}
          caption={`${signals.length} 条最近信号`}
          loading={loading}
        />
      </div>

      {/* Equity chart */}
      <Card
        className="mt-5"
        title="组合权益走势"
        description="跨组合按交易日聚合的模拟净值"
        actions={
          <Link to="/trading/portfolio">
            <Button variant="ghost" size="sm">
              组合管理
              <Icon name="arrowRight" className="h-4 w-4" />
            </Button>
          </Link>
        }
        padding="sm"
      >
        {loading ? (
          <Skeleton className="h-64 w-full" />
        ) : navSeries.length === 0 ? (
          <EmptyState
            icon="chart"
            title="暂无模拟净值"
            description="发布组合并在信号面板运行每日模拟后生成。"
          />
        ) : (
          <EChart option={chartOption} style={{ height: 280 }} notMerge />
        )}
      </Card>

      {/* Active deployments + recent signals */}
      <div className="mt-5 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card
          title="活动部署"
          actions={
            <Link to="/strategies">
              <Button variant="ghost" size="sm">
                策略库
                <Icon name="arrowRight" className="h-4 w-4" />
              </Button>
            </Link>
          }
          padding="none"
        >
          {loading ? (
            <div className="p-4"><Skeleton lines={4} className="h-9 w-full" /></div>
          ) : deployments.length === 0 ? (
            <EmptyState icon="trading" title="暂无活动部署" />
          ) : (
            <ul className="divide-y divide-ink-100">
              {deployments.slice(0, 6).map((deployment) => (
                <li key={deployment.id} className="flex items-center justify-between gap-3 px-4 py-3 sm:px-5">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-ink-800">
                      {deployment.display_name}
                    </p>
                    <p className="mt-0.5 truncate text-xs text-ink-400">
                      <span className="font-mono">{deployment.strategy_id}</span>
                      {' · '}{deployment.mode}
                    </p>
                  </div>
                  <StatusTag variant="paper">{deployment.status}</StatusTag>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card
          title="最近信号"
          actions={
            <Link to="/trading/signals">
              <Button variant="ghost" size="sm">
                信号面板
                <Icon name="arrowRight" className="h-4 w-4" />
              </Button>
            </Link>
          }
          padding="none"
        >
          {loading ? (
            <div className="p-4"><Skeleton lines={4} className="h-9 w-full" /></div>
          ) : signals.length === 0 ? (
            <EmptyState icon="inbox" title="暂无交易信号" />
          ) : (
            <ul className="divide-y divide-ink-100">
              {signals.map((signal, index) => {
                const action = signal.action?.toUpperCase();
                return (
                  <li key={`${signal.code}-${signal.date ?? ''}-${index}`} className="flex items-center justify-between gap-3 px-4 py-2.5 sm:px-5">
                    <div className="min-w-0">
                      <p className="font-mono text-sm font-medium text-ink-800">{signal.code}</p>
                      <p className="mt-0.5 text-xs text-ink-400">
                        {signal.date ?? '-'} · 部署 #{signal.deployment_id ?? '-'}
                      </p>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <span
                        className={`text-xs font-semibold ${
                          action === 'BUY' ? 'text-rise' : action === 'SELL' ? 'text-fall' : 'text-ink-500'
                        }`}
                      >
                        {action === 'BUY' ? '买入' : action === 'SELL' ? '卖出' : action || '-'}
                      </span>
                      <span className="tnum text-xs text-ink-500">
                        评分 {(signal.score ?? 0).toFixed(3)}
                      </span>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
      </div>

      {/* Platform trust footer */}
      <div className="mt-5 flex flex-wrap items-center gap-x-6 gap-y-2 rounded-md border border-ink-200 bg-surface px-4 py-3 text-xs text-ink-500">
        <span className="flex items-center gap-1.5">
          <Icon name="lock" className="h-4 w-4 text-danger-fg" aria-hidden />
          实盘交易未认证、已锁定
          <Link to="/trading/brokers" className="font-medium text-accent-700 hover:underline">
            查看门禁证据
          </Link>
        </span>
        <span className="flex items-center gap-1.5">
          <Icon name="database" className="h-4 w-4 text-ink-400" aria-hidden />
          行情缓存覆盖与质量
          <Link to="/data" className="font-medium text-accent-700 hover:underline">
            前往数据中心
          </Link>
        </span>
        <span className="flex items-center gap-1.5">
          <Icon name="info" className="h-4 w-4 text-ink-400" aria-hidden />
          本页盈亏按 A 股习惯标注：红为盈、绿为亏
        </span>
      </div>
    </div>
  );
}

function KpiCard({
  label,
  value,
  caption,
  loading,
  valueClass = 'text-ink-900',
}: {
  label: string;
  value: string;
  caption: string;
  loading: boolean;
  valueClass?: string;
}) {
  return (
    <div className="rounded-md border border-ink-200 bg-surface p-4">
      <p className="text-xs font-medium uppercase tracking-wide text-ink-400">{label}</p>
      {loading ? (
        <Skeleton className="mt-2 h-7 w-24" />
      ) : (
        <p className={`tnum mt-1.5 text-2xl font-semibold leading-8 ${valueClass}`}>{value}</p>
      )}
      <p className="mt-1 text-xs text-ink-400">{caption}</p>
    </div>
  );
}
