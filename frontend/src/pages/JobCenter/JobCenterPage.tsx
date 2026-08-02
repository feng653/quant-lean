import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import {
  cancelJob,
  getJob,
  getJobObservability,
  getJobSummary,
  listJobs,
  retryJob,
} from '../../services/jobs';
import { diagnoseError } from '../../services/ai';
import { useJobEvents } from '../../hooks/useWebSocket';
import { useAuthStore } from '../../store/authStore';
import type { Job, JobObservability, JobStatus, JobSummary } from '../../types/job';
import {
  formatJobDate,
  formatJobDuration,
  formatSchedulerReasons,
  getJobResourcePath,
  jobStatusLabel,
  jobTypeLabel,
} from '../../utils/jobs';
import { AiDiagnosisCard } from '../../components/ai';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import DescriptionList from '../../components/shared/DescriptionList';
import EmptyState from '../../components/shared/EmptyState';
import Icon from '../../components/shared/Icon';
import PageHeader from '../../components/shared/PageHeader';
import Pagination from '../../components/shared/Pagination';
import ProgressBar from '../../components/shared/ProgressBar';
import Select from '../../components/shared/Select';
import StatusTag from '../../components/shared/StatusTag';
import type { StatusVariant } from '../../components/shared/StatusTag';
import Table from '../../components/shared/Table';
import type { Column } from '../../components/shared/Table';
import OperationsPanel from './OperationsPanel';

const STATUS_VARIANT: Record<JobStatus, StatusVariant> = {
  pending: 'queued',
  running: 'running',
  cancel_requested: 'warning',
  completed: 'verified',
  failed: 'error',
  cancelled: 'neutral',
};

const PAGE_SIZE = 20;

