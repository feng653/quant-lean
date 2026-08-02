import Badge from '../shared/Badge';
import type { RemoteTrainingTask } from '../../types/remoteTraining';
import {
  displayMetricValue,
  remoteTrainingDeviceLabel,
  remoteTrainingError,
  remoteTrainingMetrics,
  remoteTrainingProgressPercent,
  remoteTrainingStatusLabel,
} from './remoteTrainingView';

const statusVariant = (status: string) => {
  if (status === 'completed') return 'success' as const;
  if (status === 'failed' || status === 'expired') return 'danger' as const;
  if (status === 'cancelled') return 'default' as const;
  if (status === 'cancel_requested') return 'warning' as const;
  return 'info' as const;
};

export default function RemoteTrainingTaskDetails({
  task,
}: {
  task: RemoteTrainingTask;
}) {
  const percent = remoteTrainingProgressPercent(task.progress);
  const error = remoteTrainingError(task);
  const metrics = Object.entries(remoteTrainingMetrics(task));

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-lg bg-gray-50 p-3">
          <p className="text-xs text-gray-500">任务状态</p>
          <div className="mt-1">
            <Badge size="sm" variant={statusVariant(task.status)}>
              {remoteTrainingStatusLabel(task.status)}
            </Badge>
          </div>
        </div>
        <div className="rounded-lg bg-gray-50 p-3">
          <p className="text-xs text-gray-500">Windows 客户端</p>
          <p className="mt-1 truncate text-sm font-medium text-gray-800">
            {task.worker_name || '尚未领取'}
          </p>
        </div>
        <div className="rounded-lg bg-gray-50 p-3 sm:col-span-2">
          <p className="text-xs text-gray-500">训练设备</p>
          <p className="mt-1 break-words text-sm font-medium text-gray-800">
            {remoteTrainingDeviceLabel(task)}
          </p>
        </div>
      </div>

      <div>
        <div className="mb-1.5 flex items-center justify-between text-xs">
          <span className="text-gray-500">
            {task.progress_message || remoteTrainingStatusLabel(task.status)}
          </span>
          <span className="font-mono text-gray-700">{percent}%</span>
        </div>
        <div
          className="h-2 overflow-hidden rounded-full bg-gray-100"
          role="progressbar"
          aria-label="远程训练进度"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent}
        >
          <div
            className="h-full rounded-full bg-primary-600 transition-[width]"
            style={{ width: `${percent}%` }}
          />
        </div>
      </div>

      {metrics.length > 0 && (
        <div>
          <h4 className="text-sm font-medium text-gray-800">训练指标</h4>
          <dl className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {metrics.map(([name, value]) => (
              <div key={name} className="rounded-lg border border-gray-100 px-3 py-2">
                <dt className="break-all font-mono text-[11px] text-gray-500">{name}</dt>
                <dd className="mt-0.5 break-all text-sm font-medium text-gray-800">
                  {displayMetricValue(value)}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      {error && (
        <div
          className="rounded-lg border border-red-200 bg-red-50 p-3"
          role="alert"
        >
          <p className="text-sm font-medium text-red-700">训练失败</p>
          <p className="mt-1 whitespace-pre-wrap break-words text-xs text-red-700">
            {error}
          </p>
        </div>
      )}
    </div>
  );
}
