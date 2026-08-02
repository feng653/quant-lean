import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import EChart from '../../components/shared/EChart';
import FactorEvidenceExportButtons from '../../components/factorResearch/FactorEvidenceExportButtons';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import Input from '../../components/shared/Input';
import PageHeader from '../../components/shared/PageHeader';
import ProgressBar from '../../components/shared/ProgressBar';
import Select from '../../components/shared/Select';
import StatusTag from '../../components/shared/StatusTag';
import {
  archiveFactorResearchRun,
  compareFactorResearchRuns,
  exportFactorStrategy,
  getFactorResearchReadiness,
  getFactorResearchRun,
  listFactorResearchRuns,
  listResearchFactors,
  submitFactorResearchJob,
} from '../../services/factorResearch';
import { cancelJob, listJobs, retryJob } from '../../services/jobs';
import { useJobEvents } from '../../hooks/useWebSocket';
import type {
  FactorDefinition,
  FactorCorrelationMatrix,
  FactorResearchReadiness,
  FactorResearchResult,
  FactorResearchRun,
  FactorProtocolPayload,
  FactorProtocolReference,
  FactorRunComparison,
  FactorStabilityConfig,
  NeutralizationMode,
} from '../../services/factorResearch';
import type { Job, JobStatus } from '../../types/job';
import { formatBackendDateTime } from '../../utils/datetime';
import { jobStatusLabel } from '../../utils/jobs';
import {
  defaultResearchStart,
  firstAvailableFactor,
  firstReadyPool,
  parseResearchHorizons,
  researchConfigEquals,
} from './factorResearchForm';
import {
  boundedChartSeries,
  capacityStatusText,
  equalFactorWeights,
  parseBoundedNumberList,
} from './factorQualityForm';
import {
  PointInTimeReadinessDetails,
  PointInTimeReadinessSummary,
} from './PointInTimeReadiness';
import FactorStabilityConfigPanel from './FactorStabilityConfig';
import FactorStabilityResults from './FactorStabilityResults';
import { validateStabilityConfig } from './factorStabilityForm';
import NeutralizationConfig from './NeutralizationConfig';
import NeutralizationResult from './NeutralizationResult';
import { neutralizationUnavailableReason } from './neutralizationForm';
import FactorCatalogGovernance from './FactorCatalogGovernance';
import FactorExportEvidence from './FactorExportEvidence';
import FactorProtocolPanel from './FactorProtocolPanel';
import FactorResultWorkbench from './FactorResultWorkbench';
import FactorComparisonVisualization from './FactorComparisonVisualization';
import {
  FACTOR_WORKBENCH_PRESETS,
  type FactorRunSort,
  type FactorWorkbenchPreset,
} from './factorWorkbench';

const RUN_PAGE_SIZE = 10;
const JOB_PAGE_SIZE = 8;

function metric(value: number | null | undefined, digits = 4): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toFixed(digits)
    : '-';
}

