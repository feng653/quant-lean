import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router';
import Sidebar from './Sidebar';
import { useAuthStore } from '../../store/authStore';
import { useJobEvents } from '../../hooks/useWebSocket';
import { getJobSummary, listJobs } from '../../services/jobs';
import type { Job, JobStatus, JobSummary } from '../../types/job';
import { jobStatusLabel, jobTypeLabel } from '../../utils/jobs';
import Icon from '../shared/Icon';
import ProgressBar from '../shared/ProgressBar';
import StatusTag from '../shared/StatusTag';
import type { StatusVariant } from '../shared/StatusTag';

interface AppShellProps {
  children: ReactNode;
}

const JOB_STATUS_VARIANT: Record<JobStatus, StatusVariant> = {
  pending: 'queued',
  running: 'running',
  cancel_requested: 'warning',
  completed: 'verified',
  failed: 'error',
  cancelled: 'neutral',
};

export default function AppShell({ children }: AppShellProps) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [showUserMenu, setShowUserMenu] = useState(false);
  const [showJobs, setShowJobs] = useState(false);
  const [jobSummary, setJobSummary] = useState<JobSummary | null>(null);
  const [recentJobs, setRecentJobs] = useState<Job[]>([]);

  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const navigate = useNavigate();

  const refreshJobs = useCallback(async () => {
    try {
      const [summary, jobs] = await Promise.all([
        getJobSummary(),
        listJobs({ page: 1, page_size: 6 }),
      ]);
      setJobSummary(summary);
      setRecentJobs(jobs.items);
    } catch {
      // The main application remains usable when the status endpoint is unavailable.
    }
  }, []);

  useEffect(() => {
    void refreshJobs();
    const timer = window.setInterval(() => void refreshJobs(), 10000);
    return () => window.clearInterval(timer);
  }, [refreshJobs]);

  const onJobChange = useCallback(() => {
    void refreshJobs();
  }, [refreshJobs]);
  useJobEvents(onJobChange);

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  return (
    <div className="flex h-dvh overflow-hidden bg-paper">
      {/* Desktop sidebar */}
      <div className="hidden h-full min-h-0 md:flex">
        <Sidebar collapsed={sidebarCollapsed} onToggle={() => setSidebarCollapsed(!sidebarCollapsed)} />
      </div>

      {/* Mobile drawer */}
      {mobileSidebarOpen && (
        <div className="fixed inset-0 z-[60] flex md:hidden">
          <button
            type="button"
            aria-label="关闭导航"
            className="absolute inset-0 bg-ink-950/40"
            onClick={() => setMobileSidebarOpen(false)}
          />
          <div className="relative flex h-full">
            <Sidebar
              collapsed={false}
              onToggle={() => setMobileSidebarOpen(false)}
              onNavigate={() => setMobileSidebarOpen(false)}
            />
          </div>
        </div>
      )}

      {/* Main column */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        {/* Persistent platform safety strip — always visible, never dismissible. */}
        <div className="flex items-center gap-2 bg-ink-900 px-4 py-1.5 text-ink-100">
          <Icon name="shield" className="h-4 w-4 shrink-0 text-warn-border" aria-hidden />
          <p className="min-w-0 flex-1 truncate text-xs leading-5">
            <span className="font-semibold">平台未通过实盘认证</span>
            <span className="hidden sm:inline"> · 仅限研究与模拟交易，禁止接入真实资金账户</span>
          </p>
          <Link
            to="/trading/brokers"
            className="shrink-0 text-xs font-medium text-accent-200 underline-offset-2 hover:underline"
          >
            查看实盘门禁
          </Link>
        </div>

        {/* Header */}
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-ink-200 bg-surface px-4">
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="rounded p-2 text-ink-600 hover:bg-ink-100 md:hidden"
              aria-label="打开导航"
              onClick={() => setMobileSidebarOpen(true)}
            >
              <Icon name="menu" className="h-5 w-5" />
            </button>
          </div>

          <div className="flex items-center gap-1.5">
            {/* Global job status */}
            <button
              type="button"
              onClick={() => setShowJobs(!showJobs)}
              aria-expanded={showJobs}
              aria-label="后台任务状态"
              className="relative flex items-center gap-2 rounded px-2.5 py-2 text-sm text-ink-600 transition-colors hover:bg-ink-100"
            >
              <Icon name="jobs" className="h-5 w-5" />
              <span className="hidden sm:inline tnum">
                {jobSummary?.active ? `${jobSummary.active} 个任务` : '任务'}
              </span>
              <span
                aria-label={jobSummary?.worker.online ? 'Worker 在线' : 'Worker 离线'}
                className={`h-2 w-2 rounded-full ${
                  jobSummary?.worker.online ? 'bg-ok-fg' : 'bg-danger-fg'
                }`}
              />
            </button>

            {/* User menu */}
            <div className="relative">
              <button
                type="button"
                onClick={() => setShowUserMenu(!showUserMenu)}
                aria-expanded={showUserMenu}
                aria-haspopup="menu"
                className="flex items-center gap-2 rounded p-1.5 transition-colors hover:bg-ink-100"
              >
                <span className="flex h-8 w-8 items-center justify-center rounded-sm bg-accent-700 text-sm font-semibold text-white">
                  {user?.display_name?.charAt(0) ?? 'U'}
                </span>
                <span className="hidden text-sm text-ink-700 sm:block">
                  {user?.display_name ?? '用户'}
                </span>
                <Icon name="chevronDown" className="hidden h-4 w-4 text-ink-400 sm:block" />
              </button>

              {showUserMenu && (
                <div role="menu" className="absolute right-0 top-full z-50 mt-1 w-52 rounded-md border border-ink-200 bg-surface py-1 shadow-menu">
                  <div className="border-b border-ink-100 px-4 py-2.5">
                    <p className="text-sm font-medium text-ink-900">{user?.display_name}</p>
                    <p className="text-xs text-ink-400">{user?.username}</p>
                    {user?.is_admin && (
                      <p className="mt-1 text-2xs font-medium text-accent-700">管理员</p>
                    )}
                  </div>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => { setShowUserMenu(false); navigate('/experiment'); }}
                    className="flex w-full items-center gap-2 px-4 py-2 text-left text-sm text-ink-700 hover:bg-ink-50"
                  >
                    <Icon name="experiment" className="h-4 w-4 text-ink-400" />
                    我的实验
                  </button>
                  {user?.is_admin && (
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => { setShowUserMenu(false); navigate('/admin'); }}
                      className="flex w-full items-center gap-2 px-4 py-2 text-left text-sm text-ink-700 hover:bg-ink-50"
                    >
                      <Icon name="admin" className="h-4 w-4 text-ink-400" />
                      用户管理
                    </button>
                  )}
                  <div className="border-t border-ink-100" />
                  <button
                    type="button"
                    role="menuitem"
                    onClick={handleLogout}
                    className="flex w-full items-center gap-2 px-4 py-2 text-left text-sm text-danger-fg hover:bg-danger-bg"
                  >
                    <Icon name="logout" className="h-4 w-4" />
                    退出登录
                  </button>
                </div>
              )}
            </div>
          </div>
        </header>

        {/* Jobs dropdown */}
        {showJobs && (
          <div className="absolute left-2 right-2 top-[4.75rem] z-50 max-h-[32rem] overflow-y-auto rounded-md border border-ink-200 bg-surface shadow-overlay sm:left-auto sm:right-4 sm:w-96">
            <div className="flex items-center justify-between border-b border-ink-100 p-3">
              <div>
                <span className="text-sm font-semibold text-ink-800">后台任务</span>
                <p className="mt-0.5 text-xs text-ink-400 tnum">
                  Worker {jobSummary?.worker.online ? '在线' : '离线'}
                  {' · '}运行 {jobSummary?.counts.running ?? 0}
                  {' · '}排队 {jobSummary?.counts.pending ?? 0}
                </p>
              </div>
              <button
                type="button"
                aria-label="关闭任务面板"
                onClick={() => setShowJobs(false)}
                className="rounded p-1 text-ink-400 hover:bg-ink-100 hover:text-ink-600"
              >
                <Icon name="close" className="h-4 w-4" />
              </button>
            </div>
            <div className="divide-y divide-ink-100">
              {recentJobs.length === 0 ? (
                <div className="p-6 text-center text-sm text-ink-400">暂无任务</div>
              ) : recentJobs.map((job) => (
                <button
                  type="button"
                  key={job.job_uuid}
                  className="block w-full px-4 py-3 text-left transition-colors hover:bg-ink-50"
                  onClick={() => {
                    setShowJobs(false);
                    navigate('/jobs');
                  }}
                >
                  <div className="flex items-center justify-between gap-3">
                    <span className="truncate text-sm font-medium text-ink-700">
                      {job.display_name || jobTypeLabel[job.job_type] || job.job_type}
                    </span>
                    <StatusTag variant={JOB_STATUS_VARIANT[job.status] ?? 'neutral'}>
                      {jobStatusLabel[job.status]}
                    </StatusTag>
                  </div>
                  <ProgressBar
                    value={(job.progress || 0) * 100}
                    label={`任务进度 ${jobStatusLabel[job.status]}`}
                    variant={job.status === 'failed' ? 'danger' : 'accent'}
                    showValue={false}
                    className="mt-2"
                  />
                  <p className="mt-1 truncate text-xs text-ink-400">
                    {job.progress_message || '等待状态更新'}
                  </p>
                </button>
              ))}
            </div>
            <div className="border-t border-ink-100 p-2">
              <button
                type="button"
                className="w-full rounded py-2 text-sm font-medium text-accent-700 transition-colors hover:bg-accent-50"
                onClick={() => {
                  setShowJobs(false);
                  navigate('/jobs');
                }}
              >
                查看全部任务
              </button>
            </div>
          </div>
        )}

        {/* Page content */}
        <main className="min-h-0 flex-1 overflow-auto overscroll-contain">
          <div className="mx-auto w-full max-w-content px-4 py-6 sm:px-6">
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
