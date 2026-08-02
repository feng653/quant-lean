import { Component, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useNavigate, useParams } from 'react-router';
import {
  createParameterPreset,
  downloadExperimentEvidence,
  getEquityCurve,
  getExperiment,
  getExperimentMetrics,
  getExperimentModels,
  getTradeLog,
  toggleStar,
} from '../../services/experiments';
import { createDeployment, listPortfolios } from '../../services/trading';
import { analyzeBacktest, diagnoseError } from '../../services/ai';
import type { EquityPoint, Experiment, ExperimentMetrics, ModelArtifact, Trade } from '../../types/experiment';
import type { Portfolio } from '../../types/trading';
import { useAuthStore } from '../../store/authStore';
import { AiAnalysisCard, AiDiagnosisCard } from '../../components/ai';
import { RemoteTrainingCard } from '../../components/remoteTraining';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import DescriptionList from '../../components/shared/DescriptionList';
import EChart from '../../components/shared/EChart';
import EmptyState from '../../components/shared/EmptyState';
import Icon from '../../components/shared/Icon';
import Input from '../../components/shared/Input';
import Modal from '../../components/shared/Modal';
import PageHeader from '../../components/shared/PageHeader';
import Pagination from '../../components/shared/Pagination';
import ProgressBar from '../../components/shared/ProgressBar';
import Skeleton from '../../components/shared/Skeleton';
import StatusTag from '../../components/shared/StatusTag';
import Table from '../../components/shared/Table';
import type { Column } from '../../components/shared/Table';
import Tabs, { TabPanel } from '../../components/shared/Tabs';
import {
  EXPERIMENT_POLL_INTERVAL_MS,
  shouldPollExperiment,
} from './experimentPolling';
import {
  baseGrid,
  baseLegend,
  baseTooltip,
  baseXAxis,
  baseYAxis,
  CHART_COLORS,
  formatCny,
  formatPct,
} from '../../components/shared/chartTheme';
import { formatBackendDateTime } from '../../utils/datetime';

const STATUS_TAG: Record<string, { label: string; variant: 'queued' | 'running' | 'verified' | 'error' | 'neutral' }> = {
  pending: { label: '等待中', variant: 'queued' },
  running: { label: '运行中', variant: 'running' },
  completed: { label: '已完成', variant: 'verified' },
  failed: { label: '失败', variant: 'error' },
  cancelled: { label: '已取消', variant: 'neutral' },
};

const PCT_KEYS = new Set([
  'cumulative_return', 'annualized_return', 'annual_return', 'max_drawdown', 'win_rate',
  'annualized_volatility', 'turnover_rate', 'positive_day_ratio', 'capital_utilization',
  'target_weight_deviation', 'contribution_return', 'risk_contribution', 'benchmark_return',
]);
const MONEY_KEYS = new Set(['transaction_cost', 'contribution_pnl']);
const INT_KEYS = new Set(['total_trades', 'trading_days']);

const METRIC_GROUPS: Array<{ key: string; label: string; metrics: Array<{ key: string; label: string }> }> = [
  {
    key: 'return',
    label: '收益类',
    metrics: [
      { key: 'cumulative_return', label: '累计收益' },
      { key: 'annual_return', label: '年化收益' },
      { key: 'annualized_return', label: '年化收益（年化口径）' },
      { key: 'benchmark_return', label: '基准收益' },
    ],
  },
  {
    key: 'risk',
    label: '风险类',
    metrics: [
      { key: 'max_drawdown', label: '最大回撤' },
      { key: 'annualized_volatility', label: '年化波动' },
    ],
  },
  {
    key: 'ratio',
    label: '比率类',
    metrics: [
      { key: 'sharpe_ratio', label: 'Sharpe' },
      { key: 'sortino_ratio', label: 'Sortino' },
      { key: 'calmar_ratio', label: 'Calmar' },
      { key: 'win_rate', label: '胜率' },
      { key: 'profit_loss_ratio', label: '盈亏比' },
      { key: 'profit_factor', label: 'Profit Factor' },
    ],
  },
  {
    key: 'trade',
    label: '交易类',
    metrics: [
      { key: 'total_trades', label: '总交易数' },
      { key: 'turnover_rate', label: '换手率' },
      { key: 'transaction_cost', label: '交易成本' },
    ],
  },
];

/**
 * Missing metrics stay unavailable and are rendered as "-"; they are never
 * estimated in the browser.
 */
function normalizeMetrics(raw: ExperimentMetrics | null): Record<string, number> {
  if (!raw) return {};
  const result: Record<string, number> = {};
  for (const [key, value] of Object.entries(raw)) {
    const num = typeof value === 'number' ? value : Number(value);
    result[key] = Number.isFinite(num) ? num : Number.NaN;
  }
  return result;
}

