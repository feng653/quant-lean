import { useCallback, useEffect, useMemo, useState } from 'react';
import { getPositions } from '../../services/trading';
import type { Position } from '../../types/trading';
import Badge from '../../components/shared/Badge';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import EChart from '../../components/shared/EChart';
import EmptyState from '../../components/shared/EmptyState';
import PageHeader from '../../components/shared/PageHeader';
import StatusTag from '../../components/shared/StatusTag';
import {
  baseTooltip,
  CHART_COLORS,
  formatCny,
  SERIES_PALETTE,
} from '../../components/shared/chartTheme';

interface PositionRow extends Position {
  row_key: string;
  display: string;
  deployment: string;
}

export default function PositionMonitorPage() {
  const [positions, setPositions] = useState<PositionRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await getPositions();
      setPositions(
        result.map((item) => ({
          ...item,
          row_key: `${item.portfolio_id ?? 0}-${item.deployment_id ?? 0}-${item.code}`,
          display: item.name ?? item.code,
          deployment: item.deployment_name ?? (item.deployment_id ? `部署 #${item.deployment_id}` : '-'),
        })),
      );
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '获取持仓失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const result = await getPositions();
        if (cancelled) return;
        setPositions(
          result.map((item) => ({
            ...item,
            row_key: `${item.portfolio_id ?? 0}-${item.deployment_id ?? 0}-${item.code}`,
            display: item.name ?? item.code,
            deployment: item.deployment_name ?? (item.deployment_id ? `部署 #${item.deployment_id}` : '-'),
          })),
        );
        setLoading(false);
      } catch (err: unknown) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : '获取持仓失败');
          setLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const totalMarketValue = positions.reduce((sum, item) => sum + (item.market_value || 0), 0);
  const totalPnl = positions.reduce((sum, item) => sum + (item.unrealized_pnl || 0), 0);

  const pieOption = useMemo(() => ({
    color: [...SERIES_PALETTE],
    tooltip: baseTooltip({
      trigger: 'item',
      formatter: '{b}: ¥{c} ({d}%)',
    }),
    legend: { show: false },
    series: [
      {
        type: 'pie',
        radius: ['42%', '68%'],
        center: ['50%', '50%'],
        label: { color: CHART_COLORS.axisLabel, fontSize: 11 },
        data: positions.map((item) => ({
          name: item.code,
          value: Math.max(0, item.market_value || 0),
        })),
      },
    ],
  }), [positions]);

  return (
    <div>
      <PageHeader
        title="持仓监控"
        description="查看最近模拟交易日的收盘持仓与浮动盈亏。盈亏按 A 股习惯标注：红为盈、绿为亏。"
        breadcrumb={[{ label: '执行' }, { label: '交易工作台', to: '/trading' }, { label: '持仓' }]}
        tags={<StatusTag variant="paper">模拟盘持仓</StatusTag>}
      />

      {error && (
        <Banner
          variant="danger"
          className="mb-4"
          action={<Button variant="secondary" size="sm" onClick={() => void load()}>重试</Button>}
        >
          {error}
        </Banner>
      )}

      {loading ? (
        <Card><p className="py-10 text-center text-sm text-ink-400">加载持仓...</p></Card>
      ) : positions.length === 0 ? (
        <div className="rounded-md border border-ink-200 bg-surface">
          <EmptyState
            icon="positions"
            title="暂无持仓数据"
            description="模拟交易产生持仓后，这里会展示仓位、成本与浮动盈亏。"
          />
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <Card title="仓位分布" padding="sm">
            <EChart option={pieOption} style={{ height: 320 }} notMerge />
          </Card>
          <Card className="xl:col-span-2" title="持仓明细" padding="none">
            <div className="overflow-x-auto scrollbar-thin">
              <table className="w-full text-sm" style={{ minWidth: 900 }}>
                <caption className="sr-only">模拟盘收盘持仓</caption>
                <thead>
                  <tr className="border-b border-ink-200 bg-ink-50">
                    {['代码', '名称', '数量', '成本价', '现价', '市值', '盈亏', '占比', '所属策略'].map((header, index) => (
                      <th
                        key={header}
                        scope="col"
                        className={`px-3 py-2.5 text-xs font-semibold uppercase tracking-wide text-ink-500 ${
                          index >= 2 && index <= 7 ? 'text-right' : 'text-left'
                        }`}
                      >
                        {header}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-ink-100">
                  {positions.map((item) => (
                    <tr key={item.row_key}>
                      <td className="px-3 py-2.5 font-mono text-xs">{item.code}</td>
                      <td className="px-3 py-2.5 text-ink-700">{item.display}</td>
                      <td className="tnum px-3 py-2.5 text-right">{item.shares.toLocaleString('zh-CN')}</td>
                      <td className="tnum px-3 py-2.5 text-right">{item.avg_cost.toFixed(2)}</td>
                      <td className="tnum px-3 py-2.5 text-right">{item.close_price.toFixed(2)}</td>
                      <td className="tnum px-3 py-2.5 text-right">{formatCny(item.market_value)}</td>
                      <td className={`tnum px-3 py-2.5 text-right font-medium ${item.unrealized_pnl >= 0 ? 'text-rise' : 'text-fall'}`}>
                        {item.unrealized_pnl >= 0 ? '+' : ''}{formatCny(item.unrealized_pnl)}
                      </td>
                      <td className="tnum px-3 py-2.5 text-right">{((item.weight_in_portfolio || 0) * 100).toFixed(1)}%</td>
                      <td className="px-3 py-2.5">
                        <Badge variant="info" size="sm">{item.deployment}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="border-t border-ink-200 bg-ink-50 font-medium">
                    <td className="px-3 py-2.5" colSpan={5}>汇总</td>
                    <td className="tnum px-3 py-2.5 text-right">{formatCny(totalMarketValue)}</td>
                    <td className={`tnum px-3 py-2.5 text-right ${totalPnl >= 0 ? 'text-rise' : 'text-fall'}`}>
                      {totalPnl >= 0 ? '+' : ''}{formatCny(totalPnl)}
                    </td>
                    <td className="tnum px-3 py-2.5 text-right">100.0%</td>
                    <td />
                  </tr>
                </tfoot>
              </table>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}
