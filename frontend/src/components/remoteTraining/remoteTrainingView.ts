import type {
  RemoteTrainingDevice,
  RemoteTrainingTask,
  RemoteTrainingTaskStatus,
} from '../../types/remoteTraining';

export const ACTIVE_REMOTE_TRAINING_STATUSES = new Set<RemoteTrainingTaskStatus>([
  'created',
  'pending',
  'waiting_for_worker',
  'claimed',
  'running',
  'uploading',
  'cancel_requested',
]);

const STATUS_LABELS: Record<string, string> = {
  created: '等待客户端',
  pending: '等待客户端',
  waiting_for_worker: '等待客户端',
  claimed: '客户端已领取',
  running: '训练中',
  uploading: '正在上传结果',
  cancel_requested: '正在取消',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  expired: '领取令牌已过期',
};

export function remoteTrainingStatusLabel(status: RemoteTrainingTaskStatus): string {
  return STATUS_LABELS[status] ?? status;
}

export function remoteTrainingProgressPercent(progress?: number | null): number {
  if (typeof progress !== 'number' || !Number.isFinite(progress)) return 0;
  const normalized = progress <= 1 ? progress * 100 : progress;
  return Math.max(0, Math.min(100, Math.round(normalized)));
}

export function remoteTrainingError(task: RemoteTrainingTask): string | null {
  return task.error_message || task.error || null;
}

export function remoteTrainingMetrics(
  task: RemoteTrainingTask,
): Record<string, unknown> {
  return task.training_metrics ?? task.metrics ?? task.report_json?.metrics ?? {};
}

export function remoteTrainingDeviceLabel(task: RemoteTrainingTask): string {
  if (task.device_name || task.device_type) {
    return [task.device_name, task.device_type].filter(Boolean).join(' · ');
  }
  if (typeof task.device === 'string') return task.device;
  if (task.device) {
    const device = task.device as RemoteTrainingDevice;
    const accelerator = device.accelerator ?? device.type;
    return [device.name, accelerator, device.platform].filter(Boolean).join(' · ');
  }
  if (task.report_json?.device_actual) {
    const runtime = task.report_json.runtime;
    const platform = runtime?.platform
      || [runtime?.os, runtime?.os_release].filter(Boolean).join(' ');
    return [task.report_json.device_actual, platform].filter(Boolean).join(' · ');
  }
  return '尚未上报';
}

export function latestRemoteTrainingTask(
  tasks: RemoteTrainingTask[],
): RemoteTrainingTask | null {
  return [...tasks].sort((left, right) => {
    const leftTime = Date.parse(left.updated_at || left.created_at || '') || 0;
    const rightTime = Date.parse(right.updated_at || right.created_at || '') || 0;
    return rightTime - leftTime;
  })[0] ?? null;
}

export function displayMetricValue(value: unknown): string {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(4);
  }
  if (typeof value === 'string' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value);
}
