import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router';
import {
  backfillSimulation,
  createPortfolio,
  createPortfolioDraft,
  getPortfolioNav,
  getPortfolioOverview,
  getSimulationCalendar,
  getSimulationSchedule,
  getSimulationStatus,
  listDeployments,
  listPortfolios,
  listSimulationRuns,
  previewRebalance,
  publishPortfolioDraft,
  triggerSimulation,
  updateDeployment,
  validateAllocations,
} from '../../services/trading';
import type {
  AllocationValidation,
  PortfolioOverview,
  PortfolioStrategyOverview,
  RebalancePreview,
  SimulationCalendar,
  SimulationRun,
  SimulationSchedule,
  SimulationStatus,
} from '../../services/trading';
import { marketInsight } from '../../services/ai';
import { allocateBasisPoints } from '../../utils/allocation';
import type { Deployment, Portfolio, PortfolioAllocation, PortfolioNavPoint } from '../../types/trading';
import { useAuthStore } from '../../store/authStore';
import { AiMarketInsightCard } from '../../components/ai';
import StrategyAnalyticsPanel from './StrategyAnalyticsPanel';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import EChart from '../../components/shared/EChart';
import Icon from '../../components/shared/Icon';
import Input from '../../components/shared/Input';
import Modal from '../../components/shared/Modal';
import PageHeader from '../../components/shared/PageHeader';
import Select from '../../components/shared/Select';
import Skeleton from '../../components/shared/Skeleton';
import StatusTag from '../../components/shared/StatusTag';
import {
  baseGrid,
  baseLegend,
  baseTooltip,
  baseXAxis,
  baseYAxis,
  CHART_COLORS,
  formatCny,
  formatPct,
  formatSignedPct,
  SERIES_PALETTE,
  signedToneClass,
} from '../../components/shared/chartTheme';

const WORKBENCH_SECTIONS = [
  { key: 'overview', label: '组合概览' },
  { key: 'allocation', label: '策略配置' },
  { key: 'analytics', label: '策略分析' },
  { key: 'simulation', label: '模拟运行' },
  { key: 'history', label: '运行记录' },
] as const;

type SectionKey = (typeof WORKBENCH_SECTIONS)[number]['key'];

