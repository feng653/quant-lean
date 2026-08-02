import { useCallback, useEffect, useMemo, useState } from 'react';
import { getSignals, listDeployments, triggerSimulation } from '../../services/trading';
import { explainSignal } from '../../services/ai';
import type { Deployment, Signal } from '../../types/trading';
import { useAuthStore } from '../../store/authStore';
import { SignalExplanationDialog } from '../../components/ai';
import Badge from '../../components/shared/Badge';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import EmptyState from '../../components/shared/EmptyState';
import Icon from '../../components/shared/Icon';
import Input from '../../components/shared/Input';
import PageHeader from '../../components/shared/PageHeader';
import Select from '../../components/shared/Select';
import StatusTag from '../../components/shared/StatusTag';
import Table from '../../components/shared/Table';
import type { Column } from '../../components/shared/Table';

interface SignalItem extends Signal {
  row_id: string;
  signal_type: 'buy' | 'sell' | 'hold';
  target_weight_pct: number;
}

const SIGNAL_BADGE: Record<string, { label: string; variant: 'danger' | 'info' | 'default' }> = {
  buy: { label: '买入', variant: 'danger' },
  sell: { label: '卖出', variant: 'info' },
  hold: { label: '持有', variant: 'default' },
};

export default function SignalPanelPage() {
  const user = useAuthStore((s) => s.user);
  const canUseAi = user?.is_admin || user?.permissions.includes('ai:use');
  const canExecute = user?.is_admin || user?.permissions.includes('trading:execute');

  const [signals, setSignals] = useState<SignalItem[]>([]);
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [deploymentFilter, setDeploymentFilter] = useState('');
  const [dateFilter, setDateFilter] = useState('');
  const [livePolling, setLivePolling] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [jobMessage, setJobMessage] = useState<string | null>(null);
  const [explainTarget, setExplainTarget] = useState<SignalItem | null>(null);

  const loadSignals = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const result = await getSignals(
        deploymentFilter ? Number(deploymentFilter) : undefined,
        dateFilter || undefined,
      );
      setSignals(
        result.map((item, index) => {
          const action = (item.action || '').toLowerCase();
          const weight = item.weight ?? 0;
          return {
            ...item,
            row_id: `${item.code}-${item.date ?? ''}-${index}`,
            signal_type: action === 'buy' ? 'buy' : action === 'sell' ? 'sell' : 'hold',
            target_weight_pct: Math.abs(weight) <= 1 ? weight * 100 : weight,
          };
        }),
      );
      setError(null);
    } catch (err: unknown) {
      if (!quiet) setError(err instanceof Error ? err.message : '获取信号失败');
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [deploymentFilter, dateFilter]);

  useEffect(() => {
    void listDeployments()
      .then((result) => setDeployments(result))
      .catch(() => setDeployments([]));
  }, []);

  useEffect(() => {
    void loadSignals();
  }, [loadSignals]);

  useEffect(() => {
    if (!livePolling) return;
    const timer = window.setInterval(() => void loadSignals(true), 5000);
    return () => window.clearInterval(timer);
  }, [livePolling, loadSignals]);

  const runSimulation = async () => {
    setJobMessage(null);
    try {
      const result = await triggerSimulation(dateFilter || undefined);
      setJobMessage(`模拟任务已提交：${result.job_id}`);
      window.setTimeout(() => void loadSignals(true), 1500);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '提交模拟任务失败');
    }
  };

  const stats = useMemo(() => {
    const buy = signals.filter((item) => item.signal_type === 'buy').length;
    const sell = signals.filter((item) => item.signal_type === 'sell').length;
    const avgScore = signals.length > 0
      ? signals.reduce((sum, item) => sum + (item.score || 0), 0) / signals.length
      : 0;
    return { total: signals.length, buy, sell, avgScore };
  }, [signals]);

  const deploymentNameOf = (id?: number): string =>
    deployments.find((item) => item.id === id)?.display_name ?? (id ? `部署 #${id}` : '-');

  const columns: Column<SignalItem>[] = [
    { key: 'code', header: '代码', render: (item) => <span className="font-mono text-xs font-medium">{item.code}</span> },
    {
      key: 'type',
      header: '信号类型',
      render: (item) => {
        const spec = SIGNAL_BADGE[item.signal_type];
        return <Badge variant={spec.variant} size="sm">{spec.label}</Badge>;
      },
    },
    { key: 'score', header: '评分', numeric: true, render: (item) => (item.score ?? 0).toFixed(3) },
    { key: 'weight', header: '目标权重', numeric: true, render: (item) => `${item.target_weight_pct.toFixed(1)}%` },
    { key: 'confidence', header: '置信度', numeric: true, render: (item) => `${((item.confidence ?? 0) * 100).toFixed(0)}%` },
    {
      key: 'reasoning',
      header: '理由',
      render: (item) => (
        <span className="block max-w-[280px] truncate text-xs text-ink-500" title={item.reasoning}>
          {item.reasoning || '-'}
        </span>
      ),
    },
    {
      key: 'time',
      header: '时间',
      render: (item) => <span className="tnum text-xs text-ink-500">{item.date ?? item.created_at ?? '-'}</span>,
    },
    {
      key: 'deployment',
      header: '部署',
      render: (item) => <span className="text-xs text-ink-600">{deploymentNameOf(item.deployment_id)}</span>,
    },
    ...(canUseAi
      ? [{
          key: 'ai',
          header: 'AI',
          render: (item: SignalItem) => (
            <Button
              variant="ghost"
              size="sm"
              disabled={!item.deployment_id}
              onClick={() => setExplainTarget(item)}
            >
              解释
            </Button>
          ),
        } as Column<SignalItem>]
      : []),
  ];

  return (
    <div>
      <PageHeader
        title="信号面板"
        description="展示模拟执行产生并已落库的真实信号。"
        breadcrumb={[{ label: '执行' }, { label: '交易工作台', to: '/trading' }, { label: '信号' }]}
        tags={<StatusTag variant="paper">模拟信号</StatusTag>}
        actions={
          <>
            <Button
              variant={livePolling ? 'secondary' : 'ghost'}
              size="sm"
              aria-pressed={livePolling}
              onClick={() => setLivePolling((current) => !current)}
            >
              <Icon name={livePolling ? 'pause' : 'play'} className="h-4 w-4" />
              {livePolling ? '停止轮询' : '实时轮询'}
            </Button>
            {canExecute && (
              <Button size="sm" onClick={() => void runSimulation()}>
                <Icon name="play" className="h-4 w-4" />
                运行日频模拟
              </Button>
            )}
          </>
        }
      />

      <p className="mb-4 text-xs text-ink-400">
        {livePolling ? '每 5 秒从服务端刷新（轮询模式，与实盘交易无关）。' : '按需查询。'}
      </p>

      {jobMessage && <Banner variant="ok" className="mb-4">{jobMessage}</Banner>}
      {error && (
        <Banner
          variant="danger"
          className="mb-4"
          action={<Button variant="secondary" size="sm" onClick={() => void loadSignals()}>重试</Button>}
        >
          {error}
        </Banner>
      )}

      {/* Stats */}
      <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="rounded-md border border-ink-200 bg-surface p-3.5">
          <p className="text-xs text-ink-400">信号总数</p>
          <p className="tnum mt-1 text-xl font-semibold">{stats.total}</p>
        </div>
        <div className="rounded-md border border-ink-200 bg-surface p-3.5">
          <p className="text-xs text-ink-400">买入</p>
          <p className="tnum mt-1 text-xl font-semibold text-rise">{stats.buy}</p>
        </div>
        <div className="rounded-md border border-ink-200 bg-surface p-3.5">
          <p className="text-xs text-ink-400">卖出</p>
          <p className="tnum mt-1 text-xl font-semibold text-fall">{stats.sell}</p>
        </div>
        <div className="rounded-md border border-ink-200 bg-surface p-3.5">
          <p className="text-xs text-ink-400">平均评分</p>
          <p className="tnum mt-1 text-xl font-semibold">{stats.avgScore.toFixed(3)}</p>
        </div>
      </div>

      {/* Filters */}
      <Card className="mb-4" padding="md">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Select
            label="策略部署"
            value={deploymentFilter}
            onChange={(event) => setDeploymentFilter(event.target.value)}
            options={[
              { value: '', label: '全部部署' },
              ...deployments.map((item) => ({ value: String(item.id), label: item.display_name })),
            ]}
          />
          <Input
            label="交易日期"
            type="date"
            value={dateFilter}
            onChange={(event) => setDateFilter(event.target.value)}
          />
        </div>
      </Card>

      <Card padding="none">
        {signals.length === 0 && !loading ? (
          <EmptyState
            icon="inbox"
            title="暂无已落库信号，请先创建部署和组合后运行日频模拟。"
          />
        ) : (
          <Table
            columns={columns}
            data={signals}
            keyField="row_id"
            loading={loading}
            caption="已落库模拟信号"
            minWidth="1024px"
          />
        )}
      </Card>

      {explainTarget && canUseAi && (
        <SignalExplanationDialog
          isOpen={explainTarget !== null}
          onClose={() => setExplainTarget(null)}
          onExplain={() => {
            const deployment = deployments.find((item) => item.id === explainTarget.deployment_id);
            if (!deployment) {
              return Promise.reject(new Error('该信号缺少对应的策略部署'));
            }
            return explainSignal(
              deployment.strategy_id,
              {
                code: explainTarget.code,
                action: explainTarget.action?.toUpperCase(),
                score: explainTarget.score,
                confidence: explainTarget.confidence,
                target_weight: explainTarget.target_weight_pct,
                timestamp: explainTarget.date,
              },
              { deployment_id: explainTarget.deployment_id, reasoning: explainTarget.reasoning },
            );
          }}
          signalLabel={`${explainTarget.code} · ${SIGNAL_BADGE[explainTarget.signal_type].label}`}
          scopeKey={`signal:${explainTarget.row_id}`}
        />
      )}
    </div>
  );
}
