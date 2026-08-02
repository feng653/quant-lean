export type JobStatus =
  | 'pending'
  | 'running'
  | 'cancel_requested'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface JobEvent {
  status: JobStatus;
  progress: number;
  stage?: string | null;
  message?: string | null;
  created_at: string;
}

export interface Job {
  id: number;
  job_uuid: string;
  job_type: string;
  display_name?: string | null;
  params: Record<string, unknown>;
  status: JobStatus;
  progress: number;
  progress_message?: string | null;
  current_stage?: string | null;
  result?: Record<string, unknown> | null;
  error?: string | null;
  resource_type?: string | null;
  resource_id?: string | null;
  parent_job_uuid?: string | null;
  attempt: number;
  queue_position?: number | null;
  queue_reason?: string | null;
  worker_id?: string | null;
  created_at: string;
  updated_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  cancel_requested_at?: string | null;
  user_id?: number | null;
  events?: JobEvent[];
}

export interface JobSummary {
  counts: Record<JobStatus, number>;
  active: number;
  worker: {
    online: boolean;
    capacity: number;
    desired_capacity: number;
    configured_max: number;
    running_slots: number;
    degraded: boolean;
    reasons: string[];
    execution_mode: 'hybrid_spawn_factor_research';
    pause_heavy?: boolean;
    admission_mode?: 'normal' | 'pause_heavy' | 'starting';
    budgets?: {
      cpu: { scale_up_max: number; heavy_pause_at: number };
      memory: {
        scale_up_min_available_mb: number;
        heavy_pause_min_available_mb: number;
        heavy_pause_used_ratio: number;
      };
      io: { min_disk_free_mb: number; max_pressure: number };
      cpu_threads_per_heavy_job: number;
    };
    leader: boolean;
    metrics?: {
      cpu_count: number;
      load_1m?: number | null;
      normalized_load?: number | null;
      memory_total_mb?: number | null;
      memory_available_mb?: number | null;
      memory_used_ratio?: number | null;
      swap_used_mb?: number | null;
      disk_free_mb?: number | null;
      io_pressure?: number | null;
      io_source?: string | null;
      source: string;
      error?: string | null;
    } | null;
    started_at?: string | null;
    heartbeat_at?: string | null;
  };
}

export interface JobList {
  items: Job[];
  total: number;
  page: number;
  page_size: number;
}

export interface JobUpdateEvent {
  type: 'job_updated';
  job_uuid: string;
  job_type: string;
  status: JobStatus;
  progress: number;
  progress_message?: string | null;
  updated_at?: string | null;
}

export interface JobObservability {
  schema_version: 'operations-observability/v1';
  window_hours: number;
  retention_hours: number;
  jobs: {
    sample_count: number;
    by_type: Record<string, {
      submitted: number;
      completed: number;
      failed: number;
      cancelled: number;
      active: number;
      success_rate: number | null;
      failure_rate: number | null;
      cancel_rate: number | null;
      queue_wait_seconds: { p50: number | null; p95: number | null };
      run_duration_seconds: { p50: number | null; p95: number | null };
    }>;
    queue_wait_seconds: { p50: number | null; p95: number | null };
    run_duration_seconds: { p50: number | null; p95: number | null };
  };
  data_refresh: {
    recent: Array<{
      status: JobStatus;
      stage: string;
      progress: number;
      message: string;
      updated_at: string | null;
    }>;
  };
  cache_quality: {
    schema_version: string;
    counts: {
      total: number;
      research_ready: number;
      execution_ready: number;
      quality_ready: number;
      legacy_or_invalid: number;
      missing_quality: number;
    };
  };
  slo: {
    schema_version: 'operations-slo/v1';
    objectives: Record<string, {
      target?: number;
      target_max?: number;
      actual: number | null;
      met: boolean | null;
      minimum_samples?: number;
      sample_count?: number;
    }>;
    alerting?: {
      confirmations_required: number;
      cooldown_seconds: number;
      evaluation_interval_seconds: number;
      states: Record<string, {
        status: 'healthy' | 'breaching';
        pending_status: 'healthy' | 'breaching' | null;
        consecutive_observations: number;
        last_transition_at: string | null;
        updated_at: string;
      }>;
      recent: Array<{
        objective: string;
        transition: 'breach' | 'recovery';
        actual: number | null;
        threshold: number;
        window_hours: number;
        notification_emitted: boolean;
        created_at: string;
      }>;
    };
  };
  events: Array<{
    event_name: string;
    category: string;
    job_type: string | null;
    outcome: string | null;
    stage: string | null;
    value: number;
  }>;
  worker: JobSummary['worker'];
}