const SIMULATION_STATUS_LABEL: Record<SimulationStatus['status'], string> = {
  not_started: '尚未运行',
  pending: '排队中',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

interface AllocationRow {
  deployment_id: number;
  display_name: string;
  strategy_id: string;
  target_pct: string;
  min_pct: string;
  max_pct: string;
  risk_budget_pct: string;
  locked: boolean;
}

function pctToBps(value: string): number {
  const num = Number(value);
  return Number.isFinite(num) ? Math.round(num * 100) : 0;
}

function bpsToPct(bps: number): string {
  return (bps / 100).toFixed(2);
}

export default function PortfolioManagerPage() {
  const navigate = useNavigate();
  const params = useParams<{ portfolioId: string; section: string }>();
  const [searchParams] = useSearchParams();
  const fromExperiment = Number(searchParams.get('from_experiment')) || null;

  const user = useAuthStore((s) => s.user);
  const canRebalance = Boolean(user?.is_admin || user?.permissions.includes('trading:rebalance'));
  const canExecute = Boolean(user?.is_admin || user?.permissions.includes('trading:execute'));
  const canUseAi = Boolean(user?.is_admin || user?.permissions.includes('ai:use'));

  const routePortfolioId = Number(params.portfolioId) || null;
  const activeSection: SectionKey = WORKBENCH_SECTIONS.some((item) => item.key === params.section)
    ? (params.section as SectionKey)
    : 'overview';

  const [portfolios, setPortfolios] = useState<Portfolio[]>([]);
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [selected, setSelected] = useState<Portfolio | null>(null);
  const [overview, setOverview] = useState<PortfolioOverview | null>(null);
  const [navPoints, setNavPoints] = useState<PortfolioNavPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('我的模拟组合');
  const [newCapital, setNewCapital] = useState('1000000');
  const [creating, setCreating] = useState(false);

  const [rows, setRows] = useState<AllocationRow[]>([]);
  const [validation, setValidation] = useState<AllocationValidation | null>(null);
  const [preview, setPreview] = useState<RebalancePreview | null>(null);
  const [draftRevision, setDraftRevision] = useState<number | null>(null);
  const [allocationBusy, setAllocationBusy] = useState<string | null>(null);

  const [simulationStatus, setSimulationStatus] = useState<SimulationStatus | null>(null);
  const [simulationRuns, setSimulationRuns] = useState<SimulationRun[]>([]);
  const [schedule, setSchedule] = useState<SimulationSchedule | null>(null);
  const [calendar, setCalendar] = useState<SimulationCalendar | null>(null);
  const [calendarError, setCalendarError] = useState<string | null>(null);
  const [simulationDate, setSimulationDate] = useState('');
  const [replayStart, setReplayStart] = useState('');
  const [replayEnd, setReplayEnd] = useState('');
  const [restartReplay, setRestartReplay] = useState(false);
  const [simulationBusy, setSimulationBusy] = useState(false);

  const routeFor = useCallback((portfolioId: number, section: SectionKey) => {
    const query = searchParams.toString();
    return `/trading/portfolio/${portfolioId}/${section}${query ? `?${query}` : ''}`;
  }, [searchParams]);

  /* ── Load portfolios + deployments ───────────────────────────────────── */
  const loadPortfolios = useCallback(async () => {
    try {
      const [portfolioList, deploymentList] = await Promise.all([listPortfolios(), listDeployments()]);
      setPortfolios(portfolioList);
      setDeployments(deploymentList);
      return portfolioList;
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '加载组合失败');
      return [];
    }
  }, []);

  const loadOverview = useCallback(async (portfolioId: number) => {
    try {
      const [overviewData, navData] = await Promise.all([
        getPortfolioOverview(portfolioId),
        getPortfolioNav(portfolioId).catch(() => [] as PortfolioNavPoint[]),
      ]);
      setOverview(overviewData);
      setNavPoints(navData);
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '加载组合失败');
    }
  }, []);

  const reloadSimulation = useCallback(async (portfolioId: number) => {
    try {
      const [statusData, runsData] = await Promise.all([
        getSimulationStatus(portfolioId),
        listSimulationRuns(10, portfolioId),
      ]);
      setSimulationStatus(statusData);
      setSimulationRuns(runsData);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '加载模拟运行状态失败');
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setLoading(true);
      const portfolioList = await loadPortfolios();
      if (cancelled) return;
      const target = portfolioList.find((item) => item.id === routePortfolioId) ?? portfolioList[0] ?? null;
      setSelected(target);
      if (target) {
        await Promise.all([loadOverview(target.id), reloadSimulation(target.id)]);
      }
      if (!cancelled) setLoading(false);
    })();
    void getSimulationSchedule().then(setSchedule).catch(() => setSchedule(null));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selected) {
      setCalendar(null);
      return;
    }
    void getSimulationCalendar(selected.id)
      .then((data) => {
        setCalendar(data);
        setCalendarError(null);
        setReplayStart(data.suggested_start);
        setReplayEnd(data.max_date);
      })
      .catch((err: unknown) => {
        setCalendar(null);
        setCalendarError(
          err instanceof Error ? err.message : '模拟盘股票池行情尚未就绪，请先在数据中心更新数据',
        );
      });
  }, [selected]);

  /* ── Sync selection with route ───────────────────────────────────────── */
  useEffect(() => {
    if (portfolios.length === 0) return;
    const target = portfolios.find((item) => item.id === routePortfolioId) ?? null;
    if (target && target.id !== selected?.id) {
      setSelected(target);
      setValidation(null);
      setPreview(null);
      setDraftRevision(null);
      void loadOverview(target.id);
      void reloadSimulation(target.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routePortfolioId, portfolios]);

  /* ── Canonicalize URL ────────────────────────────────────────────────── */
  useEffect(() => {
    if (selected && !routePortfolioId) {
      navigate(routeFor(selected.id, activeSection), { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, routePortfolioId]);

  /* ── Editor rows from overview ───────────────────────────────────────── */
  useEffect(() => {
    if (!overview) {
      setRows([]);
      return;
    }
    setRows(
      overview.strategies.map((strategy) => ({
        deployment_id: strategy.deployment_id,
        display_name: strategy.display_name,
        strategy_id: strategy.strategy_id,
        target_pct: bpsToPct(strategy.target_weight_bps),
        min_pct: bpsToPct(
          selected?.allocations.find((item) => item.deployment_id === strategy.deployment_id)?.min_weight_bps ?? 0,
        ),
        max_pct: bpsToPct(
          selected?.allocations.find((item) => item.deployment_id === strategy.deployment_id)?.max_weight_bps ?? 10_000,
        ),
        risk_budget_pct: selected?.allocations.find((item) => item.deployment_id === strategy.deployment_id)
          ?.risk_budget_bps != null
          ? bpsToPct(selected.allocations.find((item) => item.deployment_id === strategy.deployment_id)!.risk_budget_bps!)
          : '',
        locked:
          selected?.allocations.find((item) => item.deployment_id === strategy.deployment_id)?.locked ?? false,
      })),
    );
  }, [overview, selected]);

  /* ── Poll simulation while active ────────────────────────────────────── */
  useEffect(() => {
    if (!selected || !simulationStatus) return;
    if (simulationStatus.status !== 'pending' && simulationStatus.status !== 'running') return;
    const timer = window.setInterval(() => void reloadSimulation(selected.id), 3000);
    return () => window.clearInterval(timer);
  }, [selected, simulationStatus, reloadSimulation]);

  /* ── Portfolio creation ──────────────────────────────────────────────── */
  const handleCreate = async () => {
    setCreating(true);
    setError(null);
    try {
      const activeAllocations: PortfolioAllocation[] = [];
      const result = await createPortfolio({
        name: newName.trim() || '我的模拟组合',
        total_capital: Number(newCapital) || 0,
        rebalance_frequency: 'daily',
        allocations: activeAllocations,
      });
      const portfolioList = await loadPortfolios();
      const created = portfolioList.find((item) => item.id === result.portfolio_id) ?? null;
      setCreateOpen(false);
      if (created) {
        setSelected(created);
        navigate(routeFor(created.id, 'overview'), { replace: true });
        await loadOverview(created.id);
      }
      setMessage('模拟盘已创建');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '创建组合失败');
    } finally {
      setCreating(false);
    }
  };

  /* ── Allocation editor ───────────────────────────────────────────────── */
  const resetDraftState = () => {
    setValidation(null);
    setPreview(null);
    setDraftRevision(null);
  };

  const updateRow = (deploymentId: number, patch: Partial<AllocationRow>) => {
    setRows((current) =>
      current.map((row) => (row.deployment_id === deploymentId ? { ...row, ...patch } : row)),
    );
    resetDraftState();
  };

  const rowsToAllocations = (source: AllocationRow[]): PortfolioAllocation[] =>
    source.map((row) => ({
      deployment_id: row.deployment_id,
      target_weight_bps: pctToBps(row.target_pct),
      min_weight_bps: pctToBps(row.min_pct),
      max_weight_bps: pctToBps(row.max_pct),
      locked: row.locked,
      risk_budget_bps: row.risk_budget_pct === '' ? null : pctToBps(row.risk_budget_pct),
    }));

  const applyAllocation = (mode: 'equal' | 'normalize' | 'risk') => {
    const allocations = allocateBasisPoints(
      rows.map((row) => ({
        deploymentId: row.deployment_id,
        locked: row.locked,
        currentBps: pctToBps(row.target_pct),
        minBps: pctToBps(row.min_pct),
        maxBps: pctToBps(row.max_pct),
        score:
          mode === 'equal'
            ? 1
            : mode === 'risk'
              ? row.risk_budget_pct === '' ? 0 : pctToBps(row.risk_budget_pct)
              : pctToBps(row.target_pct),
      })),
    );
    setRows((current) =>
      current.map((row) => ({
        ...row,
        target_pct: bpsToPct(allocations.get(row.deployment_id) ?? 0),
      })),
    );
    resetDraftState();
  };

  const strategyWeightBps = rows.reduce((sum, row) => sum + pctToBps(row.target_pct), 0);

  const handleValidate = async () => {
    if (!selected) return;
    setAllocationBusy('validate');
    setError(null);
    try {
      const result = await validateAllocations(selected.id, rowsToAllocations(rows));
      setValidation(result.validation);
      setDraftRevision(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '校验失败');
    } finally {
      setAllocationBusy(null);
    }
  };

  const handlePreview = async () => {
    if (!selected) return;
    setAllocationBusy('preview');
    setError(null);
    try {
      const result = await previewRebalance(selected.id, rowsToAllocations(rows));
      setPreview(result);
      setValidation(result.validation);
      setDraftRevision(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '预览失败');
    } finally {
      setAllocationBusy(null);
    }
  };

  const handleSaveDraft = async () => {
    if (!selected) return;
    setAllocationBusy('draft');
    setError(null);
    try {
      const result = await createPortfolioDraft(selected.id, rowsToAllocations(rows));
      setDraftRevision(result.revision);
      setValidation(result.validation);
      setMessage(`草稿已保存（版本 ${result.revision}）`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '保存草稿失败');
    } finally {
      setAllocationBusy(null);
    }
  };

  const handlePublish = async () => {
    if (!selected || draftRevision === null) return;
    setAllocationBusy('publish');
    setError(null);
    try {
      await publishPortfolioDraft(selected.id, draftRevision);
      setMessage(`组合版本 ${draftRevision} 已发布，新的交易日按新权重执行`);
      setDraftRevision(null);
      await loadOverview(selected.id);
      const portfolioList = await loadPortfolios();
      setSelected(portfolioList.find((item) => item.id === selected.id) ?? selected);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '发布失败');
    } finally {
      setAllocationBusy(null);
    }
  };

  const removeStrategy = async (strategy: PortfolioStrategyOverview) => {
    if (!selected || !overview) return;
    const positionCount = strategy.position_count;
    const openOrders = overview.recent_orders.filter(
      (order) => order.deployment_id === strategy.deployment_id && order.status === 'pending',
    ).length;
    const confirmed = window.confirm(
      `将“${strategy.display_name}”从组合“${selected.name}”的新版本中移除。\n\n` +
      `当前持仓：${positionCount} 项，市值 ¥${strategy.current_market_value.toLocaleString('zh-CN')}\n` +
      `最近未完成订单：${openOrders} 笔\n\n` +
      '本操作停止该策略在此组合中的后续信号，但保留历史净值、订单和持仓记录；现有持仓不会被物理删除。是否继续？',
    );
    if (!confirmed) return;

    setAllocationBusy('remove');
    setError(null);
    try {
      const remaining = rows.filter((row) => row.deployment_id !== strategy.deployment_id);
      const draft = await createPortfolioDraft(selected.id, rowsToAllocations(remaining));
      if (!draft.validation.valid) {
        throw new Error('移出后的组合版本未通过校验');
      }
      await publishPortfolioDraft(selected.id, draft.revision);

      let stopped = false;
      const stillUsed = portfolios.some(
        (portfolio) =>
          portfolio.id !== selected.id
          && (portfolio.allocations ?? []).some((item) => item.deployment_id === strategy.deployment_id),
      );
      if (!stillUsed) {
        const stopConfirmed = window.confirm('该部署已不再被其他组合使用。是否同时停止部署，阻止其继续产生新信号？');
        if (stopConfirmed) {
          await updateDeployment(strategy.deployment_id, { status: 'stopped' });
          stopped = true;
        }
      }
      setMessage(
        `已通过组合版本 ${draft.revision} 移出“${strategy.display_name}”${stopped ? '，并停止对应部署。' : '；历史数据已保留。'}`,
      );
      setRows(remaining);
      resetDraftState();
      await loadOverview(selected.id);
      await loadPortfolios();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '移出策略失败');
    } finally {
      setAllocationBusy(null);
    }
  };

  /* ── Simulation actions ──────────────────────────────────────────────── */
  const handleRunSimulation = async () => {
    if (!selected) return;
    setSimulationBusy(true);
    setError(null);
    try {
      const result = await triggerSimulation(simulationDate || undefined, selected.id);
      setMessage(`模拟任务已提交：${result.job_id}`);
      await reloadSimulation(selected.id);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '提交模拟任务失败');
    } finally {
      setSimulationBusy(false);
    }
  };

  const handleReplay = async () => {
    if (!selected || !calendar) return;
    if (restartReplay) {
      const confirmed = window.confirm(
        `将清空“${selected.name}”当前模拟订单、持仓和净值后重新回放，其他模拟盘不受影响。是否继续？`,
      );
      if (!confirmed) return;
    }
    setSimulationBusy(true);
    setError(null);
    try {
      const result = await backfillSimulation(replayStart, replayEnd, selected.id, restartReplay);
      setMessage(`历史模拟已提交：${result.job_id}`);
      await reloadSimulation(selected.id);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '提交历史模拟失败');
    } finally {
      setSimulationBusy(false);
    }
  };

  /* ── Derived view data ───────────────────────────────────────────────── */
  const strategyWeightPct = strategyWeightBps / 100;
  const cashWeightPct = overview && overview.current_equity > 0
    ? (overview.cash_balance / overview.current_equity) * 100
    : null;

  const navChartOption = useMemo(() => ({
    color: [CHART_COLORS.accent, CHART_COLORS.ochre],
    grid: baseGrid(),
    legend: baseLegend(),
    tooltip: baseTooltip(),
    xAxis: baseXAxis({ data: navPoints.map((point) => point.date) }),
    yAxis: [
      baseYAxis({
        axisLabel: {
          color: CHART_COLORS.axisLabel,
          fontSize: 11,
          formatter: (value: number) => `${(value / 10_000).toFixed(0)}万`,
        },
      }),
      baseYAxis({
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
        name: '组合净值',
        type: 'line',
        data: navPoints.map((point) => point.nav),
        showSymbol: false,
        lineStyle: { width: 2 },
      },
      {
        name: '累计收益',
        type: 'line',
        yAxisIndex: 1,
        data: navPoints.map((point) => point.cumulative_return),
        showSymbol: false,
        lineStyle: { width: 1.5, type: 'dashed' },
      },
    ],
  }), [navPoints]);

  const dailyChartOption = useMemo(() => ({
    grid: baseGrid(),
    legend: baseLegend(),
    tooltip: baseTooltip(),
    xAxis: baseXAxis({ data: navPoints.map((point) => point.date) }),
    yAxis: [
      baseYAxis({
        axisLabel: {
          color: CHART_COLORS.axisLabel,
          fontSize: 11,
          formatter: (value: number) => `${(value * 100).toFixed(1)}%`,
        },
      }),
      baseYAxis({
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
        name: '每日收益',
        type: 'bar',
        data: navPoints.map((point) => ({
          value: point.daily_return,
          itemStyle: {
            color: (point.daily_return ?? 0) >= 0 ? CHART_COLORS.rise : CHART_COLORS.fall,
          },
        })),
        barMaxWidth: 10,
      },
      {
        name: '累计收益',
        type: 'line',
        yAxisIndex: 1,
        data: navPoints.map((point) => point.cumulative_return),
        showSymbol: false,
        lineStyle: { width: 1.5, color: CHART_COLORS.ochre },
      },
    ],
  }), [navPoints]);

  const pieOption = useMemo(() => {
    if (!overview) return null;
    const data = [
      ...overview.strategies.map((strategy) => ({
        name: strategy.display_name,
        value: Math.max(0, strategy.target_capital),
      })),
      { name: '现金', value: Math.max(0, overview.cash_balance) },
    ];
    return {
      color: [...SERIES_PALETTE],
      tooltip: baseTooltip({ trigger: 'item', formatter: '{b}: ¥{c} ({d}%)' }),
      legend: baseLegend({ top: undefined, bottom: 0, right: undefined, left: 'center' }),
      series: [
        {
          type: 'pie',
          radius: ['40%', '66%'],
          label: { color: CHART_COLORS.axisLabel, fontSize: 11 },
          data,
        },
      ],
    };
  }, [overview]);

  const weightBarOption = useMemo(() => {
    if (!overview || overview.strategies.length === 0) return null;
    return {
      color: [CHART_COLORS.accent, CHART_COLORS.ink],
      grid: baseGrid(),
      legend: baseLegend(),
      tooltip: baseTooltip({
        valueFormatter: (value: unknown) =>
          typeof value === 'number' ? `${value.toFixed(1)}%` : '-',
      }),
      xAxis: baseXAxis({ data: overview.strategies.map((strategy) => strategy.display_name) }),
      yAxis: baseYAxis({
        axisLabel: {
          color: CHART_COLORS.axisLabel,
          fontSize: 11,
          formatter: (value: number) => `${value}%`,
        },
      }),
      series: [
        {
          name: '目标权重',
          type: 'bar',
          data: overview.strategies.map((strategy) => strategy.target_weight_bps / 100),
          barMaxWidth: 28,
        },
        {
          name: '实际仓位',
          type: 'bar',
          data: overview.strategies.map((strategy) => strategy.actual_weight_pct),
          barMaxWidth: 28,
        },
      ],
    };
  }, [overview]);

  /* ── Render ──────────────────────────────────────────────────────────── */
  if (loading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-80" />
        <Skeleton className="h-44 w-full" />
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        title="交易工作台"
        description="创建和维护独立模拟盘，跟踪策略从配置、成交到每日收益的完整过程。"
        breadcrumb={[
          { label: '执行' },
          ...(fromExperiment
            ? [{ label: '实验中心', to: '/experiment' }, { label: `来源实验 #${fromExperiment}`, to: `/experiment/${fromExperiment}` }]
            : []),
          { label: '交易工作台' },
        ]}
        tags={
          <>
            <StatusTag variant="paper">模拟盘</StatusTag>
            {selected && <StatusTag variant="neutral">{selected.name}</StatusTag>}
          </>
        }
        actions={
          <>
            <Button variant="secondary" size="sm" onClick={() => navigate('/trading/brokers')}>
              <Icon name="bank" className="h-4 w-4" />
              QMT / PTrade 准备
            </Button>
            {canRebalance && (
              <Button size="sm" onClick={() => setCreateOpen(true)}>
                <Icon name="plus" className="h-4 w-4" />
                新建模拟盘
              </Button>
            )}
          </>
        }
      />

      {message && <Banner variant="ok" className="mb-4">{message}</Banner>}
      {error && <Banner variant="danger" className="mb-4">{error}</Banner>}

      {portfolios.length === 0 ? (
        <Card>
          <div className="py-8 text-center">
            <p className="text-sm text-ink-500">还没有模拟盘。创建第一个模拟组合开始纸面跟踪。</p>
            {canRebalance && (
              <Button className="mt-4" onClick={() => setCreateOpen(true)}>
                <Icon name="plus" className="h-4 w-4" />
                新建模拟盘
              </Button>
            )}
          </div>
        </Card>
      ) : (
        <>
          {/* Portfolio selector + section tabs */}
          <div className="mb-4 flex flex-wrap items-center gap-3">
            <div className="w-full max-w-xs">
              <Select
                aria-label="选择模拟盘"
                value={selected ? String(selected.id) : ''}
                onChange={(event) => {
                  const next = portfolios.find((item) => item.id === Number(event.target.value));
                  if (next) {
                    setSelected(next);
                    navigate(routeFor(next.id, activeSection));
                  }
                }}
                options={portfolios.map((item) => ({ value: String(item.id), label: item.name }))}
              />
            </div>
            {schedule && (
              <span className="text-xs text-ink-400">
                {schedule.enabled
                  ? `自动执行：${schedule.timezone} 每个工作日 ${schedule.run_time}`
                  : '自动执行：已关闭'}
              </span>
            )}
          </div>

          <nav aria-label="交易工作台分区" className="mb-5 flex flex-wrap gap-1 border-b border-ink-200">
            {WORKBENCH_SECTIONS.map((section) => (
              <button
                key={section.key}
                type="button"
                aria-current={activeSection === section.key ? 'page' : undefined}
                onClick={() => selected && navigate(routeFor(selected.id, section.key))}
                className={`-mb-px border-b-2 px-3.5 py-2.5 text-sm font-medium transition-colors ${
                  activeSection === section.key
                    ? 'border-accent-700 text-accent-800'
                    : 'border-transparent text-ink-500 hover:border-ink-300 hover:text-ink-800'
                }`}
              >
                {section.label}
              </button>
            ))}
          </nav>

          {selected && overview && activeSection === 'overview' && (
            <OverviewSection
              overview={overview}
              navChartOption={navChartOption}
              dailyChartOption={dailyChartOption}
              pieOption={pieOption}
              weightBarOption={weightBarOption}
              cashWeightPct={cashWeightPct}
              deployments={deployments}
              canUseAi={canUseAi}
              portfolioId={selected.id}
            />
          )}

          {selected && overview && activeSection === 'allocation' && (
            <AllocationSection
              overview={overview}
              rows={rows}
              updateRow={updateRow}
              applyAllocation={applyAllocation}
              strategyWeightBps={strategyWeightBps}
              strategyWeightPct={strategyWeightPct}
              validation={validation}
              preview={preview}
              draftRevision={draftRevision}
              allocationBusy={allocationBusy}
              canRebalance={canRebalance}
              onValidate={() => void handleValidate()}
              onPreview={() => void handlePreview()}
              onSaveDraft={() => void handleSaveDraft()}
              onPublish={() => void handlePublish()}
              onRemove={(strategy) => void removeStrategy(strategy)}
            />
          )}

          {selected && activeSection === 'analytics' && (
            <StrategyAnalyticsPanel portfolioId={selected.id} deployments={deployments} />
          )}

          {selected && activeSection === 'simulation' && (
            <SimulationSection
              simulationStatus={simulationStatus}
              simulationRuns={simulationRuns}
              calendar={calendar}
              calendarError={calendarError}
              simulationDate={simulationDate}
              setSimulationDate={setSimulationDate}
              replayStart={replayStart}
              setReplayStart={setReplayStart}
              replayEnd={replayEnd}
              setReplayEnd={setReplayEnd}
              restartReplay={restartReplay}
              setRestartReplay={setRestartReplay}
              simulationBusy={simulationBusy}
              canExecute={canExecute}
              hasStrategies={(overview?.strategies.length ?? 0) > 0}
              onRun={() => void handleRunSimulation()}
              onReplay={() => void handleReplay()}
            />
          )}

          {selected && activeSection === 'history' && (
            <HistorySection runs={simulationRuns} overview={overview} />
          )}

          {selected && overview && overview.strategies.length === 0 && activeSection === 'overview' && (
            <EmptyPortfolioGuide />
          )}
        </>
      )}

      {/* Create portfolio modal */}
      <Modal
        isOpen={createOpen}
        onClose={() => setCreateOpen(false)}
        title="新建模拟盘"
        footer={
          <>
            <Button variant="ghost" onClick={() => setCreateOpen(false)}>取消</Button>
            <Button onClick={() => void handleCreate()} loading={creating}>创建</Button>
          </>
        }
      >
        <div className="space-y-4">
          <Input
            label="组合名称"
            value={newName}
            onChange={(event) => setNewName(event.target.value)}
          />
          <Input
            label="初始资金（¥）"
            type="number"
            min={0}
            value={newCapital}
            onChange={(event) => setNewCapital(event.target.value)}
            hint="虚拟资金，仅用于模拟核算，不涉及真实账户。"
          />
        </div>
      </Modal>
    </div>
  );
}

/* ── Overview ────────────────────────────────────────────────────────────── */

function OverviewSection({
  overview,
  navChartOption,
  dailyChartOption,
  pieOption,
  weightBarOption,
  cashWeightPct,
  deployments,
  canUseAi,
  portfolioId,
}: {
  overview: PortfolioOverview;
  navChartOption: Record<string, unknown>;
  dailyChartOption: Record<string, unknown>;
  pieOption: Record<string, unknown> | null;
  weightBarOption: Record<string, unknown> | null;
  cashWeightPct: number | null;
  deployments: Deployment[];
  canUseAi: boolean;
  portfolioId: number;
}) {
  const strategyWeightPct = overview.strategies.reduce((sum, item) => sum + item.target_weight_bps, 0) / 100;
  const kpis = [
    { label: '当前权益', value: formatCny(overview.current_equity) },
    { label: '今日盈亏', value: formatCny(overview.daily_pnl), tone: signedToneClass(overview.daily_pnl), sub: formatSignedPct(overview.daily_return) },
    { label: '累计收益', value: formatSignedPct(overview.cumulative_return), tone: signedToneClass(overview.cumulative_return) },
    { label: '最大回撤', value: formatPct(overview.max_drawdown) },
    { label: 'Sharpe', value: overview.sharpe_ratio !== null ? overview.sharpe_ratio.toFixed(2) : '-' },
    { label: '现金余额', value: formatCny(overview.cash_balance) },
    { label: '策略权重', value: `${strategyWeightPct.toFixed(1)}%` },
    { label: '现金权重', value: cashWeightPct !== null ? `${cashWeightPct.toFixed(1)}%` : '-' },
  ];
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {kpis.map((kpi) => (
          <div key={kpi.label} className="rounded-md border border-ink-200 bg-surface p-3.5">
            <p className="text-xs text-ink-400">{kpi.label}</p>
            <p className={`tnum mt-1 text-xl font-semibold ${kpi.tone ?? 'text-ink-900'}`}>{kpi.value}</p>
            {kpi.sub && <p className="tnum mt-0.5 text-2xs text-ink-400">{kpi.sub}</p>}
          </div>
        ))}
      </div>
      <p className="text-xs text-ink-400 tnum">
        模拟区间：{overview.start_date ?? '-'} ~ {overview.latest_date ?? '-'}（{overview.trading_days} 个交易日）
        {' · '}初始资金 {formatCny(overview.initial_capital)}
        {' · '}当前版本 v{overview.current_revision}
      </p>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card title="净值与累计收益" padding="sm">
          <EChart option={navChartOption} style={{ height: 280 }} notMerge />
        </Card>
        <Card title="每日收益（红盈绿亏）" padding="sm">
          <EChart option={dailyChartOption} style={{ height: 280 }} notMerge />
        </Card>
        {pieOption && (
          <Card title="目标资金分布" padding="sm">
            <EChart option={pieOption} style={{ height: 300 }} notMerge />
          </Card>
        )}
        {weightBarOption && (
          <Card title="目标权重与实际仓位" padding="sm">
            <EChart option={weightBarOption} style={{ height: 300 }} notMerge />
          </Card>
        )}
      </div>

      {overview.strategies.length > 0 && (
        <Card title="策略参数快照" padding="md">
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {overview.strategies.map((strategy) => (
              <div key={strategy.deployment_id} className="rounded border border-ink-100 p-3">
                <div className="mb-1.5 flex items-center justify-between gap-2">
                  <p className="text-sm font-medium text-ink-800">{strategy.display_name}</p>
                  {strategy.source_experiment_id && (
                    <span className="text-2xs text-ink-400">来源实验 #{strategy.source_experiment_id}</span>
                  )}
                </div>
                <pre className="max-h-32 overflow-auto whitespace-pre-wrap font-mono text-2xs leading-4 text-ink-500 scrollbar-thin">
                  {JSON.stringify(strategy.params, null, 2)}
                </pre>
                {deployments.find((item) => item.id === strategy.deployment_id)?.research_risk_snapshot && (
                  <div className="mt-2 rounded border border-amber-300 bg-amber-50 px-2.5 py-2 text-xs text-amber-800">
                    <p className="font-medium">研究风险告警已绑定</p>
                    <p className="mt-1 break-all">
                      数据代 {deployments.find((item) => item.id === strategy.deployment_id)?.research_generation_id ?? '未标识'}
                      {' · '}来源 {deployments.find((item) => item.id === strategy.deployment_id)?.research_source_id ?? '未标识'}
                      {' · '}告警 {deployments.find((item) => item.id === strategy.deployment_id)?.research_risk_snapshot?.warnings.length ?? 0} 项
                    </p>
                  </div>
                )}
              </div>
            ))}
          </div>
        </Card>
      )}

      {canUseAi && (
        <AiMarketInsightCard
          onRequestInsight={() => marketInsight(portfolioId)}
          scopeKey={`portfolio:${portfolioId}`}
        />
      )}
    </div>
  );
}

