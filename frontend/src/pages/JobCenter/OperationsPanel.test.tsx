import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { JobObservability } from '../../types/job';
import OperationsPanel from './OperationsPanel';

const DATA: JobObservability = {
  schema_version: 'operations-observability/v1',
  window_hours: 24,
  retention_hours: 168,
  jobs: {
    sample_count: 3,
    by_type: {
      factor_research: {
        submitted: 3,
        completed: 2,
        failed: 1,
        cancelled: 0,
        active: 0,
        success_rate: 2 / 3,
        failure_rate: 1 / 3,
        cancel_rate: 0,
        queue_wait_seconds: { p50: 2, p95: 8 },
        run_duration_seconds: { p50: 20, p95: 50 },
      },
    },
    queue_wait_seconds: { p50: 2, p95: 8 },
    run_duration_seconds: { p50: 20, p95: 50 },
  },
  data_refresh: {
    recent: [{
      status: 'running',
      stage: 'validating',
      progress: 0.75,
      message: '正在校验',
      updated_at: '2026-07-31T00:00:00Z',
    }],
  },
  cache_quality: {
    schema_version: 'cache-quality-summary/v1',
    counts: {
      total: 5,
      research_ready: 3,
      execution_ready: 1,
      quality_ready: 4,
      legacy_or_invalid: 1,
      missing_quality: 1,
    },
  },
  slo: {
    schema_version: 'operations-slo/v1',
    objectives: {
      job_success_rate: {
        target: 0.95,
        actual: 2 / 3,
        minimum_samples: 5,
        sample_count: 6,
        met: false,
      },
      queue_wait_p95_seconds: {
        target_max: 300,
        actual: 8,
        met: true,
      },
    },
    alerting: {
      confirmations_required: 2,
      cooldown_seconds: 900,
      evaluation_interval_seconds: 60,
      states: {
        job_success_rate: {
          status: 'breaching',
          pending_status: null,
          consecutive_observations: 0,
          last_transition_at: '2026-07-31T00:00:00Z',
          updated_at: '2026-07-31T00:00:00Z',
        },
      },
      recent: [{
        objective: 'job_success_rate',
        transition: 'breach',
        actual: 2 / 3,
        threshold: 0.95,
        window_hours: 24,
        notification_emitted: true,
        created_at: '2026-07-31T00:00:00Z',
      }],
    },
  },
  events: [],
  worker: {
    online: true,
    capacity: 1,
    desired_capacity: 1,
    configured_max: 2,
    running_slots: 1,
    degraded: true,
    reasons: ['memory_budget_exhausted'],
    execution_mode: 'hybrid_spawn_factor_research',
    leader: true,
    pause_heavy: true,
    admission_mode: 'pause_heavy',
    metrics: {
      cpu_count: 8,
      normalized_load: 0.81,
      memory_available_mb: 700,
      disk_free_mb: 9000,
      source: 'test',
    },
  },
};

describe('OperationsPanel', () => {
  it('shows bounded SLO, resource admission and cache quality evidence', () => {
    const html = renderToStaticMarkup(<OperationsPanel data={DATA} />);

    expect(html).toContain('服务 SLO 与资源预算');
    expect(html).toContain('重任务暂停领取');
    expect(html).toContain('内存硬预算已耗尽');
    expect(html).toContain('3/5 研究可用');
    expect(html).toContain('因子研究');
    expect(html).toContain('66.7%');
    expect(html).toContain('validating 75%');
    expect(html).toContain('任务成功率');
    expect(html).toContain('越界');
    expect(html).toContain('最近 SLO 告警');
    expect(html).toContain('连续 2 次确认');
    expect(html).toContain('24 小时窗');
    expect(html).not.toContain('job_uuid');
  });
});
