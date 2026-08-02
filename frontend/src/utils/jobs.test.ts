import { describe, expect, it } from 'vitest';
import type { Job } from '../types/job';
import {
  formatJobDuration,
  formatSchedulerReasons,
  getJobResourcePath,
} from './jobs';

const baseJob: Job = {
  id: 1,
  job_uuid: 'job-1',
  job_type: 'backtest',
  params: {},
  status: 'completed',
  progress: 1,
  attempt: 1,
  created_at: '2026-07-28 01:00:00',
};

describe('job presentation', () => {
  it('formats scheduler degradation reason codes for operators', () => {
    expect(formatSchedulerReasons(['memory_available_low', 'cpu_load_high']))
      .toBe('可用内存不足、CPU 负载较高');
    expect(formatSchedulerReasons([])).toBe('负载正常');
  });

  it('links experiment jobs to their experiment detail', () => {
    expect(
      getJobResourcePath({ ...baseJob, resource_type: 'experiment', resource_id: '42' })
    ).toBe('/experiment/42');
    expect(
      getJobResourcePath({
        ...baseJob,
        job_type: 'factor_research',
        resource_type: 'factor_research',
        resource_id: 'momentum_20',
      })
    ).toBe('/factor-research');
  });

  it('formats a completed task duration', () => {
    expect(
      formatJobDuration({
        ...baseJob,
        started_at: '2026-07-28 01:00:00',
        completed_at: '2026-07-28 01:02:05',
      })
    ).toBe('2 分 5 秒');
  });
});
