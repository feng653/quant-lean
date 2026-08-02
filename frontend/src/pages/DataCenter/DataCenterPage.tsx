import { useCallback, useEffect, useState } from 'react';
import {
  getDataUpdateStatus,
  getResearchDataConflicts,
  getResearchDataSources,
  invalidatePoolCache,
  listPools,
  refreshIndustryCatalog,
  triggerDataUpdate,
  triggerPitGovernanceRefresh,
  triggerResearchDataRefresh,
} from '../../services/data';
import type {
  DataUpdateStatus,
  PoolCacheInfo,
  PoolInfo,
  ResearchDataConflictReport,
  ResearchDataSource,
} from '../../services/data';
import { industryClassificationLabel } from '../../services/industryCatalog';
import { useIndustryCatalog } from '../../components/data/useIndustryCatalog';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import Icon from '../../components/shared/Icon';
import PageHeader from '../../components/shared/PageHeader';
import ProgressBar from '../../components/shared/ProgressBar';
import Skeleton from '../../components/shared/Skeleton';
import StatusTag from '../../components/shared/StatusTag';
import Table from '../../components/shared/Table';
import type { Column } from '../../components/shared/Table';

interface PoolRow extends PoolCacheInfo {
  expected_count: number | null;
  pool_name: string;
}