/* ── Allocation editor ───────────────────────────────────────────────────── */

function AllocationSection({
  overview,
  rows,
  updateRow,
  applyAllocation,
  strategyWeightBps,
  strategyWeightPct,
  validation,
  preview,
  draftRevision,
  allocationBusy,
  canRebalance,
  onValidate,
  onPreview,
  onSaveDraft,
  onPublish,
  onRemove,
}: {
  overview: PortfolioOverview;
  rows: AllocationRow[];
  updateRow: (deploymentId: number, patch: Partial<AllocationRow>) => void;
  applyAllocation: (mode: 'equal' | 'normalize' | 'risk') => void;
  strategyWeightBps: number;
  strategyWeightPct: number;
  validation: AllocationValidation | null;
  preview: RebalancePreview | null;
  draftRevision: number | null;
  allocationBusy: string | null;
  canRebalance: boolean;
  onValidate: () => void;
  onPreview: () => void;
  onSaveDraft: () => void;
  onPublish: () => void;
  onRemove: (strategy: PortfolioStrategyOverview) => void;
}) {
  if (overview.strategies.length === 0) {
    return (
      <Card>
        <div className="py-8 text-center">
          <p className="text-sm text-ink-500">
            当前模拟盘还没有策略。请从已完成实验中选择这个模拟盘进行部署。
          </p>
        </div>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <Card
        title="策略配置"
        description="先校验和预览，保存草稿后发布，新的交易日按新权重执行。"
        padding="none"
      >
        <div className="overflow-x-auto scrollbar-thin">
          <table className="w-full text-sm" style={{ minWidth: 1080 }}>
            <caption className="sr-only">策略权重配置</caption>
            <thead>
              <tr className="border-b border-ink-200 bg-ink-50">
                {['策略', '目标 %', '下限 %', '上限 %', '风险预算 %', '锁定', '目标资金', '变化', ''].map((header) => (
                  <th key={header} scope="col" className="px-3 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-ink-500">
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-ink-100">
              {rows.map((row) => {
                const strategy = overview.strategies.find((item) => item.deployment_id === row.deployment_id);
                const targetCapital = (pctToBps(row.target_pct) / 10_000) * overview.current_equity;
                const currentCapital = strategy?.current_market_value ?? 0;
                const delta = targetCapital - currentCapital;
                return (
                  <tr key={row.deployment_id}>
                    <td className="px-3 py-2.5">
                      <p className="font-medium text-ink-800">{row.display_name}</p>
                      <p className="font-mono text-2xs text-ink-400">{row.strategy_id}</p>
                    </td>
                    {(['target_pct', 'min_pct', 'max_pct', 'risk_budget_pct'] as const).map((field) => (
                      <td key={field} className="px-2 py-2.5">
                        <input
                          type="number"
                          aria-label={`${row.display_name} ${field}`}
                          value={row[field]}
                          disabled={!canRebalance}
                          onChange={(event) => updateRow(row.deployment_id, { [field]: event.target.value })}
                          className="tnum w-20 rounded border border-ink-300 px-2 py-1.5 text-xs focus:border-accent-600 focus:outline-none disabled:bg-ink-100"
                        />
                      </td>
                    ))}
                    <td className="px-2 py-2.5 text-center">
                      <input
                        type="checkbox"
                        aria-label={`锁定 ${row.display_name} 权重`}
                        checked={row.locked}
                        disabled={!canRebalance}
                        onChange={(event) => updateRow(row.deployment_id, { locked: event.target.checked })}
                        className="h-4 w-4 rounded-sm border-ink-300 text-accent-700 focus:ring-accent-600"
                      />
                    </td>
                    <td className="tnum px-3 py-2.5 text-xs">{formatCny(targetCapital)}</td>
                    <td className={`tnum px-3 py-2.5 text-xs ${delta >= 0 ? 'text-rise' : 'text-fall'}`}>
                      {delta >= 0 ? '+' : ''}{formatCny(delta)}
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      {canRebalance && strategy && (
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={allocationBusy !== null}
                          onClick={() => onRemove(strategy)}
                        >
                          移出组合
                        </Button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
            <tfoot>
              <tr className="border-t border-ink-200 bg-ink-50 text-xs">
                <td className="px-3 py-2.5 font-medium" colSpan={1}>合计</td>
                <td className={`tnum px-2 py-2.5 font-semibold ${strategyWeightBps > 10_000 ? 'text-danger-fg' : ''}`} colSpan={4}>
                  {strategyWeightPct.toFixed(2)}%{strategyWeightBps > 10_000 && '（超过 100%）'}
                </td>
                <td colSpan={4} />
              </tr>
            </tfoot>
          </table>
        </div>
        {canRebalance && (
          <div className="flex flex-wrap items-center gap-2 border-t border-ink-100 px-4 py-3">
            <span className="text-xs text-ink-400">快捷分配：</span>
            <Button variant="ghost" size="sm" onClick={() => applyAllocation('equal')}>等权分配未锁定项</Button>
            <Button variant="ghost" size="sm" onClick={() => applyAllocation('normalize')}>归一化未锁定项</Button>
            <Button variant="ghost" size="sm" onClick={() => applyAllocation('risk')}>按风险预算分配</Button>
            <span className="mx-2 h-4 w-px bg-ink-200" aria-hidden />
            <Button variant="secondary" size="sm" onClick={onValidate} loading={allocationBusy === 'validate'} disabled={allocationBusy !== null}>
              校验
            </Button>
            <Button variant="secondary" size="sm" onClick={onPreview} loading={allocationBusy === 'preview'} disabled={allocationBusy !== null}>
              调仓预览
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={onSaveDraft}
              loading={allocationBusy === 'draft'}
              disabled={allocationBusy !== null || strategyWeightBps > 10_000}
            >
              保存草稿
            </Button>
            <Button
              size="sm"
              onClick={onPublish}
              loading={allocationBusy === 'publish'}
              disabled={allocationBusy !== null || draftRevision === null || !validation?.valid}
            >
              发布版本{draftRevision !== null ? ` v${draftRevision}` : ''}
            </Button>
          </div>
        )}
      </Card>

      {validation && (
        <Card title={validation.valid ? '配置校验通过' : '配置校验失败'} padding="md">
          <div className="space-y-1.5 text-sm">
            {validation.errors.map((item, index) => (
              <p key={index} className="text-danger-strong">{item}</p>
            ))}
            {validation.warnings.map((item, index) => (
              <p key={index} className="text-warn-strong">{item}</p>
            ))}
            {validation.valid && validation.errors.length === 0 && (
              <p className="text-ink-500 tnum">
                策略权重 {(validation.strategy_weight_bps / 100).toFixed(2)}%，现金权重 {(validation.cash_weight_bps / 100).toFixed(2)}%，
                现金 {formatCny(validation.cash_capital)}。
              </p>
            )}
          </div>
        </Card>
      )}

      {preview && (
        <Card title="调仓预览" padding="none">
          <div className="grid grid-cols-3 gap-3 border-b border-ink-100 px-4 py-3 text-center">
            <div>
              <p className="tnum text-lg font-semibold">{formatCny(preview.one_way_turnover)}</p>
              <p className="text-2xs text-ink-400">单边换手金额</p>
            </div>
            <div>
              <p className="tnum text-lg font-semibold">{formatPct(preview.turnover_rate)}</p>
              <p className="text-2xs text-ink-400">换手率</p>
            </div>
            <div>
              <p className="tnum text-lg font-semibold">{formatCny(preview.estimated_cost)}</p>
              <p className="text-2xs text-ink-400">预计费用</p>
            </div>
          </div>
          <div className="overflow-x-auto scrollbar-thin">
            <table className="w-full text-sm" style={{ minWidth: 720 }}>
              <caption className="sr-only">调仓预览明细</caption>
              <thead>
                <tr className="border-b border-ink-200 bg-ink-50">
                  {['策略', '当前资金', '目标资金', '差额', '方向', '预计费用'].map((header) => (
                    <th key={header} scope="col" className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-ink-500">
                      {header}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-ink-100">
                {preview.rows.map((row) => (
                  <tr key={row.deployment_id}>
                    <td className="px-3 py-2 text-xs">{row.display_name ?? `部署 #${row.deployment_id}`}</td>
                    <td className="tnum px-3 py-2 text-xs">{formatCny(row.current_capital)}</td>
                    <td className="tnum px-3 py-2 text-xs">{formatCny(row.target_capital)}</td>
                    <td className={`tnum px-3 py-2 text-xs ${row.capital_delta >= 0 ? 'text-rise' : 'text-fall'}`}>
                      {row.capital_delta >= 0 ? '+' : ''}{formatCny(row.capital_delta)}
                    </td>
                    <td className="px-3 py-2 text-xs">
                      {row.direction === 'BUY' ? '买入' : row.direction === 'SELL' ? '卖出' : '不变'}
                    </td>
                    <td className="tnum px-3 py-2 text-xs">{formatCny(row.estimated_cost)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}

/* ── Simulation ──────────────────────────────────────────────────────────── */

export function SimulationSection({
  simulationStatus,
  simulationRuns,
  calendar,
  calendarError,
  simulationDate,
  setSimulationDate,
  replayStart,
  setReplayStart,
  replayEnd,
  setReplayEnd,
  restartReplay,
  setRestartReplay,
  simulationBusy,
  canExecute,
  hasStrategies,
  onRun,
  onReplay,
}: {
  simulationStatus: SimulationStatus | null;
  simulationRuns: SimulationRun[];
  calendar: SimulationCalendar | null;
  calendarError: string | null;
  simulationDate: string;
  setSimulationDate: (value: string) => void;
  replayStart: string;
  setReplayStart: (value: string) => void;
  replayEnd: string;
  setReplayEnd: (value: string) => void;
  restartReplay: boolean;
  setRestartReplay: (value: boolean) => void;
  simulationBusy: boolean;
  canExecute: boolean;
  hasStrategies: boolean;
  onRun: () => void;
  onReplay: () => void;
}) {
  return (
    <div className="space-y-4">
      {!hasStrategies && (
        <Banner variant="warning">
          当前模拟盘还没有策略。请从已完成实验中选择这个模拟盘进行部署。
        </Banner>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="日频模拟" description="对单个交易日执行信号→订单→持仓→净值结算">
          <div className="flex flex-wrap items-end gap-3">
            <div className="w-48">
              <Input
                label="交易日期（留空为下一交易日）"
                type="date"
                value={simulationDate}
                onChange={(event) => setSimulationDate(event.target.value)}
              />
            </div>
            <Button onClick={onRun} loading={simulationBusy} disabled={!canExecute}>
              <Icon name="play" className="h-4 w-4" />
              运行模拟
            </Button>
            {!canExecute && <span className="pb-2 text-xs text-ink-400">需要 trading:execute 权限</span>}
          </div>
          <div className="mt-4 flex items-center gap-2 text-sm">
            <span className="text-ink-500">当前状态：</span>
            <StatusTag
              variant={
                simulationStatus?.status === 'failed'
                  ? 'error'
                  : simulationStatus?.status === 'completed'
                    ? 'verified'
                    : simulationStatus?.status === 'running' || simulationStatus?.status === 'pending'
                      ? 'running'
                      : 'neutral'
              }
            >
              {SIMULATION_STATUS_LABEL[simulationStatus?.status ?? 'not_started']}
            </StatusTag>
            {(simulationStatus?.status === 'pending' || simulationStatus?.status === 'running')
              && simulationStatus.progress !== undefined && (
              <span className="tnum text-xs text-ink-400">{(simulationStatus.progress * 100).toFixed(0)}%</span>
            )}
          </div>
          {simulationStatus?.error && (
            <Banner variant="danger" className="mt-3">{simulationStatus.error}</Banner>
          )}
        </Card>

        <Card
          title="历史回放"
          description="按交易日顺序生成信号、订单、持仓和每日净值；首次建议先回放最近 20 个交易日。"
        >
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="开始日期"
              type="date"
              min={calendar?.min_date}
              max={calendar?.max_date}
              value={replayStart}
              onChange={(event) => setReplayStart(event.target.value)}
            />
            <Input
              label="结束日期"
              type="date"
              min={calendar?.min_date}
              max={calendar?.max_date}
              value={replayEnd}
              onChange={(event) => setReplayEnd(event.target.value)}
            />
          </div>
          {calendar && (
            <>
              <p className="mt-2 text-xs text-ink-400 tnum">
                可用数据：{calendar.min_date} ~ {calendar.max_date}（{calendar.trading_days} 个共同交易日，{calendar.pool_id}）
              </p>
              {calendar.warnings && calendar.warnings.length > 0 && (
                <Banner variant="warning" className="mt-3">
                  研究/模拟数据告警：{calendar.warnings.join('、')}
                </Banner>
              )}
            </>
          )}
          {calendarError && (
            <Banner variant="warning" className="mt-3">
              {calendarError}
            </Banner>
          )}
          <label className="mt-3 flex cursor-pointer items-center gap-2 text-sm text-ink-700">
            <input
              type="checkbox"
              checked={restartReplay}
              onChange={(event) => setRestartReplay(event.target.checked)}
              className="h-4 w-4 rounded-sm border-ink-300 text-accent-700 focus:ring-accent-600"
            />
            清空当前模拟数据后重新回放
          </label>
          <Button
            className="mt-3"
            aria-label="提交历史模拟"
            onClick={onReplay}
            loading={simulationBusy}
            disabled={!canExecute || !calendar || !replayStart || !replayEnd}
          >
            <Icon name="history" className="h-4 w-4" />
            提交历史模拟
          </Button>
        </Card>
      </div>

      <SimulationRunsTable runs={simulationRuns} />
    </div>
  );
}

function SimulationRunsTable({ runs }: { runs: SimulationRun[] }) {
  return (
    <Card title="最近模拟运行" padding="none">
      {runs.length === 0 ? (
        <p className="px-4 py-8 text-center text-sm text-ink-400">暂无模拟运行记录</p>
      ) : (
        <div className="overflow-x-auto scrollbar-thin">
          <table className="w-full text-sm" style={{ minWidth: 720 }}>
            <caption className="sr-only">模拟运行记录</caption>
            <thead>
              <tr className="border-b border-ink-200 bg-ink-50">
                {['交易日', '状态', '提交时间', '完成时间', '错误'].map((header) => (
                  <th key={header} scope="col" className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-ink-500">
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-ink-100">
              {runs.map((run) => (
                <tr key={run.id} className={run.status === 'failed' ? 'text-danger-strong' : ''}>
                  <td className="tnum px-3 py-2">{run.trade_date}</td>
                  <td className="px-3 py-2">
                    <StatusTag
                      variant={
                        run.status === 'failed'
                          ? 'error'
                          : run.status === 'completed'
                            ? 'verified'
                            : run.status === 'cancelled'
                              ? 'neutral'
                              : 'running'
                      }
                    >
                      {SIMULATION_STATUS_LABEL[run.status]}
                    </StatusTag>
                  </td>
                  <td className="tnum px-3 py-2 text-xs text-ink-500">
                    {new Date(run.created_at).toLocaleString('zh-CN', { hour12: false })}
                  </td>
                  <td className="tnum px-3 py-2 text-xs text-ink-500">
                    {run.completed_at ? new Date(run.completed_at).toLocaleString('zh-CN', { hour12: false }) : '-'}
                  </td>
                  <td className="max-w-[240px] truncate px-3 py-2 text-xs" title={run.error ?? undefined}>
                    {run.error ?? '-'}
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

/* ── History ─────────────────────────────────────────────────────────────── */

function HistorySection({ runs, overview }: { runs: SimulationRun[]; overview: PortfolioOverview | null }) {
  return (
    <div className="space-y-4">
      <SimulationRunsTable runs={runs} />
      {overview && overview.recent_orders.length > 0 && (
        <Card title="最近订单" padding="none">
          <div className="overflow-x-auto scrollbar-thin">
            <table className="w-full text-sm" style={{ minWidth: 760 }}>
              <caption className="sr-only">最近订单</caption>
              <thead>
                <tr className="border-b border-ink-200 bg-ink-50">
                  {['日期', '代码', '方向', '价格', '数量', '金额', '状态'].map((header) => (
                    <th key={header} scope="col" className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-ink-500">
                      {header}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-ink-100">
                {overview.recent_orders.map((order) => (
                  <tr key={order.id}>
                    <td className="tnum px-3 py-2 text-xs">{order.date}</td>
                    <td className="px-3 py-2 font-mono text-xs">{order.code}</td>
                    <td className={`px-3 py-2 text-xs font-semibold ${order.action === 'BUY' ? 'text-rise' : 'text-fall'}`}>
                      {order.action === 'BUY' ? '买入' : '卖出'}
                    </td>
                    <td className="tnum px-3 py-2 text-xs">{order.price.toFixed(2)}</td>
                    <td className="tnum px-3 py-2 text-xs">{order.shares.toLocaleString('zh-CN')}</td>
                    <td className="tnum px-3 py-2 text-xs">{formatCny(order.amount)}</td>
                    <td className="px-3 py-2 text-xs">{order.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}

/* ── Empty-portfolio onboarding ──────────────────────────────────────────── */

function EmptyPortfolioGuide() {
  const steps = [
    { title: '1. 配置策略', description: '在“策略配置”中调整权重，或从已完成实验部署新策略。' },
    { title: '2. 发布版本', description: '先校验和预览，保存草稿后发布，新的交易日按新权重执行。' },
    { title: '3. 建立模拟基线', description: '用已下载中证500数据回放，生成信号、成交、持仓和每日净值。' },
    { title: '4. 每日观察', description: '在“模拟运行”执行日频结算，在“组合概览”跟踪净值。' },
  ];
  return (
    <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {steps.map((step) => (
        <div key={step.title} className="rounded-md border border-ink-200 bg-surface p-4">
          <p className="text-sm font-semibold text-ink-800">{step.title}</p>
          <p className="mt-1 text-xs leading-5 text-ink-500">{step.description}</p>
        </div>
      ))}
    </div>
  );
}
