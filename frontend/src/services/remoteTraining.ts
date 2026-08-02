import api, { API_BASE_URL } from './api';
import type { ApiResponse } from '../types/api';
import type {
  CreateRemoteTrainingTaskBody,
  RemoteTrainingTask,
  RemoteTrainingTaskCredential,
  RemoteTrainingTaskList,
} from '../types/remoteTraining';

type TaskEnvelope = RemoteTrainingTask | { task: RemoteTrainingTask };
type TaskListEnvelope = RemoteTrainingTask[] | RemoteTrainingTaskList;
type CredentialEnvelope =
  | RemoteTrainingTaskCredential
  | { task: RemoteTrainingTaskCredential };

function unwrap<T>(response: { data: ApiResponse<T> }): T {
  const payload = response.data.data;
  if (payload === undefined || payload === null) {
    throw new Error(response.data.detail || response.data.error || '远程训练服务返回了空响应');
  }
  return payload;
}

function unwrapTask(payload: TaskEnvelope): RemoteTrainingTask {
  return 'task' in payload ? payload.task : payload;
}

export async function createRemoteTrainingTask(
  body: CreateRemoteTrainingTaskBody,
): Promise<RemoteTrainingTaskCredential> {
  const response = await api.post<ApiResponse<CredentialEnvelope>>(
    '/api/remote-training/tasks',
    body,
    { timeout: 10 * 60 * 1000 },
  );
  const payload = unwrap(response);
  return 'task' in payload ? payload.task : payload;
}

export async function listRemoteTrainingTasks(
  experimentId: number,
): Promise<RemoteTrainingTask[]> {
  const response = await api.get<ApiResponse<TaskListEnvelope>>(
    '/api/remote-training/tasks',
    { params: { experiment_id: experimentId } },
  );
  const payload = unwrap(response);
  return Array.isArray(payload) ? payload : payload.items;
}

export async function getRemoteTrainingTask(
  taskUuid: string,
): Promise<RemoteTrainingTask> {
  const response = await api.get<ApiResponse<TaskEnvelope>>(
    `/api/remote-training/tasks/${encodeURIComponent(taskUuid)}`,
  );
  return unwrapTask(unwrap(response));
}

export async function cancelRemoteTrainingTask(
  taskUuid: string,
): Promise<RemoteTrainingTask> {
  const response = await api.post<ApiResponse<TaskEnvelope>>(
    `/api/remote-training/tasks/${encodeURIComponent(taskUuid)}/cancel`,
  );
  return unwrapTask(unwrap(response));
}

export function buildRemoteTrainingClientCommand(
  credential: RemoteTrainingTaskCredential,
): string | null {
  const suppliedCommand = credential.client_command?.trim();
  if (
    suppliedCommand
    && (!credential.task_token || !suppliedCommand.includes(credential.task_token))
  ) {
    return suppliedCommand;
  }

  const server = API_BASE_URL.replace(/\/+$/, '');
  return [
    'python scripts/remote_train_client.py',
    `--server "${server}"`,
    `--task-id "${credential.task_uuid}"`,
  ].join(' ');
}
