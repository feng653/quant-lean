import { useCallback, useEffect, useMemo, useState } from 'react';
import { getOrders, listDeployments } from '../../services/trading';
import type { Deployment, Order } from '../../types/trading';
import Badge from '../../components/shared/Badge';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import EmptyState from '../../components/shared/EmptyState';
import Input from '../../components/shared/Input';
import PageHeader from '../../components/shared/PageHeader';
import Pagination from '../../components/shared/Pagination';
import Select from '../../components/shared/Select';
import StatusTag from '../../components/shared/StatusTag';
import Table from '../../components/shared/Table';
import type { Column } from '../../components/shared/Table';
import { formatCny } from '../../components/shared/chartTheme';

const ORDER_STATUS: Record<string, { label: string; variant: 'verified' | 'queued' | 'neutral' | 'error' | 'info' }> = {
  filled: { label: '已成交', variant: 'verified' },
  pending: { label: '待成交', variant: 'queued' },
  cancelled: { label: '已取消', variant: 'neutral' },
  rejected: { label: '已拒绝', variant: 'error' },
  partial: { label: '部分成交', variant: 'info' },
};

const PAGE_SIZE = 10;

interface OrderRow extends Order {
  row_id: number;
  display_time: string;
  deployment_display: string;
}

export default function OrderHistoryPage() {
  const [orders, setOrders] = useState<OrderRow[]>([]);
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [deploymentId, setDeploymentId] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await getOrders(deploymentId ? Number(deploymentId) : undefined, 1, 500);
      setOrders(
        result.items.map((item) => ({
          ...item,
          row_id: item.id,
          display_time: item.created_at ?? item.date,
          deployment_display: item.deployment_name ?? `部署 #${item.deployment_id}`,
        })),
      );
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '获取订单失败');
    } finally {
      setLoading(false);
    }
  }, [deploymentId]);

  useEffect(() => {
    void listDeployments()
      .then((result) => setDeployments(result))
      .catch(() => setDeployments([]));
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const filtered = useMemo(() => orders.filter((item) => {
    if (statusFilter && item.status !== statusFilter) return false;
    if (dateFrom && item.display_time < dateFrom) return false;
    if (dateTo && item.display_time > `${dateTo}T23:59:59`) return false;
    return true;
  }), [orders, statusFilter, dateFrom, dateTo]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageItems = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const summary = useMemo(() => {
    const totalAmount = filtered.reduce((sum, item) => sum + (item.amount || 0), 0);
    const buys = filtered.filter((item) => item.action === 'BUY').length;
    const sells = filtered.filter((item) => item.action === 'SELL').length;
    return { totalAmount, count: filtered.length, buys, sells };
  }, [filtered]);

  const columns: Column<OrderRow>[] = [
    {
      key: 'time',
      header: '时间',
      render: (item) => (
        <span className="tnum text-xs text-ink-500">
          {item.display_time ? new Date(item.display_time).toLocaleString('zh-CN', { hour12: false }) : '-'}
        </span>
      ),
    },
    { key: 'code', header: '代码', render: (item) => <span className="font-mono text-xs">{item.code}</span> },
    {
      key: 'action',
      header: '方向',
      render: (item) => (
        <Badge variant={item.action === 'BUY' ? 'danger' : 'info'} size="sm">
          {item.action === 'BUY' ? '买入' : '卖出'}
        </Badge>
      ),
    },
    {
      key: 'order_type',
      header: '类型',
      render: (item) => (
        <Badge variant="default" size="sm">
          {item.order_type === 'market' ? '市价' : item.order_type === 'limit' ? '限价' : item.order_type}
        </Badge>
      ),
    },
    { key: 'price', header: '价格', numeric: true, render: (item) => item.price.toFixed(2) },
    { key: 'shares', header: '数量', numeric: true, render: (item) => item.shares.toLocaleString('zh-CN') },
    { key: 'amount', header: '金额', numeric: true, render: (item) => formatCny(item.amount) },
    {
      key: 'status',
      header: '状态',
      render: (item) => {
        const spec = ORDER_STATUS[item.status] ?? { label: item.status, variant: 'neutral' as const };
        return (
          <span title={item.reject_reason ?? undefined}>
            <StatusTag variant={spec.variant}>{spec.label}</StatusTag>
          </span>
        );
      },
    },
    {
      key: 'reject',
      header: '备注',
      render: (item) => (
        <span className="block max-w-[200px] truncate text-xs text-ink-500" title={item.reject_reason ?? undefined}>
          {item.reject_reason ?? '-'}
        </span>
      ),
    },
    {
      key: 'deployment',
      header: '策略',
      render: (item) => <span className="text-xs text-ink-600">{item.deployment_display}</span>,
    },
  ];

  return (
    <div>
      <PageHeader
        title="订单历史"
        description="查看模拟盘的策略订单与成交状态。拒单与部分成交按真实执行语义保留。"
        breadcrumb={[{ label: '执行' }, { label: '交易工作台', to: '/trading' }, { label: '订单' }]}
        tags={<StatusTag variant="paper">模拟订单</StatusTag>}
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

      {/* Summary */}
      <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="rounded-md border border-ink-200 bg-surface p-3.5">
          <p className="text-xs text-ink-400">总成交额</p>
          <p className="tnum mt-1 text-xl font-semibold">{formatCny(summary.totalAmount)}</p>
        </div>
        <div className="rounded-md border border-ink-200 bg-surface p-3.5">
          <p className="text-xs text-ink-400">总笔数</p>
          <p className="tnum mt-1 text-xl font-semibold">{summary.count}</p>
        </div>
        <div className="rounded-md border border-ink-200 bg-surface p-3.5">
          <p className="text-xs text-ink-400">买入</p>
          <p className="tnum mt-1 text-xl font-semibold text-rise">{summary.buys}</p>
        </div>
        <div className="rounded-md border border-ink-200 bg-surface p-3.5">
          <p className="text-xs text-ink-400">卖出</p>
          <p className="tnum mt-1 text-xl font-semibold text-fall">{summary.sells}</p>
        </div>
      </div>

      {/* Filters */}
      <Card className="mb-4" padding="md">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Select
            label="策略"
            value={deploymentId}
            onChange={(event) => { setDeploymentId(event.target.value); setPage(1); }}
            options={[
              { value: '', label: '全部策略' },
              ...deployments.map((item) => ({ value: String(item.id), label: item.display_name })),
            ]}
          />
          <Input label="开始日期" type="date" value={dateFrom} onChange={(event) => { setDateFrom(event.target.value); setPage(1); }} />
          <Input label="结束日期" type="date" value={dateTo} onChange={(event) => { setDateTo(event.target.value); setPage(1); }} />
          <Select
            label="状态"
            value={statusFilter}
            onChange={(event) => { setStatusFilter(event.target.value); setPage(1); }}
            options={[
              { value: '', label: '全部' },
              { value: 'filled', label: '已成交' },
              { value: 'pending', label: '待成交' },
              { value: 'cancelled', label: '已取消' },
            ]}
          />
          <div className="flex items-end">
            <Button variant="secondary" className="w-full" onClick={() => setPage(1)}>
              查询
            </Button>
          </div>
        </div>
      </Card>

      <Card padding="none">
        {pageItems.length === 0 && !loading ? (
          <EmptyState
            icon="clipboard"
            title="暂无订单记录"
            description="调整筛选条件后，可在这里查看策略订单与成交状态。"
          />
        ) : (
          <Table
            columns={columns}
            data={pageItems}
            keyField="row_id"
            loading={loading}
            caption="模拟盘订单历史"
            minWidth="1080px"
          />
        )}
        <div className="border-t border-ink-100 px-4 py-3">
          <Pagination page={page} total={filtered.length} limit={PAGE_SIZE} onChange={setPage} />
        </div>
      </Card>
      {totalPages > 1 && (
        <p className="sr-only">共 {totalPages} 页</p>
      )}
    </div>
  );
}
