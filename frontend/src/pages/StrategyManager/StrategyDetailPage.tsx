import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { getStrategy } from '../../services/strategies';
import { listExperiments } from '../../services/experiments';
import type { Experiment } from '../../types/experiment';
import type { StrategyMetadata } from '../../types/strategy';
import { strategyCategoryLabel, trainingModeLabel, strategyTrainingMode } from '../../utils/strategy';
import { formatBackendDateTime } from '../../utils/datetime';
import Badge from '../../components/shared/Badge';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import DescriptionList from '../../components/shared/DescriptionList';
import Icon from '../../components/shared/Icon';
import PageHeader from '../../components/shared/PageHeader';
import Skeleton from '../../components/shared/Skeleton';
import StatusTag from '../../components/shared/StatusTag';
import Table from '../../components/shared/Table';
import type { Column } from '../../components/shared/Table';

const EXPERIMENT_STATUS: Record<string, { label: string; variant: 'queued' | 'running' | 'verified' | 'error' | 'neutral' }> = {
  pending: { label: '等待中', variant: 'queued' },
  running: { label: '运行中', variant: 'running' },
  completed: { label: '已完成', variant: 'verified' },
  failed: { label: '失败', variant: 'error' },
  cancelled: { label: '已取消', variant: 'neutral' },
};