function formatMetric(key: string, value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value)) return '-';
  if (PCT_KEYS.has(key)) return formatPct(value);
  if (MONEY_KEYS.has(key)) return formatCny(value);
  if (INT_KEYS.has(key)) return Math.round(value).toLocaleString('zh-CN');
  return value.toFixed(2);
}

/** Isolate legacy chart payloads: a chart crash must not take down the page. */
class EquityChartBoundary extends Component<{ children: ReactNode }, { hasError: boolean }> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex h-72 items-center justify-center text-sm text-ink-400">
          该实验的图表暂时无法显示
        </div>
      );
    }
    return this.props.children;
  }
}

export default function ExperimentDetailPage() {
  const { id } = useParams<{ id: string }>();
  const numId = Number(id);
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const canCreateExperiment = user?.is_admin || user?.permissions.includes('experiments:create');
  const canDeploy = user?.is_admin || user?.permissions.includes('trading:deploy');
  const canUseAi = user?.is_admin || user?.permissions.includes('ai:use');

  const [experiment, setExperiment] = useState<Experiment | null>(null);
  const [metrics, setMetrics] = useState<Record<string, number>>({});
  const [equityCurve, setEquityCurve] = useState<EquityPoint[]>([]);
  const [models, setModels] = useState<ModelArtifact[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [tradesTotal, setTradesTotal] = useState(0);
  const [tradesPage, setTradesPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pollingStopped, setPollingStopped] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [metricTab, setMetricTab] = useState('return');
  const [presetMessage, setPresetMessage] = useState<string | null>(null);
  const [exportMessage, setExportMessage] = useState<{
    variant: 'info' | 'danger';
    text: string;
  } | null>(null);
  const [exporting, setExporting] = useState<'json' | 'csv' | null>(null);
  const [deployOpen, setDeployOpen] = useState(false);

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    let attempts = 0;

    if (!Number.isInteger(numId) || numId <= 0) {
      setError('实验不存在');
      setLoading(false);
      return () => {
        disposed = true;
      };
    }
    setLoading(true);
    setError(null);
    setPollingStopped(false);
    setModelsError(null);

    const loadTerminalArtifacts = async (result: Experiment) => {
      const [metricResult, equityResult, modelResult] = await Promise.allSettled([
        getExperimentMetrics(numId),
        getEquityCurve(numId),
        result.requires_training
          ? getExperimentModels(numId)
          : Promise.resolve([] as ModelArtifact[]),
      ]);
      if (disposed) return;
      setMetrics(
        metricResult.status === 'fulfilled'
          ? normalizeMetrics(metricResult.value)
          : {},
      );
      setEquityCurve(
        equityResult.status === 'fulfilled' ? equityResult.value : [],
      );
      if (modelResult.status === 'fulfilled') {
        setModels(modelResult.value);
      } else {
        setModels([]);
        setModelsError(
          modelResult.reason instanceof Error
            ? modelResult.reason.message
            : '模型产物加载失败',
        );
      }
    };

    const poll = async () => {
      attempts += 1;
      try {
        const result = await getExperiment(numId);
        if (disposed) return;
        setExperiment(result);
        setError(null);
        setLoading(false);
        if (shouldPollExperiment(result.status, attempts)) {
          timer = window.setTimeout(() => void poll(), EXPERIMENT_POLL_INTERVAL_MS);
          return;
        }
        if (result.status === 'pending' || result.status === 'running') {
          setPollingStopped(true);
          return;
        }
        await loadTerminalArtifacts(result);
      } catch (err: unknown) {
        if (disposed) return;
        setError(err instanceof Error ? err.message : '获取实验详情失败');
        setLoading(false);
      }
    };

    void poll();
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [numId, reloadKey]);

  const experimentStatus = experiment?.status;
  useEffect(() => {
    if (!Number.isInteger(numId) || numId <= 0 || experimentStatus === undefined) {
      return;
    }
    let disposed = false;
    void getTradeLog(numId, tradesPage, 50)
      .then((result) => {
        if (disposed) return;
        setTrades(result.items);
        setTradesTotal(result.total);
      })
      .catch(() => {
        if (disposed) return;
        setTrades([]);
        setTradesTotal(0);
      });
    return () => {
      disposed = true;
    };
  }, [experimentStatus, numId, tradesPage]);

  const handleToggleStar = async () => {
    if (!experiment) return;
    try {
      await toggleStar(experiment.id, !experiment.is_starred);
      setExperiment({ ...experiment, is_starred: !experiment.is_starred });
    } catch {
      // Star toggling is non-critical.
    }
  };

  const saveAsPreset = async () => {
    if (!experiment) return;
    const presetName = window.prompt('请输入参数方案名称', `${experiment.name} - 优选参数`);
    if (!presetName) return;
    try {
      await createParameterPreset({
        name: presetName,
        strategy_id: experiment.strategy_id,
        params: experiment.params,
        mode: experiment.mode,
        pool_preset: experiment.pool_preset ?? 'csi300',
        pool_custom_codes: experiment.pool_custom_codes,
        pool_industries: experiment.pool_industries,
        source_experiment_id: experiment.id,
        metrics_snapshot: experiment.status === 'completed' ? { ...metrics } : {},
      });
      setPresetMessage('参数方案已保存，可在新建实验时直接套用。');
    } catch (err: unknown) {
      setPresetMessage(err instanceof Error ? err.message : '保存参数方案失败');
    }
  };

  const exportEvidence = async (format: 'json' | 'csv') => {
    if (!experiment || experiment.status !== 'completed') return;
    setExporting(format);
    setExportMessage(null);
    try {
      const filename = await downloadExperimentEvidence(
        experiment.id,
        format,
      );
      setExportMessage({
        variant: 'info',
        text: `研究证据已导出：${filename}`,
      });
    } catch (err: unknown) {
      setExportMessage({
        variant: 'danger',
        text: err instanceof Error ? err.message : '研究证据导出失败',
      });
    } finally {
      setExporting(null);
    }
  };

  const chartOption = useMemo(() => {
    const dates = equityCurve.map((point) => point.date);
    return {
      color: [CHART_COLORS.accent, CHART_COLORS.ink, CHART_COLORS.danger],
      grid: baseGrid(),
      legend: baseLegend(),
      tooltip: baseTooltip(),
      xAxis: baseXAxis({ data: dates }),
      yAxis: [
        baseYAxis({ name: '净值', nameTextStyle: { color: CHART_COLORS.axisLabel } }),
        baseYAxis({
          name: '回撤',
          nameTextStyle: { color: CHART_COLORS.axisLabel },
          axisLabel: {
            color: CHART_COLORS.axisLabel,
            fontSize: 11,
            formatter: (value: number) => `${(value * 100).toFixed(0)}%`,
          },
          splitLine: { show: false },
        }),
      ],
      series: [
        {
          name: '净值',
          type: 'line',
          data: equityCurve.map((point) => point.equity),
          showSymbol: false,
          lineStyle: { width: 2 },
        },
        {
          name: '基准',
          type: 'line',
          data: equityCurve.map((point) => point.benchmark_equity ?? point.benchmark ?? null),
          showSymbol: false,
          lineStyle: { width: 1.5, type: 'dashed' },
        },
        {
          name: '回撤',
          type: 'line',
          yAxisIndex: 1,
          data: equityCurve.map((point) => point.drawdown),
          showSymbol: false,
          lineStyle: { width: 1 },
          areaStyle: { opacity: 0.12 },
        },
      ],
    };
  }, [equityCurve]);

  if (loading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-72" />
        <Skeleton className="h-44 w-full" />
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }

  if (error || !experiment) {
    return (
      <div>
        <PageHeader title="实验详情" breadcrumb={[{ label: '研究' }, { label: '实验中心', to: '/experiment' }, { label: `#${id}` }]} />
        <Banner
          variant="danger"
          title={error ?? '实验不存在'}
          action={
            <Button
              variant="secondary"
              size="sm"
              onClick={() => (
                error
                  ? setReloadKey((current) => current + 1)
                  : navigate('/experiment')
              )}
            >
              {error ? '重试' : '返回列表'}
            </Button>
          }
        >
          {error ? '加载失败，请重试。' : '该实验可能已被删除，或当前账号无权查看。'}
        </Banner>
      </div>
    );
  }

  const statusSpec = STATUS_TAG[experiment.status] ?? { label: experiment.status, variant: 'neutral' as const };

  const tradeColumns: Column<Trade>[] = [
    {
      key: 'signal_date',
      header: '信号日（T）',
      render: (trade) => <span className="tnum text-ink-500">{trade.signal_date ?? '-'}</span>,
    },
    {
      key: 'date',
      header: '成交日（T+1）',
      render: (trade) => <span className="tnum">{trade.date}</span>,
    },
    { key: 'code', header: '代码', render: (trade) => <span className="font-mono text-xs">{trade.code}</span> },
    {
      key: 'action',
      header: '方向',
      render: (trade) => (
        <span className={`text-xs font-semibold ${trade.action === 'BUY' ? 'text-rise' : 'text-fall'}`}>
          {trade.action === 'BUY' ? '买入' : '卖出'}
        </span>
      ),
    },
    { key: 'price', header: '价格', numeric: true, render: (trade) => trade.price.toFixed(2) },
    { key: 'shares', header: '数量', numeric: true, render: (trade) => trade.shares.toLocaleString('zh-CN') },
    { key: 'amount', header: '金额', numeric: true, render: (trade) => formatCny(trade.amount) },
    { key: 'cost', header: '成本', numeric: true, render: (trade) => formatCny(trade.cost) },
    {
      key: 'signal_strategy',
      header: '信号来源',
      render: (trade) => <span className="text-xs text-ink-500">{trade.signal_strategy ?? '-'}</span>,
    },
  ];

  return (
    <div>
      <PageHeader
        title={
          <span className="flex items-center gap-2">
            {experiment.name}
            <button
              type="button"
              aria-label={experiment.is_starred ? '取消星标' : '星标实验'}
              aria-pressed={experiment.is_starred}
              onClick={() => void handleToggleStar()}
              className={`rounded p-1 transition-colors ${
                experiment.is_starred ? 'text-warn-fg hover:text-warn-strong' : 'text-ink-300 hover:text-ink-500'
              }`}
            >
              <Icon name={experiment.is_starred ? 'starFilled' : 'star'} className="h-5 w-5" />
            </button>
          </span>
        }
        breadcrumb={[{ label: '研究' }, { label: '实验中心', to: '/experiment' }, { label: `#${experiment.id}` }]}
        description={`实验 #${experiment.id} · 创建于 ${formatBackendDateTime(experiment.created_at)}`}
        tags={
          <>
            <StatusTag variant={statusSpec.variant}>{statusSpec.label}</StatusTag>
            <StatusTag variant="unverified" title="历史实验结果未经过实盘准入验证">
              未验证研究证据
            </StatusTag>
          </>
        }
        actions={
          <>
            <Button
              variant="secondary"
              size="sm"
              loading={exporting === 'json'}
              disabled={
                experiment.status !== 'completed' || exporting !== null
              }
              onClick={() => void exportEvidence('json')}
            >
              <Icon name="download" className="h-4 w-4" />
              导出证据 JSON
            </Button>
            <Button
              variant="secondary"
              size="sm"
              loading={exporting === 'csv'}
              disabled={
                experiment.status !== 'completed' || exporting !== null
              }
              onClick={() => void exportEvidence('csv')}
            >
              <Icon name="download" className="h-4 w-4" />
              导出证据 CSV ZIP
            </Button>
            {canCreateExperiment && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => navigate(`/experiment/new?from_experiment=${experiment.id}`)}
              >
                <Icon name="copy" className="h-4 w-4" />
                继承配置，新建实验
              </Button>
            )}
            {canCreateExperiment && (
              <Button
                variant="secondary"
                size="sm"
                disabled={experiment.status !== 'completed'}
                onClick={() => void saveAsPreset()}
              >
                <Icon name="presets" className="h-4 w-4" />
                保存为参数方案
              </Button>
            )}
            {canDeploy && (
              <Button
                size="sm"
                disabled={experiment.status !== 'completed'}
                onClick={() => setDeployOpen(true)}
              >
                <Icon name="trading" className="h-4 w-4" />
                部署到模拟盘
              </Button>
            )}
          </>
        }
      />

      {presetMessage && (
        <Banner variant="info" className="mb-4">
          {presetMessage}
        </Banner>
      )}

      {exportMessage && (
        <Banner variant={exportMessage.variant} className="mb-4">
          {exportMessage.text}
        </Banner>
      )}

      {pollingStopped && (
        <Banner
          variant="warning"
          className="mb-4"
          title="自动刷新已达到安全上限"
          action={
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setReloadKey((current) => current + 1)}
            >
              继续刷新
            </Button>
          }
        >
          实验仍未结束。为避免页面长期产生后台请求，自动轮询已暂停。
        </Banner>
      )}

      {experiment.status === 'running' && (
        <Card className="mb-4" padding="md">
          <div className="flex items-center gap-4">
            <ProgressBar
              value={experiment.progress_pct}
              label="实验运行进度"
              className="flex-1"
            />
            <p className="min-w-0 flex-1 truncate text-sm text-ink-500">
              {experiment.progress_message || '运行中'}
            </p>
          </div>
        </Card>
      )}

      {/* Configuration */}
      <Card title="实验配置" className="mb-4">
        <DescriptionList
          columns={3}
          items={[
            { label: '策略', value: <span className="font-mono text-[13px]">{experiment.strategy_id}</span> },
            { label: '策略分类', value: experiment.strategy_category },
            { label: '执行模式', value: experiment.mode, mono: true },
            {
              label: '数据访问',
              value: experiment.data_access_policy === 'cache_only'
                ? '仅本地缓存（不联网）'
                : '允许按需更新',
            },
            {
              label: '研究信任层',
              value: experiment.research_trust?.profile === 'tushare_research_trusted'
                ? 'Tushare 条件研究/模拟（高风险警告；不可实盘）'
                : '严格治理 PIT',
            },
            { label: '股票池', value: experiment.pool_preset ?? '自定义' },
            {
              label: '自定义代码',
              value: experiment.pool_custom_codes.length > 0 ? `${experiment.pool_custom_codes.length} 只` : '-',
            },
            {
              label: '行业筛选',
              value: experiment.pool_industries.length > 0 ? experiment.pool_industries.join('、') : '全部行业',
            },
            ...(experiment.train_start && experiment.train_end
              ? [{ label: '训练区间', value: `${experiment.train_start} ~ ${experiment.train_end}`, mono: true }]
              : []),
            { label: '测试区间', value: `${experiment.test_start} ~ ${experiment.test_end}`, mono: true },
            { label: '参数哈希', value: experiment.params_hash || '-', mono: true },
            ...(experiment.source_experiment_id
              ? [{
                  label: '来源实验',
                  value: (
                    <button
                      type="button"
                      className="text-accent-700 hover:underline"
                      onClick={() => navigate(`/experiment/${experiment.source_experiment_id}`)}
                    >
                      实验 #{experiment.source_experiment_id}
                    </button>
                  ),
                }]
              : []),
          ]}
        />
        {Object.keys(experiment.params).length > 0 && (
          <div className="mt-4 border-t border-ink-100 pt-3">
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-400">参数快照</p>
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(experiment.params).map(([key, value]) => (
                <span
                  key={key}
                  className="rounded-sm border border-ink-200 bg-ink-50 px-2 py-0.5 font-mono text-2xs text-ink-600"
                >
                  {key}={JSON.stringify(value)}
                </span>
              ))}
            </div>
          </div>
        )}
        {experiment.research_trust?.profile === 'tushare_research_trusted' && (
          <Banner variant="warning" className="mt-4" title="Tushare 条件数据：高等级警告">
            本结果可以用于个人研究和模拟观察，但月内调样时点、历史 available_at/revision、
            官方事件对账和生产双价格账本尚未认证。警告已写入不可变研究清单；本实验及其模拟部署均不具备实盘资格。
          </Banner>
        )}
      </Card>

      {/* Failure */}
      {experiment.status === 'failed' && (
        <Card title="错误信息" className="mb-4">
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-ink-50 p-3 font-mono text-xs leading-5 text-danger-strong scrollbar-thin">
            {experiment.error_log || '无错误日志'}
          </pre>
          {canUseAi && experiment.error_log && (
            <div className="mt-4">
              <AiDiagnosisCard
                onDiagnose={() => diagnoseError(experiment.id, experiment.error_log ?? '')}
                initialResult={
                  experiment.ai_diagnosis
                    ? {
                        experiment_id: experiment.id,
                        diagnosis: experiment.ai_diagnosis,
                        evidence: [],
                        fix_suggestions: [],
                        cached: true,
                      }
                    : undefined
                }
                scopeKey={`experiment:${experiment.id}`}
              />
            </div>
          )}
        </Card>
      )}

      {/* Results */}
      {experiment.status === 'completed' && (
        <>
          <Card title="净值走势" className="mb-4" padding="sm">
            <EquityChartBoundary>
              {equityCurve.length === 0 ? (
                <EmptyState icon="chart" title="暂无净值数据" />
              ) : (
                <EChart option={chartOption} style={{ height: 320 }} notMerge />
              )}
            </EquityChartBoundary>
          </Card>

          <Card title="回测指标" className="mb-4" padding="none">
            <div className="px-4 pt-3 sm:px-5">
              <Tabs
                tabs={METRIC_GROUPS.map((group) => ({ key: group.key, label: group.label }))}
                active={metricTab}
                onChange={setMetricTab}
                ariaLabel="指标分类"
              />
            </div>
            <div className="p-4 sm:p-5">
              {METRIC_GROUPS.map((group) => (
                <TabPanel key={group.key} tabKey={group.key} active={metricTab}>
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                    {group.metrics.map((metric) => (
                      <div key={metric.key} className="rounded border border-ink-100 p-3">
                        <p className="text-xs text-ink-400">{metric.label}</p>
                        <p className="tnum mt-1 text-lg font-semibold">
                          {formatMetric(metric.key, metrics[metric.key])}
                        </p>
                      </div>
                    ))}
                  </div>
                  <p className="mt-3 text-xs text-ink-400">
                    缺失或无法计算的指标显示为 “-”，不会在浏览器端估算补齐。
                  </p>
                </TabPanel>
              ))}
            </div>
          </Card>

          <Card title="成交记录" className="mb-4" padding="none">
            <Table
              columns={tradeColumns}
              data={trades}
              keyField="id"
              emptyMessage="暂无成交记录"
              caption="回测成交记录"
              minWidth="960px"
            />
            <div className="border-t border-ink-100 px-4 py-3">
              <Pagination page={tradesPage} total={tradesTotal} limit={50} onChange={setTradesPage} />
            </div>
          </Card>

          {canUseAi && (
            <div className="mb-4">
              <AiAnalysisCard
                onAnalyze={() => analyzeBacktest(experiment.id)}
                scopeKey={`experiment:${experiment.id}`}
              />
            </div>
          )}
        </>
      )}

      {/* Model artifacts */}
      {experiment.requires_training && experiment.status === 'completed' && (
        <ModelSection models={models} modelsError={modelsError} />
      )}

      {/* Remote training */}
      {experiment.requires_training && (
        <div className="mb-4">
          <RemoteTrainingCard
            experimentId={experiment.id}
            trainStart={experiment.train_start ?? undefined}
            trainEnd={experiment.train_end ?? undefined}
            canManage={canCreateExperiment}
          />
        </div>
      )}

      {/* Deploy modal */}
      <DeployDialog
        experiment={experiment}
        isOpen={deployOpen}
        onClose={() => setDeployOpen(false)}
      />
    </div>
  );
}

