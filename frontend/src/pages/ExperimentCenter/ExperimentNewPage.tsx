import { useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate, useSearchParams } from 'react-router';
import { createExperiment, getExperiment, getParameterPreset, listParameterPresets } from '../../services/experiments';
import {
  describeExperimentReadinessBlockers,
  getPoolStocks,
  inspectExperimentDataReadiness,
  listPools,
  refreshIndustryCatalog,
} from '../../services/data';
import { listStrategies } from '../../services/strategies';
import { suggestParams } from '../../services/ai';
import { canSubmitIndustries } from '../../services/industryCatalog';
import type { IndustryCatalogState } from '../../services/industryCatalog';
import type { Experiment, ParameterPreset } from '../../types/experiment';
import type { PoolInfo, PoolStocksResponse } from '../../services/data';
import type { ParamField, StrategyMetadata } from '../../types/strategy';
import { useAuthStore } from '../../store/authStore';
import { strategyCategoryLabel, strategyTrainingMode } from '../../utils/strategy';
import { AiParamSuggestions } from '../../components/ai';
import IndustryMultiSelect from '../../components/data/IndustryMultiSelect';
import { useIndustryCatalog } from '../../components/data/useIndustryCatalog';
import Badge from '../../components/shared/Badge';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import DescriptionList from '../../components/shared/DescriptionList';
import Icon from '../../components/shared/Icon';
import Input from '../../components/shared/Input';
import PageHeader from '../../components/shared/PageHeader';
import Select from '../../components/shared/Select';
import Skeleton from '../../components/shared/Skeleton';
import Textarea from '../../components/shared/Textarea';

const STEPS = ['选择策略', '配置参数', '选择股票池', '选择时间', '确认运行'] as const;

const CATEGORY_TABS = [
  { key: '', label: '全部' },
  { key: 'technical', label: '技术指标' },
  { key: 'ml', label: '机器学习' },
  { key: 'factor', label: '因子' },
  { key: 'portfolio', label: '组合优化' },
  { key: 'composite', label: '组合策略' },
];

function formatDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function addMonths(date: Date, months: number): Date {
  const next = new Date(date);
  next.setMonth(next.getMonth() + months);
  return next;
}

