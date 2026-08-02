import { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router';
import {
  buildPortfolioCandidates,
  getStrategyCorrelation,
  getExperiment,
  listExperiments,
  type PortfolioCandidateSet,
  type StrategyCorrelationMethod,
  type StrategyCorrelationPair,
  type StrategyCorrelationReport,
} from '../../services/experiments';
import type { Experiment } from '../../types/experiment';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import EChart from '../../components/shared/EChart';
import EmptyState from '../../components/shared/EmptyState';
import Icon from '../../components/shared/Icon';
import Input from '../../components/shared/Input';
import PageHeader from '../../components/shared/PageHeader';
import Select from '../../components/shared/Select';
import Skeleton from '../../components/shared/Skeleton';
import {
  buildCorrelationHeatmap,
  CORRELATION_CLASS_LABEL,
  pairKey,
} from './strategyCorrelationView';

const MAX_SELECTION = 20;

const METHOD_OPTIONS = [
  { value: 'pearson', label: 'Pearson（线性相关）' },
  { value: 'spearman', label: 'Spearman（秩相关）' },
];

function pairDescription(pair: StrategyCorrelationPair): string {
  if (pair.unavailable_reason === 'insufficient_overlap') {
    return '共同观测不足，未计算相关系数。';
  }
  if (pair.unavailable_reason === 'constant_series') {
    return '至少一条收益序列为常数，相关系数没有定义。';
  }
  if (pair.correlation === null) return '相关系数不可用。';
  if (pair.correlation >= 0.8) return '收益高度同向，组合后的风险分散效果可能有限。';
  if (pair.correlation <= -0.25) return '历史收益呈负相关，可作为分散化候选，但仍需检查成本、容量和样本外稳定性。';
  if (Math.abs(pair.correlation) <= 0.2) return '历史收益低相关，可能提供分散化，但不代表未来仍保持低相关。';
  return '历史收益存在中等相关性，需结合回撤共振与市场状态进一步判断。';
}

export default function StrategyCorrelationPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const initialIds = useMemo(
    () => (location.state as { ids?: number[] } | null)?.ids ?? [],
    [location.state],
  );
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [selectedIds, setSelectedIds] = useState<number[]>(initialIds.slice(0, MAX_SELECTION));
  const [method, setMethod] = useState<StrategyCorrelationMethod>('pearson');
  const [minObservations, setMinObservations] = useState(60);
  const [tailFraction, setTailFraction] = useState(0.1);
  const [weights, setWeights] = useState<Record<number, number>>(
    Object.fromEntries(initialIds.map((id) => [id, 1])),
  );
  const [search, setSearch] = useState('');
  const [report, setReport] = useState<StrategyCorrelationReport | null>(null);
  const [candidateSet, setCandidateSet] = useState<PortfolioCandidateSet | null>(null);
  const [selectedPairKey, setSelectedPairKey] = useState<string | null>(null);
  const [loadingCatalog, setLoadingCatalog] = useState(true);
  const [analyzing, setAnalyzing] = useState(false);
  const [buildingCandidates, setBuildingCandidates] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoadingCatalog(true);
    Promise.all([
      listExperiments({ status: 'completed', page: 1, limit: 100 }),
      Promise.allSettled(initialIds.slice(0, MAX_SELECTION).map((id) => getExperiment(id))),
    ])
      .then(([result, requested]) => {
        if (!active) return;
        const requestedCompleted = requested.flatMap((entry) => (
          entry.status === 'fulfilled' && entry.value.status === 'completed'
            ? [entry.value]
            : []
        ));
        const merged = [...requestedCompleted, ...result.items].filter(
          (experiment, index, items) =>
            items.findIndex((item) => item.id === experiment.id) === index,
        );
        setExperiments(merged);
        const visibleIds = new Set(merged.map((experiment) => experiment.id));
        setSelectedIds((current) => current.filter((id) => visibleIds.has(id)));
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : '加载已完成实验失败');
      })
      .finally(() => {
        if (active) setLoadingCatalog(false);
      });
    return () => { active = false; };
  }, [initialIds]);

  const runAnalysis = useCallback(async () => {
    if (selectedIds.length < 2) {
      setError('请至少选择两个已完成实验。');
      return;
    }
    setAnalyzing(true);
    setCandidateSet(null);
    setError(null);
    setSelectedPairKey(null);
    try {
      const selectedWeights = selectedIds.map((id) => weights[id] ?? 1);
      if (selectedWeights.some((value) => !Number.isFinite(value) || value < 0)
        || selectedWeights.every((value) => value === 0)) {
        setError('组合权重必须为非负数，且至少一个大于 0。');
        return;
      }
      const next = await getStrategyCorrelation(
        selectedIds,
        method,
        minObservations,
        selectedWeights,
        tailFraction,
      );
      setReport(next);
      const firstPair = next.pairs.find((pair) => pair.correlation !== null) ?? next.pairs[0];
      setSelectedPairKey(
        firstPair ? pairKey(firstPair.left_experiment_id, firstPair.right_experiment_id) : null,
      );
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '策略相关性分析失败');
    } finally {
      setAnalyzing(false);
    }
  }, [method, minObservations, selectedIds, tailFraction, weights]);

  const buildCandidates = useCallback(async () => {
    if (selectedIds.length < 3) {
      setError('生成组合候选至少需要三个不同的非机器学习单策略实验。');
      return;
    }
    setBuildingCandidates(true);
    setError(null);
    try {
      setCandidateSet(await buildPortfolioCandidates(
        selectedIds,
        method,
        minObservations,
        tailFraction,
      ));
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '组合候选生成失败');
    } finally {
      setBuildingCandidates(false);
    }
  }, [method, minObservations, selectedIds, tailFraction]);

  useEffect(() => {
    if (initialIds.length >= 2 && !loadingCatalog) {
      void runAnalysis();
    }
    // Run once for the explicit navigation intent; later changes require the
    // visible "开始分析" action.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadingCatalog]);

  const toggleExperiment = (id: number) => {
    setReport(null);
    setCandidateSet(null);
    setSelectedPairKey(null);
    setSelectedIds((current) => {
      if (current.includes(id)) return current.filter((item) => item !== id);
      if (current.length >= MAX_SELECTION) return current;
      setWeights((currentWeights) => ({ ...currentWeights, [id]: 1 }));
      return [...current, id];
    });
  };

  const filteredExperiments = experiments.filter((experiment) => {
    const query = search.trim().toLowerCase();
    return !query
      || experiment.name.toLowerCase().includes(query)
      || experiment.strategy_id.toLowerCase().includes(query)
      || String(experiment.id).includes(query);
  });
  const names = useMemo(
    () => new Map(experiments.map((experiment) => [experiment.id, experiment.name])),
    [experiments],
  );
  const selectedPair = report?.pairs.find(
    (pair) => pairKey(pair.left_experiment_id, pair.right_experiment_id) === selectedPairKey,
  ) ?? null;
  const heatmapOption = useMemo(
    () => (report ? buildCorrelationHeatmap(report) : null),
    [report],
  );
  const heatmapEvents = useMemo(() => ({
    click: (params: { data?: { leftId?: number; rightId?: number } }) => {
      const leftId = params.data?.leftId;
      const rightId = params.data?.rightId;
      if (leftId && rightId && leftId !== rightId) {
        setSelectedPairKey(pairKey(leftId, rightId));
      }
    },
  }), []);

  return (
    <div>
      <PageHeader
        title="策略相关性分析"
        description="基于已落库净值推导并按日期对齐日收益，识别策略重复暴露与潜在分散化关系。"
        breadcrumb={[
          { label: '研究' },
          { label: '实验中心', to: '/experiment' },
          { label: '策略相关性' },
        ]}
        actions={
          <Button variant="secondary" size="sm" onClick={() => navigate('/experiment')}>
            <Icon name="arrowLeft" className="h-4 w-4" />
            返回实验中心
          </Button>
        }
      />

      <Banner variant="info" className="mb-4" title="研究口径">
        这是事后分散化诊断，不参与选模或自动构建组合。相关性会随市场状态变化，仍需样本外验证、成本和容量检验。
      </Banner>
      {error && <Banner variant="danger" className="mb-4">{error}</Banner>}

      <Card title="1. 选择实验与统计口径" className="mb-4">
        <div className="grid gap-4 lg:grid-cols-[1fr_240px]">
          <div>
            <Input
              label="筛选已完成实验"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="名称、策略 ID 或实验编号"
            />
            <div
              className="mt-2 max-h-64 overflow-y-auto rounded border border-ink-200"
              aria-label="已完成实验候选"
            >
              {loadingCatalog ? (
                <div className="space-y-2 p-3"><Skeleton className="h-8" /><Skeleton className="h-8" /></div>
              ) : filteredExperiments.length === 0 ? (
                <p className="p-4 text-sm text-ink-500">没有匹配的已完成实验。</p>
              ) : filteredExperiments.map((experiment) => (
                <label
                  key={experiment.id}
                  className="flex cursor-pointer items-start gap-3 border-b border-ink-100 px-3 py-2.5 last:border-0 hover:bg-ink-50"
                >
                  <input
                    type="checkbox"
                    aria-label={`选择实验 ${experiment.name}`}
                    checked={selectedIds.includes(experiment.id)}
                    disabled={!selectedIds.includes(experiment.id) && selectedIds.length >= MAX_SELECTION}
                    onChange={() => toggleExperiment(experiment.id)}
                    className="mt-0.5 h-4 w-4 rounded border-ink-300 text-accent-700"
                  />
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-medium text-ink-800">
                      #{experiment.id} {experiment.name}
                    </span>
                    <span className="block font-mono text-2xs text-ink-500">
                      {experiment.strategy_id} · {experiment.test_start} ~ {experiment.test_end}
                    </span>
                  </span>
                </label>
              ))}
            </div>
            <p className="mt-2 text-xs text-ink-500 tnum">
              已选择 {selectedIds.length}/{MAX_SELECTION} 个实验
            </p>
            {selectedIds.length > 0 && (
              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                {selectedIds.map((id) => (
                  <Input
                    key={id}
                    label={`#${id} 研究权重`}
                    type="number"
                    min={0}
                    step={0.05}
                    value={weights[id] ?? 1}
                    onChange={(event) => {
                      setWeights((current) => ({
                        ...current,
                        [id]: Number(event.target.value),
                      }));
                      setReport(null);
                    }}
                  />
                ))}
              </div>
            )}
          </div>
          <div className="space-y-3">
            <Select
              label="相关系数"
              value={method}
              onChange={(event) => {
                setMethod(event.target.value as StrategyCorrelationMethod);
                setReport(null);
              }}
              options={METHOD_OPTIONS}
            />
            <Input
              label="最少共同日收益观测"
              type="number"
              min={10}
              max={2520}
              value={minObservations}
              onChange={(event) => {
                setMinObservations(Math.max(10, Math.min(2520, Number(event.target.value) || 10)));
                setReport(null);
              }}
            />
            <Input
              label="尾部样本比例"
              type="number"
              min={0.01}
              max={0.25}
              step={0.01}
              value={tailFraction}
              onChange={(event) => {
                setTailFraction(Math.max(0.01, Math.min(0.25, Number(event.target.value) || 0.1)));
                setReport(null);
              }}
              hint="0.10 表示最差 10% 收益区间"
            />
            <p className="text-xs leading-5 text-ink-500">
              Pearson 适合判断线性共振；Spearman 对极端值更稳健，用于判断收益排序是否同向。
            </p>
            <Button
              className="w-full"
              disabled={selectedIds.length < 2 || analyzing}
              loading={analyzing}
              onClick={() => void runAnalysis()}
            >
              开始分析
            </Button>
          </div>
        </div>
      </Card>

      {report ? (
        <>
          <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
            {[
              ['可计算配对', report.summary.available_pairs],
              ['不可计算配对', report.summary.unavailable_pairs],
              ['高度同向', report.summary.high_correlation_pairs],
              ['负相关候选', report.summary.negative_diversifier_pairs],
            ].map(([label, value]) => (
              <Card key={String(label)} padding="sm">
                <p className="text-xs text-ink-500">{label}</p>
                <p className="mt-1 text-xl font-semibold text-ink-900 tnum">{value}</p>
              </Card>
            ))}
          </div>

          <Card
            title="PIT 证据化组合候选"
            description="从所选非机器学习单策略的收益、相关性、尾部共振与持仓重叠生成 5 个可复现草案；不会自动提交实验、注册策略或进入模拟盘。"
            className="mb-4"
            actions={(
              <Button
                size="sm"
                disabled={selectedIds.length < 3 || buildingCandidates}
                loading={buildingCandidates}
                onClick={() => void buildCandidates()}
              >
                生成 5 个草案
              </Button>
            )}
          >
            {!candidateSet ? (
              <p className="text-sm text-ink-500">
                先完成上方相关性分析，再选择至少三个不同的非 ML 单策略生成候选。旧实验、无 PIT 清单或清单哈希不一致会被后端拒绝。
              </p>
            ) : (
              <div className="space-y-3">
                <Banner variant="info">
                  已验证 {candidateSet.common_observations} 个共同日收益观测（{candidateSet.common_start} ~ {candidateSet.common_end}）。来源摘要 {candidateSet.source_digest.slice(0, 12)}…
                </Banner>
                <div className="grid gap-3 xl:grid-cols-2">
                  {candidateSet.candidates.map((candidate) => (
                    <article key={candidate.candidate_id} className="rounded border border-ink-200 p-3">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <h3 className="text-sm font-semibold text-ink-900">{candidate.name}</h3>
                          <p className="mt-0.5 font-mono text-2xs text-ink-500">{candidate.candidate_id}</p>
                        </div>
                        <span className={`rounded px-2 py-1 text-2xs ${candidate.risk_constraints.passed ? 'bg-success-bg text-success-fg' : 'bg-warn-bg text-warn-fg'}`}>
                          {candidate.risk_constraints.passed ? '约束通过' : '需要复核'}
                        </span>
                      </div>
                      <div className="mt-3 space-y-1.5 text-xs">
                        {candidate.components.map((component) => (
                          <div key={component.experiment_id} className="flex items-center justify-between gap-3">
                            <span className="truncate">#{component.experiment_id} {component.strategy_id}</span>
                            <span className="font-mono">{(component.weight * 100).toFixed(1)}%</span>
                          </div>
                        ))}
                      </div>
                      <p className="mt-3 text-xs text-ink-500">
                        来源清单 {candidate.source_manifest.source_run_manifest_hashes.length} 份 · 定义 {candidate.source_manifest.definition_sha256.slice(0, 12)}…
                      </p>
                      {!candidate.risk_constraints.holding_evidence_complete && (
                        <p className="mt-1 text-xs text-warn-fg">持仓证据不完整，需补齐后再进入候选实验。</p>
                      )}
                      {candidate.risk_constraints.violations.length > 0 && (
                        <p className="mt-1 text-xs text-warn-fg">
                          {candidate.risk_constraints.violations.join('；')}
                        </p>
                      )}
                      <Button
                        className="mt-3"
                        size="sm"
                        variant="secondary"
                        disabled={!candidate.publication.eligible_for_experiment}
                        onClick={() => {
                          const source = experiments.find(
                            (item) => item.id === candidate.components[0]?.experiment_id,
                          );
                          navigate(`/experiment/new?strategy_id=${candidate.strategy_id}`, {
                            state: {
                              portfolioCandidateDraft: {
                                name: `${candidate.name} · ${candidate.candidate_id}`,
                                params: candidate.params,
                                poolPreset: source?.pool_preset ?? '',
                                customCodes: source?.pool_custom_codes ?? [],
                                industries: source?.pool_industries ?? [],
                                testStart: candidateSet.common_start,
                                testEnd: candidateSet.common_end,
                              },
                            },
                          });
                        }}
                      >
                        带入新建实验
                      </Button>
                    </article>
                  ))}
                </div>
              </div>
            )}
          </Card>

          {report.portfolio_contribution && (
            <Card
              title="边际风险 / 收益贡献"
              description="按上方研究权重归一化计算；仅生成审查建议，不会修改组合、订单或策略池。"
              className="mb-4"
            >
              {report.portfolio_contribution.available ? (
                <>
                  <div className="mb-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
                    {[
                      ['共同观测', report.portfolio_contribution.common_observations],
                      ['组合年化收益', `${((report.portfolio_contribution.annualized_return ?? 0) * 100).toFixed(2)}%`],
                      ['组合年化波动', `${((report.portfolio_contribution.annualized_volatility ?? 0) * 100).toFixed(2)}%`],
                      ['尾部观测', report.portfolio_contribution.tail_observations ?? 0],
                    ].map(([label, value]) => (
                      <div key={String(label)} className="rounded border border-ink-200 p-3">
                        <p className="text-xs text-ink-500">{label}</p>
                        <p className="mt-1 font-mono text-lg">{value}</p>
                      </div>
                    ))}
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[720px] text-sm">
                      <thead className="border-b border-ink-200 text-left text-xs text-ink-500">
                        <tr>
                          <th className="p-2">实验</th>
                          <th className="p-2 text-right">权重</th>
                          <th className="p-2 text-right">年化收益贡献</th>
                          <th className="p-2 text-right">年化风险贡献</th>
                          <th className="p-2 text-right">尾部收益贡献</th>
                          <th className="p-2">约束建议</th>
                        </tr>
                      </thead>
                      <tbody>
                        {report.portfolio_contribution.contributions?.map((item) => {
                          const suggestion = report.constraint_suggestions?.find(
                            (candidate) => candidate.experiment_id === item.experiment_id,
                          );
                          return (
                            <tr key={item.experiment_id} className="border-b border-ink-100">
                              <td className="p-2">#{item.experiment_id} {names.get(item.experiment_id)}</td>
                              <td className="p-2 text-right font-mono">{(item.weight * 100).toFixed(1)}%</td>
                              <td className="p-2 text-right font-mono">{(item.annual_return_contribution * 100).toFixed(2)}%</td>
                              <td className="p-2 text-right font-mono">{item.annual_risk_contribution == null ? '-' : `${(item.annual_risk_contribution * 100).toFixed(2)}%`}</td>
                              <td className="p-2 text-right font-mono">{item.tail_return_contribution == null ? '-' : `${(item.tail_return_contribution * 100).toFixed(3)}%`}</td>
                              <td className="p-2 text-xs">
                                {suggestion ? `建议上限 ${(suggestion.suggested_max_weight * 100).toFixed(0)}%：${suggestion.reasons.join('；')}` : '-'}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </>
              ) : (
                <Banner variant="warning">
                  共同收益观测不足，无法计算组合边际贡献。
                </Banner>
              )}
            </Card>
          )}

          {report.warnings.length > 0 && (
            <Card title="分散化与数据质量提示" className="mb-4">
              <div className="space-y-2">
                {report.warnings.slice(0, 20).map((warning, index) => (
                  <Banner
                    key={`${warning.code}-${warning.experiment_ids.join('-')}-${index}`}
                    variant={warning.level === 'danger' ? 'danger' : 'warning'}
                  >
                    {warning.message}
                  </Banner>
                ))}
                {report.warnings.length > 20 && (
                  <p className="text-xs text-ink-500 tnum">
                    仅展示前 20 条；其余 {report.warnings.length - 20} 条可在配对明细中检查。
                  </p>
                )}
              </div>
            </Card>
          )}

          <Card
            title={`${report.method === 'pearson' ? 'Pearson' : 'Spearman'} 日收益相关性热力图`}
            description={`单元格为相关系数；悬停查看共同观测数，点击非对角单元格查看配对详情。最少共同观测 ${report.min_observations}。`}
            className="mb-4"
          >
            {heatmapOption && (
              <>
                <EChart
                  option={heatmapOption}
                  onEvents={heatmapEvents}
                  style={{ height: Math.max(430, report.experiments.length * 42) }}
                  notMerge
                />
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-500" aria-label="相关性热力图颜色图例">
                  {[
                    ['bg-danger-fg', `高度同向 ≥ ${report.thresholds.high_positive.toFixed(2)}`],
                    ['bg-warn-border', '正相关'],
                    ['bg-ink-100', `低相关 |r| ≤ ${report.thresholds.low_absolute.toFixed(2)}`],
                    ['bg-info-border', '负相关'],
                    ['bg-accent-800', `高度反向 ≤ -${report.thresholds.high_positive.toFixed(2)}`],
                    ['bg-ink-300', '不可计算'],
                  ].map(([color, label]) => (
                    <span key={label} className="inline-flex items-center gap-1.5">
                      <span className={`h-2.5 w-2.5 rounded-sm ${color}`} />
                      {label}
                    </span>
                  ))}
                </div>
              </>
            )}
          </Card>

          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
            <Card title="配对明细" padding="none">
              <div className="max-h-[32rem] overflow-auto">
                <table className="w-full min-w-[640px] text-sm">
                  <caption className="sr-only">策略相关性配对明细</caption>
                  <thead className="sticky top-0 bg-ink-50">
                    <tr className="border-b border-ink-200 text-left text-xs text-ink-500">
                      <th className="px-3 py-2">策略实验</th>
                      <th className="px-3 py-2 text-right">相关系数</th>
                      <th className="px-3 py-2 text-right">共同观测</th>
                      <th className="px-3 py-2 text-right">尾部相关</th>
                      <th className="px-3 py-2 text-right">持仓重叠</th>
                      <th className="px-3 py-2">判断</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-ink-100">
                    {report.pairs.map((pair) => {
                      const key = pairKey(pair.left_experiment_id, pair.right_experiment_id);
                      return (
                        <tr
                          key={key}
                          className={key === selectedPairKey ? 'bg-accent-50' : 'hover:bg-ink-50'}
                        >
                          <td className="px-3 py-2">
                            <button
                              type="button"
                              className="text-left text-accent-800 hover:underline"
                              onClick={() => setSelectedPairKey(key)}
                            >
                              #{pair.left_experiment_id} {names.get(pair.left_experiment_id)}
                              <span className="block">#{pair.right_experiment_id} {names.get(pair.right_experiment_id)}</span>
                            </button>
                          </td>
                          <td className="px-3 py-2 text-right font-mono">
                            {pair.correlation === null ? '-' : pair.correlation.toFixed(3)}
                          </td>
                          <td className="px-3 py-2 text-right tnum">{pair.overlap}</td>
                          <td className="px-3 py-2 text-right font-mono">
                            {pair.tail_correlation?.correlation?.toFixed(3) ?? '-'}
                          </td>
                          <td className="px-3 py-2 text-right font-mono">
                            {pair.holding_overlap?.mean == null
                              ? '-'
                              : `${(pair.holding_overlap.mean * 100).toFixed(1)}%`}
                          </td>
                          <td className="px-3 py-2">{CORRELATION_CLASS_LABEL[pair.classification]}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card title="配对诊断">
              {selectedPair ? (
                <div className="space-y-4">
                  <div>
                    <p className="text-sm font-semibold text-ink-900">
                      #{selectedPair.left_experiment_id} {names.get(selectedPair.left_experiment_id)}
                    </p>
                    <p className="my-1 text-xs text-ink-400">与</p>
                    <p className="text-sm font-semibold text-ink-900">
                      #{selectedPair.right_experiment_id} {names.get(selectedPair.right_experiment_id)}
                    </p>
                  </div>
                  <dl className="grid grid-cols-2 gap-3 text-sm">
                    <div><dt className="text-xs text-ink-500">相关系数</dt><dd className="mt-1 font-mono text-lg">{selectedPair.correlation?.toFixed(3) ?? '-'}</dd></div>
                    <div><dt className="text-xs text-ink-500">共同观测</dt><dd className="mt-1 font-mono text-lg">{selectedPair.overlap}</dd></div>
                    <div><dt className="text-xs text-ink-500">尾部相关</dt><dd className="mt-1 font-mono text-lg">{selectedPair.tail_correlation?.correlation?.toFixed(3) ?? '-'}</dd></div>
                    <div><dt className="text-xs text-ink-500">平均持仓重叠</dt><dd className="mt-1 font-mono text-lg">{selectedPair.holding_overlap?.mean == null ? '-' : `${(selectedPair.holding_overlap.mean * 100).toFixed(1)}%`}</dd></div>
                    <div className="col-span-2">
                      <dt className="text-xs text-ink-500">对齐区间</dt>
                      <dd className="mt-1 tnum">{selectedPair.overlap_start ?? '-'} ~ {selectedPair.overlap_end ?? '-'}</dd>
                    </div>
                    {selectedPair.interval_mismatch_exclusions > 0 && (
                      <div className="col-span-2">
                        <dt className="text-xs text-ink-500">排除的非同区间收益</dt>
                        <dd className="mt-1 tnum">{selectedPair.interval_mismatch_exclusions}</dd>
                      </div>
                    )}
                  </dl>
                  <Banner variant={selectedPair.correlation !== null && selectedPair.correlation >= 0.8 ? 'warning' : 'info'}>
                    {pairDescription(selectedPair)}
                  </Banner>
                  <div className="flex gap-2">
                    <Button size="sm" variant="secondary" onClick={() => navigate(`/experiment/${selectedPair.left_experiment_id}`)}>查看 #{selectedPair.left_experiment_id}</Button>
                    <Button size="sm" variant="secondary" onClick={() => navigate(`/experiment/${selectedPair.right_experiment_id}`)}>查看 #{selectedPair.right_experiment_id}</Button>
                  </div>
                </div>
              ) : (
                <p className="text-sm text-ink-500">点击热力图或配对表查看诊断。</p>
              )}
            </Card>
          </div>
        </>
      ) : !analyzing && (
        <div className="rounded border border-ink-200 bg-surface">
          <EmptyState
            icon="chart"
            title="等待相关性分析"
            description="选择至少两个已完成实验，并设置共同观测门槛。"
          />
        </div>
      )}
    </div>
  );
}