/* ── Model artifacts section ─────────────────────────────────────────────── */

function ModelSection({ models, modelsError }: { models: ModelArtifact[]; modelsError: string | null }) {
  if (modelsError) {
    return (
      <Card title="模型产物" className="mb-4">
        <Banner variant="danger">{`模型产物加载失败：${modelsError}`}</Banner>
      </Card>
    );
  }
  if (models.length === 0) {
    return (
      <Card title="模型产物" className="mb-4">
        <p className="text-sm text-ink-500">
          暂无模型产物记录；旧实验需要重新运行后才能展示真实训练信息。
        </p>
      </Card>
    );
  }
  const latest = models.find((model) => model.is_latest) ?? models[0];
  const trainMetrics = latest.train_metrics ?? {};
  const cycles = trainMetrics.cycles ?? [];

  return (
    <Card
      title="模型产物"
      description={`模型版本 v${latest.model_version} · 参数哈希 ${latest.params_hash || '-'}`}
      className="mb-4"
      padding="md"
    >
      <DescriptionList
        columns={4}
        items={[
          {
            label: '平台保留验证窗口（未参与拟合）',
            value: trainMetrics.last_validation_window
              ? `${trainMetrics.last_validation_window[0]} ~ ${trainMetrics.last_validation_window[1]}`
              : '-',
            mono: true,
          },
          {
            label: '最后训练窗口',
            value: trainMetrics.last_training_window
              ? `${trainMetrics.last_training_window[0]} ~ ${trainMetrics.last_training_window[1]}`
              : '-',
            mono: true,
          },
          { label: '实际训练次数', value: trainMetrics.retrain_count ?? '-', mono: true },
          {
            label: '总训练耗时',
            value: trainMetrics.elapsed_seconds !== null && trainMetrics.elapsed_seconds !== undefined
              ? `${trainMetrics.elapsed_seconds.toFixed(1)} 秒`
              : '-',
            mono: true,
          },
          { label: '最终训练样本', value: latest.train_samples ?? '-', mono: true },
          { label: '累计拟合样本', value: trainMetrics.total_fit_samples ?? '-', mono: true },
          { label: '特征数', value: latest.feature_count ?? '-', mono: true },
          {
            label: '训练窗口',
            value: latest.train_window_start && latest.train_window_end
              ? `${latest.train_window_start} ~ ${latest.train_window_end}`
              : '-',
            mono: true,
          },
        ]}
      />
      {trainMetrics.summary && (
        <p className="mt-3 rounded bg-ink-50 p-3 text-sm leading-6 text-ink-600">{trainMetrics.summary}</p>
      )}
      {cycles.length > 0 && (
        <div className="mt-4 overflow-x-auto scrollbar-thin">
          <table className="w-full text-sm" style={{ minWidth: 860 }}>
            <caption className="sr-only">训练周期明细</caption>
            <thead>
              <tr className="border-b border-ink-200 bg-ink-50">
                {['预测月', '训练窗口', '平台保留验证窗口', '样本/特征', '耗时', '状态'].map((header) => (
                  <th key={header} scope="col" className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-ink-500">
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-ink-100">
              {cycles.map((cycle, index) => (
                <tr key={`${cycle.pred_month}-${index}`}>
                  <td className="tnum px-3 py-2">{cycle.pred_month}</td>
                  <td className="tnum px-3 py-2 text-xs text-ink-600">
                    {cycle.train_start && cycle.train_end ? `${cycle.train_start} ~ ${cycle.train_end}` : '-'}
                  </td>
                  <td className="tnum px-3 py-2 text-xs text-ink-600">
                    {cycle.validation_start && cycle.validation_end
                      ? `${cycle.validation_start} ~ ${cycle.validation_end}`
                      : '-'}
                  </td>
                  <td className="tnum px-3 py-2 text-xs text-ink-600">
                    {cycle.n_train_samples ?? '-'} / {cycle.n_train_features ?? '-'}
                  </td>
                  <td className="tnum px-3 py-2 text-xs text-ink-600">
                    {Number.isFinite(cycle.fit_seconds) ? `${cycle.fit_seconds.toFixed(1)} 秒` : '-'}
                  </td>
                  <td className="px-3 py-2">
                    {cycle.error ? (
                      <StatusTag variant="error" title={cycle.error}>失败</StatusTag>
                    ) : cycle.retrained ? (
                      <StatusTag variant="verified">已训练</StatusTag>
                    ) : (
                      <StatusTag variant="neutral">复用</StatusTag>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

/* ── Deploy dialog (2 steps, immutable paper-risk binding) ──────────────── */

function DeployDialog({
  experiment,
  isOpen,
  onClose,
}: {
  experiment: Experiment;
  isOpen: boolean;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [portfolios, setPortfolios] = useState<Portfolio[]>([]);
  const [loadingPortfolios, setLoadingPortfolios] = useState(false);
  const [step, setStep] = useState(1);
  const [selectedPortfolioId, setSelectedPortfolioId] = useState<number | null>(null);
  const [weightPct, setWeightPct] = useState('');
  const [promotionId, setPromotionId] = useState('');
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!isOpen) return;
    setStep(1);
    setSelectedPortfolioId(null);
    setWeightPct('');
    setPromotionId('');
    setFormError(null);
    setLoadingPortfolios(true);
    void listPortfolios()
      .then((result) => setPortfolios(result.filter((item) => (item.status ?? 'active') === 'active')))
      .catch(() => setPortfolios([]))
      .finally(() => setLoadingPortfolios(false));
  }, [isOpen]);

  const remainingWeightBps = (portfolio: Portfolio): number =>
    10_000 - (portfolio.allocations ?? []).reduce((sum, item) => sum + (item.target_weight_bps || 0), 0);

  const selectedPortfolio = portfolios.find((item) => item.id === selectedPortfolioId) ?? null;
  const selectedRemainingBps = selectedPortfolio ? remainingWeightBps(selectedPortfolio) : 0;

  const validateStep1 = (): boolean => {
    if (!selectedPortfolio) {
      setFormError('请选择一个目标模拟盘');
      return false;
    }
    const pct = Number(weightPct);
    if (!Number.isFinite(pct) || pct <= 0 || pct * 100 > selectedRemainingBps) {
      setFormError(`目标仓位必须大于 0%，且不能超过可用现金仓位 ${(selectedRemainingBps / 100).toFixed(2)}%`);
      return false;
    }
    if (promotionId.trim()) {
      const parsedPromotionId = Number(promotionId);
      if (!Number.isInteger(parsedPromotionId) || parsedPromotionId < 1) {
        setFormError('研究晋级记录 ID 必须是正整数；也可留空并以高风险告警部署到模拟盘');
        return false;
      }
    }
    setFormError(null);
    return true;
  };

  const handleConfirm = async () => {
    if (!selectedPortfolio) return;
    setSubmitting(true);
    setFormError(null);
    try {
      const result = await createDeployment({
        strategy_id: experiment.strategy_id,
        display_name: experiment.name,
        params: experiment.params,
        mode: experiment.mode,
        source_experiment_id: experiment.id,
        ...(promotionId.trim()
          ? { research_promotion_id: Number(promotionId) }
          : {}),
        portfolio_id: selectedPortfolio.id,
        target_weight_bps: Math.round(Number(weightPct) * 100),
      });
      const portfolioId = result.portfolio_id ?? selectedPortfolio.id;
      onClose();
      navigate(`/trading/portfolio/${portfolioId}/overview?from_experiment=${experiment.id}`);
    } catch (err: unknown) {
      setFormError(err instanceof Error ? err.message : '创建部署失败');
      setSubmitting(false);
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={step === 1 ? '部署到模拟盘' : '确认部署'}
      size="lg"
      footer={
        step === 1 ? (
          <>
            <Button variant="ghost" onClick={onClose}>取消</Button>
            <Button onClick={() => { if (validateStep1()) setStep(2); }}>下一步</Button>
          </>
        ) : (
          <>
            <Button variant="ghost" onClick={() => setStep(1)}>上一步</Button>
            <Button onClick={() => void handleConfirm()} loading={submitting}>确认部署</Button>
          </>
        )
      }
    >
      {step === 1 ? (
        <div className="space-y-4">
          <p className="text-sm leading-6 text-ink-500">
            本次只会把“{experiment.name}”加入你选择的一个模拟盘，其他模拟盘不会变化。
          </p>
          {loadingPortfolios ? (
            <Skeleton lines={3} className="h-12 w-full" />
          ) : portfolios.length === 0 ? (
            <div className="rounded border border-ink-200 p-4 text-center">
              <p className="text-sm text-ink-500">还没有可用的模拟盘组合。</p>
              <Button
                variant="secondary"
                size="sm"
                className="mt-3"
                onClick={() => { onClose(); navigate('/trading'); }}
              >
                先创建模拟盘
              </Button>
            </div>
          ) : (
            <div role="radiogroup" aria-label="目标模拟盘" className="space-y-2">
              {portfolios.map((portfolio) => {
                const remaining = remainingWeightBps(portfolio);
                const disabled = remaining === 0;
                return (
                  <button
                    type="button"
                    role="radio"
                    aria-checked={selectedPortfolioId === portfolio.id}
                    key={portfolio.id}
                    disabled={disabled}
                    onClick={() => setSelectedPortfolioId(portfolio.id)}
                    className={`flex w-full items-center justify-between rounded border px-3.5 py-2.5 text-left text-sm transition-colors ${
                      selectedPortfolioId === portfolio.id
                        ? 'border-accent-700 bg-accent-50'
                        : 'border-ink-200 hover:border-accent-400'
                    } disabled:cursor-not-allowed disabled:opacity-50`}
                  >
                    <span className="font-medium text-ink-800">{portfolio.name}</span>
                    <span className="tnum text-xs text-ink-500">
                      可用现金仓位 {(remaining / 100).toFixed(2)}%
                    </span>
                  </button>
                );
              })}
            </div>
          )}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Input
              label="新策略目标仓位（%）"
              type="number"
              min={0.01}
              step={0.01}
              value={weightPct}
              onChange={(event) => setWeightPct(event.target.value)}
              hint={selectedPortfolio ? `可用上限 ${(selectedRemainingBps / 100).toFixed(2)}%` : '先选择模拟盘'}
            />
            <Input
              label="研究晋级记录 ID（可选）"
              type="number"
              min={1}
              step={1}
              value={promotionId}
              onChange={(event) => setPromotionId(event.target.value)}
              hint="个人模拟盘不强制审批；留空会永久记录高风险告警，未来实盘仍禁止。"
            />
          </div>
          {formError && (
            <Banner variant="danger">{formError}</Banner>
          )}
        </div>
      ) : (
        <div className="space-y-4">
          <DescriptionList
            columns={2}
            items={[
              { label: '本次部署范围', value: selectedPortfolio?.name ?? '-' },
              { label: '实验策略', value: experiment.name },
              { label: '目标仓位', value: `${Number(weightPct).toFixed(2)}%`, mono: true },
              {
                label: '研究晋级',
                value: promotionId.trim() ? `#${promotionId}` : '未审批（高风险告警）',
                mono: Boolean(promotionId.trim()),
              },
              { label: '其他模拟盘', value: '不会变更' },
              { label: '发布方式', value: '立即生成新组合版本' },
            ]}
          />
          <Banner variant="warning">
            确认后，部署记录和所选模拟盘的新版本会原子提交。数据代、来源、窗口、实验告警和审批状态会形成不可静默改写的风险快照；此操作只允许模拟盘，绝不授予实盘资格。
          </Banner>
          {formError && (
            <Banner variant="danger">{formError}</Banner>
          )}
        </div>
      )}
    </Modal>
  );
}