export default function JobCenterPage() {
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const canUseAi = Boolean(user?.is_admin || user?.permissions.includes('ai:use'));

  const [jobs, setJobs] = useState<Job[]>([]);
  const [summary, setSummary] = useState<JobSummary | null>(null);
  const [observability, setObservability] = useState<JobObservability | null>(null);
  const lastObservabilityLoad = useRef(0);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState('');
  const [typeFilter, setTypeFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedJob, setSelectedJob] = useState<Job | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const [jobList, jobSummary] = await Promise.all([
        listJobs({
          status: (statusFilter || undefined) as JobStatus | undefined,
          job_type: typeFilter || undefined,
          page,
          page_size: PAGE_SIZE,
        }),
        getJobSummary(),
      ]);
      setJobs(jobList.items);
      setTotal(jobList.total);
      setSummary(jobSummary);
      if (
        user?.is_admin
        && (!quiet || Date.now() - lastObservabilityLoad.current >= 30_000)
      ) {
        lastObservabilityLoad.current = Date.now();
        try {
          setObservability(await getJobObservability(24));
        } catch {
          // Core task controls remain usable when optional telemetry is
          // temporarily unavailable during SQLite contention.
          setObservability(null);
        }
      }
      setError(null);
    } catch (err: unknown) {
      if (!quiet) setError(err instanceof Error ? err.message : '获取任务列表失败');
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [statusFilter, typeFilter, page, user?.is_admin]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const timer = window.setInterval(() => void load(true), 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  const onJobEvent = useCallback(() => {
    void load(true);
  }, [load]);
  useJobEvents(onJobEvent);

  const openJob = async (job: Job) => {
    try {
      setSelectedJob(await getJob(job.job_uuid));
      setActionError(null);
    } catch {
      setSelectedJob(job);
    }
  };

  const handleCancel = async (job: Job) => {
    const label = job.display_name || jobTypeLabel[job.job_type] || job.job_type;
    if (!window.confirm(`确定取消“${label}”吗？`)) return;
    try {
      await cancelJob(job.job_uuid);
      await load(true);
      if (selectedJob?.job_uuid === job.job_uuid) {
        setSelectedJob(await getJob(job.job_uuid));
      }
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : '取消任务失败');
    }
  };

  const handleRetry = async (job: Job) => {
    try {
      const newJobId = await retryJob(job.job_uuid);
      await load(true);
      setSelectedJob(await getJob(newJobId));
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : '重试任务失败');
    }
  };

  const columns: Column<Job>[] = [
    {
      key: 'name',
      header: '任务',
      render: (job) => (
        <div className="min-w-0">
          <p className="truncate font-medium text-ink-800">
            {job.display_name || jobTypeLabel[job.job_type] || job.job_type}
          </p>
          <p className="font-mono text-2xs text-ink-400">
            {job.job_uuid.slice(0, 12)}
            {job.attempt > 1 && ` · 第 ${job.attempt} 次`}
          </p>
        </div>
      ),
    },
    {
      key: 'type',
      header: '类型',
      render: (job) => <span className="text-xs text-ink-600">{jobTypeLabel[job.job_type] || job.job_type}</span>,
    },
    {
      key: 'status',
      header: '状态',
      render: (job) => (
        <StatusTag variant={STATUS_VARIANT[job.status] ?? 'neutral'}>
          {jobStatusLabel[job.status]}
        </StatusTag>
      ),
    },
    {
      key: 'progress',
      header: '进度',
      className: 'min-w-[180px]',
      render: (job) => (
        <div>
          {job.status === 'pending' && job.queue_position ? (
            <div>
              <span className="text-xs text-ink-500">队列第 {job.queue_position} 位</span>
              {job.queue_reason && (
                <p className="mt-1 max-w-[220px] truncate text-2xs text-ink-400">
                  {formatSchedulerReasons([job.queue_reason])}
                </p>
              )}
            </div>
          ) : (
            <>
              <ProgressBar
                value={(job.progress || 0) * 100}
                label={`任务进度 ${jobStatusLabel[job.status]}`}
                variant={job.status === 'failed' ? 'danger' : 'accent'}
              />
              <p className="mt-1 max-w-[220px] truncate text-xs text-ink-400">
                {job.progress_message || job.queue_reason || '-'}
              </p>
            </>
          )}
        </div>
      ),
    },
    {
      key: 'created_at',
      header: '提交时间',
      render: (job) => <span className="tnum text-xs text-ink-500">{formatJobDate(job.created_at)}</span>,
    },
    {
      key: 'duration',
      header: '耗时',
      render: (job) => <span className="tnum text-xs text-ink-500">{formatJobDuration(job)}</span>,
    },
    {
      key: 'actions',
      header: '操作',
      className: 'text-right',
      render: (job) => (
        <div className="flex justify-end gap-1.5" onClick={(event) => event.stopPropagation()}>
          {['pending', 'running', 'cancel_requested'].includes(job.status) && (
            <Button
              variant="ghost"
              size="sm"
              disabled={job.status === 'cancel_requested'}
              onClick={() => void handleCancel(job)}
            >
              取消
            </Button>
          )}
          {['failed', 'cancelled'].includes(job.status) && (
            <Button variant="ghost" size="sm" onClick={() => void handleRetry(job)}>
              重试
            </Button>
          )}
        </div>
      ),
    },
  ];

  const statusOptions = [
    { value: '', label: '全部状态' },
    ...Object.entries(jobStatusLabel).map(([value, label]) => ({ value, label })),
  ];
  const typeOptions = [
    { value: '', label: '全部类型' },
    ...Object.entries(jobTypeLabel).map(([value, label]) => ({ value, label })),
  ];

  return (
    <div>
      <PageHeader
        title="任务中心"
        description="查看后端队列、执行进度和失败原因。取消与重试会重新校验当前权限。"
        breadcrumb={[{ label: '执行' }, { label: '任务中心' }]}
        actions={
          <Button variant="secondary" size="sm" onClick={() => void load()}>
            <Icon name="refresh" className="h-4 w-4" />
            刷新
          </Button>
        }
      />

      {/* Summary */}
      <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-5">
        <div
          className="rounded-md border border-ink-200 bg-surface p-3.5"
          title={formatSchedulerReasons(summary?.worker.reasons)}
        >
          <p className="text-xs text-ink-400">Worker</p>
          <p className="mt-1 flex items-center gap-1.5 text-lg font-semibold">
            <span className={`h-2 w-2 rounded-full ${summary?.worker.online ? 'bg-ok-fg' : 'bg-danger-fg'}`} aria-hidden />
            {summary?.worker.online ? '在线' : '离线'}
          </p>
          <p className="tnum mt-0.5 text-2xs text-ink-400">
            运行槽 {summary?.worker.running_slots ?? 0}/{summary?.worker.capacity ?? '-'}
            {summary?.worker.configured_max ? ` · 上限 ${summary.worker.configured_max}` : ''}
          </p>
          <p className="mt-1 truncate text-2xs text-ink-400">
            {formatSchedulerReasons(summary?.worker.reasons)}
          </p>
        </div>
        {([
          { label: '运行中', value: summary?.counts.running, tone: 'text-info-strong' },
          { label: '排队中', value: summary?.counts.pending, tone: 'text-warn-strong' },
          { label: '取消中', value: summary?.counts.cancel_requested, tone: 'text-warn-strong' },
          { label: '失败', value: summary?.counts.failed, tone: 'text-danger-strong' },
        ]).map((item) => (
          <div key={item.label} className="rounded-md border border-ink-200 bg-surface p-3.5">
            <p className="text-xs text-ink-400">{item.label}</p>
            <p className={`tnum mt-1 text-lg font-semibold ${item.tone}`}>{item.value ?? 0}</p>
          </div>
        ))}
      </div>

      {user?.is_admin && observability && (
        <OperationsPanel data={observability} />
      )}

      {/* Filters */}
      <Card className="mb-4" padding="md">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Select
            label="状态"
            aria-label="按状态筛选任务"
            value={statusFilter}
            onChange={(event) => { setStatusFilter(event.target.value); setPage(1); }}
            options={statusOptions}
          />
          <Select
            label="类型"
            aria-label="按类型筛选任务"
            value={typeFilter}
            onChange={(event) => { setTypeFilter(event.target.value); setPage(1); }}
            options={typeOptions}
          />
        </div>
      </Card>

      {error && (
        <Banner
          variant="danger"
          className="mb-4"
          action={<Button variant="secondary" size="sm" onClick={() => void load()}>重试</Button>}
        >
          {error}
        </Banner>
      )}
      {actionError && (
        <Banner variant="danger" className="mb-4">{actionError}</Banner>
      )}

      <Card padding="none">
        {jobs.length === 0 && !loading ? (
          <EmptyState icon="jobs" title="暂无任务" description="后台任务提交后会出现在这里。" />
        ) : (
          <Table
            columns={columns}
            data={jobs}
            keyField="job_uuid"
            loading={loading}
            onRowClick={(job) => void openJob(job)}
            caption="后台任务列表"
            minWidth="1024px"
          />
        )}
        <div className="border-t border-ink-100 px-4 py-3">
          <Pagination page={page} total={total} limit={PAGE_SIZE} onChange={setPage} />
        </div>
      </Card>

      {/* Detail drawer */}
      {selectedJob && (
        <JobDrawer
          job={selectedJob}
          onClose={() => setSelectedJob(null)}
          onCancel={() => void handleCancel(selectedJob)}
          onRetry={() => void handleRetry(selectedJob)}
          onOpenResource={() => {
            const path = getJobResourcePath(selectedJob);
            if (path) navigate(path);
          }}
          canUseAi={canUseAi}
        />
      )}
    </div>
  );
}

