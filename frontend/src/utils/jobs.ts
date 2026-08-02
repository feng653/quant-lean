import type { Job, JobStatus } from '../types/job';
import { formatBackendDateTime, parseBackendTimestamp } from './datetime';

export const jobStatusLabel: Record<JobStatus, string> = {
  pending: '排队中',
  running: '运行中',
  cancel_requested: '取消中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

export const jobTypeLabel: Record<string, string> = {
  backtest: '回测实验',
  daily_simulation: '模拟盘日结',
  simulation_backfill: '历史回放',
  data_update: '数据更新',
  retrain: '模型重训练',
  factor_research: '因子研究',
};

export const jobSchedulerReasonLabel: Record<string, string> = {
  starting: '调度器正在启动',
  scheduler_disabled: '动态扩容已关闭',
  configured_single_slot: '配置限制为单槽',
  insufficient_cpu_cores: 'CPU 核心数不足',
  cpu_load_unavailable: 'CPU 负载不可用',
  cpu_load_high: 'CPU 负载较高',
  memory_pressure_unavailable: '内存压力不可用',
  memory_used_high: '内存占用较高',
  memory_available_low: '可用内存不足',
  swap_pressure_unavailable: 'Swap 压力不可用',
  swap_used_high: 'Swap 使用较高',
  swap_growing: 'Swap 正在增长',
  scale_up_warmup: '等待稳定低负载样本',
  scheduler_lease_held_by_other_process: '其他服务进程持有调度租约',
  scheduler_lease_lost: '调度租约已丢失',
  worker_stopped: '调度器已停止',
  disk_capacity_unavailable: '磁盘容量不可用',
  disk_free_low: '磁盘剩余空间不足',
  io_pressure_high: 'I/O 压力较高',
  cpu_budget_exhausted: 'CPU 硬预算已耗尽',
  memory_budget_exhausted: '内存硬预算已耗尽',
  memory_reserve_exhausted: '内存安全余量不足',
  io_budget_exhausted: 'I/O 硬预算已耗尽',
  resource_pressure_heavy_jobs_paused: '资源压力下暂停领取重任务',
  waiting_for_capacity: '等待可用执行槽',
  scheduler_not_leader: '当前进程不是调度领导者',
  sqlite_writer_contention: 'SQLite 写入争用',
};

export function formatSchedulerReasons(reasons: string[] | undefined): string {
  if (!reasons?.length) return '负载正常';
  return reasons.map((reason) => jobSchedulerReasonLabel[reason] || reason).join('、');
}

export function formatJobDate(value?: string | null): string {
  return formatBackendDateTime(value);
}

export function formatJobDuration(job: Job, now = new Date()): string {
  const start = parseBackendTimestamp(job.started_at) ?? parseBackendTimestamp(job.created_at);
  const end = parseBackendTimestamp(job.completed_at) ?? now;
  if (!start) return '-';
  const seconds = Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  const hours = Math.floor(seconds / 3600);
  return `${hours} 小时 ${Math.floor((seconds % 3600) / 60)} 分`;
}

export function getJobResourcePath(job: Job): string | null {
  if (!job.resource_id) return null;
  if (job.resource_type === 'experiment') return `/experiment/${job.resource_id}`;
  if (job.resource_type === 'portfolio' || job.resource_type === 'deployment') return '/trading';
  if (job.resource_type === 'data_pool') return '/data';
  if (job.resource_type === 'factor_research') return '/factor-research';
  return null;
}
