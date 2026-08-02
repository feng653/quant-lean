import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { listExperiments, toggleStar as toggleStarApi } from '../../services/experiments';
import type { ExperimentSortKey, ExperimentSortOrder } from '../../services/experiments';
import { listStrategies } from '../../services/strategies';
import type { Experiment } from '../../types/experiment';
import type { StrategyMetadata } from '../../types/strategy';
import { useAuthStore } from '../../store/authStore';
import {
  buildExperimentListFilters,
  buildExperimentSortSearchParams,
  experimentSortOptions,
  experimentSortOrderOptions,
  filterStrategiesByCategory,
  parseExperimentSortKey,
  parseExperimentSortOrder,
  reconcileStrategySelection,
  strategyCategoryOptions,
} from './experimentListFilters';
import type { StrategyCategoryFilter } from './experimentListFilters';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import EmptyState from '../../components/shared/EmptyState';
import Icon from '../../components/shared/Icon';
import Input from '../../components/shared/Input';
import PageHeader from '../../components/shared/PageHeader';
import Pagination from '../../components/shared/Pagination';
import Select from '../../components/shared/Select';
import StatusTag from '../../components/shared/StatusTag';
import Table from '../../components/shared/Table';
import type { Column } from '../../components/shared/Table';
import { formatSignedPct } from '../../components/shared/chartTheme';
import { formatBackendDateTime } from '../../utils/datetime';

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'pending', label: '等待中' },
  { value: 'running', label: '运行中' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
];

const STATUS_TAG: Record<string, { label: string; variant: 'queued' | 'running' | 'verified' | 'error' | 'neutral' }> = {
  pending: { label: '等待中', variant: 'queued' },
  running: { label: '运行中', variant: 'running' },
  completed: { label: '已完成', variant: 'verified' },
  failed: { label: '失败', variant: 'error' },
  cancelled: { label: '已取消', variant: 'neutral' },
};

const PAGE_LIMIT = 20;