function JobDrawer({
  job,
  onClose,
  onCancel,
  onRetry,
  onOpenResource,
  canUseAi,
}: {
  job: Job;
  onClose: () => void;
  onCancel: () => void;
  onRetry: () => void;
  onOpenResource: () => void;
  canUseAi: boolean;
}) {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [onClose]);

  const resourcePath = getJobResourcePath(job);
  const label = job.display_name || jobTypeLabel[job.job_type] || job.job_type;
  const showDiagnosis =
    canUseAi && job.status === 'failed' && Boolean(job.error)
    && job.resource_type === 'experiment' && Number(job.resource_id) > 0;

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <button
        type="button"
        aria-label="关闭任务详情"
        className="absolute inset-0 bg-ink-950/40"
        onClick={onClose}
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={`任务详情 ${label}`}
        className="relative flex h-full w-full max-w-2xl flex-col border-l border-ink-200 bg-surface shadow-overlay"
      >
        <header className="flex items-start justify-between gap-3 border-b border-ink-200 px-5 py-4">
          <div className="min-w-0">
            <h2 className="truncate text-base font-semibold text-ink-900">{label}</h2>
            <p className="mt-0.5 break-all font-mono text-2xs text-ink-400">{job.job_uuid}</p>
          </div>
          <button
            type="button"
            aria-label="关闭"
            onClick={onClose}
            className="rounded p-1.5 text-ink-400 hover:bg-ink-100 hover:text-ink-700"
          >
            <Icon name="close" className="h-5 w-5" />
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-5 py-4 scrollbar-thin">
          <DescriptionList
            columns={3}
            items={[
              {
                label: '状态',
                value: (
                  <StatusTag variant={STATUS_VARIANT[job.status] ?? 'neutral'}>
                    {jobStatusLabel[job.status]}
                  </StatusTag>
                ),
              },
              { label: '任务类型', value: jobTypeLabel[job.job_type] || job.job_type },
              { label: '提交时间', value: formatJobDate(job.created_at), mono: true },
              { label: '开始时间', value: formatJobDate(job.started_at), mono: true },
              { label: '运行耗时', value: formatJobDuration(job), mono: true },
              { label: '当前阶段', value: job.progress_message || job.current_stage || '-' },
            ]}
          />

          {job.error && (
            <div className="mt-4">
              <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-danger-strong">失败原因</p>
              <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded bg-danger-bg p-3 font-mono text-xs leading-5 text-danger-strong scrollbar-thin">
                {job.error}
              </pre>
            </div>
          )}

          {showDiagnosis && (
            <div className="mt-4">
              <AiDiagnosisCard
                onDiagnose={() => diagnoseError(Number(job.resource_id), job.error ?? '')}
                scopeKey={`job:${job.job_uuid}`}
              />
            </div>
          )}

          <div className="mt-5">
            <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-400">任务参数</p>
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded bg-ink-50 p-3 font-mono text-xs leading-5 text-ink-600 scrollbar-thin">
              {job.params && Object.keys(job.params).length > 0
                ? JSON.stringify(job.params, null, 2)
                : '无'}
            </pre>
          </div>
          <div className="mt-4">
            <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-400">执行结果</p>
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded bg-ink-50 p-3 font-mono text-xs leading-5 text-ink-600 scrollbar-thin">
              {job.result ? JSON.stringify(job.result, null, 2) : '无'}
            </pre>
          </div>

          {job.events && job.events.length > 0 && (
            <div className="mt-5">
              <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-400">状态记录</p>
              <ol className="space-y-2.5 border-l-2 border-ink-100 pl-4">
                {job.events.map((event, index) => (
                  <li key={index} className="relative">
                    <span className="absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full border border-ink-300 bg-surface" aria-hidden />
                    <div className="flex flex-wrap items-center gap-2">
                      <StatusTag variant={STATUS_VARIANT[event.status] ?? 'neutral'}>
                        {jobStatusLabel[event.status]}
                      </StatusTag>
                      <span className="tnum text-2xs text-ink-400">{formatJobDate(event.created_at)}</span>
                    </div>
                    <p className="mt-0.5 text-xs text-ink-500">{event.message || event.stage || '-'}</p>
                  </li>
                ))}
              </ol>
            </div>
          )}
        </div>

        <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-ink-200 px-5 py-3">
          {resourcePath && (
            <Button variant="secondary" size="sm" onClick={onOpenResource}>
              查看关联对象
            </Button>
          )}
          {['pending', 'running'].includes(job.status) && (
            <Button variant="danger" size="sm" onClick={onCancel}>
              取消任务
            </Button>
          )}
          {['failed', 'cancelled'].includes(job.status) && (
            <Button size="sm" onClick={onRetry}>
              重试任务
            </Button>
          )}
        </footer>
      </aside>
    </div>
  );
}