export default function StrategyDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [strategy, setStrategy] = useState<StrategyMetadata | null>(null);
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const result = await getStrategy(id);
      setStrategy(result);
      try {
        const starred = await listExperiments({ strategy_id: id, is_starred: true, limit: 5 });
        setExperiments(starred.items);
      } catch {
        setExperiments([]);
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '获取策略详情失败');
      setStrategy(null);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (error || !strategy) {
    return (
      <div>
        <PageHeader title="策略详情" breadcrumb={[{ label: '研究' }, { label: '策略管理', to: '/strategies' }, { label: id ?? '' }]} />
        <Banner
          variant="danger"
          title={error ? '获取策略详情失败' : '策略不存在'}
          action={
            <Button variant="secondary" size="sm" onClick={() => (error ? void load() : navigate('/strategies'))}>
              {error ? '重试' : '返回列表'}
            </Button>
          }
        >
          {error ?? `没有找到策略 ${id}，它可能未注册或已被移除。`}
        </Banner>
      </div>
    );
  }

  const execution = strategy.execution_config?.defaults;
  const experimentColumns: Column<Experiment>[] = [
    {
      key: 'name',
      header: '实验名称',
      render: (experiment) => <span className="font-medium text-ink-800">{experiment.name}</span>,
    },
    {
      key: 'status',
      header: '状态',
      render: (experiment) => {
        const spec = EXPERIMENT_STATUS[experiment.status] ?? { label: experiment.status, variant: 'neutral' as const };
        return <StatusTag variant={spec.variant}>{spec.label}</StatusTag>;
      },
    },
    {
      key: 'created_at',
      header: '创建时间',
      render: (experiment) => (
        <span className="tnum text-ink-500">
          {formatBackendDateTime(experiment.created_at)}
        </span>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title={strategy.display_name}
        breadcrumb={[{ label: '研究' }, { label: '策略管理', to: '/strategies' }, { label: strategy.display_name }]}
        actions={
          <Button variant="secondary" size="sm" onClick={() => navigate('/strategies')}>
            <Icon name="arrowLeft" className="h-4 w-4" />
            返回列表
          </Button>
        }
        tags={
          <>
            <Badge variant="accent">{strategyCategoryLabel(strategy.category)}</Badge>
            <Badge variant="default">v{strategy.version}</Badge>
            <Badge variant={strategyTrainingMode(strategy) === 'periodic' ? 'warning' : strategyTrainingMode(strategy) === 'train_once' ? 'info' : 'default'}>
              {trainingModeLabel(strategy)}
            </Badge>
            {strategy.supported_modes.map((mode) => (
              <Badge key={mode} variant="default">{mode}</Badge>
            ))}
          </>
        }
      />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card title="基本信息" className="lg:col-span-2">
          <DescriptionList
            columns={2}
            items={[
              { label: '策略 ID', value: strategy.strategy_id, mono: true },
              { label: '版本', value: `v${strategy.version}`, mono: true },
              { label: '分类', value: strategyCategoryLabel(strategy.category) },
              { label: '需要训练', value: strategy.requires_training ? '是' : '否' },
              { label: '组合信号模式', value: strategy.portfolio_signal_mode, mono: true },
              ...(strategy.integration_method
                ? [{ label: '整合方式', value: strategy.integration_method, mono: true }]
                : []),
              ...(strategy.tags.length > 0
                ? [{ label: '标签', value: strategy.tags.join('、'), span: 2 as const }]
                : []),
            ]}
          />
        </Card>

        {execution && (
          <Card title="默认执行口径" description="回测撮合的默认成本与约束">
            <DescriptionList
              columns={1}
              items={[
                { label: '初始资金', value: `¥${execution.initial_capital.toLocaleString('zh-CN')}`, mono: true },
                { label: '佣金费率', value: `${(execution.commission_rate * 100).toFixed(3)}%`, mono: true },
                { label: '滑点费率', value: `${(execution.slippage_rate * 100).toFixed(3)}%`, mono: true },
                { label: '印花税费率', value: `${(execution.stamp_duty_rate * 100).toFixed(3)}%`, mono: true },
                { label: '最低佣金', value: `¥${execution.min_commission}`, mono: true },
                {
                  label: '成交量参与率',
                  value: execution.volume_participation !== null ? `${(execution.volume_participation * 100).toFixed(0)}%` : '未设置',
                  mono: true,
                },
              ]}
            />
          </Card>
        )}
      </div>

      <Card className="mt-4" title="策略原理">
        <p className="whitespace-pre-wrap text-sm leading-6 text-ink-700">
          {strategy.description || '暂无描述'}
        </p>
      </Card>

      <Card className="mt-4" title={`参数列表（${strategy.params.length} 个参数）`} padding="none">
        {strategy.params.length === 0 ? (
          <p className="px-4 py-8 text-center text-sm text-ink-400">该策略无参数</p>
        ) : (
          <Table
            columns={[
              {
                key: 'name',
                header: '参数名',
                render: (param) => (
                  <span className="font-mono text-[13px] font-medium text-ink-800">
                    {param.name}
                    {param.required && <span className="ml-0.5 text-danger-fg" title="必填">*</span>}
                  </span>
                ),
              },
              { key: 'type', header: '类型', render: (param) => <Badge variant="default" size="sm">{param.type}</Badge> },
              {
                key: 'default',
                header: '默认值',
                render: (param) => (
                  <span className="font-mono text-xs text-ink-600">{JSON.stringify(param.default)}</span>
                ),
              },
              {
                key: 'range',
                header: '取值范围',
                render: (param) => (
                  <span className="tnum text-xs text-ink-500">
                    {param.choices
                      ? param.choices.join(' / ')
                      : param.min != null || param.max != null
                        ? `${param.min ?? '-∞'} ~ ${param.max ?? '+∞'}`
                        : '-'}
                  </span>
                ),
              },
              { key: 'description', header: '说明', render: (param) => <span className="text-ink-600">{param.description}</span> },
            ]}
            data={strategy.params}
            keyField="name"
            caption="策略参数契约"
            minWidth="760px"
          />
        )}
      </Card>

      {strategy.sub_strategies.length > 0 && (
        <Card
          className="mt-4"
          title="子策略列表"
          description={`整合方式：${strategy.integration_method || '未指定'}`}
          padding="none"
        >
          <ul className="divide-y divide-ink-100">
            {strategy.sub_strategies.map((sub, index) => (
              <li key={`${sub.strategy_id}-${index}`} className="flex flex-wrap items-center gap-3 px-4 py-3 sm:px-5">
                <span className="tnum w-6 text-sm text-ink-400">#{index + 1}</span>
                <span className="font-mono text-sm text-ink-800">{sub.strategy_id}</span>
                <Badge variant="info" size="sm">{sub.role}</Badge>
                {sub.params_override && Object.keys(sub.params_override).length > 0 && (
                  <span className="text-xs text-ink-400">
                    参数覆盖：{Object.keys(sub.params_override).join(', ')}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card
        className="mt-4"
        title="星标实验"
        description="人工标记为该策略代表性结果的实验"
        padding="none"
        actions={
          <Button variant="ghost" size="sm" onClick={() => navigate(`/experiment/new?strategy_id=${strategy.strategy_id}`)}>
            新建实验
            <Icon name="arrowRight" className="h-4 w-4" />
          </Button>
        }
      >
        <Table
          columns={experimentColumns}
          data={experiments}
          keyField="id"
          emptyMessage="暂无星标实验"
          onRowClick={(experiment) => navigate(`/experiment/${experiment.id}`)}
          caption="星标实验"
        />
      </Card>
    </div>
  );
}