function addDays(date: Date, days: number): Date {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

function isNumericParam(param: ParamField): boolean {
  return ['int', 'integer', 'float', 'number'].includes(param.type.toLowerCase());
}

function isBoolParam(param: ParamField): boolean {
  return ['bool', 'boolean'].includes(param.type.toLowerCase());
}

function getParamError(param: ParamField, value: unknown): string | null {
  if (param.required && (value === undefined || value === null || value === '')) {
    return '该参数为必填项';
  }
  if (isNumericParam(param) && value !== '' && value !== undefined && value !== null) {
    const num = Number(value);
    if (!Number.isFinite(num)) return '请输入有效数字';
    if (param.min != null && num < param.min) return `不能小于 ${param.min}`;
    if (param.max != null && num > param.max) return `不能大于 ${param.max}`;
  }
  return null;
}

function strategyDefaults(strategy: StrategyMetadata): Record<string, unknown> {
  return Object.fromEntries(strategy.params.map((param) => [param.name, param.default]));
}

function parsePositiveInt(value: string | null): number | null {
  if (!value) return null;
  const num = Number(value);
  return Number.isInteger(num) && num > 0 ? num : null;
}

function parseStockCodes(value: string): string[] {
  return [...new Set(
    value
      .split(/[,，\s]+/)
      .map((item) => item.trim())
      .filter(Boolean),
  )];
}

export default function ExperimentNewPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const user = useAuthStore((s) => s.user);
  const canUseAi = user?.is_admin || user?.permissions.includes('ai:use');
  const canUpdateData =
    Boolean(user?.is_admin) || Boolean(user?.permissions.includes('data:update'));

  const [step, setStep] = useState(0);
  const [strategies, setStrategies] = useState<StrategyMetadata[]>([]);
  const [strategiesLoading, setStrategiesLoading] = useState(true);
  const [categoryTab, setCategoryTab] = useState('');
  const [selectedStrategy, setSelectedStrategy] = useState<StrategyMetadata | null>(null);

  const [paramValues, setParamValues] = useState<Record<string, unknown>>({});
  const [mode, setMode] = useState('batch');
  const [dataAccessPolicy, setDataAccessPolicy] = useState<'allow_fetch' | 'cache_only'>('cache_only');
  const [researchTrustProfile, setResearchTrustProfile] = useState<
    'governed_production_pit' | 'tushare_research_trusted'
  >('tushare_research_trusted');
  const [presets, setPresets] = useState<ParameterPreset[]>([]);

  const [pools, setPools] = useState<PoolInfo[]>([]);
  const [poolPreset, setPoolPreset] = useState('');
  const [customCodes, setCustomCodes] = useState('');
  const [poolStocks, setPoolStocks] = useState<string[]>([]);
  const [poolStockEvidence, setPoolStockEvidence] = useState<PoolStocksResponse | null>(null);
  const [poolStocksLoading, setPoolStocksLoading] = useState(false);
  const [stockSearch, setStockSearch] = useState('');
  const [selectedPoolCodes, setSelectedPoolCodes] = useState<string[]>([]);
  const [selectedIndustries, setSelectedIndustries] = useState<string[]>([]);
  const [industryRefreshing, setIndustryRefreshing] = useState(false);
  const [industryRefreshError, setIndustryRefreshError] = useState<string | null>(null);

  const [trainStart, setTrainStart] = useState('');
  const [trainEnd, setTrainEnd] = useState('');
  const [testStart, setTestStart] = useState('');
  const [testEnd, setTestEnd] = useState('');

  const [name, setName] = useState('');
  const [sourceExperiment, setSourceExperiment] = useState<Experiment | null>(null);
  const [inheritError, setInheritError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const parsedCustomCodes = useMemo(() => parseStockCodes(customCodes), [customCodes]);
  const selectedIndustryCodes =
    poolPreset === 'custom'
      ? parsedCustomCodes
      : selectedPoolCodes.length > 0
        ? selectedPoolCodes
        : undefined;
  const selectedIndustryPool =
    poolPreset && poolPreset !== 'custom' && !selectedIndustryCodes
      ? poolPreset
      : undefined;
  const hasIndustryScope =
    Boolean(selectedIndustryPool) || Boolean(selectedIndustryCodes?.length);
  const { catalog, loading: catalogLoading, error: catalogError, retry: retryCatalog } =
    useIndustryCatalog(undefined, selectedIndustryPool, selectedIndustryCodes);

  const catalogState: IndustryCatalogState | null = catalog;
  const industryGuard = canSubmitIndustries(selectedIndustries, catalogState);
  const industryBusy = catalogLoading && selectedIndustries.length > 0;

  const refreshSelectedIndustryScope = async () => {
    if (!hasIndustryScope) return;
    setIndustryRefreshing(true);
    setIndustryRefreshError(null);
    try {
      await refreshIndustryCatalog(
        selectedIndustryPool,
        undefined,
        selectedIndustryCodes,
      );
      retryCatalog();
    } catch (err: unknown) {
      setIndustryRefreshError(
        err instanceof Error ? err.message : '行业映射补全失败',
      );
    } finally {
      setIndustryRefreshing(false);
    }
  };

  /* ── Initial catalogs ────────────────────────────────────────────────── */
  useEffect(() => {
    let cancelled = false;
    void listStrategies()
      .then((result) => {
        if (!cancelled) setStrategies(result);
      })
      .catch(() => {
        if (!cancelled) setStrategies([]);
      })
      .finally(() => {
        if (!cancelled) setStrategiesLoading(false);
      });
    void listPools()
      .then((result) => {
        if (!cancelled) setPools(result);
      })
      .catch(() => {
        if (!cancelled) setPools([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /* ── Presets for selected strategy ───────────────────────────────────── */
  useEffect(() => {
    if (!selectedStrategy) {
      setPresets([]);
      return;
    }
    let cancelled = false;
    void listParameterPresets(selectedStrategy.strategy_id)
      .then((result) => {
        if (!cancelled) setPresets(result);
      })
      .catch(() => {
        if (!cancelled) setPresets([]);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedStrategy]);

  /* ── Inheritance: ?from_experiment= / ?preset_id= / ?strategy_id= ────── */
  const inheritLoadedRef = useState({ done: false })[0];
  useEffect(() => {
    if (strategiesLoading || strategies.length === 0 || inheritLoadedRef.done) return;
    const fromExperiment = parsePositiveInt(searchParams.get('from_experiment'));
    const presetId = parsePositiveInt(searchParams.get('preset_id'));
    const strategyIdParam = searchParams.get('strategy_id');
    const candidateDraft = (
      location.state as {
        portfolioCandidateDraft?: {
          name: string;
          params: Record<string, unknown>;
          poolPreset: string;
          customCodes: string[];
          industries: string[];
          testStart: string;
          testEnd: string;
        };
      } | null
    )?.portfolioCandidateDraft;
    if (!fromExperiment && !presetId && !strategyIdParam) {
      inheritLoadedRef.done = true;
      return;
    }
    inheritLoadedRef.done = true;

    const applyStrategy = (strategyId: string): StrategyMetadata | null => {
      const strategy = strategies.find((item) => item.strategy_id === strategyId) ?? null;
      if (!strategy) {
        setInheritError('来源配置对应的策略当前不可用');
        return null;
      }
      setSelectedStrategy(strategy);
      setParamValues(strategyDefaults(strategy));
      setMode(strategy.supported_modes[0] || 'batch');
      return strategy;
    };

    if (fromExperiment) {
      void getExperiment(fromExperiment)
        .then((experiment) => {
          const strategy = applyStrategy(experiment.strategy_id);
          if (!strategy) return;
          setSourceExperiment(experiment);
          setName(`${experiment.name} - 副本`);
          setParamValues({ ...strategyDefaults(strategy), ...experiment.params });
          setMode(experiment.mode || strategy.supported_modes[0] || 'batch');
          setDataAccessPolicy('cache_only');
          setPoolPreset(experiment.pool_preset ?? 'custom');
          setCustomCodes((experiment.pool_custom_codes ?? []).join(','));
          setSelectedPoolCodes(experiment.pool_custom_codes ?? []);
          setSelectedIndustries(experiment.pool_industries ?? []);
          setTrainStart(experiment.train_start ?? '');
          setTrainEnd(experiment.train_end ?? '');
          setTestStart(experiment.test_start);
          setTestEnd(experiment.test_end);
        })
        .catch(() => setInheritError('来源实验加载失败'));
    } else if (presetId) {
      void getParameterPreset(presetId)
        .then((preset) => {
          const strategy = applyStrategy(preset.strategy_id);
          if (!strategy) return;
          setName(preset.name);
          setParamValues({ ...strategyDefaults(strategy), ...preset.params });
          setMode(preset.mode || strategy.supported_modes[0] || 'batch');
          setPoolPreset(preset.pool_preset);
          setCustomCodes((preset.pool_custom_codes ?? []).join(','));
          setSelectedPoolCodes(preset.pool_custom_codes ?? []);
          setSelectedIndustries(preset.pool_industries ?? []);
        })
        .catch(() => setInheritError('参数方案加载失败'));
    } else if (strategyIdParam) {
      const strategy = applyStrategy(strategyIdParam);
      if (strategy && candidateDraft) {
        setName(candidateDraft.name);
        setParamValues({ ...strategyDefaults(strategy), ...candidateDraft.params });
        setDataAccessPolicy('cache_only');
        setPoolPreset(candidateDraft.poolPreset);
        setCustomCodes(candidateDraft.customCodes.join(','));
        setSelectedPoolCodes(candidateDraft.customCodes);
        setSelectedIndustries(candidateDraft.industries);
        setTestStart(candidateDraft.testStart);
        setTestEnd(candidateDraft.testEnd);
      }
    }
  }, [
    strategies,
    strategiesLoading,
    searchParams,
    inheritLoadedRef,
    location.state,
  ]);

  /* ── Pool stocks for the selected preset pool ────────────────────────── */
  useEffect(() => {
    if (!poolPreset || poolPreset === 'custom') {
      setPoolStocks([]);
      setPoolStockEvidence(null);
      return;
    }
    let cancelled = false;
    setPoolStocksLoading(true);
    void getPoolStocks(poolPreset)
      .then((result) => {
        if (!cancelled) {
          setPoolStocks(result.stocks);
          setPoolStockEvidence(result);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setPoolStocks([]);
          setPoolStockEvidence(null);
        }
      })
      .finally(() => {
        if (!cancelled) setPoolStocksLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [poolPreset]);

  const selectStrategy = (strategy: StrategyMetadata) => {
    setSelectedStrategy(strategy);
    setParamValues(strategyDefaults(strategy));
    setMode(strategy.supported_modes[0] || 'batch');
    setDataAccessPolicy('cache_only');
    setSourceExperiment(null);
    setInheritError(null);
    if (!name) setName(`${strategy.display_name} 实验`);
  };

  const applyPreset = (presetId: string) => {
    const preset = presets.find((item) => String(item.id) === presetId);
    if (!preset || !selectedStrategy) return;
    setParamValues({ ...strategyDefaults(selectedStrategy), ...preset.params });
    setMode(preset.mode || selectedStrategy.supported_modes[0] || 'batch');
    setPoolPreset(preset.pool_preset);
    setCustomCodes((preset.pool_custom_codes ?? []).join(','));
    setSelectedPoolCodes(preset.pool_custom_codes ?? []);
    setSelectedIndustries(preset.pool_industries ?? []);
  };

  const setParam = (param: ParamField, raw: string | boolean) => {
    setParamValues((current) => {
      let value: unknown = raw;
      if (typeof raw === 'string') {
        const type = param.type.toLowerCase();
        if (type === 'int' || type === 'integer') value = parseInt(raw, 10) || 0;
        else if (type === 'float' || type === 'number') value = parseFloat(raw) || 0;
        else if (type === 'list' || type === 'array') {
          value = raw.split(/[,，]/).map((item) => item.trim()).filter(Boolean);
        } else value = raw;
      }
      return { ...current, [param.name]: value };
    });
  };

  const paramErrors = useMemo(() => {
    if (!selectedStrategy) return {} as Record<string, string>;
    const errors: Record<string, string> = {};
    for (const param of selectedStrategy.params) {
      const error = getParamError(param, paramValues[param.name]);
      if (error) errors[param.name] = error;
    }
    return errors;
  }, [selectedStrategy, paramValues]);

  const trainingMode = selectedStrategy ? strategyTrainingMode(selectedStrategy) : 'none';
  const isTrainOnce = trainingMode === 'train_once';

  const applyQuickRange = (months: number) => {
    const today = new Date();
    const start = addMonths(today, -months);
    setTestStart(formatDate(start));
    setTestEnd(formatDate(today));
    if (isTrainOnce) {
      const trainEndDate = addDays(start, -1);
      setTrainEnd(formatDate(trainEndDate));
      setTrainStart(formatDate(addMonths(trainEndDate, -60)));
    } else {
      setTrainStart('');
      setTrainEnd('');
    }
  };

  const filteredStocks = useMemo(() => {
    const keyword = stockSearch.trim().toUpperCase();
    if (!keyword) return poolStocks;
    return poolStocks.filter((code) => code.toUpperCase().includes(keyword));
  }, [poolStocks, stockSearch]);

  const visibleStocks = filteredStocks.slice(0, 200);

  const togglePoolCode = (code: string) => {
    setSelectedPoolCodes((current) =>
      current.includes(code) ? current.filter((item) => item !== code) : [...current, code],
    );
  };

  /* ── Step gating ─────────────────────────────────────────────────────── */
  const canNext = (): boolean => {
    if (step === 0) return selectedStrategy !== null;
    if (step === 1) return Object.keys(paramErrors).length === 0;
    if (step === 2) {
      if (!poolPreset) return false;
      if (poolPreset === 'custom' && parsedCustomCodes.length === 0) {
        return false;
      }
      if (industryBusy) return false;
      return industryGuard.ok;
    }
    if (step === 3) {
      if (!testStart || !testEnd || testStart >= testEnd) return false;
      if (isTrainOnce && (!trainStart || !trainEnd || trainStart >= trainEnd || trainEnd >= testStart)) {
        return false;
      }
      return true;
    }
    if (step === 4) return name.trim().length > 0;
    return false;
  };

  const handleSubmit = async () => {
    setSubmitError(null);
    if (!selectedStrategy) return;
    if (!poolPreset || (poolPreset === 'custom' && !customCodes.trim())) {
      setSubmitError('请选择股票池；自定义股票池必须填写股票代码');
      return;
    }
    if (!testStart || !testEnd || testStart >= testEnd) {
      setSubmitError('测试开始日期必须早于测试结束日期');
      return;
    }
    if (isTrainOnce && (!trainStart || !trainEnd || trainStart >= trainEnd || trainEnd >= testStart)) {
      setSubmitError('一次训练模型必须提供早于测试窗口的完整训练区间');
      return;
    }
    if (!industryGuard.ok) {
      setSubmitError(industryGuard.reason);
      return;
    }
    if (researchTrustProfile === 'tushare_research_trusted' && poolPreset === 'custom') {
      setSubmitError('Tushare 条件信任仅支持已有历史成分的预置指数池；自定义股票池尚未物化');
      return;
    }

    setSubmitting(true);
    try {
      const submittedCodes =
        poolPreset === 'custom'
          ? parsedCustomCodes
          : selectedPoolCodes;
      if (dataAccessPolicy === 'cache_only') {
        const readiness = await inspectExperimentDataReadiness({
          data_access_policy: 'cache_only',
          research_trust_profile: researchTrustProfile,
          price_purpose: 'return_research',
          pool_preset: poolPreset,
          pool_custom_codes: submittedCodes,
          train_start: isTrainOnce ? trainStart : undefined,
          test_start: testStart,
          test_end: testEnd,
        });
        if (!readiness.ready) {
          const issues = describeExperimentReadinessBlockers(readiness);
          throw new Error(`本地缓存未覆盖实验输入：${issues.join('、')}`);
        }
      }
      const result = await createExperiment({
        name: name.trim(),
        strategy_id: selectedStrategy.strategy_id,
        pool_preset: poolPreset || 'custom',
        pool_custom_codes:
          poolPreset === 'custom'
            ? parsedCustomCodes
            : selectedPoolCodes.length > 0
              ? selectedPoolCodes
              : null,
        pool_industries: selectedIndustries,
        train_start: isTrainOnce ? trainStart : undefined,
        train_end: isTrainOnce ? trainEnd : undefined,
        test_start: testStart,
        test_end: testEnd,
        params: paramValues,
        mode,
        data_access_policy: dataAccessPolicy,
        research_trust_profile: researchTrustProfile,
        source_experiment_id: sourceExperiment?.id,
      });
      navigate(`/experiment/${result.experiment_id}`);
    } catch (err: unknown) {
      setSubmitError(err instanceof Error ? err.message : '创建实验失败');
      setSubmitting(false);
    }
  };

  const filteredStrategies = strategies.filter(
    (strategy) => !categoryTab || strategy.category === categoryTab,
  );

  return (
    <div>
      <PageHeader
        title="新建实验"
        description="按步骤配置策略、参数、股票池与研究窗口。提交前所有内容都可修改。"
        breadcrumb={[{ label: '研究' }, { label: '实验中心', to: '/experiment' }, { label: '新建实验' }]}
      />

      {sourceExperiment && (
        <Banner
          variant="info"
          className="mb-4"
          action={
            <Button variant="ghost" size="sm" onClick={() => navigate(`/experiment/${sourceExperiment.id}`)}>
              查看来源实验
            </Button>
          }
        >
          当前配置继承自实验 #{sourceExperiment.id}「{sourceExperiment.name}」，提交前可修改全部参数。
        </Banner>
      )}
      {inheritError && (
        <Banner variant="warning" className="mb-4">
          {inheritError}
        </Banner>
      )}

      {/* Step indicator */}
      <ol aria-label="创建步骤" className="mb-6 flex flex-wrap items-center gap-y-2">
        {STEPS.map((label, index) => {
          const complete = index < step;
          const current = index === step;
          return (
            <li key={label} className="flex items-center">
              {index > 0 && <span className="mx-2 h-px w-6 bg-ink-200 sm:w-10" aria-hidden />}
              <button
                type="button"
                disabled={index >= step}
                onClick={() => setStep(index)}
                aria-current={current ? 'step' : undefined}
                aria-label={`步骤 ${index + 1}：${label}${complete ? '，已完成' : ''}${current ? '，当前步骤' : ''}`}
                className={`flex items-center gap-2 rounded px-1.5 py-1 text-sm transition-colors ${
                  current
                    ? 'font-semibold text-accent-800'
                    : complete
                      ? 'text-ink-600 hover:text-accent-700'
                      : 'cursor-not-allowed text-ink-300'
                }`}
              >
                <span
                  className={`flex h-6 w-6 items-center justify-center rounded-full border text-xs font-semibold ${
                    current
                      ? 'border-accent-700 bg-accent-700 text-white'
                      : complete
                        ? 'border-accent-600 bg-accent-50 text-accent-700'
                        : 'border-ink-300 text-ink-300'
                  }`}
                >
                  {complete ? <Icon name="check" className="h-3.5 w-3.5" /> : index + 1}
                </span>
                <span className="hidden sm:inline">{label}</span>
              </button>
            </li>
          );
        })}
      </ol>

      {/* Step 1: strategy */}
      {step === 0 && (
        <Card title="选择策略" description="策略参数契约与训练模式由此决定">
          <div className="mb-4 flex flex-wrap gap-1.5" role="group" aria-label="按分类筛选策略">
            {CATEGORY_TABS.map((tab) => (
              <button
                key={tab.key}
                type="button"
                aria-pressed={categoryTab === tab.key}
                onClick={() => setCategoryTab(tab.key)}
                className={`rounded border px-2.5 py-1 text-xs font-medium transition-colors ${
                  categoryTab === tab.key
                    ? 'border-accent-700 bg-accent-700 text-white'
                    : 'border-ink-300 bg-surface text-ink-600 hover:bg-ink-100'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>
          {strategiesLoading ? (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <Skeleton className="h-28 w-full" />
              <Skeleton className="h-28 w-full" />
            </div>
          ) : (
            <div role="radiogroup" aria-label="可选策略" className="grid grid-cols-1 gap-3 md:grid-cols-2">
              {filteredStrategies.map((strategy) => {
                const selected = selectedStrategy?.strategy_id === strategy.strategy_id;
                const modeOf = strategyTrainingMode(strategy);
                return (
                  <button
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    key={strategy.strategy_id}
                    onClick={() => selectStrategy(strategy)}
                    className={`rounded-md border p-4 text-left transition-colors ${
                      selected
                        ? 'border-accent-700 bg-accent-50 ring-1 ring-accent-700'
                        : 'border-ink-200 bg-surface hover:border-accent-400'
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <p className="font-semibold text-ink-900">{strategy.display_name}</p>
                      <Badge variant="accent" size="sm">
                        {strategyCategoryLabel(strategy.category)}
                      </Badge>
                    </div>
                    <p className="mt-1 line-clamp-2 text-xs leading-5 text-ink-500">{strategy.description}</p>
                    <div className="mt-2 flex flex-wrap items-center gap-1.5">
                      {modeOf !== 'none' && (
                        <Badge variant={modeOf === 'periodic' ? 'warning' : 'info'} size="sm">
                          {modeOf === 'periodic' ? '周期重训练' : '一次训练'}
                        </Badge>
                      )}
                      <span className="font-mono text-2xs text-ink-400">{strategy.strategy_id}</span>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </Card>
      )}

      {/* Step 2: params */}
      {step === 1 && selectedStrategy && (
        <Card title="配置参数" description={`${selectedStrategy.display_name} · 带 * 为必填参数`}>
          {presets.length > 0 && (
            <div className="mb-4 max-w-md">
              <Select
                label="已保存参数方案"
                aria-label="套用已保存参数方案"
                value=""
                onChange={(event) => applyPreset(event.target.value)}
                placeholder="选择以套用参数方案..."
                options={presets.map((preset) => ({
                  value: String(preset.id),
                  label: `${preset.name}${preset.is_default ? '（默认）' : ''}`,
                }))}
              />
            </div>
          )}
          {selectedStrategy.params.length === 0 ? (
            <p className="text-sm text-ink-500">该策略无可配置参数。</p>
          ) : (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {selectedStrategy.params.map((param) => {
                const value = paramValues[param.name];
                const error = paramErrors[param.name];
                if (isBoolParam(param)) {
                  return (
                    <div key={param.name} className="flex flex-col justify-end">
                      <label className="flex min-h-[38px] cursor-pointer items-center gap-2 text-sm text-ink-700">
                        <input
                          type="checkbox"
                          checked={Boolean(value)}
                          onChange={(event) => setParam(param, event.target.checked)}
                          className="h-4 w-4 rounded-sm border-ink-300 text-accent-700 focus:ring-accent-600"
                        />
                        {param.name}
                        {param.required && <span className="text-danger-fg" aria-hidden>*</span>}
                      </label>
                      {param.description && <p className="text-xs text-ink-400">{param.description}</p>}
                    </div>
                  );
                }
                if (param.choices && param.choices.length > 0) {
                  return (
                    <Select
                      key={param.name}
                      label={param.name}
                      value={String(value ?? '')}
                      onChange={(event) => setParam(param, event.target.value)}
                      error={error}
                      hint={param.description}
                      options={param.choices.map((choice) => ({ value: choice, label: choice }))}
                    />
                  );
                }
                return (
                  <Input
                    key={param.name}
                    label={param.name}
                    requiredMark={param.required}
                    type={isNumericParam(param) ? 'number' : 'text'}
                    value={value === undefined || value === null ? '' : String(value)}
                    min={param.min ?? undefined}
                    max={param.max ?? undefined}
                    step={param.step ?? undefined}
                    onChange={(event) => setParam(param, event.target.value)}
                    error={error}
                    hint={param.description}
                  />
                );
              })}
            </div>
          )}

          <div className="mt-4 max-w-md">
            <Select
              label="执行模式"
              value={mode}
              onChange={(event) => setMode(event.target.value)}
              options={(selectedStrategy.supported_modes.length > 0
                ? selectedStrategy.supported_modes
                : ['batch']
              ).map((item) => ({ value: item, label: item }))}
            />
          </div>

          {canUseAi && (
            <div className="mt-5">
              <AiParamSuggestions
                strategy={selectedStrategy}
                currentParams={paramValues}
                onSuggest={suggestParams}
                onApply={(nextParams) => setParamValues(nextParams)}
                scopeKey={`experiment-new:${selectedStrategy.strategy_id}`}
              />
            </div>
          )}
        </Card>
      )}

      {/* Step 3: universe */}
      {step === 2 && (
        <Card title="选择股票池" description="行业筛选在实验运行时由服务端按映射执行">
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div>
              <Select
                label="股票池"
                value={poolPreset}
                onChange={(event) => {
                  setPoolPreset(event.target.value);
                  setSelectedIndustries([]);
                  setSelectedPoolCodes([]);
                  setStockSearch('');
                  setIndustryRefreshError(null);
                }}
                placeholder="请选择股票池"
                options={[
                  ...pools.map((pool) => ({
                    value: pool.id,
                    label: `${pool.name} (${pool.count}只)`,
                  })),
                  { value: 'custom', label: '自定义' },
                ]}
              />
              {poolPreset === 'custom' && (
                <div className="mt-4">
                  <Textarea
                    label="自定义股票代码"
                    requiredMark
                    value={customCodes}
                    onChange={(event) => setCustomCodes(event.target.value)}
                    placeholder="000001.SZ, 600000.SH, ..."
                    hint="使用逗号分隔多个代码。"
                  />
                </div>
              )}
            </div>

            {poolPreset && poolPreset !== 'custom' && (
              <div>
                <p className="mb-1 text-sm font-medium text-ink-700">股票子集（可选）</p>
                <Input
                  aria-label="搜索股票代码"
                  value={stockSearch}
                  onChange={(event) => setStockSearch(event.target.value)}
                  placeholder="搜索代码..."
                />
                <p className="mt-1 text-xs text-ink-400 tnum">
                  未选择时使用整个股票池{selectedPoolCodes.length > 0 ? `；已选 ${selectedPoolCodes.length} 只` : ''}。
                </p>
                {poolStockEvidence && !poolStockEvidence.availability.ready && (
                  <Banner variant="warning" className="mt-2" title="PIT 股票池证据不可用">
                    {poolStockEvidence.availability.reason ?? '没有可用于该日期的已激活 PIT 成分证据'}。
                    不会回退到当前成分或联网抓取；请等待 PIT 数据补齐后再提交实验。
                  </Banner>
                )}
                {poolStockEvidence?.availability.resolution === 'weekend_prior_activated_observation' && (
                  <Banner variant="info" className="mt-2" title="仅用于页面展示的周末解析">
                    请求日期 {poolStockEvidence.availability.requested_as_of} 使用
                    {` ${poolStockEvidence.availability.resolved_as_of} `}的已激活 PIT 观察值（
                    {poolStockEvidence.availability.staleness_calendar_days} 个自然日陈旧）。实验执行仍需精确 PIT 覆盖。
                  </Banner>
                )}
                <div className="mt-2 max-h-56 overflow-y-auto rounded border border-ink-200 scrollbar-thin">
                  {poolStocksLoading ? (
                    <p className="px-3 py-2 text-sm text-ink-400">加载股票列表...</p>
                  ) : poolStocks.length === 0 ? (
                    <p className="px-3 py-2 text-sm text-ink-400">暂无可用股票，请先更新该股票池数据。</p>
                  ) : (
                    <ul>
                      {visibleStocks.map((code) => (
                        <li key={code}>
                          <label className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-sm hover:bg-ink-50">
                            <input
                              type="checkbox"
                              checked={selectedPoolCodes.includes(code)}
                              onChange={() => togglePoolCode(code)}
                              className="h-4 w-4 rounded-sm border-ink-300 text-accent-700 focus:ring-accent-600"
                            />
                            <span className="font-mono text-xs">{code}</span>
                          </label>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                {filteredStocks.length > 200 && (
                  <p className="mt-1 text-xs text-ink-400">列表仅展示前 200 条搜索结果。</p>
                )}
              </div>
            )}
          </div>

          {poolPreset && (
            <div className="mt-5 border-t border-ink-100 pt-4">
              <p className="mb-1 text-sm font-medium text-ink-700">行业筛选（可选）</p>
              <p className="mb-2 text-xs text-ink-500">
                筛选就绪状态按实际提交范围单独校验；股票子集和自定义代码也会使用各自的覆盖证据。
              </p>
              {!hasIndustryScope ? (
                <Banner variant="info">
                  请先输入自定义股票代码，再校验或补全该范围的行业映射。
                </Banner>
              ) : (
                <IndustryMultiSelect
                  catalog={catalogState}
                  loading={catalogLoading}
                  error={industryRefreshError ?? catalogError}
                  onRetry={() => {
                    setIndustryRefreshError(null);
                    retryCatalog();
                  }}
                  onRefresh={
                    canUpdateData
                      ? () => void refreshSelectedIndustryScope()
                      : undefined
                  }
                  refreshing={industryRefreshing}
                  selected={selectedIndustries}
                  onChange={setSelectedIndustries}
                />
              )}
              {!industryGuard.ok && (
                <p role="alert" className="mt-2 text-xs text-danger-fg">
                  {industryGuard.reason}
                </p>
              )}
            </div>
          )}
        </Card>
      )}

      {/* Step 4: windows */}
      {step === 3 && (
        <Card title="选择时间" description="研究窗口决定训练、验证与测试的数据边界">
          {trainingMode === 'periodic' && (
            <Banner variant="warning" className="mb-4">
              这是周期重训练策略。平台会在每个重训练点只使用当时可见的历史数据，自动生成训练窗口；你只需设置测试区间。
            </Banner>
          )}
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <span className="text-xs text-ink-500">快捷区间：</span>
            {[
              { label: '近1年', months: 12 },
              { label: '近3年', months: 36 },
              { label: '近5年', months: 60 },
              { label: '全部', months: 120 },
            ].map((item) => (
              <Button key={item.label} variant="secondary" size="sm" onClick={() => applyQuickRange(item.months)}>
                {item.label}
              </Button>
            ))}
          </div>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {isTrainOnce && (
              <>
                <Input
                  label="训练开始"
                  type="date"
                  value={trainStart}
                  onChange={(event) => setTrainStart(event.target.value)}
                />
                <Input
                  label="训练结束"
                  type="date"
                  value={trainEnd}
                  onChange={(event) => setTrainEnd(event.target.value)}
                  hint="必须早于测试开始"
                />
              </>
            )}
            <Input
              label="测试开始"
              type="date"
              requiredMark
              value={testStart}
              onChange={(event) => setTestStart(event.target.value)}
            />
            <Input
              label="测试结束"
              type="date"
              requiredMark
              value={testEnd}
              onChange={(event) => setTestEnd(event.target.value)}
              hint="必须晚于测试开始"
            />
          </div>
          {testStart && testEnd && testStart >= testEnd && (
            <p role="alert" className="mt-2 text-xs text-danger-fg">
              测试开始日期必须早于测试结束日期
            </p>
          )}
          {isTrainOnce && trainStart && trainEnd && (trainStart >= trainEnd || (testStart && trainEnd >= testStart)) && (
            <p role="alert" className="mt-2 text-xs text-danger-fg">
              一次训练模型必须提供早于测试窗口的完整训练区间
            </p>
          )}
          <div className="mt-5 max-w-xl border-t border-ink-100 pt-4">
            <Select
              label="数据访问策略"
              value={dataAccessPolicy}
              onChange={(event) => setDataAccessPolicy(
                event.target.value as 'allow_fetch' | 'cache_only',
              )}
              options={[
                { value: 'cache_only', label: '仅使用已激活 PIT 治理数据（不联网）' },
              ]}
              hint="正式实验只接受已激活 PIT 成分与精确双价格账本绑定；隔离数据、旧缓存和联网补数都会安全拒绝。"
            />
            <div className="mt-3">
              <Select
                label="研究信任档案"
                value={researchTrustProfile}
                onChange={(event) => setResearchTrustProfile(
                  event.target.value as 'governed_production_pit' | 'tushare_research_trusted',
                )}
                options={[
                  { value: 'tushare_research_trusted', label: '个人研究先信任 Tushare' },
                  { value: 'governed_production_pit', label: '严格生产 PIT 门禁' },
                ]}
                hint="条件信任允许研究、调优和模拟盘，但会保留高等级数据警告；实盘和生产 PIT 激活仍保持关闭。"
              />
            </div>
            {researchTrustProfile === 'tushare_research_trusted' && (
              <Banner variant="warning" className="mt-3" title="条件性个人研究，不是生产 PIT">
                使用 Tushare 四指数月度历史和本地研究行情。月内调样生效时点、历史行业、
                available_at/revision、官方事件对账和双价格账本仍未认证；行业筛选可继续，但会把“当前分类代替历史分类”作为高等级警告永久绑定。
                2026-07 空快照不会被视为已解决，实盘始终不可用。
              </Banner>
            )}
          </div>
        </Card>
      )}

      {/* Step 5: confirm */}
      {step === 4 && selectedStrategy && (
        <Card title="确认运行" description="提交后实验进入队列，由后台任务执行">
          <div className="mb-5 max-w-md">
            <Input
              label="实验名称"
              requiredMark
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="为这次研究起一个可追踪的名称"
            />
          </div>
          <DescriptionList
            columns={2}
            items={[
              {
                label: '策略',
                value: (
                  <span>
                    {selectedStrategy.display_name}
                    <span className="ml-2 font-mono text-xs text-ink-400">{selectedStrategy.strategy_id}</span>
                  </span>
                ),
              },
              {
                label: '训练模式',
                value:
                  trainingMode === 'periodic'
                    ? `周期重训练（${selectedStrategy.retrain_frequency}）`
                    : trainingMode === 'train_once'
                      ? `一次训练（${trainStart} ~ ${trainEnd}）`
                      : '无需训练',
              },
              {
                label: '股票池',
                value:
                  poolPreset === 'custom'
                    ? `自定义（${parsedCustomCodes.length} 只）`
                    : `${pools.find((pool) => pool.id === poolPreset)?.name ?? poolPreset}`,
              },
              {
                label: '股票子集',
                value: selectedPoolCodes.length > 0 ? `已选 ${selectedPoolCodes.length} 只` : '整个股票池',
              },
              {
                label: '行业筛选',
                value:
                  selectedIndustries.length > 0
                    ? `${selectedIndustries.length} 个行业：${selectedIndustries.join('、')}`
                    : '全部行业',
                span: 2,
              },
              { label: '测试区间', value: `${testStart} ~ ${testEnd}`, mono: true },
              { label: '执行模式', value: mode, mono: true },
              {
                label: '数据访问',
                value: dataAccessPolicy === 'cache_only' ? 'PIT 治理数据（不联网）' : '旧策略（已停用）',
              },
              ...(sourceExperiment
                ? [{ label: '继承来源', value: `实验 #${sourceExperiment.id}` }]
                : []),
            ]}
          />
          {!industryGuard.ok && (
            <Banner variant="danger" className="mt-4">
              {industryGuard.reason}
            </Banner>
          )}
        </Card>
      )}

      {/* Submit error + navigation */}
      {submitError && (
        <Banner variant="danger" className="mt-4" title="无法提交实验">
          {submitError}
        </Banner>
      )}
      <div className="mt-6 flex items-center justify-between">
        <Button
          variant="secondary"
          disabled={step === 0}
          onClick={() => setStep((current) => Math.max(0, current - 1))}
        >
          <Icon name="arrowLeft" className="h-4 w-4" />
          上一步
        </Button>
        {step < STEPS.length - 1 ? (
          <Button disabled={!canNext()} onClick={() => setStep((current) => current + 1)}>
            下一步
            <Icon name="arrowRight" className="h-4 w-4" />
          </Button>
        ) : (
          <Button
            onClick={() => void handleSubmit()}
            loading={submitting}
            disabled={!canNext() || !industryGuard.ok || industryBusy}
          >
            <Icon name="play" className="h-4 w-4" />
            提交运行
          </Button>
        )}
      </div>
    </div>
  );
}
