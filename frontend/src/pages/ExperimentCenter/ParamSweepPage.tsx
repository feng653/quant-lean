import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import {
  createSweep,
  getExperiment,
  getExperimentPicker,
  getSweepResult,
  promoteSweepExperiment,
  repairSweep,
} from '../../services/experiments';
import { describeExperimentReadinessBlockers, inspectExperimentDataReadiness } from '../../services/data';
import type { PromoteSweepResponse, SweepExperimentResult } from '../../services/experiments';
import { getStrategy } from '../../services/strategies';
import type { Experiment } from '../../types/experiment';
import type { ParamField, StrategyMetadata } from '../../types/strategy';
import { strategyTrainingMode } from '../../utils/strategy';
import {
  buildSweepGrid,
  cartesianCombinations,
} from './paramSweepUtils';
import type { SweepParameterDraft } from './paramSweepUtils';
import {
  canPromoteSweepTrial,
  compareSweepResults,
  hasSweepWindowErrors,
  mapSelectionResult,
  parsePositiveQueryId,
  restoredSweepPromotion,
  sweepMetricValue,
  validateStrictSweepWindows,
} from './paramSweepProtocol';
import type { SweepDisplayResult, SweepMetricTarget } from './paramSweepProtocol';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import EChart from '../../components/shared/EChart';
import EmptyState from '../../components/shared/EmptyState';
import Icon from '../../components/shared/Icon';
import Input from '../../components/shared/Input';
import PageHeader from '../../components/shared/PageHeader';
import Select from '../../components/shared/Select';
import StatusTag from '../../components/shared/StatusTag';
import {
  baseGrid,
  baseLegend,
  baseTooltip,
  baseXAxis,
  baseYAxis,
  CHART_COLORS,
  formatPct,
} from '../../components/shared/chartTheme';

const MAX_EXPERIMENTS = 100;
const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled'];
const METRIC_TARGETS = [
  { value: 'sharpe', label: '选模 Sharpe' },
  { value: 'return', label: '选模年化收益' },
  { value: 'max_drawdown', label: '选模最大回撤' },
] as const;

let draftRowId = 0;
function newDraftRow(): SweepParameterDraft {
  draftRowId += 1;
  return { id: draftRowId, name: '', valueType: 'float', mode: 'linear', min: '', max: '', steps: '5', custom: '' };
}

function formatSweepValue(value: unknown): string {
  if (typeof value === 'number') return String(Number(value.toPrecision(8)));
  return String(value);
}