export default function ExperimentListPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const user = useAuthStore((s) => s.user);
  const canCreate = user?.is_admin || user?.permissions.includes('experiments:create');
  const canSweep = user?.is_admin || user?.permissions.includes('experiments:sweep');

  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [strategies, setStrategies] = useState<StrategyMetadata[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [strategyCategory, setStrategyCategory] = useState<StrategyCategoryFilter>('');
  const [strategyId, setStrategyId] = useState('');
  const [status, setStatus] = useState('');
  const [starredOnly, setStarredOnly] = useState(false);
  const [search, setSearch] = useState('');
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const sortBy = parseExperimentSortKey(searchParams.get('sort_by'));
  const sortOrder = parseExperimentSortOrder(searchParams.get('sort_order'));

  const loadStrategies = useCallback(async () => {
    try {
      const result = await listStrategies();
      setStrategies(result);
    } catch {
      // Strategy filter degrades to "all strategies" when the catalog is unavailable.
    }
  }, []);

  const loadExperiments = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listExperiments(
        buildExperimentListFilters({
          strategyCategory,
          strategyId,
          status,
          starredOnly,
          search,
          sortBy,
          sortOrder,
          page,
          limit: PAGE_LIMIT,
        }),
      );
      setExperiments(result.items);
      setTotal(result.total);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '获取实验列表失败');
    } finally {
      setLoading(false);
    }
  }, [
    strategyCategory,
    strategyId,
    status,
    starredOnly,
    search,
    sortBy,
    sortOrder,
    page,
  ]);

  useEffect(() => {
    void loadStrategies();
  }, [loadStrategies]);

  useEffect(() => {
    void loadExperiments();
  }, [loadExperiments]);

  useEffect(() => {
    setStrategyId((current) => reconcileStrategySelection(current, strategyCategory, strategies));
  }, [strategyCategory, strategies]);

  const toggleStar = async (experiment: Experiment) => {
    try {
      await toggleStarApi(experiment.id, !experiment.is_starred);
      setExperiments((current) =>
        current.map((item) =>
          item.id === experiment.id ? { ...item, is_starred: !item.is_starred } : item,
        ),
      );
    } catch {
      // Star toggling is non-critical; keep the list usable.
    }
  };

  const toggleSelect = (id: number) => {
    setSelectedIds((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );
  };

  const updateSort = (
    nextSortBy: ExperimentSortKey,
    nextSortOrder: ExperimentSortOrder,
  ) => {
    setSearchParams(
      (current) =>
        buildExperimentSortSearchParams(
          current,
          nextSortBy,
          nextSortOrder,
        ),
      { replace: true },
    );
    setPage(1);
  };

  const strategyOptions = [
    { value: '', label: '全部策略' },
    ...filterStrategiesByCategory(strategies, strategyCategory).map((strategy) => ({
      value: strategy.strategy_id,
      label: strategy.display_name,
    })),
  ];

  const columns: Column<Experiment>[] = [
    {
      key: 'select',
      header: <span className="sr-only">选择</span>,
      className: 'w-10',
      render: (experiment) => (
        <input
          type="checkbox"
          aria-label={`选择实验 ${experiment.name}`}
          checked={selectedIds.includes(experiment.id)}
          onChange={() => toggleSelect(experiment.id)}
          onClick={(event) => event.stopPropagation()}
          className="h-4 w-4 rounded-sm border-ink-300 text-accent-700 focus:ring-accent-600"
        />
      ),
    },
    {
      key: 'name',
      header: '名称',
      render: (experiment) => (
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            aria-label={experiment.is_starred ? `取消星标 ${experiment.name}` : `星标 ${experiment.name}`}
            aria-pressed={experiment.is_starred}
            onClick={(event) => {
              event.stopPropagation();
              void toggleStar(experiment);
            }}
            className={`rounded p-0.5 transition-colors ${
              experiment.is_starred ? 'text-warn-fg hover:text-warn-strong' : 'text-ink-300 hover:text-ink-500'
            }`}
          >
            <Icon name={experiment.is_starred ? 'starFilled' : 'star'} className="h-4 w-4" />
          </button>
          <div className="min-w-0">
            <p className="truncate font-medium text-ink-800">{experiment.name}</p>
            <p className="tnum text-2xs text-ink-400">#{experiment.id}</p>
          </div>
        </div>
      ),
    },
    {
      key: 'strategy_id',
      header: '策略',
      render: (experiment) => (
        <span className="font-mono text-xs text-ink-600">{experiment.strategy_id}</span>
      ),
    },
    {
      key: 'pool',
      header: '股票池',
      render: (experiment) => (
        <span className="text-xs text-ink-600">
          {experiment.pool_preset ?? '自定义'}
          {experiment.pool_industries.length > 0 && (
            <span className="ml-1 text-ink-400" title={experiment.pool_industries.join('、')}>
              +{experiment.pool_industries.length} 行业
            </span>
          )}
        </span>
      ),
    },
    {
      key: 'window',
      header: '测试区间',
      render: (experiment) => (
        <span className="tnum text-xs text-ink-600">
          {experiment.test_start} ~ {experiment.test_end}
        </span>
      ),
    },
    {
      key: 'status',
      header: '状态',
      render: (experiment) => {
        const spec = STATUS_TAG[experiment.status] ?? { label: experiment.status, variant: 'neutral' as const };
        return <StatusTag variant={spec.variant}>{spec.label}</StatusTag>;
      },
    },
    {
      key: 'sharpe',
      header: 'Sharpe',
      numeric: true,
      render: (experiment) =>
        experiment.status === 'completed' && experiment.sharpe_ratio !== null && experiment.sharpe_ratio !== undefined ? (
          <span className="tnum">{experiment.sharpe_ratio.toFixed(2)}</span>
        ) : (
          <span className="text-xs text-ink-400">待计算</span>
        ),
    },
    {
      key: 'return',
      header: '年化收益',
      numeric: true,
      render: (experiment) =>
        experiment.status === 'completed' && experiment.annual_return !== null && experiment.annual_return !== undefined ? (
          <span className={`tnum ${formatSignedPct(experiment.annual_return).startsWith('+') ? 'text-rise' : 'text-fall'}`}>
            {formatSignedPct(experiment.annual_return)}
          </span>
        ) : (
          <span className="text-xs text-ink-400">待计算</span>
        ),
    },
    {
      key: 'created_at',
      header: '创建时间',
      render: (experiment) => (
        <span className="tnum text-xs text-ink-500">
          {formatBackendDateTime(experiment.created_at)}
        </span>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title="实验中心"
        description="回测实验的创建、跟踪与对比。历史实验统一视为未验证研究证据。"
        breadcrumb={[{ label: '研究' }, { label: '实验中心' }]}
        actions={
          <>
            <Button variant="ghost" size="sm" onClick={() => navigate('/experiment/correlation')}>
              <Icon name="chart" className="h-4 w-4" />
              策略相关性
            </Button>
            <Button variant="ghost" size="sm" onClick={() => navigate('/experiment/parameters')}>
              <Icon name="presets" className="h-4 w-4" />
              参数管理
            </Button>
            {canSweep && (
              <Button variant="secondary" size="sm" onClick={() => navigate('/experiment/sweep')}>
                <Icon name="compare" className="h-4 w-4" />
                参数扫描
              </Button>
            )}
            {canCreate && (
              <Button size="sm" onClick={() => navigate('/experiment/new')}>
                <Icon name="plus" className="h-4 w-4" />
                新建实验
              </Button>
            )}
          </>
        }
      />

      {error && (
        <Banner
          variant="danger"
          className="mb-4"
          action={
            <Button variant="secondary" size="sm" onClick={() => void loadExperiments()}>
              重试
            </Button>
          }
        >
          {error}
        </Banner>
      )}

      {/* Filters */}
      <Card padding="md" className="mb-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Select
            label="策略分类"
            aria-label="策略分类筛选"
            value={strategyCategory}
            onChange={(event) => {
              setStrategyCategory(event.target.value as StrategyCategoryFilter);
              setPage(1);
            }}
            options={strategyCategoryOptions.map((option) => ({
              value: option.value,
              label: option.label,
            }))}
          />
          <Select
            label="策略"
            aria-label="策略筛选"
            value={strategyId}
            onChange={(event) => {
              setStrategyId(event.target.value);
              setPage(1);
            }}
            options={strategyOptions}
          />
          <Select
            label="状态"
            aria-label="状态筛选"
            value={status}
            onChange={(event) => {
              setStatus(event.target.value);
              setPage(1);
            }}
            options={STATUS_OPTIONS}
          />
          <Input
            label="搜索"
            aria-label="按名称搜索实验"
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
              setPage(1);
            }}
            placeholder="搜索实验名称..."
          />
          <div className="flex items-end pb-1">
            <label className="flex min-h-[38px] cursor-pointer items-center gap-2 text-sm text-ink-700">
              <input
                type="checkbox"
                checked={starredOnly}
                onChange={(event) => {
                  setStarredOnly(event.target.checked);
                  setPage(1);
                }}
                className="h-4 w-4 rounded-sm border-ink-300 text-accent-700 focus:ring-accent-600"
              />
              仅星标
            </label>
          </div>
        </div>
        <div className="mt-4 border-t border-ink-100 pt-3">
          <p className="mb-2 text-xs font-medium text-ink-500">
            全部实验排序（分页前）
          </p>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:max-w-xl">
            <Select
              label="排序字段"
              aria-label="实验排序字段"
              value={sortBy}
              onChange={(event) =>
                updateSort(
                  event.target.value as ExperimentSortKey,
                  sortOrder,
                )
              }
              options={experimentSortOptions}
            />
            <Select
              label="排序方向"
              aria-label="实验排序方向"
              value={sortOrder}
              onChange={(event) =>
                updateSort(
                  sortBy,
                  event.target.value as ExperimentSortOrder,
                )
              }
              options={experimentSortOrderOptions}
            />
          </div>
        </div>
      </Card>

      {/* Compare action bar */}
      {selectedIds.length > 0 && (
        <div className="mb-4 flex items-center justify-between rounded-md border border-accent-200 bg-accent-50 px-4 py-2.5">
          <p className="text-sm text-accent-900 tnum">已选择 {selectedIds.length} 个实验</p>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => setSelectedIds([])}>
              清除选择
            </Button>
            <Button
              size="sm"
              disabled={selectedIds.length < 2}
              onClick={() => navigate('/experiment/compare', { state: { ids: selectedIds } })}
            >
              <Icon name="compare" className="h-4 w-4" />
              对比所选（{selectedIds.length}）
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={selectedIds.length < 2 || selectedIds.length > 20}
              onClick={() => navigate('/experiment/correlation', { state: { ids: selectedIds } })}
            >
              <Icon name="chart" className="h-4 w-4" />
              相关性分析
            </Button>
          </div>
        </div>
      )}

      {/* List */}
      <Card padding="none">
        {loading ? (
          <div className="p-12 text-center">
            <span className="inline-block"><Icon name="clock" className="h-6 w-6 animate-pulse text-ink-300" /></span>
            <p className="mt-2 text-sm text-ink-400">加载实验列表...</p>
          </div>
        ) : experiments.length === 0 ? (
          <EmptyState
            icon="experiment"
            title="还没有实验"
            description="点击新建开始你的第一个回测。"
            action={
              canCreate ? (
                <Button size="sm" onClick={() => navigate('/experiment/new')}>
                  <Icon name="plus" className="h-4 w-4" />
                  新建实验
                </Button>
              ) : undefined
            }
          />
        ) : (
          <Table
            columns={columns}
            data={experiments}
            keyField="id"
            onRowClick={(experiment) => navigate(`/experiment/${experiment.id}`)}
            caption="实验列表"
            minWidth="1080px"
          />
        )}
        <div className="border-t border-ink-100 px-4 py-3">
          <Pagination page={page} total={total} limit={PAGE_LIMIT} onChange={setPage} />
        </div>
      </Card>
    </div>
  );
}
