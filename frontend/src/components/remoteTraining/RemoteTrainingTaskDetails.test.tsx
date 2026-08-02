import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import RemoteTrainingCredential from './RemoteTrainingCredential';
import RemoteTrainingTaskDetails from './RemoteTrainingTaskDetails';
import {
  latestRemoteTrainingTask,
  remoteTrainingProgressPercent,
} from './remoteTrainingView';

describe('remote training components', () => {
  it('renders running progress, device, and metrics accessibly', () => {
    const html = renderToStaticMarkup(
      <RemoteTrainingTaskDetails
        task={{
          task_uuid: 'task-1',
          experiment_id: 7,
          status: 'running',
          progress: 0.64,
          progress_message: '正在拟合第 4 个周期',
          worker_name: 'Research-PC',
          device: { type: 'CUDA', name: 'RTX 4090', platform: 'Windows 11' },
          metrics: { validation_loss: 0.0182, epochs: 12 },
        }}
      />,
    );

    expect(html).toContain('训练中');
    expect(html).toContain('64%');
    expect(html).toContain('RTX 4090');
    expect(html).toContain('validation_loss');
    expect(html).toContain('role="progressbar"');
  });

  it('renders a failed state with the reported reason', () => {
    const html = renderToStaticMarkup(
      <RemoteTrainingTaskDetails
        task={{
          task_uuid: 'task-2',
          experiment_id: 7,
          status: 'failed',
          error_message: '显存不足',
        }}
      />,
    );

    expect(html).toContain('失败');
    expect(html).toContain('显存不足');
    expect(html).toContain('role="alert"');
  });

  it('shows the one-time credential without embedding it in the command', () => {
    const html = renderToStaticMarkup(
      <RemoteTrainingCredential
        credential={{ task_uuid: 'task-3', task_token: 'only-show-once' }}
        onDismiss={() => {}}
      />,
    );

    expect(html).toContain('only-show-once');
    expect(html).toContain('客户端会安全提示');
    expect(html).not.toContain('--token');
  });

  it('normalizes progress and selects the most recently updated task', () => {
    expect(remoteTrainingProgressPercent(0.325)).toBe(33);
    expect(remoteTrainingProgressPercent(120)).toBe(100);
    expect(latestRemoteTrainingTask([
      {
        task_uuid: 'older',
        experiment_id: 7,
        status: 'failed',
        updated_at: '2026-07-28T08:00:00Z',
      },
      {
        task_uuid: 'newer',
        experiment_id: 7,
        status: 'running',
        updated_at: '2026-07-28T09:00:00Z',
      },
    ])?.task_uuid).toBe('newer');
  });
});