function CorrelationTable({
  matrix,
  factorNames,
}: {
  matrix: FactorCorrelationMatrix;
  factorNames: Record<string, string>;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-center text-xs">
        <thead>
          <tr className="border-b border-ink-200 text-ink-500">
            <th className="p-2 text-left">因子</th>
            {matrix.factors.map((factorId) => (
              <th key={factorId} className="p-2">
                {factorNames[factorId] ?? factorId}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {matrix.factors.map((factorId, row) => (
            <tr key={factorId} className="border-b border-ink-100">
              <th className="p-2 text-left font-medium">
                {factorNames[factorId] ?? factorId}
              </th>
              {matrix.matrix[row].map((value, column) => (
                <td
                  key={matrix.factors[column]}
                  className="p-2 tnum"
                  title={`有效日期 ${matrix.valid_date_counts[row][column]}`}
                >
                  {metric(value, 3)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const readinessReason: Record<string, string> = {
  daily_cache_missing: '缓存缺失',
  legacy_or_unverified_schema: '旧版缓存，需受控刷新',
  source_not_trusted_for_research: '来源证据不足',
  required_price_field_missing: '缺少收盘价字段',
  daily_cache_missing_or_invalid: '缓存为空或损坏',
  daily_cache_integrity_invalid: '完整性校验失败',
};

function safeJobFailure(job: Job): {
  code: string;
  message: string;
  action?: string;
  cacheKey?: string;
} {
  const code = typeof job.result?.error_code === 'string'
    ? job.result.error_code
    : 'factor_research_failed';
  const message = typeof job.result?.message === 'string'
    ? job.result.message
    : '研究任务未能安全完成；内部错误细节已隐藏，请重试或查看服务日志。';
  return {
    code,
    message,
    action: typeof job.result?.action === 'string' ? job.result.action : undefined,
    cacheKey: typeof job.result?.cache_key === 'string' ? job.result.cache_key : undefined,
  };
}

export default function FactorResearchPage() {
  const navigate = useNavigate();
  const [factors, setFactors] = useState<FactorDefinition[]>([]);
  const [readiness, setReadiness] = useState<FactorResearchReadiness | null>(null);
  const [runs, setRuns] = useState<FactorResearchRun[]>([]);
  const [runTotal, setRunTotal] = useState(0);
  const [runPage, setRunPage] = useState(1);
  const [loadingRuns, setLoadingRuns] = useState(false);
  const [factorId, setFactorId] = useState('');
  const [poolPreset, setPoolPreset] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [horizonsText, setHorizonsText] = useState('1, 5, 20');
  const [primaryHorizon, setPrimaryHorizon] = useState(5);
  const [quantiles, setQuantiles] = useState(5);
  const [winsorMethod, setWinsorMethod] = useState<'mad' | 'quantile' | 'none'>('mad');
  const [relatedFactorIds, setRelatedFactorIds] = useState<string[]>([]);
  const [rebalanceInterval, setRebalanceInterval] = useState(5);
  const [defaultCostBps, setDefaultCostBps] = useState(10);
  const [costScenariosText, setCostScenariosText] = useState('0, 5, 10, 20');
  const [participationRatesText, setParticipationRatesText] = useState('0.01, 0.05, 0.1');
  const [orthogonalize, setOrthogonalize] = useState(true);
  const [stability, setStability] = useState<FactorStabilityConfig | null>(null);
  const [neutralization, setNeutralization] = useState<NeutralizationMode>('none');
  const [result, setResult] = useState<FactorResearchResult | null>(null);
  const [selectedRunIds, setSelectedRunIds] = useState<string[]>([]);
  const [comparison, setComparison] = useState<FactorRunComparison | null>(null);
  const [weights, setWeights] = useState<Record<string, number>>({});
  const [strategyName, setStrategyName] = useState('研究证据多因子策略');
  const [exportedStrategyId, setExportedStrategyId] = useState<string | null>(null);
  const [exportedVersion, setExportedVersion] = useState<{
    version: string;
    evidenceCount: number;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingPage, setLoadingPage] = useState(true);
  const [comparing, setComparing] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [researchJobs, setResearchJobs] = useState<Job[]>([]);
  const [jobTotal, setJobTotal] = useState(0);
  const [jobPage, setJobPage] = useState(1);
  const [jobStatus, setJobStatus] = useState<JobStatus | ''>('');
  const [currentJobId, setCurrentJobId] = useState<string | null>(null);
  const [handledJobId, setHandledJobId] = useState<string | null>(null);
  const [runQuery, setRunQuery] = useState('');
  const [runFactorFilter, setRunFactorFilter] = useState('');
  const [runSort, setRunSort] = useState<FactorRunSort>('newest');
  const [activeProtocol, setActiveProtocol] = useState<{
    payload: FactorProtocolPayload;
    reference: FactorProtocolReference;
  } | null>(null);

  const refreshRuns = useCallback(async () => {
    setLoadingRuns(true);
    try {
      const response = await listFactorResearchRuns({
        factor_id: runFactorFilter || undefined,
        query: runQuery.trim() || undefined,
        sort: runSort,
        page: runPage,
        page_size: RUN_PAGE_SIZE,
      });
      setRuns(response.items);
      setRunTotal(response.total);
      if (response.total > 0 && response.items.length === 0 && runPage > 1) {
        setRunPage(Math.max(1, Math.ceil(response.total / RUN_PAGE_SIZE)));
      }
    } finally {
      setLoadingRuns(false);
    }
  }, [runFactorFilter, runPage, runQuery, runSort]);
  const refreshCatalog = async () => setFactors(await listResearchFactors());
  const refreshResearchJobs = useCallback(async () => {
    const response = await listJobs({
      status: jobStatus,
      job_type: 'factor_research',
      page: jobPage,
      page_size: JOB_PAGE_SIZE,
      mine: true,
    });
    setResearchJobs(response.items);
    setJobTotal(response.total);
    if (response.total > 0 && response.items.length === 0 && jobPage > 1) {
      setJobPage(Math.max(1, Math.ceil(response.total / JOB_PAGE_SIZE)));
    }
    setCurrentJobId((current) => {
      const selected = response.items.find((job) => job.job_uuid === current);
      if (
        selected
        && (
          selected.status === 'pending'
          || selected.status === 'running'
          || selected.status === 'cancel_requested'
          || selected.status === 'completed'
        )
      ) return current;
      return response.items.find((job) => (
        job.status === 'pending'
        || job.status === 'running'
        || job.status === 'cancel_requested'
      ))?.job_uuid ?? null;
    });
  }, [jobPage, jobStatus]);

  useEffect(() => {
    void Promise.all([
      listResearchFactors(),
      getFactorResearchReadiness(),
      listFactorResearchRuns({
        sort: 'newest',
        page: 1,
        page_size: RUN_PAGE_SIZE,
      }),
      listJobs({
        job_type: 'factor_research',
        page: 1,
        page_size: JOB_PAGE_SIZE,
        mine: true,
      }),
    ]).then(([factorItems, readinessData, runPageData, jobItems]) => {
      setFactors(factorItems);
      setReadiness(readinessData);
      setRuns(runPageData.items);
      setRunTotal(runPageData.total);
      setResearchJobs(jobItems.items);
      setJobTotal(jobItems.total);
      setCurrentJobId(jobItems.items.find((job) => (
        job.status === 'pending'
        || job.status === 'running'
        || job.status === 'cancel_requested'
      ))?.job_uuid ?? null);
      setWeights(Object.fromEntries(factorItems.map((factor) => [factor.factor_id, 0])));
      const firstPool = firstReadyPool(readinessData);
      setPoolPreset(firstPool?.pool_id ?? '');
      const firstFactor = firstAvailableFactor(
        factorItems.filter((factor) => factor.current && !factor.deprecated),
        firstPool,
      );
      setFactorId(firstFactor?.factor_id ?? '');
      setStart(defaultResearchStart(firstPool?.date_start ?? null, firstPool?.date_end ?? null));
      setEnd(firstPool?.date_end ?? '');
    }).catch((err: unknown) => {
      setError(err instanceof Error ? err.message : '研究工作台加载失败');
    }).finally(() => setLoadingPage(false));
  }, []);

  useEffect(() => {
    if (loadingPage) return undefined;
    const timer = window.setTimeout(() => {
      void refreshRuns().catch((err: unknown) => {
        setError(err instanceof Error ? err.message : '研究历史加载失败');
      });
    }, runQuery ? 250 : 0);
    return () => window.clearTimeout(timer);
  }, [loadingPage, refreshRuns, runQuery]);

  useEffect(() => {
    if (!loadingPage) {
      void refreshResearchJobs().catch((err: unknown) => {
        setError(err instanceof Error ? err.message : '研究任务加载失败');
      });
    }
  }, [jobPage, jobStatus, loadingPage, refreshResearchJobs]);

  const onJobEvent = useCallback((event: { job_type: string }) => {
    if (event.job_type === 'factor_research') void refreshResearchJobs();
  }, [refreshResearchJobs]);
  useJobEvents(onJobEvent);

  const currentJob = researchJobs.find((job) => job.job_uuid === currentJobId) ?? null;
  useEffect(() => {
    if (!currentJob || !['pending', 'running', 'cancel_requested'].includes(currentJob.status)) {
      return undefined;
    }
    const timer = window.setInterval(() => void refreshResearchJobs(), 3000);
    return () => window.clearInterval(timer);
  }, [currentJob, refreshResearchJobs]);

  useEffect(() => {
    if (
      currentJob?.status !== 'completed'
      || currentJob.job_uuid === handledJobId
      || typeof currentJob.result?.run_id !== 'string'
    ) return;
    setHandledJobId(currentJob.job_uuid);
    const runId = currentJob.result.run_id;
    void Promise.all([
      getFactorResearchRun(runId),
      listFactorResearchRuns({
        sort: 'newest',
        page: 1,
        page_size: RUN_PAGE_SIZE,
      }),
    ]).then(([runItem, runPageData]) => {
      if (!runItem.result) throw new Error('研究运行没有结果载荷');
      setRunQuery('');
      setRunFactorFilter('');
      setRunSort('newest');
      setRunPage(1);
      setRuns(runPageData.items);
      setRunTotal(runPageData.total);
      setResult({
        ...runItem.result,
        run: {
          run_id: runItem.run_id,
          created_at: runItem.created_at,
          request_digest: runItem.request_digest,
          dataset_digest: runItem.dataset_digest,
          result_digest: runItem.result_digest,
          run_digest: runItem.run_digest,
          source_job_uuid: runItem.source_job_uuid,
          archived_at: runItem.archived_at,
        },
      });
      setSelectedRunIds((current) => [runItem.run_id, ...current].slice(0, 20));
      setWeights((current) => ({
        ...current,
        [runItem.factor_id]: current[runItem.factor_id] || 1,
      }));
      setNotice('后台研究完成，结果与数据摘要已作为不可变运行保存。');
      setCurrentJobId(null);
    }).catch((err: unknown) => {
      setHandledJobId(null);
      setError(err instanceof Error ? err.message : '已完成研究结果加载失败');
      window.setTimeout(() => void refreshResearchJobs(), 3000);
    });
  }, [currentJob, handledJobId, refreshResearchJobs]);

  const readyPools = readiness?.pools.filter((pool) => pool.ready) ?? [];
  const selectedPool = readiness?.pools.find((pool) => pool.pool_id === poolPreset);
  const availableFactors = factors.filter(
    (factor) => (
      factor.current
      && !factor.deprecated
      && selectedPool?.available_factor_ids.includes(factor.factor_id)
    ),
  );
  const horizons = parseResearchHorizons(horizonsText);
  const costScenarios = parseBoundedNumberList(costScenariosText, {
    min: 0,
    max: 100,
    maxItems: 8,
  });
  const participationRates = parseBoundedNumberList(participationRatesText, {
    min: 0,
    max: 0.25,
    maxItems: 5,
    includeMin: false,
  });
  const protocolBase = useMemo<Omit<
    FactorProtocolPayload,
    'question' | 'hypothesis' | 'thresholds' | 'export_rules'
  > | null>(() => {
    if (!horizons || !costScenarios || !factorId || !poolPreset || !start || !end) return null;
    return {
      schema_version: 'factor-research-protocol/v1',
      factor_ids: [factorId, ...relatedFactorIds],
      data: activeProtocol?.payload.data.pool_id === poolPreset
        ? activeProtocol.payload.data
        : {
          pool_id: poolPreset,
          version_policy: 'latest_trusted_at_execution',
          expected_dataset_digest: null,
        },
      window: { start, end },
      implementation: {
        horizons,
        primary_horizon: primaryHorizon,
        quantiles,
        rebalance_interval: rebalanceInterval,
        default_cost_bps: defaultCostBps,
        cost_scenarios_bps: costScenarios,
        neutralization,
      },
    };
  }, [
    activeProtocol, costScenarios, defaultCostBps, end, factorId, horizons, neutralization,
    poolPreset, primaryHorizon, quantiles, rebalanceInterval, relatedFactorIds,
    start,
  ]);
  const configError = useMemo(() => {
    if (!selectedPool?.ready) return '没有可用于研究的可信行情缓存';
    if (!factorId || !selectedPool.available_factor_ids.includes(factorId)) {
      return '当前缓存不包含所选因子所需字段';
    }
    if (!start || !end || start >= end) return '研究开始日期必须早于结束日期';
    if (
      (selectedPool.date_start && start < selectedPool.date_start)
      || (selectedPool.date_end && end > selectedPool.date_end)
    ) return '研究日期超出可信缓存覆盖范围';
    if (!horizons) return '周期须为至多 12 个不重复的 1–252 正整数';
    if (!horizons.includes(primaryHorizon)) return '主评估周期必须包含在研究周期中';
    if (!Number.isInteger(quantiles) || quantiles < 2 || quantiles > 10) {
      return '分组数必须为 2–10 的整数';
    }
    if (!Number.isInteger(rebalanceInterval) || rebalanceInterval < 1 || rebalanceInterval > 252) {
      return '调仓间隔必须为 1–252 个交易日';
    }
    if (!Number.isFinite(defaultCostBps) || defaultCostBps < 0 || defaultCostBps > 100) {
      return '默认交易成本必须为 0–100 bps';
    }
    if (!costScenarios) return '成本敏感性须为至多 8 个不重复的 0–100 bps 数字';
    if (!costScenarios.some((value) => Math.abs(value - defaultCostBps) < 1e-9)) {
      return '成本敏感性档位必须包含默认交易成本';
    }
    if (!participationRates) return '容量参与率须为至多 5 个不重复的 (0, 0.25] 数字';
    if (
      relatedFactorIds.length > 5
      || relatedFactorIds.some((item) => (
        item === factorId || !selectedPool.available_factor_ids.includes(item)
      ))
    ) return '相关因子必须来自当前缓存，且最多选择 5 个';
    const stabilityError = validateStabilityConfig(stability, start, end);
    if (stabilityError) return stabilityError;
    const neutralizationReason = neutralizationUnavailableReason(
      selectedPool,
      neutralization,
    );
    if (neutralizationReason) return neutralizationReason;
    if (activeProtocol && !researchConfigEquals(protocolBase, {
      schema_version: activeProtocol.payload.schema_version,
      factor_ids: activeProtocol.payload.factor_ids,
      data: activeProtocol.payload.data,
      window: activeProtocol.payload.window,
      implementation: activeProtocol.payload.implementation,
    })) return '当前配置偏离已应用的锁定协议；请恢复协议配置或创建并锁定新版本';
    return null;
  }, [
    costScenarios,
    defaultCostBps,
    end,
    factorId,
    horizons,
    participationRates,
    primaryHorizon,
    quantiles,
    rebalanceInterval,
    relatedFactorIds,
    selectedPool,
    stability,
    start,
    neutralization,
    activeProtocol,
    protocolBase,
  ]);

  const changePool = (poolId: string) => {
    setPoolPreset(poolId);
    const pool = readiness?.pools.find((item) => item.pool_id === poolId);
    if (!pool) return;
    const nextFactor = factors.find((factor) => pool.available_factor_ids.includes(factor.factor_id));
    if (nextFactor) setFactorId(nextFactor.factor_id);
    setRelatedFactorIds([]);
    setStability(null);
    setNeutralization('none');
    setStart(defaultResearchStart(pool.date_start, pool.date_end));
    setEnd(pool.date_end ?? '');
    setResult(null);
  };

  const applyPreset = (preset: FactorWorkbenchPreset) => {
    setHorizonsText(preset.horizonsText);
    setPrimaryHorizon(preset.primaryHorizon);
    setQuantiles(preset.quantiles);
    setRebalanceInterval(preset.rebalanceInterval);
    setDefaultCostBps(preset.defaultCostBps);
    setCostScenariosText(preset.costScenariosText);
    setParticipationRatesText(preset.participationRatesText);
    setWinsorMethod(preset.winsorMethod);
    setOrthogonalize(preset.orthogonalize);
    setNotice(`已应用“${preset.name}”模板；提交前仍需确认日期、因子与样本外设计。`);
    document.getElementById('factor-config')?.scrollIntoView({
      behavior: 'smooth',
      block: 'start',
    });
  };

  const applyProtocol = (
    payload: FactorProtocolPayload,
    reference: FactorProtocolReference,
  ) => {
    setFactorId(payload.factor_ids[0]);
    setRelatedFactorIds(payload.factor_ids.slice(1));
    setPoolPreset(payload.data.pool_id);
    setStart(payload.window.start);
    setEnd(payload.window.end);
    setHorizonsText(payload.implementation.horizons.join(', '));
    setPrimaryHorizon(payload.implementation.primary_horizon);
    setQuantiles(payload.implementation.quantiles);
    setRebalanceInterval(payload.implementation.rebalance_interval);
    setDefaultCostBps(payload.implementation.default_cost_bps);
    setCostScenariosText(payload.implementation.cost_scenarios_bps.join(', '));
    setNeutralization(payload.implementation.neutralization);
    setActiveProtocol({ payload, reference });
    setNotice(`已应用锁定协议 v${reference.version}；提交时服务端会再次核验全部配置。`);
  };

  const run = async () => {
    if (configError || !horizons || !costScenarios || !participationRates) return;
    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      const submission = await submitFactorResearchJob({
        factor_id: factorId,
        pool_preset: poolPreset,
        pool_custom_codes: [],
        start,
        end,
        horizons,
        primary_horizon: primaryHorizon,
        quantiles,
        winsor_method: winsorMethod,
        related_factor_ids: relatedFactorIds,
        rebalance_interval: rebalanceInterval,
        default_cost_bps: defaultCostBps,
        cost_scenarios_bps: costScenarios,
        capacity_participation_rates: participationRates,
        orthogonalize,
        combination_weights: equalFactorWeights([factorId, ...relatedFactorIds]),
        stability,
        neutralization,
        industry_scope: selectedPool?.neutralization?.industry.scope_id ?? 'cninfo_008001',
        size_field: 'auto',
        protocol: activeProtocol?.reference ?? null,
      });
      setJobPage(1);
      setJobStatus('');
      setHandledJobId(null);
      const latestJobs = await listJobs({
        job_type: 'factor_research',
        page: 1,
        page_size: JOB_PAGE_SIZE,
        mine: true,
      });
      setResearchJobs(latestJobs.items);
      setJobTotal(latestJobs.total);
      setCurrentJobId(submission.job_id);
      setNotice('研究任务已进入可恢复队列；可以离开页面，返回后会继续追踪。');
    } catch (err: unknown) {
      setResult(null);
      setError(err instanceof Error ? err.message : '因子分析失败');
    } finally {
      setLoading(false);
    }
  };

  const cancelResearchJob = async (jobId: string) => {
    setError(null);
    try {
      await cancelJob(jobId);
      await refreshResearchJobs();
      setNotice('已提交安全取消请求；CPU 计算阶段可能延迟到本阶段结束。');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '取消研究任务失败');
    }
  };

  const retryResearchJob = async (jobId: string) => {
    setError(null);
    try {
      const nextJobId = await retryJob(jobId);
      setJobPage(1);
      setJobStatus('');
      setHandledJobId(null);
      const latestJobs = await listJobs({
        job_type: 'factor_research',
        page: 1,
        page_size: JOB_PAGE_SIZE,
        mine: true,
      });
      setResearchJobs(latestJobs.items);
      setJobTotal(latestJobs.total);
      setCurrentJobId(nextJobId);
      setNotice('研究任务已重新排队。');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '重试研究任务失败');
    }
  };

  const openRun = async (runId: string) => {
    setError(null);
    try {
      const runItem = await getFactorResearchRun(runId);
      if (!runItem.result) throw new Error('研究运行没有结果载荷');
      setResult({
        ...runItem.result,
        run: {
          run_id: runItem.run_id,
          created_at: runItem.created_at,
          request_digest: runItem.request_digest,
          dataset_digest: runItem.dataset_digest,
          result_digest: runItem.result_digest,
          run_digest: runItem.run_digest,
          source_job_uuid: runItem.source_job_uuid,
          archived_at: runItem.archived_at,
        },
      });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '研究运行加载失败');
    }
  };

  const archiveRun = async (runId: string) => {
    setError(null);
    try {
      await archiveFactorResearchRun(runId);
      setSelectedRunIds((current) => current.filter((item) => item !== runId));
      await refreshRuns();
      setNotice('研究运行已归档；证据未被物理删除。');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '归档失败');
    }
  };

  const compare = async () => {
    if (selectedRunIds.length < 2) return;
    setComparing(true);
    setError(null);
    try {
      setComparison(await compareFactorResearchRuns(selectedRunIds));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '因子比较失败');
    } finally {
      setComparing(false);
    }
  };

  const exportStrategy = async () => {
    const selected = runs.filter((runItem) => selectedRunIds.includes(runItem.run_id));
    const evidenceFactors = new Set(selected.map((runItem) => runItem.factor_id));
    const components = factors
      .filter((factor) => evidenceFactors.has(factor.factor_id) && (weights[factor.factor_id] ?? 0) > 0)
      .map((factor) => ({ factor_id: factor.factor_id, weight: weights[factor.factor_id] }));
    if (components.length === 0) {
      setError('请先选择研究运行，并为至少一个对应因子设置正权重');
      return;
    }
    setExporting(true);
    setError(null);
    try {
      const componentIds = new Set(components.map((component) => component.factor_id));
      const exported = await exportFactorStrategy({
        name: strategyName,
        components,
        top_k_pct: 0.1,
        research_run_ids: selected
          .filter((runItem) => componentIds.has(runItem.factor_id))
          .map((runItem) => runItem.run_id),
      });
      setExportedStrategyId(exported.strategy_id);
      setExportedVersion({
        version: exported.version,
        evidenceCount: exported.research_evidence.length,
      });
      setNotice(
        `已发布策略 ${exported.version}，并绑定 ${exported.research_evidence.length} 条研究证据。`,
      );
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '策略导出失败');
    } finally {
      setExporting(false);
    }
  };

  const primary = result?.ic[String(result.request.primary_horizon)];
  const icOption = useMemo(() => ({
    tooltip: { trigger: 'axis' },
    legend: { data: ['IC', 'RankIC'] },
    grid: { left: 52, right: 20, top: 42, bottom: 46 },
    xAxis: {
      type: 'category',
      data: primary?.series.map((point) => point.date) ?? [],
      axisLabel: { hideOverlap: true },
    },
    yAxis: { type: 'value', name: '相关系数' },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18 }],
    series: [
      { name: 'IC', type: 'line', symbol: 'none', data: primary?.series.map((point) => point.pearson_ic) ?? [] },
      { name: 'RankIC', type: 'line', symbol: 'none', data: primary?.series.map((point) => point.rank_ic) ?? [] },
    ],
  }), [primary]);

  const decayOption = useMemo(() => ({
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: result?.decay.points.map((point) => `${point.horizon} 日`) ?? [] },
    yAxis: { type: 'value' },
    series: [
      { name: '平均 RankIC', type: 'bar', data: result?.decay.points.map((point) => point.rank_ic.mean) ?? [] },
    ],
  }), [result]);

  const quantileOption = useMemo(() => {
    const entries = Object.entries(result?.quantile_returns.mean_group_returns ?? {});
    return {
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: entries.map(([group]) => `Q${group}`) },
      yAxis: { type: 'value', name: '平均前瞻收益' },
      series: [{ name: '分层收益', type: 'bar', data: entries.map(([, value]) => value) }],
    };
  }, [result]);
  const turnoverPoints = useMemo(
    () => boundedChartSeries(result?.implementation?.turnover.series ?? []),
    [result],
  );
  const turnoverOption = useMemo(() => ({
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'category',
      data: turnoverPoints.map((point) => point.date),
      axisLabel: { hideOverlap: true },
    },
    yAxis: { type: 'value', name: '单边换手率', min: 0 },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18 }],
    series: [{
      name: '多空组合换手',
      type: 'line',
      symbol: 'none',
      data: turnoverPoints.map((point) => point.long_short_turnover),
    }],
  }), [turnoverPoints]);
  const factorNames = useMemo(
    () => Object.fromEntries(factors.map((factor) => [factor.factor_id, factor.name])),
    [factors],
  );
  const visibleRuns = runs;
  const navigateToSection = (target: string) => {
    document.getElementById(target)?.scrollIntoView({
      behavior: 'smooth',
      block: 'start',
    });
  };

  return (
    <div>
      <PageHeader
        title="因子研究工作台"
        description="在来源可验证的本地行情上研究因子、保存证据、横向比较并导出组合策略。"
        tags={<StatusTag variant="paper">研究环境 · 不构成投资建议</StatusTag>}
      />

      <nav
        aria-label="因子研究页面导航"
        className="sticky top-0 z-20 mt-4 flex flex-wrap gap-2 rounded-md border border-ink-200 bg-surface/95 p-2 shadow-sm backdrop-blur"
      >
        {[
          ['factor-config', '配置与模板'],
          ['factor-protocols', '预注册协议'],
          ['factor-jobs', '任务'],
          ['factor-readiness', '数据就绪'],
          ['factor-results', '结果'],
          ['factor-history', '历史比较'],
          ['factor-export', '导出策略'],
          ['factor-catalog', '因子目录'],
        ].map(([target, label]) => (
          <Button
            key={target}
            size="sm"
            variant="ghost"
            onClick={() => document.getElementById(target)?.scrollIntoView({
              behavior: 'smooth',
              block: 'start',
            })}
          >
            {label}
          </Button>
        ))}
      </nav>

      {loadingPage && <Card><p role="status">正在检查研究能力与数据可信度…</p></Card>}
      {!loadingPage && !readiness?.ready && (
        <Banner variant="warning" title="暂无可信研究缓存">
          指数旧缓存已被安全阻断。请等待数据中心完成 schema-v4 受控刷新；系统不会用旧缓存或合成验收数据生成研究结论。
        </Banner>
      )}
      {error && <Banner variant="danger" className="mt-4" title="因子研究操作失败">{error}</Banner>}
      {notice && <Banner variant="ok" className="mt-4" title="操作完成">{notice}</Banner>}
      <div id="factor-catalog" className="scroll-mt-16">
        <FactorCatalogGovernance
          factors={factors}
          onChanged={refreshCatalog}
        />
      </div>

      {!loadingPage && (
        <Card id="factor-config" className="mt-4 scroll-mt-16" title="研究配置" description="日期、字段与来源先通过 readiness 检查，再允许运行">
          <div className="mb-5">
            <h3 className="text-sm font-semibold text-ink-800">快速起步模板</h3>
            <p className="mt-1 text-xs text-ink-500">
              模板只填写研究参数，不替代预注册或样本外检验；应用后仍可逐项调整。
            </p>
            <div className="mt-3 grid gap-3 lg:grid-cols-3">
              {FACTOR_WORKBENCH_PRESETS.map((preset) => (
                <button
                  key={preset.id}
                  type="button"
                  onClick={() => applyPreset(preset)}
                  className="rounded-md border border-ink-200 p-3 text-left transition-colors hover:border-accent-500 hover:bg-accent-50"
                >
                  <span className="text-sm font-semibold text-ink-800">{preset.name}</span>
                  <span className="mt-1 block text-xs leading-5 text-ink-500">
                    {preset.description}
                  </span>
                </button>
              ))}
            </div>
          </div>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            <Select
              label="可信股票池"
              value={poolPreset}
              disabled={readyPools.length === 0}
              placeholder={readyPools.length === 0 ? '暂无可用股票池' : undefined}
              onChange={(event) => changePool(event.target.value)}
              options={readyPools.map((pool) => ({
                value: pool.pool_id,
                label: `${pool.label} · ${pool.n_stocks}只`,
              }))}
            />
            <Select
              label="研究因子"
              value={factorId}
              disabled={availableFactors.length === 0}
              onChange={(event) => {
                setFactorId(event.target.value);
                setRelatedFactorIds((current) => (
                  current.filter((item) => item !== event.target.value)
                ));
              }}
              options={availableFactors.map((factor) => ({
                value: factor.factor_id,
                label: `${factor.name} · ${factor.category}`,
              }))}
            />
            <Input
              label="研究周期（交易日）"
              value={horizonsText}
              error={horizons ? undefined : '示例：1, 5, 20'}
              onChange={(event) => setHorizonsText(event.target.value)}
              hint="最多 12 个，范围 1–252"
            />
            <Input label="研究开始" type="date" min={selectedPool?.date_start ?? undefined}
              max={selectedPool?.date_end ?? undefined} value={start}
              onChange={(event) => setStart(event.target.value)} />
            <Input label="研究结束" type="date" min={selectedPool?.date_start ?? undefined}
              max={selectedPool?.date_end ?? undefined} value={end}
              onChange={(event) => setEnd(event.target.value)} />
            <Select label="主评估周期" value={String(primaryHorizon)}
              onChange={(event) => setPrimaryHorizon(Number(event.target.value))}
              options={(horizons ?? []).map((value) => ({ value: String(value), label: `${value} 个交易日` }))} />
            <Select label="去极值" value={winsorMethod}
              onChange={(event) => setWinsorMethod(event.target.value as typeof winsorMethod)}
              options={[{ value: 'mad', label: 'MAD（推荐）' }, { value: 'quantile', label: '1% / 99%' }, { value: 'none', label: '不处理' }]} />
            <Input label="分组数" type="number" min={2} max={10} value={quantiles}
              onChange={(event) => setQuantiles(Number(event.target.value))} />
          </div>
          <div className="mt-5 rounded border border-ink-200 bg-ink-50/40 p-4">
            <h3 className="text-sm font-semibold text-ink-800">实施成本与多因子检验</h3>
            <p className="mt-1 text-xs text-ink-500">
              默认按单边换手收取 10 bps；容量只在可信缓存提供成交额时估算，否则明确标记不可用。
            </p>
            <div className="mt-3 grid grid-cols-1 gap-4 md:grid-cols-3">
              <Input label="调仓间隔（交易日）" type="number" min={1} max={252}
                value={rebalanceInterval}
                onChange={(event) => setRebalanceInterval(Number(event.target.value))} />
              <Input label="默认交易成本（bps）" type="number" min={0} max={100} step={0.1}
                value={defaultCostBps}
                onChange={(event) => setDefaultCostBps(Number(event.target.value))} />
              <Input label="成本敏感性档位（bps）" value={costScenariosText}
                hint="例如：0, 5, 10, 20"
                error={costScenarios ? undefined : '需要 1–8 个不重复费率'}
                onChange={(event) => setCostScenariosText(event.target.value)} />
              <Input label="容量参与率" value={participationRatesText}
                hint="例如：0.01, 0.05, 0.1"
                error={participationRates ? undefined : '范围 (0, 0.25]'}
                onChange={(event) => setParticipationRatesText(event.target.value)} />
              <label className="flex items-center gap-2 text-sm text-ink-700">
                <input type="checkbox" checked={orthogonalize}
                  onChange={(event) => setOrthogonalize(event.target.checked)} />
                按固定因子 ID 顺序正交化
              </label>
            </div>
            <fieldset className="mt-4">
              <legend className="text-sm font-medium text-ink-700">
                同窗相关与组合因子（最多再选 5 个）
              </legend>
              <div className="mt-2 flex flex-wrap gap-3">
                {availableFactors.filter((factor) => factor.factor_id !== factorId).map((factor) => (
                  <label key={factor.factor_id}
                    className="flex items-center gap-2 rounded border border-ink-200 bg-surface px-3 py-2 text-sm">
                    <input
                      type="checkbox"
                      checked={relatedFactorIds.includes(factor.factor_id)}
                      disabled={!relatedFactorIds.includes(factor.factor_id)
                        && relatedFactorIds.length >= 5}
                      onChange={(event) => setRelatedFactorIds((current) => (
                        event.target.checked
                          ? [...current, factor.factor_id]
                          : current.filter((item) => item !== factor.factor_id)
                      ))}
                    />
                    {factor.name}
                  </label>
                ))}
                {availableFactors.length <= 1 && (
                  <p className="text-sm text-ink-500">当前缓存没有其他可用因子。</p>
                )}
              </div>
              <p className="mt-2 text-xs text-ink-500">
                研究组合使用有界非负等权，权重和固定为 1；计算完成后不会自动发布策略。
              </p>
            </fieldset>
          </div>
          <FactorStabilityConfigPanel
            value={stability}
            researchStart={start}
            researchEnd={end}
            onChange={setStability}
          />
          <NeutralizationConfig
            pool={selectedPool}
            value={neutralization}
            onChange={setNeutralization}
          />
          {selectedPool && (
            <div className="mt-3 space-y-1 text-xs text-ink-500">
              <p>
                数据 {selectedPool.date_start} 至 {selectedPool.date_end} ·
                来源可信度 {selectedPool.source_trust} ·
                提供方 {selectedPool.source_providers.join(', ') || '-'}
              </p>
              <PointInTimeReadinessSummary pool={selectedPool} />
            </div>
          )}
          <div className="mt-4 flex items-center gap-3">
            <Button onClick={() => void run()} loading={loading} disabled={Boolean(configError)}>
              提交后台研究
            </Button>
            {configError && <p role="alert" className="text-sm text-danger-fg">{configError}</p>}
          </div>
        </Card>
      )}

      {!loadingPage && (
        <FactorProtocolPanel
          base={protocolBase}
          suggestedDatasetDigest={result?.dataset.content_sha256}
          activeReference={activeProtocol?.reference ?? null}
          onApply={applyProtocol}
        />
      )}

      {!loadingPage && (
        <Card
          id="factor-jobs"
          className="mt-4 scroll-mt-16"
          title="研究任务"
          description="任务由资源调度器持久化执行；服务重启或页面刷新后仍可恢复"
        >
          <div className="space-y-3">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <Select
                label="任务状态"
                value={jobStatus}
                onChange={(event) => {
                  setJobStatus(event.target.value as JobStatus | '');
                  setJobPage(1);
                }}
                options={[
                  { value: '', label: '全部状态' },
                  { value: 'pending', label: '排队中' },
                  { value: 'running', label: '运行中' },
                  { value: 'completed', label: '已完成' },
                  { value: 'failed', label: '失败' },
                  { value: 'cancelled', label: '已取消' },
                ]}
              />
              <p className="text-xs text-ink-500">
                共 {jobTotal} 条 · 第 {jobPage} / {Math.max(1, Math.ceil(jobTotal / JOB_PAGE_SIZE))} 页
              </p>
            </div>
            {researchJobs.map((job) => {
              const active = ['pending', 'running', 'cancel_requested'].includes(job.status);
              const failure = job.status === 'failed' ? safeJobFailure(job) : null;
              const variant = job.status === 'completed'
                ? 'verified'
                : job.status === 'failed'
                  ? 'error'
                  : job.status === 'cancelled'
                    ? 'neutral'
                    : job.status === 'pending'
                      ? 'queued'
                      : job.status === 'cancel_requested'
                        ? 'warning'
                        : 'running';
              return (
                <div
                  key={job.job_uuid}
                  className="rounded border border-ink-200 p-3"
                  aria-current={job.job_uuid === currentJobId ? 'true' : undefined}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <p className="text-sm font-medium text-ink-800">
                        {factors.find((factor) => (
                          factor.factor_id === String(job.params.factor_id ?? job.resource_id ?? '')
                        ))?.name ?? String(job.params.factor_id ?? job.resource_id ?? '因子研究')}
                      </p>
                      <p className="mt-0.5 text-xs text-ink-500">
                        {formatBackendDateTime(job.created_at)}
                        {' · '}
                        {job.progress_message || job.current_stage || '等待调度'}
                        {job.queue_position ? ` · 队列第 ${job.queue_position}` : ''}
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      <StatusTag variant={variant}>{jobStatusLabel[job.status]}</StatusTag>
                      {active && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => void cancelResearchJob(job.job_uuid)}
                        >
                          取消
                        </Button>
                      )}
                      {(job.status === 'failed' || job.status === 'cancelled') && (
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => void retryResearchJob(job.job_uuid)}
                        >
                          重试
                        </Button>
                      )}
                    </div>
                  </div>
                  <ProgressBar
                    className="mt-2"
                    value={job.progress * 100}
                    label={`${String(job.params.factor_id ?? '因子')}研究进度`}
                    variant={job.status === 'failed' ? 'danger' : 'accent'}
                  />
                  {job.status === 'failed' && (
                    <div role="alert" className="mt-2 text-xs text-danger-fg">
                      <p>
                        安全失败原因：<code>{failure?.code}</code> · {failure?.message}
                      </p>
                      {failure?.cacheKey && <p>数据缓存：{failure.cacheKey}</p>}
                      {failure?.action === 'refresh_in_data_center' && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => navigate('/data')}
                        >
                          前往数据中心修复
                        </Button>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
            {researchJobs.length === 0 && (
              <p role="status" className="text-sm text-ink-500">当前筛选条件下没有研究任务。</p>
            )}
            <div className="flex justify-end gap-2">
              <Button
                size="sm"
                variant="ghost"
                disabled={jobPage <= 1}
                onClick={() => setJobPage((page) => Math.max(1, page - 1))}
              >
                上一页任务
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={jobPage * JOB_PAGE_SIZE >= jobTotal}
                onClick={() => setJobPage((page) => page + 1)}
              >
                下一页任务
              </Button>
            </div>
          </div>
        </Card>
      )}

      {readiness && (
        <Card id="factor-readiness" className="mt-4 scroll-mt-16" title="数据就绪状态" description="无效缓存仍可见，但不能被选择运行">
          <div className="grid gap-2 md:grid-cols-2">
            {readiness.pools.map((pool) => (
              <div key={pool.pool_id} className="rounded border border-ink-200 p-3 text-sm">
                <div className="flex justify-between gap-2">
                  <span className="font-medium">{pool.label}</span>
                  <StatusTag variant={pool.ready ? 'verified' : 'warning'}>
                    {pool.ready ? '可研究' : readinessReason[pool.disabled_reason ?? ''] ?? '不可用'}
                  </StatusTag>
                </div>
                <p className="mt-1 text-xs text-ink-500">
                  schema {pool.schema_version ?? '-'} · {pool.date_start ?? '-'} 至 {pool.date_end ?? '-'}
                </p>
                <PointInTimeReadinessDetails pool={pool} />
              </div>
            ))}
          </div>
        </Card>
      )}

      {result && primary && (
        <div id="factor-results" className="scroll-mt-16">
          <Banner variant="warning" className="mt-4" title="解释边界">
            {result.limitations.join(' ')}
          </Banner>
          <FactorResultWorkbench result={result} onNavigate={navigateToSection} />
          <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-5">
            {[
              ['因子', result.factor.name],
              ['平均 RankIC', metric(primary.summary.rank_ic.mean)],
              ['RankIC IR', metric(primary.summary.rank_ic.icir)],
              ['RankIC 胜率', primary.summary.rank_ic.positive_ratio == null ? '-' : `${(primary.summary.rank_ic.positive_ratio * 100).toFixed(1)}%`],
              ['多空均值', metric(result.quantile_returns.long_short.mean)],
            ].map(([label, value]) => (
              <Card key={label} padding="sm">
                <p className="text-xs text-ink-500">{label}</p>
                <p className="mt-1 text-lg font-semibold tnum">{value}</p>
              </Card>
            ))}
          </div>
          <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-2">
            <Card title={`${result.request.primary_horizon} 日 IC / RankIC 时序`}>
              <EChart option={icOption} style={{ height: 330 }} />
            </Card>
            <Card title="RankIC 衰减">
              <EChart option={decayOption} style={{ height: 330 }} />
            </Card>
            <Card title="分层收益" description={`单调性 ${metric(result.quantile_returns.monotonicity)}`}>
              <EChart option={quantileOption} style={{ height: 300 }} />
            </Card>
            <Card id="factor-result-evidence" className="scroll-mt-16" title="数据与不可变证据">
              <dl className="space-y-2 text-sm">
                <div><dt className="text-ink-500">研究运行</dt><dd className="break-all font-mono text-xs">{result.run?.run_id ?? '-'}</dd></div>
                <div><dt className="text-ink-500">来源任务</dt><dd className="break-all font-mono text-xs">{result.run?.source_job_uuid ?? '同步运行（无后台任务）'}</dd></div>
                <div><dt className="text-ink-500">覆盖</dt><dd className="tnum">{result.dataset.codes} 只 · {result.dataset.rows} 个交易日</dd></div>
                <div><dt className="text-ink-500">数据区间</dt><dd className="tnum">{result.dataset.date_start} 至 {result.dataset.date_end}</dd></div>
                <div><dt className="text-ink-500">请求摘要</dt><dd className="break-all font-mono text-xs">{result.run?.request_digest ?? '-'}</dd></div>
                <div><dt className="text-ink-500">数据版本 / 摘要</dt><dd className="break-all font-mono text-xs">{result.dataset.content_sha256}</dd></div>
                <div><dt className="text-ink-500">缓存身份</dt><dd className="break-all font-mono text-xs">{result.dataset.cache_key}</dd></div>
                <div><dt className="text-ink-500">来源证据版本</dt><dd className="break-all font-mono text-xs">
                  {String(result.dataset.source_provenance.content_sha256 ?? '-')}
                </dd></div>
                <div><dt className="text-ink-500">结果摘要</dt><dd className="break-all font-mono text-xs">{result.run?.result_digest ?? '-'}</dd></div>
                <div><dt className="text-ink-500">运行摘要</dt><dd className="break-all font-mono text-xs">{result.run?.run_digest ?? '-'}</dd></div>
              </dl>
            </Card>
          </div>
          <div id="factor-stability-results" className="scroll-mt-16">
            <FactorStabilityResults
              stability={result.stability}
              configured={Boolean(result.request.stability)}
            />
          </div>
          <NeutralizationResult result={result.neutralization} />
          {result.protocol_review && (
            <Card
              id="factor-protocol-review"
              className="mt-4"
              title="预注册协议审查"
              description={`${result.protocol_review.protocol_id} v${result.protocol_review.version} · 只读判定`}
            >
              <Banner variant={result.protocol_review.passed ? 'ok' : 'warning'}>
                {result.protocol_review.passed
                  ? '全部预注册阈值通过。'
                  : '至少一项预注册阈值未通过；若协议要求全部通过，策略导出将被阻断。'}
              </Banner>
              <div className="mt-3 overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-ink-200 text-left text-xs text-ink-500">
                      <th className="p-2">指标</th>
                      <th className="p-2 text-right">实际值</th>
                      <th className="p-2 text-right">门槛</th>
                      <th className="p-2">结论</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.protocol_review.checks.map((check) => (
                      <tr key={check.metric} className="border-b border-ink-100">
                        <td className="p-2">{check.metric}</td>
                        <td className="p-2 text-right font-mono">{metric(check.actual)}</td>
                        <td className="p-2 text-right font-mono">
                          {check.operator} {metric(check.threshold)}
                        </td>
                        <td className="p-2">{check.passed ? '通过' : '未通过'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}
          {!result.implementation && (
            <Card id="factor-implementation-results" className="mt-4 scroll-mt-16" title="实施质量">
              <p className="text-sm text-ink-500">
                这是旧版研究运行，未保存换手、成本或容量证据。重新运行可生成新指标。
              </p>
            </Card>
          )}
          {result.implementation && (
            <Card id="factor-implementation-results" className="mt-4 scroll-mt-16" title="实施质量：毛收益、成本、换手与容量"
              description={`每 ${result.implementation.assumptions.rebalance_interval_sessions} 个交易日调仓；默认成本 ${result.implementation.assumptions.default_cost_bps} bps`}>
              <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                {[
                  ['毛多空均值', metric(result.implementation.gross.long_short.mean)],
                  ['净多空均值', metric(result.implementation.net_default.long_short.mean)],
                  ['平均单边换手', metric(result.implementation.turnover.long_short.mean)],
                  ['可评估覆盖', result.implementation.coverage.evaluation_ratio == null
                    ? '-'
                    : `${(result.implementation.coverage.evaluation_ratio * 100).toFixed(1)}%`],
                ].map(([label, value]) => (
                  <div key={label} className="rounded border border-ink-200 p-3">
                    <p className="text-xs text-ink-500">{label}</p>
                    <p className="mt-1 font-semibold tnum">{value}</p>
                  </div>
                ))}
              </div>
              <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-2">
                <div>
                  <h4 className="mb-2 text-sm font-medium text-ink-700">费率敏感性与净分层</h4>
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-sm">
                      <thead><tr className="border-b border-ink-200 text-ink-500">
                        <th className="p-2">成本</th>
                        {Object.keys(result.implementation.gross.mean_group_returns).map((group) => (
                          <th key={group} className="p-2">Q{group} 净</th>
                        ))}
                        <th className="p-2">多空净值</th>
                      </tr></thead>
                      <tbody>{result.implementation.cost_sensitivity.map((scenario) => (
                        <tr key={scenario.cost_bps} className="border-b border-ink-100">
                          <td className="p-2 tnum">{scenario.cost_bps} bps</td>
                          {Object.entries(scenario.mean_group_returns).map(([group, value]) => (
                            <td key={group} className="p-2 tnum">{metric(value)}</td>
                          ))}
                          <td className="p-2 tnum">{metric(scenario.long_short.mean)}</td>
                        </tr>
                      ))}</tbody>
                    </table>
                  </div>
                </div>
                <EChart option={turnoverOption} style={{ height: 280 }} />
              </div>
              <div className="mt-4 rounded border border-ink-200 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h4 className="text-sm font-medium text-ink-700">容量与可交易覆盖</h4>
                    <p className="mt-1 text-xs text-ink-500">
                      {capacityStatusText(
                        result.implementation.capacity.status,
                        result.implementation.capacity.reason,
                      )}
                      {' · '}
                      成交额币种沿用来源字段，不做未知币种换算
                    </p>
                  </div>
                  <StatusTag variant={result.implementation.capacity.status === 'available'
                    ? 'verified'
                    : 'warning'}>
                    {result.implementation.capacity.status}
                  </StatusTag>
                </div>
                {result.implementation.capacity.status === 'unavailable' ? (
                  <p role="status" className="mt-3 text-sm text-ink-500">
                    未生成容量数字；请先构建含可信 amount 字段的数据缓存。
                  </p>
                ) : (
                  <div className="mt-3 overflow-x-auto">
                    <table className="w-full text-left text-sm">
                      <thead><tr className="border-b border-ink-200 text-ink-500">
                        <th className="p-2">成交额参与率</th><th className="p-2">平均容量</th>
                        <th className="p-2">最小</th><th className="p-2">有效调仓日</th>
                      </tr></thead>
                      <tbody>{Object.entries(result.implementation.capacity.scenarios).map(([rate, item]) => (
                        <tr key={rate} className="border-b border-ink-100">
                          <td className="p-2 tnum">{(Number(rate) * 100).toFixed(1)}%</td>
                          <td className="p-2 tnum">{metric(item.mean, 0)}</td>
                          <td className="p-2 tnum">{metric(item.min, 0)}</td>
                          <td className="p-2 tnum">{item.count}</td>
                        </tr>
                      ))}</tbody>
                    </table>
                  </div>
                )}
              </div>
            </Card>
          )}
          {!result.multi_factor && (
            <Card className="mt-4" title="多因子质量">
              <p className="text-sm text-ink-500">
                这是旧版研究运行，未保存同窗相关、正交化或受约束组合证据。
              </p>
            </Card>
          )}
          {result.multi_factor && (
            <Card className="mt-4" title="多因子相关、正交化与受约束组合"
              description={result.multi_factor.status === 'single_factor'
                ? '本次只选择了一个因子；可重新运行并勾选其他因子生成交叉相关。'
                : '所有因子按同一日期与股票代码对齐，正交化只拟合请求窗口。'}>
              <Banner variant="warning" title="研究组合尚未发布">
                {result.multi_factor.publication.message}
              </Banner>
              <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-2">
                <div>
                  <h4 className="mb-2 text-sm font-medium text-ink-700">Pearson 截面相关</h4>
                  <CorrelationTable matrix={result.multi_factor.correlation.pearson}
                    factorNames={factorNames} />
                </div>
                <div>
                  <h4 className="mb-2 text-sm font-medium text-ink-700">Spearman 截面相关</h4>
                  <CorrelationTable matrix={result.multi_factor.correlation.spearman}
                    factorNames={factorNames} />
                </div>
              </div>
              <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-2">
                <div>
                  <h4 className="mb-2 text-sm font-medium text-ink-700">确定性正交化步骤</h4>
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-sm">
                      <thead><tr className="border-b border-ink-200 text-ink-500">
                        <th className="p-2">顺序</th><th className="p-2">因子</th>
                        <th className="p-2">回归基准</th><th className="p-2">有效日期</th>
                      </tr></thead>
                      <tbody>{result.multi_factor.orthogonalization.steps.map((step, index) => (
                        <tr key={step.factor_id} className="border-b border-ink-100">
                          <td className="p-2 tnum">{index + 1}</td>
                          <td className="p-2">{factorNames[step.factor_id] ?? step.factor_id}</td>
                          <td className="p-2">{step.regressed_on.map((item) => (
                            factorNames[item] ?? item
                          )).join('、') || '原始标准分'}</td>
                          <td className="p-2 tnum">{step.successful_dates}</td>
                        </tr>
                      ))}</tbody>
                    </table>
                  </div>
                  {!result.multi_factor.orthogonalization.enabled && (
                    <p className="mt-2 text-xs text-ink-500">本次请求关闭了正交化，组合使用预处理分值。</p>
                  )}
                </div>
                <div>
                  <h4 className="mb-2 text-sm font-medium text-ink-700">非负有界组合</h4>
                  <dl className="space-y-2 text-sm">
                    {Object.entries(result.multi_factor.combination.weights).map(([factor, weight]) => (
                      <div key={factor} className="flex justify-between gap-4">
                        <dt>{factorNames[factor] ?? factor}</dt>
                        <dd className="tnum">{(weight * 100).toFixed(1)}%</dd>
                      </div>
                    ))}
                    <div className="flex justify-between gap-4 border-t border-ink-200 pt-2">
                      <dt>组合 RankIC</dt>
                      <dd className="tnum">{metric(
                        result.multi_factor.combination.ic.summary.rank_ic.mean,
                      )}</dd>
                    </div>
                    <div className="flex justify-between gap-4">
                      <dt>组合多空均值</dt>
                      <dd className="tnum">{metric(
                        result.multi_factor.combination.quantile_returns.long_short.mean,
                      )}</dd>
                    </div>
                    <div><dt className="text-ink-500">输入摘要</dt>
                      <dd className="break-all font-mono text-xs">
                        {result.multi_factor.orthogonalization.input_digest}
                      </dd>
                    </div>
                    <div><dt className="text-ink-500">组合分值摘要</dt>
                      <dd className="break-all font-mono text-xs">
                        {result.multi_factor.combination.score_digest}
                      </dd>
                    </div>
                  </dl>
                </div>
              </div>
            </Card>
          )}
        </div>
      )}

      <Card
        id="factor-history"
        className="mt-4 scroll-mt-16"
        title="研究历史与因子对比"
        description="历史仅显示当前用户的运行；选择 2–20 条可比较或用于导出证据"
        actions={<Button size="sm" variant="secondary" loading={comparing}
          disabled={selectedRunIds.length < 2} onClick={() => void compare()}>比较已选</Button>}
      >
        <div className="mb-4 grid gap-3 md:grid-cols-3">
          <Input
            label="搜索运行"
            value={runQuery}
            placeholder="运行 ID 或因子 ID"
            onChange={(event) => {
              setRunQuery(event.target.value);
              setRunPage(1);
            }}
          />
          <Select
            label="筛选因子"
            value={runFactorFilter}
            onChange={(event) => {
              setRunFactorFilter(event.target.value);
              setRunPage(1);
            }}
            options={[
              { value: '', label: '全部因子' },
              ...factors.map((factor) => ({
                value: factor.factor_id,
                label: factor.name,
              })),
            ]}
          />
          <Select
            label="排列顺序"
            value={runSort}
            onChange={(event) => {
              setRunSort(event.target.value as FactorRunSort);
              setRunPage(1);
            }}
            options={[
              { value: 'newest', label: '创建时间：最新优先' },
              { value: 'oldest', label: '创建时间：最早优先' },
              { value: 'factor', label: '因子 ID' },
              { value: 'horizon', label: '主周期' },
            ]}
          />
        </div>
        {loadingRuns ? (
          <p role="status" className="text-sm text-ink-500">正在加载研究历史…</p>
        ) : runTotal === 0 && !runQuery && !runFactorFilter ? (
          <p className="text-sm text-ink-500">尚无研究运行。通过上方配置完成第一条研究。</p>
        ) : visibleRuns.length === 0 ? (
          <p role="status" className="text-sm text-ink-500">
            没有符合当前筛选条件的研究运行。
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead><tr className="border-b border-ink-200 text-ink-500">
                <th className="p-2">选择</th><th className="p-2">因子</th>
                <th className="p-2">周期</th><th className="p-2">创建时间</th><th className="p-2">操作</th>
              </tr></thead>
              <tbody>{visibleRuns.map((runItem) => (
                <tr key={runItem.run_id} className="border-b border-ink-100">
                  <td className="p-2"><input type="checkbox" aria-label={`选择研究 ${runItem.run_id}`}
                    checked={selectedRunIds.includes(runItem.run_id)}
                    onChange={(event) => setSelectedRunIds((current) => event.target.checked
                      ? [...current, runItem.run_id].slice(-20)
                      : current.filter((item) => item !== runItem.run_id))} /></td>
                  <td className="p-2">{factors.find((factor) => factor.factor_id === runItem.factor_id)?.name ?? runItem.factor_id}</td>
                  <td className="p-2 tnum">{runItem.request.primary_horizon} 日</td>
                  <td className="p-2 tnum">{formatBackendDateTime(runItem.created_at)}</td>
                  <td className="p-2"><div className="flex gap-2">
                    <Button size="sm" variant="ghost" onClick={() => void openRun(runItem.run_id)}>查看</Button>
                    <FactorEvidenceExportButtons runId={runItem.run_id} />
                    <Button size="sm" variant="ghost" onClick={() => void archiveRun(runItem.run_id)}>归档</Button>
                  </div></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-ink-500">
            共 {runTotal} 条 · 第 {runPage} / {Math.max(1, Math.ceil(runTotal / RUN_PAGE_SIZE))} 页
          </p>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="ghost"
              disabled={runPage <= 1 || loadingRuns}
              onClick={() => setRunPage((page) => Math.max(1, page - 1))}
            >
              上一页历史
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={runPage * RUN_PAGE_SIZE >= runTotal || loadingRuns}
              onClick={() => setRunPage((page) => page + 1)}
            >
              下一页历史
            </Button>
          </div>
        </div>
      </Card>

      {comparison && (
        <Card className="mt-4" title="因子比较"
          description={comparison.dataset_consistent ? '数据摘要一致，可直接横向比较' : '数据摘要不一致，仅作提示性比较'}>
          <FactorComparisonVisualization comparison={comparison} factorNames={factorNames} />
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead><tr className="border-b border-ink-200 text-ink-500">
                <th className="p-2">因子</th><th className="p-2">RankIC 均值</th>
                <th className="p-2">IR</th><th className="p-2">胜率</th>
                <th className="p-2">多空收益</th><th className="p-2">单调性</th>
              </tr></thead>
              <tbody>{comparison.runs.map((item) => (
                <tr key={item.run_id} className="border-b border-ink-100">
                  <td className="p-2">{factors.find((factor) => factor.factor_id === item.factor_id)?.name ?? item.factor_id}</td>
                  <td className="p-2 tnum">{metric(item.rank_ic_mean)}</td>
                  <td className="p-2 tnum">{metric(item.rank_ic_ir)}</td>
                  <td className="p-2 tnum">{item.rank_ic_positive_ratio == null ? '-' : `${(item.rank_ic_positive_ratio * 100).toFixed(1)}%`}</td>
                  <td className="p-2 tnum">{metric(item.long_short_mean)}</td>
                  <td className="p-2 tnum">{metric(item.monotonicity)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </Card>
      )}

      <Card id="factor-export" className="mt-4 scroll-mt-16" title="证据化因子组合策略"
        description="仅从已选研究运行导出；策略定义绑定运行、数据和结果摘要">
        <FactorExportEvidence
          runs={runs}
          selectedRunIds={selectedRunIds}
          published={exportedStrategyId && exportedVersion ? {
            strategyId: exportedStrategyId,
            version: exportedVersion.version,
            evidenceCount: exportedVersion.evidenceCount,
          } : null}
        />
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
          <Input label="策略名称" value={strategyName}
            onChange={(event) => setStrategyName(event.target.value)} />
          <div>
            <p className="mb-1 text-sm font-medium text-ink-700">已选研究因子权重</p>
            <div className="grid grid-cols-2 gap-2">
              {factors.filter((factor) => runs.some((runItem) =>
                selectedRunIds.includes(runItem.run_id) && runItem.factor_id === factor.factor_id
              )).map((factor) => (
                <Input key={factor.factor_id} aria-label={`${factor.name}权重`} type="number"
                  min={0} max={100} step={0.1} value={weights[factor.factor_id] ?? 0}
                  onChange={(event) => setWeights((current) => ({
                    ...current, [factor.factor_id]: Number(event.target.value),
                  }))} hint={factor.name} />
              ))}
              {selectedRunIds.length === 0 && <p className="text-sm text-ink-500">请先选择研究运行。</p>}
            </div>
          </div>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Button variant="secondary" loading={exporting}
            disabled={!strategyName.trim() || selectedRunIds.length === 0}
            onClick={() => void exportStrategy()}>导出到策略池</Button>
          {exportedStrategyId && (
            <Button variant="ghost" onClick={() => navigate(`/strategies/${exportedStrategyId}`)}>
              查看策略 {exportedStrategyId}
            </Button>
          )}
        </div>
      </Card>
    </div>
  );
}