export default function DataCenterPage() {
  const [pools, setPools] = useState<PoolInfo[]>([]);
  const [status, setStatus] = useState<DataUpdateStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [runningPool, setRunningPool] = useState<string | null>(null);
  const [researchSources, setResearchSources] = useState<ResearchDataSource[]>([]);
  const [researchConflicts, setResearchConflicts] = useState<ResearchDataConflictReport | null>(null);
  const [researchSourceId, setResearchSourceId] = useState<ResearchDataSource['source_id']>('tushare');
  const [researchFromMonth, setResearchFromMonth] = useState('2016-01');
  const [researchToMonth, setResearchToMonth] = useState('');

  const load = useCallback(async () => {
    try {
      const [poolList, updateStatus, sourceReport, conflictReport] = await Promise.all([
        listPools(),
        getDataUpdateStatus(),
        getResearchDataSources(),
        getResearchDataConflicts(),
      ]);
      setPools(poolList);
      setStatus(updateStatus);
      setResearchSources(sourceReport.sources);
      setResearchConflicts(conflictReport);
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '加载数据状态失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 10_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const update = async (poolId?: string) => {
    setRunningPool(poolId ?? '*');
    setMessage(null);
    try {
      const result = await triggerDataUpdate(poolId);
      setMessage(result.message);
      await load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '提交更新失败');
    } finally {
      setRunningPool(null);
    }
  };

  const refreshResearchData = async () => {
    setRunningPool('*research');
    setMessage(null);
    try {
      const result = await triggerResearchDataRefresh({
        source_id: researchSourceId,
        from_month: researchFromMonth,
        ...(researchToMonth ? { to_month: researchToMonth } : {}),
        max_calls: 16,
      });
      setMessage(result.message);
      await load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '提交研究数据刷新失败');
    } finally {
      setRunningPool(null);
    }
  };

  const refreshGovernance = async (poolId?: string) => {
    setRunningPool(poolId ?? '*governance');
    setMessage(null);
    try {
      const result = await triggerPitGovernanceRefresh(poolId);
      setMessage(result.message);
      await load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '提交 PIT 治理证据刷新失败');
    } finally {
      setRunningPool(null);
    }
  };

  const invalidate = async (poolId: string) => {
    if (!window.confirm(`确认失效 ${poolId} 的本地行情缓存？`)) return;
    setRunningPool(poolId);
    setMessage(null);
    try {
      await invalidatePoolCache(poolId);
      setMessage(`缓存 ${poolId} 已失效`);
      await load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '缓存失效失败');
    } finally {
      setRunningPool(null);
    }
  };

  const poolNameById = new Map(pools.map((pool) => [pool.id, pool.name]));
  const expectedById = new Map(pools.map((pool) => [pool.id, pool.declared_count ?? null]));
  const researchPoolById = new Map(
    (status?.research_pools ?? []).map((pool) => [pool.pool_id, pool]),
  );
  const rows: PoolRow[] = (status?.pools_cache ?? []).map((cache) => ({
    ...cache,
    expected_count: expectedById.get(cache.pool_id) ?? null,
    pool_name: poolNameById.get(cache.pool_id) ?? cache.pool_id,
  }));

  const broker = status?.broker_status;
  const brokerProgress = broker?.progress;
  const selectedResearchSource = researchSources.find(
    (source) => source.source_id === researchSourceId,
  );
  const visibleResearchSources = selectedResearchSource
    ? [selectedResearchSource]
    : researchSources;
  const visibleResearchComparisons = (researchConflicts?.comparisons ?? []).filter(
    (item) => item.left_source === researchSourceId || item.right_source === researchSourceId,
  );
  const researchCollection = status?.research_refresh_status?.result?.collection;
  const researchCollectionPercent = researchCollection?.planned_tasks
    ? (researchCollection.completed_tasks / researchCollection.planned_tasks) * 100
    : 0;
  const researchCollectionFailures = Object.entries(researchCollection?.failures ?? {});
  const researchCollectionOptionalFailures = researchCollection?.optional_failures ?? [];

  const columns: Column<PoolRow>[] = [
    {
      key: 'pool',
      header: '股票池',
      render: (row) => (
        <div>
          <p className="font-medium text-ink-800">{row.pool_name}</p>
          <p className="font-mono text-2xs text-ink-400">{row.pool_id}</p>
        </div>
      ),
    },
    {
      key: 'status',
      header: '缓存状态',
      render: (row) =>
        row.exists ? (
          <StatusTag variant="ok">可用</StatusTag>
        ) : researchPoolById.get(row.pool_id)?.available ? (
          <StatusTag variant="warning">
            {status?.research_data_contract?.market?.available
              ? '研究行情可用'
              : '研究股票池可用，行情待导入'}
          </StatusTag>
        ) : (
          <StatusTag variant="neutral">未缓存</StatusTag>
        ),
    },
    {
      key: 'range',
      header: '日期覆盖',
      render: (row) => (
        <span className="tnum text-ink-600">
          {row.date_start && row.date_end
            ? `${row.date_start} → ${row.date_end}`
            : researchPoolById.get(row.pool_id)?.resolved_month
              ? `股票池快照截至 ${researchPoolById.get(row.pool_id)?.resolved_month}`
              : '-'}
        </span>
      ),
    },
    {
      key: 'n_dates',
      header: '交易日',
      numeric: true,
      render: (row) => (row.exists ? row.n_dates.toLocaleString('zh-CN') : '-'),
    },
    {
      key: 'n_stocks',
      header: '缓存证券 / PIT 声明',
      numeric: true,
      render: (row) => {
        if (!row.exists) return '-';
        const expected = row.expected_count;
        const mismatch = expected !== null && row.n_stocks !== expected;
        return (
          <span className={mismatch ? 'text-warn-strong' : ''}>
            {row.n_stocks.toLocaleString('zh-CN')}
            {expected !== null && ` / ${expected.toLocaleString('zh-CN')}`}
            {mismatch && (
              <span className="ml-1 inline-flex items-center" title="本地证券数与池声明数量不一致，可能缺失成分">
                <Icon name="warning" className="h-3.5 w-3.5" aria-hidden />
              </span>
            )}
          </span>
        );
      },
    },
    {
      key: 'size',
      header: '文件',
      numeric: true,
      render: (row) => (row.exists ? `${row.file_size_mb.toFixed(2)} MB` : '-'),
    },
    {
      key: 'updated',
      header: '最后更新',
      render: (row) => (
        <span className="tnum text-ink-500">
          {row.last_updated ? new Date(row.last_updated).toLocaleString('zh-CN', { hour12: false }) : '-'}
        </span>
      ),
    },
    {
      key: 'actions',
      header: '操作',
      className: 'text-right',
      render: (row) => (
        <div className="flex justify-end gap-2">
          <Button
            variant="secondary"
            size="sm"
            loading={runningPool === row.pool_id}
            disabled={runningPool !== null}
            onClick={() => void update(row.pool_id)}
          >
            更新 PIT 行情
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={!row.exists || runningPool !== null}
            onClick={() => void invalidate(row.pool_id)}
          >
            失效
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title="数据中心"
        description="优先维护可用于研究与模拟的数据版本；已知缺口和跨源差异显示为告警，实盘仍保持硬锁。"
        breadcrumb={[{ label: '研究' }, { label: '数据中心' }]}
        actions={
          <div className="flex gap-2">
            <Button
              onClick={() => void refreshResearchData()}
              loading={runningPool === '*research'}
              disabled={runningPool !== null || !selectedResearchSource?.refreshable}
            >
              <Icon name="download" className="h-4 w-4" />
              更新研究数据
            </Button>
          </div>
        }
      />

      <Banner variant="info" className="mb-5" title="股票池成分使用已激活 PIT 时间线">
        股票池名称与数量来自本地已激活 PIT 观察值，不会回退到当前成分或联网抓取。周末页面可只读显示最近周五的观察值，并会在请求结果中标明解析日期与陈旧天数；工作日缺口保持失败关闭。下表的行情缓存仍须通过实验前 PIT/双价格账本门禁。
      </Banner>

      <Card
        className="mb-5"
        title="研究数据源"
        description="研究和模拟允许使用带风险告警的数据；数据源版本、能力和冲突均可追溯"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <label className="sr-only" htmlFor="research-data-source">研究数据源</label>
            <select
              id="research-data-source"
              value={researchSourceId}
              onChange={(event) => setResearchSourceId(event.target.value as ResearchDataSource['source_id'])}
              className="h-8 rounded border border-ink-200 bg-surface px-2 text-sm text-ink-700"
            >
              {researchSources.map((source) => (
                <option key={source.source_id} value={source.source_id}>
                  {source.display_name}{source.refreshable ? '' : '（只读/复核）'}
                </option>
              ))}
            </select>
            <label className="sr-only" htmlFor="research-from-month">开始月份</label>
            <input
              id="research-from-month"
              type="month"
              value={researchFromMonth}
              onChange={(event) => setResearchFromMonth(event.target.value)}
              className="h-8 rounded border border-ink-200 bg-surface px-2 text-sm text-ink-700"
            />
            {!researchToMonth && <span className="text-xs text-ink-500">自动探测最新完整月</span>}
            <label className="sr-only" htmlFor="research-to-month">结束月份</label>
            <input
              id="research-to-month"
              type="month"
              value={researchToMonth}
              onChange={(event) => setResearchToMonth(event.target.value)}
              className="h-8 rounded border border-ink-200 bg-surface px-2 text-sm text-ink-700"
            />
            <Button
              size="sm"
              onClick={() => void refreshResearchData()}
              loading={runningPool === '*research'}
              disabled={
                runningPool !== null
                || !selectedResearchSource?.refreshable
                || Boolean(researchToMonth && researchFromMonth > researchToMonth)
              }
            >
              拉取并生成研究版本
            </Button>
          </div>
        }
      >
        <div className="overflow-x-auto">
          <table className="w-full text-sm" style={{ minWidth: 860 }}>
            <thead><tr className="border-b border-ink-200 text-left text-xs text-ink-500">
              <th className="px-3 py-2">数据源</th><th className="px-3 py-2">状态</th>
              <th className="px-3 py-2">能力</th><th className="px-3 py-2">最后观察</th>
              <th className="px-3 py-2 text-right">研究记录</th><th className="px-3 py-2">告警</th>
            </tr></thead>
            <tbody className="divide-y divide-ink-100">
              {visibleResearchSources.map((source) => (
                <tr key={source.source_id}>
                  <td className="px-3 py-2 font-medium">{source.display_name}</td>
                  <td className="px-3 py-2"><StatusTag variant={source.available ? 'warning' : 'neutral'}>
                    {source.available ? '研究可用' : source.configured ? '尚无版本' : '未配置'}
                  </StatusTag></td>
                  <td className="px-3 py-2 text-xs text-ink-500">
                    {source.datasets.map((dataset) => (
                      <span key={dataset.dataset} className="mr-2 inline-block">
                        {dataset.dataset}：{dataset.status}
                        {dataset.record_count > 0 && ` (${dataset.record_count.toLocaleString('zh-CN')})`}
                      </span>
                    ))}
                  </td>
                  <td className="px-3 py-2 tnum">{source.last_observation ?? '-'}</td>
                  <td className="px-3 py-2 text-right tnum">{source.row_count.toLocaleString('zh-CN')}</td>
                  <td className="px-3 py-2 text-xs text-warn-strong">{source.warnings.join('；') || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {status?.research_refresh_status?.status && status.research_refresh_status.status !== 'unknown' && (
          <div className="mt-3 space-y-2 text-xs text-ink-500">
            <p>最近研究数据刷新：{status.research_refresh_status.status}；此状态不会授予实盘资格。</p>
            {researchCollection && (
              <>
                <ProgressBar
                  value={researchCollectionPercent}
                  label="研究数据实际采集进度"
                  variant={researchCollectionFailures.length ? 'danger' : 'accent'}
                />
                <p className="tnum">
                  实际任务：已完成 {researchCollection.completed_tasks.toLocaleString('zh-CN')}
                  {' / '}计划 {researchCollection.planned_tasks.toLocaleString('zh-CN')}
                  {'；'}待处理 {researchCollection.pending_tasks.toLocaleString('zh-CN')}
                  {'；'}已物化交易日 {(researchCollection.reconciled_session_count ?? 0).toLocaleString('zh-CN')}
                  {researchCollection.calls_this_invocation !== undefined
                    ? `；本批调用 ${researchCollection.calls_this_invocation.toLocaleString('zh-CN')}`
                    : ''}
                </p>
                {researchCollectionFailures.length > 0 && (
                  <div className="rounded border border-danger-border bg-danger-muted p-2 text-danger-fg">
                    <p>失败任务 {researchCollectionFailures.length} 个：</p>
                    {researchCollectionFailures.slice(0, 3).map(([taskId, failure]) => (
                      <p key={taskId} className="break-all">
                        {failure.task?.dataset ?? taskId}：{failure.diagnostic?.code ?? 'unknown_error'}
                        {failure.diagnostic?.retryable ? '（可重试）' : ''}
                      </p>
                    ))}
                  </div>
                )}
                {researchCollectionOptionalFailures.length > 0 && (
                  <p className="text-warn-strong">
                    可选数据缺口 {researchCollectionOptionalFailures.length} 个：
                    {researchCollectionOptionalFailures.slice(0, 3).map((failure) => (
                      `${failure.task?.dataset ?? 'optional'}=${failure.diagnostic?.code ?? 'unavailable'}`
                    )).join('；')}
                  </p>
                )}
              </>
            )}
          </div>
        )}
      </Card>

      <Card className="mb-5" title="跨数据源具体冲突" description="逐股票池列出双方数量和仅单侧存在的证券样例">
        {!researchConflicts || visibleResearchComparisons.length === 0 ? (
          <p className="text-sm text-ink-500">至少两个来源在同一日期有数据后才可比较。</p>
        ) : (
          <div className="space-y-2">
            {visibleResearchComparisons.map((item) => (
              <div key={`${item.pool_id}-${item.as_of}`} className="rounded border border-ink-100 p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusTag variant={item.status === 'match' ? 'ok' : 'warning'}>{item.status}</StatusTag>
                  <span className="font-medium">{item.pool_id}</span>
                  <span className="tnum text-ink-500">{item.as_of}</span>
                  <span className="tnum">{item.left_source} {item.left_count} / {item.right_source} {item.right_count}</span>
                </div>
                {(item.only_left_sample?.length || item.only_right_sample?.length) ? (
                  <p className="mt-2 break-all text-xs text-ink-500">
                    仅 {item.left_source}：{item.only_left_sample?.join('、') || '-'}；仅 {item.right_source}：{item.only_right_sample?.join('、') || '-'}
                  </p>
                ) : null}
                <p className="mt-2 text-xs text-ink-500">
                  独立性：{item.independent ? '独立来源' : item.lineage_status ?? '未证明独立'}
                </p>
                {item.weight_conflict_sample?.map((conflict) => (
                  <p key={`${item.pool_id}-${conflict.security_code}`} className="mt-1 text-xs text-warn-strong">
                    {conflict.security_code} 权重 {conflict.left_value} / {conflict.right_value}；
                    差值 {conflict.absolute_delta}，容差 {conflict.tolerance}
                  </p>
                ))}
              </div>
            ))}
            {researchConflicts.uncompared?.filter(
              (item) => item.left_source === researchSourceId || item.right_source === researchSourceId,
            ).map((item, index) => (
              <p key={`${item.pool_id ?? 'all'}-${item.reason}-${index}`} className="text-xs text-ink-500">
                未比较：{item.reason}{item.fields?.length ? `（${item.fields.join('、')}）` : ''}
              </p>
            ))}
          </div>
        )}
      </Card>

      {status?.market_data_update_contract?.available === false && (
        <Banner variant="warning" className="mb-5" title="实盘级数据维护已后移">
          当前研究数据可以带告警用于实验和模拟；生产 raw/研究复权双价格账本仍未获准，
          因此不会授予实盘资格。下方旧运行时缓存仅用于诊断，不代表研究数据源不可用。
          <span className="ml-2 inline-flex gap-2">
            <Button variant="secondary" size="sm" onClick={() => void refreshGovernance()} disabled={runningPool !== null}>
              刷新治理证据
            </Button>
            <Button variant="ghost" size="sm" onClick={() => void update()} disabled={runningPool !== null}>
              检查实盘账本门禁
            </Button>
          </span>
        </Banner>
      )}

      {error && (
        <Banner variant="danger" className="mb-4" title="数据状态异常">
          {error}
        </Banner>
      )}
      {message && (
        <Banner variant="ok" className="mb-4">
          {message}
        </Banner>
      )}
      {broker?.error && (
        <Banner variant="danger" className="mb-4" title="最近任务错误">
          {broker.error}
        </Banner>
      )}
      {status?.governance_refresh_status?.error && (
        <Banner variant="danger" className="mb-4" title="最近 PIT 治理刷新错误">
          {status.governance_refresh_status.error}
        </Banner>
      )}

      {/* Summary */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div className="rounded-md border border-ink-200 bg-surface p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-ink-400">可用股票池</p>
          <p className="tnum mt-1.5 text-2xl font-semibold">{loading ? '-' : pools.length}</p>
        </div>
        <div className="rounded-md border border-ink-200 bg-surface p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-ink-400">已缓存文件</p>
          <p className="tnum mt-1.5 text-2xl font-semibold">
            {loading ? '-' : rows.filter((row) => row.exists).length}
          </p>
        </div>
        <div className="rounded-md border border-ink-200 bg-surface p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-ink-400">最近更新任务</p>
          <p className="mt-1.5 text-lg font-semibold">{broker?.status ?? '暂无任务'}</p>
          {brokerProgress !== null && brokerProgress !== undefined && (
            <ProgressBar value={brokerProgress * 100} label="数据更新进度" className="mt-2" />
          )}
        </div>
      </div>

      {status?.governance_refresh_status?.status && status.governance_refresh_status.status !== 'unknown' && (
        <p className="mt-3 text-sm text-ink-500">
          最近 PIT 治理证据刷新：{status.governance_refresh_status.status}（不代表行情缓存或双价格账本已更新）
        </p>
      )}

      {/* Pool cache table */}
      <Card className="mt-5" title="旧运行时行情缓存诊断" description="此表为空不会否定上方研究股票池；实验行情导入由后续研究价格版本处理" padding="none">
        <Table
          columns={columns}
          data={rows}
          keyField="pool_id"
          loading={loading}
          emptyMessage="暂无缓存状态数据"
          caption="股票池缓存状态"
          minWidth="960px"
        />
      </Card>

      {/* Industry catalog */}
      <IndustryCatalogCard pools={pools} />
    </div>
  );
}

function IndustryCatalogCard({ pools }: { pools: PoolInfo[] }) {
  const [poolId, setPoolId] = useState('csi300');
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const { catalog, loading, error, retry } = useIndustryCatalog(undefined, poolId);

  const refresh = async () => {
    setRefreshing(true);
    setRefreshError(null);
    try {
      await refreshIndustryCatalog(poolId);
      retry();
    } catch (err: unknown) {
      setRefreshError(err instanceof Error ? err.message : '行业分类更新失败');
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <Card
      className="mt-5"
      title="行业目录"
      description="按股票池校验巨潮 008001 行业映射；外部刷新是显式的数据更新操作"
      actions={
        <div className="flex items-center gap-2">
          <label className="sr-only" htmlFor="industry-refresh-pool">行业股票池</label>
          <select
            id="industry-refresh-pool"
            value={poolId}
            onChange={(event) => setPoolId(event.target.value)}
            className="h-8 rounded border border-ink-200 bg-surface px-2 text-sm text-ink-700"
          >
            {pools.map((pool) => (
              <option key={pool.id} value={pool.id}>{pool.name}</option>
            ))}
          </select>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void refresh()}
            loading={refreshing}
            disabled={!poolId}
          >
            <Icon name="refresh" className="h-4 w-4" />
            更新行业分类
          </Button>
        </div>
      }
    >
      {loading && <Skeleton lines={3} className="h-6 w-full" />}

      {!loading && (error || refreshError) && (
        <Banner variant="danger" title="行业目录加载失败">
          {refreshError ?? error}
        </Banner>
      )}

      {!loading && !error && !refreshError && catalog?.status === 'unavailable' && (
        <Banner variant="warning" title="行业目录当前不可用于筛选">
          {catalog.reason} 如需联网更新，请点击“更新行业分类”（需要数据更新权限）。
        </Banner>
      )}

      {!loading && !error && !refreshError && catalog?.status === 'ready' && (
        <div>
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <StatusTag variant="info">
              {industryClassificationLabel(catalog.meta.classification)}
            </StatusTag>
            {catalog.meta.source && (
              <StatusTag variant="neutral">来源：{catalog.meta.source}</StatusTag>
            )}
            <StatusTag variant="neutral">目录条目 {catalog.entries.length} 个</StatusTag>
            {catalog.invalidCount > 0 && (
              <StatusTag variant="warning">
                已排除 {catalog.invalidCount} 条名称不可读条目
              </StatusTag>
            )}
          </div>

          {(catalog.meta.mappedStocks !== null || catalog.meta.mapCoverage !== null) && (
            <p className="mb-3 text-xs leading-5 text-ink-500 tnum">
              行业映射覆盖 {catalog.meta.mappedStocks?.toLocaleString('zh-CN') ?? '-'} 只股票
              {catalog.meta.mapCoverage !== null &&
                `，覆盖率 ${(catalog.meta.mapCoverage * 100).toFixed(1)}%`}
              {catalog.meta.minimumCoverage !== null &&
                `（准入下限 ${(catalog.meta.minimumCoverage * 100).toFixed(0)}%）`}
            </p>
          )}

          <div className="overflow-x-auto rounded border border-ink-100 scrollbar-thin">
            <table className="w-full text-sm" style={{ minWidth: 420 }}>
              <caption className="sr-only">行业目录预览（前 20 条）</caption>
              <thead>
                <tr className="border-b border-ink-200 bg-ink-50">
                  <th scope="col" className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-ink-500">
                    行业名称
                  </th>
                  <th scope="col" className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-ink-500">
                    板块代码
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-ink-100">
                {catalog.entries.slice(0, 20).map((entry) => (
                  <tr key={entry.code}>
                    <td className="px-3 py-2 text-ink-700">{entry.name}</td>
                    <td className="px-3 py-2 font-mono text-xs text-ink-400">{entry.code}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {catalog.entries.length > 20 && (
            <p className="mt-2 text-xs text-ink-400">
              仅预览前 20 条；当前覆盖率仅对应所选股票池。
            </p>
          )}
        </div>
      )}
    </Card>
  );
}