export default function ParamSweepPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  /* Baseline experiment + strategy */
  const [pickerExperiments, setPickerExperiments] = useState<Experiment[]>([]);
  const [baselineId, setBaselineId] = useState('');
  const [baselineExperiment, setBaselineExperiment] = useState<Experiment | null>(null);
  const [strategy, setStrategy] = useState<StrategyMetadata | null>(null);
  const [selectionLoading, setSelectionLoading] = useState(false);
  const [baselineError, setBaselineError] = useState<string | null>(null);
  const selectionRequestRef = useRef(0);

  /* Windows */
  const [selectionStart, setSelectionStart] = useState('');
  const [selectionEnd, setSelectionEnd] = useState('');
  const [lockedTestStart, setLockedTestStart] = useState('');
  const [lockedTestEnd, setLockedTestEnd] = useState('');

  /* Parameter rows */
  const [rows, setRows] = useState<SweepParameterDraft[]>([newDraftRow()]);
  const [metricTarget, setMetricTarget] = useState<SweepMetricTarget>('sharpe');

  /* Submission / results */
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [sweepId, setSweepId] = useState<number | null>(() =>
    parsePositiveQueryId(searchParams.get('sweep_id')),
  );
  const [sweepLoadError, setSweepLoadError] = useState<string | null>(null);
  const [researchTrust, setResearchTrust] = useState<'locked_test' | 'legacy_unlocked'>('locked_test');
  const [dataAccessPolicy, setDataAccessPolicy] = useState<'allow_fetch' | 'cache_only'>('cache_only');
  const [sweepStatus, setSweepStatus] = useState('');
  const [sweepParamNames, setSweepParamNames] = useState<string[]>([]);
  const [results, setResults] = useState<SweepDisplayResult[]>([]);
  const [rawResults, setRawResults] = useState<SweepExperimentResult[]>([]);
  const [totalExperiments, setTotalExperiments] = useState(0);
  const [completedExperiments, setCompletedExperiments] = useState(0);
  const [repairableExperimentIds, setRepairableExperimentIds] = useState<number[]>([]);
  const [repairing, setRepairing] = useState(false);
  const [repairError, setRepairError] = useState<string | null>(null);
  const [pollGeneration, setPollGeneration] = useState(0);

  /* Promotion */
  const [selectedTrialId, setSelectedTrialId] = useState<number | null>(null);
  const [promoting, setPromoting] = useState(false);
  const [promotion, setPromotion] = useState<PromoteSweepResponse | null>(null);
  const [promotionError, setPromotionError] = useState<string | null>(null);

  /* ── Baseline selection with race guard ──────────────────────────────── */
  const handleSelectBaseline = useCallback((value: string) => {
    const requestId = ++selectionRequestRef.current;
    setBaselineId(value);
    setBaselineError(null);
    setBaselineExperiment(null);
    setStrategy(null);
    setRows([newDraftRow()]);
    if (!value) {
      setSelectionLoading(false);
      return;
    }
    setSelectionLoading(true);
    void (async () => {
      try {
        const experiment = await getExperiment(Number(value));
        const metadata = await getStrategy(experiment.strategy_id);
        if (selectionRequestRef.current !== requestId) return;
        setBaselineExperiment(experiment);
        setStrategy(metadata);
        setPickerExperiments((current) => (
          current.some((item) => item.id === experiment.id)
            ? current
            : [experiment, ...current]
        ));
        setSelectionStart(experiment.test_start);
        setSelectionEnd(experiment.test_end);
      } catch (err: unknown) {
        if (selectionRequestRef.current === requestId) {
          setBaselineExperiment(null);
          setStrategy(null);
          setBaselineError(
            err instanceof Error
              ? err.message
              : `实验 #${value} 不存在或当前账号无权访问`,
          );
        }
      } finally {
        if (selectionRequestRef.current === requestId) setSelectionLoading(false);
      }
    })();
  }, []);

  /* ── URL restoration and picker list ─────────────────────────────────── */
  useEffect(() => {
    void getExperimentPicker({ limit: 100 })
      .then((items) => setPickerExperiments((current) => {
        const directEntries = current.filter(
          (item) => !items.some((pickerItem) => pickerItem.id === item.id),
        );
        return [...directEntries, ...items];
      }))
      .catch(() => setPickerExperiments((current) => current));
  }, []);

  const requestedBaselineId = parsePositiveQueryId(searchParams.get('baseline_id'));
  useEffect(() => {
    if (
      sweepId === null
      && requestedBaselineId !== null
      && baselineId !== String(requestedBaselineId)
    ) {
      handleSelectBaseline(String(requestedBaselineId));
    }
  }, [baselineId, handleSelectBaseline, requestedBaselineId, sweepId]);

  const requestedSweepId = parsePositiveQueryId(searchParams.get('sweep_id'));
  useEffect(() => {
    if (requestedSweepId !== null && requestedSweepId !== sweepId) {
      setSweepLoadError(null);
      setPromotion(null);
      setResults([]);
      setRawResults([]);
      setSweepId(requestedSweepId);
    }
  }, [requestedSweepId, sweepId]);

  /* ── Derived state ───────────────────────────────────────────────────── */
  const isTrainOnce = strategy ? strategyTrainingMode(strategy) === 'train_once' : false;
  const windowErrors = validateStrictSweepWindows({
    selectionStart,
    selectionEnd,
    lockedTestStart,
    lockedTestEnd,
    trainStart: isTrainOnce ? baselineExperiment?.train_start : null,
    trainEnd: isTrainOnce ? baselineExperiment?.train_end : null,
  });
  const gridResult = useMemo(() => buildSweepGrid(rows), [rows]);
  const combinations = useMemo(
    () => (gridResult.total > 0 ? cartesianCombinations(gridResult.grid) : []),
    [gridResult],
  );
  const exceedsCap = gridResult.total > MAX_EXPERIMENTS;
  const canSubmit =
    baselineExperiment !== null &&
    strategy !== null &&
    strategy.params.length > 0 &&
    !hasSweepWindowErrors(windowErrors) &&
    Object.keys(gridResult.rowErrors).length === 0 &&
    gridResult.total >= 1 &&
    !exceedsCap;

  /* ── Row editing ─────────────────────────────────────────────────────── */
  const updateRow = (id: number, patch: Partial<SweepParameterDraft>) => {
    setRows((current) => current.map((row) => (row.id === id ? { ...row, ...patch } : row)));
  };

  const selectRowParam = (id: number, paramName: string) => {
    const param = strategy?.params.find((item) => item.name === paramName);
    const forceCustom = param
      ? ['bool', 'boolean', 'choice', 'str', 'string', 'text'].includes(param.type.toLowerCase()) || Boolean(param.choices)
      : false;
    updateRow(id, {
      name: paramName,
      valueType: param?.type ?? 'float',
      choices: param?.choices ?? undefined,
      mode: forceCustom ? 'custom' : 'linear',
      min: param?.min != null ? String(param.min) : '',
      max: param?.max != null ? String(param.max) : '',
      custom: '',
    });
  };

  /* ── Submit ──────────────────────────────────────────────────────────── */
  const handleSubmit = async () => {
    if (!baselineExperiment || !strategy) return;
    setSubmitError(null);
    setSubmitting(true);
    try {
      const inheritedDataPolicy = (
        baselineExperiment.data_access_policy ?? 'cache_only'
      );
      const inheritedTrustProfile = (
        baselineExperiment.research_trust?.profile ?? 'governed_production_pit'
      );
      if (inheritedDataPolicy === 'cache_only') {
        const readiness = await inspectExperimentDataReadiness({
          data_access_policy: 'cache_only',
          research_trust_profile: inheritedTrustProfile,
          price_purpose: 'real_tuning',
          pool_preset: baselineExperiment.pool_preset ?? 'custom',
          pool_custom_codes: baselineExperiment.pool_custom_codes ?? [],
          train_start: isTrainOnce
            ? baselineExperiment.train_start ?? undefined
            : undefined,
          test_start: selectionStart,
          test_end: lockedTestEnd,
        });
        if (!readiness.ready) {
          throw new Error(
            `本地缓存未覆盖扫描与锁定测试窗口：${describeExperimentReadinessBlockers(readiness).join('、')}`,
          );
        }
      }
      const response = await createSweep({
        strategy_id: strategy.strategy_id,
        name: `${baselineExperiment.name}-${Object.keys(gridResult.grid).join('-')}`,
        param_grid: gridResult.grid,
        pool_preset: baselineExperiment.pool_preset,
        pool_custom_codes: (baselineExperiment.pool_custom_codes ?? []).join(','),
        pool_industries: (baselineExperiment.pool_industries ?? []).join(','),
        train_start: isTrainOnce ? baselineExperiment.train_start : undefined,
        train_end: isTrainOnce ? baselineExperiment.train_end : undefined,
        selection_start: selectionStart,
        selection_end: selectionEnd,
        locked_test_start: lockedTestStart,
        locked_test_end: lockedTestEnd,
        base_params: baselineExperiment.params,
        mode: baselineExperiment.mode,
        data_access_policy: inheritedDataPolicy,
        research_trust_profile: inheritedTrustProfile,
        source_experiment_id: baselineExperiment.id,
      });
      setSweepId(response.sweep_id);
      setResearchTrust(response.research_trust);
      setDataAccessPolicy(response.data_access_policy);
      setTotalExperiments(response.total_experiments);
      setSweepStatus('pending');
      setSweepParamNames(Object.keys(gridResult.grid));
      setSweepLoadError(null);
      setSearchParams({ sweep_id: String(response.sweep_id) });
    } catch (err: unknown) {
      setSubmitError(err instanceof Error ? err.message : '创建参数扫描失败');
    } finally {
      setSubmitting(false);
    }
  };

  /* ── Poll results ────────────────────────────────────────────────────── */
  useEffect(() => {
    if (sweepId === null || promotion) return;
    let cancelled = false;
    let timer: number | undefined;
    let consecutiveErrors = 0;

    const poll = async () => {
      try {
        const data = await getSweepResult(sweepId);
        if (cancelled) return;
        consecutiveErrors = 0;
        setSweepLoadError(null);
        setSweepStatus(data.sweep.status);
        setSweepParamNames(Object.keys(data.sweep.sweep_config ?? {}));
        setResearchTrust(data.sweep.research_trust);
        setDataAccessPolicy(data.sweep.data_access_policy);
        setTotalExperiments(data.sweep.total_experiments);
        setCompletedExperiments(data.sweep.completed_experiments);
        setRepairableExperimentIds(data.repairable_experiment_ids ?? []);
        setRawResults(data.experiments);
        setResults(data.experiments.map(mapSelectionResult));
        const restoredPromotion = restoredSweepPromotion(data);
        if (restoredPromotion) {
          setPromotion(restoredPromotion);
          return;
        }
        const allTerminal =
          data.experiments.length >= data.sweep.total_experiments
          && data.experiments.every((experiment) =>
            TERMINAL_STATUSES.includes(experiment.status),
          );
        if (!allTerminal) {
          timer = window.setTimeout(() => void poll(), 2500);
        }
      } catch (err: unknown) {
        if (!cancelled) {
          consecutiveErrors += 1;
          if (consecutiveErrors < 3) {
            timer = window.setTimeout(() => void poll(), 2500);
          } else {
            setSweepLoadError(
              err instanceof Error
                ? err.message
                : `参数扫描 #${sweepId} 不存在或当前账号无权访问`,
            );
          }
        }
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [sweepId, promotion, pollGeneration]);

  const handleRepair = async () => {
    if (sweepId === null || repairableExperimentIds.length === 0 || repairing) return;
    setRepairing(true);
    setRepairError(null);
    try {
      await repairSweep(sweepId);
      setSweepStatus('running');
      setRepairableExperimentIds([]);
      setPollGeneration((value) => value + 1);
    } catch (err: unknown) {
      setRepairError(err instanceof Error ? err.message : '恢复参数扫描失败');
    } finally {
      setRepairing(false);
    }
  };

  /* ── Sorted display results ──────────────────────────────────────────── */
  const sortedResults = useMemo(() => {
    const items = [...results];
    items.sort((a, b) => {
      return compareSweepResults(a, b, metricTarget);
    });
    return items;
  }, [results, metricTarget]);

  const topCompletedId = useMemo(() => {
    const first = sortedResults.find((item) => item.status === 'completed' && sweepMetricValue(item, metricTarget) !== null);
    return first?.experiment_id ?? null;
  }, [sortedResults, metricTarget]);

  const handlePromote = async () => {
    if (sweepId === null || selectedTrialId === null) return;
    const trial = results.find((item) => item.experiment_id === selectedTrialId);
    if (!trial || researchTrust !== 'locked_test' || !canPromoteSweepTrial(trial.status)) return;
    setPromoting(true);
    setPromotionError(null);
    try {
      const result = await promoteSweepExperiment(sweepId, selectedTrialId);
      setPromotion(result);
    } catch (err: unknown) {
      setPromotionError(err instanceof Error ? err.message : '创建锁定最终测试失败');
    } finally {
      setPromoting(false);
    }
  };

  /* ── Charts ──────────────────────────────────────────────────────────── */
  const resultParamNames = useMemo(
    () => Array.from(new Set(results.flatMap((item) => Object.keys(item.params)))),
    [results],
  );
  const paramNames = sweepId === null
    ? Object.keys(gridResult.grid)
    : sweepParamNames.length > 0
      ? sweepParamNames
      : resultParamNames;
  const completedWithMetric = sortedResults.filter(
    (item) => item.status === 'completed' && sweepMetricValue(item, metricTarget) !== null,
  );

  const oneDOption = useMemo(() => {
    if (paramNames.length !== 1 || completedWithMetric.length === 0) return null;
    const paramName = paramNames[0];
    const xData = completedWithMetric.map((item) => formatSweepValue(item.params[paramName]));
    return {
      color: [CHART_COLORS.accent, CHART_COLORS.ochre],
      grid: baseGrid(),
      legend: baseLegend(),
      tooltip: baseTooltip(),
      xAxis: baseXAxis({ data: xData, name: paramName, nameLocation: 'middle', nameGap: 26 }),
      yAxis: [baseYAxis(), baseYAxis({ splitLine: { show: false } })],
      series: [
        {
          name: METRIC_TARGETS.find((item) => item.value === metricTarget)?.label ?? metricTarget,
          type: 'bar',
          data: completedWithMetric.map((item) => sweepMetricValue(item, metricTarget)),
          barMaxWidth: 36,
        },
        {
          name: '选模胜率',
          type: 'line',
          yAxisIndex: 1,
          data: completedWithMetric.map((item) => item.win_rate),
          showSymbol: true,
          lineStyle: { width: 1.5, type: 'dashed' },
        },
      ],
    };
  }, [paramNames, completedWithMetric, metricTarget]);

  const heatmapOption = useMemo(() => {
    if (paramNames.length !== 2 || completedWithMetric.length === 0) return null;
    const [paramA, paramB] = paramNames;
    const valuesA = Array.from(new Set(completedWithMetric.map((item) => Number(item.params[paramA])))).filter(Number.isFinite).sort((a, b) => a - b);
    const valuesB = Array.from(new Set(completedWithMetric.map((item) => Number(item.params[paramB])))).filter(Number.isFinite).sort((a, b) => a - b);
    if (valuesA.length === 0 || valuesB.length === 0) return null;
    const cells: Array<[number, number, number]> = [];
    let min = Number.POSITIVE_INFINITY;
    let max = Number.NEGATIVE_INFINITY;
    for (const item of completedWithMetric) {
      const ai = valuesA.indexOf(Number(item.params[paramA]));
      const bi = valuesB.indexOf(Number(item.params[paramB]));
      const value = sweepMetricValue(item, metricTarget);
      if (ai < 0 || bi < 0 || value === null) continue;
      cells.push([ai, bi, value]);
      min = Math.min(min, value);
      max = Math.max(max, value);
    }
    if (cells.length === 0) return null;
    return {
      grid: baseGrid({ bottom: 48 }),
      tooltip: baseTooltip({
        formatter: (params: { value: [number, number, number] }) =>
          `${paramA}=${valuesA[params.value[0]]}，${paramB}=${valuesB[params.value[1]]}<br/>${METRIC_TARGETS.find((item) => item.value === metricTarget)?.label}: ${params.value[2].toFixed(3)}`,
      }),
      xAxis: baseXAxis({ data: valuesA.map(String), name: paramA, nameLocation: 'middle', nameGap: 26 }),
      yAxis: baseYAxis({ type: 'category', data: valuesB.map(String), name: paramB, splitLine: { show: false } }),
      visualMap: {
        min,
        max,
        calculable: true,
        orient: 'horizontal',
        left: 'center',
        bottom: 0,
        inRange: { color: [CHART_COLORS.split, CHART_COLORS.accent] },
        textStyle: { color: CHART_COLORS.axisLabel, fontSize: 11 },
      },
      series: [{ type: 'heatmap', data: cells, label: { show: false } }],
    };
  }, [paramNames, completedWithMetric, metricTarget]);

  /* ── Results view ────────────────────────────────────────────────────── */
  if (sweepId !== null) {
    const sweepEnded = TERMINAL_STATUSES.includes(sweepStatus);
    const noDisplayable =
      sweepEnded &&
      results.length > 0 &&
      sortedResults.every((item) => item.status !== 'completed' || sweepMetricValue(item, metricTarget) === null);
    const completedNoMetrics = rawResults.filter((item) => item.status === 'completed').length - completedWithMetric.length;
    const failedCount = rawResults.filter((item) => item.status === 'failed').length;
    const cancelledCount = rawResults.filter((item) => item.status === 'cancelled').length;

    return (
      <div>
        <PageHeader
          title={`参数扫描 #${sweepId}`}
          description="排序仅用于辅助人工选择，不会自动晋级；最大回撤越接近 0 越优。"
          breadcrumb={[{ label: '研究' }, { label: '实验中心', to: '/experiment' }, { label: '参数扫描' }]}
          actions={
            <Button
              variant="secondary"
              onClick={() => {
                setSweepId(null);
                setPromotion(null);
                setSearchParams({});
              }}
            >
              新建扫描
            </Button>
          }
          tags={
            <>
              <StatusTag
                variant={
                  sweepStatus === 'completed'
                    ? 'verified'
                    : sweepStatus === 'failed'
                      ? 'error'
                      : sweepStatus === 'cancelled'
                        ? 'neutral'
                        : 'running'
                }
              >
                {
                  sweepStatus === 'completed'
                    ? '扫描完成'
                    : sweepStatus === 'failed'
                      ? '扫描失败'
                      : sweepStatus === 'cancelled'
                        ? '扫描已取消'
                        : '扫描进行中'
                }
              </StatusTag>
              <StatusTag variant={researchTrust === 'locked_test' ? 'verified' : 'legacy'}>
                {researchTrust === 'locked_test' ? '锁定测试协议' : '旧版未锁定协议'}
              </StatusTag>
              <StatusTag variant={dataAccessPolicy === 'cache_only' ? 'verified' : 'neutral'}>
                {dataAccessPolicy === 'cache_only' ? 'PIT 治理数据' : '旧策略（已停用）'}
              </StatusTag>
              <span className="tnum text-xs text-ink-400">
                已结束 {completedExperiments}/{totalExperiments}
              </span>
            </>
          }
        />

        {sweepLoadError && (
          <Banner variant="danger" className="mb-4" title="无法恢复参数扫描">
            {sweepLoadError}
          </Banner>
        )}

        {repairableExperimentIds.length > 0 && (
          <Banner variant="warning" className="mb-4" title="检测到可安全恢复的瞬态失败">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <span>
                {repairableExperimentIds.length} 个成员因本地数据库短暂写冲突失败，可原子重新入队；已有不可变研究清单的成员会创建恢复副本，原始证据不会被覆盖。策略本身的失败不会被自动重试。
              </span>
              <Button
                variant="secondary"
                loading={repairing}
                onClick={() => void handleRepair()}
              >
                重试可恢复成员（{repairableExperimentIds.length}）
              </Button>
            </div>
          </Banner>
        )}

        {repairError && (
          <Banner variant="danger" className="mb-4" title="恢复失败">
            {repairError}
          </Banner>
        )}

        {researchTrust !== 'locked_test' && (
          <Banner variant="danger" className="mb-4">
            此扫描使用旧版未锁定协议，不能创建锁定最终测试。请重新创建双阶段扫描。
          </Banner>
        )}

        {promotion && (
          <Card className="mb-4" title="唯一晋级实验">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="text-sm leading-6 text-ink-600">
                <p>
                  锁定最终测试实验 <span className="tnum font-semibold">#{promotion.experiment_id}</span>
                </p>
                <p className="mt-1 text-ink-500">
                  来源组合 #{promotion.source_experiment_id} 已提交。锁定测试结果不参与本页参数排名。
                </p>
              </div>
              <Button onClick={() => navigate(`/experiment/${promotion.experiment_id}`)}>
                查看晋级实验详情
                <Icon name="arrowRight" className="h-4 w-4" />
              </Button>
            </div>
          </Card>
        )}

        {results.length === 0 ? (
          <Card>
            <EmptyState icon="compare" title="扫描已提交，等待结果..." description="选模窗口中的参数组合完成后会出现在这里。" />
          </Card>
        ) : noDisplayable ? (
          <Card>
            <EmptyState
              icon="compare"
              title="扫描已结束，但所选指标没有可展示的结果。"
              description={`已完成但无指标 ${Math.max(0, completedNoMetrics)} 个，失败 ${failedCount} 个，已取消 ${cancelledCount} 个。可前往实验中心查看失败日志。`}
            />
          </Card>
        ) : (
          <>
            {completedWithMetric.length === 0 && (
              <Banner variant="info" className="mb-4">等待已完成的实验结果...</Banner>
            )}

            {oneDOption && (
              <Card title="参数敏感度" className="mb-4" padding="sm">
                <EChart option={oneDOption} style={{ height: 300 }} notMerge />
              </Card>
            )}
            {paramNames.length === 2 &&
              (heatmapOption ? (
                <Card title="二维参数热力图" className="mb-4" padding="sm">
                  <EChart option={heatmapOption} style={{ height: 340 }} notMerge />
                </Card>
              ) : (
                <Banner variant="info" className="mb-4">
                  二维热力图仅支持两个参数均为数值的扫描；本次扫描包含非数值参数。
                </Banner>
              ))}

            <Card title="选模结果" description='只有状态为“已完成”的组合可以被人工选择' padding="none">
              <div className="overflow-x-auto scrollbar-thin">
                <table className="w-full text-sm" style={{ minWidth: 900 }}>
                  <caption className="sr-only">选模窗口结果排名</caption>
                  <thead>
                    <tr className="border-b border-ink-200 bg-ink-50">
                      {['排名', '参数组合', '选模 Sharpe', '选模收益', '选模回撤', '选模胜率', '状态', '晋级'].map((header) => (
                        <th key={header} scope="col" className="px-3 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-ink-500">
                          {header}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-ink-100">
                    {sortedResults.map((item, index) => {
                      const promotable = researchTrust === 'locked_test' && canPromoteSweepTrial(item.status);
                      return (
                        <tr key={item.experiment_id} className={selectedTrialId === item.experiment_id ? 'bg-accent-50' : ''}>
                          <td className="tnum px-3 py-2.5 text-ink-500">#{index + 1}</td>
                          <td className="px-3 py-2.5">
                            <span className="font-mono text-xs text-ink-700">
                              {Object.entries(item.params)
                                .map(([key, value]) => `${key}=${formatSweepValue(value)}`)
                                .join('，')}
                            </span>
                            <span className="tnum ml-2 text-2xs text-ink-400">#{item.experiment_id}</span>
                          </td>
                          <td className="tnum px-3 py-2.5">{item.sharpe !== null ? item.sharpe.toFixed(2) : '-'}</td>
                          <td className="tnum px-3 py-2.5">{formatPct(item.return)}</td>
                          <td className="tnum px-3 py-2.5">{formatPct(item.max_drawdown)}</td>
                          <td className="tnum px-3 py-2.5">{formatPct(item.win_rate)}</td>
                          <td className="px-3 py-2.5">
                            {item.status === 'completed' ? (
                              <StatusTag variant="verified">已完成</StatusTag>
                            ) : item.status === 'failed' ? (
                              <StatusTag variant="error">失败</StatusTag>
                            ) : item.status === 'cancelled' ? (
                              <StatusTag variant="neutral">已取消</StatusTag>
                            ) : (
                              <StatusTag variant="running">进行中</StatusTag>
                            )}
                          </td>
                          <td className="px-3 py-2.5">
                            {researchTrust !== 'locked_test' ? (
                              <span className="text-xs text-danger-fg">旧协议不可晋级</span>
                            ) : !canPromoteSweepTrial(item.status) ? (
                              <span className="text-xs text-ink-400">完成后可选择</span>
                            ) : (
                              <div className="flex items-center gap-2">
                                {item.experiment_id === topCompletedId && selectedTrialId !== item.experiment_id && (
                                  <span className="text-2xs text-ink-400">当前排序首位，仅供参考</span>
                                )}
                                <Button
                                  variant={selectedTrialId === item.experiment_id ? 'primary' : 'secondary'}
                                  size="sm"
                                  disabled={!promotable || promotion !== null}
                                  onClick={() =>
                                    setSelectedTrialId((current) =>
                                      current === item.experiment_id ? null : item.experiment_id,
                                    )
                                  }
                                >
                                  {selectedTrialId === item.experiment_id ? '已选择' : '选择此组合'}
                                </Button>
                              </div>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </Card>

            {selectedTrialId !== null && !promotion && (
              <Card className="mt-4" title={`确认唯一晋级组合 #${selectedTrialId}`}>
                <p className="text-sm leading-6 text-ink-600">
                  当前选择由你明确作出，不是系统按 Sharpe 自动决定。确认后将使用锁定最终测试窗口创建一个新实验，同一扫描不能改选其他组合。
                </p>
                {promotionError && (
                  <Banner variant="danger" className="mt-3">{promotionError}</Banner>
                )}
                <div className="mt-4 flex items-center gap-2">
                  <Button onClick={() => void handlePromote()} loading={promoting}>
                    确认并创建锁定测试
                  </Button>
                  <Button variant="ghost" onClick={() => setSelectedTrialId(null)}>
                    取消选择
                  </Button>
                </div>
              </Card>
            )}
          </>
        )}
      </div>
    );
  }

  /* ── Form view ───────────────────────────────────────────────────────── */
  return (
    <div>
      <PageHeader
        title="参数扫描"
        description="在选模窗口比较参数组合，再由你明确选择唯一组合进入锁定最终测试。"
        breadcrumb={[{ label: '研究' }, { label: '实验中心', to: '/experiment' }, { label: '参数扫描' }]}
      />

      {/* Baseline */}
      <Card title="基准实验" description="扫描继承基准实验的策略、股票池与成本口径" className="mb-4">
        <div className="max-w-xl">
          <Select
            label="选择基准实验"
            value={baselineId}
            onChange={(event) => {
              const value = event.target.value;
              handleSelectBaseline(value);
              setSearchParams(value ? { baseline_id: value } : {});
            }}
            placeholder="选择一个已完成配置的实验..."
            options={pickerExperiments.map((experiment) => ({
              value: String(experiment.id),
              label: `#${experiment.id} ${experiment.name}（${experiment.strategy_id}）`,
            }))}
          />
        </div>
        {selectionLoading && (
          <p className="mt-3 text-sm text-ink-500">正在加载实验运行快照和策略参数定义...</p>
        )}
        {baselineError && (
          <Banner variant="danger" className="mt-3">
            {baselineError}
          </Banner>
        )}
        {baselineExperiment && strategy && (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <StatusTag variant="info">{strategy.display_name}</StatusTag>
            <StatusTag variant="neutral">{baselineExperiment.pool_preset ?? '自定义股票池'}</StatusTag>
            <StatusTag variant={baselineExperiment.data_access_policy === 'cache_only' ? 'verified' : 'neutral'}>
              {baselineExperiment.data_access_policy === 'cache_only' ? 'PIT 治理数据' : '旧策略（已停用）'}
            </StatusTag>
            {baselineExperiment.pool_industries.length > 0 && (
              <StatusTag variant="neutral">
                行业：{baselineExperiment.pool_industries.join('、')}
              </StatusTag>
            )}
            {strategyTrainingMode(strategy) === 'periodic' && (
              <StatusTag variant="warning">周期重训练 · 训练窗口由平台生成</StatusTag>
            )}
          </div>
        )}
        {strategy && strategy.params.length === 0 && (
          <Banner variant="warning" className="mt-3">
            策略“{strategy.display_name}”没有可配置参数，无法创建参数扫描。
          </Banner>
        )}
      </Card>

      {/* Two-phase windows */}
      <Card
        title="双阶段研究窗口"
        description="参数组合只在选模窗口中运行和排名。锁定最终测试数据不会下发给扫描实验，仅在你人工选择一个已完成组合后使用一次。"
        className="mb-4"
      >
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <fieldset className="rounded border border-ink-200 p-4">
            <legend className="px-1 text-sm font-semibold text-ink-800">阶段 01 · 选模 / 验证窗口</legend>
            <p className="mb-3 text-xs text-ink-500">所有表格、图表和排序指标均来自此窗口。</p>
            {isTrainOnce && (
              <p className={`mb-3 text-xs ${windowErrors.trainWindow ? 'font-medium text-danger-fg' : 'text-ink-500'}`}>
                固定训练窗口：{baselineExperiment?.train_start ?? '-'} 至 {baselineExperiment?.train_end ?? '-'}
                {windowErrors.trainWindow && `（${windowErrors.trainWindow}）`}
              </p>
            )}
            <div className="grid grid-cols-2 gap-3">
              <Input
                label="选模开始"
                type="date"
                value={selectionStart}
                onChange={(event) => setSelectionStart(event.target.value)}
                error={windowErrors.selectionStart}
              />
              <Input
                label="选模结束"
                type="date"
                value={selectionEnd}
                onChange={(event) => setSelectionEnd(event.target.value)}
                error={windowErrors.selectionEnd}
              />
            </div>
          </fieldset>
          <fieldset className="rounded border border-danger-border bg-danger-bg/40 p-4">
            <legend className="px-1 text-sm font-semibold text-danger-strong">阶段 02 · 锁定最终测试窗口</legend>
            <p className="mb-3 text-xs text-danger-strong">
              必须完全晚于选模窗口，扫描期间不可见。锁定测试不是第二轮参数选择。晋级后应按最终验证结果决定是否部署，不应返回扫描阶段继续调参。
            </p>
            <div className="grid grid-cols-2 gap-3">
              <Input
                label="锁定测试开始"
                type="date"
                value={lockedTestStart}
                onChange={(event) => setLockedTestStart(event.target.value)}
                error={windowErrors.lockedTestStart}
              />
              <Input
                label="锁定测试结束"
                type="date"
                value={lockedTestEnd}
                onChange={(event) => setLockedTestEnd(event.target.value)}
                error={windowErrors.lockedTestEnd}
              />
            </div>
          </fieldset>
        </div>
      </Card>

      {/* Parameter grid */}
      {strategy && strategy.params.length > 0 && (
        <Card
          title="参数组合"
          description="各参数取值数的笛卡尔积，单次安全上限为 100 个"
          className="mb-4"
          actions={
            <Button variant="secondary" size="sm" onClick={() => setRows((current) => [...current, newDraftRow()])}>
              <Icon name="plus" className="h-4 w-4" />
              添加参数
            </Button>
          }
        >
          <div className="space-y-3">
            {rows.map((row) => {
              const param: ParamField | undefined = strategy.params.find((item) => item.name === row.name);
              const forcedCustom = param
                ? ['bool', 'boolean', 'choice', 'str', 'string', 'text'].includes(param.type.toLowerCase()) || Boolean(param.choices)
                : false;
              return (
                <div key={row.id} className="rounded border border-ink-200 p-3.5">
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-6">
                    <Select
                      label="参数"
                      value={row.name}
                      onChange={(event) => selectRowParam(row.id, event.target.value)}
                      placeholder="选择参数..."
                      options={strategy.params.map((item) => ({ value: item.name, label: item.name }))}
                    />
                    <Select
                      label="取值方式"
                      value={row.mode}
                      disabled={forcedCustom}
                      onChange={(event) => updateRow(row.id, { mode: event.target.value as SweepParameterDraft['mode'] })}
                      options={[
                        { value: 'linear', label: '等距 Linear' },
                        { value: 'log', label: '对数 Log' },
                        { value: 'custom', label: '自定义' },
                      ]}
                      hint={forcedCustom ? '布尔和选项参数只能使用自定义取值' : undefined}
                    />
                    {row.mode !== 'custom' ? (
                      <>
                        <Input label="最小值" value={row.min} onChange={(event) => updateRow(row.id, { min: event.target.value })} />
                        <Input label="最大值" value={row.max} onChange={(event) => updateRow(row.id, { max: event.target.value })} />
                        <Input label="取值数" type="number" min={2} max={100} value={row.steps} onChange={(event) => updateRow(row.id, { steps: event.target.value })} />
                      </>
                    ) : (
                      <div className="lg:col-span-3">
                        <Input
                          label="自定义取值"
                          value={row.custom}
                          onChange={(event) => updateRow(row.id, { custom: event.target.value })}
                          placeholder={param?.choices ? `选项：${param.choices.join(', ')}` : '逗号分隔，例如 10, 20, 30'}
                          hint={param?.choices ? `可选：${param.choices.join(' / ')}` : undefined}
                        />
                      </div>
                    )}
                    <div className="flex items-end justify-end">
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label="删除此参数行"
                        disabled={rows.length === 1}
                        onClick={() => setRows((current) => current.filter((item) => item.id !== row.id))}
                      >
                        <Icon name="trash" className="h-4 w-4" />
                      </Button>
                    </div>
                  </div>
                  {gridResult.rowErrors[row.id] && (
                    <p role="alert" className="mt-2 text-xs text-danger-fg">
                      {gridResult.rowErrors[row.id]}
                    </p>
                  )}
                </div>
              );
            })}
          </div>

          <div className="mt-4">
            {exceedsCap ? (
              <Banner variant="danger">
                预计生成 {gridResult.total} 个实验，超出上限，请减少参数或取值数量。
              </Banner>
            ) : (
              <Banner variant="info">
                预计生成 {gridResult.total} 个实验（各参数取值数的笛卡尔积，单次安全上限为 {MAX_EXPERIMENTS} 个）。
              </Banner>
            )}
          </div>

          <div className="mt-4 max-w-xs">
            <Select
              label="排序指标（仅供人工参考）"
              value={metricTarget}
              onChange={(event) => setMetricTarget(event.target.value as SweepMetricTarget)}
              options={METRIC_TARGETS.map((item) => ({ value: item.value, label: item.label }))}
            />
          </div>
        </Card>
      )}

      {submitError && (
        <Banner variant="danger" className="mb-4" title="无法创建参数扫描">
          {submitError}
        </Banner>
      )}

      <div className="flex justify-end">
        <Button onClick={() => void handleSubmit()} loading={submitting} disabled={!canSubmit}>
          <Icon name="play" className="h-4 w-4" />
          提交扫描{combinations.length > 0 ? `（${combinations.length} 个组合）` : ''}
        </Button>
      </div>
    </div>
  );
}
