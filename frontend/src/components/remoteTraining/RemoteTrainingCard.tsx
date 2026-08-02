import { useCallback, useEffect, useState } from 'react';
import Button from '../shared/Button';
import Card from '../shared/Card';
import {
  cancelRemoteTrainingTask,
  createRemoteTrainingTask,
  getRemoteTrainingTask,
  listRemoteTrainingTasks,
} from '../../services/remoteTraining';
import type {
  RemoteTrainingTask,
  RemoteTrainingTaskCredential,
} from '../../types/remoteTraining';
import RemoteTrainingCredential from './RemoteTrainingCredential';
import RemoteTrainingTaskDetails from './RemoteTrainingTaskDetails';
import {
  ACTIVE_REMOTE_TRAINING_STATUSES,
  latestRemoteTrainingTask,
} from './remoteTrainingView';

interface RemoteTrainingCardProps {
  experimentId: number;
  trainStart?: string | null;
  trainEnd?: string | null;
  canManage?: boolean;
}

export default function RemoteTrainingCard({
  experimentId,
  trainStart,
  trainEnd,
  canManage = true,
}: RemoteTrainingCardProps) {
  const [task, setTask] = useState<RemoteTrainingTask | null>(null);
  const [credential, setCredential] = useState<RemoteTrainingTaskCredential | null>(null);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      if (task?.task_uuid) {
        setTask(await getRemoteTrainingTask(task.task_uuid));
      } else {
        setTask(latestRemoteTrainingTask(await listRemoteTrainingTasks(experimentId)));
      }
      setError(null);
    } catch (requestError) {
      if (!quiet) {
        setError(
          requestError instanceof Error ? requestError.message : '远程训练任务加载失败',
        );
      }
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [experimentId, task?.task_uuid]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!task || !ACTIVE_REMOTE_TRAINING_STATUSES.has(task.status)) return undefined;
    const timer = window.setInterval(() => void refresh(true), 5000);
    return () => window.clearInterval(timer);
  }, [refresh, task]);

  const createTask = async () => {
    setCreating(true);
    setError(null);
    try {
      const created = await createRemoteTrainingTask({
        experiment_id: experimentId,
        ...(trainStart ? { train_start: trainStart } : {}),
        ...(trainEnd ? { train_end: trainEnd } : {}),
      });
      setCredential(created);
      setTask({
        task_uuid: created.task_uuid,
        experiment_id: experimentId,
        status: created.status || 'created',
        progress: created.progress ?? 0,
        progress_message: created.progress_message || '等待 Windows 客户端领取',
        report_json: created.report_json,
      });
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : '远程训练任务创建失败',
      );
    } finally {
      setCreating(false);
    }
  };

  const cancelTask = async () => {
    if (!task) return;
    setCancelling(true);
    setError(null);
    try {
      setTask(await cancelRemoteTrainingTask(task.task_uuid));
      setCredential(null);
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : '远程训练任务取消失败',
      );
    } finally {
      setCancelling(false);
    }
  };

  const active = task ? ACTIVE_REMOTE_TRAINING_STATUSES.has(task.status) : false;

  return (
    <Card className="overflow-hidden border-primary-100">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 gap-3">
          <span
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-primary-50 text-primary-700"
            aria-hidden="true"
          >
            <svg viewBox="0 0 24 24" fill="none" className="h-5 w-5" stroke="currentColor">
              <path
                d="M8 18h8m-7-3h6a4 4 0 0 0 4-4V8a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v3a4 4 0 0 0 4 4Zm3 0v3"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <div>
            <h3 className="text-base font-semibold text-gray-900">远程训练</h3>
            <p className="mt-1 max-w-2xl text-sm leading-6 text-gray-500">
              在受信任的 Windows 客户端本地读取数据并训练，网页负责创建任务、查看进度和接收训练结果。
            </p>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <Button
            size="sm"
            variant="secondary"
            disabled={loading}
            onClick={() => void refresh()}
          >
            刷新状态
          </Button>
          {canManage && !active && (
            <Button size="sm" loading={creating} onClick={() => void createTask()}>
              创建远程训练
            </Button>
          )}
          {canManage && active && task?.status !== 'cancel_requested' && (
            <Button
              size="sm"
              variant="danger"
              loading={cancelling}
              onClick={() => void cancelTask()}
            >
              取消任务
            </Button>
          )}
        </div>
      </div>

      {error && (
        <div className="mt-4 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700" role="alert">
          {error}
        </div>
      )}

      {creating && (
        <div
          className="mt-4 rounded-lg border border-primary-100 bg-primary-50 px-3 py-2 text-sm text-primary-800"
          role="status"
          aria-live="polite"
        >
          正在生成不可变数据快照，首次可能较久，请勿关闭页面。
        </div>
      )}

      {!canManage && (
        <p className="mt-4 rounded-lg bg-gray-50 px-3 py-2 text-xs text-gray-500">
          你可以查看远程训练状态，但当前账号没有创建或取消训练任务的权限。
        </p>
      )}

      {credential && (
        <div className="mt-5">
          <RemoteTrainingCredential
            credential={credential}
            onDismiss={() => setCredential(null)}
          />
        </div>
      )}

      <div className="mt-5 border-t border-gray-100 pt-5" aria-live="polite">
        {loading && !task ? (
          <p className="text-sm text-gray-500">正在加载远程训练任务…</p>
        ) : task ? (
          <RemoteTrainingTaskDetails task={task} />
        ) : (
          <div className="rounded-xl border border-dashed border-gray-200 bg-gray-50/60 p-5">
            <p className="text-sm font-medium text-gray-700">尚未创建远程训练任务</p>
            <p className="mt-1 text-xs leading-5 text-gray-500">
              创建后请立即保存一次性令牌和 PowerShell 命令；页面不会持久化这些凭据。
            </p>
          </div>
        )}
      </div>
    </Card>
  );
}
