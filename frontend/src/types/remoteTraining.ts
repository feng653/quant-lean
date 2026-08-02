export type RemoteTrainingTaskStatus =
  | 'created'
  | 'pending'
  | 'waiting_for_worker'
  | 'claimed'
  | 'running'
  | 'uploading'
  | 'cancel_requested'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'expired'
  | (string & {});

export interface RemoteTrainingDevice {
  name?: string | null;
  type?: string | null;
  platform?: string | null;
  accelerator?: string | null;
}

export interface RemoteTrainingTask {
  task_uuid: string;
  experiment_id: number;
  status: RemoteTrainingTaskStatus;
  progress?: number | null;
  progress_message?: string | null;
  worker_name?: string | null;
  device?: string | RemoteTrainingDevice | null;
  device_name?: string | null;
  device_type?: string | null;
  metrics?: Record<string, unknown> | null;
  training_metrics?: Record<string, unknown> | null;
  report_json?: {
    device_actual?: string | null;
    metrics?: Record<string, unknown> | null;
    runtime?: {
      platform?: string | null;
      os?: string | null;
      os_release?: string | null;
    } | null;
  } | null;
  error?: string | null;
  error_message?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
}

export interface CreateRemoteTrainingTaskBody {
  experiment_id: number;
  train_start?: string;
  train_end?: string;
}

export interface RemoteTrainingTaskCredential extends Partial<RemoteTrainingTask> {
  task_uuid: string;
  task_token?: string;
  token_expires_at?: string;
  client_command?: string;
}

export interface RemoteTrainingTaskList {
  items: RemoteTrainingTask[];
  total?: number;
}
